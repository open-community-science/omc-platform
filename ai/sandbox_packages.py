"""Packages an analyst may add to the sandbox for itself (#62 follow-on).

`request_package` records what an analyst reached for. This grants a bounded subset of
those requests automatically, so a run is not blocked on a library that was always
going to be approved.

THE ALLOWLIST IS THE ENTIRE SECURITY CONTROL. A model names a package and, if that name
is a key here, a `pip install` runs against the interpreter the sandbox executes in. So:

- Only exact keys are honoured. The model's string is a lookup, never an argument —
  nothing it writes reaches the command line, which rules out version specifiers,
  extras, index URLs, VCS or local paths, and anything else pip would accept.
- The PyPI name comes from this file, not from the request. `skbio` is the import name;
  `scikit-bio` is what gets installed. A model asking for "skbio==0.5.9" matches nothing.
- Entries are here because a scientist decided they belong in a microbial-ecology
  sandbox, not because a model asked twice.

Anything outside the list stays recorded and uninstalled, which is the point of keeping
the requests: the list should grow by review, not by demand.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys

# import name -> PyPI distribution name
ALLOWED: dict[str, str] = {
    "skbio": "scikit-bio",          # diversity, ordination, PERMANOVA/ANOSIM
    "statsmodels": "statsmodels",   # GLMs, mixed models, multiple-testing beyond fdr()
    "networkx": "networkx",         # co-occurrence / association networks
    "Bio": "biopython",             # sequence handling
    "umap": "umap-learn",           # non-linear ordination
    "patsy": "patsy",               # formula interface statsmodels expects
}

INSTALL_TIMEOUT = 300


def is_available(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def install(import_name: str, *, timeout: int = INSTALL_TIMEOUT) -> dict:
    """Install an allowlisted package into the interpreter the sandbox runs in.

    Returns what happened, for the tool reply and the run record. Never raises: a
    failed install leaves the analyst exactly where it already was."""
    name = (import_name or "").strip()
    if name not in ALLOWED:
        return {"installed": False, "available": is_available(name),
                "reason": "not on the sandbox allowlist"}
    if is_available(name):
        return {"installed": False, "available": True, "reason": "already available"}
    dist = ALLOWED[name]           # from this file — never the caller's string
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-input", dist],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"installed": False, "available": False,
                "reason": f"pip install {dist} timed out after {timeout}s"}
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        return {"installed": False, "available": False,
                "reason": f"pip install {dist} failed: {tail[-1][:200] if tail else '?'}"}
    importlib.invalidate_caches()   # the spec was cached as missing a moment ago
    ok = is_available(name)
    return {"installed": ok, "available": ok, "distribution": dist,
            "reason": "installed" if ok else
                      f"pip reported success but {name} is still not importable"}
