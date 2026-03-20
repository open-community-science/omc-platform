"""ENA submission proxy — pass-through endpoints for ENA Webin V2 API.

Users bring their own Webin credentials (per-request, never stored).
The portal proxies requests to ENA so session containers don't need
direct internet access.

ENA's programmatic submission uses XML: we POST a multipart form with
SUBMISSION XML + object XML (PROJECT, SAMPLE, EXPERIMENT, RUN) to
/submit. The response is an XML receipt with assigned accessions.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ena", tags=["ena"])
settings = get_settings()

ENA_PROD_BASE = "https://www.ebi.ac.uk/ena/submit/webin-v2"
ENA_TEST_BASE = "https://wwwdev.ebi.ac.uk/ena/submit/webin-v2"


# ── Pydantic models ──────────────────────────────────────────────────────────

class WebinCredentials(BaseModel):
    webin_id: str = Field(..., description="ENA Webin account ID (e.g. Webin-12345)")
    password: str = Field(..., description="Webin account password")

    def __repr__(self) -> str:
        return f"WebinCredentials(webin_id={self.webin_id!r}, password='***')"


class RegisterStudyRequest(BaseModel):
    credentials: WebinCredentials
    alias: str
    title: str
    description: str = ""
    hold_date: str | None = None
    use_test: bool = True


class RegisterSamplesRequest(BaseModel):
    credentials: WebinCredentials
    samples: list[dict[str, Any]]
    checklist: str = "ERC000011"
    use_test: bool = True


class SubmitReadsRequest(BaseModel):
    credentials: WebinCredentials
    experiments: list[dict[str, Any]]
    use_test: bool = True


class UpdateRequest(BaseModel):
    credentials: WebinCredentials
    object_type: str
    accession: str
    updates: dict[str, Any]
    use_test: bool = True


# ── Helper ────────────────────────────────────────────────────────────────────

async def _ena_xml_submit(
    credentials: WebinCredentials,
    submission_xml: bytes,
    object_xml: bytes,
    object_type: str,
    use_test: bool = True,
) -> dict:
    """Submit XML to ENA's programmatic endpoint.

    Sends a multipart form POST with:
    - SUBMISSION: the submission action XML (ADD/MODIFY/VALIDATE)
    - <OBJECT_TYPE>: the object XML (PROJECT, SAMPLE, etc.)

    Returns parsed receipt dict with accessions and messages.
    """
    from ai.ena_submission import parse_receipt_xml

    base = ENA_TEST_BASE if use_test else ENA_PROD_BASE
    url = f"{base}/submit"

    files = {
        "SUBMISSION": ("submission.xml", submission_xml, "application/xml"),
        object_type.upper(): (f"{object_type.lower()}.xml", object_xml, "application/xml"),
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            url,
            files=files,
            auth=(credentials.webin_id, credentials.password),
        )

    if resp.status_code == 401:
        raise HTTPException(401, "ENA authentication failed — check Webin ID and password")
    if resp.status_code >= 500:
        raise HTTPException(502, f"ENA server error ({resp.status_code}): {resp.text[:300]}")

    receipt = parse_receipt_xml(resp.text)

    if not receipt["success"] and receipt["messages"]["error"]:
        raise HTTPException(
            400,
            f"ENA rejected the submission: {'; '.join(receipt['messages']['error'][:3])}"
        )

    return receipt


async def _ena_get(
    path: str,
    credentials: WebinCredentials,
    use_test: bool = True,
) -> dict:
    """Make an authenticated GET request to the ENA Webin V2 API."""
    base = ENA_TEST_BASE if use_test else ENA_PROD_BASE
    url = f"{base}{path}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            url,
            auth=(credentials.webin_id, credentials.password),
        )

    if resp.status_code == 401:
        raise HTTPException(401, "ENA authentication failed — check Webin ID and password")
    if resp.status_code == 400:
        raise HTTPException(400, f"ENA rejected the request: {resp.text[:500]}")
    if resp.status_code >= 500:
        raise HTTPException(502, f"ENA server error ({resp.status_code}): {resp.text[:300]}")

    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "body": resp.text[:1000]}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/validate-credentials")
async def validate_credentials(creds: WebinCredentials):
    """Test Webin login against ENA."""
    try:
        result = await _ena_get("/auth", creds, use_test=True)
        return {"valid": True, "detail": result}
    except HTTPException as e:
        if e.status_code == 401:
            return {"valid": False, "detail": "Invalid Webin ID or password"}
        raise


@router.post("/register-study")
async def register_study(req: RegisterStudyRequest):
    """Register a new ENA project (study)."""
    from ai.ena_submission import build_project_xml, build_submission_xml

    submission_xml = build_submission_xml(action="ADD", hold_date=req.hold_date)
    project_xml = build_project_xml(
        alias=req.alias,
        title=req.title,
        description=req.description,
    )
    return await _ena_xml_submit(
        req.credentials, submission_xml, project_xml, "PROJECT", req.use_test,
    )


@router.post("/register-samples")
async def register_samples(req: RegisterSamplesRequest):
    """Submit sample metadata to ENA."""
    from ai.ena_submission import build_sample_xml, build_submission_xml

    submission_xml = build_submission_xml(action="ADD")
    sample_xml = build_sample_xml(req.samples, req.checklist)
    return await _ena_xml_submit(
        req.credentials, submission_xml, sample_xml, "SAMPLE", req.use_test,
    )


@router.post("/submit-reads")
async def submit_reads(req: SubmitReadsRequest):
    """Submit experiment + run metadata after user has FTPed files.

    This creates both EXPERIMENT and RUN XML and submits them together.
    The user must have already uploaded files via FTP.
    """
    from ai.ena_submission import build_experiment_xml, build_run_xml, build_submission_xml

    # Build experiment XML
    submission_xml = build_submission_xml(action="ADD")
    experiment_xml = build_experiment_xml(req.experiments)

    # Build run XML (one run per experiment, referencing the experiment alias)
    runs = []
    for exp in req.experiments:
        alias = exp.get("alias", exp.get("sample_accession", ""))
        runs.append({
            "alias": f"run-{alias}",
            "experiment_alias": alias,
            "files": exp.get("files", []),
        })
    run_xml = build_run_xml(runs)

    # Submit experiments first, then runs
    exp_result = await _ena_xml_submit(
        req.credentials, submission_xml, experiment_xml, "EXPERIMENT", req.use_test,
    )
    run_result = await _ena_xml_submit(
        req.credentials, submission_xml, run_xml, "RUN", req.use_test,
    )

    # Merge accessions from both submissions
    merged = {
        "success": exp_result["success"] and run_result["success"],
        "accessions": {**exp_result["accessions"], **run_result["accessions"]},
        "messages": {
            "info": exp_result["messages"]["info"] + run_result["messages"]["info"],
            "error": exp_result["messages"]["error"] + run_result["messages"]["error"],
        },
    }
    return merged


@router.get("/fetch/{accession}")
async def fetch_metadata(accession: str, creds: WebinCredentials):
    """Fetch existing metadata for an ENA/SRA accession."""
    return await _ena_get(f"/objects/{accession}", creds, use_test=True)


@router.post("/update")
async def update_record(req: UpdateRequest):
    """Push a MODIFY action for existing ENA records.

    Builds a MODIFY submission with the updated fields.
    """
    from ai.ena_submission import build_sample_xml, build_submission_xml

    submission_xml = build_submission_xml(action="MODIFY")

    # For now, only sample modifications are supported
    if req.object_type == "sample":
        sample_data = {"sample_alias": req.accession, **req.updates}
        # Use default checklist — ENA will match by accession on MODIFY
        object_xml = build_sample_xml([sample_data], "ERC000011")
        return await _ena_xml_submit(
            req.credentials, submission_xml, object_xml, "SAMPLE", req.use_test,
        )

    raise HTTPException(400, f"MODIFY not yet supported for object type: {req.object_type}")


@router.post("/test-submit")
async def test_submit(req: RegisterSamplesRequest):
    """Dry run against ENA test server using VALIDATE action."""
    from ai.ena_submission import build_sample_xml, build_submission_xml

    submission_xml = build_submission_xml(action="VALIDATE")
    sample_xml = build_sample_xml(req.samples, req.checklist)
    result = await _ena_xml_submit(
        req.credentials, submission_xml, sample_xml, "SAMPLE", use_test=True,
    )
    return {"test_server": True, "result": result}


@router.get("/checklist/{checklist_id}")
async def get_checklist(checklist_id: str):
    """Fetch and return parsed field definitions from an ENA checklist.

    This hits the ENA Browser API to get live field definitions including
    mandatory flags, regex patterns, and enum values. Results are cached.
    """
    from ai.ena_submission import fetch_checklist_fields

    result = await fetch_checklist_fields(checklist_id)
    if not result:
        raise HTTPException(404, f"Could not fetch checklist {checklist_id} from ENA")
    return result
