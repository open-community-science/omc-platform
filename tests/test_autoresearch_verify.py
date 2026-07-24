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
    _match_num, _nums,
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
