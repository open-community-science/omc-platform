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
from ai.sandbox_packages import (ALLOWED as GRANTABLE_PACKAGES,
                                 install as install_package, is_available as is_package_available)
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
    # grid's llama.cpp CONTAINER. A server process serves ONE model and has no load
    # verb, so `lms` is None and model switching is a no-op — the host simply holds
    # whatever it was started with. Worth having as its own host because that container
    # runs a CURRENT llama.cpp: grid's llmster is pinned at the last version its glibc
    # can load (0.0.6-1, March), whose jinja cannot render a tool-calling template at
    # all, so the explorer could never run there. Through the container it can.
    "grid-cc": {"base_url": os.environ.get("GRID_LLAMACPP_URL", "http://localhost:1237/v1"),
                "lms": None},
}
BASE_URL = os.environ.get("EXPLORER_BASE_URL", HOSTS["local"]["base_url"])
API_KEY = os.environ.get("EXPLORER_API_KEY", "lm-studio")
REMOTE = not any(h in BASE_URL for h in ("localhost", "127.0.0.1"))

MODEL = os.environ.get("EXPLORER_MODEL", "qwen/qwen3.6-35b-a3b")
# The clean-room analyst. Its whole job is to WRITE ONE ANALYSIS, so it defaults to a
# code model rather than to the explorer. Measured on the same prompt and claim:
# qwen3-coder-30b wrote the derivation in 604 tokens and 16s; the reasoning explorer
# spent 7765 tokens and 251s inside <think> and emitted no code at all, nine times in a
# row across a run (#73). "Point it at a different model" was already the documented
# advice — the default just contradicted it, so the shipped configuration was the
# broken one.
REPLICATE_MODEL = os.environ.get("REPLICATE_MODEL", "unsloth/qwen3-coder-30b-a3b-instruct")
# The judge. Defaults AWAY from the explorer: a claimant grading its own claims is
# not verification, and with a model doing the judging that conflict is real. It no
# longer borrows REPLICATE_MODEL, which would now hand the judging to a code model —
# reading evidence and grading a claim is not the job that default was chosen for.
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", MODEL)
# Round 3's casting vote — which also RE-DERIVES rather than opining (it runs the same
# path as round 2), so it wants a code model too. It no longer defaults to
# REPLICATE_MODEL: a tiebreak cast by the same model that produced one of the two votes
# shares that vote's blind spots, which is the whole thing round 3 exists to escape. A
# different lineage from either of the first two (qwen explorer, qwen coder) by default.
ADJUDICATE_MODEL = os.environ.get("ADJUDICATE_MODEL", "mistralai/devstral-small-2-2512")

# Which host serves each role. Replication defaults to `grid` because that is where
# its model lives: the Q4 coder is indexed there and the big card holds it alongside
# whatever the small card is running, so the judge and the clean-room analyst stop
# taking turns on one GPU. A model name is only valid on its own host, which
# check_role_models() enforces before a run starts.
ROLE_HOST = {r: os.environ.get(f"{r.upper()}_HOST", "grid" if r == "replicate" else "local")
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
# thinking and return no tool call, which costs a step and reads as a refusal. But the
# budget is also a LATENCY CEILING: a run-away turn costs the whole of it, measured at
# ~11 minutes for 16000 tokens on a model too large for its card (~24 tok/s). It costs
# nothing when unused, so this is a tail-risk setting, not a throughput one.
MAX_TOKENS = int(os.environ.get("EXPLORER_MAX_TOKENS", "6000"))
# The clean-room analyst writes a whole analysis from scratch having never seen the
# original code, so it needs MORE room than a turn of exploration, not less. It had 3000
# hardcoded, and returned no code block on 9 consecutive attempts (#73).
REPLICATE_MAX_TOKENS = int(os.environ.get("EXPLORER_REPLICATE_MAX_TOKENS", "12000"))

OUT = HERE / "writings"
OUT.mkdir(exist_ok=True)


# Roles whose job is to WRITE ONE ANALYSIS, not to deliberate. A reasoning model given
# `replicate` spent its whole budget inside <think> and emitted no code nine times in a
# row; the same model with thinking off wrote the analysis and stopped (#73). Measured on
# one claim, same prompt, same server:
#
#   nemotron-3.5-lightning  thinking on  -> 12000 tok, 107s, no code
#                           thinking off ->   717 tok,   6s, code
#   qwen3.6-35b-a3b         thinking on  ->  7765 tok, 251s, no code
#                           thinking off ->  6506 tok, 223s, code
#
# `chat_template_kwargs` is a llama.cpp/vLLM extension — a hosted frontier API would
# reject it — so this is per-endpoint and switchable off wholesale with
# EXPLORER_NO_THINK_ROLES="" for a backend that does not speak it.
NO_THINK_ROLES = {r for r in os.environ.get("EXPLORER_NO_THINK_ROLES",
                                            "replicate,adjudicate").split(",") if r}


def _client_for(role: str) -> LLMClient:
    """An OpenAI-compatible client pointed at whichever host serves `role`."""
    host = HOSTS[ROLE_HOST[role]]
    extra = ({"chat_template_kwargs": {"enable_thinking": False}}
             if role in NO_THINK_ROLES else None)
    return LLMClient(AsyncOpenAI(base_url=host["base_url"], api_key=API_KEY, timeout=1800),
                     ROLE_MODEL[role], extra_body=extra)


def _clients() -> dict:
    return {r: _client_for(r) for r in ROLE_HOST}


_LOADED = {}     # host -> currently resident model


def _host_models(hostname: str) -> set:
    """Model names THIS host will answer to. Asked of the host itself rather than
    assumed, because the same weights carry different names on different machines:
    one LM Studio calls the file `qwen/qwen3-coder-30b`, another calls the identical
    file (same repo, same quant, same bytes) `qwen3-coder-30b-a3b-instruct`."""
    host = HOSTS[hostname]
    if not host.get("lms"):                     # a container: ask its API
        try:
            import urllib.request
            with urllib.request.urlopen(host["base_url"].rstrip("/") + "/models",
                                        timeout=20) as r:
                return {m["id"] for m in json.load(r).get("data", [])}
        except Exception:
            return set()
    cmd = host["lms"][:]
    cmd = cmd[:2] + [f"{cmd[2]} ls"] if cmd[0] == "ssh" else cmd + ["ls"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return set()
    return {ln.split()[0] for ln in out.splitlines()
            if ln.strip() and not ln.startswith(" ") and ln.split()[0] not in ("LLM", "EMBEDDING", "You")}


def check_role_models() -> bool:
    """Every role's model must exist ON THE HOST THAT SERVES IT.

    A model string is only meaningful on its own host, and `_switch_model` shells out
    to `lms load <model>`: give it a name from the wrong host and the load fails, the
    return value is ignored, and the API then JIT-loads by fuzzy match ON TOP of what
    is already resident. That is not a crash — it is 36 GB of models on a 20 GB card,
    4% GPU utilisation, and a pass that takes 45x longer with no error anywhere. Cheap
    to check up front; nearly invisible afterwards."""
    ok = True
    for role, hostname in ROLE_HOST.items():
        want = ROLE_MODEL[role]
        have = _host_models(hostname)
        if not have:
            print(f"  ! {role}: could not list models on {hostname}; not checking", flush=True)
            continue
        if want not in have:
            near = [m for m in sorted(have) if want.split("/")[-1][:12].lower() in m.lower()]
            print(f"  ! {role} wants '{want}' but {hostname} does not serve it."
                  + (f" Did you mean: {', '.join(near[:3])}?" if near else ""), flush=True)
            ok = False
    return ok


def _switch_model(model: str, role: str = "explore") -> bool:
    """Make `model` the resident model on the host serving `role`.

    Unload before load: LM Studio's JIT loads on top and the engine dies once VRAM is
    near full. Because roles are spread across hosts, a host is only disturbed when
    one of ITS roles changes model."""
    hostname = ROLE_HOST[role]
    host = HOSTS[hostname]
    if not host.get("lms"):
        # A host that serves one fixed model. Say so once, because "switching" to a
        # model that is not the one loaded would otherwise look like it succeeded.
        if _LOADED.get(hostname) is None:
            _LOADED[hostname] = model
            print(f"  {hostname} serves a fixed model; not switching for {role}", flush=True)
        return True
    if _LOADED.get(hostname) == model:
        return True
    _run_lms(host, "unload --all", timeout=120)
    ok = _run_lms(host, f"load {model} -c 65536 --parallel 1 -y", timeout=600)
    _LOADED[hostname] = model if ok else None
    if not ok:
        print(f"  ! could not load {model} on {hostname} at 65536; trying smaller", flush=True)
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
            # `or "ungraded"`, because a verdict is NOT guaranteed here. When the judge
            # fails it leaves the claim ungraded on purpose (ae62511) rather than
            # inventing a verdict, and `{None:11}` is a TypeError. A run that had
            # produced 53 claims and 190 computations over 14 hours died on this line
            # while writing its final output.
            print(f"    [{(c.get('verdict') or 'ungraded'):11}] {c['id']:4} "
                  f"{_round_marks(reps):34} {c['statement'][:44]}", flush=True)


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


# ── Sandbox ───────────────────────────────────────────────────────────────────
# The default is a child process, which is a STAND-IN and not a sandbox: it runs on this
# interpreter, with this environment, on this filesystem. EXPLORER_EXECUTOR=container
# runs the analyst's code in the image production actually uses (omc-session — R with
# vegan/phyloseq/DESeq2/ALDEx2 layered over the Python stack) through the SAME
# ContainerExecutor the portal drives, so a bench run exercises the production sandbox
# without the portal, its database, or a launched author session.
EXECUTOR = os.environ.get("EXPLORER_EXECUTOR", "subprocess")
CONTAINER_IMAGE = os.environ.get("EXPLORER_CONTAINER_IMAGE", "omc-session:latest")
CONTAINER = f"omc-session-{DATA_DIR.name}"       # the name ContainerExecutor derives
# Seconds ONE analysis may run. Nothing passes a per-call timeout, so the executor
# default is the whole budget — and it silently differed by sandbox (30s subprocess,
# 60s container). A timeout reaches the analyst as a bare failure it cannot tell from
# bad code, so an analysis that merely needed longer reads as one that was wrong.
# Production's equivalent is AUTORESEARCH_MAX_ANALYSIS_S.
ANALYSIS_S = int(os.environ.get("EXPLORER_ANALYSIS_S", "60"))


def _ensure_container() -> str:
    """Start — or reuse — the container ContainerExecutor talks to.

    Mirrors what sessions.py gives a real session (data read-only at /data/viz, 2g/1cpu,
    no network) minus Chainlit/Marimo, which analysis never touches: the entrypoint is
    replaced with a sleep so the container is nothing but a place to `docker exec` into.
    Reused when already up, so a series of bench runs pays the start cost once."""
    up = subprocess.run(["docker", "ps", "-q", "-f", f"name=^{CONTAINER}$"],
                        capture_output=True, text=True).stdout.strip()
    if up:
        return CONTAINER
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    r = subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER,
         # DATA_DIR holds the viz JSONs directly, which is where /data/viz points.
         "-v", f"{DATA_DIR}:/data/viz:ro",
         "--memory", "2g", "--cpus", "1", "--network", "none",
         "--entrypoint", "sleep", CONTAINER_IMAGE, "infinity"],
        capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"could not start {CONTAINER}: {(r.stderr or '').strip()[:300]}")
    return CONTAINER


def _container_is_available(import_name: str) -> bool:
    """Is `import_name` importable INSIDE the sandbox? Only allowlisted names are ever
    asked, so nothing a model wrote reaches the code that runs in the container."""
    if import_name not in GRANTABLE_PACKAGES:
        return False
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "python3", "-c",
         f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({import_name!r}) else 1)"],
        capture_output=True, timeout=60)
    return r.returncode == 0


def _container_install(import_name: str) -> dict:
    """`sandbox_packages.install`, but into the container instead of this interpreter.
    Same discipline: the model's string is a lookup key, never a command-line argument,
    and the PyPI name comes from the allowlist."""
    name = (import_name or "").strip()
    if name not in GRANTABLE_PACKAGES:
        return {"installed": False, "available": False,
                "reason": "not on the sandbox allowlist"}
    if _container_is_available(name):
        return {"installed": False, "available": True, "reason": "already available"}
    dist = GRANTABLE_PACKAGES[name]              # from the allowlist, not the request
    r = subprocess.run(["docker", "exec", CONTAINER, "pip", "install", "--quiet",
                        "--no-input", dist], capture_output=True, text=True, timeout=600)
    ok = r.returncode == 0 and _container_is_available(name)
    return {"installed": ok, "available": ok,
            "reason": "installed" if ok else (r.stderr or r.stdout or "install failed").strip()[:200]}


def _executor():
    """The sandbox the analyst's code runs in — a child process, or the real container."""
    if EXECUTOR == "container":
        from portal.app.autoresearch_executor import ContainerExecutor
        _ensure_container()
        return ContainerExecutor(DATA_DIR.name, default_timeout=ANALYSIS_S)
    return SubprocessExecutor(DATA_DIR, default_timeout=ANALYSIS_S)


_ANNOUNCED: set = set()      # agenda ids already printed, so epoch 2 lists only its own


async def _print_progress(event: str, detail: dict):
    """Exploration used to print NOTHING until it finished, which on a multi-hour run
    is indistinguishable from a hang. Keep it to the structural events."""
    if event == "epoch_budget":
        # What this round may actually spend, said before it spends it. The banner
        # announces the epoch count up front and is never corrected, so this is the
        # only place a reader learns whether the round was funded (#74).
        print(f"  round {detail['epoch'] + 1}: {detail['steps']} steps "
              f"({detail['spent']}/{detail['of']} of the budget already spent)", flush=True)
    elif event == "epoch_budget_thin":
        print(f"  ⚠ {detail['epochs']} epochs share {detail['budget']} steps — "
              f"{detail['per_epoch']} each, less than one tip ({detail['tip_steps']}). "
              f"Raise EXPLORER_MAX_STEPS or ask for fewer epochs.", flush=True)
    elif event == "germinate":
        print("  germinating agenda…", flush=True)
    elif event == "agenda":
        print(f"  agenda for round {detail.get('epoch', 0) + 1} — "
              f"{len(detail['items'])} investigations:", flush=True)
        for it in detail["items"]:
            if it["id"] not in _ANNOUNCED:
                _ANNOUNCED.add(it["id"])
                print(f"    · {it['id']}: {it['question']}", flush=True)
    elif event == "tip":
        parent = f" ⤶ {detail['parent']}" if detail.get("parent") else ""
        print(f"  ▸ {detail['id']}{parent}: {detail.get('question')} "
              f"({detail.get('claims_seen')} claims in hand)", flush=True)
    elif event == "tip_done":
        print(f"    ✓ {detail['id']} {detail['status']} ({detail.get('claims')} claims)", flush=True)
    elif event == "sweep":
        print(f"  sweeping {detail.get('claims')} claims for assumptions…", flush=True)
    elif event == "verify":
        # The judge emitted these from the start and nothing printed them, so a run
        # where judging had quietly stalled at 2 of 19 claims looked exactly like a run
        # where it was keeping up. Verdicts are the point of the subsystem; show them.
        v = detail.get("verdict", "?")
        mark = {"verified": "✔", "replicated": "✔", "partial": "◐", "refuted": "✘",
                "overturned": "✘", "disputed": "⚠", "contested": "⚠"}.get(v, "?")
        print(f"      {mark} {detail.get('claim')} {v}", flush=True)
    elif event == "verify_error":
        print(f"      ✘ {detail.get('claim')} judge error — {detail.get('error')}",
              flush=True)
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
    elif event == "package_installed":
        print(f"      + installed {detail['package']} ({detail.get('distribution')}) "
              "— now importable", flush=True)
    elif event == "package_refused":
        print(f"      ? wants {detail['package']} — {detail.get('reason')}", flush=True)
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
        if event in ("agenda", "tip", "tip_done", "sweep", "hyphal_done", "verify") or (
                event == "record_claim" and (detail.get("result") or {}).get("recorded")):
            _write_json(OUT / "run_state.json", _ledger_dict(ar, None))
    return _progress


def _make_researcher(llm: LLMClient) -> Autoresearcher:
    ar = Autoresearcher(_data_source(), llm, _executor(), clients=_clients(),
                        explore_model=MODEL, verify_model=VERIFY_MODEL,
                        replicate_model=REPLICATE_MODEL,
                        adjudicate_model=ADJUDICATE_MODEL,
                        max_steps=MAX_STEPS, max_followups=MAX_FOLLOWUPS,
                        max_tokens=MAX_TOKENS,
                        replicate_max_tokens=REPLICATE_MAX_TOKENS,
                        # A grantable request is granted wherever the sandbox actually
                        # is: this interpreter for the subprocess stand-in, the running
                        # container otherwise. Installing on the host while the code
                        # runs in the container would report success the analyst can't use.
                        package_installer=(_container_install if EXECUTOR == "container"
                                           else install_package))
    ar.on_progress = _progress_for(ar)      # needs the researcher it reports on
    ar.grantable_packages = tuple(sorted(GRANTABLE_PACKAGES))
    # Anything a previous run installed is still there — the interpreter persists between
    # bench runs, and so does the container while it is up. Record what was already
    # present rather than pretending each run starts clean; several of these are
    # load-bearing elsewhere and must not be removed.
    _available = _container_is_available if EXECUTOR == "container" else is_package_available
    ar.preinstalled_packages = tuple(sorted(
        p for p in GRANTABLE_PACKAGES if _available(p)))
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
            "assumptions": ar.assumptions, "package_requests": ar.package_requests,
            "failures": ar.failures, "run": ar.run_summary(completed)}


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
    verified_claims = [c for c in ar.ledger if c.get("verdict") in ("verified", "replicated")]
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
        if c.get("verdict") != "verified":
            # None when the judge failed rather than graded — same guard as the fresh run.
            print(f"    [{str(c.get('verdict') or 'ungraded'):12}] {c['id']} {c['statement'][:60]}")
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
    verified = [c for c in ar.ledger if c.get("verdict") in ("verified", "replicated")]
    status = "complete" if completed else f"INCOMPLETE ({done}/{len(ar.agenda)} investigations)"
    banner = "" if completed else (
        f"> ⚠️ PRELIMINARY — exploration stopped with {len(outstanding)} of {len(ar.agenda)} "
        f"investigations outstanding ({', '.join(a['id'] for a in outstanding)}); "
        f"these Results are partial.\n\n")
    # One atomic snapshot: ledger, DAG, and prose written from the SAME run state.
    # BEFORE the summary below: these lines are the whole product of the run, and
    # printing it is not worth risking it. A formatting error in a progress line has
    # twice now destroyed the output of a run that had already finished the work.
    _write_ledger(ar, completed)
    _write_dag(ar, len(verified), status)

    print(f"\n  agenda ({done}/{len(ar.agenda)} investigations done"
          f"{'' if completed else f' — INCOMPLETE, {len(outstanding)} outstanding'}):")
    for a in ar.agenda:
        tag = "  └─" if a["parent"] else "•"
        print(f"    {tag} [{a['status']}] {a['id']}: {a['question'][:72]}")
    print(f"\n  {len(ar.ledger)} claims, {len(ar.computations)} computations in {time.time()-t0:.0f}s")
    for c in ar.ledger:
        # `or "ungraded"`: a judge that fails leaves the verdict None on purpose, and
        # `{None:12}` is a TypeError (see the same guard in _run_independent_rounds).
        print(f"    [{str(c.get('verdict') or 'ungraded'):12}|{c.get('kind','?')[:7]:7}] "
              f"{c['statement'][:60]}  (={c['value']})")

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
    # Before anything is loaded or any hours are spent. A role pointed at a model its
    # host does not serve does not fail — it degrades, silently, into loading on top of
    # whatever is resident.
    if not check_role_models() and "--force-models" not in sys.argv:
        print("  refusing to start; fix the role/host pairing or pass --force-models")
        return
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
