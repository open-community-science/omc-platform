"""Claim-centric autoresearch for the Results section (issue #29) — OFFLINE HARNESS.

This is now a THIN CLI over the reusable core in ``ai/autoresearch.py``. The core
holds the agenda loop, tools, two-layer verification (deterministic re-execution +
skeptical model reconciliation), the provenance DAG, and the Results writer,
refactored around injected collaborators. This file wires those to the bench's
local pieces — the LM-Studio loader, a synchronous-endpoint ``AsyncOpenAI`` client,
argparse, and file writing — so the offline eval stays reproducible:

    python tests/bench/results_explorer.py            # fresh model run
    python tests/bench/results_explorer.py --replicate   # + clean-room re-derivation
    python tests/bench/results_explorer.py --reverify [--reconcile]

``--replicate`` runs the clean-room pass (#50): a second analyst re-derives each
strong claim from the raw data WITHOUT seeing the original code. Set
``REPLICATE_MODEL`` to a different model than ``EXPLORER_MODEL`` — a model checking
its own work shares its own blind spots, which is the failure the pass exists to catch.

An agent EXPLORES the pipeline data — reading summaries AND running its own
analysis code — and records a ledger of VERIFIABLE claims, each linked to its
antecedents (data paths and/or computations), forming a provenance DAG. Results
prose is written from verified claims only.

Explorer model: env EXPLORER_MODEL (default qwen3.6-35b-a3b).
Out:  writings/{claims_ledger.json, claims_dag.json, claims_dag.md, results_from_claims.md}
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

from openai import AsyncOpenAI
from ai.autoresearch import (
    Autoresearcher, DirDataSource, LLMClient, SubprocessExecutor,
    dag_mermaid, _flatten_numbers,
)
from ai.manuscript_checks import check_numbers_supported
from fixtures import load_fixture, STUDY_GROUNDED, DATA_DIR
from run_bench import _unload_all, _lms_load

# Endpoint/model are env-configurable so the same loop can run on a LOCAL LM Studio
# model or a REMOTE OpenAI-compatible API (e.g. OpenRouter → anthropic/claude-sonnet-5).
# ── Hosts ─────────────────────────────────────────────────────────────────────
# Roles can run on DIFFERENT machines. With one GPU every role swaps the card, so a
# four-model run costs six loads; split across two boxes, each host holds its own
# model and the swaps mostly disappear. `lms` is driven per host — locally for the
# local server, over ssh for a remote one (reached through an ssh -L tunnel, so the
# remote LM Studio stays loopback-bound).
HOSTS = {
    "local": {"base_url": os.environ.get("LOCAL_BASE_URL", "http://localhost:1234/v1"),
              "lms": ["lms"]},
    "grid":  {"base_url": os.environ.get("GRID_BASE_URL", "http://localhost:1235/v1"),
              "lms": ["ssh", "grid", "export PATH=$PATH:~/.lmstudio/bin; lms"]},
}
BASE_URL = os.environ.get("EXPLORER_BASE_URL", HOSTS["local"]["base_url"])
API_KEY = os.environ.get("EXPLORER_API_KEY", "lm-studio")
REMOTE = not any(h in BASE_URL for h in ("localhost", "127.0.0.1"))

MODEL = os.environ.get("EXPLORER_MODEL", "qwen/qwen3.6-35b-a3b")
# The judge. Defaults AWAY from the explorer: a claimant grading its own claims is
# not verification, and with a model doing the judging that conflict is real.
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", os.environ.get("REPLICATE_MODEL", MODEL))
REPLICATE_MODEL = os.environ.get("REPLICATE_MODEL", MODEL)
# Round 3's casting vote. A third distinct model is ideal — the tiebreak should not
# share a lineage with either of the first two.
ADJUDICATE_MODEL = os.environ.get("ADJUDICATE_MODEL", REPLICATE_MODEL)

# Which host serves each role (default: everything local).
ROLE_HOST = {r: os.environ.get(f"{r.upper()}_HOST", "local")
             for r in ("explore", "verify", "replicate", "adjudicate", "write")}
ROLE_MODEL = {"explore": MODEL, "verify": VERIFY_MODEL, "replicate": REPLICATE_MODEL,
              "adjudicate": ADJUDICATE_MODEL, "write": MODEL}

OUT = HERE / "writings"
OUT.mkdir(exist_ok=True)


def _client_for(role: str) -> LLMClient:
    """An OpenAI-compatible client pointed at whichever host serves `role`."""
    host = HOSTS[ROLE_HOST[role]]
    return LLMClient(AsyncOpenAI(base_url=host["base_url"], api_key=API_KEY, timeout=1800),
                     ROLE_MODEL[role])


def _clients() -> dict:
    return {r: _client_for(r) for r in ROLE_HOST}


_LOADED = {}     # host -> currently resident model


def _switch_model(model: str, role: str = "explore") -> bool:
    """Make `model` the resident model on the host serving `role`.

    Unload before load: LM Studio's JIT loads on top and the engine dies once VRAM is
    near full. Because roles are spread across hosts, a host is only disturbed when
    one of ITS roles changes model."""
    hostname = ROLE_HOST[role]
    host = HOSTS[hostname]
    if _LOADED.get(hostname) == model:
        return True
    _run_lms(host, "unload --all", timeout=120)
    ok = _run_lms(host, f"load {model} -c 65536 --parallel 1 -y", timeout=600)
    _LOADED[hostname] = model if ok else None
    if not ok:      # fall back down the context ladder on a tight card
        for ctx in (49152, 32768, 16384):
            if _run_lms(host, f"load {model} -c {ctx} --parallel 1 -y", timeout=600):
                _LOADED[hostname] = model
                return True
    return ok


def _ensure_model_loaded():
    """Load the claimant's model on whichever host serves it."""
    return _switch_model(MODEL, "explore")


def _round_marks(reps) -> str:
    """Per-round outcome. `nocode`/`noresult` are the ANALYST failing, which is a
    different thing from disagreeing — printing both as "differ" made a broken
    derivation read as evidence against the claim."""
    out = []
    for r in reps:
        if not r.get("code"):
            mark = "nocode"
        elif not r.get("usable", True):
            mark = "noresult"
        else:
            mark = "agree" if r.get("agrees") else "differ"
        out.append(f"r{r.get('round', 2)}:{mark}")
    return " ".join(out)


async def _run_independent_rounds(ar):
    """Rounds 2 and 3, phased so each model gets its host to itself.

    Deriving and judging are separate passes because they use different models:
    judging inline made LM Studio JIT-load the judge on top of the analyst and the
    engine died. With roles spread across hosts, a host is only disturbed when one
    of ITS roles changes model."""
    print(f"\n=== ROUND 2: CLEAN-ROOM REPLICATION ({REPLICATE_MODEL}) ===", flush=True)
    if not _switch_model(REPLICATE_MODEL, "replicate"):
        print("  load failed — skipping", flush=True)
        return
    print(f"  {await ar.replicate(defer_judgment=True)} claims independently re-derived",
          flush=True)

    print(f"\n=== JUDGE ROUND 2 ({VERIFY_MODEL}) ===", flush=True)
    if _switch_model(VERIFY_MODEL, "verify"):
        print(f"  {await ar.judge_replications()} derivations judged", flush=True)

    print(f"\n=== ROUND 3: ADJUDICATION ({ADJUDICATE_MODEL}) ===", flush=True)
    if _switch_model(ADJUDICATE_MODEL, "adjudicate"):
        print(f"  {await ar.adjudicate(defer_judgment=True)} stand-offs given a casting vote",
              flush=True)
        print(f"\n=== JUDGE ROUND 3 ({VERIFY_MODEL}) ===", flush=True)
        if _switch_model(VERIFY_MODEL, "verify"):
            print(f"  {await ar.judge_replications()} derivations judged", flush=True)

    for c in ar.ledger:
        reps = c.get("replications") or []
        if reps:
            print(f"    [{c['verdict']:11}] {c['id']:4} {_round_marks(reps):34} "
                  f"{c['statement'][:44]}", flush=True)


def _run_lms(host: dict, args: str, timeout: int) -> bool:
    """Run an `lms` subcommand on a host (locally, or over ssh for a remote one)."""
    cmd = host["lms"][:]
    if cmd[0] == "ssh":
        cmd = cmd[:2] + [f"{cmd[2]} {args}"]
    else:
        cmd = cmd + args.split()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception as e:
        print(f"  [lms {args} on {cmd[:2]} failed: {e}]", flush=True)
        return False


def _data_source() -> DirDataSource:
    """A ``DataSource`` over the bench data dir. ``overview`` comes from the committed
    fixture snapshot (``load_fixture``) — kept explicit so the offline datasets match
    the prototype byte-for-byte; ``study`` is the module-level ``STUDY_GROUNDED``
    (which ``run_real_sample.py`` reassigns for a real submission)."""
    return DirDataSource(DATA_DIR, study=STUDY_GROUNDED or {}, overview=load_fixture())


def _executor() -> SubprocessExecutor:
    """The DEV/offline sandbox: a resource-limited subprocess over the same data dir."""
    return SubprocessExecutor(DATA_DIR)


def _make_researcher(llm: LLMClient) -> Autoresearcher:
    return Autoresearcher(_data_source(), llm, _executor(), clients=_clients(),
                          explore_model=MODEL, verify_model=VERIFY_MODEL,
                          replicate_model=REPLICATE_MODEL,
                          adjudicate_model=ADJUDICATE_MODEL)


def _supported_results_data(computations, ledger):
    """Ground-truth for check_numbers_supported = raw pipeline data PLUS verified
    computed values (a verified computation IS support). Includes fraction→percent
    forms so '0.4311' also backs a '43.1%' claim."""
    fx = load_fixture()
    forms = []
    for comp in computations.values():
        nums = []
        _flatten_numbers(comp["result"], nums)
        for n in nums:
            for v in (n, n * 100):
                forms += [round(v, 4), round(v, 2), round(v, 1)]
    return {"pipeline": fx,
            "verified_claims": [c["value"] for c in ledger
                                if c.get("verdict") in ("verified", "replicated")],
            "computed_support": sorted(set(forms))}


def _write_ledger(ar: Autoresearcher, completed: bool):
    """Ledger snapshot: claims, computations, agenda, assumptions, and the run
    summary (which now includes the clean-room replication counts)."""
    (OUT / "claims_ledger.json").write_text(json.dumps(
        {"claims": ar.ledger, "computations": ar.computations, "agenda": ar.agenda,
         "assumptions": ar.assumptions, "run": ar.run_summary(completed)},
        indent=2, default=str) + "\n")


def _write_dag(ar: Autoresearcher, verified: int, status: str):
    dag = ar.build_dag()
    (OUT / "claims_dag.json").write_text(json.dumps(dag, indent=2, default=str) + "\n")
    (OUT / "claims_dag.md").write_text(
        f"# Claim provenance DAG ({MODEL}) — {status}\n\n{verified}/{len(ar.ledger)} claims verified · "
        f"{len(ar.computations)} computations\n\nLegend: 🟢 replicated · 🟩 verified · "
        f"🟪 disputed · 🟧 partly supported · 🟥 refuted · 🟨 unverifiable · "
        f"🟦 computation · ⬛ data\n\n{dag_mermaid(dag)}\n")


async def _reverify_async(llm):
    """Re-run verification (and rebuild the DAG) on the saved ledger. With no client
    it is purely deterministic (no model — fully reproducible); with a client, a
    deterministic miss escalates to skeptical model reconciliation."""
    saved = json.loads((OUT / "claims_ledger.json").read_text())
    ar = Autoresearcher.from_snapshot(saved, _data_source(), llm or LLMClient(None, MODEL),
                                      _executor(), clients=(_clients() if llm else None),
                                      explore_model=MODEL, verify_model=VERIFY_MODEL,
                                      replicate_model=REPLICATE_MODEL,
                                      adjudicate_model=ADJUDICATE_MODEL)
    _switch_model(VERIFY_MODEL, "verify")
    await ar.verify()
    # Re-grade a SAVED ledger through the independent rounds without re-exploring —
    # the cheap way to ask "how many of these claims survive a clean-room check?".
    if llm is not None and "--replicate" in sys.argv:
        await _run_independent_rounds(ar)
    done = sum(a["status"] == "done" for a in ar.agenda)
    completed = bool(ar.agenda) and all(a["status"] == "done" for a in ar.agenda)
    _write_ledger(ar, completed)
    verified_claims = [c for c in ar.ledger if c["verdict"] in ("verified", "replicated")]
    verified = len(verified_claims)
    status = "complete" if completed else f"INCOMPLETE ({done}/{len(ar.agenda)} investigations)"
    _write_dag(ar, verified, status)
    # With a client, regenerate the Results prose so the snapshot is coherent.
    if llm is not None and verified_claims:
        banner = "" if completed else (
            f"> ⚠️ PRELIMINARY — {len(ar.agenda) - done} of {len(ar.agenda)} investigations outstanding; "
            f"these Results are partial.\n\n")
        text = await ar.write_results(verified_claims)
        (OUT / "results_from_claims.md").write_text(
            f"# Results from claims ({MODEL}) — {status}\n\n{banner}"
            f"_{verified}/{len(ar.ledger)} verified · {len(ar.computations)} computations · "
            f"{done}/{len(ar.agenda)} investigations_\n\n{text}\n")
    print(f"re-verified {verified}/{len(ar.ledger)} claims (offline)")
    for c in ar.ledger:
        if c["verdict"] != "verified":
            print(f"    [{c['verdict']:12}] {c['id']} {c['statement'][:60]}")
        elif c.get("method") and c["method"] != "direct":
            print(f"    [verified:{c['method']:14}] {c['id']} {c['statement'][:52]}")


def reverify_saved(client=None):
    """Sync entry point (used by run_real_sample.py): re-verify the saved ledger.
    ``client`` is an ``LLMClient`` or None."""
    asyncio.run(_reverify_async(client))


async def _main_async(llm: LLMClient):
    print(f"model: {MODEL} @ {BASE_URL}")
    print("=== EXPLORE (agenda-driven, recursive) ===")
    t0 = time.time()
    ar = _make_researcher(llm)
    completed = await ar.explore()
    print(f"\n=== VERIFY (judged by {VERIFY_MODEL}) ===", flush=True)
    _switch_model(VERIFY_MODEL, "verify")
    await ar.verify()
    if "--replicate" in sys.argv:
        await _run_independent_rounds(ar)
    done = sum(a["status"] == "done" for a in ar.agenda)
    outstanding = [a for a in ar.agenda if a["status"] != "done"]
    print(f"\n  agenda ({done}/{len(ar.agenda)} investigations done"
          f"{'' if completed else f' — INCOMPLETE, {len(outstanding)} outstanding'}):")
    for a in ar.agenda:
        tag = "  └─" if a["parent"] else "•"
        print(f"    {tag} [{a['status']}] {a['id']}: {a['question'][:72]}")
    print(f"\n  {len(ar.ledger)} claims, {len(ar.computations)} computations in {time.time()-t0:.0f}s")
    for c in ar.ledger:
        print(f"    [{c.get('verdict'):12}|{c.get('kind','?')[:7]:7}] {c['statement'][:60]}  (={c['value']})")

    verified = [c for c in ar.ledger if c["verdict"] in ("verified", "replicated")]
    status = "complete" if completed else f"INCOMPLETE ({done}/{len(ar.agenda)} investigations)"
    banner = "" if completed else (
        f"> ⚠️ PRELIMINARY — exploration stopped with {len(outstanding)} of {len(ar.agenda)} "
        f"investigations outstanding ({', '.join(a['id'] for a in outstanding)}); "
        f"these Results are partial.\n\n")
    # One atomic snapshot: ledger, DAG, and prose written from the SAME run state.
    _write_ledger(ar, completed)
    _write_dag(ar, len(verified), status)

    print(f"\n=== WRITE (verified claims only, by {MODEL}) ===")
    _switch_model(MODEL, "write")   # the writer is the draft model, not the judge
    text = await ar.write_results(verified)
    unsupported = check_numbers_supported({"results": text}, results_data=_supported_results_data(
        ar.computations, ar.ledger))
    n_unsupported = sum(len(i["detail"].split("may be unsupported:")[1].split(","))
                        for i in unsupported) if unsupported else 0
    (OUT / "results_from_claims.md").write_text(
        f"# Results from claims ({MODEL}) — {status}\n\n{banner}"
        f"_{len(verified)}/{len(ar.ledger)} verified · {len(ar.computations)} computations · "
        f"{n_unsupported} unsupported numbers · {done}/{len(ar.agenda)} investigations_\n\n{text}\n")
    print(f"  {status}; verified {len(verified)}/{len(ar.ledger)}; {n_unsupported} unsupported numbers")
    print(f"  -> claims_ledger.json, claims_dag.json, claims_dag.md, results_from_claims.md")


def main():
    if "--reverify" in sys.argv:
        client = None
        if "--reconcile" in sys.argv:   # verification needs a model to judge
            if not _ensure_model_loaded():
                print("load failed"); return
            client = _client_for("verify")
        reverify_saved(client); return
    if not _ensure_model_loaded():
        print("load failed"); return
    asyncio.run(_main_async(_client_for("explore")))


if __name__ == "__main__":
    main()
