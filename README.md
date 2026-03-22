# OMC Platform

Open Microbial Community — git-native, AI-assisted scientific publishing for microbial ecology.

**How it works:** Point at an SRA accession → select analyses → pipelines run on HPC → AI drafts manuscript → peer review via GitHub PRs.

## Quick Start (Development)

```bash
pip install -r portal/requirements.txt
cd portal
cp .env.example .env   # edit as needed
uvicorn app.main:app --reload --port 8002
```

Visit http://localhost:8002. In dev mode (`DEBUG=true`), GitHub OAuth is bypassed — you're auto-logged in as `dev-user`.

### Local AI (LM Studio)

AI modules (manuscript generation, review agents, author interview) use any OpenAI-compatible API:

1. Install [LM Studio](https://lmstudio.ai/)
2. Load a model (we use `qwen3-coder-30b`)
3. Start the server on port 1234

Configure `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in `.env` for cloud providers.

## Architecture

```
portal/              # FastAPI web application
├── app/
│   ├── main.py              # App entry, route registration
│   ├── config.py            # Pydantic settings from .env
│   ├── database.py          # SQLAlchemy async models (User, Submission)
│   ├── auth.py              # GitHub OAuth + dev mode bypass
│   ├── submissions.py       # Submission CRUD + accession lookup
│   ├── reviews.py           # AI review + manuscript generation endpoints
│   ├── interviews.py        # Author interview flow
│   ├── metadata.py          # AI metadata assistant endpoints
│   ├── sra_metadata.py      # NCBI Entrez: accession resolution + metadata
│   ├── slurm.py             # SLURM job submission via OpenSSH ControlMaster
│   ├── pipeline_processing.py  # Post-pipeline: AI draft → GitHub repo
│   ├── github_integration.py   # Repo creation, file commits, review PRs
│   └── github_app_auth.py  # GitHub App JWT auth with PAT fallback
├── templates/       # Jinja2 HTML templates
└── static/          # CSS

ai/                  # AI modules (OpenAI-compatible API)
├── llm_client.py            # Shared client — LM Studio or cloud
├── manuscript_generator.py  # Section generation + citation resolution
├── review_agents.py         # Statistical, methodological, clarity review
├── author_interview.py      # Conversational interview
├── metadata_assistant.py    # SRA metadata preparation
├── citation_resolver.py     # [CITE] → PubMed → inline citations + BibTeX
├── pubmed_search.py         # Direct NCBI E-utilities search
├── figure_generator.py      # Plotly JSON from pipeline outputs
└── pipeline_parser.py       # Parse outputs (nanopore_mag, illumina_mag, rnaseq, isolate_genome)

templates/paper-repo/   # Quarto manuscript template for generated papers
tests/                  # pytest test suite
```

## Submission Flow

1. **Enter any NCBI accession** (PRJNA, SRR, SAMN, etc.) → resolves to parent BioProject
2. **Select data types** from breakdown table (platform/strategy/source/layout combos)
3. **Pipeline auto-selected** from library tags (AMPLICON → microscape, WGS → illumina_mag, etc.)
4. **Author interview** → AI gathers research context via conversation
5. **Submit to HPC** → Nextflow pipeline via SLURM on Alliance Canada
6. **AI manuscript draft** → generated from pipeline outputs + interview data
   - Citations resolved via PubMed E-utilities
   - Figures generated as Plotly JSON
7. **GitHub paper repo** → `open-community-science/micro-NNNN`
   - Quarto manuscript auto-rendered to HTML + PDF via GitHub Actions
   - `.omc/` provenance directory (interview transcripts, first drafts, metadata)
8. **AI peer review** → statistical, methodological, clarity agents create review PRs

## GitHub Architecture

- **Org:** [`open-community-science`](https://github.com/open-community-science)
- **GitHub App:** "OMC Platform" — handles both bot operations (JWT → installation tokens) and user OAuth login
  - Falls back to PAT if App not configured
- **Paper repos:** `micro-NNNN` — org-owned, authors fork if desired
- **Reviews:** Each review type (statistical, methodological, clarity) creates its own PR
- **Training data:** `.omc/` directory in each paper repo stores AI interaction history — the diff between `manuscript_v1.json` and the final manuscript is labeled training data

## Pipelines

| Pipeline | Strategy | Parser |
|----------|----------|--------|
| `microscape` | Illumina amplicon (16S/ITS) | Planned |
| `nanopore_mag` | Long-read MAG assembly | Implemented |
| `illumina_mag` | Short-read MAG assembly | Implemented |
| `rnaseq` | RNA-Seq differential expression | Implemented |
| `isolate_genome` | Isolate genome assembly | Implemented |

## Configuration

All settings via environment variables or `portal/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG` | `false` | Dev mode (auto-login, verbose logging) |
| `LLM_BASE_URL` | `http://10.151.49.182:1234/v1` | OpenAI-compatible API endpoint |
| `LLM_MODEL` | `qwen3-coder-30b-a3b-instruct` | Model for AI features |
| `GITHUB_APP_ID` | | GitHub App numeric ID |
| `GITHUB_APP_PRIVATE_KEY` | | PEM file path or content |
| `GITHUB_ORG` | `open-community-science` | GitHub org for paper repos |
| `SLURM_ENABLED` | `false` | Enable HPC job submission |
| `SLURM_HOST` | | HPC cluster hostname (e.g. `fir.alliancecan.ca`) |
| `SLURM_USER` | | HPC username |
| `SLURM_ACCOUNT` | | SLURM account (e.g. `def-rec3141_cpu`) |

See `portal/app/config.py` for the full list.

## Tests

```bash
# Fast unit tests (~1s)
python -m pytest tests/test_figure_generator.py tests/test_review_pr.py -v

# Citation tests (~5s, hits NCBI)
python -m pytest tests/test_pubmed_citations.py -v

# Full E2E workflow (~2 min, needs LLM server)
python -m pytest tests/test_e2e_workflow.py -v --timeout=300
```

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy async + SQLite (aiosqlite)
- **Frontend**: Jinja2 templates + htmx + vanilla JS
- **AI**: OpenAI-compatible API (LM Studio locally, Claude in production)
- **Metadata**: NCBI Entrez (Biopython) + PubMed E-utilities
- **HPC**: OpenSSH ControlMaster → SLURM → Nextflow on Fir (Alliance Canada)
- **Hosting**: Arbutus cloud VM (Alliance Canada OpenStack)
- **Papers**: Quarto → GitHub Pages
- **GitHub**: GitHub App (bot ops + OAuth login)

## Deployment

**Production:** https://microbial.opencommunity.science (Arbutus cloud VM)

```bash
./deploy.sh              # deploy to Arbutus VM
# After DNS setup:
ssh omc2 sudo certbot --nginx -d microbial.opencommunity.science
```

**SLURM:** Uses OpenSSH ControlMaster for Duo MFA bypass on Alliance clusters. Authenticate once:
```bash
ssh ubuntu@<arbutus-ip>
ssh fir                  # triggers Duo MFA, opens persistent connection
```

## License

CC-BY 4.0
