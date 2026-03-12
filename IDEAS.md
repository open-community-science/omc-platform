# IDEAS.md - Future Discussion Topics

Notes and ideas for future development of the OMC platform.

---

## Architecture Questions

### Remote SLURM Submission ✅ DECIDED: asyncssh
Alliance Canada does NOT expose slurmrestd. SSH requires 2FA from external networks, but the portal will run on an Alliance Cloud VM (likely internal network, no 2FA). Using **asyncssh** for async-native fit with FastAPI. Revisit if 2FA is required on internal network.

### Database Choice ✅ DECIDED: SQLite
SQLite is sufficient for the expected scale. SQLAlchemy abstraction means migration to Postgres is trivial if ever needed.

### Job Status Monitoring ✅ DECIDED: Completion marker + daily poll
Job script writes `.completed` file. Portal checks once daily via SSH (cron or scheduled task). Not time-sensitive — even daily is fine.

---

## Feature Ideas

### Pipeline Output Processing ✅ DECIDED: All plots, AI curates
nanopore_mag already has a `viz/` step that produces interactive plots + JSONs. Approach:
1. Pipeline outputs all plots from viz JSONs (interactive + static parallel versions)
2. AI selects the most salient figures for the first draft
3. Authors iterate on figure selection/emphasis **through the portal only** (no local download/reupload)
4. All interactions are training data

**Key principle: everything happens in the portal.** No local round-trips — this preserves the training data flywheel.

### Paper Rendering ✅ DECIDED: Quarto → GitHub Pages
Single self-contained HTML per paper via Quarto (`self-contained: true`). GitHub Action renders `.qmd` → `index.html` on PR merge. Served at `omc-papers.github.io/paper-NNNN`. MIT licensed, built on Pandoc — output is dependency-free static HTML that survives even if Quarto disappears.

### Citation Graph / Discoverability — DEFERRED
Will be a whole module later. Citation network, cross-references, search across OMC papers.

### AI Modules ✅ IMPLEMENTED (prototype)
All AI modules use an **OpenAI-compatible API** adapter (`ai/llm_client.py`), so they work with LM Studio locally or any cloud provider (OpenAI, Anthropic via proxy, etc.) by changing `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`.

**Implemented modules:**
- `ai/manuscript_generator.py` — generates introduction, methods, results, discussion, abstract (in order, abstract last)
- `ai/review_agents.py` — statistical, methodological, clarity review agents with structured JSON output + confidence scores (0.0–1.0)
- `ai/author_interview.py` — conversational multi-turn interview, metadata-aware, auto-detects completion, extracts structured summary
- `ai/metadata_assistant.py` — SRA metadata preparation helper (stub)

**Author interview design** (needs wiring into portal routes):
- NOT a fixed questionnaire — free-form conversation
- AI has full BioProject metadata loaded in context
- BioProject accession required before interview begins
- Asks about intentions, hypotheses, research goals
- Collects key references / BibTeX
- Adapts questions based on what metadata already provides vs gaps
- Interview can run in parallel with pipeline execution
- Sample/run selection happens dynamically during interview

**Submission flow (two AI stages):**
1. Metadata assistant (helps prepare SRA submission if needed) — pre-submission
2. Author interview (AI has metadata, asks about science + refs) — post-submission, parallel with pipeline

**Better Manuscript Generation** — DEFERRED
- Few-shot examples from good papers
- Style matching to target journals
- Iterative refinement based on review feedback

**Review Agent Enhancements** — DEFERRED
- Domain-specific checklists (e.g., MIMARKS compliance)
- Integration with checklists from journals
- Comparison with similar published papers

---

## Integration Ideas

### SRA Metadata Auto-Fill ✅ IMPLEMENTED
On accession entry, fetch complete metadata from NCBI via Entrez. Any INSDC accession (SRR, SAMN, SRX, SRP, PRJNA) resolves upward to the parent BioProject. Full esummary over all runs provides per-combination breakdown (platform/instrument/strategy/source/layout) with run, sample, and data counts. Metadata stored in the paper repo as `data/sra_metadata.json`.

### DOI Minting ✅ DECIDED: Zenodo from day one
Use Zenodo's GitHub integration — auto-mints DOI on release tag. Near-zero effort, papers are citable immediately.

### ORCID Integration — DEFERRED
GitHub identity sufficient for pilot. Add ORCID OAuth later for formal scholarly attribution.

### Preprint Posting — DEFERRED (bioRxiv/EcoEvoRxiv)
Zenodo handles DOI + archival. Quarto auto-generates PDF + HTML + DOCX from the same `.qmd` source, so a bioRxiv-ready PDF is always available in the repo for manual submission. Auto-posting to preprint servers deferred.

---

## Community & Process

### Pay-It-Forward Review System — DEFERRED
Design TBD after first real papers. Questions: how many reviews, matching, enforcement.

### Quality Tiers — DEFERRED
Rapid (AI-only) / Standard (human) / Comprehensive. Design after MVP.

### AI Review Agents ✅ PROTOTYPE IMPLEMENTED (3 of 6 agents)
**Design principles:**
- Don't hold back due to uncertainty, but be transparent about confidence
- Each comment includes a **confidence score** (0.0–1.0) so authors can triage
- **Educational tone** — every flag explains *why it matters* and suggests alternatives with references. Researchers should learn something from the review, not just feel judged
- Comment format: flag → explanation → suggestion → reference
- Author corrections to AI claims are high-quality training data
- Structured JSON output: `{"review_type", "comments": [{"section", "issue", "detail", "severity", "confidence"}], "summary"}`

Agents (all as GitHub PR comments):
1. **Statistical** ✅ — appropriate tests, multiple testing, effect sizes, sample size
2. **Methodological** ✅ — pipeline params, QC, reproducibility, MIMARKS/MIxS compliance
3. **Completeness** — all sections present, figures referenced, data availability, accessions
4. **Biological plausibility** — ecological sense, unexpected taxa, contamination flags, known artifacts
5. **Clarity** ✅ — writing quality, jargon, flow, abstract alignment
6. **Citation** — do refs support claims, missing key refs, outdated refs (needs lit search)

### Training Data Flywheel — DEFERRED
Consent mechanism, data format, privacy. Think about before launch but implement later.

---

## Technical Debt / Cleanup

- [ ] Add proper error handling throughout
- [x] Implement SSH-based SLURM submission (asyncssh, `portal/app/slurm.py`)
- [x] BioProject-first accession resolution (`portal/app/sra_metadata.py`)
- [x] Pipeline auto-selection from library tags
- [x] AI modules with OpenAI-compatible API adapter (`ai/`)
- [ ] Wire AI interview into portal routes
- [ ] Add tests (pytest)
- [ ] Type hints everywhere
- [ ] API documentation (OpenAPI/Swagger)
- [ ] Rate limiting on routes
- [ ] Better session management
- [ ] Logging infrastructure

---

## Open Questions

1. **Should papers be public by default or opt-in?** ✅ DECIDED: Always public
   No private option. Aligns with open science, keeps things simple, free GitHub repos.

2. **How to handle data that can't be on GitHub?** ✅ DECIDED: Lightweight repos + containerized reproducibility
   Repos contain only manuscript, figures, tables, stats. Intermediate files are trashed after pipeline completes. Reproducibility via containers — nanopore_mag already containerized, all pipelines will be. Future: easy import of workflow into Seqera/AWS/user's own HPC for tinkering.

3. **What's the minimal viable pilot?** ✅ DECIDED: Lean MVP
   Submit → Pipeline → AI draft → Manual review on GitHub. Author interview and review agents are prototyped (`ai/`) but not yet wired into the portal. Portal-based editing deferred until we learn from first real papers.

4. **Domain name?** ✅ DECIDED: opencommunity.science
   Registered on Porkbun. Extensible to other fields via subdomains (microbial.opencommunity.science, etc). DNS only — portal hosted on Alliance VM, papers on GitHub Pages. No traditional web hosting needed.

---

## Next Session TODOs

- [ ] Wire AI interview into portal routes (template + endpoints)
- [ ] Wire AI review agents into GitHub PR workflow
- [ ] Create `omc-papers` GitHub organization
- [ ] Set up GitHub OAuth app
- [ ] Alliance Canada VM setup + configure DNS for opencommunity.science
- [ ] End-to-end test with real pipeline run
- [ ] Parse pipeline outputs from HPC (currently placeholder in `pipeline_processing.py`)
- [ ] Implement remaining review agents (completeness, biological plausibility, citation)
