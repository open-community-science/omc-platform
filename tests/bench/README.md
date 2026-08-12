# OMC Prompt / Model Benchmark

Runs the **real production prompt builders** against a **real submission fixture**
across local LM Studio models, scoring objective failure modes. Built to iterate
on prompts and to pick models for each AI role (draft / cite / review).

## Quick start

```bash
# LM Studio server must be running (lms server start); models auto-load JIT
python tests/bench/run_bench.py                       # DEFAULT_SET × default tasks
python tests/bench/run_bench.py --all                 # every model in models.py
python tests/bench/run_bench.py --models qwen/qwen3-coder-30b openai/gpt-oss-20b
python tests/bench/run_bench.py --tasks results_single results_twopart_honest results_strunk results_scholarly
python tests/bench/run_bench.py --out tests/bench/run-2026-07-22.md
```

Reports write to `--out` (default `results.md`) plus a sibling `.json`. Run
artifacts are gitignored; commit a report explicitly if you want to keep it.

Flags: `--timeout` (per-task seconds, default 150), `--load-timeout` (model
warm-up seconds, default 240). Each model is warmed with a tiny call first so
per-task latency reflects inference, not cold load.

## Fixture

`fixtures.py` derives a `parse_amplicon`-shaped `pipeline_outputs` from real
submission **c5af6277** — a sea-ice "frost flower" 16S amplicon run
(BioProject PRJNA1473294) with NCBI-verified study metadata. This is the run
whose subject earlier models hallucinated (see
`manuscript_generator._format_study`), so it doubles as an anti-fabrication
regression fixture. Notable real signal: **84 samples were filtered but only 11
retained** in the final ASV table — a data-quality fact an honest draft/review
must surface.

Raw viz data lives outside the repo (`OMC_BENCH_DATA`, default
`/data/dev/testdata/c5af6277`); the committed `fixture_c5af6277.json` snapshot
lets the harness run without it. Regenerate with `python tests/bench/fixtures.py`.

## Tasks

| task | prompt surface | scored for |
|---|---|---|
| `results_single` | `build_results_prompt` (grounded) | fabricated cites, unsupported numbers, data honesty |
| `results_twopart` / `_strunk` / `_scholarly` / `_twopart_honest` | same, different writing style | style A/B + the above |
| `results_minimal` | `build_results_prompt` (minimal metadata) | **anti-fabrication** — must not invent the study system |
| `review_stat` | `statistical_review` | JSON-contract validity, confidence bounds, catches 84→11 dropout |
| `citation_queries` | `generate_search_queries` | JSON array of focused queries |
| `interview_open` | `start_interview` | specific, asks a question, concise, no leftover placeholders |

## Writing styles (A/B dimension)

Same data + base prompt, different composition guidance (`STYLES` in
`run_bench.py`): `single` (baseline), `twopart` (outline-then-fill), `strunk`
(Elements of Style), `scholarly` (IMRaD best practice), `twopart_honest`
(outline + candor about data quality — candidate production prompt).

## Scoring signals

- **fabricated citations** — SYSTEM_PROMPT forbids `(Smith et al., 2022)`; only bare `[CITE]`.
- **unsupported numbers** — via `ai.manuscript_checks.check_numbers_supported` (decimals/percentages not traceable to the data).
- **invented subject** — under minimal metadata, mentioning frost flower / sea ice / ice chamber / brine (or wrong domains) = fabrication.
- **data honesty** — does the text own the 84→11 dropout / 37% retention.
- **JSON validity** — reviews and citation queries must parse to the expected shape.
- **`<think>` waste**, **latency**, **output length**.

## Notes

- `fir` is SSH keyboard-interactive (no batch pulls); `arbutus` is reachable via `ssh arbutus`.
- Reasoning models (`ministral-3-14b-reasoning`, `deepseek-r1`) spend tokens on `<think>` blocks — `llm_client` strips them and scales the token budget.
