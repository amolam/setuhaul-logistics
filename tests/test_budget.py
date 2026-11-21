"""Decision T42: no paid call without an estimate, a hard cap per run, a per-call check that retries cannot bypass,
and a cache so unchanged inputs make zero API calls. Stub models only: these tests cost nothing."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals import budget as B  # noqa: E402
from setuhaul.agent.models import ModelError, Trace  # noqa: E402

HAIKU = "anthropic/claude-haiku-4.5"
BLOCK = ("Now (India time): 2026-11-19 10:10\n"
         "Option 1: option_id=11111111-1111-4111-8111-111111111111 dock D1 at 12:00 PM on 19 Nov\n"
         "Option 2: option_id=22222222-2222-4222-8222-222222222222 dock D2 at 1:00 PM on 19 Nov\n"
         "Driver message: the second one")


class Paid:
    """A stand-in for a paid model: counts calls, reports realistic token usage."""
    extract_model, phrase_model = HAIKU, "anthropic/claude-sonnet-5"

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def extract(self, block):
        self.calls += 1
        if self.fail:
            raise ModelError("timeout")
        ids = [l.split("option_id=")[1].split()[0] for l in block.splitlines() if "option_id=" in l]
        tr = Trace("extract", HAIKU, "openrouter:stub")
        tr.input_tokens, tr.output_tokens = 1950, 335
        return {"intent": "BOOK", "selected_option_id": ids[1] if len(ids) > 1 else None}, tr


def guarded(cap=0.10, **kw):
    cache = B.Cache(Path(tempfile.mkdtemp()) / "c.jsonl")
    return B.GuardedModel(Paid(**kw), B.Budget(cap=cap), cache)


def test_a_run_over_the_cap_is_refused_unless_approved_with_a_limit():
    b = B.Budget(cap=0.10)
    with pytest.raises(B.BudgetExceeded, match="--approved"):
        b.authorise(0.35)
    with pytest.raises(B.BudgetExceeded, match="--max-usd"):
        B.Budget(approved=True).authorise(0.05)
    B.Budget(approved=True, max_usd=0.40).authorise(0.35)          # explicit approval with an explicit limit


def test_the_run_stops_before_the_call_that_would_pass_the_limit():
    g = guarded(cap=0.01)                                        # a call costs ~$0.0036: two fit, the third does not
    for n in range(2):
        g.extract(BLOCK.replace("second", f"second {n}"))
    with pytest.raises(B.BudgetExceeded, match="run stopped"):
        g.extract(BLOCK.replace("second", "second 2"))
    assert g.model.calls == 2 and g.budget.calls == 2 and g.budget.refused == 1
    assert g.budget.spent <= 0.01


def test_retries_count_against_the_same_budget_and_failures_are_not_charged():
    g = guarded(cap=0.01, fail=True)
    for _ in range(3):                                           # an orchestrator retry is just another call
        with pytest.raises(ModelError):
            g.extract(BLOCK)
    assert g.model.calls == 3 and g.budget.spent == 0              # the provider bills no usage for a failure


def test_an_unchanged_input_makes_no_second_api_call():
    g = guarded()
    a, _ = g.extract(BLOCK)
    b, tr = g.extract(BLOCK)
    assert a == b and g.model.calls == 1 and g.budget.cached == 1 and tr.outcome == "cached"


def test_new_option_ids_and_a_new_date_still_hit_the_cache_and_the_pick_is_remapped():
    g = guarded()
    g.extract(BLOCK)
    later = (BLOCK.replace("11111111-1111-4111-8111-111111111111", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
                  .replace("22222222-2222-4222-8222-222222222222", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
                  .replace("2026-11-19", "2026-11-20").replace("19 Nov", "20 Nov"))
    out, _ = g.extract(later)
    assert g.model.calls == 1 and out["selected_option_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_a_different_message_or_prompt_version_is_a_new_call(monkeypatch):
    g = guarded()
    g.extract(BLOCK)
    g.extract(BLOCK.replace("the second one", "the first one"))
    assert g.model.calls == 2
    monkeypatch.setattr(B, "PROMPT_VERSION", "p9-test")
    g.extract(BLOCK)
    assert g.model.calls == 3


def test_a_model_without_a_published_price_is_never_called():
    class Unknown(Paid):
        extract_model = "some/unpriced-model"
    g = B.GuardedModel(Unknown(), B.Budget(), B.Cache(Path(tempfile.mkdtemp()) / "c.jsonl"))
    with pytest.raises(B.BudgetExceeded, match="no published price"):
        g.extract(BLOCK)
    assert g.model.calls == 0


def test_cost_reports_say_how_the_number_is_known():
    g = guarded()
    g.extract(BLOCK)
    g.extract(BLOCK)
    r = g.budget.report(0.0036)
    assert r["basis"] == "computed" and r["paid_calls"] == 1 and r["cached_calls"] == 1
    assert r["spent_usd"] == round(B.cost(HAIKU, 1950, 335), 4)
