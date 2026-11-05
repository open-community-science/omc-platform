"""Parse what a user types into the accession box.

One box, many shapes of input:

    PRJNA656268
    SRR1234567 SRR1234568, SRR1234570
    SRR1234567-SRR1234572          # inclusive range
    SRR1234567..SRR1234572         # same
    SRR1234567-72                  # short form: same prefix, new suffix digits
    SAMN100-SAMN105 PRJEB1 PRJEB2

Tokens are separated by whitespace, commas or semicolons. A range joins two
accessions of the same prefix with "-", "..", or an en/em dash; the right-hand
side may omit the prefix. Ranges are expanded here, so callers only ever see a
flat list of individual accessions, in input order, deduplicated.
"""
from __future__ import annotations

import re

# Accession families, keyed by the kind of INSDC object they name.
_KINDS: dict[str, tuple[str, ...]] = {
    "project": ("PRJNA", "PRJEB", "PRJDB"),
    "study": ("SRP", "ERP", "DRP"),
    "biosample": ("SAMN", "SAMEA", "SAME", "SAMD"),
    "sample": ("SRS", "ERS", "DRS"),
    "experiment": ("SRX", "ERX", "DRX"),
    "run": ("SRR", "ERR", "DRR"),
}
# Longest prefixes first so SAMEA wins over SAME.
_PREFIXES = sorted((p for ps in _KINDS.values() for p in ps), key=len, reverse=True)

# Prefixes resolve_to_bioproject() can look up by itself. Others are only
# matched against the metadata of an already-resolved project.
RESOLVABLE = ("PRJNA", "PRJEB", "PRJDB", "SRR", "ERR", "DRR", "SRX", "SRP", "ERS",
              "SAMN", "SAME", "SAMD", "SAMEA")

MAX_RANGE = 5000           # expanded accessions per range
MAX_ACCESSIONS = 10000     # total per submission

_ACC = re.compile(r"^([A-Z]+)(\d+)$")
_SPLIT = re.compile(r"[\s,;]+")
_RANGE_SEP = re.compile(r"\s*(?:\.\.|[-–—])\s*")
_SPACED_RANGE = re.compile(r"(\w)\s*(?:\.\.|[-\u2013\u2014])\s*(\w)")


class AccessionError(ValueError):
    """Input the parser cannot turn into accessions; message is user-facing."""


def kind_of(acc: str) -> str | None:
    """'project' | 'study' | 'biosample' | 'sample' | 'experiment' | 'run' | None."""
    acc = acc.upper()
    for kind, prefixes in _KINDS.items():
        if acc.startswith(prefixes):
            return kind
    return None


def split_accession(acc: str) -> tuple[str, str] | None:
    """'SRR0012' -> ('SRR', '0012'); None if not prefix+digits."""
    m = _ACC.match(acc.upper())
    if not m:
        return None
    prefix, digits = m.group(1), m.group(2)
    if not any(prefix == p for p in _PREFIXES):
        return None
    return prefix, digits


def _expand_range(lo: str, hi: str, token: str) -> list[str]:
    a = split_accession(lo)
    if not a:
        raise AccessionError(f"Unrecognized accession in range: {token}")
    prefix, lo_digits = a
    hi = hi.upper()
    if hi.isdigit():
        # Short form (SRR100-105): the suffix replaces the trailing digits.
        if len(hi) > len(lo_digits):
            raise AccessionError(f"Range end {hi} is longer than the start of {token}")
        hi_digits = lo_digits[: len(lo_digits) - len(hi)] + hi
    else:
        b = split_accession(hi)
        if not b:
            raise AccessionError(f"Unrecognized accession in range: {token}")
        if b[0] != prefix:
            raise AccessionError(f"Range {token} mixes {prefix} and {b[0]} accessions")
        hi_digits = b[1]
    start, end = int(lo_digits), int(hi_digits)
    if end < start:
        raise AccessionError(f"Range {token} runs backwards")
    if end - start + 1 > MAX_RANGE:
        raise AccessionError(f"Range {token} expands to {end - start + 1:,} accessions (limit {MAX_RANGE:,})")
    width = max(len(lo_digits), len(hi_digits))
    return [f"{prefix}{n:0{width}d}" for n in range(start, end + 1)]


def parse_accessions(text: str) -> list[str]:
    """Flatten free text into an ordered, deduplicated list of accessions.

    Raises AccessionError on anything that is not an accession or a range.
    """
    out: list[str] = []
    seen: set[str] = set()
    # "SRR1 - SRR5" is a range too: pull spaced range separators tight first.
    text = _SPACED_RANGE.sub(r"\1-\2", (text or "").strip())
    for token in _SPLIT.split(text):
        if not token:
            continue
        parts = _RANGE_SEP.split(token)
        if len(parts) == 2 and parts[0] and parts[1]:
            accs = _expand_range(parts[0], parts[1], token)
        elif len(parts) == 1:
            acc = token.upper()
            if not split_accession(acc):
                raise AccessionError(f"Unrecognized accession format: {token}")
            accs = [acc]
        else:
            raise AccessionError(f"Cannot read {token} as an accession or a range")
        for acc in accs:
            if acc not in seen:
                seen.add(acc)
                out.append(acc)
        if len(out) > MAX_ACCESSIONS:
            raise AccessionError(f"More than {MAX_ACCESSIONS:,} accessions in one submission")
    if not out:
        raise AccessionError("Enter at least one accession")
    return out


def summarize(accs: list[str], limit: int = 50) -> str:
    """Short human label for a list: 'SRR1 SRR2 … (+8 more)', at most `limit` chars."""
    if not accs:
        return ""
    label = accs[0]
    for a in accs[1:]:
        candidate = f"{label} {a}"
        if len(candidate) > limit - 12:
            break
        label = candidate
    shown = len(label.split())
    if shown < len(accs):
        label += f" (+{len(accs) - shown} more)"
    return label[:limit]
