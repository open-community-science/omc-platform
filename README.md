# OMC Platform

Open Microbial Community platform for AI-assisted scientific publishing in microbial ecology.

## Quick Start (Development)

```bash
cd portal
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your credentials

uvicorn app.main:app --reload
```

Visit http://localhost:8000

## Architecture

```
omc-platform/
├── portal/           # FastAPI web application
│   ├── app/          # Application code
│   ├── templates/    # Jinja2 HTML templates
│   └── static/       # CSS, JS
├── ai/               # AI modules (Claude integration)
├── pipelines/        # Nextflow pipeline configs
├── templates/        # Paper repo template
└── scripts/          # Deployment scripts
```

## Features

- **GitHub OAuth** - Sign in with GitHub
- **SRA Submission** - Enter accession, select pipeline
- **SLURM Integration** - Submit jobs to Alliance Canada HPC
- **Author Interview** - Chat-style context gathering
- **AI Manuscript** - Generate draft from outputs + interview
- **GitHub Repos** - Each paper gets its own repo

## Deployment

See `scripts/setup_vm.sh` for Alliance Canada Cloud VM setup.

## License

CC-BY 4.0
