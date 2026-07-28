"""BioSample attribute tests (#62).

The experimental design lives in the BioSample records. What matters here is that it
is attributed to the right samples, that a fetch failure degrades rather than breaks,
and that what gets offered as a "grouping" is actually usable as a factor.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.biosample import design_columns, write_attributes  # noqa: E402
from ai.autoresearch import format_briefing  # noqa: E402


ATTRS = {
    "SAMN1": {"env_local_scale": "frost flowers", "collection_date": "2017-10",
              "geo_loc_name": "United Kingdom:Norwich", "env_medium": "SI.FF-1;FF"},
    "SAMN2": {"env_local_scale": "frost flowers", "collection_date": "2017-10",
              "geo_loc_name": "United Kingdom:Norwich", "env_medium": "SI.FF-2;FF"},
    "SAMN3": {"env_local_scale": "Brine", "collection_date": "2017-10",
              "geo_loc_name": "United Kingdom:Norwich", "env_medium": "SI.B-1;Brine"},
    "SAMN4": {"env_local_scale": "seawater", "collection_date": "2017-10",
              "geo_loc_name": "United Kingdom:Norwich", "env_medium": "SI.SW1;SW"},
}


class TestDesignColumns:
    def test_a_field_that_never_varies_is_not_a_grouping(self):
        """It describes the study, not the samples, and cannot group anything."""
        cols = design_columns(ATTRS)
        assert "geo_loc_name" not in cols
        assert "collection_date" not in cols

    def test_a_field_with_a_value_per_sample_is_an_identifier(self):
        assert "env_medium" not in design_columns(ATTRS)

    def test_the_real_treatment_survives(self):
        assert design_columns(ATTRS) == ["env_local_scale"]

    def test_design_hints_are_ranked_ahead_of_incidental_columns(self):
        attrs = {k: {**v, "zzz_batch": f"b{i % 2}"} for i, (k, v) in enumerate(ATTRS.items())}
        assert design_columns(attrs)[0] == "env_local_scale"

    def test_no_attributes_yields_no_groupings(self):
        assert design_columns({}) == []


class TestWriteAttributes:
    def test_a_cached_file_is_reused_without_a_fetch(self, tmp_path, monkeypatch):
        (tmp_path / "sample_attributes.json").write_text(json.dumps(ATTRS))
        monkeypatch.setattr("ai.biosample.fetch_attributes",
                            lambda *a, **k: pytest.fail("should not have fetched"))
        assert write_attributes(tmp_path, ["SAMN1"]) == ATTRS

    def test_a_failed_fetch_falls_back_to_the_cache(self, tmp_path, monkeypatch):
        """Missing covariates make an analysis poorer, not impossible — a network
        failure must not take the run with it."""
        (tmp_path / "sample_attributes.json").write_text(json.dumps(ATTRS))

        def boom(*a, **k):
            raise OSError("NCBI unreachable")

        monkeypatch.setattr("ai.biosample.fetch_attributes", boom)
        assert write_attributes(tmp_path, ["SAMN1"], refresh=True) == ATTRS

    def test_a_failed_fetch_with_no_cache_is_empty_not_an_exception(self, tmp_path,
                                                                   monkeypatch):
        def boom(*a, **k):
            raise OSError("NCBI unreachable")

        monkeypatch.setattr("ai.biosample.fetch_attributes", boom)
        assert write_attributes(tmp_path, ["SAMN1"]) == {}

    def test_a_corrupt_cache_is_refetched_rather_than_crashing(self, tmp_path,
                                                              monkeypatch):
        (tmp_path / "sample_attributes.json").write_text("{not json")
        monkeypatch.setattr("ai.biosample.fetch_attributes", lambda *a, **k: ATTRS)
        assert write_attributes(tmp_path, ["SAMN1"]) == ATTRS


class TestBriefingGroupings:
    """The briefing has to NAME the grouping columns. Every run so far invented one
    (high vs low sequencing depth) because nothing pointed at the real one. It names
    them and stops there — what to do with them is the analyst's call."""

    def test_groupings_are_named_with_their_sizes(self):
        text = format_briefing({
            "meta": {"shape": [63, 23], "columns": ["x", "env_local_scale"]},
            "meta_groupings": {"env_local_scale": {
                "n_groups": 5, "groups": {"frost flowers": 24, "Brine": 17}}}})
        assert "COLUMNS THAT GROUP THE SAMPLES" in text
        assert "meta['env_local_scale']: 5 groups" in text
        assert "frost flowers (n=24)" in text
        assert "..." in text          # 5 groups, 2 shown — say so rather than imply all
        # states what is there; does not tell the analyst how to use it
        assert "inventing" not in text and "use these" not in text

    def test_a_dataset_with_no_groupings_says_nothing(self):
        text = format_briefing({"meta": {"shape": [63, 4], "columns": ["x"]}})
        assert "COLUMNS THAT GROUP" not in text

    def test_the_source_prefix_convention_is_explained(self):
        text = format_briefing({
            "meta": {"shape": [63, 23], "columns": ["pipeline_x"]},
            "meta_groupings": {"biosample_env_local_scale": {
                "n_groups": 2, "groups": {"frost flowers": 24, "Brine": 17}}}})
        for src in ("pipeline_", "sra_", "biosample_"):
            assert src in text
        assert "they can disagree" in text


class TestSampleAttrition:
    """#59 — an unreconciled 84 beside a 63-row table does not merely withhold the
    attrition, it makes an analyst doubt the table. One was last seen concluding its
    own `counts` frame must be transposed."""

    PROV = {"stages": [{"id": s} for s in ("raw", "filter", "denoise", "final")],
            "total": {"raw": 100, "final": 60},
            "samples": {"S1": {"raw": 50, "filter": 40, "denoise": 30, "final": 30},
                        "S2": {"raw": 50, "filter": 40, "denoise": 30, "final": 30},
                        "S3": {"raw": 10, "filter": 8, "denoise": 0, "final": 0},
                        "S4": {"raw": 10, "filter": 0, "denoise": 0, "final": 0},
                        "S5": {"raw": 10, "filter": 9, "denoise": 5, "final": 0}}}

    def _summary(self):
        from ai.autoresearch import _provenance_summary
        return _provenance_summary(self.PROV, n_analysed=2)

    def test_attempted_and_analysed_are_both_reported(self):
        s = self._summary()
        assert s["n_samples_attempted"] == 5 and s["n_samples_analysed"] == 2

    def test_each_dropped_sample_is_attributed_to_where_it_was_lost(self):
        """A run of samples dying at one stage is a finding, not noise."""
        s = self._summary()
        assert s["n_dropped"] == 3
        assert s["dropped_at_stage"] == {"denoise": 1, "filter": 1, "final": 1}
        assert s["dropped_samples"] == ["S3", "S4", "S5"]

    def test_the_note_says_absent_not_zero(self):
        """`counts` has no row at all for these — an analyst told "zero reads" could
        reasonably expect a zero row and go looking for one."""
        assert "ABSENT" in self._summary()["note"]

    def test_a_run_that_lost_nobody_says_nothing_about_attrition(self):
        from ai.autoresearch import _provenance_summary
        prov = {**self.PROV, "samples": {"S1": {"raw": 50, "filter": 40,
                                                "denoise": 30, "final": 30}}}
        assert "n_dropped" not in _provenance_summary(prov, n_analysed=1)

    def test_missing_provenance_degrades_rather_than_raising(self):
        from ai.autoresearch import _provenance_summary
        s = _provenance_summary({}, n_analysed=63)
        assert s["n_samples_attempted"] == 0 and s["n_samples_analysed"] == 63

    def test_the_briefing_states_the_attrition_beside_the_axis_rule(self):
        text = format_briefing({
            "counts": {"shape": [63, 735], "sample_ids_sample": ["a"],
                       "asv_ids_sample": ["b"]},
            "provenance": {"n_samples_attempted": 84, "n_samples_analysed": 63,
                           "n_dropped": 21, "dropped_at_stage": {"chimera": 11}}})
        assert "SAMPLE ATTRITION: 63 of 84" in text
        assert "11 at chimera" in text
        assert "ABSENT" in text
        assert text.index("ROWS ARE SAMPLES") < text.index("SAMPLE ATTRITION")
        # the fact, not a directive about what to conclude from it
        assert "Say so" not in text and "finding in its own right" not in text


class TestRawAndRelative:
    """Both forms of the table are built, rather than one being left to be re-derived.
    The arithmetic is one line, but it is a line along the axis that has twice been got
    backwards, and a proportion divided by the wrong margin looks entirely plausible."""

    def test_the_sandbox_exposes_both_tables(self):
        import inspect
        import ai.autoresearch as A
        src = inspect.getsource(A)
        assert "props = counts.div(" in src
        assert "counts=counts, props=props" in src   # the namespace is an allowlist

    def test_the_briefing_says_which_is_which(self):
        text = format_briefing({"counts": {
            "shape": [63, 735], "sample_ids_sample": ["a"], "asv_ids_sample": ["b"],
            "has_props": True}})
        assert "RAW READ COUNTS" in text
        assert "rows sum to 1" in text

    def test_the_frames_are_described_as_bound_not_importable(self):
        """Wording matters: "nothing else is importable" was read as a list of things
        to import, and analyses went looking for counts.csv on disk."""
        text = format_briefing({"available": ["counts (DataFrame 63x735)", "np"]})
        assert "ALREADY LOADED" in text
        assert "do not read files" in text
        assert "importable" not in text

    def test_the_invented_helpers_declare_their_signatures(self):
        """fdr, clr and rarefy exist nowhere else, so their return shapes cannot be
        inferred and were being guessed — fdr() was compared against a threshold as
        though it returned the adjusted p-values."""
        text = format_briefing({"available": ["fdr", "clr", "rarefy", "np"]})
        assert "fdr(pvals) ->" in text and "'p_adj'" in text
        assert "clr(df, pseudocount=0.5) ->" in text
        assert "rarefy(df, depth=None, seed=0) ->" in text

    def test_a_worked_example_is_shown_not_just_prohibited(self):
        """"Do not read files" failed in four separate contexts. A claim-sized context
        cannot inherit the correction its predecessor got, so the cost is paid once per
        context — one line of working code is the cheapest thing that might land."""
        text = format_briefing({"available": ["counts", "meta"]})
        assert "a complete analysis looks like" in text
        assert "result = {" in text and "counts.loc[" in text

    def test_the_worked_example_actually_runs(self):
        """An example that does not execute is worse than none."""
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        ok, r = asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(
            "sub = counts.loc[meta.index[meta['pipeline_in_counts']]]\n"
            "result = {'n_samples': int(sub.shape[0]), "
            "'mean_richness': float((sub > 0).sum(axis=1).mean())}"))
        assert ok and r["n_samples"] == 63

    def test_columns_blank_for_dropped_samples_are_named(self):
        """Carrying 84 rows into a frame whose columns mostly come from the 63-row
        survivors table leaves 13 columns empty for exactly the dropped samples —
        including the one naming the submission, which is what an analyst comparing
        dropped against retained reaches for first."""
        text = format_briefing({
            "provenance": {"n_samples_attempted": 84, "n_samples_analysed": 63,
                           "n_dropped": 21, "dropped_at_stage": {"chimera": 21}},
            "blank_for_dropped": ["sra_submission_accession", "pipeline_total_reads"]})
        assert "no value for any dropped sample" in text
        assert "sra_submission_accession" in text

    def test_a_run_that_lost_nobody_names_no_blank_columns(self):
        text = format_briefing({"provenance": {"n_samples_attempted": 63,
                                               "n_samples_analysed": 63}})
        assert "no value for any dropped" not in text

    def test_the_grantable_packages_are_named(self):
        """The allowlist was invisible from inside the sandbox, so hand-rolling looked
        like the only option — an analyst wrote PERMANOVA by hand twice, wrongly, with
        skbio one tool call away."""
        text = format_briefing({"grantable": ["skbio", "statsmodels"]})
        assert "request_package will install" in text
        assert "skbio" in text and "Nothing else is available" in text

    def test_the_grantable_line_defers_to_what_is_already_bound(self):
        """An analyst installed scikit-bio, spent three turns guessing at its API,
        re-requested it, and never called the `permanova` already in its namespace."""
        text = format_briefing({"grantable": ["skbio"], "available": ["permanova"]})
        assert "already cover" in text and "ready to call" in text

    def test_nothing_is_promised_when_nothing_can_be_granted(self):
        """A sandbox with no installer must not advertise one."""
        assert "request_package will install" not in format_briefing(
            {"available": ["counts", "np"]})

    def test_a_dataset_without_counts_says_nothing_about_props(self):
        assert "props" not in format_briefing({"tax": {"shape": [7, 6], "columns": ["a"]}})


class TestEverySubmittedSample:
    """Every sample submitted through the portal reaches the analysis, whether or not
    it produced reads. samples.json lists survivors, so "is the attrition biased by
    treatment?" was unanswerable from inside — the dropped runs were bare ids."""

    def test_the_sandbox_builds_meta_over_every_submitted_run(self):
        import inspect
        import ai.autoresearch as A
        src = inspect.getsource(A)
        assert "_every = sorted(set(meta.index) | set(_chain) | set(_runmap))" in src
        assert '"pipeline_in_counts"' in src

    def test_the_mislabelled_centre_column_is_renamed_not_dropped(self):
        """samples.json's `center_name` holds a SUB submission accession, copied
        faithfully from ENA. Renaming it frees the name for the real centre."""
        import inspect
        import ai.autoresearch as A
        src = inspect.getsource(A)
        assert '"sra_center_name": "sra_submission_accession"' in src
        assert '("sra_center_name", "center_name")' in src


class TestSandboxHints:
    """A briefing line read ten minutes upstream loses to a habit. These arrive
    attached to the traceback that proves the habit wrong."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        return asyncio.run(ar._exec_tool("run_analysis", {"code": code, "label": "t"}))

    def test_reading_a_file_is_answered_with_the_frames_in_scope(self):
        r = self._run("import pandas as pd; df = pd.read_csv('counts.csv')")
        assert r["ok"] is False
        assert "already bound" in r["hint"] and "counts" in r["hint"]

    def test_an_unknown_name_is_answered_with_what_exists(self):
        r = self._run("result = skbio.stats()")
        assert "listed in the briefing" in r["hint"]

    def test_forgetting_result_is_answered_plainly(self):
        r = self._run("x = counts.sum()")
        assert r["hint"] == "Assign what you want returned to `result`."

    def test_importing_a_sandbox_helper_is_answered(self):
        """fdr, clr and rarefy are ours — not in scipy, so the import fails by
        construction and the traceback blames scipy."""
        r = self._run("from scipy.stats import fdr\nresult = 1")
        assert "exist in no library at all" in r["hint"]

    def test_importing_a_bound_library_function_is_answered_correctly(self):
        """braycurtis IS real scipy, just not in scipy.stats, and it is already bound.
        The first version of this hint called it a sandbox helper, which was false."""
        r = self._run("from scipy.stats import braycurtis\nresult = 1")
        assert "already bound" in r["hint"] and "braycurtis" in r["hint"]

    def test_indexing_a_condensed_distance_array_is_answered(self):
        r = self._run("D = pdist(counts.values); result = {'d': float(D[0, 1])}")
        assert "squareform" in r["hint"]

    def test_a_silently_killed_analysis_says_what_happened(self):
        """A child killed on a resource limit prints no traceback. The analyst saw the
        bare word "error" — the one failure whose cause it cannot see from inside."""
        r = self._run("import os; os._exit(9)")
        assert r["ok"] is False
        assert "produced no output" in r["error"] and "memory or CPU" in r["error"]

    def test_a_mask_on_the_wrong_axis_is_answered(self):
        """A 63-long per-sample mask against the 735-ASV axis. The briefing's axis rule
        covers this in principle; the correction at the point of failure is what has
        actually been landing."""
        r = self._run("arr = counts.values.T\n"
                      "m = (counts.sum(axis=1) > 0).values\n"
                      "result = {'x': int(arr[m].shape[0])}")
        assert r["ok"] is False
        assert "per-sample mask selects rows" in r["hint"]

    def test_the_same_error_twice_escalates(self):
        """An analyst repeated one identical error four times without self-correcting,
        and nothing in the loop knew it was a loop. A different error each time is
        debugging; the same one repeatedly is being stuck."""
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        bad = ("arr = counts.values.T\nm = (counts.sum(axis=1) > 0).values\n"
               "result = {'x': int(arr[m].shape[0])}")
        first = asyncio.run(ar._exec_tool("run_analysis", {"label": "t", "code": bad}))
        second = asyncio.run(ar._exec_tool("run_analysis", {"label": "t", "code": bad}))
        assert "same error" not in first["hint"]
        assert "same error 2 times" in second["hint"]
        assert "change it rather than adjusting it" in second["hint"]
        # a DIFFERENT error is not treated as a loop
        third = asyncio.run(ar._exec_tool("run_analysis", {"label": "t",
                                                           "code": "result = 1/0"}))
        assert "same error" not in (third.get("hint") or "")

    def test_both_boolean_index_phrasings_are_caught(self):
        """numpy says "boolean index did not match"; pandas says "Boolean index has
        wrong length". Matching one of them answered half the occurrences of the same
        mistake."""
        numpy_case = self._run("arr = counts.values.T\n"
                               "m = (counts.sum(axis=1) > 0).values\n"
                               "result = {'x': int(arr[m].shape[0])}")
        pandas_case = self._run(
            "m = tax['Domain'].astype(str).str.contains('Bacteria')\n"
            "sub = tax[m]\nresult = {'x': int(counts.loc[:, sub.index[m]].shape[1])}")
        for r in (numpy_case, pandas_case):
            assert "per-sample mask selects rows" in r["hint"]

    def test_treating_an_array_as_pandas_is_answered(self):
        r = self._run("a = counts.values\nresult = {'m': float(a.median())}")
        assert "not a pandas object" in r["hint"] and ".loc" in r["hint"]

    def test_near_identical_errors_escalate_as_one_mistake(self):
        """`.median` and `.values` on an ndarray are the same mistake. A 60-character
        signature filed them as unrelated and neither escalated."""
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        asyncio.run(ar._exec_tool("run_analysis", {"label": "t", "code":
            "a = counts.values\nresult = {'m': float(a.median())}"}))
        second = asyncio.run(ar._exec_tool("run_analysis", {"label": "t", "code":
            "a = counts.values\nresult = {'m': float(a.values.mean())}"}))
        assert "same error" in second["hint"]

    def test_a_missing_grantable_module_points_at_request_package(self):
        """ModuleNotFoundError contains neither "ImportError" nor "cannot import name",
        so the one failure that should point straight at request_package fired no hint
        at all — an analyst reached for skbio, the package the tool was built for, and
        was told nothing."""
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        from ai.sandbox_packages import ALLOWED
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        # NOT a real allowlist entry: the bench installs into this same interpreter,
        # so a test that assumes skbio is absent passes until a run grants it and then
        # fails for a reason that has nothing to do with the code under test.
        ar.grantable_packages = ("definitely_not_installed_pkg",) + tuple(sorted(ALLOWED))
        r = asyncio.run(ar._exec_tool("run_analysis", {
            "label": "t", "code": "import definitely_not_installed_pkg\nresult = 1"}))
        assert "request_package" in r["hint"]
        assert 'package="definitely_not_installed_pkg"' in r["hint"]

    def test_a_missing_ungrantable_module_says_so_and_lists_what_is(self):
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        from ai.sandbox_packages import ALLOWED
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        ar.grantable_packages = tuple(sorted(ALLOWED))
        r = asyncio.run(ar._exec_tool("run_analysis",
                                      {"label": "t", "code": "import torch\nresult = 1"}))
        assert "cannot be installed" in r["hint"] and "skbio" in r["hint"]

    def test_hint_selection_against_literal_tracebacks(self):
        """Hints are chosen by matching the error TEXT, so they must be tested against
        the exact strings seen in real runs. Several were written against tracebacks I
        imagined and matched only some of the real phrasings — numpy's "boolean index
        did not match" but not pandas' "Boolean index has wrong length", ImportError but
        not ModuleNotFoundError."""
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient)

        class _Raises:
            def __init__(self, msg): self.msg = msg
            async def run(self, code, timeout=30): return False, self.msg

        cases = {
            "IndexingError: Unalignable boolean Series provided as indexer (index of "
            "the boolean Series and of the indexed object do not match).": "align first",
            "IndexError: boolean index did not match indexed array along axis 0":
                "per-sample mask selects rows",
            "IndexError: Boolean index has wrong length: 418 instead of 735":
                "per-sample mask selects rows",
            "ModuleNotFoundError: No module named 'skbio'": "request_package",
            "AttributeError: 'numpy.ndarray' object has no attribute 'median'":
                "not a pandas object",
            "FileNotFoundError: [Errno 2] No such file or directory: 'counts.csv'":
                "already bound",
        }
        for err, expected in cases.items():
            ar = Autoresearcher(DirDataSource("/data/dev/testdata/1543a4c1", study={},
                                              overview=None),
                                LLMClient(None, "x"), _Raises(err))
            ar.grantable_packages = ("skbio",)
            r = asyncio.run(ar._exec_tool("run_analysis", {"label": "t", "code": "x"}))
            assert expected in (r.get("hint") or ""), f"no hint for: {err[:50]}"

    def test_an_ordinary_error_gets_no_invented_hint(self):
        """A hint that fires on everything teaches nothing."""
        r = self._run("result = 1 / 0")
        assert r["ok"] is False and "hint" not in r


class TestResultEncoding:
    """A correct analysis was lost at the last step because its result contained
    tax.columns.values — the encoder handled numpy and pandas containers but not the
    extension arrays pandas actually returns."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    def test_a_pandas_string_array_survives(self):
        ok, r = self._run("result = {'cols': tax.columns.values}")
        assert ok and r["cols"][0] == "Domain"

    def test_an_index_survives(self):
        ok, r = self._run("result = {'idx': counts.index[:2]}")
        assert ok and len(r["idx"]) == 2

    def test_a_categorical_survives(self):
        ok, r = self._run("result = {'c': pd.Categorical(['a','b'])}")
        assert ok and r["c"] == ["a", "b"]

    def test_something_genuinely_unserialisable_degrades_to_text(self):
        """Better a string than losing the whole analysis to a TypeError."""
        ok, r = self._run("result = {'s': {1, 2}}")
        assert ok and isinstance(r["s"], str)

    def test_ordinary_values_are_untouched(self):
        ok, r = self._run("result = {'n': 5, 'x': 1.23456, 'ok': True, 'none': None}")
        assert ok and r == {"n": 5, "x": 1.2346, "ok": True, "none": None}


class TestPermanovaHelper:
    """Five independent analysts hand-rolled PERMANOVA and returned F = 0.0048, 0.39,
    22.89 and 92.01 for a dataset whose answer is 4.19 — four orders of magnitude, none
    right, while fdr/clr/rarefy were used correctly every time. The difference was
    availability, not care."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    SETUP = ("g = meta.loc[counts.index, 'biosample_env_local_scale']\n"
             "P = counts.div(counts.sum(axis=1), axis=0)\n")

    def test_it_gets_the_answer_the_analysts_could_not(self):
        ok, r = self._run(self.SETUP + "result = permanova(P, g, permutations=199)")
        assert ok
        assert abs(r["F"] - 4.19) < 0.01
        assert abs(r["R2"] - 0.224) < 0.005
        assert r["n"] == 63 and r["groups"] == 5

    def test_a_precomputed_distance_matrix_is_accepted_as_is(self):
        ok, r = self._run(self.SETUP + "D = squareform(pdist(P.values, metric='braycurtis'))\n"
                          "result = permanova(D, g, permutations=199)")
        assert ok and abs(r["F"] - 4.19) < 0.01

    def test_random_labels_sit_at_the_null(self):
        """A test that cannot tell signal from noise is worse than none."""
        ok, r = self._run(self.SETUP + "import numpy as np\n"
                          "rng = np.random.default_rng(0)\n"
                          "result = permanova(P, rng.permutation(g.values), permutations=199)")
        assert ok and r["F"] < 2.0 and r["p"] > 0.05

    def test_misaligned_labels_are_refused_not_silently_wrong(self):
        ok, r = self._run("result = permanova(counts, ['a', 'b'], permutations=9)")
        assert not ok and "line up row for row" in str(r)

    def test_one_group_is_refused(self):
        ok, r = self._run(self.SETUP + "result = permanova(P, ['x'] * 63, permutations=9)")
        assert not ok and "at least 2 groups" in str(r)

    def test_the_signature_is_declared_in_the_briefing(self):
        text = format_briefing({"available": ["permanova"]})
        assert "permanova(counts_or_distances, groups" in text


class TestAlphaDiversityHelper:
    """Two claims in one ledger reported frost-flower Shannon as 4.59 and 3.18. Both
    were right — bits and nats — and neither said which, so a clean-room analyst
    computing the other refutes a correct claim over a logarithm base."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    def test_the_unit_is_in_the_column_name(self):
        ok, r = self._run("result = list(alpha_diversity(counts).columns)")
        assert ok and "shannon_nats" in r and "shannon_bits" in r
        assert "shannon" not in r          # no ambiguous bare name to reach for

    def test_both_units_are_the_same_quantity(self):
        import math
        ok, r = self._run("a = alpha_diversity(counts)\n"
                          "result = {'n': float(a['shannon_nats'].mean()), "
                          "'b': float(a['shannon_bits'].mean())}")
        # the sandbox encoder rounds to 4 decimals, so the exact ratio cannot survive
        assert ok and abs(r["b"] - r["n"] / math.log(2)) < 1e-3

    def test_it_agrees_with_the_values_verified_by_hand(self):
        ok, r = self._run(
            "a = alpha_diversity(counts)\n"
            "g = meta.loc[counts.index, 'biosample_env_local_scale'].astype(str)\n"
            "result = {'nats': round(float(a.loc[g=='frost flowers','shannon_nats'].mean()),2),"
            " 'rich': round(float(a.loc[g=='frost flowers','richness'].mean()),1)}")
        assert ok and r["nats"] == 3.18 and r["rich"] == 92.0

    def test_evenness_stays_within_its_bounds(self):
        ok, r = self._run("a = alpha_diversity(counts)\n"
                          "result = [float(a['evenness'].min()), float(a['evenness'].max())]")
        assert ok and 0.0 <= r[0] and r[1] <= 1.0

    def test_a_zero_read_sample_does_not_divide_by_zero(self):
        ok, r = self._run("z = counts.copy(); z.iloc[0] = 0\n"
                          "a = alpha_diversity(z)\n"
                          "result = {'shannon': float(a['shannon_nats'].iloc[0]), "
                          "'rich': int(a['richness'].iloc[0])}")
        assert ok and r["shannon"] == 0.0 and r["rich"] == 0


class TestNumericCoercion:
    """`sra_read_count` arrives from samples.json as text, so
    meta['sra_read_count'].median() raised "Cannot perform reduction with string dtype"
    — a plainly numeric column refusing arithmetic."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    def test_a_numeric_column_that_arrived_as_text_is_usable(self):
        ok, r = self._run("result = {'dtype': str(meta['sra_read_count'].dtype), "
                          "'median': float(meta['sra_read_count'].median())}")
        assert ok and r["dtype"].startswith("float") and r["median"] > 0

    def test_label_columns_are_not_turned_into_nans(self):
        """Coercing too eagerly would destroy the grouping variables entirely."""
        ok, r = self._run(
            "result = {'env': int(meta['biosample_env_local_scale'].notna().sum()), "
            "'lib': int(meta['sra_library_name'].notna().sum()), "
            "'env_dtype': str(meta['biosample_env_local_scale'].dtype)}")
        assert ok and r["env"] == 84 and r["lib"] == 84
        assert not r["env_dtype"].startswith("float")

    def test_the_stage_columns_are_integers(self):
        ok, r = self._run("result = {c: str(meta[c].dtype) for c in meta.columns "
                          "if c.startswith('pipeline_reads_')}")
        assert ok and all(v.startswith("int") or v.startswith("float")
                          for v in r.values())


class TestTrailingExpression:
    """Forgetting `result =` was the most common single failure in a run — four times —
    despite the briefing saying it and the worked example showing it. Accepting the
    obvious intent is cheaper than explaining the contract again."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    def test_a_trailing_expression_becomes_the_result(self):
        ok, r = self._run("n = int(counts.shape[0])\n{'n': n}")
        assert ok and r == {"n": 63}

    def test_an_explicit_result_still_wins(self):
        ok, r = self._run("result = {'a': 1}\n{'b': 2}")
        assert ok and r == {"a": 1}

    def test_code_that_produces_nothing_is_still_an_error(self):
        """Silence must not be dressed up as an answer."""
        ok, r = self._run("x = 1")
        assert not ok and "did not set a `result`" in str(r)

    def test_a_real_error_is_unaffected(self):
        ok, r = self._run("result = 1/0")
        assert not ok and "ZeroDivisionError" in str(r)

    def test_a_syntax_error_still_reports_as_one(self):
        ok, r = self._run("result = (1 +")
        assert not ok and "SyntaxError" in str(r)


class TestByRank:
    """One analyst produced a single-row frame three times running trying to aggregate
    counts to a taxonomic rank, and the investigation was abandoned with zero claims.
    It is a counts-by-tax join with an axis choice in the middle: wrong shape, no error."""

    def _run(self, code):
        import asyncio
        from ai.autoresearch import SubprocessExecutor
        return asyncio.run(SubprocessExecutor("/data/dev/testdata/1543a4c1").run(code))

    def test_samples_stay_as_rows(self):
        ok, r = self._run("t = by_rank(counts, tax, 'Genus')\n"
                          "result = {'shape': list(t.shape), "
                          "'index_matches': bool((t.index == counts.index).all())}")
        assert ok and r["shape"][0] == 63 and r["index_matches"]

    def test_each_rank_collapses_further(self):
        ok, r = self._run("result = {k: int(by_rank(counts, tax, k).shape[1]) "
                          "for k in ['Domain','Phylum','Class','Order','Family','Genus']}")
        assert ok
        widths = [r[k] for k in ("Domain", "Phylum", "Class", "Order", "Family", "Genus")]
        assert widths == sorted(widths) and widths[0] < widths[-1]

    def test_reads_are_conserved_or_dropped_not_invented(self):
        ok, r = self._run("t = by_rank(counts, tax, 'Domain')\n"
                          "result = {'agg': float(t.values.sum()), "
                          "'raw': float(counts.values.sum())}")
        assert ok and r["agg"] <= r["raw"]

    def test_an_unknown_rank_says_what_is_available(self):
        ok, r = self._run("result = by_rank(counts, tax, 'Kingdom')")
        assert not ok and "not a rank" in str(r) and "Genus" in str(r)

    def test_it_feeds_permanova_directly(self):
        ok, r = self._run("g = meta.loc[counts.index, 'biosample_env_local_scale']\n"
                          "result = permanova(by_rank(counts, tax, 'Genus'), g, "
                          "permutations=99)")
        assert ok and r["n"] == 63 and r["groups"] == 5


class TestSourcePrefixes:
    """Three sources reach `meta` and they are not interchangeable. Nothing in a value
    says which one produced it, so the column name has to."""

    def test_the_sandbox_prefixes_every_column_by_source(self):
        """pipeline_total_reads matches the counts table; sra_read_count is a declared
        figure that is zero for 19 of these samples. Reading one for the other is a
        silent error, and nothing in the value gives it away."""
        import inspect
        import ai.autoresearch as A
        src = inspect.getsource(A)
        assert '("pipeline_" if c in _PIPE else "sra_")' in src
        assert '"biosample_" + str(c)' in src

    def test_the_prompt_points_at_the_prefixed_columns(self):
        from ai.autoresearch import EXPLORE_SYSTEM
        assert "meta['pipeline_x']" in EXPLORE_SYSTEM
        assert "biosample_lat_lon" in EXPLORE_SYSTEM
        assert "meta['x']" not in EXPLORE_SYSTEM      # the old, now-wrong name

    def test_the_prompt_does_not_prime_the_agent_to_expect_bad_metadata(self):
        """Submitted metadata is mostly sound if incomplete. Telling an analyst it is
        "frequently wrong" is inaccurate and invites a hunt for mislabels, which is how
        false positives get recorded as findings."""
        from ai.autoresearch import EXPLORE_SYSTEM
        for phrase in ("frequently wrong", "commonly mislabel", "often a database"):
            assert phrase not in EXPLORE_SYSTEM
        assert "usually sound" in EXPLORE_SYSTEM
        assert "neither settles the other" in EXPLORE_SYSTEM


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
