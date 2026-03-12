# LM Studio Model Notes

Remote server: `10.151.49.182:1234` (Quadro GV100, 32 GB VRAM)
Date: 2026-03-12

## VRAM Estimates (lms load --estimate-only)

| Model | Size | 32k ctx | 64k ctx | 128k ctx | Notes |
|-------|------|---------|---------|----------|-------|
| qwen3-coder-30b-a3b (Q3_K_L) | 14.6 GB | 18.9 GB | 22.8 GB | **30.6 GB** | MoE, fits 128k! Best for writing |
| gpt-oss-20b (MXFP4) | 12.1 GB | 18.1 GB | 23.7 GB | 35.0 GB | Too big at 128k |
| codeqwen3-14b (Q8_0) | 15.7 GB | 24.5 GB | — | 49.5 GB | Q8 quant heavy, failed to load in benchmark |
| gemma-3-27b-it-qat (Q4_0) | 16.4 GB | 41.7 GB | — | — | Too big even at 32k |
| seed-oss-36b (Q4_K_M) | 21.8 GB | 32.5 GB | — | — | Borderline at 32k |
| devstral-small-2-24b | 15.2 GB | ~20 GB | — | — | OK at 32k |
| deepseek-r1-qwen3-8b | 5.0 GB | ~8 GB | — | — | Small, reasoning model |
| glm-4.7-flash | 18.1 GB | — | — | — | Verbose, failed JSON extraction |

## Benchmark Results (2026-03-12)

Tasks: scientific_writing, json_extraction, peer_review

| Model | Pass | Avg Time | Notes |
|-------|------|----------|-------|
| **qwen3-coder-30b-a3b** | **3/3** | **2.0s** | Fastest, best quality |
| gpt-oss-20b | 3/3 | 10.6s | Good but slower |
| devstral-small-2-24b | 3/3 | 9.8s | Solid all-round |
| deepseek-r1-qwen3-8b | 3/3 | 7.3s | Good but uses think tokens |
| glm-4.7-flash | 2/3 | 16.9s | Too verbose, failed JSON |
| codeqwen3-14b | 0/3 | — | Failed to load (VRAM) |
| gemma-3-27b-it-qat | 0/3 | — | Failed to load (VRAM) |

## Recommended Configurations

### For manuscript writing (needs long context):
- **qwen3-coder-30b-a3b @ 128k** — 30.6 GB VRAM, fits GV100 solo
- Full manuscript fits easily in context (typically ~20k tokens)
- Fastest model tested (2.0s avg per task)

### For quick tasks (JSON extraction, reviews):
- **gpt-oss-20b @ 32k** — 18.1 GB, good quality
- **devstral-small-2-24b @ 32k** — 20 GB, solid

### Avoid:
- glm-4.7-flash: verbose, unreliable JSON
- codeqwen3-14b: Q8 quant too heavy for GV100
- gemma-3-27b: too large even at 32k context
- deepseek-r1-qwen3-8b: wastes tokens on `<think>` blocks

## Full Test Suite Results (2026-03-12)

All tests pass with qwen3-coder-30b-a3b-instruct @ 128k context:

| Test | Count | Time | Status |
|------|-------|------|--------|
| test_health | 2 | <1s | PASS |
| test_auth | 2 | <1s | PASS |
| test_sra_lookup | 2 | 18s | PASS (real NCBI) |
| test_submissions | 2 | <1s | PASS |
| test_interview | 1 | <1s | PASS |
| test_llm_client | 3 | 5s | PASS |
| test_metadata_assistant | 4 | 18s | PASS |
| test_ai_interview | 2 | 5s | PASS |
| test_review_agents | 3 | 102s | PASS (22 review comments) |
| test_manuscript | 1 | 100s | PASS (5 sections) |
| test_real_data (parse) | 3 | <1s | PASS |
| test_real_data (manuscript) | 1 | 107s | PASS (real pipeline data) |
| test_real_data (review) | 1 | — | Pending |
