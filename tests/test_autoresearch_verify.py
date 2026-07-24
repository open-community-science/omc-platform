"""Verification-layer tests for claim-grounded autoresearch (issue #48).

These pin the behaviour the real-sample run exposed: a claim must not be graded on
what the VERIFIER could see (a truncated evidence prefix, a dropped list tail), a
sign or an exponent is part of a quantity, and a mostly-right claim keeps the part
that holds instead of dying whole.

No LLM and no sandbox: the executor and the reconciler are stubs, so the whole file
runs in the fast (`-m "not ai"`) suite.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.autoresearch import (  # noqa: E402
    Autoresearcher, LLMClient, MODEL_VIEW_CAP, _jsonify, _locate_numbers,
    _match_num, _nums, _claim_nums, _usable_derivation, format_briefing,
)


# ── the number tokenizer ──────────────────────────────────────────────────────
class TestNums:
    def test_sign_is_part_of_the_quantity(self):
        """A mutual-exclusion claim must not verify against a positive correlation."""
        assert _nums("rho=-0.732 (mutual exclusion)") == [-0.732]
        assert _match_num(-0.732, [0.7315]) is None      # +0.73 does NOT back -0.73
        assert _match_num(-0.732, [-0.7315]) == "direct"  # -0.73 does

    def test_hyphen_between_digits_is_still_a_range(self):
        assert _nums("richness ranged 19-98 ASVs") == [19.0, 98.0]

    def test_leading_minus_in_a_range(self):
        assert _nums("y in [-3.33, -2.51]") == [-3.33, -2.51]

    def test_scientific_notation_is_one_number(self):
        assert _nums("p=6e-16") == [6e-16]
        assert _nums("p<1e-6") == [1e-6]
        assert _nums("1.4e3 reads") == [1400.0]

    def test_identifiers_are_not_quantities(self):
        assert _nums("SRR38966955 on PC1 and PC2") == []
        assert _nums("SAR92 clade") == []

    def test_thousands_separators(self):
        assert _nums("1,398,204 raw reads") == [1398204.0]


class TestMatchNum:
    def test_unit_conversions_and_derivation(self):
        assert _match_num(43.1, [0.4311]) == "x100"
        assert _match_num(0.431, [43.11]) == "/100"
        assert _match_num(73, [84, 11]) == "derived:diff"

    def test_an_element_never_derives_from_itself(self):
        """A lone value must not pair with itself to 'derive' 0 (84 - 84)."""
        assert _match_num(0.0, [84.0]) is None

    def test_derivation_does_not_depend_on_float_interning(self):
        """The guard means 'same element', not 'same object'. With `a is b` the answer
        for two equal values flipped on whether CPython happened to intern them —
        same numbers, different verdict, depending on how the list was built."""
        literal = [84.0, 84.0]
        built = [float("8" + "4"), float("8" + "4")]
        assert built[0] is not built[1]                       # distinct objects
        assert _match_num(0.0, literal) == _match_num(0.0, built)

    def test_a_wrong_number_is_still_rejected(self):
        assert _match_num(915.0, [84.0, 11.0, 161.0]) is None


class TestClaimNums:
    """A threshold defines the analysis; it is not a result, so no re-derivation
    will ever produce it. Demanding it as evidence failed an exact replication."""

    def test_thresholds_are_excluded_from_the_claimed_values(self):
        v = "core (>=50% prevalence): 14 ASVs; transient (<=10% prevalence): 511/735 ASVs (69.5%)"
        assert _nums(v) == [50.0, 14.0, 10.0, 511.0, 735.0, 69.5]      # every number
        assert _claim_nums(v) == [14.0, 511.0, 69.5]     # assertions only (735 is a denominator)

    def test_unicode_and_worded_comparisons_too(self):
        assert _claim_nums("core (\u226550% prevalence): 14 ASVs") == [14.0]
        assert _claim_nums("at least 30 samples had 12 ASVs") == [12.0]

    def test_a_denominator_is_context_not_a_finding(self):
        """"2/63 samples" asserts the 2; the 63 is the population it came from.
        Requiring it failed an otherwise exact replication (k11)."""
        v = "Delftia: 1 ASV, 16 reads, present in 2/63 samples; rho=-0.015"
        assert _claim_nums(v) == [1.0, 16.0, 2.0, -0.015]
        assert 63.0 in _nums(v)                       # still a number, just not a claim

    def test_a_bare_ratio_keeps_both_sides(self):
        assert _claim_nums("ratio 3/4 and value 12.5") == [3.0, 12.5]

    def test_ordinary_numbers_and_signs_survive(self):
        assert _claim_nums("rho=-0.73, n=44") == [-0.73, 44.0]

    def test_an_exact_replication_of_a_thresholded_claim_now_matches(self):
        """The k12 case end to end: every derived quantity is backed, so the claim
        must match even though its stated cutoffs appear in no result."""
        v = "core (>=50% prevalence): 14 ASVs; transient (<=10% prevalence): 511/735 ASVs (69.5%)"
        pool = [14.0, 511.0, 0.019, 0.6952]            # what the analyst derived
        assert all(_match_num(x, pool) for x in _claim_nums(v))
        assert not all(_match_num(x, pool) for x in _nums(v))   # would have failed before


# ── claim-directed evidence ───────────────────────────────────────────────────
class TestLocateNumbers:
    def test_finds_a_value_that_serialises_far_past_the_old_600_char_cap(self):
        """The real-run failure: k15 cited a correlation buried past the prefix and
        was refuted for it. The value must be locatable wherever it sits."""
        result = {f"pair_{i}": {"rho": 0.1 + i / 1000} for i in range(120)}
        result["Flavicella-Colwellia"] = {"rho": 0.8121}
        import json
        assert len(json.dumps(result)) > 600          # would have been cut off
        hits = _locate_numbers(result, [0.812])
        assert ("Flavicella-Colwellia.rho", 0.8121) in hits

    def test_reports_the_path_not_just_the_value(self):
        hits = _locate_numbers({"batch": {"bacteria": [0.78, 0.11]}}, [0.78])
        assert hits == [("batch.bacteria[0]", 0.78)]

    def test_a_genuine_miss_returns_nothing(self):
        assert _locate_numbers({"rho": 0.11}, [0.99]) == []

    def test_finds_a_fraction_backing_a_percent_claim(self):
        assert _locate_numbers({"retained": 0.4311}, [43.1]) == [("retained", 0.4311)]


class TestJsonifyCaps:
    def test_the_model_view_is_capped_but_the_stored_result_is_not(self):
        """Context economy for the model must not cost the verifier its evidence."""
        n_items = 150
        big = {f"k{i}": float(i) for i in range(n_items)}
        assert len(_jsonify(big, cap=MODEL_VIEW_CAP)) == MODEL_VIEW_CAP
        assert len(_jsonify(big, cap=n_items)) == n_items


# ── verify(): verdicts end-to-end ─────────────────────────────────────────────
class _StubExecutor:
    """Re-executes by returning a canned result per code string."""

    def __init__(self, results):
        self.results = results
        self.calls = 0

    async def run(self, code, timeout=30):
        self.calls += 1
        return (True, self.results[code]) if code in self.results else (False, "boom")


class _StubData:
    def __init__(self, datasets=None):
        self._d = datasets or {}

    def read_json(self, name):
        return None

    def datasets(self):
        return self._d

    def navigate(self, path):
        cur = self._d
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False, None
        return True, cur

    @property
    def study(self):
        return {}


def _researcher(*, results=None, datasets=None, reconcile=False, reply=None):
    """An Autoresearcher whose reconciler returns a fixed model reply."""
    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                class M:
                    content = reply
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(datasets), LLMClient(_Client(), "stub-model"),
                        _StubExecutor(results or {}), reconcile=reconcile)
    return ar


def test_verified_by_direct_reexecution():
    ar = _researcher(results={"code_a": {"n_asvs": 161}})
    ar.computations = {"c1": {"label": "asv count", "code": "code_a", "result": {"n_asvs": 161}}}
    ar.ledger = [{"id": "k1", "statement": "161 ASVs retained", "value": "161",
                  "antecedents": ["c1"], "kind": "observation"}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "verified"
    assert ar.ledger[0]["method"] == "direct"


def test_refuted_when_reexecution_contradicts():
    ar = _researcher(results={"code_a": {"n_asvs": 161}})
    ar.computations = {"c1": {"label": "asv count", "code": "code_a", "result": {"n_asvs": 161}}}
    ar.ledger = [{"id": "k1", "statement": "915 ASVs retained", "value": "915",
                  "antecedents": ["c1"], "kind": "observation"}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "refuted"


def test_unverifiable_without_a_checkable_antecedent():
    ar = _researcher()
    ar.ledger = [{"id": "k1", "statement": "the batches sample the same sites",
                  "value": "shared prefixes FF, SW, WC", "antecedents": ["nowhere.at.all"],
                  "kind": "observation"}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "unverifiable"


def test_partial_keeps_the_claim_and_names_the_bad_numbers():
    """A mostly-right claim used to die whole; now the failure is itemised."""
    ar = _researcher(
        results={"code_a": {"rho": [0.8319, -0.7315]}},
        reconcile=True,
        reply=("VERDICT: PARTIAL\nUNSUPPORTED: 0.812, 0.781\n"
               "Two of the four correlations are not in the evidence."))
    ar.computations = {"c1": {"label": "co-occurrence", "code": "code_a",
                              "result": {"rho": [0.8319, -0.7315]}}}
    ar.ledger = [{"id": "k1", "statement": "a co-occurring guild", "kind": "pattern",
                  "value": "0.832, 0.812, 0.781, -0.732", "antecedents": ["c1"]}]
    asyncio.run(ar.verify())
    c = ar.ledger[0]
    assert c["verdict"] == "partial"
    assert c["unsupported_numbers"] == [0.812, 0.781]
    assert c["reconciled_by"] == "stub-model"


def test_reconciler_can_still_upgrade_to_verified():
    ar = _researcher(results={"code_a": {"rho": 0.8319}}, reconcile=True,
                     reply="VERDICT: SUPPORTED\nUNSUPPORTED: none\nMatches within rounding.")
    ar.computations = {"c1": {"label": "corr", "code": "code_a", "result": {"rho": 0.8319}}}
    ar.ledger = [{"id": "k1", "statement": "strong correlation", "kind": "pattern",
                  "value": "rho about four fifths", "antecedents": ["c1"]}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "verified"
    assert ar.ledger[0]["method"] == "reconciled"


def test_each_computation_is_reexecuted_once_per_verify():
    ar = _researcher(results={"code_a": {"n": 5}})
    ar.computations = {"c1": {"label": "n", "code": "code_a", "result": {"n": 5}}}
    ar.ledger = [{"id": f"k{i}", "statement": "n is 5", "value": "5",
                  "antecedents": ["c1"], "kind": "observation"} for i in range(4)]
    asyncio.run(ar.verify())
    assert ar.executor.calls == 1


# ── evidence handed to the reconciler ─────────────────────────────────────────
def test_evidence_quotes_the_claim_values_by_path():
    ar = _researcher()
    result = {f"pair_{i}": 0.1 + i / 1000 for i in range(120)}
    result["Flavicella-Colwellia"] = 0.8121
    ar.computations = {"c1": {"label": "co-occurrence", "code": "x", "result": result}}
    claim = {"statement": "a guild co-occurs", "value": "0.812", "antecedents": ["c1"]}
    ev = ar._evidence_for(claim, {"c1": (True, result)})
    assert "Flavicella-Colwellia = 0.8121" in ev      # located despite the tail position
    assert "values matching the claim" in ev


def test_evidence_marks_a_real_miss_as_a_real_miss():
    """The reconciler must be able to tell 'not in the data' from 'not shown'."""
    ar = _researcher()
    result = {f"pair_{i}": 0.1 + i / 1000 for i in range(120)}
    ar.computations = {"c1": {"label": "co-occurrence", "code": "x", "result": result}}
    claim = {"statement": "rho was 0.99", "value": "0.99", "antecedents": ["c1"]}
    ev = ar._evidence_for(claim, {"c1": (True, result)})
    assert "not a display truncation" in ev


# ── write_results(): salvage ──────────────────────────────────────────────────
def test_partial_claims_reach_the_writer_with_a_do_not_state_list():
    captured = {}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                captured.update(kw)
                class M:
                    content = "Results prose."
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "stub-model"), _StubExecutor({}))
    ar.ledger = [
        {"id": "k1", "statement": "161 ASVs retained", "value": "161", "kind": "observation",
         "antecedents": ["c1"], "verdict": "verified"},
        {"id": "k2", "statement": "a co-occurring guild", "value": "0.832, 0.812", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "partial", "unsupported_numbers": [0.812]},
        {"id": "k3", "statement": "wrong thing", "value": "915", "kind": "observation",
         "antecedents": ["c1"], "verdict": "refuted"},
    ]
    asyncio.run(ar.write_results())
    prompt = captured["messages"][-1]["content"]
    assert "a co-occurring guild" in prompt          # the finding survives
    assert "DO NOT STATE these unsupported values: 0.812" in prompt
    assert "wrong thing" not in prompt               # refuted still excluded


# ── explore(): malformed tool arguments ───────────────────────────────────────
def test_unparseable_tool_args_are_reported_not_silently_executed():
    """Truncated JSON used to become `{}` — recording an empty claim. It must now
    come back to the model as an error, and record nothing."""
    calls = {"n": 0}

    class _ToolCall:
        id = "tc1"

        class function:
            name = "record_claim"
            arguments = '{"statement": "half a cl'   # truncated at max_tokens

        def model_dump(self):
            return {"id": self.id, "function": {"name": self.function.name,
                                                "arguments": self.function.arguments}}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                calls["n"] += 1
                class M:
                    content = ""
                    tool_calls = [_ToolCall()] if calls["n"] == 1 else None
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "stub-model"), _StubExecutor({}),
                        max_steps=2)
    asyncio.run(ar.explore())
    assert ar.ledger == []          # nothing empty recorded


# ── clean-room replication (issue #50) ────────────────────────────────────────
def _replicating_researcher(reply, results, *, ledger, comps):
    """An Autoresearcher whose clean-room analyst returns a fixed reply, and whose
    sandbox returns `results` keyed by the code it is handed."""
    seen = []

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                seen.append(kw)
                class M:
                    content = reply[len(seen) - 1] if isinstance(reply, list) else reply
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "explorer-model"),
                        _StubExecutor(results), replicate_model="auditor-model")
    ar.ledger, ar.computations = ledger, comps
    ar.seen_prompts = seen
    return ar


_LEDGER = [{"id": "k1", "statement": "richness tracks depth", "value": "rho=0.78",
            "antecedents": ["c1"], "kind": "pattern", "verdict": "verified",
            "reproduced": True}]
_COMPS = {"c1": {"label": "depth vs richness", "code": "orig_code", "result": {"rho": 0.78}}}


def test_agreement_upgrades_the_claim_to_replicated():
    ar = _replicating_researcher(
        "```python\nresult = {'rho': 0.7801}\n```\nSUPPORTS: YES\nSame answer my own way.",
        {"result = {'rho': 0.7801}": {"rho": 0.7801}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    n = asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert n == 1
    assert c["verdict"] == "replicated"
    assert c["replications"][0]["numbers_match"] is True
    assert c["replications"][0]["by"] == "auditor-model"
    assert c["replications"][0]["round"] == 2


def test_disagreement_marks_the_claim_disputed_not_verified():
    """The whole point: reproducible is not correct. A second derivation that lands
    somewhere else must not leave the claim looking confirmed."""
    ar = _replicating_researcher(
        "```python\nresult = {'rho': 0.12}\n```\nSUPPORTS: NO\nI get a much weaker relationship.",
        {"result = {'rho': 0.12}": {"rho": 0.12}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert c["verdict"] == "disputed"
    assert c["replications"][0]["numbers_match"] is False
    assert c["replications"][0]["analyst"] == "contradicts"


def test_the_analyst_never_sees_the_original_code():
    """Clean room means clean: the original implementation must not leak into the
    replication prompt, or the two derivations are not independent."""
    ar = _replicating_researcher(
        "```python\nresult = {'rho': 0.7801}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.7801}": {"rho": 0.7801}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    prompt = "\n".join(m["content"] for m in ar.seen_prompts[0]["messages"])
    assert "richness tracks depth" in prompt      # the claim, necessarily
    assert "orig_code" not in prompt              # the implementation, never
    assert "depth vs richness" not in prompt      # nor the original's own label


def test_a_failing_analysis_is_retried_with_the_error():
    ar = _replicating_researcher(
        ["```python\nresult = boom\n```\nSUPPORTS: YES",
         "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES"],
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    assert ar.ledger[0]["verdict"] == "replicated"
    assert ar.ledger[0]["replications"][0]["attempts"] == 2
    retry = "\n".join(m["content"] for m in ar.seen_prompts[1]["messages"])
    assert "Your code failed" in retry


def test_an_analyst_that_never_produces_code_leaves_the_verdict_alone():
    ar = _replicating_researcher(
        "I would rather not write code.",
        {}, ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    n = asyncio.run(ar.replicate())
    assert n == 0
    assert ar.ledger[0]["verdict"] == "verified"      # unchanged, not punished


def test_only_computed_surviving_claims_are_candidates_insights_first():
    ar = _replicating_researcher("", {}, ledger=[
        {"id": "k1", "statement": "s", "value": "1", "antecedents": ["c1"],
         "kind": "observation", "verdict": "verified"},
        {"id": "k2", "statement": "s", "value": "2", "antecedents": ["c1"],
         "kind": "anomaly", "verdict": "verified"},
        {"id": "k3", "statement": "s", "value": "3", "antecedents": ["c1"],
         "kind": "pattern", "verdict": "refuted"},          # already dead
        {"id": "k4", "statement": "s", "value": "4", "antecedents": ["overview.n"],
         "kind": "pattern", "verdict": "verified"},          # data read, not computed
    ], comps=dict(_COMPS))
    got = [c["id"] for c in ar._replication_candidates()]
    assert got == ["k2", "k1"]      # insight first; refuted and data-only excluded


def test_replicated_claims_are_verified_tier_for_the_writer_and_disputed_are_not():
    captured = {}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                captured.update(kw)
                class M:
                    content = "prose"
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "m"), _StubExecutor({}))
    ar.ledger = [
        {"id": "k1", "statement": "replicated finding", "value": "1", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "replicated"},
        {"id": "k2", "statement": "disputed finding", "value": "2", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "disputed"},
    ]
    asyncio.run(ar.write_results())
    prompt = captured["messages"][-1]["content"]
    assert "replicated finding" in prompt
    assert "disputed finding" not in prompt


def test_run_summary_reports_the_clean_room_outcome():
    ar = Autoresearcher(_StubData(), LLMClient(None, "m"), _StubExecutor({}))
    ar.ledger = [
        {"id": "k1", "verdict": "replicated", "replications": [{"round": 2, "by": "auditor-model"}]},
        {"id": "k2", "verdict": "disputed",
         "replications": [{"round": 2, "by": "auditor-model"}, {"round": 3, "by": "third-model"}]},
        {"id": "k3", "verdict": "verified"},
    ]
    s = ar.run_summary(completed=True)
    assert s["replication_attempted"] == 2
    assert s["replication_agreed"] == 1
    assert s["replication_disputed"] == 1
    assert s["replication_rounds"] == 3
    assert s["adjudicated"] == 1
    assert "auditor-model" in s["models"] and "third-model" in s["models"]


class TestUsableDerivation:
    """An all-nan or empty result is a FAILED derivation, not a disagreement — an
    empty selection or a broken join. Four real claims were voted against on the
    strength of `{}` or `{'rho': nan}`."""

    def test_nan_and_empty_are_not_derivations(self):
        assert _usable_derivation({"rho": float("nan")}) is False
        assert _usable_derivation({}) is False
        assert _usable_derivation({"a": {"b": [float("nan"), float("inf")]}}) is False

    def test_any_finite_number_counts(self):
        assert _usable_derivation({"rho": 0.78, "p": float("nan")}) is True
        assert _usable_derivation({"n": 0}) is True


def test_an_unusable_result_is_retried_with_the_reason():
    replies = ["```python\nresult = {'rho': float('nan')}\n```\nSUPPORTS: NO",
               "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES"]
    ar = _replicating_researcher(
        replies,
        {"result = {'rho': float('nan')}": {"rho": float("nan")},
         "result = {'rho': 0.78}": {"rho": 0.78}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert c["replications"][0]["attempts"] == 2
    assert c["verdict"] == "replicated"
    retry = "\n".join(m["content"] for m in ar.seen_prompts[1]["messages"])
    assert "no finite numbers" in retry


def test_a_failed_derivation_does_not_vote_against_the_claim():
    """The k3/k7/k10/k16 failure: nan results counted as disagreement."""
    ar = _replicating_researcher(
        "```python\nresult = {'rho': float('nan')}\n```\nSUPPORTS: NO",
        {"result = {'rho': float('nan')}": {"rho": float("nan")}},
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
    n = asyncio.run(ar.replicate())
    assert n == 0                                   # nothing usable was produced
    assert ar.ledger[0]["verdict"] == "verified"    # unchanged, not disputed


# ── round 3: adjudication ─────────────────────────────────────────────────────
def _claim(verdict="disputed", reproduced=True, value="rho=0.78", reps=()):
    reps = [{"by": "analyst-2", **r} for r in reps]      # round 2 ran on its own model
    return {"id": "k1", "statement": "richness tracks depth", "value": value,
            "antecedents": ["c1"], "kind": "pattern", "verdict": verdict,
            "reproduced": reproduced, "replications": list(reps)}


def _round3(reply, results, claim):
    ar = _replicating_researcher(reply, results, ledger=[claim], comps=dict(_COMPS))
    ar.adjudicate_model = "third-model"
    n = asyncio.run(ar.adjudicate())
    return ar, n


def test_round_three_rescues_a_claim_the_lone_dissenter_got_wrong():
    """Two of three derivations agreeing beats one that stands alone."""
    ar, n = _round3(
        "```python\nresult = {'rho': 0.7799}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.7799}": {"rho": 0.7799}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "numbers_match": False, "analyst": "contradicts"}]))
    assert n == 1
    assert ar.ledger[0]["verdict"] == "contested"   # 1 agree, 1 dissent — unstable
    assert ar.ledger[0]["replications"][-1]["round"] == 3
    assert ar.ledger[0]["replications"][-1]["by"] == "third-model"


def test_two_independents_concurring_against_the_claim_overturn_it():
    """A single dissent is a stand-off; two that land together is a conclusion."""
    ar, n = _round3(
        "```python\nresult = {'rho': 0.21}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.21}": {"rho": 0.21}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "numbers_match": False, "analyst": "contradicts"}]))
    c = ar.ledger[0]
    assert c["verdict"] == "overturned"
    # And it hands back what they agreed ON, which is the useful part.
    assert any(abs(x - 0.2) < 0.02 for x in c["consensus_numbers"])


def test_independents_wrong_in_different_directions_are_contested_not_overturned():
    """Instability is a finding about the analysis, not a verdict on the claim."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.95}\n```\nSUPPORTS: INCONCLUSIVE",
        {"result = {'rho': 0.95}": {"rho": 0.95}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.05},
                      "numbers_match": False, "analyst": "inconclusive"}]))
    assert ar.ledger[0]["verdict"] == "contested"
    assert ar.ledger[0].get("consensus_numbers", []) == []


def test_numbers_decide_concurrence_not_the_analysts_self_reports():
    """Two analysts can both say "contradicts" while landing on entirely different
    values — that is contested, not overturned. Their results are the evidence; what
    they say about their results is opinion."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.95}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.95}": {"rho": 0.95}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.05},
                      "numbers_match": False, "analyst": "contradicts"}]))
    assert ar.ledger[0]["verdict"] == "contested"


def test_a_non_numeric_derivation_cannot_settle_a_numeric_claim():
    """Superseded behaviour: a result with no numbers used to let the analyst's own
    SUPPORTS line decide. It cannot confirm or refute a quantity, so it now simply
    does not vote."""
    ar, n = _round3(
        "```python\nresult = {'note': 'no such pattern'}\n```\nSUPPORTS: NO",
        {"result = {'note': 'no such pattern'}": {"note": "no such pattern"}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "numbers_match": False, "analyst": "contradicts"}]))
    assert n == 0
    assert ar.ledger[0]["verdict"] == "disputed"      # still just the one dissent


def test_round_three_rescues_a_refuted_claim_whose_citation_was_wrong():
    """Correct science, broken bookkeeping: the antecedents don't produce the number
    but an independent derivation does. Rescue it, and flag the provenance."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        _claim(verdict="refuted", reproduced=False))
    c = ar.ledger[0]
    assert c["verdict"] == "replicated"
    assert c["antecedent_mismatch"] is True


def test_a_refuted_claim_no_one_can_re_derive_stays_refuted():
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.11}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.11}": {"rho": 0.11}},
        _claim(verdict="refuted", reproduced=False))
    assert ar.ledger[0]["verdict"] == "refuted"


def test_contested_claims_are_not_sent_to_round_three():
    """Evidence already inconsistent with itself is not settled by adding more."""
    ar = _replicating_researcher("", {}, ledger=[_claim(verdict="contested")],
                                 comps=dict(_COMPS))
    assert ar._adjudication_candidates() == []


def test_replicated_claims_are_not_re_adjudicated():
    ar = _replicating_researcher("", {}, ledger=[_claim(verdict="replicated")],
                                 comps=dict(_COMPS))
    assert ar._adjudication_candidates() == []


def test_overturned_and_contested_stay_out_of_the_prose():
    captured = {}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                captured.update(kw)
                class M:
                    content = "prose"
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "m"), _StubExecutor({}))
    ar.ledger = [
        {"id": "k1", "statement": "solid finding", "value": "1", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "replicated"},
        {"id": "k2", "statement": "overturned finding", "value": "2", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "overturned"},
        {"id": "k3", "statement": "contested finding", "value": "3", "kind": "pattern",
         "antecedents": ["c1"], "verdict": "contested"},
    ]
    asyncio.run(ar.write_results())
    prompt = captured["messages"][-1]["content"]
    assert "solid finding" in prompt
    assert "overturned finding" not in prompt
    assert "contested finding" not in prompt


# ── the data briefing, and correlated analysts ────────────────────────────────
class TestBriefing:
    def test_states_orientation_and_the_axis_rule_not_just_the_shape(self):
        """Prose ("counts is a samples x ASV DataFrame") was not enough — an analyst
        transposed it anyway and overturned two correct claims."""
        out = format_briefing({"counts": {"shape": [63, 735],
                                          "sample_ids_sample": ["SRR1"], "asv_ids_sample": ["ASV_1"]},
                               "tax": {"shape": [735, 6], "columns": ["Domain", "Genus"]}})
        assert "63 rows x 735 columns" in out
        assert "ROWS ARE SAMPLES" in out and "COLUMNS ARE ASVs" in out
        assert "axis=0" in out and "axis=1" in out
        # Names the exact failure mode observed, in the data's own numbers.
        assert "63 ASVs or 735 samples has them backwards" in out

    def test_empty_probe_renders_nothing(self):
        assert format_briefing({}) == ""

    def test_partial_data_does_not_break_it(self):
        out = format_briefing({"meta": {"shape": [63, 16], "columns": ["x", "y"]}})
        assert "meta: 63 rows x 16 columns" in out and "counts" not in out


def test_briefing_is_probed_once_and_reaches_the_analyst():
    calls = []

    class _Exec:
        async def run(self, code, timeout=30):
            calls.append(code)
            return True, {"counts": {"shape": [63, 735], "sample_ids_sample": ["SRR1"],
                                     "asv_ids_sample": ["ASV_1"]}}

    seen = []

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                seen.append(kw)
                class M:
                    content = "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES"
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "m"), _Exec(), replicate_model="a2")
    ar.ledger = [dict(c) for c in _LEDGER]
    ar.computations = dict(_COMPS)
    asyncio.run(ar.replicate())
    prompt = seen[0]["messages"][-1]["content"]
    assert "ROWS ARE SAMPLES" in prompt          # orientation reached the analyst
    assert calls.count(  # probed once, then cached — not re-run per claim
        [c for c in calls if "sample_ids_sample" in c][0]) == 1


def test_briefing_failure_never_blocks_replication():
    class _Exec:
        async def run(self, code, timeout=30):
            raise RuntimeError("sandbox down")

    ar = Autoresearcher(_StubData(), LLMClient(None, "m"), _Exec())
    assert asyncio.run(ar.data_briefing()) == ""


def test_one_model_cannot_concur_with_itself():
    """Observed on real data: the same model transposed a table in round 2 and
    emitted byte-identical code in round 3, so its own error 'concurred' and
    overturned two claims that were exactly right."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.21}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.21}": {"rho": 0.21}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "numbers_match": False, "analyst": "contradicts",
                      "by": "third-model"}]))          # SAME model as round 3
    c = ar.ledger[0]
    assert c["verdict"] == "disputed"                  # not overturned
    assert c["correlated_analysts"] is True


def test_two_distinct_models_concurring_still_overturn():
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.21}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.21}": {"rho": 0.21}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "numbers_match": False, "analyst": "contradicts"}]))  # by=analyst-2
    assert ar.ledger[0]["verdict"] == "overturned"
    assert ar.ledger[0].get("correlated_analysts", False) is False


def test_a_fresh_pass_supersedes_the_previous_one():
    """Rounds append, so re-running replication over a saved ledger stacked a second
    pass on the first — one real run reached [2, 3, 2, 3] on a single claim, with
    superseded derivations still voting."""
    ar = _replicating_researcher(
        "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        ledger=[{**_LEDGER[0], "verdict": "overturned", "verdict_round1": "verified",
                 "consensus_numbers": [0.2], "correlated_analysts": True,
                 "replications": [{"round": 2, "code": "stale", "result": {"rho": 0.2},
                                   "numbers_match": False, "by": "old-model"}]}],
        comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert [r["round"] for r in c["replications"]] == [2]      # not [2, 2]
    assert c["replications"][0]["code"] != "stale"
    assert c["verdict"] == "replicated"
    assert "correlated_analysts" not in c and "consensus_numbers" not in c


def test_accumulating_across_passes_is_possible_but_opt_in():
    ar = _replicating_researcher(
        "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        ledger=[{**_LEDGER[0],
                 "replications": [{"round": 2, "code": "prior", "result": {"rho": 0.2},
                                   "numbers_match": False, "by": "old-model"}]}],
        comps=dict(_COMPS))
    asyncio.run(ar.replicate(fresh=False))
    assert len(ar.ledger[0]["replications"]) == 2


def test_assumptions_reach_the_writer():
    """The agent is made to surface what it could not confirm; dropping that at the
    writing step is exactly where candour matters most."""
    captured = {}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                captured.update(kw)
                class M:
                    content = "prose"
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    ar = Autoresearcher(_StubData(), LLMClient(_Client(), "m"), _StubExecutor({}))
    ar.ledger = [{"id": "k1", "statement": "a finding", "value": "1", "kind": "observation",
                  "antecedents": ["c1"], "verdict": "verified"}]
    ar.assumptions = [{"id": "as1", "statement": "Assuming counts are raw, not rarefied",
                       "why": "no renorm_stats present"}]
    asyncio.run(ar.write_results())
    prompt = captured["messages"][-1]["content"]
    assert "Assuming counts are raw, not rarefied" in prompt
    assert "no renorm_stats present" in prompt
    assert "not findings" in prompt          # framed so they aren't reported as results


def test_the_dag_carries_the_research_narrative():
    """A graph of claims alone shows what was found, not why it was looked for."""
    from ai.autoresearch import build_dag

    agenda = [{"id": "a4", "question": "Do samples cluster?", "status": "done", "parent": None},
              {"id": "a5", "question": "Which taxa drive it?", "status": "done", "parent": "a4"}]
    ledger = [{"id": "k7", "statement": "batches separate", "value": "1.0", "kind": "pattern",
               "antecedents": ["c10"], "verdict": "verified", "investigation": "a4"}]
    dag = build_dag({"c10": {"label": "bray-curtis"}}, ledger, agenda)
    kinds = {(e["from"], e["to"]): e.get("kind") for e in dag["edges"]}
    assert kinds[("a4", "a5")] == "followup"     # the follow-up lineage
    assert kinds[("a4", "k7")] == "answers"      # the question the claim answers
    assert kinds[("c10", "k7")] is None          # antecedent edges stay unlabelled
    assert {n["id"]: n["type"] for n in dag["nodes"]}["a4"] == "investigation"


def test_the_dag_is_unchanged_when_no_agenda_is_passed():
    """Backward compatibility: the two-argument form still works."""
    from ai.autoresearch import build_dag

    dag = build_dag({"c1": {"label": "x"}},
                    [{"id": "k1", "statement": "s", "antecedents": ["c1"], "kind": "observation"}])
    assert [n["type"] for n in dag["nodes"]] == ["computation", "claim"]


# ── statistical hygiene helpers in the sandbox (issue #49) ────────────────────
def _sandbox_eval(code, tmp_path):
    """Run a snippet in the real subprocess sandbox. No data dir is needed — these
    helpers are pure numpy, which is the point: no new dependency, no image rebuild."""
    from ai.autoresearch import SubprocessExecutor
    return asyncio.run(SubprocessExecutor(tmp_path).run(code, timeout=60))


class TestStatHelpers:
    def test_fdr_matches_benjamini_hochberg_by_hand(self, tmp_path):
        ok, r = _sandbox_eval(
            "result = fdr([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216])",
            tmp_path)
        assert ok, r
        assert r["n_tests"] == 10
        assert r["p_adj"][0] == pytest.approx(0.01)      # 0.001 * 10/1
        assert r["p_adj"][-1] == pytest.approx(0.216)    # 0.216 * 10/10
        assert r["n_sig"] == 2                           # only the first two survive
        assert r["p_adj"] == sorted(r["p_adj"])          # monotone after step-up

    def test_fdr_handles_an_empty_family(self, tmp_path):
        ok, r = _sandbox_eval("result = fdr([])", tmp_path)
        assert ok and r["n_tests"] == 0 and r["n_sig"] == 0

    def test_clr_shrinks_the_closure_artifact_on_a_synthetic_case(self, tmp_path):
        """Two independent taxa correlate spuriously once counts are closed to
        proportions — the k15 failure mode. This pins that the helper behaves as a
        compositional control on a clean synthetic case; it is NOT a claim that CLR
        is reliable on real data (it is sensitive to the pseudocount, and with few
        parts the sum-to-zero constraint manufactures negative correlation itself)."""
        ok, r = _sandbox_eval("""
# Realistic shape: two INDEPENDENT minor taxa, one dominant taxon swinging wildly
# between samples (this is what drives the closure), plus a tail of rare taxa.
rng = np.random.default_rng(0); n = 60
a = rng.integers(20, 60, n); b = rng.integers(20, 60, n)
dom = rng.integers(200, 20000, n)
tail = rng.integers(1, 40, (n, 48))
df = pd.DataFrame(np.column_stack([a, b, dom, tail]))
df.columns = ['a', 'b', 'dom'] + [f't{i}' for i in range(48)]
prop = df.div(df.sum(1), axis=0); Z = clr(df)
result = {'true': float(spearmanr(a, b)[0]),
          'closed': float(spearmanr(prop.a, prop.b)[0]),
          'clr': float(spearmanr(Z.a, Z.b)[0])}
""", tmp_path)
        assert ok, r
        assert abs(r["true"]) < 0.2                   # the taxa really are independent
        assert r["closed"] > 0.6                      # yet closure manufactures a "guild"
        assert abs(r["clr"]) < r["closed"] / 3        # CLR removes most of the artifact

    def test_clr_manufactures_negative_correlation_when_parts_are_few(self, tmp_path):
        """The documented failure mode, pinned so nobody 'fixes' the docstring away:
        with 3 parts the CLR values sum to zero, so two independent taxa come out
        anti-correlated. This is why the prompt treats CLR as one control, not the
        answer."""
        ok, r = _sandbox_eval("""
rng = np.random.default_rng(0)
a = rng.integers(5, 50, 40); b = rng.integers(5, 50, 40)
dom = rng.integers(500, 8000, 40)
df = pd.DataFrame({'a': a, 'b': b, 'dom': dom})
Z = clr(df)
result = {'true': float(spearmanr(a, b)[0]), 'clr': float(spearmanr(Z.a, Z.b)[0])}
""", tmp_path)
        assert ok, r
        assert abs(r["true"]) < 0.2      # independent by construction
        assert r["clr"] < -0.3           # yet CLR reports mutual exclusion

    def test_clr_preserves_labels(self, tmp_path):
        ok, r = _sandbox_eval(
            "df = pd.DataFrame([[1,2],[3,4]], index=['s1','s2'], columns=['x','y']);"
            " z = clr(df); result = {'idx': list(z.index), 'cols': list(z.columns)}", tmp_path)
        assert ok and r["idx"] == ["s1", "s2"] and r["cols"] == ["x", "y"]

    def test_rarefy_is_common_depth_and_seeded(self, tmp_path):
        """Seeded so the claim re-executes identically at verification time (#39)."""
        ok, r = _sandbox_eval("""
rng = np.random.default_rng(1)
df = pd.DataFrame(rng.integers(0, 60, (6, 12)), index=[f's{i}' for i in range(6)])
r1, dropped = rarefy(df, depth=100, seed=7)
r2, _ = rarefy(df, depth=100, seed=7)
r3, _ = rarefy(df, depth=100, seed=8)
result = {'depths': sorted(set(r1.sum(1).tolist())), 'kept': list(r1.index),
          'dropped': dropped, 'same_seed_identical': bool((r1.values == r2.values).all()),
          'other_seed_differs': bool((r1.values != r3.values).any())}
""", tmp_path)
        assert ok, r
        assert r["depths"] == [100]                   # every sample at a common depth
        assert r["same_seed_identical"]
        assert r["other_seed_differs"]
        assert len(r["kept"]) + len(r["dropped"]) == 6

    def test_rarefy_drops_samples_below_the_depth(self, tmp_path):
        ok, r = _sandbox_eval(
            "df = pd.DataFrame([[100,100],[2,1]], index=['deep','shallow']);"
            " kept, dropped = rarefy(df, depth=50, seed=0);"
            " result = {'kept': list(kept.index), 'dropped': dropped}", tmp_path)
        assert ok and r["kept"] == ["deep"] and r["dropped"] == ["shallow"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
