import os
from setuptools import setup, find_packages
from pavfinder import __version__

setup(
    name='pavfinder',
    version=__version__,
    description='Post Assembly Variant Finder',
    long_description='Identifies genomic structural variants or transcriptomic splice variants given the alignments of assembly contigs',
    url='https://github.com/bcgsc/pavfinder.git',
    author='Readman Chiu',
    author_email='rchiu@bcgsc.ca',
    license='BCCA',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Programming Language :: Python :: 2.7',
        'Intended Audience :: Science/Research',
        'Topic :: Scientific/Engineering :: Bio-Informatics',
        ],
    packages=find_packages(),
    install_requires = [
        'pysam>=0.8.1',
        'pybedtools>=0.7.0',
        'intspan>=0.701',
        'numpy>=1.9.2',
        'pandas',
        'biopython',
        'ruffus',
        'python-igraph',
        ],
    package_data = {'pavfinder': ['test/*', 'scripts/*']},
    data_files = [('config', ['pavfinder/cfg/tap.cfg']), 
                  ('test/genome', ["pavfinder/test/genome/c2g.bam",
                                   "pavfinder/test/genome/r2c.bam",
                                   "pavfinder/test/genome/r2c.bam.bai",
                                   "pavfinder/test/genome/test.fa"]),
                  ('test/genome/expected_output', ["pavfinder/test/genome/expected_output/adjacencies.bedpe",
                                                   "pavfinder/test/genome/expected_output/variants.vcf",
                                                   "pavfinder/test/genome/expected_output/adjacencies_filtered.bedpe",
                                                   "pavfinder/test/genome/expected_output/variants_filtered.vcf",
                                                   "pavfinder/test/genome/expected_output/coords.tsv",
                                                   "pavfinder/test/genome/expected_output/support.tsv"]),
                  ('test/transcriptome', ["pavfinder/test/transcriptome/c2g.bam",
                                          "pavfinder/test/transcriptome/c2t.bam",
                                          "pavfinder/test/transcriptome/r2c.bam",
                                          "pavfinder/test/transcriptome/r2c.bam.bai",
                                          "pavfinder/test/transcriptome/refGene.fa",
                                          "pavfinder/test/transcriptome/refGene.fa.amb",
                                          "pavfinder/test/transcriptome/refGene.fa.ann",
                                          "pavfinder/test/transcriptome/refGene.fa.bwt",
                                          "pavfinder/test/transcriptome/refGene.fa.fai",
                                          "pavfinder/test/transcriptome/refGene.fa.pac",
                                          "pavfinder/test/transcriptome/refGene.fa.sa",
                                          "pavfinder/test/transcriptome/refGene.sorted.gtf.gz",
                                          "pavfinder/test/transcriptome/refGene.sorted.gtf.gz.tbi",
                                          "pavfinder/test/transcriptome/ensGene.sorted.gtf.gz",
                                          "pavfinder/test/transcriptome/ensGene.sorted.gtf.gz.tbi",
                                          "pavfinder/test/transcriptome/test_1.fastq.gz",
                                          "pavfinder/test/transcriptome/test_2.fastq.gz",
                                          "pavfinder/test/transcriptome/test.cfg",
                                          "pavfinder/test/transcriptome/test.fa",
                                          "pavfinder/test/transcriptome/test_genes.bf",
                                          "pavfinder/test/transcriptome/test_genes.bf.sdsl",
                                          "pavfinder/test/transcriptome/test_genes_ids.txt"]),
                  ('test/transcriptome/expected_output/pavfinder', ["pavfinder/test/transcriptome/expected_output/pavfinder/junctions.bed",
                                                                    "pavfinder/test/transcriptome/expected_output/pavfinder/mappings.tsv",
                                                                    "pavfinder/test/transcriptome/expected_output/pavfinder/novel_splicing.bedpe",
                                                                    "pavfinder/test/transcriptome/expected_output/pavfinder/sv.bedpe"]),
                  ('test/transcriptome/expected_output', ["pavfinder/test/transcriptome/expected_output/tap.tar.gz"]),
                  ],
    scripts = ['pavfinder/scripts/pavfinder',
               'pavfinder/scripts/check_support.py',
               'pavfinder/scripts/find_sv_genome.py',
               'pavfinder/scripts/find_sv_transcriptome.py',
               'pavfinder/scripts/map_splice.py',
               'pavfinder/scripts/extract_transcript_sequence.py',
               'pavfinder/scripts/tap.py',
               'pavfinder/scripts/rescue_fusion.py',
               ],
)
