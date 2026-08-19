"""Package a published run so it can be read without a network.

A deployed site is a static SPA: an HTML file that pulls in a module script, a
stylesheet and its data. Opened from a `file://` URL none of those arrive. A
module script is fetched under CORS even when a <script> tag names it, a
stylesheet likewise, and `file://` is its own opaque origin — so the browser
refuses its own sibling files and the page renders nothing at all.

Inline content is not fetched, so it is the only thing that survives. The bundle
is therefore one HTML file with the script, the stylesheet and the data all
inlined, and no second file for the page to ask for. The run's JSON is kept
alongside as well, for a reader who wants the numbers rather than the page.

Fonts are dropped rather than inlined: they cannot load from `file://` either,
the stack already falls back to the system UI font, and carrying them would add
megabytes to buy nothing.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import re
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

Open index.html in a browser. It is one self-contained file — the page, its code
and its data — so it works with no server and no internet connection.

The numbers behind the figures are in data/ as JSON, for reading rather than
viewing; tree.nwk is the phylogeny in Newick format. Every table in the Data
Tables tab also exports to CSV from the page itself.

The page uses your system font rather than the one the website uses, which a
browser will not load from a local file.

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


_LINK_RE = re.compile(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_SCRIPT_RE = re.compile(r'<script([^>]*)\ssrc=["\']([^"\']+)["\'][^>]*>\s*</script>', re.I)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}", re.I)


def _read(site_dir: Path, url: str) -> str | None:
    """A file the page refers to, resolved against the site root."""
    rel = url.split("?", 1)[0].split("#", 1)[0].lstrip("./")
    path = site_dir / rel
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("offline bundle: %s referenced but not readable", rel)
        return None


def _selfcontained(index_html: str, site_dir: Path, payload: dict) -> str:
    """One HTML file: nothing left for the page to fetch."""

    def inline_css(m):
        href = _HREF_RE.search(m.group(0))
        css = _read(site_dir, href.group(1)) if href else None
        if css is None:
            return m.group(0)
        # The font files are not carried, so the rules that name them would only
        # produce failed requests; the family list falls back on its own.
        return "<style>\n" + _FONT_FACE_RE.sub("", css) + "\n</style>"

    def inline_js(m):
        attrs, src = m.group(1), m.group(2)
        js = _read(site_dir, src)
        if js is None:
            return m.group(0)
        # `type` is kept — an inline module is still a module, and this bundle is
        # one — while `crossorigin` and `src` are meaningless once inlined.
        kind = ' type="module"' if "module" in attrs else ""
        return f"<script{kind}>\n{js}\n</script>"

    html = _LINK_RE.sub(inline_css, index_html)
    html = _SCRIPT_RE.sub(inline_js, html)

    # Before the app, and a classic script so it runs during parsing rather than
    # after the deferred module that reads it.
    data = ("<script>window.__VIZ_GZ = " + json.dumps(payload) + ";</script>\n")
    if "</head>" in html:
        html = html.replace("</head>", data + "</head>", 1)
    else:
        html = data + html
    return html


def build_zip(site_dir: Path, run_info: dict | None = None) -> io.BytesIO:
    """Zip a deployed site into something that opens from a file:// URL."""
    site_dir = Path(site_dir)
    info = run_info or {}
    slug = info.get("slug") or site_dir.name
    buf = io.BytesIO()

    payload = _payload(site_dir / "data")
    index = (site_dir / "index.html").read_text(encoding="utf-8", errors="replace")
    page = _selfcontained(index, site_dir, payload)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(f"{slug}/index.html", page)
        # The numbers on their own, for reading rather than viewing. assets/ is
        # not carried: everything in it is now part of the page above, and
        # shipping it again would double the download.
        for f in sorted((site_dir / "data").glob("*")):
            if f.is_file():
                z.writestr(f"{slug}/data/{f.name}", f.read_bytes())

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
