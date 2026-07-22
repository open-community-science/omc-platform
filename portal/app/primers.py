"""Primer resolution for amplicon (microscape) submissions.

Amplicon reads still carry their PCR primers at the 5' end; microscape needs to
know them to trim before denoising. We resolve them in three tiers:

  1. metadata  — parse primer names/sequences from SRA/ENA/BioSample fields
  2. manual    — user-entered forward/reverse sequences (set via the submission
                 sheet; handled in the route, stored on the submission)
  3. inferred  — guess from a sample of reads:
                 (a) match 5' ends against a curated primer database, and if
                     nothing scores well,
                 (b) de-novo: build a degenerate IUPAC consensus of the 5'
                     prefix (the conserved primer) across the sampled reads.

The resolved primers are stored on ``Submission.primers`` as
``{fwd, rev, fwd_name, rev_name, region, source, confidence}`` and wired into
microscape as ``--primers_fwd``/``--primers_rev``.
"""
from __future__ import annotations

import csv
import gzip
import logging
import math
import os
import re
import subprocess
from collections import Counter

# IUPAC nucleotide codes → the set of bases each represents.
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "GC", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}
# Reverse map: frozenset of bases → the tightest IUPAC code.
_IUPAC_REV = {frozenset(v): k for k, v in IUPAC.items()}

# Curated primer database — common 16S/18S/ITS amplicon primers (5'->3'),
# mirroring microscape's bundled sets plus a few widely used pairs. Sequences
# use IUPAC degeneracy; the reverse primer is written 5'->3' as synthesised.
# Curated core of named primer pairs, hand-verified against real reads. This is
# the canonical layer: 18S / protist primers (which FoodMicrobionet does not
# cover) plus the standard 16S/ITS pairs with clean names. The vendored
# FoodMicrobionet tables are merged on top for breadth (see _load_vendored_
# primers), deduped by sequence so these canonical entries win name resolution.
#
# Sequences are the biological primer only (5'->3', IUPAC, adapters stripped).
# Detection matches on SEQUENCE, never on the name in SRA metadata — EMP renamed
# 515FB->515F(Parada)/806R(Apprill), so submitter names are unreliable (exactly
# what mislabelled PRJNA1473294's 18S runs as "16S"). Sources: Herlemann 2011;
# Parada 2016 / Apprill 2015 / EMP; Caporaso 2011; Quince 2011; Lane 1991;
# Stoeck 2010; Amaral-Zettler 2009; Comeau 2011; White 1990; Gardes & Bruns
# 1993; Ihrmark 2012; UNITE; pr2-primers (Vaulot 2022).
_CORE_PRIMER_DB = [
    # ── 16S rRNA (bacteria / archaea) ──
    {"name": "341F", "rev_name": "805R", "region": "16S V3-V4",
     "fwd": "CCTACGGGNGGCWGCAG", "rev": "GACTACHVGGGTATCTAATCC"},
    {"name": "515F", "rev_name": "806R", "region": "16S V4",  # Parada/Apprill (EMP)
     "fwd": "GTGYCAGCMGCCGCGGTAA", "rev": "GGACTACNVGGGTWTCTAAT"},
    {"name": "515F", "rev_name": "806R", "region": "16S V4",  # Caporaso 2011 (original)
     "fwd": "GTGCCAGCMGCCGCGGTAA", "rev": "GGACTACHVGGGTWTCTAAT"},
    {"name": "515F", "rev_name": "926R", "region": "16S V4-V5",  # EMP long
     "fwd": "GTGYCAGCMGCCGCGGTAA", "rev": "CCGYCAATTYMTTTRAGTTT"},
    {"name": "27F", "rev_name": "1492R", "region": "16S (near full length)",
     "fwd": "AGAGTTTGATCMTGGCTCAG", "rev": "TACGGYTACCTTGTTACGACTT"},
    # ── 18S rRNA (eukaryotes / protists) ──
    {"name": "TAReuk454FWD1", "rev_name": "TAReukREV3", "region": "18S V4",
     "fwd": "CCAGCASCYGCGGTAATTCC", "rev": "ACTTTCGTTCTTGATYRA"},
    {"name": "E572F", "rev_name": "E1009R", "region": "18S V4",  # Comeau 2011
     "fwd": "CYGCGGTAATTCCAGCTC", "rev": "AYGGTATCTRATCRTCTTYG"},
    {"name": "Euk1391F", "rev_name": "EukBr", "region": "18S V9",  # EMP
     "fwd": "GTACACACCGCCCGTC", "rev": "TGATCCTTCTGCAGGTTCACCTAC"},
    {"name": "1389F", "rev_name": "1510R", "region": "18S V9",  # Amaral-Zettler 2009
     "fwd": "TTGTACACACCGCCC", "rev": "CCTTCYGCAGGTTCACCTAC"},
    # ── ITS (fungi) ──
    {"name": "ITS1F", "rev_name": "ITS2", "region": "fungal ITS1",
     "fwd": "CTTGGTCATTTAGAGGAAGTAA", "rev": "GCTGCGTTCTTCATCGATGC"},
    {"name": "ITS1", "rev_name": "ITS4", "region": "fungal ITS (full)",  # White 1990
     "fwd": "TCCGTAGGTGAACCTGCGG", "rev": "TCCTCCGCTTATTGATATGC"},
    {"name": "ITS3", "rev_name": "ITS4", "region": "fungal ITS2",  # White 1990
     "fwd": "GCATCGATGAAGAACGCAGC", "rev": "TCCTCCGCTTATTGATATGC"},
    {"name": "gITS7", "rev_name": "ITS4", "region": "fungal ITS2",  # Ihrmark 2012
     "fwd": "GTGARTCATCGARTCTTTG", "rev": "TCCTCCGCTTATTGATATGC"},
]

_IUPAC_CHARS = set("ACGTRYSWKMBDHVN")
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load_vendored_primers() -> list[dict]:
    """Parse the vendored FoodMicrobionet primer tables (16S + ITS).

    MIT-licensed data from github.com/ep142/FoodMicrobionet — see data/README.md.
    Schema: Target_region, primer_f_name, primer_f_seq, primer_r_name,
    primer_r_seq, reference, expected_length|notes. Skips rows that are empty,
    contain non-IUPAC characters (a stray typo), or are adapter-laden (a real
    metabarcoding primer is <=30 bp; longer entries carry sequencing adapters
    that wouldn't match demultiplexed reads).
    """
    out = []
    for fname, marker in (("primer_pairs_bacteria.txt", "16S"),
                          ("primer_pairs_fungi.txt", "ITS")):
        path = os.path.join(_DATA_DIR, fname)
        try:
            with open(path, encoding="latin-1") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                for row in reader:
                    fwd = (row.get("primer_f_seq") or "").strip().upper()
                    rev = (row.get("primer_r_seq") or "").strip().upper()
                    if not fwd or not rev:
                        continue
                    if len(fwd) > 30 or len(rev) > 30:
                        continue  # adapter/pad-laden, not a clean primer
                    if set(fwd) - _IUPAC_CHARS or set(rev) - _IUPAC_CHARS:
                        continue  # stray non-nucleotide character
                    region = (row.get("Target_region") or "").strip()
                    out.append({
                        "name": (row.get("primer_f_name") or "?").strip(),
                        "rev_name": (row.get("primer_r_name") or "?").strip(),
                        "region": f"{marker} {region}".strip() if region else marker,
                        "fwd": fwd, "rev": rev,
                    })
        except OSError as e:
            logging.getLogger(__name__).warning("primer table %s unreadable: %s", fname, e)
    return out


def _build_primer_db() -> list[dict]:
    """Core (canonical, verified) primers first, then vendored ones deduped by
    sequence — so a pair we curated keeps its clean name over any FMBN variant."""
    db, seen = [], set()
    for p in _CORE_PRIMER_DB + _load_vendored_primers():
        key = (p["fwd"], p["rev"])
        if key in seen:
            continue
        seen.add(key)
        db.append(p)
    return db


PRIMER_DB = _build_primer_db()

_DB_MATCH_MIN = 0.6   # min fraction of reads whose 5' matches a DB forward primer
_CONSENSUS_STOP_ENTROPY = 1.7  # bits; above this a column is "biological", stop


def _iupac_regex(seq: str) -> re.Pattern:
    """Compile an IUPAC-aware regex matching `seq` against plain-ACGT reads."""
    return re.compile("".join(f"[{IUPAC.get(b, b)}]" for b in seq.upper()))


def sample_reads(fastq_path: str, n: int = 500) -> list[str]:
    """Return up to `n` sequence lines from a (optionally gzipped) FASTQ."""
    reads: list[str] = []
    opener = gzip.open if str(fastq_path).endswith(".gz") else open
    try:
        with opener(fastq_path, "rt") as f:
            for i, line in enumerate(f):
                if i % 4 == 1:
                    reads.append(line.strip().upper())
                    if len(reads) >= n:
                        break
    except (OSError, EOFError):
        pass
    return reads


def _match_fraction(reads: list[str], primer: str, max_offset: int = 3) -> float:
    """Fraction of reads whose 5' end matches `primer` (small offset allowed)."""
    if not reads:
        return 0.0
    rx = _iupac_regex(primer)
    L = len(primer)
    hits = 0
    for r in reads:
        for off in range(max_offset + 1):
            seg = r[off:off + L]
            if len(seg) == L and rx.match(seg):
                hits += 1
                break
    return hits / len(reads)


def _consensus_primer(reads: list[str], length: int = 25, cover: float = 0.9) -> str:
    """De-novo degenerate consensus of the first `length` bp across `reads`.

    At each position, pick the smallest set of bases covering `cover` of the
    reads and emit its IUPAC code. Stop at the first high-entropy column — that
    is where the conserved primer ends and the biological (variable) region
    begins.
    """
    out: list[str] = []
    for i in range(length):
        col = [r[i] for r in reads if len(r) > i and r[i] in "ACGT"]
        if len(col) < max(10, 0.5 * len(reads)):
            break
        counts = Counter(col)
        tot = len(col)
        ent = -sum((c / tot) * math.log2(c / tot) for c in counts.values())
        bases: list[str] = []
        acc = 0
        for b, c in counts.most_common():
            bases.append(b)
            acc += c
            if acc / tot >= cover:
                break
        if ent > _CONSENSUS_STOP_ENTROPY and len(bases) >= 3:
            break
        out.append(_IUPAC_REV.get(frozenset(bases), "N"))
    return "".join(out)


def detect_from_reads(r1_path: str, r2_path: str | None = None, n: int = 500) -> dict | None:
    """Tier 3. Infer primers from a sample of reads (given FASTQ paths)."""
    r1 = sample_reads(r1_path, n)
    r2 = sample_reads(r2_path, n) if r2_path else []
    return detect_from_read_lists(r1, r2)


def detect_from_read_lists(r1: list[str], r2: list[str] | None = None) -> dict | None:
    """Tier 3 core, operating on already-sampled reads.

    Split out from detect_from_reads so callers that have reads in hand (e.g.
    detect_primer_sets, which re-probes the same sample against several
    candidate sets) don't re-read the FASTQ each time.

    Returns a primer dict, or None if there aren't enough reads to try.
    """
    r2 = r2 or []
    if len(r1) < 20:
        return None

    # 3a — database match: score each pair by forward match on R1 (and, if we
    # have R2, reverse match on R2), keep the best.
    best = None
    for p in PRIMER_DB:
        fwd_frac = _match_fraction(r1, p["fwd"])
        rev_frac = _match_fraction(r2, p["rev"]) if r2 else None
        score = fwd_frac if rev_frac is None else (fwd_frac + rev_frac) / 2
        if best is None or score > best["score"]:
            best = {**p, "score": score, "fwd_frac": fwd_frac, "rev_frac": rev_frac}
    if best and best["fwd_frac"] >= _DB_MATCH_MIN:
        return {
            "fwd": best["fwd"], "rev": best["rev"],
            "fwd_name": best["name"], "rev_name": best["rev_name"],
            "region": best["region"], "source": "inferred-db",
            "confidence": round(best["score"], 2),
        }

    # 3b — de-novo consensus of the conserved 5' prefix.
    fwd = _consensus_primer(r1)
    rev = _consensus_primer(r2) if r2 else ""
    if len(fwd) < 8:
        return None
    return {
        "fwd": fwd, "rev": rev, "fwd_name": "inferred", "rev_name": "inferred",
        "region": "unknown", "source": "inferred-denovo", "confidence": None,
    }


# ── Tier 1: metadata ────────────────────────────────────────────────────────

# Named-primer lookup for when metadata gives a name but not a sequence.
# First occurrence wins so a bare "515F" resolves to the standard V4 (806R) pair,
# not a later V4-V5 variant sharing the forward-primer name.
_NAME_TO_PRIMER: dict[str, dict] = {}
for _p in PRIMER_DB:
    _NAME_TO_PRIMER.setdefault(_p["name"].lower(), _p)
# Region phrases → a representative primer pair.
_REGION_HINTS = [
    (re.compile(r"v3.?v4", re.I), "341F"),
    (re.compile(r"\bv4\b", re.I), "515F"),
    (re.compile(r"v4.?v5", re.I), "515F"),
    (re.compile(r"\bits\b", re.I), "ITS1F"),
    (re.compile(r"18s|eukary", re.I), "TAReuk454FWD1"),
]
_SEQ_RE = re.compile(r"[ACGTRYSWKMBDHVN]{15,30}", re.I)


def parse_metadata_primers(metadata: dict | None) -> dict | None:
    """Tier 1. Extract primers from SRA/ENA/BioSample metadata fields.

    Looks for explicit forward/reverse sequences first, then named primers or a
    target region in free-text fields (e.g. library_construction_protocol).
    Returns a primer dict or None.
    """
    if not metadata:
        return None
    # Flatten string values we might search (top level + a nested 'attributes'/
    # 'samples' bag if present).
    text_fields: list[str] = []
    kv: dict[str, str] = {}
    for k, v in metadata.items():
        if isinstance(v, str):
            kv[k.lower()] = v
            text_fields.append(v)
    for key in ("pcr_primers", "primers", "primer"):
        if key in kv:
            seqs = _SEQ_RE.findall(kv[key])
            if len(seqs) >= 2:
                return {"fwd": seqs[0].upper(), "rev": seqs[1].upper(),
                        "fwd_name": "metadata", "rev_name": "metadata",
                        "region": kv.get("target_subfragment", "") or kv.get("target_gene", ""),
                        "source": "metadata", "confidence": None}
    blob = " ".join(text_fields)
    # Named primer, e.g. "341F/805R" in the protocol text.
    for name, p in _NAME_TO_PRIMER.items():
        if re.search(rf"\b{re.escape(name)}\b", blob, re.I):
            return {"fwd": p["fwd"], "rev": p["rev"], "fwd_name": p["name"],
                    "rev_name": p["rev_name"], "region": p["region"],
                    "source": "metadata", "confidence": None}
    # Region phrase.
    for rx, pname in _REGION_HINTS:
        if rx.search(blob):
            p = _NAME_TO_PRIMER[pname.lower()]
            return {"fwd": p["fwd"], "rev": p["rev"], "fwd_name": p["name"],
                    "rev_name": p["rev_name"], "region": p["region"],
                    "source": "metadata", "confidence": None}
    return None


def fetch_read_sample(run_accession: str, spots: int = 1000) -> tuple[str, str] | None:
    """Download a small read sample for one run via sra-toolkit (fast, offline
    later). Returns (r1_path, r2_path) of gzipped FASTQs in a temp dir, or None.
    """
    import tempfile
    d = tempfile.mkdtemp(prefix="omc-primer-")
    try:
        # fastq-dump (not fasterq-dump) supports -X to fetch just the first N spots,
        # which is fast and enough to identify primers.
        subprocess.run(
            ["fastq-dump", "-X", str(spots), "--split-files", "-O", d, run_accession],
            check=True, capture_output=True, timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    import os
    r1 = os.path.join(d, f"{run_accession}_1.fastq")
    r2 = os.path.join(d, f"{run_accession}_2.fastq")
    if os.path.exists(r1):
        return (r1, r2 if os.path.exists(r2) else None)
    return None


def resolve(metadata: dict | None, manual: dict | None,
            fastq_r1: str | None = None, fastq_r2: str | None = None) -> dict | None:
    """Resolve primers by tier precedence: manual > metadata > inferred.

    `manual` is {fwd, rev} from the submission sheet (empty strings ignored).
    Read paths, if given, enable the inferred tier. Returns a primer dict or None.
    """
    if manual and manual.get("fwd") and manual.get("rev"):
        return {"fwd": manual["fwd"].upper(), "rev": manual["rev"].upper(),
                "fwd_name": "manual", "rev_name": "manual", "region": "",
                "source": "manual", "confidence": None}
    meta = parse_metadata_primers(metadata)
    if meta:
        return meta
    if fastq_r1:
        return detect_from_reads(fastq_r1, fastq_r2)
    return None


def detect_primer_sets(
    accessions: list[str],
    max_sets: int = 4,
    max_probe: int = 12,
    spots: int = 600,
) -> list[dict]:
    """Discover the distinct primer sets used across a run selection.

    Sampling one run and applying its primers to everything is how a mixed
    BioProject gets destroyed: PRJNA1473294 labels all 84 runs "16S" but 40 are
    eukaryotic 18S, and forcing 341F/805R on those discarded 99.9% of reads.

    Rather than fetching every run (each is an SRA download), work adaptively:
    detect a set from one run, cheaply test that set against the others, then
    only fetch a run the known sets *fail* to explain. Each fetch therefore
    discovers a genuinely new set instead of re-confirming a known one.

    Returns a list of primer dicts, each with `runs` (accessions it explains)
    and `n_runs`, ordered by coverage. Empty if nothing could be sampled.
    """
    accs = [a for a in accessions if a]
    if not accs:
        return []

    cache: dict[str, tuple[list[str], list[str]]] = {}

    def reads_for(acc: str) -> tuple[list[str], list[str]]:
        """Sampled (R1, R2) reads for a run — fetched once."""
        if acc not in cache:
            got = fetch_read_sample(acc, spots=spots)
            if not got:
                cache[acc] = ([], [])
            else:
                r1p, r2p = got
                cache[acc] = (sample_reads(r1p, 300), sample_reads(r2p, 300) if r2p else [])
        return cache[acc]

    def explains(primer: dict, acc: str, threshold: float = 0.5) -> bool:
        """Does this primer set actually match the reads of `acc`?"""
        r1, _ = reads_for(acc)
        if len(r1) < 20:
            return False  # can't tell — don't claim it's explained
        return _match_fraction(r1, primer["fwd"]) >= threshold

    # Probe a spread of the selection rather than the first N, so a project
    # ordered by amplicon type doesn't hide its second half.
    if len(accs) <= max_probe:
        probe = list(accs)
    else:
        step = len(accs) / max_probe
        probe = [accs[int(i * step)] for i in range(max_probe)]

    sets: list[dict] = []
    unexplained = list(probe)

    while unexplained and len(sets) < max_sets:
        seed = unexplained[0]
        r1, r2 = reads_for(seed)
        if len(r1) < 20:
            unexplained.pop(0)
            continue
        found = detect_from_read_lists(r1, r2)
        if not found:
            unexplained.pop(0)
            continue
        found["runs"] = [a for a in probe if explains(found, a)]
        if seed not in found["runs"]:
            found["runs"].append(seed)
        found["n_runs"] = len(found["runs"])
        sets.append(found)
        covered = set(found["runs"])
        unexplained = [a for a in unexplained if a not in covered]

    sets.sort(key=lambda s: -s.get("n_runs", 0))
    return sets
