# Vendored primer databases

`primer_pairs_bacteria.txt` (16S) and `primer_pairs_fungi.txt` (ITS) are
copied verbatim from **FoodMicrobionet** by Eugenio Parente (MIT License):

  https://github.com/ep142/FoodMicrobionet/tree/master/dada2_pipeline
  pinned commit 47ed6230899a9d6d4146f4937e053e73559886eb

Loaded into `primers.PRIMER_DB` by `_load_vendored_primers()`. FoodMicrobionet
covers bacteria (16S) and fungi (ITS) only; 18S / protist primers come from the
curated core in `primers.py`. Refresh by re-copying newer revisions of these
two files (schema: Target_region, primer_f_name, primer_f_seq, primer_r_name,
primer_r_seq, reference, expected_length/notes).
