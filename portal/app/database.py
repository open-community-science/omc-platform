"""Database setup and models."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, DateTime, Text, Enum, ForeignKey, JSON
from datetime import datetime
import enum

from .config import get_settings


class Base(DeclarativeBase):
    pass


class SubmissionStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"
    DRAFTING = "drafting"
    REVIEW = "review"
    PUBLISHED = "published"
    FAILED = "failed"


class PipelineType(str, enum.Enum):
    NANOPORE_MAG = "nanopore_mag"
    MICROSCAPE = "microscape"


class User(Base):
    """User model - linked to GitHub account."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_id = Column(Integer, unique=True, nullable=False)
    github_login = Column(String(255), nullable=False)
    github_name = Column(String(255))
    github_email = Column(String(255))
    github_avatar_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, default=datetime.utcnow)


class Submission(Base):
    """Paper submission model."""

    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # SRA/data info
    sra_accession = Column(String(50), nullable=False)
    pipeline = Column(Enum(PipelineType), nullable=False)

    # Status tracking
    status = Column(Enum(SubmissionStatus), default=SubmissionStatus.DRAFT)
    slurm_job_id = Column(String(50))

    # GitHub repo
    github_repo = Column(String(255))

    # Author interview data (JSON)
    interview_data = Column(JSON)

    # Metadata
    title = Column(String(500))
    sample_metadata = Column(JSON)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime)
    completed_at = Column(DateTime)

    # Error tracking
    error_message = Column(Text)


class Review(Base):
    """Review assignment model."""

    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    status = Column(String(50), default="pending")  # pending, in_progress, completed
    github_pr_url = Column(String(500))

    assigned_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


# Database engine and session
settings = get_settings()
engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    """Dependency for getting database session."""
    async with async_session() as session:
        yield session
