"""Interview question/answer flow."""
import asyncio
import httpx
import pytest

BASE = "http://127.0.0.1:8002"

pytestmark = pytest.mark.ai

ANSWERS = {
    "research_question": "Investigating marine microbial community structure across environmental gradients.",
    "study_context": "Ocean samples from tropical to polar waters, 0-4000m depth, 2018-2020.",
    "sample_info": "486 samples, size-fractionated, 16S amplicon on MiSeq.",
    "expected_findings": "Temperature and depth as primary drivers. SAR11/Prochlorococcus in surface, Thaumarchaeota at depth.",
    "broader_significance": "Understanding ocean microbiome response to climate change and biogeochemical cycling.",
    "limitations": "Amplicon-only, DNA-based, single time point, primer bias.",
    "additional_context": "ETH Zurich initiative, environmental metadata concurrent, Nextflow reproducible.",
}


@pytest.mark.asyncio
async def test_full_interview_flow():
    async with httpx.AsyncClient(base_url=BASE, follow_redirects=False) as c:
        await c.get("/auth/login", follow_redirects=True)
        await asyncio.sleep(1)

        # Create submission
        r = await c.post(
            "/submissions/create",
            data={"sra_accession": "PRJNA656268", "pipeline": "illumina_mag"},
        )
        sub_id = r.headers["location"].split("/")[-1]

        # Get initial state
        r = await c.get(f"/interviews/{sub_id}")
        data = r.json()
        assert len(data["questions"]) == 7
        assert data["progress"] == 0.0

        # Answer all questions
        for i, (qid, answer) in enumerate(ANSWERS.items()):
            r = await c.post(
                f"/interviews/{sub_id}/answer",
                json={"question_id": qid, "answer": answer},
            )
            assert r.status_code == 200
            resp = r.json()
            assert resp["success"] is True
            assert abs(resp["progress"] - (i + 1) / 7) < 0.02

        # Verify complete
        r = await c.get(f"/interviews/{sub_id}")
        assert r.json()["complete"] is True

        # Reset
        r = await c.post(f"/interviews/{sub_id}/reset")
        assert r.status_code == 200

        r = await c.get(f"/interviews/{sub_id}")
        assert r.json()["progress"] == 0.0

        # Cleanup
        await c.post(f"/submissions/{sub_id}/delete")
