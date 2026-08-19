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
import json
import logging
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import httpx
from sqlalchemy.orm import attributes

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


def results_have_output(slug: str) -> bool:
    """True if the results archive actually contains pipeline output (a viz site
    or a final seqtab), False for an empty/failed run.

    A microscape run whose REMOVE_PRIMERS steps all failed still exits 0 (task
    errors are ignored to keep the node alive) and gets marked "transferred",
    producing an all-empty archive that must not be reported as success.
    """
    sqsh = _results_sqsh(slug)
    if not sqsh.exists():
        return False
    try:
        out = subprocess.run(
            ["unsquashfs", "-l", str(sqsh)],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return True  # can't tell — don't falsely fail a real run
    return ("/site/" in out or "/viz/" in out or "seqtab_final" in out)


def diagnose_empty_run(slug: str) -> str:
    """Say where an empty run actually lost its reads, reading the pipeline's stats.

    A run that produces nothing is not self-explanatory, and guessing costs real
    time: PRJNA779070 and PRJNA895866 were both reported as "check primers" when
    cutadapt had written 96-99.8% of pairs and the loss was entirely at the
    quality filter, where the truncation length exceeded the reads. Read the
    numbers the pipeline already wrote and name the stage that emptied the run.
    """
    generic = "Pipeline finished but produced no results."
    sqsh = _results_sqsh(slug)
    if not sqsh.exists():
        return generic
    tmp = Path(tempfile.mkdtemp(prefix=f"omc-diag-{slug}-"))
    try:
        try:
            subprocess.run(
                ["unsquashfs", "-f", "-d", str(tmp), str(sqsh),
                 "filtered", "quality_check", "trimmed"],
                check=True, capture_output=True, timeout=180,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return generic

        # Primer removal: did cutadapt keep anything?
        pairs_in = pairs_out = 0
        for log in (tmp / "trimmed").glob("*_cutadapt.log"):
            for line in log.read_text(errors="replace").splitlines():
                if line.startswith("Total read pairs processed:"):
                    pairs_in += int(line.split(":")[1].strip().replace(",", ""))
                elif line.startswith("Pairs written (passing filters):"):
                    pairs_out += int(line.split(":")[1].split("(")[0].strip().replace(",", ""))

        # Quality filter: reads in vs out, per sample.
        filt_in = filt_out = 0
        n_samples = n_zero = 0
        for stats in (tmp / "filtered").glob("*_filt_stats.tsv"):
            for line in stats.read_text(errors="replace").splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                try:
                    r_in, r_out = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                n_samples += 1
                filt_in += r_in
                filt_out += r_out
                if r_out == 0:
                    n_zero += 1

        if pairs_in and pairs_out == 0:
            return (
                f"No reads survived primer removal: cutadapt kept 0 of {pairs_in:,} "
                f"read pairs. The primers do not match these reads — check the "
                f"primer sequences and orientation."
            )
        if n_samples and filt_out == 0:
            msg = (
                f"All {n_samples} samples lost their reads at the quality filter, "
                f"not at primer removal"
            )
            if pairs_in:
                msg += f" (cutadapt kept {100 * pairs_out / pairs_in:.1f}% of pairs)"
            # The usual cause: a truncation length longer than the reads.
            for policy in (tmp / "quality_check").glob("*_trunc_policy.tsv"):
                vals = {}
                for line in policy.read_text(errors="replace").splitlines():
                    parts = line.split("\t")
                    if len(parts) == 2:
                        vals[parts[0]] = parts[1]
                past = vals.get("samples_truncated_past_read_len", "0")
                if past.isdigit() and int(past) > 0:
                    msg += (
                        f". Truncation was fwd={vals.get('trunc_len_fwd_applied', '?')} "
                        f"rev={vals.get('trunc_len_rev_applied', '?')} while {past} "
                        f"sample(s) have shorter reads — dada2 discards reads shorter "
                        f"than truncLen"
                    )
                    break
            return msg + "."
        if n_zero and n_samples:
            return (
                f"Pipeline produced no final table: {n_zero} of {n_samples} samples "
                f"came out of the quality filter empty."
            )
        return generic
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ENA carries a release date for every study, keyed by the same accession NCBI
# issues, and answers without a key or an account. NCBI's own Registration_Date
# is behind an Entrez call that the deploy path has no other reason to make.
_ENA_STUDY = "https://www.ebi.ac.uk/ena/portal/api/search"


async def bioproject_dates(accession: str) -> dict:
    """{'first_public': 'YYYY-MM-DD', 'last_updated': ...} for a BioProject.

    Empty when the accession is unknown to ENA or ENA is unreachable — a run
    still deploys without a date on it.
    """
    if not accession:
        return {}
    params = {
        "result": "study",
        "query": f"study_accession={accession}",
        "fields": "first_public,last_updated",
        "format": "tsv",
        "limit": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(_ENA_STUDY, params=params)
        r.raise_for_status()
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return {}
        header = lines[0].split("\t")
        row = lines[1].split("\t")
        got = dict(zip(header, row))
        return {k: got.get(k, "") for k in ("first_public", "last_updated") if got.get(k)}
    except Exception as exc:
        logger.warning("ENA date lookup failed for %s: %s", accession, exc)
        return {}


def _extract_site(slug: str, site_source: Path | None = None,
                  run_info: dict | None = None) -> Path | None:
    """unsquashfs the built site + its viz data from the results archive.

    The pipeline writes these to two separate trees: the SPA bundle lands in
    `site/` (nested under site/dist/ by BUNDLE_VIZ_SITE) while the data JSONs
    land in `viz/`. The SPA fetches them from `data/` relative to its own root,
    so the viz/ payload is copied into <site>/data/ here — without it the page
    loads but reports "0 samples | 0 ASVs".

    `site_source` replaces the archive's own bundle with one built elsewhere, so
    a run can be re-skinned with a newer viz without rerunning the pipeline. The
    data still comes from that run's archive — only the app is swapped.

    Returns the directory containing index.html, or None if there's no built site.
    """
    sqsh = _results_sqsh(slug)
    if not sqsh.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix=f"omc-deploy-{slug}-"))
    # Without a replacement bundle the archive has to supply one; with it, only
    # the data is needed and a missing site/ is no longer fatal.
    members = ["viz"] if site_source else ["site", "viz"]
    try:
        subprocess.run(
            ["unsquashfs", "-f", "-d", str(tmp), str(sqsh), *members],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    if site_source:
        staged = tmp / "site"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(site_source, staged)
    index = next(tmp.rglob("index.html"), None)
    if not index:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    site_dir = index.parent

    # Stage the viz payload as the site's data/, which is where every SPA fetches
    # from. The amplicon pipeline writes its JSONs straight into viz/; the MAG
    # pipelines put them in viz/data/ and keep other things — a build tree, the
    # site they shipped with — alongside. So the payload is viz/data when that
    # exists and viz itself otherwise, and either way only the files directly in
    # it: recursing pulls a whole node_modules into data/ one file at a time.
    viz_dir = tmp / "viz"
    payload = viz_dir / "data" if (viz_dir / "data").is_dir() else viz_dir
    staged = 0
    if payload.is_dir():
        data_dir = site_dir / "data"
        data_dir.mkdir(exist_ok=True)
        for f in payload.iterdir():
            if f.is_file():
                shutil.copy2(f, data_dir / f.name)
                staged += 1
    if not staged:
        logger.warning("no viz/ data in results for %s — site will render empty", slug)

    if run_info:
        # What the study is called and which BioProject it came from are OMC's
        # to know: the pipeline is handed a directory of reads and never learns
        # the accession. Written at deploy so the page can say whose data this
        # is without the viewer going back to the portal to find out.
        (site_dir / "data").mkdir(exist_ok=True)
        with open(site_dir / "data" / "run_info.json", "w") as fh:
            json.dump(run_info, fh, indent=2)
    return site_dir


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


async def deploy_submission(submission, user, visibility: str = "public",
                            site_source: Path | None = None) -> str | None:
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

    # Asked once and kept on the submission: the date a study was released does
    # not change, and a refresh re-deploys every run at once.
    meta = dict(submission.sample_metadata or {})
    dates = meta.get("bioproject_dates")
    if dates is None:
        dates = await bioproject_dates(submission.bioproject_accession or "")
        meta["bioproject_dates"] = dates
        submission.sample_metadata = meta
        attributes.flag_modified(submission, "sample_metadata")

    site_dir = _extract_site(
        submission.slug, site_source=site_source,
        run_info={
            "slug": submission.slug,
            "title": submission.title or "",
            "bioproject": submission.bioproject_accession or "",
            "registered": (dates or {}).get("first_public", ""),
            "updated": (dates or {}).get("last_updated", ""),
            "pipeline": submission.pipeline.value if submission.pipeline else "",
            "cluster": submission.target_cluster or "",
            "build": (submission.image_revision or "").split("=")[-1],
            "portal_url": f"{settings.portal_public_url.rstrip('/')}"
                          f"/submissions/{submission.slug}",
        },
    )
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
                    "X-Microscape-Pipeline": "danaseq-illumina-amplicon",
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
