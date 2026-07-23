"""Benchmark different LM Studio models for OMC tasks.

Run with: pytest tests/test_model_benchmark.py -v -s
Results saved to tests/model_notes.md
"""
import sys
import time
import json
import pytest
from pathlib import Path

# Repo root (two levels up from tests/) — not a hardcoded absolute path, so the
# suite runs from any checkout location.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai.llm_client import get_client, chat, _strip_think

pytestmark = pytest.mark.ai

NOTES_PATH = Path(__file__).parent / "model_notes.md"

# Models to test (subset of what's in LM Studio)
MODELS = [
    "qwen3-coder-30b-a3b-instruct",
    "codeqwen3-14b",
    "gpt-oss-20b",
    "gemma-3-27b-it-qat",
    "glm-4.7-flash",
    "devstral-small-2-24b-instruct-2512",
    "deepseek-r1-0528-qwen3-8b",
]

# Tasks representative of OMC workloads
TASKS = {
    "scientific_writing": {
        "system": "You are a scientific writing assistant for microbial ecology.",
        "user": """Write one paragraph about how temperature affects marine microbial diversity.
Include a [CITE] placeholder. Keep it under 100 words.""",
        "max_tokens": 500,
        "check": lambda r: len(r) > 50 and any(w in r.lower() for w in ["temperature", "microbial", "diversity"]),
    },
    "json_extraction": {
        "system": "Extract structured data. Return ONLY valid JSON.",
        "user": """Extract metadata from this description:
"Sediment sample from Arctic Ocean near Svalbard, collected 2023-06-15 at 200m depth, temp -1.2C"

Return JSON with fields: organism, collection_date, geo_loc_name, depth, temp""",
        "max_tokens": 500,
        "check": lambda r: "2023" in r and ("svalbard" in r.lower() or "arctic" in r.lower()),
    },
    "peer_review": {
        "system": "You are a peer reviewer for microbial ecology manuscripts.",
        "user": """Review this claim: "PERMANOVA showed temperature explained 18% of community variation (p=0.001)."
Give one constructive comment about what's missing or could be improved. Keep it brief.""",
        "max_tokens": 500,
        "check": lambda r: len(r) > 30,
    },
}


def _run_task(client, model, task_name, task):
    """Run a single task and return results."""
    start = time.time()
    try:
        raw = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": task["system"]},
                {"role": "user", "content": task["user"]},
            ],
            max_tokens=task["max_tokens"],
            temperature=0.5,
        ).choices[0].message.content
        elapsed = time.time() - start
        clean = _strip_think(raw)
        passed = task["check"](clean)
        return {
            "task": task_name,
            "model": model,
            "time_s": round(elapsed, 1),
            "output_len": len(clean),
            "passed": passed,
            "output_preview": clean[:150],
            "had_thinking": "<think>" in (raw or ""),
        }
    except Exception as e:
        return {
            "task": task_name,
            "model": model,
            "time_s": round(time.time() - start, 1),
            "output_len": 0,
            "passed": False,
            "output_preview": f"ERROR: {e}",
            "had_thinking": False,
        }


@pytest.mark.timeout(600)
def test_benchmark_models():
    """Benchmark available models across OMC tasks."""
    client = get_client()
    results = []

    for model in MODELS:
        print(f"\n--- {model} ---")
        for task_name, task in TASKS.items():
            r = _run_task(client, model, task_name, task)
            results.append(r)
            status = "PASS" if r["passed"] else "FAIL"
            think = " (reasoning)" if r["had_thinking"] else ""
            print(f"  {status} {task_name}: {r['time_s']}s, {r['output_len']} chars{think}")
            if not r["passed"]:
                print(f"       {r['output_preview'][:100]}")

    # Write notes
    lines = ["# LM Studio Model Benchmark\n"]
    lines.append(f"Date: {time.strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("| Model | Task | Time | Chars | Pass | Thinking |")
    lines.append("|-------|------|------|-------|------|----------|")
    for r in results:
        lines.append(
            f"| {r['model']} | {r['task']} | {r['time_s']}s | {r['output_len']} | "
            f"{'Y' if r['passed'] else 'N'} | {'Y' if r['had_thinking'] else 'N'} |"
        )

    # Summary
    lines.append("\n## Summary\n")
    for model in MODELS:
        mr = [r for r in results if r["model"] == model]
        passed = sum(1 for r in mr if r["passed"])
        avg_time = sum(r["time_s"] for r in mr) / max(len(mr), 1)
        loaded = any(r["output_len"] > 0 for r in mr)
        status = f"{passed}/{len(mr)} passed, avg {avg_time:.1f}s" if loaded else "FAILED TO LOAD"
        lines.append(f"- **{model}**: {status}")

    notes = "\n".join(lines) + "\n"
    NOTES_PATH.write_text(notes)
    print(f"\nResults saved to {NOTES_PATH}")
