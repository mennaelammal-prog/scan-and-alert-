// scanalert UI: dependency-free SPA. PAPER ONLY - there is no order-entry UI anywhere.
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "-" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const money = (v) => (v === null || v === undefined ? "-" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 }));
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
// All market times are shown in US Eastern time (the exchange clock), never the browser's timezone.
const ET_OPTS = { timeZone: "America/New_York" };
const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-US", { ...ET_OPTS, hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " ET" : "-");
const dt = (iso) => (iso ? new Date(iso).toLocaleString("en-US", { ...ET_OPTS, dateStyle: "short", timeStyle: "medium" }) + " ET" : "-");

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const d = data && (data.detail ?? data);
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return data;
}

// ------------------------------------------------------------------ global state / status
const state = { status: null, alerts: new Map(), ws: null, sound: true, feats: [], timers: [] };
function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }

function renderBadges() {
  const s = state.status;
  const el = $("#badges");
  const b = ['<span class="badge paper">PAPER ONLY</span>', '<span class="badge paper">SIMULATED</span>'];
  if (s) {
    b.push(`<span class="badge">${esc(s.market_session).toUpperCase()}</span>`);
    const fs = s.feed_state;
    b.push(`<span class="badge ${fs === "stale" || fs === "disconnected" ? "bad" : "good"}">${esc(fs).toUpperCase()}</span>`);
    if (fs === "stale") b.push('<span class="badge bad">STALE FEED</span>');
    b.push(`<span class="badge">${esc(hhmm(s.clock))}</span>`);
  }
  el.innerHTML = b.join("");
  const banner = $("#banner");
  if (s && (s.feed_state === "stale" || s.feed_state === "disconnected")) {
    banner.hidden = false;
    banner.textContent = s.feed_state === "stale" ? "STALE FEED: no recent market data. Alerts and Top Lists may be out of date." : "Data provider disconnected. Reconnecting automatically.";
  } else banner.hidden = true;
}

async function refreshStatus() {
  try { state.status = await api("/api/status"); renderBadges(); } catch { /* ignore */ }
  return state.status;
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  state.ws = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "hello") msg.recent.forEach((a) => state.alerts.set(a.event_id, a));
    else if (msg.type === "test_notification") {
      notify(msg.alert, msg.warnings || []);
      const b = $("#banner"); b.hidden = false; b.textContent = "Test notification received (paper only, no order was created).";
      setTimeout(renderBadges, 4000);
      return;
    } else if (msg.type === "alert" || msg.type === "alert_update" || msg.type === "ack_ok") {
      const a = msg.alert;
      state.alerts.set(a.event_id, a);
      if (msg.type === "alert") notify(a, msg.warnings || []);
    }
    window.dispatchEvent(new CustomEvent("alerts-changed"));
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.value = 0.05;
    o.start(); o.stop(ctx.currentTime + 0.12);
  } catch { /* audio blocked */ }
}
function notify(a, warnings) {
  if (state.sound) beep();
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(`${a.symbol} ${a.event_type} (paper only)`, { body: `${a.strategy_id} @ ${a.trigger_price ?? "-"}${warnings.length ? " | " + warnings.join(", ") : ""}` });
  }
}

// ------------------------------------------------------------------------ SVG charts
function lineChart(points, { w = 640, h = 180, color = "var(--accent)", baseline = null, label = "" } = {}) {
  if (!points.length) return '<p class="muted">no data</p>';
  const xs = points.map((p, i) => i); const ys = points.map((p) => p.y);
  let lo = Math.min(...ys, baseline ?? Infinity), hi = Math.max(...ys, baseline ?? -Infinity);
  if (lo === hi) { lo -= 1; hi += 1; }
  const X = (i) => 30 + (i / Math.max(1, xs.length - 1)) * (w - 40);
  const Y = (v) => 10 + (1 - (v - lo) / (hi - lo)) * (h - 30);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(" ");
  const base = baseline !== null ? `<line x1="30" x2="${w - 10}" y1="${Y(baseline)}" y2="${Y(baseline)}" stroke="var(--line)" stroke-dasharray="4"/>` : "";
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="${esc(label)}">${base}<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>
  <text x="2" y="14">${fmt(hi, 0)}</text><text x="2" y="${h - 22}">${fmt(lo, 0)}</text>
  <text x="30" y="${h - 6}">${esc((points[0].x || "").slice(0, 10))}</text><text x="${w - 90}" y="${h - 6}">${esc((points.at(-1).x || "").slice(0, 10))}</text></svg>`;
}
function barChart(items, { w = 640, h = 160 } = {}) {
  if (!items.length) return '<p class="muted">no data</p>';
  const max = Math.max(...items.map((i) => Math.abs(i.y)), 1);
  const bw = Math.max(2, (w - 40) / items.length - 3);
  const mid = h / 2 - 8;
  const bars = items.map((it, i) => {
    const bh = (Math.abs(it.y) / max) * (mid - 6); const x = 30 + i * ((w - 40) / items.length);
    return `<rect x="${x.toFixed(1)}" y="${(it.y >= 0 ? mid - bh : mid).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, bh).toFixed(1)}" fill="${it.y >= 0 ? "var(--good)" : "var(--bad)"}"><title>${esc(it.x)}: ${money(it.y)}</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="daily profit and loss"><line x1="30" x2="${w - 10}" y1="${mid}" y2="${mid}" stroke="var(--line)"/>${bars}<text x="2" y="12">${money(max)}</text><text x="2" y="${h - 22}">${money(-max)}</text></svg>`;
}
function candles(bars, triggerTs, triggerPrice) {
  if (!bars.length) return '<p class="muted">no stored bars for this alert yet</p>';
  const w = 720, h = 260, pad = 34;
  const lo = Math.min(...bars.map((b) => b.low)), hi = Math.max(...bars.map((b) => b.high));
  const Y = (v) => 10 + (1 - (v - lo) / (hi - lo || 1)) * (h - 40);
  const step = (w - pad - 10) / bars.length; const cw = Math.max(1.5, step * 0.6);
  let out = ""; let markX = null;
  bars.forEach((b, i) => {
    const x = pad + i * step + step / 2; const up = b.close >= b.open;
    const c = up ? "var(--good)" : "var(--bad)";
    out += `<line x1="${x}" x2="${x}" y1="${Y(b.high)}" y2="${Y(b.low)}" stroke="${c}"/><rect x="${x - cw / 2}" y="${Y(Math.max(b.open, b.close))}" width="${cw}" height="${Math.max(1, Math.abs(Y(b.open) - Y(b.close)))}" fill="${c}"><title>${esc(hhmm(b.bar_ts))} O${b.open} H${b.high} L${b.low} C${b.close} V${b.volume}</title></rect>`;
    if (new Date(b.bar_ts).getTime() + 60000 >= new Date(triggerTs).getTime() && markX === null) markX = x;
  });
  const mark = markX !== null ? `<line x1="${markX}" x2="${markX}" y1="8" y2="${h - 28}" stroke="var(--paper)" stroke-dasharray="4"/><text x="${markX + 3}" y="18">alert${triggerPrice ? " @ " + triggerPrice : ""}</text>` : "";
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="1-minute chart around the alert">${out}${mark}<text x="2" y="14">${fmt(hi)}</text><text x="2" y="${h - 30}">${fmt(lo)}</text><text x="${pad}" y="${h - 8}">${esc(hhmm(bars[0].bar_ts))}</text><text x="${w - 70}" y="${h - 8}">${esc(hhmm(bars.at(-1).bar_ts))}</text></svg>`;
}

// ----------------------------------------------------------------------------- views
const HOLD_ORDER = ["<5m", "5-15m", "15-30m", "30-60m", "1-2h", "2h-1d", ">=1 session"];
const views = {};

views.dashboard = async (root) => {
  const s = await refreshStatus();
  const alerts = [...state.alerts.values()].sort((a, b) => b.source_timestamp.localeCompare(a.source_timestamp));
  const trig = alerts.filter((a) => a.status === "triggered").length;
  root.innerHTML = `<h1>Dashboard</h1>
  <div class="grid">
    <div class="card"><div class="k">Market session</div><div class="v">${esc(s.market_session)}</div><div class="muted">${s.early_close ? "early close" : s.is_trading_day ? "trading day" : "market closed"}</div></div>
    <div class="card"><div class="k">Feed</div><div class="v">${esc(s.feed_state)}</div><div class="muted">${esc(s.provider.provider)} / ${esc(s.provider.feed)}</div></div>
    <div class="card"><div class="k">Alerts</div><div class="v">${alerts.length}</div><div class="muted">${trig} triggered</div></div>
    <div class="card"><div class="k">Events processed</div><div class="v">${fmt(s.events_processed, 0)}</div><div class="muted">${s.symbols} symbols</div></div>
    <div class="card"><div class="k">Last event</div><div class="v" style="font-size:15px">${esc(dt(s.last_event_time))}</div><div class="muted">reconnects ${s.provider.reconnects} | dup ${s.provider.duplicate_events} | gaps ${s.provider.gaps}</div></div>
    <div class="card"><div class="k">Mode</div><div class="v">PAPER ONLY</div><div class="muted">live trading not supported</div></div>
  </div>
  <div class="panel" style="margin-top:12px"><div class="row"><h2 style="margin:0">Recent events</h2><a href="#/alerts">all alerts</a></div>${alertsTable(alerts.slice(0, 10))}</div>
  <div class="panel"><h2>Provider events</h2>${s.recent_provider_events.length ? "<ul class='plain mono'>" + s.recent_provider_events.map((e) => `<li>${esc(e.kind)} ${esc(e.symbol || "")} ${esc(e.detail || "")}</li>`).join("") + "</ul>" : '<p class="muted">none</p>'}</div>
  ${s.provider.provider === "fixture" ? `<div class="panel"><h2>Fixture replay (development)</h2><div class="row"><button id="rr">Restart replay</button><button id="rf">Run to end</button><span id="rmsg" class="muted"></span></div></div>` : ""}`;
  bindAlertRows(root);
  const rr = $("#rr", root), rf = $("#rf", root);
  if (rr) rr.onclick = async () => { state.alerts.clear(); await api("/api/dev/replay/restart", { method: "POST" }); $("#rmsg", root).textContent = "replay restarted"; };
  if (rf) rf.onclick = async () => { $("#rmsg", root).textContent = "running..."; const r = await api("/api/dev/replay/run", { method: "POST" }); $("#rmsg", root).textContent = `${r.events} events, ${r.alerts} alerts`; };
  state.timers.push(setInterval(() => { if (location.hash.startsWith("#/dashboard") || location.hash === "") views.dashboard(root); }, 5000));
};

function alertsTable(list, { ack = false } = {}) {
  if (!list.length) return '<p class="muted">No alerts yet. Alerts are event-driven: they appear when a strategy condition transitions to true.</p>';
  return `<table><thead><tr><th>Time</th><th>Symbol</th><th>Strategy</th><th>Event</th><th>Dir</th><th>Priority</th><th>Status</th><th class="num">Price</th><th class="num">Spread bps</th><th class="num">Delay</th><th>Flags</th>${ack ? "<th></th>" : ""}</tr></thead><tbody>${list.map((a) => `
  <tr class="clickable ${a.status === "suppressed" ? "sup" : ""}" data-id="${esc(a.event_id)}"><td>${esc(hhmm(a.source_timestamp))}</td><td><b>${esc(a.symbol)}</b></td><td>${esc(a.strategy_id)} v${a.strategy_version}</td><td>${esc(a.event_type)}</td><td>${esc(a.direction)}</td>
  <td><span class="pill ${esc(a.priority)}">${esc(a.priority)}</span></td><td><span class="pill ${esc(a.status)}" title="${esc(a.status_reason)}">${esc(a.status)}</span></td><td class="num">${fmt(a.trigger_price)}</td><td class="num">${fmt(a.spread_bps, 1)}</td>
  <td class="num">${a.delivery_delay_ms == null ? "-" : a.delivery_delay_ms + " ms"}</td><td>${a.stale_data ? '<span class="badge bad">STALE FEED</span> ' : ""}${a.delayed_delivery ? '<span class="badge warn">DATA DELAY</span>' : ""}</td>
  ${ack ? `<td>${a.status === "triggered" ? `<button data-ack="${esc(a.event_id)}">Acknowledge</button>` : ""}</td>` : ""}</tr>`).join("")}</tbody></table>`;
}
function bindAlertRows(root) {
  root.querySelectorAll("tr.clickable").forEach((tr) => (tr.onclick = (e) => { if (e.target.dataset.ack) return; location.hash = `#/alert/${tr.dataset.id}`; }));
  root.querySelectorAll("button[data-ack]").forEach((b) => (b.onclick = async () => {
    try { const a = await api(`/api/alerts/${b.dataset.ack}/acknowledge`, { method: "POST" }); state.alerts.set(a.event_id, a); window.dispatchEvent(new CustomEvent("alerts-changed")); } catch (e) { alert(e.message); }
  }));
}

views.alerts = async (root) => {
  const initial = await api("/api/alerts?limit=300");
  initial.forEach((a) => { if (!state.alerts.has(a.event_id) || state.alerts.get(a.event_id).status === a.status) state.alerts.set(a.event_id, a); });
  let showSup = false, statusFilter = "";
  const draw = () => {
    const list = [...state.alerts.values()].filter((a) => (showSup || a.status !== "suppressed") && (!statusFilter || a.status === statusFilter)).sort((a, b) => b.source_timestamp.localeCompare(a.source_timestamp)).slice(0, 300);
    $("#tbl", root).innerHTML = alertsTable(list, { ack: true });
    bindAlertRows($("#tbl", root));
  };
  root.innerHTML = `<h1>Live alerts</h1>
  <div class="panel"><div class="row">
    <label>Status<select id="sf"><option value="">all</option><option>triggered</option><option>working</option><option>acknowledged</option><option>expired</option><option>invalidated</option></select></label>
    <label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="sup"> show suppressed</label>
    <label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="snd" ${state.sound ? "checked" : ""}> sound</label>
    <button id="perm">Enable browser notifications</button><button id="tst">Send test notification</button><span id="msg" class="muted"></span></div>
    <div id="tbl"></div></div>
  <p class="muted">Alerts are hypothetical signals. Suppressed rows (cooldown, duplicate, daily cap) are kept for audit.</p>`;
  $("#sup", root).onchange = (e) => { showSup = e.target.checked; draw(); };
  $("#sf", root).onchange = (e) => { statusFilter = e.target.value; draw(); };
  $("#snd", root).onchange = (e) => { state.sound = e.target.checked; };
  $("#perm", root).onclick = async () => { if ("Notification" in window) $("#msg", root).textContent = "permission: " + (await Notification.requestPermission()); };
  $("#tst", root).onclick = async () => { const r = await api("/api/notifications/test", { method: "POST" }); $("#msg", root).textContent = r.sent.map((d) => `${d.channel}: ${d.status}${d.reason ? " (" + d.reason + ")" : ""}`).join(", ") + " - test only, no order"; if (state.sound) beep(); };
  const on = () => draw();
  window.addEventListener("alerts-changed", on);
  root._cleanup = () => window.removeEventListener("alerts-changed", on);
  draw();
};

views.toplists = async (root) => {
  const strategies = await api("/api/strategies");
  const sel = location.hash.split("?s=")[1] || (strategies[0] && strategies[0].id);
  root.innerHTML = `<h1>Top Lists</h1><div class="panel"><div class="row"><label>Strategy<select id="st">${strategies.map((s) => `<option ${s.id === sel ? "selected" : ""} value="${esc(s.id)}">${esc(s.name)} (v${s.version})</option>`).join("")}</select></label><span id="meta" class="muted"></span></div><div id="tl"></div></div>
  <p class="muted">A Top List is a periodic ranked snapshot (server-side rank, then optional display sort). It is separate from event alerts and is never an order.</p>`;
  const load = async () => {
    const id = $("#st", root).value;
    try {
      const t = await api(`/api/top-lists/${encodeURIComponent(id)}`);
      $("#meta", root).innerHTML = `refreshed ${esc(hhmm(t.generated_at))} (as of ${esc(hhmm(t.as_of))}) | evaluated ${t.evaluated}, qualified ${t.qualified} | rank by ${esc(t.ranking.formula || t.ranking.field)} ${esc(t.ranking.order)} | data age ${t.data_age_seconds == null ? "-" : fmt(t.data_age_seconds, 0) + "s"} ${t.stale ? '<span class="badge bad">STALE FEED</span>' : ""} <span class="badge paper">HYPOTHETICAL</span>`;
      $("#tl", root).innerHTML = t.rows.length ? `<table><thead><tr><th>#</th><th>Symbol</th><th class="num">Score</th><th class="num">Last</th><th class="num">% chg</th><th class="num">RVOL</th><th class="num">Volume</th><th class="num">Spread bps</th><th class="num">VWAP dist %</th></tr></thead><tbody>${t.rows.map((r) => `<tr><td>${r.rank}</td><td><b>${esc(r.symbol)}</b></td><td class="num">${fmt(r.score, 3)}</td><td class="num">${fmt(r.last)}</td><td class="num ${cls(r.pct_change)}">${fmt(r.pct_change)}</td><td class="num">${fmt(r.rvol)}</td><td class="num">${fmt(r.volume, 0)}</td><td class="num">${fmt(r.spread_bps, 1)}</td><td class="num">${fmt(r.features.vwap_dist_pct)}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">No symbols currently qualify.</p>';
    } catch (e) { $("#tl", root).innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  };
  $("#st", root).onchange = load;
  await load();
  state.timers.push(setInterval(load, 5000));
};

views.alert = async (root, id) => {
  let d;
  try { d = await api(`/api/alerts/${encodeURIComponent(id)}`); } catch (e) { root.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  const a = d.alert;
  const chart = await api(`/api/alerts/${encodeURIComponent(id)}/chart`).catch(() => ({ bars: [] }));
  const fs = a.filter_snapshot || {};
  const frows = [...(fs.strategy_filters || []).map((r) => ["gate", r]), ...(fs.condition_filters || []).map((r) => ["condition", r])];
  root.innerHTML = `<h1>${esc(a.symbol)} <span class="pill ${esc(a.status)}">${esc(a.status)}</span> <span class="badge paper">HYPOTHETICAL SIGNAL</span> ${a.stale_data ? '<span class="badge bad">STALE FEED</span>' : ""} ${a.delayed_delivery ? '<span class="badge warn">DATA DELAY</span>' : ""}</h1>
  <div class="grid"><div class="card"><div class="k">Strategy</div><div class="v" style="font-size:15px">${esc(a.strategy_id)} v${a.strategy_version}</div><div class="muted">cond ${esc(a.condition_id)} | ${esc(a.strategy_config_hash)}</div></div>
  <div class="card"><div class="k">Trigger</div><div class="v">${fmt(a.trigger_price)}</div><div class="muted">${esc(a.direction)} ${esc(a.event_type)} (${esc(a.session)})</div></div>
  <div class="card"><div class="k">Bid / Ask</div><div class="v" style="font-size:15px">${fmt(a.bid)} / ${fmt(a.ask)}</div><div class="muted">${fmt(a.spread_bps, 1)} bps</div></div>
  <div class="card"><div class="k">Source / detected / delivered</div><div class="mono">${esc(a.source_timestamp)}<br>${esc(a.detected_timestamp)}<br>${esc(a.delivered_timestamp || "-")}</div></div></div>
  <div class="row">${a.status === "triggered" ? '<button id="ack">Acknowledge</button>' : ""}<a href="${esc(chart.external_chart || "#")}" target="_blank" rel="noopener noreferrer">External chart link</a><span class="muted">${esc(a.status_reason)}</span></div>
  <div class="panel"><h2>Chart (1-minute, local)</h2>${candles(chart.bars, a.source_timestamp, a.trigger_price)}</div>
  <div class="two"><div class="panel"><h2>Filter evaluation (exact config preserved)</h2><table><thead><tr><th>Scope</th><th>Filter</th><th>Op</th><th>Threshold</th><th>Observed</th><th>Result</th></tr></thead><tbody>${frows.map(([s, r]) => `<tr><td>${s}</td><td>${esc(r.filter_id)} v${r.filter_version}</td><td>${esc(r.operator)}</td><td class="mono">${esc(JSON.stringify(r.threshold))}</td><td class="mono">${esc(JSON.stringify(r.observed))}</td><td class="${r.passed ? "ok" : "err"}">${r.passed ? "pass" : "fail"}${r.null ? " (null)" : ""}</td></tr>`).join("")}</tbody></table></div>
  <div class="panel"><h2>Feature snapshot</h2><table><tbody>${Object.entries(a.feature_snapshot).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num mono">${esc(v === null ? "null" : v)}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="panel"><h2>Paper entry proposal <span class="badge paper">SIMULATED</span></h2>
    <p class="muted">Creates a hypothetical intent and simulated fill. It is never sent to a broker.</p>
    <div class="form"><label>Risk $<input id="risk" type="number" value="100" min="1"></label>
    <label>Entry model<select id="em"><option>spread_plus_slippage</option><option>bid_ask_cross</option><option>fixed_slippage</option><option>next_trade</option><option>next_bar_open</option></select></label>
    <label>Slippage bps<input id="slip" type="number" value="2" step="0.5"></label><label>Stop %<input id="stp" type="number" value="1" step="0.1"></label>
    <label>Target (R multiple)<input id="tgt" type="number" value="2" step="0.5"></label><label>Time exit (min)<input id="tex" type="number" value="60"></label>
    <button id="mk" class="primary">Create paper intent</button></div><div id="pi"></div></div>
  <div class="panel"><h2>Notification audit</h2>${d.deliveries.length ? `<table><thead><tr><th>Channel</th><th>Status</th><th>Attempts</th><th>Reason/Error</th><th>Latency</th></tr></thead><tbody>${d.deliveries.map((x) => `<tr><td>${esc(x.channel)}</td><td>${esc(x.status)}</td><td>${x.attempts}</td><td>${esc(x.reason || x.error)}</td><td>${x.latency_ms ?? "-"} ms</td></tr>`).join("")}</tbody></table>` : '<p class="muted">none</p>'}</div>`;
  const ack = $("#ack", root); if (ack) ack.onclick = async () => { await api(`/api/alerts/${id}/acknowledge`, { method: "POST" }); views.alert(root, id); };
  const piBox = $("#pi", root);
  const showIntents = (list) => { piBox.innerHTML = list.map((i) => intentCard(i)).join(""); };
  showIntents(d.paper_intents);
  $("#mk", root).onclick = async () => {
    try {
      const r = await api("/api/paper-intents", { method: "POST", body: { event_id: id, risk_dollars: +$("#risk", root).value, entry_model: $("#em", root).value, slippage_bps: +$("#slip", root).value, stop: { type: "percent", value: +$("#stp", root).value }, target: { type: "r_multiple", value: +$("#tgt", root).value }, time_exit_minutes: +$("#tex", root).value || null } });
      piBox.innerHTML = intentCard(r.intent, r.fills) + piBox.innerHTML;
    } catch (e) { piBox.innerHTML = `<p class="err">${esc(e.message)}</p>` + piBox.innerHTML; }
  };
};
function intentCard(i, fills = []) {
  return `<div class="card" style="margin-top:8px"><div class="row"><b>${esc(i.symbol)} ${esc(i.direction)} x${fmt(i.quantity, 0)}</b><span class="pill">${esc(i.status)}</span><span class="badge paper">SIMULATED - NOT SUBMITTED</span></div>
  <div class="mono">entry~${fmt(i.est_entry_price ?? i.detail?.est_entry_price, 4)} stop ${fmt(i.stop_price ?? i.detail?.stop_price, 4)} target ${fmt(i.target_price ?? i.detail?.target_price, 4)} | spread ${fmt(i.est_spread_bps, 1)} bps | slip ${fmt(i.est_slippage_bps, 1)} bps | max modelled loss ${money(i.max_modeled_loss)} | strategy v${i.strategy_version} | expires ${esc(hhmm(i.expires_at))}</div>
  ${fills.map((f) => `<div class="mono">fill: ${esc(f.status)} ${esc(f.side)} ${fmt(f.quantity, 0)} @ ${fmt(f.price, 4)} (${esc(f.model)}) ${f.reject_reason ? "- " + esc(f.reject_reason) : ""} [simulated]</div>`).join("")}</div>`;
}

// ---------------------------------------------------------------- scanner builder
const TEMPLATE = () => ({ id: "my-scan", name: "My scan", direction: "long", filters: [{ id: "min-price", name: "Price >= 5", field: "last", operator: "gte", value: 5, null_policy: "fail", session_basis: "regular", enabled: true, version: 1 }], alert_conditions: [{ id: "cond-1", name: "Above VWAP on volume", event_type: "signal", priority: "normal", cooldown_seconds: 900, filters: [{ id: "rvol", name: "RVOL >= 1.5", field: "rvol", operator: "gte", value: 1.5, null_policy: "fail", session_basis: "regular", enabled: true, version: 1 }, { id: "vwap", name: "Above VWAP", field: "vwap_dist_pct", operator: "gt", value: 0, null_policy: "fail", session_basis: "regular", enabled: true, version: 1 }] }], ranking: { field: "rvol", order: "desc" }, display_sort: { field: "pct_change", order: "desc" } });

views.scanner = async (root) => {
  if (!state.feats.length) state.feats = await api("/api/features");
  const strategies = await api("/api/strategies");
  let draft = TEMPLATE();
  const featOpts = (sel) => state.feats.map((f) => `<option value="${f.name}" ${f.name === sel ? "selected" : ""}>${f.name} (${f.unit})</option>`).join("") + `<option value="formula" ${sel === "formula" ? "selected" : ""}>formula...</option>`;
  const filterRow = (f, path) => `<tr data-path="${path}"><td><input data-k="id" value="${esc(f.id)}" size="10"></td><td><select data-k="field">${featOpts(f.field)}</select></td>
    <td><select data-k="operator">${["gt", "gte", "lt", "lte", "eq", "neq", "between", "outside", "is_true", "is_false"].map((o) => `<option ${o === f.operator ? "selected" : ""}>${o}</option>`).join("")}</select></td>
    <td>${f.field === "formula" ? `<input data-k="formula" value="${esc(f.formula || "")}" size="34" placeholder="rvol > 2 and vwap_dist_pct > 0">` : `<input data-k="value" value="${esc(Array.isArray(f.value) ? f.value.join(",") : f.value ?? "")}" size="9">`}</td>
    <td><input data-k="lookback" type="number" value="${f.lookback ?? ""}" style="width:64px"></td>
    <td><select data-k="null_policy">${["fail", "pass", "skip"].map((o) => `<option ${o === f.null_policy ? "selected" : ""}>${o}</option>`).join("")}</select></td>
    <td><select data-k="session_basis">${["regular", "extended", "premarket", "postmarket"].map((o) => `<option ${o === f.session_basis ? "selected" : ""}>${o}</option>`).join("")}</select></td>
    <td><button data-del="${path}">x</button></td></tr>`;
  const get = (path) => path.split(".").reduce((o, k) => o[k], draft);
  const draw = () => {
    root.innerHTML = `<h1>Scanner builder</h1>
    <div class="panel"><div class="row"><label>Load<select id="load"><option value="">new (template)</option>${strategies.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} v${s.version}</option>`).join("")}</select></label>
    <label>Id<input id="sid" value="${esc(draft.id)}"></label><label>Name<input id="sname" value="${esc(draft.name)}" size="28"></label>
    <label>Direction<select id="sdir"><option ${draft.direction === "long" ? "selected" : ""}>long</option><option ${draft.direction === "short" ? "selected" : ""}>short</option></select></label></div></div>
    <div class="panel"><h2>Strategy filters (all must pass: AND)</h2><table><thead><tr><th>id</th><th>field</th><th>op</th><th>value / formula</th><th>lookback</th><th>null</th><th>session</th><th></th></tr></thead><tbody id="gate">${draft.filters.map((f, i) => filterRow(f, `filters.${i}`)).join("")}</tbody></table><button id="addg">+ filter</button></div>
    ${draft.alert_conditions.map((c, ci) => `<div class="panel"><div class="row"><h2 style="margin:0">Alert condition (any condition may fire: OR)</h2><label>id<input data-cond="${ci}" data-ck="id" value="${esc(c.id)}" size="10"></label><label>event type<input data-cond="${ci}" data-ck="event_type" value="${esc(c.event_type)}" size="10"></label>
      <label>priority<select data-cond="${ci}" data-ck="priority">${["low", "normal", "high", "critical"].map((p) => `<option ${p === c.priority ? "selected" : ""}>${p}</option>`).join("")}</select></label><label>cooldown s<input data-cond="${ci}" data-ck="cooldown_seconds" type="number" value="${c.cooldown_seconds}" style="width:80px"></label><button data-delc="${ci}">remove condition</button></div>
      <table><thead><tr><th>id</th><th>field</th><th>op</th><th>value / formula</th><th>lookback</th><th>null</th><th>session</th><th></th></tr></thead><tbody>${c.filters.map((f, i) => filterRow(f, `alert_conditions.${ci}.filters.${i}`)).join("")}</tbody></table><button data-addf="${ci}">+ filter</button></div>`).join("")}
    <div class="row"><button id="addc">+ alert condition</button></div>
    <div class="panel"><h2>Ranking (server side) and display sort</h2><div class="form"><label>Rank by<select id="rk">${state.feats.filter((f) => f.type === "number").map((f) => `<option ${f.name === draft.ranking.field ? "selected" : ""}>${f.name}</option>`).join("")}</select></label><label>or formula<input id="rkf" value="${esc(draft.ranking.formula || "")}" placeholder="rvol * pct_change"></label><label>Order<select id="rko"><option ${draft.ranking.order === "desc" ? "selected" : ""}>desc</option><option ${draft.ranking.order === "asc" ? "selected" : ""}>asc</option></select></label>
    <label>Display sort<select id="ds">${state.feats.filter((f) => f.type === "number").map((f) => `<option ${f.name === draft.display_sort?.field ? "selected" : ""}>${f.name}</option>`).join("")}</select></label></div></div>
    <div class="panel"><h2>JSON (source of truth)</h2><textarea id="json" spellcheck="false">${esc(JSON.stringify(draft, null, 2))}</textarea><div class="row"><button id="applyj">Apply JSON to form</button></div></div>
    <div class="row"><button id="val">Validate</button><button id="prev">Preview on current data</button><button id="save" class="primary">Save (new version if it exists)</button><span id="msg"></span></div><div id="out"></div>`;
    wire();
  };
  const coerce = (f, k, v) => {
    if (k === "value") { if (f.operator === "between" || f.operator === "outside") return v.split(",").map(Number); if (["is_true", "is_false"].includes(f.operator)) return null; if (v === "true" || v === "false") return v === "true"; return v === "" ? null : Number(v); }
    if (k === "lookback") return v === "" ? null : Number(v);
    return v;
  };
  const wire = () => {
    root.querySelectorAll("tbody tr[data-path]").forEach((tr) => {
      const f = get(tr.dataset.path);
      tr.querySelectorAll("[data-k]").forEach((el) => (el.onchange = () => {
        const k = el.dataset.k; f[k] = coerce(f, k, el.value);
        if (k === "field") { if (el.value === "formula") { f.operator = "is_true"; f.formula = f.formula || "rvol > 1"; f.value = null; } else { delete f.formula; if (["is_true", "is_false"].includes(f.operator) && state.feats.find((x) => x.name === el.value)?.type !== "bool") f.operator = "gte"; } draw(); return; }
        if (k === "operator") { f.value = coerce(f, "value", String(f.value ?? "")); }
        syncJson();
      }));
    });
    root.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => { const parts = b.dataset.del.split("."); const idx = +parts.pop(); get(parts.join(".")).splice(idx, 1); draw(); }));
    root.querySelectorAll("[data-cond]").forEach((el) => (el.onchange = () => { const c = draft.alert_conditions[+el.dataset.cond]; const k = el.dataset.ck; c[k] = k === "cooldown_seconds" ? +el.value : el.value; syncJson(); }));
    root.querySelectorAll("[data-delc]").forEach((b) => (b.onclick = () => { draft.alert_conditions.splice(+b.dataset.delc, 1); draw(); }));
    root.querySelectorAll("[data-addf]").forEach((b) => (b.onclick = () => { draft.alert_conditions[+b.dataset.addf].filters.push(newFilter()); draw(); }));
    $("#addg", root).onclick = () => { draft.filters.push(newFilter()); draw(); };
    $("#addc", root).onclick = () => { draft.alert_conditions.push({ id: "cond-" + (draft.alert_conditions.length + 1), name: "condition", event_type: "signal", priority: "normal", cooldown_seconds: 900, filters: [newFilter()] }); draw(); };
    $("#sid", root).onchange = (e) => { draft.id = e.target.value; syncJson(); };
    $("#sname", root).onchange = (e) => { draft.name = e.target.value; syncJson(); };
    $("#sdir", root).onchange = (e) => { draft.direction = e.target.value; syncJson(); };
    $("#rk", root).onchange = (e) => { draft.ranking.field = e.target.value; syncJson(); };
    $("#rkf", root).onchange = (e) => { if (e.target.value) draft.ranking.formula = e.target.value; else delete draft.ranking.formula; syncJson(); };
    $("#rko", root).onchange = (e) => { draft.ranking.order = e.target.value; syncJson(); };
    $("#ds", root).onchange = (e) => { draft.display_sort = { field: e.target.value, order: "desc" }; syncJson(); };
    $("#load", root).onchange = async (e) => { if (!e.target.value) draft = TEMPLATE(); else draft = await api(`/api/strategies/${e.target.value}`); delete draft.config_hash; draw(); };
    $("#applyj", root).onclick = () => { try { draft = JSON.parse($("#json", root).value); draw(); } catch (e) { msg(e.message, true); } };
    $("#val", root).onclick = () => run("validate");
    $("#prev", root).onclick = () => run("preview");
    $("#save", root).onclick = () => run("save");
  };
  const newFilter = () => ({ id: "f" + Math.random().toString(36).slice(2, 6), name: "filter", field: "last", operator: "gte", value: 1, null_policy: "fail", session_basis: "regular", enabled: true, version: 1 });
  const syncJson = () => { $("#json", root).value = JSON.stringify(draft, null, 2); };
  const msg = (t, bad) => { $("#msg", root).innerHTML = `<span class="${bad ? "err" : "ok"}">${esc(t)}</span>`; };
  const cleaned = () => JSON.parse(JSON.stringify(draft, (k, v) => (v === null && ["lookback", "formula", "unit"].includes(k) ? undefined : v)));
  const run = async (kind) => {
    const out = $("#out", root); out.innerHTML = "";
    try {
      const body = cleaned();
      if (kind === "validate") {
        const r = await api(`/api/strategies/${encodeURIComponent(body.id)}/validate`, { method: "POST", body });
        if (r.valid) { msg("valid"); out.innerHTML = r.warnings.length ? `<ul class="plain">${r.warnings.map((w) => `<li class="muted">${esc(w)}</li>`).join("")}</ul>` : ""; } else { msg("invalid", true); out.innerHTML = `<ul class="plain">${r.errors.map((e) => `<li class="err">${esc(e.path)}: ${esc(e.message)}</li>`).join("")}</ul>`; }
      } else if (kind === "preview") {
        const r = await api(`/api/strategies/${encodeURIComponent(body.id)}/preview`, { method: "POST", body });
        const t = r.top_list; msg(`preview: ${t.qualified} of ${t.evaluated} qualify`);
        out.innerHTML = `<div class="panel"><p class="muted">${esc(r.note)}</p><table><thead><tr><th>#</th><th>Symbol</th><th class="num">Score</th><th class="num">Last</th><th class="num">% chg</th><th class="num">RVOL</th></tr></thead><tbody>${t.rows.map((x) => `<tr><td>${x.rank}</td><td>${esc(x.symbol)}</td><td class="num">${fmt(x.score, 3)}</td><td class="num">${fmt(x.last)}</td><td class="num">${fmt(x.pct_change)}</td><td class="num">${fmt(x.rvol)}</td></tr>`).join("")}</tbody></table></div>`;
      } else {
        const exists = strategies.some((s) => s.id === body.id);
        const r = await api(exists ? `/api/strategies/${encodeURIComponent(body.id)}` : "/api/strategies", { method: exists ? "PUT" : "POST", body });
        msg(`saved ${r.id} v${r.version} (hash ${r.config_hash})`);
      }
    } catch (e) { msg(e.message.slice(0, 600), true); }
  };
  draw();
};

// --------------------------------------------------------------- backtest runner & reports
views.backtest = async (root) => {
  const strategies = await api("/api/strategies");
  const cfg = await api("/api/config/public");
  root.innerHTML = `<h1>Backtest runner <span class="badge paper">BACKTEST</span> <span class="badge paper">SIMULATED</span></h1>
  <p class="muted">Historical simulation on 1-minute OHLC bars, regular session only. Not a prediction.</p>
  <div class="panel"><div class="form">
  <label>Strategy<select id="s">${strategies.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} v${s.version}</option>`).join("")}</select></label>
  <label>Start<input id="sd" type="date" value="2026-09-08"></label><label>End<input id="ed" type="date" value="2026-09-28"></label>
  <label>Symbols (blank = universe)<input id="sy" placeholder="${esc((cfg.universe || []).join(","))}"></label>
  <label>Direction<select id="dir"><option value="strategy">per strategy</option><option>long</option><option>short</option></select></label>
  <label>Entry price<select id="epm"><option value="next_open">next bar open</option><option value="alert_close">alert bar close (optimistic)</option></select></label>
  <label>Entry window start (ET)<input id="es" value="09:35"></label><label>Entry window end (ET)<input id="ee" value="15:30"></label>
  <label>Profit target %<input id="pt" type="number" step="0.1" value="1.5"></label><label>Stop loss %<input id="sl" type="number" step="0.1" value="0.75"></label>
  <label>Trailing stop % (opt)<input id="tr" type="number" step="0.1"></label><label>Exit after N min (opt)<input id="ta" type="number" value="90"></label>
  <label>Hold days (0 = same-day close)<input id="hd" type="number" value="0" min="0"></label><label>Multi-day exit<select id="mde"><option value="close">close</option><option value="open">open</option></select></label>
  <label>Intrabar policy<select id="ip"><option value="stop_first">stop first (conservative)</option><option value="target_first">target first</option><option value="reject_ambiguous">reject ambiguous</option></select></label>
  <label>Sizing<select id="sz"><option value="fixed_dollars">fixed dollars</option><option value="fixed_shares">fixed shares</option><option value="percent_equity">% of equity</option><option value="risk_percent">% equity risk</option></select></label>
  <label>Size value<input id="szv" type="number" value="10000"></label><label>Starting equity $<input id="eq" type="number" value="100000"></label>
  <label>Max concurrent<input id="mc" type="number" value="5"></label><label>Daily trade cap<input id="dc" type="number" value="20"></label><label>Daily loss limit $ (opt)<input id="dl" type="number"></label>
  <label>Commission $/share<input id="cm" type="number" step="0.001" value="0.005"></label><label>Spread bps (fill assumption)<input id="sp" type="number" step="0.5" value="5"></label><label>Slippage bps<input id="sg" type="number" step="0.5" value="2"></label>
  <label>Synthesize quotes at spread bps (opt)<input id="qs" type="number" step="0.5" placeholder="off"></label>
  </div><div class="row"><button id="go" class="primary">Run backtest</button><span id="msg"></span></div></div>`;
  $("#go", root).onclick = async () => {
    const v = (id) => $("#" + id, root).value;
    const num = (id) => (v(id) === "" ? null : +v(id));
    const exits = { hold_days: +v("hd"), multi_day_exit: v("mde") };
    if (num("pt") != null) exits.profit_target = { type: "percent", value: num("pt") };
    if (num("sl") != null) exits.stop_loss = { type: "percent", value: num("sl") };
    if (num("tr") != null) exits.trailing_stop = { type: "percent", value: num("tr") };
    if (num("ta") != null) exits.time_after_entry_minutes = num("ta");
    const z = v("sz"); const sizing = { mode: z };
    sizing[{ fixed_dollars: "dollars", fixed_shares: "shares", percent_equity: "percent", risk_percent: "risk_percent" }[z]] = +v("szv");
    const config = { start_date: v("sd"), end_date: v("ed"), direction: v("dir"), entry_price_model: v("epm"), entry_start: v("es"), entry_end: v("ee"), exits, intrabar_policy: v("ip"), sizing, starting_equity: +v("eq"), max_concurrent_positions: +v("mc"), max_trades_per_day: +v("dc"), costs: { commission_per_share: +v("cm"), spread_bps: +v("sp"), slippage_bps: +v("sg") } };
    if (v("sy").trim()) config.symbols = v("sy").split(",").map((x) => x.trim()).filter(Boolean);
    if (num("dl") != null) config.daily_loss_limit = num("dl");
    if (num("qs") != null) config.assumed_spread_bps = num("qs");
    $("#msg", root).textContent = "running...";
    try {
      const r = await api("/api/backtests", { method: "POST", body: { strategy_id: v("s"), config, wait: true } });
      location.hash = `#/backtest/${r.run_id}`;
    } catch (e) { $("#msg", root).innerHTML = `<span class="err">${esc(e.message.slice(0, 500))}</span>`; }
  };
};

views.backtests = async (root) => {
  const runs = await api("/api/backtests");
  root.innerHTML = `<h1>Backtest reports</h1><div class="panel">${runs.length ? `<table><thead><tr><th>Run</th><th>Strategy</th><th>Range</th><th>Status</th><th>Created</th></tr></thead><tbody>${runs.map((r) => `<tr class="clickable" data-r="${esc(r.run_id)}"><td class="mono">${esc(r.run_id)}</td><td>${esc(r.strategy_id)} v${r.strategy_version}</td><td>${esc(r.date_start)} to ${esc(r.date_end)}</td><td>${esc(r.status)}</td><td>${esc(dt(r.created_at))}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">No runs yet. <a href="#/backtest">Run a backtest</a>.</p>'}</div>`;
  root.querySelectorAll("tr[data-r]").forEach((tr) => (tr.onclick = () => (location.hash = `#/backtest/${tr.dataset.r}`)));
};

views.report = async (root, id) => {
  const run = await api(`/api/backtests/${id}`).catch((e) => ({ error: e.message }));
  if (run.error) { root.innerHTML = `<p class="err">${esc(run.error)}</p>`; return; }
  if (run.status !== "completed") { root.innerHTML = `<h1>Run ${esc(id)}</h1><p>${esc(run.status)} ${esc(run.error || "")}</p>`; if (run.status === "running") setTimeout(() => views.report(root, id), 1500); return; }
  const r = run.report, m = r.metrics;
  const trades = (await api(`/api/backtests/${id}/trades?limit=500`)).trades;
  const card = (k, v, sub = "") => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="muted">${sub}</div></div>`;
  root.innerHTML = `<h1>Backtest ${esc(id)} <span class="badge paper">BACKTEST</span> <span class="badge paper">SIMULATED</span></h1>
  <div class="banner" style="margin-bottom:12px">${esc(r.label)}</div>
  <p class="muted">${esc(r.strategy.name)} v${r.strategy.version} (${esc(r.strategy.config_hash)}) | data: ${esc(r.data.provider)}/${esc(r.data.feed)} ${esc(r.data.adjustment)} | ${esc(r.data.first_trading_day)} to ${esc(r.data.last_trading_day)} (${r.data.trading_days_in_range} sessions, ${r.data.overall_coverage_pct}% bar coverage) | intrabar policy: <b>${esc(r.intrabar_policy)}</b></p>
  <div class="grid">${card("Trades", m.total_trades, `${m.winners} W / ${m.losers} L`)}${card("Win rate", m.win_rate == null ? "-" : fmt(m.win_rate * 100, 1) + "%")}${card("Net P&L", `<span class="${cls(m.net_pnl)}">${money(m.net_pnl)}</span>`, `gross ${money(m.gross_pnl)}`)}
  ${card("Profit factor", m.profit_factor == null ? "-" : fmt(m.profit_factor), esc(m.profit_factor_note || ""))}${card("Expectancy/trade", money(m.expectancy))}${card("Avg win / loss", `${money(m.average_winner)} / ${money(m.average_loser)}`)}
  ${card("Max drawdown", money(m.max_drawdown), fmt(m.max_drawdown_pct) + "%")}${card("Buying power peak", money(m.buying_power_peak))}${card("Costs total", money(m.total_costs), `spread ${money(m.total_spread_cost)} | slip ${money(m.total_slippage_cost)} | comm ${money(m.total_commissions_fees)}`)}
  ${card("Streaks", `${m.max_consecutive_wins}W / ${m.max_consecutive_losses}L`, "max consecutive")}${card("Trades/day", fmt(m.avg_trades_per_day))}${card("Signals", m.signals_detected, `ambiguous excluded ${m.ambiguous_bars_excluded}`)}</div>
  <div class="two"><div class="panel"><h2>Equity curve</h2>${lineChart(r.equity_curve.map((p) => ({ x: p.ts, y: p.equity })), { baseline: m.starting_equity, label: "equity curve" })}</div>
  <div class="panel"><h2>Drawdown ($)</h2>${lineChart(r.drawdown_series.map((p) => ({ x: p.ts, y: -p.drawdown })), { color: "var(--bad)", baseline: 0, label: "drawdown" })}</div></div>
  <div class="panel"><h2>Daily P&L</h2>${barChart(r.daily_pnl.map((d) => ({ x: d.date, y: d.net_pnl })))}</div>
  <div class="two"><div class="panel"><h2>Exit reasons</h2><table><tbody>${Object.entries(r.exit_reasons).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v}</td></tr>`).join("") || "<tr><td class='muted'>none</td></tr>"}</tbody></table></div>
  <div class="panel"><h2>Holding time</h2><p class="muted">mean ${fmt(r.holding_time.mean_minutes, 1)} min, median ${fmt(r.holding_time.median_minutes, 1)} min</p><table><tbody>${HOLD_ORDER.filter((k) => k in r.holding_time.distribution).map((k) => `<tr><td>${esc(k)}</td><td class="num">${r.holding_time.distribution[k]}</td></tr>`).join("")}</tbody></table></div>
  <div class="panel"><h2>Skipped signals</h2><table><tbody>${Object.entries(r.skipped_signals).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v}</td></tr>`).join("") || "<tr><td class='muted'>none</td></tr>"}</tbody></table></div></div>
  <div class="panel"><h2>Trades (${trades.length} shown)</h2><table><thead><tr><th>#</th><th>Symbol</th><th>Dir</th><th>Entry</th><th class="num">Entry px</th><th>Exit</th><th class="num">Exit px</th><th class="num">Qty</th><th class="num">Gross</th><th class="num">Costs</th><th class="num">Net</th><th>Reason</th><th class="num">Min</th></tr></thead><tbody>${trades.map((t) => `<tr><td>${t.trade_no}</td><td>${esc(t.symbol)}</td><td>${esc(t.direction)}</td><td>${esc(dt(t.entry_ts))}</td><td class="num">${fmt(t.entry_price, 3)}</td><td>${esc(dt(t.exit_ts))}</td><td class="num">${fmt(t.exit_price, 3)}</td><td class="num">${fmt(t.quantity, 0)}</td><td class="num ${cls(t.gross_pnl)}">${money(t.gross_pnl)}</td><td class="num">${money(t.costs)}</td><td class="num ${cls(t.net_pnl)}">${money(t.net_pnl)}</td><td>${esc(t.exit_reason)}</td><td class="num">${fmt(t.holding_minutes, 0)}</td></tr>`).join("")}</tbody></table></div>
  <div class="two"><div class="panel"><h2>Filter attribution (avg observed at entry)</h2><table><thead><tr><th>Filter</th><th>Field</th><th class="num">Winners</th><th class="num">Losers</th></tr></thead><tbody>${Object.entries(r.attribution.by_filter).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v.field)} ${esc(v.operator)} ${esc(JSON.stringify(v.threshold))}</td><td class="num">${fmt(v.avg_observed_winners, 3)}</td><td class="num">${fmt(v.avg_observed_losers, 3)}</td></tr>`).join("")}</tbody></table></div>
  <div class="panel"><h2>Data coverage</h2><table><thead><tr><th>Symbol</th><th class="num">Bars</th><th class="num">Expected</th><th class="num">Coverage</th></tr></thead><tbody>${Object.entries(r.data.per_symbol).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v.bars}</td><td class="num">${v.expected}</td><td class="num">${v.coverage_pct}%</td></tr>`).join("")}</tbody></table></div></div>
  <div class="panel"><h2>Assumptions</h2><ul class="plain">${r.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>`;
};

// ------------------------------------------------------------------------- settings
views.settings = async (root) => {
  const [cfg, prefs, status, symbols, deliveries, audit] = await Promise.all([api("/api/config/public"), api("/api/preferences"), api("/api/status"), api("/api/symbols"), api("/api/deliveries?limit=30"), api("/api/audit?limit=30")]);
  root.innerHTML = `<h1>Settings</h1>
  <div class="two"><div class="panel"><h2>Mode and provider</h2><table><tbody><tr><td>Trading mode</td><td><span class="badge paper">${esc(cfg.trading_mode.toUpperCase())}</span> (live trading not supported)</td></tr><tr><td>Data provider</td><td>${esc(cfg.data_provider)}</td></tr><tr><td>Feed</td><td>${esc(status.provider.feed)}</td></tr><tr><td>Connected</td><td>${status.provider.connected}</td></tr><tr><td>Reconnects / dropped / duplicates / gaps</td><td>${status.provider.reconnects} / ${status.provider.dropped_events} / ${status.provider.duplicate_events} / ${status.provider.gaps}</td></tr><tr><td>Last event</td><td>${esc(dt(status.last_event_time))}</td></tr><tr><td>Premarket / postmarket</td><td>${cfg.enable_premarket} / ${cfg.enable_postmarket}</td></tr><tr><td>Stale feed threshold</td><td>${cfg.stale_feed_seconds}s</td></tr><tr><td>Top List refresh / rows</td><td>${cfg.top_list_refresh_seconds}s / ${cfg.top_list_max_rows}</td></tr></tbody></table><p class="muted">Configuration is read from the environment / .env. Secrets are never shown.</p></div>
  <div class="panel"><h2>Notification preferences</h2><div class="form">
    <label><span>Browser</span><select id="pb"><option value="true" ${prefs.browser_enabled ? "selected" : ""}>on</option><option value="false" ${!prefs.browser_enabled ? "selected" : ""}>off</option></select></label>
    <label>Min priority<select id="pm">${["low", "normal", "high", "critical"].map((p) => `<option ${p === prefs.min_priority ? "selected" : ""}>${p}</option>`).join("")}</select></label>
    <label>Quiet hours<select id="qe"><option value="false" ${!prefs.quiet_hours.enabled ? "selected" : ""}>off</option><option value="true" ${prefs.quiet_hours.enabled ? "selected" : ""}>on</option></select></label>
    <label>Quiet start (ET)<input id="qs" value="${esc(prefs.quiet_hours.start)}"></label><label>Quiet end (ET)<input id="qn" value="${esc(prefs.quiet_hours.end)}"></label>
    <label>Muted symbols<input id="mu" value="${esc(prefs.muted_symbols.join(","))}"></label></div>
    <div class="row"><button id="sp" class="primary">Save preferences</button><button id="tn">Send test notification</button><span id="msg" class="muted"></span></div>
    <p class="muted">Email and webhook channels exist as disabled abstractions (EMAIL_ENABLED / WEBHOOK_ENABLED). They are non-transactional.</p></div></div>
  <div class="panel"><h2>Universe</h2><table><thead><tr><th>Symbol</th><th>Status</th><th>Active</th></tr></thead><tbody>${symbols.map((s) => `<tr><td>${esc(s.symbol)}</td><td>${esc(s.status)}</td><td><input type="checkbox" data-sym="${esc(s.symbol)}" ${s.active ? "checked" : ""}></td></tr>`).join("")}</tbody></table></div>
  <div class="two"><div class="panel"><h2>Recent deliveries</h2>${deliveries.length ? `<table><tbody>${deliveries.map((d) => `<tr><td>${esc(d.channel)}</td><td>${esc(d.status)}</td><td>${esc(d.reason || d.error)}</td><td class="mono">${esc(d.event_id)}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">none</p>'}</div>
  <div class="panel"><h2>Audit log</h2>${audit.length ? `<table><tbody>${audit.map((a) => `<tr><td class="mono">${esc(hhmm(a.ts))}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td class="mono">${esc(a.entity_id || "")}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">none</p>'}</div></div>`;
  root.querySelectorAll("input[data-sym]").forEach((c) => (c.onchange = () => api(`/api/symbols/${c.dataset.sym}/active`, { method: "POST", body: { active: c.checked } })));
  $("#sp", root).onclick = async () => {
    const body = { ...prefs, browser_enabled: $("#pb", root).value === "true", min_priority: $("#pm", root).value, muted_symbols: $("#mu", root).value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean), quiet_hours: { ...prefs.quiet_hours, enabled: $("#qe", root).value === "true", start: $("#qs", root).value, end: $("#qn", root).value } };
    try { await api("/api/preferences", { method: "PUT", body }); $("#msg", root).innerHTML = '<span class="ok">saved</span>'; } catch (e) { $("#msg", root).innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
  $("#tn", root).onclick = async () => { const r = await api("/api/notifications/test", { method: "POST" }); $("#msg", root).textContent = r.sent.map((d) => `${d.channel}: ${d.status}${d.reason ? " (" + d.reason + ")" : ""}`).join(", "); };
};

// ------------------------------------------------------------------------- router
async function route() {
  clearTimers();
  const main = $("#view");
  if (main._cleanup) { main._cleanup(); main._cleanup = null; }
  const h = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [name, ...rest] = h.split("?")[0].split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#/" + (name === "alert" ? "alerts" : name === "backtest" && rest.length ? "backtests" : name)));
  try {
    if (name === "alert") await views.alert(main, rest.join("/"));
    else if (name === "backtest" && rest.length) await views.report(main, rest[0]);
    else await (views[name] || views.dashboard)(main);
  } catch (e) { main.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
window.addEventListener("hashchange", route);
connectWs();
refreshStatus();
setInterval(refreshStatus, 4000);
route();
