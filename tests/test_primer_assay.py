"""Interpreting an observed primer pair into an assay description (issue #57).

The pipeline can only report which adapter name matched — a primer FASTA carries
nothing else, and cutadapt truncates headers at whitespace, so the name cannot be
annotated on the way through. Turning `341Fv3` into "bacterial/archaeal 16S rRNA,
V3-V4" is OMC's job, from the curated table.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.app import primers as pm


# ── every entry says which gene it means ──────────────────────────────────────

def test_every_primer_carries_gene_and_lineage():
    # A bare "16S" is the prokaryotic SSU to one field and the mitochondrial LSU
    # (rrnL) to another, so an entry without a lineage is not interpretable.
    missing = [p["name"] for p in pm.PRIMER_DB
               if not p.get("gene") or not p.get("lineage")]
    assert not missing, f"entries without gene/lineage: {missing[:5]}"


def test_genes_map_to_the_lineage_their_primers_target():
    seen = {(p["gene"], p["lineage"]) for p in pm.PRIMER_DB}
    assert ("16S rRNA", "Bacteria/Archaea") in seen
    assert ("18S rRNA", "Eukaryota") in seen
    assert ("ITS", "Fungi") in seen
    # and no gene maps to two different lineages
    by_gene = {}
    for gene, lineage in seen:
        by_gene.setdefault(gene, set()).add(lineage)
    assert all(len(v) == 1 for v in by_gene.values()), by_gene


# ── region label splitting ────────────────────────────────────────────────────

def test_split_region_handles_marker_and_subregion():
    assert pm._split_region("16S V3-V4") == ("16S", "V3-V4")
    assert pm._split_region("18S") == ("18S", None)


def test_split_region_treats_the_its_digit_as_the_spacer():
    # "ITS1" is the first spacer, not a V-region — and "fungal ITS2" has the
    # marker in the second token.
    assert pm._split_region("ITS1") == ("ITS", "1")
    assert pm._split_region("fungal ITS2") == ("ITS", "2")


def test_split_region_tolerates_nothing():
    assert pm._split_region(None) == (None, None)
    assert pm._split_region("  ") == (None, None)


# ── describe_pair: the interpretation OMC contributes ─────────────────────────

def test_observed_pairs_from_a_real_run_resolve():
    # Both winners actually seen in 1543a4c1's cutadapt logs (44 vs 40 samples).
    assert pm.describe_pair("341Fv3", "Bakt_805R") == {
        "gene": "16S rRNA", "lineage": "Bacteria/Archaea", "region": "V3-V4"}
    assert pm.describe_pair("A-528F", "B-706R") == {
        "gene": "18S rRNA", "lineage": "Eukaryota", "region": "V4"}


def test_forward_name_alone_is_enough_when_unambiguous():
    assert pm.describe_pair("341Fv3")["gene"] == "16S rRNA"


def test_region_is_omitted_when_the_name_spans_several():
    # Bakt_805R is the reverse for both 16S V3-V4 and 16S V4. The gene is still
    # certain; the sub-region is not, so it must not be invented.
    out = pm.describe_pair(None, "Bakt_805R")
    assert out["gene"] == "16S rRNA"
    assert "region" not in out


def test_unknown_primers_describe_nothing():
    assert pm.describe_pair("NotAPrimer", "AlsoNot") is None
    assert pm.describe_pair(None, None) is None
    assert pm.describe_pair("", "") is None
