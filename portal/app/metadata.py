"""Metadata assistant endpoints for SRA/GenBank submission help."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy import select

from .config import get_settings
from .database import get_db, Submission, User
from .auth import require_user

router = APIRouter(prefix="/metadata", tags=["metadata"])
settings = get_settings()


class MetadataMessage(BaseModel):
    message: str = ""


@router.post("/{slug}/chat")
async def metadata_chat(
    slug: str,
    body: MetadataMessage,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Interactive AI chat for metadata preparation.

    Helps users fill in SRA/GenBank required metadata fields
    through conversational Q&A. Extracts structured values from
    natural language responses.
    """
    from ai.metadata_assistant import interactive_metadata_help

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    interview_data = dict(submission.interview_data or {})
    chat_history = interview_data.get("_metadata_chat_history", [])
    current_metadata = interview_data.get("_metadata", {})

    result = await interactive_metadata_help(
        api_key=settings.anthropic_api_key or settings.llm_api_key,
        user_message=body.message or "Help me prepare metadata for my SRA submission.",
        conversation_history=chat_history,
        current_metadata=current_metadata,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )

    # Update stored metadata with any extracted values
    current_metadata.update(result.get("extracted_metadata", {}))
    interview_data["_metadata"] = current_metadata
    interview_data["_metadata_chat_history"] = result["conversation_history"]
    submission.interview_data = interview_data
    attributes.flag_modified(submission, "interview_data")
    await db.commit()

    return {
        "response": result["response"],
        "extracted_metadata": result.get("extracted_metadata", {}),
        "current_metadata": current_metadata,
    }


@router.get("/{slug}/validate")
async def validate_metadata(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Validate current metadata against SRA requirements."""
    from ai.metadata_assistant import validate_metadata as validate

    stmt = select(Submission).where(
        Submission.slug == slug,
        Submission.user_id == user.id,
    )
    result = await db.execute(stmt)
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    current_metadata = (submission.interview_data or {}).get("_metadata", {})
    validation = validate(current_metadata)

    return {
        "metadata": current_metadata,
        "validation": validation,
    }
