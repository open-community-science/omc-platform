# Autoresearch — Deploy & Enable Checklist (arbutus prod)

Claim-grounded autoresearch (issue #29) is on `main` but **gated off**
(`AUTORESEARCH_ENABLED=false`). Nothing below changes existing behavior until you
flip that flag. The feature needs three things live at once: **updated code**, the
**`.env` flags**, and a **submission with completed `.sqsh` results** whose
`omc-session` container can launch.

## 0. Preconditions (already true on arbutus — verify once)

- [ ] Portal service user is in the `docker` group — `ContainerExecutor` shells
      out to `docker exec`. Check: `sudo -u <portal-user> docker ps` works without sudo.
- [ ] Session image built: `docker image inspect omc-session:latest`.
- [ ] Session network up: `docker network inspect omc-sessions`.
- [ ] `squashfuse` + FUSE `user_allow_other` configured (same mount path sessions
      already use).
- [ ] LLM reachable from the portal (`LLM_BASE_URL` tunnel healthy) — autoresearch
      uses the same `resolve_llm` path as reviews.

## 1. Deploy the code

Standard rsync deploy (excludes as always):

```bash
rsync -av --exclude .venv --exclude .env --exclude omc.db \
  /data/dev/omc-platform/ arbutus:/opt/omc-platform/
```

New/changed files that must land (spot-check after rsync):

- [ ] `ai/autoresearch.py` (core)
- [ ] `portal/app/autoresearch.py` (route) + `portal/app/autoresearch_executor.py`
      (ContainerExecutor)
- [ ] `portal/templates/_autoresearch_trigger.html`, `portal/templates/provenance.html`
- [ ] `portal/app/config.py`, `portal/app/main.py` (router registration)

Sanity import on the host **before** restart:

```bash
cd /opt/omc-platform && python3 -c "import ai.autoresearch, portal.app.autoresearch, portal.app.autoresearch_executor; print('imports OK')"
```

## 2. Set the `.env` flags (`/opt/omc-platform/portal/.env`)

Start conservative — commit **off**, reconcile **on**:

```ini
AUTORESEARCH_ENABLED=true
AUTORESEARCH_COMMIT_ENABLED=false        # keep OFF for first runs — no auto-PR to .omc/ yet
AUTORESEARCH_RECONCILE_ENABLED=true
# Budgets (defaults shown — tune down for the first live run if you want a quick one):
AUTORESEARCH_MAX_STEPS=48
AUTORESEARCH_MAX_FOLLOWUPS=12
AUTORESEARCH_TIME_BUDGET_S=1800
AUTORESEARCH_MAX_ANALYSIS_S=60           # per run_analysis exec, inside the sandbox
# Models (fall back to LLM_MODEL if unset):
LLM_MODEL_EXPLORE=<agent model>
LLM_MODEL_VERIFY=<skeptical reconciler model>
```

- [ ] Restart: `sudo systemctl restart omc-portal`
- [ ] Confirm live: portal log shows the `autoresearch` router registered; the
      route responds (403 if a request lacks auth, **not** 404).

## 3. Pick a target submission

Autoresearch **requires completed pipeline results** — it errors cleanly if the
`.sqsh` is missing.

- [ ] Choose a slug with `results_format` at `archived`/`transferred` and confirm
      the archive exists: `ls -la /data/results/<slug>.sqsh`.
- [ ] Confirm the viz JSON is inside it (route reads `.../viz/data`): after a
      session mount, `/mnt/omc-sessions/<slug>/viz/data/*.json*` should be non-empty.

## 4. First live run (watch it)

The Step-3 trigger appears on the submission page only when `autoresearch_enabled`
is true.

- [ ] Trigger via UI **or** stream directly:

  ```bash
  curl -N -X POST https://microbial.opencommunity.science/autoresearch/<slug>/run-stream \
    -H "Cookie: <authed session cookie>"
  ```

- [ ] Watch SSE events: `propose_agenda` → `run_analysis` → `record_claim` →
      verify events.
- [ ] In another shell, confirm code is actually running **in the container, not
      the host**: `docker ps | grep omc-session-<slug>` is up during the run, and
      `docker exec` processes appear (the sandbox constraint from #29).

## 5. Verify results & provenance

- [ ] Provenance DAG viewer renders: `GET /autoresearch/<slug>/provenance`
      (method badges: `direct` / `derived` / `reconciled`).
- [ ] Persisted snapshot present: submission's `interview_data['_autoresearch']`
      has claims with verdicts + the Results prose.
- [ ] Spot-check honesty: any low-quality / contamination / depth caveats are
      stated plainly, and no claim carries a number that isn't backed by a
      verified computation.

## 6. Rollback (instant, no redeploy)

- [ ] Set `AUTORESEARCH_ENABLED=false` in `.env` → `sudo systemctl restart
      omc-portal`. Route returns 403, Step-3 trigger disappears. Nothing else is
      affected.

## 7. Only after runs look good — enable commit

- [ ] `AUTORESEARCH_COMMIT_ENABLED=true` → restart. Now verified Results prose is
      PR'd to the paper repo `.omc/`. Requires the submission's `github_repo` set
      and GitHub App auth healthy. Review the **first** auto-PR by hand before
      trusting the flow.

---

## Watch-outs

- **`docker exec` permission** is the #1 first-run failure — if the portal user
  can't reach Docker, every `run_analysis` returns `"docker not available on
  host"` and all computed claims fail verification. Test in step 0.
- **Non-deterministic computations** (PERMANOVA / permutation p-values) won't
  reproduce on re-execution → they're refuted deterministically and recovered only
  by the reconciler. Keep `AUTORESEARCH_RECONCILE_ENABLED=true` or expect those
  claims to drop (tracked in issue #39).
- **Budget vs. wall-clock**: `MAX_STEPS=48` + a slow reasoning model can run many
  minutes; the SSE stream keeps the request alive but confirm nginx
  `proxy_read_timeout` on the portal `/` location is generous (it already is for
  `/staging/`).
- **First run**: consider `MAX_STEPS=12` for a fast confidence check before a full
  48-step run.
