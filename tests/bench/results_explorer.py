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
import re
import sys
import time
import traceback
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


# ── Sandboxed analysis execution (prototype of a Marimo cell) ─────────────────
_SAFE_BUILTINS = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
                  for k in ("len range sum min max sorted list dict set tuple float int str bool "
                            "round abs enumerate zip map filter any all print isinstance").split()}


def _analysis_namespace():
    from scipy.spatial.distance import pdist, squareform, braycurtis
    from scipy.stats import entropy
    from sklearn.decomposition import PCA
    return {"__builtins__": _SAFE_BUILTINS, "np": np, "pd": pd, "counts": COUNTS.copy(),
            "pdist": pdist, "squareform": squareform, "braycurtis": braycurtis,
            "entropy": entropy, "PCA": PCA}


def _run_code(code: str):
    """Exec analysis code that must set `result`. Returns (ok, result_or_err)."""
    ns = _analysis_namespace()
    try:
        exec(code, ns)
        if "result" not in ns:
            return False, "code did not set a `result` variable"
        return True, _jsonify(ns["result"])
    except Exception:
        return False, traceback.format_exc().splitlines()[-1]


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


TOOLS = [
    {"type": "function", "function": {
        "name": "list_datasets",
        "description": "List available summary datasets you can inspect.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_dataset",
        "description": "Load a named summary dataset's contents.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "run_analysis",
        "description": ("Run Python to compute a statistic the summaries don't provide (diversity, "
                        "richness, Bray-Curtis, ordination). Available in scope: `counts` (pandas "
                        "DataFrame, 11 samples x 162 ASVs of read counts), np, pd, pdist, squareform, "
                        "braycurtis, entropy, PCA. Your code MUST assign the answer to `result`. "
                        "Returns result. Use this before claiming any computed number."),
        "parameters": {"type": "object", "properties": {
            "label": {"type": "string", "description": "short name, e.g. 'shannon_diversity'"},
            "code": {"type": "string", "description": "Python that sets `result`"}},
            "required": ["label", "code"]}}},
    {"type": "function", "function": {
        "name": "record_claim",
        "description": ("Record ONE verifiable claim. Cite antecedents so it can be re-checked: "
                        "data paths (e.g. 'renorm_stats.prokaryote.n_asvs') and/or computation ids "
                        "returned by run_analysis (e.g. 'c1')."),
        "parameters": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "value": {"type": "string", "description": "the exact supporting value"},
            "antecedents": {"type": "array", "items": {"type": "string"},
                            "description": "data paths and/or computation ids this claim depends on"},
            "kind": {"type": "string", "enum": ["observation", "quality_caveat"]}},
            "required": ["statement", "value", "antecedents"]}}},
]


def _exec_tool(name, args):
    if name == "list_datasets":
        return {"datasets": list(DATASETS), "note": "use run_analysis + `counts` for diversity/ordination"}
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
                 "kind": args.get("kind", "observation")}
        LEDGER.append(claim)
        return {"recorded": True, "claim_id": claim["id"], "n_claims": len(LEDGER)}
    return {"error": f"unknown tool {name}"}


EXPLORE_SYSTEM = """You are a microbial-ecology data analyst establishing the factual basis
for a Results section from a 16S amplicon pipeline. Work by EXPLORATION:

1. list_datasets / get_dataset to read reported numbers.
2. For statistics the summaries DON'T contain (alpha diversity, richness per sample,
   Bray-Curtis dissimilarity, ordination), WRITE AND RUN code with run_analysis over the
   `counts` matrix. Never claim a computed number you did not actually compute.
3. record_claim for every finding, citing antecedents (data paths and/or computation ids)
   so another analyst can re-verify it. Be honest about data quality (record quality_caveat
   for lost samples, thin coverage, etc.). Do NOT invent statistics.

Cover: retention & how many samples survived, ASV counts, taxonomic composition, AND at
least one computed diversity/ordination result. Aim for ~8-12 sourced claims. Reply DONE
when finished."""


def explore(client, max_steps=20):
    messages = [{"role": "system", "content": EXPLORE_SYSTEM},
                {"role": "user", "content": "Explore, compute, and record verifiable claims for the Results section."}]
    for step in range(max_steps):
        r = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS,
                                           tool_choice="auto", temperature=0.2, max_tokens=2500)
        msg = r.choices[0].message
        if not msg.tool_calls:
            content, _ = _visible_and_finish(r)
            if "DONE" in (content or "").upper() or len(LEDGER) >= 8:
                break
            messages.append({"role": "user", "content": "Continue: run_analysis / record_claim, or DONE."})
            continue
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _exec_tool(tc.function.name, args)
            label = args.get("label") or args.get("name") or (args.get("statement") or "")[:40]
            print(f"  step {step}: {tc.function.name}({label}) -> {str(result)[:80]}")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)[:3500]})


# ── Verification: re-derive every claim ───────────────────────────────────────
def _nums(s):
    """Every number in a string — handles ranges ('19-98', '2.01–4.71') and commas."""
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", str(s))]


def verify():
    """Re-derive each claim: numbers must appear among its antecedents' values
    (data re-read; computations re-EXECUTED). A range claim requires BOTH ends."""
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

        def _match(x):
            return any(abs(x - n) < 0.05 * max(abs(n), 1) for n in candidates)

        if claim_nums:
            ok = all(_match(x) for x in claim_nums) if candidates else None
        else:  # non-numeric claim (e.g. a database name)
            ok = any(c["value"].strip().lower() in s.lower() for s in strvals) if strvals else None
        c["verdict"] = "verified" if ok else ("unverifiable" if ok is None else "refuted")
        c["checked"] = checked


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


def main():
    _unload_all()
    if not _lms_load(MODEL, 65536, 300)[1]:
        print("load failed"); return
    client = OpenAI(base_url=BASE_URL, api_key="lm-studio")

    print("=== EXPLORE (read + compute) ===")
    t0 = time.time()
    explore(client)
    verify()
    print(f"  {len(LEDGER)} claims, {len(COMPUTATIONS)} computations in {time.time()-t0:.0f}s")
    for c in LEDGER:
        print(f"    [{c.get('verdict'):12}] {c['statement'][:64]}  (={c['value']}; {','.join(c['checked'])})")

    verified = [c for c in LEDGER if c["verdict"] == "verified"]
    (OUT / "claims_ledger.json").write_text(json.dumps(
        {"claims": LEDGER, "computations": COMPUTATIONS}, indent=2, default=str) + "\n")
    dag = build_dag()
    (OUT / "claims_dag.json").write_text(json.dumps(dag, indent=2, default=str) + "\n")
    (OUT / "claims_dag.md").write_text(
        f"# Claim provenance DAG ({MODEL})\n\n"
        f"{len(verified)}/{len(LEDGER)} claims verified · {len(COMPUTATIONS)} computations\n\n"
        f"Legend: 🟩 verified · 🟥 refuted · 🟨 unverifiable · 🟦 computation · ⬛ data\n\n"
        f"{dag_mermaid(dag)}\n")

    print("\n=== WRITE (verified claims only) ===")
    text = write_results(client, verified)
    unsupported = check_numbers_supported({"results": text}, results_data=_supported_results_data())
    n_unsupported = sum(len(i["detail"].split("may be unsupported:")[1].split(","))
                        for i in unsupported) if unsupported else 0
    (OUT / "results_from_claims.md").write_text(
        f"# Results from claims ({MODEL})\n\n_{len(verified)}/{len(LEDGER)} verified · "
        f"{len(COMPUTATIONS)} computations · {n_unsupported} unsupported numbers_\n\n{text}\n")
    print(f"  verified {len(verified)}/{len(LEDGER)}; {n_unsupported} unsupported numbers")
    print(f"  -> claims_ledger.json, claims_dag.json, claims_dag.md, results_from_claims.md")


if __name__ == "__main__":
    main()
