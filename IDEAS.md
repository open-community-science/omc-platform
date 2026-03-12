# IDEAS.md - Future Discussion Topics

Notes and ideas for future development of the OMC platform.

---

## Architecture Questions

### Remote SLURM Submission
Currently using local `sbatch` - need to implement SSH-based remote submission for when the portal runs on a separate VM from the HPC cluster. Options:
- **paramiko/asyncssh** - Direct SSH library
- **Fabric** - Higher-level SSH wrapper
- **SLURM REST API** - If available on Alliance Canada clusters (slurmrestd)

### Database Choice
Currently SQLite for simplicity. Consider:
- **PostgreSQL** for production (better concurrency, JSONB for metadata)
- **Keep SQLite** - It's probably fine for 50 papers

### Job Status Monitoring
How to track SLURM jobs efficiently:
- Polling with `squeue/sacct` (current approach)
- File-based completion markers (`.completed` file)
- Webhook/callback from job script
- SLURM REST API notifications

---

## Feature Ideas

### Pipeline Output Processing
Need to build the post-pipeline processing step:
1. Parse Nextflow execution trace
2. Convert outputs to interactive Plotly figures
3. Generate results tables (TSV)
4. Create figure legends from output metadata

**Q: What specific outputs from nanopore_mag and microscape should we capture?**

### Paper Rendering
Each paper repo needs a rendered view:
- **GitHub Pages** with custom template
- Interactive figure embedding (Plotly.js)
- Automatic rebuild on PR merge

Could use:
- Jekyll (GitHub native)
- Hugo/Quarto (more flexible)
- Custom static generator

### Citation Graph
Build citation network from:
- References added during review
- Cross-references between OMC papers
- DOI linking to external papers

### AI Improvements

**Smarter Interview**
- Adaptive questions based on pipeline type
- Skip questions that can be inferred from SRA metadata
- Claude-guided clarification follow-ups

**Better Manuscript Generation**
- Few-shot examples from good papers
- Style matching to target journals
- Iterative refinement based on review feedback

**Review Agent Enhancements**
- Domain-specific checklists (e.g., MIMARKS compliance)
- Integration with checklists from journals
- Comparison with similar published papers

---

## Integration Ideas

### SRA Metadata Auto-Fill
Before submission, fetch metadata from SRA:
```python
# Could pull: organism, collection date, geo_loc, etc.
from Bio import Entrez
Entrez.efetch(db="sra", id=accession)
```

### DOI Minting
For published papers:
- Zenodo integration (free DOIs)
- DataCite (if we get institutional support)

### ORCID Integration
- Auto-populate author info
- Credit tracking across papers

### Preprint Posting
Auto-submit accepted papers to:
- bioRxiv
- EcoEvoRxiv
- Zenodo

---

## Community & Process

### Pay-It-Forward Review System
Design decisions needed:
- How many reviews required before publishing?
- Review queue prioritization
- Reviewer matching (expertise, availability)
- What if someone doesn't complete their review obligation?

### Quality Tiers
Could have different publication tracks:
1. **Rapid** - AI review only, labeled as preprint
2. **Standard** - 1-2 human reviews
3. **Comprehensive** - 3+ reviews, statistical audit

### Training Data Flywheel
Each review PR is training data. Need:
- Consent mechanism for training use
- Data export format
- Privacy considerations (anonymize reviewers?)

---

## Technical Debt / Cleanup

- [ ] Add proper error handling throughout
- [ ] Implement SSH-based SLURM submission
- [ ] Add tests (pytest)
- [ ] Type hints everywhere
- [ ] API documentation (OpenAPI/Swagger)
- [ ] Rate limiting on routes
- [ ] Better session management
- [ ] Logging infrastructure

---

## Open Questions

1. **Should papers be public by default or opt-in?**
   - Currently assuming public (like GitHub)

2. **How to handle data that can't be on GitHub?**
   - Large files, sensitive data
   - Git LFS? External storage with links?

3. **What's the minimal viable pilot?**
   - Maybe skip review system for v0.1?
   - Just: submit → run → draft → manual review?

4. **Domain name?**
   - openmicrobial.community?
   - omc.science?
   - Something else?

---

## Next Session TODOs

- [ ] Create `omc-papers` GitHub organization
- [ ] Set up GitHub OAuth app
- [ ] Test the portal locally
- [ ] Alliance Canada VM setup
- [ ] First test submission with real SRA accession
