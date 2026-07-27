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
