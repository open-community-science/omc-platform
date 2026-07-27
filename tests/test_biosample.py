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
    (high vs low sequencing depth) because nothing pointed at the real one."""

    def test_groupings_are_named_with_their_sizes(self):
        text = format_briefing({
            "meta": {"shape": [63, 23], "columns": ["x", "env_local_scale"]},
            "meta_groupings": {"env_local_scale": {
                "n_groups": 5, "groups": {"frost flowers": 24, "Brine": 17}}}})
        assert "COLUMNS THAT GROUP THE SAMPLES" in text
        assert "meta['env_local_scale']: 5 groups" in text
        assert "frost flowers (n=24)" in text
        assert "..." in text          # 5 groups, 2 shown — say so rather than imply all

    def test_a_dataset_with_no_groupings_says_nothing(self):
        text = format_briefing({"meta": {"shape": [63, 4], "columns": ["x"]}})
        assert "COLUMNS THAT GROUP" not in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
