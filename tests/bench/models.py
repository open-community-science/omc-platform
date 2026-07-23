"""Local model registry for the prompt/model benchmark.

IDs match `lms ls` (LM Studio). DEFAULT_SET is a representative spread across
families/sizes that fit the GV100 (32 GB); pass --all or --models to override.
Reasoning models are marked so we can account for <think> token spend.
"""

# id, reasoning?, note
ALL_MODELS = [
    ("qwen/qwen3-coder-30b",              False, "30B-A3B MoE — current production default"),
    ("qwen/qwen3.6-27b",                  False, "27B — newest qwen"),
    ("google/gemma-4-26b-a4b-qat",        False, "26B-A4B MoE QAT"),
    ("google/gemma-3-27b",                False, "27B dense"),
    ("openai/gpt-oss-20b",                False, "20B"),
    ("openai/gpt-oss-120b",               False, "120B — largest, may offload"),
    ("mistralai/devstral-small-2-2512",   False, "24B"),
    ("mistralai/ministral-3-14b-reasoning", True, "14B reasoning"),
    ("nvidia/nemotron-3-nano",            False, "30B MoE"),
    ("zai-org/glm-4.7-flash",             False, "30B — historically verbose/bad JSON"),
    ("bytedance/seed-oss-36b",            False, "36B"),
    ("qwen/qwen3-coder-next",             False, "80B — large MoE"),
    ("codeqwen3-14b",                     False, "14B"),
    ("deepseek/deepseek-r1-0528-qwen3-8b", True, "8B reasoning"),
]

# Fast, representative first-pass set
DEFAULT_SET = [
    "qwen/qwen3-coder-30b",
    "qwen/qwen3.6-27b",
    "google/gemma-4-26b-a4b-qat",
    "openai/gpt-oss-20b",
    "mistralai/devstral-small-2-2512",
    "nvidia/nemotron-3-nano",
    "mistralai/ministral-3-14b-reasoning",
]

REASONING = {m for m, r, _ in ALL_MODELS if r}
NOTES = {m: n for m, _, n in ALL_MODELS}
