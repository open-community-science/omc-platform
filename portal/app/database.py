"""Database setup and models."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, DateTime, Text, Enum, ForeignKey, JSON, text
from datetime import datetime
import enum
import logging
import uuid

from .config import get_settings


class Base(DeclarativeBase):
    pass


class SubmissionStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"       # HPC still packaging results (machine's turn)
    RESULTS_READY = "results_ready"  # pipeline done, results on arbutus (author's turn)
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


class ResultsFormat(str, enum.Enum):
    NONE = "none"              # no results yet
    LIVE = "live"              # loose files on HPC scratch
    ARCHIVED = "archived"      # squashfs on HPC
    TRANSFERRED = "transferred"  # squashfs on arbutus, ready for sessions


class User(Base):
    """User model - linked to GitHub account."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_id = Column(Integer, unique=True, nullable=False)
    github_login = Column(String(255), nullable=False)
    github_name = Column(String(255))
    github_email = Column(String(255))
    github_avatar_url = Column(String(500))
    openrouter_key = Column(String(500))  # Fernet-encrypted OpenRouter API key
    openrouter_model = Column(String(255))  # e.g. "anthropic/claude-sonnet-4"
    # Which LLM backend this user's AI features should use:
    #   "local"    — the self-hosted LLM (LLM_BASE_URL)
    #   "admin"    — the site-wide OpenRouter key provisioned by an admin
    #   "personal" — the user's own OpenRouter key (openrouter_key above)
    # NULL means "not chosen yet"; resolution falls back personal → admin → local.
    llm_backend = Column(String(20))
    llm_model = Column(String(255))  # model for the chosen backend
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, default=datetime.utcnow)


class SiteConfig(Base):
    """Site-wide key/value settings that outlive any single user.

    Holds the shared ("admin") OpenRouter credentials so every user can fall
    back to them without bringing their own key. The value is Fernet-encrypted
    for secrets, same as User.openrouter_key.
    """

    __tablename__ = "site_config"

    key = Column(String(64), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# SiteConfig keys
SITE_OPENROUTER_KEY = "openrouter_admin_key"      # encrypted
SITE_OPENROUTER_MODEL = "openrouter_admin_model"  # plaintext model id — recommended free-tier pick
SITE_LOCAL_MODEL = "local_admin_model"            # plaintext model id — recommended local pick


class Submission(Base):
    """Paper submission model."""

    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    slug = Column(String(12), unique=True, nullable=False, default=lambda: uuid.uuid4().hex[:8])
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Project & data info
    bioproject_accession = Column(String(50))  # nullable until accession is resolved
    sra_accession = Column(String(50))  # original accession entered by user
    selected_runs = Column(JSON)  # list of SRA run accessions chosen during interview
    pipeline = Column(Enum(PipelineType), default=PipelineType.NANOPORE_MAG)

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

    # Amplicon primers (microscape): {fwd, rev, fwd_name, rev_name, region,
    # source: metadata|manual|inferred-db|inferred-denovo, confidence}
    primers = Column(JSON)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime)
    completed_at = Column(DateTime)

    # Results tracking
    results_format = Column(Enum(ResultsFormat, values_callable=lambda x: [e.value for e in x]), default=ResultsFormat.NONE)

    # Error tracking
    error_message = Column(Text)

    # Soft delete
    deleted_at = Column(DateTime)


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

        # Migration: add results_format column
        try:
            await conn.execute(text("SELECT results_format FROM submissions LIMIT 1"))
            # Normalize any uppercase enum names to lowercase values
            await conn.execute(text(
                "UPDATE submissions SET results_format = LOWER(results_format) "
                "WHERE results_format != LOWER(results_format)"
            ))
        except Exception:
            await conn.execute(text(
                "ALTER TABLE submissions ADD COLUMN results_format VARCHAR(20) DEFAULT 'none'"
            ))
            logging.getLogger(__name__).info("Added results_format column to submissions")

        # Migration: add primers column to submissions
        try:
            await conn.execute(text("SELECT primers FROM submissions LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE submissions ADD COLUMN primers JSON"))
            logging.getLogger(__name__).info("Added primers column to submissions")

        # Migration: add openrouter columns to users
        try:
            await conn.execute(text("SELECT openrouter_key FROM users LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE users ADD COLUMN openrouter_key VARCHAR(500)"))
            logging.getLogger(__name__).info("Added openrouter_key column to users")
        try:
            await conn.execute(text("SELECT openrouter_model FROM users LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE users ADD COLUMN openrouter_model VARCHAR(255)"))
            logging.getLogger(__name__).info("Added openrouter_model column to users")

        # Migration: per-user LLM backend choice (local / admin / personal)
        try:
            await conn.execute(text("SELECT llm_backend FROM users LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE users ADD COLUMN llm_backend VARCHAR(20)"))
            logging.getLogger(__name__).info("Added llm_backend column to users")
        try:
            await conn.execute(text("SELECT llm_model FROM users LIMIT 1"))
        except Exception:
            await conn.execute(text("ALTER TABLE users ADD COLUMN llm_model VARCHAR(255)"))
            logging.getLogger(__name__).info("Added llm_model column to users")

        # Migration: make bioproject_accession nullable (SQLite can't ALTER constraints,
        # so recreate the table if the column is NOT NULL)
        try:
            # Test if NULL is allowed
            await conn.execute(text(
                "INSERT INTO submissions (slug, user_id, title, status, created_at) "
                "VALUES ('_test_', 0, 'test', 'DRAFT', '2000-01-01')"
            ))
            await conn.execute(text("DELETE FROM submissions WHERE slug = '_test_'"))
        except Exception:
            # Column is NOT NULL — recreate table
            log = logging.getLogger(__name__)
            log.info("Migrating submissions table: making bioproject_accession nullable")
            await conn.execute(text("ALTER TABLE submissions RENAME TO _submissions_old"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "INSERT INTO submissions SELECT * FROM _submissions_old"
            ))
            await conn.execute(text("DROP TABLE _submissions_old"))
            log.info("Migration complete")


async def get_db() -> AsyncSession:
    """Dependency for getting database session."""
    async with async_session() as session:
        yield session
