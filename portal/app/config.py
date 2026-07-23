"""Application configuration."""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App settings
    app_name: str = "OMC Portal"
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # Admin access — comma-separated GitHub logins granted the /admin panel.
    # Deliberately not hardcoded: set ADMIN_GITHUB_LOGINS in .env (e.g. "rec3141").
    # Empty means no admins in production (dev mode always grants the dev user).
    admin_github_logins: str = ""

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
    github_app_client_id: str = ""    # For OAuth flows

    # LLM settings (OpenAI-compatible — works with LM Studio, OpenAI, etc.)
    llm_base_url: str = "http://10.151.49.182:1234/v1"
    llm_api_key: str = "lm-studio"
    llm_model: str = "qwen/qwen3.5-35b-a3b"

    # Claude API (for production — falls back to llm_* settings if empty)
    anthropic_api_key: str = ""

    # Manuscript revise loop (issue #20). When enabled, after review agents run
    # the portal feeds their findings + deterministic checks back into a section
    # rewrite. Off by default — reviews always produce PRs regardless (the
    # "agents always help, never block" principle); revise is purely additive.
    manuscript_revise_enabled: bool = False
    manuscript_revise_max_passes: int = 2

    # Database
    database_url: str = "sqlite+aiosqlite:///./omc.db"

    # SLURM settings (Alliance Canada HPC)
    slurm_enabled: bool = False
    slurm_account: str = ""

    # Local SRA download staging (arbutus → fir over HTTP, no SSH)
    local_download_path: str = "/data/sra_downloads"

    # Staging API key (fir cron uses this to pull downloads + push status)
    staging_api_key: str = ""

    # ENA submission proxy (safety default: test server)
    ena_test_mode: bool = True

    # microscape.app viz hosting. OMC provisions a per-user omc-<login> lab via
    # the provision endpoint (service token), then pushes the built viz site to
    # the deploy endpoint. Co-located on arbutus, so the base URL is internal.
    microscape_app_url: str = "http://localhost:3100"
    microscape_app_public_url: str = "https://microscape.app"
    microscape_provision_token: str = ""  # shared secret with microscape-app

    # Pipeline paths on HPC.
    # The danaSeq repo is organised into building-block pipelines. OMC's
    # user-facing pipelines compose them:
    #   Nanopore Metagenome (NANOPORE_MAG) = nanopore_assembly -> mag_analysis
    #   Illumina Metagenome (ILLUMINA_MAG) = illumina_assembly -> mag_analysis
    #   Illumina Amplicons  (MICROSCAPE)   = microscape-nf (runs from its SIF)
    pipeline_base: str = "/home/rec3141/GENICE/danaSeq"
    pipeline_nanopore_assembly: str = "/home/rec3141/GENICE/danaSeq/nanopore_assembly"
    pipeline_illumina_assembly: str = "/home/rec3141/GENICE/danaSeq/illumina_assembly"
    pipeline_mag_analysis: str = "/home/rec3141/GENICE/danaSeq/mag_analysis"
    # microscape ships as a self-contained SIF (pipeline code baked into the image)
    microscape_sif: str = "/home/rec3141/GENICE/microscape-nf.sif"
    # microscape taxonomy reference DB(s), format "name:path:Level1,Level2,...".
    # REQUIRED for output: the taxonomy → renormalize → BUILD_VIZ branch (which
    # produces the viz/ folder) is gated on --ref_databases. SILVA SSU covers
    # 16S + 18S; ITS would need UNITE. `{db}` is substituted with the executing
    # cluster's ${OMC_DB_DIR} at run time so this is portable across clusters.
    microscape_ref_databases: str = (
        "silva:{db}/silva_db/SILVA_138.2_SSURef_NR99.fasta"
        ":Domain,Phylum,Class,Order,Family,Genus"
    )
    # rnaseq (illumina_rna) is on hold; isolate genomes are submitted as metagenomes
    pipeline_rnaseq: str = ""

    # HPC paths. These are the *defaults* baked into a generated pipeline script;
    # the script reads them as ${OMC_SCRATCH}/${OMC_GENICE}/${OMC_DB_DIR} so the
    # pickup on any cluster can override them (fir, nibi, …) without regenerating.
    hpc_scratch: str = "/home/rec3141/scratch"
    hpc_genice_dir: str = "/home/rec3141/GENICE"   # holds danaSeq + microscape-nf.sif
    hpc_db_dir: str = "/home/rec3141/scratch/databases"
    results_path: str = "/home/rec3141/scratch/omc_results"

    # illumina_assembly host (human) read removal. The pipeline needs a bbmap
    # index of the masked hg19 reference at ${OMC_DB_DIR}/human_ref (build it
    # with danaSeq's download-databases.sh --human). Until that DB is present on
    # every executing cluster, keep this off (--run_remove_human false), which is
    # fine for environmental data with no human host. Flip to True once the DB is
    # staged everywhere; OMC will then pass --human_ref instead.
    illumina_remove_human: bool = True

    @property
    def admin_logins(self) -> set[str]:
        """Parsed, lowercased set of admin GitHub logins."""
        return {x.strip().lower() for x in self.admin_github_logins.split(",") if x.strip()}

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
