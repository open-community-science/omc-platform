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
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

# ── Prompts (moved verbatim from the prototype) ───────────────────────────────
_ANALYST_ROLE = """You are a curious microbial ecologist REANALYZING an existing 16S/18S
amplicon dataset as an independent scientist. Your job is not to restate summary statistics —
it is to TEST HYPOTHESES and find PATTERNS, RELATIONSHIPS, and ANOMALIES a scientist would care
about, then ground each in a re-runnable computation. Report what the data show; do not presume
the original collectors' intent, study design, or prior hypotheses — every claim is grounded in
the data itself, not a presumed backstory."""


# How the linear explore() works its way through the agenda. A hyphal tip gets a
# different workflow (it is handed ONE item and never sees the rest), but the same
# role, the same claim craft, and the same statistical rules — so those are shared.
_LINEAR_WORKFLOW = """Work systematically and recursively:
1. FIRST look at what is actually here — the frames, their columns, what varies across
   samples — and then call propose_agenda with the questions THIS dataset can answer.
   You are a microbial ecologist; the standard toolkit is yours to draw on where it fits,
   but an agenda that could have been written before seeing the data is not an agenda.
2. Work the agenda item by item. For each: run_analysis over `counts`/`tax`/`meta` to test
   it, then record_claim(s) with the exact value(s) and antecedents. mark_done and move on.
3. RECURSE: whenever a result is surprising or opens a question, add_followup — that is how
   you go deeper (a cluster in ordination → its driver taxa → are they contamination?)."""


_CLAIM_CRAFT = """WRITE CLAIMS THAT CAN BE REPRODUCED. Every claim you record will be handed to an
   independent analyst who gets the raw data and your claim — but NOT your code — and must
   re-derive it. A vague claim is one you lose.
   Break each claim into `assertions`: one entry per number you are asserting, each with a
   `label` saying what it IS and an `of` saying which subset it applies to. Each assertion is
   checked SEPARATELY, so a claim whose main finding holds keeps its credit even if one
   secondary number is disputed — but only if you separated them. Bundling several findings
   into one assertion means they stand or fall together, which is your loss, not the checker's.
   Put your knobs in `parameters` (thresholds, permutation counts, group sizes, how many
   genera you screened). Nobody re-derives those, and keeping them out of `assertions` stops
   them being mistaken for findings.
   DEFINE AMBIGUOUS TERMS in the label or `of`: "doubletons" meant three different things to
   three analysts once, and the claim lost the one number nobody could agree how to compute.
   A good call looks like:
     record_claim(
       statement="PERMANOVA separates habitat types within each domain batch",
       assertions=[{"label":"bacteria_F","value":"14.61","of":"bacteria batch, 6 habitat groups"},
                   {"label":"bacteria_p","value":"0.001","of":"bacteria batch"},
                   {"label":"eukaryote_F","value":"10.52","of":"eukaryote batch, 4 groups"}],
       parameters={"permutations":999, "n_bacteria":44, "n_eukaryote":18},
       antecedents=["c12"], kind="pattern")
   Note what is NOT an assertion: the permutation count and the group sizes. Those are how
   you ran it, not what you found.
   Prefer claims of kind "pattern" or "anomaly" (an insight) over "observation" (a restated
   number). Be honest: record quality_caveat for depth bias, low evenness, contamination,
   or anything that undermines a result. Never claim a number you did not compute. Phrase
   caveats and anomalies collegially and matter-of-factly — a mislabel, mix-up, or
   contamination is a routine good-faith observation to note neutrally (likely an honest
   accident), not a failing to flag with alarm or suspicion.
SURFACE YOUR ASSUMPTIONS. Real analysis always rests on things you can't confirm — an
   unstated normalization, an inferred grouping, an ambiguous field's meaning, the stated
   amplicon target/primers, a database version. Every time you proceed past one, call
   record_assumption right then (not silently). Assumptions are not claims; they make explicit
   what your findings depend on. Expect to record several across a run — if you've recorded
   none, you haven't looked hard enough.

THREE PROPERTIES OF THIS DATA TYPE DECIDE WHETHER AN ANALYSIS IS VALID. Getting these
wrong produces confident numbers that a reviewer will reject, so handle them explicitly:
- COMPOSITIONAL. Counts carry only relative information, so proportions are not independent:
  when one dominant taxon swings between samples, every other taxon's proportion moves with
  it, manufacturing "co-occurring guilds" and "mutual exclusion" out of nothing. So a
  correlation between relative abundances is PROVISIONAL. Before reporting one, check whether
  it survives a control — is it driven by a dominant taxon's swing, or by depth? `clr(...)`
  is one available control, though it is no cure-all (it is sensitive to the pseudocount and
  behaves badly when few taxa are involved), so treat agreement between approaches as the
  evidence, not any single transform. Say which controls you ran, and record a quality_caveat
  when a co-occurrence pattern rests on raw proportions alone.
- MULTIPLE-TESTED. A per-taxon test across taxa, or a sweep of pairwise correlations, is a
  FAMILY of tests: run `fdr(pvals)` and report adjusted p-values and how many tests were in
  the family. An uncorrected "significant" result from a sweep is not a finding.
- DEPTH-CONFOUNDED. Richness and detection rise with sequencing depth. Before comparing
  richness or presence/absence across samples, either `rarefy(...)` to a common depth or
  test the depth-vs-metric relationship and report it as a caveat.
When you cannot satisfy one of these (too few samples to rarefy, a test with no p-value
family), say so in the claim or a quality_caveat rather than proceeding silently.

KNOW WHAT THE FIELDS MEAN, then think critically about the data:
- EVERY `meta` COLUMN IS PREFIXED WITH WHERE IT CAME FROM: `pipeline_` was computed from
  this data, `sra_` is what the submitter declared to the sequence archive, `biosample_`
  describes the physical sample. Where two sources carry the same quantity you get both,
  and they are not always the same measurement — `pipeline_total_reads` is the depth of the
  table in scope, while `sra_read_count` is a separately declared figure.
- `meta['pipeline_x']`/`meta['pipeline_y']` are PRECOMPUTED ORDINATION coordinates, not
  geographic lat/lon. Real coordinates, if any, arrive as `biosample_lat_lon`.
- Dates can mean different things: an archive record carries the date it was created as
  well as the date a sample was taken. get_dataset('study') gives today's `analysis_date`;
  judge any date against it (a recent past date is normal, not "future").
- Submitted metadata records what the study reported and is usually sound, though often
  incomplete. It has not been checked against these reads, so it is evidence rather than
  ground truth, and the same holds of the data: neither settles the other by default.
- A named test (Mantel, PERMANOVA, ...) must be the test you actually ran; don't upgrade a
  plain correlation to a named test.
- Read the data against the stated context in get_dataset('study'). Don't invent effects
  the data doesn't support; where the two genuinely disagree, that disagreement is itself
  a grounded finding worth recording."""


EXPLORE_SYSTEM = f"""{_ANALYST_ROLE}

{_LINEAR_WORKFLOW}

{_CLAIM_CRAFT}

Keep going until the agenda (including follow-ups) is worked through AND you have recorded the
assumptions your findings rest on, then reply DONE."""


# ── Hyphal growth (#58) ───────────────────────────────────────────────────────
# Three short contexts instead of one long-lived session. Nothing here needs the
# transcript to persist: the agenda, ledger and assumptions live on the researcher,
# so each phase is seeded from that state and discarded when it finishes.

GERMINATE_SYSTEM = f"""{_ANALYST_ROLE}

Right now you are PLANNING ONLY.

LOOK BEFORE YOU PLAN. Read what is in front of you — the frames and their shapes, which
columns vary across samples and how, what the pipeline did and what it lost, what the
submitters said this was. list_datasets and get_dataset are available. An agenda you could
have written without seeing this dataset is a list of techniques, not a plan.

Then propose the questions THIS data can answer. You are a microbial ecologist and the
standard toolkit is yours to draw on wherever it fits — but let what you just read decide
which parts of it are worth running here, and what else is worth asking that no toolkit
would have suggested. The findings that matter are usually the ones nobody thought to
look for.

Each item will be worked SEPARATELY by an analyst who is given that item and the findings so
far, but not the other items — so each question must stand on its own, name what it is
testing precisely enough to act on, and not split one test across two items.

Call propose_agenda once. Do not start analysing."""


TIP_SYSTEM = f"""{_ANALYST_ROLE}

You have been handed ONE investigation from a larger agenda. Work it, record what you find,
and stop. Other analysts are working the other items; you will not see them and they will not
see your reasoning — only the claims you record. That is why the claims must stand alone.

1. Test the question you were given with run_analysis over `counts`/`tax`/`meta`.
2. record_claim for each finding, with exact values and antecedents.
3. If a result is surprising or opens a deeper question, add_followup — a fresh analyst will
   be given that question, seeded with your claims. Add it; do not chase it yourself.
4. Call mark_done when this investigation is finished, then reply DONE.

Stay on your own question. Findings from the other investigations are given to you as context
so you can build on them and avoid repeating them — not as work to redo.

{_CLAIM_CRAFT}"""


SWEEP_SYSTEM = f"""{_ANALYST_ROLE}

The investigation is finished. You are doing the final pass over the WHOLE body of findings,
which several analysts produced separately. Your only job now is to surface the assumptions
those findings rest on.

Look over every claim and call record_assumption for EACH thing that had to be taken for
granted but could not be confirmed from the data or the stated context. Every real analysis
makes some — whether counts are raw or normalized, what an ambiguous field means, an inferred
grouping, the stated amplicon target/primers, a database version, or that a named test's
assumptions held. Assumptions already on record are listed for you; do NOT re-record those.

You are also the only reader who sees all the findings at once, so note any assumption that is
only visible from that vantage — two investigations depending on incompatible readings of the
same field, for instance.

Record each one, then reply DONE. Do not record claims and do not run analyses."""


JUDGE_SYSTEM = """You are a strict verification auditor. You are given a CLAIM broken into
separately checkable ASSERTIONS, the PARAMETERS its analysis used, and INDEPENDENT EVIDENCE
re-executed from the raw data with the analyst's own labels intact.

Grade EVERY assertion on its own. They stand or fall separately — a claim whose main finding
holds keeps its credit even if one secondary number is contradicted.

Read the LABELS, not just the numbers. `{"bacteria_F": 14.61}` backs an assertion labelled
bacteria_F; a bare 14.61 under some unrelated label does not.

- PARAMETERS are never graded. Permutation counts, thresholds, group sizes, how many things
  were screened — these describe how the analysis was done, not what it found.
- SUPPORTED: the evidence gives this value (allow rounding, fraction vs percent, and simple
  derivations — 0.0002 and 0.0003 for the same p-value is rounding, not a contradiction).
- CONTRADICTED: the evidence gives a DIFFERENT value for this same quantity.
- NOT_ADDRESSED: the evidence is silent on it. Silence is neither support nor contradiction.
- IGNORE identifiers (SRR38966955, ASV_000123, PC1) — they are names, not quantities.

Reply with one line per assertion, then nothing else:
ASSERTION <label>: SUPPORTED|CONTRADICTED|NOT_ADDRESSED — <evidence value, or why>"""


REPLICATE_JUDGE_SYSTEM = """You are auditing a CLEAN-ROOM REPLICATION. A second analyst was
given the claim and the raw data — never the original code — and wrote its own analysis. You
see the claim's ASSERTIONS and that analyst's labelled result.

Grade EVERY assertion separately: did this independent derivation reproduce it?

- AGREES: the analyst's result gives this value (allow rounding, fraction vs percent, simple
  derivations — 0.0002 vs 0.0003 for a p-value is rounding, not disagreement).
- DIFFERS: the analyst computed a DIFFERENT value for this same quantity. Say what it got.
- NOT_ADDRESSED: the analyst did not compute this. An independent analyst reports its own
  findings and has no reason to restate every number, so silence is NOT disagreement.
- PARAMETERS are never graded.

Reply with one line per assertion, then nothing else:
ASSERTION <label>: AGREES|DIFFERS|NOT_ADDRESSED — <what the analyst got>"""


REPLICATE_SYSTEM = """You are an independent analyst performing a CLEAN-ROOM REPLICATION.

You are given a CLAIM about an amplicon dataset and direct access to the raw data. You have
NOT been shown the code that produced the claim, its intermediate results, or the approach
taken — that is deliberate. Re-derive the claim's quantitative content YOUR OWN WAY. Two
independent derivations that agree is evidence; re-running one derivation twice is not.

Data in scope (identical to what the original analyst had):
- `counts` — samples x ASV read-count DataFrame
- `tax`    — ASV x rank taxonomy (Domain..Genus); join to counts to work at taxon level
- `meta`   — per-sample metadata (library_name, collection_date, precomputed ordination x/y)
Helpers: np, pd, pdist, squareform, braycurtis, entropy, pearsonr, spearmanr, kruskal,
mannwhitneyu, PCA, fdr(pvals), clr(df), rarefy(df, depth, seed).

Reply with ONE fenced python block that assigns `result` — a SMALL dict of just the key
quantities needed to judge the claim (not a data dump; a big result makes agreement
meaningless). Then, after the block, one line:
SUPPORTS: YES|NO|INCONCLUSIVE
and one sentence on what you found.

Do not reverse-engineer what the original analyst probably did — solve it directly from the
data. If the claim is too vague to test quantitatively, say INCONCLUSIVE and explain why."""


WRITE_SYSTEM = """Scientific writing assistant for microbial ecology. Write a Results section
using ONLY the claims provided — every number must come from a claim. Do not add findings not
in the claims. Report quality caveats plainly and politely. Past tense, objective, no
interpretation. Synthesize into flowing prose (not a bullet list). Reference Figure/Table
where natural.

Some claims are marked PARTIALLY SUPPORTED: verification backed the finding but could not back
the specific values listed as unsupported. You may report the finding — it is real — but you
MUST NOT state those particular numbers. Give the qualitative result instead, or use only the
values that were backed. Never repeat a number listed as unsupported.

You may also be given ASSUMPTIONS the analysis had to make and could not confirm. These are
not findings and must not be reported as results, but where a stated finding depends on one,
say so plainly in the same breath — "assuming counts are raw rather than rarefied, ..." — so
a reader can see what rests on what. Do not collect them into a disclaimer paragraph, and do
not apologise for them; an honest analysis has some."""


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
                        "mannwhitneyu, PCA — plus `fdr(pvals)` (Benjamini-Hochberg, for any family of "
                        "tests), `clr(df)` (centred log-ratio, for correlating taxa), and "
                        "`rarefy(df, depth, seed)` (common-depth subsampling, seeded). "
                        "Code MUST assign to `result`. Compute before you claim."),
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
            "assertions": {"type": "array", "description": (
                "One entry per number you are asserting. Each is checked SEPARATELY, so a "
                "claim keeps credit for the findings that hold even if one value is "
                "disputed. Example: [{\"label\": \"bacteria_F\", \"value\": \"14.61\", "
                "\"of\": \"bacteria batch, 6 habitat groups\"}, {\"label\": \"bacteria_p\", "
                "\"value\": \"0.001\"}]"),
                "items": {"type": "object", "properties": {
                    "label": {"type": "string", "description": "what the number IS, e.g. 'bacteria_F'"},
                    "value": {"type": "string", "description": "the value, e.g. '14.61'"},
                    "of": {"type": "string", "description": "which subset/grouping, e.g. 'bacteria batch'"}},
                    "required": ["label", "value"]}},
            "parameters": {"type": "object", "description": (
                "The knobs your analysis used — thresholds, permutation counts, group sizes, "
                "how many things you screened. CONTEXT, never graded. Putting them here is "
                "what stops them being mistaken for findings. "
                "Example: {\"permutations\": 999, \"prevalence_threshold\": 0.5}")},
            "antecedents": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["observation", "pattern", "anomaly", "quality_caveat"]}},
            "required": ["statement", "assertions", "antecedents"]}}},
    {"type": "function", "function": {
        "name": "request_package",
        "description": ("When an analysis needs a library that is not in scope, record the "
                        "request instead of working around it silently. The sandbox will not "
                        "gain it during this run — say what you did instead, or leave the "
                        "question open — but the request is kept, so a package that analysts "
                        "keep reaching for can be added."),
        "parameters": {"type": "object", "properties": {
            "package": {"type": "string", "description": "importable name, e.g. 'skbio'"},
            "why": {"type": "string", "description": "the analysis it would let you run, and what you can or cannot do without it"}},
            "required": ["package"]}}},
    {"type": "function", "function": {
        "name": "record_assumption",
        "description": ("When you must proceed despite something you CANNOT confirm from the data or the "
                        "stated context, record the assumption. This is not a verifiable claim — it "
                        "surfaces honest uncertainty where your judgment filled a gap (e.g. an unstated "
                        "normalization, an inferred sample grouping, an ambiguous field's meaning), so a "
                        "reader knows what rests on it. Record one whenever you'd otherwise silently assume."),
        "parameters": {"type": "object", "properties": {
            "statement": {"type": "string", "description": "the assumption, e.g. 'Assuming counts are raw reads (not rarefied), as no renorm_stats is present'"},
            "why": {"type": "string", "description": "what you could not confirm, and why the assumption was needed"},
            "impact": {"type": "string", "description": "how the findings would change if it is wrong (optional)"}},
            "required": ["statement"]}}},
]


def tools_for(*names: str) -> list[dict]:
    """The subset of TOOLS with these names, in TOOLS order.

    Hyphal growth (#58) runs three different kinds of short context and each needs a
    different surface: germination proposes the agenda and nothing else, a tip works
    one item, the sweep only records assumptions. Offering a tool the phase must not
    use is an invitation to use it — germination given `run_analysis` starts the
    investigation it was supposed to be planning."""
    want = set(names)
    return [t for t in TOOLS if t["function"]["name"] in want]


GERMINATE_TOOLS = tools_for("list_datasets", "get_dataset", "propose_agenda")
TIP_TOOLS = tools_for("get_agenda", "add_followup", "mark_done", "get_dataset",
                      "list_datasets", "run_analysis", "record_claim", "record_assumption",
                      "request_package")
SWEEP_TOOLS = tools_for("get_agenda", "record_assumption")


# Read the real shape of the data instead of describing it in prose. An analyst
# told "counts is a samples x ASV DataFrame" still transposed it and graded two
# correct claims as overturned; shapes plus an explicit axis rule are much harder
# to misread than an English sentence (#50).
_BRIEFING_CODE = """
_b = {}
if counts is not None and getattr(counts, "size", 0):
    _b["counts"] = {"shape": list(counts.shape),
                    "sample_ids_sample": [str(x) for x in list(counts.index[:3])],
                    "asv_ids_sample": [str(x) for x in list(counts.columns[:3])],
                    "has_props": props is not None}
if tax is not None and getattr(tax, "size", 0):
    _b["tax"] = {"shape": list(tax.shape), "columns": [str(c) for c in tax.columns]}
if meta is not None and getattr(meta, "size", 0):
    _b["meta"] = {"shape": list(meta.shape), "columns": [str(c) for c in meta.columns]}
    # Columns that could actually GROUP samples. A column with one value across every
    # sample describes the study; one with a distinct value per sample is an
    # identifier. What is left is the design, and an agent that is not shown it
    # invents a grouping (high vs low sequencing depth) for want of a real one.
    _g = {}
    for _c in meta.columns:
        try:
            _vc = meta[_c].astype(str).value_counts()
        except Exception:
            continue
        # A column with a level for every few samples is an identifier or a
        # measurement, not a grouping — 44 library names across 63 samples groups
        # nothing. Cap the arity so what is listed is actually usable as a factor.
        if 2 <= len(_vc) <= max(2, min(12, len(meta) // 4)):
            _g[str(_c)] = {"n_groups": int(len(_vc)),
                           "groups": {str(k): int(v) for k, v in _vc.head(8).items()}}
    _rank = {"biosample_": 0, "pipeline_": 1, "sra_": 2}
    def _order(_kv):
        _p = 1
        for _k, _v in _rank.items():
            if _kv[0].startswith(_k):
                _p = _v
        return (_p, _kv[1]["n_groups"])
    if _g:
        _b["meta_groupings"] = dict(sorted(_g.items(), key=_order)[:12])
    # Columns that came from the survivors' table have NO value for the dropped
    # samples. Comparing dropped against retained on one of those yields an empty
    # cell rather than an answer, and the column that names the submission is one
    # of them — precisely the comparison an analyst reaches for.
    if "pipeline_in_counts" in meta.columns:
        _out = meta[~meta["pipeline_in_counts"].astype(bool)]
        if len(_out):
            _blank = sorted(str(c) for c in meta.columns
                            if _out[c].isna().all() and meta[c].notna().any())
            if _blank:
                _b["blank_for_dropped"] = _blank[:16]
def _describe(_k):
    _v = globals().get(_k)
    _sh = getattr(_v, "shape", None)
    if isinstance(_sh, tuple) and len(_sh) == 2:
        return "%s (DataFrame %dx%d)" % (_k, _sh[0], _sh[1])
    return _k
_b["available"] = sorted(
    _describe(k) for k in list(globals())
    if not k.startswith("_") and k not in ("result", "sys", "json", "os"))
result = _b
"""


def format_briefing(b: dict) -> str:
    """Render the probe result as the orientation block an analyst gets. The axis
    rule is the load-bearing line: shape alone still leaves which-way-round open."""
    if not b:
        return ""
    lines = ["DATA IN SCOPE — read from THIS dataset just now, not assumed:"]
    c = b.get("counts")
    if c:
        rows, cols = c["shape"]
        lines += [
            f"  counts: {rows} rows x {cols} columns.",
            f"    ROWS ARE SAMPLES ({', '.join(c.get('sample_ids_sample', []))} ...) — {rows} of them.",
            f"    COLUMNS ARE ASVs ({', '.join(c.get('asv_ids_sample', []))} ...) — {cols} of them.",
            "    So a PER-ASV statistic reduces over axis=0, and a PER-SAMPLE statistic",
            "    reduces over axis=1. Check your orientation before you trust a number:",
            f"    anything claiming there are {rows} ASVs or {cols} samples has them backwards.",
        ]
        if c.get("has_props"):
            lines.append("    `counts` holds RAW READ COUNTS. `props` holds the same table "
                         "as each ASV's share of its own sample (rows sum to 1).")
    if (p := b.get("provenance")) and p.get("n_dropped"):
        # Stated up front and beside the axis rule, because an analyst that meets the
        # attempted-sample count later, unexplained, concludes its own frame is wrong.
        at = ", ".join(f"{n} at {s}" for s, n in (p.get("dropped_at_stage") or {}).items())
        lines += [
            f"  SAMPLE ATTRITION: {p['n_samples_analysed']} of {p['n_samples_attempted']} "
            f"samples reached the final table.",
            f"    The other {p['n_dropped']} produced zero reads ({at}) and are ABSENT "
            f"from counts — not present as zero rows.",
            f"    meta has all {p['n_samples_attempted']} rows; counts and props have "
            f"the {p['n_samples_analysed']} with reads. meta.loc[counts.index] lines "
            f"them up.",
        ]
        if blank := b.get("blank_for_dropped"):
            lines.append(f"    These meta columns have no value for any dropped sample, "
                         f"because they come from the analysed table: {', '.join(blank)}.")
    if g := b.get("grantable"):
        # Say what they are FOR. An analyst installed scikit-bio, spent three turns
        # guessing at its API, re-requested it, and never used the `permanova` already
        # bound in its namespace — which had given it the right answer twice.
        lines.append("  request_package will install any of these into the sandbox for "
                     f"you: {', '.join(g)}. Nothing else is available. Ask only for what "
                     "the names above do not already cover — the helpers listed there "
                     "are ready to call and need no import.")
    if av := b.get("available"):
        # Named because it was being discovered by failing into it — a tip lost a step
        # to `import skbio` and had no way to know what was in scope.
        # "importable" was read as a list of things to import, and analyses went looking
        # for counts.csv on disk. These are ALREADY BOUND — say so, and say it first.
        lines.append("  run_analysis runs with these ALREADY LOADED — do not read files "
                     "and do not import them; assign to `result` when you are done:")
        lines.append(f"    {', '.join(av)}")
        # These three exist nowhere else, so their signatures cannot be guessed and
        # were being guessed — fdr() compared against a threshold as though it
        # returned the adjusted p-values.
        lines += [
            "    fdr(pvals) -> {'p_adj': [...], 'reject': [...], 'n_sig': int, "
            "'n_tests': int, 'alpha': float}",
            "    clr(df, pseudocount=0.5) -> DataFrame, same axes",
            "    rarefy(df, depth=None, seed=0) -> (DataFrame, [ids dropped below depth])",
            "    permanova(counts_or_distances, groups, permutations=999) -> "
            "{'F', 'R2', 'p', 'n', 'groups', 'group_sizes'}",
            "    alpha_diversity(counts) -> DataFrame[richness, shannon_nats, "
            "shannon_bits, evenness, depth] — the unit is in the column name",
            # Shown, not prohibited. "Do not read files" has now failed in four
            # separate contexts, and a claim-sized context cannot inherit the
            # correction its predecessor was given — each one starts naive, so the
            # cost is paid once per context rather than once per run. One line of
            # working code is the cheapest thing that has a chance of landing.
            "    a complete analysis looks like:",
            "      sub = counts.loc[meta.index[meta['pipeline_in_counts']]]",
            "      result = {'n_samples': int(sub.shape[0]), "
            "'mean_richness': float((sub > 0).sum(axis=1).mean())}",
        ]
    for key in ("tax", "meta"):
        d = b.get(key)
        if d:
            lines.append(f"  {key}: {d['shape'][0]} rows x {d['shape'][1]} columns "
                         f"[{', '.join(d.get('columns', [])[:12])}]")
    if g := b.get("meta_groupings"):
        # Named explicitly for the same reason as the axis rule: an analyst that is not
        # shown the real grouping variable invents one (high vs low sequencing depth),
        # and a claim about an invented grouping is reproducible and pointless.
        lines.append("  every meta column is prefixed with its SOURCE — pipeline_ "
                     "(computed from this data), sra_ (declared to the archive), "
                     "biosample_ (the physical sample). Where two sources carry the "
                     "same quantity you get both, and they can disagree.")
        lines.append("  COLUMNS THAT GROUP THE SAMPLES:")
        for col, info in g.items():
            shown = ", ".join(f"{k} (n={v})" for k, v in info["groups"].items())
            more = "" if info["n_groups"] <= len(info["groups"]) else ", ..."
            lines.append(f"    meta['{col}']: {info['n_groups']} groups — {shown}{more}")
    return "\n".join(lines)


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


def _close(x, n):
    return abs(x - n) < 0.05 * max(abs(n), 1)


_FIRST_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _first_number(text: str):
    """The first quantity in a judge's note ("I get 72" -> 72.0), or None.

    Used only to ask whether two DISSENTING analysts landed on the same value — a
    comparison between two derivations, never a judgment about a claim."""
    m = _FIRST_NUM_RE.search(str(text or ""))
    return float(m.group().replace(",", "")) if m else None


_KV_RE = re.compile(r"^\s*([A-Za-z_][\w .\-/%()]*?)\s*=\s*(.+?)\s*$")


def _split_labelled(text: str) -> list[dict]:
    """Split "a=1; b=2" into assertions. Claimants that ignore `assertions` still tend
    to write labelled values into `value`, and that is already the structure — treating
    it as one opaque blob threw away a decomposition the claimant had made."""
    parts = [x for x in re.split(r"[;\n]", str(text)) if x.strip()]
    if len(parts) == 1:
        comma = [x for x in str(text).split(",") if x.strip()]
        if len(comma) > 1 and all(_KV_RE.match(x) for x in comma):
            parts = comma
    out = []
    for part in parts:
        m = _KV_RE.match(part)
        if m:
            out.append({"label": m.group(1).strip(), "value": m.group(2).strip(), "of": ""})
    return out if len(out) > 1 else []       # one pair is no better than the whole string


_INLINE_KV_RE = re.compile(
    r"([A-Za-z_][\w.\-]{0,40})\s*(?:<=|>=|=|<|>)\s*"
    r"([<>]?=?\s*-?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?)")


def _assertions_from_text(text: str) -> list[dict]:
    """Last-resort salvage: labelled quantities embedded in prose ("rho=1.0, p<0.001").

    Claimants that skip the `assertions` field do not stop asserting numbers — they
    just move them into the statement. Recovering them keeps the claim checkable
    instead of unverifiable, though it is strictly worse than being told the labels."""
    out, seen = [], set()
    for m in _INLINE_KV_RE.finditer(str(text or "")):
        label = m.group(1).strip()
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"label": label, "value": m.group(2).strip(), "of": ""})
    return out


# Per-TOKEN synonyms: labels are split before mapping, so these are the words a label
# is built from, not whole label names. `squared` -> `r2` collapses R_squared and R2;
# `h` -> `kw` collapses KW_H and kw_statistic.
_LABEL_SYNONYMS = {"pseudof": "f", "fstat": "f", "rsquared": "r2", "rsq": "r2",
                   "squared": "r2", "r": "r2", "pval": "p", "pvalue": "p",
                   "padjusted": "adj", "adjusted": "adj", "h": "kw", "kruskal": "kw",
                   "kwh": "kw", "kwstatistic": "kw", "num": "n", "count": "n"}


_LABEL_NOISE = {"stat", "statistic", "value", "val", "the", "of", "vs", "versus", "and",
                "pseudo", "overall", "observed", "test"}


def _label_key(label: str) -> str:
    """A label reduced to what it MEANS, for comparing two claims.

    Order-insensitive, because successors rename freely and each variant slipped the
    guards in turn: `F` vs `F_statistic` (synonym), then `F_raw` vs `raw_F` (order).
    Tokens are split on punctuation and camelCase, mapped through the synonyms, stripped
    of filler, and sorted — so `p_frost_vs_ice` and `ice_frost_p` are one quantity."""
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(label or ""))
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", raw.lower()) if p]
    toks = sorted({_LABEL_SYNONYMS.get(p, p) for p in parts} - _LABEL_NOISE)
    key = "|".join(toks)
    return _LABEL_SYNONYMS.get(key, key) or re.sub(
        r"[^a-z0-9]", "", str(label or "").lower())


def _match_label(raw_label: str, known: list[str]):
    """Map a judge's label onto one of ours. Judges echo what they are grading —
    "n_core = 14", "bacteria_F (bacteria batch)" — so exact matching loses grades that
    were correctly made."""
    raw = str(raw_label).strip().lower()
    lowered = {k.lower(): k for k in known}
    if raw in lowered:
        return lowered[raw]
    head = raw.split("=")[0].split("(")[0].strip()
    if head in lowered:
        return lowered[head]
    for low, orig in lowered.items():        # longest first: prefer the specific label
        if low and (raw.startswith(low) or low in raw):
            return orig
    return None


def _norm_assertions(raw, value_fallback: str = "") -> list[dict]:
    """Assertions as a clean list of ``{label, value, of}``.

    A claim is a bundle of separately checkable quantities, and grading the bundle
    as one unit was the single biggest source of wrong verdicts: every failure
    observed was a claim whose findings held with ONE element in question — four
    correlations reproduced and a p-value off in the last decimal, or 86 rare ASVs
    and 0 singletons confirmed twice over while nobody could agree what a
    "doubleton" was.

    Ledgers written before assertions existed (and models that ignore the field)
    degrade to a single implicit assertion over the whole ``value`` string, which
    grades exactly as it used to."""
    # Models often hand an array argument back as a STRING. Iterating that yields one
    # "assertion" per CHARACTER — the same trap `_norm_antecedents` exists to avoid, and
    # it cost a whole run: every claim arrived as {label: "[", value: "["} and graded
    # unverifiable. Parse it as JSON first, then as "label=value; ..." text.
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        raw = parsed if isinstance(parsed, list) else _split_labelled(raw)
    out = []
    for a in (raw or []):
        if isinstance(a, dict) and a.get("label"):
            out.append({"label": str(a["label"]), "value": str(a.get("value", "")),
                        "of": str(a.get("of", ""))})
        elif isinstance(a, str) and a.strip():          # tolerate a bare list of strings
            out.append({"label": a.strip()[:60], "value": a.strip(), "of": ""})
    if not out and str(value_fallback).strip():
        out = (_split_labelled(value_fallback)
               or [{"label": "claim", "value": str(value_fallback).strip(), "of": ""}])
    return out


def _assertions_summary(assertions: list[dict]) -> str:
    """One-line rendering of assertions, for display and for older consumers."""
    return ", ".join(f"{a['label']}={a['value']}" for a in assertions)


def _format_assertions(assertions: list[dict]) -> str:
    """The checklist a judge grades, one line per separately-checkable quantity."""
    return "\n".join(
        f"  - {a['label']} = {a['value']}" + (f"   (of: {a['of']})" if a.get("of") else "")
        for a in assertions) or "  (none)"


MODEL_VIEW_CAP = 50     # items shown to the model in a tool result
RESULT_CAP = 200        # items KEPT in the stored/re-executed result

_CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str | None:
    """The last fenced python block in a reply (models often narrate, then code).
    Returns None when there is nothing runnable to extract."""
    blocks = _CODE_RE.findall(text or "")
    return blocks[-1].strip() if blocks else None


def _flatten_numbers(v, acc):
    """Collect every number in a nested result into ``acc`` (bools are not numbers)."""
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


def _usable_derivation(res) -> bool:
    """Did this derivation actually compute anything comparable?

    An all-``nan`` or empty result is a FAILED derivation, not a disagreement — it
    means an empty selection, a failed join, or a groupby that matched nothing. On
    real data four claims were voted against on the strength of ``{}`` or
    ``{'rho': nan}``; that is the analyst failing, and it must not count against
    the claim."""
    nums = _flatten_result_numbers(res)
    return any(math.isfinite(x) for x in nums)


def _flatten_result_numbers(v) -> list[float]:
    """Every number in a result, as a list (convenience over ``_flatten_numbers``)."""
    acc: list[float] = []
    _flatten_numbers(v, acc)
    return acc


def _jsonify(v, depth=0, cap=MODEL_VIEW_CAP):
    """Shrink an arbitrary computation result to a JSON-safe, size-capped form
    (lists/dict items→``cap``, depth 4). numpy/pandas are handled lazily so this
    module imports cleanly even where they are absent (they always are wherever a
    real result is produced).

    The cap is a per-caller choice, not a global truth: the model sees
    ``MODEL_VIEW_CAP`` items (context economy), while the sandbox keeps
    ``RESULT_CAP`` so a value the claim cites isn't discarded before ``verify()``
    can find it (#48)."""
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
        return _jsonify(v.tolist(), depth + 1, cap)
    if isinstance(v, dict):
        return {str(k): _jsonify(x, depth + 1, cap) for k, x in list(v.items())[:cap]}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x, depth + 1, cap) for x in list(v)[:cap]]
    if pd is not None and isinstance(v, (pd.Series, pd.DataFrame)):
        return _jsonify(v.to_dict(), depth + 1, cap)
    return v


# The explore transcript grows monotonically: every step appends an assistant turn
# plus the tool results answering it. At a 48-step cap that stayed under a 64k window
# by luck; above it the server starts dropping from the FRONT, which is exactly where
# the system prompt and the data briefing live — so a long run silently loses its
# instructions and its warning about which axis is samples. Elide from the MIDDLE
# instead, oldest first, with the head pinned.
EXPLORE_CHAR_BUDGET = 140_000    # ≈35k tokens of transcript at ~4 chars/token


# Keyword arguments that encode a CHOICE rather than plumbing. A claim whose antecedent
# code sets one of these and whose `parameters` is empty is not reproducible: a
# clean-room analyst picks a different value and refutes a correct claim. Observed three
# times in one evening — a Shannon log base, a fitting method, an invented ordinal scale.
_CHOICE_KWARGS = ("base", "metric", "method", "depth", "permutations", "pseudocount",
                  "alpha", "threshold", "deg", "n_components", "min_count", "cutoff",
                  "quantile", "q", "center", "ddof")
_KWARG_RE = re.compile(r"\b(" + "|".join(_CHOICE_KWARGS) + r")\s*=\s*([^,)\s]{1,24})")


def _choices_in_code(code: str) -> dict:
    """Choice-bearing keyword arguments a computation actually used."""
    out = {}
    for name, val in _KWARG_RE.findall(code or ""):
        out.setdefault(name, val.strip("'\""))
    return out


def _clean_label(label: Any) -> str:
    """A progress event's human-readable label.

    Kept WHOLE. This used to be cut to 40 characters at the claim and 80 at the
    event, which meant a watcher could only ever show "Read abundance is nearly
    evenly split be" — the text was gone before anything downstream could choose
    how to display it. Truncating for a screen is the screen's job. Newlines are
    flattened because consumers are line-oriented, and there is a generous ceiling
    only so a runaway argument can't flood a progress stream."""
    return " ".join(str(label).split())[:400]


def _msg_chars(m: dict) -> int:
    n = len(m.get("content") or "")
    for tc in (m.get("tool_calls") or []):
        n += len(json.dumps(tc, default=str))
    return n


def _compact_messages(messages: list[dict], budget: int = EXPLORE_CHAR_BUDGET) -> list[dict]:
    """Drop the OLDEST steps once the transcript outgrows ``budget``, keeping the
    system prompt and the opening briefing pinned at the head.

    Steps are dropped whole — an assistant turn together with the tool messages that
    answer it — because a ``tool`` message whose ``tool_calls`` are gone is a 400 from
    the API, not a smaller prompt. Little of substance is lost: the agenda, claims,
    assumptions and computations all live on the ``Autoresearcher``, and the model can
    re-read them with ``get_agenda``. Only the raw chatter goes.
    """
    total = sum(_msg_chars(m) for m in messages)
    if total <= budget:
        return messages
    head, tail = messages[:2], messages[2:]      # system prompt + opening briefing
    i = dropped = 0
    while i < len(tail) and total > budget:
        j = i + 1
        while j < len(tail) and tail[j].get("role") == "tool":
            j += 1
        if j >= len(tail):      # never drop the newest step — it has to answer something
            break
        total -= sum(_msg_chars(m) for m in tail[i:j])
        dropped += 1
        i = j
    if not dropped:
        return messages
    return head + [{"role": "user", "content": (
        f"[{dropped} earlier steps were elided to stay within the context window. "
        "Nothing you recorded was lost — your agenda, claims and assumptions are all "
        "intact. Call get_agenda to see where you are, and carry on from there.]")}] + tail[i:]


def build_dag(computations: dict, ledger: list, agenda: list | None = None) -> dict:
    """Claim→antecedent provenance DAG. Nodes: computations (blue), claims
    (verdict-coloured), data paths (grey), and — when ``agenda`` is passed —
    the INVESTIGATIONS that produced the claims, with follow-up lineage.

    The questions are the research narrative: a cluster in ordination led to its
    driver taxa led to a contamination screen. A graph of claims alone shows what
    was found but not why it was looked for (#50)."""
    nodes, edges, seen = [], [], set()

    def add(nid, **kw):
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, **kw})

    for a in (agenda or []):
        add(a["id"], type="investigation", label=a.get("question", "")[:70],
            status=a.get("status"))
    for a in (agenda or []):        # follow-up lineage: which question led to which
        if a.get("parent") and a["parent"] in seen:
            edges.append({"from": a["parent"], "to": a["id"], "kind": "followup"})
    for cid, comp in computations.items():
        add(cid, type="computation", label=comp["label"])
    for c in ledger:
        add(c["id"], type="claim", label=c["statement"][:60], verdict=c.get("verdict"),
            kind=c.get("kind", "observation"))
        if c.get("investigation") and c["investigation"] in seen:
            edges.append({"from": c["investigation"], "to": c["id"], "kind": "answers"})
        for ant in c["antecedents"]:
            if ant in computations:
                edges.append({"from": ant, "to": c["id"]})
            else:
                add(ant, type="data", label=ant)
                edges.append({"from": ant, "to": c["id"]})
    return {"nodes": nodes, "edges": edges}


def dag_mermaid(dag):
    """Render a DAG dict as a Mermaid ```graph LR``` block (for the bench .md)."""
    sty = {"replicated": "fill:#1b5e20,color:#fff", "disputed": "fill:#6a1b9a,color:#fff",
           "contested": "fill:#4527a0,color:#fff", "overturned": "fill:#880e4f,color:#fff",
           "verified": "fill:#2e7d32,color:#fff", "refuted": "fill:#c62828,color:#fff",
           "partial": "fill:#ef6c00,color:#fff", "unverifiable": "fill:#f9a825,color:#000",
           "computation": "fill:#1565c0,color:#fff", "data": "fill:#455a64,color:#fff",
           "investigation": "fill:#00695c,color:#fff"}
    lines = ["```mermaid", "graph LR"]
    for n in dag["nodes"]:
        lbl = n["label"].replace('"', "'")
        shape = ({"computation": f'[["{lbl}"]]', "data": f'("{lbl}")',
                  "investigation": f'{{{{"{lbl}"}}}}'}.get(n["type"], f'["{lbl}"]'))
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


def _provenance_summary(prov: dict, n_analysed: int) -> dict:
    """Pipeline stage totals PLUS what happened to the samples that did not make it.

    ``n_samples: 84`` next to a 63-row counts table, with nothing to reconcile them,
    does not merely withhold the attrition — it makes an analyst doubt the table. One
    was last seen deciding its own `counts` frame must be transposed, which is the very
    error the data briefing exists to prevent (#59).

    So say it: how many were attempted, how many survived, and WHERE the rest died.
    Where they were lost is part of that."""
    per = prov.get("samples") or {}
    stages = [s.get("id") for s in prov.get("stages", []) if s.get("id")]
    out = {"total": prov.get("total", {}), "stages": stages,
           "n_samples_attempted": len(per), "n_samples_analysed": n_analysed}
    if not per or not stages:
        return out
    dropped, where = [], {}
    for sid, chain in per.items():
        if chain.get(stages[-1]):
            continue                       # reached the final table
        dropped.append(sid)
        # the first stage at which this sample hit zero is where it was lost
        lost = next((s for s in stages if not chain.get(s)), stages[-1])
        where[lost] = where.get(lost, 0) + 1
    if dropped:
        out["n_dropped"] = len(dropped)
        out["dropped_at_stage"] = dict(sorted(where.items(), key=lambda kv: -kv[1]))
        out["dropped_samples"] = sorted(dropped)
        out["note"] = (f"{len(dropped)} of {len(per)} samples produced zero reads in the "
                       f"final table and are ABSENT from `counts` — not present as zero "
                       f"rows, so `counts` has {n_analysed} rows.")
    return out


def _primers_summary(primers: dict | None) -> Optional[dict]:
    """Compact INFERRED-primer view for the `study` dataset: which amplicon design(s)
    the submission appears to use, and how confident that inference is.

    States what was inferred and stops. It used to add that a domain split across
    samples is "EXPECTED by design, not a mislabel to flag" — which decides in advance
    the question an analyst is there to answer. Per-sample assignment is not available
    (issue #37), so whether a given split follows from the assay structure is exactly
    what has to be worked out rather than asserted here."""
    if not primers:
        return None
    sets = primers.get("sets") or [primers]
    designs = [{
        "region": s.get("region"),
        "fwd": s.get("fwd_name") or s.get("fwd"),
        "rev": s.get("rev_name") or s.get("rev"),
        "confidence": s.get("confidence"),
        "example_runs": (s.get("runs") or [])[:6],
        "n_runs_mapped": s.get("n_runs") or len(s.get("runs") or []),
    } for s in sets]
    return {
        "source": primers.get("source", "inferred"),
        "designs": designs,
        "note": ("INFERRED from the reads, not confirmed, and not mapped to individual "
                 f"samples (issue #37). {len(designs)} design(s) detected."),
    }


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
                # The WHOLE table, which is what `counts` holds. This reported the
                # prokaryote sub-table's dimensions (414 ASVs, 44 samples) while listing
                # all 63 sample ids beside them, so an analyst comparing it with a
                # 63 x 735 `counts` found a contradiction and started doubting the frame.
                "total_asvs": len(assignments),
                "n_samples": len(samples),
                # The per-domain split from renorm_stats was surfaced here as `by_category`.
                # Removed: its `n_samples` means different things per row. prokaryote 44 and
                # eukaryote 19 partition the samples by assay, but chloroplast 11 does not
                # describe this table at all — 46 chloroplast-labelled ASVs appear in 30 of its
                # samples. An analyst read that row as "11 chloroplast samples". The numbers stay
                # under get_dataset('renorm_stats'), named for the step that produced them.
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
            # Stated study/methods context, as the submitters gave it. Every key here is
            # a CLAIM by someone else, and the field names say so (`stated_target_label`).
            # It carried a `caveat` string that told the agent 18S runs are commonly
            # mislabelled '16S' and to check tax Domain — which is the finding, handed
            # over. A run then "discovering" the mislabel had only followed instructions.
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
                # Primers as recorded, when the pipeline captured them. What assay they
                # imply is the analyst's to work out.
                "amplicon_primers": _primers_summary(sg.get("primers")),
            },
            "renorm_stats": self.read_json("renorm_stats") or fx.get("renorm", {}),
            "provenance": _provenance_summary(prov, len(samples)),
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
try:  # scipy/sklearn are the analysis toolkit; if a sandbox lacks them, numpy/
    # pandas analyses still run and only toolkit-using code errors per-call.
    from scipy.spatial.distance import pdist, squareform, braycurtis
    from scipy.stats import entropy, pearsonr, spearmanr, kruskal, mannwhitneyu
    from sklearn.decomposition import PCA
except ImportError:
    pdist = squareform = braycurtis = entropy = pearsonr = spearmanr = kruskal = mannwhitneyu = PCA = None
_c = _rj("counts"); counts = None; props = None
if _c:
    _m = np.zeros((len(_c["samples"]), len(_c["asvs"])))
    for _s, _a, _n, _r in _c["data"]:
        _m[_s, _a] = _n
    counts = pd.DataFrame(_m, index=_c["samples"], columns=_c["asvs"])
    # Both forms, built here rather than left to be re-derived. `counts` is RAW READS;
    # `props` is each ASV's share of its own sample. The arithmetic is one line, but it
    # is a line along the axis that has twice been got backwards, and a proportion
    # divided by the wrong margin looks entirely plausible.
    _tot = counts.sum(axis=1)
    props = counts.div(_tot.where(_tot > 0), axis=0).fillna(0.0)
_t = _rj("taxonomy") or {}
_db, _body = next(iter(_t.items()), ("none", {}))
_lv = _body.get("levels", [])
tax = pd.DataFrame.from_dict({a: dict(zip(_lv, l)) for a, l in _body.get("assignments", {}).items()},
                            orient="index").reindex(columns=_lv)
meta = pd.DataFrame(_rj("samples") or [])
meta = meta.set_index("id") if "id" in meta.columns else meta
# EVERY SUBMITTED RUN gets a row, not just the ones that produced reads. `samples.json`
# holds survivors only, so a question like "is the attrition biased by treatment?" was
# unanswerable from inside — the dropped runs existed in provenance as bare ids with no
# environment attached. `counts` still holds only the runs with reads; this frame is the
# submission, and `pipeline_in_counts` says which is which.
_prov_all = _rj("provenance") or {}
_chain = _prov_all.get("samples") or {}
_runmap = _rj("sample_runs") or {}
_stage_ids = [x.get("id") for x in _prov_all.get("stages", []) if x.get("id")]
_every = sorted(set(meta.index) | set(_chain) | set(_runmap))
if len(_every) > len(meta.index):
    meta = meta.reindex(_every)
    meta.index.name = "id"
# Every column says where it came from. Three sources reach this frame and they are
# NOT interchangeable: `pipeline_total_reads` is the post-processing depth and matches
# the counts table exactly, while `sra_read_count` is what the submitter declared and
# is zero for 19 of these samples. Reading one for the other is a silent error, and
# nothing in the value itself gives it away.
_PIPE = {"x", "y", "total_reads", "n_asvs"}   # computed by the pipeline from the data
meta.columns = [("pipeline_" if c in _PIPE else "sra_") + str(c) for c in meta.columns]
# Per-sample BioSample attributes (#62) — what each sample IS. Without these the only
# covariate is sequencing depth, and every question about grouping gets answered
# against depth for want of anything better.
# Per-run pipeline chain, and the accession/library for runs samples.json never held.
for _st in _stage_ids:
    meta["pipeline_reads_" + _st] = [(_chain.get(_i) or {}).get(_st) for _i in meta.index]
if _stage_ids:
    meta["pipeline_in_counts"] = [bool((_chain.get(_i) or {}).get(_stage_ids[-1]))
                                  for _i in meta.index]
# The pipeline's `center_name` carries a SUB submission accession, not a centre. Named
# for what it holds, so the real centre can take the name it belongs to.
if "sra_center_name" in meta.columns:
    meta = meta.rename(columns={"sra_center_name": "sra_submission_accession"})
if _runmap:
    for _c, _k in (("sra_sample_accession", "biosample"), ("sra_library_name", "library_name"),
                   ("sra_experiment_title", "title"), ("sra_center_name", "center_name"),
                   ("sra_submitter_accession", "submitter_acc")):
        _fill = pd.Series({_i: (_runmap.get(_i) or {}).get(_k) for _i in meta.index})
        meta[_c] = meta[_c].fillna(_fill) if _c in meta.columns else _fill
# Numbers that arrived as text. `sra_read_count` and `sra_base_count` come out of
# samples.json as strings, so meta['sra_read_count'].median() raises "Cannot perform
# reduction with string dtype" — a column that is plainly numeric refusing arithmetic.
for _c in list(meta.columns):
    if meta[_c].dtype == object or str(meta[_c].dtype) == "str":
        _num = pd.to_numeric(meta[_c], errors="coerce")
        # only if it really is numeric: don't turn a labels column into NaNs
        if _num.notna().sum() and _num.notna().sum() >= meta[_c].notna().sum():
            meta[_c] = _num
_sa = _rj("sample_attributes") or {}
if _sa and "sra_sample_accession" in meta.columns:
    _at = pd.DataFrame.from_dict(_sa, orient="index")
    _at.columns = ["biosample_" + str(c) for c in _at.columns]
    meta = meta.join(_at, on="sra_sample_accession")
import socket
def _no_net(*a, **k):
    raise OSError("network disabled in analysis sandbox")
socket.socket = _no_net; socket.create_connection = _no_net; socket.getaddrinfo = _no_net
# ── statistical hygiene helpers (#49) ────────────────────────────────────────
# Amplicon data is COMPOSITIONAL and these analyses are MULTIPLE-TESTED. Without
# a correction function and a log-ratio transform in scope, an agent cannot do
# the right thing even when it knows it should. Pure numpy — no new dependency,
# so this works in the existing session image.
def fdr(pvals, alpha=0.05):
    """Benjamini-Hochberg across a family of tests -> adjusted p-values."""
    p = np.asarray(list(pvals), dtype=float); n = p.size
    if n == 0: return {"p_adj": [], "reject": [], "n_sig": 0, "n_tests": 0}
    order = np.argsort(p)
    q = np.clip(np.minimum.accumulate((p[order] * n / np.arange(1, n + 1))[::-1])[::-1], 0, 1)
    out = np.empty(n); out[order] = q
    return {"p_adj": out.tolist(), "reject": (out <= alpha).tolist(),
            "n_sig": int((out <= alpha).sum()), "n_tests": int(n), "alpha": alpha}
def clr(df, pseudocount=0.5):
    """Centred log-ratio (rows = samples) — ONE compositional control, not a cure.
    It is sensitive to the pseudocount, and with few parts the sum-to-zero constraint
    induces negative correlation on its own (with 3 taxa it will invent mutual
    exclusion). Use it to check whether a proportion-based pattern survives, not as
    the authoritative answer."""
    X = np.asarray(df, dtype=float) + pseudocount
    L = np.log(X)
    Z = L - L.mean(axis=1, keepdims=True)
    return pd.DataFrame(Z, index=getattr(df, "index", None), columns=getattr(df, "columns", None))
def alpha_diversity(df):
    """Per-sample alpha diversity, with the UNIT IN THE COLUMN NAME.

    Two claims in one ledger reported frost-flower Shannon as 4.59 and 3.18. Both were
    right — bits and nats — and neither said which, so a clean-room analyst computing
    the other one refutes a correct claim over a logarithm base. Naming the column
    `shannon_nats` removes the ambiguity without anyone having to declare a parameter.

    Returns richness, shannon_nats, shannon_bits, evenness (Pielou, 0-1) and depth,
    indexed like the rows of `df`."""
    X = df.values.astype(float)
    depth = X.sum(axis=1)
    rich = (X > 0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        P = np.divide(X, depth[:, None], out=np.zeros_like(X), where=depth[:, None] > 0)
        logP = np.where(P > 0, np.log(P, where=P > 0), 0.0)
        nats = -(P * logP).sum(axis=1)
        even = np.divide(nats, np.log(rich), out=np.zeros_like(nats),
                         where=rich > 1)
    return pd.DataFrame({"richness": rich, "shannon_nats": nats,
                         "shannon_bits": nats / np.log(2), "evenness": even,
                         "depth": depth}, index=getattr(df, "index", None))


def permanova(data, groups, permutations=999, seed=0):
    """PERMANOVA (Anderson 2001) — pseudo-F, R2 and a permutation p-value.

    Supplied because five independent analysts hand-rolled this test and returned
    F = 0.0048, 0.39, 22.89 and 92.01 for a dataset whose answer is 4.19 — four orders
    of magnitude, none right, while `fdr`, `clr` and `rarefy` were used correctly every
    time. The difference was availability, not care.

    `data` is either a square distance matrix or a samples x features table, in which
    case Bray-Curtis distances are computed from it. `groups` is one label per sample,
    in the same order as the rows."""
    X = np.asarray(getattr(data, "values", data), dtype=float)
    square = X.ndim == 2 and X.shape[0] == X.shape[1] and X.shape[0] > 1
    if square and np.allclose(np.diag(X), 0) and np.allclose(X, X.T):
        D = X
    else:
        D = squareform(pdist(X, metric="braycurtis"))
    g = np.asarray(getattr(groups, "values", groups), dtype=object)
    if len(g) != D.shape[0]:
        raise ValueError(f"{len(g)} group labels for {D.shape[0]} samples — they must "
                         "line up row for row (meta.loc[counts.index] aligns them)")
    levels, d2, N = np.unique(g), D ** 2, D.shape[0]
    a = len(levels)
    if a < 2 or N <= a:
        raise ValueError(f"need at least 2 groups and more samples than groups; "
                         f"got {a} groups over {N} samples")

    def _F(labels):
        sst = d2.sum() / (2 * N)
        ssw = 0.0
        for lev in levels:
            idx = np.where(labels == lev)[0]
            if len(idx) > 1:
                ssw += d2[np.ix_(idx, idx)].sum() / (2 * len(idx))
        ssa = sst - ssw
        return ((ssa / (a - 1)) / (ssw / (N - a)) if ssw > 0 else float("nan"),
                ssa / sst if sst > 0 else float("nan"))

    F, R2 = _F(g)
    rng = np.random.default_rng(seed)
    ge = sum(1 for _ in range(permutations) if _F(rng.permutation(g))[0] >= F)
    return {"F": round(float(F), 4), "R2": round(float(R2), 4),
            "p": round((ge + 1) / (permutations + 1), 4),
            "n": int(N), "groups": int(a), "group_sizes":
                {str(l): int((g == l).sum()) for l in levels},
            "permutations": int(permutations)}


def rarefy(df, depth=None, seed=0):
    """Subsample every sample to a common depth without replacement (seeded, so
    the result re-executes identically at verification time). Rows below `depth`
    are dropped — returns (rarefied_df, dropped_sample_ids)."""
    M = np.asarray(df, dtype=float).round().astype(np.int64)
    sums = M.sum(axis=1)
    d = int(depth if depth is not None else sums[sums > 0].min())
    rng = np.random.default_rng(seed)
    keep = sums >= d
    rows = [rng.multivariate_hypergeometric(M[i], d) for i in np.where(keep)[0]]
    idx = getattr(df, "index", None)
    kept_idx = (idx[keep] if idx is not None else None)
    dropped = [str(x) for x in (idx[~keep] if idx is not None else [])]
    return pd.DataFrame(rows, index=kept_idx, columns=getattr(df, "columns", None)), dropped
# Items kept per container/list. The RESULT is what verification re-derives from,
# so it keeps more than the model is shown (the parent re-caps for context economy).
_CAP = int(os.environ.get("EXPLORER_RESULT_CAP", "200"))
def _j(v, d=0):
    if d > 4: return str(v)
    if isinstance(v, (np.floating, np.integer)): return round(float(v), 4)
    if isinstance(v, float): return round(v, 4)
    if isinstance(v, np.ndarray): return _j(v.tolist(), d + 1)
    if isinstance(v, dict): return {str(k): _j(x, d + 1) for k, x in list(v.items())[:_CAP]}
    if isinstance(v, (list, tuple)): return [_j(x, d + 1) for x in list(v)[:_CAP]]
    if isinstance(v, (pd.Series, pd.DataFrame)): return _j(v.to_dict(), d + 1)
    if isinstance(v, pd.Index): return _j(v.tolist(), d + 1)
    # pandas extension arrays (StringArray, Categorical, nullable Int64...) and numpy
    # scalars of any other flavour. A correct analysis was failing at the last step
    # because its result happened to contain tax.columns.values, and the traceback
    # named a type the analyst had never mentioned.
    if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
        try: return _j(v.tolist(), d + 1)
        except Exception: return str(v)
    if isinstance(v, (np.bool_,)): return bool(v)
    if v is None or isinstance(v, (str, bool, int)): return v
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)
_ns = dict(np=np, pd=pd, counts=counts, props=props, tax=tax, meta=meta,
           pdist=pdist, squareform=squareform,
           braycurtis=braycurtis, entropy=entropy, pearsonr=pearsonr, spearmanr=spearmanr,
           kruskal=kruskal, mannwhitneyu=mannwhitneyu, PCA=PCA,
           fdr=fdr, clr=clr, rarefy=rarefy, permanova=permanova,
           alpha_diversity=alpha_diversity)
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
            # 400, not 200: pandas puts the part that says WHICH axis failed at the
            # end of the message, and a truncated KeyError is indistinguishable from
            # any other KeyError.
            return False, ((p.stderr or p.stdout or "no output").strip().splitlines()
                           or ["error"])[-1][:400]
        if not r:
            # No output at all and no traceback: the child died without saying so —
            # killed on a resource limit, or a native crash inside numpy/scipy. A bare
            # "error" told the analyst nothing it could act on, and this is the one
            # failure it cannot see the cause of from inside.
            why = (p.stderr or "").strip().splitlines()
            sig = f" (exit {p.returncode})" if p.returncode else ""
            return False, (f"the analysis process produced no output{sig} — it was "
                           "killed before finishing, most often by the memory or CPU "
                           "limit on this sandbox. Try it on a smaller subset, or with "
                           "fewer permutations."
                           + (f" stderr: {why[-1][:200]}" if why else ""))
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
                 write_model: str | None = None, replicate_model: str | None = None,
                 adjudicate_model: str | None = None,
                 clients: dict | None = None,
                 package_installer: Optional[Callable[[str], dict]] = None,
                 max_steps: int = 48, max_followups: int = 12, max_tokens: int = 4000,
                 on_progress: Optional[Callable[[str, Any], Awaitable]] = None):
        self.data = data
        self.llm = llm
        # Optional per-ROLE clients ('explore'/'verify'/'replicate'/'adjudicate'/'write').
        # Roles can live on different endpoints — e.g. a big claimant on a 32 GB box and
        # the judge on another — which is what lets several models stay resident at once
        # instead of being swapped in and out of one card. Absent roles use `llm`.
        self.clients = dict(clients or {})
        self.executor = executor
        self.explore_model = explore_model or llm.model
        self.verify_model = verify_model or llm.model
        # Prose is a DRAFTING role, not the agent loop — let it be pointed at the
        # drafting model like every other writing surface (falls back to explore).
        self.write_model = write_model or self.explore_model
        # The clean-room analyst. Pointing this at a DIFFERENT model than explore is
        # the whole point — a model re-deriving its own claim shares its own blind
        # spots — so this is worth configuring even when the other roles are not.
        self.replicate_model = replicate_model or self.verify_model
        # Round 3's analyst. A third distinct model is ideal — the casting vote
        # should not share a lineage with either of the first two.
        self.adjudicate_model = adjudicate_model or self.replicate_model
        self.max_steps = max_steps
        self.max_followups = max_followups
        # Generation budget per turn. 2500 was set for a non-reasoning model; a model
        # that thinks before answering can spend the whole budget thinking and return
        # no tool call at all, which costs a step and looks like the model refusing.
        self.max_tokens = max_tokens
        self.on_progress = on_progress
        # per-run state (was module globals in the prototype)
        self.computations: dict[str, Any] = {}   # cid -> {label, code, result}
        self.ledger: list[dict] = []              # claim dicts
        self.assumptions: list[dict] = []         # acknowledged, unconfirmable assumptions
        self.agenda: list[dict] = []              # {id, question, rationale, status, parent}
        self.results_prose: str | None = None     # last write_results() output (for snapshot)
        self.results_prose_by: str | None = None  # model that wrote the prose
        self._briefing: str | None = None         # cached data-shape briefing
        self.refused_claims: int = 0              # claims rejected as uncheckable
        # Libraries analysts asked for and did not have. A ModuleNotFoundError is a
        # wasted step and nothing else; a recorded request is evidence about what the
        # sandbox should contain.
        self.package_requests: list[dict] = []
        # Recent sandbox error signatures. An analyst repeated one identical error four
        # times without self-correcting, and nothing in the loop knew it was a loop.
        self._recent_errors: list[str] = []
        # Optional: given a package name, make it available and say what happened. The
        # POLICY of which packages are grantable belongs to whoever owns the sandbox,
        # not here — in production that is a container image, not a pip call.
        self.package_installer = package_installer
        # Hyphal growth (#58): the agenda item the ACTIVE TIP is working. In the linear
        # explore() this stays None and the agenda's own statuses decide; under
        # explore_hyphal() each tip owns one item for its whole (short) life, so claims
        # and assumptions attribute to the branch that produced them.
        self._active_tip: str | None = None
        self.exploration: str = "linear"          # or "hyphal" — recorded in run_summary
        # Re-executed computations, cached for the life of the run so a claim judged
        # live during exploration and the end-of-run pass never re-run the same code.
        self._comp_cache: dict[str, tuple[bool, Any]] = {}
        # Claims awaiting live judgment. Set only while a live verifier is running;
        # `record_claim` posts to it, a task on the VERIFY host drains it (#61).
        self._verify_queue: Optional[asyncio.Queue] = None

    # -- data briefing ----------------------------------------------------------
    # Packages the sandbox would install on request, if anything is wired to grant
    # them. Named in the briefing because the allowlist is otherwise invisible: an
    # analyst hand-rolled PERMANOVA twice, wrongly, with skbio one tool call away.
    grantable_packages: tuple[str, ...] = ()

    async def data_briefing(self) -> str:
        """Real shapes/orientation of the analysis frames, probed once per run.

        An executor call, not a model call, so it costs seconds and no tokens. Any
        failure degrades to "" — a missing briefing must never block exploration or
        replication, and non-amplicon pipelines may have no counts/tax/meta at all."""
        if self._briefing is None:
            try:
                ok, res = await self.executor.run(_BRIEFING_CODE)
                if ok and isinstance(res, dict):
                    # The sandbox can only see the frames it was given. How many samples
                    # were ATTEMPTED lives in provenance, and an unreconciled 84 beside a
                    # 63-row table makes an analyst doubt the table (#59).
                    try:
                        res = {**res, "provenance": self.data.datasets().get("provenance")}
                    except Exception:
                        pass
                    if self.grantable_packages:
                        res = {**res, "grantable": list(self.grantable_packages)}
                    self._briefing = format_briefing(res)
                else:
                    self._briefing = ""
            except Exception:
                self._briefing = ""
        return self._briefing

    # -- role routing -----------------------------------------------------------
    def client_for(self, role: str) -> LLMClient:
        """The client serving `role`, falling back to the shared one."""
        return self.clients.get(role) or self.llm

    async def _chat(self, role: str, messages, **kw):
        return await self.client_for(role).chat(messages, **kw)

    # -- progress ---------------------------------------------------------------
    async def _emit(self, event: str, detail: Any) -> None:
        if self.on_progress is not None:
            await self.on_progress(event, detail)

    # -- agenda helpers ---------------------------------------------------------
    def _current_investigation(self) -> str | None:
        """The investigation being worked (first in-progress, else first pending).

        A live tip answers this outright: it owns exactly one item, and it keeps
        owning it after ``mark_done`` so a trailing ``record_claim`` still lands on
        the branch that earned it rather than on whatever came next."""
        if self._active_tip is not None:
            return self._active_tip
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
            if self._active_tip is not None:
                # A tip closes its own item and stops; promoting the next one is the
                # scheduler's job, not this tip's — it will never see that item.
                return {"done": cur, "remaining": sum(
                    a["status"] in ("pending", "in_progress") for a in self.agenda)}
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
                # Correct the mistake where it was made. The briefing says the frames
                # are already bound and so does this tool's description, and analyses
                # still opened counts.csv — a hint ten minutes upstream loses to a
                # habit. This one arrives attached to the traceback that proves it.
                err = str(res)
                hint = None
                if "FileNotFoundError" in err or "No such file" in err:
                    hint = ("Nothing is read from disk here. `counts`, `props`, `tax` "
                            "and `meta` are already bound as pandas DataFrames — use "
                            "them directly instead of opening a file.")
                elif "is not defined" in err:
                    hint = ("Only the names listed in the briefing exist in this "
                            "sandbox; nothing else can be imported or loaded.")
                elif "No module named" in err:
                    # The one failure that should point straight at request_package,
                    # and it fired no hint at all: ModuleNotFoundError contains neither
                    # "ImportError" nor "cannot import name". An analyst reached for
                    # skbio — the package this tool was built for — and was told nothing.
                    want = (re.search(r"No module named '([^']+)'", err) or [None, ""])[1]
                    root = want.split(".")[0]
                    if root in (self.grantable_packages or ()):
                        hint = (f"`{root}` is not installed but CAN be: call "
                                f"request_package with package=\"{root}\" and it will be "
                                "available for your next analysis.")
                    else:
                        hint = (f"`{root}` is not available and cannot be installed. "
                                + (f"These can: {', '.join(self.grantable_packages)}. "
                                   if self.grantable_packages else "")
                                + "Otherwise work with what the briefing lists.")
                elif "cannot import name" in err or "ImportError" in err:
                    # Two different cases, and the earlier wording covered only one.
                    # `fdr`/`clr`/`rarefy` are ours and exist nowhere else; but
                    # `braycurtis`, `entropy` and `PCA` are real library functions,
                    # already bound, and simply not in the module being guessed at.
                    hint = ("Everything the briefing lists is already bound and none of "
                            "it needs importing — that includes the scipy and sklearn "
                            "functions (braycurtis, entropy, pdist, squareform, PCA, "
                            "the tests) as well as `fdr`, `clr` and `rarefy`, which are "
                            "defined here and exist in no library at all. Use the name "
                            "directly.")
                elif "numpy.ndarray' object has no attribute" in err:
                    hint = ("That is a numpy array, not a pandas object — `.values` and "
                            "`.to_numpy()` drop you out of pandas. `counts`, `props`, "
                            "`tax` and `meta` are DataFrames: stay in pandas (.loc, "
                            ".groupby, .mean(axis=...)), or wrap a result back up with "
                            "pd.Series(arr, index=...).")
                elif "unalignable boolean" in err.lower() or "do not match" in err.lower():
                    # pandas' THIRD phrasing for this mistake, and the one that shows up
                    # now that meta has 84 rows against counts' 63: a mask built from
                    # meta cannot index counts until the frames are aligned.
                    hint = ("That mask was built on a different index. `meta` has a row "
                            "for every submitted sample and `counts` only the ones with "
                            "reads — align first with meta.loc[counts.index], then build "
                            "the mask from the aligned frame.")
                elif "boolean index" in err.lower():
                    # numpy says "boolean index did not match"; pandas says "Boolean
                    # index has wrong length". Matching only one of them meant the
                    # hint fired on half the occurrences of the same mistake.
                    hint = ("A boolean mask has to match the axis it indexes. `counts` "
                            "and `props` are samples (rows) x ASVs (columns): a "
                            "per-sample mask selects rows, a per-ASV mask selects "
                            "columns — `df.loc[sample_mask]` vs `df.loc[:, asv_mask]`.")
                elif "1-dimensional, but 2 were indexed" in err:
                    hint = ("`pdist` returns a CONDENSED 1-D array. `squareform(...)` "
                            "turns it into the square distance matrix.")
                elif "did not set a `result`" in err:
                    hint = "Assign what you want returned to `result`."
                # A different error each time is debugging; the same one repeatedly is
                # being stuck, and the two need different advice. Nothing distinguished
                # them, so a tip could spend its whole budget on one mistake.
                # 40, not 60. Two AttributeErrors differing only in the attribute name
                # ('median' vs 'values') are the same mistake — treating a numpy array
                # as a pandas object — and a 60-character signature filed them as
                # unrelated, so neither escalated.
                sig = " ".join(err.split())[:40]
                self._recent_errors.append(sig)
                self._recent_errors = self._recent_errors[-5:]
                if (n := self._recent_errors.count(sig)) >= 2:
                    hint = ((hint + " ") if hint else "") + (
                        f"This is the same error {n} times running — the approach is "
                        "not working, so change it rather than adjusting it. Inspect "
                        "the shapes you are indexing, or take a simpler route to the "
                        "same answer.")
                return {"ok": False, "error": err, **({"hint": hint} if hint else {})}
            cid = f"c{len(self.computations) + 1}"
            # Store the FULL sandbox result (verification re-derives from it); show
            # the model a re-capped view so context economy doesn't cost the verifier
            # numbers it will later be asked to find (#48).
            self.computations[cid] = {"label": args.get("label", cid),
                                      "code": args.get("code", ""), "result": res,
                                      "by": self.explore_model}  # model that wrote it
            return {"ok": True, "computation_id": cid, "result": _jsonify(res, cap=MODEL_VIEW_CAP)}
        if name == "record_claim":
            statement = args.get("statement", "")
            assertions = (_norm_assertions(args.get("assertions"), args.get("value", ""))
                          or _assertions_from_text(statement))
            if not assertions:
                # Refuse the claim rather than bank one nothing can check. The model gets
                # told why and can re-send; a silently unverifiable claim helps nobody.
                self.refused_claims += 1
                return {"recorded": False, "error":
                        "no assertions — a claim with nothing separately checkable cannot be "
                        "verified. Re-send with assertions=[{\"label\": ..., \"value\": ..., "
                        "\"of\": ...}], one entry per number you are asserting."}
            # A successor context re-derived its predecessor's claim and recorded it
            # again, word for word — the seed says "do NOT re-record these" and that
            # was not enough. Claim-sized contexts (#61) make this the default failure
            # rather than an oddity: each one starts fresh, works the same question,
            # and reaches the same answer. Instruction loses; identity is checkable.
            _sig = sorted((_label_key(a.get("label")), str(a.get("value", "")))
                          for a in assertions)
            for prior in self.ledger:
                if _sig and sorted((_label_key(a.get("label")), str(a.get("value", "")))
                                   for a in (prior.get("assertions") or [])) == _sig:
                    self.refused_claims += 1
                    return {"recorded": False, "error":
                            f"{prior['id']} already asserts exactly these values. Either "
                            "take this investigation further than it did, or call "
                            "mark_done if there is nothing left to find."}
            # Contradiction on the same investigation. Four successor contexts each
            # re-derived the same PERMANOVA and recorded four different F values, and
            # the identity check above passed every one of them because the numbers
            # DIFFER — which is the worse failure, not the acceptable one. A ledger
            # that asserts two values for one quantity is incoherent whatever the
            # verifier later says about each in isolation.
            _here = self._current_investigation()
            _mine = {_label_key(a.get("label")): str(a.get("value", "")) for a in assertions}
            for prior in self.ledger:
                if prior.get("investigation") != _here:
                    continue
                for pa in (prior.get("assertions") or []):
                    lbl, was = _label_key(pa.get("label")), str(pa.get("value", ""))
                    if lbl not in _mine or _mine[lbl] == was:
                        continue
                    _a, _b = _first_number(was), _first_number(_mine[lbl])
                    # Rounding and re-phrasing are not contradictions; a different
                    # answer is. Non-numeric values fall back to plain inequality.
                    if _a is not None and _b is not None and _close(_a, _b):
                        continue
                    self.refused_claims += 1
                    return {"recorded": False, "error":
                            f"{prior['id']} already asserts {lbl}={was} for this same "
                            f"investigation and you have {lbl}={_mine[lbl]}. One of them "
                            "is wrong. Work out which — check your method against the "
                            "earlier computation — and record the corrected value with "
                            "the reason, rather than a second answer beside it."}
            claim = {"id": f"k{len(self.ledger) + 1}", "statement": statement,
                     "value": str(args.get("value", "")) or _assertions_summary(assertions),
                     "assertions": assertions,
                     "parameters": args.get("parameters") or {},
                     "antecedents": _norm_antecedents(args.get("antecedents")),
                     "kind": args.get("kind", "observation"),
                     "investigation": self._current_investigation(),
                     "by": self.explore_model}  # model that recorded this claim
            self.ledger.append(claim)
            if not claim["parameters"]:
                # Name what the code chose, rather than asking again in the abstract.
                found = {}
                for ant in claim["antecedents"]:
                    if ant in self.computations:
                        found.update(_choices_in_code(self.computations[ant].get("code", "")))
                claim["_choices"] = found
                # An analyst correlated diversity against an "environmental harshness"
                # ranking it invented, recorded the ranking nowhere, and no replicator
                # could reproduce the number: four plausible orderings give r between
                # -0.22 and +0.20 against a claimed -0.39. The prompt asks for knobs by
                # name; saying it again where the claim lands costs nothing.
                claim["_no_parameters"] = True
            if self._verify_queue is not None:
                self._verify_queue.put_nowait(claim["id"])   # judged while we carry on
            out = {"recorded": True, "claim_id": claim["id"], "n_claims": len(self.ledger)}
            if claim.pop("_no_parameters", False):
                found = claim.pop("_choices", {}) or {}
                shown = ", ".join(f"{k}={v}" for k, v in sorted(found.items()))
                out["note"] = (
                    (f"`parameters` is empty, but your computation set {shown}. Those are "
                     "choices — put them there. " if shown else
                     "`parameters` is empty. If this analysis used a threshold, a cutoff, "
                     "a grouping or an ordering that YOU chose rather than read from the "
                     "data, add it. ")
                    + "An independent analyst picking differently will get a different "
                      "number and your claim will read as refuted. If you had to assume "
                      "something you could not confirm, record_assumption too.")
            return out
        if name == "request_package":
            pkg = (args.get("package") or "").strip()
            if not pkg:
                return {"recorded": False, "error": "name the package"}
            self.package_requests.append({
                "id": f"pkg{len(self.package_requests) + 1}", "package": pkg,
                "why": args.get("why", ""),
                "investigation": self._current_investigation(),
                "by": self.explore_model})
            n = sum(r["package"] == pkg for r in self.package_requests)
            outcome = {}
            if self.package_installer is not None:
                try:
                    outcome = self.package_installer(pkg) or {}
                except Exception as e:                     # noqa: BLE001
                    outcome = {"installed": False, "available": False, "reason": str(e)}
            self.package_requests[-1].update(
                {k: outcome.get(k) for k in ("installed", "available", "reason")})
            await self._emit("package_installed" if outcome.get("installed")
                             else "package_refused",
                             {"package": pkg, **outcome})
            if outcome.get("available"):
                return {"recorded": True, "package": pkg, "times_requested_this_run": n,
                        "available_this_run": True, "note":
                        f"{pkg} is now importable in run_analysis "
                        f"({outcome.get('reason', 'available')}) — go ahead and use it."}
            return {"recorded": True, "package": pkg, "times_requested_this_run": n,
                    "available_this_run": False,
                    "note": (f"not available this run ({outcome.get('reason', 'no installer')}); "
                             "carry on with what is in scope")}
        if name == "record_assumption":
            assumption = {"id": f"as{len(self.assumptions) + 1}",
                          "statement": args.get("statement", ""),
                          "why": args.get("why", ""),
                          "impact": args.get("impact", ""),
                          "investigation": self._current_investigation(),
                          "by": self.explore_model}  # model that made the assumption
            self.assumptions.append(assumption)
            return {"recorded": True, "assumption_id": assumption["id"],
                    "n_assumptions": len(self.assumptions)}
        return {"error": f"unknown tool {name}"}

    # -- explore ----------------------------------------------------------------
    def _resume_briefing(self) -> str:
        """User message that re-orients the model when CONTINUING a prior run: what
        it already found (so it doesn't repeat claims) and where it left off (so it
        digs deeper). Keeps the agenda/ledger as the shared state, not the chat."""
        agenda_lines = "\n".join(
            f"  [{a['status']}] {a['id']}: {a['question']}"
            + (f"  (follow-up of {a['parent']})" if a.get("parent") else "")
            for a in self.agenda) or "  (none)"
        claim_lines = "\n".join(
            f"  {c['id']} [{c.get('kind', 'observation')}] {c['statement']}"
            for c in self.ledger) or "  (none)"
        assumption_lines = "\n".join(
            f"  {a['id']}: {a['statement']}" for a in self.assumptions) or "  (none yet)"
        return (
            "You are RESUMING your own earlier investigation of this dataset — keep "
            "digging DEEPER, don't restart.\n\n"
            f"Agenda so far (statuses):\n{agenda_lines}\n\n"
            f"Claims you already recorded — do NOT repeat these; build beyond them:\n{claim_lines}\n\n"
            f"Assumptions already on record — do NOT re-record these. Better: where digging "
            f"deeper now lets you CONFIRM or REFUTE one, do that and record_claim the result "
            f"(that is high-value work); otherwise leave it standing:\n{assumption_lines}\n\n"
            "Work any pending/interrupted items, then add_followup on the most promising "
            "or surprising leads and pursue them (a cluster → its driver taxa → are they "
            "contamination?). Record new claims for what you find, and record_assumption for "
            "anything NEW you had to take for granted but couldn't confirm. Reply DONE only "
            "when you judge the investigation has gone deep enough.")

    async def explore(self, resume: bool = False) -> bool:
        """Run the agenda-driven tool-calling loop. Returns True only when the
        agenda (including follow-ups) was actually worked through; on a step-cap
        exit the in-progress item is marked ``interrupted`` (never faked done).

        With ``resume=True`` this CONTINUES a run reconstructed from a snapshot:
        interrupted items are reactivated and the model is re-briefed with what it
        already found so it goes deeper instead of proposing a fresh agenda."""
        if resume:
            for a in self.agenda:            # reactivate work parked at the last stop
                if a["status"] == "interrupted":
                    a["status"] = "pending"
            messages = [
                {"role": "system", "content": EXPLORE_SYSTEM},
                {"role": "user", "content": self._resume_briefing()}]
        else:
            messages = [
                {"role": "system", "content": EXPLORE_SYSTEM},
                {"role": "user", "content": (f"{await self.data_briefing()}\n\n"
                 if await self.data_briefing() else "")
                 + "Propose your agenda of microbial-ecology tests, "
                 "then work through it, recursing where it gets interesting."}]
        swept_assumptions = False   # force one assumptions pass before finishing
        for step in range(self.max_steps):
            messages = _compact_messages(messages)
            r = await self._chat("explore", messages, model=self.explore_model, tools=TOOLS,
                                    tool_choice="auto", temperature=0.25,
                                 max_tokens=self.max_tokens)
            msg = r.choices[0].message
            if not msg.tool_calls:
                content = _strip_think(msg.content or "")
                active = [a for a in self.agenda if a["status"] in ("pending", "in_progress")]
                if "DONE" in content.upper() and not active:
                    # Don't let it finish without one explicit look for assumptions —
                    # models otherwise skip record_assumption entirely.
                    if not swept_assumptions:
                        swept_assumptions = True
                        messages.append({"role": "user", "content":
                            "Before you finish — required pass: look back over your whole analysis and "
                            "call record_assumption for EACH thing you had to take for granted but could "
                            "not confirm from the data or the stated context. Every real analysis makes "
                            "some — e.g. whether counts are raw or normalized, what an ambiguous field "
                            "means, an inferred grouping, the stated amplicon target/primers, a database "
                            "version, or that a named test's assumptions held. Record each one now. Only "
                            "if you truly relied on none, reply 'NO ASSUMPTIONS' then DONE."})
                        continue
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
                except json.JSONDecodeError as e:
                    # Malformed args used to fall through as `{}` — silently recording
                    # an empty claim or running empty code. Tell the model instead so
                    # it can re-issue the call (truncation at max_tokens is the usual
                    # cause, and it is recoverable).
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(
                        {"error": f"could not parse tool arguments as JSON ({e}); "
                                  "re-send this call with valid, complete JSON arguments"})})
                    await self._emit(tc.function.name, {"step": step, "label": "bad arguments",
                                                        "result": {"error": "unparseable args"}})
                    continue
                result = await self._exec_tool(tc.function.name, args)
                label = (args.get("label") or args.get("name") or args.get("question")
                         or args.get("why") or (args.get("statement") or ""))
                await self._emit(tc.function.name, {"step": step, "label": _clean_label(label),
                                                    "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)[:3500]})
        # Step cap hit with work outstanding: the current item was interrupted, and
        # pending items stay pending. Return True only when nothing is outstanding.
        for a in self.agenda:
            if a["status"] == "in_progress":
                a["status"] = "interrupted"
        # `bool(self.agenda)` is load-bearing: an agenda that was never proposed has
        # nothing outstanding, and reporting that as a completed investigation is how a
        # run that did nothing comes to look finished (it once ran all six downstream
        # phases on an empty ledger and printed "0/0 investigations done").
        return bool(self.agenda) and not any(
            a["status"] in ("pending", "in_progress", "interrupted") for a in self.agenda)

    # -- hyphal growth (#58) ----------------------------------------------------
    async def _agent_loop(self, *, system: str, seed: str, tools: list, max_steps: int,
                          nudge: str, stop=None, tag: str = "", prod=None) -> int:
        """One bounded tool-calling loop in its OWN context. Returns steps used.

        This is the plumbing the linear ``explore`` grew organically — parse the tool
        arguments, run the tool, emit progress, feed the result back — with the loop's
        *purpose* (which tools, what seeds it, what ends it) supplied by the caller.
        Every hyphal phase is one of these; none of them outlives its own return."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": seed}]
        for step in range(max_steps):
            messages = _compact_messages(messages)   # a well-behaved tip never needs this
            if prod is not None and (say := prod(step, max_steps)):
                messages.append({"role": "user", "content": say})
            r = await self._chat("explore", messages, model=self.explore_model, tools=tools,
                                 tool_choice="auto", temperature=0.25,
                                 max_tokens=self.max_tokens)
            msg = r.choices[0].message
            cut = getattr(r.choices[0], "finish_reason", None) == "length"
            if not msg.tool_calls:
                if cut:
                    # Out of generation budget mid-thought. Saying so beats the generic
                    # nudge, which reads as "you did nothing" and invites a repeat.
                    await self._emit("truncated", {"tip": tag, "step": step})
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    messages.append({"role": "user", "content":
                        "Your reply was cut off at the token limit before you called a "
                        "tool. Keep the reasoning short and make the call."})
                    continue
                if "DONE" in _strip_think(msg.content or "").upper():
                    return step + 1
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": nudge})
                continue
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(
                        {"error": f"could not parse tool arguments as JSON ({e}); "
                                  "re-send this call with valid, complete JSON arguments"})})
                    await self._emit(tc.function.name, {"step": step, "label": "bad arguments",
                                                        "tip": tag, "result": {"error": "unparseable args"}})
                    continue
                result = await self._exec_tool(tc.function.name, args)
                label = (args.get("label") or args.get("name") or args.get("question")
                         or args.get("why") or (args.get("statement") or ""))
                await self._emit(tc.function.name, {"step": step, "label": _clean_label(label),
                                                    "tip": tag, "result": result,
                                                    "error": result.get("error")})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)[:3500]})
            if stop is not None and stop():
                return step + 1
        return max_steps

    def _ancestry(self, item: dict) -> list[dict]:
        """The agenda items this one branched from, oldest first."""
        by_id = {a["id"]: a for a in self.agenda}
        chain, cur, seen = [], by_id.get(item.get("parent") or ""), set()
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])            # a malformed parent cycle must not hang the run
            chain.append(cur)
            cur = by_id.get(cur.get("parent") or "")
        return list(reversed(chain))

    def _claim_lines(self, claims: list[dict]) -> str:
        """Claims as an analyst is shown them, carrying a verdict once one exists.

        The judge's reasoning comes along whatever the verdict. It is what a later
        analyst can actually act on — "cited a number its antecedent never produced"
        stops the next one doing the same. Showing it does let the claimant learn what
        its checker wants, but judging, replication and adjudication are three
        different models, so there is no single checker to learn."""
        out = []
        for c in claims:
            line = (f"  {c['id']} [{c.get('kind', 'observation')}] {c['statement']} "
                    f"— {c.get('value', '')}")
            verdict = c.get("verdict_round1") or c.get("verdict")
            if verdict:
                line += f"\n      VERDICT: {verdict}"
                if bad := c.get("unsupported_numbers"):
                    line += f" — these did not hold: {', '.join(bad)}"
                if verdict == "unverifiable":
                    line += " — its antecedents produced no evidence at all"
                notes = (c.get("judgment") or {}).get("notes") or {}
                if why := [f"{l}: {n}" for l, n in notes.items() if n]:
                    line += f"\n      the judge said: {'; '.join(why)[:300]}"
            out.append(line)
        return "\n".join(out) or "  (none yet)"

    async def _tip_seed(self, item: dict, one_claim: bool = False) -> str:
        """What a tip is handed instead of the whole conversation: the data briefing,
        its own question, the branch it grew from, and the colony's findings so far.

        The findings are the shared medium — a tip that cannot see them would redo
        work and miss connections, which is the one real thing a single long session
        buys. Claims are a snapshot taken when the tip is seeded, so a tip's input is
        fixed at birth even though other tips keep recording."""
        ancestors = self._ancestry(item)
        anc_ids = {a["id"] for a in ancestors}
        own = [c for c in self.ledger if c.get("investigation") == item["id"]]
        mine = [c for c in self.ledger if c.get("investigation") in anc_ids]
        others = [c for c in self.ledger
                  if c.get("investigation") not in anc_ids | {item["id"]}]
        briefing = await self.data_briefing()
        parts = [f"{briefing}\n" if briefing else ""]
        parts.append("YOUR INVESTIGATION — work this one and only this one:\n"
                     f"  {item['id']}: {item['question']}")
        if item.get("rationale"):
            parts.append(f"  why it matters: {item['rationale']}")
        if ancestors:
            parts.append("\nThis question branched off an earlier one:\n" + "\n".join(
                f"  {a['id']}: {a['question']}" for a in ancestors)
                + f"\n\nWhat that line of investigation found — build DIRECTLY on these:\n"
                  f"{self._claim_lines(mine)}")
        if own:
            # A successor context continuing this same investigation. It has to know
            # what its predecessors already banked, and that their computations are
            # still available by id rather than needing to be redone.
            parts.append("\nEarlier analysts have already worked this same "
                         f"investigation and recorded:\n{self._claim_lines(own)}\n"
                         "Carry on from there — do NOT re-record these, and cite an "
                         "existing computation id as an antecedent rather than "
                         "recomputing it.")
        parts.append("\nFindings from the other investigations, for context — do not repeat "
                     f"them and do not redo them:\n{self._claim_lines(others)}")
        parts.append("\nAssumptions already on record — do NOT re-record these:\n" + ("\n".join(
            f"  {a['id']}: {a['statement']}" for a in self.assumptions) or "  (none yet)"))
        parts.append(
            "\nWork your question now. Record ONE claim and stop — a fresh analyst will "
            "be given this same investigation and everything you recorded, and will "
            "carry it on. If the investigation is already finished, call mark_done "
            "instead." if one_claim else "\nWork your question now.")
        return "\n".join(parts)

    def _next_tip(self, last: str | None) -> dict | None:
        """Which pending item to grow next.

        Depth-first off the branch that just finished: a follow-up is at its most
        valuable while the findings that provoked it are the freshest thing in the
        ledger. Falls back to agenda order once a branch is exhausted."""
        pending = [a for a in self.agenda if a["status"] == "pending"]
        if not pending:
            return None
        if last is not None:
            for a in pending:
                if a["id"] == last:
                    return a        # same investigation, fresh context (#61)
            for a in pending:
                if a.get("parent") == last:
                    return a
        return pending[0]

    async def explore_hyphal(self, tip_steps: int = 16,
                             max_total_steps: int | None = None,
                             one_claim: bool = False,
                             max_claims_per_item: int = 6,
                             live_verify: bool = False,
                             epochs: int = 1) -> bool:
        """Explore by branching growth rather than one long-lived session (#58, #61).

        Germinate an agenda, then grow short-lived tips, each seeded from the shared
        ledger and discarded when its work is done. Follow-ups branch: a tip that finds
        something surprising adds the question and a fresh tip is grown for it.

        With ``live_verify`` the judge runs alongside on its own host, so verdicts land
        in the ledger while the analyst is still working and later tips are seeded
        knowing which earlier claims held. With ``epochs`` > 1 the whole thing repeats:
        a new agenda is proposed once the last one is worked out, written by a
        germinator that can now see what survived rather than guessing before any data
        was seen.

        Returns True only when every agenda item was actually worked through, exactly
        as ``explore`` does — a step budget is still a budget, and an item left
        standing is still reported as outstanding."""
        self.exploration = "hyphal"
        budget = self.max_steps if max_total_steps is None else max_total_steps
        q: asyncio.Queue | None = asyncio.Queue() if live_verify else None
        self._verify_queue = q
        judge = asyncio.create_task(self._verify_stream(q)) if q is not None else None
        used = 0
        try:
            for epoch in range(max(1, epochs)):
                if used >= budget:
                    break
                grew = await self._grow_epoch(epoch, budget, used, tip_steps,
                                              one_claim, max_claims_per_item)
                used += grew
                if not self.agenda:
                    return False
                # Sweep at the END OF EVERY EPOCH, before any exit. Under claim-sized
                # contexts nothing else ever asks what was assumed: a tip dies the
                # moment it banks a claim, and no run has yet reached a final sweep.
                # Zero assumptions were recorded across an entire evening of runs.
                if used < budget:
                    used += await self._sweep_assumptions(
                        max_steps=min(tip_steps, budget - used))
                if not any(a["status"] == "pending" for a in self.agenda) and \
                        epoch + 1 >= epochs:
                    break
        finally:
            self._verify_queue = None
            if judge is not None:
                q.put_nowait(None)          # sentinel lands behind every queued claim
                await judge
        await self._emit("hyphal_done", {"steps": used, "tips": len(self.agenda),
                                         "claims": len(self.ledger)})
        return bool(self.agenda) and not any(
            a["status"] in ("pending", "in_progress", "interrupted") for a in self.agenda)

    async def _grow_epoch(self, epoch: int, budget: int, spent: int, tip_steps: int,
                          one_claim: bool, max_claims_per_item: int) -> int:
        """One epoch: propose an agenda, then work it to exhaustion. Returns steps used.

        Epoch 0 germinates from the data alone. Later epochs germinate from the data
        AND the ledger, so the agenda stops being a single guess made before anything
        was known — the questions worth asking second are mostly the ones the first
        round turned up."""
        used = await self._germinate(max_steps=min(tip_steps, budget - spent),
                                     epoch=epoch)
        if not self.agenda:
            await self._emit("germinate_failed", {"steps": used, "epoch": epoch})
            return used
        last: str | None = None
        while spent + used < budget:
            item = self._next_tip(last)
            if item is None:
                break
            used += await self._grow_tip(
                item, max_steps=min(tip_steps, budget - spent - used), one_claim=one_claim)
            # An investigation that keeps banking claims would spawn successors forever.
            # The cap closes it; it is a budget, so it closes as `interrupted`, not done.
            if (one_claim and item["status"] == "pending"
                    and sum(c.get("investigation") == item["id"] for c in self.ledger)
                    >= max_claims_per_item):
                item["status"] = "interrupted"
                await self._emit("tip_capped", {"id": item["id"],
                                                "claims": max_claims_per_item})
            last = item["id"]
        return used

    async def _verify_stream(self, q: asyncio.Queue) -> int:
        """Judge claims as they are recorded, while the analyst keeps exploring (#61).

        This runs on the VERIFY host, which is not the host the analyst occupies, so
        the two never contend for a card — the judge's GPU used to sit at 0% for the
        whole exploration and then the analyst's would idle in turn.

        A failure here must never take the exploration down with it: an unjudged claim
        is picked up by the end-of-run pass, whereas a crashed run loses everything."""
        judged = 0
        while True:
            cid = await q.get()
            try:
                if cid is None:
                    return judged
                claim = next((c for c in self.ledger if c["id"] == cid), None)
                if claim is not None and not claim.get("verdict_round1"):
                    await self._verify_one(claim)
                    judged += 1
            except Exception as e:                       # noqa: BLE001 — see docstring
                await self._emit("verify_error", {"claim": cid, "error": str(e)})
            finally:
                q.task_done()

    async def _germinate(self, max_steps: int = 6, epoch: int = 0) -> int:
        """Propose the agenda in a context that can do nothing else. Given
        ``run_analysis`` this phase starts the investigation it was meant to plan.

        From epoch 1 on, the germinator is shown what the last round produced and how
        it fared. An agenda written after seeing the data beats one written before it,
        and a refuted claim is often a better lead than a verified one — it says the
        question was worth asking and the approach was wrong."""
        briefing = await self.data_briefing()
        seed = (f"{briefing}\n\n" if briefing else "") + (
            "Propose the agenda of microbial-ecology analyses and hypothesis tests worth "
            "running on this dataset.")
        if epoch:
            worked = "\n".join(
                f"  [{a['status']}] {a['id']}: {a['question']}" for a in self.agenda)
            seed = (
                f"{briefing}\n\n" if briefing else "") + (
                f"This is round {epoch + 1}. Earlier rounds already investigated:\n"
                f"{worked}\n\nand recorded these claims, with how each one fared when "
                f"an independent check was run against it:\n"
                f"{self._claim_lines(self.ledger)}\n\n"
                "Propose the NEXT agenda: the questions this body of work has opened "
                "up. Go deeper rather than broader — follow the anomalies, the "
                "caveats, and the claims that did not survive (a refuted claim often "
                "means the question was right and the approach was wrong). Do NOT "
                "re-propose anything above.")
        await self._emit("germinate", {"phase": "agenda", "epoch": epoch})
        used = await self._agent_loop(
            system=GERMINATE_SYSTEM, seed=seed, tools=GERMINATE_TOOLS, max_steps=max_steps,
            nudge="Call propose_agenda with your agenda items now.",
            stop=lambda: bool(self.agenda), tag="germinate")
        if not self.agenda:
            # Germination that proposes nothing is a FAILED run, not a short one —
            # everything downstream then works an empty agenda and reports success.
            # Retry once with the looking-around tools removed: the usual failure is a
            # model spending its whole budget inspecting the data it was asked to plan
            # over, and offering it only propose_agenda takes that option away.
            used += await self._agent_loop(
                system=GERMINATE_SYSTEM,
                seed=seed + "\n\nCall propose_agenda NOW, with at least six items. Do "
                            "not inspect the data further and do not reply in prose.",
                tools=tools_for("propose_agenda"), max_steps=max_steps,
                nudge="Call propose_agenda. It is the only tool available to you.",
                stop=lambda: bool(self.agenda), tag="germinate-retry")
        # propose_agenda promotes its first item for the linear loop's benefit. Here the
        # scheduler decides what gets grown, and it only looks at pending — leaving that
        # promotion in place would strand item 1 as permanently in_progress, unworked.
        for a in self.agenda:
            if a["status"] == "in_progress":
                a["status"] = "pending"
        await self._emit("agenda", {"epoch": epoch, "items": [
            {"id": a["id"], "question": a["question"], "parent": a.get("parent")}
            for a in self.agenda]})
        return used

    async def _grow_tip(self, item: dict, max_steps: int, one_claim: bool = False) -> int:
        """Grow one tip: a fresh short context working exactly one agenda item.

        With ``one_claim`` the context dies at the CLAIM boundary rather than the
        investigation boundary — it explores, records one claim, and a successor is
        born to carry the same investigation on. Contexts stay uniformly small, state
        is durable after every claim, and a context that wedges costs one claim instead
        of a whole investigation (#61).

        The item is marked ``interrupted`` rather than ``done`` when the tip runs out of
        steps having done neither — a tip that stopped early is outstanding work, and
        faking it done is how a partial run comes to look complete."""
        item["status"] = "in_progress"
        self._active_tip = item["id"]
        before = len(self.ledger)
        recorded = lambda: len(self.ledger) > before        # noqa: E731
        await self._emit("tip", {"id": item["id"], "question": item["question"],
                                 "parent": item.get("parent"), "claims_seen": before})
        prodded = [False]

        def _prod(step: int, total: int) -> str | None:
            """Bank what you have, once. A tip spent fourteen steps and eight successful
            computations on a three-part investigation and recorded NOTHING, because it
            was still working towards a complete answer when the budget ran out. The
            instruction to record one claim and stop was already in its seed; what was
            missing was anything that noticed it hadn't."""
            if prodded[0] or not one_claim or recorded() or step < total // 2:
                return None
            prodded[0] = True
            n = len(self.computations)
            return (f"You are halfway through this investigation's budget with {n} "
                    "computations and no claim recorded. Record what you have "
                    "established so far, even if it is only part of the question — a "
                    "fresh analyst will be given this same investigation and your claim, "
                    "and can carry on from there.")

        try:
            return await self._agent_loop(
                system=TIP_SYSTEM, seed=await self._tip_seed(item, one_claim=one_claim),
                tools=TIP_TOOLS, max_steps=max_steps, prod=_prod,
                nudge=("Record the claim this investigation has reached, or call "
                       "mark_done if it is finished." if one_claim else
                       "Continue with this investigation, or call mark_done if it is finished."),
                stop=(lambda: item["status"] == "done" or (one_claim and recorded())),
                tag=item["id"])
        finally:
            if item["status"] != "done":
                # A tip that banked a claim leaves the investigation OPEN — a successor
                # picks it up. One that banked nothing simply ran out.
                item["status"] = "pending" if (one_claim and recorded()) else "interrupted"
            self._active_tip = None
            # Emitted so a watcher can tell a finished tip from an abandoned one while
            # the run is still going. Without it the two are indistinguishable until
            # the final agenda dump, hours later.
            await self._emit("tip_done", {"id": item["id"], "status": item["status"],
                                          "claims": sum(c.get("investigation") == item["id"]
                                                        for c in self.ledger)})

    async def _sweep_assumptions(self, max_steps: int = 6) -> int:
        """The one context that sees every finding at once. Several analysts each
        recorded what THEY had to assume; only this pass can see an assumption that
        exists between them."""
        if not self.ledger:
            return 0
        seed = ("Every claim on record:\n" + self._claim_lines(self.ledger)
                + "\n\nAssumptions already recorded:\n" + ("\n".join(
                    f"  {a['id']}: {a['statement']}" for a in self.assumptions) or "  (none yet)")
                + "\n\nRecord the assumptions these findings rest on.")
        await self._emit("sweep", {"claims": len(self.ledger)})
        return await self._agent_loop(
            system=SWEEP_SYSTEM, seed=seed, tools=SWEEP_TOOLS, max_steps=max_steps,
            nudge="Call record_assumption for each one, then reply DONE.", tag="sweep")

    # -- verify -----------------------------------------------------------------
    def _evidence_for(self, claim, comp_cache: dict) -> str:
        """Deterministic evidence block: re-executed computation results and re-read
        data values, WITH their labels, which is what the judge reads.

        Results are shown whole. The old claim-directed extraction existed to work
        around a 600-char prefix cap; with a model doing the judging, the labels are
        the signal ("bacteria_F: 14.61" is what makes 14.61 mean something) and
        cutting them out was the problem, not the size."""
        parts = []
        for ant in claim["antecedents"]:
            if ant in self.computations:
                good, res = comp_cache.get(ant, (False, None))
                label = self.computations[ant]["label"]
                body = json.dumps(res, default=str, indent=1)[:4000] if good else "ERROR — did not re-execute"
                parts.append(f"[{ant}] {label} (re-executed):\n{body}")
            else:
                found, val = self.data.navigate(ant)
                parts.append(f"[{ant}] (data path) = "
                             + (json.dumps(val, default=str)[:1500] if found else "NOT FOUND"))
        return "\n\n".join(parts) or "(no antecedents cited)"

    async def _judge(self, system: str, user: str, model: str,
                     assertions: list[dict]) -> dict:
        """Grade every assertion in one call. Returns
        ``{per: {label: verdict}, notes: {label: str}, by, raw}``."""
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        resp = await self._chat("verify", msgs, model=model,
                                max_tokens=self.max_tokens, temperature=0.0)
        cut = getattr(resp.choices[0], "finish_reason", None) == "length"
        text = _strip_think(resp.choices[0].message.content or "")
        if cut and "ASSERTION" not in text.upper():
            # It spent the whole budget reasoning — re-reading the instructions back to
            # itself — and never reached the output format. Five of six claims in one
            # run were graded by nobody because of this. Asking again for the LINES
            # ONLY, with its own working already on the table, costs one short call.
            retry = await self._chat(
                "verify",
                msgs + [{"role": "assistant", "content": resp.choices[0].message.content or ""},
                        {"role": "user", "content":
                         "You ran out of room before giving the verdicts. Output ONLY the "
                         "result lines now — one per assertion, nothing before or after:\n"
                         "ASSERTION <label>: SUPPORTED|CONTRADICTED|NOT_ADDRESSED|AGREES|"
                         "DIFFERS — <evidence value, or why>"}],
                model=model, max_tokens=min(1200, self.max_tokens), temperature=0.0)
            retry_text = _strip_think(retry.choices[0].message.content or "")
            if "ASSERTION" in retry_text.upper():
                text, cut = retry_text, False
        per, notes = {}, {}
        for m in re.finditer(
                r"ASSERTION\s+(.+?)\s*:\s*(SUPPORTED|CONTRADICTED|NOT[_ ]ADDRESSED|AGREES|DIFFERS)"
                r"\s*(?:[—\-–]\s*(.*))?$", text, re.IGNORECASE | re.MULTILINE):
            label, verdict, note = m.group(1).strip(), m.group(2).upper().replace(" ", "_"), m.group(3)
            per[label] = {"SUPPORTED": "supported", "CONTRADICTED": "contradicted",
                          "NOT_ADDRESSED": "not_addressed", "AGREES": "agrees",
                          "DIFFERS": "differs"}[verdict]
            notes[label] = (note or "").strip()[:200]
        # Map the judge's labels back onto ours FORGIVINGLY. Judges echo the assertion
        # they are grading ("ASSERTION n_core = 14: SUPPORTED"), and an exact-match filter
        # discarded a correct grade as if nothing had been graded at all — six claims came
        # back unverifiable off the back of six SUPPORTED verdicts.
        known = [a["label"] for a in assertions]
        mapped, mapped_notes = {}, {}
        for raw_label, verdict in per.items():
            k = _match_label(raw_label, known)
            if k and k not in mapped:
                mapped[k] = verdict
                mapped_notes[k] = notes.get(raw_label, "")
        if not mapped and per:
            if len(known) == 1 and len(per) == 1:
                # One assertion, one verdict, label unrecognisable: it is about that one.
                mapped[known[0]] = next(iter(per.values()))
                mapped_notes[known[0]] = next(iter(notes.values()), "")
            else:
                # The judge decomposed further than we did — a finer grading of the same
                # claim is more information, not less. Take its labels.
                mapped, mapped_notes = dict(per), dict(notes)
        # A judge that ran out of tokens mid-reasoning emitted no ASSERTION lines at
        # all, and the empty grade rolled up to `unverifiable` — reporting a JUDGE
        # failure as a property of the CLAIM. That is the one lie this subsystem
        # exists to prevent, so say which happened.
        return {"per": mapped, "notes": mapped_notes, "by": model,
                "raw": text.strip()[:600], "truncated": cut,
                "graded_nothing": not mapped,
                "unmatched": [l for l in per if not _match_label(l, known)]}

    @staticmethod
    def _roll_up(per: dict, good: str, bad: str) -> str:
        """Claim-level grade from per-assertion outcomes: all-good, none-good, or mixed."""
        vals = [v for v in per.values() if v != "not_addressed"]
        if not vals:
            return "unaddressed"
        if all(v == good for v in vals):
            return "all"
        if any(v == good for v in vals) and any(v == bad for v in vals):
            return "mixed"
        return "none"

    async def verify(self) -> None:
        """Re-derive each claim's evidence, then have the verifier JUDGE it.

        The mechanics stay deterministic — every cited computation is re-executed
        (once, cached) and every data path re-read, so the evidence a verdict rests
        on is reproducible. The judgment is the model's, because every failure the
        numeric matcher produced was semantic: it could not tell a finding from a
        parameter (a 999-permutation count, a >=50%% threshold, an n=44), nor
        silence from contradiction, nor see that ``bacteria_F: 14.61`` IS the F the
        claim asserts. Two independent models once reproduced a claim exactly and
        the matcher graded it overturned.

        Claims already judged — by the live verifier running alongside exploration
        (#61) — are skipped, so this is a drain of whatever is left rather than a
        second opinion on work already done."""
        for c in self.ledger:
            if not c.get("verdict_round1"):
                await self._verify_one(c)

    def _resolved_antecedents(self, claim: dict, depth: int = 4) -> list[str]:
        """A claim's antecedents with any CLAIM references replaced by what those
        claims rest on.

        Claim-sized contexts (#61) make claim-to-claim citation the norm: a successor
        is handed its predecessor's findings and builds on them. The DAG has always
        modelled that edge, but verification resolved antecedents only against
        computations and data paths — so a claim citing `k4` found no evidence and was
        graded `unverifiable`, which reads as a fault in the claim rather than a gap in
        the checker."""
        by_id = {k["id"]: k for k in self.ledger}
        out, seen, queue = [], set(), list(claim.get("antecedents") or [])
        while queue and depth >= 0:
            nxt = []
            for ant in queue:
                if ant in seen:
                    continue
                seen.add(ant)
                if ant in by_id:
                    # A claim is never itself evidence — follow it to what it rests on.
                    # Self-reference and cycles simply contribute nothing, which `seen`
                    # already terminates.
                    if ant != claim.get("id"):
                        nxt.extend(by_id[ant].get("antecedents") or [])
                else:
                    out.append(ant)
            queue, depth = nxt, depth - 1
        return out

    async def _rerun(self, cid: str) -> tuple[bool, Any]:
        """Re-execute a cited computation, once per run. The cache is per-RESEARCHER
        rather than per-verify-pass so a claim judged live during exploration and the
        end-of-run pass never pay for the same computation twice."""
        if cid not in self._comp_cache:
            self._comp_cache[cid] = await self.executor.run(self.computations[cid]["code"])
        return self._comp_cache[cid]

    async def _verify_one(self, c: dict) -> None:
        """Re-derive one claim's evidence and judge it. Split out of ``verify`` so a
        claim can be judged the moment it is recorded, on the machine that is not
        exploring, instead of waiting for the whole run to finish (#61)."""
        c["antecedents"] = _norm_antecedents(c["antecedents"])
        checked, have_evidence = [], False
        for ant in self._resolved_antecedents(c):
            if ant in self.computations:
                good, _ = await self._rerun(ant)
                have_evidence = have_evidence or good
                checked.append(f"{ant}:{'run' if good else 'err'}")
            else:
                found, _ = self.data.navigate(ant)
                have_evidence = have_evidence or found
                checked.append(f"{ant}:{'ok' if found else 'nopath'}")
        c["checked"] = checked

        if not have_evidence:
            c["verdict"] = "unverifiable"
        elif self.client_for("verify").client is None:
            # No model: the evidence is still re-derived (that half stays
            # deterministic), but nothing can judge it. Leave the prior grade
            # rather than inventing one.
            c["method"] = "evidence-only"
            await self._emit("verify", {"claim": c["id"], "verdict": "not judged"})
            return
        else:
            assertions = c.setdefault(
                "assertions", _norm_assertions(None, c.get("value", "")))
            j = await self._judge(
                JUDGE_SYSTEM,
                f"CLAIM: {c['statement']}\n\nASSERTIONS TO GRADE:\n"
                f"{_format_assertions(assertions)}\n\n"
                f"PARAMETERS (context — do not grade): "
                f"{json.dumps(c.get('parameters') or {}, default=str)}\n\n"
                f"INDEPENDENT EVIDENCE (re-executed from the raw data):\n"
                f"{self._evidence_for(c, self._comp_cache)}",
                self.verify_model, assertions)
            c["judgment"] = j
            c["assertion_verdicts"] = j["per"]
            if j.get("graded_nothing"):
                # Not judged at all. Leave it ungraded rather than calling it
                # unverifiable: the end-of-run pass picks up anything without a
                # verdict_round1, so it gets another chance instead of a false one.
                c["method"] = "judge-failed"
                await self._emit("judge_failed", {"claim": c["id"], "by": j.get("by"),
                                                  "truncated": bool(j.get("truncated"))})
                return
            roll = self._roll_up(j["per"], "supported", "contradicted")
            c["verdict"] = {"all": "verified", "mixed": "partial", "none": "refuted",
                            "unaddressed": "unverifiable"}[roll]
            # Which assertions actually failed — what the writer must not restate,
            # and what a reader needs in order to judge the rest.
            c["unsupported_numbers"] = [
                f"{lbl}={next((a['value'] for a in assertions if a['label'] == lbl), '')}"
                for lbl, v in j["per"].items() if v == "contradicted"]
        c["method"] = "judged"
        # `partial` counts as reproduced: its findings came back out of its own
        # antecedents with one element in question, which is a different thing from
        # a claim the antecedents never produced at all.
        c["reproduced"] = c["verdict"] in ("verified", "partial")
        c["verdict_round1"] = c["verdict"]
        await self._emit("verify", {"claim": c["id"], "verdict": c["verdict"]})

    # -- clean-room replication -------------------------------------------------
    def _replication_candidates(self) -> list[dict]:
        """Which claims are worth an independent re-derivation, best first.

        Only claims that survived reproduction and rest on a COMPUTATION: a data-path
        read is already trivially checkable, and re-deriving a refuted claim tells us
        nothing we don't know. Insights (pattern/anomaly) go first because they are
        the claims a reader will lean on and the ones most likely to be subtly wrong."""
        rank = {"anomaly": 0, "pattern": 1, "quality_caveat": 2, "observation": 3}
        eligible = [c for c in self.ledger
                    if c.get("verdict") in ("verified", "partial")
                    and any(a in self.computations for a in c.get("antecedents", []))]
        return sorted(eligible, key=lambda c: rank.get(c.get("kind"), 9))

    async def _replicate_claim(self, claim: dict, max_attempts: int = 3,
                               round_no: int = 2, model: str | None = None,
                               temperature: float = 0.3, judge: bool = True) -> dict:
        """Have an independent analyst re-derive one claim from the raw data.

        The analyst sees the claim and the data dictionary — never the original code,
        its result, or its label. It writes its own code, which we run in the SAME
        sandbox; a failing run comes back with the error so it can fix its own bug
        (that is debugging its own approach, not learning the original's).

        Honest about what "clean room" means here: the claim's own wording can name a
        method ("Spearman rho between depth and richness"), and it must — you cannot
        test a claim without knowing what it asserts. What is withheld is the
        implementation: which columns, what filtering, how the join was done. That is
        where the errors re-running the same code can never catch actually live."""
        brief = await self.data_briefing()
        msgs = [{"role": "system", "content": REPLICATE_SYSTEM},
                {"role": "user", "content": (f"{brief}\n\n" if brief else "")
                                            + f"CLAIM: {claim['statement']}\n"
                                            f"CLAIMED VALUE: {claim['value']}\n\n"
                                            "Re-derive this independently."}]
        model = model or self.replicate_model
        rep: dict[str, Any] = {"round": round_no, "by": model, "attempts": 0}
        for attempt in range(max_attempts):
            r = await self._chat("adjudicate" if round_no >= 3 else "replicate", msgs,
                                 model=model, temperature=temperature, max_tokens=3000)
            text = _strip_think(r.choices[0].message.content or "")
            code = _extract_code(text)
            rep["attempts"] = attempt + 1
            if not code:
                rep["error"] = "no code block in the analyst's reply"
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Reply with ONE fenced python block "
                                                     "that assigns `result`, then the SUPPORTS line."}]
                continue
            ok, res = await self.executor.run(code)
            if not ok:
                rep["error"] = str(res)
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"Your code failed: {res}\nFix it and "
                                                     "re-send the full block."}]
                continue
            if not _usable_derivation(res):
                # Code ran but computed nothing comparable. Spend a retry telling the
                # analyst why, rather than banking a "disagreement" that is really a
                # failed selection or join.
                rep["error"] = "result contained no finite numbers (empty selection or failed join?)"
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content":
                          f"Your code ran but produced {json.dumps(_jsonify(res), default=str)[:200]} "
                          "— no finite numbers, so it tested nothing. Likely an empty filter, a failed "
                          "join, or a group that matched no rows. Check your selections against the "
                          "shapes above and re-send the full block."}]
                continue
            m = re.search(r"SUPPORTS:\s*(YES|NO|INCONCLUSIVE)", text.upper())
            analyst = {"YES": "supports", "NO": "contradicts"}.get(
                m.group(1) if m else "", "inconclusive")
            rep.update({
                "code": code, "result": _jsonify(res, cap=MODEL_VIEW_CAP), "usable": True,
                "analyst": analyst,
                "reasoning": re.sub(r"```.*?```", "", text, flags=re.DOTALL).strip()[:400],
                "error": None,
            })
            # The verdict is a judgment over the analyst's LABELLED result, not a
            # digit hunt: an independent analyst reports its findings, not the
            # claim's parameters, and only a different VALUE for the same quantity
            # is disagreement. Deferred when the judge runs in its own phase — on a
            # single-GPU host the analyst and the judge cannot both be resident.
            if judge:
                await self._judge_replication(claim, rep)
            return rep
        rep["agrees"] = False
        rep["analyst"] = "inconclusive"
        return rep

    @staticmethod
    def _disputed_assertions(reps: list[dict]) -> dict:
        """Per assertion, how many independent derivations agreed and how many differed,
        with what each of the dissenters got."""
        tally: dict[str, dict] = {}
        for r in reps:
            for label, v in (r.get("assertion_verdicts") or {}).items():
                t = tally.setdefault(label, {"agrees": 0, "differs": 0, "got": []})
                if v == "agrees":
                    t["agrees"] += 1
                elif v == "differs":
                    t["differs"] += 1
                    note = (r.get("judgment") or {}).get("notes", {}).get(label, "")
                    if note:
                        t["got"].append(note)
        return tally

    def _resolve_verdict(self, claim: dict) -> str:
        """Grade a claim from ALL the evidence gathered about it.

        Round 1 asks "does this come back out of its OWN cited antecedents?"
        (reproduction). Rounds 2+ ask "does anyone else, from the raw data, get the
        same answer?" (replication). Both are graded per ASSERTION, so a claim keeps
        credit for the findings that held:

          every addressed assertion reproduced         -> replicated
          some reproduced, some did not                -> partial   (the usual shape)
          none reproduced, one independent             -> disputed  (a stand-off)
          none reproduced, 2+ independents CONCURRING  -> overturned
          none reproduced, 2+ independents disagreeing
            with each other as well                    -> contested (unstable quantity)
          claim never reproduced from its antecedents,
            but an independent got it                  -> replicated + antecedent_mismatch
        """
        reps = [r for r in claim.get("replications", [])
                if r.get("usable", bool(r.get("code")))]
        if not reps:
            return claim["verdict"]
        rolls = [r.get("roll") for r in reps]
        tally = self._disputed_assertions(reps)
        claim["assertion_replication"] = tally
        reproduced = claim.get("reproduced")
        if reproduced is None:
            reproduced = claim.get("verdict") in ("verified", "partial", "replicated")

        # Three ways an assertion can land across independent derivations.
        agreed  = [l for l, t in tally.items() if t["agrees"] and not t["differs"]]
        differed = [l for l, t in tally.items() if t["differs"] and not t["agrees"]]
        split   = [l for l, t in tally.items() if t["differs"] and t["agrees"]]

        # An assertion that failed INDEPENDENT re-derivation must not be restated in the
        # prose either. Previously only round-1 contradictions were withheld, so a claim
        # that passed verification and then lost a value to replication handed the writer
        # an empty do-not-state list.
        by_label = {a["label"]: a.get("value", "") for a in (claim.get("assertions") or [])}
        claim["unsupported_numbers"] = sorted(set(
            (claim.get("unsupported_numbers") or [])
            + [f"{l}={by_label.get(l, '')}" for l in differed + split]))

        if not reproduced:
            # Correct science, broken bookkeeping: its own antecedents do not produce it
            # but an independent analyst does. Worth rescuing, worth flagging.
            if agreed:
                claim["antecedent_mismatch"] = True
                return "replicated"
            return "refuted"
        if split:
            # Analysts disagree with EACH OTHER about this quantity, which says the
            # quantity is unstable rather than that the claim is wrong.
            return "contested"
        if agreed and not differed:
            return "replicated"
        if agreed and differed:
            return "partial"                       # the findings that held, keep
        if not differed:
            return claim["verdict"]                # nobody addressed anything
        # Nothing reproduced. Do the dissenters concur, or is each wrong its own way?
        n_models = len({r.get("by") for r in reps if r.get("by")})
        if len(reps) == 1 or n_models < 2:
            if n_models < 2 and len(reps) > 1:
                claim["correlated_analysts"] = True
            return "disputed"
        # Concurrence is judged on what the dissenters GOT, not merely that they dissented:
        # two analysts landing on 0 and 72 for the same quantity means the quantity is
        # ill-defined, not that the claim is wrong.
        # Compare what the dissenters GOT, numerically — two analysts landing on 0 and
        # 72 for the same quantity means it is ill-defined, not that the claim is wrong.
        def _concur(got: list[str]) -> bool:
            nums = [n for n in (_first_number(g) for g in got) if n is not None]
            if len(nums) < 2:
                return True            # nothing to contradict each other with
            return all(_close(nums[0], n) for n in nums[1:])
        concur = all(_concur(t["got"]) for t in tally.values() if t["differs"])
        return "overturned" if concur else "contested"

    async def _run_round(self, claim: dict, round_no: int, model: str | None = None,
                         temperature: float = 0.3, judge: bool = True) -> dict:
        """One independent derivation appended to the claim's evidence record."""
        rep = await self._replicate_claim(claim, round_no=round_no, model=model,
                                          temperature=temperature, judge=judge)
        claim.setdefault("replications", []).append(rep)
        await self._emit("replicate", {"claim": claim["id"], "round": round_no,
                                       "agrees": rep.get("agrees"),
                                       "analyst": rep.get("analyst")})
        return rep

    def _clear_replications(self) -> None:
        """Drop evidence from a PREVIOUS replication pass.

        Rounds append, so re-running replication over a saved ledger silently stacked
        a second pass on top of the first — one real run ended up with rounds
        [2, 3, 2, 3] on a single claim, letting superseded derivations (written before
        a prompt fix, by a since-changed model) keep voting alongside current ones.
        A new pass supersedes the old one; the round-1 grade is restored so verdicts
        do not carry over either."""
        for c in self.ledger:
            if c.get("replications"):
                c["replications"] = []
                for k in ("consensus_numbers", "antecedent_mismatch", "correlated_analysts"):
                    c.pop(k, None)
                if c.get("verdict_round1"):
                    c["verdict"] = c["verdict_round1"]

    async def _judge_replication(self, claim: dict, rep: dict) -> dict:
        """Judge one derivation against the claim, and record the outcome on it."""
        assertions = claim.get("assertions") or _norm_assertions(None, claim.get("value", ""))
        j = await self._judge(
            REPLICATE_JUDGE_SYSTEM,
            f"CLAIM: {claim['statement']}\n\nASSERTIONS TO GRADE:\n"
            f"{_format_assertions(assertions)}\n\n"
            f"PARAMETERS (context — do not grade): "
            f"{json.dumps(claim.get('parameters') or {}, default=str)}\n\n"
            f"THE INDEPENDENT ANALYST'S RESULT:\n"
            f"{json.dumps(rep.get('result'), default=str, indent=1)[:4000]}",
            self.verify_model, assertions)
        rep["judgment"] = j
        rep["assertion_verdicts"] = j["per"]
        roll = self._roll_up(j["per"], "agrees", "differs")
        rep["roll"] = roll
        rep["agrees"] = roll == "all"          # every addressed assertion reproduced
        return rep

    async def judge_replications(self) -> int:
        """Judge every derivation that is still unjudged, then re-grade its claim.

        Exists so the analyst and the judge can occupy the card one at a time: derive
        the whole round with one model, swap, judge the whole round with another.
        Returns how many derivations were judged."""
        n = 0
        for c in self.ledger:
            touched = False
            for rep in c.get("replications") or []:
                if rep.get("usable") and "judgment" not in rep:
                    await self._judge_replication(c, rep)
                    n, touched = n + 1, True
            if touched:
                c["verdict"] = self._resolve_verdict(c)
        return n

    async def replicate(self, max_claims: int = 12, fresh: bool = True,
                        defer_judgment: bool = False) -> int:
        """Round 2 — the clean-room pass: independently re-derive the strongest
        claims (#50).

        ``verify()`` re-runs the SAME code, so it establishes reproducibility — it
        structurally cannot catch a wrong axis, an unnormalized comparison or a
        misjoined table, because the bug re-appears identically. This pass answers
        the question the ledger actually promises: can someone else, working from
        the raw data alone, get the same number?

        Additive: with replication off, every verdict is exactly what it was.
        ``fresh`` (the default) discards any previous pass first — pass False only to
        deliberately accumulate rounds across passes.
        Returns the number of claims that got a usable independent derivation."""
        if fresh:
            self._clear_replications()
        done = 0
        for claim in self._replication_candidates()[:max_claims]:
            rep = await self._run_round(claim, round_no=2, judge=not defer_judgment)
            if not rep.get("usable"):
                continue                       # the analyst never produced runnable code
            done += 1
            if not defer_judgment:
                claim["verdict"] = self._resolve_verdict(claim)
        return done

    def _adjudication_candidates(self) -> list[dict]:
        """Claims left in doubt after rounds 1-2: a stand-off between the original
        and one independent analyst (``disputed``), or a claim its own antecedents
        never produced (``refuted``). Both deserve a casting vote rather than being
        left as a shrug. ``contested`` is excluded — evidence that is already
        mutually inconsistent is not settled by adding more of it; that instability
        is the finding."""
        return [c for c in self.ledger
                if c.get("verdict") in ("disputed", "refuted")
                and any(a in self.computations for a in c.get("antecedents", []))]

    async def adjudicate(self, max_claims: int = 12, defer_judgment: bool = False) -> int:
        """Round 3 — break the tie with evidence rather than opinion.

        When rounds 1 and 2 disagree, the honest move is not to have a third model
        arbitrate between two accounts it can read; that is judgment stacked on
        judgment. It is to go and derive the quantity a THIRD independent time,
        under the same blinding, and see which way the evidence falls. Two
        independent derivations that land together outweigh one that stands alone —
        and when all three disagree, the claim is ``contested``, which is a real
        finding about the analysis, not a failure to decide.

        For a ``refuted`` claim this round is the rescue path: if an independent
        analyst does get the claimed number, the science was right and only the
        cited antecedents were wrong (``antecedent_mismatch``).

        The third analyst runs at a slightly higher temperature so that even the
        same model reaches for a different approach rather than retracing its own.
        Returns the number of claims adjudicated."""
        done = 0
        for claim in self._adjudication_candidates()[:max_claims]:
            rep = await self._run_round(claim, round_no=3, model=self.adjudicate_model,
                                        temperature=0.5, judge=not defer_judgment)
            if not rep.get("usable"):
                continue
            done += 1
            if not defer_judgment:
                claim["verdict"] = self._resolve_verdict(claim)
        return done

    # -- DAG --------------------------------------------------------------------
    def build_dag(self) -> dict:
        """The claim→antecedent provenance DAG for this run's current state,
        including the agenda lineage that motivated each claim."""
        return build_dag(self.computations, self.ledger, self.agenda)

    # -- write ------------------------------------------------------------------
    async def write_results(self, verified: list[dict] | None = None,
                            partial: list[dict] | None = None) -> str:
        """Write the Results prose from verified claims, plus any PARTIALLY SUPPORTED
        claim with its unbacked values withheld (WRITE_SYSTEM). When either list is
        omitted it is taken from this run's ledger. The result is cached on
        ``self.results_prose`` so a subsequent ``snapshot()`` includes it.

        A partial claim is a finding the evidence backs whose specific numbers it does
        not; dropping the whole claim threw away real science, so the writer gets it
        with an explicit do-not-state list instead (#48)."""
        if verified is None:
            # "replicated" is verified PLUS an independent re-derivation — strictly
            # stronger, so it belongs in the same block. "disputed" deliberately does
            # not: two derivations that disagree must not be asserted as fact.
            verified = [c for c in self.ledger
                        if c.get("verdict") in ("verified", "replicated")]
        if partial is None:
            partial = [c for c in self.ledger if c.get("verdict") == "partial"]
        claims_txt = "\n".join(
            f"- [{c['kind']}] {c['statement']} (value={c['value']})" for c in verified)
        partial_txt = "\n".join(
            f"- [{c['kind']}] {c['statement']} (value={c['value']})\n"
            f"    DO NOT STATE these unsupported values: "
            f"{', '.join(str(n) for n in c.get('unsupported_numbers') or []) or '(unspecified — avoid its numbers)'}"
            for c in partial)
        sg = self.data.study or {}
        study = (f"Study: {sg.get('title', '(unknown)')} — {sg.get('study_name', '')} "
                 f"({sg.get('bioproject', '')}), {sg.get('platform', '')}.")
        user = f"{study}\n\nVERIFIED CLAIMS:\n{claims_txt}\n"
        if self.assumptions:
            # The agent is made to surface what it could not confirm; dropping that at
            # the writing step is exactly where candour matters most (#50).
            user += "\nASSUMPTIONS THIS ANALYSIS RESTS ON (not findings — qualify the "
            user += "claims that depend on them):\n" + "\n".join(
                f"- {a['statement']}" + (f" (why: {a['why']})" if a.get("why") else "")
                for a in self.assumptions) + "\n"
        if partial_txt:
            user += ("\nPARTIALLY SUPPORTED CLAIMS (report the finding, withhold the listed "
                     f"values):\n{partial_txt}\n")
        msgs = [{"role": "system", "content": WRITE_SYSTEM},
                {"role": "user", "content": user + "\nWrite the Results section."}]
        content = ""
        for mt in (6000, 12000):
            r = await self._chat("write", msgs, model=self.write_model,
                                 temperature=0.4, max_tokens=mt)
            content = _strip_think(r.choices[0].message.content or "")
            if content.strip():
                break
        self.results_prose = content
        self.results_prose_by = self.write_model       # stamp the writer
        return content

    # -- run summary ------------------------------------------------------------
    def run_summary(self, completed: bool | None = None) -> dict:
        """The ``run`` block (completed + investigation counts + the roster of models
        that contributed) for the snapshot/ledger. ``models`` is every distinct model
        that recorded a claim, wrote a computation, judged one, or wrote the prose —
        so a run continued ("keep digging") by a different model attributes truthfully."""
        done = sum(a["status"] == "done" for a in self.agenda)
        if completed is None:
            completed = bool(self.agenda) and all(a["status"] == "done" for a in self.agenda)
        models = {c.get("by") for c in self.ledger}
        models |= {(c.get("judgment") or {}).get("by") for c in self.ledger}
        models |= {c.get("by") for c in self.computations.values()}
        models.add(self.results_prose_by)
        replicated = [c for c in self.ledger if c.get("replications")]
        for c in self.ledger:
            models |= {r.get("by") for r in (c.get("replications") or [])}
        return {"completed": completed, "investigations_done": done,
                # How the exploration was run: one long-lived session, or branching
                # short-lived tips (#58). The two produce very different ledgers.
                "exploration": self.exploration,
                # Refused claims are otherwise invisible: a claimant that cannot produce
                # checkable assertions just looks unproductive, which is a different
                # problem with a different fix.
                "claims_refused": self.refused_claims,
                # What analysts reached for and did not have, most-wanted first.
                "packages_requested": {
                    p: sum(r["package"] == p for r in self.package_requests)
                    for p in sorted({r["package"] for r in self.package_requests},
                                    key=lambda p: -sum(r["package"] == p
                                                       for r in self.package_requests))},
                "investigations_total": len(self.agenda),
                # Clean-room pass (#50): how many claims a second analyst re-derived
                # from the raw data, and how many of those agreed.
                "replication_attempted": len(replicated),
                "replication_rounds": sum(len(c.get("replications") or []) for c in self.ledger),
                "replication_agreed": sum(c.get("verdict") == "replicated" for c in self.ledger),
                "replication_disputed": sum(c.get("verdict") == "disputed" for c in self.ledger),
                "replication_overturned": sum(c.get("verdict") == "overturned" for c in self.ledger),
                "replication_contested": sum(c.get("verdict") == "contested" for c in self.ledger),
                "adjudicated": sum(any(r.get("round") == 3 for r in (c.get("replications") or []))
                                   for c in self.ledger),
                "models": sorted(m for m in models if m)}

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
                # Values a PARTIAL adjudication could not back — the viewer strikes
                # them, and the writer is forbidden from restating them.
                "unsupported_numbers": c.get("unsupported_numbers") or [],
                # Independent re-derivations (#50), round 2 onward: each analyst's own
                # code, its result, and whether it agreed. A list because a disputed
                # claim goes to a third round.
                "replications": c.get("replications") or [],
                # Per-assertion outcomes: which quantities held and which were disputed,
                # so a reader can see WHAT survived rather than only whether the bundle did.
                "assertions": c.get("assertions") or [],
                "parameters": c.get("parameters") or {},
                "assertion_verdicts": c.get("assertion_verdicts") or {},
                "assertion_replication": c.get("assertion_replication") or {},
                "antecedent_mismatch": c.get("antecedent_mismatch", False),
                # Set when rounds 2/3 shared a model: their agreement is correlated,
                # not independent, so it must not read as a settled conclusion.
                "correlated_analysts": c.get("correlated_analysts", False),
                "reproduced": c.get("reproduced"),
                # Per-artifact attribution: the model that recorded the claim, and
                # and the model that judged it.
                "by": c.get("by"), "judged_by": (c.get("judgment") or {}).get("by"),
            }
            if c.get("judgment"):
                entry["judgment"] = c["judgment"]
            if resolve:
                entry["resolved"] = [_resolve(a) for a in c["antecedents"]]
            claims.append(entry)
        return {
            "claims": claims,
            "assumptions": self.assumptions,
            "computations": self.computations,
            "agenda": self.agenda,
            "dag": self.build_dag(),
            "results_prose": results_prose,
            "results_prose_by": self.results_prose_by,
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
        ar.assumptions = list(snap.get("assumptions", []))
        ar.computations = dict(snap.get("computations", {}))
        ar.agenda = list(snap.get("agenda", []))
        ar.exploration = (snap.get("run") or {}).get("exploration", "linear")
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
