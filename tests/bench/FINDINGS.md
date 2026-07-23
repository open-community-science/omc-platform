# Prompt / Model Benchmark — Findings (2026-07-22)

Local LM Studio (GV100, 32 GB), fixture **c5af6277** (sea-ice frost-flower 16S,
PRJNA1473294). Real production prompt builders; see `README.md` for the harness.
Reproduce: `python tests/bench/run_bench.py --all`.

## Model recommendations for OMC roles

| model | draft | review (JSON) | interview | speed | verdict |
|---|---|---|---|---|---|
| **openai/gpt-oss-20b** | ✅ | ✅ | ✅ | fast (6–16s) | **Strong all-rounder.** Reports real read counts + 37.2% retention. Note: single-pass drafting fabricated more numbers than outline-first (6→1–2). |
| **qwen/qwen3-coder-30b** | ✅ | ✅ | ✅ | fastest (0.5–15s) | **Current production default — well justified.** Robust across all writing styles (0–1 unsupported numbers). |
| mistralai/devstral-small-2-2512 | ✅ | ✅ | ✅ | slow (3–64s) | Solid, slower. Good fallback. |
| mistralai/ministral-3-14b-reasoning | ✅ | ✅ | ✅ | med (2–49s) | Passes, but reasoning burns `<think>` tokens and it fabricated more precise numbers (3–10 unsupported). |
| nvidia/nemotron-3-nano | ✅ | ✅ (after #27 fix) | ✅ (after placeholder fix) | slow (6–82s) | Verbose; was silently degrading review JSON before the salvage fix. |
| google/gemma-4-26b-a4b-qat | ✅ | ✅ (after #27 fix) | ❌ not specific | med (5–31s) | Good drafts; weak interview opener (doesn't cite metadata specifics). |
| **qwen/qwen3.6-27b** | ❌ timeouts | ❌ context overflow | ❌ | ~407s/call | **Avoid as currently loaded** — inference far too slow, review prompt overflowed its context window. Likely a bad LM Studio load config; revisit context/offload settings before use. |

## Cross-cutting findings

1. **Data honesty is good with current prompts + data.** Nearly every model caught
   the planted 84-vs-11 sample discrepancy in review, and most Results drafts
   reported real read counts and 37.2% retention. The frost-flower fixture did
   **not** reproduce the old subject-hallucination once study metadata is grounded;
   under *minimal* metadata no model invented the study system.

2. **Outline-then-fill reduces fabrication on smaller models.** Style A/B (grounded
   Results): gpt-oss-20b unsupported numbers by style — single **6**, scholarly **8**,
   twopart **1**, strunk **1**, twopart_honest **2**. qwen3-coder-30b: 0–1 across all.
   → Adopted as production default (`manuscript_outline_first`, on).

3. **The "scholarly / cite exact values" style backfires on small models** — pushing
   for precise numbers made gpt-oss-20b fabricate *more* (8). Prefer outline-first.

4. **Review JSON was silently truncating** (issue #27): verbose models (gemma-4,
   nemotron) overran `max_tokens` and fell back to a single unstructured comment,
   discarding good reviews. Fixed with a salvage parser + larger budget.

5. **Interview openers leaked template placeholders** (`[Your Name]`, `[Author's
   Name]`) on nemotron and ministral. Fixed by asserting editor identity in the
   interview prompt.

## Open issues filed

- **#26** — session AI tools (`get_results_summary` enum + chat prompts) are
  MAG-centric and break for amplicon/microscape submissions.
- **#27** — review agents silently degrade on truncated JSON. *(fixed on this branch)*

## Caveats

- Per-run number counts vary at temperature 0.7; treat single-run unsupported-number
  counts as indicative, not exact. Averaging N runs would sharpen the style comparison.
- First-call latency excludes model load (harness warms each model first), but
  JIT eviction between models still adds real wall-clock in a full sweep.
