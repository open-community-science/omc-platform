"""Shared Jinja2 templates instance.

Every router that renders HTML must import `templates` from here rather than
constructing its own Jinja2Templates. Separate instances don't share globals or
filters, so a global added in one module (e.g. `is_admin`, used by base.html for
the Admin nav link) would raise UndefinedError in every other module's
templates — which is exactly how /sessions/{slug}/chat started 500ing.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .auth import is_admin

BASE_DIR = Path(__file__).parent.parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Gate the Admin nav link in base.html.
templates.env.globals["is_admin"] = is_admin

# Human-friendly submission-status labels for the status pill. The raw enum
# values (e.g. "results_ready") are fine as CSS class suffixes but read poorly.
# "results_ready" is deliberately worded to signal the author must act next.
STATUS_LABELS = {
    "draft": "Draft", "submitted": "Submitted", "queued": "Queued",
    "running": "Running", "processing": "Processing",
    "results_ready": "Ready", "drafting": "Drafting",
    "review": "In review", "published": "Published", "failed": "Failed",
}


def status_label(v) -> str:
    """Friendly label for a SubmissionStatus (or its raw value)."""
    raw = getattr(v, "value", v)
    return STATUS_LABELS.get(raw, str(raw).replace("_", " ").title())


templates.env.filters["status_label"] = status_label

# User-facing pipeline names. "microscape" is the current implementation detail
# (microscape-nf, pending merge into danaSeq); users should only ever see the
# illumina_amplicon naming, matching the other danaSeq building blocks.
PIPELINE_LABELS = {
    "microscape": "illumina_amplicon",
    "nanopore_mag": "nanopore_metagenome",
    "illumina_mag": "illumina_metagenome",
    "rnaseq": "illumina_rna",
    "isolate_genome": "isolate_genome",
}


def pipeline_label(v) -> str:
    """Friendly, implementation-agnostic name for a PipelineType (or its value)."""
    raw = getattr(v, "value", v)
    return PIPELINE_LABELS.get(raw, str(raw))


templates.env.filters["pipeline_label"] = pipeline_label
