# Results from claims (qwen/qwen3.6-35b-a3b)

_18/18 verified · 30 computations · 2 unsupported numbers_

**Results**

The initial dataset comprised 84 frost flower samples yielding 1,398,204 raw reads. Following primer removal, 860,222 reads remained. After quality filtering, 519,769 reads were retained, representing 37.2% of the initial raw reads. Subsequent denoising and renormalization reduced the dataset from 84 samples to 11 samples, with 73 samples (86.9%) lost during this step. This substantial sample attrition may bias downstream analyses. The retention of only 37.2% of raw reads following aggressive quality filtering may have removed valid biological signal. Additionally, one chloroplast ASV was detected in three samples with 26 total reads, indicating potential mitochondrial or chloroplast contamination.

A total of 161 prokaryotic ASVs were retained across the 11 samples, comprising 46,656 total reads. These ASVs, along with one additional unclassified ASV, were classified against the SILVA database, resulting in 162 classified ASVs. Taxonomic classification at the phylum level identified Pseudomonadota (116 ASVs), Bacteroidota (22), Campylobacterota (13), Verrucomicrobiota (4), and Actinomycetota (3). Classification completeness was high at higher taxonomic ranks, with 162/162 ASVs classified at the domain and phylum levels, 161/162 at the class and order levels, 158/162 at the family level, and 137/162 at the genus level.

Total reads per sample ranged from 1,218 in SRR38958128 to 7,026 in SRR38958118. Shannon diversity (bits) per sample ranged from 2.01 to 4.71, with a mean of 3.82. Observed richness (ASVs per sample) ranged from 19 to 98, with a mean of 58.73. Among the retained samples, SRR38958118 exhibited the highest Shannon diversity (4.71 bits) and highest observed richness (98 ASVs), whereas SRR38958147 displayed the lowest Shannon diversity (2.01 bits) and lowest observed richness (32 ASVs) (Table 1).

The mean pairwise Bray-Curtis dissimilarity between samples was 0.635. Principal component analysis (PCA) performed on log-transformed counts revealed that PC1 explained 36.76% of the
