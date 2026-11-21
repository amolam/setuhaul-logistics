const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fa = require("react-icons/fa");
const { applyTheme } = require("C:/Users/Administrator/AppData/Local/Claude-Data/claude_pzs8sxrjxfjjc/local-agent-mode-sessions/skills-plugin/ead17a4b-e865-47cc-8f37-af6723c981f7/72829ed9-818e-487b-9594-b9708605c9a7/skills/pptx/scripts/apply_theme.js");

const OUT = process.argv[2];

const THEME = {
  name: "SetuHaul Road",
  headFontFace: "Arial",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1B2333", lt1: "FFFFFF", dk2: "14213D", lt2: "EEF1F5",
    accent1: "F2A007", accent2: "14213D", accent3: "2E7D5B", accent4: "C0392B", accent5: "5B6B82", accent6: "3A6EA5",
    hlink: "3A6EA5", folHlink: "5B6B82",
  },
};
const HEX = { navy: "14213D", amber: "F2A007", ink: "1B2333", muted: "5B6B82", line: "D5DBE4", light: "EEF1F5", green: "2E7D5B", red: "C0392B" };

async function icon(Comp, color, size = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color: "#" + color, size: String(size) }));
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

(async () => {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9"; // 10 x 5.625
  pres.title = "SetuHaul: one dock slot, one promise";
  pres.author = "SetuHaul FDE team";
  pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
  const C = pres.SchemeColor;

  // ---------------------------------------------------------------- layouts
  pres.defineSlideMaster({
    title: "DARK_TITLE",
    background: { color: C.text2 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 1.5, w: 8.8, h: 1.5, fontSize: 40, bold: true, color: C.background1, valign: "bottom", align: "left", margin: 0 }, text: "" } },
      { placeholder: { options: { name: "body", type: "body", x: 0.6, y: 3.15, w: 8.8, h: 1.2, fontSize: 18, color: C.accent1, valign: "top", margin: 0 }, text: "" } },
    ],
  });
  pres.defineSlideMaster({
    title: "CONTENT",
    background: { color: C.background1 },
    margin: [0.5, 0.5, 0.5, 0.5],
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9.0, h: 0.75, fontSize: 26, bold: true, color: C.text2, valign: "middle", align: "left", margin: 0 }, text: "" } },
      { text: { text: "SetuHaul · FDE challenge", options: { x: 0.5, y: 5.25, w: 4, h: 0.25, fontSize: 10, color: C.accent5, margin: 0 } } },
    ],
    slideNumber: { x: 9.0, y: 5.25, w: 0.5, h: 0.25, fontSize: 10, color: C.accent5, align: "right" },
  });
  pres.defineSlideMaster({
    title: "DARK_CONTENT",
    background: { color: C.text2 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 9.0, h: 0.75, fontSize: 26, bold: true, color: C.background1, valign: "middle", align: "left", margin: 0 }, text: "" } },
    ],
    slideNumber: { x: 9.0, y: 5.25, w: 0.5, h: 0.25, fontSize: 10, color: C.background2, align: "right" },
  });

  const I = {
    truck: await icon(fa.FaTruck, HEX.navy), comment: await icon(fa.FaWhatsapp, HEX.navy), brain: await icon(fa.FaRobot, HEX.navy),
    cogs: await icon(fa.FaCogs, HEX.navy), db: await icon(fa.FaDatabase, HEX.navy), user: await icon(fa.FaUserCheck, HEX.navy),
    lock: await icon(fa.FaLock, HEX.navy), flask: await icon(fa.FaFlask, HEX.navy), search: await icon(fa.FaSearch, HEX.navy),
    shield: await icon(fa.FaShieldAlt, HEX.navy), bolt: await icon(fa.FaBolt, HEX.navy), warn: await icon(fa.FaExclamationTriangle, HEX.navy),
    play: await icon(fa.FaPlay, HEX.navy), ban: await icon(fa.FaBan, HEX.navy), random: await icon(fa.FaRandom, HEX.navy),
    phone: await icon(fa.FaPhoneAlt, HEX.navy), question: await icon(fa.FaQuestion, HEX.navy), list: await icon(fa.FaClipboardList, HEX.navy),
  };
  // icon in amber circle (the deck's motif)
  function badge(slide, img, x, y, d = 0.55, name = "icon") {
    slide.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color: C.accent1 }, line: { type: "none" }, objectName: name + " circle" });
    const p = d * 0.26;
    slide.addImage({ data: img, x: x + p, y: y + p, w: d - 2 * p, h: d - 2 * p, objectName: name, altText: name });
  }
  function card(slide, x, y, w, h, name, fill = C.background2) {
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: fill }, line: { type: "none" }, objectName: name });
  }
  const T = (slide, text, opts) => slide.addText(text, Object.assign({ isTextBox: true, margin: 0, color: C.text1, fontSize: 14, valign: "top" }, opts));

  // ================================================================ MAIN
  pres.addSection({ title: "Main story" });

  // ---- 1. Title
  let s = pres.addSlide({ masterName: "DARK_TITLE", sectionTitle: "Main story" });
  s.addText("Ten drivers, one dock slot, one promise", { placeholder: "title" });
  s.addText("SetuHaul driver-exception service · FDE challenge readout", { placeholder: "body" });
  s.addText("WhatsApp in, warehouse decides, database guarantees", { isTextBox: true, x: 0.6, y: 4.6, w: 8.8, h: 0.4, fontSize: 14, color: C.background2, margin: 0 });
  s.addNotes(`[0:00-0:20] Title. One sentence: "Drivers message SetuHaul when they run late. We built the service that answers them, and we'll prove live that it stays correct when many drivers want the same dock slot."
Then move straight on.`);

  // ---- 2. Problem in one message
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Main story" });
  s.addText("One message holds only part of the truth", { placeholder: "title" });
  card(s, 0.5, 1.25, 9.0, 0.85, "driver message card", C.background2);
  badge(s, I.comment, 0.7, 1.4, 0.55, "whatsapp icon");
  T(s, "\"Tyre damaged near Neemrana. Around 90 minutes late. Can I get something after 7? I also have another pickup tonight.\"",
    { x: 1.45, y: 1.32, w: 7.9, h: 0.7, fontSize: 15, italic: true, valign: "middle" });
  // two columns
  T(s, "Ask the driver", { x: 0.5, y: 2.3, w: 4.3, h: 0.3, fontSize: 16, bold: true, color: C.text2 });
  T(s, [
    { text: "Is the tyre fixed? Is anyone in danger?", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "90 minutes of repair, or 90 minutes late?", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Latest time you can finish unloading", options: { bullet: { indent: 15 } } },
  ], { x: 0.5, y: 2.65, w: 4.3, h: 1.2, fontSize: 14, paraSpaceAfter: 4 });
  T(s, "Look it up, never ask", { x: 5.2, y: 2.3, w: 4.3, h: 0.3, fontSize: 16, bold: true, color: C.text2 });
  T(s, [
    { text: "Which shipment, appointment and priority", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Which docks fit this truck and load", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Is a slot already held or confirmed?", options: { bullet: { indent: 15 } } },
  ], { x: 5.2, y: 2.65, w: 4.3, h: 1.2, fontSize: 14, paraSpaceAfter: 4 });
  // stat strip
  const stats = [["20–35", "driver messages in a 30-min disruption spike"], ["32", "dock doors across 6 facilities"], ["0", "live GPS feeds: the ETA is the driver's word"]];
  stats.forEach(([n, l], i) => {
    const x = 0.5 + i * 3.05;
    card(s, x, 4.0, 2.85, 1.05, "stat " + n, C.background2);
    const nw = i === 0 ? 1.3 : 0.75;
    T(s, n, { x: x + 0.15, y: 4.08, w: nw, h: 0.85, fontSize: 30, bold: true, color: C.text2, valign: "middle", fontFace: "Arial" });
    T(s, l, { x: x + 0.25 + nw, y: 4.08, w: 2.45 - nw, h: 0.85, fontSize: 12, color: C.accent5, valign: "middle" });
  });
  s.addNotes(`[0:20-1:15] The brief's own example. Point: an FDE splits what to ASK from what to LOOK UP. Asking for a fact the backend holds wastes the driver's time; trusting the driver over the backend makes a wrong promise.
Then the scale: 20-35 messages in half an hour during rain or a road closure, competing for a handful of docks. So the problem is not one chat. It is many chats racing for the same scarce slot.`);

  // ---- 3. Where the AI stops
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Main story" });
  s.addText("The model reads; code and people decide", { placeholder: "title" });
  const lanes = [
    [I.comment, "Driver", "WhatsApp, English, Hindi or Hinglish"],
    [I.brain, "Model reads", "Claude Haiku 4.5 turns free text into facts. Nothing else"],
    [I.cogs, "Code decides", "Feasibility, state machine, which tool runs next"],
    [I.db, "Database commits", "One booking per dock and time, enforced by constraint"],
    [I.user, "Person approves", "Planner confirms; safety and no-slot go to a coordinator"],
  ];
  lanes.forEach(([img, h, d], i) => {
    const x = 0.5 + i * 1.84;
    card(s, x, 1.3, 1.66, 2.2, "lane " + h);
    badge(s, img, x + 0.55, 1.45, 0.56, h + " icon");
    T(s, h, { x: x + 0.04, y: 2.12, w: 1.58, h: 0.35, fontSize: 13, bold: true, color: C.text2, align: "center" });
    T(s, d, { x: x + 0.1, y: 2.5, w: 1.46, h: 1.15, fontSize: 11, color: C.text1, align: "center" });
    if (i < 4) s.addShape(pres.shapes.ISOSCELES_TRIANGLE, { x: x + 1.67, y: 2.46, w: 0.2, h: 0.14, rotate: 90, fill: { color: C.accent5 }, line: { type: "none" }, objectName: "arrow " + i, flipH: false });
  });
  card(s, 0.5, 3.75, 9.0, 1.2, "invariant card", C.text2);
  T(s, [
    { text: "The agent requests; the application and the database decide.", options: { bold: true, breakLine: true, color: C.background1 } },
    { text: "The agent's database role cannot confirm, cannot write driver messages, and cannot forge a driver's YES. If the model is down, a rules reader takes over; capacity never depended on the model.", options: { color: C.background2, fontSize: 12 } },
  ], { x: 0.75, y: 3.85, w: 8.5, h: 1.0, fontSize: 15, valign: "middle" });
  s.addNotes(`[1:15-2:30] The brief asks: workflow or agent? Our answer: an agent only at the edges, where language is messy. Everything with a business consequence is code, a database constraint, or a person.
- The model only extracts facts (forced tool call, strict schema). Since owner decision OD-3, no model writes replies: they are templates in English, Hindi and Hinglish.
- Code picks every tool call from a state machine.
- The database makes double booking impossible (exclusion constraint).
- A planner confirms bookings; safety and no-slot cases go to a coordinator.
Line to land: "The agent requests; the app and the database decide."`);

  // ---- 4. Live demo
  s = pres.addSlide({ masterName: "DARK_CONTENT", sectionTitle: "Main story" });
  s.addText("Live: four beats, about six minutes", { placeholder: "title" });
  const beats = [
    ["A", "Late driver", "Delay, one clarifying question, options compared, YES, planner confirms, driver told", "2:00", I.truck],
    ["B", "Two drivers, one slot", "Both say yes at the same instant. One wins; the other hears why, with fresh options", "1:30", I.random],
    ["C", "Option disappears", "A dock breaks down while the request waits. Request cancelled, driver told", "1:15", I.ban],
    ["D", "No safe outcome", "No dock fits. Escalation with a full hand-off record, never an invented slot", "1:15", I.phone],
  ];
  beats.forEach(([k, h, d, t, img], i) => {
    const x = 0.5 + (i % 2) * 4.6, y = 1.3 + Math.floor(i / 2) * 1.75;
    card(s, x, y, 4.4, 1.5, "beat " + k, C.background1);
    badge(s, img, x + 0.2, y + 0.2, 0.6, "beat " + k + " icon");
    T(s, `${k}. ${h}`, { x: x + 0.95, y: y + 0.2, w: 2.6, h: 0.4, fontSize: 16, bold: true, color: C.text2 });
    T(s, t, { x: x + 3.55, y: y + 0.2, w: 0.65, h: 0.4, fontSize: 13, color: C.accent5, align: "right" });
    T(s, d, { x: x + 0.95, y: y + 0.65, w: 3.25, h: 0.75, fontSize: 13, color: C.text1 });
  });
  T(s, "Real model (Haiku 4.5) for driver messages · switchable to the rules reader · beat C is the first to drop if time is short",
    { x: 0.5, y: 5.0, w: 8.4, h: 0.35, fontSize: 11, color: C.background2 });
  s.addNotes(`[2:30-8:30] SWITCH TO THE DEMO CONSOLE. Run the Presentation flow, one button per beat.
Say first: "Driver messages go to the real model; I can switch to the rules reader if the network misbehaves."
A (2:00): read the options aloud; point at "NOT confirmed yet" after the YES (showing is not reserving, reserving is not confirming). Planner confirms in the console; the phone gets the confirmation.
B (1:30): race with 2 drivers. Point at the loser's phone: "Another truck confirmed D2 a moment ago. Here are the current options." Plain words, never a bare no.
C (1:15): block the dock (breakdown) while the request waits. Request INVALIDATED in the same transaction; the driver is told; a planner can no longer confirm it.
D (1:15): no dock fits this truck. Open the hand-off record in Escalations: shipment, what was asked, why nothing fits, deadline.
If a faculty question lands mid-demo, answer it and drop beat C.`);

  // ---- 5. Integrity proof
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Main story" });
  s.addText("50 drivers, one slot: one booking, every time", { placeholder: "title" });
  const big = [["50", "drivers say YES to the same dock and minute, released at once"], ["1", "booking committed"], ["49", "told \"taken a moment ago\" with fresh options"], ["0", "overlapping bookings in the database"]];
  big.forEach(([n, l], i) => {
    const y = 1.25 + i * 0.95;
    card(s, 0.5, y, 4.3, 0.82, "proof stat " + n, C.background2);
    T(s, n, { x: 0.65, y: y + 0.05, w: 1.0, h: 0.72, fontSize: 34, bold: true, color: i === 3 ? C.accent3 : C.text2, valign: "middle", fontFace: "Arial" });
    T(s, l, { x: 1.5, y: y + 0.05, w: 3.2, h: 0.72, fontSize: 13, valign: "middle" });
  });
  T(s, "Why it holds", { x: 5.2, y: 1.25, w: 4.3, h: 0.35, fontSize: 16, bold: true, color: C.text2 });
  const layers = [
    [I.db, "Constraint", "No two active rows may overlap on one dock: holds, bookings, blocks and trucks in a dock"],
    [I.lock, "Lock per dock", "Racers queue instead of deadlocking; losers get a clean LOST"],
    [I.shield, "Re-check at commit", "A shown option is re-proven when held and again when a planner confirms"],
    [I.search, "Test and checker", "Race tests in CI plus a query that self-joins active occupancy"],
  ];
  layers.forEach(([img, h, d], i) => {
    const y = 1.7 + i * 0.86;
    badge(s, img, 5.2, y, 0.5, h + " icon");
    T(s, h, { x: 5.85, y: y - 0.02, w: 3.65, h: 0.28, fontSize: 13, bold: true, color: C.text2 });
    T(s, d, { x: 5.85, y: y + 0.26, w: 3.65, h: 0.55, fontSize: 11 });
  });
  s.addNotes(`[8:30-10:00] Press "Race for one slot" with N = 50 in the console (or show this slide if time is short).
The point of the brief: "Code, not a prompt, decides feasibility and commitment."
- Layer 1 is the database: an exclusion constraint on (dock, time range) for every active occupancy row. Even buggy application code cannot write a double booking.
- Layer 2: a per-dock advisory lock. Our first contention run hit PostgreSQL deadlocks (40P01); the lock is the fix. Clean losers, no errors.
- Earlier validation: 10, 50 and 200 parallel holds over 10-30 rounds gave exactly one winner every round; 150 random overlapping holds gave 0 overlaps.
- Losers are not just refused: they get fresh options in the same reply (decision D11).`);

  // ---- 6. Evidence and open risks
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Main story" });
  s.addText("What we can show, and what is still open", { placeholder: "title" });
  const ev = [["95%", "of 167 held-out driver messages read correctly by the model (rules reader: 59%)"], ["0", "hard-gate violations in the end-to-end replay of 64 scenarios"], ["800+", "automated tests on PostgreSQL 18, incl. race, flow and failure tests"], ["$0.0035", "per message read; app-enforced cap of $0.25 per day"]];
  ev.forEach(([n, l], i) => {
    const x = 0.5 + (i % 2) * 2.3, y = 1.25 + Math.floor(i / 2) * 1.9;
    card(s, x, y, 2.15, 1.75, "evidence " + n, C.background2);
    T(s, n, { x: x + 0.15, y: y + 0.12, w: 1.9, h: 0.6, fontSize: 28, bold: true, color: C.text2, fontFace: "Arial" });
    T(s, l, { x: x + 0.15, y: y + 0.75, w: 1.9, h: 0.95, fontSize: 11, color: C.text1 });
  });
  T(s, "Open risks", { x: 5.4, y: 1.25, w: 4.1, h: 0.35, fontSize: 16, bold: true, color: C.accent4 });
  T(s, [
    { text: "Live booking is NO-GO until six AWS deployment gates have evidence (queue, Aurora, workers, workflow, identity, outbound)", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Pilot runs on synthetic data via OpenRouter; real driver data needs India-region inference", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Deferred: a \"talk to a person\" intent, and device ETA signals (FASTag, location pin)", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Needs SetuHaul input: confirmation windows, priority weights, coordinator response times", options: { bullet: { indent: 15 } } },
  ], { x: 5.4, y: 1.65, w: 4.1, h: 3.4, fontSize: 13, paraSpaceAfter: 8 });
  s.addNotes(`[10:00-10:30] Evidence, not claims. Keep this to 30 seconds; the next slide is cost.
- Held-out set: 167 paraphrased messages written by a different model that never saw our prompt, frozen before any run. Haiku 158/167, rules 99/167. So the prompt is not overfitted.
- Replay: every golden scenario through the real API, orchestrator and database: 64/64 scripted, 61/64 with the live model, zero hard-gate violations. The replay found 12 bugs before any reviewer did.
- Cost: about a third of a cent per message; the application refuses calls above $0.25 per day, plus a $3/month limit on the key.
Open risks, said plainly: production booking is switched off until the AWS gates have evidence. That is a deliberate NO-GO, not a gap we hid.`);

  // ---- 6b. Running cost
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Main story" });
  s.addText("Running cost: $440 a month, $2,060 at 10×", { placeholder: "title" });
  const cats = ["AI model", "Database", "Security", "Monitoring", "Agent runtime", "Serverless glue"];
  s.addChart(pres.charts.BAR, [
    { name: "100 conversations/day", labels: cats, values: [120, 174, 65, 35, 20, 23] },
    { name: "1,000 conversations/day (10×)", labels: cats, values: [1200, 307, 115, 120, 200, 120] },
  ], {
    x: 0.5, y: 1.15, w: 5.6, h: 3.95, barDir: "bar", barGrouping: "clustered", barGapWidthPct: 60,
    chartColors: ["3A6EA5", "E08A00"],
    catAxisOrientation: "maxMin", valAxisHidden: true, valGridLine: { style: "none" }, catGridLine: { style: "none" },
    catAxisLabelColor: "1B2333", catAxisLabelFontSize: 11, catAxisLabelFontFace: "+mn-lt", catAxisLineShow: false,
    showValue: true, dataLabelPosition: "outEnd", dataLabelFormatCode: "$#,##0", dataLabelFontSize: 10, dataLabelColor: "1B2333", dataLabelFontFace: "+mn-lt",
    showLegend: true, legendPos: "t", legendFontSize: 11, legendFontFace: "+mn-lt", legendColor: "1B2333",
    showTitle: true, title: "AWS cost per month (USD), Mumbai list prices", titleFontSize: 12, titleColor: "1B2333", titleFontFace: "+mn-lt",
    objectName: "monthly cost by area chart", altText: "Monthly AWS cost by area at 100 and 1,000 conversations per day",
  });
  const cst = [["4.7×", "cost for 10× the load: fixed costs (database, security) barely move"], ["$0.15 → $0.07", "per conversation, at 100 and at 1,000 a day"], ["58%", "of the 10× bill is the model: the line to watch"]];
  cst.forEach(([n, l], i) => {
    const y = 1.15 + i * 1.33;
    card(s, 6.4, y, 3.1, 1.18, "cost stat " + i, C.background2);
    T(s, n, { x: 6.55, y: y + 0.1, w: 2.85, h: 0.45, fontSize: 22, bold: true, color: C.text2, fontFace: "Arial" });
    T(s, l, { x: 6.55, y: y + 0.55, w: 2.85, h: 0.6, fontSize: 11 });
  });
  s.addNotes(`[~10:30, 45 s] Design-phase cost model for a production AWS deployment in Mumbai (docs/2-design/02_architecture.md §7), from the AWS price list of 17 Nov 2026.
- 100 conversations a day: about $440 a month. 10x (1,000 a day): about $2,060. So 10x the load costs 4.7x, because the database, security and monitoring are mostly fixed.
- Per conversation that is about 15 cents, falling to about 7 cents.
- The model dominates at scale ($1,200 of $2,060). That estimate is conservative: it assumed $0.04 per conversation including a Sonnet reply call. Since OD-3 replies are templates, and measured extraction is $0.0013-0.0035 per message, so the real model line should be well below this.
- Not included: Meta WhatsApp fees and FASTag/SIM vendor fees (quotes pending).
- Pilot today: $0.25/day model cap, $3/month key limit.`);

  // ---- 7. Questions
  s = pres.addSlide({ masterName: "DARK_TITLE", sectionTitle: "Main story" });
  s.addText("Questions", { placeholder: "title" });
  s.addText("The model reads · code and the database decide · a person owns every high-risk call", { placeholder: "body" });
  s.addNotes(`[11:00-15:00] Questions. Use the appendix slides to answer:
- "How did you audit the data?" -> Data audit
- "What states does a booking go through?" -> Booking states
- "Who wins when capacity is short?" -> Allocation policy
- "When does a person step in?" -> Human in the loop
- "Duplicates, retries, stale options?" -> Failure handling
- "How did you break your own system?" -> How we broke it
- "Why OpenRouter / what does the model cost?" -> Model and cost
- "What does the infrastructure cost, line by line?" -> Cost model
- "How does this map to the brief's measures?" -> Measures scorecard`);

  // ================================================================ APPENDIX
  pres.addSection({ title: "Appendix" });

  // A1 data audit
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("Data audit: 30 checks, each proven by a query", { placeholder: "title" });
  const sev = [["9", "HIGH"], ["13", "MEDIUM"], ["5", "LOW"], ["3", "INFO"]];
  sev.forEach(([n, l], i) => {
    card(s, 0.5, 1.25 + i * 0.95, 1.9, 0.8, "severity " + l, C.background2);
    T(s, n, { x: 0.65, y: 1.3 + i * 0.95, w: 0.7, h: 0.7, fontSize: 26, bold: true, color: i === 0 ? C.accent4 : C.text2, valign: "middle", fontFace: "Arial" });
    T(s, l, { x: 1.35, y: 1.3 + i * 0.95, w: 1.0, h: 0.7, fontSize: 13, color: C.accent5, valign: "middle" });
  });
  const auditRows = [
    ["A01", "Same driver on overlapping in-flight shipments"],
    ["A03", "Booked dock's weight limit below the load"],
    ["A06", "Confirmed booking already infeasible from the driver's latest ETA"],
    ["A08", "Confirmed booking on a dock with an open breakdown"],
    ["A12", "Historical double booking on one slot"],
    ["A23", "Pending confirmations with no expiry"],
    ["A30", "What the two unique indexes do not cover"],
  ];
  s.addTable([[{ text: "ID", options: { bold: true, color: HEX.navy } }, { text: "Example finding", options: { bold: true, color: HEX.navy } }],
    ...auditRows.map(r => [r[0], r[1]])], {
    x: 2.7, y: 1.25, w: 6.8, colW: [0.7, 6.1], fontSize: 12, color: HEX.ink, fontFace: "Calibri",
    border: { type: "solid", pt: 0.5, color: HEX.line }, rowH: 0.42, valign: "middle",
  });
  s.addNotes("Full list with SQL and result rows: docs/1-frame/audit_results.md. Key point: the database already had two partial unique indexes, but they cover slot rows, not time. An unload that spills past its slot end (A04) or a dock breakdown (A08) was invisible to them. That gap drove the exclusion constraint on real time ranges.");

  // A2 booking states
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("Showing is not reserving, reserving is not confirming", { placeholder: "title" });
  const states = [["Shown", "Agent, read-only"], ["Selected", "Driver picks \"2\""], ["Held", "Driver's explicit YES to the read-back"], ["Pending", "Request sent to the warehouse"], ["Confirmed", "Planner, or an authorised rule"]];
  states.forEach(([h, d], i) => {
    const x = 0.5 + i * 1.84;
    card(s, x, 1.35, 1.6, 1.25, "state " + h, i === 4 ? C.accent3 : C.text2);
    T(s, h, { x: x + 0.08, y: 1.45, w: 1.44, h: 0.4, fontSize: 15, bold: true, color: C.background1, align: "center" });
    T(s, d, { x: x + 0.08, y: 1.88, w: 1.44, h: 0.65, fontSize: 11, color: C.background2, align: "center" });
  });
  T(s, "Side exits, each with a plain message to the driver", { x: 0.5, y: 2.9, w: 9, h: 0.35, fontSize: 16, bold: true, color: C.text2 });
  const exits = [["LOST", "Someone else got it; fresh options in the same reply"], ["EXPIRED", "Nobody answered in time; a person is alerted"], ["REJECTED", "Warehouse said no; slot released, alternatives offered"], ["INVALIDATED", "Capacity vanished (breakdown, change); same transaction"]];
  exits.forEach(([h, d], i) => {
    const x = 0.5 + (i % 2) * 4.6, y = 3.35 + Math.floor(i / 2) * 0.85;
    card(s, x, y, 4.4, 0.72, "exit " + h, C.background2);
    T(s, h, { x: x + 0.15, y: y + 0.08, w: 1.4, h: 0.56, fontSize: 13, bold: true, color: C.accent4, valign: "middle" });
    T(s, d, { x: x + 1.55, y: y + 0.08, w: 2.75, h: 0.56, fontSize: 11, valign: "middle" });
  });
  s.addNotes("Decision D6. The driver's YES is an attestation written by the trusted inbound path, bound to the message, driver, shipment, conversation, slot and selection, and usable once. The agent cannot write it, so it cannot forge consent. A later NO always wins.");

  // A3 allocation policy
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("When capacity is short: who gets the slot", { placeholder: "title" });
  const pol = [
    ["1", "First valid commit wins", "Integrity comes from the database. Typing speed gives no business priority: every driver's options come from the same state."],
    ["2", "Priority by policy, not by race", "When a critical load has no option, the facility scheduler proposes a re-sequence of unconfirmed bookings. Moving a confirmed promise needs a planner."],
    ["3", "Fairness guard", "No waiting truck is starved: maximum yard wait of 90 minutes, a hard constraint."],
  ];
  pol.forEach(([n, h, d], i) => {
    const y = 1.25 + i * 1.05;
    s.addShape(pres.shapes.OVAL, { x: 0.5, y, w: 0.5, h: 0.5, fill: { color: C.accent1 }, line: { type: "none" }, objectName: "step " + n });
    T(s, n, { x: 0.5, y, w: 0.5, h: 0.5, fontSize: 16, bold: true, color: C.text2, align: "center", valign: "middle" });
    T(s, h, { x: 1.2, y: y - 0.02, w: 4.3, h: 0.32, fontSize: 14, bold: true, color: C.text2 });
    T(s, d, { x: 1.2, y: y + 0.3, w: 4.3, h: 0.7, fontSize: 11 });
  });
  card(s, 5.9, 1.25, 3.6, 3.8, "solver evidence card", C.background2);
  T(s, "Tested on the competing trucks", { x: 6.1, y: 1.4, w: 3.2, h: 0.35, fontSize: 14, bold: true, color: C.text2 });
  T(s, "−32%", { x: 6.1, y: 1.85, w: 3.2, h: 0.7, fontSize: 36, bold: true, color: C.accent3, fontFace: "Arial" });
  T(s, "weighted waiting with the CP-SAT scheduler versus first-come-first-served, with the fewest broken promises", { x: 6.1, y: 2.55, w: 3.2, h: 0.8, fontSize: 11 });
  T(s, [
    { text: "\"Priority first\" did worse than FCFS: docks sat idle while critical trucks were still on the road", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Unconstrained, the solver left one truck waiting 139 min, hence the fairness guard", options: { bullet: { indent: 15 } } },
  ], { x: 6.1, y: 3.4, w: 3.2, h: 1.55, fontSize: 11, paraSpaceAfter: 4 });
  s.addNotes("Decisions D7 and D8 (the optional scheduling extension). Objective in plain words: less weighted waiting, fewer missed appointments, fewer changes to promises already made. Weights and the 90-minute guard are pending SetuHaul confirmation. Evidence: docs/1-frame/solver_results.md.");

  // A4 human in the loop
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("A person owns every high-risk decision", { placeholder: "title" });
  const modes = [
    [I.user, "Confirm", "Planner confirms a requested booking (all rules ship as planner-required)"],
    [I.warn, "Take over", "Safety: accident, injury, fire. The agent goes silent until a person hands back"],
    [I.phone, "Decide", "No feasible slot, unrepaired breakdown, contradictory data: coordinator gets a hand-off record"],
    [I.list, "Approve", "Moving someone's confirmed promise; after-hours or downgraded docks"],
    [I.search, "Oversee", "Override rates per rule; promote to automation only through change control"],
  ];
  modes.forEach(([img, h, d], i) => {
    const y = 1.2 + i * 0.78;
    badge(s, img, 0.5, y, 0.5, h + " icon");
    T(s, h, { x: 1.15, y, w: 1.3, h: 0.5, fontSize: 14, bold: true, color: C.text2, valign: "middle" });
    T(s, d, { x: 2.45, y: y - 0.05, w: 4.2, h: 0.6, fontSize: 12, valign: "middle" });
  });
  card(s, 6.9, 1.2, 2.6, 3.85, "silence card", C.text2);
  T(s, "Silence is never approval", { x: 7.1, y: 1.4, w: 2.2, h: 0.75, fontSize: 16, bold: true, color: C.accent1 });
  T(s, "A request nobody answers expires, a person is alerted, and the driver is told. 14 triggers across 5 modes, each with a response time and a safe timeout.",
    { x: 7.1, y: 2.2, w: 2.2, h: 2.6, fontSize: 12, color: C.background1 });
  s.addNotes("Decision D12, docs/1-frame/08_human_in_the_loop.md. Owner decision OD-2: a puncture at a dhaba is not an emergency. Unrepaired breakdown without danger is a BREAKDOWN escalation in DECIDE mode; danger words or an unknown safety status lead to a SAFETY takeover. Hand-back (H-5): the driver is told, and the coordinator's note reaches no model.");

  // A5 failure handling
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("Duplicates, retries and stale options", { placeholder: "title" });
  const fh = [
    ["Stale option", "Re-proven at hold. If gone: \"D1 13:00 was just taken; here are the current options\""],
    ["Duplicate message", "Same WhatsApp id stored once; same text from the same driver within 2 min treated as a repeat"],
    ["Queue redelivery", "One turn per message: a processing lease and a fence on every write; a repeat is refused"],
    ["Lost send", "Sent from the stored, hash-checked reply; a timeout becomes UNKNOWN and a receipt settles it, at most one resend"],
    ["Two people at once", "Confirm and reject pressed together: exactly one wins, the driver gets one message"],
  ];
  s.addTable([[{ text: "Problem", options: { bold: true, color: HEX.navy } }, { text: "What happens", options: { bold: true, color: HEX.navy } }], ...fh], {
    x: 0.5, y: 1.25, w: 9.0, colW: [2.2, 6.8], fontSize: 12, color: HEX.ink, fontFace: "Calibri",
    border: { type: "solid", pt: 0.5, color: HEX.line }, rowH: 0.6, valign: "middle",
  });
  s.addNotes("Decision D9 and flows F6, F7, F9. WhatsApp offers no idempotency key, so we never claim exactly-once delivery: at least once, with at most one identical resend. Every case has a test in tests/test_flows.py.");

  // A6 how we broke it
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("How we broke our own system", { placeholder: "title" });
  const broke = [
    [I.lock, "Contention test", "First run hit database deadlocks. Fix: a lock per dock. Then 0 deadlocks"],
    [I.play, "End-to-end replay", "First scripted run: 46/64 with 3 hard-gate violations. 12 bugs fixed, now 64/64"],
    [I.random, "Flow tests F1–F9", "Found 3 defects: a safety message suppressed, two duplicate messages"],
    [I.search, "External review", "Many rounds, each finding fixed and logged. Local build accepted; live booking still NO‑GO"],
    [I.user, "Human walk-through", "Owner found a stuck conversation and missed Hindi times (\"12 baje\"). Both fixed with tests"],
  ];
  broke.forEach(([img, h, d], i) => {
    const y = 1.2 + i * 0.78;
    badge(s, img, 0.5, y, 0.5, h + " icon");
    T(s, h, { x: 1.15, y, w: 2.3, h: 0.5, fontSize: 14, bold: true, color: C.text2, valign: "middle" });
    T(s, d, { x: 3.5, y: y - 0.05, w: 6.0, h: 0.6, fontSize: 12, valign: "middle" });
  });
  s.addNotes("The brief's Break phase. Every finding is in docs/review/FINDINGS_LEDGER.md and REVIEW_LOG.md, traced to a test in TRACEABILITY.md.");

  // A7 model and cost
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("Smallest model, smallest role, capped cost", { placeholder: "title" });
  T(s, [
    { text: "Haiku 4.5 extracts facts only (forced tool call, strict schema)", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Replies are templates in English, Hindi and Hinglish (owner decision OD-3), so no model can promise a slot", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "Pilot provider: OpenRouter pinned to one zero-data-retention route, no fallbacks, synthetic data only", options: { bullet: { indent: 15 }, breakLine: true } },
    { text: "For real driver data: Bedrock with India-region inference (original design)", options: { bullet: { indent: 15 } } },
  ], { x: 0.5, y: 1.25, w: 5.2, h: 3.7, fontSize: 13, paraSpaceAfter: 8 });
  const costs = [["$0.0035", "per extraction"], ["$0.25", "daily cap, enforced by the app"], ["$3", "monthly limit on the key"]];
  costs.forEach(([n, l], i) => {
    const y = 1.25 + i * 1.25;
    card(s, 6.1, y, 3.4, 1.1, "cost " + n, C.background2);
    T(s, n, { x: 6.3, y: y + 0.1, w: 1.6, h: 0.9, fontSize: 26, bold: true, color: C.text2, valign: "middle", fontFace: "Arial" });
    T(s, l, { x: 7.9, y: y + 0.1, w: 1.5, h: 0.9, fontSize: 11, color: C.accent5, valign: "middle" });
  });
  s.addNotes("Decisions T3, T4, T24, T42, T52. Why OpenRouter: the AWS account cannot call Claude in an India region (blocker B-AWS-0), and this pilot uses synthetic data only. Any other upstream provider's answer is refused; if the pinned route is down, the turn falls back to the rules reader, never another provider.");

  // A8 measures scorecard
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("The brief's measures and our evidence", { placeholder: "title" });
  const meas = [
    ["Conflicting or duplicate allocations", "0 in every race test (10, 50 and 200 racers)"],
    ["Options later found infeasible", "Every option re-proven at hold and at confirm; stale ones become LOST with fresh options"],
    ["No-slot escalations handled", "Escalation with a hand-off record; tested in flows and the replay"],
    ["Priority-policy violations", "Policy rules are database rows; one test per rule"],
    ["Driver clarification turns", "Asks only for missing facts; measured per scenario in the replay"],
    ["Resolved without takeover, driver waiting", "Not measurable without live operations: pilot metrics"],
  ];
  s.addTable([[{ text: "Measure", options: { bold: true, color: HEX.navy } }, { text: "Evidence", options: { bold: true, color: HEX.navy } }], ...meas], {
    x: 0.5, y: 1.2, w: 9.0, colW: [3.2, 5.8], fontSize: 12, color: HEX.ink, fontFace: "Calibri",
    border: { type: "solid", pt: 0.5, color: HEX.line }, rowH: 0.52, valign: "middle",
  });
  s.addNotes("Page 9 of the brief. Be honest on the last row: waiting time and automation share need real traffic, so they are pilot metrics, not claims.");

  // A9 cost assumptions
  s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Appendix" });
  s.addText("Cost model: what is in it and what is not", { placeholder: "title" });
  const crow = [
    ["Aurora Serverless v2 + storage + Data API", "$174", "$307"],
    ["Claude on Bedrock (India) + Guardrails", "$120", "$1,200"],
    ["AgentCore Runtime and Gateway", "$20", "$200"],
    ["Lambda, API Gateway, SQS, Step Functions, Cognito, S3", "$23", "$120"],
    ["CloudWatch, X-Ray", "$35", "$120"],
    ["WAF, KMS, Secrets, GuardDuty, Security Hub", "$65", "$115"],
    [{ text: "AWS total per month", options: { bold: true } }, { text: "≈ $440", options: { bold: true } }, { text: "≈ $2,060", options: { bold: true } }],
  ];
  s.addTable([[{ text: "Line", options: { bold: true, color: HEX.navy } }, { text: "100/day", options: { bold: true, color: HEX.navy, align: "right" } }, { text: "1,000/day", options: { bold: true, color: HEX.navy, align: "right" } }],
    ...crow.map(r => [r[0], typeof r[1] === "string" ? { text: r[1], options: { align: "right" } } : Object.assign(r[1], { options: Object.assign(r[1].options, { align: "right" }) }), typeof r[2] === "string" ? { text: r[2], options: { align: "right" } } : Object.assign(r[2], { options: Object.assign(r[2].options, { align: "right" }) })])], {
    x: 0.5, y: 1.2, w: 5.9, colW: [3.9, 1.0, 1.0], fontSize: 11, color: HEX.ink, fontFace: "Calibri",
    border: { type: "solid", pt: 0.5, color: HEX.line }, rowH: 0.42, valign: "middle",
  });
  T(s, "Levers already applied", { x: 6.7, y: 1.2, w: 2.8, h: 0.3, fontSize: 13, bold: true, color: C.text2 });
  T(s, [
    { text: "RDS Data API: no proxy, NAT or endpoints (≈ $200/month saved)", options: { bullet: { indent: 12 }, breakLine: true } },
    { text: "Haiku for extraction; template replies", options: { bullet: { indent: 12 }, breakLine: true } },
    { text: "arm64 Lambdas; dev databases scale to zero", options: { bullet: { indent: 12 } } },
  ], { x: 6.7, y: 1.55, w: 2.8, h: 1.7, fontSize: 11, paraSpaceAfter: 4 });
  T(s, "Not included", { x: 6.7, y: 3.35, w: 2.8, h: 0.3, fontSize: 13, bold: true, color: C.accent4 });
  T(s, [
    { text: "Meta WhatsApp messaging fees", options: { bullet: { indent: 12 }, breakLine: true } },
    { text: "FASTag and SIM-tracking vendor fees (quotes pending)", options: { bullet: { indent: 12 }, breakLine: true } },
    { text: "India Bedrock prices to confirm in the console", options: { bullet: { indent: 12 } } },
  ], { x: 6.7, y: 3.7, w: 2.8, h: 1.4, fontSize: 11, paraSpaceAfter: 4 });
  s.addNotes("Source: docs/2-design/02_architecture.md §7 (AWS public price list for ap-south-1, 17 Nov 2026; marked lines are estimates). Next lever if needed: drop the Aurora reader (about -$65/month) and accept a slower failover; RTO of 30 minutes is still met. The model line predates OD-3 (no model in replies), so it is an upper bound.");

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
})().catch(e => { console.error(e); process.exit(1); });
