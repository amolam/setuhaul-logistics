// SetuHaul hosted staff console (docs/release/11_STAFF_CONSOLE.md), served by the API under /app/ (same origin).
// Sign-in: Cognito hosted page, authorization code with PKCE, public client (no secret here). The access token is
// kept in memory only and sent as a bearer token; the API checks it, the staff group and the facility scope on every
// request. This page decides nothing: what it shows is what the API returns for this person.
"use strict";
const $ = s => document.querySelector(s);
// G.1 (G5): mirrors SHIPMENT_ID_PATTERN in setuhaul/api/app.py (the server validates the same pattern again)
const SHIPMENT_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$";
const LS_UI = ["https://smith.langchain.com/", "https://eu.smith.langchain.com/"];   // links only; never a credential
function extLink(u, text){ return typeof u === "string" && LS_UI.some(p => u.startsWith(p))
  ? `<a class="ext" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : "" }
let CFG = null, TOKEN = null, EXPIRES = 0, tab = "threads", selected = null, nextBefore = null, threads = [];

function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])) }
function when(s){ if(!s) return ""; const d = new Date(s); return isNaN(d) ? esc(s) : d.toLocaleString("en-IN",
  {timeZone:"Asia/Kolkata", day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit"}) }
function toast(t){ const e = $("#toast"); e.textContent = t; e.style.display = "block"; clearTimeout(e._t);
  e._t = setTimeout(() => e.style.display = "none", 6000) }
function key(){ return crypto.randomUUID() }
function b64url(bytes){ return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"") }
function claims(jwt){ try { return JSON.parse(atob(jwt.split(".")[1].replace(/-/g,"+").replace(/_/g,"/"))) } catch { return {} } }
const REDIRECT = () => location.origin + location.pathname;          // exactly https://<api>/pilot/app/

// ---------------------------------------------------------------- Cognito: authorization code + PKCE
async function signIn(){
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem("pkce", JSON.stringify({verifier, state}));       // one sign-in attempt; removed after use
  const q = new URLSearchParams({response_type:"code", client_id:CFG.clientId, redirect_uri:REDIRECT(), scope:"openid",
                                 state, code_challenge:challenge, code_challenge_method:"S256"});
  location.assign(`https://${CFG.cognitoDomain}/oauth2/authorize?${q}`);
}
async function finishSignIn(){
  const u = new URL(location.href), code = u.searchParams.get("code"), state = u.searchParams.get("state");
  const err = u.searchParams.get("error_description") || u.searchParams.get("error");
  history.replaceState(null, "", location.pathname);                       // the code never stays in the address bar
  if(err) throw new Error("Cognito: " + err);
  if(!code) return false;
  const saved = JSON.parse(sessionStorage.getItem("pkce") || "null"); sessionStorage.removeItem("pkce");
  if(!saved || saved.state !== state) throw new Error("sign-in state did not match; please sign in again");
  const r = await fetch(`https://${CFG.cognitoDomain}/oauth2/token`, {method:"POST",
    headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:new URLSearchParams({grant_type:"authorization_code", client_id:CFG.clientId, code, redirect_uri:REDIRECT(),
                              code_verifier:saved.verifier})});
  if(!r.ok) throw new Error("token exchange failed (" + r.status + ")");
  const t = await r.json();
  TOKEN = t.access_token; EXPIRES = Date.now() + (t.expires_in || 1800) * 1000;
  return true;
}
function signOut(){
  TOKEN = null;
  const q = new URLSearchParams({client_id:CFG.clientId, logout_uri:REDIRECT()});
  location.assign(`https://${CFG.cognitoDomain}/logout?${q}`);
}

// ---------------------------------------------------------------- API
async function api(method, path, body){
  if(!TOKEN || Date.now() > EXPIRES - 15000){ TOKEN = null; showSignIn("Your session has ended. Please sign in again."); throw new Error("signed out") }
  const r = await fetch(new URL(CFG.apiBase + path.replace(/^\//, ""), location.href), {method, headers:{Authorization:"Bearer " + TOKEN,
    ...(body ? {"Content-Type":"application/json"} : {})}, body: body ? JSON.stringify(body) : undefined});
  let data = null; try { data = await r.json() } catch {}
  if(r.status === 401){ TOKEN = null; showSignIn("The server refused your session (401). Please sign in again."); throw new Error("401") }
  if(!r.ok){ const e = new Error((data && (data.detail || data.error || data.message)) ? JSON.stringify(data) : "HTTP " + r.status);
    e.status = r.status; throw e }
  return data;
}

// ---------------------------------------------------------------- G.1 (G5): live status pills from /health
// /health is public and read-only (no token): database, booking switch, kill switch, environment. The booking switch and
// the kill switch are shown as two separate pills; nothing says "production"; a failed read says "status unavailable".
function pill(id, text, cls){ const e = $(id); if(!e) return; e.textContent = text; e.className = "pill " + (cls || "") }
// How a reply left (from its provider reference kind; never the reference itself)
const DELIVERY_LABEL = {test_sink: "WhatsApp test sink", telegram_test_sink: "Telegram test sink",
                        telegram: "Telegram", provider: "WhatsApp"};
const TELEGRAM_PILL = {test_bot: ["Telegram: test bot · allowlisted test chats only", "warn"],
                       test_sink: ["Telegram: internal test sink only", ""], off: ["Telegram: off", "unk"],
                       live: ["Telegram: LIVE", "off"]};
async function loadStatus(){
  try {
    const r = await fetch(new URL(CFG.apiBase + "health", location.href), {cache:"no-store"});
    const h = await r.json();
    if(typeof h.booking_enabled !== "boolean") throw new Error("no booking state");
    const env = String(h.environment || "environment unknown");
    pill("#envpill", `${env} · synthetic data only`, "");
    pill("#bookingpill", h.booking_enabled ? "booking execution ON · synthetic data only" : "booking execution OFF",
         h.booking_enabled ? "on" : "off");
    const ks = h.booking_kill_switch;
    if(!h.booking_enabled && h.booking_state !== "local_test_harness") pill("#killpill", "kill switch: not applicable (booking off)", "unk");
    else if(ks === "ENGAGED") pill("#killpill", "kill switch: ENGAGED (bookings refused)", "warn");
    else if(ks === "disengaged") pill("#killpill", "kill switch: disengaged", "on");
    else pill("#killpill", "kill switch: status unavailable", "unk");
    if(h.booking_state === "local_test_harness")      // a local run without a deployment: not the release-gated switch
      pill("#bookingpill", "booking: local test harness · synthetic data (no deployment gates)", "warn");
    if(h.database !== "ok") pill("#bookingpill", "booking execution: status unavailable (database " + (h.database || "?") + ")", "unk");
    const tg = TELEGRAM_PILL[h.telegram] || ["Telegram: status unavailable", "unk"];
    pill("#tgpill", tg[0], tg[1]);
  } catch {
    pill("#tgpill", "Telegram: status unavailable", "unk");
    pill("#envpill", "environment: status unavailable", "unk");
    pill("#bookingpill", "booking execution: status unavailable", "unk");
    pill("#killpill", "kill switch: status unavailable", "unk");
  }
}

// ---------------------------------------------------------------- views
function showSignIn(msg){ $("#app").hidden = true; $("#signout").hidden = true; $("#who").textContent = "";
  $("#signin").hidden = false; $("#autherr").textContent = msg || "" }
function showApp(){
  const c = claims(TOKEN);
  const groups = (c["cognito:groups"] || []).filter(g => !g.startsWith("facility-"));
  $("#who").textContent = `${c.username || ""} · ${groups.join(", ") || "no staff role"} · ${c.facility_scope || "no facility"}`;
  $("#signin").hidden = true; $("#app").hidden = false; $("#signout").hidden = false;
  // what the SERVER derives from the verified token (roles, facilities, permitted actions): the browser cannot change it
  api("GET", "/console/me").then(me => {
    const denied = Object.entries(me.permissions).filter(([, ok]) => !ok).map(([a]) => a);
    $("#who").textContent = `${c.username || ""} · server roles: ${me.roles.join(", ") || "none"} · ${me.facilities.join(", ")}`;
    $("#who").title = "Not permitted: " + (denied.join(", ") || "nothing");
    // 0037: hide the tabs the server would refuse (a convenience only: every call is still checked by the server).
    // A dispatcher-only account has no conversation list, so it starts on Dispatch.
    const need = {dispatch: "view_dispatch", schedule: "view_schedule", ops: "view_ops", threads: "list_requests",
                  requests: "list_requests", escalations: "list_requests", docks: "list_requests",
                  optimizer: "view_recommendations", observability: "view_observability"};
    document.querySelectorAll("#tabs button").forEach(b => {
      const p = need[b.dataset.t]; b.hidden = !!p && me.permissions[p] === false });
    if(need[tab] && me.permissions[need[tab]] === false){
      const first = [...document.querySelectorAll("#tabs button")].find(b => !b.hidden);
      if(first) setTab(first.dataset.t);
    }
  }).catch(() => {});
  render();
}
function setTab(t){ tab = t; selected = null; nextBefore = null; threads = []; pagedThreads = false; threadSig = "";
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t)); render() }

async function render(){
  const titles = {threads:"Conversations", requests:"Requests waiting for a decision", escalations:"Escalations", docks:"Docks",
                  optimizer:"Slot optimizer (Phase G)", observability:"Observability (Phase G)", dispatch:"Dispatch",
                  schedule:"Planner schedule", ops:"Operations health"};
  $("#lefttitle").textContent = titles[tab]; $("#more").hidden = true;
  const list = $("#list"), detail = $("#detail");
  try {
    if(tab === "threads"){ await loadThreads(false); if(!selected) detail.innerHTML = `<div class="empty">Select a conversation.</div>` }
    else if(tab === "requests"){
      const r = (await api("GET", "/console/requests")).requests;
      list.innerHTML = r.length ? r.map(x => `<div class="item"><div class="top"><b class="mono">${esc(x.shipment_id)}</b><span>${esc(x.kind)}</span></div>
        <div class="muted">${esc(x.facility_id)} · dock ${esc(x.dock_code)} · ${when(x.start)} · answer by ${when(x.pending_expires_at)}</div>
        <div class="row" style="margin-top:4px"><button class="ok" data-d="CONFIRM" data-a="${esc(x.appointment_id)}">Confirm</button>
        <button class="bad" data-d="REJECT" data-a="${esc(x.appointment_id)}">Reject</button>
        <button class="ghost" data-w="${esc(x.appointment_id)}">Withdraw</button></div></div>`).join("")
        : `<div class="empty">No open requests. A request appears here when a driver confirms a slot (booking must be switched on).</div>`;
      detail.innerHTML = `<div class="empty">Decisions are checked by the server against your role and facility.</div>`;
    } else if(tab === "escalations"){
      const r = (await api("GET", "/console/escalations")).escalations;
      list.innerHTML = r.length ? r.map(x => `<div class="item" data-open="${esc(x.thread_id || "")}"><div class="top"><b>${esc(x.reason_code)}</b><span>${esc(x.status)}</span></div>
        <div class="muted">${esc(x.facility_id || "CENTRAL")} · ${esc(x.mode)} · <span class="mono">${esc(x.shipment_id || "")}</span></div></div>`).join("")
        : `<div class="empty">No escalations visible to you.</div>`;
      detail.innerHTML = `<div class="empty">Select an escalation to open its conversation.</div>`;
    } else if(tab === "docks"){
      list.innerHTML = `<p class="muted">Block a dock over a time window (breakdown or maintenance). Planner or ops_lead only; checked by the server.</p>`;
      const now = new Date(), pad = n => String(n).padStart(2, "0");
      const local = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
      detail.innerHTML = `<h2>Block a dock</h2><div class="row"><select id="bdock" style="width:auto"><option>DOCK-JAI-D1</option><option>DOCK-JAI-D2</option>
        <option>DOCK-JAI-D3</option><option>DOCK-GGN-D1</option></select>
        <select id="btype" style="width:auto"><option>BREAKDOWN</option><option>MAINTENANCE</option></select></div>
        <div class="row" style="margin-top:6px"><label>From <input type="datetime-local" id="bstart" value="${local(now)}"></label>
        <label>To <input type="datetime-local" id="bend" value="${local(new Date(now.getTime() + 2 * 3600e3))}"></label></div>
        <div class="row" style="margin-top:6px"><input id="breason" class="grow" value="hydraulic leak" maxlength="200"><button id="block">Block dock</button></div>`;
      $("#block").onclick = async () => { try {
        const r = await api("POST", `/console/docks/${encodeURIComponent($("#bdock").value)}/blocks`, {event_id:"DE-" + key().slice(0, 8),
          start:new Date($("#bstart").value).toISOString(), end:new Date($("#bend").value).toISOString(), type:$("#btype").value, reason:$("#breason").value});
        toast("Dock blocked: " + JSON.stringify(r)) } catch(e){ toast("Not done: " + e.message) } };
      detail.insertAdjacentHTML("beforeend", `<h2 style="margin-top:18px">Facility schedule proposal</h2>
        <p class="muted">Read only: the scheduling tool's proposed order for the whole facility. A proposal, never a booking.
        Planner or ops_lead only; checked by the server.</p>
        <div class="row"><select id="sfac" style="width:auto"><option>FAC-JAI-01</option><option>FAC-GGN-01</option></select>
        <button id="sched">Show proposal</button></div><div id="schedout"></div>`);
      $("#sched").onclick = async () => { const out = $("#schedout");
        try {
          const r = await api("GET", `/console/facilities/${encodeURIComponent($("#sfac").value)}/schedule-proposal`);
          out.innerHTML = `<p class="muted">${esc(r.status)} · ${esc(r.note || "")} · ${r.assignments.length} trucks</p>` +
            `<table><tr><th>#</th><th>Shipment</th><th>Action</th><th>Dock</th><th>Start</th><th>Why</th></tr>` +
            r.assignments.map(x => `<tr><td>${x.seq}</td><td class="mono">${esc(x.shipment_id)}</td><td>${esc(x.action)}</td>
              <td>${esc(x.dock_code)}</td><td>${when(x.start)}</td><td class="muted">${esc(x.reason)}</td></tr>`).join("") + `</table>` +
            (r.unscheduled.length ? `<p class="muted">To a person: ${r.unscheduled.map(u => esc(u.shipment_id) + " (" + esc(u.reason_code) + ")").join(", ")}</p>` : "");
        } catch(e){ out.innerHTML = `<div class="empty err">Refused by the server: ${esc(e.message)}</div>` } };
    }
    else if(tab === "dispatch") await renderDispatch(list, detail);
    else if(tab === "schedule") await renderSchedule(list, detail);
    else if(tab === "ops") await renderOps(list, detail);
    else if(tab === "optimizer") renderOptimizer(list, detail);
    else if(tab === "observability") await renderObservability(list, detail);
  } catch(e){ if(e.message !== "signed out" && e.message !== "401"){
    list.innerHTML = `<div class="empty err">${e.status === 403 ? "Not permitted for your role: " : "Service failure: the server did not answer this view ("}${esc(e.message)}${e.status === 403 ? "" : "). Numbers are not shown rather than guessed."}</div>`;
    if(tab === "observability") detail.innerHTML = e.status === 403
      ? `<div class="empty">This view is for planners and ops leads; the server refused it for your role.</div>`
      : `<div class="empty err">Metrics unavailable because the request failed, not because nothing happened.</div>` } }
}

// ---------------------------------------------------------------- Phase G: optimizer and observability (read only)
function kvTable(o){ const e = Object.entries(o || {}); return e.length ? `<table>${e.map(([k, v]) =>
  `<tr><td>${esc(k)}</td><td><b>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v ?? "unavailable")}</b></td></tr>`).join("")}</table>`
  : `<div class="muted">none recorded</div>` }
function renderOptimizer(list, detail){
  list.innerHTML = `<p class="muted">Ranked dock and slot options for one truck from the deterministic optimizer. Every hard
    constraint is evaluated; rejected alternatives are listed with the reason. A recommendation, never a booking.
    Planner or ops_lead only; facility scope is checked by the server.</p>
    <div class="row"><input id="osid" class="grow mono" placeholder="Shipment id, format SYN-SHP-XXXXXXXX" maxlength="80"
      title="Upper-case letters, digits and dashes, as shown in Conversations or Requests">
    <select id="odays" style="width:auto"><option value="1">ready day + 1 operating day</option><option value="2">+2</option><option value="3">+3</option></select>
    <button id="orun">Recommend</button></div>
    <p class="muted">Shipment ids look like <span class="mono">SYN-SHP-</span> followed by 8 letters or digits (synthetic pilot
    data). Copy one from a conversation or a request, then press <b>Recommend</b>.</p>`;
  detail.innerHTML = `<div class="empty">No recommendation yet. Enter a shipment id (format <span class="mono">SYN-SHP-XXXXXXXX</span>)
    and press <b>Recommend</b>. Read only: nothing is held, booked or changed.</div>`;
  $("#orun").onclick = async () => {
    const sid = $("#osid").value.trim();
    if(!sid){ detail.innerHTML = `<div class="empty">Enter a shipment id first (format <span class="mono">SYN-SHP-XXXXXXXX</span>).</div>`; return }
    if(!new RegExp(SHIPMENT_ID_PATTERN).test(sid)){
      detail.innerHTML = `<div class="empty err">"${esc(sid)}" is not a shipment id: use upper-case letters, digits and dashes,
        like <span class="mono">SYN-SHP-XXXXXXXX</span>. Nothing was sent to the server.</div>`; return }
    detail.innerHTML = `<div class="empty">Optimizing…</div>`;
    try {
      const r = await api("GET", `/console/shipments/${encodeURIComponent(sid)}/recommendations?days=${encodeURIComponent($("#odays").value)}`);
      const hc = Object.entries(r.constraints_evaluated).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
      detail.innerHTML = `<h2>${esc(r.shipment_id)} · ${esc(r.facility_id || "")}</h2>
        <p class="muted">${esc(r.note)}</p>
        <dl class="kv"><dt>Solver status</dt><dd><b>${esc(r.solver_status)}</b> · ${esc(r.outcome_code)} · action ${esc(r.recommended_action)}</dd>
        <dt>Input ETA / ready</dt><dd>${when(r.input_eta)} / ${when(r.ready_at)}</dd>
        <dt>Solver</dt><dd>${esc(r.solver)} ${esc(r.solver_version)} · ${esc(r.engine_version)} · ${esc(r.duration_ms)} ms · ${esc(r.candidates_evaluated)} candidates</dd>
        <dt>Data</dt><dd>as of ${when(r.data_as_of)} · schedule version ${esc(r.schedule_version)} · <span class="mono">${esc((r.input_fingerprint || "").slice(0, 16))}</span></dd>
        <dt>Correlation id</dt><dd class="mono">${esc(r.correlation_id)}</dd></dl>
        ${r.warnings.length ? `<p class="err">${r.warnings.map(esc).join("<br>")}</p>` : ""}
        <h3>Ranked options</h3>
        ${r.options.length ? `<div class="tablewrap"><table><tr><th>#</th><th>Dock</th><th>Start</th><th>End</th><th>Wait</th><th>Score</th><th>Approval</th><th>Why</th></tr>${r.options.map(o =>
          `<tr><td>${o.rank}</td><td>${esc(o.dock_code)}</td><td>${when(o.start)}</td><td>${when(o.end)}</td><td>${o.wait_min} min</td>
           <td title="${esc(JSON.stringify(o.score_terms))}">${o.score}</td><td>${o.needs_approval ? "yes" : "no"}</td><td class="muted">${esc(o.reasons.join("; "))}</td></tr>`).join("")}</table></div>`
          : `<div class="empty">No feasible option: the truck goes to a person.</div>`}
        <h3>Rejected alternatives (${r.rejected.length})</h3>
        <div class="tablewrap"><table><tr><th>Dock</th><th>Stage</th><th>Reason</th></tr>${r.rejected.slice(0, 40).map(x =>
          `<tr><td>${esc(x.dock_code)}</td><td>${esc(x.stage)}</td><td class="muted">${esc(x.reason)}</td></tr>`).join("")}</table></div>
        <h3>Hard constraints evaluated</h3><div class="tablewrap"><table>${hc}</table></div>`;
    } catch(e){ detail.innerHTML = `<div class="empty err">${e.status === 404 ? "Not found, or not in your facilities." : "Refused by the server: " + esc(e.message)}</div>` }
  };
}
// Driver traceability (0041): filter the recent turns by one internal driver id (the server checks its format).
let OBS_DRIVER = "";
async function renderObservability(list, detail){
  const q = new URLSearchParams({hours: "24"}); if (OBS_DRIVER) q.set("driver", OBS_DRIVER);
  let r;
  try { r = await api("GET", "/console/observability?" + q); }
  catch(e){
    if (e.status === 422 && OBS_DRIVER) { const bad = OBS_DRIVER; OBS_DRIVER = ""; await renderObservability(list, detail);
      const m = document.getElementById("obsdrivermsg"); if (m) m.textContent = `"${bad}" is not a driver id (example: DRV-JAI-0042). Showing all drivers.`; return; }
    throw e;
  }
  const o = r.overview, a = r.agent, z = r.optimization, rel = r.reliability;
  list.innerHTML = `<p class="pill">${esc(r.data_label)}</p>
    <p class="muted">Window: ${when(r.since)} – ${when(r.generated_at)} · facilities ${esc(r.facilities.join(", "))}.
    Every number is a stored row or a measured timing; anything else says unavailable.</p>
    <h3>Operational overview</h3>${kvTable({"open approval requests": o.open_approval_requests, "docks blocked now": o.docks_blocked_now,
      "active conversations": o.conversations_active})}
    <h3>Shipments by status</h3>${kvTable(o.shipments_by_status)}
    <h3>Current appointments</h3>${kvTable(o.appointments_by_status)}
    <h3>Open escalations</h3>${kvTable(o.open_escalations_by_reason)}`;
  const mu = a.model_usage || {}, mo = a.model_outcomes || {}, mt = mo.turns || {}, ma = mo.attempts || {};
  const noTurns = a.status_code === "NO_RECENT_TURNS";
  const recon = ok => ok === undefined ? "" : `<span class="recon ${ok ? "ok" : "bad"}">${ok ? "parts add up to the total" : "parts do NOT add up: check the ledger"}</span>`;
  detail.innerHTML = `<h2>Agent performance</h2>
    ${noTurns ? `<div class="empty">No agent turns in this window: no driver message was processed. The service answered,
      so this is not a failure. Send a synthetic driver message to see turns here.</div>` : ""}
    ${kvTable({"turns": a.turns, "model extraction": a.model_turns, "rules reader": a.rules_turns, "fallbacks": a.fallbacks,
      "escalated turns": a.escalated, "turn p50 ms": a.turn_p50_ms, "turn p95 ms": a.turn_p95_ms})}
    <h3>Tool calls</h3>${a.tools.length ? `<div class="tablewrap"><table><tr><th>Tool</th><th>Calls</th><th>Not OK</th><th>p50 ms</th><th>p95 ms</th></tr>${a.tools.map(t =>
      `<tr><td>${esc(t.tool)}</td><td>${t.calls}</td><td>${t.not_ok}</td><td>${esc(t.p50_ms)}</td><td>${esc(t.p95_ms)}</td></tr>`).join("")}</table></div>`
      : `<div class="muted">${noTurns ? "No tool calls: no agent turns ran in this window (not a service failure)."
                                      : "Agent turns ran but recorded no tool calls (a person owned those conversations)."}</div>`}
    <h3>Model use: what happened</h3>
    <p class="${mo.status_code === "OK" || mo.status_code === "NO_ACTIVITY" ? "muted" : "err"}"><b>${esc(mo.status_code || "")}</b> ${esc(mo.status || "")}</p>
    <h3>Turns by reader (your facilities)</h3>${kvTable({"total turns": mt.total, "model extraction succeeded": mt.model_extraction,
      "rules reader after a provider failure": (mt.fallback_by_class || {}).PROVIDER_FAILURE,
      "rules reader after a budget refusal": (mt.fallback_by_class || {}).BUDGET_REFUSAL,
      "rules reader, model never started (secret/config)": (mt.fallback_by_class || {}).NOT_STARTED,
      "rules reader, no model configured": mt.rules_reader_no_model})} ${recon(mt.reconciles)}
    <h3>Failure categories (turns)</h3>${kvTable(mt.fallback_by_category)}
    <h3>Model attempts (ledger, all facilities)</h3><p class="muted">${esc(a.model_usage_scope)}</p>
    ${kvTable({"attempts": ma.total, "succeeded": ma.ok, "provider errors": ma.errors, "refused by the daily budget (not sent)": ma.budget_refusals,
      "reserved, not yet settled": ma.unsettled})} ${recon(ma.reconciles)}
    <h3>Attempt failure categories</h3>${kvTable(ma.by_category)}
    <h3>Tokens and cost</h3>${kvTable({"input tokens (provider-reported)": (mo.tokens || {}).input, "output tokens (provider-reported)": (mo.tokens || {}).output,
      "attempts with reported usage": (mo.tokens || {}).calls_with_reported_usage, ...((mo.cost || {}).note
        ? {"cost USD": (mo.cost || {}).total_usd, "note": mo.cost.note}
        : {"cost USD, provider-reported": (mo.cost || {}).provider_reported_usd, "cost USD, ESTIMATED (reservation kept)": (mo.cost || {}).estimated_usd,
           "open reservations USD": (mo.cost || {}).open_reservations_usd}),
      "spent today USD": mu.today_spent_usd, "budget day": (mo.budget || {}).day_boundary,
      "latency p50 ms": mu.latency_p50_ms, "latency p95 ms": mu.latency_p95_ms, "by model": mu.by_model})}
    <p class="muted">${esc((mo.tokens || {}).note || "")}</p>
    ${renderLangSmith(r.langsmith || {}, a.recent_turns || [], r.driver_filter)}
    <h2 style="margin-top:18px">Optimization performance</h2>${kvTable({"runs": z.runs, "with options": z.with_options, "fallbacks to engine order": z.fallbacks,
      "p50 ms": z.p50_ms, "p95 ms": z.p95_ms, "max ms": z.max_ms, "avg candidates": z.avg_candidates, "avg rejected": z.avg_rejected})}
    <h3>By solver status</h3>${kvTable(z.by_status)}<h3>By outcome</h3>${kvTable(z.by_outcome)}<h3>By caller</h3>${kvTable(z.by_caller)}
    <h2 style="margin-top:18px">Reliability</h2>
    <h3>Approval requests</h3>${kvTable(rel.approval_requests_by_status)}
    <h3>Booking events</h3>${kvTable(rel.booking_events_by_action)}
    <h3>Driver notifications</h3>${kvTable({...rel.notifications_by_status, "retried": rel.notification_retries})}
    <h3>Inbound messages</h3>${kvTable(rel.inbound)}
    <h3>Idempotency</h3>${kvTable(rel.idempotency)}
    <h3>Approval workflow callbacks</h3>${kvTable(rel.approval_callbacks)}
    <h3>Outbox</h3>${kvTable(rel.outbox_by_status)}
    <h3>Queue / DLQ</h3><p class="muted">${esc(rel.queue_dlq.status)}: ${esc(rel.queue_dlq.reason)}</p>
    <h2 style="margin-top:18px">Evaluation results</h2>
    <div class="tablewrap"><table><tr><th>Suite</th><th>Mode</th><th>Dataset</th><th>Passed</th><th>Failed</th><th>Skipped</th><th>Cost USD</th><th>Finished</th></tr>${r.evaluation.map(e => e.status
      ? `<tr><td colspan="8" class="muted">${esc(e.status)}</td></tr>`
      : `<tr><td>${esc(e.suite)}</td><td>${esc(e.mode)}</td><td>${esc(e.dataset_version)}</td><td>${e.passed}/${e.total}</td><td>${e.failed}</td><td>${e.skipped}</td><td>${esc(e.cost_usd)}</td><td>${when(e.finished_at)}</td></tr>`).join("")}</table></div>`;
  const f = document.getElementById("obsdriverform");
  if (f) f.onsubmit = ev => { ev.preventDefault(); OBS_DRIVER = (document.getElementById("obsdriver").value || "").trim(); renderObservability(list, detail); };
  const clr = document.getElementById("obsdriverclear");
  if (clr) clr.onclick = () => { OBS_DRIVER = ""; renderObservability(list, detail); };
}

// G.1 (G6): LangSmith links for planner/ops_lead (the server only returns this tab to them). Links only: the browser
// holds no LangSmith credential and makes no call to LangSmith.
function renderLangSmith(ls, turns, driverFilter){
  const ev = ls.latest_eval;
  return `<h2 style="margin-top:18px">LangSmith (traces and evaluation)</h2>
    ${kvTable({"tracing": ls.tracing, "project": ls.project_url ? "configured (link below)" : (ls.project_status || "unavailable"),
      "latest evaluation": ev ? `${ev.suite}: ${ev.passed}/${ev.total} passed, ${ev.failed} failed` : "unavailable: no evaluation run recorded",
      "dataset version": ls.dataset_version || "unavailable", "evaluator version": ls.evaluator_version || "unavailable",
      "experiment": ls.experiment_url ? "recorded (link below)" : "unavailable: no LangSmith experiment recorded"})}
    ${ls.project_url ? `<p>Project: ${extLink(ls.project_url, ls.project_url)}</p>` : ""}
    ${ls.experiment_url ? `<p>Latest experiment: ${extLink(ls.experiment_url, ls.experiment_url)}</p>` : ""}
    <h3>Recent turns</h3>
    <form id="obsdriverform" class="row" style="gap:6px;align-items:center;margin:4px 0 8px">
      <label for="obsdriver" class="muted">Driver</label>
      <input id="obsdriver" maxlength="48" placeholder="DRV-JAI-0042" value="${esc(driverFilter || "")}" style="width:180px">
      <button type="submit">Filter</button>${driverFilter ? ` <button type="button" id="obsdriverclear">Show all drivers</button>` : ""}
      <span id="obsdrivermsg" class="muted"></span>
    </form>
    <p class="muted">The same driver id finds a turn in LangSmith (metadata <span class="mono">driver_id</span>) and in CloudWatch
      (<span class="mono">agent.turn</span> lines). Turns before a shipment was known show no driver here.</p>
    ${turns.length ? `<div class="tablewrap"><table><tr><th>When</th><th>Facility</th><th>Driver</th><th>Shipment</th><th>Reader</th><th>Failure category</th><th>ms</th><th>Trace</th></tr>${turns.map(t =>
      `<tr><td>${when(t.created_at)}</td><td>${esc(t.facility_id || "CENTRAL")}</td><td class="mono">${esc(t.driver_id || "")}</td><td class="mono">${esc(t.shipment_id || "")}</td><td>${esc(t.extractor)}</td><td>${esc(t.fallback_reason || "")}</td>
       <td>${esc(t.turn_ms)}</td><td>${t.trace_url ? extLink(t.trace_url, "open trace") : `<span class="muted mono" title="Search CloudWatch for this turn_ref">${esc(String(t.turn_ref).slice(0, 8))}… (no trace link: tracing ${esc(ls.tracing)}${ls.project_url ? "" : ", project URL not configured"})</span>`}</td></tr>`).join("")}</table></div>`
      : `<div class="muted">${driverFilter ? "No turns for this driver in this window." : "No turns in this window."}</div>`}`;
}

async function loadThreads(more){
  const q = new URLSearchParams({limit:"25"}); if(more && nextBefore) q.set("before", nextBefore);
  const r = await api("GET", "/console/threads?" + q);
  threads = more ? threads.concat(r.threads) : r.threads; nextBefore = r.next_before;
  $("#list").innerHTML = threads.length ? threads.map(t => `<div class="item ${t.thread_id === selected ? "sel" : ""}" data-open="${esc(t.thread_id)}">
    <div class="top"><b class="mono">${esc(t.driver_id || t.driver_phone)}</b><span class="muted">${when(t.last_message_at)}</span></div>
    <div class="muted">${esc(t.facility_id)} · ${esc(t.flow_state || t.status)}${t.human_active ? " · <b>a person has it</b>" : ""}${t.intent ? " · " + esc(t.intent) : ""}</div>
    <div>${t.last_sender === "AGENT" ? "↩ " : ""}${esc(t.last_message_preview)}</div>
    ${t.notification_status ? `<div class="muted">reply ${esc(t.notification_status)}${t.notification_delivery ? " · " + esc(DELIVERY_LABEL[t.notification_delivery] || t.notification_delivery) : ""}</div>` : ""}</div>`).join("")
    : `<div class="empty">No conversations in your facilities yet. Send a synthetic driver message (docs/release/11_STAFF_CONSOLE.md).</div>`;
  $("#more").hidden = !nextBefore;
}

let threadSig = "";
async function openThread(id, quiet){
  // quiet = the live refresh: no "Loading…" flash, the scroll position kept, and nothing redrawn when nothing changed
  selected = id; if(tab !== "threads"){ tab = "threads"; document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === "threads")); await loadThreads(false) }
  document.querySelectorAll("#list .item").forEach(e => e.classList.toggle("sel", e.dataset.open === id));
  const d = $("#detail"); if(!quiet) d.innerHTML = `<div class="empty">Loading…</div>`;
  let b; try { b = await api("GET", "/console/threads/" + encodeURIComponent(id)) }
  catch(e){ if(quiet) return; d.innerHTML = `<div class="empty err">${e.status === 404 ? "Not found, or not in your facilities." : "Could not load: " + esc(e.message)}</div>`; return }
  if(selected !== id) return;                                  // another conversation was opened meanwhile
  const sig = JSON.stringify([id, b.messages.map(m => [m.message_id, m.at]), b.notifications.map(n => [n.notification_id, n.status]),
                              b.thread.human_active, b.flow.state, b.flow.appointment_id]);
  if(quiet && sig === threadSig) return;
  threadSig = sig;
  const keep = quiet ? d.scrollTop : 0;
  try { renderThread(d, b) } finally { if(quiet) d.scrollTop = keep }
}

function renderThread(d, b){
  const t = b.thread, f = b.flow, x = b.extracted;
  const facts = x.facts ? Object.entries(x.facts).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("") : "";
  d.innerHTML = `
    <div class="row"><h2 class="grow">Conversation <span class="mono">${esc(t.thread_id)}</span></h2>
      ${t.human_active ? `<button class="ghost" data-hb="${esc(t.thread_id)}">Hand back to the agent</button>` : `<button class="ghost" data-to="${esc(t.thread_id)}">Take over</button>`}</div>
    <dl class="kv"><dt>Driver</dt><dd class="mono">${esc(t.driver_phone)} (${esc(t.driver_id)})</dd><dt>Shipment</dt><dd class="mono">${esc(t.shipment_id)}</dd>
      <dt>Facility</dt><dd>${esc(t.facility_id)}</dd><dt>Conversation</dt><dd>${esc(t.status)}${t.human_active ? " · a person has it since " + when(t.human_active_since) : " · the agent has it"}</dd>
      <dt>Flow state</dt><dd>${esc(f.state)}${f.appointment_id ? " · appointment " + esc(f.appointment_id) : " · no appointment"}</dd></dl>
    <h3>Messages (masked)</h3>
    <div class="chat">${b.messages.map(m => m.read_back_record
        ? `<div class="b sys" title="Stored so the driver's YES can be checked against exactly this wording; the driver receives the reply below, once">${esc(m.body_masked)}<small>READ-BACK RECORD (not sent) · ${when(m.at)}</small></div>`
        : `<div class="b ${m.sender === "DRIVER" ? "in" : m.sender === "AGENT" ? "out" : "sys"}">${esc(m.body_masked)}<small>${esc(m.sender === "SYSTEM" ? (m.channel === "TELEGRAM" ? "BOT MESSAGE (Telegram)" : "NOTICE") : m.sender)} · ${when(m.at)}</small></div>`).join("") || `<div class="empty">No messages.</div>`}</div>
    <h3>What the reader recorded</h3>
    ${facts ? `<dl class="kv">${facts}</dl>` : `<div class="muted">No open facts carried (the last report was acted on, or nothing was extracted yet).</div>`}
    ${x.asked ? `<div class="muted">Waiting for the driver to answer: ${esc(x.asked)}</div>` : ""}
    ${x.eta_signals.length ? `<div class="tablewrap"><table><tr><th>ETA source</th><th>Arrival</th><th>Confidence</th><th>Recorded</th></tr>${x.eta_signals.map(e =>
      `<tr><td>${esc(e.source)}</td><td>${when(e.derived_eta)}</td><td>${esc(e.confidence)}${e.uncertainty_min ? " ±" + esc(e.uncertainty_min) + " min" : ""}</td><td>${when(e.observed_at)}</td></tr>`).join("")}</table></div>` : `<div class="muted">No ETA recorded yet.</div>`}
    <h3>Options shown to the driver</h3>
    ${b.options.length ? `<div class="tablewrap"><table><tr><th>#</th><th>Dock</th><th>Slot</th><th>Needs approval</th><th>Expires</th><th>Picked</th></tr>${b.options.map(o =>
      `<tr><td>${esc(o.rank)}</td><td>${esc(o.dock_code)}</td><td>${when(o.start)} – ${when(o.end)}</td><td>${o.needs_approval ? "yes" : "no"}</td><td>${when(o.expires_at)}</td><td>${b.selections.some(s => s.option_id === o.option_id) ? "<b>picked</b>" : ""}</td></tr>`).join("")}</table></div>` : `<div class="muted">No options shown yet.</div>`}
    <h3>Notifications to the driver</h3>
    ${b.notifications.length ? `<div class="tablewrap"><table><tr><th>Template</th><th>Status</th><th>Tries</th><th>Delivered by</th><th>Error</th><th>Updated</th></tr>${b.notifications.map(n =>
      `<tr><td>${esc(n.template_id)}</td><td><b>${esc(n.status)}</b></td><td>${esc(n.attempts)}/${esc(n.max_attempts)}</td><td>${n.delivery === "test_sink" ? `internal test sink <span class="mono">${esc(n.test_sink_reference)}</span>` : esc(n.delivery || "not yet")}</td><td>${esc(n.last_error)}</td><td>${when(n.updated_at)}</td></tr>`).join("")}</table></div>` : `<div class="muted">No notifications for this conversation.</div>`}
    <p class="muted">Delivered-by shows the internal test sink only when the stored record carries a test-sink reference; a queued record is never shown as delivered.</p>`;
}

// ---------------------------------------------------------------- live refresh (Demo release)
// Conversations update on their own: every few seconds while the tab is visible and the Conversations view is open,
// the first page of the list and the open conversation are read again (read-only GETs, the same RLS-scoped API).
// Skipped while a request is in flight, while the user has paged further down the list, and while typing.
const LIVE_REFRESH_MS = 3000;
let liveBusy = false, pagedThreads = false;
async function liveRefresh(){
  if(liveBusy || !TOKEN || tab !== "threads" || document.visibilityState !== "visible") return;
  const a = document.activeElement; if(a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
  liveBusy = true;
  try {
    if(!pagedThreads) await loadThreads(false);
    if(selected) await openThread(selected, true);
  } catch(e){ /* the next tick tries again; the manual Refresh button still works */ }
  finally { liveBusy = false }
}

// ---------------------------------------------------------------- events
document.addEventListener("click", async ev => {
  const b = ev.target.closest("button,[data-open]"); if(!b) return;
  try {
    if(b.dataset.t) setTab(b.dataset.t);
    else if(b.dataset.open !== undefined){ if(b.dataset.open) openThread(b.dataset.open); else toast("No conversation for this escalation.") }
    else if(b.id === "refresh"){ loadStatus(); selected && tab === "threads" ? (await loadThreads(false), openThread(selected)) : render() }
    else if(b.id === "more"){ pagedThreads = true; loadThreads(true) }
    else if(b.id === "signinbtn") signIn();
    else if(b.id === "signout") signOut();
    else if(b.dataset.d){ const r = await api("POST", `/console/requests/${encodeURIComponent(b.dataset.a)}/decision`, {decision:b.dataset.d, idempotency_key:key(), note:"hosted console"}); toast(`${b.dataset.d}: ${JSON.stringify(r)}`); render() }
    else if(b.dataset.w){ const note = prompt("Withdraw note (why):", "driver called the dock office"); if(!note) return;
      const r = await api("POST", `/console/requests/${encodeURIComponent(b.dataset.w)}/withdraw`, {reason_code:"DRIVER_REQUESTED", note, idempotency_key:key()}); toast("Withdrawn: " + JSON.stringify(r)); render() }
    else if(b.dataset.to){ const reason = prompt("Why are you taking over?", "driver upset, calling them"); if(!reason) return;
      await api("POST", `/console/threads/${encodeURIComponent(b.dataset.to)}/takeover`, {reason}); toast("You have the conversation; the agent is silent."); openThread(b.dataset.to) }
    else if(b.dataset.hb){ const summary = prompt("Hand-back summary (at least 10 characters):", "Called the driver, slot stays as booked"); if(!summary) return;
      await api("POST", `/console/threads/${encodeURIComponent(b.dataset.hb)}/handback`, {summary}); toast("Handed back to the agent."); openThread(b.dataset.hb) }
  } catch(e){ if(e.message !== "signed out" && e.message !== "401") toast("Not done: " + e.message) }
});

(async () => {
  try { CFG = await (await fetch("config.json", {cache:"no-store"})).json() }
  catch { showSignIn("This console is not configured (config.json missing)."); loadStatus(); return }
  loadStatus(); setInterval(loadStatus, 60000);
  setInterval(liveRefresh, LIVE_REFRESH_MS);
  setInterval(() => { if(TOKEN && Date.now() > EXPIRES - 15000){ TOKEN = null; showSignIn("Your session has ended. Please sign in again.") } }, 10000);
  try { if(await finishSignIn()) return showApp() } catch(e){ return showSignIn(e.message) }
  showSignIn();
})();

// ---------------------------------------------------------------- roadmap: Dispatch, Schedule, Ops health (0035/0036)
// The approved design (Dispatcher / Planner schedule / Operations health). Every state shown is the server's; every
// action is checked by the API and the database, so a button here is a convenience, never the authorization.
// Business state, delivery state, planner decision and escalation state are shown apart and never merged.
const IST = {timeZone:"Asia/Kolkata"};
function exact(s){ if(!s) return "—"; const d = new Date(s); return isNaN(d) ? esc(s) : d.toLocaleString("en-IN",
  {...IST, day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit", second:"2-digit"}) + " IST" }
function chip(text, tone){ const c = {ok:"var(--ok)", bad:"var(--bad)", warn:"var(--warn)", info:"var(--accent)"}[tone] || "var(--muted)";
  return `<span class="pill" style="color:${c};font-weight:600">${esc(text)}</span>` }
function stamp(asOf){ return `<p class="muted" style="font-size:12px">Last refreshed ${exact(asOf || new Date().toISOString())}</p>` }
function errText(e){ try { const j = JSON.parse(e.message); return (j.error || "") + (j.detail ? ": " + j.detail : "") } catch { return e.message } }
const OFFER_TONE = {OFFERED:"info", ACCEPTED:"ok", DECLINED:"bad", ALTERNATIVE_REQUESTED:"warn", EXPIRED:"", SUPERSEDED:"",
                    UNAVAILABLE:"warn"};
const OFFER_WORDS = {OFFERED:"Offered · waiting for the driver", ACCEPTED:"Accepted by the driver", DECLINED:"Declined",
  ALTERNATIVE_REQUESTED:"Driver asked for another time · send a new offer", EXPIRED:"Expired · no answer in time",
  SUPERSEDED:"Superseded by a newer offer", UNAVAILABLE:"Unavailable"};
const KIND_WORDS = {INITIAL_BOOKING:"New booking", REASSIGNMENT:"Reassignment (before booking)",
  CONFIRMED_REASSIGNMENT:"Reassignment of a confirmed appointment"};
let DX = {rows:[], filter:"NEW_BOOKING", sel:null, opts:null, pending:{}};     // pending: in-flight idempotency keys

function busyKey(name){ return DX.pending[name] || (DX.pending[name] = key()) }   // one key per intent: a double
function doneKey(name){ delete DX.pending[name] }                                  // click or a retry reuses it

async function renderDispatch(list, detail){
  const r = await api("GET", "/console/dispatch/shipments");
  DX.rows = r.shipments; DX.asOf = new Date().toISOString();
  const n = w => DX.rows.filter(x => w === "ALL" || x.workflow === w).length;
  const f = DX.filter;
  const rows = DX.rows.filter(x => f === "ALL" || x.workflow === f);
  list.innerHTML = `<div class="row" role="group" aria-label="Booking state filter">
      ${[["NEW_BOOKING","Needs booking"],["PENDING_PLANNER","Awaiting planner"],["CONFIRMED","Confirmed"],["ALL","All"]].map(([k, t]) =>
        `<button class="${f === k ? "" : "ghost"}" data-act="dfilter" data-arg="${k}">${t} · ${n(k)}</button>`).join("")}</div>
    ${stamp(DX.asOf)}
    ${rows.length ? rows.map(x => { const o = x.latest_offer;
      return `<div class="item ${DX.sel === x.shipment_id ? "sel" : ""}" data-act="dopen" data-arg="${esc(x.shipment_id)}" role="button" tabindex="0">
        <div class="top"><b>${esc(x.order_reference)}</b>${x.workflow === "NEW_BOOKING" ? chip("Needs booking", "warn")
          : x.workflow === "CONFIRMED" ? chip("Confirmed", "ok") : chip("Awaiting planner", "info")}</div>
        <div class="muted">ETA ${when(x.planned_eta)} · ${esc(x.facility_id)} · ${esc(x.vehicle_type)} ${esc(x.vehicle_id)}</div>
        <div class="muted">Driver ${esc(x.driver_name || x.driver_id)} · ${x.telegram.linked ? chip("Telegram linked", "ok") : chip("Not linked", "bad")}</div>
        ${o ? `<div class="muted">${esc(KIND_WORDS[o.kind] || o.kind)}: ${chip(o.status, OFFER_TONE[o.status])}${o.status === "OFFERED" ? " until " + exact(o.expires_at) : ""}</div>` : ""}
      </div>` }).join("") : `<div class="empty">No shipments in this state in your facilities.</div>`}`;
  if(DX.sel && DX.rows.some(x => x.shipment_id === DX.sel)) await openDispatch(DX.sel);
  else detail.innerHTML = `<div class="empty">Select a shipment. New bookings and reassignments are separate workflows; a
    planner approves every booking, and the dispatcher cannot.</div>`;
}

async function openDispatch(sid){
  DX.sel = sid; DX.opts = null;
  const x = DX.rows.find(r => r.shipment_id === sid); const d = $("#detail");
  if(!x){ d.innerHTML = `<div class="empty">Not in your facilities.</div>`; return }
  const offers = (await api("GET", "/console/dispatch/offers?limit=200")).offers.filter(o => o.shipment_id === sid);
  const t = x.telegram, a = x.appointment;
  d.innerHTML = `<h2>${esc(x.order_reference)} · ${x.workflow === "NEW_BOOKING" ? "New booking" : x.workflow === "CONFIRMED"
        ? "Confirmed appointment" : "Awaiting planner decision"}</h2>
    <dl class="kv"><dt>Shipment</dt><dd class="mono">${esc(x.shipment_id)}</dd><dt>Facility</dt><dd>${esc(x.facility_id)}</dd>
      <dt>Driver</dt><dd>${esc(x.driver_name || "")} <span class="mono">${esc(x.driver_id)}</span></dd>
      <dt>Vehicle</dt><dd>${esc(x.vehicle_type)} <span class="mono">${esc(x.vehicle_id)}</span> (vehicle changes are not in this release)</dd>
      <dt>Planned ETA</dt><dd>${exact(x.planned_eta)}</dd><dt>Origin</dt><dd class="muted">not held in this system</dd>
      <dt>Appointment</dt><dd>${a ? `${chip(a.status, a.status === "CONFIRMED" ? "ok" : "info")} dock ${esc(a.dock_code)} at ${exact(a.start)}` : "none"}</dd>
      ${x.cancellation ? `<dt>Cancellation</dt><dd>${chip(x.cancellation, x.cancellation === "OPEN" ? "warn" : "")}</dd>` : ""}</dl>
    <h3>Telegram</h3>
    <p>${t.linked ? `${chip("Linked", "ok")} since ${exact(t.linked_at)} <button class="ghost" data-act="drevoke" data-arg="${esc(x.driver_id)}">Revoke link</button>`
      : `${chip("Not linked", "bad")} ${t.code_expires_at ? `a code is open until ${exact(t.code_expires_at)} · ` : ""}
         <button class="ghost" data-act="dcode" data-arg="${esc(x.driver_id)}">Issue link code</button>
         <span class="muted">Until linked, offers reach the driver on the existing channel; if that fails, the failure is escalated to Operations.</span>`}</p>
    <div id="dcodebox"></div>
    ${x.workflow === "NEW_BOOKING" ? `
      <h3>New booking</h3>
      <p class="muted">Feasible slots come from the same feasibility engine as the driver conversation and are re-checked when you
        send the offer and again when the driver accepts. Sending an offer holds no dock time.</p>
      <button class="ghost" data-act="dopts">Load feasible slots</button><div id="dopts"></div>
      <h3>Reassign before booking</h3>
      <p class="muted">Sends the selected slot to another driver. ${esc(x.driver_id)} stays assigned until the new driver accepts; a
        decline or expiry changes nothing.</p>
      <div class="row"><label>New driver id <input id="dnewdrv" maxlength="80" style="width:200px"></label>
        <button class="ghost" data-act="doffer" data-arg="reassign">Send reassignment offer</button></div>`
    : x.workflow === "CONFIRMED" ? `
      <h3>Reassign this confirmed appointment</h3>
      <p class="muted">The new driver is asked to accept the trip AND the existing time (dock ${esc(a.dock_code)}, ${exact(a.start)}).
        Until they accept, ${esc(x.driver_id)} stays assigned and the appointment stays confirmed. Their other trips are checked.</p>
      <div class="row"><label>New driver id <input id="dcrdrv" maxlength="80" style="width:200px"></label>
        <button data-act="dreassign">Send reassignment request</button></div>
      <h3>Request cancellation</h3>
      <p class="muted">After agreeing it with the driver. A planner or ops lead decides; dock time is released only when they approve.</p>
      <div class="row"><select id="dcreason" style="width:auto"><option>DRIVER_UNAVAILABLE</option><option>VEHICLE_BREAKDOWN</option>
        <option>SHIPMENT_CANCELLED</option><option>CUSTOMER_REQUEST</option><option>OPERATIONS_DECISION</option></select>
        <input id="dcnote" class="grow" maxlength="500" placeholder="What was agreed, with whom"><button class="bad" data-act="dcancel" data-arg="${esc(a.appointment_id)}">Request cancellation</button></div>`
    : `<p class="muted">A planner is deciding this request${offers[0] && offers[0].planner_decision_due ? " (due " + exact(offers[0].planner_decision_due) + ")" : ""}.
        The dispatcher cannot approve it. Until then the dock time is claimed for it.</p>`}
    <h3>Offers for this shipment</h3>
    ${offers.length ? `<div class="tablewrap"><table><tr><th>Sent</th><th>Workflow</th><th>To</th><th>Slot</th><th>Offer state</th><th>Delivery</th><th>Booking</th></tr>
      ${offers.map(o => `<tr><td>${exact(o.created_at)}</td><td>${esc(KIND_WORDS[o.kind] || o.kind)}</td><td class="mono">${esc(o.driver_id)}</td>
        <td>${esc(o.dock_code)} · ${exact(o.start)}</td>
        <td>${chip(o.status, OFFER_TONE[o.status])}<div class="muted">${esc(OFFER_WORDS[o.status] || "")}${o.status_reason ? " — " + esc(o.status_reason) : ""}</div>
          ${o.status === "OFFERED" ? `<div class="muted">expires ${exact(o.expires_at)}</div>` : ""}</td>
        <td>${chip(o.delivery_state, o.delivery_state === "FAILED" ? "bad" : o.delivery_state === "UNKNOWN" ? "warn" : "")}</td>
        <td>${o.appointment_status ? chip(o.appointment_status, o.appointment_status === "CONFIRMED" ? "ok" : "info") : `<span class="muted">${esc(o.conversation_state || "—")}</span>`}</td></tr>`).join("")}</table></div>
      <p class="muted">Offer state is the business answer; delivery is only whether the message was handed to a channel (Telegram and the
        test sink give no read receipt, and UNKNOWN means a timeout, not a failure). Accepted is not booked: a planner still decides.</p>`
      : `<div class="empty">No offers yet.</div>`}`;
  document.querySelectorAll("#list .item").forEach(i => i.classList.toggle("sel", i.dataset.arg === sid));
}

async function dispatchAction(act, arg, btn){
  const x = DX.rows.find(r => r.shipment_id === DX.sel);
  if(act === "dfilter"){ DX.filter = arg; DX.sel = null; return render() }
  if(act === "dopen") return openDispatch(arg);
  if(act === "dopts"){ const out = $("#dopts"); out.innerHTML = `<div class="empty">Checking feasibility…</div>`;
    const r = await api("GET", `/console/dispatch/shipments/${encodeURIComponent(DX.sel)}/options`); DX.opts = r.options;
    out.innerHTML = r.options.length ? `<div role="radiogroup" aria-label="Feasible slots">${r.options.map((o, i) => `<label class="row" style="border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0">
        <input type="radio" name="dslot" value="${i}" ${i === 0 ? "checked" : ""} style="width:auto"> <b>Dock ${esc(o.dock_code)} · ${exact(o.start)} – ${exact(o.end)}</b>
        ${o.needs_approval ? `<span class="muted">after the last new start: needs a planner's after-hours decision</span>` : ""}</label>`).join("")}</div>
        <p class="muted">Checked at ${exact(new Date().toISOString())} (${esc(r.outcome)}).</p>
        <button data-act="doffer" data-arg="initial">Send offer to ${esc(x.driver_id)}</button>`
      : `<div class="empty">${esc(r.outcome)}: no feasible slot. The shipment can be escalated from the conversation.</div>`; return }
  if(act === "doffer"){
    const sel = document.querySelector('input[name="dslot"]:checked');
    if(!DX.opts || !sel) return toast("Load the feasible slots and pick one first.");
    const o = DX.opts[+sel.value], drv = arg === "reassign" ? ($("#dnewdrv").value || "").trim() : x.driver_id;
    if(!drv) return toast("Enter the new driver's id.");
    const name = `offer:${DX.sel}:${drv}:${o.dock_code}:${o.start}`;
    btn.disabled = true;
    try { const r = await api("POST", "/console/dispatch/offers", {shipment_id:DX.sel, driver_id:drv, vehicle_id:x.vehicle_id,
        dock_code:o.dock_code, start:o.start, idempotency_key:busyKey(name)});
      doneKey(name); toast(`${r.replayed ? "Already sent" : "Offer sent"} to ${drv}: ${r.status}, expires ${exact(r.expires_at)}. Nothing is held until they accept.`) }
    catch(e){ if(e.status === 409) doneKey(name); throw e } finally { btn.disabled = false }
    return render() }
  if(act === "dreassign"){ const drv = ($("#dcrdrv").value || "").trim(); if(!drv) return toast("Enter the new driver's id.");
    const name = `reassign:${DX.sel}:${drv}`; btn.disabled = true;
    try { const r = await api("POST", "/console/dispatch/reassignments", {shipment_id:DX.sel, driver_id:drv, idempotency_key:busyKey(name)});
      doneKey(name); toast(`Reassignment request sent to ${drv}, open until ${exact(r.expires_at)}. ${x.driver_id} stays assigned until they accept.`) }
    catch(e){ if(e.status === 409) doneKey(name); throw e } finally { btn.disabled = false }
    return render() }
  if(act === "dcancel"){ const note = ($("#dcnote").value || "").trim(); if(note.length < 3) return toast("Say what was agreed (at least 3 characters).");
    const name = `cancel:${arg}`; btn.disabled = true;
    try { await api("POST", `/console/appointments/${encodeURIComponent(arg)}/cancellation-requests`, {reason_code:$("#dcreason").value,
        note, idempotency_key:busyKey(name)}); doneKey(name);
      toast("Cancellation requested. The appointment stays confirmed and the dock time reserved until a planner approves.") }
    catch(e){ if(e.status === 409) doneKey(name); throw e } finally { btn.disabled = false }
    return render() }
  if(act === "dcode"){ btn.disabled = true;
    try { const r = await api("POST", `/console/drivers/${encodeURIComponent(arg)}/telegram-link-code`);
      // shown once, never stored by the page
      $("#dcodebox").innerHTML = `<div class="card" style="margin:6px 0"><b class="mono" style="font-size:18px">${esc(r.code)}</b>
        <p class="muted">Read it to the driver now; they send <span class="mono">/start ${esc(r.code)}</span> to the SetuHaul bot.
        Single use, valid until ${exact(r.expires_at)}. It will not be shown again.</p></div>` }
    finally { btn.disabled = false } return }
  if(act === "drevoke"){ const reason = prompt("Why unlink this Telegram account? (e.g. new phone)", ""); if(!reason) return;
    await api("POST", `/console/drivers/${encodeURIComponent(arg)}/telegram-link/revoke`, {reason});
    toast("Unlinked. Issue a new code to relink."); return render() }
}

// ---------------------------------------------------------------- Planner schedule
let SX = {fac:null, keys:{}};
async function renderSchedule(list, detail){
  // pilot E2E (26 Nov 2026): a planner with several facilities saw only the first one; every facility is a button now
  if(!SX.fac){ try { SX.facs = (await api("GET", "/console/me")).facilities || []; SX.fac = SX.facs[0] } catch { SX.fac = null } }
  if(!SX.fac){ list.innerHTML = `<div class="empty">No facility in your scope.</div>`; detail.innerHTML = ""; return }
  const s = await api("GET", `/console/facilities/${encodeURIComponent(SX.fac)}/schedule?hours=12`);
  const cap = s.capacity, hrs = m => (m / 60).toFixed(1);
  list.innerHTML = `<h3>Pending your decision · ${s.pending.length}</h3>${stamp(s.as_of)}
    ${s.pending.length ? s.pending.map(p => `<div class="card" style="margin-bottom:8px">
      <div class="top row"><b class="grow">${esc(p.order_reference)} · dock ${esc(p.dock_code)} · ${exact(p.start)}</b>
        ${chip(p.approval_kind, "info")}</div>
      <dl class="kv"><dt>Driver / vehicle</dt><dd class="mono">${esc(p.driver_id)} · ${esc(p.vehicle_type)} ${esc(p.vehicle_id)} · ${esc(p.load_kg)} kg</dd>
        <dt>Facility</dt><dd>${esc(s.facility_id)}</dd><dt>Source</dt><dd>${esc(p.source === "DISPATCHER_OFFER" ? "Dispatcher offer (" + (p.offered_by || "") + ")" : p.source)}</dd>
        <dt>Compatibility</dt><dd>${p.compatibility.map(c => esc(c)).join("; ")} (dock ${esc(p.dock_type)}, max ${esc(p.dock_max_kg)} kg)</dd>
        <dt>Capacity</dt><dd>${p.capacity_claimed ? chip("claimed", "ok") : chip("no live claim", "bad")} ${esc(p.capacity_note)}</dd>
        <dt>Decide by</dt><dd>${exact(p.decision_due)} ${p.expired ? chip("expired", "bad") : ""}</dd></dl>
      ${p.history.length ? `<details><summary class="muted">History (${p.history.length})</summary>${p.history.map(h =>
        `<div class="muted">${exact(h.at)} · ${esc(h.action)} · ${esc(h.by)}</div>`).join("")}</details>` : ""}
      <div class="row" style="margin-top:6px"><button class="ok" data-act="sdecide" data-arg="CONFIRM" data-appt="${esc(p.appointment_id)}">Approve</button>
        <button class="bad" data-act="sdecide" data-arg="REJECT" data-appt="${esc(p.appointment_id)}">Reject</button></div></div>`).join("")
      : `<div class="empty">Nothing waiting for a decision.</div>`}
    <h3>Cancellation requests · ${s.cancellations.length}</h3>
    ${s.cancellations.length ? s.cancellations.map(c => `<div class="card" style="margin-bottom:8px"><b>${esc(c.order_reference)} · dock ${esc(c.dock_code)} · ${exact(c.start)}</b>
      <div class="muted">${esc(c.reason_code)} · "${esc(c.note)}" · requested by ${esc(c.requested_by)} at ${exact(c.created_at)}</div>
      <p class="muted">The appointment stays confirmed and its dock time reserved until you approve.</p>
      <div class="row"><button class="bad" data-act="scancel" data-arg="APPROVE" data-req="${esc(c.request_id)}">Approve cancellation</button>
        <button class="ghost" data-act="scancel" data-arg="REJECT" data-req="${esc(c.request_id)}">Keep the appointment</button></div></div>`).join("")
      : `<div class="empty">No cancellation requests.</div>`}`;
  const t0 = new Date(s.window_start).getTime(), t1 = new Date(s.window_end).getTime(), pct = v => Math.max(0, Math.min(100, (new Date(v).getTime() - t0) / (t1 - t0) * 100));
  const style = {CONFIRMED:"background:var(--accent);color:#fff", IN_PROGRESS:"background:var(--accent);color:#fff",
    PENDING_DECISION:"background:var(--card);border:2px dashed #6d3fc0", HOLD_ACTIVE:"background:#f2c46d;color:#3b2a05",
    HOLD_EXPIRED:"background:var(--card);border:1px dotted var(--muted);color:var(--muted)",
    BLOCKED:"background:repeating-linear-gradient(45deg,#c9ced8 0 6px,#eef0f4 6px 12px);color:#1d2330"};
  const label = {CONFIRMED:"Confirmed", IN_PROGRESS:"In dock", PENDING_DECISION:"Pending decision", HOLD_ACTIVE:"Hold (≤180 s)",
    HOLD_EXPIRED:"Expired hold", BLOCKED:"Blocked"};
  const facButtons = (SX.facs || []).length > 1 ? `<div class="row" style="margin-bottom:6px">${SX.facs.map(f =>
      `<button class="${f === SX.fac ? "" : "ghost"}" data-act="sfac" data-arg="${esc(f)}">${esc(f)}</button>`).join(" ")}</div>` : "";
  detail.innerHTML = `${facButtons}<div class="row"><h2 class="grow">${esc(s.facility_id)} · ${exact(s.window_start)} – ${exact(s.window_end)}</h2>
      <button class="ghost" data-act="srefresh">Refresh</button></div>
    <p class="muted">${Object.entries(label).map(([k, v]) => `<span class="pill" style="${style[k]}">${v}</span>`).join(" ")}</p>
    <div class="tablewrap"><div style="min-width:640px">${s.docks.map(dk => `<div class="row" style="align-items:stretch;border-bottom:1px solid var(--line);padding:4px 0">
      <div style="width:110px"><b>${esc(dk.dock_code)}</b><div class="muted" style="font-size:11px">${esc(dk.dock_type)} · ${esc(dk.max_vehicle_weight_kg)} kg${dk.supports_refrigerated ? " · reefer" : ""}</div></div>
      <div class="grow" style="position:relative;height:44px;background:var(--bg);border-radius:6px">
        ${s.items.filter(i => i.dock_id === dk.dock_id).map(i => `<div title="${esc(label[i.kind])} ${esc(i.order_reference || "")} ${exact(i.start)}–${exact(i.end)}"
          style="position:absolute;top:4px;bottom:4px;left:${pct(i.start)}%;width:${Math.max(1, pct(i.end) - pct(i.start))}%;${style[i.kind]};border-radius:5px;font-size:11px;overflow:hidden;padding:2px 4px">
          ${esc(i.order_reference || label[i.kind])}</div>`).join("")}
        <div style="position:absolute;top:0;bottom:0;left:${pct(s.as_of)}%;border-left:2px solid var(--bad)" title="now"></div></div></div>`).join("")}</div></div>
    <h3>Receiving capacity (${esc(cap.unit)}, next ${hrs(cap.window_minutes)} h)</h3>
    <div class="row">${[["Total", cap.total], ["Confirmed", cap.confirmed], ["Pending decision", cap.pending_decision], ["Live holds", cap.held],
      ["Blocked", cap.blocked], ["Available", cap.available]].map(([k, v]) => `<div class="card" style="min-width:120px"><div class="muted">${k}</div>
      <b style="font-size:18px">${esc(v)}</b> <span class="muted">dock-min (${hrs(v)} h)</span></div>`).join("")}</div>
    <p class="muted">${esc(cap.explanation)}. A pending request's claim holds its dock time until the decision is due; the 180-second hold only covers
      the moment between a driver's yes and the request.</p>${stamp(s.as_of)}`;
}

async function scheduleAction(act, arg, btn){
  if(act === "srefresh") return render();
  if(act === "sfac"){ if((SX.facs || []).includes(arg)) SX.fac = arg; return render() }
  if(act === "sdecide"){ const appt = btn.dataset.appt, name = `decide:${appt}:${arg}`; btn.disabled = true;
    try { const r = await api("POST", `/console/requests/${encodeURIComponent(appt)}/decision`, {decision:arg, idempotency_key:SX.keys[name] || (SX.keys[name] = key()),
        note:"schedule tab"});
      delete SX.keys[name];
      toast(arg === "CONFIRM" ? `Approved: ${r.status}. The driver is notified through the outbox.`
        : `Rejected: the dock time is released and there is NO confirmed appointment. The driver is notified.`) }
    catch(e){ delete SX.keys[name]; if(e.status === 409){ toast("Not done, the schedule changed: " + errText(e) + ". Refreshed."); return render() } throw e }
    finally { btn.disabled = false }
    return render() }
  if(act === "scancel"){ btn.disabled = true;
    try { const r = await api("POST", `/console/cancellation-requests/${encodeURIComponent(btn.dataset.req)}/decision`, {decision:arg, note:"schedule tab"});
      toast(arg === "APPROVE" ? "Cancelled; the dock time is released and the driver is told." : `Kept: the appointment stays confirmed (${r.status}).`) }
    catch(e){ if(e.status === 409){ toast("Not done: " + errText(e) + ". Refreshed."); return render() } throw e }
    finally { btn.disabled = false }
    return render() }
}

// ---------------------------------------------------------------- Operations health
let OX = {state:null, shipment:null};
async function renderOps(list, detail){
  const [h, esc_, nt, au] = await Promise.all([api("GET", "/console/ops/health"), api("GET", "/console/escalations"),
    api("GET", "/console/ops/notifications" + (OX.state ? "?state=" + OX.state : "")),
    api("GET", "/console/ops/audit" + (OX.shipment ? "?shipment_id=" + encodeURIComponent(OX.shipment) : ""))]);
  const open = esc_.escalations.filter(e => e.status !== "RESOLVED" && e.status !== "REROUTED");
  list.innerHTML = `<h3>Exception queue · ${open.length} open</h3>${stamp(h.as_of)}
    ${open.length ? open.map(e => `<div class="card" style="margin-bottom:8px"><div class="top row"><b class="grow">${esc(e.reason_code)}</b>
        ${chip(e.status, e.status === "OPEN" ? "warn" : "info")}</div>
      <div class="muted">${esc(e.facility_id || "CENTRAL")} · ${esc(e.mode)} · <span class="mono">${esc(e.shipment_id || "")}</span></div>
      <div class="row" style="margin-top:6px">${e.status === "OPEN" ? `<button class="ghost" data-act="oack" data-arg="${esc(e.escalation_id)}">Acknowledge</button>` : ""}
        ${e.thread_id ? `<button class="ghost" data-to="${esc(e.thread_id)}">Take over</button><button class="ghost" data-open="${esc(e.thread_id)}">Conversation</button>` : ""}
        <button class="ok" data-act="oresolve" data-arg="${esc(e.escalation_id)}">Resolve…</button>
        ${e.shipment_id ? `<button class="ghost" data-act="oaudit" data-arg="${esc(e.shipment_id)}">Audit</button>` : ""}</div></div>`).join("")
      : `<div class="empty">No open escalations in your facilities.</div>`}
    <p class="muted">Acknowledge = you own it now; it stays open until resolved. Resolving needs a written outcome.</p>`;
  const NT = {FAILED:"bad", UNKNOWN:"warn", RETRY:"warn", SENT:"", DELIVERED:"ok", PENDING:"", SENDING:""};
  detail.innerHTML = `<div class="row"><h2 class="grow">System health</h2><button class="ghost" data-act="orefresh">Refresh</button></div>
    <div class="row">${[["Escalations open", h.escalations_open], ["Approvals waiting", h.approvals_waiting], ["Cancellations waiting", h.cancellations_waiting],
      ["Telegram last inbound", h.telegram_last_inbound_at ? exact(h.telegram_last_inbound_at) : "none yet"]].map(([k, v]) =>
      `<div class="card" style="min-width:150px"><div class="muted">${k}</div><b>${v === "unavailable" ? "unavailable" : esc(v)}</b></div>`).join("")}</div>
    <p class="muted">Notifications in 24 h by delivery state: ${esc(Object.entries(h.notifications_24h || {}).map(([k, v]) => k + " " + v).join(" · ") || "none")}.
      Queue depth and the dead-letter queue are in CloudWatch, not here.</p>${stamp(h.as_of)}
    <h3>Notifications (delivery state only)</h3>
    <div class="row">${["", "FAILED", "UNKNOWN", "RETRY", "SENT"].map(sv => `<button class="${(OX.state || "") === sv ? "" : "ghost"}" data-act="ostate" data-arg="${sv}">${sv || "All"}</button>`).join("")}</div>
    ${nt.notifications.length ? `<div class="tablewrap"><table><tr><th>What</th><th>Order</th><th>Route now</th><th>State</th><th>Tries</th><th>Last attempt</th><th>Next retry</th><th>Last error</th></tr>
      ${nt.notifications.map(n => `<tr><td>${esc(n.what)}</td><td>${esc(n.order_reference || "")}</td><td>${esc(n.route_now)}${n.transport === "test sink" ? " · test sink" : ""}</td>
        <td>${chip(n.status, NT[n.status])}${n.status === "UNKNOWN" ? `<div class="muted">timeout: may have arrived; deadline ${exact(n.unknown_deadline)}</div>` : ""}</td>
        <td>${esc(n.attempts)}/${esc(n.max_attempts)}</td><td>${exact(n.last_attempt_at)}</td><td>${exact(n.next_retry_at)}</td><td class="muted">${esc(n.last_error || "")}</td></tr>`).join("")}</table></div>`
      : `<div class="empty">No notifications in this state.</div>`}
    <h3>Audit trail ${OX.shipment ? "· " + esc(OX.shipment) + ` <button class="ghost" data-act="oaudit" data-arg="">All</button>` : ""}</h3>
    ${au.events.length ? `<div class="tablewrap"><table><tr><th>When</th><th>Who</th><th>Action</th><th>Shipment</th><th>Note</th></tr>
      ${au.events.map(e => `<tr><td>${exact(e.at)}</td><td class="mono">${esc(e.by)}</td><td>${esc(e.action)}</td><td class="mono">${esc(e.shipment_id || "")}</td>
        <td class="muted">${esc(e.note || "")}</td></tr>`).join("")}</table></div>` : `<div class="empty">No events.</div>`}`;
}

async function opsAction(act, arg, btn){
  if(act === "orefresh") return render();
  if(act === "ostate"){ OX.state = arg || null; return render() }
  if(act === "oaudit"){ OX.shipment = arg || null; return render() }
  if(act === "oack"){ btn.disabled = true; try { await api("POST", `/console/escalations/${arg}/acknowledge`, {note:"acknowledged in Ops health"});
    toast("Acknowledged: you own it. It is not resolved.") } finally { btn.disabled = false } return render() }
  if(act === "oresolve"){ const resolution = prompt("What was done? (at least 5 characters)", ""); if(!resolution) return;
    await api("POST", `/console/escalations/${arg}/resolve`, {resolution}); toast("Resolved."); return render() }
}

document.addEventListener("click", async ev => {
  const b = ev.target.closest("[data-act]"); if(!b) return;
  const act = b.dataset.act, arg = b.dataset.arg;
  try {
    if(act.startsWith("d")) await dispatchAction(act, arg, b);
    else if(act.startsWith("s")) await scheduleAction(act, arg, b);
    else if(act.startsWith("o")) await opsAction(act, arg, b);
  } catch(e){ if(e.message !== "signed out" && e.message !== "401")
    toast((e.status === 403 ? "Not permitted for your role: " : e.status === 409 ? "Not done: " : "Failed: ") + errText(e)) }
});
document.addEventListener("keydown", ev => { if(ev.key === "Enter" && ev.target.matches("[data-act='dopen']")) ev.target.click() });
