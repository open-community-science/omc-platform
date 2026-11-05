"""cancel_staging() kills the download tree and clears the pickup markers."""
import os, subprocess, sys, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "portal"))


@pytest.fixture
def staging(tmp_path, monkeypatch):
    from app import staging as st
    monkeypatch.setattr(st.settings, "local_download_path", str(tmp_path))
    return st, tmp_path


def test_cancel_kills_download_tree_and_clears_markers(staging):
    st, root = staging
    d = root / "abc12345"; d.mkdir()
    (d / "fastq").mkdir(); (d / "fastq" / "SRR1_1.fastq.gz").write_bytes(b"x" * 10)
    (d / ".run-ready").mkdir(); (d / ".run-ready" / "SRR1").write_text("t")
    (d / ".queued").write_text("t"); (d / "SRR2").mkdir(); (d / "tmp").mkdir()
    script = d / "download.sh"
    script.write_text("#!/bin/bash\nsleep 300 &\nwait\n")
    proc = subprocess.Popen(["bash", str(script)])
    time.sleep(0.5)

    res = st.cancel_staging("abc12345")

    proc.wait(timeout=10)
    assert res["killed"] >= 1 and res["freed_bytes"] == 10
    assert (d / st.CANCELLED_MARKER).exists()
    assert not (d / ".queued").exists() and not (d / ".run-ready").exists()
    assert not (d / "SRR2").exists() and not (d / "tmp").exists()
    assert list((d / "fastq").iterdir()) == []
    assert st._is_cancelled(d)
    # the sleep child is gone too
    out = subprocess.run(["pgrep", "-f", "sleep 300"], capture_output=True, text=True).stdout
    assert str(proc.pid) not in out.split()


def test_cancel_without_staging_dir_still_marks(staging):
    st, root = staging
    st.cancel_staging("nodir123")
    assert (root / "nodir123" / st.CANCELLED_MARKER).exists()
