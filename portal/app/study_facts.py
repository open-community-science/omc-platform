"""The single source of what an AI is told about a study (issue #56).

`build_study_facts()` is the one place study context is assembled, for
autoresearch, the manuscript formatter and review alike. Assembling it per
caller means a fact added in one place is missing from the others, and it fails
*silently* — the model simply never learns it, with no error anywhere. It returns plain JSON-able data (no ORM
objects), so the `ai/` side can render it without importing portal models.

Two properties worth preserving when extending this:

- **It mirrors `sample_metadata` key-for-key.** Existing consumers that reach
  for `facts["title"]` keep working, so new facts are purely additive. The one
  exception is size: bulk per-sample containers are replaced by a count, because
  this dict goes into prompts verbatim (see `_summarize_bulk`).
- **Absent means absent.** A fact we do not have is left out rather than
  included empty, because a key present with a null value reads to a model as
  "measured and found to be nothing".
"""
from __future__ import annotations

import json

# Keys carried straight through from the SRA record. Listed explicitly so a
# reader can see what the AI is grounded on without tracing NCBI's schema.
_PASSTHROUGH_DOC = (
    "title", "organism", "organization", "description",
    "num_samples", "num_sra_runs", "platform", "study_name",
)


def _amplicon_design(primers: dict | None) -> dict | None:
    """Design-level amplicon summary: which primer pair(s) this study appears to use.

    Design-level only — this is OMC's own inference, and it carries no per-sample
    mapping, so consumers must not report which sample used which primer. The
    ground truth is per-sample and lives in the pipeline's cutadapt logs; see #55.
    """
    if not primers:
        return None
    sets = primers.get("sets") or [primers]
    designs = []
    for s in sets:
        d = {
            "region": s.get("region"),
            "forward": s.get("fwd_name") or s.get("fwd"),
            "reverse": s.get("rev_name") or s.get("rev"),
        }
        runs = s.get("runs") or []
        if runs:
            d["n_runs"] = s.get("n_runs") or len(runs)
        designs.append({k: v for k, v in d.items() if v})
    if not designs:
        return None
    return {
        "source": primers.get("source", "inferred"),
        "designs": designs,
    }


# A study fact has to fit in a prompt. `sample_metadata` also carries bulk
# per-sample payloads — `sample_records` alone reaches 6 MB on a large
# BioProject — and the review agent json.dumps this dict wholesale, so passing
# those through turns a 62-character config into ~1.8M tokens. Containers above
# this size are replaced by a count; per-sample detail belongs in the viz
# datasets the agent navigates, not inlined into every prompt.
_MAX_CONTAINER_CHARS = 2000


def _summarize_bulk(key: str, value):
    """Replace an oversized list/dict with a compact descriptor, or keep it.

    Only containers are capped. Long strings (a study abstract, say) are left
    alone: they are the actual subject matter the model needs, and they do not
    grow with sample count the way these collections do.
    """
    if not isinstance(value, (list, dict)):
        return value
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return value
    if size <= _MAX_CONTAINER_CHARS:
        return value
    return {
        "n": len(value),
        "omitted": f"{size:,} chars of per-sample detail — too large for a prompt; "
                   "read the per-sample datasets instead",
    }


def build_study_facts(submission) -> dict:
    """Everything an AI consumer is told about a study, as one flat-ish dict.

    Consumed by manuscript drafting and the review agents; see issue #56 for the
    autoresearch path, which still assembles its own `study` dataset.
    """
    facts: dict = {
        k: _summarize_bulk(k, v)
        for k, v in (getattr(submission, "sample_metadata", None) or {}).items()
    }

    accession = getattr(submission, "bioproject_accession", None)
    if accession:
        facts.setdefault("accession", accession)

    pipeline = getattr(submission, "pipeline", None)
    if pipeline is not None:
        facts["pipeline"] = getattr(pipeline, "value", pipeline)

    primers = getattr(submission, "primers", None)
    if primers:
        # Kept under both keys on purpose: `primers` is the raw record other code
        # already reads, `amplicon` is the rendered view meant for a prompt.
        facts["primers"] = primers
        design = _amplicon_design(primers)
        if design:
            facts["amplicon"] = design

    return facts
