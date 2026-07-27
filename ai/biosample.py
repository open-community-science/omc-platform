"""Per-sample BioSample attributes from NCBI (#62).

An SRA submission's `samples.json` carries sequencing metadata — instrument, read
counts, library names. The EXPERIMENT lives in the BioSample records: what each sample
actually was, where it came from, and when it was really collected. Without those an
agent has nothing to group samples by, and ends up correlating against sequencing depth
because depth is the only covariate it has.

Deliberately dependency-free (urllib + ElementTree, like the rest of the bench's NCBI
access) so it can run anywhere the platform runs, and fetched ONCE into a file rather
than from inside the analysis sandbox, which has no network by design.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Attributes that say what a sample IS rather than how it was sequenced. Not a
# whitelist — everything is kept — but these are the ones worth pointing an analyst at.
DESIGN_HINTS = ("env_broad_scale", "env_local_scale", "env_medium", "host",
                "isolation_source", "geo_loc_name", "lat_lon", "collection_date",
                "depth", "temp", "salinity", "ph", "treatment", "tissue")


def _get(url: str, timeout: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode()


def fetch_attributes(accessions: Iterable[str], timeout: int = 40,
                     chunk: int = 100) -> dict[str, dict]:
    """BioSample accession -> {attribute: value}, for every accession given.

    Results are keyed by the accession the RECORD reports, not by request order —
    esearch does not promise to return uids in the order asked, and silently
    misattributing one sample's environment to another is worse than fetching nothing.
    """
    todo = sorted({a for a in accessions if a})
    out: dict[str, dict] = {}
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        term = urllib.parse.quote(" OR ".join(batch))
        found = json.loads(_get(
            f"{EUTILS}/esearch.fcgi?db=biosample&term={term}&retmax={len(batch) * 2}"
            "&retmode=json", timeout))["esearchresult"]["idlist"]
        if not found:
            continue
        root = ET.fromstring(_get(
            f"{EUTILS}/efetch.fcgi?db=biosample&id={','.join(found)}"
            "&rettype=full&retmode=xml", timeout))
        for s in root.iter("BioSample"):
            acc = s.get("accession")
            if not acc:
                continue
            attrs = {}
            for a in s.iter("Attribute"):
                name = a.get("harmonized_name") or a.get("attribute_name")
                if name and a.text:
                    attrs[name] = a.text
            title = s.find(".//Title")
            if title is not None and title.text:
                # Bare `title`: consumers prefix by source, and "biosample_title"
                # here would come out as biosample_biosample_title.
                attrs.setdefault("title", title.text)
            out[acc] = attrs
    return out


def design_columns(attrs: dict[str, dict], min_distinct: int = 2,
                   max_distinct: int | None = None) -> list[str]:
    """Attributes that actually VARY across samples, most-informative first.

    A field with one value across every sample describes the study, not the samples,
    and cannot group anything. A field with a distinct value per sample is an
    identifier. What is left is the design."""
    n = len(attrs) or 1
    cap = max_distinct if max_distinct is not None else max(2, n - 1)
    counts = {}
    for a in attrs.values():
        for k, v in a.items():
            counts.setdefault(k, set()).add(v)
    varying = {k: len(v) for k, v in counts.items() if min_distinct <= len(v) <= cap}
    # design hints first, then by how few groups they make (fewer = more usable)
    return sorted(varying, key=lambda k: (k not in DESIGN_HINTS, varying[k], k))


def write_attributes(data_dir: Path, accessions: Iterable[str], *,
                     refresh: bool = False, timeout: int = 40) -> dict[str, dict]:
    """Fetch into ``<data_dir>/sample_attributes.json`` and return what is there.

    Cached because the sandbox has no network and this is the one place the metadata
    can legitimately be pulled. A fetch failure returns whatever is already cached
    rather than raising — missing covariates make an analysis poorer, not impossible.
    """
    path = Path(data_dir) / "sample_attributes.json"
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    try:
        attrs = fetch_attributes(accessions, timeout=timeout)
    except Exception:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                return {}
        return {}
    if attrs:
        path.write_text(json.dumps(attrs, indent=2, sort_keys=True) + "\n")
    return attrs
