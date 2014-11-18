import pysam
import sys
import re
from itertools import groupby, chain
from optparse import OptionParser
from sets import Set
import multiprocessing as mp
from intspan import intspan

def find_flanking(reads, breaks, contig_len, overlap_buffer=1, debug=False):
    tlens = []
    proper_pairs = Set()
    for read in reads:
	if read.is_proper_pair and is_fully_mapped(read, contig_len):
	    proper_pairs.add(read.qname + str(read.pos))
	    if read.tlen > 0:
		tlens.append(read.tlen)
    
    uniq_frags = Set()
    for read in [r for r in reads if not r.is_unmapped]:
	frag = None
	
	if read.qname + str(read.pos) in proper_pairs and read.tlen > 0:
	    # internal fragment
	    frag = (read.pos + read.alen, read.pnext)
	 
	if frag is not None and frag[0] <= breaks[0] - overlap_buffer and frag[1] >= breaks[1] + overlap_buffer:
	    uniq_frags.add(frag)
		
    if debug:
	for f in uniq_frags:
	    sys.stdout.write("Accepted flanking: %s %s\n" % (breaks, f))
	    
    return len(uniq_frags), tlens

def find_spanning(reads, breaks, contig_seq, overlap_buffer=1, debug=False, perfect=False):
    contig_len = len(contig_seq)
    break_seq = contig_seq[breaks[0] - 1 - overlap_buffer: breaks[1] + overlap_buffer]

    pos = Set()
    # cannot use 2 mates of same read as evidence
    names = Set()
    for read in reads:
	if read.alen:
	    if read.pos < breaks[0] - overlap_buffer and\
	       read.pos + read.alen >= breaks[1] + overlap_buffer and\
	       is_fully_mapped(read, contig_len, perfect=perfect) and\
	       is_break_region_perfect(read, break_seq, breaks, overlap_buffer):
		strand = '+' if not read.is_reverse else '-'

		start_pos = read.pos
		if read.cigar[0][0] >= 4 and read.cigar[0][0] <= 5:
		    start_pos = -1 * read.cigar[0][1]
		key = str(start_pos) + strand
		
 		if not key in pos and not read.qname in names:
		    pos.add(key)
		    names.add(read.qname)
		    
		    if debug:
			sys.stdout.write("Accepted spanning read(perfect:%s): %s %s %s %s %s\n" % (perfect, 
			                                                                        read.qname, 
			                                                                        breaks, 
			                                                                        (read.pos + 1, read.pos + read.alen),
			                                                                        read.seq,
			                                                                        strand))
			
    return len(pos)

def is_break_region_perfect(read, break_seq, breaks, overlap_buffer):
    start_idx = breaks[0] - read.pos - 1
    if read.cigar[0][0] >= 4 and read.cigar[0][0] <= 5:
	start_idx += read.cigar[0][1]
    read_break_seq = read.seq[start_idx - overlap_buffer : start_idx - overlap_buffer + len(break_seq)]
    if read_break_seq.lower() == break_seq.lower():
	return True
    else:
	return False
    
def check_tiling(reads, breaks, contig_len, debug=False):
    """Checks if there are reads tiling across breakpoints with no gaps
    
    This will be used for checking integrity of breakpoint where there is a novel
    sequence of considerable size and there is not enough flanking sequences for read pairs
    to suggest validity of fragment
    
    Args:
        reads:  (list) Pysam AlignedRead objects
        breaks: (tuple) sorted coordinates of breakpoint positions in contigs
    Returns:
        Boolean if there are reads spanning across breakpoints with no gaps
    """
    span = None
    for read in reads:
	# skip reads that is unmapped, not properly paired, or the second mate, or not fully mapped
        if not read.alen or not is_fully_mapped(read, contig_len):
            continue
	
	# skip reads that don't overlap the breakpoints
	if read.pos + read.alen < breaks[0] or read.pos > breaks[1]:
	    continue
	
	try:
	    span = span.union(intspan('%d-%d' % (read.pos + 1, read.pos + read.alen)))
	except:
	    span = intspan('%d-%d' % (read.pos + 1, read.pos + read.alen))
	    	
    if span is not None:
	break_span = intspan('%d-%d' % (breaks[0], breaks[1]))
	# make sure there is no gap in tiling reads and spans the entire breakpoint
	if len(span.ranges()) == 1 and len(span & break_span) == len(break_span):
	    return True
    
    return False
    
def is_fully_mapped(read, contig_len, perfect=False):
    """Checks to see if given read's alignment is prefect
    2 conditions: if read length is the same as aligned length
                  if edit distance is 0 (assume edit distance is given by aligner
    Args:
        read: (Pysam Aligned object)
    Returns:
        boolean of whether alignment is perfect
    """
    # take out rlen==alen condition to allow for insertions and deletion in alignments
    if not re.search('[HS]', read.cigarstring) or is_fully_mapped_to_edge(read, contig_len):
        if perfect:
            return True if read.opt('NM') == 0 else False
        return True
    
    return False

def is_fully_mapped_to_edge(read, contig_len):
    if read.cigar:
	if len(read.cigar) == 2:
	    # clipped at beginning
	    if (read.cigar[0][0] == 4 or read.cigar[0][0] == 5) and read.cigar[1][0] == 0 and read.pos == 0 and read.inferred_length == read.rlen:
		return True
	    # clipped at the end
	    elif read.cigar[0][0] == 0 and (read.cigar[1][0] or 4 and read.cigar[1][0] == 5) and read.pos + read.alen == contig_len and read.inferred_length == read.rlen:
		return True
	elif len(read.cigar) == 3 and\
	     read.pos == 0 and read.pos + read.alen == contig_len and\
	     (read.cigar[0][0] == 4 or read.cigar[0][0] == 5) and\
	     read.cigar[1][0] == 0 and\
	     (read.cigar[2][0] == 4 or read.cigar[2][0] == 5) and\
	     read.inferred_length == read.rlen:
	    return True
    
    return False

def worker(args):
    """Wrapper of extract_reads() to extract read support
    
    Args:
        args: (tuple) list of items returned by create_batches()
    """
    bam_file, contigs, coords, tids, overlap_buffer, contig_fasta_file, perfect, debug = args
    bam = pysam.Samfile(bam_file, 'rb')
    contig_fasta = pysam.Fastafile(contig_fasta_file)
    return extract_reads(bam, Set(contigs), coords, tids, overlap_buffer, contig_fasta, perfect=perfect, debug=debug)
    
def extract_reads(bam, contigs, coords, tids, overlap_buffer, contig_fasta, perfect=False, debug=False):
    """Extract read support of given list of contigs
    
    Args:
        bam: (Pysam bam handle)
        contigs: (set) contig names to get support for
        coords: (dictionary) coords[contig] = [spans]
                spans = (list) of (start, end) where 'start' and 'end' are not sorted
        tids: target ids of corresponding contigs 
    Returns:
        List of tuples:
        contig: (str) contig name
        start of break: (int) first break position
        end of break: (int) second break position
        positive spanning reads: (int) number of positive spanning reads
        negative spanning reads: (int) number of negative spanning reads
        flanking pairs: (int) number of flanking pairs
    """
    min_tid = min(tids)
    max_tid = max(tids)
    
    support = []
    count = 1
    tlens_all = []
    for key, group in groupby(bam.fetch(until_eof=True), lambda x: x.tid):        
        if key < 0 or key > max_tid:
            break
        elif key < min_tid or key not in tids:
            continue        
        else:
            contig = bam.getrname(key)
            if not contig in contigs:
                continue
            
	contig_seq = contig_fasta.fetch(contig)
	contig_len = len(contig_seq)
        results = {}
        reads = list(group)
        for breaks in coords[contig]:
            # initialization
            results[breaks] = {'spanning':0, 'flanking':0, 'depth':0, 'tiling':False}
            
            breaks_sorted = sorted(breaks)
	    results[breaks]['spanning'] = find_spanning(reads, breaks_sorted, contig_seq, overlap_buffer=overlap_buffer, perfect=perfect, debug=debug)
	    results[breaks]['tiling'] = check_tiling(reads, breaks_sorted, contig_len, debug=debug)	    
	    results[breaks]['flanking'], tlens = find_flanking(reads, breaks_sorted, contig_len, overlap_buffer=overlap_buffer, debug=debug)
	    tlens_all.extend(tlens)
                                                                                
        for breaks in results.keys():
            support.append((contig, breaks[0], breaks[1], results[breaks]['spanning'], results[breaks]['flanking'], results[breaks]['tiling']))

        count += 1        
        if count > len(contigs):
            break
            
    support.append(tlens_all)
    return support
                    
def create_batches(bam_file, coords, contigs, size, overlap_buffer, contig_fasta_file, perfect, debug=False):
    """Iterator to creates list of arguments of processes spawned
    
    Args:
        bam_file: (str) absolute path of reads-to-contigs bam file
        coords: (dictionary) coords[contig] = [spans]
                spans = (list) of (start, end) where 'start' and 'end' are not sorted
        contigs: (list) contig names
        sizes: (int) number of contigs per process
    Yeilds:
        tuple of:
        bam_file: (str) original bam file argument
        contigs: (list) contig names to be processed
        coords: (dict) original coords argument
        tids: (list) target ids of corresponding contigs 
        debug: (boolean) outputs debug statements
    """
    bam = pysam.Samfile(bam_file, 'rb')
    tids = [bam.gettid(contig) for contig in contigs]
    
    if size == 0:
        yield bam_file, contigs, coords, tids, overlap_buffer, contig_fasta_file, perfect, debug
    else:
        for i in xrange(0, len(contigs), size):
            if len(contigs) - (i + size) < size:
                yield bam_file, contigs[i:], coords, tids[i:], overlap_buffer, contig_fasta_file, perfect, debug
                break
            else:
                yield bam_file, contigs[i:i + size], coords, tids[i:i + size], overlap_buffer, contig_fasta_file, perfect, debug
            
def scan_all(coords, bam_file, contig_fasta_file, num_procs, overlap_buffer, perfect=False, debug=False):
    """Scans every read in reads to contig bam file for support
    
    Args:
        coords: (dictionary) coords[contig] = [spans]
                spans = (list) of (start, end) where 'start' and 'end' are not sorted
        bam_file: (string) absolute path of reads-to-contigs bam file
        num_procs: (int) number of concurrent processes
        debug: (boolean) prints debug statements
    Returns:
        dictionary of number of read support
        results[contig][coords] = (spanning_pos, spanning_neg, flanking)
        contig: (str)contig name
        coords: (str) original given coordinates (won't be sorted) "coord1-coord2"
        spanning_pos: (int) number unique postively spanning reads
        spanning_neg: (int) number unique postively spanning reads
        flanking: (int) number of unique flanking pairs
    """
    contigs = coords.keys()
    batches = list(create_batches(bam_file, coords, contigs, len(contigs)/num_procs, overlap_buffer, contig_fasta_file, perfect, debug=debug))
    pool = mp.Pool(processes=num_procs)
    batch_results = pool.map(worker, batches)
    pool.close()
    pool.join()
    
    results = {}
    tlens_all = []
    for batch_result in batch_results:
        for support in batch_result:
	    # insert sizes
	    if len(support) > 6:
		tlens_all.extend(support)
		continue
	    
	    if len(support) == 0:
		continue
	    
            contig, start, stop, spanning, flanking, tiling = support
            coords = '%s-%s' % (start, stop)
            try:
                results[contig][coords] = (spanning, flanking, tiling)
            except:
                results[contig] = {}
                results[contig][coords] = (spanning, flanking, tiling)
            
    return results, tlens_all

def fetch_support(coords, bam_file, contig_fasta, overlap_buffer=0, perfect=False, debug=False):
    """Fetches read support when number given coords is relatively small
    It will use Pysam's fetch() instead of going through all read alignments
    Args:
        coords: (dictionary) coords[contig] = [spans]
                spans = (list) of (start, end) where 'start' and 'end' are not sorted
        bam_file: (string) absolute path of reads-to-contigs bam file
        debug: (boolean) prints debug statements
    Returns:
        dictionary of number of read support
        results[contig][coords] = (spanning_pos, spanning_neg, flanking)
        contig: (str)contig name
        coords: (str) original given coordinates (won't be sorted) "coord1-coord2"
        spanning_pos: (int) number unique postively spanning reads
        spanning_neg: (int) number unique postively spanning reads
        flanking: (int) number of unique flanking pairs
    """
    bam = pysam.Samfile(bam_file, 'rb')
    results = {}
    tlens_all = []
    for contig, spans in coords.iteritems():
        results[contig] = {}
	contig_seq = contig_fasta.fetch(contig)
	contig_len = len(contig_seq)
        for span in spans:
            # initialize results
            coords = '-'.join(map(str, span))
            results[contig][coords] = [0, 0]
            
            # extract all read objects
            reads = []
            for read in bam.fetch(contig):
                reads.append(read)

            # sort each span
            span_sorted = sorted(span)
            # flanking pairs
            #flanking = find_flanking_pairs(reads, span_sorted)
	    spanning = find_spanning(reads, span_sorted, contig_seq, debug=debug, overlap_buffer=overlap_buffer, perfect=perfect)
	    tiling = check_tiling(reads, span_sorted, contig_len, debug=debug)
	    
	    flanking, tlens = find_flanking(reads, span_sorted, contig_len, overlap_buffer=overlap_buffer, debug=debug)
	    results[contig][coords] = (spanning, flanking, tiling)
	    tlens_all.extend(tlens)
                                           
    return results, tlens_all

def expand_contig_breaks(chrom, breaks, contig, contig_breaks, event, ref_fasta, contig_fasta, debug=False):
    def extract_repeat(seq):
	repeat = {'start':None, 'end':None}
	if len(seq) == 1:
	    repeat['start'] = seq
	    repeat['end'] = seq
	else:
	    re_start = re.compile(r"^(.+?)\1+")
	    re_end = re.compile(r"(.+?)\1+$")

	    repeats = re_start.findall(seq)
	    if repeats:
		repeat['start'] = repeats[0]
	    repeats = re_end.findall(seq)
	    if repeats:
		repeat['end'] = repeats[0]
	
	return repeat

    contig_breaks_sorted = sorted(contig_breaks)
    pos_strand = True if contig_breaks[0] < contig_breaks[1] else False
    contig_seq = contig_fasta.fetch(contig)
    contig_breaks_expanded = [contig_breaks[0], contig_breaks[1]]
    
    seq = None
    if event == 'del':
	seq = ref_fasta.fetch(chrom, breaks[0], breaks[1] - 1)
	if not pos_strand:
	    seq = reverse_complement(seq)
	    
    elif event == 'ins':
	seq = contig_seq[contig_breaks_sorted[0] : contig_breaks_sorted[1] - 1]
	    
    if seq is None:
	return None
    
    repeat = extract_repeat(seq)
    
    if repeat['end'] is not None:
	seq = repeat['end']
	size = len(seq)
	# downstream
	start = contig_breaks_sorted[1]
	expand = 0
	while start + size <= len(contig_seq):
	    next_seq = contig_seq[start : start + size]
	    if next_seq.upper() != seq.upper():
		break
	    else:
		expand += size
		start += size
	if pos_strand:
	    contig_breaks_expanded[1] += expand
	else:
	    contig_breaks_expanded[0] += expand
	
    # upstream
    if repeat['start'] is not None:
	seq = repeat['start']
	size = len(seq)
	start = contig_breaks_sorted[0] - 1
	expand = 0
	while start - size >= 0:
	    next_seq = contig_seq[start - size : start]
	    if next_seq.upper() != seq.upper():
		break
	    else:
		expand -= size
		start -= size
	if pos_strand:
	    contig_breaks_expanded[0] += expand
	else:
	    contig_breaks_expanded[1] += expand
	
    if debug and tuple(contig_breaks_expanded) != contig_breaks:
	sys.stdout.write('contig breaks expanded:%s %s -> %s\n' % (contig, contig_breaks, contig_breaks_expanded))
	
    return tuple(contig_breaks_expanded)


def main(args, options):
    coords_file = args[0]
        
    coords = {}
    for line in open(coords_file, 'r'):
        cols = line.rstrip('\n').split()
        span = tuple(sorted((int(cols[1]), int(cols[2]))))
                    
        try:
            coords[cols[0]].append(span)
        except:
            coords[cols[0]] = [span]
            
    contigs = coords.keys()
    
    batches = list(create_batches(args[1], coords, contigs, len(contigs)/options.num_procs))
    pool = mp.Pool(processes=options.num_procs)
    supports = pool.map(worker, batches)
    
    pool.close()
    pool.join()
    
if __name__ == '__main__':
    usage = "Usage: %prog coords_file bamfile"
    parser = OptionParser(usage=usage)
    parser.add_option("-n", "--num_procs", dest="num_procs", help="Number of processes. Default: 5", default=5, type=int)
    (options, args) = parser.parse_args()
    if len(args) == 2:
        main(args, options)