"""The single source of what an AI is told about a study (issue #56).

Three paths used to assemble study context independently, and each saw a
different subset: autoresearch merged `sample_metadata` with `primers`, the
manuscript formatter flattened six whitelisted fields, and review received
`{pipeline, accession}` and nothing else. Adding a fact meant editing three
places, and forgetting one failed *silently* — the model simply never learned
it, with no error anywhere.

`build_study_facts()` is that one place. It returns plain JSON-able data (no ORM
objects), so the `ai/` side can render it without importing portal models.

Two properties worth preserving when extending this:

- **It is a superset of `sample_metadata`.** Existing consumers that reach for
  `facts["title"]` keep working, so new facts are purely additive.
- **Absent means absent.** A fact we do not have is left out rather than
  included empty, because a key present with a null value reads to a model as
  "measured and found to be nothing".
"""
from __future__ import annotations

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


def build_study_facts(submission) -> dict:
    """Everything an AI consumer is told about a study, as one flat-ish dict.

    Consumed by manuscript drafting and the review agents; see issue #56 for the
    autoresearch path, which still assembles its own `study` dataset.
    """
    facts: dict = dict(getattr(submission, "sample_metadata", None) or {})

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
