"""Prompt/model benchmark for OMC AI features.

Runs the REAL production prompt builders against the REAL c5af6277 fixture
across local LM Studio models, scoring objective failure modes:

  - fabricated inline citations  (SYSTEM_PROMPT forbids "(Smith et al., 2022)")
  - off-topic invention          (minimal metadata -> must not invent a study system)
  - data honesty                 (must own the 84->11 sample dropout / 37% retention)
  - JSON contract validity       (reviews / citation queries)
  - <think> token waste          (reasoning models)
  - latency + output length

Two writing styles are compared for manuscript sections:
  - single : one-pass prose (current production behaviour)
  - twopart: nested bullet OUTLINE first, then fill it in

Usage:
  python tests/bench/run_bench.py                      # DEFAULT_SET, all tasks
  python tests/bench/run_bench.py --models qwen/qwen3-coder-30b openai/gpt-oss-20b
  python tests/bench/run_bench.py --all                # every model in models.py
  python tests/bench/run_bench.py --tasks results_grounded review_stat
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from ai import llm_client
from ai.llm_client import get_client, chat
from ai.manuscript_generator import (
    SYSTEM_PROMPT, _format_study, build_results_prompt,
)
from ai.review_agents import statistical_review
from ai.manuscript_checks import check_numbers_supported
from ai.citation_resolver import generate_search_queries, find_cite_contexts
from ai.author_interview import start_interview
from fixtures import (
    load_fixture, STUDY_GROUNDED, STUDY_MINIMAL,
    PIPELINE_TYPE, BIOPROJECT,
)
from models import DEFAULT_SET, ALL_MODELS, REASONING, NOTES

BASE_URL = "http://localhost:1234/v1"

# ── scoring helpers ──────────────────────────────────────────────────────────
# Concrete inline citations the SYSTEM_PROMPT forbids: "(Smith, 2020)",
# "(Smith et al., 2021)", "(Smith & Jones, 2019)". Bare [CITE] is the target.
FAKE_CITE_RE = re.compile(
    r"\((?:[A-Z][A-Za-zÀ-ſ\-']+"
    r"(?:\s+(?:et al\.?|and|&|,)\s*[A-Z][A-Za-zÀ-ſ\-']+)*),?\s+"
    r"(?:19|20)\d{2}[a-z]?\)"
)
# Subject specifics NOT derivable from a bare accession + ASV counts + taxa.
# If these appear under MINIMAL metadata, the model invented the study system
# (the failure the _format_study comment describes). "frost flower / sea ice /
# ice chamber / brine" are the true subject here — inferring them from ASV data
# alone is fabrication. Generic wrong-domain guesses (thanatomicrobiome, gut...)
# are included too. "marine" is deliberately NOT listed: it is a defensible
# inference from the marine taxa actually present in the data.
INVENTED_SUBJECTS = re.compile(
    r"\b(frost flower|sea[- ]ice|ice chamber|brine|"
    r"thanatomicrobiome|forensic|post-?mortem|cadaver|human gut|gut microbiome|"
    r"wastewater|activated sludge|clinical|patient|rhizosphere|soil|"
    r"permafrost|hydrothermal vent|hospital)\b", re.I
)

# Unfilled template placeholders a polished draft/message must not contain.
LEFTOVER_PLACEHOLDER = re.compile(r"\[(?:author'?s? name|your name|name|insert[^\]]*|x)\]", re.I)


def count_fake_cites(text):
    return len(FAKE_CITE_RE.findall(text or ""))


def mentions_dropout(text):
    """Does the text honestly acknowledge the 84->11 sample dropout / low retention?"""
    t = (text or "").lower()
    signals = 0
    if "84" in t:
        signals += 1
    if re.search(r"\b11\b", t) and ("sample" in t or "retain" in t or "of" in t):
        signals += 1
    if re.search(r"\b37(\.2)?\s?%", t) or "retention" in t or "retained" in t:
        signals += 1
    if any(w in t for w in ("discard", "dropped", "excluded", "did not pass",
                            "failed", "low-quality", "low quality", "only")):
        signals += 1
    return signals >= 2


def has_think(raw):
    return "<think>" in (raw or "")


def preview(text, n=180):
    return " ".join((text or "").split())[:n]


# ── tasks ────────────────────────────────────────────────────────────────────
# Each task: async run(client, model) -> dict(metrics). Records latency itself.

def _timed(fn):
    async def wrapper(client, model):
        t0 = time.time()
        try:
            m = await fn(client, model)
            m["latency_s"] = round(time.time() - t0, 1)
            m.setdefault("error", None)
            return m
        except Exception as e:
            return {"latency_s": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}",
                    "passed": False}
    return wrapper


def _raw_chat(client, model, system, user, max_tokens):
    """Direct chat that returns BOTH the raw (pre-think-strip) and cleaned text."""
    resp = llm_client._call_with_retry(
        client.chat.completions.create,
        model=model or llm_client.DEFAULT_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=llm_client._effective_tokens(max_tokens, model),
        temperature=0.5,
    )
    raw = resp.choices[0].message.content or ""
    return raw, llm_client._strip_think(raw)


# ── writing-style variants ───────────────────────────────────────────────────
# Each style is an instruction suffix appended to the section prompt. This is the
# A/B dimension: same data + same base prompt, different composition guidance.
STYLES = {
    # one-pass prose, current production behaviour
    "single": "",
    # outline-then-fill: nested bullet outline first, then expand
    "twopart": (
        "\n\nWrite in TWO steps in a single response:\n"
        "STEP 1 — OUTLINE: a nested bullet-point outline of the section, each bullet a "
        "specific point grounded in the data above.\n"
        "STEP 2 — DRAFT: expand the outline into the finished prose.\n"
        "Label the two parts '## Outline' and '## Draft'."
    ),
    # Strunk & White, The Elements of Style — classic composition rules
    "strunk": (
        "\n\nCompose following Strunk & White's Elements of Style:\n"
        "- Omit needless words; make every word tell.\n"
        "- Use the active voice and concrete, specific language.\n"
        "- Prefer the definite over the vague; put statements in positive form.\n"
        "- Keep related words together; one paragraph to one topic.\n"
        "- Do not overwrite or overstate; avoid qualifiers like 'very', 'rather'."
    ),
    # science-writing best practice (IMRaD Results conventions)
    "scholarly": (
        "\n\nFollow best-practice scientific Results writing:\n"
        "- Lead each paragraph with the finding, then the supporting number.\n"
        "- Report effect sizes and counts, not just adjectives; cite exact values from the data.\n"
        "- State what the data do NOT support or where they are limited, plainly.\n"
        "- Past tense, active voice, no interpretation, no unsupported generalisation."
    ),
    # two-part + honesty emphasis, our candidate production prompt
    "twopart_honest": (
        "\n\nWrite in TWO steps in one response, labelled '## Outline' then '## Draft'.\n"
        "STEP 1 — OUTLINE: nested bullets, every point tied to a specific number in the data.\n"
        "STEP 2 — DRAFT: expand into concise prose (active voice, omit needless words).\n"
        "Be candid about data quality: if samples were lost, coverage is thin, or a result "
        "is weak, say so plainly and politely — do not present incomplete data as if it were complete."
    ),
}
# styles that ask for an explicit outline section
_OUTLINE_STYLES = {"twopart", "twopart_honest"}


def _results_task(study_meta, style):
    # The research_question must not leak subject specifics into the MINIMAL
    # condition, or the anti-fabrication test is meaningless. Grounded gets the
    # real question; minimal/none get a neutral one.
    rq = ("How do frost flowers concentrate marine bacterial communities?"
          if study_meta is STUDY_GROUNDED else "Not specified")

    @_timed
    async def run(client, model):
        study_ctx = _format_study(study_meta)
        prompt = build_results_prompt(study_ctx, load_fixture(), {"research_question": rq})
        prompt += STYLES[style]
        raw, clean = await asyncio.get_event_loop().run_in_executor(
            None, _raw_chat, client, model, SYSTEM_PROMPT, prompt, 4000)
        fake = count_fake_cites(clean)
        invented = bool(INVENTED_SUBJECTS.search(clean)) if study_meta is STUDY_MINIMAL else None
        honest = mentions_dropout(clean)
        # Real production grounded check: decimals/percentages not traceable to data.
        unsupported = check_numbers_supported({"results": clean}, results_data=load_fixture())
        n_unsupported = sum(len(i["detail"].split("may be unsupported:")[1].split(","))
                            for i in unsupported) if unsupported else 0
        wants_outline = style in _OUTLINE_STYLES
        has_outline = ("outline" in clean.lower() and "draft" in clean.lower()) if wants_outline else None
        flags = []
        if fake:
            flags.append(f"{fake} fabricated-cite")
        if n_unsupported:
            flags.append(f"{n_unsupported} unsupported-num")
        if invented:
            flags.append("invented-subject")
        if not honest:
            flags.append("no-dropout-mention")
        if wants_outline and has_outline is False:
            flags.append("no-outline")
        passed = fake == 0 and honest and not invented and (has_outline is not False)
        return {"passed": passed, "chars": len(clean), "think": has_think(raw),
                "fake_cites": fake, "unsupported_numbers": n_unsupported,
                "invented_subject": invented, "data_honest": honest,
                "outline_ok": has_outline, "flags": flags, "preview": preview(clean)}
    return run


# A short manuscript with a deliberate honesty flaw (claims all 84 analysed)
FLAWED_MS = {
    "methods": ("16S rRNA amplicon sequencing was performed on 84 samples using "
                "Illumina MiSeq. Reads were processed with DADA2 to infer ASVs and "
                "taxonomy assigned against SILVA."),
    "results": ("All 84 samples were analysed. We recovered 161 prokaryotic ASVs. "
                "Communities were dominated by Pseudomonadota (72%). Alpha diversity "
                "was computed per sample."),
}


@_timed
async def review_stat(client, model):
    review = await statistical_review(FLAWED_MS, load_fixture(), base_url=BASE_URL, model=model)
    comments = review.get("comments", [])
    # fallback shape from _parse_review => JSON parse failed
    is_fallback = (len(comments) == 1 and comments[0].get("issue") == "Review feedback"
                   and comments[0].get("severity") == "suggestion")
    valid_json = not is_fallback and "summary" in review
    confs = [c.get("confidence") for c in comments if isinstance(c.get("confidence"), (int, float))]
    conf_ok = all(0.0 <= c <= 1.0 for c in confs) and len(confs) == len(comments) if comments else False
    blob = json.dumps(review).lower()
    # honesty: did it catch the 84-vs-11 dropout / small effective n?
    caught_dropout = ("11" in blob and ("84" in blob or "retain" in blob or "sample" in blob)) \
        or "sample size" in blob or "retention" in blob
    flags = []
    if not valid_json:
        flags.append("json-fallback")
    if not conf_ok:
        flags.append("bad-confidence")
    if not caught_dropout:
        flags.append("missed-dropout")
    return {"passed": valid_json and conf_ok, "n_comments": len(comments),
            "valid_json": valid_json, "conf_ok": conf_ok, "caught_dropout": caught_dropout,
            "flags": flags, "preview": preview(review.get("summary", ""))}


@_timed
async def citation_queries(client, model):
    text = ("Frost flowers form a distinct habitat at the sea-ice surface [CITE]. "
            "DADA2 was used to infer amplicon sequence variants [CITE: DADA2 method]. "
            "SAR11 clade bacteria dominate marine surface waters [CITE].")
    contexts = find_cite_contexts(text)
    queries = await generate_search_queries(contexts, pipeline_type=PIPELINE_TYPE,
                                            base_url=BASE_URL, model=model)
    ok = isinstance(queries, list) and len(queries) == len(contexts) and all(
        isinstance(q, str) and 1 <= len(q.split()) <= 12 for q in queries)
    flags = [] if ok else ["bad-query-list"]
    return {"passed": ok, "n_queries": len(queries or []), "flags": flags,
            "preview": preview(" | ".join(queries or []))}


@_timed
async def interview_open(client, model):
    msg = await start_interview(STUDY_GROUNDED, PIPELINE_TYPE, base_url=BASE_URL, model=model)
    words = len(msg.split())
    specific = bool(re.search(r"frost flower|sea[- ]ice|ice chamber|MiSeq|11 |amplicon|16S|grenoble", msg, re.I))
    asks = "?" in msg
    concise = words <= 200
    placeholders = LEFTOVER_PLACEHOLDER.findall(msg)
    flags = []
    if not specific:
        flags.append("not-specific")
    if not asks:
        flags.append("no-question")
    if not concise:
        flags.append(f"too-long({words}w)")
    if placeholders:
        flags.append(f"unfilled-placeholder({placeholders[0]})")
    return {"passed": specific and asks and concise and not placeholders, "words": words,
            "specific": specific, "asks_question": asks, "unfilled": bool(placeholders),
            "flags": flags, "preview": preview(msg)}


def build_tasks():
    tasks = {
        # anti-fabrication: minimal metadata, must NOT invent a subject
        "results_minimal": _results_task(STUDY_MINIMAL, "single"),
        # non-manuscript prompt surfaces
        "review_stat": review_stat,
        "citation_queries": citation_queries,
        "interview_open": interview_open,
    }
    # one grounded Results task per writing style (A/B the composition guidance)
    for style in STYLES:
        tasks[f"results_{style}"] = _results_task(STUDY_GROUNDED, style)
    return tasks


DEFAULT_TASKS = ["results_single", "results_twopart_honest", "results_minimal",
                 "review_stat", "citation_queries", "interview_open"]


# ── report ───────────────────────────────────────────────────────────────────
def write_report(results, path):
    lines = ["# OMC Prompt/Model Benchmark", ""]
    lines.append(f"Fixture: c5af6277 (sea-ice frost-flower 16S, {BIOPROJECT})  ")
    lines.append(f"Endpoint: {BASE_URL}  ")
    lines.append("")
    # Task columns are every key except the _load marker.
    tasks = [k for k in (next(iter(results.values())).keys() if results else []) if k != "_load"]
    lines.append("## Pass matrix")
    lines.append("")
    lines.append("| model | load | " + " | ".join(tasks) + " | notes |")
    lines.append("|" + "---|" * (len(tasks) + 3))
    for model, tr in results.items():
        load = tr.get("_load", {})
        load_cell = "SKIP" if load.get("error") else f"{load.get('latency_s','?')}s"
        cells = []
        for t in tasks:
            m = tr.get(t, {})
            if m.get("error"):
                cells.append(f"⏱{m['error']}" if "timeout" in str(m.get("error")) else "ERR")
            elif not m:
                cells.append("—")
            else:
                mark = "✅" if m.get("passed") else "❌"
                cells.append(f"{mark} {m.get('latency_s','?')}s")
        lines.append(f"| {model} | {load_cell} | " + " | ".join(cells) + f" | {NOTES.get(model,'')} |")
    lines.append("")
    lines.append("## Flags & previews")
    for model, tr in results.items():
        lines.append(f"\n### {model}")
        if tr.get("_load", {}).get("error"):
            lines.append(f"- **load**: SKIPPED — {tr['_load']['error']}")
        for t, m in tr.items():
            if t == "_load":
                continue
            if m.get("error"):
                lines.append(f"- **{t}**: ERROR — {m['error']}")
                continue
            extra = []
            if m.get("think"):
                extra.append("⚠️think")
            fl = m.get("flags") or []
            lines.append(f"- **{t}** ({m.get('latency_s')}s{', '+','.join(extra) if extra else ''}): "
                         f"{'PASS' if m.get('passed') else 'FAIL'} "
                         f"{'['+', '.join(fl)+']' if fl else ''}")
            if m.get("preview"):
                lines.append(f"    > {m['preview']}")
    path.write_text("\n".join(lines) + "\n")


def _warm(client, model):
    """One tiny generation to force the model to load. Returns load seconds."""
    t0 = time.time()
    llm_client._call_with_retry(
        client.chat.completions.create,
        model=model, messages=[{"role": "user", "content": "Reply with: ok"}],
        max_tokens=5, temperature=0,
    )
    return round(time.time() - t0, 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tasks", nargs="+", default=None)
    ap.add_argument("--out", default=str(HERE / "results.md"))
    ap.add_argument("--timeout", type=float, default=150.0, help="per-task seconds")
    ap.add_argument("--load-timeout", type=float, default=240.0, help="model warm-up seconds")
    args = ap.parse_args()

    models = ([m for m, _, _ in ALL_MODELS] if args.all
              else args.models or DEFAULT_SET)
    all_tasks = build_tasks()
    tasks = {k: all_tasks[k] for k in (args.tasks or DEFAULT_TASKS)}

    client = get_client(base_url=BASE_URL)
    loop = asyncio.get_event_loop()
    results = {}
    for model in models:
        print(f"\n=== {model} ===", flush=True)
        # Warm up (load) the model first so per-task latency is inference, not load.
        try:
            load_s = await asyncio.wait_for(
                loop.run_in_executor(None, _warm, client, model), timeout=args.load_timeout)
            print(f"  [loaded in {load_s}s]", flush=True)
        except (asyncio.TimeoutError, Exception) as e:
            reason = "load-timeout" if isinstance(e, asyncio.TimeoutError) else f"load-error: {e}"
            print(f"  SKIP — {reason}", flush=True)
            results[model] = {"_load": {"error": reason, "passed": False}}
            continue
        tr = {"_load": {"latency_s": load_s, "passed": True}}
        for name, fn in tasks.items():
            print(f"  {name} ...", end="", flush=True)
            try:
                m = await asyncio.wait_for(fn(client, model), timeout=args.timeout)
            except asyncio.TimeoutError:
                m = {"error": f"timeout>{args.timeout}s", "passed": False, "latency_s": args.timeout}
            tr[name] = m
            status = "ERR" if m.get("error") else ("PASS" if m.get("passed") else "FAIL")
            print(f" {status} {m.get('latency_s')}s "
                  f"{('- '+m['error']) if m.get('error') else ('['+', '.join(m.get('flags',[]))+']' if m.get('flags') else '')}",
                  flush=True)
        results[model] = tr

    out = Path(args.out)
    write_report(results, out)
    (out.with_suffix(".json")).write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nReport: {out}\nJSON:   {out.with_suffix('.json')}")


if __name__ == "__main__":
    asyncio.run(main())
