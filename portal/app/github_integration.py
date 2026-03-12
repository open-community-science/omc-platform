"""GitHub integration for paper repo creation and management."""
import httpx
from typing import Optional
import base64
import yaml

from .config import get_settings
from .database import Submission

settings = get_settings()

GITHUB_API = "https://api.github.com"


async def create_paper_repo(submission: Submission, results_data: dict) -> str:
    """Create a new paper repository from template."""
    if not settings.github_token:
        raise RuntimeError("GitHub token not configured")

    repo_name = f"paper-{submission.id:04d}"
    full_name = f"{settings.github_org}/{repo_name}"

    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"token {settings.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        # Create repository
        resp = await client.post(
            f"{GITHUB_API}/orgs/{settings.github_org}/repos",
            headers=headers,
            json={
                "name": repo_name,
                "description": f"OMC Paper: {submission.title}",
                "private": False,
                "auto_init": True,
                "license_template": "cc-by-4.0",
            },
        )

        if resp.status_code not in (201, 422):  # 422 = already exists
            raise RuntimeError(f"Failed to create repo: {resp.text}")

        # Create initial file structure
        await _create_repo_structure(client, headers, full_name, submission, results_data)

        return f"https://github.com/{full_name}"


async def _create_repo_structure(
    client: httpx.AsyncClient,
    headers: dict,
    repo: str,
    submission: Submission,
    results_data: dict,
):
    """Create the standard paper repo structure."""

    # data/accessions.yaml
    accessions = {
        "sra_accession": submission.sra_accession,
        "pipeline": submission.pipeline.value,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
    }
    await _create_file(
        client, headers, repo,
        "data/accessions.yaml",
        yaml.dump(accessions),
        "Add data accessions"
    )

    # paper.yaml (JSON-LD metadata)
    paper_meta = {
        "@context": "https://schema.org",
        "@type": "ScholarlyArticle",
        "name": submission.title,
        "author": [],  # Will be populated from git contributors
        "dateCreated": submission.created_at.isoformat(),
        "license": "https://creativecommons.org/licenses/by/4.0/",
    }
    await _create_file(
        client, headers, repo,
        "paper.yaml",
        yaml.dump(paper_meta),
        "Add paper metadata"
    )

    # manuscript/01-introduction.md
    await _create_file(
        client, headers, repo,
        "manuscript/01-introduction.md",
        "# Introduction\n\n*AI-generated draft pending*\n",
        "Add manuscript template"
    )

    # manuscript/02-methods.md
    methods = f"""# Methods

## Data Acquisition

Sequence data were obtained from the NCBI Sequence Read Archive under accession {submission.sra_accession}.

## Analysis Pipeline

Analysis was performed using the `{submission.pipeline.value}` pipeline through the Open Microbial Community platform.

*Additional methods details to be generated from pipeline execution trace.*
"""
    await _create_file(
        client, headers, repo,
        "manuscript/02-methods.md",
        methods,
        "Add methods template"
    )

    # manuscript/03-results.md
    await _create_file(
        client, headers, repo,
        "manuscript/03-results.md",
        "# Results\n\n*Results will be generated from pipeline outputs.*\n",
        "Add results template"
    )

    # manuscript/04-discussion.md
    await _create_file(
        client, headers, repo,
        "manuscript/04-discussion.md",
        "# Discussion\n\n*AI-generated draft pending author interview.*\n",
        "Add discussion template"
    )

    # CONTRIBUTORS.md
    await _create_file(
        client, headers, repo,
        "CONTRIBUTORS.md",
        "# Contributors\n\n*Auto-generated from git history.*\n",
        "Add contributors template"
    )


async def _create_file(
    client: httpx.AsyncClient,
    headers: dict,
    repo: str,
    path: str,
    content: str,
    message: str,
):
    """Create or update a file in the repo."""
    content_b64 = base64.b64encode(content.encode()).decode()

    resp = await client.put(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=headers,
        json={
            "message": message,
            "content": content_b64,
        },
    )

    if resp.status_code not in (200, 201):
        # May fail if file exists - that's ok for now
        pass


async def commit_results(repo_url: str, results_path: str) -> bool:
    """Commit pipeline results to the paper repo."""
    # This would use git commands or GitHub API to commit
    # results files (figures, tables, stats) to the repo
    # For now, placeholder
    raise NotImplementedError("Results commit not yet implemented")


async def create_review_pr(repo_url: str, reviewer: str, review_type: str) -> str:
    """Create a PR for review."""
    # Create a branch, add review comments, open PR
    raise NotImplementedError("Review PR creation not yet implemented")
