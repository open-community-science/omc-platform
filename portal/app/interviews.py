"""Author interview routes - chat-style context gathering."""
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import json

from .config import get_settings
from .database import get_db, Submission, User
from .auth import require_user

router = APIRouter(prefix="/interviews", tags=["interviews"])
settings = get_settings()

# Interview question flow
INTERVIEW_QUESTIONS = [
    {
        "id": "research_question",
        "question": "What is the main research question or hypothesis you're investigating?",
        "section": "introduction",
    },
    {
        "id": "study_context",
        "question": "Can you describe the context of your study? (e.g., sampling location, ecosystem type, any relevant environmental conditions)",
        "section": "methods",
    },
    {
        "id": "sample_info",
        "question": "How many samples are included, and what do they represent? (e.g., different sites, time points, treatments)",
        "section": "methods",
    },
    {
        "id": "expected_findings",
        "question": "What do you expect to find, based on prior work or your understanding of the system?",
        "section": "discussion",
    },
    {
        "id": "broader_significance",
        "question": "What is the broader significance of this work? Why does it matter?",
        "section": "discussion",
    },
    {
        "id": "limitations",
        "question": "Are there any known limitations or caveats we should mention?",
        "section": "discussion",
    },
    {
        "id": "additional_context",
        "question": "Is there anything else you'd like to add that would help contextualize the results?",
        "section": "general",
    },
]


@router.get("/{submission_id}")
async def get_interview(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Get current interview state."""
    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    interview_data = submission.interview_data or {}
    answered = set(interview_data.keys())

    # Find next unanswered question
    next_question = None
    for q in INTERVIEW_QUESTIONS:
        if q["id"] not in answered:
            next_question = q
            break

    return {
        "submission_id": submission_id,
        "questions": INTERVIEW_QUESTIONS,
        "answers": interview_data,
        "next_question": next_question,
        "complete": next_question is None,
        "progress": len(answered) / len(INTERVIEW_QUESTIONS),
    }


@router.post("/{submission_id}/answer")
async def submit_answer(
    submission_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Submit an answer to an interview question."""
    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    body = await request.json()
    question_id = body.get("question_id")
    answer = body.get("answer", "").strip()

    if not question_id or not answer:
        raise HTTPException(status_code=400, detail="question_id and answer required")

    # Validate question_id
    valid_ids = {q["id"] for q in INTERVIEW_QUESTIONS}
    if question_id not in valid_ids:
        raise HTTPException(status_code=400, detail="Invalid question_id")

    # Update interview data
    interview_data = submission.interview_data or {}
    interview_data[question_id] = answer
    submission.interview_data = interview_data

    await db.commit()

    # Find next question
    answered = set(interview_data.keys())
    next_question = None
    for q in INTERVIEW_QUESTIONS:
        if q["id"] not in answered:
            next_question = q
            break

    return {
        "success": True,
        "next_question": next_question,
        "complete": next_question is None,
        "progress": len(answered) / len(INTERVIEW_QUESTIONS),
    }


@router.post("/{submission_id}/reset")
async def reset_interview(
    submission_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reset interview answers."""
    stmt = select(Submission).where(
        Submission.id == submission_id,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    submission.interview_data = {}
    await db.commit()

    return {"success": True, "message": "Interview reset"}
