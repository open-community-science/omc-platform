# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The Open Microbial Community (OMC) is a git-native, AI-assisted scientific publishing platform for microbial ecology and metagenomics research. The working prototype lives in `omc-platform/`.

Researchers point at an SRA accession, select analyses, the system runs standardized pipelines on Alliance Canada HPC, drafts manuscripts with AI, and manages peer review through GitHub pull requests.

## Repository Structure

```
omc-platform/           # Main platform code
├── portal/              # FastAPI web application
│   ├── app/
│   │   ├── main.py              # App entry, route registration
│   │   ├── config.py            # Pydantic settings from .env
│   │   ├── database.py          # SQLAlchemy async models
│   │   ├── auth.py              # GitHub OAuth + dev mode bypass
│   │   ├── submissions.py       # Submission CRUD + accession lookup
│   │   ├── reviews.py           # AI review + manuscript generation endpoints
│   │   ├── interviews.py        # Author interview flow
│   │   ├── metadata.py          # AI metadata assistant endpoints
│   │   ├── sra_metadata.py      # NCBI Entrez: accession resolution
│   │   ├── slurm.py             # Job submission + status (SSH-free, uses staging API)
│   │   ├── staging.py           # SRA download staging API (fir pulls over HTTP)
│   │   ├── sessions.py          # Docker session manager (launch/stop/resume)
│   │   ├── llm_proxy.py         # LLM proxy for session containers
│   │   ├── pipeline_processing.py  # Post-pipeline: AI draft → GitHub repo
│   │   ├── github_integration.py   # Repo creation, file commits, review PRs
│   │   └── github_app_auth.py  # GitHub App JWT auth with PAT fallback
│   ├── templates/       # Jinja2 HTML templates
│   └── static/          # CSS
├── session/             # Author session container (Chainlit + Marimo)
│   ├── Dockerfile               # Session container image
│   ├── entrypoint.sh            # Starts Chainlit (8080) + Marimo (8081)
│   ├── chat_app.py              # Chainlit: AI-driven 4-phase author flow
│   ├── notebooks/
│   │   └── explore.py           # Marimo: interactive data explorer
│   └── setup-network.sh         # Docker network + iptables isolation
├── ai/                  # AI modules (OpenAI-compatible API)
│   ├── llm_client.py            # Shared client — LM Studio or cloud
│   ├── manuscript_generator.py  # Section generation + citation resolution
│   ├── review_agents.py         # Statistical, methodological, clarity review
│   ├── author_interview.py      # Conversational interview
│   ├── metadata_assistant.py    # SRA metadata preparation
│   ├── citation_resolver.py     # [CITE] → PubMed search → inline citations
│   ├── pubmed_search.py         # Direct NCBI E-utilities (no MCP)
│   ├── figure_generator.py      # Plotly JSON from pipeline outputs
│   └── pipeline_parser.py       # Parse pipeline outputs (MAG, RNA-seq, etc.)
├── relay/               # Agent relay (local ↔ HPC chat bridge)
│   └── app.py                   # FastAPI relay server
├── templates/
│   └── paper-repo/      # Quarto manuscript template
├── scripts/
│   └── omc-pickup.sh    # Fir cron: polls staging API, downloads, submits sbatch
├── tests/               # pytest test suite (use -m "not ai" for fast run)
└── omc-site/            # Original concept paper (static HTML)
```

## Commands

```bash
# Run portal (dev)
cd portal && uvicorn app.main:app --reload --port 8002

# Run tests
python -m pytest tests/ -m "not ai" -v         # fast tests, no LLM needed (~15s)
python -m pytest tests/ -v --timeout=30        # all tests (needs LLM running)
python -m pytest tests/ -m ai -v               # AI tests only (~10 min)

# Install dependencies
pip install -r portal/requirements.txt
```

## GitHub Architecture

- **Org:** `open-community-science` — owns all paper repos
- **GitHub App:** "OMC Platform" (App ID 3078928) for bot operations
  - JWT → installation token flow (auto-refreshes, cached 50min)
  - Falls back to PAT if App not configured
- **Paper repos:** `micro-NNNN` (e.g., `micro-0001`)
  - Quarto manuscript + GitHub Actions for HTML/PDF rendering
  - `.omc/` provenance directory for AI training data
  - Reviews submitted as PRs with severity-tagged comments
- **Auth:** GitHub App handles both bot ops and user OAuth login

## Key Design Decisions

- **"Everything is a commit"** — all AI interactions logged for training data
- **"Agents always help, never block"** — review agents run even without GitHub repo
- **Reviews always produce PRs** — each review type gets its own PR with structured comments
- **Org owns repos, users fork** — users don't need to know git
- **`.omc/` provenance directory** — interview transcripts, first drafts, metadata stored per paper for the training data flywheel

## Configuration

All settings via environment or `.env` (see `portal/app/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://10.151.49.182:1234/v1` | OpenAI-compatible API |
| `LLM_MODEL` | `qwen/qwen3.5-35b-a3b` | Model for AI features |
| `LLM_MODEL_DRAFT` | (falls back to `LLM_MODEL`) | Model for manuscript drafting + revise |
| `LLM_MODEL_CITE` | (falls back to `LLM_MODEL`) | Model for citation resolution (high-volume; use a cheap/local model) |
| `LLM_MODEL_REVIEW` | (falls back to `LLM_MODEL`) | Model for the review agents |
| `MANUSCRIPT_REVISE_ENABLED` | `false` | Run the review→revise loop after reviews (additive; stores `_manuscript_revised`) |
| `MANUSCRIPT_REVISE_MAX_PASSES` | `2` | Max rewrite passes per section in the revise loop |
| `AUTORESEARCH_ENABLED` | `false` | Enable claim-grounded autoresearch (issue #29); gates the route + Step 3 trigger |
| `AUTORESEARCH_MAX_STEPS` | `48` | Agent tool-call budget per run |
| `AUTORESEARCH_MAX_FOLLOWUPS` | `12` | Cap on self-added agenda items |
| `AUTORESEARCH_TIME_BUDGET_S` | `1800` | Wall-clock cap on the explore loop |
| `AUTORESEARCH_MAX_ANALYSIS_S` | `60` | Per `run_analysis` exec timeout (inside the session sandbox) |
| `AUTORESEARCH_RECONCILE_ENABLED` | `true` | Skeptical-model fallback when deterministic verify misses |
| `AUTORESEARCH_COMMIT_ENABLED` | `false` | PR the verified Results prose to the paper repo `.omc/` |
| `LLM_MODEL_EXPLORE` | (falls back to `LLM_MODEL`) | Model for the autoresearch agent loop |
| `LLM_MODEL_VERIFY` | (falls back to `LLM_MODEL`) | Model for the skeptical reconciler |
| `GITHUB_APP_ID` | | GitHub App numeric ID |
| `GITHUB_APP_PRIVATE_KEY` | | PEM file path or content |
| `GITHUB_ORG` | `open-community-science` | Org for paper repos |
| `SLURM_ENABLED` | `false` | Enable HPC job submission |
| `LOCAL_DOWNLOAD_PATH` | `/data/sra_downloads` | SRA download staging dir on arbutus |
| `STAGING_API_KEY` | | Bearer token for staging API (fir cron auth) |

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy async + SQLite (aiosqlite)
- **Frontend**: Jinja2 templates + htmx + vanilla JS
- **AI**: OpenAI-compatible API (LM Studio locally, Claude in production)
- **Author Sessions**: Chainlit (AI chat) + Marimo (data explorer) in Docker containers
- **Metadata**: NCBI Entrez (Biopython) + PubMed E-utilities
- **HPC**: HTTP staging API → fir cron pickup → SLURM → Nextflow (Alliance Canada)
- **Hosting**: Arbutus cloud VM (Alliance Canada OpenStack) at https://microbial.opencommunity.science
- **Papers**: Quarto → GitHub Pages, review PRs
- **GitHub**: GitHub App (bot ops + OAuth login)

## Author Sessions (Chainlit + Marimo)

Interactive, AI-driven research sessions where the AI pulls the author through the manuscript process. Each session runs in an isolated Docker container.

### Architecture

```
Author browser → Portal /sessions/{slug}/chat
  → Portal launches Docker container (omc-session image)
    ├── Chainlit (port 8080) — AI chat, drives the conversation
    ├── Viz SPA  (port 8082) — danaSeq interactive charts (primary data explorer)
    └── Marimo   (port 8081) — editable notebooks (10 tabs matching viz views)
  → Container on 'omc-sessions' network (isolated, no internet)
  → LLM access via portal proxy only
```

### Session Phases

The AI guides the author through four phases:

1. **Interview** — AI asks focused questions about the research, informed by SRA metadata
2. **Results Review** — AI presents pipeline findings, highlights patterns, links to data explorer
3. **Figure Workshop** — AI suggests and generates Plotly figures, author iterates
4. **Manuscript** — AI drafts sections (Abstract → Discussion), author provides feedback

### LLM Proxy

Containers cannot access the LLM directly. All AI calls go through the portal's proxy:

```
Container (OpenAI SDK) → http://172.30.0.1:8002/api/llm/chat/completions
  → Portal authenticates session token
    → Portal forwards to LLM_BASE_URL (LM Studio, Claude, etc.)
      → Response back to container
```

- **Per-session tokens**: SHA-256, 48 chars, created at launch, revoked on removal. Re-registered on portal restart via `_recover_sessions` (reads token from container env)
- **Rate limiting**: 30 requests/min per session
- **Logging**: Every LLM call logged with submission slug for provenance
- **OpenRouter routing**: if user has OpenRouter key configured, proxy routes to OpenRouter instead of local LLM. Requires `user_id` in session token (dev_launch passes submission owner's user_id)
- **AI tools**: The chat AI has 4 tools for inspecting pipeline data — `list_data_files`, `read_data_file`, `get_results_summary` (preprocessed viz JSONs), and `browse_samples` (SRA metadata). Tool call deduplication prevents models from spamming identical calls
- **Single-connection guard**: Only one browser tab drives the chat at a time. Second tab sees "already open" with a takeover button. Old tab gets blocked on next message with same takeover option

### Results Storage (squashfs)

Pipeline results are packaged as squashfs archives for efficient storage and transfer:

```
fir: pipeline completes → mksquashfs results → POST /staging/{slug}/upload-results
  → arbutus stores {slug}.sqsh in /data/results/
    → session launch: squashfuse mount → bind-mount into container at /data:ro
```

- **Transfer**: single `.sqsh` file instead of thousands of loose files
- **Inode efficiency**: 1 file per submission on both fir scratch and arbutus storage
- **Session integration**: session manager auto-detects `.sqsh`, mounts via `squashfuse -o allow_other` (using `subprocess.Popen` with full daemon detach — `close_fds=True`, `start_new_session=True`, all streams to `DEVNULL`), falls back to loose files
- **FUSE gotcha**: asyncio subprocess deadlocks on FUSE daemons because they inherit pipe fds. Must use `Popen` with detach, never `asyncio.create_subprocess_exec`
- **Re-analysis**: fir uses fuse-overlayfs (squashfs lower + writable upper) for `--resume`
- **Provenance**: `.sqsh` is a checksummable artifact — hash stored in paper repo `.omc/`
- **Tracking**: `Submission.results_format` field: `none` → `live` → `archived` → `transferred`

### Chat Persistence

Chat history persists across container stop/resume and is committed to the paper repo for provenance.

**Container-side:** Chainlit writes session state (phase, history, summaries) to `/app/.omc/chat_state.json` after every message. This file survives `docker stop`/`docker start`. On startup, `on_chat_start` checks for the file and restores history + phase, replaying messages to the UI.

**Portal-side:** On "Save & Close", the portal extracts chat state via `docker exec cat`, commits it to GitHub, then stops the container. On resume, if the container-local file was lost, the portal injects cached state via `docker exec` with base64-encoded JSON.

**Git-side:** Two files committed to the paper repo's `.omc/` directory via GitHub Contents API:
- `.omc/chat_transcript.json` — full message history with timestamps
- `.omc/session_state.json` — phase, summaries, message count

**Endpoints:**
- `POST /sessions/{slug}/save` — save chat to GitHub without stopping
- `POST /sessions/{slug}/stop` — extracts chat state, commits to GitHub, then stops container

**When commits happen:**

| Event | Action |
|-------|--------|
| "Save & Close" button | Save → commit → stop |
| `POST /save` | Extract → commit (no stop) |
| Every message | Container writes to local file (no git) |

### Container Isolation

- **Network**: `omc-sessions` Docker bridge, inter-container communication disabled
- **Firewall**: iptables restrict outbound to portal port only (run `session/setup-network.sh`)
- **Data**: Submission dataset mounted read-only at `/data` (loose files or squashfuse mount)
- **Chat state**: Written to `/app/.omc/chat_state.json` inside container (writable layer, never on host)
- **Resources**: 2GB memory, 1 CPU per container
- **Lifecycle**: Containers are stopped (not destroyed) when idle — `docker start` resumes with full state including chat history
- **Live-mounted files**: `chat_app.py`, `tools.py`, `data_layer.py`, `viz_server.py`, `entrypoint.sh`, and `notebooks/` are bind-mounted from host for live editing without rebuilding the image. **Important**: Chainlit caches Python at import time, so code changes require `docker rm` + relaunch, not just `docker restart`
- **Notebooks writable**: The `notebooks/` mount is read-write so Marimo edit mode can save changes

### Chainlit Configuration

The Chainlit config lives at `session/.chainlit/config.toml` — generated by `chainlit init` (v2.10.0), customized, and committed to the repo. The Dockerfile COPYs it directly into the image. No `chainlit init` or sed patching at build time.

Key customizations from defaults:
- `spontaneous_file_upload.enabled = false` — no file uploads in isolated containers
- `unsafe_allow_html = true` — allows rich formatting in chat
- `name = "OMC Research Assistant"`
- `layout = "wide"`

### Commands

```bash
# Build session image
cd session && docker build -t omc-session:latest .

# Set up isolated network (once, needs sudo for iptables)
sudo session/setup-network.sh

# Launch a session (normally done via portal API)
POST /sessions/{slug}/launch

# Save / stop / resume / remove
POST /sessions/{slug}/save
POST /sessions/{slug}/stop
POST /sessions/{slug}/resume

# Hard-reset a session (kills container, remounts squashfuse, restarts portal)
scripts/reset-session.sh {slug}
```

### Key Files

| File | Purpose |
|------|---------|
| `session/Dockerfile` | Container image: Python 3.12 + Chainlit + Marimo + viz server + data science stack |
| `session/.chainlit/config.toml` | Chainlit config: UI settings, file upload disabled, generated by v2.10.0 |
| `session/chat_app.py` | Chainlit app: 4-phase AI-driven author flow with streaming + state persistence |
| `session/viz_server.py` | Static file server for danaSeq viz SPA (port 8082), strips reverse-proxy prefix |
| `session/viz/` | danaSeq viz SPA app shell (~7MB): index.html + assets + phylocanvas. Data comes from pipeline results at runtime via symlink |
| `session/notebooks/explore.py` | All-in-one tabbed Marimo notebook: 10 tabs matching danaSeq viz views (Overview, Quality, Taxonomy, Contigs, Coverage, KEGG, MGE, Eukaryotic, Biosynthetic, Phylogeny) |
| `session/notebooks/data_utils.py` | Shared data loader for viz JSON and pipeline TSV files |
| `session/tools.py` | AI tool definitions: `browse_samples`, `list_data_files`, `read_data_file`, `get_results_summary` |
| `portal/app/sessions.py` | Session manager: Docker lifecycle, triple port allocation, token management, chat persistence, squashfuse mount |
| `portal/app/llm_proxy.py` | LLM proxy: auth, rate-limit, forward to LM Studio or OpenRouter, stream, log |
| `portal/templates/session.html` | Tabbed UI: Chat, Data Explorer (viz SPA), Notebook (Marimo), Split View |
| `scripts/reset-session.sh` | Hard-reset a session: kill container, unmount squashfuse, restart portal, relaunch |

## Agent Relay

A lightweight chat bridge between Claude Code instances (local ↔ HPC). Hosted on arbutus at `https://microbial.opencommunity.science/relay/`.

```bash
# CLI wrapper (preferred — handles auth and JSON escaping)
relay send "your message"          # send to general channel
relay send -c debug "message"      # send to specific channel
relay read 5                       # last 5 messages
relay read 10 debug                # last 10 from debug channel
relay poll                         # block until new message
relay watch                        # continuous stream
relay channels                     # list active channels
```

- **CLI:** `~/.local/bin/relay` (on both local and fir, uses `RELAY_ROLE` env var)
- **Skill:** `/relay message` sends, `/relay` reads, `/relay that` summarizes and forwards
- **Key location:** `~/.config/omc/relay-key` (on both local and fir)
- **Channels:** default `general`, use `-c channel` for topic separation
- **Service:** `relay.service` on arbutus, code at `/opt/omc-platform/relay/`
- **Source:** `omc-platform/relay/app.py`

## Deployment

- **Production:** https://microbial.opencommunity.science — Arbutus VM (`134.87.12.190`)
  - Runs on the **new** Arbutus cloud (`arbutus.alliancecan.ca`), project `def-rec3141-dev`.
    Migrated 2026-08-07 from the legacy cloud (`206.12.96.115`), which is decommissioned in 2026.
  - SSH: `ssh arbutus` (key `~/.ssh/arbutus-new`). The legacy VM is `arbutus-legacy`.
- **Quick deploy:** `rsync` changed files to `arbutus:/opt/omc-platform/`, then `sudo systemctl restart omc-portal`. Exclude `.venv`, `.env`, `omc.db`.
- **Full deploy:** `./deploy.sh` — installs packages, clones repo, sets up systemd + nginx
- **HPC account:** `def-rec3141_cpu` on `fir.alliancecan.ca` — don't specify partition, let scheduler auto-route
- **Cluster pickup:** `omc-pickup.sh` runs every 5min inside a long-lived `omc-pickup` SLURM job, polls the arbutus staging API, downloads data over HTTP, submits sbatch, pushes status back. No SSH needed in either direction.

## HPC Clusters

Three targets run the same pickup loop and the same portal-generated `pipeline.sh`. What differs between them lives in `~/.config/omc/cluster.env` (template: `scripts/cluster.env.example`), never in the scripts.

| | fir | nibi | grex |
|---|---|---|---|
| Host | `fir.alliancecan.ca` | `nibi.alliancecan.ca` | `grex.hpc.umanitoba.ca` (login node `bison`) |
| Account | `def-rec3141_cpu` | `def-rec3141_cpu` | `def-rec3141` |
| Container runtime | `module load apptainer` (1.3.5) | `module load apptainer` (1.3.5) | `module load singularity/4.4.1` — no apptainer exists here at all |
| Storage | `~/scratch` | `~/scratch` | no scratch filesystem — `~/scratch` and `~/GENICE` symlink into `/project/6043541/rec3141` (48 TB, 4M inodes; home is only 100 GB / 500k) |
| Batch env | Lmod + SLURM bin present | Lmod + SLURM bin present | starts with **neither Lmod nor `/opt/slurm/bin` on PATH** — `cluster.env` sources `/etc/profile.d/z09-modules.sh` and prepends the SLURM bin |
| Reference DBs | full 571 GB set | — | silva only (amplicons); the rest is a pending Globus transfer |

**Routing.** `Submission.target_cluster` pins a run to one cluster; unpinned runs go to whichever cluster the admin panel has made *active*. The decision travels with the staged data as a `.cluster` marker, and `/staging/ready`+`/ready-runs` filter on the `cluster=` the loop sends. A pinned run reaches its cluster even while that cluster is standby. Admins pin from the submission page (Step 3), up to the moment a cluster claims the run. A loop old enough not to send `cluster=` is offered unpinned work only — it cannot honour a pin, so it is never shown one.

**Globus.** Bulk DB transfers between clusters: fir is `d6a86f93-b5de-4d26-ae5a-bcbec9cc6600` (`alliancecan#fir-globus-ipv6`), grex is `35a6851d-7ab1-41d0-b614-9a864b6ded17` (`UManitoba Grex HPC`). The grex collection needs a one-time `globus session consent` in a browser before the CLI can touch it.

## HPC Job Flow (SSH-free)

1. **Download** (arbutus VM): `prefetch` + `fasterq-dump` + `pigz` per run, with 30min timeout and single retry. Stages files in `LOCAL_DOWNLOAD_PATH/{slug}/`, writes `.ready` marker.
2. **Transfer to fir** (fir cron → arbutus HTTP): `omc-pickup.sh` polls `GET /staging/ready`, downloads fastq + `pipeline.sh` via `GET /staging/{slug}/download/...`, then `POST /staging/{slug}/picked-up` to clean up.
3. **Pipeline** (fir compute node via `sbatch`): Cron submits pipeline after transfer. Pipeline pushes status back to `POST /staging/{slug}/status`.
4. **Archive** (fir post-pipeline): `mksquashfs` results → `{slug}.sqsh` (excludes BAMs and fastqs). Work dir also squashed separately (excludes fastqs and flye intermediates). Frees inodes on scratch.
5. **Transfer to arbutus** (fir → arbutus HTTP): `POST /staging/{slug}/upload-results` with `.sqsh` body. Arbutus stores at `/data/results/{slug}.sqsh`.
6. **Session mount**: Portal squashfuse-mounts `.sqsh` → bind-mounts into author session container at `/data:ro`.
7. **Status polling**: Portal reads local staging markers (download phase) + pushed HPC status JSON (pipeline phase). Background poller (60s) + htmx polling on user page. No SSH anywhere.

## Pipeline (danaSeq)

- **Code:** `~/Desktop/claude-code/danaSeq` locally, `/home/rec3141/GENICE/danaSeq` on fir
- **Containers (per stage):** `ghcr.io/rec3141/danaseq-illumina-assembly`, `danaseq-nanopore-assembly`, `danaseq-mag-analysis`, `danaseq-illumina-rna`, `danaseq-illumina-amplicon` (each `:latest`, rebuilt via GitHub Actions on push to `main`). The single `danaseq-mag` image was retired when the pipeline was split into per-stage images.
- **Amplicons:** the `illumina_amplicon` stage runs from its own SIF at `/home/rec3141/GENICE/danaseq-illumina-amplicon.sif` (pipeline code baked in at `/pipeline`), not from the danaSeq checkout. It was the separate `microscape-nf` repo until 2026-08-08; the `microscape` (Python) and `microscapeR` (R) packages that mirrored it are retired, and the pipeline now runs a single Python engine.
- **Rebuilding the amplicon SIF:** `bash ~/build-amplicon-sif.sh` on fir, then move `~/danaseq-illumina-amplicon.new.sif` into `~/GENICE/`. Verify the fix is really in the image (`apptainer exec <sif> grep ... /pipeline/modules/...`) — a stale pin has shipped an image matching no commit before.
- **Checking `.nf` edits compile:** run with real parameters and a nonexistent `--input`, e.g. `apptainer run <sif> run /pipeline/main.nf --input /nonexistent --outdir /tmp/x`. `--help` returns *before* module compilation, so it passes even when a module has a syntax error.
- **Important:** Pipeline runs inside apptainer container. Host edits to `.nf` files don't take effect until container is rebuilt.
- **Default flags:** `--all --run_sendsketch false --run_vamb_tax false` (sendsketch needs TaxServer)
- **Resources:** 128G mem, 32 CPUs for `--all` mode (kaiju/kraken2/GTDB need 128G minimum)
- **Read type:** Auto-detected from median quality (Q>=20 → `--nano-hq`, Q<20 → `--nano-raw`)

## Host Environment (Arbutus VM)

Everything the portal needs beyond a base Ubuntu 24.04 VM. This list is the recipe for reproducing the environment on a new host.

### System Packages

```bash
sudo apt install -y \
    python3.12 python3-pip python3-venv \
    docker.io \
    squashfs-tools \
    squashfuse \
    fuse3 \
    nginx \
    certbot python3-certbot-nginx \
    sra-toolkit      # prefetch, fasterq-dump for SRA downloads
```

### FUSE Config

```bash
# Enable allow_other so Docker can access squashfuse mounts
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

### Docker Setup

```bash
sudo usermod -aG docker $USER   # run docker without sudo (re-login required)
# Session network (once)
sudo session/setup-network.sh   # creates omc-sessions bridge + iptables rules
# Session image
cd session && docker build -t omc-session:latest .
```

### Python Packages

```bash
pip install -r portal/requirements.txt
pip install chainlit marimo   # for session container dev/testing
```

### Directory Layout on Host

```
/opt/omc-platform/           # deployed code (rsync from repo)
/data/sra_downloads/          # SRA download staging (LOCAL_DOWNLOAD_PATH)
  {slug}/fastq/              # per-submission fastq files
  .hpc_status/               # status JSON pushed by fir
/data/results/                # squashfs result archives
  {slug}.sqsh                # pipeline results from fir
/mnt/omc-sessions/            # squashfuse mountpoints (ephemeral)
  {slug}/                    # mounted .sqsh for active sessions
```

### Systemd Services

```
omc-portal.service    — uvicorn portal on 0.0.0.0:8002 --http httptools
relay.service         — relay API on port 8484
nginx.service         — reverse proxy, TLS termination
```

**Important:** The portal must listen on `0.0.0.0`, not `127.0.0.1`, so session containers can reach the LLM proxy via the Docker gateway (`172.30.0.1:8002`). The `--http httptools` flag uses the C-based HTTP parser instead of h11, which is required for streaming large request bodies (h11 stalls on uploads >10G).

### Nginx Config

The main site config (`/etc/nginx/sites-enabled/omc-platform`) handles:

- `location /staging/` → `localhost:8002` (HPC uploads — dedicated block with streaming settings)
- `location /relay/` → `localhost:8484` (relay)
- `location /` → `localhost:8002` (portal)
- `location ~ ^/session-proxy/(\d+)/` → `localhost:$1` (session containers, with WebSocket upgrade)

Key settings:
- `client_max_body_size 0;` — unlimited, required for `.sqsh` uploads from fir (up to 50G+)
- `proxy_http_version 1.1;` — **critical** for all proxy locations; nginx defaults to HTTP/1.0 upstream which doesn't support chunked transfer encoding, causing large uploads to stall
- `proxy_request_buffering off;` — on `/staging/` location, streams request body directly to uvicorn instead of buffering to disk
- `proxy_temp_path /data/nginx-tmp;` — on `/staging/` location, redirects any fallback buffering to the data volume (nginx may still buffer despite `proxy_request_buffering off` in some cases, e.g. SSL connections with `Content-Length`). Without this, nginx buffers to `/var/lib/nginx/body/` on root disk which will fill up on large uploads
- `client_body_temp_path /data/nginx-tmp;` — set globally in `/etc/nginx/nginx.conf` as a safety net for the same reason
- `proxy_read_timeout 3600;` + `proxy_send_timeout 3600;` — on `/staging/` location, 1h timeouts for multi-GB uploads
- `Upgrade`/`Connection` headers on session proxy — required for Chainlit/Marimo WebSocket

### LLM Access (Reverse SSH Tunnel)

The LLM (LM Studio) runs on the local network behind a VPN. Arbutus can't initiate connections inbound. A reverse SSH tunnel from the LLM host (concentration) forwards the port to arbutus:

```
concentration (local) → SSH → arbutus
  localhost:1234 on arbutus → 10.151.49.182:1234 (LM Studio)
```

**Setup on concentration** (the machine that can reach LM Studio):
```bash
# Install the systemd service for persistence
sudo cp session/llm-tunnel.service /etc/systemd/system/
sudo systemctl enable --now llm-tunnel

# Or manually:
ssh -R 1234:10.151.49.182:1234 arbutus -N -o ServerAliveInterval=30 -f
```

The portal's `.env` on arbutus sets `LLM_BASE_URL=http://localhost:1234/v1`. Session containers never see the tunnel — they go through the LLM proxy at `172.30.0.1:8002/api/llm`.

### Environment File (`/opt/omc-platform/portal/.env`)

```
SECRET_KEY=<random>
LLM_BASE_URL=http://localhost:1234/v1   # via reverse SSH tunnel from concentration
LLM_MODEL=qwen/qwen3.5-35b-a3b
# Optional per-role model overrides (default to LLM_MODEL when unset):
# LLM_MODEL_DRAFT=qwen/qwen3.5-35b-a3b   # drafting + revise
# LLM_MODEL_CITE=qwen/qwen3-4b            # cheap/local model for citation rounds
# LLM_MODEL_REVIEW=qwen/qwen3.5-35b-a3b   # review agents
# MANUSCRIPT_REVISE_ENABLED=false         # opt-in review→revise loop
# Claim-grounded autoresearch (issue #29; off by default, needs .sqsh results):
# AUTORESEARCH_ENABLED=false
# AUTORESEARCH_COMMIT_ENABLED=false        # PR the Results prose to .omc/
# LLM_MODEL_EXPLORE=qwen/qwen3.5-35b-a3b   # agent loop model
# LLM_MODEL_VERIFY=qwen/qwen3.5-35b-a3b    # skeptical reconciler model
GITHUB_APP_ID=3078928
GITHUB_APP_PRIVATE_KEY=<pem path>
GITHUB_ORG=open-community-science
STAGING_API_KEY=<shared key with fir>
LOCAL_DOWNLOAD_PATH=/data/sra_downloads
SLURM_ENABLED=true
DEBUG=false
```

### Reference DB Squashfs

Large reference databases are squashed to save inodes and stored in project storage. Squashfuse-mounted at runtime by the pipeline wrapper.

| DB | Files | Original | Squashed |
|----|-------|----------|----------|
| gtdbtk_db | 143,875 | 139G | 131G |
| kofam_db | 27,508 | 7.2G | 1.5G |
| defensefinder_models | 2,350 | 320M | 59M |
| bakta | 418 | 88G | 45G |

Created with `mksquashfs /path/to/db db.sqsh -noappend -no-xattrs`. Mounted with `squashfuse -o allow_other db.sqsh /mnt/db`.

### Portability Checklist

To deploy on a new VM:

1. Install system packages (above)
2. Clone repo to `/opt/omc-platform/`
3. Create `.env` with site-specific values (see Environment File above)
4. `pip install -r portal/requirements.txt`
5. Enable FUSE `user_allow_other` in `/etc/fuse.conf`
6. Add deploy user to docker group: `sudo usermod -aG docker $USER`
7. Build session image: `cd session && docker build -t omc-session:latest .`
8. Run network setup: `sudo session/setup-network.sh`
9. Create directories: `mkdir -p /data/sra_downloads /data/results /mnt/omc-sessions`
10. Set up systemd services — portal must listen on `0.0.0.0:8002` with `--http httptools`
11. Configure nginx + certbot for TLS (see Nginx Config above) — must use `proxy_http_version 1.1`, dedicated `/staging/` location with `proxy_request_buffering off`, and `client_body_temp_path`/`proxy_temp_path` on the data volume (root disk is too small for upload buffering)
12. Set up relay key: `mkdir -p ~/.config/omc && openssl rand -base64 32 > ~/.config/omc/relay-key`
13. Set up LLM access — reverse SSH tunnel from LLM host, or point `LLM_BASE_URL` at a cloud API

## License

CC-BY 4.0
