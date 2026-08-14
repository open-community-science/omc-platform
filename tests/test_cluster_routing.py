"""Which cluster may claim a staged run.

A submission can be pinned to one HPC (Submission.target_cluster); an unpinned
one belongs to whichever cluster the admin panel has made active. The decision
travels with the staged data as a `.cluster` marker, so the staging API can route
without a database lookup.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portal.app.slurm as slurm
import portal.app.staging as staging
from portal.app.database import Submission


def _staged(tmp_path: Path, slug: str, pinned: str | None) -> Path:
    d = tmp_path / slug
    d.mkdir()
    if pinned is not None:
        (d / ".cluster").write_text(pinned)
    return d


def test_pinned_run_goes_only_to_its_cluster(tmp_path):
    d = _staged(tmp_path, "abc", "grex")
    assert staging._claimable_by(d, "grex", active="fir")
    assert not staging._claimable_by(d, "fir", active="fir")


def test_unpinned_run_goes_to_the_active_cluster(tmp_path):
    d = _staged(tmp_path, "abc", None)
    assert staging._claimable_by(d, "fir", active="fir")
    assert not staging._claimable_by(d, "grex", active="fir")


def test_pinning_survives_the_cluster_being_standby(tmp_path):
    """A pinned run is the whole point of pinning: the switch doesn't override it."""
    d = _staged(tmp_path, "abc", "grex")
    assert staging._claimable_by(d, "grex", active="nibi")


def test_an_empty_marker_reads_as_unpinned(tmp_path):
    d = _staged(tmp_path, "abc", "  \n")
    assert staging._claimable_by(d, "fir", active="fir")
    assert not staging._claimable_by(d, "grex", active="fir")


def test_a_caller_that_names_no_cluster_gets_unpinned_work_only(tmp_path):
    """A loop too old to name itself is too old to respect a pin.

    Offering it unpinned runs matches what it saw before pinning existed, and
    stops an un-upgraded cluster (nibi, which needs MFA to reach) from claiming
    another cluster's run should it ever be made active.
    """
    assert not staging._claimable_by(_staged(tmp_path, "a", "grex"), "", active="fir")
    assert staging._claimable_by(_staged(tmp_path, "b", None), "", active="fir")


def test_marker_tracks_the_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(slurm.settings, "local_download_path", str(tmp_path))
    sub = Submission(slug="abc")
    (tmp_path / "abc").mkdir()
    marker = tmp_path / "abc" / ".cluster"

    sub.target_cluster = "grex"
    slurm.write_cluster_marker(sub)
    assert marker.read_text() == "grex"

    # Retargeting before pickup rewrites it; unpinning removes it entirely, so
    # the run falls back to the active cluster rather than to a stale name.
    sub.target_cluster = "fir"
    slurm.write_cluster_marker(sub)
    assert marker.read_text() == "fir"

    sub.target_cluster = None
    slurm.write_cluster_marker(sub)
    assert not marker.exists()


def test_marker_write_is_a_noop_before_the_run_is_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(slurm.settings, "local_download_path", str(tmp_path))
    sub = Submission(slug="not-staged-yet", target_cluster="grex")
    slurm.write_cluster_marker(sub)
    assert not (tmp_path / "not-staged-yet").exists()
