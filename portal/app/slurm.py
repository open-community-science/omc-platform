"""SLURM job submission and monitoring — SSH-free.

Job flow:
  1. Portal downloads SRA data locally on arbutus, stages for HTTP pickup
  2. Cron on fir polls /staging/ready, downloads files, submits sbatch
  3. Fir pushes status updates back to /staging/{slug}/status over HTTP
  4. Portal reads local staging markers + pushed HPC status (no SSH)
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .database import Submission, PipelineType

settings = get_settings()
logger = logging.getLogger(__name__)


def _amplicon_primer_prelude(submission: Submission) -> tuple[str, str]:
    """Return (shell_prelude, primer_args) passing OMC-resolved primers to the amplicon pipeline.

    OMC resolves primers before the run — from submission metadata where it
    exists, otherwise inferred from the reads — and passes them explicitly, so
    what was trimmed is recorded on the submission, visible in the portal and
    correctable by hand, instead of being decided inside the job.

    The pipeline detects its own primers when none are passed — DETECT_PRIMERS
    samples reads, matches 5' ends against its curated table, and writes a FASTA
    for REMOVE_PRIMERS — but it cannot see the study metadata OMC already holds.

    Crucially we emit *every* resolved set into one combined fwd.fa / rev.fa.
    cutadapt with `-g file:` tries each primer and trims the best match per
    read, so a BioProject that mixes amplicon targets (PRJNA1473294 is really
    16S + 18S despite all runs being labelled "16S") is handled in a single
    pass — 16S reads get 341F, 18S reads get TAReuk, both survive. This is why
    it's safe to force detected primers now, unlike forcing a single inferred
    pair globally (which discarded 99.9% of the mismatched reads).

    Either exact primers or none: when nothing is resolved we pass no primer
    arguments and let the pipeline detect. Handing cutadapt the whole curated
    pool instead looks harmless but is not — it picks whichever of a hundred
    candidates scores best in each sample independently, and the pipeline keys
    its truncation and error-model groups on the name of whatever was matched,
    so one assay is split into as many groups as the pool has near-synonyms.
    """
    p = submission.primers or {}
    # Collect every resolved pair: the primary plus any additional detected sets,
    # deduped by (fwd, rev) so a set that repeats the primary isn't listed twice.
    pairs, _seen = [], set()
    for src in ([p] + list(p.get("sets") or [])):
        fwd, rev = (src.get("fwd") or "").strip(), (src.get("rev") or "").strip()
        if fwd and rev and (fwd, rev) not in _seen:
            _seen.add((fwd, rev))
            pairs.append((fwd, rev, src.get("fwd_name", "fwd"), src.get("rev_name", "rev")))

    source = p.get("source", "detected")
    if not pairs:
        return "", ""

    # De-dup sequences across sets so the combined FASTA stays tidy.
    def _fasta(entries: dict) -> str:
        return "".join(f">{n}\\n{s}\\n" for s, n in entries.items())

    fwd_seen, rev_seen = {}, {}
    for fwd, rev, fn, rn in pairs:
        fwd_seen.setdefault(fwd.upper(), fn or "fwd")
        rev_seen.setdefault(rev.upper(), rn or "rev")
    fwd_fa = _fasta(fwd_seen)
    rev_fa = _fasta(rev_seen)
    label = " + ".join(f"{fn}/{rn}" for _, _, fn, rn in pairs)

    prelude = f"""echo ">>> Primers: {label} (source: {source})"
mkdir -p "${{OUTPUT_DIR}}/primers"
printf '{fwd_fa}' > "${{OUTPUT_DIR}}/primers/fwd.fa"
printf '{rev_fa}' > "${{OUTPUT_DIR}}/primers/rev.fa"
"""
    args = (' \\\n    --primers_fwd "${OUTPUT_DIR}/primers/fwd.fa"'
            ' \\\n    --primers_rev "${OUTPUT_DIR}/primers/rev.fa"')
    return prelude, args


def _amplicon_metadata_prelude(submission: Submission) -> tuple[str, str]:
    """Return (shell_prelude, metadata_args) staging a sample metadata TSV.

    OMC already holds the SRA record for every run, but never handed it to the
    pipeline, so LOAD_METADATA never ran: the viz had no sample grouping and
    generated Methods sections said "sample metadata not provided". Emit the
    records as a TSV keyed by run accession (which is what the fastq filenames —
    and therefore the pipeline's sample ids — are derived from).

    The TSV is base64-encoded into the script so arbitrary field text can't
    break quoting.
    """
    meta = submission.sample_metadata or {}
    records = meta.get("sample_records") or []
    if not records:
        return "", ""

    # Keep fields that are useful for grouping/plotting and stable across SRA.
    cols = [
        ("sample_name", "run_accession"),
        ("sample_accession", "sample_accession"),
        ("library_name", "library_name"),
        ("description", "description"),
        ("experiment_title", "experiment_title"),
        ("library_strategy", "library_strategy"),
        ("library_source", "library_source"),
        ("library_layout", "library_layout"),
        ("instrument_model", "instrument_model"),
        ("center_name", "center_name"),
        ("read_count", "read_count"),
        ("base_count", "base_count"),
        # collection_date must be the biological SAMPLING date, not ENA's record
        # creation time (first_created) — else temporal/ecological grouping is wrong
        # (issue #33). Keep first_created in its own column for provenance.
        ("collection_date", "collection_date"),
        ("first_created", "first_created"),
    ]

    def _clean(v) -> str:
        # Tabs/newlines would corrupt the TSV; collapse them.
        return " ".join(str(v if v is not None else "").split())

    seen, lines = set(), ["\t".join(c for c, _ in cols)]
    for rec in records:
        sid = _clean(rec.get("run_accession"))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        lines.append("\t".join(_clean(rec.get(src)) for _, src in cols))
    if len(lines) < 2:
        return "", ""

    import base64 as _b64
    blob = _b64.b64encode(("\n".join(lines) + "\n").encode()).decode()
    prelude = f"""echo ">>> Staging sample metadata ({len(lines) - 1} samples)"
mkdir -p "${{OUTPUT_DIR}}/metadata"
printf '%s' '{blob}' | base64 -d > "${{OUTPUT_DIR}}/metadata/samples.tsv"
"""
    args = (' \\\n    --metadata "${OUTPUT_DIR}/metadata/samples.tsv"'
            ' \\\n    --sample_id_column sample_name')
    return prelude, args


def _build_pipeline_cmd(submission: Submission) -> str:
    """Return the shell command block that runs a given OMC pipeline.

    OMC's user-facing pipelines compose danaSeq building blocks:
      NANOPORE_MAG (Nanopore Metagenome) = nanopore_assembly -> mag_analysis
      ILLUMINA_MAG (Illumina Metagenome) = illumina_assembly -> mag_analysis
      ILLUMINA_AMPLICON (Illumina Amplicons) = danaSeq/illumina_amplicon (self-contained SIF)

    The returned block is executed inside a `set -e` subshell, so any step
    failing aborts the pipeline with its exit code. Uses ${INPUT_DIR},
    ${OUTPUT_DIR} exported by the surrounding sbatch script. Each danaSeq
    run-*.sh --container resolves its component's rebuilt .danaseq-*.sif and
    picks the container runtime the executing cluster actually has — apptainer
    on fir, singularity on grex. The amplicon SIF is invoked directly, so it
    goes through ${OMC_APPTAINER} for the same reason.
    """
    pipeline = submission.pipeline
    # Reference the executing cluster's paths via shell vars (OMC_GENICE / OMC_DB_DIR),
    # set by the pickup + defaulted in the sbatch header, so the same script is
    # portable across clusters (fir, nibi, …) rather than baking fir's paths.
    nano = "${OMC_GENICE}/danaSeq/nanopore_assembly"
    illu = "${OMC_GENICE}/danaSeq/illumina_assembly"
    mag = "${OMC_GENICE}/danaSeq/mag_analysis"
    db_dir = "${OMC_DB_DIR}"
    # Beside its wrapper in the checkout, like every other component's .sif, so
    # danaSeq/tools/rebuild-sifs.sh keeps it current. An image anywhere else is
    # rebuilt by nothing, and runs silently pin to whatever was last pulled.
    amplicon_sif = "${OMC_GENICE}/danaSeq/illumina_amplicon/.danaseq-illumina-amplicon.sif"

    if pipeline == PipelineType.NANOPORE_MAG:
        # Single co-assembly: results/assembly/assembly.fasta + results/mapping/depths.txt
        return f"""ASM="${{OUTPUT_DIR}}/assembly"; MAG="${{OUTPUT_DIR}}/mag"
echo ">>> Step 1/2: nanopore assembly"
"{nano}/run-nanopore-assembly.sh" --container \\
    --input "${{INPUT_DIR}}/fastq" \\
    --outdir "$ASM"
echo ">>> Step 2/2: MAG analysis"
"{mag}/run-mag-analysis.sh" --container \\
    --assembly "$ASM/assembly/assembly.fasta" \\
    --depths "$ASM/mapping/depths.txt" \\
    --bam_dir "$ASM/mapping/" \\
    --outdir "$MAG" \\
    --all --db_dir "{db_dir}\""""

    # Host (human) read removal needs a bbmap index at ${OMC_DB_DIR}/human_ref.
    # Until that's staged on every cluster, disable it (fine for environmental
    # data); flip settings.illumina_remove_human once the DB is everywhere.
    if settings.illumina_remove_human:
        human_arg = ' \\\n    --human_ref "${OMC_DB_DIR}/human_ref"'
    else:
        human_arg = ' \\\n    --run_remove_human false'

    if pipeline == PipelineType.ILLUMINA_MAG:
        # Per-sample: results/assembly/<s>/<s>.dedupe.fasta + results/mapping/<s>/<s>.depths.txt
        return f"""ASM="${{OUTPUT_DIR}}/assembly"; MAG="${{OUTPUT_DIR}}/mag"
# illumina_assembly discovers pairs via *_R1_*/*_R2_*; SRA delivers _1/_2.
# Add relative symlinks (relative so they resolve inside the read-only bind mount).
( shopt -s nullglob; cd "${{INPUT_DIR}}/fastq" || exit 0
  for f1 in *_1.fastq.gz; do
    b=${{f1%_1.fastq.gz}}
    ln -sf "$f1" "${{b}}_R1_001.fastq.gz"
    [ -e "${{b}}_2.fastq.gz" ] && ln -sf "${{b}}_2.fastq.gz" "${{b}}_R2_001.fastq.gz"
  done )
echo ">>> Step 1/2: illumina assembly"
"{illu}/run-illumina-assembly.sh" --container \\
    --input "${{INPUT_DIR}}/fastq" \\
    --assembly_memory "${{OMC_ASM_MEM_GB}}GB"{human_arg} \\
    --outdir "$ASM"
echo ">>> Step 2/2: MAG analysis (per sample)"
shopt -s nullglob
found=0
for asm in "$ASM"/assembly/*/*.dedupe.fasta; do
    found=1
    s=$(basename "$(dirname "$asm")")
    echo "  --- MAG analysis for sample: $s ---"
    "{mag}/run-mag-analysis.sh" --container \\
        --assembly "$asm" \\
        --depths "$ASM/mapping/$s/$s.depths.txt" \\
        --bam_dir "$ASM/mapping/$s/" \\
        --outdir "$MAG/$s" \\
        --all --db_dir "{db_dir}"
done
[ "$found" -eq 1 ] || {{ echo "ERROR: assembly produced no *.dedupe.fasta"; exit 1; }}"""

    if pipeline == PipelineType.ILLUMINA_AMPLICON:
        # The amplicon stage runs entirely from its SIF (code baked in at /pipeline).
        # The image bakes its Nextflow framework jar and, via the entrypoint, forces the
        # CA bundle to the Ubuntu path, a writable NXF_HOME, and the legacy syntax parser.
        # We still set the CA env here defensively (an older SIF may lack the entrypoint fix).
        # Primers come from OMC's resolver and are passed explicitly when it has
        # them. When it does not, no primer argument is passed and the pipeline
        # detects its own — exact primers or none, never a pool to choose from.
        primer_prelude, primer_args = _amplicon_primer_prelude(submission)
        meta_prelude, meta_args = _amplicon_metadata_prelude(submission)
        # Taxonomy DB gates the taxonomy → BUILD_VIZ branch that produces viz/.
        ref_dbs = settings.amplicon_ref_databases.replace("{db}", "${OMC_DB_DIR}")
        ref_arg = f' \\\n    --ref_databases "{ref_dbs}"' if ref_dbs else ""
        # Bind the reference DB dir(s) into the container so paths resolve.
        ref_binds = ""
        for entry in (ref_dbs.split(";") if ref_dbs else []):
            parts = entry.split(":")
            if len(parts) >= 2 and parts[1].strip():
                import os as _os
                d = _os.path.dirname(parts[1].strip())
                if d:
                    ref_binds += f',"{d}:{d}:ro"'
        return f"""echo ">>> Illumina amplicon analysis"
mkdir -p "${{WORK_DIR}}"
# Match the pipeline's resources to what SLURM actually granted. Its defaults
# (8 threads / 16GB denoise) left most of the requested CPUs and memory idle.
# Denoising gets 75% of the job's memory so concurrent Nextflow tasks still fit.
OMC_CPUS="${{SLURM_CPUS_PER_TASK:-8}}"
OMC_DENOISE_MEM=$(( ${{OMC_MEM_GB:-16}} * 3 / 4 ))
{primer_prelude}{meta_prelude}"${{OMC_APPTAINER}}" run \\
    --env CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \\
    --env SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \\
    --env REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \\
    --bind "${{OUTPUT_DIR}}:${{OUTPUT_DIR}}","${{WORK_DIR}}:${{WORK_DIR}}","${{INPUT_DIR}}/fastq:${{INPUT_DIR}}/fastq:ro"{ref_binds} \\
    "{amplicon_sif}" \\
    run /pipeline/main.nf \\
    --input "${{INPUT_DIR}}/fastq"{primer_args}{ref_arg} \\
    --build_viz_site \\
    --run_phylogeny{meta_args} \\
    --threads "${{OMC_CPUS}}" \\
    --denoise_cpus "${{OMC_CPUS}}" \\
    --denoise_memory "${{OMC_DENOISE_MEM}} GB" \\
    --min_prevalence 3 \\
    --min_samples 1 \\
    -work-dir "${{WORK_DIR}}" \\
    --outdir "${{OUTPUT_DIR}}"
# Read accounting from the raw FASTQ onward. Every stats file the pipeline emits
# starts after primer removal, so samples whose reads cutadapt discarded (wrong
# primer pair for that run) were indistinguishable from shallow ones. Run after
# the pipeline over its published outputs — best effort, never fails the job.
"${{OMC_APPTAINER}}" exec \\
    --bind "${{OUTPUT_DIR}}:${{OUTPUT_DIR}}" \\
    "{amplicon_sif}" \\
    python3 /pipeline/bin/read_tracking.py \\
        "${{OUTPUT_DIR}}/seqtab_final/read_tracking.tsv" \\
        "${{OUTPUT_DIR}}"/trimmed/*_cutadapt.log \\
        "${{OUTPUT_DIR}}"/filtered/*_filt_stats.tsv 2>/dev/null \\
  && tail -1 "${{OUTPUT_DIR}}/seqtab_final/read_tracking.tsv" \\
  || echo "WARN: read tracking unavailable\""""

    raise NotImplementedError(
        f"Pipeline '{pipeline.value}' is not yet available for HPC submission."
    )


# Memory tiers (GB) available on fir; the OOM-retry in omc-pickup.sh walks up
# these when a job is OOM-killed.
_MEM_TIERS = [128, 187, 249, 373, 498]


def _estimate_mem_gb(submission: Submission, attempt: int = 0) -> int:
    """Estimate sbatch memory (GB), escalating a tier per OOM-retry attempt.

    Metagenome assembly (metaSPAdes/Tadpole) dominates memory, so scale from the
    dataset's total bases off a floor, snap to a node tier, and double the target
    per retry attempt. Amplicon is light and fixed.
    """
    pipe = submission.pipeline
    if pipe == PipelineType.ILLUMINA_AMPLICON:
        base = 128
    elif pipe in (PipelineType.ILLUMINA_MAG, PipelineType.NANOPORE_MAG):
        m = submission.sample_metadata or {}
        gbp = (m.get("total_bases") or 0) / 1e9
        base = 128 + int(gbp * 8)   # ~+8 GB per Gbp of input
    else:
        base = 128
    target = base * (2 ** max(0, attempt))
    for t in _MEM_TIERS:
        if t >= target:
            return t
    return _MEM_TIERS[-1]


def _build_pipeline_script(submission: Submission, attempt: int = 0) -> str:
    """Generate sbatch script for pipeline execution (heavy job).

    The pipeline script also pushes status updates back to arbutus
    via the staging API so the portal can track progress without SSH.
    """
    scratch = settings.hpc_scratch
    genice = settings.hpc_genice_dir
    db_dir_default = settings.hpc_db_dir
    # #SBATCH --output can't reference a shell var, so it uses the default scratch
    # path; a cluster overriding OMC_SCRATCH shares the Alliance /home/<user>/scratch
    # layout, so this resolves there too.
    output_dir = f"{settings.results_path}/{submission.slug}"
    accession = submission.bioproject_accession
    mem_gb = _estimate_mem_gb(submission, attempt)

    # Pipeline-specific run command (assembly->mag chain, or amplicon SIF)
    pipeline_cmd = _build_pipeline_cmd(submission)

    return f"""#!/bin/bash
#SBATCH --job-name=omc-run-{submission.slug}
#SBATCH --account={settings.slurm_account}
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=32
#SBATCH --mem={mem_gb}G
#SBATCH --output={output_dir}/slurm-pipeline-%j.out
#SBATCH --error={output_dir}/slurm-pipeline-%j.err

set -uo pipefail

# Cluster paths — env-overridable so the same script runs on any cluster (the
# pickup exports these per-cluster). Defaults match the portal config, so a
# same-layout Alliance cluster (fir, nibi, …) needs no override.
OMC_SCRATCH="${{OMC_SCRATCH:-{scratch}}}"
OMC_GENICE="${{OMC_GENICE:-{genice}}}"
OMC_DB_DIR="${{OMC_DB_DIR:-{db_dir_default}}}"
# Container runtime for the amplicon SIF. Alliance's national clusters ship
# apptainer; grex ships SingularityCE as a module, so its pickup sets this to
# `singularity`. danaSeq's own run-*.sh --container auto-detects the same way.
OMC_APPTAINER="${{OMC_APPTAINER:-apptainer}}"
INPUT_DIR="$OMC_SCRATCH/sra_downloads/{submission.slug}"
OUTPUT_DIR="$OMC_SCRATCH/omc_results/{submission.slug}"
WORK_DIR="$OMC_SCRATCH/omc_work/{submission.slug}"

# Memory bookkeeping. OMC_MEM_GB is the sbatch allocation; the assembly step
# caps its tools below it so they can't overcommit the cgroup and OOM. On an
# OOM kill, omc-pickup.sh seds these (and --mem) up a tier and resubmits.
OMC_MEM_GB={mem_gb}
OMC_MEM_ATTEMPT={attempt}
# Budget handed to assemblers (leave headroom for OS + nextflow overhead).
OMC_ASM_MEM_GB=$(( OMC_MEM_GB - 32 )); [ "$OMC_ASM_MEM_GB" -lt 32 ] && OMC_ASM_MEM_GB=32

# Status reporting — push updates to arbutus over HTTP
# These env vars are set by omc-pickup.sh before sbatch
OMC_STAGING_URL="${{OMC_STAGING_URL:-}}"
OMC_STAGING_KEY="${{OMC_STAGING_KEY:-}}"
SLUG="{submission.slug}"

# $extra is spliced into the JSON body verbatim, so it must be written with
# plain double quotes. Backslash-escaping it (',\"job_id\":...') looks right
# next to the escaping on the line below, but that one is inside double quotes
# where bash unescapes it, while $extra is single-quoted and reaches curl with
# the backslashes intact -- invalid JSON, a 500, and a status the portal never
# receives. `|| true` then hides it, so runs finished silently and only the
# pickup reconciler ever corrected them.
push_status() {{
    local phase="$1"
    local extra="${{2:-}}"
    if [ -n "$OMC_STAGING_URL" ] && [ -n "$OMC_STAGING_KEY" ]; then
        curl -sf -X POST "${{OMC_STAGING_URL}}/staging/${{SLUG}}/status" \\
            -H "Authorization: Bearer ${{OMC_STAGING_KEY}}" \\
            -H "Content-Type: application/json" \\
            --data-raw "{{\\"phase\\":\\"$phase\\"$extra}}" >/dev/null 2>&1 || true
    fi
}}

# Trap SLURM signals (timeout/cancel) to write status markers
cleanup() {{
    echo "Signal caught — marking job as failed"
    echo "failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
    push_status "failed" ',"reason":"Signal caught"'
    exit 1
}}
trap cleanup SIGTERM SIGUSR1 SIGUSR2

# Load modules
module load apptainer 2>/dev/null || true
export APPTAINER_CACHEDIR={scratch}/apptainer_cache
export APPTAINER_TMPDIR=${{SLURM_TMPDIR:-/tmp}}

# Create directories
mkdir -p "${{OUTPUT_DIR}}" "${{WORK_DIR}}"

# Verify download data exists
NUM_FILES=$(ls ${{INPUT_DIR}}/fastq/*.fastq* 2>/dev/null | wc -l)
if [ "$NUM_FILES" -eq 0 ]; then
    echo "ERROR: No fastq files found in ${{INPUT_DIR}}/fastq/"
    echo "failed" > ${{OUTPUT_DIR}}/.status
    echo 1 > ${{OUTPUT_DIR}}/.completed
    push_status "failed" ',"reason":"No fastq files"'
    exit 1
fi

echo "running" > ${{OUTPUT_DIR}}/.status
push_status "running" ',"job_id":"'$SLURM_JOB_ID'","slurm_state":"RUNNING"'

echo "=== OMC Pipeline: {submission.pipeline.value} ==="
echo "Accession: {accession}"
echo "Slug: {submission.slug}"
echo "Input files: $NUM_FILES"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Run the pipeline in a `set -e` subshell so any step in a multi-step chain
# aborts with its exit code; capture it without killing this wrapper.
(
set -e
{pipeline_cmd}
)
PIPELINE_EXIT=$?

echo "--- Pipeline finished with exit code $PIPELINE_EXIT ---"
echo "Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Apptainer squashfuse_ll cleanup can return exit 2 even on success
# (github.com/apptainer/apptainer#2216). Only downgrade that specific code,
# and only when the pipeline actually produced outputs.
if [ $PIPELINE_EXIT -eq 2 ] && [ -n "$(ls -A "${{OUTPUT_DIR}}" 2>/dev/null)" ]; then
    echo "WARNING: Pipeline exited 2 but outputs exist — treating as success (likely Apptainer cleanup timeout)"
    PIPELINE_EXIT=0
fi

# Signal completion
echo $PIPELINE_EXIT > ${{OUTPUT_DIR}}/.completed
if [ $PIPELINE_EXIT -eq 0 ]; then
    echo "completed" > ${{OUTPUT_DIR}}/.status
    push_status "completed" ',"exit_code":"0"'

    # Archive results and work dir to squashfs (reduces inode footprint)
    echo "=== Archiving to squashfs ==="
    push_status "archiving"

    # squashfs-tools only grew -quiet in 4.4; grex ships 4.3, where passing it
    # makes mksquashfs print its usage and exit non-zero. That would strand a
    # pipeline that had already succeeded, so ask before using it.
    MKSQ_QUIET=""
    if mksquashfs -help 2>&1 | grep -q -- '-quiet'; then
        MKSQ_QUIET="-quiet"
    fi

    echo "Archiving results (excluding BAMs and raw reads)..."
    if mksquashfs "${{OUTPUT_DIR}}" "${{OUTPUT_DIR}}.sqsh" -noappend $MKSQ_QUIET -no-xattrs \
        -wildcards -e '*.bam' '*.bam.bai' '*.fastq' '*.fastq.gz' '*.fq' '*.fq.gz'; then
        echo "Results archived: $(du -h "${{OUTPUT_DIR}}.sqsh" | cut -f1)"
    else
        echo "WARNING: Failed to archive results to squashfs"
    fi

    echo "Archiving work dir (excluding raw reads and flye intermediates)..."
    if mksquashfs "${{WORK_DIR}}" "${{WORK_DIR}}.sqsh" -noappend $MKSQ_QUIET -no-xattrs \\
        -wildcards -e '*.fastq' '*.fastq.gz' '*.fq' '*.fq.gz' 'flye_out'; then
        echo "Work dir archived: $(du -h "${{WORK_DIR}}.sqsh" | cut -f1)"
        if [ "${{OMC_CLEANUP_WORKDIR:-false}}" = "true" ]; then
            rm -rf "${{WORK_DIR}}"
            echo "Work dir cleaned up"
        else
            echo "Work dir retained (set OMC_CLEANUP_WORKDIR=true to auto-delete)"
        fi
    else
        echo "WARNING: Failed to archive work dir to squashfs"
    fi

    # Upload results squashfs to arbutus
    if [ -f "${{OUTPUT_DIR}}.sqsh" ] && [ -n "$OMC_STAGING_URL" ] && [ -n "$OMC_STAGING_KEY" ]; then
        echo "Uploading results to arbutus..."
        UPLOAD_RC=0
        curl -sf -X POST "${{OMC_STAGING_URL}}/staging/${{SLUG}}/upload-results" \\
            -H "Authorization: Bearer ${{OMC_STAGING_KEY}}" \\
            -H "Content-Type: application/octet-stream" \\
            -T "${{OUTPUT_DIR}}.sqsh" || UPLOAD_RC=$?
        if [ $UPLOAD_RC -eq 0 ]; then
            echo "Upload complete"
            touch ${{OUTPUT_DIR}}/.transferred
            push_status "transferred" ',"results_format":"archived"'
        else
            echo "WARNING: Upload failed (exit $UPLOAD_RC) — results remain on scratch"
        fi
    fi
else
    echo "failed" > ${{OUTPUT_DIR}}/.status
    push_status "failed" ',"exit_code":"'$PIPELINE_EXIT'","reason":"Pipeline exited with code '$PIPELINE_EXIT'"'
fi
"""


def _build_local_download_wrapper(submission: Submission) -> str:
    """Generate a wrapper script that runs locally on arbutus.

    Downloads SRA data to local staging, writes .ready marker.
    A cron job on fir polls the staging API over HTTP to pick up files
    and submit the pipeline — no SSH required.
    """
    local_dir = f"{settings.local_download_path}/{submission.slug}"
    accession = submission.bioproject_accession

    # Extract run accessions — prefer individual selections from run selector,
    # fall back to all runs from selected type groups
    run_accessions = []
    interview_data = submission.interview_data or {}
    if interview_data.get("_selected_run_accessions"):
        run_accessions = interview_data["_selected_run_accessions"]
    elif submission.selected_runs:
        for row in submission.selected_runs:
            if isinstance(row, dict) and row.get("run_accessions"):
                run_accessions.extend(row["run_accessions"])

    # Helper function: prefetch with retries + backoff, then fasterq-dump
    download_fn = f"""
check_disk() {{
    # Returns available GB on the staging volume
    local avail_kb=$(df --output=avail "${{LOCAL_DIR}}" 2>/dev/null | tail -1)
    echo $(( ${{avail_kb:-0}} / 1048576 ))
}}

download_run() {{
    local acc="$1"
    local STALL_TIMEOUT=600  # kill if no new bytes for 10 min
    local STALL_CHECK=30     # check every 30s
    local MAX_RETRIES=3
    local RETRY_DELAY=30     # seconds between retries
    local MIN_DISK_GB=50     # pause if less than 50GB free

    # Check disk before starting this run
    local avail=$(check_disk)
    if [ "$avail" -lt "$MIN_DISK_GB" ]; then
        echo "  SKIPPED: $acc — only ${{avail}}GB free (need ${{MIN_DISK_GB}}GB)" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi

    echo "Downloading $acc... (${{avail}}GB free)"
    local attempt=1
    while [ $attempt -le $MAX_RETRIES ]; do
        # Run prefetch in background with stall detection
        prefetch "$acc" -O ${{LOCAL_DIR}} &
        local pid=$!
        local last_size=0
        local stall_seconds=0

        while kill -0 $pid 2>/dev/null; do
            sleep $STALL_CHECK
            local cur_size=$(du -sb "${{LOCAL_DIR}}/${{acc}}" 2>/dev/null | cut -f1)
            cur_size=${{cur_size:-0}}
            if [ "$cur_size" -eq "$last_size" ]; then
                stall_seconds=$((stall_seconds + STALL_CHECK))
                if [ $stall_seconds -ge $STALL_TIMEOUT ]; then
                    echo "  prefetch stalled for $acc (no new bytes for ${{STALL_TIMEOUT}}s) — killing"
                    kill $pid 2>/dev/null; wait $pid 2>/dev/null
                    break
                fi
            else
                stall_seconds=0
                last_size=$cur_size
            fi
        done

        # Check if prefetch succeeded
        wait $pid 2>/dev/null
        local exit_code=$?
        if [ $exit_code -eq 0 ]; then
            echo "  prefetch OK for $acc (attempt $attempt)"
            break
        fi

        echo "  prefetch failed (attempt $attempt/$MAX_RETRIES, exit $exit_code)"
        rm -rf "${{LOCAL_DIR}}/${{acc}}" 2>/dev/null
        if [ $attempt -lt $MAX_RETRIES ]; then
            echo "  waiting ${{RETRY_DELAY}}s before retry..."
            sleep $RETRY_DELAY
            RETRY_DELAY=$((RETRY_DELAY * 2))
        fi
        attempt=$((attempt + 1))
    done

    if [ $attempt -gt $MAX_RETRIES ]; then
        echo "  FAILED: $acc (gave up after $MAX_RETRIES attempts)" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi

    fasterq-dump "${{LOCAL_DIR}}/${{acc}}/${{acc}}.sra" -O ${{LOCAL_DIR}}/fastq -t ${{LOCAL_DIR}}/tmp -e 2 && \\
    pigz -p 2 ${{LOCAL_DIR}}/fastq/${{acc}}*.fastq
    if [ $? -ne 0 ]; then
        echo "  FAILED: fasterq-dump/pigz for $acc" | tee -a ${{LOCAL_DIR}}/failed_runs.txt
        return 1
    fi
    # Clean up .sra file to save disk (fastq.gz is what we need)
    rm -rf "${{LOCAL_DIR}}/${{acc}}"
    # Mark this run as ready for pickup by fir
    mkdir -p "${{LOCAL_DIR}}/.run-ready"
    date -u +%Y-%m-%dT%H:%M:%SZ > "${{LOCAL_DIR}}/.run-ready/${{acc}}"
    echo "  Run $acc staged for pickup"
    return 0
}}
export -f check_disk download_run
export LOCAL_DIR

PARALLEL_JOBS=4  # concurrent downloads per job (network-bound)
"""

    if run_accessions:
        # Write accessions to a file and process in parallel
        acc_lines = "\n".join(run_accessions)
        download_cmd = download_fn + f"""
cat > ${{LOCAL_DIR}}/run_list.txt << 'ACCLIST'
{acc_lines}
ACCLIST
echo "Downloading $(wc -l < ${{LOCAL_DIR}}/run_list.txt) runs ($PARALLEL_JOBS in parallel)..."
cat ${{LOCAL_DIR}}/run_list.txt | xargs -P $PARALLEL_JOBS -I {{}} bash -c 'download_run "$@" || true' _ {{}}
"""
    else:
        logger.warning(f"No run_accessions in selected_runs for {submission.slug} — downloading all runs (local mode)")
        download_cmd = f"""{download_fn}
echo "WARNING: No explicit run accessions — downloading ALL runs for {accession}"
echo "Resolving runs for {accession}..."
esearch -db sra -query "{accession}" | efetch -format runinfo | \\
    awk -F',' 'NR>1 && $1 != "" {{print $1}}' > ${{LOCAL_DIR}}/run_list.txt

NUM_RUNS=$(wc -l < ${{LOCAL_DIR}}/run_list.txt)
echo "Found $NUM_RUNS runs to download ($PARALLEL_JOBS in parallel)"

cat ${{LOCAL_DIR}}/run_list.txt | xargs -P $PARALLEL_JOBS -I {{}} bash -c 'download_run "$@" || true' _ {{}}
"""

    return f"""#!/bin/bash
# OMC Local Download Wrapper — runs on arbutus, stages for HTTP pickup by fir
set -uo pipefail

LOCAL_DIR="{local_dir}"

# Trap: if the script dies, write a failure marker
cleanup_on_failure() {{
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "Local download wrapper died with exit code $exit_code at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "failed" > ${{LOCAL_DIR}}/.status
    fi
}}
trap cleanup_on_failure EXIT

mkdir -p "${{LOCAL_DIR}}/fastq" "${{LOCAL_DIR}}/tmp"

echo "=== OMC Local Download: {accession} ==="
echo "Slug: {submission.slug}"
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Pre-flight disk check
AVAIL_GB=$(df --output=avail "${{LOCAL_DIR}}" 2>/dev/null | tail -1)
AVAIL_GB=$(( ${{AVAIL_GB:-0}} / 1048576 ))
echo "Disk available: ${{AVAIL_GB}}GB"
if [ "$AVAIL_GB" -lt 50 ]; then
    echo "ERROR: Not enough disk space (${{AVAIL_GB}}GB free, need at least 50GB)"
    echo "failed" > ${{LOCAL_DIR}}/.status
    exit 1
fi

echo "downloading" > ${{LOCAL_DIR}}/.status

{download_cmd}

echo "Download complete. Files:"
ls -lh ${{LOCAL_DIR}}/fastq/
NUM_FILES=$(ls ${{LOCAL_DIR}}/fastq/*.fastq* 2>/dev/null | wc -l)
FAILED_RUNS=0
if [ -f "${{LOCAL_DIR}}/failed_runs.txt" ]; then
    FAILED_RUNS=$(wc -l < ${{LOCAL_DIR}}/failed_runs.txt)
fi
echo "Total: $NUM_FILES fastq files ($FAILED_RUNS failed)"

if [ "$NUM_FILES" -eq 0 ]; then
    echo "ERROR: No files downloaded at all"
    echo "failed" > ${{LOCAL_DIR}}/.status
    exit 1
fi

if [ "$FAILED_RUNS" -gt 0 ]; then
    echo "WARNING: $FAILED_RUNS run(s) failed but $NUM_FILES files downloaded — proceeding with partial data"
fi

echo "Download finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Clean up temp dir
rm -rf "${{LOCAL_DIR}}/tmp"

# Mark as ready for pickup by fir cron
date -u +%Y-%m-%dT%H:%M:%SZ > ${{LOCAL_DIR}}/.ready
echo "Staged and ready for pickup."

# Clear trap on successful exit
trap - EXIT
"""


def write_cluster_marker(submission: Submission) -> None:
    """Pin the staged run to submission.target_cluster, or unpin it.

    The staging API reads this marker to decide which cluster's pickup loop may
    claim the run; an unpinned run goes to whichever cluster is currently active.
    The marker travels with the data, so routing needs no DB lookup on the
    staging path. No-op before the staging directory exists — the submit path
    writes it again once the directory is there.
    """
    marker = Path(settings.local_download_path) / submission.slug / ".cluster"
    if not marker.parent.is_dir():
        return
    target = (submission.target_cluster or "").strip()
    if target:
        marker.write_text(target)
    else:
        marker.unlink(missing_ok=True)


async def submit_local_download_job(submission: Submission) -> str:
    """Stage scripts for download and mark as queued for the worker.

    No SSH required — pipeline.sh and download.sh are written locally.
    The omc-download-worker picks them up (via .queued marker) and runs
    them with concurrency control. After download, fir cron picks up
    the files over HTTP.

    Returns 'queued' as a placeholder.
    """
    if not settings.slurm_enabled:
        raise RuntimeError("SLURM not enabled")

    local_wrapper = _build_local_download_wrapper(submission)
    pipeline_script = _build_pipeline_script(submission)
    local_dir = f"{settings.local_download_path}/{submission.slug}"

    os.makedirs(local_dir, exist_ok=True)

    # Write submission metadata — travels with the data through the pipeline,
    # gets squashed into the results .sqsh, read by session containers at /data/metadata.json
    import json
    meta = {
        "slug": submission.slug,
        "accession": submission.bioproject_accession,
        "pipeline": submission.pipeline.value,
        "title": submission.title or "",
        "sample_metadata": submission.sample_metadata or {},
        "interview_data": submission.interview_data or {},
        "selected_runs": submission.selected_runs or [],
    }
    with open(f"{local_dir}/metadata.json", "w") as f:
        json.dump(meta, f, indent=2, default=str)

    # Write pipeline.sh locally — fir cron will download it via HTTP
    with open(f"{local_dir}/pipeline.sh", "w") as f:
        f.write(pipeline_script)

    write_cluster_marker(submission)

    # Write download wrapper (worker will execute it)
    dl_path = f"{local_dir}/download.sh"
    with open(dl_path, "w") as f:
        f.write(local_wrapper)
    os.chmod(dl_path, 0o755)

    # Mark as queued for the download worker to pick up
    with open(f"{local_dir}/.queued", "w") as f:
        f.write(datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))

    logger.info(f"Queued download for {submission.slug}")

    return "queued"


async def get_submission_status(slug: str) -> dict:
    """Get submission status from local staging markers + pushed HPC status.

    No SSH required. Status sources (checked in order):
      1. Local staging dir (.status / .ready) — download phase
      2. Pushed HPC status via /staging/{slug}/status — pipeline phase
    """
    from .staging import get_hpc_status

    # Check local staging first (download still in progress on arbutus)
    local_dir = Path(settings.local_download_path) / slug
    if local_dir.exists():
        local_ready = local_dir / ".ready"
        local_status = local_dir / ".status"
        local_queued = local_dir / ".queued"
        if local_ready.exists():
            return {"phase": "downloading", "detail": "Waiting for HPC pickup"}
        if local_status.exists():
            status = local_status.read_text().strip()
            if status == "downloading":
                return {"phase": "downloading"}
            elif status == "failed":
                return {"phase": "failed", "reason": "Local download failed"}
        if local_queued.exists():
            return {"phase": "downloading", "detail": "Queued for download"}

    # Check pushed HPC status (fir → arbutus over HTTP)
    hpc = get_hpc_status(slug)
    if hpc:
        return hpc

    # No info available yet
    return {"phase": "unknown"}


async def check_job_status(job_id: str) -> dict:
    """Check job status (placeholder-aware)."""
    if job_id.startswith("local-"):
        return {"job_id": job_id, "state": "DOWNLOADING", "running": True}
    # For real SLURM job IDs, check pushed status
    return {"job_id": job_id, "state": "UNKNOWN", "running": True}


async def check_completion_marker(slug: str) -> dict:
    """Check for completion via pushed HPC status."""
    from .staging import get_hpc_status

    hpc = get_hpc_status(slug)
    if hpc:
        phase = hpc.get("phase", "")
        if phase in ("completed", "failed"):
            exit_code = hpc.get("exit_code", "1" if phase == "failed" else "0")
            return {
                "completed": True,
                "success": phase == "completed",
                "exit_code": exit_code,
            }
    return {"completed": False}


async def cancel_job(job_id: str) -> bool:
    """Cancel a job."""
    if job_id.startswith("local-"):
        pid = job_id[6:]
        try:
            os.kill(int(pid), 15)  # SIGTERM
            return True
        except (ProcessLookupError, ValueError):
            return False
    # Can't cancel remote SLURM jobs without SSH
    logger.warning(f"Cannot cancel remote job {job_id} — no SSH available")
    return False


# Phases that mean the cluster is doing something, as opposed to reporting a
# failure or saying nothing at all.
_FORWARD_PHASES = ("queued", "running", "archiving", "completed", "transferred")


def _failure_superseded(sub, hpc: dict, status_mtime) -> bool:
    """Has the cluster made progress *since* we recorded this failure?

    A FAILED submission is normally terminal, but operational recovery — killing
    a hung job and resubmitting, a cluster failover, a manual sbatch after a fix —
    produces exactly this: a real, finished run whose results the portal would
    otherwise never show (issue #53).

    The risk in re-reading FAILED submissions is the mirror image: a stale status
    file resurrecting a run that genuinely failed. So this only returns True when
    the pushed status is demonstrably *newer* than the failure, judged by
    whichever evidence exists:

    - a different SLURM job id than the one recorded — some other job has since
      run, which is precisely the resubmit case; or
    - a status file written after we recorded the failure.

    With neither (an old submission whose failure predates `completed_at` being
    recorded at all), it stays failed rather than guessing.
    """
    if hpc.get("phase", "") not in _FORWARD_PHASES:
        return False  # still failed, or nothing meaningful pushed

    job_id = str(hpc.get("job_id") or "")
    if job_id and job_id != (sub.slurm_job_id or ""):
        return True

    if sub.completed_at and status_mtime:
        return status_mtime > sub.completed_at

    return False


async def poll_all_running_jobs(db_session) -> list:
    """Poll for completed submissions using pushed HPC status.

    No SSH required — reads status files pushed by fir over HTTP.
    """
    from sqlalchemy import select
    from .database import Submission, SubmissionStatus
    from .staging import get_hpc_status, get_hpc_status_mtime

    # Include PROCESSING so a job that reported "completed"/"archiving" first is
    # still upgraded to RESULTS_READY once its results reach arbutus (transferred).
    # FAILED is included so a *later* successful run can undo it — see
    # _failure_superseded. Without that, cancelling a hung job and resubmitting
    # left the portal permanently showing a failure whose results were on disk.
    stmt = select(Submission).where(
        Submission.status.in_([
            SubmissionStatus.QUEUED, SubmissionStatus.RUNNING, SubmissionStatus.PROCESSING,
            SubmissionStatus.FAILED,
        ])
    )
    result = await db_session.execute(stmt)
    running = result.scalars().all()

    completed = []
    for sub in running:
        hpc = get_hpc_status(sub.slug)
        if not hpc:
            continue

        phase = hpc.get("phase", "")
        job_id = hpc.get("job_id")

        if sub.status == SubmissionStatus.FAILED:
            if not _failure_superseded(sub, hpc, get_hpc_status_mtime(sub.slug)):
                continue
            logger.info(
                "Submission %s: cluster reports phase=%r (job %s) after a recorded "
                "failure — recovering", sub.slug, phase, job_id or "?",
            )
            sub.error_message = None
            sub.completed_at = None

        # Update job ID if we got a real one
        if job_id and sub.slurm_job_id != job_id:
            sub.slurm_job_id = job_id

        # Update status based on phase
        if phase == "running" and sub.status != SubmissionStatus.RUNNING:
            sub.status = SubmissionStatus.RUNNING
        elif phase in ("completed", "transferred", "archiving"):
            # Pipeline finished on HPC. "transferred" means results are already on
            # arbutus (the pickup reconciler pushes it directly, skipping "completed").
            # completed/archiving = HPC still packaging (machine's turn) → PROCESSING;
            # transferred = results ready for the author → RESULTS_READY.
            new_status = (
                SubmissionStatus.RESULTS_READY if phase == "transferred"
                else SubmissionStatus.PROCESSING
            )
            status_changed = sub.status != new_status
            sub.status = new_status
            if phase == "transferred":
                from .database import ResultsFormat
                sub.results_format = ResultsFormat.TRANSFERRED
                # Guard against a run that "succeeded" (exit 0, task errors
                # ignored) but produced nothing — e.g. every REMOVE_PRIMERS
                # failed. Such an archive has no viz/seqtab; mark it FAILED
                # instead of RESULTS_READY so it doesn't read as done.
                if sub.pipeline == PipelineType.ILLUMINA_AMPLICON:
                    from .microscape_deploy import results_have_output, diagnose_empty_run
                    if not results_have_output(sub.slug):
                        sub.status = SubmissionStatus.FAILED
                        # Read the pipeline's own stats rather than guessing at a
                        # cause: "check primers" sent two investigations at the
                        # primers when the loss was entirely at the quality filter.
                        try:
                            sub.error_message = diagnose_empty_run(sub.slug)
                        except Exception:
                            logger.exception("Submission %s: empty-run diagnosis failed", sub.slug)
                            sub.error_message = "Pipeline finished but produced no results."
                        logger.warning("Submission %s transferred but empty — marked FAILED", sub.slug)
                        completed.append(sub.slug)
                        continue
                # Deploy the amplicon viz site to microscape.app (once).
                if sub.pipeline == PipelineType.ILLUMINA_AMPLICON:
                    meta = dict(sub.sample_metadata or {})
                    if not meta.get("microscape_viz_url"):
                        try:
                            from .microscape_deploy import deploy_submission
                            from .database import User
                            from sqlalchemy.orm import attributes as _attrs
                            owner = (await db_session.execute(
                                select(User).where(User.id == sub.user_id))).scalar_one_or_none()
                            url = await deploy_submission(sub, owner) if owner else None
                            if url:
                                meta["microscape_viz_url"] = url
                                sub.sample_metadata = meta
                                _attrs.flag_modified(sub, "sample_metadata")
                        except Exception as e:
                            logger.warning(f"microscape deploy trigger failed for {sub.slug}: {e}")
                    # RESULTS_READY (set above) renders the Pipeline step as "Done"
                    # and surfaces the viz link, while leaving Manuscript/Review/Publish
                    # (steps 3–5) as still-to-do. Do NOT set PUBLISHED — that's the
                    # *paper* live on GitHub Pages (step 5), not the viz.
            if status_changed:
                logger.info(f"Submission {sub.slug} finished on HPC (phase={phase})")
                completed.append(sub.slug)
        elif phase == "failed":
            sub.status = SubmissionStatus.FAILED
            sub.error_message = hpc.get("reason", f"Exit code {hpc.get('exit_code', '?')}")
            # Stamp when we gave up, so a later push can be compared against it
            # and recognised as newer (_failure_superseded).
            sub.completed_at = datetime.utcnow()
            logger.warning(f"Submission {sub.slug} failed: {sub.error_message}")
            completed.append(sub.slug)
        elif phase == "queued" and sub.status != SubmissionStatus.QUEUED:
            sub.status = SubmissionStatus.QUEUED

    await db_session.commit()

    return completed
