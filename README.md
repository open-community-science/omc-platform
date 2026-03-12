# OMC Platform

Open Microbial Community platform for AI-assisted scientific publishing in microbial ecology.

## Quick Start (Development)

```bash
cd portal
uv sync          # install dependencies
cp .env .env     # already configured for local dev

uv run uvicorn app.main:app --reload --port 8000
```

Visit http://localhost:8000. In dev mode (`DEBUG=true`), GitHub OAuth is bypassed — you're auto-logged in as `dev-user`.

### Local AI (LM Studio)

The AI modules (manuscript generation, review agents, author interview) use any OpenAI-compatible API. For local development:

1. Install [LM Studio](https://lmstudio.ai/)
2. Load a model (we use `qwen/qwen3-coder-30b`)
3. Start the server on port 1234 (default)

The `.env` is pre-configured for `http://127.0.0.1:1234/v1`. Change `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` for cloud providers.

## Architecture

```
omc-platform/
├── portal/              # FastAPI web application
│   ├── app/
│   │   ├── main.py          # App entry point, routes, lifespan
│   │   ├── config.py         # Pydantic settings from .env
│   │   ├── database.py       # SQLAlchemy async models (User, Submission, Review)
│   │   ├── auth.py            # GitHub OAuth + dev mode bypass
│   │   ├── submissions.py     # Submission CRUD + accession lookup
│   │   ├── sra_metadata.py    # NCBI Entrez: accession resolution + metadata
│   │   ├── slurm.py           # asyncssh SLURM job submission
│   │   ├── pipeline_processing.py  # Post-pipeline: AI draft → GitHub repo
│   │   ├── github_integration.py   # Repo creation, file commits, review PRs
│   │   └── interviews.py     # Interview question flow (being replaced by AI)
│   ├── templates/       # Jinja2 HTML templates
│   └── static/          # CSS
├── ai/                  # AI modules (OpenAI-compatible API)
│   ├── llm_client.py        # Shared client — works with LM Studio or cloud
│   ├── manuscript_generator.py  # Generate intro/methods/results/discussion/abstract
│   ├── review_agents.py     # Statistical, methodological, clarity review
│   ├── author_interview.py  # Conversational interview with metadata context
│   └── metadata_assistant.py # SRA metadata preparation helper
├── templates/
│   └── paper-repo/      # Quarto manuscript template
│       ├── manuscript.qmd
│       ├── _quarto.yml
│       └── .github/workflows/render.yml
└── IDEAS.md             # Design decisions and future plans
```

## Submission Flow

1. **Enter any NCBI accession** (PRJNA, SRR, SAMN, etc.) → resolves to parent BioProject
2. **Select data types** from breakdown table (platform/strategy/source/layout combos)
   - Enforces consistency: can mix instruments but not strategies or sources
3. **Pipeline auto-selected** from library tags (AMPLICON → microscape, WGS → illumina_mag, etc.)
4. **Save and continue** → detail page with metadata summary
5. **(Future) Author interview** → AI gathers research context, handles run selection
6. **Submit to HPC** → Nextflow pipeline via SLURM on Alliance Canada
7. **AI manuscript draft** → generated from pipeline outputs + interview
8. **GitHub repo** → paper published via Quarto + GitHub Pages
9. **AI peer review** → statistical, methodological, clarity agents as PR comments

## Pipelines

| Pipeline | Strategy | Status |
|----------|----------|--------|
| `microscape` | Illumina amplicon (16S/ITS) | Implemented |
| `nanopore_mag` | Long-read MAG assembly | Implemented |
| `illumina_mag` | Short-read MAG assembly | Planned |
| `rnaseq` | RNA-Seq analysis | Planned |
| `isolate_genome` | Isolate genome assembly | Planned |

Note: `microscape` only supports Illumina amplicons (not 454/Nanopore) due to different error profiles.

## Configuration

All settings via environment variables or `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG` | `false` | Enable dev mode (auto-login, verbose logging) |
| `LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | OpenAI-compatible API endpoint |
| `LLM_MODEL` | `qwen/qwen3-coder-30b` | Model to use for AI features |
| `SLURM_ENABLED` | `false` | Enable HPC job submission |
| `GITHUB_TOKEN` | | GitHub API token for repo creation |
| `GITHUB_ORG` | `omc-papers` | GitHub org for paper repos |

See `portal/app/config.py` for the full list.

## Tech Stack

- **Backend**: FastAPI + SQLAlchemy async + SQLite (aiosqlite)
- **Frontend**: Jinja2 templates + htmx + vanilla JS
- **AI**: OpenAI-compatible API (LM Studio locally, cloud in production)
- **Metadata**: NCBI Entrez (Biopython) — SRA, BioProject, BioSample
- **HPC**: asyncssh → SLURM → Nextflow pipelines
- **Papers**: Quarto → GitHub Pages, DOI via Zenodo
- **Package management**: uv (portal), conda (pipelines)

## License

CC-BY 4.0
