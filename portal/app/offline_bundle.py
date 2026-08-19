"""Package a published run so it can be read without a network.

A deployed site is a static SPA that fetches its data from `data/` beside it.
Downloaded and opened from a `file://` URL, those fetches are treated as
cross-origin and refused: the app loads, finds nothing, and reports an empty
run. Nothing about that tells the reader the data is sitting in the same folder.

A `<script>` tag is not subject to that rule, so the bundle carries the payload
as one — `data.js`, holding each file base64-encoded and still gzipped, keyed by
the path the app would otherwise have fetched. The app prefers it when present
(see `embedded()` in the viz data store), so the same build serves both. The
site's own `data/` is kept as well: it costs little next to the JS bundle, and a
reader who does serve the folder over HTTP gets the normal path.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Files the SPA asks for by name. Anything else in data/ is carried but not
# indexed, since the app would never look for it.
_DATA_FILES = [
    "samples.json", "asvs.json", "counts.json", "network.json", "taxonomy.json",
    "heatmap.json", "aggregated_counts.json", "provenance.json",
    "run_info.json", "run_manifest.json", "tree.nwk",
]

_README = """\
{title}

BioProject {bioproject}{released}
Run {slug} — {portal_url}

Open index.html in a browser. Everything it needs is in this folder; nothing is
fetched from the network, and it works with no internet connection.

The numbers behind the figures are in data/ as JSON, and again inside data.js
where the page reads them from. tree.nwk is the phylogeny in Newick format.
Every table in the Data Tables tab exports to CSV from the page itself.

Built by the dānaSeq amplicon pipeline, {build}.
"""


def _payload(data_dir: Path) -> dict[str, str]:
    """{'./data/samples.json': '<base64 of gzip bytes>'} for what the app loads."""
    out = {}
    for name in _DATA_FILES:
        plain, packed = data_dir / name, data_dir / f"{name}.gz"
        if packed.exists():
            raw = packed.read_bytes()          # already gzip on disk
        elif plain.exists():
            raw = gzip.compress(plain.read_bytes(), 6)
        else:
            continue
        out[f"./data/{name}"] = base64.b64encode(raw).decode("ascii")
    return out


def _with_data_script(index_html: str) -> str:
    """Load data.js before the app, so the payload is there when it looks."""
    tag = '<script src="./data.js"></script>\n  '
    marker = '<script type="module"'
    if tag.strip() in index_html:
        return index_html
    if marker in index_html:
        return index_html.replace(marker, tag + marker, 1)
    return index_html.replace("</body>", f"  {tag}</body>", 1)


def build_zip(site_dir: Path, run_info: dict | None = None) -> io.BytesIO:
    """Zip a deployed site into something that opens from a file:// URL."""
    site_dir = Path(site_dir)
    info = run_info or {}
    slug = info.get("slug") or site_dir.name
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(site_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(site_dir)
            if rel.name == "data.js":
                continue  # rebuilt below
            data = f.read_bytes()
            if rel.as_posix() == "index.html":
                data = _with_data_script(data.decode("utf-8")).encode("utf-8")
            z.writestr(f"{slug}/{rel.as_posix()}", data)

        payload = _payload(site_dir / "data")
        # One assignment rather than a literal per file: a few large base64
        # strings parse faster than a deeply nested object, and the app only
        # ever indexes into it.
        z.writestr(f"{slug}/data.js",
                   "window.__VIZ_GZ = " + json.dumps(payload) + ";\n")

        released = info.get("registered")
        z.writestr(f"{slug}/README.txt", _README.format(
            title=info.get("title") or "dānaSeq amplicon analysis",
            bioproject=info.get("bioproject") or "(not recorded)",
            released=f", released {released}" if released else "",
            slug=slug,
            portal_url=info.get("portal_url") or "(not recorded)",
            build=info.get("build") or "build not recorded",
        ))

    buf.seek(0)
    logger.info("offline bundle for %s: %d files, %.1f MB",
                slug, len(payload), buf.getbuffer().nbytes / 1e6)
    return buf
