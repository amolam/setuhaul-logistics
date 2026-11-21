"""B-AI-3 stages 1–4: the deterministic checks on option replies catch what they claim to, and today's template
replies pass them. No model calls."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals import budget as B  # noqa: E402
from evals.phrase_ablation import OUT, PICK, checks, template_arm  # noqa: E402
from setuhaul.agent.replies import options_text  # noqa: E402

OPTS = [{"dock_code": "D2", "start": "1:00 PM on 19 Nov", "end": "1:55 PM on 19 Nov", "needs_approval": False},
        {"dock_code": "D4", "start": "2:00 PM on 19 Nov", "end": "2:55 PM on 19 Nov", "needs_approval": False}]
GOOD = options_text(OPTS, "en")


def flagged(reply, lang="en", opts=OPTS):
    return {k for k, v in checks(reply, opts, lang).items() if v}


def test_the_template_passes_every_stage():
    assert flagged(GOOD) == set()
    assert flagged(options_text(OPTS, "hi"), "hi") == set()


@pytest.mark.parametrize("reply,stage", [
    (GOOD.replace("D4", "D9"), "grounding"),                                   # a dock that was never offered
    (GOOD.replace("2:00 PM", "3:00 PM"), "grounding"),                          # a time that was never offered
    (GOOD.replace("Available slots", "Your slot is confirmed. Available slots"), "grounding"),
    ("1. Dock D2, 1:00 PM on 19 Nov\nReply with the number you want.", "completeness"),   # an option dropped
    (GOOD.replace("Reply with the number you want.", "Let me know."), "completeness"),
    (options_text(OPTS, "hi"), "language"),                                     # Devanagari to a Latin-script driver
    (GOOD + "\nA coordinator will call you.", "policy"),
    (GOOD + "\nRef APT1014A.", "policy"),
    (GOOD.replace("1:00 PM", "13:00"), "policy"),
    (GOOD + " 🚚", "policy"),
    (GOOD + "\n" + "We are doing our best. " * 30, "policy"),
])
def test_each_check_catches_its_problem(reply, stage):
    assert stage in flagged(reply)


def test_language_and_register_follow_the_conversation():
    assert "language" in flagged(GOOD, "hi")                                     # Hindi driver, English reply
    assert "register" in flagged(GOOD, "hi-Latn")                                # Hinglish driver, English reply
    hinglish = "Ye slot available hain:\n1. Dock D2, 1:00 PM on 19 Nov\n2. Dock D4, 2:00 PM on 19 Nov\nJo chahiye uska number bhejo."
    assert flagged(hinglish, "hi-Latn") == set()


def test_an_option_needing_approval_must_say_so():
    opts = [dict(OPTS[0], needs_approval=True)]
    assert "policy" in flagged("1. Dock D2, 1:00 PM on 19 Nov\nReply with the number you want.", opts=opts)
    assert flagged(options_text(opts, "en"), opts=opts) == set()


def test_the_frozen_situations_are_the_listed_twenty_and_templates_stay_correct():
    sits = json.loads((OUT / "situations.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in sits] == PICK
    for s in sits:
        c = checks(template_arm(s), s["options"], s["conversation_language"])
        assert not c["grounding"] and not c["completeness"] and not c["policy"], s["id"]


def test_the_sonnet_arm_cannot_spend_without_approval():
    class Paid:
        extract_model, phrase_model, calls = "anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-5", 0

        def phrase(self, block, correction=None):
            self.calls += 1
            raise AssertionError("must not be called")
    m = Paid()
    g = B.GuardedModel(m, B.Budget(cap=0.0), B.Cache(Path(tempfile.mkdtemp()) / "c.jsonl"))
    with pytest.raises(B.BudgetExceeded):
        g.phrase("FACTS: ...")
    assert m.calls == 0
