"""Claim-centric autoresearch for the Results section (issue #29) — OFFLINE HARNESS.

This is now a THIN CLI over the reusable core in ``ai/autoresearch.py``. The core
holds the agenda loop, tools, two-layer verification (deterministic re-execution +
skeptical model reconciliation), the provenance DAG, and the Results writer,
refactored around injected collaborators. This file wires those to the bench's
local pieces — the LM-Studio loader, a synchronous-endpoint ``AsyncOpenAI`` client,
argparse, and file writing — so the offline eval stays reproducible:

    python tests/bench/results_explorer.py            # fresh model run
    python tests/bench/results_explorer.py --replicate   # + clean-room re-derivation
    python tests/bench/results_explorer.py --hyphal      # branching tips, not one session
    python tests/bench/results_explorer.py --reverify [--reconcile]

``--hyphal`` explores by growing one short-lived tip per investigation (#58) instead
of one long-lived session: each tip is seeded from the shared ledger and discarded
when its item is done. `EXPLORER_TIP_STEPS` sizes a tip.

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

# Exploration budget. Every bench run so far has ended `interrupted` on the step cap,
# so we have never seen what a COMPLETE investigation produces — these are env knobs
# specifically so a run can be given enough rope to finish on its own DONE instead.
MAX_STEPS = int(os.environ.get("EXPLORER_MAX_STEPS", "48"))
MAX_FOLLOWUPS = int(os.environ.get("EXPLORER_MAX_FOLLOWUPS", "12"))
# Claims sent for independent re-derivation per round. The default truncates silently;
# raise it in step with the step cap or a long run's later claims never get checked.
MAX_REPLICATE = int(os.environ.get("EXPLORER_MAX_REPLICATE", "12"))
# --hyphal (#58): steps ONE tip gets for its own investigation. Its context is seeded
# fresh, so this is the whole size of a tip — not a share of a growing transcript.
TIP_STEPS = int(os.environ.get("EXPLORER_TIP_STEPS", "16"))
# --one-claim (#61): the analyst's context dies when it banks a claim and a successor
# carries the same investigation on. EPOCHS re-germinates an agenda that can see what
# the previous round found. LIVE_VERIFY judges on the verify host WHILE the analyst
# explores — which only pays off when that host is not the analyst's.
EPOCHS = int(os.environ.get("EXPLORER_EPOCHS", "1"))
MAX_CLAIMS_PER_ITEM = int(os.environ.get("EXPLORER_MAX_CLAIMS_PER_ITEM", "6"))
# Generation budget per turn. A reasoning model can spend the whole of a small budget
# thinking and return no tool call, which costs a step and reads as a refusal.
MAX_TOKENS = int(os.environ.get("EXPLORER_MAX_TOKENS", "6000"))

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
    print(f"  {await ar.replicate(max_claims=MAX_REPLICATE, defer_judgment=True)} "
          "claims independently re-derived", flush=True)

    print(f"\n=== JUDGE ROUND 2 ({VERIFY_MODEL}) ===", flush=True)
    if _switch_model(VERIFY_MODEL, "verify"):
        print(f"  {await ar.judge_replications()} derivations judged", flush=True)

    print(f"\n=== ROUND 3: ADJUDICATION ({ADJUDICATE_MODEL}) ===", flush=True)
    if _switch_model(ADJUDICATE_MODEL, "adjudicate"):
        print(f"  {await ar.adjudicate(max_claims=MAX_REPLICATE, defer_judgment=True)} "
              "stand-offs given a casting vote", flush=True)
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


async def _print_progress(event: str, detail: dict):
    """Exploration used to print NOTHING until it finished, which on a multi-hour run
    is indistinguishable from a hang. Keep it to the structural events."""
    if event == "germinate":
        print("  germinating agenda…", flush=True)
    elif event == "tip":
        parent = f" ⤶ {detail['parent']}" if detail.get("parent") else ""
        print(f"  ▸ {detail['id']}{parent}: {detail.get('question')} "
              f"({detail.get('claims_seen')} claims in hand)", flush=True)
    elif event == "tip_done":
        print(f"    ✓ {detail['id']} {detail['status']} ({detail.get('claims')} claims)", flush=True)
    elif event == "sweep":
        print(f"  sweeping {detail.get('claims')} claims for assumptions…", flush=True)
    elif event == "run_analysis":
        # Printed mainly so silence means something. A tip can spend many minutes on
        # analyses without recording anything, and an unbroken quiet log is otherwise
        # indistinguishable from a hang.
        res = detail.get("result") or {}
        mark = res.get("computation_id", "failed")
        # Carry the sandbox error. "failed" alone hides whether the analysis was wrong,
        # the frame was misread, or the code never ran — three different problems.
        err = f"  — {detail['error']}" if detail.get("error") else ""
        print(f"      · {mark} {detail.get('label')}{err}", flush=True)
    elif event == "record_claim" and (detail.get("result") or {}).get("recorded"):
        print(f"      + {detail['result']['claim_id']} {detail.get('label')}", flush=True)
    elif event == "add_followup" and (detail.get("result") or {}).get("added"):
        print(f"      ↳ {detail['result']['added']}: {detail.get('label')}", flush=True)
    elif event == "truncated":
        print(f"      ! reply cut off at the token limit ({detail.get('tip')})", flush=True)
    elif event == "germinate_failed":
        print(f"  !! germination proposed no agenda after {detail['steps']} steps — "
              "nothing to explore; stopping", flush=True)
    elif event == "hyphal_done":
        print(f"  {detail['tips']} tips, {detail['claims']} claims, "
              f"{detail['steps']} steps", flush=True)


def _progress_for(ar: Autoresearcher):
    """Print the structural events, and publish the run's own state as it goes.

    The ledger used to reach disk exactly once, at the very end. Everything watching
    a run in progress therefore had to read the LOG, which is a summary by
    construction — and a tip's transcript is discarded when the tip ends, so nothing
    else survived either. A run that died at hour three left nothing but its printout.
    Snapshotting at each structural event makes the authoritative record continuously
    available, to a viewer and to a post-mortem alike."""
    async def _progress(event: str, detail: dict):
        await _print_progress(event, detail)
        if event in ("tip_done", "sweep", "hyphal_done") or (
                event == "record_claim" and (detail.get("result") or {}).get("recorded")):
            _write_json(OUT / "run_state.json", _ledger_dict(ar, None))
    return _progress


def _make_researcher(llm: LLMClient) -> Autoresearcher:
    ar = Autoresearcher(_data_source(), llm, _executor(), clients=_clients(),
                        explore_model=MODEL, verify_model=VERIFY_MODEL,
                        replicate_model=REPLICATE_MODEL,
                        adjudicate_model=ADJUDICATE_MODEL,
                        max_steps=MAX_STEPS, max_followups=MAX_FOLLOWUPS,
                        max_tokens=MAX_TOKENS)
    ar.on_progress = _progress_for(ar)      # needs the researcher it reports on
    return ar


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


def _ledger_dict(ar: Autoresearcher, completed: bool | None) -> dict:
    """Claims, computations, agenda, assumptions and the run summary.

    One shape for the live snapshot and the final artifact, so anything that can read
    a finished run can read a running one without knowing the difference."""
    return {"claims": ar.ledger, "computations": ar.computations, "agenda": ar.agenda,
            "assumptions": ar.assumptions, "run": ar.run_summary(completed)}


def _write_json(path: Path, payload: dict):
    """Write via a temp file and rename. A viewer polls this every few seconds, and a
    half-written file is a parse error at exactly the moment something interesting
    just happened."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    tmp.replace(path)


def _write_ledger(ar: Autoresearcher, completed: bool):
    _write_json(OUT / "claims_ledger.json", _ledger_dict(ar, completed))


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
    hyphal = "--hyphal" in sys.argv
    one_claim = "--one-claim" in sys.argv
    # Judging concurrently is only a win when the judge has its own machine. On one
    # host it would fight the analyst for the card, which is what phasing avoids.
    live = one_claim and ROLE_HOST["verify"] != ROLE_HOST["explore"]
    print(f"model: {MODEL} @ {BASE_URL}")
    mode = ("hyphal — branching short-lived tips (#58)" if not one_claim else
            f"hyphal, claim-sized contexts, {EPOCHS} epoch(s)"
            + (f", judging live on {ROLE_HOST['verify']}" if live else ""))
    print(f"=== EXPLORE ({mode if hyphal else 'agenda-driven, one long-lived session'}) ===",
          flush=True)
    if one_claim and not live:
        print("  (verify shares the explorer's host — judging stays batched)", flush=True)
    t0 = time.time()
    ar = _make_researcher(llm)
    completed = await (ar.explore_hyphal(
        tip_steps=TIP_STEPS, one_claim=one_claim, live_verify=live, epochs=EPOCHS,
        max_claims_per_item=MAX_CLAIMS_PER_ITEM) if hyphal else ar.explore())
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
