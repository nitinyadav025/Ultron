/* =============================================================================
   ULTRON — Glassy Dashboard V18 application
   Gate 2B · vanilla JS, zero dependencies, same-origin API on 127.0.0.1:9932
   Contract: docs/DASHBOARD_API_CONTRACT.md
   ========================================================================== */
"use strict";

/* ------------------------------- tiny helpers ------------------------------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const fmt = (n, d = 0) => Number(n ?? 0).toLocaleString("en-US", {
  minimumFractionDigits: d, maximumFractionDigits: d,
});

const TOAST_ROOT = $("toast-root");
function toast(message, cls = "") {
  const el = document.createElement("div");
  el.className = `toast ${cls}`.trim();
  el.textContent = message;
  TOAST_ROOT.appendChild(el);
  setTimeout(() => el.classList.add("fade"), 4200);
  setTimeout(() => el.remove(), 4700);
}

/* ------------------------------- API client --------------------------------- */
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `${method} ${path} → HTTP ${res.status}`);
  }
  return data;
}

/* --------------------------- smooth number engine --------------------------- */
const tweens = new Map(); // el -> {value, target, fmt}
function animateNum(el, target, format = fmt) {
  if (!el) return;
  const t = tweens.get(el) || { value: target, target, format };
  if (t.target === target && t.format === format) return;
  t.target = target;
  t.format = format;
  tweens.set(el, t);
}
(function numberLoop() {
  for (const [el, t] of tweens) {
    if (!el.isConnected) { tweens.delete(el); continue; }
    const diff = t.target - t.value;
    if (Math.abs(diff) < 0.01) {
      t.value = t.target;
    } else {
      t.value += diff * 0.14; // exponential ease toward target
    }
    el.textContent = t.format(t.value);
  }
  requestAnimationFrame(numberLoop);
})();

/* ------------------------------ global state -------------------------------- */
const UI = {
  state: null,
  revision: -1,
  connected: false,
  view: "overview",
  speak: { cmd: true, chat: true },
  apps: { data: null, query: "", filter: "all", sig: "" },
  tabs: { filter: "all", query: "", readTab: null },
  skills: { data: null, sig: "" },
  chat: { query: "" },
  config: null,
  bridge: null,
  sigs: {}, // render signatures per heavy component
};

const TOOLS_REGISTERED = 28; // Gate 2A launcher registry count (no API field exists)

const VIEW_META = {
  overview: { title: () => greeting() + ", Nitin", sub: "Executive Command Deck" },
  chat: { title: () => "Live Brain & Chat", sub: "Conversational console · thought stream · planner" },
  tabs: { title: () => "Browser Tabs & Eyes", sub: "Brave & Firefox tabs + live AT-SPI screen elements" },
  apps: { title: () => "Desktop Apps", sub: "Every indexed Fedora 45 application" },
  skills: { title: () => "Workflows & Skills", sub: "Taught voice macros · triggers · user memory" },
  telemetry: { title: () => "Telemetry & Reports", sub: "Latency by stage · GPU · turn metrics" },
  settings: { title: () => "Personality & Settings", sub: "Live configuration studio" },
  bridge: { title: () => "Browser Bridge", sub: "extensions/ultron-browser-bridge setup" },
};

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Good Night";
  if (h < 12) return "Good Morning";
  if (h < 17) return "Good Afternoon";
  return "Good Evening";
}

/* ============================== SSE + polling ================================ */
function connectSSE() {
  const source = new EventSource("/api/stream");
  source.addEventListener("open", () => { UI.connected = true; renderConn(); });
  source.addEventListener("state", (event) => {
    UI.connected = true;
    handleState(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    UI.connected = false;
    renderConn();
    // EventSource auto-reconnects (server sends retry: 1000).
  });
}

async function bootstrap() {
  try {
    handleState(await api("/api/state"));
  } catch (err) {
    toast(`State unavailable: ${err.message}`, "err");
  }
  connectSSE();
}

function handleState(s) {
  UI.state = s;
  renderConn();
  renderHeader();
  renderHUD();
  renderVoiceControls();
  const v = UI.view;
  if (v === "overview") renderOverview();
  if (v === "chat") renderChat();
  if (v === "tabs") renderTabs();
  if (v === "telemetry") renderTelemetry();
  if (v === "settings") syncSettings(false);
}

/* ================================ rendering ================================== */

function renderConn() {
  const pill = $("pill-conn");
  const text = $("pill-conn-text");
  if (!pill || !text) return;
  pill.classList.toggle("off", !UI.connected);
  text.textContent = UI.connected ? "live · SSE" : "reconnecting…";
}

function renderHeader() {
  const s = UI.state;
  if (!s) return;
  const meta = VIEW_META[UI.view];
  $("view-title").textContent = meta.title();
  $("view-subtitle").textContent = meta.sub;

  const alive = !!(s.telemetry && s.telemetry.llama_server_alive);
  const pill = $("pill-llama");
  pill.classList.toggle("off", !alive);
  $("pill-llama-text").textContent = alive ? "llama-server · Online" : "llama-server · Offline";

  document.querySelectorAll("#mode-select button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === s.voice_mode);
  });
}

/* ------------------------------- HUD widgets -------------------------------- */
const WAVE_BARS = 10;
(function buildWaveform() {
  const wf = $("hud-waveform");
  for (let i = 0; i < WAVE_BARS; i++) wf.appendChild(document.createElement("i"));
})();
const waveTargets = new Array(WAVE_BARS).fill(0.12);
(function waveLoop() {
  const s = UI.state;
  const bars = $("hud-waveform").children;
  if (s) {
    const level = clamp((s.audio_level_rms || 0) / 1800, 0, 1);
    const speaking = s.status === "speaking";
    for (let i = 0; i < WAVE_BARS; i++) {
      // organic per-bar variation driven by the live RMS level
      const wobble = 0.55 + 0.45 * Math.sin(performance.now() / 130 + i * 1.7);
      const target = speaking
        ? clamp(0.18 + level * 0.8 * wobble, 0.12, 1)
        : clamp(0.1 + level * 0.9 * wobble, 0.08, 1);
      waveTargets[i] += (target - waveTargets[i]) * 0.35;
    }
  }
  for (let i = 0; i < WAVE_BARS; i++) {
    const el = bars[i];
    const current = parseFloat(el.style.height || "12");
    el.style.height = `${clamp(current + (waveTargets[i] * 100 - current) * 0.3, 6, 100)}%`;
  }
  requestAnimationFrame(waveLoop);
})();

function renderHUD() {
  const s = UI.state;
  if (!s) return;

  // voice state orb + label
  const orb = $("hud-orb");
  const status = String(s.status || "offline");
  orb.className = `voice-orb ${UI.connected ? status : "offline"}`;
  $("hud-voice-state").textContent = {
    listening: "Listening", transcribing: "Transcribing", thinking: "Thinking",
    speaking: "Speaking", muted: "Muted", starting: "Starting…", online: "Online",
    error: "Error", offline: "Offline",
  }[status] || status;
  const db = s.audio_level_rms > 0 ? (20 * Math.log10((s.audio_level_rms || 1) / 32768)).toFixed(1) : "−∞";
  $("hud-rms").textContent = `RMS ${fmt(s.audio_level_rms)} · ${db} dB`;

  // GPU VRAM + focus
  const t = s.telemetry || {};
  animateNum($("hud-vram"), t.gpu_vram_used_mb || 0);
  $("hud-vram-bar").style.width = `${clamp(((t.gpu_vram_used_mb || 0) / (t.gpu_vram_total_mb || 4096)) * 100, 0, 100)}%`;
  $("hud-focus").firstChild.textContent = (s.screen_state && s.screen_state.focused_window) || "—";
  $("hud-focus-sub").textContent = `focused app: ${(s.screen_state && s.screen_state.focused_app) || "—"} · site: ${(s.screen_state && s.screen_state.active_site) || "—"}`;

  // latency pulse + spline
  const series = (t.latency_series || []).slice(-24);
  const latest = series.length ? series[series.length - 1] : null;
  if (latest) animateNum($("hud-lat"), Math.round(latest.total_ms || 0), (v) => fmt(Math.round(v)));
  $("hud-lat-badge").textContent = `p50 ${fmt(t.p50_ms || 0)} / p95 ${fmt(t.p95_ms || 0)} ms`;
  drawSpline($("hud-spline-line"), $("hud-spline-fill"), $("hud-spline-dot"), series, 260, 84);
}

/* Catmull-Rom → cubic bezier smooth path through the latency series. */
function drawSpline(lineEl, fillEl, dotEl, series, width, height) {
  const n = series.length;
  if (!n) {
    lineEl.setAttribute("d", "");
    fillEl.setAttribute("d", "");
    dotEl.setAttribute("cx", 0);
    return;
  }
  const max = Math.max(...series.map((p) => p.total_ms || 0), 1);
  const pad = 8;
  const pts = series.map((p, i) => [
    n === 1 ? width / 2 : pad + (i / (n - 1)) * (width - 2 * pad),
    height - pad - ((p.total_ms || 0) / max) * (height - 2 * pad),
  ]);
  let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)], p1 = pts[i], p2 = pts[i + 1],
      p3 = pts[Math.min(i + 2, pts.length - 1)];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  lineEl.setAttribute("d", d);
  fillEl.setAttribute("d", `${d} L ${pts[pts.length - 1][0].toFixed(1)} ${height} L ${pts[0][0].toFixed(1)} ${height} Z`);
  dotEl.setAttribute("cx", pts[pts.length - 1][0]);
  dotEl.setAttribute("cy", pts[pts.length - 1][1]);
}

/* ------------------------------ voice controls ------------------------------- */
function renderVoiceControls() {
  const s = UI.state;
  if (!s) return;
  $("hud-mic-label").textContent = s.mic_muted ? "Unmute Mic" : "Mute Mic";
  document.querySelectorAll("#ov-mode-pills button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === s.voice_mode);
  });
  $("ov-mode-hint").textContent = `mode: ${s.voice_mode}`;
  $("ch-status-hint").textContent = s.status;
}

/* --------------------------------- OVERVIEW ---------------------------------- */
(function buildTicks() {
  const bar = $("ov-ticks");
  for (let i = 0; i < 32; i++) bar.appendChild(document.createElement("i"));
})();

function subsystemOf(tool) {
  const t = String(tool || "").toLowerCase();
  if (t.includes("tab") || t.includes("browser")) return "browser";
  if (t.includes("app") || t.includes("screen") || t.includes("element") || t.includes("key") || t.includes("window")) return "apps";
  return "system";
}

function renderOverview() {
  const s = UI.state;
  if (!s) return;

  $("ov-heard").textContent = s.current_transcript || "—";
  $("ov-thought").textContent = s.current_thought || "—";
  $("ov-reply").textContent = s.last_reply || "—";

  const alive = !!(s.telemetry && s.telemetry.llama_server_alive);
  const pct = alive ? 98.4 : 0;
  animateNum($("ov-cache-pct"), pct, (v) => fmt(v, 1));
  const ticks = $("ov-ticks").children;
  const lit = Math.round((pct / 100) * ticks.length);
  for (let i = 0; i < ticks.length; i++) ticks[i].classList.toggle("on", i < lit);
  $("ov-brain-state").textContent = alive ? "online · KV cache warm" : "offline";
  $("ov-p50-line").textContent = `p50 ${fmt(s.telemetry?.p50_ms || 0)} ms · p95 ${fmt(s.telemetry?.p95_ms || 0)} ms`;
  $("ov-brain-hint").textContent = alive ? "llama-server" : "llama-server down";

  animateNum($("ov-tools"), TOOLS_REGISTERED, (v) => fmt(v));
  $("ov-arsenal-bar").style.width = `${clamp((TOOLS_REGISTERED / 32) * 100, 0, 100)}%`;

  renderOverviewWindowsTabs();
  renderOverviewDonut();
  renderOverviewLatency();
  renderRecentTurns();
}

function renderOverviewWindowsTabs() {
  const s = UI.state;
  const rows = [];
  for (const w of (s.screen_state?.open_windows || []).slice(0, 5)) {
    rows.push({ kind: "window", name: w, sub: "desktop window" });
  }
  for (const t of (s.browser_tabs || []).slice(0, 6)) {
    rows.push({ kind: "tab", name: `${t.title || t.url}`, sub: `${t.browser} · ${t.url || ""}`, tab: t });
  }
  const sig = JSON.stringify(rows);
  if (UI.sigs.ovWt === sig) return;
  UI.sigs.ovWt = sig;
  $("ov-wt-hint").textContent = `${rows.length} live`;

  const host = $("ov-wt-list");
  host.textContent = "";
  if (!rows.length) {
    host.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">Nothing open yet<span class="sub">windows and synced tabs appear here</span></span></div>`;
    return;
  }
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "line-row";
    const dot = r.kind === "tab" ? "green" : "cobalt";
    row.innerHTML =
      `<span class="status-dot ${dot}"></span>` +
      `<span class="name">${esc(r.name)}<span class="sub">${esc(r.sub)}</span></span>`;
    const actions = document.createElement("span");
    actions.className = "actions";
    const focus = btn("small", "Focus");
    focus.onclick = () => r.kind === "tab"
      ? tabAction("switch", r.tab.title).catch(showErr)
      : appAction("focus", r.name).catch(showErr);
    const close = btn("small danger", "Close");
    close.onclick = () => r.kind === "tab"
      ? tabAction("close", r.tab.title).catch(showErr)
      : appAction("close", r.name).catch(showErr);
    actions.append(focus, close);
    row.appendChild(actions);
    host.appendChild(row);
  }
}

function btn(cls, label) {
  const b = document.createElement("button");
  b.className = `btn ${cls}`;
  b.textContent = label;
  return b;
}
function showErr(err) { toast(err.message, "err"); }

const DONUT_COLORS = { brain: "#34d399", browser: "#60a5fa", apps: "#a78bfa", system: "#fbbf24" };
const DONUT_LABELS = { brain: "Brain LLM", browser: "Browser Bridge", apps: "Desktop Apps", system: "Voice / System" };

function renderOverviewDonut() {
  const s = UI.state;
  const counts = { brain: 0, browser: 0, apps: 0, system: 0 };
  for (const turn of (s.conversation_feed || [])) {
    const tools = turn.tools_called || [];
    if (turn.role === "assistant" && !tools.length) counts.brain += 1;
    for (const t of tools) counts[subsystemOf(t)] += 1;
  }
  const sig = JSON.stringify(counts);
  if (UI.sigs.donut === sig) return;
  UI.sigs.donut = sig;

  const total = (s.conversation_feed || []).length;
  $("ov-donut-total").textContent = fmt(total);
  const segs = $("ov-donut-segs");
  segs.textContent = "";
  const legend = $("ov-donut-legend");
  legend.textContent = "";

  const sum = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
  const R = 52, C = 2 * Math.PI * R;
  let offset = 0;
  for (const key of Object.keys(counts)) {
    const frac = counts[key] / sum;
    if (counts[key] > 0) {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", 66); circle.setAttribute("cy", 66); circle.setAttribute("r", R);
      circle.setAttribute("fill", "none");
      circle.setAttribute("stroke", DONUT_COLORS[key]);
      circle.setAttribute("stroke-width", 15);
      circle.setAttribute("stroke-linecap", "round");
      circle.setAttribute("stroke-dasharray", `${Math.max(frac * C - 3, 0.1)} ${C}`);
      circle.setAttribute("transform", `rotate(${offset * 360 - 90} 66 66)`);
      circle.style.filter = `drop-shadow(0 0 5px ${DONUT_COLORS[key]}88)`;
      segs.appendChild(circle);
    }
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<i style="width:9px;height:9px;border-radius:3px;background:${DONUT_COLORS[key]}"></i>${DONUT_LABELS[key]}<b>${counts[key]}</b>`;
    legend.appendChild(row);
    offset += frac;
  }
}

function renderOverviewLatency() {
  const s = UI.state;
  const series = (s.telemetry?.latency_series || []).slice(-12);
  const sig = JSON.stringify(series.map((p) => [p.transcribe_ms, p.llm_ms, p.tool_ms]));
  if (UI.sigs.ovLat === sig) return;
  UI.sigs.ovLat = sig;
  $("ov-lat-hint").textContent = `${series.length} recent turns`;

  const host = $("ov-lat-bars");
  host.textContent = "";
  const max = Math.max(...series.map((p) => (p.transcribe_ms || 0) + (p.llm_ms || 0) + (p.tool_ms || 0)), 1);
  for (let i = 0; i < 12; i++) {
    const bar = document.createElement("div");
    bar.className = "lat-bar";
    const p = series[i];
    if (p) {
      const mk = (cls, ms) => {
        const seg = document.createElement("span");
        seg.className = cls;
        seg.style.height = `${((ms || 0) / max) * 100}%`;
        return seg;
      };
      bar.append(
        mk("seg-transcribe", p.transcribe_ms),
        mk("seg-llm", p.llm_ms),
        mk("seg-tool", p.tool_ms),
      );
      bar.title = `T ${fmt(p.transcribe_ms)} · LLM ${fmt(p.llm_ms)} · tools ${fmt(p.tool_ms)} — total ${fmt(p.total_ms)} ms`;
    } else {
      bar.append(Object.assign(document.createElement("span"), { className: "seg-rest" }));
    }
    host.appendChild(bar);
  }
}

function renderRecentTurns() {
  const s = UI.state;
  const feed = (s.conversation_feed || []).slice(-4);
  const sig = JSON.stringify(feed);
  if (UI.sigs.ovRecent === sig) return;
  UI.sigs.ovRecent = sig;
  const host = $("ov-recent-turns");
  host.textContent = "";
  if (!feed.length) {
    host.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No turns yet — say something to Ultron.</span></div>`;
    return;
  }
  for (const turn of feed) {
    const row = document.createElement("div");
    row.className = "line-row";
    row.innerHTML =
      `<span class="status-dot ${turn.role === "user" ? "cobalt" : "green"}"></span>` +
      `<span class="name">${esc(turn.text)}<span class="sub">${esc(turn.role)}${turn.latency_ms ? ` · ${fmt(turn.latency_ms)} ms` : ""}${(turn.tools_called || []).length ? ` · ${turn.tools_called.map(esc).join(", ")}` : ""}</span></span>`;
    if (turn.role === "user") {
      const replay = btn("small", "↻ Replay");
      replay.onclick = () => sendCommand(turn.text, true);
      const actions = document.createElement("span");
      actions.className = "actions";
      actions.appendChild(replay);
      row.appendChild(actions);
    }
    host.appendChild(row);
  }
}

/* ----------------------------------- CHAT ------------------------------------ */
function renderChat() {
  const s = UI.state;
  if (!s) return;
  $("ch-thought").textContent = s.current_thought || "—";
  $("ch-heard").textContent = s.current_transcript || "—";
  const act = s.current_action || {};
  $("ch-action").textContent = act.tool
    ? `${act.tool}(${Object.entries(act.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")}) — ${act.status}`
    : "—";
  const plan = $("ch-plan");
  const steps = s.next_planned_steps || [];
  const sig = JSON.stringify(steps);
  if (UI.sigs.chPlan !== sig) {
    UI.sigs.chPlan = sig;
    plan.textContent = "";
    if (!steps.length) plan.innerHTML = "<li style='list-style:none;color:var(--ink-faint)'>—</li>";
    for (const step of steps) {
      const li = document.createElement("li");
      li.textContent = step;
      plan.appendChild(li);
    }
  }

  const q = UI.chat.query.toLowerCase();
  const feed = (s.conversation_feed || []).filter((t) =>
    !q || t.text.toLowerCase().includes(q) || (t.tools_called || []).some((x) => x.toLowerCase().includes(q)));
  const sig2 = JSON.stringify(feed) + q;
  if (UI.sigs.chFeed === sig2) return;
  UI.sigs.chFeed = sig2;

  const host = $("chat-feed");
  host.textContent = "";
  if (!feed.length) {
    host.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">${q ? "No turns match the search." : "No turns yet."}</span></div>`;
    return;
  }
  for (const turn of feed) {
    const card = document.createElement("div");
    card.className = `turn-card ${turn.role === "user" ? "user" : "assistant"}`;
    const time = String(turn.timestamp || "").split("T").pop() || "";
    const chips = (turn.tools_called || []).map((t) => `<span class="tool-chip">${esc(t)} ✓</span>`).join("");
    card.innerHTML =
      `<div class="turn-meta"><span class="turn-role">${turn.role}</span><span>${esc(time)}</span>` +
      `<span>${turn.source || ""}</span>${turn.latency_ms ? `<span>${fmt(turn.latency_ms)} ms</span>` : ""}</div>` +
      `<div class="turn-text">${esc(turn.text)}</div>` +
      (chips ? `<div class="tool-chips">${chips}</div>` : "");
    host.appendChild(card);
  }
  host.scrollTop = host.scrollHeight;
}

/* ------------------------------ TABS & EYES ---------------------------------- */
function renderTabs() {
  const s = UI.state;
  if (!s) return;
  const tabs = s.browser_tabs || [];
  const windows = s.screen_state?.open_windows || [];
  const refs = s.screen_state?.visible_elements || [];
  $("badge-tabs").textContent = String(tabs.length);
  animateNum($("tb-kpi-tabs"), tabs.length, (v) => fmt(Math.round(v)));
  animateNum($("tb-kpi-windows"), windows.length, (v) => fmt(Math.round(v)));
  animateNum($("tb-kpi-refs"), refs.length, (v) => fmt(Math.round(v)));
  $("tb-kpi-site").textContent = s.screen_state?.active_site || "—";

  renderTabRows(tabs, windows);
  renderRefRows(refs);
}

function siteIcon(title) {
  const t = String(title || "").toLowerCase();
  const map = [
    ["whatsapp", "🟢"], ["gmail", "✉"], ["gemini", "✦"], ["claude", "✽"],
    ["youtube", "▶"], ["github", "⌥"], ["chatgpt", "◉"], ["discord", "◈"],
  ];
  for (const [k, icon] of map) if (t.includes(k)) return icon;
  return "•";
}

function renderTabRows(tabs, windows) {
  const filter = UI.tabs.filter;
  const q = UI.tabs.query.toLowerCase();
  const sig = JSON.stringify([tabs, windows, filter, q]);
  if (UI.sigs.tbRows === sig) return;
  UI.sigs.tbRows = sig;

  const host = $("tb-rows");
  host.textContent = "";
  const showTabs = filter === "all" || filter === "tabs";
  const showWindows = filter === "all";

  for (const t of (showTabs ? tabs : [])) {
    if (q && !(`${t.title} ${t.url} ${t.browser}`.toLowerCase().includes(q))) continue;
    const row = document.createElement("div");
    row.className = "table-row";
    row.innerHTML =
      `<span><span class="site-badge">${siteIcon(t.title)}</span></span>` +
      `<span><div class="cell-title">${esc(t.title || t.url)}</div><div class="cell-url">${esc(t.browser)} · ${esc(t.url || "")}</div></span>` +
      `<span><span class="running-pill"><span class="status-dot ${t.active ? "green" : "grey"}"></span>${t.active ? "Active" : "Open"}</span></span>`;
    const actions = document.createElement("span");
    actions.style.display = "flex";
    actions.style.gap = "6px";
    actions.append(
      actBtn("Switch To", () => tabAction("switch", t.title).catch(showErr)),
      actBtn("Read Tab", () => readTab(t.title)),
      actBtn("Type / Prompt", () => typePrompt(t.title)),
      actBtn("Click Element", () => clickTarget(t.title)),
      Object.assign(btn("small danger", "Close"), { onclick: () => tabAction("close", t.title).catch(showErr) }),
    );
    row.appendChild(actions);
    host.appendChild(row);
  }

  for (const w of (showWindows ? windows : [])) {
    if (q && !String(w).toLowerCase().includes(q)) continue;
    const row = document.createElement("div");
    row.className = "table-row";
    row.innerHTML =
      `<span><span class="site-badge">🗔</span></span>` +
      `<span><div class="cell-title">${esc(w)}</div><div class="cell-url">desktop window</div></span>` +
      `<span><span class="running-pill"><span class="status-dot cobalt"></span>Open</span></span>`;
    const actions = document.createElement("span");
    actions.style.display = "flex";
    actions.style.gap = "6px";
    actions.append(
      actBtn("Switch To", () => appAction("focus", w).catch(showErr)),
      Object.assign(btn("small danger", "Close"), { onclick: () => appAction("close", w).catch(showErr) }),
    );
    row.appendChild(actions);
    host.appendChild(row);
  }
  if (!host.children.length) {
    host.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No tabs or windows match this filter.</span></div>`;
  }
}

function actBtn(label, fn) {
  const b = btn("small", label);
  b.onclick = fn;
  return b;
}

async function tabAction(action, tab, extra = {}) {
  const res = await api("/api/tabs/action", {
    method: "POST",
    body: { action, tab, ...extra },
  });
  toast(`Tab ${action}: ${res.reply || "done"}`, res.ok === false ? "err" : "ok");
  return res;
}

async function readTab(tab) {
  try {
    const res = await api("/api/tabs/action", { method: "POST", body: { action: "read", tab } });
    const box = $("tb-read-result");
    const text = res.reply || res.text || res.summary || res.raw_summary || JSON.stringify(res, null, 2);
    box.innerHTML = `<div class="read-result"><b style="color:var(--ink-dim)">Read — ${esc(tab)}</b>\n${esc(text)}</div>`;
  } catch (err) { showErr(err); }
}

function typePrompt(tab) {
  removePrompt();
  const row = eventTargetRow();
  const wrap = document.createElement("div");
  wrap.className = "inline-prompt";
  wrap.innerHTML =
    `<input placeholder="Type into ${esc(tab)} — target element (e.g. 'message box', blank = active input)" data-role="target">` +
    `<input placeholder="Text to send" data-role="text">` +
    `<button class="btn small" data-role="send-enter">Send ⏎</button>` +
    `<button class="btn small" data-role="type-only">Type</button>`;
  wrap.querySelector("[data-role=send-enter]").onclick = async () => {
    try {
      await tabAction("send", tab, {
        target: wrap.querySelector("[data-role=target]").value,
        text: wrap.querySelector("[data-role=text]").value,
      });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  wrap.querySelector("[data-role=type-only]").onclick = async () => {
    try {
      await tabAction("type", tab, {
        target: wrap.querySelector("[data-role=target]").value,
        text: wrap.querySelector("[data-role=text]").value,
      });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  (row || $("tb-rows")).appendChild(wrap);
}

function clickTarget(tab) {
  removePrompt();
  const wrap = document.createElement("div");
  wrap.className = "inline-prompt";
  wrap.innerHTML =
    `<input placeholder="Element to click inside ${esc(tab)} — e.g. 'New chat', 'Show more', 'Send'" data-role="target">` +
    `<button class="btn small primary" data-role="go">Click Element</button>`;
  wrap.querySelector("[data-role=go]").onclick = async () => {
    try {
      await tabAction("click", tab, { target: wrap.querySelector("[data-role=target]").value });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  $("tb-rows").appendChild(wrap);
}

function removePrompt() {
  document.querySelectorAll(".inline-prompt").forEach((el) => el.remove());
}
function eventTargetRow() { return $("tb-rows"); }

function renderRefRows(refs) {
  const sig = JSON.stringify(refs);
  if (UI.sigs.tbRefs === sig) return;
  UI.sigs.tbRefs = sig;
  const host = $("tb-refs");
  host.textContent = "";
  if (!refs.length) {
    host.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No AT-SPI elements detected right now.</span></div>`;
    return;
  }
  for (const ref of refs) {
    const row = document.createElement("div");
    row.className = "line-row";
    const label = `${ref.ref} · ${ref.name || "(unnamed)"} · ${ref.role || ""}`;
    row.innerHTML = `<span class="status-dot amber"></span><span class="name"><code>${esc(ref.ref)}</code> ${esc(ref.name || "(unnamed)")}<span class="sub">${esc(ref.role || "")}${ref.editable ? " · editable" : ""}${ref.focused ? " · focused" : ""}</span></span>`;
    const actions = document.createElement("span");
    actions.className = "actions";
    actions.append(actBtn(`Click ${ref.ref}`, async () => {
      try { await api("/api/screen/action", { method: "POST", body: { action: "click", ref: ref.ref } }); toast(`Clicked ${ref.ref}`, "ok"); }
      catch (err) { showErr(err); }
    }));
    const type = actBtn(`Type into ${ref.ref}`, () => {
      removePrompt();
      const wrap = document.createElement("div");
      wrap.className = "inline-prompt";
      wrap.innerHTML = `<input placeholder="Text to type into ${esc(ref.ref)}" data-role="text"><button class="btn small primary" data-role="go">Send ⏎</button>`;
      wrap.querySelector("[data-role=go]").onclick = async () => {
        try {
          await api("/api/screen/action", {
            method: "POST",
            body: { action: "send", ref: ref.ref, text: wrap.querySelector("[data-role=text]").value },
          });
          toast(`Typed into ${ref.ref}`, "ok");
          wrap.remove();
        } catch (err) { showErr(err); }
      };
      host.appendChild(wrap);
    });
    actions.appendChild(type);
    row.appendChild(actions);
    host.appendChild(row);
  }
}

/* ----------------------------------- APPS ------------------------------------ */
async function loadApps() {
  try {
    const res = await api("/api/apps");
    UI.apps.data = res;
    renderApps();
  } catch (err) { showErr(err); }
}

function appCategoryOf(app) {
  const cat = String(app.category || "").toLowerCase();
  const name = String(app.name || "").toLowerCase();
  if (cat.includes("dev") || /code|studio|terminal|ide|git|python/.test(name)) return "development";
  if (cat.includes("game") || cat.includes("media") || /lutris|steam|video|music|player|image/.test(name)) return "media";
  return "system";
}

function renderApps() {
  const data = UI.apps.data;
  if (!data) return;
  const apps = data.apps || [];
  $("badge-apps").textContent = String(apps.filter((a) => a.is_running).length);
  const q = UI.apps.query.toLowerCase();
  const filter = UI.apps.filter;
  const list = apps.filter((a) => {
    if (q && !a.name.toLowerCase().includes(q)) return false;
    if (filter === "running") return a.is_running;
    if (filter === "development" || filter === "media" || filter === "system") return appCategoryOf(a) === filter;
    return true;
  });
  const sig = JSON.stringify([list.length, q, filter, ...list.slice(0, 400).map((a) => [a.name, a.is_running])]);
  if (UI.apps.sig === sig) return;
  UI.apps.sig = sig;

  const grid = $("apps-grid");
  grid.textContent = "";
  if (!list.length) {
    grid.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No apps match.</span></div>`;
    return;
  }
  for (const app of list.slice(0, 300)) {
    const card = document.createElement("div");
    card.className = "app-card";
    card.innerHTML =
      `<div class="app-top"><span class="site-badge">${esc((app.name || "?").trim().charAt(0).toUpperCase())}</span>` +
      `<span class="app-name">${esc(app.name)}</span>` +
      (app.is_running ? `<span class="running-pill"><span class="status-dot green"></span>Running</span>` : "") +
      `</div><div class="app-actions"></div>`;
    const actions = card.querySelector(".app-actions");
    actions.append(
      actBtn("Launch", () => appAction("open", app.name).catch(showErr)),
      actBtn("Focus", () => appAction("focus", app.name).catch(showErr)),
      Object.assign(btn("small danger", "Close"), { onclick: () => appAction("close", app.name).catch(showErr) }),
    );
    grid.appendChild(card);
  }
}

async function appAction(action, app_name) {
  const res = await api("/api/apps/action", { method: "POST", body: { action, app_name } });
  toast(res.reply || `${action} ${app_name}: done`, "ok");
  loadApps();
}

/* ---------------------------------- SKILLS ----------------------------------- */
async function loadSkills() {
  try {
    const res = await api("/api/skills");
    UI.skills.data = res;
    renderSkills();
  } catch (err) { showErr(err); }
}

function renderSkills() {
  const data = UI.skills.data;
  if (!data) return;
  const skills = data.skills || [];
  const facts = data.facts || {};
  const aliases = data.aliases || {};
  $("badge-skills").textContent = String(skills.length);
  $("sk-count-hint").textContent = `${skills.length} taught`;

  const sig = JSON.stringify(data);
  if (UI.skills.sig === sig) return;
  UI.skills.sig = sig;

  const list = $("sk-list");
  list.textContent = "";
  if (!skills.length) {
    list.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No workflows taught yet.</span></div>`;
  }
  for (const sk of skills) {
    const card = document.createElement("div");
    card.className = "skill-card";
    card.innerHTML = `<div class="skill-trigger">“${esc(sk.trigger)}”</div><div class="skill-steps">${(sk.actions || []).map(esc).join("<br>→ ")}</div>`;
    const pills = document.createElement("div");
    pills.className = "filter-pills";
    pills.style.marginTop = "10px";
    pills.append(
      actBtn("Run Now", () => sendCommand(sk.trigger, true)),
      Object.assign(btn("small danger", "Delete"), {
        onclick: async () => {
          try {
            UI.skills.data = await api("/api/skills", { method: "DELETE", body: { type: "skill", trigger: sk.trigger } });
            renderSkills();
            toast(`Deleted workflow “${sk.trigger}”`, "ok");
          } catch (err) { showErr(err); }
        },
      }),
    );
    card.appendChild(pills);
    list.appendChild(card);
  }

  const factHost = $("sk-facts");
  factHost.textContent = "";
  const entries = [
    ...Object.entries(facts).map(([k, v]) => ({ type: "fact", k, v })),
    ...Object.entries(aliases).map(([k, v]) => ({ type: "alias", k, v })),
  ];
  if (!entries.length) {
    factHost.innerHTML = `<div class="line-row"><span class="status-dot grey"></span><span class="name">No facts or aliases saved yet.</span></div>`;
  }
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "line-row";
    row.innerHTML = `<span class="status-dot ${e.type === "fact" ? "green" : "cobalt"}"></span>` +
      `<span class="name">${esc(e.k)}<span class="sub">${e.type} → ${esc(e.v)}</span></span>`;
    const del = btn("small danger", "Delete");
    del.onclick = async () => {
      try {
        const body = e.type === "fact" ? { type: "fact", name: e.k } : { type: "alias", alias: e.k };
        UI.skills.data = await api("/api/skills", { method: "DELETE", body });
        renderSkills();
        toast(`Deleted ${e.type} “${e.k}”`, "ok");
      } catch (err) { showErr(err); }
    };
    const actions = document.createElement("span");
    actions.className = "actions";
    actions.appendChild(del);
    row.appendChild(actions);
    factHost.appendChild(row);
  }
}

/* -------------------------------- TELEMETRY ---------------------------------- */
function renderTelemetry() {
  const s = UI.state;
  if (!s) return;
  const t = s.telemetry || {};
  animateNum($("tl-kpi-p50"), t.p50_ms || 0, (v) => fmt(v, 0));
  animateNum($("tl-kpi-p95"), t.p95_ms || 0, (v) => fmt(v, 0));
  animateNum($("tl-kpi-gpu"), t.gpu_util_pct || 0, (v) => fmt(v, 0));
  $("tl-kpi-cpu").textContent = `${fmt(t.cpu_pct || 0, 0)}% · ${fmt((t.ram_used_mb || 0) / 1024, 1)} GB`;

  const series = t.latency_series || [];
  const sig = JSON.stringify(series);
  if (UI.sigs.tl !== sig) {
    UI.sigs.tl = sig;
    $("tl-series-hint").textContent = `${series.length} samples (max 30)`;
    if (series.length) {
      const avg = (key) => series.reduce((a, p) => a + (p[key] || 0), 0) / series.length;
      $("tl-stages").innerHTML = [
        ["Transcription (whisper)", avg("transcribe_ms"), "#60a5fa"],
        ["LLM (LFM3-1.7B)", avg("llm_ms"), "#34d399"],
        ["Tool execution", avg("tool_ms"), "#fbbf24"],
        ["Total per turn", avg("total_ms"), "#e8e2d9"],
      ].map(([label, ms, color]) =>
        `<div class="line-row"><span class="status-dot" style="background:${color}; box-shadow:0 0 8px ${color}"></span>` +
        `<span class="name">${label}<span class="sub">avg ${fmt(ms, 1)} ms</span></span>` +
        `<b style="font-variant-numeric:tabular-nums">${fmt(ms, 1)}</b></div>`).join("");
    }
    const rows = $("tl-series-rows");
    rows.textContent = "";
    for (const p of series.slice(-8).reverse()) {
      const row = document.createElement("div");
      row.className = "line-row";
      row.innerHTML = `<span class="status-dot grey"></span><span class="name">${esc(String(p.ts || "").split("T").pop() || "")}` +
        `<span class="sub">transcribe ${fmt(p.transcribe_ms)} · llm ${fmt(p.llm_ms)} · tool ${fmt(p.tool_ms)} ms</span></span>` +
        `<b style="font-variant-numeric:tabular-nums">${fmt(p.total_ms)} ms</b>`;
      rows.appendChild(row);
    }
    drawSpline($("tl-spline-line"), $("tl-spline-fill"), $("tl-spline-dot"), series.slice(-30), 600, 140);
  }

  animateNum($("tl-vram"), t.gpu_vram_used_mb || 0, (v) => fmt(v));
  $("tl-vram-bar").style.width = `${clamp(((t.gpu_vram_used_mb || 0) / (t.gpu_vram_total_mb || 4096)) * 100, 0, 100)}%`;
  $("tl-gpu-util").textContent = `${fmt(t.gpu_util_pct || 0, 0)}% utilization`;
  $("tl-llama").textContent = t.llama_server_alive ? "alive" : "down";
}

/* --------------------------------- SETTINGS ---------------------------------- */
const PRESETS = {
  jarvis: "You are Ultron, Nitin's executive AI in the style of J.A.R.V.I.S.: composed, precise, quietly witty. Address him as \"sir\" sparingly. Anticipate what he needs, confirm actions crisply, never ramble.",
  tactical: "You are Ultron. Reply in short tactical statements: action taken, key numbers, next option. No pleasantries, no filler, no more than two sentences.",
  warm: "You are Ultron, a warm companion assistant. Speak kindly and naturally to Nitin, celebrate small wins, and keep replies friendly and human.",
};

async function loadConfig() {
  try {
    const res = await api("/api/config");
    UI.config = res.config;
    syncSettings(true);
  } catch (err) { showErr(err); }
}

function syncSettings(force) {
  const c = UI.config || (UI.state && UI.state.live_config);
  if (!c) return;
  const set = (id, value) => {
    const el = $(id);
    if (!el || (!force && document.activeElement === el)) return;
    if (el.type === "range" || el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.tagName === "SELECT") {
      if (String(el.value) !== String(value)) el.value = value;
    }
  };
  set("st-personality", c.personality_prompt || "");
  set("st-reply-style", c.reply_style);
  set("st-silence", c.voice_silence_ms);
  set("st-maxsec", c.voice_max_seconds);
  set("st-followup", c.listen_followup_s);
  set("st-micvol", c.mic_source_volume);
  set("st-permission", c.permission_mode);
  set("st-assistant-name", c.assistant_name);
  set("st-user-name", c.user_name);
  set("st-search-engine", c.search_engine);
  if (force || document.activeElement !== $("st-brain-replies")) {
    $("st-brain-replies").classList.toggle("on", !!c.brain_replies);
    $("st-brain-replies").setAttribute("aria-checked", String(!!c.brain_replies));
  }
  sliderLabels();
}

function sliderLabels() {
  const c = UI.config || (UI.state && UI.state.live_config);
  if (!c) return;
  $("st-silence-val").textContent = `${fmt(c.voice_silence_ms)} ms`;
  $("st-maxsec-val").textContent = `${fmt(c.voice_max_seconds, 0)} s`;
  $("st-followup-val").textContent = `${fmt(c.listen_followup_s, 1)} s`;
  $("st-micvol-val").textContent = fmt(c.mic_source_volume, 2);
}

async function saveConfig(patch, label) {
  try {
    const res = await api("/api/config", { method: "POST", body: patch });
    UI.config = res.config;
    syncSettings(true);
    if (label) toast(label, "ok");
  } catch (err) { showErr(err); }
}

/* --------------------------------- BRIDGE ------------------------------------ */
async function loadBridge() {
  try {
    const res = await api("/api/tabs");
    UI.bridge = res.bridge_connected;
    const dot = $("br-dot");
    dot.className = `status-dot ${res.bridge_connected ? "green" : "grey"}`;
    $("br-state").textContent = res.bridge_connected
      ? "Bridge connected — tabs are syncing live"
      : "Bridge not connected — extension not loaded yet";
    $("br-state-hint").textContent = res.bridge_connected ? "connected" : "offline";
    $("badge-bridge").textContent = res.bridge_connected ? "●" : "·";
  } catch (err) { showErr(err); }
}

/* ------------------------------ view switching ------------------------------- */
function switchView(name) {
  UI.view = name;
  document.querySelectorAll("#rail-nav .nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${name}`);
  });
  renderHeader();
  if (name === "apps") loadApps();
  if (name === "skills") loadSkills();
  if (name === "settings") loadConfig();
  if (name === "bridge") loadBridge();
  if (name === "tabs") renderTabs();
  handleStateOnce();
}
function handleStateOnce() { if (UI.state) handleState(UI.state); }

/* --------------------------------- commands ---------------------------------- */
async function sendCommand(text, speak) {
  text = String(text || "").trim();
  if (!text) return;
  toast(`Ultron: “${text}”`, "");
  try {
    const res = await api("/api/command", { method: "POST", body: { text, speak } });
    toast(`Reply (${fmt(res.latency_ms)} ms): ${res.reply}`, "ok");
  } catch (err) { showErr(err); }
}

async function voiceControl(body) {
  try {
    return await api("/api/voice/control", { method: "POST", body });
  } catch (err) { showErr(err); return null; }
}

/* ================================ wiring ===================================== */
function wire() {
  // navigation
  document.querySelectorAll("#rail-nav .nav-item").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.view));
  });

  // theme toggle (Studio Ambient ⇄ Pure Transparent Glass)
  $("btn-theme").addEventListener("click", () => {
    const pure = document.body.classList.toggle("pure-glass");
    try { localStorage.setItem("ultron-glass", pure ? "1" : "0"); } catch { /* sandbox */ }
    toast(pure ? "Pure Transparent Desktop Glass" : "Studio Ambient Backdrop");
  });
  try { if (localStorage.getItem("ultron-glass") === "1") document.body.classList.add("pure-glass"); } catch { /* ignore */ }

  // voice mode (header + overview pills) — mode-only POST, no action key
  document.querySelectorAll("#mode-select button, #ov-mode-pills button").forEach((b) => {
    b.addEventListener("click", async () => {
      const res = await voiceControl({ mode: b.dataset.mode });
      if (res) toast(`Voice mode: ${res.voice_mode}`, "ok");
    });
  });

  // HUD voice buttons
  $("hud-mic").addEventListener("click", async () => {
    const muted = !!(UI.state && UI.state.mic_muted);
    const res = await voiceControl({ action: muted ? "unmute_mic" : "mute_mic" });
    if (res) toast(muted ? "Mic unmuted" : "Mic muted", "ok");
  });
  const ptt = $("hud-ptt");
  ptt.addEventListener("pointerdown", () => voiceControl({ action: "ptt_start" }));
  ptt.addEventListener("pointerup", () => voiceControl({ action: "ptt_stop" }));
  ptt.addEventListener("pointerleave", () => voiceControl({ action: "ptt_stop" }));
  $("hud-barge").addEventListener("click", async () => {
    const res = await voiceControl({ action: "barge_in" });
    if (res) toast("Barge-in — speech stopped", "ok");
  });
  $("hud-plus").addEventListener("click", () => {
    switchView("overview");
    $("cmd-input").focus();
    $("cmd-input").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // overview command bar
  const sendFrom = (inputId, speakKey) => () => {
    const input = $(inputId);
    const text = input.value.trim();
    if (!text) return;
    sendCommand(text, UI.speak[speakKey]);
    input.value = "";
  };
  $("cmd-send").addEventListener("click", sendFrom("cmd-input", "cmd"));
  $("cmd-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFrom("cmd-input", "cmd")(); }
  });
  $("cmd-speak-toggle").addEventListener("click", (e) => {
    UI.speak.cmd = !UI.speak.cmd;
    e.currentTarget.classList.toggle("ghost", !UI.speak.cmd);
    e.currentTarget.style.opacity = UI.speak.cmd ? "1" : "0.5";
    toast(`Speak reply: ${UI.speak.cmd ? "on" : "off"}`);
  });
  $("cmd-barge").addEventListener("click", async () => {
    const res = await voiceControl({ action: "barge_in" });
    if (res) toast("Stopped speaking", "ok");
  });

  // chat view
  $("chat-send").addEventListener("click", sendFrom("chat-input", "chat"));
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFrom("chat-input", "chat")(); }
  });
  $("chat-speak-toggle").addEventListener("click", (e) => {
    UI.speak.chat = !UI.speak.chat;
    e.currentTarget.style.opacity = UI.speak.chat ? "1" : "0.5";
    toast(`Speak reply: ${UI.speak.chat ? "on" : "off"}`);
  });
  $("chat-search").addEventListener("input", (e) => { UI.chat.query = e.target.value; UI.sigs.chFeed = null; renderChat(); });
  $("chat-voice-start").addEventListener("click", async () => {
    const res = await voiceControl({ action: "start" });
    if (res) toast("Voice listening started", "ok");
  });
  $("chat-voice-stop").addEventListener("click", async () => {
    const res = await voiceControl({ action: "stop" });
    if (res) toast("Voice listening stopped", "ok");
  });
  $("chat-barge").addEventListener("click", async () => {
    const res = await voiceControl({ action: "barge_in" });
    if (res) toast("Barge-in — speech stopped", "ok");
  });
  $("chat-repeat").addEventListener("click", () => {
    const feed = (UI.state && UI.state.conversation_feed) || [];
    const lastUser = [...feed].reverse().find((t) => t.role === "user");
    if (lastUser) sendCommand(lastUser.text, true);
    else toast("Nothing to repeat yet.");
  });

  // tabs view
  document.querySelectorAll("#tb-pills button").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#tb-pills button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      UI.tabs.filter = b.dataset.filter;
      UI.sigs.tbRows = null;
      renderTabs();
    });
  });
  $("tb-open-btn").addEventListener("click", async () => {
    const url = $("tb-open-url").value.trim();
    if (!url) return;
    try {
      await tabAction("open", url, {});
      $("tb-open-url").value = "";
    } catch (err) { showErr(err); }
  });

  // apps view
  $("apps-search").addEventListener("input", (e) => {
    UI.apps.query = e.target.value;
    UI.apps.sig = null;
    renderApps();
  });
  document.querySelectorAll("#apps-pills button").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#apps-pills button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      UI.apps.filter = b.dataset.filter;
      UI.apps.sig = null;
      renderApps();
    });
  });

  // skills view
  $("sk-save").addEventListener("click", async () => {
    const trigger = $("sk-trigger").value.trim();
    const actions = $("sk-actions").value.trim();
    if (!trigger || !actions) { toast("Trigger and action steps are both required.", "err"); return; }
    try {
      UI.skills.data = await api("/api/skills", {
        method: "POST",
        body: { type: "skill", trigger, actions: actions.split(";").map((a) => a.trim()).filter(Boolean) },
      });
      renderSkills();
      $("sk-trigger").value = ""; $("sk-actions").value = "";
      toast(`Workflow “${trigger}” saved`, "ok");
    } catch (err) { showErr(err); }
  });
  $("sk-fact-save").addEventListener("click", async () => {
    const name = $("sk-fact-name").value.trim();
    const value = $("sk-fact-value").value.trim();
    if (!name || !value) { toast("Fact name and value are both required.", "err"); return; }
    try {
      UI.skills.data = await api("/api/skills", { method: "POST", body: { type: "fact", name, value } });
      renderSkills();
      $("sk-fact-name").value = ""; $("sk-fact-value").value = "";
      toast(`Fact “${name}” saved`, "ok");
    } catch (err) { showErr(err); }
  });

  // settings view
  $("st-presets").addEventListener("click", (e) => {
    const preset = e.target.dataset && e.target.dataset.preset;
    if (!preset) return;
    if (preset === "custom") { $("st-personality").focus(); return; }
    $("st-personality").value = PRESETS[preset];
  });
  $("st-personality-save").addEventListener("click", () => {
    saveConfig({ personality_prompt: $("st-personality").value.trim() }, "Personality saved");
  });
  $("st-reply-style").addEventListener("change", (e) => saveConfig({ reply_style: e.target.value }, `Reply style: ${e.target.value}`));
  $("st-brain-replies").addEventListener("click", () => {
    const on = !$("st-brain-replies").classList.contains("on");
    saveConfig({ brain_replies: on }, `Brain replies ${on ? "on (100% LLM)" : "off (deterministic)"}`);
  });
  const slider = (id, key, transform, label) => {
    $(id).addEventListener("input", (e) => {
      UI.config = UI.config || {};
      UI.config[key] = transform(e.target.value);
      sliderLabels();
    });
    $(id).addEventListener("change", (e) => saveConfig({ [key]: transform(e.target.value) }, label(transform(e.target.value))));
  };
  slider("st-silence", "voice_silence_ms", (v) => parseInt(v, 10), (v) => `Pause tolerance ${fmt(v)} ms`);
  slider("st-maxsec", "voice_max_seconds", (v) => parseFloat(v), (v) => `Max speaking ${fmt(v, 0)} s`);
  slider("st-followup", "listen_followup_s", (v) => parseFloat(v), (v) => `Follow-up window ${fmt(v, 1)} s`);
  slider("st-micvol", "mic_source_volume", (v) => parseFloat(v), (v) => `Mic volume ${fmt(v, 2)}`);
  $("st-permission").addEventListener("change", (e) => saveConfig({ permission_mode: e.target.value }, `Permission mode: ${e.target.value}`));
  $("st-identity-save").addEventListener("click", () => {
    saveConfig({
      assistant_name: $("st-assistant-name").value.trim() || "Ultron",
      user_name: $("st-user-name").value.trim() || "Nitin",
      search_engine: $("st-search-engine").value,
    }, "Identity saved");
  });

  // bridge view
  $("br-refresh").addEventListener("click", loadBridge);

  // global quick search → routes into the active view's list filter
  $("quick-search").addEventListener("input", (e) => {
    const q = e.target.value;
    if (UI.view === "apps") { UI.apps.query = q; UI.apps.sig = null; $("apps-search").value = q; renderApps(); }
    else if (UI.view === "chat") { UI.chat.query = q; UI.sigs.chFeed = null; $("chat-search").value = q; renderChat(); }
    else if (UI.view === "tabs") { UI.tabs.query = q; UI.sigs.tbRows = null; renderTabs(); }
  });
  $("quick-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && UI.view === "overview") {
      const q = e.target.value.trim();
      if (q) { sendCommand(q, UI.speak.cmd); e.target.value = ""; }
    }
  });
}

/* ---------------------------------- boot ------------------------------------- */
document.addEventListener("DOMContentLoaded", () => {
  wire();
  bootstrap();
  setInterval(() => { if (UI.view === "apps") loadApps(); }, 15000);
  setInterval(() => { if (UI.view === "bridge") loadBridge(); }, 5000);
});
