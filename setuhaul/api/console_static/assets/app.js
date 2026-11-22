// SetuHaul hosted staff console (docs/release/11_STAFF_CONSOLE.md), served by the API under /app/ (same origin).
// Sign-in: Cognito hosted page, authorization code with PKCE, public client (no secret here). The access token is
// kept in memory only and sent as a bearer token; the API checks it, the staff group and the facility scope on every
// request. This page decides nothing: what it shows is what the API returns for this person.
"use strict";
const $ = s => document.querySelector(s);
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

// ---------------------------------------------------------------- views
function showSignIn(msg){ $("#app").hidden = true; $("#signout").hidden = true; $("#who").textContent = "";
  $("#signin").hidden = false; $("#autherr").textContent = msg || "" }
function showApp(){
  const c = claims(TOKEN);
  const groups = (c["cognito:groups"] || []).filter(g => !g.startsWith("facility-"));
  $("#who").textContent = `${c.username || ""} · ${groups.join(", ") || "no staff role"} · ${c.facility_scope || "no facility"}`;
  $("#signin").hidden = true; $("#app").hidden = false; $("#signout").hidden = false;
  render();
}
function setTab(t){ tab = t; selected = null; nextBefore = null; threads = [];
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t)); render() }

async function render(){
  const titles = {threads:"Conversations", requests:"Requests waiting for a decision", escalations:"Escalations", docks:"Docks"};
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
        : `<div class="empty">No open requests. Booking execution is off in this pilot, so driver choices do not create requests.</div>`;
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
    }
  } catch(e){ if(e.message !== "signed out" && e.message !== "401") list.innerHTML = `<div class="empty err">Could not load: ${esc(e.message)}</div>` }
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
    <div class="chat">${b.messages.map(m => `<div class="b ${m.sender === "DRIVER" ? "in" : m.sender === "AGENT" ? "out" : "sys"}">${esc(m.body_masked)}<small>${esc(m.sender)} · ${when(m.at)}</small></div>`).join("") || `<div class="empty">No messages.</div>`}</div>
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
    else if(b.id === "refresh") selected && tab === "threads" ? (await loadThreads(false), openThread(selected)) : render();
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
  catch { showSignIn("This console is not configured (config.json missing)."); return }
  setInterval(() => { if(TOKEN && Date.now() > EXPIRES - 15000){ TOKEN = null; showSignIn("Your session has ended. Please sign in again.") } }, 10000);
  try { if(await finishSignIn()) return showApp() } catch(e){ return showSignIn(e.message) }
  showSignIn();
})();
