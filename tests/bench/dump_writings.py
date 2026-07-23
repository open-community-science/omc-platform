"""Dump full Results-section prose from each model for qualitative voice review.

Generates the grounded Results section (single-pass, the model's natural voice)
for each model and writes the complete text to writings/<model>.md so a human (or
Claude) can judge how well each nails the scientific voice — beyond pass/fail.
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

from ai.llm_client import get_client, chat
from ai.manuscript_generator import SYSTEM_PROMPT, _format_study, build_results_prompt
from fixtures import load_fixture, STUDY_GROUNDED
from run_bench import _unload_all, _lms_load

BASE_URL = "http://localhost:1234/v1"
OUT = HERE / "writings"
OUT.mkdir(exist_ok=True)

# Viable models (qwen3.6-27b excluded — too slow to finish drafting here).
MODELS = [
    "qwen/qwen3-coder-30b",
    "openai/gpt-oss-20b",
    "google/gemma-4-26b-a4b-qat",
    "mistralai/devstral-small-2-2512",
    "nvidia/nemotron-3-nano",
    "mistralai/ministral-3-14b-reasoning",
]

prompt = build_results_prompt(
    _format_study(STUDY_GROUNDED), load_fixture(),
    {"research_question": "How do frost flowers concentrate marine bacterial communities?"},
)
client = get_client(base_url=BASE_URL)

for model in MODELS:
    print(f"=== {model} ===", flush=True)
    _unload_all()
    secs, ok, msg, ctx = _lms_load(model, 65536, 300)
    if not ok:
        print(f"  load failed: {msg}", flush=True)
        continue
    t0 = time.time()
    text = chat(client, SYSTEM_PROMPT, prompt, model=model, max_tokens=2000, temperature=0.5)
    dt = time.time() - t0
    safe = model.replace("/", "__")
    (OUT / f"{safe}.md").write_text(f"# {model}\n\n_({dt:.1f}s, {len(text)} chars)_\n\n{text}\n")
    print(f"  wrote {len(text)} chars in {dt:.1f}s", flush=True)

print("done", flush=True)
