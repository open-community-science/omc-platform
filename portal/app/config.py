"""Application configuration."""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App settings
    app_name: str = "OMC Portal"
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # GitHub OAuth
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/auth/callback"

    # GitHub API (for repo creation)
    github_token: str = ""
    github_org: str = "open-community-science"

    # GitHub App (preferred over PAT for bot operations)
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM content or file path

    # LLM settings (OpenAI-compatible — works with LM Studio, OpenAI, etc.)
    llm_base_url: str = "http://10.151.49.182:1234/v1"
    llm_api_key: str = "lm-studio"
    llm_model: str = "qwen3-coder-30b-a3b-instruct"

    # Claude API (for production — falls back to llm_* settings if empty)
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./omc.db"

    # SLURM settings (Alliance Canada HPC)
    slurm_enabled: bool = False
    slurm_host: str = ""
    slurm_user: str = ""
    slurm_ssh_key: str = ""
    slurm_partition: str = "def-alloc"
    slurm_account: str = ""

    # Pipeline paths on HPC
    pipeline_nanopore_mag: str = "/project/def-alloc/pipelines/nanopore_mag"
    pipeline_microscape: str = "/project/def-alloc/pipelines/microscape"
    pipeline_illumina_mag: str = "/project/def-alloc/pipelines/illumina_mag"
    pipeline_rnaseq: str = "/project/def-alloc/pipelines/rnaseq"
    pipeline_isolate_genome: str = "/project/def-alloc/pipelines/isolate_genome"

    # Shared filesystem mount (for results)
    results_path: str = "/project/def-alloc/omc/results"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
