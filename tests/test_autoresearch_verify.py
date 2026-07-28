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


class TestTruncatedJudgeIsNotUnverifiable:
    """A claim whose evidence re-executed perfectly was recorded `unverifiable` because
    the judge ran out of tokens mid-sentence. k22's judge emitted "n_batch_418 =" — the
    label welded to its value — so only 2 of 8 assertions mapped, both not_addressed.
    Saying "unverifiable" blames the claim for the judge's failure."""

    def _ar(self, per, truncated, n_assertions=8):
        import asyncio
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        ar.computations["c1"] = {"label": "x", "code": "result = 1", "result": 1}
        claim = {"id": "k1", "statement": "s", "antecedents": ["c1"],
                 "assertions": [{"label": f"a{i}", "value": str(i)}
                                for i in range(n_assertions)]}
        ar.ledger = [claim]

        class _C:
            client = object()
        ar.client_for = lambda role: _C()

        async def _judge(*a, **k):
            return {"per": per, "notes": {}, "by": "m", "truncated": truncated,
                    "graded_nothing": not per}
        ar._judge = _judge
        asyncio.run(ar._verify_one(claim))
        return claim

    def test_a_truncated_judge_that_addressed_nothing_is_a_judge_failure(self):
        c = self._ar({"mangled_label": "not_addressed"}, truncated=True)
        assert c.get("verdict") is None, "must not grade the claim"
        assert c["method"] == "judge-failed"

    def test_most_assertions_ungraded_is_a_judge_failure_even_untruncated(self):
        c = self._ar({"a0": "not_addressed"}, truncated=False, n_assertions=8)
        assert c.get("verdict") is None and c["method"] == "judge-failed"

    def test_a_judge_that_addressed_everything_still_says_unverifiable(self):
        """When the judge really did engage and found the evidence silent, that IS a
        property of the claim and must keep its verdict."""
        per = {f"a{i}": "not_addressed" for i in range(8)}
        c = self._ar(per, truncated=False, n_assertions=8)
        assert c["verdict"] == "unverifiable" and c["method"] == "judged"

    def test_a_real_grade_is_untouched(self):
        per = {f"a{i}": "supported" for i in range(8)}
        c = self._ar(per, truncated=False, n_assertions=8)
        assert c["verdict"] == "verified"


class TestJudgeRetryBudget:
    """The judge retry fires because the first call ran out of room, and was then given
    a fifth of that room. Measured against the live verify model: 1024 tokens of
    reasoning does not reach a verdict on the simplest possible question, so the 1200
    cap could not succeed."""

    def test_the_retry_gets_at_least_the_original_budget(self):
        import inspect
        from ai.autoresearch import Autoresearcher
        src = inspect.getsource(Autoresearcher._judge)
        assert "min(1200, self.max_tokens)" not in src, \
            "the retry must not be given less room than the call that overran"
        # Both the first call and the retry ask for the full budget.
        assert src.count("max_tokens=self.max_tokens") >= 2


class TestSweepSeesTheParameters:
    """Shown only the claim statements, the sweep guessed at the knobs and said so:
    "999 or 9999 permutations", "pseudocount (e.g., +1)". The pseudocount guess was
    wrong — the code uses 0.5 — so the assumption record held a false value."""

    def _ar(self):
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        ar.computations["c1"] = {"label": "x", "result": 1, "code":
                                 "a = clr(counts, pseudocount=0.5)\n"
                                 "r = permanova(a, g, permutations=999)"}
        ar.ledger = [{"id": "k1", "statement": "s", "antecedents": ["c1"],
                      "assertions": [{"label": "F", "value": "3.4"}]}]
        return ar

    def _seed(self, ar):
        import asyncio
        seen = {}

        async def _loop(**kw):
            seen.update(kw)
            return 0
        ar._agent_loop = _loop
        asyncio.run(ar._sweep_assumptions(max_steps=2))
        return seen.get("seed", "")

    def test_the_actual_parameter_values_reach_the_sweep(self):
        seed = self._seed(self._ar())
        assert "pseudocount = 0.5" in seed and "permutations = 999" in seed
        assert "do not guess" in seed

    def test_expressions_are_not_offered_as_values(self):
        """The extractor also catches `depth=sub.sum(axis=1` — a variable name is not a
        recorded parameter, it is more to look up."""
        ar = self._ar()
        ar.computations["c1"]["code"] += "\nrarefy(counts, depth=sub.sum(axis=1).min())"
        seed = self._seed(ar)
        assert "sub.sum" not in seed


class TestDuplicateClaimBySubset:
    """The duplicate guard has now been beaten four ways: by respelling, by synonyms, by
    word order, and — this one — by subtraction. a10 banked k21 with nine assertions,
    then k23 with five of the same nine, same labels, same values."""

    def _ar(self):
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        return Autoresearcher(DirDataSource(d, study={}, overview=None),
                              LLMClient(None, "x"), SubprocessExecutor(d))

    def _record(self, ar, labels_values):
        import asyncio
        return asyncio.run(ar._exec_tool("record_claim", {
            "statement": "a statement", "antecedents": ["c1"],
            "assertions": [{"label": l, "value": v, "of": "x"}
                           for l, v in labels_values]}))

    def test_a_strict_subset_of_an_earlier_claim_is_refused(self):
        ar = self._ar()
        ar.computations["c1"] = {"label": "c", "code": "result = 1", "result": 1}
        nine = [("n_asvs_batch1", "418"), ("n_asvs_batch2", "317"),
                ("n_shared_asvs", "0"), ("domain_batch1", "Bacteria"),
                ("domain_batch2", "Eukaryota")]
        assert self._record(ar, nine)["recorded"]
        five = [("n_shared_asvs", "0"), ("n_batch1_asvs", "418"),
                ("domain_batch1", "Bacteria")]
        out = self._record(ar, five)
        assert not out["recorded"]
        assert "every value you are asserting here" in out["error"]

    def test_an_exact_repeat_still_says_exactly(self):
        ar = self._ar()
        ar.computations["c1"] = {"label": "c", "code": "result = 1", "result": 1}
        vals = [("n_shared_asvs", "0"), ("domain_batch1", "Bacteria")]
        assert self._record(ar, vals)["recorded"]
        out = self._record(ar, vals)
        assert not out["recorded"] and "exactly these values" in out["error"]

    def test_one_new_assertion_is_enough_to_get_recorded(self):
        """Containment must not block a claim that genuinely adds something."""
        ar = self._ar()
        ar.computations["c1"] = {"label": "c", "code": "result = 1", "result": 1}
        assert self._record(ar, [("n_shared_asvs", "0")])["recorded"]
        assert self._record(ar, [("n_shared_asvs", "0"),
                                 ("within_batch_R2", "0.4294")])["recorded"]


class TestEpochBoundary:
    """Epoch 2 re-opened a1 — an investigation closed at its claim cap in epoch 1 —
    every single round, and would have done so again in epoch 3."""

    def _ar(self):
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        d = "/data/dev/testdata/1543a4c1"
        return Autoresearcher(DirDataSource(d, study={}, overview=None),
                              LLMClient(None, "x"), SubprocessExecutor(d))

    def test_the_first_agenda_promotes_its_first_item(self):
        """The linear loop relies on this."""
        import asyncio
        ar = self._ar()
        asyncio.run(ar._exec_tool("propose_agenda", {"items": [
            {"question": "q1"}, {"question": "q2"}]}))
        assert ar.agenda[0]["status"] == "in_progress"

    def test_a_later_agenda_does_not_reopen_a_closed_investigation(self):
        """agenda[0] on a later epoch is a1, long since finished. Germination's cleanup
        turns in_progress back into pending, so promoting it here silently resurrected
        it and cost a whole tip re-capping the same item."""
        import asyncio
        ar = self._ar()
        asyncio.run(ar._exec_tool("propose_agenda", {"items": [{"question": "q1"}]}))
        ar.agenda[0]["status"] = "interrupted"          # closed at its claim cap
        asyncio.run(ar._exec_tool("propose_agenda", {"items": [{"question": "q2"}]}))
        assert ar.agenda[0]["status"] == "interrupted", "a1 must stay closed"
        assert ar.agenda[1]["status"] == "pending"


class TestTruncationSpiral:
    """One investigation burned three consecutive steps being cut off. Each cut-off
    reply was appended to the transcript, so the context grew by a paragraph that went
    nowhere and the next reply was likelier to be cut off in turn."""

    def _loop(self, finish_reasons):
        """Drive _agent_loop against a client that returns the given finish_reasons."""
        import asyncio
        from types import SimpleNamespace
        from ai.autoresearch import (Autoresearcher, DirDataSource, LLMClient,
                                     SubprocessExecutor)
        seen = []

        class _Client:
            def __init__(self): self.i = 0
            async def chat(self, *a, **k):
                seen.append([dict(m) for m in k.get("messages") or a[0]])
                fr = finish_reasons[min(self.i, len(finish_reasons) - 1)]
                self.i += 1
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason=fr,
                    message=SimpleNamespace(content="thinking " * 50, tool_calls=None))])

        d = "/data/dev/testdata/1543a4c1"
        ar = Autoresearcher(DirDataSource(d, study={}, overview=None),
                            LLMClient(None, "x"), SubprocessExecutor(d))
        ar._chat = lambda role, messages, **kw: _Client.chat(c, messages=messages, **kw)
        c = _Client()
        asyncio.run(ar._agent_loop(system="s", seed="q", tools=[],
                                   max_steps=len(finish_reasons), nudge="n", tag="t"))
        return seen

    def test_the_first_cutoff_keeps_the_text_and_asks_for_brevity(self):
        seen = self._loop(["length", "length"])
        assert any(m["role"] == "assistant" and "thinking" in (m.get("content") or "")
                   for m in seen[1]), "first cut-off text should be kept"
        assert "cut off at the token limit" in seen[1][-1]["content"]

    def test_a_repeat_cutoff_drops_the_dead_text(self):
        """The second cut-off paragraph is what tightens the spiral, so it must not be
        carried forward."""
        seen = self._loop(["length", "length", "length"])
        before = sum("thinking" in (m.get("content") or "") for m in seen[1])
        after = sum("thinking" in (m.get("content") or "") for m in seen[2])
        assert after == before, "a repeat cut-off must not add another dead paragraph"
        assert "Cut off again" in seen[2][-1]["content"]


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


def _claim_call(label, value, statement="a finding", antecedents=()):
    """A record_claim tool call for the scripted client. Named apart from the
    `_claim` fixture above — a module-level redefinition silently rebinds it for
    every test in the file, which took out the whole round-3 suite once."""
    return ("record_claim", {"statement": statement, "kind": "pattern",
                             "antecedents": list(antecedents),
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


class TestPromptsDoNotDictateTheAgenda:
    """The germination prompt used to enumerate the toolkit — alpha diversity, then
    beta diversity, then core taxa, then a contamination screen naming five genera —
    and the model handed back that list, in that order, every run. An agenda that
    matches the prescription is the prompt's agenda, not the dataset's."""

    TECHNIQUES = ("Shannon", "Simpson", "Pielou", "Bray-Curtis", "Jaccard",
                  "Ralstonia", "Bradyrhizobium", "Cutibacterium", "Pelomonas", "Delftia")

    def test_germination_does_not_enumerate_a_checklist(self):
        from ai.autoresearch import GERMINATE_SYSTEM
        assert not [t for t in self.TECHNIQUES if t in GERMINATE_SYSTEM]

    def test_the_linear_driver_does_not_either(self):
        from ai.autoresearch import EXPLORE_SYSTEM
        assert not [t for t in self.TECHNIQUES if t in EXPLORE_SYSTEM]

    def test_germination_still_says_to_read_the_data_first(self):
        """Removing the checklist only helps if something replaces it. What replaces
        it is the data — otherwise a weaker model proposes nothing at all."""
        from ai.autoresearch import GERMINATE_SYSTEM
        assert "LOOK BEFORE YOU PLAN" in GERMINATE_SYSTEM
        assert "list_datasets" in GERMINATE_SYSTEM

    def test_germination_still_requires_self_contained_items(self):
        """Each item is worked by an analyst who never sees the others."""
        from ai.autoresearch import GERMINATE_SYSTEM
        assert "stand on its own" in GERMINATE_SYSTEM


class TestPackageRequests:
    """A ModuleNotFoundError is a wasted step and nothing else. A recorded request is
    evidence about what the sandbox should contain."""

    def test_a_request_is_recorded_and_counted(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("request_package", {"package": "skbio",
                                                       "why": "PERMANOVA"})],
                                 [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=6))
        assert [r["package"] for r in ar.package_requests] == ["skbio"]
        assert ar.package_requests[0]["why"] == "PERMANOVA"

    def test_the_request_attributes_to_the_investigation_that_needed_it(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("request_package", {"package": "skbio"})],
                                 [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=6))
        assert ar.package_requests[0]["investigation"] == "a1"

    def test_the_reply_says_it_is_not_coming_this_run(self):
        """Otherwise an analyst may wait for it, or assume the import will now work."""
        ar = _hyphal(_ScriptedClient())
        r = asyncio.run(ar._exec_tool("request_package", {"package": "skbio"}))
        assert r["available_this_run"] is False
        assert "carry on" in r["note"]

    def test_repeat_requests_are_counted_not_deduplicated(self):
        """Three analysts each reaching for the same package is the signal."""
        ar = _hyphal(_ScriptedClient())
        for _ in range(3):
            r = asyncio.run(ar._exec_tool("request_package", {"package": "skbio"}))
        assert r["times_requested_this_run"] == 3
        assert ar.run_summary()["packages_requested"] == {"skbio": 3}

    def test_a_nameless_request_is_refused(self):
        ar = _hyphal(_ScriptedClient())
        r = asyncio.run(ar._exec_tool("request_package", {"why": "stats"}))
        assert r["recorded"] is False

    def test_the_summary_ranks_by_how_many_analysts_wanted_it(self):
        ar = _hyphal(_ScriptedClient())
        for pkg in ("skbio", "statsmodels", "skbio", "skbio", "statsmodels"):
            asyncio.run(ar._exec_tool("request_package", {"package": pkg}))
        assert list(ar.run_summary()["packages_requested"]) == ["skbio", "statsmodels"]

    def test_planning_and_the_sweep_cannot_request_packages(self):
        from ai.autoresearch import GERMINATE_TOOLS, SWEEP_TOOLS, TIP_TOOLS
        names = lambda ts: {t["function"]["name"] for t in ts}   # noqa: E731
        assert "request_package" in names(TIP_TOOLS)
        assert "request_package" not in names(GERMINATE_TOOLS)
        assert "request_package" not in names(SWEEP_TOOLS)


class TestPackageGranting:
    """The allowlist is the whole security control: a model names a package and, if the
    name matches, pip runs against the interpreter the sandbox executes in."""

    def test_the_model_string_is_a_lookup_never_an_argument(self, monkeypatch):
        """Nothing the model writes may reach the command line — no version pins, no
        extras, no index URLs, no VCS or local paths."""
        from ai import sandbox_packages as sp
        calls = []
        monkeypatch.setattr(sp.subprocess, "run",
                            lambda cmd, **k: calls.append(cmd) or _Proc())
        monkeypatch.setattr(sp, "is_available", lambda n: False)
        for hostile in ("skbio==0.5.9", "skbio --index-url http://elsewhere",
                        "skbio; rm -rf /", "git+https://example/skbio", "./skbio",
                        "skbio[all]", "requests"):
            out = sp.install(hostile)
            assert out["installed"] is False
            assert out["reason"] == "not on the sandbox allowlist"
        assert calls == []                       # pip was never invoked

    def test_the_distribution_name_comes_from_the_file(self, monkeypatch):
        """The import name and the PyPI name differ; the request supplies neither."""
        from ai import sandbox_packages as sp
        calls = []
        monkeypatch.setattr(sp.subprocess, "run",
                            lambda cmd, **k: calls.append(cmd) or _Proc())
        monkeypatch.setattr(sp, "is_available", lambda n: n == "skbio" and bool(calls))
        sp.install("skbio")
        assert calls[0][-1] == "scikit-bio"
        assert calls[0][:5] == [sys.executable, "-m", "pip", "install", "--quiet"]

    def test_an_already_present_package_is_not_reinstalled(self, monkeypatch):
        from ai import sandbox_packages as sp
        monkeypatch.setattr(sp.subprocess, "run",
                            lambda *a, **k: pytest.fail("should not have run pip"))
        monkeypatch.setattr(sp, "is_available", lambda n: True)
        assert sp.install("skbio") == {"installed": False, "available": True,
                                       "reason": "already available"}

    def test_a_failed_install_leaves_the_analyst_where_it_was(self, monkeypatch):
        from ai import sandbox_packages as sp
        monkeypatch.setattr(sp, "is_available", lambda n: False)
        monkeypatch.setattr(sp.subprocess, "run",
                            lambda *a, **k: _Proc(rc=1, err="no matching distribution"))
        out = sp.install("skbio")
        assert out["installed"] is False and "failed" in out["reason"]

    def test_a_granted_request_tells_the_analyst_it_can_use_it(self):
        ar = _hyphal(_ScriptedClient())
        ar.package_installer = lambda p: {"installed": True, "available": True,
                                          "reason": "installed"}
        r = asyncio.run(ar._exec_tool("request_package", {"package": "skbio"}))
        assert r["available_this_run"] is True and "go ahead" in r["note"]
        assert ar.package_requests[0]["installed"] is True

    def test_a_refused_request_is_still_recorded(self):
        """The list should grow by review, not by demand — so refusals are the data."""
        ar = _hyphal(_ScriptedClient())
        ar.package_installer = lambda p: {"installed": False, "available": False,
                                          "reason": "not on the sandbox allowlist"}
        r = asyncio.run(ar._exec_tool("request_package", {"package": "torch"}))
        assert r["available_this_run"] is False
        assert ar.package_requests[0]["reason"] == "not on the sandbox allowlist"

    def test_an_installer_that_throws_does_not_take_the_tip_down(self):
        ar = _hyphal(_ScriptedClient())

        def boom(p):
            raise RuntimeError("pip exploded")

        ar.package_installer = boom
        r = asyncio.run(ar._exec_tool("request_package", {"package": "skbio"}))
        assert r["recorded"] is True and r["available_this_run"] is False

    def test_with_no_installer_nothing_is_granted(self):
        ar = _hyphal(_ScriptedClient())
        r = asyncio.run(ar._exec_tool("request_package", {"package": "skbio"}))
        assert r["available_this_run"] is False


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class TestBankBeforeTheCap:
    """A tip spent all fourteen steps and eight successful computations on a three-part
    investigation and recorded NOTHING — still working towards a complete answer when
    the budget ran out. Its seed already said to record one claim and stop; nothing
    noticed that it hadn't."""

    ANALYSE = ("run_analysis", {"code": "code_a", "label": "x"})

    def test_a_tip_with_no_claim_is_prodded_at_the_halfway_point(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[self.ANALYSE]] * 20)
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        asyncio.run(ar._grow_tip(item, max_steps=8, one_claim=True))
        prods = [m for _p, msgs, _t in c.calls for m in msgs
                 if m.get("role") == "user" and "no claim recorded" in (m.get("content") or "")]
        assert prods, "never prodded"
        assert "carry on from there" in prods[0]["content"]

    def test_the_prod_happens_once_not_every_step(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[self.ANALYSE]] * 20)
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar._germinate())
        asyncio.run(ar._grow_tip(ar._next_tip(None), max_steps=10, one_claim=True))
        last = [msgs for _p, msgs, _t in c.calls][-1]
        assert sum(1 for m in last if "no claim recorded" in (m.get("content") or "")) == 1

    def test_a_tip_that_already_banked_a_claim_is_left_alone(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")]] + [[self.ANALYSE]] * 10)
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar._germinate())
        asyncio.run(ar._grow_tip(ar._next_tip(None), max_steps=8, one_claim=True))
        assert not [m for _p, msgs, _t in c.calls for m in msgs
                    if "no claim recorded" in (m.get("content") or "")]

    def test_the_linear_driver_is_not_prodded(self):
        """one_claim=False means a tip is meant to work the whole investigation."""
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[self.ANALYSE]] * 20)
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar._germinate())
        asyncio.run(ar._grow_tip(ar._next_tip(None), max_steps=8, one_claim=False))
        assert not [m for _p, msgs, _t in c.calls for m in msgs
                    if "no claim recorded" in (m.get("content") or "")]


class TestAssumptionsAndParameters:
    """Zero assumptions were recorded across an entire evening of runs. The linear
    driver forced a sweep before it could finish; claim-sized contexts die at the claim
    boundary, so nothing ever asked — a regression the hyphal redesign introduced."""

    def test_every_epoch_ends_with_an_assumptions_sweep(self):
        second = [("propose_agenda", {"items": [{"question": "Q3"}]})]
        c = _ScriptedClient(germinate=[_AGENDA2, second],
                            tip=[[_claim_call("x", "1")], [("mark_done", {})]] * 4,
                            sweep=[[("record_assumption", {"statement": "counts are raw"})],
                                   [("record_assumption", {"statement": "groups inferred"})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4, epochs=2))
        assert sum(1 for phase, _, _ in c.calls if phase == "sweep") >= 2
        assert len(ar.assumptions) == 2

    def test_a_single_epoch_still_sweeps(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [("mark_done", {})],
                                 [("mark_done", {})]],
                            sweep=[[("record_assumption", {"statement": "raw counts"})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=3))
        assert [a["statement"] for a in ar.assumptions] == ["raw counts"]

    def test_a_run_with_no_claims_has_nothing_to_sweep(self):
        """Asking what a run assumed when it found nothing is a wasted model call."""
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[("mark_done", {})]] * 4,
                            sweep=[[("record_assumption", {"statement": "unused"})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=3))
        assert ar.assumptions == []

    def test_a_claim_with_no_parameters_is_told_so(self):
        """An analyst correlated diversity against an 'environmental harshness' ranking
        it invented and recorded nowhere. Four plausible orderings give r between -0.22
        and +0.20 against a claimed -0.39, so no replicator could reproduce it."""
        ar = _hyphal(_ScriptedClient())
        r = asyncio.run(ar._exec_tool("record_claim", {
            "statement": "richness falls with harshness", "kind": "pattern",
            "assertions": [{"label": "rho", "value": "-0.39"}]}))
        assert r["recorded"] is True
        assert "`parameters` is empty" in r["note"]
        assert "record_assumption" in r["note"]

    def test_the_note_names_the_choices_the_code_actually_made(self):
        """Asking for parameters in the abstract failed three times in one evening — a
        Shannon log base, a power-law fitting method, an invented ordinal scale — each
        a correct claim made unreproducible by one unstated choice."""
        ar = _hyphal(_ScriptedClient())
        ar.computations["c1"] = {"label": "l", "by": "m",
                                 "code": "sh = entropy(p, base=2)\n"
                                         "D = pdist(X, metric='braycurtis')",
                                 "result": {}}
        r = asyncio.run(ar._exec_tool("record_claim", {
            "statement": "shannon differs", "kind": "pattern", "antecedents": ["c1"],
            "assertions": [{"label": "shannon", "value": "4.59"}]}))
        assert "base=2" in r["note"] and "metric=braycurtis" in r["note"]
        assert "will read as refuted" in r["note"]

    def test_a_claim_whose_code_chose_nothing_gets_the_general_note(self):
        ar = _hyphal(_ScriptedClient())
        ar.computations["c1"] = {"label": "l", "by": "m",
                                 "code": "result = {'n': int(counts.shape[0])}",
                                 "result": {}}
        r = asyncio.run(ar._exec_tool("record_claim", {
            "statement": "n samples", "kind": "observation", "antecedents": ["c1"],
            "assertions": [{"label": "n", "value": "63"}]}))
        assert "`parameters` is empty" in r["note"] and "base=" not in r["note"]

    def test_a_claim_that_declares_its_knobs_is_left_alone(self):
        ar = _hyphal(_ScriptedClient())
        r = asyncio.run(ar._exec_tool("record_claim", {
            "statement": "richness falls with harshness", "kind": "pattern",
            "parameters": {"harshness_rank": "seawater<ice<frost<brine<air"},
            "assertions": [{"label": "rho", "value": "-0.39"}]}))
        assert "note" not in r

    def test_the_note_does_not_leak_into_the_stored_claim(self):
        ar = _hyphal(_ScriptedClient())
        asyncio.run(ar._exec_tool("record_claim", {
            "statement": "s", "kind": "pattern",
            "assertions": [{"label": "rho", "value": "-0.39"}]}))
        assert "_no_parameters" not in ar.ledger[0]


class TestDuplicateClaims:
    """A successor context re-derived its predecessor's claim and recorded it word for
    word. Claim-sized contexts make this the DEFAULT failure rather than an oddity:
    each starts fresh, works the same question, and reaches the same answer."""

    CALL = {"statement": "chi2 says no bias", "kind": "pattern",
            "assertions": [{"label": "chi2", "value": "3.8533"},
                           {"label": "p", "value": "0.4262"}]}

    def _ar(self):
        return _hyphal(_ScriptedClient())

    def test_the_same_assertions_twice_is_refused(self):
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim", self.CALL))
        assert r["recorded"] is False and "k1 already asserts" in r["error"]
        assert len(ar.ledger) == 1

    def test_rewording_the_statement_does_not_get_past_it(self):
        """The successor rewrites the prose; the numbers are what identify a claim."""
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim",
                                      dict(self.CALL, statement="entirely different words")))
        assert r["recorded"] is False

    def test_going_further_is_allowed(self):
        """Refusing anything that overlaps would stop an investigation progressing."""
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim", dict(
            self.CALL, assertions=self.CALL["assertions"] + [{"label": "dof", "value": "4"}])))
        assert r["recorded"] is True

    def test_a_different_value_for_the_same_label_is_now_refused(self):
        """This asserted the opposite until four successor contexts recorded four
        different PERMANOVA F values for one investigation — 0.0048, 0.39, 22.89 and
        one more — and the identity check passed every one because the numbers DIFFER.
        A ledger holding two values for one quantity is incoherent whatever the
        verifier later says about each in isolation."""
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim", dict(
            self.CALL, assertions=[{"label": "chi2", "value": "9.1"},
                                   {"label": "p", "value": "0.0042"}])))
        assert r["recorded"] is False
        assert "One of them is wrong" in r["error"]

    def test_rounding_is_not_treated_as_a_contradiction(self):
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim", dict(
            self.CALL, assertions=[{"label": "chi2", "value": "3.8534"},
                                   {"label": "p", "value": "0.4262"},
                                   {"label": "dof", "value": "4"}])))
        assert r["recorded"] is True

    def test_renaming_a_label_does_not_slip_the_contradiction_guard(self):
        """A successor recorded F_statistic/R_squared/p_value where an earlier claim
        had F/R2/p. No shared label meant nothing to compare, so a second answer to the
        same question could enter the ledger by renaming its columns."""
        ar = self._ar()
        ar.agenda = [{"id": "a1", "question": "Q", "status": "in_progress", "parent": None}]
        asyncio.run(ar._exec_tool("record_claim", {
            "statement": "permanova", "kind": "pattern",
            "assertions": [{"label": "F", "value": "3.447"}]}))
        r = asyncio.run(ar._exec_tool("record_claim", {
            "statement": "permanova again", "kind": "pattern",
            "assertions": [{"label": "F_statistic", "value": "92.01"}]}))
        assert r["recorded"] is False and "already asserts" in r["error"]

    def test_label_comparison_survives_renaming_and_reordering(self):
        """Each variant slipped the guard in turn: F vs F_statistic (synonym), then
        F_raw vs raw_F (word order). Both put a second answer to the same question on
        the ledger, the second one byte-identical in its prose."""
        from ai.autoresearch import _label_key
        for a, b in [("F", "F_statistic"), ("F", "pseudo_F"), ("R2", "R_squared"),
                     ("p", "p_value"), ("kw_statistic", "KW_H"),
                     ("F_raw", "raw_F"), ("R2_raw", "raw_R2"), ("p_raw", "raw_p"),
                     ("F_rarefied", "rarefied_F"), ("p_frost_vs_ice", "ice_frost_p")]:
            assert _label_key(a) == _label_key(b), f"{a} should match {b}"

    def test_distinct_quantities_stay_distinct(self):
        """Over-merging would refuse legitimate progress, which is the worse failure."""
        from ai.autoresearch import _label_key
        for a, b in [("p", "p_adj"), ("F", "F_frost_ice"), ("F_raw", "F_rarefied"),
                     ("n_samples", "n_groups"), ("R2", "F"), ("p", "kw"),
                     ("brine_richness_mean", "ice_richness_mean")]:
            assert _label_key(a) != _label_key(b), f"{a} must not match {b}"

    def test_a_contradiction_on_a_DIFFERENT_investigation_is_fine(self):
        """Two investigations may legitimately measure the same-named quantity."""
        ar = self._ar()
        ar.agenda = [{"id": "a1", "question": "Q1", "status": "in_progress", "parent": None},
                     {"id": "a2", "question": "Q2", "status": "pending", "parent": None}]
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        ar._active_tip = "a2"
        r = asyncio.run(ar._exec_tool("record_claim", dict(
            self.CALL, assertions=[{"label": "chi2", "value": "9.1"},
                                   {"label": "p", "value": "0.0042"}])))
        assert r["recorded"] is True

    def test_the_refusal_is_counted(self):
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        assert ar.run_summary()["claims_refused"] == 1

    def test_the_refusal_says_what_to_do_instead(self):
        ar = self._ar()
        asyncio.run(ar._exec_tool("record_claim", self.CALL))
        r = asyncio.run(ar._exec_tool("record_claim", self.CALL))
        assert "further" in r["error"] and "mark_done" in r["error"]


class TestClaimAntecedents:
    """A successor cited `k4` as its antecedent — the natural thing when it was handed
    k4 in its seed — and verification resolved antecedents only against computations and
    data paths. `k4:nopath`, no evidence, graded `unverifiable`: a gap in the checker
    reported as a fault in the claim. Claim-sized contexts make this the norm."""

    def _ar(self):
        ar = _researcher(results={"code_a": {"x": 1}})
        ar.computations = {"c1": {"label": "l", "code": "code_a", "result": {"x": 1}}}
        return ar

    def test_a_claim_antecedent_resolves_to_what_it_rests_on(self):
        ar = self._ar()
        ar.ledger = [{"id": "k1", "statement": "s", "value": "1", "antecedents": ["c1"],
                      "assertions": [{"label": "x", "value": "1", "of": ""}]},
                     {"id": "k2", "statement": "s2", "value": "1", "antecedents": ["k1"],
                      "assertions": [{"label": "x", "value": "1", "of": ""}]}]
        assert ar._resolved_antecedents(ar.ledger[1]) == ["c1"]

    def test_a_chain_of_claims_resolves_through(self):
        ar = self._ar()
        ar.ledger = [{"id": "k1", "statement": "s", "value": "1", "antecedents": ["c1"]},
                     {"id": "k2", "statement": "s", "value": "1", "antecedents": ["k1"]},
                     {"id": "k3", "statement": "s", "value": "1", "antecedents": ["k2"]}]
        assert ar._resolved_antecedents(ar.ledger[2]) == ["c1"]

    def test_a_mix_of_claim_and_computation_keeps_both(self):
        ar = self._ar()
        ar.computations["c2"] = {"label": "l", "code": "code_a", "result": {"x": 1}}
        ar.ledger = [{"id": "k1", "statement": "s", "value": "1", "antecedents": ["c1"]},
                     {"id": "k2", "statement": "s", "value": "1",
                      "antecedents": ["k1", "c2"]}]
        assert sorted(ar._resolved_antecedents(ar.ledger[1])) == ["c1", "c2"]

    def test_a_cycle_does_not_hang(self):
        ar = self._ar()
        ar.ledger = [{"id": "k1", "statement": "s", "value": "1", "antecedents": ["k2"]},
                     {"id": "k2", "statement": "s", "value": "1", "antecedents": ["k1"]}]
        assert ar._resolved_antecedents(ar.ledger[0]) == []

    def test_a_claim_citing_a_claim_is_now_verifiable(self):
        ar = self._ar()
        ar.ledger = [{"id": "k1", "statement": "s", "value": "1", "antecedents": ["c1"],
                      "assertions": [{"label": "x", "value": "1", "of": ""}]},
                     {"id": "k2", "statement": "s2", "value": "1", "antecedents": ["k1"],
                      "assertions": [{"label": "x", "value": "1", "of": ""}]}]
        asyncio.run(ar.verify())
        assert ar.ledger[1]["verdict"] != "unverifiable"
        assert ar.ledger[1]["checked"] == ["c1:run"]


class TestJudgeRetry:
    """glm-4.7-flash spent its whole budget re-reading the instructions back to itself
    and never reached the output format. Five of six claims in one run were graded by
    nobody. More tokens do not fix a model that expands reasoning to fill them."""

    def _client(self, first, second, first_cut=True):
        calls = {"n": 0}

        class _Chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    if kw["messages"][0]["content"] != JUDGE_SYSTEM:
                        raise AssertionError("only the judge should be called here")
                    calls["n"] += 1
                    text = first if calls["n"] == 1 else second

                    class M:
                        content, tool_calls = text, None

                    class C:
                        message = M()
                        finish_reason = "length" if (calls["n"] == 1 and first_cut) else "stop"

                    class R:
                        choices = [C()]
                    return R()

        class _Client:
            chat = _Chat()
        return _Client(), calls

    def _ar(self, client):
        return Autoresearcher(_StubData(), LLMClient(client, "judge-model"),
                              _StubExecutor({}))

    ASSERTIONS = [{"label": "F", "value": "3.447", "of": ""}]

    def test_a_truncated_judge_is_asked_again_for_just_the_lines(self):
        client, calls = self._client(
            "Let me re-read the instructions. The task is to grade...",
            "ASSERTION F: SUPPORTED — the evidence gives 3.447")
        ar = self._ar(client)
        j = asyncio.run(ar._judge(JUDGE_SYSTEM, "claim", "judge-model", self.ASSERTIONS))
        assert calls["n"] == 2
        assert j["per"] == {"F": "supported"}
        assert j["graded_nothing"] is False and j["truncated"] is False

    def test_a_judge_that_answered_first_time_is_not_asked_twice(self):
        client, calls = self._client(
            "ASSERTION F: SUPPORTED — evidence gives 3.447", "unused", first_cut=False)
        ar = self._ar(client)
        j = asyncio.run(ar._judge(JUDGE_SYSTEM, "claim", "judge-model", self.ASSERTIONS))
        assert calls["n"] == 1 and j["per"] == {"F": "supported"}

    def test_a_retry_that_also_fails_is_reported_as_a_judge_failure(self):
        """Better ungraded and retried later than a verdict nobody reached."""
        client, calls = self._client("still thinking...", "still thinking harder...")
        ar = self._ar(client)
        j = asyncio.run(ar._judge(JUDGE_SYSTEM, "claim", "judge-model", self.ASSERTIONS))
        assert calls["n"] == 2 and j["graded_nothing"] is True

    def test_a_judge_cut_off_AFTER_grading_is_left_alone(self):
        """Truncation only matters when it cost us the verdicts."""
        client, calls = self._client(
            "ASSERTION F: SUPPORTED — evidence gives 3.447\\nand furthermore the", "unused")
        ar = self._ar(client)
        j = asyncio.run(ar._judge(JUDGE_SYSTEM, "claim", "judge-model", self.ASSERTIONS))
        assert calls["n"] == 1 and j["per"] == {"F": "supported"}


class TestJudgeFailure:
    """glm-4.7-flash ran out of tokens mid-reasoning and emitted no ASSERTION lines.
    The empty grade rolled up to `unverifiable` — a JUDGE failure reported as a
    property of the CLAIM, which is the one lie this subsystem exists to prevent."""

    ANALYSE = ("run_analysis", {"code": "code_a", "label": "x"})

    def _client(self, judge_reply):
        outer = _ScriptedClient(germinate=[_AGENDA2],
                                tip=[[self.ANALYSE],
                                     [_claim_call("x", "1", antecedents=["c1"])],
                                     [("mark_done", {})], [("mark_done", {})]])
        real = outer.chat.completions.create

        async def create(**kw):
            if kw["messages"][0]["content"] == JUDGE_SYSTEM:
                class M: content, tool_calls = judge_reply, None
                class C: message = M()
                class R: choices = [C()]
                return R()
            return await real(**kw)

        outer.chat.completions.create = create
        return outer

    def test_a_judge_that_graded_nothing_leaves_the_claim_ungraded(self):
        c = self._client("Let me re-read the instructions carefully:")
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        claim = ar.ledger[0]
        assert claim.get("verdict") != "unverifiable"
        assert not claim.get("verdict_round1")
        assert claim.get("method") == "judge-failed"

    def test_the_end_of_run_pass_picks_it_up(self):
        """Leaving it ungraded is only right because something retries it."""
        c = self._client("Let me re-read the instructions carefully:")
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        c.chat.completions.create = _ScriptedClient().chat.completions.create
        ar.llm = LLMClient(_stub_client(judge=SUPPORTED), "stub")
        ar.clients = {}
        asyncio.run(ar.verify())
        assert ar.ledger[0]["verdict_round1"] == "verified"

    def test_a_judge_that_really_says_not_addressed_is_still_unverifiable(self):
        """Silence from the EVIDENCE is a real verdict; silence from the JUDGE is not."""
        c = self._client("ASSERTION x: NOT_ADDRESSED — the evidence says nothing")
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        assert ar.ledger[0]["verdict_round1"] == "unverifiable"


class TestLiveVerification:
    """#61 — the judge runs on the other machine while the analyst explores, and the
    verdicts it returns seed the contexts that come after."""

    def _client(self, tip_turns, judge=SUPPORTED):
        """A scripted analyst whose JUDGE_SYSTEM calls are answered too — live
        verification means both are in flight during the same explore_hyphal call."""
        outer = _ScriptedClient(germinate=[_AGENDA2], tip=tip_turns)
        real = outer.chat.completions.create

        async def create(**kw):
            if kw["messages"][0]["content"] == JUDGE_SYSTEM:
                class M: content, tool_calls = judge, None
                class C: message = M()
                class R: choices = [C()]
                return R()
            return await real(**kw)

        outer.chat.completions.create = create
        return outer

    ANALYSE = ("run_analysis", {"code": "code_a", "label": "x"})

    def test_a_claim_is_judged_while_exploration_continues(self):
        c = self._client([[self.ANALYSE], [_claim_call("x", "1", antecedents=["c1"])],
                          [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        assert ar.ledger[0]["verdict_round1"]        # judged without a batch verify()

    def test_the_batch_pass_does_not_re_judge_what_was_judged_live(self):
        c = self._client([[self.ANALYSE], [_claim_call("x", "1", antecedents=["c1"])],
                          [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        before = ar.ledger[0]["judgment"]
        asyncio.run(ar.verify())
        assert ar.ledger[0]["judgment"] is before    # same object: never re-judged

    def test_a_judge_that_throws_does_not_take_the_run_down(self):
        """An unjudged claim is picked up by the end-of-run pass. A crashed run loses
        everything, so the live verifier must swallow its own failures."""
        outer = _ScriptedClient(germinate=[_AGENDA2],
                                tip=[[self.ANALYSE],
                                     [_claim_call("x", "1", antecedents=["c1"])],
                                     [("mark_done", {})], [("mark_done", {})]])
        real = outer.chat.completions.create

        async def create(**kw):
            if kw["messages"][0]["content"] == JUDGE_SYSTEM:
                raise RuntimeError("judge host fell over")
            return await real(**kw)

        outer.chat.completions.create = create
        ar = _hyphal(outer, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6, live_verify=True))
        assert len(ar.ledger) == 1                   # exploration finished regardless
        assert not ar.ledger[0].get("verdict_round1")

    def test_without_live_verify_nothing_is_judged_during_exploration(self):
        c = self._client([[self.ANALYSE], [_claim_call("x", "1", antecedents=["c1"])],
                          [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c, results={"code_a": {"x": 1}})
        asyncio.run(ar.explore_hyphal(tip_steps=6))
        assert not ar.ledger[0].get("verdict_round1")


class TestVerdictFeedback:
    """What a later analyst is told about an earlier claim."""

    def _ledger(self, verdict, **kw):
        return [{"id": "k1", "statement": "richness tracks depth", "value": "rho=0.63",
                 "kind": "pattern", "investigation": "a1", "verdict_round1": verdict,
                 **kw}]

    def test_a_refuted_claim_carries_why(self):
        ar = _hyphal(_ScriptedClient())
        ar.ledger = self._ledger(
            "refuted", unsupported_numbers=["rho=0.63"],
            assertion_verdicts={"rho": "contradicted"},
            judgment={"notes": {"rho": "the antecedent gives 0.21, not 0.63"}})
        lines = ar._claim_lines(ar.ledger)
        assert "VERDICT: refuted" in lines
        assert "rho=0.63" in lines
        assert "the antecedent gives 0.21" in lines

    def test_a_claim_that_held_carries_the_reasoning_too(self):
        """Judging, replication and adjudication are three different models, so there
        is no single checker for the claimant to learn — and the reasoning is the part
        a later analyst can act on."""
        ar = _hyphal(_ScriptedClient())
        ar.ledger = self._ledger("verified",
                                 judgment={"notes": {"rho": "matches exactly"}})
        lines = ar._claim_lines(ar.ledger)
        assert "VERDICT: verified" in lines
        assert "matches exactly" in lines

    def test_an_unjudged_claim_carries_no_verdict_at_all(self):
        ar = _hyphal(_ScriptedClient())
        ar.ledger = [{"id": "k1", "statement": "s", "value": "v", "kind": "pattern",
                      "investigation": "a1"}]
        assert "VERDICT" not in ar._claim_lines(ar.ledger)


class TestEpochs:
    """#61 — after a round is worked out, a new agenda is proposed by a germinator
    that can see what the last round found."""

    def test_a_second_epoch_proposes_a_second_agenda(self):
        second = [("propose_agenda", {"items": [{"question": "Q3"}]})]
        c = _ScriptedClient(germinate=[_AGENDA2, second],
                            tip=[[("mark_done", {})]] * 6)
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=3, epochs=2))
        assert [a["question"] for a in ar.agenda] == ["Q1", "Q2", "Q3"]

    def test_the_second_germinator_sees_what_the_first_round_found(self):
        second = [("propose_agenda", {"items": [{"question": "Q3"}]})]
        c = _ScriptedClient(germinate=[_AGENDA2, second],
                            tip=[[_claim_call("x", "1", "first round found this")],
                                 [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4, epochs=2))
        seeds = [m[1]["content"] for phase, m, _ in c.calls
                 if phase == "germinate" and len(m) == 2]
        assert "first round found this" in seeds[-1]
        assert "round 2" in seeds[-1]
        assert "Do NOT re-propose" in seeds[-1]

    def test_one_epoch_is_the_default_and_germinates_once(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[("mark_done", {})]] * 4)
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=3))
        assert sum(1 for phase, _, _ in c.calls if phase == "germinate") == 1

    def test_epochs_respect_the_overall_step_budget(self):
        c = _ScriptedClient(germinate=[_AGENDA2] * 5,
                            tip=[[("run_analysis", {"code": "x"})]] * 90)
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=3, max_total_steps=8, epochs=5))
        assert sum(1 for phase, _, _ in c.calls if phase == "tip") <= 8


class TestClaimSizedContexts:
    """#61 — the context dies at the CLAIM boundary, not the investigation boundary.
    A successor is born to carry the same investigation on."""

    def test_a_context_stops_as_soon_as_it_banks_a_claim(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [("mark_done", {})],
                                 [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=8, one_claim=True))
        # tip 1 recorded and died on the spot rather than spending its eight steps
        opens = [m for phase, m, _ in c.calls if phase == "tip" and len(m) == 2]
        assert len(opens) >= 2
        assert ar.ledger[0]["investigation"] == "a1"

    def test_the_successor_gets_the_same_investigation(self):
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [_claim_call("y", "2")],
                                 [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=8, one_claim=True))
        assert [k["investigation"] for k in ar.ledger[:2]] == ["a1", "a1"]

    def test_the_successor_is_told_what_its_predecessors_banked(self):
        ar = _hyphal(_ScriptedClient())
        ar.agenda = [{"id": "a1", "question": "Q1", "status": "pending", "parent": None}]
        ar.ledger = [{"id": "k1", "statement": "predecessor found this", "value": "n=7",
                      "kind": "pattern", "investigation": "a1"}]
        seed = asyncio.run(ar._tip_seed(ar.agenda[0], one_claim=True))
        assert "already worked this same investigation" in seed
        assert "predecessor found this" in seed
        assert "do NOT re-record" in seed
        assert "cite an existing computation id" in seed   # don't redo the setup

    def test_banking_a_claim_leaves_the_investigation_open_not_done(self):
        """The distinction the status has to carry: banked-and-continuing is not the
        same as ran-out-of-steps, and neither is finished."""
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[_claim_call("x", "1")]])
        ar = _hyphal(c)
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        asyncio.run(ar._grow_tip(item, max_steps=4, one_claim=True))
        assert item["status"] == "pending"

    def test_a_context_that_banks_nothing_gets_one_more_try(self):
        """a5 banked a verified finding in its first context, wedged in its second, and
        the investigation was closed with its actual question unanswered. A bad context
        should cost a context, not the investigation."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("run_analysis", {"code": "x"})]] * 20)
        ar = _hyphal(c)
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        asyncio.run(ar._grow_tip(item, max_steps=2, one_claim=True))
        assert item["status"] == "pending" and item["wedged"] == 1

    def test_a_second_wedge_does_close_it(self):
        """Otherwise a genuinely stuck investigation loops for the whole budget."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[("run_analysis", {"code": "x"})]] * 40)
        ar = _hyphal(c)
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        for _ in range(2):
            asyncio.run(ar._grow_tip(item, max_steps=2, one_claim=True))
        assert item["status"] == "interrupted" and item["wedged"] == 2

    def test_banking_a_claim_clears_nothing_but_keeps_it_open(self):
        c = _ScriptedClient(germinate=[_AGENDA2], tip=[[_claim_call("x", "1")]] * 4)
        ar = _hyphal(c)
        asyncio.run(ar._germinate())
        item = ar._next_tip(None)
        asyncio.run(ar._grow_tip(item, max_steps=4, one_claim=True))
        assert item["status"] == "pending" and "wedged" not in item

    def test_an_investigation_cannot_spawn_successors_forever(self):
        """Without a cap, an item that keeps banking claims is never finished."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call(f"x{i}", str(i))] for i in range(40)])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=4, one_claim=True,
                                      max_claims_per_item=3))
        per_item = {}
        for k in ar.ledger:
            per_item[k["investigation"]] = per_item.get(k["investigation"], 0) + 1
        assert max(per_item.values()) == 3
        assert {a["status"] for a in ar.agenda} == {"interrupted"}   # capped, not faked done

    def test_the_old_behaviour_is_still_available_for_comparison(self):
        """one_claim=False keeps a tip running the whole investigation, so the two
        drivers can be measured against each other on the same dataset."""
        c = _ScriptedClient(germinate=[_AGENDA2],
                            tip=[[_claim_call("x", "1")], [_claim_call("y", "2")],
                                 [("mark_done", {})], [("mark_done", {})]])
        ar = _hyphal(c)
        asyncio.run(ar.explore_hyphal(tip_steps=8))
        assert [k["investigation"] for k in ar.ledger] == ["a1", "a1"]
        assert ar.agenda[0]["status"] == "done"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
