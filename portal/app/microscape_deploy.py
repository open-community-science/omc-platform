"""Deploy microscape viz sites to microscape.app.

Each user's amplicon results are hosted in a dedicated `omc-<login>` lab so the
user only authenticates with GitHub (shared OAuth) and never handles a
cross-domain key. OMC provisions the lab + deploy key via the service-token
provision endpoint, then pushes the built static viz site (from the pipeline's
`site/` output) to the deploy endpoint. Both apps are co-located on arbutus.

Flow (portal-side, after results transfer):
  1. unsquashfs `site/` out of the results archive
  2. POST /api/v1/provision  (service token)  -> per-user lab + deploy key
  3. POST /api/v1/deploy      (deploy key + tarball) -> hosted at /<slug>/
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import httpx

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def provision_lab(github_id: int, github_login: str, display_name: str | None = None,
                        email: str | None = None, avatar_url: str | None = None) -> dict:
    """Ensure the user's omc-<login> lab + deploy key; return the provision result."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.microscape_app_url}/api/v1/provision",
            headers={"Authorization": f"Bearer {settings.microscape_provision_token}"},
            json={
                "github_id": github_id,
                "github_login": github_login,
                "display_name": display_name,
                "email": email,
                "avatar_url": avatar_url,
            },
        )
    resp.raise_for_status()
    return resp.json()


def _results_sqsh(slug: str) -> Path:
    return Path(settings.local_download_path).parent / "results" / f"{slug}.sqsh"


def _extract_site(slug: str) -> Path | None:
    """unsquashfs the `site/` subtree from the results archive.

    Returns the directory that actually contains index.html (BUNDLE_VIZ_SITE may
    nest it under site/dist/), or None if there's no built site.
    """
    sqsh = _results_sqsh(slug)
    if not sqsh.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix=f"omc-deploy-{slug}-"))
    try:
        subprocess.run(
            ["unsquashfs", "-f", "-d", str(tmp), str(sqsh), "site"],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    index = next(tmp.rglob("index.html"), None)
    if not index:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return index.parent


def _web_readable(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """Force world-readable modes (dirs 755, files 644).

    Files unpacked from the results squashfs are owner-only, and tar preserves
    that, so the deployed run ended up 0700/0600 and nginx (www-data) served
    403 Forbidden. Normalise here so the site is readable regardless of how the
    source tree happened to be permissioned.
    """
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def _tar_site(site_dir: Path) -> bytes:
    """Tar a directory's contents with a flat root (index.html, assets/, data/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(site_dir.iterdir()):
            tar.add(item, arcname=item.name, filter=_web_readable)
    return buf.getvalue()


async def deploy_submission(submission, user, visibility: str = "public") -> str | None:
    """Provision the user's lab and push the run's viz site to microscape.app.

    Returns the public run URL on success, or None (best-effort — never raises
    into the caller; deploy failures shouldn't fail the pipeline).

    Runs deploy as *public* so the "Open viz" link on the submission page just
    works — for the author, collaborators, and reviewers they share it with,
    without anyone needing a microscape.app login or lab membership. Private
    runs 302 to the homepage unless the viewer is logged in AND their active lab
    is the owning lab, which made results look undeployed. Matches OMC's
    open-science model; pass visibility="private" to override per deploy.
    """
    if not settings.microscape_provision_token:
        logger.info("microscape deploy skipped for %s: no provision token", submission.slug)
        return None

    site_dir = _extract_site(submission.slug)
    if site_dir is None:
        logger.warning("microscape deploy: no built site/ in results for %s", submission.slug)
        return None
    tmp_root = site_dir
    while tmp_root.parent != tmp_root and not tmp_root.name.startswith("omc-deploy-"):
        tmp_root = tmp_root.parent

    try:
        prov = await provision_lab(
            user.github_id, user.github_login,
            getattr(user, "github_name", None), getattr(user, "github_email", None),
            getattr(user, "github_avatar_url", None),
        )
        tarball = _tar_site(site_dir)
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{settings.microscape_app_url}/api/v1/deploy",
                headers={
                    "Authorization": f"Bearer {prov['deploy_key']}",
                    "X-Microscape-Slug": submission.slug,
                    "X-Microscape-Pipeline": "microscape-nf",
                    "X-Microscape-Name": (submission.title or submission.slug)[:120],
                    "X-Microscape-Visibility": visibility,
                    "Content-Type": "application/gzip",
                },
                content=tarball,
            )
        resp.raise_for_status()
        url = f"{settings.microscape_app_public_url.rstrip('/')}/{submission.slug}/"
        logger.info("microscape deploy OK: %s -> %s (lab %s)", submission.slug, url, prov.get("lab_slug"))
        return url
    except Exception as e:
        logger.warning("microscape deploy failed for %s: %s", submission.slug, e)
        return None
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
