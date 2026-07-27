"""Verification-layer tests for claim-grounded autoresearch (issue #48).

These pin the behaviour the real-sample run exposed: a claim must not be graded on
what the VERIFIER could see (a truncated evidence prefix, a dropped list tail), a
sign or an exponent is part of a quantity, and a mostly-right claim keeps the part
that holds instead of dying whole.

No LLM and no sandbox: the executor and the reconciler are stubs, so the whole file
runs in the fast (`-m "not ai"`) suite.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.autoresearch import _norm_assertions  # noqa: E402
from ai.autoresearch import (  # noqa: E402
    Autoresearcher, JUDGE_SYSTEM, LLMClient, MODEL_VIEW_CAP, REPLICATE_SYSTEM,
    _compact_messages, _jsonify, _usable_derivation, format_briefing,
)
from ai.autoresearch import (  # noqa: E402
    GERMINATE_SYSTEM, GERMINATE_TOOLS, SWEEP_SYSTEM, SWEEP_TOOLS, TIP_SYSTEM, TIP_TOOLS,
)


def _step(n: int, n_tools: int = 2, size: int = 4000) -> list[dict]:
    """One explore step as it lands in the transcript: an assistant turn carrying
    tool_calls, followed by the tool messages that answer them."""
    calls = [{"id": f"c{n}_{k}", "type": "function",
              "function": {"name": "run_analysis", "arguments": "{}"}} for k in range(n_tools)]
    return ([{"role": "assistant", "content": "", "tool_calls": calls}]
            + [{"role": "tool", "tool_call_id": f"c{n}_{k}", "content": "x" * size}
               for k in range(n_tools)])


class TestCompactMessages:
    """Nothing trimmed the explore transcript, so every run above the step cap would
    have overflowed the window — and an overflow drops from the FRONT, taking the
    system prompt and the data briefing with it."""

    def _head(self):
        return [{"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "BRIEFING"}]

    def test_under_budget_is_returned_untouched(self):
        msgs = self._head() + _step(1)
        assert _compact_messages(msgs, budget=1_000_000) is msgs

    def test_system_prompt_and_briefing_are_never_dropped(self):
        out = _compact_messages(self._head() + [m for n in range(20) for m in _step(n)],
                                budget=20_000)
        assert out[0]["content"] == "SYSTEM"
        assert out[1]["content"] == "BRIEFING"
        assert sum(len(m.get("content") or "") for m in out) < 40_000

    def test_no_tool_message_is_left_orphaned(self):
        """A `tool` message whose `tool_calls` were dropped is a 400, not a saving."""
        out = _compact_messages(self._head() + [m for n in range(20) for m in _step(n)],
                                budget=20_000)
        live = set()
        for m in out:
            if m["role"] == "assistant":
                live |= {tc["id"] for tc in (m.get("tool_calls") or [])}
            elif m["role"] == "tool":
                assert m["tool_call_id"] in live, "orphaned tool message"

    def test_the_newest_step_survives_however_tight_the_budget(self):
        out = _compact_messages(self._head() + [m for n in range(20) for m in _step(n)],
                                budget=1)
        assert out[-1]["role"] == "tool"
        assert out[-1]["tool_call_id"].startswith("c19_")

    def test_the_model_is_told_where_its_state_still_lives(self):
        out = _compact_messages(self._head() + [m for n in range(20) for m in _step(n)],
                                budget=20_000)
        assert "get_agenda" in out[2]["content"] and "elided" in out[2]["content"]


class TestNormAssertions:
    """Models hand array arguments back as strings. Iterating one yields an assertion
    per CHARACTER — it cost a full run, every claim arriving as {label: "["}."""

    def test_a_json_string_is_parsed_not_iterated(self):
        import json as _json
        raw = _json.dumps([{"label": "rho", "value": "0.63", "of": "63 samples"},
                           {"label": "p", "value": "<0.001"}])
        got = _norm_assertions(raw)
        assert [a["label"] for a in got] == ["rho", "p"]

    def test_a_real_list_still_works(self):
        got = _norm_assertions([{"label": "rho", "value": "0.63"}])
        assert got == [{"label": "rho", "value": "0.63", "of": ""}]

    def test_labelled_text_is_split(self):
        assert [a["label"] for a in _norm_assertions("a=1; b=2")] == ["a", "b"]

    def test_unparseable_text_yields_nothing_rather_than_characters(self):
        assert _norm_assertions('[{"label"') == []

    def test_the_value_fallback_still_covers_a_bare_string(self):
        got = _norm_assertions(None, "richness rose with depth")
        assert got == [{"label": "claim", "value": "richness rose with depth", "of": ""}]


class TestAssertionSalvage:
    """Claimants that skip `assertions` keep asserting numbers — they just move them
    into the statement. Recovering them keeps the claim checkable."""

    def test_labelled_quantities_are_recovered_from_prose(self):
        from ai.autoresearch import _assertions_from_text
        got = _assertions_from_text("Richness is depth-confounded (rho=1.0, p<0.001)")
        assert [(a["label"], a["value"]) for a in got] == [("rho", "1.0"), ("p", "0.001")]

    def test_prose_with_no_quantities_recovers_nothing(self):
        from ai.autoresearch import _assertions_from_text
        assert _assertions_from_text("the community looked diverse") == []


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


# Judges grade one line per assertion. A claim recorded with only a `value` string
# degrades to a single implicit assertion labelled "claim".
SUPPORTED = "ASSERTION claim: SUPPORTED — the evidence gives this value"
UNSUPPORTED = "ASSERTION claim: CONTRADICTED — the evidence gives 161"
AGREES = "ASSERTION claim: AGREES — same answer, derived my own way"
DIFFERS = "ASSERTION claim: DIFFERS — I get 0.2"


def _stub_client(*, analyst=None, judge=SUPPORTED, seen=None):
    """A chat stub that answers by ROLE. Verification is now two different jobs —
    an analyst writing code, then a judge reading its labelled result — so replies
    are dispatched on the system prompt rather than by call order."""
    def _next(v, box):
        if isinstance(v, list):
            return v[min(box[0], len(v) - 1)]
        return v

    counts = {"analyst": [0], "judge": [0]}

    class _Chat:
        class completions:
            @staticmethod
            async def create(**kw):
                if seen is not None:
                    seen.append(kw)
                system = kw["messages"][0]["content"]
                if system == REPLICATE_SYSTEM:
                    text = _next(analyst, counts["analyst"]); counts["analyst"][0] += 1
                else:
                    text = _next(judge, counts["judge"]); counts["judge"][0] += 1

                class M:
                    content = text
                class C:
                    message = M()
                class R:
                    choices = [C()]
                return R()

    class _Client:
        chat = _Chat()

    return _Client()


def _researcher(*, results=None, datasets=None, judge=SUPPORTED):
    """An Autoresearcher whose VERIFIER returns a fixed judgment."""
    return Autoresearcher(_StubData(datasets), LLMClient(_stub_client(judge=judge), "stub-model"),
                          _StubExecutor(results or {}))


def test_verified_by_direct_reexecution():
    ar = _researcher(results={"code_a": {"n_asvs": 161}}, judge=SUPPORTED)
    ar.computations = {"c1": {"label": "asv count", "code": "code_a", "result": {"n_asvs": 161}}}
    ar.ledger = [{"id": "k1", "statement": "161 ASVs retained", "value": "n_asvs=161",
                  "antecedents": ["c1"], "kind": "observation"}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "verified"
    assert ar.ledger[0]["method"] == "judged"


def test_refuted_when_reexecution_contradicts():
    ar = _researcher(results={"code_a": {"n_asvs": 161}}, judge=UNSUPPORTED)
    ar.computations = {"c1": {"label": "asv count", "code": "code_a", "result": {"n_asvs": 161}}}
    ar.ledger = [{"id": "k1", "statement": "915 ASVs retained", "value": "n_asvs=915",
                  "antecedents": ["c1"], "kind": "observation"}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "refuted"
    assert ar.ledger[0]["assertion_verdicts"] == {"claim": "contradicted"}


def test_unverifiable_without_a_checkable_antecedent():
    """No evidence means no judgment is even attempted — that stays mechanical."""
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
        judge=("ASSERTION rho_a: SUPPORTED — evidence gives 0.8319\n"
               "ASSERTION rho_b: CONTRADICTED — evidence gives -0.7315, not 0.812"))
    ar.computations = {"c1": {"label": "co-occurrence", "code": "code_a",
                              "result": {"rho": [0.8319, -0.7315]}}}
    ar.ledger = [{"id": "k1", "statement": "a co-occurring guild", "kind": "pattern",
                  "value": "rho_a=0.832, rho_b=0.812", "antecedents": ["c1"],
                  "assertions": [{"label": "rho_a", "value": "0.832", "of": ""},
                                 {"label": "rho_b", "value": "0.812", "of": ""}]}]
    asyncio.run(ar.verify())
    c = ar.ledger[0]
    assert c["verdict"] == "partial"
    assert c["unsupported_numbers"] == ["rho_b=0.812"]
    assert c["assertion_verdicts"] == {"rho_a": "supported", "rho_b": "contradicted"}
    assert c["judgment"]["by"] == "stub-model"


def test_the_judge_can_support_a_claim_stated_in_words():
    """A claim whose value is prose, not digits — impossible for a matcher, ordinary
    for a judge reading the evidence."""
    ar = _researcher(results={"code_a": {"rho": 0.8319}},
                     judge=SUPPORTED)
    ar.computations = {"c1": {"label": "corr", "code": "code_a", "result": {"rho": 0.8319}}}
    ar.ledger = [{"id": "k1", "statement": "strong correlation", "kind": "pattern",
                  "value": "rho about four fifths", "antecedents": ["c1"]}]
    asyncio.run(ar.verify())
    assert ar.ledger[0]["verdict"] == "verified"
    assert ar.ledger[0]["method"] == "judged"


def test_each_computation_is_reexecuted_once_per_verify():
    ar = _researcher(results={"code_a": {"n": 5}})
    ar.computations = {"c1": {"label": "n", "code": "code_a", "result": {"n": 5}}}
    ar.ledger = [{"id": f"k{i}", "statement": "n is 5", "value": "5",
                  "antecedents": ["c1"], "kind": "observation"} for i in range(4)]
    asyncio.run(ar.verify())
    assert ar.executor.calls == 1


# ── evidence handed to the reconciler ─────────────────────────────────────────
def test_evidence_shows_the_whole_labelled_result():
    """The judge reads labels, so the evidence keeps them and shows the result whole.
    The old claim-directed extraction existed to dodge a 600-char prefix cap; cutting
    the labels out was the problem, not the size."""
    ar = _researcher()
    result = {f"pair_{i}": 0.1 + i / 1000 for i in range(120)}
    result["Flavicella-Colwellia"] = 0.8121
    ar.computations = {"c1": {"label": "co-occurrence", "code": "x", "result": result}}
    claim = {"statement": "a guild co-occurs", "value": "0.812", "antecedents": ["c1"]}
    ev = ar._evidence_for(claim, {"c1": (True, result)})
    assert "co-occurrence (re-executed)" in ev          # the computation's own label
    assert '"Flavicella-Colwellia": 0.8121' in ev       # value AND label, not extracted
    assert len(ev) > 600                                # no prefix cap


def test_evidence_reports_a_failed_re_execution_as_such():
    ar = _researcher()
    ar.computations = {"c1": {"label": "co-occurrence", "code": "x", "result": {}}}
    claim = {"statement": "s", "value": "1", "antecedents": ["c1"]}
    assert "ERROR" in ar._evidence_for(claim, {"c1": (False, None)})


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
def _replicating_researcher(analyst, results, *, ledger, comps, judge=None):
    """An Autoresearcher whose clean-room analyst writes `analyst` and whose judge
    returns `judge`. Two distinct roles now, so the stub dispatches on the system
    prompt rather than on call order."""
    seen = []
    ar = Autoresearcher(_StubData(),
                        LLMClient(_stub_client(analyst=analyst, judge=judge or AGREES, seen=seen),
                                  "explorer-model"),
                        _StubExecutor(results), replicate_model="auditor-model")
    ar.ledger, ar.computations = ledger, comps
    ar.seen_prompts = seen
    return ar


_LEDGER = [{"id": "k1", "statement": "richness tracks depth", "value": "rho=0.78",
            "antecedents": ["c1"], "kind": "pattern", "verdict": "verified",
            "reproduced": True}]
_COMPS = {"c1": {"label": "depth vs richness", "code": "orig_code", "result": {"rho": 0.78}}}
_CODE_OK = "```python\nresult = {'rho': 0.7801}\n```\nSUPPORTS: YES"
_RAN = {"result = {'rho': 0.7801}": {"rho": 0.7801}}


def test_agreement_upgrades_the_claim_to_replicated():
    ar = _replicating_researcher(_CODE_OK, _RAN, ledger=[dict(c) for c in _LEDGER],
                                 comps=dict(_COMPS), judge=AGREES)
    n = asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert n == 1
    assert c["verdict"] == "replicated"
    assert c["replications"][0]["agrees"] is True
    assert c["replications"][0]["by"] == "auditor-model"
    assert c["replications"][0]["round"] == 2


def test_disagreement_marks_the_claim_disputed_not_verified():
    """Reproducible is not correct: a derivation that lands elsewhere must not
    leave the claim looking confirmed."""
    ar = _replicating_researcher(_CODE_OK, _RAN, ledger=[dict(c) for c in _LEDGER],
                                 comps=dict(_COMPS), judge=DIFFERS)
    asyncio.run(ar.replicate())
    c = ar.ledger[0]
    assert c["verdict"] == "disputed"
    assert c["replications"][0]["agrees"] is False


def test_the_judge_reads_the_analysts_labelled_result_not_the_claim_text():
    """The whole point of judging over a regex: the analyst reports its findings
    under its own labels, and never restates the claim's parameters."""
    ar = _replicating_researcher(_CODE_OK, _RAN, ledger=[dict(c) for c in _LEDGER],
                                 comps=dict(_COMPS), judge=AGREES)
    asyncio.run(ar.replicate())
    judge_prompt = [k for k in ar.seen_prompts
                    if k["messages"][0]["content"] != REPLICATE_SYSTEM][-1]["messages"][-1]["content"]
    assert "THE INDEPENDENT ANALYST'S RESULT" in judge_prompt
    assert "rho" in judge_prompt                    # the labels are what it judges on
    assert "orig_code" not in judge_prompt          # never the original implementation


def test_the_analyst_never_sees_the_original_code():
    """Clean room means clean: the original implementation must not leak into the
    replication prompt, or the two derivations are not independent."""
    ar = _replicating_researcher(_CODE_OK, _RAN, ledger=[dict(c) for c in _LEDGER],
                                 comps=dict(_COMPS))
    asyncio.run(ar.replicate())
    analyst_prompt = "\n".join(
        m["content"] for k in ar.seen_prompts if k["messages"][0]["content"] == REPLICATE_SYSTEM
        for m in k["messages"])
    assert "richness tracks depth" in analyst_prompt   # the claim, necessarily
    assert "orig_code" not in analyst_prompt           # the implementation, never
    assert "depth vs richness" not in analyst_prompt   # nor the original's own label


def test_a_failing_analysis_is_retried_with_the_error():
    ar = _replicating_researcher(
        ["```python\nresult = boom\n```\nSUPPORTS: YES", _CODE_OK], _RAN,
        ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS), judge=AGREES)
    asyncio.run(ar.replicate())
    assert ar.ledger[0]["verdict"] == "replicated"
    assert ar.ledger[0]["replications"][0]["attempts"] == 2
    retry = "\n".join(m["content"] for k in ar.seen_prompts
                       if k["messages"][0]["content"] == REPLICATE_SYSTEM
                       for m in k["messages"])
    assert "Your code failed" in retry


def test_an_analyst_that_never_produces_code_leaves_the_verdict_alone():
    ar = _replicating_researcher("I would rather not write code.", {},
                                 ledger=[dict(c) for c in _LEDGER], comps=dict(_COMPS))
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
    reps = [{"by": "analyst-2", "usable": True,
             "assertion_verdicts": {"claim": "agrees" if r.get("agrees") else "differs"},
             "roll": "all" if r.get("agrees") else "none",
             "judgment": {"notes": {"claim": r.get("got", "0.2")}}, **r} for r in reps]
    return {"id": "k1", "statement": "richness tracks depth", "value": value,
            "antecedents": ["c1"], "kind": "pattern", "verdict": verdict,
            "reproduced": reproduced, "replications": list(reps)}


def _round3(reply, results, claim, judge=DIFFERS):
    ar = _replicating_researcher(reply, results, ledger=[claim], comps=dict(_COMPS),
                                 judge=judge)
    ar.adjudicate_model = "third-model"
    n = asyncio.run(ar.adjudicate())
    return ar, n


def test_round_three_rescues_a_claim_the_lone_dissenter_got_wrong():
    """Two of three derivations agreeing beats one that stands alone."""
    ar, n = _round3(
        "```python\nresult = {'rho': 0.7799}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.7799}": {"rho": 0.7799}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "agrees": False, "analyst": "contradicts"}]), judge=AGREES)
    assert n == 1
    assert ar.ledger[0]["verdict"] == "contested"   # 1 agree, 1 dissent — unstable
    assert ar.ledger[0]["replications"][-1]["round"] == 3
    assert ar.ledger[0]["replications"][-1]["by"] == "third-model"


def test_two_independents_concurring_against_the_claim_overturn_it():
    """A single dissent is a stand-off; two that land on the SAME value is a conclusion."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.21}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.21}": {"rho": 0.21}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "agrees": False, "got": "I get 0.2"}]),
        judge="ASSERTION claim: DIFFERS — I get 0.2")
    c = ar.ledger[0]
    assert c["verdict"] == "overturned"
    # And the record keeps what they each got, which is the useful part of a dissent.
    assert c["assertion_replication"]["claim"]["differs"] == 2


def test_independents_landing_on_different_values_are_contested_not_overturned():
    """Two analysts dissenting to 0 and 72 means the quantity is ill-defined, not that
    the claim is wrong. This is the real k9 case: 'doubletons' meant three things."""
    ar, _ = _round3(
        "```python\nresult = {'n': 72}\n```\nSUPPORTS: NO",
        {"result = {'n': 72}": {"n": 72}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"n": 0},
                      "agrees": False, "got": "I get 0"}]),
        judge="ASSERTION claim: DIFFERS — I get 72")
    assert ar.ledger[0]["verdict"] == "contested"


def test_an_assertion_some_agree_and_some_dispute_is_contested():
    """One analyst reproduced it, another did not — the quantity is unstable, which is
    a different finding from either 'confirmed' or 'wrong'."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "agrees": False, "got": "I get 0.2"}]),
        judge=AGREES)
    assert ar.ledger[0]["verdict"] == "contested"


def test_a_non_numeric_derivation_cannot_settle_a_numeric_claim():
    """Superseded behaviour: a result with no numbers used to let the analyst's own
    SUPPORTS line decide. It cannot confirm or refute a quantity, so it now simply
    does not vote."""
    ar, n = _round3(
        "```python\nresult = {'note': 'no such pattern'}\n```\nSUPPORTS: NO",
        {"result = {'note': 'no such pattern'}": {"note": "no such pattern"}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "agrees": False, "analyst": "contradicts"}]))
    assert n == 0
    assert ar.ledger[0]["verdict"] == "disputed"      # still just the one dissent


def test_round_three_rescues_a_refuted_claim_whose_citation_was_wrong():
    """Correct science, broken bookkeeping: the antecedents don't produce the number
    but an independent derivation does. Rescue it, and flag the provenance."""
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.78}\n```\nSUPPORTS: YES",
        {"result = {'rho': 0.78}": {"rho": 0.78}},
        _claim(verdict="refuted", reproduced=False), judge=AGREES)
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
                      "agrees": False, "analyst": "contradicts",
                      "by": "third-model"}]))          # SAME model as round 3
    c = ar.ledger[0]
    assert c["verdict"] == "disputed"                  # not overturned
    assert c["correlated_analysts"] is True


def test_two_distinct_models_concurring_still_overturn():
    ar, _ = _round3(
        "```python\nresult = {'rho': 0.21}\n```\nSUPPORTS: NO",
        {"result = {'rho': 0.21}": {"rho": 0.21}},
        _claim(reps=[{"round": 2, "code": "x", "result": {"rho": 0.2},
                      "agrees": False, "analyst": "contradicts"}]))  # by=analyst-2
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
                                   "agrees": False, "usable": True, "by": "old-model"}]}],
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
                                   "agrees": False, "usable": True, "by": "old-model"}]}],
        comps=dict(_COMPS))
    asyncio.run(ar.replicate(fresh=False))
    assert len(ar.ledger[0]["replications"]) == 2


def test_an_assertion_that_fails_replication_is_withheld_from_the_prose():
    """A claim can pass verification and still lose a value to independent
    re-derivation; the writer has to be told, or it restates it as solid."""
    claim = _claim(verdict="verified", value="corr=-0.34")
    claim["assertions"] = [{"label": "n_asvs", "value": "24", "of": ""},
                           {"label": "corr", "value": "-0.34", "of": ""}]
    claim["replications"] = [{"round": 2, "by": "analyst-2", "usable": True, "code": "x",
                              "roll": "mixed",
                              "assertion_verdicts": {"n_asvs": "agrees", "corr": "differs"},
                              "judgment": {"notes": {"corr": "I get -0.12"}}}]
    ar = _replicating_researcher("", {}, ledger=[claim], comps=dict(_COMPS))
    ar.ledger[0]["verdict"] = ar._resolve_verdict(ar.ledger[0])
    c = ar.ledger[0]
    assert c["verdict"] == "partial"
    assert c["unsupported_numbers"] == ["corr=-0.34"]      # the failed one, not the good one


def test_refused_claims_are_counted():
    """A claimant that cannot produce checkable assertions otherwise just looks
    unproductive — a different problem with a different fix."""
    ar = Autoresearcher(_StubData(), LLMClient(None, "m"), _StubExecutor({}))
    r = asyncio.run(ar._exec_tool("record_claim", {
        "statement": "the community looked diverse", "antecedents": ["c1"]}))
    assert r["recorded"] is False and "assertions" in r["error"]
    assert ar.ledger == []
    assert ar.run_summary(completed=True)["claims_refused"] == 1


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


class _ScriptedClient:
    """A chat stub driven by a per-PHASE script of turns, dispatched on the system
    prompt. A turn is either text (no tool calls) or a list of ``(tool, args)``.

    Hyphal growth runs three different short contexts, so what matters in these tests
    is not call order overall but which phase a call belongs to and what it was seeded
    with — every call is kept in ``self.calls`` for that."""

    def __init__(self, germinate=(), tip=(), sweep=()):
        self.script = {"germinate": list(germinate), "tip": list(tip), "sweep": list(sweep)}
        self.used = {k: 0 for k in self.script}
        self.calls = []                      # (phase, messages, tools) per invocation
        self.chat = self._Chat(self)

    def _phase(self, system):
        return {GERMINATE_SYSTEM: "germinate", TIP_SYSTEM: "tip",
                SWEEP_SYSTEM: "sweep"}.get(system, "other")

    class _Chat:
        def __init__(self, outer):
            self.completions = _ScriptedClient._Completions(outer)

    class _Completions:
        def __init__(self, outer):
            self.outer = outer

        async def create(self, **kw):
            o = self.outer
            phase = o._phase(kw["messages"][0]["content"])
            o.calls.append((phase, list(kw["messages"]),
                            [t["function"]["name"] for t in (kw.get("tools") or [])]))
            turns = o.script.get(phase) or []
            i = o.used.get(phase, 0)
            o.used[phase] = i + 1
            turn = turns[i] if i < len(turns) else "DONE"
            if callable(turn):
                turn = turn(kw["messages"])
            return _reply(turn, i)


def _reply(turn, i):
    """Build the OpenAI-shaped response object `_agent_loop` reads."""
    class _Fn:
        def __init__(self, name, args):
            self.name, self.arguments = name, json.dumps(args)

    class _TC:
        def __init__(self, k, name, args):
            self.id, self.type, self.function = f"t{i}_{k}", "function", _Fn(name, args)

        def model_dump(self):
            return {"id": self.id, "type": "function",
                    "function": {"name": self.function.name,
                                 "arguments": self.function.arguments}}

    class M:
        content = turn if isinstance(turn, str) else ""
        tool_calls = (None if isinstance(turn, str)
                      else [_TC(k, n, a) for k, (n, a) in enumerate(turn)])

    class C:
        message = M()

    class R:
        choices = [C()]
    return R()


_AGENDA2 = [("propose_agenda", {"items": [{"question": "Q1", "rationale": "R1"},
                                          {"question": "Q2", "rationale": "R2"}]})]


def _claim_call(label, value, statement="a finding"):
    """A record_claim tool call for the scripted client. Named apart from the
    `_claim` fixture above — a module-level redefinition silently rebinds it for
    every test in the file, which took out the whole round-3 suite once."""
    return ("record_claim", {"statement": statement, "kind": "pattern",
                             "assertions": [{"label": label, "value": value}]})


def _hyphal(client, results=None):
    return Autoresearcher(_StubData(), LLMClient(client, "stub-model"),
                          _StubExecutor(results or {}))


class TestHyphalGrowth:
    """#58 — branching short-lived tips instead of one long-lived session. The state
    lives on the researcher, so what has to hold is that each tip is seeded from that
    state, attributes its work to its own branch, and cannot fake completion."""

    def test_germination_cannot_start_analysing(self):
        """Given run_analysis, the planning phase runs the investigation it was
        supposed to be planning — so it is not given run_analysis."""
        names = {t["function"]["name"] for t in GERMINATE_TOOLS}
        assert "propose_agenda" in names
        assert not names & {"run_analysis", "record_claim", "add_followup", "mark_done"}

    def test_a_tip_cannot_repropose_the_agenda(self):
        names = {t["function"]["name"] for t in TIP_TOOLS}
        assert "propose_agenda" not in names
        assert {"run_analysis", "record_claim", "mark_done", "add_followup"} <= names

    def test_the_sweep_records_assumptions_and_nothing_else(self):
        names = {t["function"]["name"] for t in SWEEP_TOOLS}
        assert names == {"get_agenda", "record_assumption"}

    def test_each_tip_starts_from_a_fresh_context(self):
        """The whole point: a tip's context is seed-sized, not run-sized."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("n_asvs", "735")], [("mark_done", {})],
                                 [("mark_done", {})]])
        asyncio.run(_hyphal(c).explore_hyphal(tip_steps=4))
        firsts = [m for phase, m, _ in c.calls if phase == "tip" and len(m) == 2]
        assert len(firsts) == 2                     # one fresh opening per tip
        # the second tip never sees the first tip's turns, only its recorded claim
        second = firsts[1][1]["content"]
        assert "a finding" in second and "record_claim" not in second

    def test_a_tip_attributes_its_claims_to_its_own_investigation(self):
        """With items still pending, the linear rule would attribute a claim to the
        first pending item — the wrong branch. A live tip owns its item outright."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [("mark_done", {})],
                                 [_claim_call("y", "2")], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4))
        assert [k["investigation"] for k in ar.ledger] == ["a1", "a2"]

    def test_a_claim_recorded_after_mark_done_stays_on_its_branch(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("mark_done", {}), _claim_call("x", "1")],
                                 [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4))
        assert ar.ledger[0]["investigation"] == "a1"

    def test_a_tip_does_not_promote_the_next_item(self):
        """mark_done in the linear loop advances to the next item. A tip must not —
        it will never see that item, and leaving it in_progress strands it."""
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        asyncio.run(ar._grow_tip(item, max_steps=2))
        assert [a["status"] for a in ar.agenda] == ["done", "pending"]

    def test_followups_branch_depth_first(self):
        a = [{"id": "a1", "question": "Q1", "status": "done", "parent": None},
             {"id": "a2", "question": "Q2", "status": "pending", "parent": None},
             {"id": "a3", "question": "Q3", "status": "pending", "parent": "a1"}]
        ar = _hyphal(_ScriptedClient())
        ar.agenda = a
        assert ar._next_tip("a1")["id"] == "a3"      # grow from the branch just finished
        assert ar._next_tip("a2")["id"] == "a2"      # no child: fall back to agenda order
        assert ar._next_tip(None)["id"] == "a2"

    def test_a_followup_is_seeded_with_its_parents_findings(self):
        ar = _hyphal(_ScriptedClient())
        ar.agenda = [{"id": "a1", "question": "parent Q", "status": "done", "parent": None},
                     {"id": "a2", "question": "child Q", "status": "pending", "parent": "a1"}]
        ar.ledger = [{"id": "k1", "statement": "parent found this", "value": "n=7",
                      "kind": "pattern", "investigation": "a1"},
                     {"id": "k2", "statement": "someone else found this", "value": "n=9",
                      "kind": "pattern", "investigation": "a9"}]
        seed = asyncio.run(ar._tip_seed(ar.agenda[1]))
        assert "child Q" in seed and "branched off" in seed and "parent Q" in seed
        # the ancestor's claim is offered to build on; the unrelated one only as context
        before_context = seed.split("Findings from the other investigations")[0]
        assert "parent found this" in before_context
        assert "someone else found this" not in before_context
        assert "someone else found this" in seed

    def test_an_unfinished_tip_leaves_its_item_outstanding(self):
        """A tip that runs out of steps without mark_done is interrupted work. Marking
        it done anyway is how a partial run comes to look complete."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("run_analysis", {"code": "x"})]] * 8)
        ar = _hyphal(c)
        completed = asyncio.run(ar.explore_hyphal(tip_steps=2))
        assert not completed
        assert [a["status"] for a in ar.agenda] == ["interrupted", "interrupted"]

    def test_germination_that_proposes_nothing_is_a_failed_run(self):
        """A real run burned its germination budget without calling propose_agenda,
        then ran all six downstream phases on an empty ledger and printed
        "0/0 investigations done" with no INCOMPLETE marker — nothing outstanding is
        vacuously true when there is nothing."""
        c = _ScriptedClient(germinate=["thinking about it"] * 40)
        ar = _hyphal(c)
        assert asyncio.run(ar.explore_hyphal(tip_steps=4)) is False
        assert ar.run_summary()["completed"] is False

    def test_germination_retries_with_only_the_one_tool_it_needs(self):
        """The usual failure is a model spending its whole budget inspecting the data
        it was asked to plan over, so the retry takes that option away."""
        c = _ScriptedClient(germinate=[[("list_datasets", {})], [("list_datasets", {})],
                                       [("list_datasets", {})], [("list_datasets", {})],
                                       _AGENDA2])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4))
        offered = [tools for phase, _, tools in c.calls if phase == "germinate"]
        assert offered[-1] == ["propose_agenda"]      # the retry, stripped down
        assert len(ar.agenda) == 2

    def test_an_empty_agenda_is_never_reported_complete(self):
        ar = _hyphal(_ScriptedClient())
        ar.agenda = []
        assert ar.run_summary()["completed"] is False

    def test_a_worked_agenda_reports_complete(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[("mark_done", {})]] * 4)
        ar = _hyphal(c)
        assert asyncio.run(ar.explore_hyphal(tip_steps=3))
        assert ar.run_summary()["exploration"] == "hyphal"

    def test_the_step_budget_is_a_budget(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[("run_analysis", {"code": "x"})]] * 40)
        ar = _hyphal(c)
        completed = asyncio.run(ar.explore_hyphal(tip_steps=3, max_total_steps=5))
        assert not completed
        assert sum(1 for phase, _, _ in c.calls if phase == "tip") <= 5

    def test_the_sweep_sees_every_claim_at_once(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1", "first thing")], [("mark_done", {})],
                                 [_claim_call("y", "2", "second thing")], [("mark_done", {})]],
                            sweep=[[("record_assumption", {"statement": "counts are raw"})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4))
        seed = next(m for phase, m, _ in c.calls if phase == "sweep")[1]["content"]
        assert "first thing" in seed and "second thing" in seed
        assert [a["statement"] for a in ar.assumptions] == ["counts are raw"]

    def test_the_events_a_watcher_snapshots_on_are_actually_emitted(self):
        """The bench publishes the run's state on these events. If one is renamed and
        nothing notices, a run goes back to leaving no record until it finishes."""
        seen = []

        async def on_progress(event, detail):
            seen.append(event)

        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [("mark_done", {})],
                                 [("add_followup", {"question": "deeper?"})],
                                 [("mark_done", {})], [("mark_done", {})]],
                            sweep=[[("record_assumption", {"statement": "raw counts"})]])
        ar = _hyphal(c)
        ar.on_progress = on_progress
        asyncio.run(ar.explore_hyphal(tip_steps=4))
        assert {"germinate", "tip", "tip_done", "record_claim", "add_followup",
                "sweep", "hyphal_done"} <= set(seen)
        assert seen.index("tip") < seen.index("tip_done")     # a tip opens before it closes

    def test_a_parent_cycle_does_not_hang_the_ancestry_walk(self):
        ar = _hyphal(_ScriptedClient())
        ar.agenda = [{"id": "a1", "question": "Q1", "status": "pending", "parent": "a2"},
                     {"id": "a2", "question": "Q2", "status": "pending", "parent": "a1"}]
        assert [a["id"] for a in ar._ancestry(ar.agenda[0])] == ["a1", "a2"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
