"""Every status the pipeline pushes must be JSON the portal can parse.

The bodies are assembled by shell string-splicing inside the generated
pipeline.sh, so a quoting slip produces a body that is still *shaped* like
JSON and fails only at the server. It did: `extra` was written with escaped
quotes (',\\"job_id\\":...') which bash leaves intact inside single quotes,
so every push carrying extra fields — running, completed, transferred,
failed — sent invalid JSON and 500ed. `curl ... || true` swallowed it, so
runs finished silently and only the pickup reconciler ever corrected the
status. The one push with no extra fields, "archiving", was the only one
that worked, which is why the breakage looked like a partial outage.

This test runs the generated push_status through a real bash and parses what
curl would have sent.
"""
import json
import re
import shutil
import subprocess

import pytest

from portal.app.slurm import _build_pipeline_script


class _Sub:
    """Minimal stand-in for a Submission the generator can render."""
    slug = "testslug"
    bioproject_accession = "PRJNA000000"
    sra_accession = None
    selected_runs = ["SRR0000001"]
    title = "test"
    sample_metadata = {}
    interview_data = {}
    primers = {}
    slurm_job_id = None

    def __init__(self, pipeline):
        self.pipeline = pipeline


def _pipelines():
    from portal.app.database import PipelineType
    return list(PipelineType)


def _push_calls(script: str) -> list[tuple[str, str]]:
    """Extract (phase, extra) from each push_status call in the script."""
    calls = re.findall(r"""^\s*push_status "(\w+)"(?:\s+('[^\n]*'))?\s*$""",
                       script, re.MULTILINE)
    return [(phase, extra or "") for phase, extra in calls]


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("pipeline", _pipelines())
def test_every_pushed_status_body_is_valid_json(pipeline):
    try:
        script = _build_pipeline_script(_Sub(pipeline))
    except NotImplementedError:
        pytest.skip(f"{pipeline.value} is not HPC-submittable yet")
    calls = _push_calls(script)
    assert calls, f"no push_status calls found for {pipeline}"

    # Rebuild the body exactly as push_status does, in a real shell.
    for phase, extra in calls:
        prog = (
            'SLURM_JOB_ID=12345\n'
            'PIPELINE_EXIT=0\n'
            f'phase="{phase}"\n'
            f'extra={extra if extra else '""'}\n'
            r'printf "%s" "{\"phase\":\"$phase\"$extra}"'
            '\n'
        )
        body = subprocess.run(["bash", "-c", prog], capture_output=True,
                              text=True, check=True).stdout
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"{pipeline} push_status {phase!r} produced invalid JSON: "
                f"{body!r} ({exc})"
            )
        assert parsed["phase"] == phase


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_the_transferred_push_carries_its_results_format():
    """The push that tells the portal results are ready must survive quoting.

    This is the one whose loss stranded a finished run at FAILED.
    """
    from portal.app.database import PipelineType
    script = _build_pipeline_script(_Sub(PipelineType.MICROSCAPE))
    transferred = [(p, e) for p, e in _push_calls(script) if p == "transferred"]
    assert transferred, "microscape must push a transferred status"
    _, extra = transferred[0]
    prog = (
        f'extra={extra}\n'
        r'printf "%s" "{\"phase\":\"transferred\"$extra}"'
        '\n'
    )
    body = subprocess.run(["bash", "-c", prog], capture_output=True,
                          text=True, check=True).stdout
    assert json.loads(body)["results_format"] == "archived"
