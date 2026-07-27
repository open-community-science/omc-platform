"""Run the claim-grounded autoresearch explorer on a REAL submission dir.

Points the explorer at an arbitrary microscape submission's data/ dir, fetches
its real study metadata from NCBI (first SRR), and writes per-submission outputs.

Usage: python tests/bench/run_real_sample.py /data/dev/testdata/<slug>
"""
import os
import re
import sys
import urllib.request
from pathlib import Path

_args = sys.argv[1:]
flags = {a for a in _args if a.startswith("--")}
positional = [a for a in _args if not a.startswith("--")]
data_dir = Path(positional[0]).resolve()
slug = data_dir.name

# Point the fixture/explorer at this submission BEFORE importing them.
os.environ["OMC_BENCH_DATA"] = str(data_dir)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent)); sys.path.insert(0, str(HERE))
import json  # noqa: E402


def fetch_study(srr):
    """Real study metadata from NCBI eutils (title/study/bioproject/platform)."""
    def _get(url):
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode()
    try:
        ids = json.loads(_get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=sra&term={srr}&retmode=json"))
        uid = ids["esearchresult"]["idlist"][0]
        res = json.loads(_get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=sra&id={uid}&retmode=json"))
        import html
        x = html.unescape(res["result"][res["result"]["uids"][0]].get("expxml", ""))
        g = lambda p: (re.search(p, x) or [None, None])[1]
        return {"title": g(r"<Title>([^<]+)"), "study_name": g(r'Study acc="[^"]*" name="([^"]+)'),
                "bioproject": g(r"Bioproject>([^<]+)"), "platform": g(r'instrument_model="([^"]+)') or "Illumina",
                "organism": g(r'ScientificName="([^"]+)'), "num_samples": len(samples)}
    except Exception as e:
        print(f"  metadata fetch failed ({e}); using minimal")
        return {"title": slug, "study_name": slug, "bioproject": "unknown",
                "platform": "Illumina", "num_samples": len(samples)}


samples = json.loads((data_dir / "samples.json").read_text())
srr = samples[0]["id"]
study = fetch_study(srr)
print(f"submission {slug}: {study.get('title')} / {study.get('study_name')} ({study.get('bioproject')}), n={len(samples)}")

# Per-sample BioSample attributes (#62). Fetched ONCE into the data dir, because the
# analysis sandbox has no network and the experimental design — what each sample
# actually was — lives nowhere else. `--refresh-meta` re-pulls.
sys.path.insert(0, str(HERE.parent.parent))
from ai.biosample import design_columns, resolve_runs, write_attributes  # noqa: E402
_samples = json.loads((data_dir / "samples.json").read_text()) \
    if (data_dir / "samples.json").exists() else []
# EVERY submitted run, not just the ones that produced reads. samples.json lists the
# survivors, so the dropped runs had no route to their own metadata and "is the
# attrition biased by treatment?" could not be answered from inside the sandbox.
_prov = json.loads((data_dir / "provenance.json").read_text()) \
    if (data_dir / "provenance.json").exists() else {}
_all_runs = sorted(set(_prov.get("samples") or {}) | {s.get("id") for s in _samples})
_runmap_path = data_dir / "sample_runs.json"
if _all_runs and (not _runmap_path.exists() or "--refresh-meta" in flags):
    try:
        _runmap_path.write_text(json.dumps(resolve_runs(_all_runs), indent=2,
                                           sort_keys=True) + "\n")
    except Exception as _e:
        print(f"run map unavailable ({_e}); metadata limited to samples.json")
_runmap = json.loads(_runmap_path.read_text()) if _runmap_path.exists() else {}
_accs = ([s.get("sample_accession") for s in _samples]
         + [v.get("biosample") for v in _runmap.values()])
_attrs = write_attributes(data_dir, _accs, refresh="--refresh-meta" in flags)
if _runmap:
    _dropped = len(_all_runs) - len(_samples)
    print(f"submitted runs: {len(_all_runs)} ({len(_samples)} in counts, "
          f"{_dropped} dropped but carried through with their metadata)")
if _attrs:
    print(f"sample attributes: {len(_attrs)} BioSamples, grouping columns: "
          f"{', '.join(design_columns(_attrs)[:6]) or '(none vary)'}")

import fixtures  # noqa: E402
fixtures.SNAPSHOT = Path("/nonexistent")          # force fresh build from this dir
fixtures.STUDY_GROUNDED = study
import results_explorer as R  # noqa: E402 (builds DATASETS/COUNTS from OMC_BENCH_DATA)
R.STUDY_GROUNDED = study
# Hyphal runs write beside the linear ones rather than over them — the two modes are
# only worth running if their ledgers can be put side by side afterwards (#58).
R.OUT = HERE / "writings" / (
    f"real_{slug}_epoch" if "--one-claim" in flags else
    f"real_{slug}_hyphal" if "--hyphal" in flags else f"real_{slug}")
R.OUT.mkdir(parents=True, exist_ok=True)

# Offline reproduction of THIS submission's committed ledger (no fresh model run):
# `run_real_sample.py <dir> --reverify [--reconcile]` re-checks writings/real_<slug>.
if "--reverify" in flags:
    client = None
    if "--reconcile" in flags:
        R._ensure_model_loaded()
        client = R._client_for("verify")
    R.reverify_saved(client)
else:
    # Fresh run: hide OUR flags from R.main(), but pass through the ones it owns
    # (--replicate). Blanket-stripping silently disabled replication here.
    sys.argv = [sys.argv[0]] + [f for f in _args
                                if f in ("--replicate", "--hyphal", "--one-claim")]
    R.main()
print(f"\noutputs in {R.OUT}")
