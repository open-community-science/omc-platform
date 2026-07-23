"""Claim-centric autoresearch prototype for the Results section (issue #29).

Instead of one-shot drafting from a fixed summary, an agent EXPLORES the pipeline
data with tools and emits a ledger of **verifiable claims**. Claims are the
currency: each carries the exact value and a machine-checkable source
(dataset + dotted path), so an independent verifier — another model or a
skeptical human — can re-derive it from the data. Results prose is then written
from *verified claims only*.

Phase 1 (this file): read-only exploration over the c5af6277 datasets, no code
execution yet. Explorer model: qwen3.6-35b-a3b (clean tool use).

Run:  python tests/bench/results_explorer.py
Out:  writings/claims_ledger.json, writings/results_from_claims.md
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

from openai import OpenAI
from ai.llm_client import _visible_and_finish
from ai.manuscript_checks import check_numbers_supported
from fixtures import load_fixture, STUDY_GROUNDED, DATA_DIR
from run_bench import _unload_all, _lms_load

BASE_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"
OUT = HERE / "writings"
OUT.mkdir(exist_ok=True)


# ── Datasets: the single source of truth for BOTH exploration and verification ─
# A claim's `source` is a dotted path into this dict, so get_dataset (what the
# agent sees) and verify_claim (independent re-derivation) can never diverge.
def _build_datasets() -> dict:
    def _rj(name):
        import gzip
        for p in (DATA_DIR / f"{name}.json", DATA_DIR / f"{name}.json.gz"):
            if p.exists():
                op = gzip.open if p.suffix == ".gz" else open
                with op(p, "rt") as f:
                    return json.load(f)
        return None

    fx = load_fixture()
    renorm = _rj("renorm_stats") or fx.get("renorm", {})
    prov = _rj("provenance") or {}
    samples = _rj("samples") or []
    return {
        "overview": fx,  # parse_microscape summary (asv_summary/taxonomy_summary/filtering/renorm)
        "renorm_stats": renorm,
        "provenance": {
            "total": prov.get("total", {}),
            "stages": [s.get("id") for s in prov.get("stages", [])],
            "n_samples": len(prov.get("samples", {})),
        },
        "samples": {
            "n": len(samples),
            "total_reads": sum(s.get("total_reads", 0) for s in samples if isinstance(s, dict)),
            "example": samples[:3],
        },
        "taxonomy_summary": fx.get("taxonomy_summary", {}),
    }


DATASETS = _build_datasets()


def _navigate(path: str):
    """Resolve a dotted path like 'renorm_stats.prokaryote.n_asvs' into DATASETS.
    Returns (found, value)."""
    cur = DATASETS
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


# ── Tools ─────────────────────────────────────────────────────────────────────
TOOLS = [
    {"type": "function", "function": {
        "name": "list_datasets",
        "description": "List the available pipeline datasets you can inspect.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_dataset",
        "description": "Load a named dataset's contents to inspect the numbers.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "one of the names from list_datasets"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "record_claim",
        "description": ("Record ONE verifiable claim for the Results section. Every claim must "
                        "cite the exact value and its source path so another model can re-check it."),
        "parameters": {"type": "object", "properties": {
            "statement": {"type": "string", "description": "the scientific claim, e.g. 'Only 11 of 84 filtered samples yielded prokaryotic ASVs'"},
            "value": {"type": "string", "description": "the exact supporting value, e.g. '11' or '37.2'"},
            "source": {"type": "string", "description": "dotted path into a dataset, e.g. 'overview.filtering.retention_pct' or 'renorm_stats.prokaryote.n_asvs'"},
            "kind": {"type": "string", "enum": ["observation", "quality_caveat"], "description": "quality_caveat = a data-limitation the author should know"},
        }, "required": ["statement", "value", "source"]},
    }},
]


def _exec_tool(name, args, ledger):
    if name == "list_datasets":
        return {"datasets": list(DATASETS)}
    if name == "get_dataset":
        d = DATASETS.get(args.get("name"))
        return d if d is not None else {"error": f"no dataset '{args.get('name')}'", "available": list(DATASETS)}
    if name == "record_claim":
        claim = {"statement": args.get("statement", ""), "value": str(args.get("value", "")),
                 "source": args.get("source", ""), "kind": args.get("kind", "observation")}
        ledger.append(claim)
        return {"recorded": True, "n_claims": len(ledger)}
    return {"error": f"unknown tool {name}"}


EXPLORE_SYSTEM = """You are a microbial-ecology data analyst exploring the outputs of a
16S amplicon pipeline to establish the factual basis for a Results section.

Work by EXPLORATION, not assumption:
1. list_datasets, then get_dataset to actually look at the numbers.
2. For every finding, call record_claim with the EXACT value and its source path so
   another analyst can independently re-verify it. Never state a number you did not read.
3. Be honest about data quality — if samples were lost or coverage is thin, record it as a
   quality_caveat. Do NOT invent statistics (e.g. ordination, diversity indices, per-sample
   tables) that no dataset provides.

Cover: sequencing/retention, how many samples survived, ASV counts, taxonomic composition,
and any data-quality caveats. Aim for ~6-10 well-sourced claims. When you have enough,
reply with the single word DONE (no more tool calls)."""


def explore(client, max_steps=14) -> list:
    ledger = []
    messages = [{"role": "system", "content": EXPLORE_SYSTEM},
                {"role": "user", "content": "Explore the datasets and record verifiable claims for the Results section."}]
    for step in range(max_steps):
        r = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS,
                                           tool_choice="auto", temperature=0.2, max_tokens=2000)
        msg = r.choices[0].message
        if not msg.tool_calls:
            content, _ = _visible_and_finish(r)
            print(f"  step {step}: no tool call — {content[:60]!r}")
            if "DONE" in (content or "").upper() or len(ledger) >= 6:
                break
            messages.append({"role": "user", "content": "Keep going: get_dataset and record_claim, or reply DONE."})
            continue
        # Echo the assistant tool-call turn back, then each tool result.
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _exec_tool(tc.function.name, args, ledger)
            tag = f"{tc.function.name}({args.get('name') or args.get('source') or ''})"
            print(f"  step {step}: {tag} -> {str(result)[:70]}")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)[:4000]})
    return ledger


# ── Independent verification: re-derive each claim from the data ───────────────
def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def verify(ledger: list) -> list:
    for c in ledger:
        found, actual = _navigate(c["source"])
        if not found:
            c["verdict"] = "unverifiable"
            c["actual"] = None
            continue
        c["actual"] = actual
        cv, av = _num(c["value"]), _num(actual)
        if cv is not None and av is not None:
            c["verdict"] = "verified" if abs(cv - av) < 0.05 * max(abs(av), 1) else "refuted"
        else:
            c["verdict"] = "verified" if str(c["value"]).strip() in str(actual) else "refuted"
    return ledger


WRITE_SYSTEM = """You are a scientific writing assistant for microbial ecology. Write a
Results section using ONLY the verified claims provided — every number must come from a
claim. Do not add statistics or findings not in the claims. Report quality caveats plainly
and politely. Past tense, objective, no interpretation. Reference Figure/Table where natural."""


def write_results(client, verified: list) -> str:
    claims_txt = "\n".join(
        f"- [{c['kind']}] {c['statement']} (value={c['value']}, source={c['source']})"
        for c in verified)
    study = (f"Study: {STUDY_GROUNDED['title']} — {STUDY_GROUNDED['study_name']} "
             f"({STUDY_GROUNDED['bioproject']}), {STUDY_GROUNDED['platform']}.")
    user = f"{study}\n\nVERIFIED CLAIMS:\n{claims_txt}\n\nWrite the Results section from these claims only."
    msgs = [{"role": "system", "content": WRITE_SYSTEM}, {"role": "user", "content": user}]
    # Big budget + empty-retry: qwen3.6 is a hidden-reasoning model, so the
    # reasoning channel draws from max_tokens and can leave 0 visible content
    # (issue #28). We call the client directly here (for temperature control),
    # so replicate that safety net.
    for mt in (6000, 12000):
        content, finish = _visible_and_finish(
            client.chat.completions.create(model=MODEL, messages=msgs, temperature=0.4, max_tokens=mt))
        if content.strip():
            return content
    return content


def main():
    _unload_all()
    ok = _lms_load(MODEL, 65536, 300)[1]
    if not ok:
        print("load failed"); return
    client = OpenAI(base_url=BASE_URL, api_key="lm-studio")

    print("=== EXPLORE ===")
    t0 = time.time()
    ledger = explore(client)
    verify(ledger)
    print(f"  {len(ledger)} claims in {time.time()-t0:.0f}s")
    for c in ledger:
        print(f"    [{c['verdict']:12}] {c['statement'][:70]}  (={c['value']}, actual={c.get('actual')})")

    verified = [c for c in ledger if c["verdict"] == "verified"]
    (OUT / "claims_ledger.json").write_text(json.dumps(ledger, indent=2, default=str) + "\n")

    print("\n=== WRITE (from verified claims only) ===")
    text = write_results(client, verified)
    unsupported = check_numbers_supported({"results": text}, results_data=load_fixture())
    n_unsupported = sum(len(i["detail"].split("may be unsupported:")[1].split(","))
                        for i in unsupported) if unsupported else 0
    (OUT / "results_from_claims.md").write_text(
        f"# Results from claims ({MODEL})\n\n"
        f"_{len(verified)}/{len(ledger)} claims verified; {n_unsupported} unsupported numbers_\n\n{text}\n")
    print(f"  verified {len(verified)}/{len(ledger)} claims; {n_unsupported} unsupported numbers in draft")
    print(f"  -> {OUT/'claims_ledger.json'}\n  -> {OUT/'results_from_claims.md'}")


if __name__ == "__main__":
    main()
