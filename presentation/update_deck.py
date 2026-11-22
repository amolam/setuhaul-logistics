"""Add the 23 Nov 2026 Defend slides to the existing readout deck, keeping its structure, layouts and colours.

    python presentation/update_deck.py presentation/SetuHaul_readout.pptx --tests "1,1xx"

The original deck is built by build_deck.js (pptxgenjs). That toolchain is not installed on this machine, so the new
slides are added with python-pptx on the deck's own CONTENT layout, in the same colours and type sizes:
- main story: the scenario run (after the integrity slide), the scheduling comparison, and the Option A / Option D
  status with the Step 6 gates (after the evidence slide);
- appendix: the Break report summary (after "How we broke our own system").
Only one existing text is changed: the test count on the evidence slide.
"""
from __future__ import annotations

import argparse
import copy

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

C = {k: RGBColor.from_string(v) for k, v in {"navy": "14213D", "amber": "F2A007", "ink": "1B2333", "muted": "5B6B82",
                                              "line": "D5DBE4", "light": "EEF1F5", "green": "2E7D5B", "red": "C0392B",
                                              "white": "FFFFFF", "blue": "3A6EA5"}.items()}


def text(slide, x, y, w, h, runs, size=12, bold=False, color="ink", align=PP_ALIGN.LEFT, name=None):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, Inches(0.04))
    paras = runs if isinstance(runs, list) else [runs]
    for i, p in enumerate(paras):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.space_after = Pt(4)
        for seg in (p if isinstance(p, list) else [p]):
            t, opts = (seg, {}) if isinstance(seg, str) else seg
            r = para.add_run()
            r.text = t
            r.font.size = Pt(opts.get("size", size))
            r.font.bold = opts.get("bold", bold)
            r.font.color.rgb = C[opts.get("color", color)]
    return tb


def card(slide, x, y, w, h, fill="light", name=None):
    s = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    s.adjustments[0] = 0.06
    s.fill.solid()
    s.fill.fore_color.rgb = C[fill]
    s.line.fill.background()
    s.shadow.inherit = False
    if name:
        s.name = name
    return s


def table(slide, x, y, w, rows, widths, size=10, header_fill="navy", name=None, status_col=None):
    shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(x), Inches(y), Inches(w), Inches(0.28 * len(rows)))
    if name:
        shape.name = name
    t = shape.table
    for i, wd in enumerate(widths):
        t.columns[i].width = Inches(wd)
    for r, row in enumerate(rows):
        t.rows[r].height = Inches(0.27)
        for ci, val in enumerate(row):
            cell = t.cell(r, ci)
            cell.margin_left = cell.margin_right = Inches(0.06)
            cell.margin_top = cell.margin_bottom = Inches(0.02)
            cell.fill.solid()
            cell.fill.fore_color.rgb = C[header_fill] if r == 0 else (C["white"] if r % 2 else C["light"])
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = ""
            run = p.add_run()
            run.text = str(val)
            run.font.size = Pt(size)
            run.font.bold = r == 0 or (status_col is not None and ci == status_col)
            color = "white" if r == 0 else "ink"
            if r and status_col is not None and ci == status_col:
                color = {"PARTIAL": "amber", "FAIL": "red", "BLOCKED": "muted", "PASS": "green",
                         "LIVE": "green", "OFF": "red"}.get(str(val).split()[0], "ink")
            run.font.color.rgb = C[color]
    return shape


TEMPLATE = {}


def new_slide(prs, layout_name, title):
    """A slide on the deck's layout whose title and slide-number shapes are copies of an original content slide's,
    so position, insets, colour and the live page-number field match exactly."""
    tpl = TEMPLATE["slide"]
    s = prs.slides.add_slide(tpl.slide_layout)
    for ph in list(s.placeholders):
        ph._element.getparent().remove(ph._element)
    for sh in tpl.shapes:
        if sh.is_placeholder:
            s.shapes._spTree.append(copy.deepcopy(sh._element))
    ph = next(p for p in s.placeholders if p.placeholder_format.idx == 100)
    ph.text_frame.paragraphs[0].runs[0].text = title
    return s


def add_to_section(prs, slide, after_slide_id):
    """Sections (p14:sectionLst) list slide ids; put the new slide right after its neighbour's id, in its section."""
    ns = "{http://schemas.microsoft.com/office/powerpoint/2010/main}"
    for sld in prs.part._element.iter(ns + "sldId"):
        if int(sld.get("id")) == after_slide_id:
            new = copy.deepcopy(sld)
            new.set("id", str(slide.slide_id))
            sld.addnext(new)
            return
    raise SystemExit(f"slide id {after_slide_id} is in no section")


def move_after(prs, slide, after_index):
    """Put `slide` right after the slide at 0-based `after_index` (python-pptx appends at the end)."""
    lst = prs.slides._sldIdLst
    ids = list(lst)
    el = next(e for e in ids if int(e.get("id")) == slide.slide_id)
    lst.remove(el)
    lst.insert(after_index + 1, el)


# ---------------------------------------------------------------- the slides
def scenario_slide(prs):
    s = new_slide(prs, "CONTENT", "One run, every brief item, ends in escalation")
    rows = [["#", "Brief item", "What the driver saw", "Proof"],
            ["1", "Delay + clarification", "\"What time do you expect to reach the gate?\" — one question, then slots", "1 ask, then options"],
            ["2", "Later options, compared", "\"anything after 05:52?\" — a new list at 6:00 AM", "both lists kept"],
            ["3", "Many requests, one schedule", "B and C offered the same first slot", "same facility, same time"],
            ["4", "Two compete for capacity", "B: \"Request sent… NOT confirmed yet\"", "planner CONFIRMs in console"],
            ["5", "Option disappears", "C: \"That slot was just taken.\" + what is left", "1 active booking on D1 4:00"],
            ["7", "Facility schedule proposal", "Planner sees a sequence; nothing changes", "PROPOSAL, 0 bookings changed"],
            ["6", "No safe outcome → person", "\"A coordinator has your details…\"", "escalation + hand-off record"]]
    table(s, 0.5, 1.15, 9.0, rows, [0.35, 2.15, 4.1, 2.4], size=10, name="Scenario table")
    text(s, 0.5, 3.6, 9.0, 0.6, [[("Real: ", {"bold": True}), "signed webhook, API, database roles and locks, rules "
         "reader (no AI cost), approval and scheduling code. Local: WhatsApp, workflow engine, PostgreSQL 18.6. "
         "The deployed pilot's booking stays OFF. Found and fixed: \"after 05:26\" was read as 17:26."]],
         size=11, color="muted", name="Scenario note")
    return s


def scheduling_slide(prs):
    s = new_slide(prs, "CONTENT", "Facility schedule: a proposal, never a booking")
    text(s, 0.5, 1.1, 4.3, 0.35, "Early, late and unloading trucks at one facility", size=14, bold=True, name="Sched head")
    rows = [["Rule", "Changed promises", "Kept", "Weighted wait", "Applies one by one"],
            ["First come, first served", "5", "0", "95", "no (3 in swaps)"],
            ["Earliest arrival", "4", "1", "112", "no (3 in swaps)"],
            ["Facility score", "1", "4", "185", "yes"]]
    table(s, 0.5, 1.5, 5.4, rows, [1.7, 1.05, 0.55, 0.95, 1.15], size=10, name="Sched table")
    text(s, 0.5, 2.75, 5.4, 1.9, [
        [("Where it wins: ", {"bold": True}), "drivers keep the times they were told; every change can be applied safely."],
        [("Where it loses: ", {"bold": True}), "waiting. The early truck waits 75 min for its slot. On the real Jaipur "
         "snapshot, earliest-arrival scores best overall; five double-booked trucks go to a person under every rule."]],
        size=11, name="Sched reading")
    card(s, 6.2, 1.1, 3.3, 2.75, name="Sched card")
    text(s, 6.35, 1.2, 3.0, 2.6, [
        [("How it is used", {"bold": True, "size": 13, "color": "navy"})],
        "Reads structured facts only; driver text never reaches it.",
        "Trucks already unloading are fixed.",
        "Planners view and save proposals; only an ops lead applies a move.",
        "Each move is re-checked by the database under the booking locks; a stale proposal is refused.",
        "Moves stay off while booking is off."], size=11, name="Sched usage")
    return s


def status_slide(prs):
    s = new_slide(prs, "CONTENT", "Option A is live; Option D is built and switched off")
    card(s, 0.5, 1.1, 2.9, 1.3, name="Option A card")
    text(s, 0.62, 1.18, 2.7, 1.2, [[("Option A — LIVE", {"bold": True, "color": "green", "size": 14})],
         "AWS Sydney, synthetic data. Reads, offers slots, replies to an internal test inbox. Staff console with "
         "two-step sign-in."], size=11, name="Option A text")
    card(s, 0.5, 2.6, 2.9, 1.6, name="Option D card")
    text(s, 0.62, 2.68, 2.7, 1.5, [[("Option D — OFF", {"bold": True, "color": "red", "size": 14})],
         "Booking requests, approvals, schedule moves: built and tested. Turns on only with all six gates current "
         "and the owner's written approval."], size=11, name="Option D text")
    rows = [["Gate (measured live, 22 Nov)", "Status", "What is missing"],
            ["Queue", "PARTIAL", "dead-letter drill (needs approval)"],
            ["Recovery worker", "PARTIAL", "forced lease-loss test"],
            ["Workload identity", "PARTIAL", "check 5: second staff user"],
            ["Database", "FAIL (version)", "17.9 vs 18.3+: owner exception T33-P"],
            ["Approval workflow", "BLOCKED", "live run needs approval"],
            ["Outbound delivery", "BLOCKED", "Meta test number + templates"]]
    table(s, 3.65, 1.1, 5.85, rows, [2.45, 1.15, 2.25], size=10, status_col=1, name="Gate table")
    text(s, 3.65, 3.25, 5.85, 1.4, "No gate has passed, so no evidence row was written and booking cannot start. "
         "Real WhatsApp and production stay off.", size=11, color="muted", name="Gate note")
    return s


def break_slide(prs):
    s = new_slide(prs, "CONTENT", "Break report: 141 attacks, 0 failures")
    stats = [("114", "attack and failure cases passed in tests run 23 Nov, or live on AWS"),
             ("23", "scheduling and move cases: 17 closed, 5 mitigated, 1 accepted"),
             ("1 of 20", "won when 20 drivers hit one slot within 5 ms; 19 told and re-offered"),
             ("2", "blocked on the owner: live second staff user, live dead-letter drill")]
    for i, (big, small) in enumerate(stats):
        x = 0.5 + i * 2.3
        card(s, x, 1.2, 2.1, 1.65, name=f"Break card {i+1}")
        text(s, x + 0.12, 1.3, 1.9, 0.6, big, size=26, bold=True, color="navy", name=f"Break stat {i+1}")
        text(s, x + 0.12, 1.95, 1.9, 1.1, small, size=11, name=f"Break label {i+1}")
    text(s, 0.5, 3.1, 9.0, 1.4, [
        [("Areas: ", {"bold": True}), "competing requests, locks, duplicates, idempotency, stale options, callbacks, "
         "worker failure and recovery, outbox, approval bypass, roles, facility isolation, malformed input, booking "
         "switch, live AWS security, scheduling manipulation, unloading trucks."],
        [("Not covered: ", {"bold": True}), "lock order for a future bulk admin path; a cap on the inbound WhatsApp "
         "message length. Full report: docs/challenge/BREAK_REPORT.md."]], size=11, name="Break areas")
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("--tests", required=True, help="final test count for the evidence slide, e.g. 1,1xx")
    a = ap.parse_args()
    prs = Presentation(a.deck)
    titles = [next((sh.text_frame.text for sh in sl.shapes if sh.is_placeholder and sh.placeholder_format.idx == 100), "")
              for sl in prs.slides]
    if any(t.startswith("Option A is live") for t in titles):
        raise SystemExit("deck already updated")
    integrity = titles.index(next(t for t in titles if t.startswith("50 drivers, one slot")))
    evidence = titles.index("What we can show, and what is still open")
    broke = titles.index("How we broke our own system")
    for sh in prs.slides[evidence].shapes:                       # the one changed existing text
        if sh.has_text_frame and sh.text_frame.text == "800+":
            sh.text_frame.paragraphs[0].runs[0].text = a.tests
        if sh.has_text_frame and sh.text_frame.text.startswith("automated tests on PostgreSQL 18"):
            sh.text_frame.paragraphs[0].runs[0].text = "automated tests (23 Nov 2026), incl. race, flow, failure and scheduling"
            for r in sh.text_frame.paragraphs[0].runs[1:]:
                r.text = ""
    TEMPLATE["slide"] = prs.slides[evidence]
    sc, sd, st, br = scenario_slide(prs), scheduling_slide(prs), status_slide(prs), break_slide(prs)
    # order: ... integrity, [scenario], ... evidence, [scheduling], [status], ... broke, [break]
    sid = [sl.slide_id for sl in prs.slides]
    move_after(prs, br, broke)
    move_after(prs, st, evidence)
    move_after(prs, sd, evidence)
    move_after(prs, sc, integrity)
    add_to_section(prs, br, sid[broke])
    add_to_section(prs, st, sid[evidence])
    add_to_section(prs, sd, sid[evidence])
    add_to_section(prs, sc, sid[integrity])
    prs.save(a.deck)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
