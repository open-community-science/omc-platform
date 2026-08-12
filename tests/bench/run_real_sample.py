"""Run the claim-grounded autoresearch explorer on a REAL submission dir.

Points the explorer at an arbitrary amplicon submission's data/ dir, fetches
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

import fixtures  # noqa: E402
fixtures.SNAPSHOT = Path("/nonexistent")          # force fresh build from this dir
fixtures.STUDY_GROUNDED = study
import results_explorer as R  # noqa: E402 (builds DATASETS/COUNTS from OMC_BENCH_DATA)
R.STUDY_GROUNDED = study
R.OUT = HERE / "writings" / f"real_{slug}"
R.OUT.mkdir(parents=True, exist_ok=True)

# Offline reproduction of THIS submission's committed ledger (no fresh model run):
# `run_real_sample.py <dir> --reverify [--reconcile]` re-checks writings/real_<slug>.
if "--reverify" in flags:
    client = None
    if "--reconcile" in flags:
        R._ensure_model_loaded()
        client = R._client()
    R.reverify_saved(client)
else:
    sys.argv = [sys.argv[0]]  # fresh run: don't let R.main() see our flags
    R.main()
print(f"\noutputs in {R.OUT}")
