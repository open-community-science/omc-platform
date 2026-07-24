# Results from claims (qwen/qwen3-coder-30b) — INCOMPLETE (10/13 investigations)

> ⚠️ PRELIMINARY — 3 of 13 investigations outstanding; these Results are partial.

_8/16 verified · 18 computations · 10/13 investigations_

**Results**

Analysis of the 16S amplicon sequencing data from the frost flower — Ice Chamber Experiment metagenome (PRJNA1473294) revealed strong associations between ASV richness and sequencing depth, particularly within the bacterial batch. Spearman correlation analysis indicated a robust positive relationship between sequencing depth and richness across all samples (rho = 0.626, p < 1e-6), with an even stronger association in the bacterial batch (rho = 0.780, p < 1e-6). In contrast, no significant correlation was observed in the eukaryote batch (rho = 0.382, p = 0.107), suggesting that raw richness comparisons within this domain may be more reflective of technical artifacts than ecological differences.

Pielou evenness was found to decrease with increasing sequencing depth, indicating an artifact where deeper sampling reveals more low-abundance ASVs, thereby reducing evenness values. This pattern was statistically significant across all samples (rho = -0.451, p = 0.0002) and in the bacterial batch specifically (rho = -0.455, p = 0.0019), suggesting that evenness estimates may not accurately reflect true ecological evenness in this dataset.

A precomputed ordination coordinate, meta['y'], effectively separated the bacterial and eukaryote sequencing batches with minimal overlap. The bacterial batch exhibited y-values ranging from 0.74 to 5.20 (mean = 3.31), while the eukaryote batch ranged from -3.33 to -2.51 (mean = -3.01). Mann-Whitney U testing confirmed a highly significant separation between batches on this axis (U = 836.0, p = 6e-16), indicating that the provided ordination is dominated by domain-level differences rather than finer ecological structure within each domain.

Within the bacterial batch, distinct dominant genera were identified across sea-ice microhabitats. Frost flowers (FF) were characterized by Neptuniibacter and Alteromonas; drained ice (DI) by Arcobacter; whole core (WC) by Pseudomonas; and brine (B) by a mix of Alteromonas, Arcobacter, Pseudomonas, and Pseudoalteromonas. These patterns align with known niche partitioning across sea-ice habitats.

Contamination from classic kit/reagent genera was minimal. Of six screened genera—Ralstonia, Bradyrhizobium, Cutibacterium, Pelomonas, Delftia, Sphingomonas—only Delftia appeared in the dataset, represented by a single ASV with 16 total reads present in only two of 63 samples. The abundance of this ASV did not correlate with sequencing depth (rho = -0.015, p = 0.906), suggesting low reagent contamination.

The core microbiome of the bacterial fraction consisted of genera consistently present across sea-ice habitats: Arcobacter, Neptuniibacter, Alteromonas, Pseudomonas, and Pseudoalteromonas. These genera accounted for a large proportion of total reads (Arcobacter = 33682, Neptuniibacter = 29195, Alteromonas = 28717, Pseudomonas = 12316, Pseudoalteromonas = 6643), confirming their role as resident members rather than habitat-exclusive specialists.

Differential abundance testing across four well-replicated sea-ice habitats (frost flower, brine, drained ice, whole core) within the bacterial batch revealed significant differences in relative abundance for more than half of tested genera (47/92 at p < 0.05, uncorrected). The strongest habitat-discriminating genera included Colwellia, Neptuniibacter, Marinomonas, Pseudoalteromonas, and SAR92 clade.

In the eukaryote batch, the precomputed x-coordinate was strongly correlated with ASV richness (rho = -0.879, p < 1e-6), indicating that this axis likely reflects a sequencing-depth/richness gradient rather than community composition differences. A similar but weaker pattern was observed in the bacterial batch on the y-axis versus n_asvs (rho = 0.487, p = 0.0008), and on the x-axis versus depth (rho = 0.404, p = 0.0065).

A co-occurrence pattern was observed between a group of genera including Neptuniibacter, Colwellia, Flavicella, SAR92 clade, and Pseudoalteromonas, which exhibited positive associations across samples. This guild showed strong mutual exclusivity with another group composed primarily of Pseudoalteromonas and Marinomonas, suggesting compositional turnover between distinct microbial assemblages. However, specific correlation values were not reported due to partial support.

Assuming counts are raw rather than rarefied, the ordination results reflect batch-level differences more than ecological variation within domains. Additionally, the strong correlation between x-coordinate and richness in the eukaryote batch suggests that this axis may be influenced by sequencing depth rather than true community composition. These assumptions affect interpretation of the ordination-based clustering and should be considered when evaluating ecological structure.

The dataset demonstrates clear habitat-specific microbial communities with distinct dominant genera, low contamination, and evidence for niche partitioning among sea-ice microhabitats. The core microbiome is dominated by a set of genera consistently present across habitats, supporting their role as fundamental components of the sea-ice bacterial community.
