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
    if(!h.booking_enabled) pill("#killpill", "kill switch: not applicable (booking off)", "unk");
    else if(ks === "ENGAGED") pill("#killpill", "kill switch: ENGAGED (bookings refused)", "warn");
    else if(ks === "disengaged") pill("#killpill", "kill switch: disengaged", "on");
    else pill("#killpill", "kill switch: status unavailable", "unk");
    if(h.database !== "ok") pill("#bookingpill", "booking execution: status unavailable (database " + (h.database || "?") + ")", "unk");
  } catch {
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
  }).catch(() => {});
  render();
}
function setTab(t){ tab = t; selected = null; nextBefore = null; threads = [];
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t)); render() }

async function render(){
  const titles = {threads:"Conversations", requests:"Requests waiting for a decision", escalations:"Escalations", docks:"Docks",
                  optimizer:"Slot optimizer (Phase G)", observability:"Observability (Phase G)"};
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
async function renderObservability(list, detail){
  const r = await api("GET", "/console/observability?hours=24");
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
    ${renderLangSmith(r.langsmith || {}, a.recent_turns || [])}
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
}

// G.1 (G6): LangSmith links for planner/ops_lead (the server only returns this tab to them). Links only: the browser
// holds no LangSmith credential and makes no call to LangSmith.
function renderLangSmith(ls, turns){
  const ev = ls.latest_eval;
  return `<h2 style="margin-top:18px">LangSmith (traces and evaluation)</h2>
    ${kvTable({"tracing": ls.tracing, "project": ls.project_url ? "configured (link below)" : (ls.project_status || "unavailable"),
      "latest evaluation": ev ? `${ev.suite}: ${ev.passed}/${ev.total} passed, ${ev.failed} failed` : "unavailable: no evaluation run recorded",
      "dataset version": ls.dataset_version || "unavailable", "evaluator version": ls.evaluator_version || "unavailable",
      "experiment": ls.experiment_url ? "recorded (link below)" : "unavailable: no LangSmith experiment recorded"})}
    ${ls.project_url ? `<p>Project: ${extLink(ls.project_url, ls.project_url)}</p>` : ""}
    ${ls.experiment_url ? `<p>Latest experiment: ${extLink(ls.experiment_url, ls.experiment_url)}</p>` : ""}
    <h3>Recent turns</h3>
    ${turns.length ? `<div class="tablewrap"><table><tr><th>When</th><th>Facility</th><th>Reader</th><th>Failure category</th><th>ms</th><th>Trace</th></tr>${turns.map(t =>
      `<tr><td>${when(t.created_at)}</td><td>${esc(t.facility_id || "CENTRAL")}</td><td>${esc(t.extractor)}</td><td>${esc(t.fallback_reason || "")}</td>
       <td>${esc(t.turn_ms)}</td><td>${t.trace_url ? extLink(t.trace_url, "open trace") : `<span class="muted mono" title="Search CloudWatch for this turn_ref">${esc(String(t.turn_ref).slice(0, 8))}… (no trace link: tracing ${esc(ls.tracing)}${ls.project_url ? "" : ", project URL not configured"})</span>`}</td></tr>`).join("")}</table></div>`
      : `<div class="muted">No turns in this window.</div>`}`;
}

async function loadThreads(more){
  const q = new URLSearchParams({limit:"25"}); if(more && nextBefore) q.set("before", nextBefore);
  const r = await api("GET", "/console/threads?" + q);
  threads = more ? threads.concat(r.threads) : r.threads; nextBefore = r.next_before;
  $("#list").innerHTML = threads.length ? threads.map(t => `<div class="item ${t.thread_id === selected ? "sel" : ""}" data-open="${esc(t.thread_id)}">
    <div class="top"><b class="mono">${esc(t.driver_phone || t.driver_id)}</b><span class="muted">${when(t.last_message_at)}</span></div>
    <div class="muted">${esc(t.facility_id)} · ${esc(t.flow_state || t.status)}${t.human_active ? " · <b>a person has it</b>" : ""}${t.intent ? " · " + esc(t.intent) : ""}</div>
    <div>${t.last_sender === "AGENT" ? "↩ " : ""}${esc(t.last_message_preview)}</div>
    ${t.notification_status ? `<div class="muted">reply ${esc(t.notification_status)}${t.notification_delivery ? " · " + esc(t.notification_delivery) : ""}</div>` : ""}</div>`).join("")
    : `<div class="empty">No conversations in your facilities yet. Send a synthetic driver message (docs/release/11_STAFF_CONSOLE.md).</div>`;
  $("#more").hidden = !nextBefore;
}

async function openThread(id){
  selected = id; if(tab !== "threads"){ tab = "threads"; document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === "threads")); await loadThreads(false) }
  document.querySelectorAll("#list .item").forEach(e => e.classList.toggle("sel", e.dataset.open === id));
  const d = $("#detail"); d.innerHTML = `<div class="empty">Loading…</div>`;
  let b; try { b = await api("GET", "/console/threads/" + encodeURIComponent(id)) }
  catch(e){ d.innerHTML = `<div class="empty err">${e.status === 404 ? "Not found, or not in your facilities." : "Could not load: " + esc(e.message)}</div>`; return }
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
        : `<div class="b ${m.sender === "DRIVER" ? "in" : m.sender === "AGENT" ? "out" : "sys"}">${esc(m.body_masked)}<small>${esc(m.sender)} · ${when(m.at)}</small></div>`).join("") || `<div class="empty">No messages.</div>`}</div>
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

// ---------------------------------------------------------------- events
document.addEventListener("click", async ev => {
  const b = ev.target.closest("button,[data-open]"); if(!b) return;
  try {
    if(b.dataset.t) setTab(b.dataset.t);
    else if(b.dataset.open !== undefined){ if(b.dataset.open) openThread(b.dataset.open); else toast("No conversation for this escalation.") }
    else if(b.id === "refresh"){ loadStatus(); selected && tab === "threads" ? (await loadThreads(false), openThread(selected)) : render() }
    else if(b.id === "more") loadThreads(true);
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
  setInterval(() => { if(TOKEN && Date.now() > EXPIRES - 15000){ TOKEN = null; showSignIn("Your session has ended. Please sign in again.") } }, 10000);
  try { if(await finishSignIn()) return showApp() } catch(e){ return showSignIn(e.message) }
  showSignIn();
})();
