"""GitHub integration for paper repo creation and management."""
import httpx
import base64
import logging
from typing import Optional

from .config import get_settings
from .database import Submission

settings = get_settings()
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


async def create_paper_repo_from_files(submission: Submission, files: dict) -> str:
    """
    Create a new paper repository and populate it with the given files.

    files: dict of {filepath: content_string}
    Returns the repo URL.
    """
    if not settings.github_token:
        raise RuntimeError("GitHub token not configured")

    repo_name = f"paper-{submission.id:04d}"
    full_name = f"{settings.github_org}/{repo_name}"

    async with httpx.AsyncClient(timeout=30.0) as client:
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
                "has_issues": True,
                "has_projects": False,
                "has_wiki": False,
            },
        )

        if resp.status_code == 201:
            logger.info(f"Created repo {full_name}")
        elif resp.status_code == 422:
            logger.info(f"Repo {full_name} already exists")
        else:
            raise RuntimeError(f"Failed to create repo: {resp.status_code} {resp.text}")

        # Commit all files
        for filepath, content in files.items():
            await _create_or_update_file(
                client, headers, full_name, filepath, content,
                f"Add {filepath}"
            )

        # Enable GitHub Pages on the repo (from docs/ directory)
        await client.post(
            f"{GITHUB_API}/repos/{full_name}/pages",
            headers=headers,
            json={"source": {"branch": "gh-pages", "path": "/"}},
        )

        repo_url = f"https://github.com/{full_name}"
        logger.info(f"Populated repo {full_name} with {len(files)} files")
        return repo_url


async def _create_or_update_file(
    client: httpx.AsyncClient,
    headers: dict,
    repo: str,
    path: str,
    content: str,
    message: str,
):
    """Create or update a file in the repo via the Contents API."""
    content_b64 = base64.b64encode(content.encode()).decode()

    # Check if file exists (need sha for updates)
    get_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=headers,
    )

    payload = {
        "message": message,
        "content": content_b64,
    }

    if get_resp.status_code == 200:
        # File exists — include sha to update
        payload["sha"] = get_resp.json()["sha"]

    resp = await client.put(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=headers,
        json=payload,
    )

    if resp.status_code not in (200, 201):
        logger.warning(f"Failed to create {path} in {repo}: {resp.status_code}")


async def create_review_pr(
    repo_full_name: str,
    review_comments: list[dict],
    review_type: str,
) -> str:
    """
    Create a review PR with AI-generated comments.

    review_comments: list of {"file": "path", "line": N, "body": "comment"}
    Returns the PR URL.
    """
    if not settings.github_token:
        raise RuntimeError("GitHub token not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"token {settings.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        # Get default branch SHA
        repo_resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}",
            headers=headers,
        )
        default_branch = repo_resp.json()["default_branch"]

        ref_resp = await client.get(
            f"{GITHUB_API}/repos/{repo_full_name}/git/ref/heads/{default_branch}",
            headers=headers,
        )
        base_sha = ref_resp.json()["object"]["sha"]

        # Create review branch
        branch_name = f"review/{review_type}"
        await client.post(
            f"{GITHUB_API}/repos/{repo_full_name}/git/refs",
            headers=headers,
            json={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha,
            },
        )

        # Create PR
        pr_resp = await client.post(
            f"{GITHUB_API}/repos/{repo_full_name}/pulls",
            headers=headers,
            json={
                "title": f"AI Review: {review_type}",
                "body": f"Automated {review_type} review by the OMC platform.",
                "head": branch_name,
                "base": default_branch,
            },
        )

        if pr_resp.status_code != 201:
            raise RuntimeError(f"Failed to create PR: {pr_resp.text}")

        pr_data = pr_resp.json()
        pr_number = pr_data["number"]

        # Add review comments to the PR
        for comment in review_comments:
            await client.post(
                f"{GITHUB_API}/repos/{repo_full_name}/issues/{pr_number}/comments",
                headers=headers,
                json={"body": comment["body"]},
            )

        return pr_data["html_url"]
