"""Builds artifacts/fde-demo/SetuHaul_FDE_Demo.pptx (17 slides, editable) and source/diagrams_source.pptx.

    python artifacts/fde-demo/source/build_deck.py

Read-only with respect to the application: it imports nothing from setuhaul/, calls no service and no model.
Every figure on a slide is listed, with its source, in SetuHaul_FDE_Evidence_Register.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import diagrams as D  # noqa: E402
from pptx_kit import (AI_FILL, AI_LINE, AMBER, BLUE, DET_FILL, DET_LINE, EXT_FILL, EXT_LINE, GREEN, HUM_FILL,  # noqa
                      HUM_LINE, INK, LIGHT, LINE, MUTED, NAVY, RED, RISK_FILL, RISK_LINE, WHITE, arrow, blank, box,
                      legend_row, new_deck, notes, table, text)

OUT = HERE.parent
TOTAL = 17


def frame(p, n, title, message, source):
    s = blank(p)
    text(s, 0.5, 0.32, 12.33, 0.62, title, size=28, bold=True, color=NAVY, font="Arial", name="Title")
    text(s, 0.5, 0.92, 12.33, 0.45, message, size=15, color=MUTED, name="Key message")
    text(s, 0.5, 7.02, 10.6, 0.3, "Evidence: " + source, size=9.5, color=MUTED, name="Source")
    text(s, 11.3, 7.02, 1.53, 0.3, f"SetuHaul FDE · {n}/{TOTAL}", size=9.5, color=MUTED, align="r", name="Page")
    return s


def card(s, x, y, w, h, head, body, fill=LIGHT, line=LINE, head_color=NAVY, size=12, head_size=13.5):
    box(s, x, y, w, h, "", fill=fill, line=line, radius=0.05)
    text(s, x + 0.15, y + 0.1, w - 0.3, 0.4, head, size=head_size, bold=True, color=head_color)
    text(s, x + 0.15, y + 0.5, w - 0.3, h - 0.58, body, size=size, space_after=5)


def build_deck():
    p = new_deck()

    # 1 ------------------------------------------------------------------------------------------------------------
    s = blank(p, bg=NAVY)
    text(s, 0.7, 0.6, 9, 0.4, "FORWARD DEPLOYED ENGINEERING · CASE STUDY", size=13, bold=True, color=AMBER)
    text(s, 0.7, 1.1, 11.5, 0.9, "SetuHaul Logistics", size=44, bold=True, color=WHITE, font="Arial")
    text(s, 0.7, 1.95, 11.5, 0.6, "AI-Powered Logistics Operations Platform", size=26, color="CADCFC", font="Arial")
    text(s, 0.7, 2.85, 11.0, 0.9, "Drivers report delays in their own words. SetuHaul offers only dock slots that "
         "really work, and a planner approves. In concurrency tests, competing requests for one dock slot produced "
         "exactly one booking.", size=18, color=WHITE)
    cards = [("Domain", "Inbound freight to 6 warehouses with 32 dock doors (India network; all data synthetic)"),
             ("Primary users", "Truck drivers · dispatchers · warehouse planners · operations · ops leads"),
             ("Core problem", "Late trucks miss dock slots; 5 coordinators juggle 20–35 messages in a spike"),
             ("Solution", "An LLM reads each message; code and the database decide; people approve")]
    for i, (h, b) in enumerate(cards):
        x = 0.7 + i * 3.02
        box(s, x, 4.3, 2.8, 1.75, "", fill="1F2E52", line="2C3E66", radius=0.06)
        text(s, x + 0.15, 4.4, 2.5, 0.4, h, size=13, bold=True, color=AMBER)
        text(s, x + 0.15, 4.8, 2.5, 1.2, b, size=13, color=WHITE)
    text(s, 0.7, 6.55, 12, 0.4, "Synthetic pilot on AWS Sydney · release d6b20b4 (recorded 27 Nov 2026) · "
         "production off", size=12, color="AEB8CC")
    notes(s, "Open with the one-line value proposition. SetuHaul is a driver-exception and dock re-scheduling "
          "service. The AI reads messages; code and the database decide; planners approve. Say what was tested, not a "
          "guarantee: 10 and 20 concurrent requests for one slot (local proof) produced one winner and no overlap "
          "counted in the database, a 50-request test in the suite produced one winner by the service's "
          "answers, and live smoke check 4 passed on the pilot. Everything you will see is "
          "synthetic data on a synthetic pilot in AWS Sydney; production is switched off by the system itself. "
          "(30 s)")

    # 2 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 2, "Delays are routine; broken promises are the real cost",
              "The problem is correctness under burst contention, not chat volume",
              "client brief via docs/1-frame/01_problem_definition_sheet.md §1–3")
    stats = [("280–360", "loads per day"), ("32", "dock doors across 6 facilities"), ("15–25", "exception messages "
             "on a normal day"), ("20–35", "messages in a 30-minute spike"), ("5", "coordinators per shift"),
             ("0", "live GPS feeds: an ETA is a declaration")]
    for i, (big, small) in enumerate(stats):
        x = 0.5 + (i % 2) * 2.95
        y = 1.6 + (i // 2) * 1.18
        box(s, x, y, 2.75, 1.05, "", fill=LIGHT, line=LIGHT, radius=0.06)
        text(s, x + 0.15, y + 0.05, 2.5, 0.55, big, size=26, bold=True, color=NAVY, font="Arial")
        text(s, x + 0.15, y + 0.6, 2.5, 0.42, small, size=12, color=MUTED)
    text(s, 0.5, 5.2, 5.7, 0.3, "Figures from the client brief, not measured by the system", size=10.5,
         color=MUTED, italic=True)
    D.d_problem(s)
    card(s, 6.55, 4.75, 6.15, 1.65, "Why a chatbot is not enough",
         [{"t": "It can promise a slot another truck already holds", "bullet": True},
          {"t": "It cannot see dock rules, hours or truck constraints", "bullet": True},
          {"t": "It cannot keep a booking correct when 20 drivers ask at once", "bullet": True}], size=12.5)
    text(s, 0.5, 5.6, 5.8, 0.85, [[("Outcomes to measure: ", {"bold": True, "color": NAVY}),
                                   ("promises kept · double bookings (target 0) · coordinator touches per exception · "
                                    "time to a new slot. No production baseline exists yet.", {})]], size=12.5)
    notes(s, "The brief describes a high-correctness system under burst contention. A plain chatbot fails "
          "exactly where the business loses money: it promises slots it cannot keep. The manual flow on the right is "
          "my reading of the brief, not an observed process. These are the outcomes we would measure; we have no "
          "production baseline yet, and I won't pretend otherwise. (75 s)")

    # 3 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 3, "Discovery turned a vague ask into testable rules",
              "Five personas, clear decision rights, and requirements that each map to a test",
              "docs/1-frame/*, docs/2-design/01_decision_register.md, docs/review/TRACEABILITY.md, "
              "setuhaul/api/security.py")
    personas = [("Driver", "Report a delay, get real options", "Picks a slot; says YES", EXT_FILL, EXT_LINE),
                ("Dispatcher", "Offer and reassign loads", "Requests only; decides nothing", DET_FILL, DET_LINE),
                ("Planner", "No overbooked docks", "Approves or rejects requests", HUM_FILL, HUM_LINE),
                ("Operations", "One view of every exception", "Works escalations", EXT_FILL, EXT_LINE),
                ("Ops lead", "Control and safety", "Moves confirmed slots; kill switch", HUM_FILL, HUM_LINE)]
    for i, (n, need, dec, f, l) in enumerate(personas):
        x = 0.5 + i * 2.5
        box(s, x, 1.6, 2.3, 1.55, "", fill=f, line=l, radius=0.06)
        text(s, x + 0.12, 1.66, 2.1, 0.4, n, size=15, bold=True, color=NAVY)
        text(s, x + 0.12, 2.05, 2.1, 0.5, [[("Needs: ", {"bold": True}), (need, {})]], size=11.5)
        text(s, x + 0.12, 2.6, 2.1, 0.5, [[("Decides: ", {"bold": True}), (dec, {})]], size=11.5)
    rows = [["Business requirement", "Capability built", "Proof"],
            ["Never promise an infeasible or conflicting slot", "Feasibility in code; per-dock lock + EXCLUDE constraint",
             "Integrity proof 9/9; smoke check 4"],
            ["Drivers write English, Hindi, Hinglish", "LLM extraction + 3-language templates; rules fallback",
             "Held-out h1 (p5): 100% intent; native-speaker review"],
            ["People keep authority over bookings", "Planner approval in Step Functions; requester never decides",
             "Smoke checks 2a–2c, 5"],
            ["Safe to pilot on a tight budget", "Synthetic data only; $2/day LLM cap; kill switch; scale to zero",
             "Smoke checks 6–7; cost ledger"]]
    table(s, 0.5, 3.45, 12.33, rows, [3.6, 4.83, 3.9], size=12, row_h=0.5)
    text(s, 0.5, 6.1, 12.33, 0.7, [[("Non-functional: ", {"bold": True, "color": NAVY}),
                                    ("correctness under contention over throughput · every decision audited · driver "
                                     "name, phone and credentials never in traces · Sydney region only · production refused until a "
                                     "separate decision.", {})]], size=12.5)
    notes(s, "As an FDE, the first deliverable was not code: it was decision rights. Who may decide what. "
          "The dispatcher role, for example, can request but never decide. Each requirement maps to a capability and "
          "to a test or live check, which is what makes the requirements testable. (60 s)")

    # 4 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 4, "One booking, end to end: who does what, who decides",
              "Drivers confirm; planners approve (or an eligible AUTO rule); operations record arrival; no-shows return to dispatch",
              "migrations 0035, 0037, 0047, 0048; setuhaul/api/service.py; SetuHaul_FDE_Business_Workflow.md (release d6b20b4)")
    D.d_business_workflow(s)
    notes(s, "This is the business workflow as deployed. Walk it by actor. The dispatcher offers one slot to a "
          "driver. The driver's Accept is a confirmation, not a booking: it holds the slot and creates a request. A "
          "planner approves; a dispatcher's offer always goes to a planner, even when the facility is in AUTO. AUTO "
          "only confirms an eligible, low-risk booking the driver made in the chat. Operations or the ops lead record "
          "gate, dock and completed. If nobody records the arrival by start plus the facility's grace, the system "
          "marks a no-show by itself, frees the dock and tells the driver; the dispatcher then rebooks or asks a "
          "planner to cancel. The AI's only job is reading what drivers write (slide 6). (75 s)")

    # 5 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 5, "Serverless on AWS Sydney; PostgreSQL is the source of truth",
              "Event-driven Lambdas around one PostgreSQL that owns booking state",
              "infra/setuhaul_stack.py, CloudFormation outputs (26 Nov), docs/release/09_LOW_COST_PILOT_PLAN.md")
    D.d_architecture(s)
    dec = [("The database owns booking state", "50 forward-only migrations; booking, holds and approvals are "
            "PostgreSQL functions"),
           ("Scale to zero", "Lambda + Data API: no NAT, no proxy. Idle ≈ $7/month (estimate)"),
           ("Ordered, de-duplicated turns", "FIFO order per driver and dedup ids; lease + fence keep business effects idempotent"),
           ("Trade-off accepted", "Data API round trips add latency; a paused Aurora took 14.6 s to resume")]
    for i, (h, b) in enumerate(dec):
        y = 1.6 + i * 1.22
        card(s, 9.45, y, 3.38, 1.1, h, b, size=11.5, head_size=12.5)
    legend_row(s, 0.5, 6.7, D.LEGEND, size=10.5)
    notes(s, "Channels on the left, the database on the right. The API Lambda runs FastAPI through Mangum and also "
          "serves the staff console; there is no separate web host in this release. The agent Lambda reads from a "
          "FIFO queue grouped per driver. Aurora PostgreSQL 17.9 on the Data API holds every decision; 17.9 is an "
          "owner-approved pilot exception because the free plan does not offer 18. LangChain is not used: traces go "
          "to LangSmith through our own small exporter. (90 s)")

    # 6 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 6, "The orchestrator is code; the LLM is a reader, not an operator",
              "One model call per turn fills a typed form; code chooses every tool",
              "setuhaul/agent/orchestrator.py, extraction.py, prompts.py (p5), openrouter.py, docs/3-build/README.md")
    D.d_sequence(s)
    side = [("LLM reasoning", "Reads one message into record_driver_report. Decides nothing.", AI_FILL, AI_LINE),
            ("Orchestration", "A fixed pipeline in code: no agent loop, no free tool choice.", DET_FILL, DET_LINE),
            ("Deterministic logic", "Feasibility, holds and approvals in Python + PostgreSQL.", DET_FILL, DET_LINE),
            ("Human approval", "Pilot is MANUAL: a person decides. AUTO can confirm eligible chat bookings.", HUM_FILL, HUM_LINE),
            ("When the model fails", "≤ 1 retry, then the rules reader. Budget reserved before each call.",
             RISK_FILL, RISK_LINE)]
    for i, (h, b, f, l) in enumerate(side):
        y = 1.6 + i * 0.9
        box(s, 9.95, y, 2.88, 0.82, [{"t": h, "bold": True, "size": 12}, {"t": b, "size": 10.5}], fill=f, line=l,
            align="l", margin=0.1)
    text(s, 9.95, 6.12, 2.88, 0.85, [[("Pure code ", {"bold": True, "color": NAVY}),
                                      ("can't read \"tyre burst near Neemrana, 90 min late\". ", {}),
                                      ("A free agent ", {"bold": True, "color": NAVY}),
                                      ("can't be trusted to book.", {})]], size=10.5)
    notes(s, "This is the key design choice. The orchestrator is deterministic code. The LLM is used in exactly one "
          "place: it fills one form through a forced tool call, validated against a strict schema with unknown keys "
          "rejected. Code then decides which tool runs. Model routing is deliberately simple: one pinned model, "
          "Claude Haiku 4.5 on OpenRouter's Bedrock route with zero data retention, no fallback model; the fallback "
          "is a rules reader. Replies are templates since owner decision OD-3, so no model writes to a driver. "
          "(90 s)")

    # 7 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 7, "Tools are database functions behind a typed contract",
              "The same request succeeds or is refused by code, never by the model's judgement",
              "orchestrator.py tool calls, migrations 0040 and 0026, smoke option_d_smoke_20261126T140741Z.json "
              "checks 5–6")
    D.d_tool_lifecycle(s)
    card(s, 0.5, 2.6, 3.3, 3.95, "The 10 agent tools",
         [{"t": "Read", "bold": True, "color": NAVY}, "get_driver_context · get_booking_status · "
          "find_feasible_options",
          {"t": "Write (DB functions)", "bold": True, "color": NAVY}, "record_driver_report · select_option · "
          "confirm_selection · hold_option · release_hold · submit_booking_request",
          {"t": "Hand-off", "bold": True, "color": NAVY}, "escalate_to_human"], size=11.5)
    text(s, 4.1, 2.6, 4.3, 0.35, "Accepted: driver says \"haan ji, kar do\"", size=13.5, bold=True, color=GREEN)
    ok = [("LLM: intent BOOK, explicit_confirmation = true", AI_FILL, AI_LINE),
          ("Code: option was read back and is the driver's latest word", DET_FILL, DET_LINE),
          ("hold_option → per-dock lock; EXCLUDE constraint holds", DET_FILL, DET_LINE),
          ("submit_booking_request → PENDING_APPROVAL", DET_FILL, DET_LINE),
          ("Template: \"Request sent … NOT confirmed yet\"", EXT_FILL, EXT_LINE)]
    for i, (t, f, l) in enumerate(ok):
        y = 3.05 + i * 0.7
        box(s, 4.1, y, 4.3, 0.56, t, fill=f, line=l, size=11, align="l", margin=0.1)
        if i:
            arrow(s, 6.25, y - 0.14, 6.25, y)
    text(s, 8.75, 2.6, 4.1, 0.35, "Refused, whatever the model says", size=13.5, bold=True, color=RED)
    bad = [("Kill switch engaged", "agent's booking refused BOOKING_KILLED, escalated, nothing booked (live, check 6)"),
           ("Agent identity tries to approve", "refused; so are other-facility and no-identity decisions "
            "(live, check 5)"),
           ("Driver declined, then \"book it\"", "a declined offer can never be held or booked (migration 0040)")]
    for i, (h, b) in enumerate(bad):
        y = 3.05 + i * 1.18
        box(s, 8.75, y, 4.08, 1.05, [{"t": h, "bold": True, "size": 12}, {"t": b, "size": 11}], fill=RISK_FILL,
            line=RISK_LINE, align="l", margin=0.1)
    text(s, 0.5, 6.62, 12.33, 0.4, "The model has no tool access: it fills one form. Each tool is a database function "
         "running under a per-role login.", size=12, bold=True, color=NAVY)
    notes(s, "Contrast with a typical agent: here the LLM does not select tools. It reports what the driver said; "
          "code calls the tool. On the left, the success path for 'haan ji, kar do'. On the right, three refusals "
          "that were exercised live or in the database: the kill switch, an agent trying to approve, and booking an "
          "offer the driver declined. Retries and idempotency: inbound messages are de-duplicated; holds and "
          "requests are idempotent in the database. (75 s)")

    # 8 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 8, "Seven layers of guardrails; prompts are not one of the controls",
              "Every rule that matters is enforced in code or in PostgreSQL",
              "setuhaul/api/security.py, agent/extraction.py, notify/telegram.py, migrations 0001–0050, "
              "ops/kill_switch.py, ops/renewal.py")
    D.d_guardrails(s)
    qa = [("If the LLM ignores instructions?", "It can only fill a form: extra keys are rejected and code decides "
           "what happens next."),
          ("If it returns invalid output?", "Rejected and not retried; that turn falls back to the deterministic rules "
           "reader."),
          ("If it 'attempts' an unauthorised operation?", "There is no path: the model has no tools, and the agent's "
           "database login cannot decide approvals.")]
    for i, (q, a) in enumerate(qa):
        y = 1.6 + i * 1.5
        box(s, 9.2, y, 3.63, 1.36, [{"t": q, "bold": True, "size": 12.5, "color": NAVY}, {"t": a, "size": 11.5}],
            fill=LIGHT, line=LINE, align="l", margin=0.12)
    box(s, 9.2, 6.15, 3.63, 0.62, "Prompt text is never a security control", fill=NAVY, line=NAVY, color=WHITE,
        size=12.5, bold=True)
    notes(s, "Layers 1 to 3 handle input and model output; 4 to 6 are code and the database; 7 is people and "
          "operations. The database layer is what makes the rest safe: per-role logins, row-level security by "
          "facility, definer functions, an exclusion constraint. The kill switch and provisional authorization are "
          "database state, so a misconfigured Lambda cannot bypass them. Layer 2, prompt and context hygiene, is non-authoritative: "
          "it reduces errors but is not a security enforcement boundary. The prompt says the driver's text is "
          "data, but nothing depends on the model obeying that. Invalid model output is not retried; that turn "
          "uses the rules reader. (90 s)")

    # 9 ------------------------------------------------------------------------------------------------------------
    s = frame(p, 9, "AI quality is measured against versioned references, not demos",
              "Synthetic sets, re-run on deployed prompt p5; the judge is advisory, calibrated by one human reviewer",
              "evals/results/golden_*_model_20261126_*.json, replay_v4_live_20261126_2305.json, "
              "judge_20261125_172110.json, evals/judge/human_scores_v1.json")
    D.d_eval_pipeline(s)
    D.c_eval_model_vs_rules(s)
    stats = [("76 / 167", "synthetic turns in golden v4 / held-out h1 (hashed)"),
             ("62 / 64", "live replay scenarios passed on p5 (synthetic); 0 hard-gate violations"),
             ("111 / 111", "deterministic agent checks (scripted model, $0)"),
             ("91.7%", "h1 option pick on p5 (11/12; was 100% on p3)")]
    for i, (big, small) in enumerate(stats):
        y = 2.6 + i * 0.98
        text(s, 6.05, y, 3.1, 0.5, big, size=22, bold=True, color=NAVY, font="Arial")
        text(s, 6.05, y + 0.46, 3.1, 0.45, small, size=11, color=MUTED)
    box(s, 9.35, 2.6, 3.48, 3.9, "", fill=AI_FILL, line=AI_LINE, radius=0.05)
    text(s, 9.5, 2.68, 3.2, 0.4, "LLM-as-a-Judge (advisory)", size=13.5, bold=True, color=NAVY)
    text(s, 9.5, 3.1, 3.2, 3.3, [
        {"t": "93.3% of 60 ratings within ±1 of the owner's scores; avg error 0.30", "bullet": True},
        {"t": "Always-5 baseline: 88.3%, 0.517", "bullet": True},
        {"t": "4 disagreements of 2+ points: J13, J14, J17, J20", "bullet": True},
        {"t": "5/5 planted defects ranked lowest; 0/30 unparseable", "bullet": True},
        {"t": "Never gates a booking or any workflow", "bullet": True},
        {"t": [("Human calibration: ACCEPTED", {"bold": True, "color": GREEN}),
               (" (owner-scored, one reviewer, not blind to the AI reference)", {})]}], size=11, space_after=3)
    text(s, 0.5, 6.5, 12.33, 0.5, [[("Prompt version: ", {"bold": True, "color": NAVY}),
         ("all model-backed evals re-run on deployed p5 (26 Nov). Not every metric improved: option pick fell to 91.7% "
          "(BB05). Replay BB12 and AT02 fail on both p3 and p5; causes not established. Not available: an eval "
          "gate in CI.", {})]], size=10.5, color=MUTED, italic=True)
    notes(s, "Three layers of AI quality, all on synthetic data, and all model-backed runs are now on the deployed "
          "prompt p5. Golden sets: versioned and hashed driver turns with expected fields. The held-out set h1 was "
          "generated separately and never used for tuning: on p5, intent 100% against 61% for the rules reader, "
          "ETA recall 97.8%, all fields right 158 of 167, the same as p3. Not everything improved: option pick fell "
          "from 100% to 91.7% because of one miss, BB05, where the model returned no option instead of option 1. "
          "Golden v4: 74 of 76, all gates pass. Replay: 64 live replay scenarios through the real pipeline; 62 "
          "passed, with zero hard-gate violations: no unauthorised or fabricated booking in that set. BB12, a "
          "status question answered with slot options, and AT02, an unexpected no-feasible-slot escalation, failed "
          "on p3 and p5 alike; I have not established why. BB09 failed on p3 and passes on p5. I keep these sets "
          "separate; there is no combined score. The judge only scores reply quality and never gates anything. I "
          "scored the 20 calibration cases myself on 26 Nov: the judge is within one point on 93.3% of 60 ratings, "
          "average error 0.30, against 88.3% for an always-5 dummy, so the margin is real but modest. Two honest "
          "limits: one reviewer, and I had seen the AI reference scores the day before, so it is not blind; my "
          "scores match that reference on 53 of 60. Four disagreements of two or more points: J13 and J14 are "
          "judge false positives; on J17 and J20, planted defects, the judge missed part of the defect. (120 s)")

    # 10 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 10, "Live demo: two synthetic drivers, one planner, one trace",
              "About 6 minutes on the synthetic pilot, with recorded fallbacks",
              "SetuHaul_FDE_Live_Demo_Runbook.md; TELEGRAM_TEST_BOT_VALIDATION_REPORT.md (journeys A, C); owner_test_drivers_20261126.json")
    steps = [("Ops lead", "Pilot status: booking authorized, kill switch disengaged", "Read-only state", "0:30"),
             ("Dispatcher", "Offers a dock slot to SYN-DRV-TEST-1 in Telegram", "Offer with expiry", "0:45"),
             ("Driver", "Taps Accept: a request, not a booking", "Acceptance is not confirmation", "0:30"),
             ("Planner", "Approves; the driver is notified", "Human approval boundary", "0:45"),
             ("Driver", "Reports a delay in Hinglish, says YES, picks a slot", "LLM reads; code finds slots", "1:30"),
             ("Engineer", "Opens the LangSmith thread", "Correlated trace", "1:00"),
             ("Exception", "Taps Accept on an expired offer", "Expired offer refused", "0:45")]
    cw = 12.33 / 7
    arrow(s, 0.8, 2.15, 12.5, 2.15, color=LINE, width=2, head=False)
    for i, (who, what, proves, t) in enumerate(steps):
        x = 0.5 + i * cw
        box(s, x + cw / 2 - 0.3, 1.85, 0.6, 0.6, str(i + 1), fill=NAVY, line=NAVY, shape="oval", color=WHITE,
            size=16, bold=True)
        text(s, x + 0.05, 2.6, cw - 0.12, 0.35, who, size=13.5, bold=True, color=NAVY, align="c")
        text(s, x + 0.05, 2.97, cw - 0.12, 1.15, what, size=12, align="c")
        box(s, x + 0.1, 4.15, cw - 0.2, 0.62, proves, fill=LIGHT, line=LIGHT, size=11, color=MUTED)
        text(s, x + 0.05, 4.85, cw - 0.12, 0.3, t, size=11, color=MUTED, align="c")
    box(s, 0.5, 5.45, 6.0, 1.3, [{"t": "Before the demo (owner)", "bold": True, "color": NAVY, "size": 12.5},
                                 {"t": "Wake the pilot · confirm booking authorization on /health · Telegram test "
                                       "chats ready · LangSmith project open", "size": 11.5}],
        fill=HUM_FILL, line=HUM_LINE, align="l", margin=0.14)
    box(s, 6.83, 5.45, 6.0, 1.3, [{"t": "Fallback if anything is down", "bold": True, "color": RED, "size": 12.5},
                                  {"t": "Recorded screens · smoke 11/11 JSON · demo E2E JSON · this deck's slides 7, "
                                        "11 and 13", "size": 11.5}],
        fill=RISK_FILL, line=RISK_LINE, align="l", margin=0.14)
    notes(s, "Switch to the live demo now; the runbook has the exact steps. Narrate the persona, the action, the "
          "AI step, the tool call and the deterministic control at each step. Only the owner wakes the pilot and "
          "sends Telegram messages; nothing here enables production or messages anyone outside the allowlist. "
          "(6 min)")

    # 11 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 11, "One trace tree per message; name and phone masked",
              "Synthetic pilot: full operational content; driver name, phone and credentials never leave the process",
              "setuhaul/observability/tracing.py, redaction.py, notify/telegram.py, migration 0050, "
              "tests/test_trace_content_full.py, commit ff4076d, PR #17")
    D.d_trace(s)
    trio = [("Logs", "CloudWatch agent.turn lines: correlation ids, no text"),
            ("Metrics", "Ops tables + CloudWatch alarms: DLQ, errors, 4xx"),
            ("Traces", "LangSmith: inbound → agent turn → outbound, one tree per message")]
    for i, (h, b) in enumerate(trio):
        x = 7.55 + i * 1.78
        box(s, x, 1.6, 1.68, 1.35, [{"t": h, "bold": True, "size": 13, "color": NAVY}, {"t": b, "size": 10.5}],
            fill=LIGHT, line=LINE, align="l", anchor="t", margin=0.1)
    text(s, 7.55, 3.1, 5.3, 0.35, "Investigating a failed booking", size=13.5, bold=True, color=NAVY)
    text(s, 7.55, 3.45, 5.3, 1.6, [
        {"t": "Open the message's tree (or filter by driver or shipment id)", "bullet": True},
        {"t": "Read what the model saw and returned, and the decision path", "bullet": True},
        {"t": "Open the failing tool span: its arguments and result (e.g. slot taken)", "bullet": True},
        {"t": "Confirm in the database audit trail (hold, request, decision)", "bullet": True}], size=11.5,
        space_after=3)
    box(s, 7.55, 5.15, 5.28, 1.6, [{"t": "Not yet observable", "bold": True, "color": RED, "size": 12.5},
                                   {"t": "Link-code, callback, offer-continuation and proactive runs are own roots",
                                    "bullet": True, "size": 11},
                                   {"t": "No alerting on trace data; alarms are CloudWatch only", "bullet": True,
                                    "size": 11},
                                   {"t": "No per-stage breakdown inside the 8.5–11 s agent turn yet", "bullet": True,
                                    "size": 11}],
        fill=RISK_FILL, line=RISK_LINE, align="l", anchor="t", margin=0.12)
    notes(s, "Every Telegram message is one tree: the inbound webhook, the agent turn under it, and the outbound send "
          "under the turn (27 Nov, DEMO-04). Inside the turn: what the model read and returned, with model, tokens, cost "
          "and latency; each tool with its result; the decision path. The judge is a child of the turn it scored. This "
          "is a synthetic pilot, so we export full operational content; the driver's name and phone and every "
          "credential are removed before export, and full content is refused outside the synthetic pilot. On 27 Nov we "
          "found the export guard had been dropping the test drivers' traces (ordinary words in their names matched ids); "
          "it now never scans ids, with a regression test. Gaps: link-code, callback, offer-continuation and proactive "
          "runs are still their own roots, and we do not alert on trace data. (75 s)")

    # 12 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 12, "Tests prove the rules; evaluations measure the model",
              "The full suite gates every release (2,080 tests on 26 Nov; 2,233 on main since); pilot failures become regression cases",
              "CI run 45836948268 (e3c5bbe), activate-booking runs 45833537353 / 45836951650, "
              "docs/challenge/INTEGRITY_PROOF.md, docs/release/evidence/drill_*.json")
    D.d_testing(s)
    D.d_feedback_loop(s)
    text(s, 0.5, 6.62, 12.33, 0.4, "Application tests check that code does what we specified; behavioural evals check "
         "how well the model reads real driver language. Known: one redaction test has been flaky in local runs.",
         size=11.5, color=MUTED)
    notes(s, "Left: the test layers with their evidence. The full suite runs on PostgreSQL 17.9 in CI, matching the "
          "pilot, plus the Data API path through an emulator. The activation workflow re-runs the full suite before "
          "it may switch booking on. Right: the loop that turns a pilot finding into a regression case. Be explicit "
          "that CI is not the same as operational readiness. (60 s)")

    # 13 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 13, "Event-driven delivery removed the one-minute wait (preliminary)",
              "After-sample: 6 exchanges, reported and not yet committed. Both demo targets missed narrowly",
              "commit ff4076d (before), scripts/latency_report.py (after, 26 Nov, reported), "
              "docs/phase-g/PHASE_G_PERFORMANCE_REPORT.md, docs/release/10_EXPRESS_AURORA.md")
    D.c_latency(s)
    rows = [["Measure", "Target", "Achieved", "Status"],
            ["End to end p95 (pilot)", "< 15 s", "15.0 s", "Missed, narrowly"],
            ["Outbound queue → send p95", "< 2 s", "≈ 2.2 s", "Missed"],
            ["Reply → outbound p95", "–", "1.3 s (was 58.6)", "Reported"],
            ["Agent turn incl. LLM (pilot)", "–", "8.5–11 s", "Bottleneck"],
            ["LLM extraction p50 (evals)", "–", "2.2–2.4 s", "Measured"],
            ["Local load: 544 requests", "–", "p95 0.69 s, 0 failures", "Local only"],
            ["Aurora resume from 0 ACU", "–", "14.6 s first call", "Measured once"]]
    colors = [None] + [[None, None, None, RED if r[3].startswith("Missed") or r[3] == "Bottleneck" else
                        (GREEN if r[3] == "Improved" else None)] for r in rows[1:]]
    table(s, 7.35, 1.6, 5.48, rows, [1.95, 0.75, 1.55, 1.23], size=10.5, row_h=0.47, colors=colors)
    text(s, 7.35, 5.5, 5.48, 1.3, [
        {"t": "End to end and reply → outbound use the same timestamps before and after (latency_report.py, "
              "ff4076d); samples are small and different (7 vs 6); neither raw output is committed.", "bullet": True},
        {"t": "p95 ≈ worst case at n = 6; no p99; no service level is claimed. Targets are the owner's demo "
              "targets.", "bullet": True}], size=10.5, color=MUTED, space_after=2)
    notes(s, "Before the demo release, a reply waited up to a minute for the sender's one-minute tick. Event-driven "
          "delivery is the fix. The after-numbers are preliminary: six exchanges, reported from the 26 Nov run and not "
          "yet committed as evidence. Before and after use the same stage timestamps (latency_report.py), but the "
          "samples are small and different. On that sample reply-to-outbound p95 was 1.3 s against 58.6 s before, "
          "and end to end was 13.1 s at p50 and 15.0 s at p95, so we missed the under-15 s target narrowly, and queue-to-send at about 2.2 s "
          "missed its 2 s target. The next bottleneck is the agent turn itself, 8.5 to 11 s on the pilot against "
          "about 2 s locally: Data API round trips and cold starts are hypotheses, not findings. I claim no service "
          "level. (60 s)")

    # 14 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 14, "Cents per booking, capped by design",
              "Recorded dev/eval spend is known; pilot LLM, AWS and LangSmith bills are not. Scale figures are projections",
              "docs/operations/COSTS.md, evals/results/*, judge_*.json, OpenRouter key endpoint (26 Nov), "
              "docs/release/09_LOW_COST_PILOT_PLAN.md, CloudFormation output LlmJudge")
    ch = D.c_unit_cost(s)
    ch.plots[0].series[1].points[2].data_label.has_text_frame = True
    ch.plots[0].series[1].points[2].data_label.text_frame.text = ""
    text(s, 0.5, 6.12, 6.2, 0.85, "PROJECTION, not demonstrated AWS capacity: 10k and 100k/month were never run. "
         "Assumes 3 model-read turns per booking at $0.0036, judge on 20% at $0.0033, AWS from plan estimates; "
         "LangSmith excluded (unknown).", size=10, color=MUTED, italic=True)
    rows = [["Cost item", "USD", "Status"],
            ["OpenRouter, evals + dev to 18 Nov", "≈ 3.16", "recorded (key)"],
            ["Offline judge runs (2 × 30)", "0.205", "recorded (ledger)"],
            ["p5 eval re-runs, 26 Nov (3 runs)", "1.046", "recorded (results)"],
            ["Aborted v4 attempt (≈ 38 calls)", "≈ 0.14", "estimate only"],
            ["Per golden extraction (Haiku 4.5)", "≈ 0.0036", "computed"],
            ["Per pilot judgement", "≈ 0.0033", "reported, 9 turns"],
            ["Pilot LLM spend since 23 Nov", "not available", "≤ daily cap"],
            ["AWS and LangSmith bills", "not available", "not in evidence"]]
    table(s, 7.0, 1.6, 5.83, rows, [2.95, 1.25, 1.63], size=10.5, row_h=0.33)
    text(s, 7.0, 4.68, 5.83, 0.35, "Current limits (by design)", size=13.5, bold=True, color=NAVY)
    text(s, 7.0, 5.03, 5.83, 1.95, [
        {"t": "$2.00/day app cap (owner, 27 Nov; was $0.25), reserved in PostgreSQL before each call; when "
              "reached → rules reader, booking unaffected", "bullet": True},
        {"t": "OpenRouter key: $10 total, no reset; $5.98 used at the 26 Nov check. At $2/day it binds first",
         "bullet": True},
        {"t": "Cap alone ≈ 156–339 bookings/day (ESTIMATE: $2.00 ÷ $0.0059–0.0128 per booking incl. judge)",
         "bullet": True},
        {"t": "Judge: $0.75/day of the cap, 20% sample, no retries; never above the agent reserve's headroom",
         "bullet": True}], size=10.5, space_after=1)
    notes(s, "Separate actuals from projections. Recorded development and evaluation spend is a few dollars, traceable "
          "to a result file, the usage ledger or a key checkpoint. Current pilot LLM spend, the AWS bill and the "
          "LangSmith bill are not available in the evidence; do not quote them. The p5 re-runs on 26 Nov cost $1.046 across three runs, from their result files; a first v4 attempt crashed in the runner after about 38 paid calls, roughly 14 cents by estimate, and the key's own counter is the authoritative total. The chart is a projection, not "
          "demonstrated AWS capacity: 10k and 100k bookings a month have never been run. At 100 bookings a month fixed AWS cost "
          "dominates; at 100,000 the LLM dominates at about 1.3 cents per booking. Since 27 Nov the cap is $2 a day "
          "(owner decision, so the judge gets $0.75 and actually runs; at $0.25 it never did); the cap alone would allow "
          "roughly 156 to 339 bookings a day by estimate. The binding limit is the OpenRouter key: $10 that never "
          "resets, so I do not turn it into a monthly capacity. When the budget is spent the agent switches to the "
          "rules reader; booking keeps working. (75 s)")

    # 15 -----------------------------------------------------------------------------------------------------------
    s = frame(p, 15, "Four hard problems, and what is still not solved",
              "Each fix came from evidence; the open issues are listed, not hidden",
              "INTEGRITY_PROOF.md, PR #33 / commit ff4076d, LLM_JUDGE.md §7b, human_scores_v1.json, "
              "replay_v4_live_20261126_2305.json, renewal_gates.json")
    ch = [("Competing requests for one slot", "20–35 messages in 30 min compete for 32 docks", "Let PostgreSQL "
           "decide: per-dock lock + EXCLUDE constraint", "Tested: 20 concurrent → 1 winner, 0 overlaps; live check 4"),
          ("Telegram replies took a minute", "Replies waited for a 1-minute sender tick", "Event-driven send; tick "
           "kept as fallback (0046)", "Preliminary: reply → outbound p95 1.3 s on 6 reported exchanges"),
          ("Can we trust the judge?", "An always-5 'judge' scores 88% agreement", "Baseline + planted defects as "
           "acceptance rules; prompt p2", "0/30 unparseable, 5/5 defects; owner calibration 93.3% within ±1; advisory"),
          ("Safe activation of synthetic-pilot booking", "Evidence goes stale; app-only checks can drift", "DB-side evidence "
           "guard; expiring authorization; weekly renewal", "Renewal run 45836951650 passed; categories A–E")]
    for i, (h, prob, dec, out) in enumerate(ch):
        x = 0.5 + (i % 2) * 4.15
        y = 1.6 + (i // 2) * 2.62
        box(s, x, y, 3.95, 2.45, "", fill=LIGHT, line=LINE, radius=0.05)
        text(s, x + 0.14, y + 0.08, 3.7, 0.4, h, size=13.5, bold=True, color=NAVY)
        text(s, x + 0.14, y + 0.52, 3.7, 1.9, [[("Problem: ", {"bold": True}), (prob, {})],
                                               [("Decision: ", {"bold": True}), (dec, {})],
                                               [("Outcome: ", {"bold": True, "color": GREEN}), (out, {})]],
             size=12.5, space_after=6)
    box(s, 8.95, 1.6, 3.88, 5.07, "", fill=RISK_FILL, line=RISK_LINE, radius=0.04)
    text(s, 9.1, 1.68, 3.6, 0.4, "Open issues (27 Nov)", size=13.5, bold=True, color=RED)
    text(s, 9.1, 2.1, 3.6, 4.55, [
        {"t": "Prompt p6 (dates, reschedule) deployed; its paid eval deferred: not model-validated", "bullet": True},
        {"t": "Console shows the read-back record and the agent reply as 2 identical rows (1 sent)", "bullet": True},
        {"t": "Latency targets missed (15.0 s; ≈ 2.2 s)", "bullet": True},
        {"t": "Two releases stopped by their own gates on 27 Nov; fixed forward (0049, categories D/X)", "bullet": True},
        {"t": "Replay BB12, AT02 fail on p3 and p5; new p5 pick miss BB05; causes not established", "bullet": True},
        {"t": "Judge: 4 disagreements of 2+ points (J13, J14, J17, J20); one non-blind reviewer; pilot rows uncalibrated", "bullet": True},
        {"t": "WhatsApp/Meta deferred; outbound gate under an owner exception", "bullet": True},
        {"t": "PostgreSQL 17.9 pilot exception; no live drift detection", "bullet": True}], size=11,
        space_after=4)
    notes(s, "Pick two of the four to tell in full, depending on the audience. For engineers: competing requests for one slot. For AI "
          "leaders: the judge. Then read the open issues without apologising; they are what a production plan "
          "has to close. (60 s)")

    # 16 -----------------------------------------------------------------------------------------------------------
    s = blank(p, bg=NAVY)
    text(s, 0.6, 0.35, 12, 0.6, "What it proves, and what production still needs", size=28, bold=True, color=WHITE,
         font="Arial")
    cols = [("Measured (synthetic data)", "6FCF97",
             ["Concurrency: 1 winner at 10, 20 and 50; 0 overlaps counted at 10 and 20",
              "0 hard-gate violations in 64 live replay scenarios (62 passed)",
              "Held-out intent (p5): 100% model vs 61% rules", "Live smoke on the deployed release: 11/11"]),
            ("Expected (not yet measured)", AMBER,
             ["Fewer coordinator calls per delay", "Faster re-slotting after a delay",
              "One queue of requests for planners", "Needs a real-pilot baseline first"]),
            ("Before production", "CADCFC",
             ["Real data governance and DPDP consent; WhatsApp approval", "Blind, multi-reviewer judge calibration; eval gate in CI",
              "PostgreSQL 18, drift detection, alerting, DR drill", "p95 < 15 s; budget sized to volume"])]
    for i, (h, c, items) in enumerate(cols):
        x = 0.6 + i * 4.1
        box(s, x, 1.25, 3.9, 2.8, "", fill="1F2E52", line="2C3E66", radius=0.05)
        text(s, x + 0.15, 1.33, 3.6, 0.4, h, size=15, bold=True, color=c)
        text(s, x + 0.15, 1.8, 3.6, 2.45, [{"t": it, "bullet": True} for it in items], size=14, color=WHITE,
             space_after=6, bullet_color=c)
    chain = ["Business problem", "AI engineering", "Reliable execution", "Measurable business value"]
    for i, t in enumerate(chain):
        box(s, 0.6 + i * 3.05, 4.4, 3.05, 0.72, t, fill=AMBER if i == 3 else "2C3E66", line=None,
            shape="pent" if i < 3 else "rect", color=NAVY if i == 3 else WHITE, size=15, bold=True)
    text(s, 0.6, 5.45, 12.1, 0.9, "\"I don't just build AI agents. I engineer business solutions that use AI "
         "responsibly, operate reliably, deliver measurable value and can scale economically.\"", size=16,
         color=WHITE, italic=True)
    text(s, 0.6, 6.55, 10, 0.35, "All measured figures are synthetic-data evidence; see SetuHaul_FDE_Evidence_Register.md",
         size=11, color="AEB8CC")
    text(s, 11.3, 7.02, 1.53, 0.3, f"SetuHaul FDE · 16/{TOTAL}", size=9.5, color="AEB8CC", align="r")
    notes(s, "Separate what we measured from what we expect. The measured column is synthetic-data evidence: the "
          "concurrency proof (local and CI), the live replay, the held-out set and the pilot smoke test. The "
          "latency improvement is preliminary and stays on slide 13. "
          "The expected column needs a real pilot baseline. Close on the chain and the one-sentence principle. "
          "Then invite questions. (60 s)")

    # 17 (appendix) ------------------------------------------------------------------------------------------------
    s = frame(p, 17, "The driver confirms; authorized staff approve",
              "Appendix: Telegram templates as deployed; MANUAL vs AUTO by booking type",
              "setuhaul/agent/replies.py, notify/telegram.py OFFER_REPLIES, notify/outbound.py TEMPLATES, migrations 0047, 0048")
    D.d_conversation(s)
    notes(s, "Use when asked how it feels for a driver, or what AUTO changes. Left: the exact texts a driver sees in a "
          "chat booking, from the deployed templates. YES is a confirmation, and the reply says plainly it is not "
          "confirmed yet; only the planner's decision produces 'confirmed by the warehouse'. Right: MANUAL vs AUTO. "
          "AUTO only ever confirms an eligible driver-chat booking; dispatcher offers, time changes and cancellations "
          "always need a planner or ops lead. (60 s)")

    path = OUT / "SetuHaul_FDE_Demo.pptx"
    p.save(path)
    return path


def build_diagrams():
    """One diagram or chart per slide; render.ps1 exports each slide to diagrams/ or charts/."""
    p = new_deck()
    items = [
        ("diagrams/01_business_problem_and_solution", "Business problem and solution", lambda s: D.d_problem(s, 3.5, 1.9)),
        ("diagrams/02_end_to_end_booking_workflow", "End-to-end booking workflow", lambda s: (D.d_workflow(s), legend_row(s, 0.5, 6.7, D.LEGEND))),
        ("diagrams/03_high_level_architecture", "High-level architecture (synthetic pilot, AWS Sydney)", lambda s: (D.d_architecture(s), legend_row(s, 0.5, 6.7, D.LEGEND))),
        ("diagrams/04_agent_orchestrator_sequence", "Agent and orchestrator sequence (one booking)", lambda s: D.d_sequence(s, 2.2)),
        ("diagrams/05_tool_calling_lifecycle", "Tool calling lifecycle", lambda s: D.d_tool_lifecycle(s, 0.5, 3.0, 12.33, 1.2)),
        ("diagrams/06_guardrail_hierarchy", "Guardrail hierarchy", lambda s: D.d_guardrails(s, 0.5, 1.6, 12.33)),
        ("diagrams/07_golden_dataset_evaluation_pipeline", "Golden dataset and evaluation pipeline", lambda s: (D.d_eval_pipeline(s), D.c_eval_model_vs_rules(s, 0.5, 2.6, 7.0, 4.2))),
        ("diagrams/08_llm_as_a_judge_workflow", "LLM-as-a-Judge workflow", D.d_judge),
        ("diagrams/09_langsmith_trace_lifecycle", "LangSmith observability and trace lifecycle", lambda s: D.d_trace(s, 3.3, 1.6)),
        ("diagrams/10_ai_quality_feedback_loop", "AI quality feedback loop", lambda s: D.d_feedback_loop(s, 6.67, 4.1, 3.6, 2.1)),
        ("diagrams/11_testing_and_reliability_architecture", "Testing and reliability architecture", lambda s: D.d_testing(s, 2.5, 1.6, 8.3)),
        ("diagrams/12_failure_handling_recovery_matrix", "Failure handling and recovery matrix", D.d_failure_matrix),
        ("charts/13_infrastructure_cost_breakdown", "Infrastructure cost breakdown (estimate)", lambda s: D.c_infra_idle(s, 0.5, 1.5, 12.33, 5.3)),
        ("charts/14_llm_cost_breakdown", "LLM cost breakdown (actual)", lambda s: D.c_llm_spend(s, 0.5, 1.5, 12.33, 5.3)),
        ("charts/15_performance_latency_comparison", "Performance and latency comparison", lambda s: D.c_latency(s, 0.5, 1.5, 12.33, 5.3)),
        ("diagrams/16_future_roadmap", "Future roadmap", D.d_roadmap),
        ("diagrams/17_business_workflow_by_actor", "Business workflow by actor (release d6b20b4)", D.d_business_workflow),
        ("diagrams/18_telegram_conversation_manual_vs_auto", "Telegram conversation; MANUAL vs AUTO", D.d_conversation),
        ("charts/eval_model_vs_rules_heldout_h1", "Model vs rules reader, held-out h1", lambda s: D.c_eval_model_vs_rules(s, 0.5, 1.5, 12.33, 5.3)),
        ("charts/unit_cost_per_1000_bookings_projection", "Unit economics (projection)", lambda s: D.c_unit_cost(s, 0.5, 1.5, 12.33, 5.3)),
        ("charts/judge_vs_always5_baseline", "LLM judge vs always-5 baseline", lambda s: D.c_judge_vs_baseline(s, 0.5, 1.5, 12.33, 5.3)),
    ]
    names = []
    for name, title, fn in items:
        s = blank(p)
        text(s, 0.5, 0.4, 12.33, 0.6, title, size=26, bold=True, color=NAVY, font="Arial")
        text(s, 0.5, 7.02, 12.33, 0.3, "SetuHaul FDE demo · synthetic pilot · sources: SetuHaul_FDE_Evidence_Register.md",
             size=9.5, color=MUTED)
        fn(s)
        names.append(name)
    path = HERE / "diagrams_source.pptx"
    p.save(path)
    (HERE / "diagrams_source_names.txt").write_text("\n".join(names), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(build_deck())
    print(build_diagrams())
