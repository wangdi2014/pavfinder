from optparse import OptionParser
from itertools import groupby, chain
from pybedtools import create_interval_from_list, set_tempdir, BedTool
import pysam
import sys
import re
import subprocess
import os
from distutils.spawn import find_executable
from shared import gmap
from shared.annotate import overlap
from shared.alignment import reverse_complement
from sets import Set
from intspan import intspan
from SV.split_align import call_event
from SV.variant import Adjacency

class ITD_Finder: 
    @classmethod
    def detect_itd(cls, adj, align, contig_seq, outdir, min_len, max_apart, min_pid, debug=False):
	"""Determines if ins is an ITD
	
	Args:
	    adj: (Adjacency) original insertion adjacency
	    align: (Alignment) alignment
	    contig_seq: (str) contig sequence
	    outdir: (str) absolute path of output directory for temporarily storing blastn results if necessary
	    min_len: (int) minimum size of duplication
	    max_apart: (int) maximum distance apart of duplication
	    min_pid: (float) minimum percentage of identity of the 2 copies
	Returns:
	    If adj is determined to be an ITD, adj attributes will be updated accordingly
	"""
	novel_seq = adj.novel_seq if align.strand == '+' else reverse_complement(adj.novel_seq)
	dup = False
	# try regex first to see if perfect copies exist
	if len(novel_seq) >= min_len:
	    dup = cls.search_by_regex(novel_seq, contig_seq, max_apart)
	    if dup:
		cls.update_attrs(adj, align, dup)
	
	# try BLAST if regex fails
	if not dup:
	    dup = cls.search_by_align(adj.contigs[0], contig_seq, outdir, sorted(adj.contig_breaks), min_len, max_apart, min_pid, debug=debug)
	    if dup:
		cls.update_attrs(adj, align, dup, contig_seq)
	
    @classmethod
    def update_attrs(cls, adj, align, dup, contig_seq=None):
	"""Updates attributes of original insertion adjacency to become ITD
	   1. rna_event -> 'ITD'
	   2. contig_breaks -> coordinates of the breakpoint (the last coordinate of the first and first coordinate of the second copy)
	   3. breaks -> genome coordinate of the last base of the first copy and the next position (if blast was done and <contig_seq>
	      given
	   4. novel_seq -> sequence of the second copy (if blast was done and <contig_seq> given
	   
	Args:
	    adj: (Adjacency) original insertion adjacency object
	    align: (Alignment) alignment obejct
	    dup: (tuple) a tuple of 2 tuples that contain the duplication coordinates in the contig
	    contig_seq(optional): (str) only given if break coordinates need to be modified (i.e. blastn has been done) and novel_seq
				  needs to be reset
	"""
	adj.rna_event = 'ITD'
	new_contig_breaks = (dup[0][1], dup[1][0])
	adj.contig_breaks = new_contig_breaks
	
	if contig_seq is not None:
	    # adjust genome break coordiantes based on new contig break coordinates
	    shift = dup[1][0] - adj.contig_breaks[0]
	    if align.strand == '+':
		new_genome_pos = adj.breaks[0] + shift
		new_genome_breaks = new_genome_pos - 1, new_genome_pos
	    else:
		new_genome_pos = adj.breaks[0] + shift * -1
		new_genome_breaks = new_genome_pos, new_genome_pos + 1
	    adj.breaks = new_genome_breaks
	    
	    # captures second copy of duplication as novel sequence
	    dup_size = max(dup[0][1] - dup[0][0] + 1, dup[1][1] - dup[1][0] + 1)
	    novel_seq = contig_seq[dup[1][0] - 1 : dup[1][0] - 1 + dup_size]
	    if align.strand == '-':
		novel_seq = reverse_complement(novel_seq)
	    adj.novel_seq = novel_seq

        
    @classmethod
    def search_by_regex(cls, novel_seq, contig_seq, max_apart):
	"""Finds if there is a duplication of the novel_seq within the contig sequence
	by simple regex matching i.e. only perfect matches captured
	
	Args:
	    novel_seq: (str) the original novel contig sequence defined in the insertion, assuming the 
			     length is already checked to be above the threshold
	    contig_seq: (str) the contig sequence
	    max_apart:  (int) the maximum number of bases allowed for the event to be called a duplication
	Returns:
	    a tuple of 2 tuples that contain the duplication coordinates in the contig
	"""
	matches = re.findall(novel_seq, contig_seq)
		
	if len(matches) > 1:
	    starts = []
	    p = re.compile(novel_seq)
	    for m in p.finditer(contig_seq):
		starts.append(m.start())
		
	    if len(starts) == 2 and\
	       starts[1] - (starts[0] + len(novel_seq)) <= max_apart:				
		return ((starts[0] + 1, starts[0] + len(novel_seq)),
		        (starts[1] + 1, starts[1] + len(novel_seq)))
	    
	return False
    
    @classmethod
    def parse_blast_tab(cls, blast_tab):
	"""Parses a tabular BLAST output file
	
	Args:
	    blast_tab: (str) full path to a tabular blastn output file (-outfmt 6 from blastn)
			     version of blastn: ncbi-blast-2.2.29+
	Returns:
	    A list of dictionary where the keys are the fields and the values are the results
	"""
	aligns = []
	fields = ('query', 'target', 'pid', 'alen', 'num_mismatch', 'num_gaps', 'qstart', 'qend', 'tstart', 'tend', 'evalue', 'bit_score')
	for line in open(blast_tab, 'r'):
	    cols = line.rstrip('\n').split('\t')
	    # if somehow the number of values is different from the number of fields hard-coded above, no alignments will be captured
	    if len(cols) == len(fields):
		aligns.append(dict(zip(fields, cols)))
	    
	return aligns
	
    @classmethod
    def search_by_align(cls, contig, contig_seq, outdir, contig_breaks, min_len, max_apart, min_pid, debug=False):
	"""Checks if an insertion event is an ITD using blastn self-vs-self alignment
	Assumes:
	1. original contig coordiantes are the coordinates of the novel sequence
	2. original insertion will overlap the duplication ranges

	Conditions:
	1. percentage of identity between the duplications is at least <min_pid> (mismatch and gap allowed)
	2. the duplciations are at most <max_apart> apart
	3. the duplication is at least <min_len> long
	
	Returns:
	    a tuple of 2 tuples that contain the duplication coordinates in the contig
	"""						
	# run blastn of contig sequence against itself
	# generate FASTA file of contig sequence
	seq_file = '%s/tmp_seq.fa' % outdir
	out = open(seq_file, 'w')
	out.write('>%s\n%s\n' % (contig, contig_seq))
	out.close()
	
	# run blastn
	blast_output = '%s/tmp_blastn.tsv' % outdir
	cmd = 'blastn -query %s -subject %s -outfmt 6 -out %s' % (seq_file, seq_file, blast_output)

	try:
	    subprocess.call(cmd, shell=True)
	except:
	    # should check whether blastn is in the PATH right off the bet
	    sys.stderr.write('Failed to run BLAST:%s\n' % cmd)
	    sys.exit()
	    
	if os.path.exists(blast_output):
	    blast_aligns = cls.parse_blast_tab(blast_output)
	    # clean up
	    if not debug:
		for ff in (seq_file, blast_output):
		    os.remove(ff)
		
	    # identify 'significant' stretch of duplication from BLAST result
	    # and see if it overlaps contig break position
	    dups = []
	    # for picking the 'longest' dup if there are multiple candidates
	    dup_sizes = []
	    for aln in blast_aligns:
		# skip if a. it's the sequence match itself 
		#         b. match is too short 
		#         c. match is just a member of the 2 reciprocal matches
		if int(aln['alen']) == len(contig_seq) or\
		   int(aln['alen']) < min_len or\
		   int(aln['qstart']) >= int(aln['tstart']):
		    continue
		span1 = (int(aln['qstart']), int(aln['qend']))
		span2 = (int(aln['tstart']), int(aln['tend']))
		
		# 4 conditions for checking if it's an ITD
		#      1. longer than minimum length (checked above)
		#      2. percent of identity better than minumum
		#      3. the duplications less than or equal to max_apart
		#      4. either one the duplication ranges overlaps the original contigs breakpoint coordinate range
		#         (contig coordinates of the novel sequence)
		if float(aln['pid']) >= min_pid and\
		   abs(min(span2[1], span1[1]) - max(span2[0], span1[0])) <= max_apart and\
		   (min(contig_breaks[1], span1[1]) - max(contig_breaks[0], span1[0]) > 0 or
		    min(contig_breaks[1], span2[1]) - max(contig_breaks[0], span2[0]) > 0):
		    dup = [(int(aln['qstart']), int(aln['qend'])), 
		           (int(aln['tstart']), int(aln['tend']))]
		    dups.append(dup)
		    dup_sizes.append(int(aln['alen']))
		    
	    if dups:
		# if there are multiple candidates, pick the largest
		index_of_largest = sorted(range(len(dups)), key=lambda idx:dup_sizes[idx], reverse=True)[0]		
		dup = dups[index_of_largest]	
				
		return dup
		
	return False
    
class FusionFinder:
    @classmethod
    def find_chimera(cls, chimera_block_matches, transcripts, aligns):
	"""Identify gene fusion between split alignments
	
	Args:
	    chimera_block_matches: (list) dictionaries where 
	                                      key=transcript name, 
	                                      value=[match1, match2, ...] where
						    match1 = matches of each alignment block
							     i.e.
							     [(exon_id, '=='), (exon_id, '==')] 
	    transcripts: (dict) key=transcript_name value=Transcript object
	    aligns: (list) Alignments involved in chimera
	Returns:
	    Adjacency with genes, transcripts, exons annotated
	"""
	assert len(chimera_block_matches) == len(aligns), 'number of block matches(%d) != number of aligns(%d)' % \
	       (len(chimera_block_matches), len(aligns))
	for (matches1_by_txt, matches2_by_txt) in zip(chimera_block_matches, chimera_block_matches[1:]):	    
	    genes1 = Set([transcripts[txt].gene for txt in matches1_by_txt.keys()])
	    genes2 = Set([transcripts[txt].gene for txt in matches2_by_txt.keys()])
	    	    	    
	    junc_matches1 = {}
	    num_blocks = len(aligns[0].blocks)
	    for transcript in chimera_block_matches[0].keys():
		junc_matches1[transcript] = chimera_block_matches[0][transcript][num_blocks - 1]
	
	    junc_matches2 = {}
	    for transcript in chimera_block_matches[1].keys():
		junc_matches2[transcript] = chimera_block_matches[1][transcript][0]
    
	    # create adjacency first to establish L/R orientations, which is necessary to pick best transcripts
	    fusion = call_event(aligns[0], aligns[1], no_sort=True)
	    junc1, junc2 = cls.identify_fusion(junc_matches1, junc_matches2, transcripts, fusion.orients)
	    if junc1 and junc2:
		fusion.rna_event = 'fusion'
		fusion.genes = (transcripts[junc1[0]].gene, transcripts[junc2[0]].gene)
		fusion.transcripts = (junc1[0], junc2[0])
		#if junc1[1] is None or junc2[1] is None:
		    #print 'check', junc1[0], junc2[0], transcripts[junc1[0]], transcripts[junc2[0]], junc1, junc2
		    #return None
		fusion.exons = transcripts[junc1[0]].exon_num(junc1[1][0][0]), transcripts[junc2[0]].exon_num(junc2[1][0][0])
		fusion.exon_bound = junc1[2], junc2[2]
		fusion.in_frame = True if (fusion.exon_bound[0] and fusion.exon_bound[1]) else False
		cls.is_ptd(fusion)
		return fusion
	    
	return None

    @classmethod
    def find_read_through(cls, matches_by_transcript, transcripts, align):
	"""Identify read-through fusion
	
	Assume single alignment is spanning 2 genes
	
	Args:
	    matches_by_transcript: (list) dictionaries where 
	                                      key=transcript name, 
	                                      value=[match1, match2, ...] where
						    match1 = matches of each alignment block
							     i.e.
							     [(exon_id, '=='), (exon_id, '==')] 
							     or None if no_match
	    transcripts: (dict) key=transcript_name value=Transcript object
	    align: (Alignment) alignment spanning >1 gene
	Returns:
	    Adjacency with genes, transcripts, exons annotated
	"""
	def create_fusion(junc1, junc2, pos, contig_breaks):
	    """Creates Adjacency object capturing read-through
	    
	    Args:
	        junc1: (tuple) transcript_id, 'match' list [(exon_id, '==')]
		junc2: (tuple) transcript_id, 'match' list [(exon_id, '==')]
		pos: (tuple) genome coordinates of fusion
		contig_breaks: (tuple) contig coordinates of breaks
	    Returns:
		Adjacency with genes, transcripts, exons annotated
	    """
	    fusion = Adjacency((align.target, align.target),
	                       pos,
	                       '-',
	                       orients=('L', 'R'),
	                       contig_breaks = contig_breaks
	                       )               
	    fusion.rna_event = 'fusion'
	    fusion.genes = (transcripts[junc1[0]].gene, transcripts[junc2[0]].gene)
	    fusion.transcripts = (junc1[0], junc2[0])
	    fusion.exons = transcripts[junc1[0]].exon_num(junc1[1][0][0]), transcripts[junc2[0]].exon_num(junc2[1][0][0])
	    fusion.exon_bound = junc1[2], junc2[2]
	    fusion.in_frame = True if (fusion.exon_bound[0] and fusion.exon_bound[1]) else False
	    cls.is_ptd(fusion)
	    
	    return fusion
	
	num_blocks = len(align.blocks)
	matches_by_block = {}
	genes_in_block = {}
	for i in range(num_blocks):
	    matches_by_block[i] = {}
	    genes_in_block[i] = Set()
	    for transcript in matches_by_transcript.keys():
		matches_by_block[i][transcript] = matches_by_transcript[transcript][i]
		if matches_by_transcript[transcript][i] is not None:
		    genes_in_block[i].add(transcripts[transcript].gene)
		    
	junctions = zip(range(num_blocks), range(num_blocks)[1:])
	for k in range(len(junctions)):
	    i, j = junctions[k]
	    if len(genes_in_block[i]) == 1 and len(genes_in_block[j]) == 1:
		if list(genes_in_block[i])[0] != list(genes_in_block[j])[0]:
		    junc1, junc2 = cls.identify_fusion(matches_by_block[i], matches_by_block[j], transcripts, ('L', 'R'))
		    pos = (align.blocks[i][1], align.blocks[j][0])
		    contig_breaks = (align.query_blocks[i][1], align.query_blocks[j][0])
		    fusion = create_fusion(junc1, junc2, pos, contig_breaks)
		    return fusion
	
	    elif len(genes_in_block[i]) > 1:
		print 'fusion block', i, j, genes_in_block
		
	    elif len(genes_in_block[j]) > 1:
		print 'fusion block', i, j
		
	for i in genes_in_block.keys():
	    if len(genes_in_block[i]) == 2:
		cls.identify_fusion_unknown_break(matches_by_block[i], transcripts)
		
	return None
		
    @classmethod
    def identify_fusion_unknown_break(cls, matches, transcripts):
	all_matches = Set()
	for txt in matches:
	    if matches[txt] is None:
		continue
	    
	    for match in matches[txt]:
		all_matches.add((txt, match))
		
	left_bound_matches = [match for match in all_matches if match[1][1][0] == '=']
	right_bound_matches = [match for match in all_matches if match[1][1][1] == '=']

	print 'left', left_bound_matches
	print 'right', right_bound_matches
	
	if left_bound_matches and right_bound_matches:
	    left_bound_matches.sort(key=lambda m: transcripts[m[0]].length(), reverse=True)
	    right_bound_matches.sort(key=lambda m: transcripts[m[0]].length(), reverse=True)
	    
	    print 'left', left_bound_matches
	    print 'right', right_bound_matches
	    
	    print 'fusion_unknown_break', left_bound_matches[0], right_bound_matches[0]
    
    @classmethod
    def identify_fusion(cls, matches1, matches2, transcripts, orients):
	"""Given 2 block matches pick the 2 transcripts"""
	def pick_best(matches, orient, exon_bound_score=1000):
	    scores = {}
	    for transcript in matches:
		if orient == 'L':
		    junction_block = -1
		    junction_edge, distant_edge = -1, 0
		else:
		    junction_block = 0
		    junction_edge, distant_edge = 0, -1
		score = 0
		if matches[transcript] is not None:
		    if matches[transcript][junction_block][1][junction_edge] == '=':
			score += exon_bound_score
			
		    # align block within exon gets more points
		    if matches[transcript][junction_block][1][distant_edge] == '=':
			score += 15
		    elif matches[transcript][junction_block][1][distant_edge] == '>':
			score += 10
		    elif matches[transcript][junction_block][1][distant_edge] == '<':
			score += 5
		scores[transcript] = score
		
	    best_score = max(scores.values())
	    best_txt = sorted([t for t in matches.keys() if scores[t] == best_score], 
		               key=lambda t: transcripts[t].length(), reverse=True)[0]
	    return best_txt, best_score > exon_bound_score
	    	
	best_txt1, exon_bound1 = pick_best(matches1, orients[0])
	best_txt2, exon_bound2 = pick_best(matches2, orients[1])
	
	return (best_txt1, matches1[best_txt1], exon_bound1), (best_txt2, matches2[best_txt2], exon_bound2)

    @classmethod
    def is_ptd(cls, fusion):
	"""Define PTD"""
	if fusion.genes[0] == fusion.genes[1] and\
	   fusion.exon_bound[0] and\
	   fusion.exon_bound[1]:
	    fusion.rna_event = 'PTD'
	    
class NovelSpliceFinder:    
    @classmethod
    def find_novel_junctions(cls, block_matches, align, transcripts, ref_fasta):
	"""Find novel junctions within a single gene/transcript
	
	Args:
	    block_matches: (list) dictionaries where 
	                                      key=transcript name, 
	                                      value=[match1, match2, ...] where
						    match1 = matches of each alignment block
							     i.e.
							     [(exon_id, '=='), (exon_id, '==')] 
	    align: (Alignment) alignment object
	    transcripts: (dictionary) key=transcript_id value=Transcript object
	    ref_fasta: (Pysam.Fastafile) Pysam handle to access reference sequence, for checking splice motif
	Returns:
	    List of event (dictionary storing type of event, exon indices, genomic coordinates and size)
	"""
	# find annotated junctions
	annotated = Set()
	for transcript in block_matches.keys():
	    matches = block_matches[transcript]
	    # sort multiple exon matches for single exon by exon num
	    [m.sort(key=lambda mm: int(mm[0])) for m in matches if m is not None]
	    
	    for i in range(len(matches) - 1):
		j = i + 1
		
		if matches[i] == None or matches[j] == None:
		    continue
		
		if (i, j) in annotated:
		    continue
		
		if cls.is_junction_annotated(matches[i][-1], matches[j][0]):
		    annotated.add((i, j))
		    continue
		
	all_events = []
	for transcript in block_matches.keys():
	    matches = block_matches[transcript]
	    #print 'mmm', matches
	    #print 'eee', transcript, transcripts[transcript].exons
	    for i in range(len(matches) - 1):
		j = i + 1
			
		if matches[i] is None and matches[j] is not None:
		    # special case where the first 2 blocks is the utr and there's an insertion separating the 2 blocks
		    if i == 0:
			events = cls.classify_novel_junction(matches[i], 
			                                     matches[j][0], 
			                                     align.target, 
			                                     align.blocks[i:j+1], 
			                                     transcripts[transcript],
			                                     ref_fasta
			                                     )
			for e in events:
			    e['blocks'] = (i, j)
			    e['transcript'] = transcript
			    e['contig_breaks'] = (align.query_blocks[i][1], align.query_blocks[j][0])
			all_events.extend(events)
		    continue
		
		# for retained intron, not a 'junction'
		if matches[i] is not None and len(matches[i]) > 1:
		    events = cls.classify_novel_junction(matches[i], 
		                                         None,
		                                         align.target, 
		                                         align.blocks[i], 
		                                         transcripts[transcript],
		                                         ref_fasta
		                                         )
		    if events:
			for e in events:
			    e['blocks'] = (i, j)
			    e['transcript'] = transcript
			all_events.extend(events)
		    
		# skip if junction is annotated
		if (i, j) in annotated:
		    continue
		
		# for novel exon
		if matches[j] == None and matches[i] is not None:
		    for k in range(j + 1, len(matches)):
			if matches[k] is not None and matches[k][0][0] - matches[i][-1][0] == 1:
			    j = k
			    break
		
		if matches[i] is not None and matches[j] is not None:
		    # matches (i and j) are the flanking matches, blocks are the middle "novel" blocks
		    events = cls.classify_novel_junction(matches[i][-1], 
		                                         matches[j][0], 
		                                         align.target, 
		                                         align.blocks[i:j+1], 
		                                         transcripts[transcript],
		                                         ref_fasta
		                                         )
		    if events:
			for e in events:
			    e['blocks'] = range(i + 1, j)
			    e['contig_breaks'] = (align.query_blocks[i][1], align.query_blocks[j][0])
			    e['transcript'] = transcript
			all_events.extend(events)
		
	if all_events:
	    # group events
	    grouped = {}
	    for event in all_events:
		key = str(event['blocks']) + event['event']
		try:
		    grouped[key].append(event)
		except:
		    grouped[key] = [event]
		
	    uniq_events = []
	    for events in grouped.values():
		transcripts = [e['transcript'] for e in events]
		exons = [e['exons'] for e in events]
		
		contig_breaks = []
		if events[0].has_key('contig_breaks'):
		    contig_breaks = events[0]['contig_breaks']
		
		uniq_events.append({'event': events[0]['event'],
	                            'blocks': events[0]['blocks'],
	                            'transcript': transcripts,
	                            'exons': exons,
	                            'pos': events[0]['pos'],
	                            'contig_breaks': contig_breaks,
		                    'size': events[0]['size'],
	                            }
	                           )
		    
	    return uniq_events
	
    @classmethod
    def classify_novel_junction(cls, match1, match2, chrom, blocks, transcript, ref_fasta, min_intron_size=20):
	"""Classify given junction into different splicing events or indel
	
	Args:
	    match1: (tuple or list) single tuple: exon_index, 2-char match result e.g. '==', '>=', etc
				    list of 2 tuples for retained_intron (special case)
	    match2: (tuple) exon_index, 2-char match result e.g. '==', '>=', etc
	    chrom: (str) chromosome, for checking splice motif
	    blocks: (list) list of list (block coordinates)
	    transcript: (Transcript) object of transcript, for getting exon coordinates
	    ref_fasta: (Pysam.Fastafile) Pysam handle to access reference sequence, for checking splice motif
	Returns:
	    List of event (dictionary storing type of event, exon indices, genomic coordinates and size)
	"""
	events = []
	# set default values for 'pos'
	if type(blocks[0]) is int:
	    pos = (blocks[0], blocks[1])
	else:
	    pos = (blocks[0][1], blocks[1][0])
	
	if match2 is None:
	    if len(match1) == 2:
		exons = [m[0] for m in match1]
		if match1[0][1] == '=>' and\
		   match1[-1][1] == '<=' and\
		   len([(a, b) for a, b in zip(exons, exons[1:]) if b == a + 1]) == len(match1) - 1:
		    size = transcript.exons[exons[1]][0] - transcript.exons[exons[0]][1] - 1 
		    events.append({'event': 'retained_intron', 'exons': exons, 'pos':pos, 'size':size})
		  
	# genomic deletion or novel_intron
	elif match1 is None and match2 is not None:
	    if match2[0] == 0 and match2[1] == '<=':
		if transcript.strand == '+':
		    donor_start = blocks[0][1] + 1
		    acceptor_start = blocks[1][0] - 2
		else:
		    donor_start = blocks[1][0] - 2
		    acceptor_start = blocks[0][1] + 1
		print 'special check', match1, match2, pos
		gap_size = blocks[1][0] - blocks[0][1] - 1
		pos = (blocks[0][1], blocks[1][0])
		event = None
		if gap_size > 0:
		    if gap_size > min_intron_size and\
		       cls.check_splice_motif(chrom, donor_start, acceptor_start, transcript.strand, ref_fasta):
			print 'aaa', transcript.id
			event = 'novel_intron'
		    else:
			event = 'del'
		if event is not None:
		    events.append({'event': event, 'exons': [match2[0]], 'pos':pos, 'size':gap_size})
		
	else:
	    if match2[0] > match1[0] + 1 and\
	       '=' in match1[1] and\
	       '=' in match2[1]:
		size = 0
		for e in range(match1[0] + 1, match2[0]):
		    exon = transcript.exons[e]
		    size += exon[1] - exon[0] + 1
		events.append({'event': 'skipped_exon', 'exons': range(match1[0] + 1, match2[0]), 'pos':pos, 'size':size})
	       
	    if match1[0] == match2[0] and\
	       match1[1][1] == '<' and match2[1][0] == '>':		
		if transcript.strand == '+':
		    donor_start = blocks[0][1] + 1
		    acceptor_start = blocks[1][0] - 2
		else:
		    donor_start = blocks[1][0] - 2
		    acceptor_start = blocks[0][1] + 1
		
		gap_size = blocks[1][0] - blocks[0][1] - 1
		pos = (blocks[0][1], blocks[1][0])
		event = None
		if gap_size > 0:
		    if gap_size > min_intron_size and\
		       cls.check_splice_motif(chrom, donor_start, acceptor_start, transcript.strand, ref_fasta):
			print 'bbb'
			event = 'novel_intron'
		    else:
			event = 'del'
		elif gap_size == 0:
		    event = 'ins'
		
		if event is not None:
		    if event != 'ins':
			events.append({'event': event, 'exons': [match1[0]], 'pos':pos, 'size':gap_size})
		    else:
			events.append({'event': event, 'exons': [match1[0]], 'pos':pos})
		
	    # novel donor and acceptor
	    if match1[1][1] == '=' and match2[1][0] != '=':
		size = abs(blocks[1][0] - transcript.exons[match2[0]][0])
		if transcript.strand == '+':
		    event = 'novel_acceptor'
		    donor_start = blocks[0][1] + 1
		    acceptor_start = blocks[1][0] - 2
		else:
		    event = 'novel_donor'
		    donor_start = blocks[1][0] - 2
		    acceptor_start = blocks[0][1] + 1
		# check splice motif
		if cls.check_splice_motif(chrom, donor_start, acceptor_start, transcript.strand, ref_fasta):
		    events.append({'event': event, 'exons': [match1[0], match2[0]], 'pos':pos, 'size':size})
		    
	    if match1[1][1] != '=' and match2[1][0] == '=':
		size = abs(blocks[0][1] - transcript.exons[match1[0]][1])
		if transcript.strand == '+':
		    event = 'novel_donor'
		    donor_start = blocks[0][1] + 1
		    acceptor_start = blocks[1][0] - 2
		else:
		    event = 'novel_acceptor'
		    donor_start = blocks[1][0] - 2
		    acceptor_start = blocks[0][1] + 1
		# check splice motif
		if cls.check_splice_motif(chrom, donor_start, acceptor_start, transcript.strand, ref_fasta):
		    events.append({'event': event, 'exons': [match1[0], match2[0]], 'pos':pos, 'size':size})
	    		
	    if match2[0] == match1[0] + 1 and\
	       match1[1][1] == '=' and\
	       match2[1][0] == '=': 
		pos = (blocks[1][0], blocks[-2][1])
		size = blocks[-2][1] - blocks[1][0] + 1
		if transcript.strand == '+':
		    donor_start = pos[1] + 1
		    acceptor_start = pos[0] - 2
		else:
		    donor_start = pos[0] - 2
		    acceptor_start = pos[1] + 1
		# check splice motif
		if cls.check_splice_motif(chrom, donor_start, acceptor_start, transcript.strand, ref_fasta):
		    events.append({'event': 'novel_exon', 'exons': [], 'pos':pos, 'size':size})
		
	# set size to None for event that doesn't have size i.e. 'ins'
	for event in events:
	    if not event.has_key('size'):
		event['size'] = None
	
	return events
    
    @classmethod
    def check_splice_motif(cls, chrom, donor_start, acceptor_start, strand, ref_fasta):
	"""Check if the 4-base splice motif of a novel junction is canonical (gtag)
	
	Right now only considers 'gtag' as canonical
	
	Args:
	    chrom: (str) chromosome
	    donor_start: (int) genomic position of first(smallest) base of donor site (1-based)
	    acceptor_start: (int) genomic position of first(smallest) base of acceptor site (1-based)
	    strand: (str) transcript strand '+' or '-'
	    ref_fasta: (Pysam.Fastafile) Pysam handle to access reference sequence
	Returns:
	    True if it's canonical, False otherwise
	"""
	canonical_motifs = Set()
	canonical_motifs.add('gtag')
	
	donor_seq = ref_fasta.fetch(chrom, donor_start - 1, donor_start - 1 + 2)
	acceptor_seq = ref_fasta.fetch(chrom, acceptor_start - 1, acceptor_start - 1 + 2)
	
	if strand == '+':
	    motif = donor_seq + acceptor_seq
	else:
	    motif = reverse_complement(acceptor_seq + donor_seq)
	
	if motif.lower() in canonical_motifs:
	    return True
	else:
	    return False
    
    @classmethod
    def is_junction_annotated(cls, match1, match2):
	"""Checks if junction is in gene model
	
	Args:
	    match1: (tuple) exon_index, 2-char match result e.g. '==', '>=', etc
	    match2: (tuple) exon_index, 2-char match result e.g. '==', '>=', etc
	Returns:
	    True or False
	"""
	if match2[0] == match1[0] + 1 and\
	   match1[1][1] == '=' and\
	   match2[1][0] == '=':
	    return True
	
	return False

class Transcript:
    def __init__(self, id, gene=None, strand=None):
	self.id = id
	self.gene = gene
	self.strand = strand
	self.exons = []
	
    def add_exon(self, exon):
	self.exons.append(exon)
	self.exons.sort(key=lambda e: int(e[0]))
	    
    def exon(self, num):
	assert type(num) is int, 'exon number %s not given in int' % num
	assert self.strand == '+' or self.strand == '-', 'transcript strand not valid: %s %s' % (self.id, self.strand)
	assert num >= 1 and num <= len(self.exons), 'exon number out of range:%s (1-%d)' % (num, len(self.exons))
	
	if self.strand == '+':
	    return self.exons[num - 1]
	else:
	    return self.exons[-1 * num]
	
    def num_exons(self):
	return len(self.exons)
    
    def length(self):
	total = 0
	for exon in self.exons:
	    total += (exon[1] - exon[0] + 1)
	return total
    
    def txStart(self):
	return self.exons[0][0]
    
    def txEnd(self):
	return self.exons[-1][1]
    
    def exon_num(self, index):
	"""Converts exon index to exon number
	Exon number is always starting from the transcription start site
	i.e. for positive transcripts, the first exon is exon 1
	     for negative transcripts, the last exon is exon 1
	Need this method because a lot of the splicing variant code just keep
	track of the index instead of actual exon number
	
	Args:
	    index: (int) index of exon in list
	Returns:
	    Exon number in int
	"""
	assert type(index) is int, 'exon index %s not given in int' % index
	assert self.strand == '+' or self.strand == '-', 'transcript strand not valid: %s %s' % (self.id, self.strand)
	assert index >= 0 and index < len(self.exons), 'exon index out of range:%s %d' % (index, len(self.exons))
	if self.strand == '+':
	    return index + 1
	else:
	    return len(self.exons) - index
    
class Event:
    # headers of tab-delimited output
    headers = ['event_type',
               'chrom1',
               'pos1',
               'orient1',
               'gene1',
               'transcript1',
               'exon1',
               'chrom2',
               'pos2',
               'orient2',
               'gene2',
               'transcript2',
               'exon2',
               'size',
               'novel_sequence',
               'contigs',
               'contig_breaks',
               ]
    
    @classmethod
    def output(cls, events, outdir):
	"""Output events

	Args:
	    events: (list) Adjacency
	    outdir: (str) absolute path of output directory
	Returns:
	    events will be output in file outdir/events.tsv
	"""
	out_file = '%s/events.tsv' % outdir
	out = open(out_file, 'w')
	out.write('%s\n' % '\t'.join(cls.headers))
	for event in events:
	    if event.rna_event:
		out_line = {
		    'fusion': cls.from_fusion,
		    'ITD': cls.from_single_locus,
		    'PTD': cls.from_fusion,
		    'ins': cls.from_single_locus,
		    'del': cls.from_single_locus,
		    'skipped_exon': cls.from_single_locus,
		    'novel_exon': cls.from_single_locus,
		    'novel_donor': cls.from_single_locus,
		    'novel_acceptor': cls.from_single_locus,
		    'novel_intron': cls.from_single_locus,
		    'retained_intron': cls.from_single_locus,
		    }[event.rna_event](event)
		if out_line:
		    out.write('%s\n' % out_line)
	out.close()
				
    @classmethod
    def from_fusion(cls, event):
	"""Generates output line for a fusion/PTD event
	
	Args:
	    event: (Adjacency) fusion or PTD event (coming from split alignment)
	Returns:
	    Tab-delimited line
	"""
	data = [event.rna_event]
	for values in zip(event.chroms, event.breaks, event.orients, event.genes, event.transcripts, event.exons):
	    data.extend(values)
	    
	# size not applicable to fusion
	data.append('-')
	if hasattr(event, 'novel_seq') and event.novel_seq is not None:
	    data.append(event.novel_seq)
	else:
	    data.append('-')    
	data.append(','.join(event.contigs))
	data.append(cls.to_string(event.contig_breaks))
	
	return '\t'.join(map(str, data))
    
    @classmethod
    def from_single_locus(cls, event):
	"""Generates output line for an event from a single alignment
	
	Args:
	    event: (Adjacency) indel, ITD, splicing event (coming from single alignment)
	Returns:
	    Tab-delimited line
	"""
	chroms = (event.chroms[0], event.chroms[0])
	orients = ('L', 'R')
	genes = (event.genes[0], event.genes[0])
	transcripts = (event.transcripts[0], event.transcripts[0])
	if event.exons:
	    if len(event.exons) == 2:
		exons = event.exons
	    else:
		exons = (event.exons[0], event.exons[0])
	# novel exons
	else:
	    exons = ('-', '-')
	
	data = [event.rna_event]
	for values in zip(chroms, event.breaks, orients, genes, transcripts, exons):
	    data.extend(values)
	
	if hasattr(event, 'size') and event.size is not None:
	    data.append(event.size)
	else:
	    data.append('-')
	if hasattr(event, 'novel_seq') and event.novel_seq is not None:
	    data.append(event.novel_seq)
	else:
	    data.append('-')    
	data.append(','.join(event.contigs))
	data.append(cls.to_string(event.contig_breaks))
	return '\t'.join(map(str, data))
	
    @classmethod
    def to_string(cls, value):
	"""Convert value of data types other than string usually used
	in Adjacency attributes to string for print
	
	Args:
	    value: can be
	           1. None
		   2. simple list/tuple
		   3. list/tuple of list/tuple
	Returns:
	    string representation of value
	    ';' used to separate items in top-level list/tuple
	    ',' used to separate items in second-level list/tuple
	"""
	if value is None:
	    return '-'
	elif type(value) is list or type(value) is tuple:
	    items = []
	    for item in value:
		if item is None:
		    items.append('-')
		elif type(item) is list or type(item) is tuple:
		    if item:
			items.append(','.join(map(str, item)))
		else:
		    items.append(str(item))
		    
	    if items:
		return ';'.join(items)
	    else:
		return '-'
	else:
	    return str(value)
						    	    	
class Mapping:
    """Mapping per alignment"""
    def __init__(self, contig, align_blocks, transcripts=[]):
	self.contig = contig
	self.transcripts = transcripts
	self.genes = list(Set([txt.gene for txt in self.transcripts]))
	self.align_blocks = align_blocks
	# coverage for each transcript in self.transcripts
	self.coverages = []
	
    def overlap(self):
	"""Overlaps alignment spans with exon-spans of each matching transcripts
	Upates self.coverages
	"""
	align_span = self.create_span(self.align_blocks)
	
	for transcript in self.transcripts:
	    exon_span = self.create_span(transcript.exons)
	    olap = exon_span.intersection(align_span)
	    self.coverages.append(float(len(olap)) / float(len(exon_span)))
	    
    @classmethod
    def create_span(cls, blocks):
	"""Creates intspan for each block
	Used by self.overlap()
	"""
	span = None
	for block in blocks:
	    try:
		span = span.union(intspan('%s-%s' % (block[0], block[1])))
	    except:
		span = intspan('%s-%s' % (block[0], block[1]))
		
	return span
    
    @classmethod
    def header(cls):
	return '\t'.join(['contig',
	                  'gene',
	                  'transcript',
	                  'coverage'
	                  ])
	    
    def as_tab(self):
	"""Generates tab-delimited line for each mapping"""
	data = []
	data.append(self.contig)
	if self.transcripts:
	    data.append(','.join([gene for gene in Set([txt.gene for txt in self.transcripts])]))
	    data.append(','.join([txt for txt in Set([txt.id for txt in self.transcripts])]))
	else:
	    data.append('-')
	    data.append('-')
	    
	if self.coverages:
	    data.append(','.join(['%.2f' % olap for olap in self.coverages]))
	else:
	    data.append('-')
	
	return '\t'.join(map(str, data))
	
    @classmethod
    def pick_best(self, mappings, align, debug=False):
	"""Selects best mapping among transcripts"""
	scores = {}
	metrics = {}
	for transcript, matches in mappings:
	    print 'ggg', transcript.id, matches
	    metric = {'score': 0,
	              'from_edge': 0,
	              'txt_size': 0
	              }
	    # points are scored for matching exon boundaries
	    score = 0
	    for i in range(len(matches)):
		if matches[i] is None:
		    continue
		
		if i == 0:			    
		    if matches[i][0][1][0] == '=':
			score += 5
		    elif matches[i][0][1][0] == '>':
			score += 2
			
		elif i == len(matches) - 1:
		    if matches[i][-1][1][1] == '=':
			score += 5
		    elif matches[i][-1][1][1] == '<':
			score += 2
			
		if matches[i][0][1][1] == '=':
		    score += 4
		if matches[i][-1][1][1] == '=':
		    score += 4	
			
	    # points are deducted for events
	    penalty = 0
	    for i in range(len(matches)):
		# block doesn't match any exon
		if matches[i] is None:
		    penalty += 2
		    continue
		    
		# if one block is mapped to >1 exon
		if len(matches[i]) > 1:
		    penalty += 1
		
		# if consecutive exons are not mapped to consecutive blocks
		if i < len(matches) - 1 and matches[i + 1] is not None:
		    if matches[i + 1][0][0] != matches[i][-1][0] + 1:
			#print 'penalty', matches[i][-1][0], matches[i + 1][0][0]
			penalty += 1
			
	    metric['score'] = score - penalty
	    
	    # if the first or last block doesn't have matches, won't be able to calculate distance from edges
	    # in that case, will set the distance to 'very big'
	    if matches[0] is None or matches[-1] is None:
		metric['from_edge'] = 10000
	    else:
		if transcript.strand == '+':
		    start_exon_num = matches[0][0][0] + 1
		    end_exon_num = matches[-1][-1][0] + 1
		else:
		    start_exon_num = transcript.num_exons() - matches[0][0][0]
		    end_exon_num = transcript.num_exons() - matches[-1][-1][0]
		    
		metric['from_edge'] = align.tstart - transcript.exon(start_exon_num)[0] + align.tend - transcript.exon(end_exon_num)[1]
		    
	    metric['txt_size'] = transcript.length()
	    metrics[transcript] = metric
	    
	    if debug:
		sys.stdout.write("mapping %s %s %s %s %s %s\n" % (align.query, transcript.id, transcript.gene, score, penalty, metric))
	    
	transcripts_sorted = sorted(metrics.keys(), key = lambda txt: (-1 * metrics[txt]['score'], metrics[txt]['from_edge'], metrics[txt]['txt_size']))
	for t in transcripts_sorted:
	    print 'sorted', t.id, metrics[t]
	    	    
	best_transcript = transcripts_sorted[0]
	best_matches = [mapping[1] for mapping in mappings if mapping[0] == best_transcript]
	best_mapping = Mapping(align.query,
	                       align.blocks,
                               [transcripts_sorted[0]],
                               )
	best_mapping.overlap()
	
	return best_mapping
	    	
    @classmethod
    def group(cls, all_mappings):
	"""Group mappings by gene"""
	gene_mappings = []
	for gene, group in groupby(all_mappings, lambda m: m.genes[0]):
	    mappings = list(group)
	    contigs = ','.join([mapping.contig for mapping in mappings])
	    transcripts = [mapping.transcripts for mapping in mappings]
	    align_blocks = [mapping.align_blocks for mapping in mappings]
	    
	    align_blocks = None
	    
	    for mapping in mappings:
		try:
		    align_blocks = align_blocks.union(cls.create_span(mapping.align_blocks))
		except:
		    align_blocks = cls.create_span(mapping.align_blocks)		
	    
	    gene_mappings.append(Mapping(contigs,
	                                 align_blocks.ranges(),
	                                 list(Set(chain(*transcripts))),
	                                 )
	                         )
	    	    
	[mapping.overlap() for mapping in gene_mappings]
	return gene_mappings
    
    @classmethod
    def output(cls, mappings, outfile):
	"""Output mappings into output file"""
	out = open(outfile, 'w')
	out.write('%s\n' % cls.header())
	for mapping in mappings:
	    out.write('%s\n' % mapping.as_tab())
	out.close()

class ExonMapper:
    def __init__(self, bam_file, aligner, contigs_fasta_file, annotation_file, ref_fasta_file, outdir, 
                 itd_min_len=None, itd_min_pid=None, itd_max_apart=None, debug=False):
        self.bam = pysam.Samfile(bam_file, 'rb')
	self.contigs_fasta = pysam.Fastafile(contigs_fasta_file)
        self.ref_fasta = pysam.Fastafile(ref_fasta_file)
	self.annot = pysam.Tabixfile(annotation_file, parser=pysam.asGTF())
        self.aligner = aligner
        self.outdir = outdir
	self.debug = debug
        
        self.blocks_bed = '%s/blocks.bed' % outdir
        self.overlaps_bed = '%s/blocks_olap.bed' % outdir
        self.aligns = {}        

        self.annotations_file = annotation_file
	
	self.mappings = []
	self.events = []
	
	# initialize ITD conditions
	self.itd_conditions = {'min_len': itd_min_len,
	                       'min_pid': itd_min_pid,
	                       'max_apart': itd_max_apart
	                       }
					
    def map_contigs_to_transcripts(self):
	"""Maps contig alignments to transcripts, discovering variants at the same time"""
	# extract all transcripts info in dictionary
	transcripts = self.extract_transcripts()
	
	aligns = []
	for contig, group in groupby(self.bam.fetch(until_eof=True), lambda x: x.qname):
	    sys.stdout.write('analyzing %s\n' % contig)
            alns = list(group)
	    mappings = []
	    
	    aligns = self.extract_aligns(alns)
	    if aligns is None:
		sys.stdout.write('no valid alignment: %s\n' % contig)
		continue
	    
	    chimera = True if len(aligns) > 1 else False
	    chimera_block_matches = []
	    for align in aligns:
		if align is None:
		    sys.stdout.write('bad alignment: %s\n' % contig)
		    continue
		
		if re.search('[._Mm]', align.target):
		    sys.stdout.write('skip target:%s %s\n' % (contig, align.target))
		    continue
		
		if self.debug:
		    sys.stdout.write('contig:%s genome_blocks:%s contig_blocks:%s\n' % (align.query, 
		                                                                        align.blocks, 
		                                                                        align.query_blocks
		                                                                        ))
		
		# entire contig align within single exon or intron
		within_intron = []
		within_exon = []
		
		transcripts_mapped = Set()
		events = []
		# each gtf record corresponds to a feature
		for gtf in self.annot.fetch(align.target, align.tstart, align.tend):	
		    # contigs within single intron or exon
		    if not chimera and align.tstart >= gtf.start and align.tend <= gtf.end:
			match = self.match_exon((align.tstart, align.tend), (gtf.start, gtf.end)) 
			
			if gtf.feature == 'intron':	
			    within_intron.append((gtf, match))
			    
			elif gtf.feature == 'exon':
			    within_exon.append((gtf, match))
			  
		    # collect all the transcripts that have exon overlapping alignment
		    else:
			if gtf.feature == 'exon':
			    transcripts_mapped.add(gtf.transcript_id)
			    		
		if transcripts_mapped:
		    # key = transcript name, value = "matches"
		    all_block_matches = {}
		    # Transcript objects that are fully matched
		    full_matched_transcripts = []
		    for txt in transcripts_mapped:
			block_matches = self.map_exons(align.blocks, transcripts[txt].exons)
			all_block_matches[txt] = block_matches
			
			if not chimera and self.is_full_match(block_matches):
			    full_matched_transcripts.append(transcripts[txt])
			mappings.append((transcripts[txt], block_matches))
			    
		    if not full_matched_transcripts:	
			best_mapping = Mapping.pick_best(mappings, align, debug=True)
			self.mappings.append(best_mapping)
			
			# find events only for best transcript
			best_transcript = best_mapping.transcripts[0]
			events = self.find_events({best_transcript.id:all_block_matches[best_transcript.id]}, 
			                          align, 
			                          {best_transcript.id:best_transcript})
			#events = self.find_events(all_block_matches, align, transcripts)
			if events:
			    self.events.extend(events)
			elif self.debug:
			    sys.stdout.write('%s - partial but no events\n' % align.query)		    
			    			    
		    if chimera:
			chimera_block_matches.append(all_block_matches)
		    
		elif not chimera:
		    if within_exon:
			sys.stdout.write("contig mapped within single exon: %s %s:%s-%s %s\n" % (contig, 
			                                                                         align.target, 
			                                                                         align.tstart, 
			                                                                         align.tend, 
			                                                                         within_exon[0]
			                                                                         ))
		    
		    elif within_intron:
			sys.stdout.write("contig mapped within single intron: %s %s:%s-%s %s\n" % (contig, 
			                                                                           align.target, 
			                                                                           align.tstart, 
			                                                                           align.tend, 
			                                                                           within_intron[0]
			                                                                           ))
		
	    # split aligns, try to find gene fusion
	    if chimera and chimera_block_matches:
		if len(chimera_block_matches) == len(aligns):
		    fusion = FusionFinder.find_chimera(chimera_block_matches, transcripts, aligns)
		    if fusion:
			self.events.append(fusion)
		
	    ## pick best matches
	    #if len(mappings) > 1:
		#best_mapping = Mapping.pick_best(mappings, align, debug=True)
		#self.mappings.append(best_mapping)
					
    def map_exons(self, blocks, exons):
	"""Maps alignment blocks to exons
	
	Crucial in mapping contigs to transcripts
	
	Args:
	    blocks: (List) of 2-member list (start and end of alignment block)
	    exons: (List) of 2-member list (start and end of exon)
	Returns:
	    List of list of tuples, where
	         each item of the top-level list corresponds to each contig block
		 each item of the second-level list corresponds to each exon match
		 each exon-match tuple contains exon_index, 2-character matching string
		 e.g. [ [ (0, '>='), (1, '<=') ], [ (2, '==') ], [ (3, '=<') ], [ (3, '>=') ] ]
		 this says that the alignment has 4 blocks,
		 first block matches to exon 0 and 1, find a retained-intron,
		 second block matches perfectly to exon 2
		 third and fourth blocks matching to exon 3, possible a deletion or novel_intron
	"""
	result = []
	for b in range(len(blocks)):
	    block_matches = []
	    
	    for e in range(len(exons)):
		block_match = self.match_exon(blocks[b], exons[e])
		if block_match != '':
		    block_matches.append((e, block_match))
		    
	    if not block_matches:
		block_matches = None
	    result.append(block_matches)
		
	return result
		    		    
    def extract_transcripts(self):
	"""Extracts all exon info into transcript objects
	
	Requires annotation file passed to object
	Uses PyBedTool for parsing
	
	Returns:
	    List of Transcripts with exon info, strand
	"""
	transcripts = {}
	for feature in BedTool(self.annotations_file):
	    if feature[2] == 'exon':
		exon = (int(feature.start) + 1, int(feature.stop))
		exon_num = int(feature.attrs['exon_number'])
		transcript_id = feature.attrs['transcript_id']
		gene = feature.attrs['gene_name']
		strand = feature.strand
				
		try:
		    transcript = transcripts[transcript_id]
		except:
		    transcript = Transcript(transcript_id, gene=gene, strand=strand)
		    transcripts[transcript_id] = transcript
		    
		transcript.add_exon(exon)
		
	return transcripts
        
    def extract_novel_seq(self, adj):
	"""Extracts novel sequence in adjacency, should be a method of Adjacency"""
	start, end = (adj.contig_breaks[0], adj.contig_breaks[1]) if adj.contig_breaks[0] < adj.contig_breaks[1] else (adj.contig_breaks[1], adj.contig_breaks[0])
	novel_seq = self.contigs_fasta.fetch(adj.contigs[0], start, end - 1)
	return novel_seq
	    
    def find_events(self, matches_by_transcript, align, transcripts):
	"""Find events from single alignment
	
	Wrapper for finding events within a single alignment
	Maybe a read-through fusion, calls FusionFinder.find_read_through()
	Maybe splicing or indels, call NovelSpliceFinder.find_novel_junctions()
	Will take results in dictionary and convert them to Adjacencies
	
	Args:
	    matches_by_transcript: (list) dictionaries where 
	                                      key=transcript name, 
	                                      value=[match1, match2, ...] where
						    match1 = matches of each alignment block
							     i.e.
							     [(exon_id, '=='), (exon_id, '==')] 
							     or None if no_match
	    align: (Alignment) alignment object
	    transcripts: (dictionary) key=transcript_id value: Transcript
	Returns:
	    List of Adjacencies
	"""
	events = []
	
	# for detecting whether there is a read-through fusion
	genes = Set([transcripts[txt].gene for txt in matches_by_transcript.keys()])		
	if len(genes) > 1:
	    fusion = FusionFinder.find_read_through(matches_by_transcript, transcripts, align)
	    if fusion is not None:
		events.append(fusion)
	    	    	
	# events within a gene
	local_events = NovelSpliceFinder.find_novel_junctions(matches_by_transcript, align, transcripts, self.ref_fasta)
	if local_events:
	    for event in local_events:
		adj = Adjacency((align.target,), event['pos'], '-', contig=align.query)
		if event['event'] in ('ins', 'del', 'dup', 'inv'):
		    adj.rearrangement = event['event']
		adj.rna_event = event['event']
		adj.genes = (list(genes)[0],)
		adj.transcripts = (event['transcript'][0],)
		adj.size = event['size']
		
		# converts exon index to exon number (which takes transcript strand into account)
		exons = event['exons'][0]
		#if type(exons) is not list and type(exons) is not tuple:
		    #print 'check', align.query, event, exons
		    #continue
		adj.exons = map(transcripts[event['transcript'][0]].exon_num, exons)
		
		adj.contig_breaks = event['contig_breaks']
		
		if adj.rearrangement == 'ins' or adj.rearrangement == 'dup':
		    novel_seq = self.extract_novel_seq(adj)
		    adj.novel_seq = novel_seq if align.strand == '+' else reverse_complement(novel_seq)
		    
		    if len(novel_seq) >= self.itd_conditions['min_len']:
			ITD_Finder.detect_itd(adj, align, self.contigs_fasta.fetch(adj.contigs[0]), self.outdir, 
			                      self.itd_conditions['min_len'],
			                      self.itd_conditions['max_apart'],
			                      self.itd_conditions['min_pid'],
			                      debug=self.debug
			                      )
		    			
		    adj.size = len(adj.novel_seq)
		    
		elif adj.rearrangement == 'del':
		    adj.size = adj.breaks[1] - adj.breaks[0] - 1
		
		events.append(adj)
			    
	return events
		            
    def extract_aligns(self, alns):
	"""Generates Alignments objects of chimeric and single alignments

	Args:
	    alns: (list) Pysam.AlignedRead
	Returns:
	    List of Alignments that are either chimeras or single alignments
	"""
	try:
	    chimeric_aligns = {
		'gmap': gmap.find_chimera,
		}[self.aligner](alns, self.bam)
	    if chimeric_aligns:
		return chimeric_aligns
	    else:
		return [{
		    'gmap': gmap.find_single_unique,
		    }[self.aligner](alns, self.bam)]
	except:
	    sys.exit("can't convert \"%s\" alignments - abort" % self.aligner)
        
    def match_exon(self, block, exon):
	"""Match an alignment block to an exon
	
	Args:
	    block: (Tuple/List) start and end of an alignment block
	    exon: (Tuple/List) start and end of an exon
	Returns:
	    2 character string, each character the result of each coordinate
	    '=': block coordinate = exon coordinate
	    '<': block coordinate < exon coordinate
	    '>': block coordinate > exon coordinate
	"""
        assert len(block) == 2 and len(block) == len(exon), 'unmatched number of block(%d) and exon(%d)' % (len(block), len(exon))
        assert type(block[0]) is int and type(block[1]) is int and type(exon[0]) is int and type(exon[1]) is int,\
        'type of block and exon must be int'
        result = ''
        
	if min(block[1], exon[1]) - max(block[0], exon[0]) > 0:
	    for i in range(0, 2):
		if block[i] == exon[i]:
		    result += '='
		elif block[i] > exon[i]:
		    result += '>'
		else:
		    result += '<'
        
        return result
    
    def is_full_match(self, block_matches):
	"""Determines if a contig fully covers a transcript

	A 'full' match is when every exon boundary matches, except the terminal boundaries
	
	Args:
	    block_matches: List of list of tuples, where
			   each item of the top-level list corresponds to each contig block
			   each item of the second-level list corresponds to each exon match
			   each exon-match tuple contains exon_index, 2-character matching string
			   e.g. [ [ (0, '>='), (1, '<=') ], [ (2, '==') ], [ (3, '=<') ], [ (3, '>=') ] ]
			   this says that the alignment has 4 blocks,
			   first block matches to exon 0 and 1, find a retained-intron,
			   second block matches perfectly to exon 2
			   third and fourth blocks matching to exon 3, possible a deletion or novel_intron

	Returns:
	    True if full, False if partial
	"""
	if None in block_matches:
	    return False
	
	if len(block_matches) == 1:
	    if len(block_matches[0]) == 1:
		if block_matches[0][0][1] == '==' or block_matches[0][0][1] == '>=' or block_matches[0][0][1] == '=<':
		    return True
	    
	    return False
	
	# if a block is mapped to >1 exon
	if [m for m in block_matches if len(m) > 1]:
	    return False
	
	exons = [m[0][0] for m in block_matches]
	
	if block_matches[0][0][1][1] == '=' and\
	   block_matches[-1][0][1][0] == '=' and\
	   len([m for m in block_matches[1:-1] if m[0][1] == '==']) == len(block_matches) - 2 and\
	   (len([(a, b) for a, b in zip(exons, exons[1:]) if b == a + 1]) == len(block_matches) - 1 or\
	    len([(a, b) for a, b in zip(exons, exons[1:]) if b == a - 1]) == len(block_matches) - 1):
	    return True
		
	return False

def main(args, options):
    outdir = args[-1]
    # check executables
    required_binaries = ('gmap', 'blastn')
    for binary in required_binaries:
	which = find_executable(binary)
	if not which:
	    sys.exit('"%s" not in PATH - abort' % binary)
    
    # find events
    em = ExonMapper(*args, 
                    itd_min_len=options.itd_min_len,
                    itd_min_pid=options.itd_min_pid,
                    itd_max_apart=options.itd_max_apart,
                    debug=options.debug)
    em.map_contigs_to_transcripts()

    Mapping.output(em.mappings, '%s/contig_mappings.tsv' % outdir)
    gene_mappings = Mapping.group(em.mappings)
    Mapping.output(gene_mappings, '%s/gene_mappings.tsv' % outdir)
    
    Event.output(em.events, outdir)
    
if __name__ == '__main__':
    usage = "Usage: %prog c2g_bam aligner contigs_fasta annotation_file genome_file(indexed) out_dir"
    parser = OptionParser(usage=usage)
    
    parser.add_option("-b", "--r2c_bam", dest="r2c_bam_file", help="reads-to-contigs bam file")
    parser.add_option("-n", "--num_procs", dest="num_procs", help="number of processes. Default: 5", default=5, type=int)
    parser.add_option("--junctions", dest="junctions", help="output junctions", action="store_true", default=False)
    parser.add_option("--itd_min_len", dest="itd_min_len", help="minimum ITD length. Default: 10", default=10, type=int)
    parser.add_option("--itd_min_pid", dest="itd_min_pid", help="minimum ITD percentage of identity. Default: 0.95", default=0.95, type=float)
    parser.add_option("--itd_max_apart", dest="itd_max_apart", help="maximum distance apart of ITD. Default: 10", default=10, type=int)
    parser.add_option("--debug", dest="debug", help="debug mode", action="store_true", default=False)
    
    (options, args) = parser.parse_args()
    if len(args) == 6:
        main(args, options)     