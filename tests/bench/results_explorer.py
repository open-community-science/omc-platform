"""Claim-centric autoresearch for the Results section (issue #29).

An agent EXPLORES the pipeline data — reading summaries AND running its own
analysis code (Marimo-style) — and records a ledger of VERIFIABLE claims. Claims
are the currency: each links to its ANTECEDENTS (the data paths and/or the
computations it depends on), forming a provenance **DAG**. Every claim is
re-derivable — a data claim by re-reading its path, a computed claim by
re-executing its stored code — so any other model or human can verify it. Results
prose is written from verified claims only.

Phase 2: adds `run_analysis` (code execution over the real ASV count matrix) and
a growing claim→antecedent DAG exported as JSON + a Mermaid diagram.

Explorer model: qwen3.6-35b-a3b.

Run:  python tests/bench/results_explorer.py
Out:  writings/{claims_ledger.json, claims_dag.json, claims_dag.md, results_from_claims.md}
"""
import gzip
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

import numpy as np
import pandas as pd
from openai import OpenAI
from ai.llm_client import _visible_and_finish
from ai.manuscript_checks import check_numbers_supported
from fixtures import load_fixture, STUDY_GROUNDED, DATA_DIR
from run_bench import _unload_all, _lms_load

BASE_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"
OUT = HERE / "writings"
OUT.mkdir(exist_ok=True)


# ── Datasets (source of truth for reads + verification) ───────────────────────
def _rj(name):
    for p in (DATA_DIR / f"{name}.json", DATA_DIR / f"{name}.json.gz"):
        if p.exists():
            op = gzip.open if p.suffix == ".gz" else open
            with op(p, "rt") as f:
                return json.load(f)
    return None


def _build_datasets():
    fx = load_fixture()
    prov = _rj("provenance") or {}
    samples = _rj("samples") or []
    return {
        "overview": fx,
        "renorm_stats": _rj("renorm_stats") or fx.get("renorm", {}),
        "provenance": {"total": prov.get("total", {}),
                       "stages": [s.get("id") for s in prov.get("stages", [])],
                       "n_samples": len(prov.get("samples", {}))},
        "samples": {"n": len(samples),
                    "total_reads": sum(s.get("total_reads", 0) for s in samples if isinstance(s, dict))},
        "taxonomy_summary": fx.get("taxonomy_summary", {}),
    }


DATASETS = _build_datasets()


def _count_matrix() -> pd.DataFrame:
    """Dense samples × ASV read-count matrix from counts.json.gz."""
    c = _rj("counts")
    samples, asvs = c["samples"], c["asvs"]
    m = np.zeros((len(samples), len(asvs)), dtype=float)
    for s, a, cnt, _rel in c["data"]:
        m[s, a] = cnt
    return pd.DataFrame(m, index=samples, columns=asvs)


COUNTS = _count_matrix()


def _tax_df() -> pd.DataFrame:
    """ASV → taxonomic rank table (Domain..Genus), aligned to COUNTS columns.
    Lets the agent group abundances by taxon, split prokaryote vs eukaryote, and
    screen for named contaminants."""
    tax = _rj("taxonomy") or {}
    _db, body = next(iter(tax.items()), ("none", {}))
    levels, assign = body.get("levels", []), body.get("assignments", {})
    df = pd.DataFrame.from_dict({a: dict(zip(levels, lin)) for a, lin in assign.items()}, orient="index")
    return df.reindex(columns=levels)


def _samples_df() -> pd.DataFrame:
    """Per-sample metadata (library_name, collection_date, precomputed x/y, etc.)."""
    sm = _rj("samples") or []
    df = pd.DataFrame(sm)
    return df.set_index("id") if "id" in df.columns else df


TAX_DF = _tax_df()
SAMPLES_META = _samples_df()


def _navigate(path):
    cur = DATASETS
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


# ── Analysis execution in a resource-limited subprocess ───────────────────────
# Model-written code runs in a SEPARATE python process with CPU + memory rlimits,
# a wall-clock timeout, and network disabled — so a bad or hostile cell can't hang
# the run, exhaust memory, or reach the network/model. It is NOT a full container:
# the filesystem is not confined, so this is for TRUSTED local benchmarking over
# vetted pipeline outputs, not untrusted input. (A container/seccomp jail would be
# the next step to run this on arbitrary third-party code.)
_CHILD_RUNNER = r'''
import os, sys, json, gzip, resource
resource.setrlimit(resource.RLIMIT_CPU, (25, 25))          # ~25s CPU
try:  # generous virtual-address cap — numpy/BLAS reserve a lot even when RSS is small
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
except (ValueError, OSError):
    pass
DATA = os.environ["EXPLORER_DATA_DIR"]
def _rj(name):
    for p in (os.path.join(DATA, name + ".json"), os.path.join(DATA, name + ".json.gz")):
        if os.path.exists(p):
            op = gzip.open if p.endswith(".gz") else open
            with op(p, "rt") as f:
                return json.load(f)
    return None
# Trusted imports FIRST (ssl, loaded transitively, subclasses socket.socket) —
# only after they're loaded do we disable the network for the model's code.
import numpy as np, pandas as pd
from scipy.spatial.distance import pdist, squareform, braycurtis
from scipy.stats import entropy, pearsonr, spearmanr, kruskal, mannwhitneyu
from sklearn.decomposition import PCA
_c = _rj("counts"); counts = None
if _c:
    _m = np.zeros((len(_c["samples"]), len(_c["asvs"])))
    for _s, _a, _n, _r in _c["data"]:
        _m[_s, _a] = _n
    counts = pd.DataFrame(_m, index=_c["samples"], columns=_c["asvs"])
_t = _rj("taxonomy") or {}
_db, _body = next(iter(_t.items()), ("none", {}))
_lv = _body.get("levels", [])
tax = pd.DataFrame.from_dict({a: dict(zip(_lv, l)) for a, l in _body.get("assignments", {}).items()},
                            orient="index").reindex(columns=_lv)
meta = pd.DataFrame(_rj("samples") or [])
meta = meta.set_index("id") if "id" in meta.columns else meta
import socket
def _no_net(*a, **k):
    raise OSError("network disabled in analysis sandbox")
socket.socket = _no_net; socket.create_connection = _no_net; socket.getaddrinfo = _no_net
def _j(v, d=0):
    if d > 4: return str(v)
    if isinstance(v, (np.floating, np.integer)): return round(float(v), 4)
    if isinstance(v, float): return round(v, 4)
    if isinstance(v, np.ndarray): return _j(v.tolist(), d + 1)
    if isinstance(v, dict): return {str(k): _j(x, d + 1) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)): return [_j(x, d + 1) for x in list(v)[:50]]
    if isinstance(v, (pd.Series, pd.DataFrame)): return _j(v.to_dict(), d + 1)
    return v
_ns = dict(np=np, pd=pd, counts=counts, tax=tax, meta=meta, pdist=pdist, squareform=squareform,
           braycurtis=braycurtis, entropy=entropy, pearsonr=pearsonr, spearmanr=spearmanr,
           kruskal=kruskal, mannwhitneyu=mannwhitneyu, PCA=PCA)
try:
    exec(sys.stdin.read(), _ns)
    print(json.dumps({"__ok__": _j(_ns["result"])} if "result" in _ns
                     else {"__err__": "code did not set a `result` variable"}))
except Exception as e:
    print(json.dumps({"__err__": f"{type(e).__name__}: {e}"}))
'''


def _run_code(code: str, timeout: int = 30):
    """Run model-written analysis code in a resource-limited subprocess. Returns
    (ok, result_or_error). Data is loaded from OMC_BENCH_DATA inside the child, so
    the same code re-runs deterministically at verification time."""
    try:
        p = subprocess.run(
            [sys.executable, "-c", _CHILD_RUNNER], input=code, text=True,
            capture_output=True, timeout=timeout,
            env={**os.environ, "EXPLORER_DATA_DIR": str(DATA_DIR),
                 # single-threaded BLAS: deterministic + far smaller virtual footprint
                 "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout >{timeout}s"
    lines = (p.stdout or "").strip().splitlines()
    try:
        r = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return False, ((p.stderr or p.stdout or "no output").strip().splitlines() or ["error"])[-1][:200]
    return (True, r["__ok__"]) if "__ok__" in r else (False, r.get("__err__", "error"))


def _jsonify(v, depth=0):
    if depth > 4:
        return str(v)
    if isinstance(v, (np.floating, np.integer)):
        return round(float(v), 4)
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (np.ndarray,)):
        return _jsonify(v.tolist(), depth + 1)
    if isinstance(v, dict):
        return {str(k): _jsonify(x, depth + 1) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, (pd.Series, pd.DataFrame)):
        return _jsonify(v.to_dict(), depth + 1)
    return v


def _flatten_numbers(v, acc):
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        acc.append(float(v))
    elif isinstance(v, dict):
        for x in v.values():
            _flatten_numbers(x, acc)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _flatten_numbers(x, acc)


# ── DAG state ─────────────────────────────────────────────────────────────────
COMPUTATIONS = {}  # id -> {label, code, result}
LEDGER = []        # claim dicts
AGENDA = []        # investigation worklist: {id, question, rationale, status, parent}


def _current_investigation():
    """The investigation being worked (first in-progress, else first pending)."""
    for st in ("in_progress", "pending"):
        for a in AGENDA:
            if a["status"] == st:
                return a["id"]
    return None


TOOLS = [
    {"type": "function", "function": {
        "name": "propose_agenda",
        "description": ("FIRST STEP. Propose the analyses / hypothesis tests worth running on this "
                        "amplicon dataset — the things a curious microbial ecologist would actually "
                        "test. Be comprehensive and go beyond the obvious summary stats."),
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string", "description": "the analysis/hypothesis, e.g. 'Do samples cluster in ordination, and which taxa drive the separation?'"},
                "rationale": {"type": "string", "description": "why it matters ecologically"}},
                "required": ["question"]}}},
            "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "get_agenda",
        "description": "See the current agenda and each item's status (pending/in_progress/done).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "add_followup",
        "description": ("Recurse: when a result is surprising or opens a deeper question, add a "
                        "follow-up investigation (e.g. ordination shows structure → which taxa drive "
                        "it → are they contaminants or real signal?). This is how you go deeper."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "mark_done",
        "description": "Mark the current investigation finished; move to the next agenda item.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_dataset",
        "description": "Load a named summary dataset's contents (list_datasets to see names).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "list_datasets",
        "description": "List available summary datasets.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_analysis",
        "description": ("Run Python to compute anything the summaries lack. In scope: `counts` "
                        "(samples×ASV read-count DataFrame), `tax` (ASV×rank Domain..Genus — join to "
                        "counts to work at taxon level or split Bacteria/Archaea vs Eukaryota), `meta` "
                        "(per-sample metadata incl. library_name, collection_date, x/y). Helpers: np, "
                        "pd, pdist, squareform, braycurtis, entropy, pearsonr, spearmanr, kruskal, "
                        "mannwhitneyu, PCA. Code MUST assign to `result`. Compute before you claim."),
        "parameters": {"type": "object", "properties": {
            "label": {"type": "string"}, "code": {"type": "string", "description": "Python that sets `result`"}},
            "required": ["label", "code"]}}},
    {"type": "function", "function": {
        "name": "record_claim",
        "description": ("Record ONE verifiable claim from the current investigation. Cite antecedents "
                        "(data paths and/or computation ids) so it can be re-checked. Prefer insight "
                        "(patterns, relationships, anomalies) over restating summary numbers."),
        "parameters": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "value": {"type": "string", "description": "the exact supporting value"},
            "antecedents": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["observation", "pattern", "anomaly", "quality_caveat"]}},
            "required": ["statement", "value", "antecedents"]}}},
]


def _exec_tool(name, args):
    if name == "propose_agenda":
        for it in args.get("items", [])[:20]:
            AGENDA.append({"id": f"a{len(AGENDA) + 1}", "question": it.get("question", ""),
                           "rationale": it.get("rationale", ""), "status": "pending", "parent": None})
        if AGENDA:
            AGENDA[0]["status"] = "in_progress"
        return {"agenda": [{"id": a["id"], "question": a["question"], "status": a["status"]} for a in AGENDA]}
    if name == "get_agenda":
        return {"agenda": [{"id": a["id"], "question": a["question"], "status": a["status"],
                            "parent": a["parent"]} for a in AGENDA]}
    if name == "add_followup":
        parent = _current_investigation()
        AGENDA.append({"id": f"a{len(AGENDA) + 1}", "question": args.get("question", ""),
                       "rationale": args.get("rationale", ""), "status": "pending", "parent": parent})
        return {"added": AGENDA[-1]["id"], "parent": parent, "pending": sum(a["status"] == "pending" for a in AGENDA)}
    if name == "mark_done":
        cur = _current_investigation()
        for a in AGENDA:
            if a["id"] == cur:
                a["status"] = "done"
        nxt = _current_investigation()
        for a in AGENDA:
            if a["id"] == nxt and a["status"] == "pending":
                a["status"] = "in_progress"
        pend = sum(a["status"] in ("pending", "in_progress") for a in AGENDA)
        return {"done": cur, "now": nxt, "remaining": pend}
    if name == "list_datasets":
        return {"datasets": list(DATASETS), "note": "use run_analysis with `counts`/`tax`/`meta` for real tests"}
    if name == "get_dataset":
        d = DATASETS.get(args.get("name"))
        return d if d is not None else {"error": "unknown", "available": list(DATASETS)}
    if name == "run_analysis":
        ok, res = _run_code(args.get("code", ""))
        if not ok:
            return {"ok": False, "error": res}
        cid = f"c{len(COMPUTATIONS) + 1}"
        COMPUTATIONS[cid] = {"label": args.get("label", cid), "code": args.get("code", ""), "result": res}
        return {"ok": True, "computation_id": cid, "result": res}
    if name == "record_claim":
        claim = {"id": f"k{len(LEDGER) + 1}", "statement": args.get("statement", ""),
                 "value": str(args.get("value", "")), "antecedents": args.get("antecedents", []),
                 "kind": args.get("kind", "observation"), "investigation": _current_investigation()}
        LEDGER.append(claim)
        return {"recorded": True, "claim_id": claim["id"], "n_claims": len(LEDGER)}
    return {"error": f"unknown tool {name}"}


EXPLORE_SYSTEM = """You are a curious microbial ecologist investigating a 16S/18S amplicon
dataset. Your job is not to restate summary statistics — it is to TEST HYPOTHESES and find
PATTERNS, RELATIONSHIPS, and ANOMALIES a scientist would care about, then ground each in a
re-runnable computation.

Work systematically and recursively:
1. FIRST call propose_agenda with the analyses/hypothesis tests worth running here — the
   standard microbial-ecology toolkit AND less obvious ideas. Think across: alpha diversity
   (richness, Shannon, Simpson, Pielou evenness) and its dependence on sequencing depth;
   dominance and the rare biosphere; beta-diversity structure (Bray-Curtis/Jaccard,
   ordination) and WHICH taxa drive it; differential abundance / indicator taxa between
   sample groups; co-occurrence; the prokaryote-vs-eukaryote split (use `tax` Domain);
   core vs transient taxa (prevalence); and a contamination screen for known kit/reagent
   genera (e.g. Ralstonia, Bradyrhizobium, Cutibacterium, Pelomonas, Delftia).
2. Work the agenda item by item. For each: run_analysis over `counts`/`tax`/`meta` to test
   it, then record_claim(s) with the exact value(s) and antecedents. mark_done and move on.
3. RECURSE: whenever a result is surprising or opens a question, add_followup — that is how
   you go deeper (a cluster in ordination → its driver taxa → are they contamination?).
4. Prefer claims of kind "pattern" or "anomaly" (an insight) over "observation" (a restated
   number). Be honest: record quality_caveat for depth bias, low evenness, contamination,
   or anything that undermines a result. Never claim a number you did not compute.

KNOW WHAT THE FIELDS MEAN — do not over-interpret metadata:
- `meta['x']`/`meta['y']` are PRECOMPUTED ORDINATION coordinates, NOT geographic latitude/
  longitude. Never treat them as spatial coordinates or run a "geographic"/Mantel test on
  them, and never claim a geographic/distance effect from them.
- `meta['collection_date']` is often the database RECORD-CREATION date, not a verified
  biological sampling date. Do NOT claim a temporal/seasonal/sampling-date effect from it.
  If groups separate by this date, describe it as a processing/submission BATCH, and prefer
  grouping by a field whose meaning is certain (domain, library_strategy, library_name).
- A named test (Mantel, PERMANOVA, ...) must be the test you actually ran; if you computed a
  plain correlation on distances, call it that — do not upgrade the label.

Keep going until the agenda (including follow-ups) is worked through, then reply DONE."""


def explore(client, max_steps=48):
    messages = [{"role": "system", "content": EXPLORE_SYSTEM},
                {"role": "user", "content": "Propose your agenda of microbial-ecology tests, then work through it, recursing where it gets interesting."}]
    for step in range(max_steps):
        r = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS,
                                           tool_choice="auto", temperature=0.25, max_tokens=2500)
        msg = r.choices[0].message
        if not msg.tool_calls:
            content, _ = _visible_and_finish(r)
            active = [a for a in AGENDA if a["status"] in ("pending", "in_progress")]
            if "DONE" in (content or "").upper() and not active:
                break
            if "DONE" in (content or "").upper() and active:
                messages.append({"role": "user", "content": f"{len(active)} agenda items remain — work the next one (get_agenda)."})
                continue
            messages.append({"role": "user", "content": "Continue with the current investigation, or mark_done and take the next."})
            continue
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _exec_tool(tc.function.name, args)
            label = args.get("label") or args.get("name") or args.get("question") or (args.get("statement") or "")[:40]
            print(f"  [{step}] {tc.function.name}({str(label)[:46]}) -> {str(result)[:70]}", flush=True)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)[:3500]})
    # If we hit the step cap with work outstanding, DO NOT pretend it finished:
    # the current item was interrupted (not done), and pending items stay pending.
    # Returns True only when the agenda was actually worked through.
    for a in AGENDA:
        if a["status"] == "in_progress":
            a["status"] = "interrupted"
    return not any(a["status"] in ("pending", "in_progress", "interrupted") for a in AGENDA)


# ── Verification: re-derive every claim ───────────────────────────────────────
def _nums(s):
    """Every genuine quantity in a string. The lookbehind blocks numbers embedded in
    an alphanumeric token — not just glued to a letter ('PC1') but mid-token digit
    runs like 'SRR38966955' (block preceded-by letter/digit/dot) — so accession IDs
    aren't mistaken for claimed values. Handles ranges ('19-98') and thousands commas."""
    return [float(x.replace(",", ""))
            for x in re.findall(r"(?<![A-Za-z0-9.])\d[\d,]*(?:\.\d+)?", str(s))]


def _close(x, n):
    return abs(x - n) < 0.05 * max(abs(n), 1)


def _match_num(x, cands):
    """How (if at all) claim number x is backed by the antecedent numbers `cands`.

    Verification must re-derive x in its STATED representation, not demand a literal
    match: a value can be the same truth in different units (fraction<->percent) or
    a simple derivation of the inputs (difference, ratio). Pairwise derivation is
    limited to small summary sources so it can't spuriously fire on big result sets.
    """
    for n in cands:
        if _close(x, n):
            return "direct"
        if _close(x, n * 100):
            return "x100"          # claim in %, source a fraction
        if n and _close(x, n / 100):
            return "/100"          # claim a fraction, source in %
    if len(cands) <= 12:            # derived from a summary (e.g. 73 = 84 - 11)
        for a in cands:
            for b in cands:
                if a is b:
                    continue
                for val, tag in ((a - b, "diff"), (a + b, "sum"),
                                 (100 * a / b if b else None, "pct"),
                                 (100 * (b - a) / b if b else None, "pct")):
                    if val is not None and _close(x, val):
                        return "derived:" + tag
    return None


RECONCILE_SYSTEM = """You are a skeptical verification auditor. You are given a CLAIM and the
INDEPENDENT EVIDENCE that was re-executed from the raw data (computation results and data
values). Decide whether the evidence genuinely supports the claim's quantitative content.

- Judge only against the evidence shown — never from prior knowledge.
- Allow equivalent representations: unit conversions (a fraction vs a percent), values that
  are a simple derivation of the evidence (e.g. a difference or ratio), and ranges.
- IGNORE tokens that are identifiers, not quantities (sample accessions like SRR38966955).
- If the claim packs several numbers, it is SUPPORTED only if every quantitative number is
  backed; if some are and some aren't, say PARTIAL and list which fail.
- Default to UNSUPPORTED when the evidence does not clearly back a number. Be strict.

Reply with exactly one line 'VERDICT: SUPPORTED|PARTIAL|UNSUPPORTED' then one sentence why."""


def _evidence_for(claim) -> str:
    """The deterministic evidence block for a claim: re-executed computation results
    and re-read data values for each antecedent. This is what the reconciler judges
    against — not memory."""
    parts = []
    for ant in claim["antecedents"]:
        if ant in COMPUTATIONS:
            good, res = _run_code(COMPUTATIONS[ant]["code"])
            parts.append(f"[{ant}] {COMPUTATIONS[ant]['label']} (re-executed) = "
                         + (json.dumps(res)[:600] if good else "ERROR"))
        else:
            found, val = _navigate(ant)
            parts.append(f"[{ant}] = {json.dumps(val)[:300] if found else 'NOT FOUND'}")
    return "\n".join(parts)


def reconcile_claim(client, claim) -> dict:
    """Model adjudication of a claim against its re-executed evidence (issue #29).
    Only invoked on a deterministic miss. Returns {verdict, reasoning}."""
    user = (f"CLAIM: {claim['statement']}\nCLAIMED VALUE: {claim['value']}\n\n"
            f"INDEPENDENT EVIDENCE (re-executed from raw data):\n{_evidence_for(claim)}")
    from ai.llm_client import chat
    resp = chat(client, RECONCILE_SYSTEM, user, model=MODEL, max_tokens=4000, temperature=0.0)
    m = re.search(r"VERDICT:\s*(SUPPORTED|PARTIAL|UNSUPPORTED)", resp.upper())
    return {"verdict": m.group(1).lower() if m else "unsupported", "reasoning": resp.strip()[:400]}


def verify(client=None):
    """Re-derive each claim from its antecedents (data re-read; computations
    re-EXECUTED). Deterministic first; a true claim is never marked refuted for a
    representation mismatch. If `client` is given, a deterministic MISS escalates to
    a skeptical model reconciliation against the same re-executed evidence — robust
    on the hard cases, and labeled so it's clear which claims leaned on judgment."""
    for c in LEDGER:
        claim_nums = _nums(c["value"])
        candidates, strvals, checked = [], [], []
        for ant in c["antecedents"]:
            if ant in COMPUTATIONS:
                good, res = _run_code(COMPUTATIONS[ant]["code"])  # re-execute stored code
                if good:
                    _flatten_numbers(res, candidates)
                checked.append(f"{ant}:{'run' if good else 'err'}")
            else:  # data path
                found, actual = _navigate(ant)
                if found:
                    _flatten_numbers(actual, candidates)
                    strvals.append(str(actual))
                checked.append(f"{ant}:{'ok' if found else 'nopath'}")

        have_evidence = bool(candidates or strvals)
        if claim_nums:
            methods = [_match_num(x, candidates) for x in claim_nums]
            ok = all(methods) if candidates else None
            c["method"] = ",".join(m for m in methods if m) or None
        else:  # non-numeric claim (e.g. a database name)
            ok = any(c["value"].strip().lower() in s.lower() for s in strvals) if strvals else None
        c["verdict"] = "verified" if ok else ("unverifiable" if (ok is None or not have_evidence) else "refuted")
        c["checked"] = checked
        # Escalate deterministic misses to a skeptical model reconciliation against
        # the SAME re-executed evidence (labeled, so judgment-backed claims are visible).
        if c["verdict"] != "verified" and client is not None and have_evidence:
            rec = reconcile_claim(client, c)
            c["reconcile"] = rec
            if rec["verdict"] == "supported":
                c["verdict"], c["method"] = "verified", "reconciled"


def _supported_results_data():
    """Ground-truth for check_numbers_supported = raw pipeline data PLUS verified
    computed values (a verified computation IS support). Includes fraction→percent
    forms so '0.4311' also backs a '43.1%' claim."""
    fx = load_fixture()
    forms = []
    for comp in COMPUTATIONS.values():
        nums = []
        _flatten_numbers(comp["result"], nums)
        for n in nums:
            for v in (n, n * 100):
                forms += [round(v, 4), round(v, 2), round(v, 1)]
    return {"pipeline": fx, "verified_claims": [c["value"] for c in LEDGER if c.get("verdict") == "verified"],
            "computed_support": sorted(set(forms))}


# ── DAG export ────────────────────────────────────────────────────────────────
def build_dag():
    nodes, edges, seen = [], [], set()

    def add(nid, **kw):
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, **kw})

    for cid, comp in COMPUTATIONS.items():
        add(cid, type="computation", label=comp["label"])
    for c in LEDGER:
        add(c["id"], type="claim", label=c["statement"][:60], verdict=c.get("verdict"), kind=c["kind"])
        for ant in c["antecedents"]:
            if ant in COMPUTATIONS:
                edges.append({"from": ant, "to": c["id"]})
            else:
                add(ant, type="data", label=ant)
                edges.append({"from": ant, "to": c["id"]})
    return {"nodes": nodes, "edges": edges}


def dag_mermaid(dag):
    sty = {"verified": "fill:#2e7d32,color:#fff", "refuted": "fill:#c62828,color:#fff",
           "unverifiable": "fill:#f9a825,color:#000", "computation": "fill:#1565c0,color:#fff",
           "data": "fill:#455a64,color:#fff"}
    lines = ["```mermaid", "graph LR"]
    for n in dag["nodes"]:
        lbl = n["label"].replace('"', "'")
        shape = f'[["{lbl}"]]' if n["type"] == "computation" else (
            f'("{lbl}")' if n["type"] == "data" else f'["{lbl}"]')
        lines.append(f'  {n["id"]}{shape}')
        cls = n.get("verdict") if n["type"] == "claim" else n["type"]
        if cls in sty:
            lines.append(f'  style {n["id"]} {sty[cls]}')
    for e in dag["edges"]:
        lines.append(f'  {e["from"]} --> {e["to"]}')
    lines.append("```")
    return "\n".join(lines)


WRITE_SYSTEM = """Scientific writing assistant for microbial ecology. Write a Results section
using ONLY the verified claims provided — every number must come from a claim. Do not add
findings not in the claims. Report quality caveats plainly and politely. Past tense,
objective, no interpretation. Synthesize into flowing prose (not a bullet list). Reference
Figure/Table where natural."""


def write_results(client, verified):
    claims_txt = "\n".join(f"- [{c['kind']}] {c['statement']} (value={c['value']})" for c in verified)
    study = (f"Study: {STUDY_GROUNDED['title']} — {STUDY_GROUNDED['study_name']} "
             f"({STUDY_GROUNDED['bioproject']}), {STUDY_GROUNDED['platform']}.")
    msgs = [{"role": "system", "content": WRITE_SYSTEM},
            {"role": "user", "content": f"{study}\n\nVERIFIED CLAIMS:\n{claims_txt}\n\nWrite the Results section."}]
    for mt in (6000, 12000):
        content, _ = _visible_and_finish(
            client.chat.completions.create(model=MODEL, messages=msgs, temperature=0.4, max_tokens=mt))
        if content.strip():
            return content
    return content


def reverify_saved(client=None):
    """Re-run verification (and rebuild the DAG) on the saved ledger. With no client
    it is purely deterministic (no model — fully reproducible); with a client, a
    deterministic miss escalates to skeptical model reconciliation."""
    saved = json.loads((OUT / "claims_ledger.json").read_text())
    LEDGER[:] = saved["claims"]
    COMPUTATIONS.clear(); COMPUTATIONS.update(saved["computations"])
    AGENDA[:] = saved.get("agenda", [])
    verify(client)
    (OUT / "claims_ledger.json").write_text(json.dumps(
        {"claims": LEDGER, "computations": COMPUTATIONS, "agenda": AGENDA}, indent=2, default=str) + "\n")
    dag = build_dag()
    (OUT / "claims_dag.json").write_text(json.dumps(dag, indent=2, default=str) + "\n")
    verified = sum(c["verdict"] == "verified" for c in LEDGER)
    print(f"re-verified {verified}/{len(LEDGER)} claims (offline)")
    for c in LEDGER:
        if c["verdict"] != "verified":
            print(f"    [{c['verdict']:12}] {c['id']} {c['statement'][:60]}")
        elif c.get("method") and c["method"] != "direct":
            print(f"    [verified:{c['method']:14}] {c['id']} {c['statement'][:52]}")


def main():
    if "--reverify" in sys.argv:
        client = None
        if "--reconcile" in sys.argv:   # escalate deterministic misses to the model
            _unload_all(); _lms_load(MODEL, 65536, 300)
            client = OpenAI(base_url=BASE_URL, api_key="lm-studio")
        reverify_saved(client); return
    _unload_all()
    if not _lms_load(MODEL, 65536, 300)[1]:
        print("load failed"); return
    client = OpenAI(base_url=BASE_URL, api_key="lm-studio")

    print("=== EXPLORE (agenda-driven, recursive) ===")
    t0 = time.time()
    completed = explore(client)
    verify(client)  # deterministic first; escalate misses to skeptical model reconciliation
    done = sum(a["status"] == "done" for a in AGENDA)
    outstanding = [a for a in AGENDA if a["status"] != "done"]
    print(f"\n  agenda ({done}/{len(AGENDA)} investigations done"
          f"{'' if completed else f' — INCOMPLETE, {len(outstanding)} outstanding'}):")
    for a in AGENDA:
        tag = "  └─" if a["parent"] else "•"
        print(f"    {tag} [{a['status']}] {a['id']}: {a['question'][:72]}")
    print(f"\n  {len(LEDGER)} claims, {len(COMPUTATIONS)} computations in {time.time()-t0:.0f}s")
    for c in LEDGER:
        print(f"    [{c.get('verdict'):12}|{c.get('kind','?')[:7]:7}] {c['statement'][:60]}  (={c['value']})")

    verified = [c for c in LEDGER if c["verdict"] == "verified"]
    status = "complete" if completed else f"INCOMPLETE ({done}/{len(AGENDA)} investigations)"
    banner = "" if completed else (
        f"> ⚠️ PRELIMINARY — exploration stopped with {len(outstanding)} of {len(AGENDA)} "
        f"investigations outstanding ({', '.join(a['id'] for a in outstanding)}); "
        f"these Results are partial.\n\n")
    # One atomic snapshot: ledger, DAG, and prose written from the SAME run state.
    (OUT / "claims_ledger.json").write_text(json.dumps(
        {"claims": LEDGER, "computations": COMPUTATIONS, "agenda": AGENDA,
         "run": {"completed": completed, "investigations_done": done,
                 "investigations_total": len(AGENDA)}}, indent=2, default=str) + "\n")
    dag = build_dag()
    (OUT / "claims_dag.json").write_text(json.dumps(dag, indent=2, default=str) + "\n")
    (OUT / "claims_dag.md").write_text(
        f"# Claim provenance DAG ({MODEL}) — {status}\n\n"
        f"{len(verified)}/{len(LEDGER)} claims verified · {len(COMPUTATIONS)} computations\n\n"
        f"Legend: 🟩 verified · 🟥 refuted · 🟨 unverifiable · 🟦 computation · ⬛ data\n\n"
        f"{dag_mermaid(dag)}\n")

    print("\n=== WRITE (verified claims only) ===")
    text = write_results(client, verified)
    unsupported = check_numbers_supported({"results": text}, results_data=_supported_results_data())
    n_unsupported = sum(len(i["detail"].split("may be unsupported:")[1].split(","))
                        for i in unsupported) if unsupported else 0
    (OUT / "results_from_claims.md").write_text(
        f"# Results from claims ({MODEL}) — {status}\n\n{banner}"
        f"_{len(verified)}/{len(LEDGER)} verified · {len(COMPUTATIONS)} computations · "
        f"{n_unsupported} unsupported numbers · {done}/{len(AGENDA)} investigations_\n\n{text}\n")
    print(f"  {status}; verified {len(verified)}/{len(LEDGER)}; {n_unsupported} unsupported numbers")
    print(f"  -> claims_ledger.json, claims_dag.json, claims_dag.md, results_from_claims.md")


if __name__ == "__main__":
    main()
