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

import csv
import io
import json
import logging
import shutil
import statistics
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


def results_archive_mtime(slug: str) -> float | None:
    """When the run's results archive was last written, or None if there is none.

    A rerun replaces the archive whole, so this is what says that anything read
    out of it has to be read again. Cheap enough to ask on every page render.
    """
    try:
        return _results_sqsh(slug).stat().st_mtime
    except OSError:
        return None


def _read_primer_fasta(path: Path) -> dict[str, str]:
    """name -> sequence for one of the pipeline's small primer FASTAs.

    A name carrying two different sequences is dropped rather than guessed at:
    the assignment table refers to primers by name only, so there would be
    nothing left to tell the two apart. `inferred` is the name that repeats,
    because a de-novo primer has no published name to carry.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    records: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            records.append([line[1:].split()[0] if line[1:].split() else "", ""])
        elif records:
            records[-1][1] += line.upper()
    by_name: dict[str, set[str]] = {}
    for name, seq in records:
        by_name.setdefault(name, set()).add(seq)
    return {n: next(iter(seqs)) for n, seqs in by_name.items() if len(seqs) == 1}


# The only members `assay_facts` needs. Together a few kB, so the extraction
# costs about as much as opening the archive at all.
# What the pipeline quotes assay coordinates against, and what a placement there
# establishes. Which organism supplied the reference is an implementation detail
# of the measurement: a placement on E. coli says the assay targets bacteria, not
# that anyone expects E. coli in the sample.
_REFERENCE_GENE = {
    "ecoli_16S": ("bacterial", "16S rRNA"),
    "yeast_18S": ("eukaryotic", "18S rRNA"),
}

_IUPAC = set("ACGTRYSWKMBDHVN")


def _looks_like_sequence(text: str) -> bool:
    """A primer detected from the reads has no name, so its sequence stands in."""
    t = (text or "").upper()
    return len(t) >= 14 and set(t) <= _IUPAC


def _describe_placement(place: str) -> tuple[str, str]:
    """`ecoli_16S@534-786` as (gene, span) a reader can use.

    A run older than the naming work has no gene recorded but does have this, and
    it says the same thing: the assay is bacterial 16S, running 534 to 786. Left
    as-is when the reference is one this does not know.
    """
    if not place or "@" not in place:
        return "", ""
    ref, _, span = place.partition("@")
    domain, gene = _REFERENCE_GENE.get(ref, ("", ""))
    if not gene:
        return "", span
    return f"{domain} {gene}", f"{domain} {gene.split()[0]} {span}" if span else ""


_ASSAY_MEMBERS = ("trimmed/primer_assignment.tsv", "primers",
                  "viz/renorm_stats.json", "viz/samples.json",
                  "viz/data/overview.json")


def _count(value) -> int:
    """A count from the archive, which records some of them as text."""
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _sequencing_totals(path: Path) -> dict:
    """Reads and bases submitted for the run, summed over its samples.

    From what the archive was given rather than what survived it: the size of a
    study is what was sequenced, and a filter discarding most of it is a fact
    about the run rather than a smaller study.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    rows = data if isinstance(data, list) else data.get("samples") or []

    def summed(field):
        """(total, rows carrying it) — a total is only a total if every row has one."""
        have = [r for r in rows if r.get(field) is not None]
        return sum(_count(r.get(field)) for r in have), len(have)

    # read_count and base_count come from the archive's own metadata, and are
    # absent for reads nobody deposited. Summing what happens to be there gives a
    # number that looks like the run's size and is not: PRJNA599410 carries
    # read_count on 2 of its 337 samples, and their 24,354 reads were reported as
    # the whole study against the 5,644,657 it processed. A partial sum is worse
    # than the smaller question answered completely, so it is used only when
    # every sample carries it, and what reached the ASV table is used otherwise.
    reads, have_reads = summed("read_count")
    bases, have_bases = summed("base_count")
    basis = "deposited"
    if have_reads < len(rows):
        reads, basis = summed("total_reads")[0], "processed"
        if have_bases < len(rows):
            bases = 0
    return {"reads": reads, "bases": bases, "samples": len(rows),
            "reads_basis": basis}

def assay_facts(slug: str) -> dict | None:
    """Which assays this run's samples were actually assigned to, and its ASV total.

    Read from the results archive rather than the deployed viz site: the archive
    is the run's own record, it is where the portal already reads every other
    result fact from, and it is the only copy that carries primer *sequences* —
    the deployed `samples.json` names the primers but not the strings cutadapt
    was given.

      * `trimmed/primer_assignment.tsv` — one row per sample, naming the assay it
        was trimmed as (gene, region, forward and reverse primer names).
      * `primers/fwd.fa`, `primers/rev.fa` — what those names stand for.
      * `viz/renorm_stats.json` — partitions every ASV in the run by lineage
        group, so its counts sum to the run's ASV total.

    Returns None when there is no archive to read, so a caller can tell "not yet"
    from `{"assays": [], ...}` — an archive that holds none of this, which is
    every pipeline that does not remove primers.
    """
    sqsh = _results_sqsh(slug)
    try:
        mtime = sqsh.stat().st_mtime
    except OSError:
        return None
    tmp = Path(tempfile.mkdtemp(prefix=f"omc-assay-{slug}-"))
    try:
        try:
            subprocess.run(
                ["unsquashfs", "-f", "-d", str(tmp), str(sqsh), *_ASSAY_MEMBERS],
                check=True, capture_output=True, timeout=120,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            logger.warning("assay facts: cannot read %s: %s", sqsh, exc)
            return None

        fwd_seqs = _read_primer_fasta(tmp / "primers" / "fwd.fa")
        rev_seqs = _read_primer_fasta(tmp / "primers" / "rev.fa")

        # One row per sample; a mixed-target study has several distinct assays in
        # it, so rows are grouped and counted rather than read one by one.
        groups: dict[tuple, dict] = {}
        table = tmp / "trimmed" / "primer_assignment.tsv"
        if table.exists():
            with open(table, newline="", errors="replace") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    # Keyed the way the viz keys it, so the two surfaces of one
                    # run cannot disagree about how many assays it has. Where the
                    # amplicon is placed, the placement is the identity and the
                    # primers are left out of it: reads that arrived pre-trimmed
                    # have a consensus of amplicon in that field, which differs
                    # between samples of one assay and splits it into as many
                    # sets as it has variants. PRJNA779070 has three and shows
                    # six when keyed on the primers (danaSeq #53).
                    place = (row.get("assay_set") or "").strip()
                    key = (
                        (row.get("assay_gene") or "").strip(),
                        (row.get("assay_region") or "").strip(),
                        place,
                        "" if place else (row.get("assay_primer_fwd") or "").strip(),
                        "" if place else (row.get("assay_primer_rev") or "").strip(),
                    )
                    fwd_name = (row.get("assay_primer_fwd") or "").strip()
                    rev_name = (row.get("assay_primer_rev") or "").strip()
                    g = groups.setdefault(key, {
                        "gene": key[0], "region": key[1], "set": key[2],
                        "lineage": (row.get("assay_gene_lineage") or "").strip(),
                        # A supplied primer is named and the fasta says what
                        # the name stands for; a detected one has only its
                        # sequence, which is then both.
                        "fwd_name": fwd_name,
                        "fwd": fwd_seqs.get(fwd_name) or (
                            fwd_name if _looks_like_sequence(fwd_name) else ""),
                        "rev_name": rev_name,
                        "rev": rev_seqs.get(rev_name) or (
                            rev_name if _looks_like_sequence(rev_name) else ""),
                        "samples": 0, "_matched": [],
                    })
                    g["samples"] += 1
                    try:
                        g["_matched"].append(float(row["assay_match_fraction"]))
                    except (KeyError, TypeError, ValueError):
                        pass

        assays = []
        for g in sorted(groups.values(), key=lambda g: -g["samples"]):
            # The placement says two things — which gene, and which stretch of
            # it — and they are needed separately. The gene is only taken from it
            # when the run recorded none, but the span is the only source of the
            # coordinates whether or not the gene was named, and without it the
            # page falls back to printing the placement key itself:
            # "ecoli_16S@969-1406", which names a reference sequence at a reader
            # who wants a region.
            placed_gene, placed_span = (
                _describe_placement(g["set"]) if g["set"] else ("", ""))
            if not g["gene"]:
                g["gene"] = placed_gene
            if not g.get("span"):
                g["span"] = placed_span
            # What to call it, in the words the viz uses for the same run: an
            # assay with nothing but a sequence was inferred from the reads
            # rather than unidentifiable, and saying so is the difference
            # between a fact and a shrug.
            g["heading"] = g["gene"] or (
                "inferred primers"
                if _looks_like_sequence(g["fwd_name"]) or _looks_like_sequence(g["rev_name"])
                else "unidentified assay")
            matched = g.pop("_matched")
            # The median, because one sample that matched badly says less about
            # the assay than where the middle of the run sits.
            g["match_fraction"] = round(statistics.median(matched), 4) if matched else None
            assays.append(g)

        n_asvs = None
        stats = tmp / "viz" / "renorm_stats.json"
        if stats.exists():
            try:
                groups_json = json.loads(stats.read_text())
                n_asvs = sum(int(v.get("n_asvs") or 0) for v in groups_json.values())
            except (ValueError, AttributeError, TypeError):
                n_asvs = None

        totals = _sequencing_totals(tmp / "viz" / "samples.json")
        # What a MAG run produced, counted the way that pipeline counts it. Its
        # overview is the equivalent of the amplicon renormalisation summary and
        # sits one directory deeper.
        n_mags = None
        try:
            with open(tmp / "viz" / "data" / "overview.json") as fh:
                overview = json.load(fh)
            n_mags = int(overview.get("n_mags") or 0)
        except (OSError, ValueError, TypeError):
            pass
        return {"mtime": mtime, "assays": assays, "n_asvs": n_asvs,
                "n_mags": n_mags, **totals}
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
        #
        # Before the index below, which lists the directory as it finds it. A
        # file written after the index is a file the page never asks for, and
        # the run then has no title, no accession and no registration date on a
        # tab whose whole subject is where the data came from.
        (site_dir / "data").mkdir(exist_ok=True)
        with open(site_dir / "data" / "run_info.json", "w") as fh:
            json.dump(run_info, fh, indent=2)

    # What is actually there, so the page can ask for it directly. The loader
    # otherwise probes for a gzipped copy of everything and falls back, which
    # costs a 404 per file on every load — nine of them on a run predating the
    # provenance and manifest files, which are simply absent (danaSeq #28).
    if staged:
        names = sorted(f.name for f in data_dir.iterdir() if f.is_file())
        with open(data_dir / "index.json", "w") as fh:
            json.dump(names, fh)
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
