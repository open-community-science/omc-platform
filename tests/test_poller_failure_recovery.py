"""A later successful run must undo a recorded failure — but a stale one must not.

Issue #53: FAILED was terminal for the status poller, so cancelling a hung job
and resubmitting left the portal showing a failure whose results were sitting on
disk. Recovering from FAILED is only safe if we can tell "the cluster has since
made progress" from "an old status file is still lying around", which is what
_failure_superseded decides.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portal.app.slurm as slurm
import portal.app.staging as staging
from portal.app.database import Submission, SubmissionStatus, PipelineType, ResultsFormat

NOW = datetime(2026, 7, 24, 12, 0, 0)
EARLIER = NOW - timedelta(hours=2)
LATER = NOW + timedelta(hours=2)


def _sub(**kw):
    kw.setdefault("slug", "abc123")
    kw.setdefault("pipeline", PipelineType.ILLUMINA_MAG)
    return Submission(**kw)


# ── the guard ─────────────────────────────────────────────────────────────────

def test_a_new_job_id_supersedes_the_failure():
    sub = _sub(slurm_job_id="1000", completed_at=NOW)
    assert slurm._failure_superseded(sub, {"phase": "running", "job_id": "2000"}, None)


def test_a_status_file_written_after_the_failure_supersedes_it():
    # Same job id (the re-run's later pushes may omit it), but the cluster has
    # spoken since we gave up.
    sub = _sub(slurm_job_id="1000", completed_at=NOW)
    assert slurm._failure_superseded(sub, {"phase": "transferred"}, LATER)


def test_a_status_file_older_than_the_failure_does_not():
    # The classic stale-file case: this push predates the failure, so it is not
    # evidence of anything new.
    sub = _sub(slurm_job_id="1000", completed_at=NOW)
    assert not slurm._failure_superseded(sub, {"phase": "completed"}, EARLIER)


def test_a_failed_phase_never_supersedes():
    sub = _sub(slurm_job_id="1000", completed_at=EARLIER)
    assert not slurm._failure_superseded(sub, {"phase": "failed", "job_id": "2000"}, LATER)


def test_no_evidence_at_all_leaves_it_failed():
    # No differing job id and no failure timestamp to compare against: refuse to
    # guess rather than resurrect a genuinely failed run.
    sub = _sub(slurm_job_id="1000", completed_at=None)
    assert not slurm._failure_superseded(sub, {"phase": "completed"}, None)


def test_unknown_phase_is_not_progress():
    sub = _sub(slurm_job_id="1000", completed_at=NOW)
    assert not slurm._failure_superseded(sub, {"phase": "", "job_id": "2000"}, LATER)


# ── the poller loop ───────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, subs): self._subs = subs
    def scalars(self): return self
    def all(self): return self._subs


class _FakeSession:
    """Just enough session for poll_all_running_jobs."""
    def __init__(self, subs): self.subs, self.committed = subs, False
    async def execute(self, stmt): return _FakeResult(self.subs)
    async def commit(self): self.committed = True


def _poll(monkeypatch, sub, hpc, mtime=None):
    monkeypatch.setattr(staging, "get_hpc_status", lambda slug: hpc)
    monkeypatch.setattr(staging, "get_hpc_status_mtime", lambda slug: mtime)
    session = _FakeSession([sub])
    asyncio.run(slurm.poll_all_running_jobs(session))
    return sub


def test_failed_submission_recovers_when_a_new_job_transferred(monkeypatch):
    sub = _sub(status=SubmissionStatus.FAILED, slurm_job_id="1000",
               completed_at=NOW, error_message="SLURM job 1000 ended: CANCELLED+")

    _poll(monkeypatch, sub, {"phase": "transferred", "job_id": "2000"})

    assert sub.status == SubmissionStatus.RESULTS_READY
    assert sub.results_format == ResultsFormat.TRANSFERRED
    assert sub.error_message is None      # the old failure text must not linger
    assert sub.slurm_job_id == "2000"     # now points at the job that succeeded


def test_failed_submission_stays_failed_on_a_stale_status_file(monkeypatch):
    sub = _sub(status=SubmissionStatus.FAILED, slurm_job_id="1000",
               completed_at=NOW, error_message="it broke")

    _poll(monkeypatch, sub, {"phase": "completed", "job_id": "1000"}, mtime=EARLIER)

    assert sub.status == SubmissionStatus.FAILED
    assert sub.error_message == "it broke"


def test_a_running_submission_is_unaffected_by_the_new_branch(monkeypatch):
    sub = _sub(status=SubmissionStatus.RUNNING, slurm_job_id="1000")

    _poll(monkeypatch, sub, {"phase": "transferred", "job_id": "1000"})

    assert sub.status == SubmissionStatus.RESULTS_READY


def test_post_processing_failure_is_not_undone_by_the_transfer_that_preceded_it(monkeypatch):
    """The most dangerous case for this change.

    pipeline_processing marks a submission FAILED when repo creation / drafting
    blows up — which happens *after* the pipeline succeeded, so the status file
    legitimately reads "transferred" and its job id matches. That is a real
    failure of a later stage, not a superseded one, and must survive polling.
    """
    sub = _sub(status=SubmissionStatus.FAILED, slurm_job_id="1000",
               completed_at=None,  # that path doesn't stamp it
               error_message="Post-processing failed: boom")

    _poll(monkeypatch, sub, {"phase": "transferred", "job_id": "1000"}, mtime=LATER)

    assert sub.status == SubmissionStatus.FAILED
    assert sub.error_message == "Post-processing failed: boom"


def test_marking_failed_stamps_completed_at(monkeypatch):
    # Without this the mtime arm of the guard has nothing to compare against,
    # which is why every pre-existing failure had completed_at = None.
    sub = _sub(status=SubmissionStatus.RUNNING, slurm_job_id="1000")

    _poll(monkeypatch, sub, {"phase": "failed", "reason": "boom", "job_id": "1000"})

    assert sub.status == SubmissionStatus.FAILED
    assert sub.error_message == "boom"
    assert sub.completed_at is not None
