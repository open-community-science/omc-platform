"""Production ``CodeExecutor`` — runs model-written analysis code INSIDE the
isolated ``omc-session-{slug}`` container via ``docker exec``, never on the portal
host. The container is the sandbox (issue #29): the ``omc-sessions`` bridge drops all
outbound except the portal proxy, ``/data`` is a read-only squashfuse mount, and
the container is capped at 2g/1cpu and ephemeral (see ``portal/app/sessions.py``).

The child runner is the SAME ``_SUBPROCESS_RUNNER`` the dev/offline
``SubprocessExecutor`` uses (imported from ``ai.autoresearch``), so re-execution
at verification time is byte-identical to exploration time — it reads the viz
JSON from ``/data/viz/data`` (the ``.sqsh`` bind-mounted read-only) inside the
container and prints a single JSON line (``{"__ok__": ...}`` or ``{"__err__": ...}``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ai.autoresearch import _SUBPROCESS_RUNNER

logger = logging.getLogger(__name__)

# The submission's viz JSON inside the container: the .sqsh is bind-mounted
# read-only at /data (sessions.py: -v {mount}:/data:ro), and the amplicon viz
# lives under viz/data (matching the host path the DirDataSource reads).
CONTAINER_DATA_DIR = "/data/viz/data"


class ContainerExecutor:
    """``CodeExecutor`` that executes a cell in the running session container.

    Implements the protocol ``async run(code, timeout) -> (ok, result_or_error)``.
    """

    def __init__(self, slug: str, default_timeout: int = 60,
                 data_dir: str = CONTAINER_DATA_DIR):
        self.container = f"omc-session-{slug}"
        self.default_timeout = default_timeout
        self.data_dir = data_dir

    async def run(self, code: str, timeout: int | None = None) -> tuple[bool, Any]:
        t = int(timeout or self.default_timeout)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-i",
                "-e", f"EXPLORER_DATA_DIR={self.data_dir}",
                # single-threaded BLAS — deterministic + small virtual footprint
                "-e", "OMP_NUM_THREADS=1", "-e", "OPENBLAS_NUM_THREADS=1", "-e", "MKL_NUM_THREADS=1",
                self.container, "python3", "-c", _SUBPROCESS_RUNNER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, "docker not available on host"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(code.encode()), timeout=t + 5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False, f"timeout >{t}s"

        out = (stdout or b"").decode(errors="replace").strip().splitlines()
        try:
            r = json.loads(out[-1]) if out else {}
        except json.JSONDecodeError:
            err = ((stderr or b"").decode(errors="replace")
                   or "\n".join(out) or "no output").strip().splitlines()
            return False, (err[-1] if err else "error")[:200]
        return (True, r["__ok__"]) if "__ok__" in r else (False, r.get("__err__", "error"))
