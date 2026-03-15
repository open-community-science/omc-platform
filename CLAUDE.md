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
| `LLM_MODEL` | `qwen3-coder-30b-a3b-instruct` | Model for AI features |
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
    └── Marimo  (port 8081) — interactive data explorer, plots
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

- **Per-session tokens**: SHA-256, 48 chars, created at launch, revoked on removal
- **Rate limiting**: 30 requests/min per session
- **Logging**: Every LLM call logged with submission slug for provenance

### Container Isolation

- **Network**: `omc-sessions` Docker bridge, inter-container communication disabled
- **Firewall**: iptables restrict outbound to portal port only (run `session/setup-network.sh`)
- **Data**: Submission dataset mounted read-only at `/data`
- **Resources**: 2GB memory, 1 CPU per container
- **Lifecycle**: Containers are stopped (not destroyed) when idle — `docker start` resumes with full state

### Commands

```bash
# Build session image
cd session && docker build -t omc-session:latest .

# Set up isolated network (once, needs sudo for iptables)
sudo session/setup-network.sh

# Launch a session (normally done via portal API)
POST /sessions/{slug}/launch

# Stop / resume / remove
POST /sessions/{slug}/stop
POST /sessions/{slug}/resume
```

### Key Files

| File | Purpose |
|------|---------|
| `session/Dockerfile` | Container image: Python 3.12 + Chainlit + Marimo + data science stack |
| `session/chat_app.py` | Chainlit app: 4-phase AI-driven author flow with streaming |
| `session/notebooks/explore.py` | Marimo notebook: auto-discovers pipeline outputs, renders plots |
| `portal/app/sessions.py` | Session manager: Docker lifecycle, port allocation, token management |
| `portal/app/llm_proxy.py` | LLM proxy: auth, rate-limit, forward, stream, log |
| `portal/templates/session.html` | Split-view UI: chat + data explorer side by side |

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

- **Production:** https://microbial.opencommunity.science — Arbutus VM (`206.12.96.115`)
- **Quick deploy:** `rsync` changed files to `arbutus:/opt/omc-platform/`, then `sudo systemctl restart omc-portal`. Exclude `.venv`, `.env`, `omc.db`.
- **Full deploy:** `./deploy.sh` — installs packages, clones repo, sets up systemd + nginx
- **HPC account:** `def-rec3141_cpu` on `fir.alliancecan.ca` — don't specify partition, let scheduler auto-route
- **Fir cron:** `omc-pickup.sh` runs every 5min, polls arbutus staging API, downloads data over HTTP, submits sbatch, pushes status back. No SSH needed in either direction.

## HPC Job Flow (SSH-free)

1. **Download** (arbutus VM): `prefetch` + `fasterq-dump` + `pigz` per run, with 30min timeout and single retry. Stages files in `LOCAL_DOWNLOAD_PATH/{slug}/`, writes `.ready` marker.
2. **Transfer** (fir cron → arbutus HTTP): `omc-pickup.sh` polls `GET /staging/ready`, downloads fastq + `pipeline.sh` via `GET /staging/{slug}/download/...`, then `POST /staging/{slug}/picked-up` to clean up.
3. **Pipeline** (fir compute node via `sbatch`): Cron submits pipeline after transfer. Pipeline pushes status back to `POST /staging/{slug}/status`.
4. **Status polling**: Portal reads local staging markers (download phase) + pushed HPC status JSON (pipeline phase). Background poller (60s) + htmx polling on user page. No SSH anywhere.

## Pipeline (danaSeq)

- **Code:** `/data/danaseq` locally, `/home/rec3141/GENICE/danaSeq` on fir
- **Container:** `ghcr.io/rec3141/danaseq-mag:latest` — rebuilt via GitHub Actions on push to `main`
- **Important:** Pipeline runs inside apptainer container. Host edits to `.nf` files don't take effect until container is rebuilt.
- **Default flags:** `--all --run_sendsketch false --run_vamb_tax false` (sendsketch needs TaxServer)
- **Resources:** 128G mem, 32 CPUs for `--all` mode (kaiju/kraken2/GTDB need 128G minimum)
- **Read type:** Auto-detected from median quality (Q>=20 → `--nano-hq`, Q<20 → `--nano-raw`)

## License

CC-BY 4.0
