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

    # Per-role model overrides (issue #21). Each falls back to llm_model when
    # unset, so single-model setups are unaffected. Lets an operator point the
    # cheap/high-volume citation loop at a small/local model while drafting and
    # review use a stronger one — and enables local-vs-remote model comparisons.
    llm_model_draft: str = ""
    llm_model_cite: str = ""
    llm_model_review: str = ""

    # Claude API (for production — falls back to llm_* settings if empty)
    anthropic_api_key: str = ""

    # Manuscript revise loop (issue #20). When enabled, after review agents run
    # the portal feeds their findings + deterministic checks back into a section
    # rewrite. Off by default — reviews always produce PRs regardless (the
    # "agents always help, never block" principle); revise is purely additive.
    manuscript_revise_enabled: bool = False
    manuscript_revise_max_passes: int = 2

    # Outline-then-fill drafting. When on, each section is drafted in two phases
    # (a data-grounded bullet outline, then prose from that outline; the outline
    # is discarded). Benchmarks show this cuts fabricated numbers on smaller
    # models with negligible cost on stronger ones. Doubles per-section LLM calls.
    manuscript_outline_first: bool = True

    # Claim-grounded autoresearch (issue #29). An author drives an autonomous
    # analysis run over their pipeline data: the agent proposes an agenda, runs
    # its own analysis code inside the isolated session container (the sandbox),
    # records verifiable claims, then a two-layer verification (deterministic
    # re-execution + skeptical reconciliation) grades each claim before the
    # Results prose is written from the verified ones. Off by default — it needs
    # completed pipeline results (.sqsh) and a running session container.
    autoresearch_enabled: bool = False
    autoresearch_max_steps: int = 48          # agent tool-call budget per run
    autoresearch_max_followups: int = 12      # cap on self-added agenda items
    autoresearch_time_budget_s: int = 1800    # wall-clock cap on explore()
    autoresearch_max_analysis_s: int = 60     # per run_analysis exec timeout (in-sandbox)
    # OBSOLETE (kept so existing .env files still load): verification is no longer
    # "deterministic matching with a model fallback" — the model IS the verifier, so
    # there is nothing to enable or disable. See ai.autoresearch.JUDGE_SYSTEM.
    autoresearch_reconcile_enabled: bool = True
    # Clean-room replication (#50). After verification, a SECOND analyst — shown the
    # claim and the raw data but never the original code — writes its own analysis to
    # re-derive it. Agreement upgrades the claim to "replicated"; disagreement marks it
    # "disputed". Off by default: it costs a model call plus a sandbox run per claim.
    # Point llm_model_replicate at a DIFFERENT model than explore — a model checking
    # its own work shares its own blind spots.
    autoresearch_replicate_enabled: bool = False
    autoresearch_replicate_max_claims: int = 12
    # Round 3 (#50). When rounds 1 and 2 disagree, or a claim never came back out of
    # its own antecedents, a THIRD independent derivation breaks the tie with evidence
    # rather than opinion. Only fires on claims left in doubt, so it is cheap in
    # practice; gated by autoresearch_replicate_enabled.
    autoresearch_adjudicate_enabled: bool = True
    autoresearch_commit_enabled: bool = False     # PR the Results prose to .omc/
    # Per-role model overrides for the two autoresearch phases (fall back to
    # llm_model, like the manuscript roles above).
    llm_model_explore: str = ""               # the agenda-driven agent loop
    llm_model_verify: str = ""                # the skeptical reconciler
    llm_model_replicate: str = ""             # the clean-room replication analyst
    llm_model_adjudicate: str = ""            # round 3's casting-vote analyst

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

    def role_model(self, role: str, default: str = "") -> str:
        """Model for an AI role ('draft' | 'cite' | 'review' | 'explore' |
        'verify' | 'replicate' | 'adjudicate'), or `default`.

        Returns the per-role override when configured, else `default` (the
        caller's resolved model) so per-user backend choices still apply.
        """
        override = {
            "draft": self.llm_model_draft,
            "cite": self.llm_model_cite,
            "review": self.llm_model_review,
            "explore": self.llm_model_explore,
            "verify": self.llm_model_verify,
            "replicate": self.llm_model_replicate,
            "adjudicate": self.llm_model_adjudicate,
        }.get(role, "")
        return override or default

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
