"""Parser tests for the live hyphal colony view (#58).

The viewer's only input is the run log, so what has to hold is that it reads growth
honestly: a tip left behind mid-run must not be shown as finished, and claims must
attribute to the tip that recorded them.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "bench"))

from hyphal_viz import parse  # noqa: E402


MID_RUN = """\
submission 1543a4c1: 16S amplicon frost flower (PRJNA1473294), n=63
model: qwen/qwen3.6-35b-a3b @ http://localhost:1234/v1
=== EXPLORE (hyphal — branching short-lived tips (#58)) ===
  germinating agenda…
  ▸ a1: What is the domain composition?  (0 claims in hand)
      · c1 domain read totals
      · failed transposed frame
      + k1 Eukaryota dominate at 74% of reads
      + k2 zero ASVs classified as Archaea
      ↳ a9: Are the eukaryotic reads 18S contamination?
    ✓ a1 done (2 claims)
  ▸ a9 ⤶ a1: Are the eukaryotic reads 18S contamination?  (2 claims in hand)
      + k3 all eukaryote ASVs carry an 18S signature
"""

FULL_RUN = MID_RUN + """\
    ✓ a9 done (1 claims)
  ▸ a2: How does alpha diversity vary?  (3 claims in hand)
      + k4 richness tracks depth at rho=0.63
  sweeping 4 claims for assumptions…
  3 tips, 4 claims, 41 steps

=== VERIFY (judged by glm-4.7-flash) ===
    [replicated ] k1   r2:agree                Eukaryota dominate at 74% of reads
    [overturned ] k4   r2:differ r3:differ     richness tracks depth at rho=0.63

  agenda (2/3 investigations done — INCOMPLETE, 1 outstanding):
    • [done] a1: What is the domain composition?
    • [interrupted] a2: How does alpha diversity vary?
      └─ [done] a9: Are the eukaryotic reads 18S contamination?
"""


PROPOSED = """\
=== EXPLORE (hyphal — branching short-lived tips (#58)) ===
  germinating agenda…
  agenda for round 1 — 3 investigations:
    · a1: Is attrition biased by environment?
    · a2: Does beta-diversity differ across environments?
    · a3: Which taxa drive any separation?
  ▸ a1: Is attrition biased by environment?  (0 claims in hand)
      · c1 attrition table
"""


class TestVerifyProgress:
    """Judging stalled at 2 claims of 19 and the log looked identical to a run where it
    was keeping up, because nothing rendered the events the judge was already emitting."""

    def _cap(self, event, detail):
        import asyncio, io, contextlib, importlib
        R = importlib.import_module("results_explorer")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(R._print_progress(event, detail))
        return buf.getvalue()

    def test_a_verdict_is_printed(self):
        out = self._cap("verify", {"claim": "k7", "verdict": "verified"})
        assert "k7" in out and "verified" in out

    def test_a_refutation_is_visually_distinct_from_a_pass(self):
        ok = self._cap("verify", {"claim": "k1", "verdict": "verified"})
        no = self._cap("verify", {"claim": "k2", "verdict": "refuted"})
        assert ok.strip()[0] != no.strip()[0]

    def test_a_judge_error_is_printed_not_swallowed(self):
        out = self._cap("verify_error", {"claim": "k9", "error": "connection reset"})
        assert "k9" in out and "connection reset" in out


class TestProposedAgenda:
    """A tip only appeared in the log when it STARTED, so a run showed one item for
    its first ten minutes and revealed the rest one at a time over hours."""

    def setup_method(self):
        self.st = parse(PROPOSED)
        self.tips = {t["id"]: t for t in self.st["tips"]}

    def test_the_whole_agenda_is_visible_before_it_is_worked(self):
        assert set(self.tips) == {"a1", "a2", "a3"}

    def test_items_not_yet_grown_are_pending(self):
        assert self.tips["a2"]["status"] == "pending"
        assert self.tips["a3"]["status"] == "pending"

    def test_the_one_being_worked_is_still_growing(self):
        assert self.tips["a1"]["status"] == "growing"

    def test_an_analysis_line_is_not_read_as_an_agenda_item(self):
        """Both start with the same bullet; only one carries an aNN id and a colon."""
        assert "c1" not in self.tips
        assert self.tips["a1"]["analyses"] == 1


class TestMidRun:
    def setup_method(self):
        self.st = parse(MID_RUN)
        self.tips = {t["id"]: t for t in self.st["tips"]}

    def test_the_run_header_is_read(self):
        assert self.st["submission"] == "1543a4c1"
        assert self.st["model"] == "qwen/qwen3.6-35b-a3b"
        assert "hyphal" in self.st["phase"]

    def test_the_growing_tip_is_the_one_still_open(self):
        assert self.tips["a9"]["status"] == "growing"
        assert self.st["active"] == "a9"

    def test_a_closed_tip_reports_the_status_it_emitted(self):
        assert self.tips["a1"]["status"] == "done"

    def test_claims_attribute_to_the_tip_that_recorded_them(self):
        assert self.tips["a1"]["claims"] == ["k1", "k2"]
        assert self.tips["a9"]["claims"] == ["k3"]

    def test_a_followup_branches_off_the_tip_that_raised_it(self):
        assert self.tips["a9"]["parent"] == "a1"
        assert self.tips["a1"]["parent"] is None

    def test_a_followup_not_yet_grown_is_pending_not_invented(self):
        """It appeared as `↳` and was then grown, so it is growing here. A follow-up
        that had NOT been picked up must still show, and must show as pending."""
        st = parse(MID_RUN.split("  ▸ a9 ⤶ a1")[0])
        tips = {t["id"]: t for t in st["tips"]}
        assert tips["a9"]["status"] == "pending"
        assert tips["a9"]["parent"] == "a1"

    def test_analyses_are_counted_so_silence_can_be_read(self):
        """A tip can spend minutes inside run_analysis recording nothing; without
        these lines a working run and a hung one look identical."""
        assert self.tips["a1"]["analyses"] == 2
        assert self.tips["a9"]["analyses"] == 0
        assert [e["id"] for e in self.st["events"] if e["kind"] == "analysis"] == \
            ["c1", "failed"]

    def test_an_analysis_line_is_not_mistaken_for_a_claim(self):
        assert [c["id"] for c in self.st["claims"]] == ["k1", "k2", "k3"]

    def test_seeding_context_is_kept(self):
        """How much the colony had found when a tip was seeded is the whole question
        the hyphal experiment asks."""
        assert self.tips["a1"]["seeded_with"] == 0
        assert self.tips["a9"]["seeded_with"] == 2

    def test_an_abandoned_tip_is_not_called_done(self):
        """Without a `✓` line, a tip that was moved on from reads as `grown` — never
        as done, which would fake a completed investigation."""
        st = parse(MID_RUN.replace("    ✓ a1 done (2 claims)\n", ""))
        tips = {t["id"]: t for t in st["tips"]}
        assert tips["a1"]["status"] == "grown"
        assert tips["a1"]["status"] != "done"


class TestFullRun:
    def setup_method(self):
        self.st = parse(FULL_RUN)
        self.tips = {t["id"]: t for t in self.st["tips"]}

    def test_the_final_agenda_settles_done_versus_interrupted(self):
        assert self.tips["a2"]["status"] == "interrupted"
        assert self.tips["a1"]["status"] == "done"

    def test_nothing_is_left_growing_after_the_run_ends(self):
        assert not [t for t in self.st["tips"] if t["status"] == "growing"]
        assert self.st["active"] is None

    def test_verdicts_land_on_their_claims(self):
        by_id = {c["id"]: c for c in self.st["claims"]}
        assert by_id["k1"]["verdict"] == "replicated"
        assert by_id["k4"]["verdict"] == "overturned"
        assert by_id["k2"]["verdict"] is None      # never reached verification

    def test_totals_are_read(self):
        assert self.st["totals"] == {"tips": 3, "claims": 4, "steps": 41}
        assert self.st["sweep"] == 4

    def test_the_full_question_replaces_the_truncated_one(self):
        """Tip lines truncate the question at 70 chars; the agenda dump has it whole."""
        assert self.tips["a2"]["question"] == "How does alpha diversity vary?"


class TestLedgerEnrichment:
    """The log is a progress feed; the ledger is the record. Once a run writes one,
    the viewer must show the real text rather than whatever the printer summarised."""

    LEDGER = {
        "claims": [
            {"id": "k1", "statement": "Eukaryota dominate at 74% of reads, and the "
                                      "dominance is strongest in the ten deepest samples",
             "value": "mean_prop=0.7407", "verdict": "replicated", "investigation": "a1"},
            {"id": "k4", "statement": "richness tracks depth at rho=0.63",
             "value": "rho=0.63", "verdict": "overturned", "investigation": "a2"}],
        "agenda": [
            {"id": "a1", "question": "What is the domain composition of this dataset, "
                                     "and does the taxonomy assignment look sound?",
             "status": "done", "parent": None},
            {"id": "a2", "question": "How does alpha diversity vary?",
             "status": "interrupted", "parent": None},
            {"id": "a7", "question": "Never reached", "status": "pending", "parent": "a1"}],
        "assumptions": [{"id": "as1", "statement": "counts are raw reads"}],
        "run": {"exploration": "hyphal", "completed": False}}

    def setup_method(self):
        from hyphal_viz import enrich
        self.st = enrich(parse(MID_RUN), self.LEDGER)
        self.tips = {t["id"]: t for t in self.st["tips"]}

    def test_the_full_statement_replaces_the_logs_summary(self):
        k1 = next(c for c in self.st["claims"] if c["id"] == "k1")
        assert k1["statement"].endswith("strongest in the ten deepest samples")
        assert k1["value"] == "mean_prop=0.7407"

    def test_the_full_question_replaces_the_truncated_one(self):
        assert self.tips["a1"]["question"].endswith("look sound?")

    def test_verdicts_and_real_statuses_arrive(self):
        assert self.tips["a2"]["status"] == "interrupted"
        assert next(c for c in self.st["claims"]
                    if c["id"] == "k4")["verdict"] == "overturned"

    def test_an_investigation_never_grown_still_joins_the_colony(self):
        assert self.tips["a7"]["status"] == "pending"
        assert self.tips["a7"]["parent"] == "a1"

    def test_a_claim_the_ledger_never_mentions_is_kept_not_dropped(self):
        """Enrichment may only add detail. Silently losing k2/k3 — which this log
        saw and the ledger fixture does not list — would make the viewer disagree
        with the run it is reporting on."""
        assert {"k1", "k2", "k3"} <= {c["id"] for c in self.st["claims"]}
        assert "k2" in self.tips["a1"]["claims"]

    def test_a_claim_only_the_ledger_saw_still_appears(self):
        """The printer is selective; the ledger is not. A claim recorded during a
        phase the log said nothing about is still a claim."""
        assert self.tips["a2"]["claims"] == ["k4"]

    def test_claims_reattribute_to_the_investigation_the_ledger_names(self):
        assert self.tips["a1"]["claims"][0] == "k1"

    def test_assumptions_and_run_block_come_along(self):
        assert self.st["assumptions"][0]["id"] == "as1"
        assert self.st["run"]["exploration"] == "hyphal"
        assert self.st["from_ledger"] is True


class TestRecordPath:
    """A run publishes `run_state.json` while it works and `claims_ledger.json` when
    it finishes. Pointing the viewer at the output dir has to cover both, because the
    interesting moment is usually before the run is over."""

    def _dir(self, tmp_path, *names):
        for n in names:
            (tmp_path / n).write_text("{}")
        return tmp_path

    def test_the_live_state_is_used_while_a_run_is_going(self, tmp_path):
        from hyphal_viz import _record_path
        d = self._dir(tmp_path, "run_state.json")
        assert _record_path(d).name == "run_state.json"

    def test_the_finished_ledger_wins_once_it_exists(self, tmp_path):
        from hyphal_viz import _record_path
        d = self._dir(tmp_path, "run_state.json", "claims_ledger.json")
        assert _record_path(d).name == "claims_ledger.json"

    def test_an_empty_output_dir_is_not_an_error(self, tmp_path):
        from hyphal_viz import _record_path
        assert _record_path(tmp_path) is None

    def test_an_explicit_file_is_honoured(self, tmp_path):
        from hyphal_viz import _record_path
        p = tmp_path / "somewhere_else.json"
        p.write_text("{}")
        assert _record_path(p) == p

    def test_a_path_that_does_not_exist_yet_is_not_an_error(self, tmp_path):
        """The viewer is normally started BEFORE the run writes anything."""
        from hyphal_viz import _record_path
        assert _record_path(tmp_path / "claims_ledger.json") is None
        assert _record_path(None) is None


def test_an_empty_log_yields_an_empty_colony():
    st = parse("")
    assert st["tips"] == [] and st["claims"] == [] and st["order"] == []


def test_a_linear_run_log_parses_without_tips():
    """The viewer is pointed at whichever log is to hand; a linear run has no tips
    and must produce an empty colony rather than an exception."""
    st = parse("=== EXPLORE (agenda-driven, one long-lived session) ===\n"
               "  11 claims, 18 computations in 1945s\n")
    assert st["tips"] == []
    assert "EXPLORE" in st["phase"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
