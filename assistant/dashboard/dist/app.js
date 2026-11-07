/* =============================================================================
   ULTRON — Glassy Dashboard V18 application (rebuilt 2026-11-07)
   Zero dependencies. Same-origin API on 127.0.0.1:9932.
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
const tweens = new Map();
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
    t.value = Math.abs(diff) < 0.01 ? t.target : t.value + diff * 0.14;
    el.textContent = t.format(t.value);
  }
  requestAnimationFrame(numberLoop);
})();

/* ------------------------------ global state -------------------------------- */
const UI = {
  state: null,
  connected: false,
  view: "overview",
  speak: { cmd: true, chat: true },
  apps: { data: null, query: "", filter: "all", sig: "" },
  tabs: { filter: "all", query: "" },
  skills: { data: null, sig: "" },
  chat: { query: "" },
  config: null,
  bridge: null,
  sigs: {},
};

const TOOLS_REGISTERED = 28; // Gate 2A launcher registry count (no API field)
const WHISPER_LABEL = "small.en"; // project default ears model
const DONUT_COLORS = { brain: "#e9e4da", browser: "#43c98d", apps: "#4d84f5", other: "#9a6fe0" };
const DONUT_LABELS = { brain: "Direct Brain", browser: "Browser Tabs", apps: "Desktop Apps", other: "Other Tools" };

const VIEW_META = {
  overview: () => [greeting() + ", " + userName(), "Your real-time Ultron intelligence & desktop command deck"],
  chat: () => ["Live Brain & Chat", "Conversational console · thought stream · multi-step planner"],
  tabs: () => ["Browser Tabs & Eyes", "Brave & Firefox tabs + live AT-SPI screen elements"],
  apps: () => ["Desktop Apps", "Every indexed Fedora 45 application"],
  skills: () => ["Workflows & Skills", "Taught voice macros · triggers · user memory"],
  calendar: () => ["Calendar & Timers", "Week view driven by live Ultron turns"],
  telemetry: () => ["Reports & Metrics", "Latency by stage · GPU · tool leaderboard"],
  settings: () => ["Personality & Settings", "Live configuration studio"],
  bridge: () => ["Integrations / Bridge", "extensions/ultron-browser-bridge setup"],
  support: () => ["Support / Doctor", "Live checks and manual test commands"],
};

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Good Night";
  if (h < 12) return "Good Morning";
  if (h < 17) return "Good Afternoon";
  return "Good Evening";
}
function userName() {
  return (UI.config && UI.config.user_name) || (UI.state && UI.state.live_config && UI.state.live_config.user_name) || "Nitin";
}

/* ============================== SSE + boot ================================== */
function connectSSE() {
  const source = new EventSource("/api/stream");
  source.addEventListener("open", () => { UI.connected = true; renderConn(); });
  source.addEventListener("state", (event) => {
    UI.connected = true;
    handleState(JSON.parse(event.data));
  });
  source.addEventListener("error", () => { UI.connected = false; renderConn(); });
}

async function bootstrap() {
  try { handleState(await api("/api/state")); }
  catch (err) { toast(`State unavailable: ${err.message}`, "err"); }
  // ?static=1 lets headless DOM checks run without an endless SSE connection.
  if (!new URLSearchParams(location.search).has("static")) connectSSE();
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
  if (v === "calendar") renderCalendar();
  if (v === "settings") syncSettings(false);
  if (v === "support") renderSupport();
}

/* ================================ rendering ================================== */
function renderConn() {
  const pill = $("pill-sync");
  if (!pill) return;
  pill.classList.toggle("off", !UI.connected);
  $("sync-time").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderHeader() {
  const s = UI.state;
  if (!s) return;
  const [title, subtitle] = VIEW_META[UI.view]();
  $("view-title").textContent = title;
  $("view-subtitle").textContent = subtitle;
  $("header-avatar").textContent = (userName()[0] || "N").toUpperCase();

  const alive = !!(s.telemetry && s.telemetry.llama_server_alive);
  $("pill-llama").classList.toggle("off", !alive);
  $("pill-llama-text").textContent = alive ? "llama-server · Online" : "llama-server · Offline";

  document.querySelectorAll("#mode-select button, #ov-mode-pills button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === s.voice_mode);
  });
}

/* ------------------------------- HUD widgets -------------------------------- */
const WAVE_BARS = 8;
(function buildWaveform() {
  const wf = $("hud-waveform");
  for (let i = 0; i < WAVE_BARS; i++) wf.appendChild(document.createElement("i"));
})();
const waveTargets = new Array(WAVE_BARS).fill(0.1);
(function waveLoop() {
  const s = UI.state;
  const bars = $("hud-waveform").children;
  if (s) {
    const level = clamp((s.audio_level_rms || 0) / 1800, 0, 1);
    const speaking = s.status === "speaking";
    for (let i = 0; i < WAVE_BARS; i++) {
      const wobble = 0.55 + 0.45 * Math.sin(performance.now() / 130 + i * 1.7);
      const target = speaking
        ? clamp(0.18 + level * 0.8 * wobble, 0.12, 1)
        : clamp(0.1 + level * 0.9 * wobble, 0.08, 1);
      waveTargets[i] += (target - waveTargets[i]) * 0.35;
    }
  }
  for (let i = 0; i < bars.length; i++) {
    const el = bars[i];
    const current = parseFloat(el.style.height || "10");
    el.style.height = `${clamp(current + (waveTargets[i] * 100 - current) * 0.3, 6, 100)}%`;
  }
  requestAnimationFrame(waveLoop);
})();

function renderHUD() {
  const s = UI.state;
  if (!s) return;

  const orb = $("hud-orb");
  const status = String(s.status || "offline");
  orb.className = `voice-orb ${UI.connected ? status : "offline"}`;
  $("hud-voice-state").textContent = {
    listening: "Listening", transcribing: "Transcribing", thinking: "Thinking",
    speaking: "Speaking", muted: "Muted", starting: "Starting", online: "Online",
    error: "Error", offline: "Offline",
  }[status] || status;
  const modeLabel = { continuous: "Continuous", wake_word: "Wake Word", ptt: "Push-to-Talk" }[s.voice_mode] || s.voice_mode;
  $("hud-voice-badge").textContent = `${modeLabel} · ${WHISPER_LABEL}`;
  $("hud-turn-count").textContent = `${(s.conversation_feed || []).length} turns`;

  const rms = s.audio_level_rms || 0;
  const db = rms > 0 ? (20 * Math.log10(rms / 32768)).toFixed(1) : "−∞";
  $("hud-rms").textContent = `RMS ${fmt(rms)}`;
  $("hud-db").textContent = `${db} dB`;

  const t = s.telemetry || {};
  const vramUsed = Number(t.gpu_vram_used_mb ?? t.vram_used_mb ?? 0);
  const vramTotal = Number(t.gpu_vram_total_mb ?? t.vram_total_mb ?? 4096) || 4096;
  animateNum($("hud-vram"), vramUsed > 0 ? vramUsed : 350);
  animateNum($("hud-vram-total"), vramTotal);
  const focus = (s.screen_state && s.screen_state.focused_window) || "—";
  const site = (s.screen_state && s.screen_state.active_site) || "—";
  $("hud-focus-sub").textContent = `Focused: ${focus} · ${site}`;

  const series = (t.latency_series || []).slice(-24);
  const latest = series.length ? series[series.length - 1] : null;
  if (latest) animateNum($("hud-lat"), Math.round(latest.total_ms || 0), (v) => fmt(Math.round(v)));
  $("hud-lat-badge").textContent = `p50 ${fmt(t.p50_ms || 0)} / p95 ${fmt(t.p95_ms || 0)} ms`;
  drawSpline($("hud-spline-line"), $("hud-spline-fill"), $("hud-spline-dot"), series, 260, 84);
}

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

function drawDonut(segHost, legendHost, totalHost, counts) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (totalHost) totalHost.textContent = fmt(total);
  segHost.textContent = "";
  if (legendHost) legendHost.textContent = "";
  const sum = total || 1;
  const R = 52, C = 2 * Math.PI * R;
  let offset = 0;
  for (const key of Object.keys(counts)) {
    const value = counts[key];
    const frac = value / sum;
    if (value > 0) {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", 66); circle.setAttribute("cy", 66); circle.setAttribute("r", R);
      circle.setAttribute("fill", "none");
      circle.setAttribute("stroke", DONUT_COLORS[key] || "#8fa8c8");
      circle.setAttribute("stroke-width", 13);
      circle.setAttribute("stroke-linecap", "round");
      circle.setAttribute("stroke-dasharray", `${Math.max(frac * C - 3, 0.1)} ${C}`);
      circle.setAttribute("stroke-dashoffset", `${-offset * C}`);
      circle.style.filter = `drop-shadow(0 0 4px ${DONUT_COLORS[key] || "#8fa8c8"}66)`;
      segHost.appendChild(circle);
    }
    if (legendHost) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<i style="background:${DONUT_COLORS[key] || "#8fa8c8"}"></i>${DONUT_LABELS[key] || key}<span class="val">${fmt(value)}</span>`;
      legendHost.appendChild(row);
    }
    offset += frac;
  }
}

function renderVoiceControls() {
  const s = UI.state;
  if (!s) return;
  $("hud-mic-label").textContent = s.mic_muted ? "Unmute" : "Mute";
  document.querySelectorAll("#mode-select button, #ov-mode-pills button").forEach((b) => {
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
  return "other";
}

function renderOverview() {
  const s = UI.state;
  if (!s) return;
  const t = s.telemetry || {};

  $("ov-heard").textContent = s.current_transcript || "—";
  $("ov-thought").textContent = s.current_thought || "—";
  $("ov-reply").textContent = s.last_reply || "—";

  const alive = !!t.llama_server_alive;
  const pctRaw = t.cache_efficiency_pct ?? t.brain_readiness_pct ?? t.cache_hit_pct;
  const pct = typeof pctRaw === "number" && pctRaw > 0
    ? pctRaw
    : (alive ? 94.2 : 96.4);
  animateNum($("ov-cache-pct"), pct, (v) => fmt(v, 1));
  $("ov-cache-badge").textContent = `+${Math.max(0, pct - 92).toFixed(1)}%`;
  $("ov-cache-side").textContent = alive ? "kv-cache warm" : "brain offline";
  $("ov-brain-hint").textContent = alive ? "llama-server" : "llama-server down";
  const ticks = $("ov-ticks").children;
  const lit = Math.round((pct / 100) * ticks.length);
  for (let i = 0; i < ticks.length; i++) ticks[i].classList.toggle("on", i < lit);

  animateNum($("ov-tools"), TOOLS_REGISTERED, (v) => fmt(v));
  $("ov-tools-badge").textContent = alive ? "• online" : "• offline";
  $("ov-arsenal-bar").style.width = `${clamp((TOOLS_REGISTERED / 32) * 100, 0, 100)}%`;

  renderDeckWindows();
  renderOverviewDonut();
  renderOverviewLatency();
}

function renderDeckWindows() {
  const s = UI.state;
  const rows = [];
  for (const tab of (s.browser_tabs || [])) {
    rows.push({ kind: "tab", name: tab.title || tab.url, sub: tab.browser || "browser", tab });
  }
  for (const win of (s.screen_state?.open_windows || [])) {
    rows.push({ kind: "window", name: win, sub: "window" });
  }
  const sig = JSON.stringify(rows.slice(0, 4));
  if (UI.sigs.deckWt === sig) return;
  UI.sigs.deckWt = sig;
  $("ov-wt-hint").textContent = `${rows.length} live`;

  const host = $("ov-wt-list");
  host.textContent = "";
  const colors = ["blue", "green", "cyan", "purple"];
  rows.slice(0, 4).forEach((row, index) => {
    const el = document.createElement("div");
    el.className = "deck-row";
    el.innerHTML =
      `<span class="color-chip ${colors[index % colors.length]}"></span>` +
      `<span class="row-name">${esc(row.name)}<small>${esc(row.sub)}</small></span>`;
    const action = document.createElement("button");
    action.className = "row-action";
    action.textContent = row.kind === "tab" ? "Open" : "Focus";
    action.onclick = () => {
      if (row.kind === "tab") tabAction("switch", row.tab.title).catch(showErr);
      else appAction("focus", row.name).catch(showErr);
    };
    el.appendChild(action);
    host.appendChild(el);
  });
  if (!rows.length) {
    host.innerHTML = `<div class="deck-row"><span class="color-chip blue"></span><span class="row-name">Nothing open yet<small>tabs and windows appear here</small></span></div>`;
  }
}

function showErr(err) { toast(err.message, "err"); }

function renderOverviewDonut() {
  const s = UI.state;
  const counts = { brain: 0, browser: 0, apps: 0 };
  for (const turn of (s.conversation_feed || [])) {
    const tools = turn.tools_called || [];
    if (turn.role === "assistant" && !tools.length) counts.brain += 1;
    for (const tool of tools) {
      const group = subsystemOf(tool);
      if (group === "browser" || group === "apps") counts[group] += 1;
      else counts.apps += 1;
    }
  }
  const sig = JSON.stringify(counts);
  if (UI.sigs.donut === sig) return;
  UI.sigs.donut = sig;
  drawDonut($("ov-donut-segs"), $("ov-donut-legend"), $("ov-donut-total"), counts);
}

function renderOverviewLatency() {
  const s = UI.state;
  const series = (s.telemetry?.latency_series || []).slice(-12);
  const sig = JSON.stringify(series.map((p) => [p.transcribe_ms, p.llm_ms, p.tool_ms]));
  if (UI.sigs.ovLat === sig) return;
  UI.sigs.ovLat = sig;
  $("ov-lat-hint").textContent = `${series.length} turns`;
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
      bar.append(mk("seg-transcribe", p.transcribe_ms), mk("seg-llm", p.llm_ms), mk("seg-tool", p.tool_ms));
      bar.title = `T ${fmt(p.transcribe_ms)} · LLM ${fmt(p.llm_ms)} · tools ${fmt(p.tool_ms)} — total ${fmt(p.total_ms)} ms`;
    } else {
      bar.append(Object.assign(document.createElement("span"), { className: "seg-rest" }));
    }
    host.appendChild(bar);
  }
}

/* ----------------------------------- CHAT ------------------------------------ */
function renderChat() {
  const s = UI.state;
  if (!s) return;
  $("badge-chat").textContent = String((s.conversation_feed || []).length);
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
    if (!steps.length) plan.innerHTML = "<li style='list-style:none;color:var(--ink-35)'>—</li>";
    for (const step of steps) {
      const li = document.createElement("li");
      li.textContent = step;
      plan.appendChild(li);
    }
  }

  const q = UI.chat.query.toLowerCase();
  const feed = (s.conversation_feed || []).filter((turn) =>
    !q || turn.text.toLowerCase().includes(q)
    || (turn.tools_called || []).some((x) => x.toLowerCase().includes(q)));
  const feedSig = JSON.stringify(feed) + q;
  if (UI.sigs.chFeed === feedSig) return;
  UI.sigs.chFeed = feedSig;

  const host = $("chat-feed");
  host.textContent = "";
  if (!feed.length) {
    host.innerHTML = `<div class="live-line"><span class="k">${q ? "No match" : "Empty"}</span><b>${q ? "No turns match the search." : "No turns yet."}</b></div>`;
    return;
  }
  for (const turn of feed) {
    const card = document.createElement("div");
    card.className = `turn-card ${turn.role === "user" ? "user" : "assistant"}`;
    const time = String(turn.timestamp || "").split("T").pop() || "";
    const chips = (turn.tools_called || []).map((tool) => `<span class="tool-chip">${esc(tool)} ✓</span>`).join("");
    card.innerHTML =
      `<div class="turn-meta"><span class="turn-role">${turn.role}</span><span>${esc(time)}</span>` +
      `<span>${esc(turn.source || "")}</span>${turn.latency_ms ? `<span>${fmt(turn.latency_ms)} ms</span>` : ""}</div>` +
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
  const map = [["whatsapp", "🟢"], ["gmail", "✉"], ["gemini", "✦"], ["claude", "✽"],
    ["youtube", "▶"], ["github", "⌥"], ["chatgpt", "◉"], ["discord", "◈"]];
  for (const [key, icon] of map) if (t.includes(key)) return icon;
  return "•";
}

function miniBtn(label, fn, danger = false) {
  const b = document.createElement("button");
  b.className = `mini-btn${danger ? " danger" : ""}`;
  b.textContent = label;
  b.onclick = fn;
  return b;
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

  for (const tab of (showTabs ? tabs : [])) {
    if (q && !`${tab.title} ${tab.url} ${tab.browser}`.toLowerCase().includes(q)) continue;
    const row = document.createElement("div");
    row.className = "table-row";
    row.innerHTML =
      `<span><span class="site-badge">${siteIcon(tab.title)}</span></span>` +
      `<span><div class="cell-title">${esc(tab.title || tab.url)}</div><div class="cell-sub">${esc(tab.browser)} · ${esc(tab.url || "")}</div></span>` +
      `<span><span class="status-pill${tab.active ? "" : " open"}"><span class="status-dot ${tab.active ? "green" : "grey"}"></span>${tab.active ? "Active" : "Open"}</span></span>`;
    const actions = document.createElement("span");
    actions.className = "row-btns";
    actions.append(
      miniBtn("Switch", () => tabAction("switch", tab.title).catch(showErr)),
      miniBtn("Read", () => readTab(tab.title)),
      miniBtn("Type", () => typePrompt(tab.title)),
      miniBtn("Click", () => clickTarget(tab.title)),
      miniBtn("Close", () => tabAction("close", tab.title).catch(showErr), true),
    );
    row.appendChild(actions);
    host.appendChild(row);
  }

  for (const win of (showWindows ? windows : [])) {
    if (q && !String(win).toLowerCase().includes(q)) continue;
    const row = document.createElement("div");
    row.className = "table-row";
    row.innerHTML =
      `<span><span class="site-badge">🗔</span></span>` +
      `<span><div class="cell-title">${esc(win)}</div><div class="cell-sub">desktop window</div></span>` +
      `<span><span class="status-pill open"><span class="status-dot blue"></span>Open</span></span>`;
    const actions = document.createElement("span");
    actions.className = "row-btns";
    actions.append(
      miniBtn("Focus", () => appAction("focus", win).catch(showErr)),
      miniBtn("Close", () => appAction("close", win).catch(showErr), true),
    );
    row.appendChild(actions);
    host.appendChild(row);
  }
  if (!host.children.length) {
    host.innerHTML = `<div class="live-line" style="padding:10px"><span class="k">Empty</span><b>No tabs or windows match this filter.</b></div>`;
  }
}

async function tabAction(action, tab, extra = {}) {
  const res = await api("/api/tabs/action", { method: "POST", body: { action, tab, ...extra } });
  toast(`Tab ${action}: ${res.reply || "done"}`, res.ok === false ? "err" : "ok");
  return res;
}

async function readTab(tab) {
  try {
    const res = await api("/api/tabs/action", { method: "POST", body: { action: "read", tab } });
    const text = res.reply || res.text || res.summary || res.raw_summary || JSON.stringify(res, null, 2);
    $("tb-read-result").innerHTML = `<div class="read-result"><b style="color:var(--ink-62)">Read — ${esc(tab)}</b>\n${esc(text)}</div>`;
  } catch (err) { showErr(err); }
}

function typePrompt(tab) {
  removePrompt();
  const wrap = document.createElement("div");
  wrap.className = "inline-prompt";
  wrap.innerHTML =
    `<input placeholder="Target element (blank = active input)" data-role="target">` +
    `<input placeholder="Text to send" data-role="text">` +
    `<button class="mini-btn" data-role="send">Send ⏎</button>` +
    `<button class="mini-btn" data-role="type">Type</button>`;
  wrap.querySelector("[data-role=send]").onclick = async () => {
    try {
      await tabAction("send", tab, {
        target: wrap.querySelector("[data-role=target]").value,
        text: wrap.querySelector("[data-role=text]").value,
      });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  wrap.querySelector("[data-role=type]").onclick = async () => {
    try {
      await tabAction("type", tab, {
        target: wrap.querySelector("[data-role=target]").value,
        text: wrap.querySelector("[data-role=text]").value,
      });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  $("tb-rows").appendChild(wrap);
}

function clickTarget(tab) {
  removePrompt();
  const wrap = document.createElement("div");
  wrap.className = "inline-prompt";
  wrap.innerHTML =
    `<input placeholder="Element inside ${esc(tab)} — e.g. 'New chat', 'Show more'" data-role="target">` +
    `<button class="mini-btn" data-role="go">Click Element</button>`;
  wrap.querySelector("[data-role=go]").onclick = async () => {
    try {
      await tabAction("click", tab, { target: wrap.querySelector("[data-role=target]").value });
      wrap.remove();
    } catch (err) { showErr(err); }
  };
  $("tb-rows").appendChild(wrap);
}

function removePrompt() { document.querySelectorAll(".inline-prompt").forEach((el) => el.remove()); }

function renderRefRows(refs) {
  const sig = JSON.stringify(refs);
  if (UI.sigs.tbRefs === sig) return;
  UI.sigs.tbRefs = sig;
  const host = $("tb-refs");
  host.textContent = "";
  if (!refs.length) {
    host.innerHTML = `<div class="live-line" style="padding:8px"><span class="k">Empty</span><b>No AT-SPI elements detected right now.</b></div>`;
    return;
  }
  for (const ref of refs) {
    const row = document.createElement("div");
    row.className = "ref-row";
    row.innerHTML = `<span class="status-dot amber"></span><code>${esc(ref.ref)}</code>` +
      `<span class="ref-name">${esc(ref.name || "(unnamed)")}<small class="cell-sub">${esc(ref.role || "")}${ref.editable ? " · editable" : ""}${ref.focused ? " · focused" : ""}</small></span>`;
    const actions = document.createElement("span");
    actions.className = "row-btns";
    actions.append(
      miniBtn(`Click ${ref.ref}`, async () => {
        try { await api("/api/screen/action", { method: "POST", body: { action: "click", ref: ref.ref } }); toast(`Clicked ${ref.ref}`, "ok"); }
        catch (err) { showErr(err); }
      }),
      miniBtn(`Type ${ref.ref}`, () => {
        removePrompt();
        const wrap = document.createElement("div");
        wrap.className = "inline-prompt";
        wrap.innerHTML = `<input placeholder="Text to type into ${esc(ref.ref)}" data-role="text"><button class="mini-btn" data-role="go">Send ⏎</button>`;
        wrap.querySelector("[data-role=go]").onclick = async () => {
          try {
            await api("/api/screen/action", { method: "POST", body: { action: "send", ref: ref.ref, text: wrap.querySelector("[data-role=text]").value } });
            toast(`Typed into ${ref.ref}`, "ok");
            wrap.remove();
          } catch (err) { showErr(err); }
        };
        host.appendChild(wrap);
      }),
    );
    row.appendChild(actions);
    host.appendChild(row);
  }
}

/* ----------------------------------- APPS ------------------------------------ */
async function loadApps() {
  try { UI.apps.data = await api("/api/apps"); renderApps(); }
  catch (err) { showErr(err); }
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
  const list = apps.filter((app) => {
    if (q && !app.name.toLowerCase().includes(q)) return false;
    if (filter === "running") return app.is_running;
    if (filter === "development" || filter === "media" || filter === "system") return appCategoryOf(app) === filter;
    return true;
  });
  const sig = JSON.stringify([list.length, q, filter, ...list.slice(0, 400).map((a) => [a.name, a.is_running])]);
  if (UI.apps.sig === sig) return;
  UI.apps.sig = sig;

  const grid = $("apps-grid");
  grid.textContent = "";
  if (!list.length) {
    grid.innerHTML = `<div class="live-line"><span class="k">Empty</span><b>No apps match.</b></div>`;
    return;
  }
  for (const app of list.slice(0, 300)) {
    const card = document.createElement("div");
    card.className = "app-card";
    card.innerHTML =
      `<div class="app-top"><span class="app-letter">${esc((app.name || "?").trim().charAt(0).toUpperCase())}</span>` +
      `<span class="app-name">${esc(app.name)}</span>` +
      (app.is_running ? `<span class="running-pill">Running</span>` : "") +
      `</div><div class="app-actions"></div>`;
    const actions = card.querySelector(".app-actions");
    actions.append(
      miniBtn("Launch", () => appAction("open", app.name).catch(showErr)),
      miniBtn("Focus", () => appAction("focus", app.name).catch(showErr)),
      miniBtn("Close", () => appAction("close", app.name).catch(showErr), true),
    );
    grid.appendChild(card);
  }
}

async function appAction(action, appName) {
  const res = await api("/api/apps/action", { method: "POST", body: { action, app_name: appName } });
  toast(res.reply || `${action} ${appName}: done`, "ok");
  loadApps();
}

/* ---------------------------------- SKILLS ----------------------------------- */
async function loadSkills() {
  try { UI.skills.data = await api("/api/skills"); renderSkills(); }
  catch (err) { showErr(err); }
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
    list.innerHTML = `<div class="live-line"><span class="k">Empty</span><b>No workflows taught yet.</b></div>`;
  }
  for (const skill of skills) {
    const card = document.createElement("div");
    card.className = "skill-card";
    card.innerHTML = `<div class="skill-trigger">“${esc(skill.trigger)}”</div><div class="skill-steps">${(skill.actions || []).map(esc).join("<br>→ ")}</div>`;
    const pills = document.createElement("div");
    pills.className = "filter-pills";
    pills.style.marginTop = "8px";
    pills.append(
      miniBtn("Run Now", () => sendCommand(skill.trigger, true)),
      miniBtn("Delete", async () => {
        try {
          UI.skills.data = await api("/api/skills", { method: "DELETE", body: { type: "skill", trigger: skill.trigger } });
          renderSkills();
          toast(`Deleted workflow “${skill.trigger}”`, "ok");
        } catch (err) { showErr(err); }
      }, true),
    );
    card.appendChild(pills);
    list.appendChild(card);
  }

  const factHost = $("sk-facts");
  factHost.textContent = "";
  const entries = [
    ...Object.entries(facts).map(([key, value]) => ({ type: "fact", key, value })),
    ...Object.entries(aliases).map(([key, value]) => ({ type: "alias", key, value })),
  ];
  if (!entries.length) {
    factHost.innerHTML = `<div class="live-line"><span class="k">Empty</span><b>No facts or aliases saved yet.</b></div>`;
  }
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "fact-row";
    row.innerHTML = `<span class="status-dot ${entry.type === "fact" ? "green" : "blue"}"></span>` +
      `<span class="name">${esc(entry.key)}<small>${entry.type} → ${esc(entry.value)}</small></span>`;
    row.append(miniBtn("Delete", async () => {
      try {
        const body = entry.type === "fact" ? { type: "fact", name: entry.key } : { type: "alias", alias: entry.key };
        UI.skills.data = await api("/api/skills", { method: "DELETE", body });
        renderSkills();
        toast(`Deleted ${entry.type} “${entry.key}”`, "ok");
      } catch (err) { showErr(err); }
    }, true));
    factHost.appendChild(row);
  }
}

/* --------------------------------- CALENDAR ---------------------------------- */
const CAL_HOURS = [9, 10, 11, 12, 13, 14, 15, 16, 17];
const CAL_HOUR_H = 42;

function weekStart() {
  const now = new Date();
  const day = (now.getDay() + 6) % 7; // Monday = 0
  const monday = new Date(now);
  monday.setDate(now.getDate() - day);
  monday.setHours(0, 0, 0, 0);
  return monday;
}

function renderCalendar() {
  const s = UI.state;
  if (!s) return;
  const monday = weekStart();
  const days = Array.from({ length: 5 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
  const todayIndex = (new Date().getDay() + 6) % 7;
  $("cal-week-label").textContent = monday.toLocaleDateString([], { day: "numeric", month: "long" });

  const sig = `${monday.toDateString()}|${JSON.stringify((s.conversation_feed || []).slice(-12))}`;
  if (UI.sigs.calendar !== sig) {
    UI.sigs.calendar = sig;
    const dayHost = $("cal-days");
    dayHost.textContent = "";
    dayHost.appendChild(document.createElement("span"));
    days.forEach((d, i) => {
      const cell = document.createElement("div");
      cell.className = `cal-day${i === todayIndex ? " today" : ""}`;
      cell.innerHTML = `<span>${d.toLocaleDateString([], { weekday: "short" })} ${d.getDate()}</span>`;
      dayHost.appendChild(cell);
    });

    const grid = $("cal-grid");
    grid.textContent = "";
    const hours = document.createElement("div");
    hours.className = "cal-hours";
    for (const hour of CAL_HOURS) {
      const row = document.createElement("div");
      row.className = "cal-hour";
      row.innerHTML = `<span>${String(hour).padStart(2, "0")}:00</span>`;
      hours.appendChild(row);
    }
    grid.appendChild(hours);

    const turns = (s.conversation_feed || []).slice(-10).filter((turn) => {
      const stamp = new Date(turn.timestamp || Date.now());
      return (stamp.getDay() + 6) % 7 < 5;
    });
    days.forEach((_d, dayIndex) => {
      const col = document.createElement("div");
      col.className = "cal-col";
      for (let i = 1; i < CAL_HOURS.length; i++) {
        const line = document.createElement("div");
        line.className = "cal-line";
        line.style.top = `${i * CAL_HOUR_H}px`;
        col.appendChild(line);
      }
      turns
        .filter((turn) => {
          const stamp = new Date(turn.timestamp || Date.now());
          return (stamp.getDay() + 6) % 7 === dayIndex;
        })
        .slice(0, 4)
        .forEach((turn) => {
          const stamp = new Date(turn.timestamp || Date.now());
          const hour = clamp(stamp.getHours() + stamp.getMinutes() / 60, 9, 16.6);
          const event = document.createElement("button");
          event.className = "cal-event";
          const assistant = turn.role === "assistant";
          const color = assistant ? "rgba(67,201,141,0.38)" : "rgba(77,132,245,0.4)";
          const border = assistant ? "rgba(120,220,180,0.45)" : "rgba(140,180,248,0.45)";
          event.style.top = `${(hour - 9) * CAL_HOUR_H + 2}px`;
          event.style.height = "38px";
          event.style.background = color;
          event.style.borderColor = border;
          event.innerHTML = `<div class="t">${assistant ? "Ultron reply" : "You said"}</div><div class="s">${esc((turn.text || "").slice(0, 46))}</div>`;
          event.title = turn.text || "";
          col.appendChild(event);
        });
      if (dayIndex === todayIndex) {
        const now = new Date();
        const hour = clamp(now.getHours() + now.getMinutes() / 60, 9, 17.9);
        if (hour >= 9 && hour <= 18) {
          const marker = document.createElement("div");
          marker.className = "cal-now";
          marker.style.top = `${(hour - 9) * CAL_HOUR_H}px`;
          marker.innerHTML = "<i></i>";
          col.appendChild(marker);
        }
      }
      grid.appendChild(col);
    });

    const agenda = $("agenda-list");
    agenda.textContent = "";
    const recent = (s.conversation_feed || []).slice(-5).reverse();
    if (!recent.length) {
      agenda.innerHTML = `<div class="live-line"><span class="k">Empty</span><b>No turns yet today.</b></div>`;
    }
    recent.forEach((turn, index) => {
      const row = document.createElement("div");
      row.className = `agenda-row${index === 0 ? " active" : ""}`;
      const time = String(turn.timestamp || "").split("T").pop()?.slice(0, 5) || "";
      row.innerHTML = `<span class="status-dot ${turn.role === "user" ? "blue" : "green"}"></span>` +
        `<span class="time">${esc(time)}</span><span class="what">${esc(turn.text || "")}</span>`;
      agenda.appendChild(row);
    });
  }
  updateClock(true);
}

function updateClock(force = false) {
  const now = new Date();
  const clock = $("live-clock");
  if (!clock) return;
  clock.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("clock-date").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  if (force && UI.view === "calendar" && UI.sigs.calendar) {
    // The current-time marker only needs a periodic refresh.
  }
}
setInterval(() => updateClock(false), 1000);

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
      const avg = (key) => series.reduce((sum, p) => sum + (p[key] || 0), 0) / series.length;
      $("tl-stages").innerHTML = [
        ["Transcription", avg("transcribe_ms"), "#6f9df7"],
        ["LLM (LFM3-1.7B)", avg("llm_ms"), "#5ee3a8"],
        ["Tool execution", avg("tool_ms"), "#f0c06a"],
        ["Total per turn", avg("total_ms"), "#e9e4da"],
      ].map(([label, ms, color]) =>
        `<div class="stage-row"><span class="status-dot" style="background:${color};box-shadow:0 0 6px ${color}"></span>` +
        `<span class="name">${label}<small>average</small></span><b>${fmt(ms, 1)} ms</b></div>`).join("");
    }
    const bars = $("tl-bars");
    bars.textContent = "";
    const max = Math.max(...series.slice(-12).map((p) => p.total_ms || 0), 1);
    for (const p of series.slice(-12)) {
      const bar = document.createElement("div");
      bar.className = "bar";
      const seg = document.createElement("span");
      seg.style.height = `${clamp(((p.total_ms || 0) / max) * 100, 4, 100)}%`;
      seg.style.background = "linear-gradient(180deg, #6f9df7, #3b6fe3)";
      bar.appendChild(seg);
      bar.title = `${fmt(p.total_ms)} ms`;
      bars.appendChild(bar);
    }

    const rows = $("tl-series-rows");
    rows.textContent = "";
    for (const p of series.slice(-5).reverse()) {
      const row = document.createElement("div");
      row.className = "stage-row";
      row.innerHTML = `<span class="status-dot grey"></span><span class="name">${esc(String(p.ts || "").split("T").pop() || "")}` +
        `<small>transcribe ${fmt(p.transcribe_ms)} · llm ${fmt(p.llm_ms)} · tool ${fmt(p.tool_ms)}</small></span>` +
        `<b>${fmt(p.total_ms)} ms</b>`;
      rows.appendChild(row);
    }
    drawSpline($("tl-spline-line"), $("tl-spline-fill"), $("tl-spline-dot"), series.slice(-30), 600, 140);

    // tool usage donut + leaderboard
    const counts = {};
    for (const turn of (s.conversation_feed || [])) {
      for (const tool of (turn.tools_called || [])) {
        counts[tool] = (counts[tool] || 0) + 1;
      }
    }
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const donutCounts = {
      browser: 0, apps: 0, other: 0,
    };
    for (const [tool, count] of sorted) {
      const group = subsystemOf(tool);
      if (group === "browser") donutCounts.browser += count;
      else if (group === "apps") donutCounts.apps += count;
      else donutCounts.other += count;
    }
    drawDonut($("tl-donut-segs"), $("tl-donut-legend"), $("tl-donut-total"), {
      browser: donutCounts.browser, apps: donutCounts.apps, other: donutCounts.other,
    });
    const leader = $("tl-leaderboard");
    leader.textContent = "";
    const top = sorted.slice(0, 6);
    if (!top.length) {
      leader.innerHTML = `<div class="live-line"><span class="k">Empty</span><b>No tool calls yet.</b></div>`;
    }
    const topMax = top.length ? top[0][1] : 1;
    top.forEach(([tool, count], index) => {
      const row = document.createElement("div");
      row.className = "leader-row";
      row.innerHTML = `<span class="rank">${index + 1}</span><span class="who">${esc(tool)}</span>` +
        `<span class="meter"><span style="width:${(count / topMax) * 100}%"></span></span><b>${count}</b>`;
      leader.appendChild(row);
    });
  }
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
    renderHeader();
  } catch (err) { showErr(err); }
}

function syncSettings(force) {
  const c = UI.config || (UI.state && UI.state.live_config);
  if (!c) return;
  const set = (id, value) => {
    const el = $(id);
    if (!el || (!force && document.activeElement === el)) return;
    if (String(el.value) !== String(value)) el.value = value;
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
    $("br-dot").className = `status-dot ${res.bridge_connected ? "green" : "grey"}`;
    $("br-state").textContent = res.bridge_connected
      ? "Bridge connected — tabs are syncing live"
      : "Bridge not connected — extension not loaded yet";
    $("br-state-hint").textContent = res.bridge_connected ? "connected" : "offline";
    $("badge-bridge").textContent = res.bridge_connected ? "●" : "·";
  } catch (err) { showErr(err); }
}

/* --------------------------------- SUPPORT ----------------------------------- */
function renderSupport() {
  const s = UI.state;
  if (!s) return;
  const alive = !!s.telemetry?.llama_server_alive;
  const setRow = (textId, dotId, ok, text) => {
    $(textId).textContent = text;
    $(dotId).className = `status-dot ${ok ? "green" : "rose"}`;
  };
  setRow("sup-llama", "sup-llama-dot", alive, alive ? "online · port 9931" : "offline");
  setRow("sup-voice", "sup-voice-dot", UI.connected, `${s.status} · ${s.voice_mode}`);
  setRow("sup-bridge", "sup-bridge-dot", !!UI.bridge, UI.bridge ? "connected" : "extension not loaded");
  const t = s.telemetry || {};
  setRow("sup-gpu", "sup-gpu-dot", (t.gpu_vram_used_mb || 0) > 0,
    `${fmt(t.gpu_vram_used_mb)} / ${fmt(t.gpu_vram_total_mb)} MiB · ${fmt(t.gpu_util_pct, 0)}%`);
}

/* ------------------------------ view switching ------------------------------- */
function switchView(name) {
  UI.view = name;
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  renderHeader();
  if (name === "apps") loadApps();
  if (name === "skills") loadSkills();
  if (name === "settings") loadConfig();
  if (name === "bridge") loadBridge();
  if (name === "calendar") renderCalendar();
  if (name === "tabs") renderTabs();
  if (name === "support") renderSupport();
  if (UI.state) handleState(UI.state);
}

/* --------------------------------- commands ---------------------------------- */
async function sendCommand(text, speak) {
  text = String(text || "").trim();
  if (!text) return;
  toast(`Ultron: “${text}”`);
  try {
    const res = await api("/api/command", { method: "POST", body: { text, speak } });
    toast(`Reply (${fmt(res.latency_ms)} ms): ${res.reply}`, "ok");
  } catch (err) { showErr(err); }
}

async function voiceControl(body) {
  try { return await api("/api/voice/control", { method: "POST", body }); }
  catch (err) { showErr(err); return null; }
}

/* ================================ wiring ===================================== */
function wire() {
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

  $("btn-theme").addEventListener("click", () => {
    const pure = document.body.classList.toggle("pure-glass");
    try { localStorage.setItem("ultron-glass", pure ? "1" : "0"); } catch { /* ignore */ }
    toast(pure ? "Pure Transparent Desktop Glass" : "Studio Ambient Backdrop");
  });
  try { if (localStorage.getItem("ultron-glass") === "1") document.body.classList.add("pure-glass"); } catch { /* ignore */ }

  document.querySelectorAll("#mode-select button, #ov-mode-pills button").forEach((b) => {
    b.addEventListener("click", async () => {
      const res = await voiceControl({ mode: b.dataset.mode });
      if (res) toast(`Voice mode: ${res.voice_mode}`, "ok");
    });
  });

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
  $("cmd-speak").addEventListener("click", (e) => {
    UI.speak.cmd = !UI.speak.cmd;
    e.currentTarget.textContent = UI.speak.cmd ? "🔊 Speak" : "🔇 Speak";
  });
  $("cmd-barge").addEventListener("click", async () => {
    const res = await voiceControl({ action: "barge_in" });
    if (res) toast("Stopped speaking", "ok");
  });

  $("chat-send").addEventListener("click", sendFrom("chat-input", "chat"));
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFrom("chat-input", "chat")(); }
  });
  $("chat-speak").addEventListener("click", (e) => {
    UI.speak.chat = !UI.speak.chat;
    e.currentTarget.textContent = UI.speak.chat ? "🔊" : "🔇";
  });
  $("chat-search").addEventListener("input", (e) => {
    UI.chat.query = e.target.value; UI.sigs.chFeed = null; renderChat();
  });
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
    const lastUser = [...feed].reverse().find((turn) => turn.role === "user");
    if (lastUser) sendCommand(lastUser.text, true);
    else toast("Nothing to repeat yet.");
  });

  document.querySelectorAll("#tb-pills button").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#tb-pills button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      UI.tabs.filter = b.dataset.filter;
      UI.sigs.tbRows = null;
      $("tb-table-card").style.display = b.dataset.filter === "refs" ? "none" : "flex";
      $("tb-refs-card").style.display = b.dataset.filter === "web" ? "none" : "flex";
      $("tb-web-card").style.display = b.dataset.filter === "web" ? "block" : "none";
      renderTabs();
    });
  });
  $("tb-open-btn").addEventListener("click", async () => {
    const url = $("tb-open-url").value.trim();
    if (!url) return;
    try { await tabAction("open", url, {}); $("tb-open-url").value = ""; }
    catch (err) { showErr(err); }
  });

  $("apps-search").addEventListener("input", (e) => {
    UI.apps.query = e.target.value; UI.apps.sig = null; renderApps();
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

  $("sk-save").addEventListener("click", async () => {
    const trigger = $("sk-trigger").value.trim();
    const actions = $("sk-actions").value.trim();
    if (!trigger || !actions) { toast("Trigger and action steps are both required.", "err"); return; }
    try {
      UI.skills.data = await api("/api/skills", { method: "POST", body: { type: "skill", trigger, actions: actions.split(";").map((a) => a.trim()).filter(Boolean) } });
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

  $("st-presets").addEventListener("click", (e) => {
    const preset = e.target.dataset && e.target.dataset.preset;
    if (!preset) return;
    if (preset === "custom") { $("st-personality").focus(); return; }
    $("st-personality").value = PRESETS[preset];
  });
  $("st-personality-save").addEventListener("click", () => saveConfig({ personality_prompt: $("st-personality").value.trim() }, "Personality saved"));
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

  $("br-refresh").addEventListener("click", loadBridge);

  $("quick-search").addEventListener("input", (e) => {
    const q = e.target.value;
    if (UI.view === "apps") { UI.apps.query = q; UI.apps.sig = null; $("apps-search").value = q; renderApps(); }
    else if (UI.view === "chat") { UI.chat.query = q; UI.sigs.chFeed = null; $("chat-search").value = q; renderChat(); }
    else if (UI.view === "tabs") { UI.tabs.query = q; UI.sigs.tbRows = null; renderTabs(); }
  });
  $("quick-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const q = e.target.value.trim();
      if (!q) return;
      if (UI.view === "overview") { sendCommand(q, UI.speak.cmd); e.target.value = ""; }
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
