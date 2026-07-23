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
| google/gemma-4-26b-a4b-qat | ✅ | ✅ (after #27 fix) | ✅ (after #28 fix) | med (13–59s) | **Fully capable across all tasks.** Both original failures were client bugs, not model weaknesses: review truncation (#27) and empty interview output from the reasoning-budget bug (#28). Hidden-reasoning model — give it room. |
| **qwen/qwen3.6-27b** | ❌ timeouts | ❌ context overflow | (needs #28) | ~7 tok/s | **Avoid on this GPU.** Hidden-reasoning model AND 27B *dense* → ~7 tok/s (vs MoE peers). Drafting blew the 150s cap; review overran the loaded context. Even with #28, throughput makes it impractical here. Load with big context + budgeted/disabled thinking if you must. |

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

6. **Hidden-reasoning models return empty output at small token budgets** (#28).
   gemma-4 and qwen3.x emit reasoning on a separate `reasoning_content` channel
   that still counts against `max_tokens`; at 300 tokens (interview opener) the
   reasoning ate the whole budget → empty content. `_is_reasoning_model` didn't
   know these models. This alone explained gemma-4's interview "weakness" — it's
   actually a strong model. `gemma-4 max output = 32,768 tokens` (128K context on
   most deployments), so our 4000-token review budget is well within its ceiling.

## Open issues filed

- **#26** — session AI tools MAG-centric, break amplicon submissions. *(fixed on branch)*
- **#27** — review agents silently degrade on truncated JSON. *(fixed on branch)*
- **#28** — hidden-reasoning models return empty output at small budgets. *(fixed on branch)*

## Caveats

- Per-run number counts vary at temperature 0.7; treat single-run unsupported-number
  counts as indicative, not exact. Averaging N runs would sharpen the style comparison.
- First-call latency excludes model load (harness warms each model first), but
  JIT eviction between models still adds real wall-clock in a full sweep.
