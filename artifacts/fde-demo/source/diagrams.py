"""The 16 diagrams and charts of the FDE demo, as native PowerPoint shapes and charts (editable).

Each function draws on a slide at the position it occupies in the main deck, so the same drawing is used in
SetuHaul_FDE_Demo.pptx and exported to diagrams/ or charts/ (source/diagrams_source.pptx -> PNG).
Every number here is taken from the evidence register (SetuHaul_FDE_Evidence_Register.md).
"""
from __future__ import annotations

from pptx_kit import (AI_FILL, AI_LINE, AMBER, BLUE, DET_FILL, DET_LINE, EXT_FILL, EXT_LINE, GREEN, HUM_FILL,
                      HUM_LINE, INK, LIGHT, LINE, MUTED, NAVY, RED, RISK_FILL, RISK_LINE, WHITE, arrow, bar_chart,
                      box, elbow, legend_row, table, text)

LEGEND = [("AI (LLM)", AI_FILL, AI_LINE), ("Code + database", DET_FILL, DET_LINE), ("People", HUM_FILL, HUM_LINE),
          ("External / channel", EXT_FILL, EXT_LINE)]


# 1. Business problem and solution -------------------------------------------------------------------------------
def d_problem(s, ox=6.55, oy=1.6):
    text(s, ox, oy, 6.2, 0.3, "Typical manual handling (assumed from the brief; not observed)", size=12, bold=True,
         color=MUTED)
    steps = ["Driver phones or messages", "Coordinator checks dock board", "Calls the warehouse", "Re-promises a time"]
    w, g = 1.38, 0.23
    for i, st in enumerate(steps):
        x = ox + i * (w + g)
        box(s, x, oy + 0.38, w, 0.72, st, fill=EXT_FILL, line=EXT_LINE, size=11.5)
        if i:
            arrow(s, x - g + 0.02, oy + 0.74, x - 0.02, oy + 0.74)
    box(s, ox, oy + 1.2, 6.15, 0.42, [[("Risk: ", {"bold": True, "color": RED}),
                                         ("two drivers promised the same dock; no audit trail; coordinators swamped "
                                          "in a spike", {})]], fill=RISK_FILL, line=RISK_LINE, size=11.5, align="l")
    text(s, ox, oy + 1.8, 6.2, 0.3, "SetuHaul: AI reads, code decides, people approve", size=12, bold=True,
         color=NAVY)
    steps2 = [("Driver writes in own words", EXT_FILL, EXT_LINE), ("LLM reads it into a typed form", AI_FILL, AI_LINE),
              ("Code + DB find and lock a feasible slot", DET_FILL, DET_LINE),
              ("Planner approves the request", HUM_FILL, HUM_LINE)]
    for i, (st, f, l) in enumerate(steps2):
        x = ox + i * (w + g)
        box(s, x, oy + 2.18, w, 0.8, st, fill=f, line=l, size=11.5)
        if i:
            arrow(s, x - g + 0.02, oy + 2.58, x - 0.02, oy + 2.58)


# 2. End-to-end booking workflow (swim lanes) ----------------------------------------------------------------------
def d_workflow(s, ox=0.5, oy=1.6):
    lanes = [("Driver\n(Telegram)", EXT_FILL, EXT_LINE), ("AI (LLM)", AI_FILL, AI_LINE),
             ("Code +\ndatabase", DET_FILL, DET_LINE), ("People\n(staff console)", HUM_FILL, HUM_LINE)]
    lh, lg, lw = 1.0, 0.07, 1.3
    for i, (name, f, l) in enumerate(lanes):
        y = oy + i * (lh + lg)
        box(s, ox, y, 12.33, lh, "", fill="FAFBFC", line=LINE, shape="rect", line_w=0.75)
        box(s, ox, y, lw, lh, name, fill=f, line=l, shape="rect", size=12, bold=True)
    steps = [(0, 0, "1  Reports a delay in own words (EN / HI / Hinglish)"),
             (1, 1, "2  Extracts a typed report: one forced tool call"),
             (2, 2, "3  Validates; reads the time back; waits for YES"),
             (0, 3, "4  Confirms: \"haan ji\" / YES"),
             (2, 4, "5  Finds feasible slots: 12 hard constraints"),
             (0, 5, "6  Picks a slot from the list"),
             (2, 6, "7  Holds the slot and submits a request (lock + EXCLUDE)"),
             (3, 7, "8  Planner approves or rejects (Step Functions waits)")]
    cw, bw, bh = 1.375, 1.24, 0.82
    x0 = ox + lw + 0.12
    pos = []
    for lane, col, label in steps:
        x = x0 + col * cw
        y = oy + lane * (lh + lg) + (lh - bh) / 2
        f, l = lanes[lane][1], lanes[lane][2]
        box(s, x, y, bw, bh, label, fill=f, line=l, size=10.5, margin=0.05)
        pos.append((x, y))
    for i in range(len(pos) - 1):
        (x, y), (nx, ny) = pos[i], pos[i + 1]
        ym = y + bh / 2
        ncx = nx + bw / 2
        if ny > y:
            elbow(s, [(x + bw, ym), (ncx, ym), (ncx, ny)])
        else:
            elbow(s, [(x + bw, ym), (ncx, ym), (ncx, ny + bh)])
    # AI reads every driver turn (free end of the AI lane)
    ay = oy + (lh + lg) + (lh - bh) / 2
    box(s, x0 + 7 * cw - 0.05, ay, bw + 0.05, bh, "Also reads steps 4 and 6: every driver message", fill=WHITE,
        line=AI_LINE, size=10, dash=True)
    # outcome back to the driver
    lx, ly = pos[-1]
    py = oy + 3 * (lh + lg) + (lh - bh) / 2
    text(s, x0 + 4 * cw, py + 0.12, 3 * cw - 0.15, 0.6, "→ template notice to the driver: confirmed / rejected",
         size=11, color=GREEN, align="r", bold=True)
    ey = oy + 4 * (lh + lg) + 0.04
    text(s, ox, ey, 12.33, 0.6, [[("Exceptions: ", {"bold": True, "color": RED}),
                                  ("safety words → coordinator at once  ·  no slot fits → escalation with hand-off note"
                                   "  ·  model down or budget spent → rules reader  ·  dispatcher path: offer via "
                                   "Telegram buttons; reassignment needs a second person", {})]], size=11.5)


# 3. High-level architecture --------------------------------------------------------------------------------------
def d_architecture(s, ox=0.5, oy=1.6):
    c1, c2, c3, c4 = ox, ox + 2.05, ox + 4.2, ox + 6.4
    w1, w2, w3, w4 = 1.8, 1.9, 1.95, 2.25
    r = [oy + 0.25, oy + 1.45, oy + 2.65, oy + 3.85]
    h = 0.92
    box(s, c1, r[0], w1, h, [{"t": "Telegram bot", "bold": True}, "live for 2 synthetic test drivers"],
        fill=EXT_FILL, line=EXT_LINE, size=11)
    box(s, c1, r[1], w1, h, [{"t": "WhatsApp / Meta", "bold": True}, "deferred (templates ready)"],
        fill=WHITE, line=EXT_LINE, size=11, dash=True)
    box(s, c1, r[2], w1, h + 0.2, [{"t": "Staff console", "bold": True}, "browser · Cognito MFA · planner, "
                                   "ops_lead, operations, dispatcher"], fill=HUM_FILL, line=HUM_LINE, size=11)
    box(s, c2, r[0], w2, h, [{"t": "API Gateway + API Lambda", "bold": True}, "FastAPI (Mangum) · webhook · "
                             "console API"], fill=DET_FILL, line=DET_LINE, size=11)
    box(s, c2, r[1], w2, h, [{"t": "SQS FIFO inbound", "bold": True}, "grouped per driver · de-duplicated"],
        fill=DET_FILL, line=DET_LINE, size=11)
    box(s, c2, r[2] + 0.1, w2, 0.8, [{"t": "DLQ + CloudWatch alarm", "bold": True}, "→ SNS"], fill=EXT_FILL,
        line=EXT_LINE, size=11)
    box(s, c3, r[0], w3, h, [{"t": "Workers", "bold": True}, "outbound · recovery · approval relay (1-min "
                             "schedule, asleep) + event kick"], fill=DET_FILL, line=DET_LINE, size=10.5)
    box(s, c3, r[1], w3, h, [{"t": "Agent Lambda", "bold": True}, "orchestrator: fixed pipeline in code"],
        fill=DET_FILL, line=DET_LINE, size=11)
    box(s, c3, r[2] + 0.1, w3, h, [{"t": "Approval workflow", "bold": True}, "Step Functions: waits for a planner; "
                                   "expires"], fill=HUM_FILL, line=HUM_LINE, size=11)
    box(s, c4, r[0], w4, h, [{"t": "OpenRouter → Claude Haiku 4.5", "bold": True}, "pinned Bedrock route · ZDR · "
                             "no fallback model"], fill=AI_FILL, line=AI_LINE, size=11)
    box(s, c4, r[1], w4, h + 0.15, [{"t": "Aurora PostgreSQL 17.9", "bold": True}, "Serverless v2 · Data API · RLS · "
                                    "46 migrations · definer functions"], fill=DET_FILL, line=NAVY, size=11,
        line_w=2)
    box(s, c4, r[2] + 0.22, w4, 0.8, [{"t": "LangSmith", "bold": True}, "traces · judge feedback"], fill=EXT_FILL,
        line=EXT_LINE, size=11)
    box(s, c4, r[3] + 0.08, w4, 0.7, [{"t": "Secrets Manager · KMS", "bold": True}], fill=EXT_FILL, line=EXT_LINE,
        size=11)
    # flows
    arrow(s, c1 + w1, r[0] + 0.35, c2, r[0] + 0.35)                       # telegram -> api
    elbow(s, [(c1 + w1, r[2] + 0.55), (c1 + w1 + 0.12, r[2] + 0.55), (c1 + w1 + 0.12, r[0] + 0.7),
              (c2, r[0] + 0.7)])                                           # console -> api
    arrow(s, c2 + w2 / 2, r[0] + h, c2 + w2 / 2, r[1])                     # api -> sqs
    arrow(s, c2 + w2 / 2, r[1] + h, c2 + w2 / 2, r[2] + 0.1, dash=True)    # sqs -> dlq
    arrow(s, c2 + w2, r[1] + 0.46, c3, r[1] + 0.46)                        # sqs -> agent
    arrow(s, c3 + w3, r[1] + 0.55, c4, r[1] + 0.55, width=2, color=NAVY)  # agent -> aurora
    elbow(s, [(c3 + w3, r[1] + 0.2), (c3 + w3 + 0.11, r[1] + 0.2), (c3 + w3 + 0.11, r[0] + 0.46),
              (c4, r[0] + 0.46)], color=AI_LINE)                           # agent -> llm
    elbow(s, [(c3 + w3, r[1] + 0.8), (c3 + w3 + 0.11, r[1] + 0.8), (c3 + w3 + 0.11, r[2] + 0.62),
              (c4, r[2] + 0.62)], dash=True)                               # agent -> langsmith
    arrow(s, c3 + w3 / 2, r[1] + h, c3 + w3 / 2, r[2] + 0.1, both=True, dash=True)   # agent <-> approval
    elbow(s, [(c3 + w3 / 2, r[0]), (c3 + w3 / 2, oy + 0.08), (c1 + w1 / 2, oy + 0.08), (c1 + w1 / 2, r[0])],
          color=GREEN, dash=True)                                          # outbound -> telegram
    text(s, c2 + 0.1, oy - 0.14, 2.2, 0.22, "replies via outbound worker", size=10, color=GREEN)
    text(s, ox, r[3] + 0.1, 6.3, 0.7, "Lambda + Data API: no NAT gateway, no RDS Proxy. Schedules asleep → "
         "Aurora pauses at 0 ACU. Console and API are served by the same API Lambda (no separate web host).",
         size=10.5, color=MUTED)


# 4. Agent / orchestrator sequence -----------------------------------------------------------------------------
def d_sequence(s, ox=0.5, oy=1.6):
    heads = [("Driver", EXT_FILL, EXT_LINE), ("Orchestrator\n(code)", DET_FILL, DET_LINE),
             ("LLM\n(Haiku 4.5)", AI_FILL, AI_LINE), ("Booking service\n+ PostgreSQL", DET_FILL, DET_LINE),
             ("Planner", HUM_FILL, HUM_LINE)]
    xs = [ox + 0.75 + i * 1.86 for i in range(5)]
    hw = 1.5
    bottom = oy + 5.2
    for (n, f, l), x in zip(heads, xs):
        box(s, x - hw / 2, oy, hw, 0.58, n, fill=f, line=l, size=11, bold=True)
        arrow(s, x, oy + 0.58, x, bottom, color=LINE, width=1.25, head=False, dash=True)
    msgs = [(0, 1, "message (SQS FIFO, per-driver order)", False),
            (1, 2, "fill record_driver_report (forced tool)", False),
            (2, 1, "typed JSON arguments", True),
            (1, 1, "validate: schema, extra=forbid · safety words · read-back rule", False),
            (1, 3, "code picks tool: find_feasible_options", False),
            (3, 1, "options (database function result)", True),
            (1, 0, "template reply (EN / HI / Hinglish)", True),
            (0, 1, "\"YES\" / picks option 2", False),
            (1, 3, "hold_option → submit_booking_request", False),
            (3, 4, "approval request (Step Functions waits)", False),
            (4, 3, "approve / reject in console", False),
            (3, 0, "notice: confirmed / rejected (outbound worker)", True)]
    y = oy + 0.98
    step = 0.375
    for a, b, label, ret in msgs:
        col = AI_LINE if 2 in (a, b) and a != b else (GREEN if 4 in (a, b) else MUTED)
        if a == b:
            box(s, xs[a] - 0.12, y - 0.12, 0.24, 0.24, "", fill=DET_FILL, line=DET_LINE, shape="rect")
            text(s, xs[a] + 0.18, y - 0.16, 3.6, 0.3, label, size=10, color=INK, margin=0)
        else:
            arrow(s, xs[a], y, xs[b], y, color=col, width=1.4, dash=ret)
            lx = min(xs[a], xs[b]) + 0.08
            text(s, lx, y - 0.25, max(abs(xs[b] - xs[a]) - 0.1, 3.4), 0.24, label, size=10, color=INK, margin=0,
                 align="l")
        y += step


# 5. Tool calling lifecycle ------------------------------------------------------------------------------------
def d_tool_lifecycle(s, ox=0.5, oy=1.6, w=12.33, h=0.78):
    steps = [("Tool contract: name + typed schema", DET_FILL, DET_LINE),
             ("LLM fills one form (no tool choice)", AI_FILL, AI_LINE),
             ("Schema + code checks", DET_FILL, DET_LINE),
             ("Code selects the tool", DET_FILL, DET_LINE),
             ("Principal + DB role authorise", DET_FILL, DET_LINE),
             ("Definer function: lock, constraints, idempotency", DET_FILL, DET_LINE),
             ("Result code → template or escalation", DET_FILL, DET_LINE)]
    n = len(steps)
    cw = w / n
    for i, (t, f, l) in enumerate(steps):
        box(s, ox + i * cw, oy, cw + (0.12 if i < n - 1 else 0), h, t, fill=f, line=l, size=10.5,
            shape="pent" if i < n - 1 else "rect", margin=0.1)


# 6. Guardrail hierarchy -------------------------------------------------------------------------------------
GUARD_LAYERS = [
    ("1  Input", "Webhook secret · Telegram allowlist (2 test chats) · one-time link codes · pydantic models with "
     "extra fields forbidden", EXT_FILL, EXT_LINE),
    ("2  Prompt + context", "Hygiene only, NOT an enforcement boundary: driver text framed as data · per-turn "
     "facts only", AI_FILL, AI_LINE),
    ("3  Output validation", "Forced tool + strict schema · safety-word net in code · \"confirmed\" only when the "
     "database says CONFIRMED", AI_FILL, AI_LINE),
    ("4  Tool execution", "Code chooses every tool · read-back + explicit YES before any hold · per-turn lease and "
     "fence", DET_FILL, DET_LINE),
    ("5  Business policy", "Feasibility: 12 hard constraints · approval rules · offer expiry · a declined offer can "
     "never be booked", DET_FILL, DET_LINE),
    ("6  Database", "Per-role logins · RLS by facility · definer functions · EXCLUDE constraint · idempotency keys",
     DET_FILL, DET_LINE),
    ("7  People + operations", "Planner approves (requester never decides own request) · kill switch · provisional "
     "authorization with expiry · synthetic data only · production refused", HUM_FILL, HUM_LINE),
]


def d_guardrails(s, ox=0.5, oy=1.6, w=8.4):
    h, g = 0.62, 0.09
    for i, (name, body, f, l) in enumerate(GUARD_LAYERS):
        y = oy + i * (h + g)
        box(s, ox, y, 2.25, h, name, fill=f, line=l, size=12, bold=True, align="l", margin=0.12)
        box(s, ox + 2.25, y, w - 2.25, h, body, fill=WHITE, line=l, size=11, align="l", margin=0.12)


# 7. Golden dataset and evaluation pipeline -----------------------------------------------------------------------
def d_eval_pipeline(s, ox=0.5, oy=1.6, w=12.33, h=0.72):
    steps = [("Golden dataset (versioned, hashed)", DET_FILL, DET_LINE), ("Agent run (model or scripted)", AI_FILL,
             AI_LINE), ("Reference compare", DET_FILL, DET_LINE), ("Deterministic scoring + hard gates", DET_FILL,
             DET_LINE), ("LLM judge (advisory)", AI_FILL, AI_LINE), ("Acceptance rules", HUM_FILL, HUM_LINE),
             ("Report (JSON + LangSmith)", EXT_FILL, EXT_LINE), ("Fix → regression case", HUM_FILL, HUM_LINE)]
    n = len(steps)
    cw = w / n
    for i, (t, f, l) in enumerate(steps):
        box(s, ox + i * cw, oy, cw + (0.1 if i < n - 1 else 0), h, t, fill=f, line=l, size=10.5,
            shape="pent" if i < n - 1 else "rect", margin=0.1)


def c_eval_model_vs_rules(s, x=0.5, y=2.6, w=5.3, h=3.9):
    return bar_chart(s, x, y, w, h, ["Intent accuracy", "ETA recall", "All fields right"],
                     [("Haiku 4.5 extraction, prompt p5", (100.0, 97.8, 94.6)), ("Rules reader (fallback)", (61.2, 69.2, 59.3))],
                     [AMBER, "8A94A6"], title="Synthetic held-out set h1: 167 driver turns, % (prompt p5, 26 Nov)", number_format='0.0',
                     max_v=110, gap=70, overlap=-10)


# 8. LLM-as-a-Judge workflow -------------------------------------------------------------------------------------
def d_judge(s, ox=0.5, oy=1.6):
    text(s, ox, oy, 6, 0.3, "Online (pilot): after the reply is committed, never in the booking path", size=13,
         bold=True, color=NAVY)
    on = [("Agent reply committed", DET_FILL, DET_LINE), ("Sample 20% (deterministic on turn_ref)", DET_FILL,
          DET_LINE), ("Budget checks: run cap · judge sub-cap · shared daily cap", DET_FILL, DET_LINE),
          ("Haiku 4.5 judge: prompt judge-p2, forced record_judgement", AI_FILL, AI_LINE),
          ("Strict validation", DET_FILL, DET_LINE), ("Advisory row + LangSmith feedback quality:*", EXT_FILL,
                                                       EXT_LINE)]
    cw = 12.33 / len(on)
    for i, (t, f, l) in enumerate(on):
        box(s, ox + i * cw, oy + 0.4, cw - 0.22, 0.95, t, fill=f, line=l, size=11)
        if i:
            arrow(s, ox + i * cw - 0.22, oy + 0.87, ox + i * cw, oy + 0.87)
    box(s, ox + 3 * cw, oy + 1.55, 3 * cw - 0.22, 0.6, "Any failure → SKIPPED / ERROR / UNPARSEABLE row, logged; "
        "never raises into the worker, never retried", fill=RISK_FILL, line=RISK_LINE, size=11)
    text(s, ox, oy + 2.45, 8, 0.3, "Offline acceptance (how the judge itself was evaluated)", size=13, bold=True,
         color=NAVY)
    off = [("Frozen dataset: 30 synthetic replies (J01–J30)", DET_FILL, DET_LINE),
           ("Judge run (paid, capped, no retries)", AI_FILL, AI_LINE),
           ("Compare: owner scores · ALWAYS_5 baseline · 5 planted defects", DET_FILL, DET_LINE),
           ("Owner rules: beat baseline AND 5/5 defects", HUM_FILL, HUM_LINE),
           ("Human calibration: ACCEPTED (1 reviewer, not blind); advisory only", HUM_FILL, HUM_LINE)]
    cw2 = 12.33 / len(off)
    for i, (t, f, l) in enumerate(off):
        box(s, ox + i * cw2, oy + 2.85, cw2 - 0.25, 0.95, t, fill=f, line=l, size=11)
        if i:
            arrow(s, ox + i * cw2 - 0.25, oy + 3.32, ox + i * cw2, oy + 3.32)
    text(s, ox, oy + 4.0, 12.33, 0.9, "Human calibration (owner-scored, 26 Nov; one reviewer, not blind to the AI reference): judge-p2 within ±1 on "
         "93.3% of 60 ratings, average error 0.30 (always-5: 88.3%, 0.517). 4 disagreements of 2+ points: J13, J14 "
         "(judge false positives), J17, J20 (planted defects partly missed). 5/5 defects ranked lowest, 0/30 "
         "unparseable, $0.098. Prompt judge-p1 had 18/30 unparseable and was NOT accepted.", size=11.5, color=INK)


# 9. Observability: trace lifecycle --------------------------------------------------------------------------------
def d_trace(s, ox=0.5, oy=1.6):
    # 27 Nov 2026 (DEMO-04): one tree per Telegram message; full operational content on the synthetic pilot
    box(s, ox, oy, 6.7, 0.5, [[("telegram_inbound", {"bold": True}), ("  root of the message's tree · message text", {})]],
        fill=NAVY, line=NAVY, color=WHITE, size=11.5, align="l", margin=0.12)
    rows = [(1, "agent_turn", "child of inbound · driver/shipment/facility ids · decision path · outcome", DET_FILL,
             DET_LINE),
            (2, "model_call (LLM)", "model input + raw output · tokens · cost_usd · latency", AI_FILL,
             AI_LINE),
            (2, "tool spans", "get_driver_context · find_feasible_options · hold_option … · args + result", DET_FILL,
             DET_LINE),
            (2, "llm_judge (child)", "quality:clarity / tone / language_match · advisory", AI_FILL, AI_LINE),
            (2, "telegram_outbound", "child of the turn · text sent · outcome", EXT_FILL, EXT_LINE)]
    y = oy + 0.68
    rh, gap = 0.5, 0.1
    spine1 = ox + 0.25
    spine2 = ox + 0.85
    last1 = last2 = None
    for lvl, name, meta, f, l in rows:
        x = ox + 0.45 if lvl == 1 else ox + 1.05
        sp = spine1 if lvl == 1 else spine2
        arrow(s, sp, y + rh / 2, x, y + rh / 2, color=MUTED, width=1.25, head=False)
        box(s, x, y, 6.7 - (x - ox), rh, [[(name + "   ", {"bold": True}), (meta, {"size": 10.5, "color": MUTED})]],
            fill=f, line=l, size=11.5, align="l", margin=0.1)
        if lvl == 1:
            last1 = y + rh / 2
            if name == "agent_turn":
                agent_bottom = y + rh
        else:
            last2 = y + rh / 2
        y += rh + gap
    arrow(s, spine1, oy + 0.5, spine1, last1, color=MUTED, width=1.25, head=False)
    arrow(s, spine2, agent_bottom, spine2, last2, color=MUTED, width=1.25, head=False)
    text(s, ox, y + 0.0, 6.7, 0.75, "Removed before export: the driver's name and phone, every credential. Exported "
         "(synthetic pilot only): message text, model input/output, tool results, ids. Link-code, callback and "
         "proactive runs are still their own roots.", size=10.5,
         color=MUTED)


# 10. AI quality feedback loop -------------------------------------------------------------------------------------
def d_feedback_loop(s, cx=9.65, cy=4.1, rx=2.3, ry=2.2):
    import math
    nodes = [("Pilot / eval trace", EXT_FILL, EXT_LINE), ("Failure identified", RISK_FILL, RISK_LINE),
             ("Add golden or regression case", DET_FILL, DET_LINE), ("Evaluate (scripted, then paid)", AI_FILL,
                                                                      AI_LINE),
             ("Improve prompt / rules / tool", DET_FILL, DET_LINE), ("Full suite in CI (2,080 tests)", DET_FILL,
                                                                      DET_LINE),
             ("Controlled release (activate-booking)", HUM_FILL, HUM_LINE)]
    n = len(nodes)
    bw, bh = 1.5, 0.62
    pts = []
    for i in range(n):
        a = -math.pi / 2 + 2 * math.pi * i / n
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        ux, uy = dx / L, dy / L
        t = min((bw / 2 + 0.06) / max(abs(ux), 1e-6), (bh / 2 + 0.06) / max(abs(uy), 1e-6))
        sx, sy = x1 + ux * t, y1 + uy * t
        ex, ey = x2 - ux * t, y2 - uy * t
        arrow(s, sx, sy, ex, ey, color=MUTED, width=1.4)
    for (t, f, l), (x, y) in zip(nodes, pts):
        box(s, x - bw / 2, y - bh / 2, bw, bh, t, fill=f, line=l, size=10.5)
    box(s, cx - 1.05, cy - 0.42, 2.1, 0.84, "Example: live replay BB09 half-range → validator fix → new case",
        fill=WHITE, line=LINE, size=10, color=MUTED)


# 11. Testing and reliability architecture -------------------------------------------------------------------------
TEST_LAYERS = [
    ("Full suite in CI · PostgreSQL 17.9", "2,080 passed · 1 skipped · run 45836948268 on e3c5bbe", DET_FILL,
     DET_LINE),
    ("Data API path (emulated)", "678 passed in the same CI run", DET_FILL, DET_LINE),
    ("Concurrency + integrity", "20 drivers race for 1 slot → 1 winner, 0 overlaps (9/9); 50 in the suite",
     DET_FILL, DET_LINE),
    ("Security + authorization", "roles, facility scope, evidence guard; live: agent / other-facility / no-identity "
     "decisions refused", DET_FILL, DET_LINE),
    ("Agent + tools (scripted model)", "Phase G / G1 suite 111/111 · scripted replay 64/64", AI_FILL, AI_LINE),
    ("Model evals (paid, approved runs)", "golden v1–v4 + held-out h1 on p5 · live replay 62/64, 0 hard-gate violations",
     AI_FILL, AI_LINE),
    ("Live pilot smoke + drills", "smoke 11/11 on 4b9ac56 · DLQ, lease-loss and approval drills", HUM_FILL,
     HUM_LINE),
]


def d_testing(s, ox=0.5, oy=1.6, w=6.0):
    h, g = 0.6, 0.1
    for i, (name, ev, f, l) in enumerate(TEST_LAYERS):
        y = oy + i * (h + g)
        box(s, ox, y, w, h, [{"t": name, "bold": True, "size": 11.5}, {"t": ev, "size": 10.5, "color": MUTED}],
            fill=f, line=l, align="l", margin=0.12)


# 12. Failure handling and recovery matrix --------------------------------------------------------------------------
FAILURES = [
    ["Failure", "Detection", "Recovery", "Status"],
    ["Invalid LLM output", "pydantic schema, extra=forbid", "not retried; rules reader for that turn", "CI-validated"],
    ["Wrong tool arguments", "code builds args; DB function checks", "refused with a result code", "CI-validated"],
    ["Unauthorized action", "principal + DB role + RLS", "denied, audited", "Pilot-demonstrated (smoke 5)"],
    ["Worker dies mid-turn", "processing lease + fence", "redelivered; stale worker commits nothing",
     "Pilot-demonstrated (drill)"],
    ["Poison message", "SQS max receives → DLQ", "alarm → SNS; redrive tool", "Pilot-demonstrated (drill)"],
    ["Duplicate request", "FIFO dedup id + receipts + idempotency keys", "same result, nothing twice",
     "Pilot-demonstrated (smoke 3)"],
    ["Concurrency conflict", "per-dock lock + EXCLUDE constraint", "loser re-offered or escalated",
     "Pilot-demonstrated (smoke 4)"],
    ["Telegram send failure", "notification status RETRY / UNKNOWN", "relay retries; Ops view shows label",
     "Tested (fake Bot API) only"],
    ["Planner does not answer", "Step Functions timeout", "request EXPIRED; hold released", "Pilot-demonstrated"],
    ["Driver does not answer", "offer expiry (5 min), hold TTL (180 s)", "slot released; no automatic chase",
     "Implemented; no chase (gap)"],
    ["LLM budget exhausted", "atomic reservation refused", "rules reader; booking unaffected", "CI-validated"],
    ["Judge failure / budget", "judgement status row", "SKIPPED / ERROR; never blocks", "Pilot-observed (skip)"],
]


def d_failure_matrix(s, ox=0.5, oy=1.55):
    fills = [None] + [[None, None, None, ("E2F0EA" if r[3].startswith("Pilot") else ("FBE9E7" if "gap" in r[3] or
                                                                                      "only" in r[3] else None))]
                      for r in FAILURES[1:]]
    table(s, ox, oy, 12.33, FAILURES, [2.3, 3.5, 3.6, 2.93], size=11, row_h=0.4, fills=fills)


# 13. Infrastructure cost breakdown (estimate) ---------------------------------------------------------------------
def c_infra_idle(s, x=0.5, y=1.6, w=7.5, h=4.8):
    return bar_chart(s, x, y, w, h, ["Secrets Manager (10)", "CloudWatch alarms (14)", "KMS key", "SQS polling",
                                     "Aurora storage", "Logs + assets", "Aurora compute (paused)"],
                     [("USD / month (estimate)", (4.00, 1.40, 1.00, 0.35, 0.06, 0.05, 0.0))], [BLUE],
                     title="AWS pilot, idle month: ≈ $6.50–6.90 (ESTIMATE, docs/release/09)", horizontal=True,
                     number_format='$0.00', legend=False, gap=50)


# 14. LLM cost breakdown (actual, by evidence basis) --------------------------------------------------------------
def c_llm_spend(s, x=0.5, y=1.6, w=7.5, h=4.8):
    return bar_chart(s, x, y, w, h, ["Golden extraction runs (348)", "Held-out h1 scoring (167)",
                                     "Live replays (~290, inferred)", "Held-out generation, Sonnet (65)",
                                     "Spikes (~44)", "Offline judge runs (60)", "Probes (~10, inferred)"],
                     [("USD", (1.23, 0.60, 1.00, 0.16, 0.14, 0.205, 0.03))], [AMBER],
                     title="OpenRouter spend to date by activity, USD (computed / ledger / inferred)",
                     horizontal=True, number_format='$0.00', legend=False, gap=50)


def c_unit_cost(s, x=0.5, y=1.6, w=6.2, h=4.5):
    return bar_chart(s, x, y, w, h, ["100 / month ($112.8)", "10k / month ($22.8)", "100k / month ($16.5)"],
                     [("LLM (extraction + judge)", (12.8, 12.8, 12.8)), ("AWS", (100.0, 10.0, 3.7))],
                     [AMBER, BLUE], title="PROJECTION (not demonstrated capacity): cost per 1,000 bookings, USD", stacked=True,
                     number_format='$0.0', gap=60, overlap=100)


# 15. Performance and latency comparison --------------------------------------------------------------------------
def c_latency(s, x=0.5, y=1.6, w=6.6, h=4.7):
    return bar_chart(s, x, y, w, h, ["Reply → outbound p95", "End to end p50", "End to end p95"],
                     [("Before (7 exchanges, 26 Nov)", (58.6, 53.7, 72.6)),
                      ("After (6 exchanges, reported, uncommitted)", (1.3, 13.1, 15.0))],
                     ["8A94A6", GREEN], title="Telegram latency on the pilot, seconds (preliminary)", number_format='0.0',
                     max_v=85, gap=70, overlap=-10)


def c_judge_vs_baseline(s, x=0.5, y=1.6, w=7.5, h=4.6):
    return bar_chart(s, x, y, w, h, ["Within ±1 of owner scores (%)", "Exact match (%)", "Planted defects found (of 5)"],
                     [("ALWAYS_5 baseline", (88.3, 80.0, 0)), ("Judge, prompt p2", (93.3, 83.3, 5))],
                     ["8A94A6", AMBER], title="Judge vs always-5 baseline: 60 ratings, owner-scored (1 reviewer, not blind)",
                     number_format='General', gap=70, overlap=-10)


# 16. Future roadmap ------------------------------------------------------------------------------------------------
ROADMAP = [
    ("Now: synthetic pilot (done)", HUM_FILL, HUM_LINE,
     ["Booking ON under owner approval, renewed weekly", "Telegram for 2 synthetic test drivers",
      "LangSmith thread correlation; advisory judge", "2,080 tests in CI; smoke 11/11"]),
    ("Next: before production", AI_FILL, AI_LINE,
     ["Blind, multi-reviewer judge calibration; eval gate in CI", "WhatsApp/Meta approval; DPDP consent", "Latency: agent turn "
      "breakdown; p95 < 15 s", "PostgreSQL 18, drift detection, alerting, DR drill"]),
    ("Later: scale", DET_FILL, DET_LINE,
     ["Real pilot with one facility and baseline KPIs", "Model routing: smaller model / batch for simple turns",
      "Planning optimisation across the day", "Operational analytics and customer feedback loop"]),
]


def d_roadmap(s, ox=0.5, oy=1.6):
    cw = 12.33 / 3
    for i, (title, f, l, items) in enumerate(ROADMAP):
        x = ox + i * cw
        box(s, x, oy, cw - 0.25, 0.6, title, fill=f, line=l, size=14, bold=True, shape="pent" if i < 2 else "rect")
        text(s, x + 0.05, oy + 0.8, cw - 0.4, 3.5, [{"t": it, "bullet": True} for it in items], size=13,
             space_after=8)
