"""Claim-grounded autoresearch core (issue #29) — reusable, injection-based.

An agent EXPLORES pipeline data — reading summaries AND running its own analysis
code — and records a ledger of VERIFIABLE claims. Claims are the currency: each
links to its ANTECEDENTS (the data paths and/or the computations it depends on),
forming a provenance **DAG**. Every claim is re-derivable — a data claim by
re-reading its path, a computed claim by re-executing its stored code — so any
other model or human can verify it. Results prose is written from verified claims
only, with a two-layer check: deterministic re-execution first, a skeptical model
reconciliation as the labelled fallback.

This module is the production-shared core extracted from the ``tests/bench``
prototype. It is portal-free and endpoint-free: everything talks to three injected
collaborators —

  * ``DataSource``   — reads the summary datasets and navigates data-path antecedents,
  * ``LLMClient``    — an async OpenAI-compatible tool-calling client,
  * ``CodeExecutor`` — runs (and, at verify time, RE-runs) model-written analysis code.

The offline bench (``tests/bench/results_explorer.py``) constructs these with a
``DirDataSource`` over local viz JSON, a ``SubprocessExecutor``, and an
``LLMClient`` around a synchronous LM Studio / OpenRouter endpoint. The portal
route injects a squashfuse-backed ``DirDataSource``, a ``ContainerExecutor``
(``docker exec`` into the isolated session container), and ``resolve_llm``.

All prototype module globals (DATASETS / COUNTS / TAX_DF / SAMPLES_META /
COMPUTATIONS / LEDGER / AGENDA) collapse into ``Autoresearcher`` instance state so
concurrent submissions are isolated.
"""
from __future__ import annotations

import asyncio
import datetime as _datetime
import gzip
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

# ── Prompts (moved verbatim from the prototype) ───────────────────────────────
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

KNOW WHAT THE FIELDS MEAN, then think critically about the data:
- `meta['x']`/`meta['y']` are PRECOMPUTED ORDINATION coordinates, not geographic lat/lon.
- `meta['collection_date']` is often a database record-creation date, not a verified sampling
  date. Treat SRA metadata labels — including the stated amplicon target — as unverified: they
  are frequently wrong. get_dataset('study') gives today's `analysis_date`; judge any date
  against it (a recent past date is normal, not "future").
- A named test (Mantel, PERMANOVA, ...) must be the test you actually ran; don't upgrade a
  plain correlation to a named test.
- Sanity-check the data against the stated context in get_dataset('study'). Don't invent
  effects the data doesn't support; equally, don't accept a label the data contradicts —
  where they disagree, the contradiction is itself a grounded finding worth recording.

Keep going until the agenda (including follow-ups) is worked through, then reply DONE."""


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


WRITE_SYSTEM = """Scientific writing assistant for microbial ecology. Write a Results section
using ONLY the verified claims provided — every number must come from a claim. Do not add
findings not in the claims. Report quality caveats plainly and politely. Past tense,
objective, no interpretation. Synthesize into flowing prose (not a bullet list). Reference
Figure/Table where natural."""


# ── Tool schemas (moved verbatim from the prototype) ──────────────────────────
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


# ── Pure helpers (moved verbatim from the prototype) ──────────────────────────
def _norm_antecedents(x):
    """Antecedents as a clean list of tokens. Some models (e.g. Sonnet) return the
    array arg as a delimited STRING ('c2, c3; path') instead of a JSON list — split
    it rather than iterating it character-by-character."""
    if isinstance(x, list):
        items = x
    elif isinstance(x, str):
        items = re.split(r"[;,]", x)
    else:
        items = []
    return [t.strip() for t in items if t and t.strip()]


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


def _jsonify(v, depth=0):
    """Shrink an arbitrary computation result to a JSON-safe, size-capped form
    (lists→50, dict items→50, depth 4). numpy/pandas are handled lazily so this
    module imports cleanly even where they are absent (they always are wherever a
    real result is produced)."""
    if depth > 4:
        return str(v)
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        np = pd = None
    if np is not None and isinstance(v, (np.floating, np.integer)):
        return round(float(v), 4)
    if isinstance(v, float):
        return round(v, 4)
    if np is not None and isinstance(v, np.ndarray):
        return _jsonify(v.tolist(), depth + 1)
    if isinstance(v, dict):
        return {str(k): _jsonify(x, depth + 1) for k, x in list(v.items())[:50]}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x, depth + 1) for x in list(v)[:50]]
    if pd is not None and isinstance(v, (pd.Series, pd.DataFrame)):
        return _jsonify(v.to_dict(), depth + 1)
    return v


def build_dag(computations: dict, ledger: list) -> dict:
    """Claim→antecedent provenance DAG. Nodes: computations (blue), claims
    (verdict-coloured), and data paths (grey). Parameterised off the passed
    state (was ``build_dag()`` over module globals in the prototype)."""
    nodes, edges, seen = [], [], set()

    def add(nid, **kw):
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, **kw})

    for cid, comp in computations.items():
        add(cid, type="computation", label=comp["label"])
    for c in ledger:
        add(c["id"], type="claim", label=c["statement"][:60], verdict=c.get("verdict"), kind=c["kind"])
        for ant in c["antecedents"]:
            if ant in computations:
                edges.append({"from": ant, "to": c["id"]})
            else:
                add(ant, type="data", label=ant)
                edges.append({"from": ant, "to": c["id"]})
    return {"nodes": nodes, "edges": edges}


def dag_mermaid(dag):
    """Render a DAG dict as a Mermaid ```graph LR``` block (for the bench .md)."""
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


# ── Injection points ──────────────────────────────────────────────────────────
@runtime_checkable
class DataSource(Protocol):
    """Reads the summary datasets and navigates data-path antecedents. Pure file
    reads (kept synchronous); the same instance serves the agenda tools AND the
    deterministic re-read half of ``verify()``."""

    def read_json(self, name: str) -> Any | None:
        """Read a viz JSON payload by base name (``counts``/``taxonomy``/``samples``/
        ``provenance``/``renorm_stats``), trying ``.json`` then ``.json.gz``. None if absent."""
        ...

    def datasets(self) -> dict[str, Any]:
        """The named summary datasets the agent can ``get_dataset``/``list_datasets``."""
        ...

    def navigate(self, path: str) -> tuple[bool, Any]:
        """Resolve a dotted data-path antecedent against ``datasets()`` → (found, value)."""
        ...

    @property
    def study(self) -> dict:
        """Stated study/methods context (replaces the ``STUDY_GROUNDED`` global)."""
        ...


@runtime_checkable
class CodeExecutor(Protocol):
    """Runs model-written analysis code in an isolated sandbox and returns
    ``(ok, result_or_error)``. The SAME call re-runs the stored code at verify
    time, which is what makes computed claims deterministically re-derivable."""

    async def run(self, code: str, timeout: int = 30) -> tuple[bool, Any]:
        ...


@dataclass
class LLMClient:
    """Thin async wrapper over an OpenAI-compatible chat endpoint, carrying the
    default model. Tool-calling args (``tools``/``tool_choice``) pass straight
    through, so the same client drives the agenda loop and the reconciler.

    ``client`` must be an ``openai.AsyncOpenAI`` (or anything with the same
    ``.chat.completions.create`` async coroutine)."""

    client: Any
    model: str

    async def chat(self, messages, *, model: str | None = None, tools=None,
                   tool_choice=None, temperature: float = 0.25, max_tokens: int = 2500):
        """Return the raw completion (caller reads ``choices[0].message`` /
        ``tool_calls``). Only non-None optional args are forwarded so a plain
        completion and a tool-calling turn share one path."""
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return await self.client.chat.completions.create(**kwargs)


# ── Concrete DataSource ───────────────────────────────────────────────────────
_UNASSIGNED = {"", "na", "unclassified", "unassigned", "incertae sedis", "none"}


class DirDataSource:
    """A ``DataSource`` backed by a directory of viz JSON files. Used by BOTH the
    offline bench (over ``OMC_BENCH_DATA``) and the portal route (over the
    squashfuse ``/viz/data`` mount) — the server-side re-read half of ``verify()``
    reads the same bytes the in-container executor reads, preserving determinism.

    ``study`` is the stated study/methods context; the prototype's
    ``_build_datasets`` derived the ``study`` dataset from a ``STUDY_GROUNDED``
    global plus the overview fixture — here both are passed in explicitly.
    ``overview`` (the parse_microscape-shaped summary) is optional; when omitted a
    minimal one is computed from the same viz JSON so the agent's ``get_dataset``
    still returns a usable ``overview``/``taxonomy_summary``."""

    def __init__(self, data_dir: Path | str, study: dict | None = None,
                 overview: dict | None = None):
        self.data_dir = Path(data_dir)
        self._study_meta = study or {}
        self._overview = overview
        self._datasets: dict[str, Any] | None = None

    # -- file reads -------------------------------------------------------------
    def read_json(self, name: str) -> Any | None:
        """Read ``{name}.json`` then ``{name}.json.gz`` from the data dir; None if absent."""
        for p in (self.data_dir / f"{name}.json", self.data_dir / f"{name}.json.gz"):
            if p.exists():
                op = gzip.open if p.suffix == ".gz" else open
                with op(p, "rt") as f:
                    return json.load(f)
        return None

    # backwards-compatible private alias (prototype called this ``_rj``)
    _rj = read_json

    def _build_overview(self) -> dict:
        """Compute a parse_microscape-shaped summary from the viz JSON when the
        caller didn't supply one (mirrors the bench ``fixtures.build_pipeline_outputs``
        shape closely enough for the agent's ``overview``/``taxonomy_summary``)."""
        renorm = self.read_json("renorm_stats") or {}
        samples = self.read_json("samples") or []
        taxonomy = self.read_json("taxonomy") or {}
        provenance = self.read_json("provenance") or {}

        db_name, tax = next(iter(taxonomy.items()), ("none", {}))
        levels = tax.get("levels", [])
        assignments = tax.get("assignments", {})

        def _clean(v):
            return None if (v or "").strip().lower() in _UNASSIGNED else v

        classified_per_rank = {}
        for i, level in enumerate(levels):
            classified_per_rank[level.lower()] = sum(
                1 for a in assignments.values() if i < len(a) and _clean(a[i]) is not None)
        phylum_idx = levels.index("Phylum") if "Phylum" in levels else 1
        from collections import Counter
        phyla = Counter(
            v for a in assignments.values()
            if phylum_idx < len(a) and (v := _clean(a[phylum_idx])) is not None)
        top_phyla = dict(phyla.most_common(5))
        prok = renorm.get("prokaryote", {})
        return {
            "asv_summary": {
                "total_asvs": prok.get("n_asvs", len(assignments)),
                "n_samples": prok.get("n_samples", len(samples)),
                "samples": [s.get("id") for s in samples if isinstance(s, dict)],
            },
            "taxonomy_summary": {
                "database": db_name,
                "databases_used": [db_name],
                "total_asvs_classified": len(assignments),
                "classified_per_rank": classified_per_rank,
                "top_phyla": top_phyla,
                "phyla": top_phyla,
            },
        }

    def datasets(self) -> dict[str, Any]:
        """The named summary datasets (built once, cached). Ports the prototype's
        ``_build_datasets`` off injected ``study``/``overview`` instead of globals."""
        if self._datasets is not None:
            return self._datasets
        fx = self._overview if self._overview is not None else self._build_overview()
        prov = self.read_json("provenance") or {}
        samples = self.read_json("samples") or []
        sg = self._study_meta or {}
        self._datasets = {
            "overview": fx,
            # Stated study/methods context — the amplicon target here is the SRA metadata
            # LABEL, which is frequently wrong; verify it against the observed taxonomy.
            "study": {
                # Today's real date — so the agent judges temporal plausibility correctly
                # (a model with no clock can mistake a recent date for a future one).
                "analysis_date": _datetime.date.today().isoformat(),
                "stated_target_label": sg.get("title"),
                "study_name": sg.get("study_name"),
                "organism": sg.get("organism"),
                "platform": sg.get("platform"),
                "taxonomy_database": fx.get("taxonomy_summary", {}).get("database"),
                "pipeline_stages": [s.get("id") for s in prov.get("stages", [])],
                "caveat": ("stated_target_label is the SRA-metadata amplicon label and can be "
                           "wrong (18S runs are commonly mislabeled '16S'); confirm against tax Domain."),
            },
            "renorm_stats": self.read_json("renorm_stats") or fx.get("renorm", {}),
            "provenance": {"total": prov.get("total", {}),
                           "stages": [s.get("id") for s in prov.get("stages", [])],
                           "n_samples": len(prov.get("samples", {}))},
            "samples": {"n": len(samples),
                        "total_reads": sum(s.get("total_reads", 0) for s in samples if isinstance(s, dict))},
            "taxonomy_summary": fx.get("taxonomy_summary", {}),
        }
        return self._datasets

    def navigate(self, path: str) -> tuple[bool, Any]:
        """Resolve a dotted path (``a.b.0.c``) against ``datasets()``. Ports the
        prototype's ``_navigate``; used by both ``verify()`` and the DAG viewer."""
        cur: Any = self.datasets()
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return False, None
        return True, cur

    @property
    def study(self) -> dict:
        return self._study_meta


# ── DEV/offline executor: resource-limited subprocess ─────────────────────────
# Ported verbatim from the prototype ``_CHILD_RUNNER``. In production the agent's
# analysis runs inside the omc-session container (see portal/app/autoresearch_executor.py);
# THIS approximates that sandbox with a separate python process (CPU+memory rlimits,
# network cut after trusted imports). Data is read from EXPLORER_DATA_DIR inside the
# child, so the same code re-runs deterministically at verification time.
_SUBPROCESS_RUNNER = r'''
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


class SubprocessExecutor:
    """DEV/offline ``CodeExecutor``: runs model code in a resource-limited child
    process reading from ``data_dir``. This is the stand-in the isolated session
    container replaces in production (``ContainerExecutor``)."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)

    def _run_sync(self, code: str, timeout: int) -> tuple[bool, Any]:
        try:
            p = subprocess.run(
                [sys.executable, "-c", _SUBPROCESS_RUNNER], input=code, text=True,
                capture_output=True, timeout=timeout,
                env={**os.environ, "EXPLORER_DATA_DIR": str(self.data_dir),
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

    async def run(self, code: str, timeout: int = 30) -> tuple[bool, Any]:
        """Async facade over the blocking subprocess call (off the event loop)."""
        return await asyncio.to_thread(self._run_sync, code, timeout)


# ── Orchestrator ──────────────────────────────────────────────────────────────
class Autoresearcher:
    """Drives one claim-grounded autoresearch run over one submission's data.

    All state is per-instance (``computations``/``ledger``/``agenda``) so two runs
    never share globals. Construct with an injected ``DataSource`` (reads),
    ``LLMClient`` (tool-calling), and ``CodeExecutor`` (sandbox), then::

        completed = await ar.explore()
        await ar.verify()
        snap = ar.snapshot(resolve=True)
        prose = await ar.write_results()

    ``on_progress(event, detail)`` — if given — is awaited on each tool call and
    verification event so a route can stream SSE.
    """

    def __init__(self, data: DataSource, llm: LLMClient, executor: CodeExecutor, *,
                 explore_model: str | None = None, verify_model: str | None = None,
                 max_steps: int = 48, max_followups: int = 12,
                 reconcile: bool = True,
                 on_progress: Optional[Callable[[str, Any], Awaitable]] = None):
        self.data = data
        self.llm = llm
        self.executor = executor
        self.explore_model = explore_model or llm.model
        self.verify_model = verify_model or llm.model
        self.max_steps = max_steps
        self.max_followups = max_followups
        self.reconcile = reconcile
        self.on_progress = on_progress
        # per-run state (was module globals in the prototype)
        self.computations: dict[str, Any] = {}   # cid -> {label, code, result}
        self.ledger: list[dict] = []              # claim dicts
        self.agenda: list[dict] = []              # {id, question, rationale, status, parent}
        self.results_prose: str | None = None     # last write_results() output (for snapshot)

    # -- progress ---------------------------------------------------------------
    async def _emit(self, event: str, detail: Any) -> None:
        if self.on_progress is not None:
            await self.on_progress(event, detail)

    # -- agenda helpers ---------------------------------------------------------
    def _current_investigation(self) -> str | None:
        """The investigation being worked (first in-progress, else first pending)."""
        for st in ("in_progress", "pending"):
            for a in self.agenda:
                if a["status"] == st:
                    return a["id"]
        return None

    # -- tool dispatch ----------------------------------------------------------
    async def _exec_tool(self, name: str, args: dict) -> dict:
        """Execute one agent tool call against instance state (was ``_exec_tool``)."""
        if name == "propose_agenda":
            for it in args.get("items", [])[:20]:
                self.agenda.append({"id": f"a{len(self.agenda) + 1}", "question": it.get("question", ""),
                                    "rationale": it.get("rationale", ""), "status": "pending", "parent": None})
            if self.agenda:
                self.agenda[0]["status"] = "in_progress"
            return {"agenda": [{"id": a["id"], "question": a["question"], "status": a["status"]} for a in self.agenda]}
        if name == "get_agenda":
            return {"agenda": [{"id": a["id"], "question": a["question"], "status": a["status"],
                                "parent": a["parent"]} for a in self.agenda]}
        if name == "add_followup":
            # Cap follow-ups so a recursive model can't grow the agenda unboundedly.
            n_followups = sum(1 for a in self.agenda if a["parent"])
            if n_followups >= self.max_followups:
                return {"added": None, "note": f"follow-up cap reached ({self.max_followups}); "
                        "finish the current agenda instead of adding more."}
            parent = self._current_investigation()
            self.agenda.append({"id": f"a{len(self.agenda) + 1}", "question": args.get("question", ""),
                                "rationale": args.get("rationale", ""), "status": "pending", "parent": parent})
            return {"added": self.agenda[-1]["id"], "parent": parent,
                    "pending": sum(a["status"] == "pending" for a in self.agenda)}
        if name == "mark_done":
            cur = self._current_investigation()
            for a in self.agenda:
                if a["id"] == cur:
                    a["status"] = "done"
            nxt = self._current_investigation()
            for a in self.agenda:
                if a["id"] == nxt and a["status"] == "pending":
                    a["status"] = "in_progress"
            pend = sum(a["status"] in ("pending", "in_progress") for a in self.agenda)
            return {"done": cur, "now": nxt, "remaining": pend}
        if name == "list_datasets":
            return {"datasets": list(self.data.datasets()),
                    "note": "use run_analysis with `counts`/`tax`/`meta` for real tests"}
        if name == "get_dataset":
            d = self.data.datasets().get(args.get("name"))
            return d if d is not None else {"error": "unknown", "available": list(self.data.datasets())}
        if name == "run_analysis":
            ok, res = await self.executor.run(args.get("code", ""))
            if not ok:
                return {"ok": False, "error": res}
            cid = f"c{len(self.computations) + 1}"
            self.computations[cid] = {"label": args.get("label", cid),
                                      "code": args.get("code", ""), "result": res}
            return {"ok": True, "computation_id": cid, "result": res}
        if name == "record_claim":
            claim = {"id": f"k{len(self.ledger) + 1}", "statement": args.get("statement", ""),
                     "value": str(args.get("value", "")),
                     "antecedents": _norm_antecedents(args.get("antecedents")),
                     "kind": args.get("kind", "observation"),
                     "investigation": self._current_investigation()}
            self.ledger.append(claim)
            return {"recorded": True, "claim_id": claim["id"], "n_claims": len(self.ledger)}
        return {"error": f"unknown tool {name}"}

    # -- explore ----------------------------------------------------------------
    async def explore(self) -> bool:
        """Run the agenda-driven tool-calling loop. Returns True only when the
        agenda (including follow-ups) was actually worked through; on a step-cap
        exit the in-progress item is marked ``interrupted`` (never faked done)."""
        messages = [
            {"role": "system", "content": EXPLORE_SYSTEM},
            {"role": "user", "content": "Propose your agenda of microbial-ecology tests, "
             "then work through it, recursing where it gets interesting."}]
        for step in range(self.max_steps):
            r = await self.llm.chat(messages, model=self.explore_model, tools=TOOLS,
                                    tool_choice="auto", temperature=0.25, max_tokens=2500)
            msg = r.choices[0].message
            if not msg.tool_calls:
                content = _strip_think(msg.content or "")
                active = [a for a in self.agenda if a["status"] in ("pending", "in_progress")]
                if "DONE" in content.upper() and not active:
                    break
                if "DONE" in content.upper() and active:
                    messages.append({"role": "user",
                                     "content": f"{len(active)} agenda items remain — work the next one (get_agenda)."})
                    continue
                messages.append({"role": "user",
                                 "content": "Continue with the current investigation, or mark_done and take the next."})
                continue
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._exec_tool(tc.function.name, args)
                label = (args.get("label") or args.get("name") or args.get("question")
                         or (args.get("statement") or "")[:40])
                await self._emit(tc.function.name, {"step": step, "label": str(label)[:80],
                                                    "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)[:3500]})
        # Step cap hit with work outstanding: the current item was interrupted, and
        # pending items stay pending. Return True only when nothing is outstanding.
        for a in self.agenda:
            if a["status"] == "in_progress":
                a["status"] = "interrupted"
        return not any(a["status"] in ("pending", "in_progress", "interrupted") for a in self.agenda)

    # -- verify -----------------------------------------------------------------
    def _evidence_for(self, claim, comp_cache: dict) -> str:
        """Deterministic evidence block for a claim: re-executed computation results
        (from ``comp_cache``) and re-read data values. This is what the reconciler
        judges against — not memory."""
        parts = []
        for ant in claim["antecedents"]:
            if ant in self.computations:
                good, res = comp_cache.get(ant, (False, None))
                parts.append(f"[{ant}] {self.computations[ant]['label']} (re-executed) = "
                             + (json.dumps(res, default=str)[:600] if good else "ERROR"))
            else:
                found, val = self.data.navigate(ant)
                parts.append(f"[{ant}] = {json.dumps(val, default=str)[:300] if found else 'NOT FOUND'}")
        return "\n".join(parts)

    async def _reconcile_claim(self, claim, comp_cache: dict) -> dict:
        """Model adjudication of a claim against its re-executed evidence. Only
        invoked on a deterministic miss. Returns ``{verdict, reasoning}``."""
        user = (f"CLAIM: {claim['statement']}\nCLAIMED VALUE: {claim['value']}\n\n"
                f"INDEPENDENT EVIDENCE (re-executed from raw data):\n{self._evidence_for(claim, comp_cache)}")
        resp = await self.llm.chat(
            [{"role": "system", "content": RECONCILE_SYSTEM}, {"role": "user", "content": user}],
            model=self.verify_model, max_tokens=4000, temperature=0.0)
        text = _strip_think(resp.choices[0].message.content or "")
        m = re.search(r"VERDICT:\s*(SUPPORTED|PARTIAL|UNSUPPORTED)", text.upper())
        return {"verdict": m.group(1).lower() if m else "unsupported", "reasoning": text.strip()[:400]}

    async def verify(self) -> None:
        """Re-derive each claim from its antecedents (data re-read; computations
        re-EXECUTED via the same executor). Deterministic first; a true claim is
        never marked refuted for a representation mismatch. When ``reconcile`` is on,
        a deterministic MISS escalates to a skeptical model reconciliation against
        the SAME re-executed evidence — labelled so judgment-backed claims are visible.

        Each distinct computation is re-executed AT MOST ONCE per ``verify()`` call
        (cached) — the deterministic pass and any reconciliation share the result,
        which also bounds ``docker exec`` load in the container backend."""
        comp_cache: dict[str, tuple[bool, Any]] = {}

        async def _rerun(cid: str) -> tuple[bool, Any]:
            if cid not in comp_cache:
                comp_cache[cid] = await self.executor.run(self.computations[cid]["code"])
            return comp_cache[cid]

        for c in self.ledger:
            c["antecedents"] = _norm_antecedents(c["antecedents"])  # tolerate string-form ledgers
            claim_nums = _nums(c["value"])
            candidates: list[float] = []
            strvals: list[str] = []
            checked: list[str] = []
            for ant in c["antecedents"]:
                if ant in self.computations:
                    good, res = await _rerun(ant)  # re-execute stored code (cached)
                    if good:
                        _flatten_numbers(res, candidates)
                    checked.append(f"{ant}:{'run' if good else 'err'}")
                else:  # data path
                    found, actual = self.data.navigate(ant)
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
            # the SAME re-executed evidence (labelled, so judgment-backed claims are visible).
            if c["verdict"] != "verified" and self.reconcile and have_evidence:
                rec = await self._reconcile_claim(c, comp_cache)
                c["reconcile"] = rec
                if rec["verdict"] == "supported":
                    c["verdict"], c["method"] = "verified", "reconciled"

    # -- DAG --------------------------------------------------------------------
    def build_dag(self) -> dict:
        """The claim→antecedent provenance DAG for this run's current state."""
        return build_dag(self.computations, self.ledger)

    # -- write ------------------------------------------------------------------
    async def write_results(self, verified: list[dict] | None = None) -> str:
        """Write the Results prose from VERIFIED claims only (WRITE_SYSTEM). When
        ``verified`` is omitted, uses this run's verified claims. The result is
        cached on ``self.results_prose`` so a subsequent ``snapshot()`` includes it."""
        if verified is None:
            verified = [c for c in self.ledger if c.get("verdict") == "verified"]
        claims_txt = "\n".join(
            f"- [{c['kind']}] {c['statement']} (value={c['value']})" for c in verified)
        sg = self.data.study or {}
        study = (f"Study: {sg.get('title', '(unknown)')} — {sg.get('study_name', '')} "
                 f"({sg.get('bioproject', '')}), {sg.get('platform', '')}.")
        msgs = [{"role": "system", "content": WRITE_SYSTEM},
                {"role": "user", "content": f"{study}\n\nVERIFIED CLAIMS:\n{claims_txt}\n\n"
                 "Write the Results section."}]
        content = ""
        for mt in (6000, 12000):
            r = await self.llm.chat(msgs, model=self.explore_model, temperature=0.4, max_tokens=mt)
            content = _strip_think(r.choices[0].message.content or "")
            if content.strip():
                break
        self.results_prose = content
        return content

    # -- run summary ------------------------------------------------------------
    def run_summary(self, completed: bool | None = None) -> dict:
        """The ``run`` block (completed + investigation counts) for the snapshot/ledger."""
        done = sum(a["status"] == "done" for a in self.agenda)
        if completed is None:
            completed = bool(self.agenda) and all(a["status"] == "done" for a in self.agenda)
        return {"completed": completed, "investigations_done": done,
                "investigations_total": len(self.agenda)}

    # -- snapshot ---------------------------------------------------------------
    def snapshot(self, resolve: bool = True, completed: bool | None = None,
                 results_prose: str | None = None) -> dict:
        """One self-contained snapshot of this run: claims, computations, agenda,
        DAG, run summary, and (optionally) the Results prose. With ``resolve=True``
        every antecedent is pre-baked to ``{ant, code?, result?, value?}`` so the
        provenance viewer template never re-reads data (mirrors the bench
        ``build_dag_artifact.resolve()`` step). Schema is the frozen contract shared
        with the portal route + provenance template. ``results_prose`` defaults to the
        last ``write_results()`` output (call it before ``snapshot`` to include prose)."""
        if results_prose is None:
            results_prose = self.results_prose
        def _resolve(ant: str) -> dict:
            if ant in self.computations:
                c = self.computations[ant]
                return {"ant": ant, "code": c["code"],
                        "result": json.dumps(c["result"], default=str)[:1200]}
            found, val = self.data.navigate(ant)
            return {"ant": ant, "value": (json.dumps(val, default=str)[:300] if found else "—")}

        claims = []
        for c in self.ledger:
            entry = {
                "id": c["id"], "statement": c["statement"], "value": c["value"],
                "verdict": c.get("verdict", "unverifiable"), "kind": c.get("kind", "observation"),
                "method": c.get("method"), "antecedents": c["antecedents"],
            }
            if c.get("reconcile"):
                entry["reconcile"] = c["reconcile"]
            if resolve:
                entry["resolved"] = [_resolve(a) for a in c["antecedents"]]
            claims.append(entry)
        return {
            "claims": claims,
            "computations": self.computations,
            "agenda": self.agenda,
            "dag": self.build_dag(),
            "results_prose": results_prose,
            "run": self.run_summary(completed),
        }

    @classmethod
    def from_snapshot(cls, snap: dict, data: DataSource, llm: LLMClient,
                      executor: CodeExecutor, **kw) -> "Autoresearcher":
        """Reconstruct a researcher from a saved snapshot / ledger dict (keys
        ``claims``/``computations``/``agenda``) for re-verification. Accepts both a
        full ``snapshot()`` and the bench ``claims_ledger.json`` shape."""
        ar = cls(data, llm, executor, **kw)
        ar.ledger = list(snap.get("claims", []))
        ar.computations = dict(snap.get("computations", {}))
        ar.agenda = list(snap.get("agenda", []))
        return ar


# ── local think-stripper (avoids importing ai.llm_client's sync-only helpers) ─
_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)


def _strip_think(text: str) -> str:
    """Strip ``<think>...</think>`` reasoning blocks (closed or truncated) from
    model output. Mirrors ``ai.llm_client._strip_think`` but kept local so this
    core has no dependency on the sync client module."""
    if not text:
        return ""
    return _THINK_OPEN_RE.sub("", _THINK_CLOSED_RE.sub("", text)).strip()
