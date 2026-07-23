# Prompt / Model Benchmark — Findings (2026-07-22 → 07-23)

Local LM Studio (~20 GB GPU), fixture **c5af6277** (sea-ice frost-flower 16S,
PRJNA1473294). Real production prompt builders; see `README.md` for the harness.
Reproduce: `python tests/bench/run_bench.py --context 65536`.

## Clean matrix after the client/prompt fixes (2026-07-23)

Setup: harness now `lms unload`s + explicitly `lms load -c 65536 --parallel 1`
per model (context fallback 64k→48k→32k→16k for VRAM fit), with a circuit
breaker that skips a model's remaining tasks after 2 timeouts.

**6 of 7 models pass every task (36/36).** Full report format in `run_bench.py --out`.

| model | ctx | draft | 2-part | anti-fab | review | cites | interview | speed | verdict |
|---|---|---|---|---|---|---|---|---|---|
| **qwen/qwen3-coder-30b** | 48k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **fast** 0.6–14s | **Production default — best speed+correctness.** Robust across all writing styles (0–1 unsupported numbers). |
| **openai/gpt-oss-20b** | 64k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **fast** 2–11s | **Strong all-rounder.** Single-pass fabricated more numbers than outline-first (6→1–2). |
| mistralai/ministral-3-14b-reasoning | 64k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | med 2–36s | Reasoning model; a few unsupported numbers (3–4). |
| mistralai/devstral-small-2-2512 | 64k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | med 3–59s | Solid all-round, slower. |
| google/gemma-4-26b-a4b-qat | 64k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | slow 8–98s | **Fully capable** — both original failures were client bugs (#27 review truncation, #28 reasoning budget), not model weakness. Review: *"unsuitable for publication due to a fundamental discrepancy between n=84 and n=11."* |
| nvidia/nemotron-3-nano | 64k | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | slow 7–99s | Verbose; was silently degrading review JSON before #27. Now clean, outlines cite data keys. |
| **qwen/qwen3.6-27b** | 64k | ⏱ | ⏱ | skip | skip | skip | skip | **~7 tok/s** | **Avoid on this GPU.** Loads clean at 64k now (context solved) — the *sole* blocker is throughput: 27B dense (vs MoE peers) times out on every drafting task. Circuit breaker capped it. |

Speeds ≥40s reflect 26–30B models partially CPU-offloading on the ~20 GB card.
For OMC's latency: **qwen3-coder-30b** and **gpt-oss-20b** are the fast + correct picks.

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
   most deployments), so our review budget (raised to 8,000) is well within its ceiling.

7. **Hardware / LM Studio setup matters as much as the prompts.** This is a ~20 GB
   GPU (not 32). Relying on JIT to swap models crashed (SIGSEGV/SIGABRT) once VRAM
   was near full; a model pinned at 144k×4-parallel filled the card. Fix: `lms
   unload` then `lms load -c <ctx> --parallel 1` per model, with a context
   fallback ladder (64k→48k→32k→16k) since even parallel-1 at 64k OOMs the larger
   models (qwen3-coder-30b fits at 48k = 19.1 GB). qwen3.6-27b's original
   "context exceeded" was purely the small default load context — solved by `-c`.

## Open issues filed

- **#26** — session AI tools MAG-centric, break amplicon submissions. *(fixed on branch)*
- **#27** — review agents silently degrade on truncated JSON. *(fixed on branch)*
- **#28** — hidden-reasoning models return empty output at small budgets. *(fixed on branch)*

## Caveats

- Per-run number counts vary at temperature 0.7; treat single-run unsupported-number
  counts as indicative, not exact. Averaging N runs would sharpen the style comparison.
- Residual `unsupported-num` flags (gpt-oss, ministral, devstral) are mostly computed
  percentages / relative abundances not literally present in the data JSON — worth a
  glance but not hard failures.
- Per-task latency excludes model load (loaded separately, reported in the `load`
  column), but the 26–30B models partially CPU-offload on this ~20 GB card, so their
  inference times (40–100s) are hardware-bound, not model-quality signals.
