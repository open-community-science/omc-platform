"""Deterministic manuscript checks (issue #20).

Machine-verifiable problems that ground the revise loop — no LLM required.
These are OMC's analog of AI-Scientist's chktex / figure-reference checks:
instead of "make it better", the revise pass is handed concrete, checkable
defects (leftover placeholders, figure references that don't line up, precise
numbers absent from the pipeline data, missing/short required sections).

Each check returns a list of issue dicts shaped like a review comment so the
revise pass can consume review findings and check findings uniformly:

    {"section", "issue", "detail", "severity", "confidence", "source"}
"""
import json
import re

REQUIRED_SECTIONS = ["abstract", "introduction", "methods", "results", "discussion"]

# Explicit placeholder tokens the draft prompt is known to emit when it lacks
# information. Kept to a known list to avoid flagging legitimate bracketed text.
_PLACEHOLDER_RE = re.compile(
    r"\[(?:CITE(?::[^\]]*)?|AUTHOR:[^\]]*|CITATION NEEDED|TODO[^\]]*|PLACEHOLDER[^\]]*|X)\]",
    re.IGNORECASE,
)

# "Figure 3", "Fig. 2", "Figures 1 and 2"
_FIGURE_REF_RE = re.compile(r"\b[Ff]ig(?:ure|\.)?\s*(\d+)")

# Decimals and percentages — the number forms most dangerous to hallucinate.
# Plain small integers and 4-digit years are deliberately excluded (too noisy).
# A trailing % is kept in the reported token so findings read naturally.
_PRECISE_NUMBER_RE = re.compile(r"\d+\.\d+\s*%?|\d+\s*%")


def _issue(section, issue, detail, severity):
    return {
        "section": section,
        "issue": issue,
        "detail": detail,
        "severity": severity,
        "confidence": 1.0,
        "source": "check",
    }


def check_placeholders(sections: dict) -> list[dict]:
    """Flag leftover [CITE] / [AUTHOR:] / [CITATION NEEDED] / [TODO] placeholders."""
    issues = []
    for name, content in sections.items():
        seen = set()
        for m in _PLACEHOLDER_RE.finditer(content or ""):
            token = m.group(0)
            if token.lower() in seen:
                continue
            seen.add(token.lower())
            # A leftover [CITE] is expected to happen occasionally (unresolved
            # reference); [AUTHOR:]/[TODO]/[X] mean the draft is incomplete.
            is_cite = token.upper().startswith("[CITE")
            issues.append(_issue(
                name, "unresolved-placeholder",
                f"The placeholder `{token}` is still present in the {name} section. "
                f"Resolve it (supply the citation/value) or, if the information is "
                f"unavailable, convert it to an explicit [AUTHOR: ...] note.",
                "major" if is_cite else "critical",
            ))
    return issues


def check_figure_references(sections: dict, available_figures=None) -> list[dict]:
    """Cross-check numbered figure references against available figures.

    available_figures: iterable of figure identifiers actually generated from
    the pipeline data (e.g. keys of the figures JSON). When provided, flags
    references to figures that don't exist and figures that are never
    referenced — mirroring AI-Scientist's unused/invalid figure detection.
    """
    figures = list(available_figures or [])
    n_available = len(figures)

    referenced = set()
    for content in sections.values():
        for m in _FIGURE_REF_RE.finditer(content or ""):
            referenced.add(int(m.group(1)))

    issues = []
    if n_available:
        over = sorted(n for n in referenced if n > n_available)
        if over:
            issues.append(_issue(
                "general", "figure-reference-mismatch",
                f"The text references Figure(s) {over} but only {n_available} "
                f"figure(s) were generated from the pipeline data. Renumber the "
                f"references or remove those that have no corresponding figure.",
                "major",
            ))
        if not referenced:
            issues.append(_issue(
                "results", "unused-figures",
                f"{n_available} figure(s) were generated from the pipeline data "
                f"but none are referenced in the text. Reference the relevant "
                f"figures in Results, or drop the unused ones.",
                "minor",
            ))
    return issues


def _flatten_number_haystack(results_data) -> str:
    """A string containing every number present in the pipeline results."""
    if not results_data:
        return ""
    return json.dumps(results_data, default=str)


def check_numbers_supported(sections: dict, results_data=None) -> list[dict]:
    """Flag precise numbers in Abstract/Results not found in the pipeline data.

    Only decimals and percentages are checked (the forms most damaging to
    fabricate); years and small integers are ignored to limit false positives.
    Findings are advisory — the revise pass verifies each against the data.
    """
    haystack = _flatten_number_haystack(results_data)
    if not haystack:
        return []

    issues = []
    for name in ("abstract", "results"):
        content = sections.get(name) or ""
        unsupported = []
        seen = set()
        for m in _PRECISE_NUMBER_RE.finditer(content):
            digits = re.sub(r"[^\d.]", "", m.group(0))
            if not digits or digits in seen:
                continue
            seen.add(digits)
            if digits not in haystack:
                unsupported.append(m.group(0).strip())
            if len(unsupported) >= 10:
                break
        if unsupported:
            issues.append(_issue(
                name, "unsupported-numbers",
                f"These numbers in the {name} section were not found in the "
                f"pipeline results data and may be unsupported: {unsupported}. "
                f"Verify each against the data; correct or remove any that cannot "
                f"be traced to a pipeline output.",
                "major",
            ))
    return issues


def check_required_sections(sections: dict) -> list[dict]:
    """Flag required sections that are missing or suspiciously short."""
    issues = []
    for name in REQUIRED_SECTIONS:
        content = (sections.get(name) or "").strip()
        if not content:
            issues.append(_issue(
                name, "missing-section",
                f"The required {name.title()} section is missing or empty.",
                "critical",
            ))
        elif len(content) < 100:
            issues.append(_issue(
                name, "thin-section",
                f"The {name.title()} section is only {len(content)} characters — "
                f"too short to be complete. Expand it with the relevant content.",
                "major",
            ))
    return issues


def run_all_checks(sections: dict, results_data=None, available_figures=None) -> list[dict]:
    """Run every deterministic check and return the combined issue list."""
    return [
        *check_required_sections(sections),
        *check_placeholders(sections),
        *check_figure_references(sections, available_figures),
        *check_numbers_supported(sections, results_data),
    ]
