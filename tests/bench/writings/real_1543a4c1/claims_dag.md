# Claim provenance DAG (qwen/qwen3-coder-30b) — INCOMPLETE (10/13 investigations)

8/16 claims verified · 18 computations

Legend: 🟢 replicated · 🟩 verified · 🟪 disputed · 🟧 partly supported · 🟥 refuted · 🟨 unverifiable · 🟦 computation · ⬛ data

```mermaid
graph LR
  a1{{"Does the data actually support the '16S amplicon' label, or is this a "}}
  style a1 fill:#00695c,color:#fff
  a2{{"How does sequencing depth (total_reads) vary across samples, and how m"}}
  style a2 fill:#00695c,color:#fff
  a3{{"What is the dominance structure / rare biosphere shape (rank-abundance"}}
  style a3 fill:#00695c,color:#fff
  a4{{"Do samples separate into distinct clusters in beta-diversity ordinatio"}}
  style a4 fill:#00695c,color:#fff
  a5{{"Which taxa drive the separation between clusters/groups seen in ordina"}}
  style a5 fill:#00695c,color:#fff
  a6{{"Is there a strong prokaryote vs eukaryote compositional split among sa"}}
  style a6 fill:#00695c,color:#fff
  a7{{"Are known kit/reagent contaminant genera (Ralstonia, Bradyrhizobium, C"}}
  style a7 fill:#00695c,color:#fff
  a8{{"What are the core (high-prevalence) vs transient (low-prevalence) taxa"}}
  style a8 fill:#00695c,color:#fff
  a9{{"Is there differential abundance of taxa between the two accession batc"}}
  style a9 fill:#00695c,color:#fff
  a10{{"Do any taxa show co-occurrence or mutual exclusion patterns (correlati"}}
  style a10 fill:#00695c,color:#fff
  a11{{"Does the precomputed ordination (x/y) correlate with sequencing depth "}}
  style a11 fill:#00695c,color:#fff
  a12{{"Is collection_date informative (real sampling gradient) or just a data"}}
  style a12 fill:#00695c,color:#fff
  a13{{"Within each domain batch, does Bray-Curtis structure separate by sampl"}}
  style a13 fill:#00695c,color:#fff
  c1[["inspect shapes"]]
  style c1 fill:#1565c0,color:#fff
  c2[["domain composition per sample"]]
  style c2 fill:#1565c0,color:#fff
  c3[["domain purity per sample and batch alignment"]]
  style c3 fill:#1565c0,color:#fff
  c4[["check description/library_strategy consistency within eukaryote batch"]]
  style c4 fill:#1565c0,color:#fff
  c5[["alpha diversity vs depth"]]
  style c5 fill:#1565c0,color:#fff
  c6[["alpha diversity vs depth split by domain batch"]]
  style c6 fill:#1565c0,color:#fff
  c7[["rank abundance and rare biosphere"]]
  style c7 fill:#1565c0,color:#fff
  c8[["check min counts and top taxa IDs"]]
  style c8 fill:#1565c0,color:#fff
  c9[["bray-curtis PCoA vs precomputed xy"]]
  style c9 fill:#1565c0,color:#fff
  c10[["clustering by domain in bray curtis space and meta xy separation check"]]
  style c10 fill:#1565c0,color:#fff
  c11[["within-batch sample type parsing"]]
  style c11 fill:#1565c0,color:#fff
  c12[["PERMANOVA-like test for sample type within bacteria batch"]]
  style c12 fill:#1565c0,color:#fff
  c13[["indicator taxa by sample type within bacteria batch"]]
  style c13 fill:#1565c0,color:#fff
  c14[["contamination screen for kit genera"]]
  style c14 fill:#1565c0,color:#fff
  c15[["core vs transient taxa prevalence"]]
  style c15 fill:#1565c0,color:#fff
  c16[["Kruskal-Wallis differential abundance per genus across habitat types in bacteria batch"]]
  style c16 fill:#1565c0,color:#fff
  c17[["co-occurrence network among core/abundant genera in bacteria batch"]]
  style c17 fill:#1565c0,color:#fff
  c18[["xy vs depth/richness technical check within each batch"]]
  style c18 fill:#1565c0,color:#fff
  k1["The dataset is not pure 16S: SILVA domain classification spl"]
  style k1 fill:#6a1b9a,color:#fff
  get_dataset('study').caveat("get_dataset('study').caveat")
  style get_dataset('study').caveat fill:#455a64,color:#fff
  k2["Both accession batches sample the same underlying site/mater"]
  style k2 fill:#f9a825,color:#000
  k3["ASV richness is strongly confounded with sequencing depth, e"]
  style k3 fill:#2e7d32,color:#fff
  k4["Pielou evenness decreases with increasing sequencing depth, "]
  style k4 fill:#2e7d32,color:#fff
  k5["This ASV table has already been pre-filtered to remove singl"]
  style k5 fill:#880e4f,color:#fff
  k6["The community is moderately dominated by a few taxa: the sin"]
  style k6 fill:#4527a0,color:#fff
  k7["Between the two domain batches (bacteria vs eukaryote runs),"]
  style k7 fill:#4527a0,color:#fff
  k8["The precomputed ordination coordinate meta['y'] almost perfe"]
  style k8 fill:#1b5e20,color:#fff
  k9["Within each domain batch separately, sample/habitat type (fr"]
  style k9 fill:#4527a0,color:#fff
  k10["Distinct dominant genera characterize each sea-ice habitat t"]
  style k10 fill:#2e7d32,color:#fff
  k11["Classic kit/reagent contaminant genera are essentially absen"]
  style k11 fill:#2e7d32,color:#fff
  k12["Core taxa (≥50% sample prevalence) are a tiny fraction of to"]
  style k12 fill:#880e4f,color:#fff
  k13["The core microbiome genera consistently present across sea-i"]
  style k13 fill:#2e7d32,color:#fff
  k14["Differential abundance testing (Kruskal-Wallis) across the f"]
  style k14 fill:#2e7d32,color:#fff
  k15["A positively co-occurring guild of genera (Neptuniibacter, C"]
  style k15 fill:#ef6c00,color:#fff
  k16["Within the eukaryote batch, the precomputed ordination x-coo"]
  style k16 fill:#1b5e20,color:#fff
  a4 --> a13
  a1 --> k1
  c2 --> k1
  c3 --> k1
  c4 --> k1
  get_dataset('study').caveat --> k1
  a1 --> k2
  c4 --> k2
  a2 --> k3
  c5 --> k3
  c6 --> k3
  a2 --> k4
  c5 --> k4
  c6 --> k4
  a3 --> k5
  c7 --> k5
  c8 --> k5
  a3 --> k6
  c7 --> k6
  c8 --> k6
  a4 --> k7
  c10 --> k7
  a4 --> k8
  c10 --> k8
  a5 --> k9
  c12 --> k9
  a5 --> k10
  c13 --> k10
  a7 --> k11
  c14 --> k11
  a8 --> k12
  c15 --> k12
  a8 --> k13
  c15 --> k13
  c13 --> k13
  a9 --> k14
  c16 --> k14
  a10 --> k15
  c17 --> k15
  c13 --> k15
  a11 --> k16
  c18 --> k16
```
