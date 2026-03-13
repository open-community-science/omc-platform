"""Database setup and models."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, DateTime, Text, Enum, ForeignKey, JSON, text
from datetime import datetime
import enum
import uuid

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
    ILLUMINA_MAG = "illumina_mag"
    MICROSCAPE = "microscape"
    RNASEQ = "rnaseq"
    ISOLATE_GENOME = "isolate_genome"


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
    slug = Column(String(12), unique=True, nullable=False, default=lambda: uuid.uuid4().hex[:8])
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Project & data info
    bioproject_accession = Column(String(50), nullable=False)
    sra_accession = Column(String(50))  # original accession entered by user
    selected_runs = Column(JSON)  # list of SRA run accessions chosen during interview
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
    """Initialize database tables and run lightweight migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Migration: add slug column to existing submissions
        try:
            await conn.execute(text("SELECT slug FROM submissions LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE submissions ADD COLUMN slug VARCHAR(12)"))
            # Backfill existing rows with random slugs
            rows = (await conn.execute(text("SELECT id FROM submissions WHERE slug IS NULL"))).fetchall()
            for row in rows:
                slug = uuid.uuid4().hex[:8]
                await conn.execute(text(f"UPDATE submissions SET slug = '{slug}' WHERE id = {row[0]}"))
            logging.getLogger(__name__).info(f"Migrated {len(rows)} submissions with slugs")


async def get_db() -> AsyncSession:
    """Dependency for getting database session."""
    async with async_session() as session:
        yield session
