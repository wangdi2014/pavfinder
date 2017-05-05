## *P*ost-*A*ssembly *V*ariant *Finder* (PAVFinder)

PAVFinder is a Python package that detects structural variants from *de novo* assemblies (e.g. [ABySS](http://www.bcgsc.ca/platform/bioinfo/software/abyss), [Trans-ABySS](http://www.bcgsc.ca/platform/bioinfo/software/trans-abyss)).  As such, it is able to analyse both genome and transcriptome assemblies:

### genome rearrangements `pavfinder genome`
- translocations
- inversions
- duplications
- insertions
- deletions

### transcriptomic structural variants `pavfinder fusion`
- gene fusions
- internal tandem duplications (ITD)
- partial tandem duplications (PTD)
- small indels
- simple-repeat expansions/contractions

### transcriptomic splice variants `pavfinder splice`
- skipped exons
- novel exons
- novel introns
- retained introns
- novel splice acceptors/donors

PAVFinder infers variants from non-contiguous (split or gapped) contig sequence alignments to the reference genome. Assemblies can be aligned to the reference genome (`c2g `alignment) using `bwa mem`(genome) or `gmap`(transcriptome).  Read support for events can be gathered by aligning reads to the contig assembly using `bwa mem` (`r2c` alignment).

A pipeline that bundles the 3 analysis steps called `TAP` (*T*ransabyss-*A*lignment-*P*AVFinder) is provided to facilitate whole transcriptome analysis. TAP is also designed to be run in a targeted mode on selected genes. This requires a [Bloom Filter](http://www.bcgsc.ca/platform/bioinfo/software/biobloomtools) of targeted gene sequences to be created beforehand. Whereas the full assembly of a single RNAseq library with over 100 million read pairs requires more than 24 hours to complete, a targeted assembly and analysis of a gene list (e.g. [COSMIC](http://cancer.sanger.ac.uk/census/)) of several hundred can be completed within half an hour.