"""Step 3 units: extraction checks, the rules-only baseline on the 12 seed messages, grounding, model guards."""
from datetime import datetime

import re

import pytest

from setuhaul.agent import rules_extractor
from setuhaul.agent.extraction import Extraction, check, local_to_ts
from setuhaul.agent.models import BedrockModel, FakeModel, ModelError, assert_india_only
from setuhaul.agent.replies import Facts, grounding_violations

T = lambda hm: datetime.fromisoformat(f"2026-08-04T{hm}:00+05:30")


# ---------------------------------------------------------------- local time → timestamp
@pytest.mark.parametrize("now,said,expect", [
    ("09:34", "11:20", "2026-08-04T11:20"),
    ("16:24", "06:30", "2026-08-04T18:30"),      # "ETA 6:30" in the afternoon means 6:30 PM
    ("09:29", "01:30", "2026-08-04T13:30"),      # "before 1:30" in the morning means 1:30 PM
    ("23:50", "00:30", "2026-08-05T00:30"),      # after midnight
    ("10:00", "09:50", "2026-08-04T09:50"),      # within the 15-minute grace
    ("10:00", "09:30", "2026-08-04T21:30"),      # past: the next sensible occurrence
])
def test_local_to_ts(now, said, expect):
    assert local_to_ts(said, T(now)).strftime("%Y-%m-%dT%H:%M") == expect


def test_local_to_ts_rejects_garbage():
    assert local_to_ts("25:00", T("10:00")) is None and local_to_ts("soon", T("10:00")) is None


# ---------------------------------------------------------------- code-side validation (Phase 1 §1)
def ex(**kw):
    base = dict(intent="REPORT_DELAY", delay_kind="UNKNOWN", exception_type="DELAY", language="en")
    base.update(kw)
    return Extraction(**base)


def test_repair_duration_is_asked_not_guessed():
    c = check(ex(delay_kind="REPAIR_DURATION", declared_delay_min=90, exception_type="BREAKDOWN"), T("09:00"), T("10:00"))
    assert c.missing == ["delay_kind"] and c.declared_eta is None


def test_latest_finish_without_kind_is_asked():
    c = check(ex(delay_kind="ARRIVAL_TIME", declared_eta_local="11:10", latest_finish_local="13:30"), T("09:29"), None)
    assert c.declared_eta == T("11:10") and c.latest_finish == T("13:30") and c.missing == ["latest_finish_kind"]


def test_duration_proposes_the_planned_eta_and_asks():
    """Decision register D1 / THR003: 'late by one hour' has no reference point. Code proposes 11:00, the driver confirms."""
    c = check(ex(delay_kind="DELAY_DURATION", declared_delay_min=60), T("09:19"), T("10:00"))
    assert c.declared_eta is None and c.candidate_eta == T("11:00") and c.missing == ["delay_kind"]


def test_duration_beyond_24_hours_is_not_proposed():
    c = check(ex(delay_kind="DELAY_DURATION", declared_delay_min=1400), T("09:19"), T("10:00"))
    assert c.candidate_eta is None and "declared_eta_out_of_range" in c.problems and c.missing == ["declared_eta_local"]


@pytest.mark.parametrize("text,alarm", [
    ("CRITICAL medical load, give me the first dock", False), ("Hospital consumables on board, ETA 11", False),
    ("Medical devices load, late by 30 min", False), ("taking my helper to the hospital", True),
    ("need medical help, chest hurts", True), ("helper admitted to hospital", True), ("अस्पताल ले जा रहे हैं", True),
])
def test_safety_words_do_not_fire_on_cargo_names(text, alarm):
    assert check(ex(), T("09:00"), None, text=text).safety is alarm


def test_safety_from_words_even_if_the_model_missed_it():
    c = check(ex(safety_concern=False), T("09:00"), None, text="small accident near toll, driver hurt")
    assert c.safety


@pytest.mark.parametrize("text,fixed,kw,safety,breakdown", [
    # owner decision OD-2 (supersedes Phase 1's "breakdown not fixed -> safety")
    ("Puncture, I am at a dhaba, repair going on", False, {}, False, "UNREPAIRED_SAFE"),
    ("Engine failure, truck is on the service road, safe", False, {}, False, "UNREPAIRED_SAFE"),
    ("Truck broke down", False, {}, False, "UNREPAIRED_UNKNOWN"),                        # safety status unknown
    ("tyre punchar ho gya, repair me 40 min lagega", None, {"delay_kind": "REPAIR_DURATION"}, False, "UNREPAIRED_UNKNOWN"),
    ("Broke down in the middle of the highway, trucks passing fast", False, {}, True, None),     # danger
    ("Breakdown on a dangerous curve", False, {}, True, None),
    ("Truck stopped, it is not safe here", False, {}, True, None),
    ("Breakdown, helper is injured", False, {}, True, None),
    ("Tyre repaired, reaching 11:10", True, {"delay_kind": "ARRIVAL_TIME", "declared_eta_local": "11:10"}, False, "REPAIRED"),
    ("Clutch problem earlier, reaching 12:00", None, {"delay_kind": "ARRIVAL_TIME", "declared_eta_local": "12:00"}, False, "REPAIRED"),
])
def test_od2_breakdown_tiers(text, fixed, kw, safety, breakdown):
    c = check(ex(exception_type="BREAKDOWN", vehicle_fixed=fixed, **kw), T("09:00"), T("10:00"), text)
    assert (c.safety, c.breakdown) == (safety, breakdown)


def test_option_must_have_been_shown():
    c = check(ex(intent="BOOK", selected_option_id="made-up"), T("09:00"), None, shown_option_ids=["a", "b"])
    assert "selected_option_id" in c.problems and c.selected_option_id is None


def test_eta_out_of_range_rejected():
    c = check(ex(delay_kind="DELAY_DURATION", declared_delay_min=1440), T("09:00"), T("10:00"))
    assert "declared_eta_out_of_range" in c.problems


def test_extra_keys_from_the_model_are_rejected():
    with pytest.raises(Exception):
        Extraction(intent="OTHER", book_now=True)


# ---------------------------------------------------------------- rules baseline on the 12 seed driver messages
SEED = [  # (message, now, expected routing intent, eta HH:MM or None, earliest, latest, delay_min)
    ("Traffic after Shahpura. Reaching around 11:20. Any slot after 12?", "09:34", "DELAY", "11:20", "12:00", None, None),
    ("Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.", "09:29", "DELAY", "11:10", None, "13:30", None),
    ("I am late by one hour.", "09:19", "DELAY", None, None, None, 60),
    ("Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?", "09:44", "DELAY", "11:25", None, None, None),
    ("Evening traffic. ETA 6:30. Can the reefer unload tonight?", "16:24", "DELAY", "18:30", None, None, None),
    ("Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?", "09:39", "DELAY", "12:00", None, None, None),
    ("I am already at the gate. Can I get a dock now instead of waiting for 9?", "08:22", "EARLY_ARRIVAL", None, None, None, None),
    ("I reached late and am in the yard. Has a new dock been assigned?", "09:27", "CHECK_STATUS", None, None, None, None),
    ("I will be late by 45 minutes.", "09:31", "DELAY", None, None, None, 45),
    ("What slots are possible after 1 PM if I reach around 12:45?", "10:02", "DELAY", "12:45", "13:00", None, None),
    ("Is my 4 PM slot still active?", "10:12", "CHECK_STATUS", None, None, None, None),
]
ROUTE = {"REPORT_DELAY": "DELAY", "ASK_OPTIONS": "DELAY", "EARLY_ARRIVAL": "EARLY_ARRIVAL", "CHECK_STATUS": "CHECK_STATUS"}


@pytest.mark.parametrize("msg,now,route,eta,es,lf,dmin", SEED)
def test_rules_baseline_on_seed_messages(msg, now, route, eta, es, lf, dmin):
    e = rules_extractor.extract(msg)
    c = check(e, T(now), T("10:00"), msg)
    assert ROUTE.get(e.intent) == route, e
    if eta:
        assert c.declared_eta.strftime("%H:%M") == eta, e
    if es:
        assert c.earliest_start.strftime("%H:%M") == es
    if lf:
        assert c.latest_finish.strftime("%H:%M") == lf and "latest_finish_kind" in c.missing   # ambiguous → ask
    if dmin:
        assert e.declared_delay_min == dmin and e.delay_kind == "DELAY_DURATION"
    assert not c.safety


@pytest.mark.parametrize("msg,expect", [("2", 1), ("option 1", 0), ("the second one", 1), ("teesra", 2), ("5", None)])
def test_rules_option_choice(msg, expect):
    ids = ["o1", "o2", "o3"]
    e = rules_extractor.extract(msg, ids)
    assert e.selected_option_id == (ids[expect] if expect is not None else None)


def test_rules_language_and_safety():
    assert rules_extractor.extract("मैं एक घंटा लेट हूँ").language == "hi"
    assert rules_extractor.extract("bhai 2 ghante der ho jayegi").declared_delay_min == 120
    assert rules_extractor.extract("accident ho gaya, aag lagi hai").safety_concern


# ---------------------------------------------------------------- grounding (R5)
FACTS = Facts(text="1. Dock D2, 1:00 PM on 18 Nov\n2. Dock D3, 2:00 PM on 18 Nov",
              pairs={("D2", "1:00 PM"), ("D3", "2:00 PM")})


@pytest.mark.parametrize("draft,ok", [
    ("Options: 1) D2 at 1:00 PM on 18 Nov, 2) D3 at 2:00 PM. Reply with a number.", True),
    ("Options: D2 at 2:00 PM.", False),                         # real dock, real time, wrong pairing
    ("You can take D5 at 1:00 PM.", False),                     # invented dock
    ("D2 at 3:00 PM works.", False),                            # invented time
    ("Your slot is confirmed.", False),                         # nothing is confirmed
    ("Your request is not confirmed yet.", True),
    ("Reply YES to confirm D2 at 1:00 PM.", True),
])
def test_grounding(draft, ok):
    assert (grounding_violations(draft, FACTS) == []) is ok, grounding_violations(draft, FACTS)


@pytest.mark.parametrize("draft", [                                # H-5r review: unsupported status claims and promises
    "Your booking is guaranteed. The warehouse has cleared it.\n1. Dock D2, 1:00 PM on 18 Nov",
    "Good news, D2 at 1:00 PM is approved.",
    "D2 at 1:00 PM is reserved for you.",
    "You are booked on D2 at 1:00 PM.",
    "No waiting at the gate, the dock will be ready.",
    "You have priority today. 1. Dock D2, 1:00 PM on 18 Nov",
    "Don't worry, the coordinator sorted it.",
    "Aapka slot pakka hai: Dock D2, 1:00 PM on 18 Nov.",
    "Booking ho gayi hai, D2 1:00 PM.",
    "Dock D2, 1:00 PM pakka milega, tension mat lijiye.",
    "डॉक D2, 1:00 PM पक्का है।",
    "आपकी बुकिंग की गारंटी है।",
])
def test_h5r_a_phrased_reply_may_not_claim_a_status_or_make_a_promise(draft):
    assert grounding_violations(draft, FACTS), draft


_OPTS = [{"dock_code": "D2", "start": "1:00 PM on 18 Nov", "end": "1:55 PM on 18 Nov", "needs_approval": True},
         {"dock_code": "D3", "start": "2:00 PM on 18 Nov", "end": "2:55 PM on 18 Nov", "needs_approval": False}]


@pytest.mark.parametrize("claim", [                                # H-5r2 review: semantic claims no word list catches
    "The warehouse gave you the green light for D2 at 1:00 PM.",
    "You're all set for D2 at 1:00 PM.",
    "Your slot is good to go.",
    "Your appointment is now active.",
    "Your booking is guaranteed. The warehouse has cleared it.",
    "D2 is yours.",
    "Just come straight in at 1:00 PM.",
])
@pytest.mark.parametrize("where", ["before", "after", "instead_of_closing"])
def test_h5r2_an_option_reply_may_not_add_any_wording_of_its_own(claim, where):
    template, facts, _ = _R.phrasing_inputs(_OPTS, "en")
    lines = template.splitlines()
    draft = {"before": [claim] + lines, "after": lines + [claim], "instead_of_closing": lines[:-1] + [claim]}[where]
    draft = "\n".join(draft)
    assert _R.structure_violations(draft, template, "en"), draft


@pytest.mark.parametrize("lang", ["en", "hi-Latn", "hi"])
def test_h5r4_no_courtesy_line_claims_a_coordinator_update(lang):
    """Round 4: "I have the coordinator's update" passed both checks with no note at all. It is gone; what is left
    claims nothing, and the old line is now rejected as wording of the model's own."""
    for x in _R.SAFE_EXTRAS[lang]:
        assert not re.search(r"coordinator|कोऑर्डिनेटर|update|जानकारी", x, re.I), x
    template, _, block = _R.phrasing_inputs(_OPTS, lang)
    assert "coordinator" not in block.lower() and "BACKGROUND" not in block
    old = {"en": "I have the coordinator's update.", "hi-Latn": "Coordinator ki update mere paas hai.",
           "hi": "कोऑर्डिनेटर की जानकारी मेरे पास है।"}[lang]
    assert _R.structure_violations(old + "\n" + template, template, lang)


@pytest.mark.parametrize("lang", ["en", "hi", "hi-Latn"])
def test_h5r2_the_option_list_with_at_most_one_fixed_line_is_accepted(lang):
    template, facts, _ = _R.phrasing_inputs(_OPTS, lang)
    extra = _R.SAFE_EXTRAS[lang][1]
    assert _R.structure_violations(template, template, lang) == []
    assert _R.structure_violations(extra + "\n" + template, template, lang) == []
    assert _R.structure_violations(template + "\n\n" + extra, template, lang) == []
    assert _R.structure_violations(template.replace("1. ", "1) "), template, lang) == []      # numbering style only
    two = _R.SAFE_EXTRAS[lang][0] + "\n" + template + "\n" + extra
    assert _R.structure_violations(two, template, lang)                                    # only one
    lines = template.splitlines()
    swapped = "\n".join([lines[0], lines[2], lines[1]] + lines[3:])
    assert _R.structure_violations(swapped, template, lang)          # the numbers select the option: order is fixed
    assert _R.structure_violations("\n".join(lines[:2] + lines[3:]), template, lang)        # nothing dropped


@pytest.mark.parametrize("lang", ["en", "hi", "hi-Latn"])
def test_h5r_every_option_template_passes_the_claim_check(lang):
    opts = [{"dock_code": "D2", "start": "1:00 PM on 19 Nov", "end": "1:55 PM on 19 Nov", "needs_approval": True},
            {"dock_code": "D3", "start": "2:00 PM on 19 Nov", "end": "2:55 PM on 19 Nov", "needs_approval": False}]
    text, facts, _ = _R.phrasing_inputs(opts, lang)
    assert grounding_violations(text, facts) == [], text            # the safe fallback never trips its own check


# ---------------------------------------------------------------- model guards (no AWS needed)
@pytest.mark.parametrize("model,region", [("global.anthropic.claude-sonnet-5", "ap-south-1"),
                                          ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "ap-south-1"),
                                          ("anthropic.claude-sonnet-5", "ap-south-1"),
                                          ("in.anthropic.claude-sonnet-5", "us-east-1")])
def test_india_only_guard(model, region):
    with pytest.raises(ModelError, match="RESIDENCY"):
        assert_india_only(model, region)


class StubBedrock:
    """Captures the Converse request; answers like Bedrock would."""
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def converse(self, **kw):
        self.requests.append(kw)
        return self.answer


def test_bedrock_extract_request_shape_and_parsing():
    tool_input = {"intent": "REPORT_DELAY", "shipment_ref_mentioned": None, "delay_kind": "ARRIVAL_TIME",
                  "declared_eta_local": "11:20", "declared_eta_range_local": None, "declared_delay_min": None,
                  "earliest_start_local": "12:00", "latest_finish_local": None, "latest_finish_kind": None,
                  "exception_type": "TRAFFIC", "vehicle_fixed": None, "safety_concern": False, "selected_option_id": None,
                  "explicit_confirmation": False, "language": "en"}
    stub = StubBedrock({"output": {"message": {"content": [{"toolUse": {"name": "record_driver_report", "input": tool_input,
                                                                         "toolUseId": "t1"}}]}},
                        "usage": {"inputTokens": 900, "outputTokens": 80, "cacheReadInputTokens": 700},
                        "metrics": {"latencyMs": 640}, "stopReason": "tool_use"})
    m = BedrockModel(client=stub)                              # T52: Sydney, the AU geographic profile by default
    out, tr = m.extract("Driver message: Reaching around 11:20")
    req = stub.requests[0]
    assert req["modelId"].startswith("au.anthropic.claude-haiku-4-5") and tr.region == "ap-southeast-2"
    assert req["toolConfig"]["toolChoice"] == {"tool": {"name": "record_driver_report"}}      # forced tool call
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["additionalProperties"] is False
    assert req["system"][1] == {"cachePoint": {"type": "default"}} and req["inferenceConfig"]["temperature"] == 0
    assert Extraction(**out).declared_eta_local == "11:20"
    assert (tr.input_tokens, tr.output_tokens, tr.cache_read_tokens, tr.latency_ms) == (900, 80, 700, 640)
    assert tr.prompt_version and tr.schema_version and tr.model_id.startswith("au.")


def test_bedrock_failures_become_model_errors():
    class Boom:
        def converse(self, **kw):
            raise TimeoutError("read timeout")
    with pytest.raises(ModelError):
        BedrockModel(client=Boom()).extract("x")
    no_tool = StubBedrock({"output": {"message": {"content": [{"text": "I think..."}]}}, "usage": {}, "metrics": {}})
    with pytest.raises(ModelError, match="no tool call"):
        BedrockModel(client=no_tool).extract("x")


def test_fake_model_scripts_answers_and_failures():
    f = FakeModel(extractions=[{"a": 1}, ModelError("down")], replies=[lambda b: b.upper()])
    assert f.extract("x")[0] == {"a": 1}
    with pytest.raises(ModelError):
        f.extract("y")
    assert f.phrase("hi")[0] == "HI"


# ---------------------------------------------------------------- providers: OpenRouter primary, Bedrock optional (T52)
import json as _json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD))
from setuhaul.agent.openrouter import SONNET_OR, OpenRouterModel  # noqa: E402

GOOD_ARGS = {"intent": "REPORT_DELAY", "shipment_ref_mentioned": None, "delay_kind": "ARRIVAL_TIME",
             "declared_eta_local": "11:20", "declared_eta_range_local": None, "declared_delay_min": None,
             "earliest_start_local": "12:00", "latest_finish_local": None, "latest_finish_kind": None,
             "exception_type": "TRAFFIC", "vehicle_fixed": None, "safety_concern": False, "selected_option_id": None,
             "explicit_confirmation": False, "language": "en"}


def or_model(handler, **kw):
    sent = []

    def h(req):
        sent.append(_json.loads(req.content))
        return handler(req)
    m = OpenRouterModel("test-key", client=httpx.Client(transport=httpx.MockTransport(h)), sleep=lambda s: None, **kw)
    return m, sent


def tool_reply(args, provider="Amazon Bedrock"):
    return httpx.Response(200, json={"provider": provider, "usage": {"prompt_tokens": 900, "completion_tokens": 80,
                                                                    "prompt_tokens_details": {"cached_tokens": 700}},
                                     "choices": [{"message": {"tool_calls": [{"function": {
                                         "name": "record_driver_report", "arguments": _json.dumps(args)}}]}}]})


def test_openrouter_extract_forces_the_tool_and_pins_a_zero_retention_route():
    m, sent = or_model(lambda r: tool_reply(GOOD_ARGS))
    out, tr = m.extract("Driver message: Reaching around 11:20")
    assert Extraction(**out).declared_eta_local == "11:20"
    b = sent[0]
    assert b["model"] == "anthropic/claude-haiku-4.5" and b["temperature"] == 0 and "models" not in b
    assert b["tool_choice"] == {"type": "function", "function": {"name": "record_driver_report"}}
    assert b["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert b["provider"] == {"order": ["amazon-bedrock"], "only": ["amazon-bedrock"], "allow_fallbacks": False,
                             "require_parameters": True, "data_collection": "deny", "zdr": True}
    assert (tr.input_tokens, tr.output_tokens, tr.cache_read_tokens, tr.region) == (900, 80, 700, "openrouter:Amazon Bedrock")


@pytest.mark.parametrize("resp", [
    httpx.Response(429, json={"error": {"message": "rate limited"}}),
    httpx.Response(200, json={"provider": "Amazon Bedrock", "choices": [{"message": {"content": "sure!"}}]}),  # no tool call
    httpx.Response(200, json={"provider": "Amazon Bedrock", "choices": [{"message": {"tool_calls": [{"function": {
        "name": "record_driver_report", "arguments": "{not json"}}]}}]}),
    httpx.Response(502, text="bad gateway"),
])
def test_openrouter_failures_become_final_model_errors(resp):
    m, _ = or_model(lambda r: resp)
    with pytest.raises(ModelError) as e:
        m.extract("x")
    assert e.value.retryable is False                       # the client already retried what was safe to retry


def test_openrouter_phrase_returns_text():
    m, sent = or_model(lambda r: httpx.Response(200, json={"provider": "Amazon Bedrock",
                                                           "choices": [{"message": {"content": " 1. Dock D2, 1:00 PM "}}]}),
                       phrase_model=SONNET_OR)
    text, tr = m.phrase("FACTS: ...")
    assert text == "1. Dock D2, 1:00 PM" and sent[0]["model"] == "anthropic/claude-sonnet-5" and "tools" not in sent[0]


def test_the_deployed_package_never_imports_the_eval_tools():
    """Deployment-level check: setuhaul/ never imports evals/, OpenRouter's endpoint is named in one module only, and
    importing every deployed module (in a fresh process) never loads the eval package."""
    for f in (BUILD / "setuhaul").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+evals\b", src, re.M), f
        assert ("openrouter.ai" in src) == (f.name == "openrouter.py"), f
    probe = ("import pkgutil, sys, setuhaul\n"
             "for m in pkgutil.walk_packages(setuhaul.__path__, 'setuhaul.'):\n"
             "    try: __import__(m.name)\n"
             "    except ModuleNotFoundError as e:\n"   # R2.1 §5: only a missing third-party driver is tolerated
             "        if (e.name or '').split('.')[0] not in ('psycopg', 'psycopg_pool', 'boto3', 'botocore'): raise\n"
             "print(sorted(n for n in sys.modules if n == 'evals' or n.startswith('evals.')))")
    out = subprocess.run([sys.executable, "-c", probe], cwd=BUILD, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_sonnet_phrasing_never_sends_a_temperature():
    """Sonnet 5 rejects a non-default temperature (found by the OpenRouter spike; the Bedrock path had the same bug)."""
    stub = StubBedrock({"output": {"message": {"content": [{"text": "1. Dock D2, 1:00 PM"}]}}, "usage": {}, "metrics": {}})
    BedrockModel(region="ap-south-1", phrase_model="in.anthropic.claude-sonnet-5", client=stub).phrase("FACTS: ...")
    assert "temperature" not in stub.requests[0]["inferenceConfig"]
    m, sent = or_model(lambda r: httpx.Response(200, json={"provider": "Amazon Bedrock",
                                                           "choices": [{"message": {"content": "ok"}}]}),
                       phrase_model=SONNET_OR)
    m.phrase("FACTS: ...")
    assert "temperature" not in sent[0]


@pytest.mark.parametrize("text,kind,asked", [
    ("I need to leave the warehouse before 1:30", "GATE_OUT", True),       # D1: ambiguous, even if the model says gate-out
    ("must be out of the gate by 1:30", "GATE_OUT", False),
    ("unloading must start by 1:30", "UNLOAD_START", False),
    ("leave by 1:30", "UNLOAD_START", True),
    # review (step 3 update): the word "unloading" alone does not settle start vs finish
    ("Unloading must finish by 1:30", "UNLOAD_START", True),
    ("Unloading should start by 1:30", "UNLOAD_END", True),
    ("Unloading must finish by 1:30", "UNLOAD_END", False),
    ("Unloading should start by 1:30", "UNLOAD_START", False),
    ("unloading should start and be done by 1:30", "UNLOAD_END", True),
    ("1:30 tak unloading khatam ho jani chahiye", "UNLOAD_END", False),
    ("1:30 tak unloading shuru ho jaye", "UNLOAD_START", False),
])
def test_latest_finish_kind_needs_the_drivers_words(text, kind, asked):
    c = check(ex(latest_finish_local="13:30", latest_finish_kind=kind), T("09:29"), T("10:00"), text)
    assert ("latest_finish_kind" in c.missing) is asked


def test_a_range_with_a_missing_end_drops_only_the_range():
    """Live G3 replay (BB09): Haiku sent ["19:00", null]; the whole extraction used to be rejected."""
    e = Extraction(intent="ASK_OPTIONS", declared_eta_range_local=["19:00", None], earliest_start_local="19:00")
    assert e.declared_eta_range_local is None and e.earliest_start_local == "19:00" and e.intent == "ASK_OPTIONS"


def test_a_delay_with_an_earliest_start_does_not_need_an_eta_question():
    """Live G3 replay (BB09): "Running late today. Anything after 7 works for me." -> offer slots after 19:00."""
    c = check(ex(intent="REPORT_DELAY", earliest_start_local="07:00"), T("10:00"), T("12:15"), "Anything after 7 works")
    assert c.earliest_start == T("19:00") and "declared_eta_local" not in c.missing


# ---------------------------------------------------------------- B-AI-3 fixes: Hinglish templates, conversation language
import string as _string  # noqa: E402

from setuhaul.agent import replies as _R  # noqa: E402


def _fields(s):
    return {f for _, f, _, _ in _string.Formatter().parse(s) if f}


def test_every_reply_has_a_hinglish_version_with_the_same_placeholders():
    assert set(_R.T) == set(_R.HL) and set(_R.ASK) == set(_R.HL_ASK)
    dropped = {"keep": {"eta"}}            # S4-R2-08: the native reviewer dropped the new-time sentence in Hinglish
    for k, (en, hi) in _R.T.items():
        assert _fields(en) == _fields(hi), k
        assert _fields(_R.HL[k]) == _fields(en) - dropped.get(k, set()), k
        assert not _R.DEVANAGARI.search(_R.HL[k]), k                       # Latin letters only
    for k, (en, hi) in _R.ASK.items():
        assert _fields(_R.HL_ASK[k]) == _fields(en), k


def test_hinglish_option_reply_is_grounded_and_hinglish():
    opts = [{"dock_code": "D2", "start": "1:00 PM on 19 Nov", "end": "1:55 PM on 19 Nov", "needs_approval": True}]
    text, facts, _ = _R.phrasing_inputs(opts, "hi-Latn")
    assert _R.grounding_violations(text, facts) == [] and "approval" in text
    assert _R.detect_language(text) == "hi-Latn"


@pytest.mark.parametrize("text,model,earlier,want", [
    ("हाँ", "en", ["मैं एक घंटा लेट हूँ"], "hi"),                                # Devanagari always means Hindi
    ("haan", "en", ["bhai traffic bahut hai, 1 ghanta late ho jaunga"], "hi-Latn"),   # short reply keeps Hinglish
    ("Yes", "hi-Latn", ["I will be late by 45 minutes."], "en"),                # ...and keeps English
    ("2", "en", ["do ghante mein pahunch jaunga"], "hi-Latn"),
    ("Reaching 11:20, any slot after 12?", "en", ["bhai traffic hai"], "en"),   # a full message: the model decides
    ("yes", "en", [], "en"),
])
def test_reply_language_follows_the_conversation(text, model, earlier, want):
    assert _R.reply_language(text, model, earlier) == want


# ---------------------------------------------------------------- human test H-1 (demo console, 20 Nov 2026)
@pytest.mark.parametrize("msg,asked,now,eta", [
    ("12 baje", "declared_eta_local", "08:37", "12:00"),       # Hindi word order, answering "kitne baje pahunchenge?"
    ("dopahar 12 baje", "declared_eta_local", "08:37", "12:00"),
    ("shaam 6 baje pahunch jaunga", None, "08:37", "18:00"),   # an arrival verb needs no question
    ("raat 10 baje", "declared_eta_local", "08:37", "22:00"),  # not "at 10" inside "raat"
    ("subah 5 baje", "declared_eta_local", "02:00", "05:00"),
    ("1 baje", "declared_eta_local", "08:44", "13:00"),        # nearest sensible occurrence
    ("late ho jayega, raat 10 baje", None, "08:37", "22:00"),  # a late report gives the time its meaning
    ("traffic hai, around 1 pm", None, "08:37", "13:00"),
    ("12 pm", "declared_eta_local", "08:37", "12:00"),         # a bare time answering "what time will you reach?"
    ("1 pm", "delay_kind", "08:38", "13:00"),                  # ... or answering "around 12:33?"
    ("12 ko", "declared_eta_local", "08:37", "12:00"),
    ("12:30 tak", "declared_eta_local", "08:37", "12:30"),
])
def test_h1_rules_read_hindi_order_and_bare_answer_times(msg, asked, now, eta):
    e = rules_extractor.extract(msg, asked=asked)
    c = check(e, T(now), T("10:00"), msg)
    assert e.delay_kind == "ARRIVAL_TIME" and c.declared_eta.strftime("%H:%M") == eta, e


@pytest.mark.parametrize("msg,asked", [("1 pm", None), ("12 ko", None), ("2", "declared_eta_local"), ("2", None)])
def test_h1_a_bare_number_is_a_time_only_as_an_answer_to_the_time_question(msg, asked):
    e = rules_extractor.extract(msg, asked=asked)
    if asked is None:
        assert e.delay_kind == "UNKNOWN" and e.declared_eta_local is None
    else:
        assert e.delay_kind == "ARRIVAL_TIME"                  # "2" after "kitne baje pahunchenge?" is 2 o'clock


@pytest.mark.parametrize("msg,asked", [
    ("warehouse closes at 12 baje", None),                     # review of H-1: these set the ETA before the fix
    ("call me raat 10 baje", None),
    ("call me raat 10 baje", "declared_eta_local"),            # even right after the time question: it is a call time
    ("12 baje", None),                                         # no question pending, no arrival word
    ("dock kitne baje khulta hai? 6 baje?", None),
    ("I am late, warehouse closes at 12 baje", None),          # late, but the time belongs to the warehouse
    ("I'm late. Please call me at 10 pm", None),
    ("lunch break at 1 pm", None),
    ("my shift ends at 6", None),
    ("I'm late, at 12 baje the warehouse closes", None),       # H-1r review: the subject comes AFTER the time
    ("Traffic hai, at 1 pm lunch break", None),
    ("Late ho gaya, 12 baje office band", None),
    ("late, around 3 pm for the call", None),
    ("pahunchne ke baad 12 baje call karna", None),            # an arrival word, but the time is for the call
    ("late hoon, 12 baje meeting hai", None),
    ("traffic, at 5 pm my brother will come", None),           # a clause that is about something else
    ("late, 12 baje, office band", None),                      # H-1r2 review: the NEXT clause owns the bare time
    ("late, 12 baje, warehouse closes", None),
    ("late, 12 baje, meeting hai", None),
    ("late hoon. 3 pm. lunch break", None),
])
def test_h1_a_time_that_is_not_about_arriving_never_sets_the_eta(msg, asked):
    e = rules_extractor.extract(msg, asked=asked)
    assert e.declared_eta_local is None and e.delay_kind != "ARRIVAL_TIME", e
    assert e.earliest_start_local is None, e                   # nor a "slot after" time


@pytest.mark.parametrize("msg", ["late, 12 baje, office band", "late, 12 baje, warehouse closes", "late, 12 baje, meeting hai"])
def test_h1_a_time_owned_by_another_event_makes_the_agent_ask_for_the_arrival_time(msg):
    e = rules_extractor.extract(msg)
    c = check(e, T("08:37"), T("10:00"), msg)
    assert e.intent == "REPORT_DELAY" and c.declared_eta is None and "declared_eta_local" in c.missing, (e, c)


@pytest.mark.parametrize("msg,now,eta", [                      # H-1r review: legitimate arrival cases still read
    ("I'm late, will reach at 12 baje", "08:37", "12:00"),
    ("late ho gaya, 12 baje tak pahunch jaunga", "08:37", "12:00"),
    ("Traffic hai, around 1 pm", "08:37", "13:00"),
    ("late hoon. raat 10 baje", "08:37", "22:00"),
    ("stuck in jam, at 3 pm maybe", "08:37", "15:00"),
    ("late, around 3 pm, call me if needed", "08:37", "15:00"),  # the call is a different clause
])
def test_h1_arrival_times_in_a_late_report_are_still_read(msg, now, eta):
    e = rules_extractor.extract(msg)
    c = check(e, T(now), T("10:00"), msg)
    assert e.delay_kind == "ARRIVAL_TIME" and c.declared_eta.strftime("%H:%M") == eta, e


def test_h1_duration_after_the_late_word():
    assert rules_extractor.extract("Mera late hai - 3 ghanta").declared_delay_min == 180
    assert rules_extractor.extract("thoda late hoga, 45 min").declared_delay_min == 45
    assert rules_extractor.extract("3 ghanta").declared_delay_min is None          # no "late": not a delay report


# ================================================================ S4-R2-08 native-speaker review (21 Nov 2026)
def test_s4_r2_08_reviewed_hinglish_wording():
    assert _R.t("keep", "hi-Latn", dock_code="D2", start="1:30 PM", eta="1:10 PM") == \
        "Aapka slot wahi rahega: dock D2, 1:30 PM. Kuch badla to main bataunga."
    q = _R.ask("latest_finish_kind", "hi-Latn")
    assert "gate se bahar nikalna" in q and "maal utarna" in q
    # the answers the new wording invites are understood
    from setuhaul.agent.extraction import kind_stated
    assert kind_stated("GATE_OUT", "gate se bahar nikalna hai")
    assert kind_stated("UNLOAD_START", "maal utarna shuru ho jaana chahiye")


@pytest.mark.parametrize("lang,tail", [("en", "Reply with the number."), ("hi-Latn", "Jo order hai uska number bhejiye."),
                                       ("hi", "ऑर्डर 1, 2 या 3 नंबर भेजें।")])
def test_s4_r2_08_the_order_question_numbers_the_orders(lang, tail):
    text = _R.ask("shipment_id", lang, refs="ORD-251119-004, ORD-251119-007, ORD-251119-009")
    lines = text.splitlines()
    assert lines[1:4] == ["1. ORD-251119-004", "2. ORD-251119-007", "3. ORD-251119-009"] and lines[-1] == tail, text


def test_s4_r2_08_a_bare_number_is_read_as_the_answer_to_a_numbered_question():
    from setuhaul.agent.orchestrator import _bare_number
    assert [_bare_number(x) for x in ("1", "2 number", "no. 2", " 3. ", "2 wala")] == [1, 2, 2, 3, 2]
    assert [_bare_number(x) for x in ("12", "haan", "1 number ka kar dijiye", "2 ghante late", "")] == [None] * 5


_NOW = datetime(2026, 11, 21, 8, 0, tzinfo=__import__("datetime").timezone.utc)        # 1:30 PM IST
_BASE = _NOW + __import__("datetime").timedelta(hours=2)


def _read(msg, shown=None):
    e = rules_extractor.extract(msg, shown)
    return e, check(e, _NOW, _BASE, msg, shown or [])


def _ist(c):
    return c.declared_eta.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).strftime("%H:%M") if c.declared_eta else None


# The reviewer's own messages (Part C of the review sheet), read by the rules reader that runs when the AI is down.
def test_s4_r2_08_reading_test_arrival_times():
    e, c = _read("Traffic mein fas gaya hu, thoda late ho jaunga")
    assert e.exception_type == "TRAFFIC" and c.missing == ["declared_eta_local"]          # late, no time: ask
    e, c = _read("2 ghante late chal raha hu. 4 baje pohonch jaunga")
    assert _ist(c) == "16:00" and not c.missing                                           # the stated time wins
    e, c = _read("Kal subah pochunga.")
    assert e.intent == "REPORT_DELAY" and e.language == "hi-Latn" and c.missing == ["declared_eta_local"]
    e, c = _read("Late hoon. Shaam 4 se 6 tak pohunchuga")
    assert e.declared_eta_range_local == ["16:00", "18:00"]
    e, c = _read("Office band hone se pehle pohunch jaunga")
    assert c.declared_eta is None and c.missing == ["declared_eta_local"]                 # no time of its own: ask
    e, c = _read("Jaldi pahunch gaye hai. Warehouse ke paas hai")
    assert e.intent == "EARLY_ARRIVAL"
    e, c = _read("raat 10 baje se pehle gate se nikalna hai")
    assert e.latest_finish_local == "22:00" and e.latest_finish_kind == "GATE_OUT" and e.intent == "ASK_OPTIONS"
    assert c.declared_eta is None                                                         # a deadline, not an arrival


def test_s4_r2_08_reading_test_breakdowns_and_safety():
    e, c = _read("Tyre puncure ho gayi hai. Shaam tak pahunch jaunga")
    assert e.exception_type == "BREAKDOWN" and c.breakdown == "UNREPAIRED_UNKNOWN"
    e, c = _read("Truck mein problem hai, repairing chal rahi hai")
    assert e.exception_type == "BREAKDOWN" and e.vehicle_fixed is not True
    e, c = _read("Repairing ka kaam ho gaya hai. Hum nikal gaye hai")
    assert e.vehicle_fixed is True and c.breakdown == "REPAIRED"                          # fixed: no escalation
    e, c = _read("repair ka kaam abhi tak nahi ho gaya")
    assert e.vehicle_fixed is not True                                                    # a negation is not fixed
    e, c = _read("Accident ho gaya hai, par hum theek hai")
    assert c.safety                                                                       # a coordinator calls


def test_s4_r2_08_reading_test_status_pick_and_yes():
    from setuhaul.domain.booking import classify_confirmation
    assert _read("Booking status kya hai")[0].intent == "CHECK_STATUS"
    assert _read("1 number ka kar dijiye", ["o1", "o2", "o3"])[0].selected_option_id == "o1"
    assert classify_confirmation("haaji") == "YES"
