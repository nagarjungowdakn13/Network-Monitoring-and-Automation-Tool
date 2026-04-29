// netmon dashboard frontend.
//
// Strategy: HTTP-first with WebSocket as a live upgrade.
//   1. On load, fetch /api/config + /api/status + /api/history concurrently.
//      Charts and KPIs populate immediately, even before any WS handshake.
//   2. Open /ws. Each message updates state in real time.
//   3. If WS fails (network blocks ws://, CDN-restricted env, etc.), fall
//      back to polling /api/status + /api/history every 2.5 s. The UI
//      stays alive either way.

(() => {
  "use strict";

  const MAX_POINTS = 300;
  const WS_RECONNECT_MS = 1500;
  const POLL_INTERVAL_MS = 2500;
  const FRESH_THRESHOLD_S = 6;          // "last" pill turns warn after this many s

  const $ = (id) => document.getElementById(id);

  // ---------- Application state ----------
  const state = {
    config: null,
    history: [],                        // ring buffer of small sample dicts
    recentAnomalies: [],                // newest pushed at end
    lastSample: null,
    pid: null,
    startedAt: null,
    samplesProcessed: 0,
    alertsFired: 0,
    alertsSuppressed: 0,
    transport: "init",                  // 'ws' | 'poll' | 'init' | 'down'
    ws: null,
    wsReconnectTimer: null,
    pollTimer: null,
    seenAnomalyKeys: new Set(),
    paused: false,
    theme: "light",                     // 'light' | 'dark'
    severityFilter: { info: true, warning: true, critical: true },
  };

  const THEME_KEY = "netmon.theme";

  // Pull a CSS var as a string. Used so Chart.js follows the active theme
  // without us hand-syncing colour tables. Read at chart-build time and
  // again on every theme switch.
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // ---------- Charts ----------
  let bwChart = null;
  let connChart = null;

  // Custom plugin: draws horizontal threshold guides and vertical
  // anomaly markers on the bandwidth chart. Editorial-style: thin
  // dashed strokes, severity-coloured, with a small printed label.
  const annotationsPlugin = {
    id: "annotations",
    afterDatasetsDraw(chart) {
      const cfg = state.config;
      if (!cfg) return;
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales || !scales.x || !scales.y) return;

      ctx.save();

      // --- threshold guide lines (horizontal) ---
      const tIn  = cfg.thresholds.bandwidth_mbps_in;
      const tOut = cfg.thresholds.bandwidth_mbps_out;
      const drawGuide = (value, color, label) => {
        const yMax = scales.y.max ?? 1;
        if (value > yMax * 1.5) return;     // skip if guide is way off-screen
        const y = scales.y.getPixelForValue(value);
        if (y < chartArea.top || y > chartArea.bottom) return;
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(chartArea.left, y);
        ctx.lineTo(chartArea.right, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = "10px 'IBM Plex Mono', monospace";
        ctx.fillStyle = color;
        ctx.textAlign = "right";
        ctx.fillText(label, chartArea.right - 4, y - 3);
      };
      drawGuide(tIn,  cssVar("--c-in")  || "#2c4159", `threshold in ${tIn} Mbps`);
      drawGuide(tOut, cssVar("--c-out") || "#8b2e2a", `threshold out ${tOut} Mbps`);

      // --- anomaly markers (vertical) ---
      const sevColor = {
        info:     cssVar("--ink-blue") || "#2c4159",
        warning:  cssVar("--ochre")    || "#a37016",
        critical: cssVar("--madder")   || "#8b2e2a",
      };
      // Look at recent anomalies whose timestamp falls in the visible range.
      const xMin = scales.x.min, xMax = scales.x.max;
      for (const a of state.recentAnomalies.slice(-30)) {
        const ts = (a.sample_ts || 0) * 1000;
        if (ts < xMin || ts > xMax) continue;
        const x = scales.x.getPixelForValue(ts);
        const c = sevColor[a.severity] || sevColor.warning;
        ctx.setLineDash([2, 3]);
        ctx.lineWidth = 1;
        ctx.strokeStyle = c;
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        // small triangle pointing down at top of marker
        ctx.fillStyle = c;
        ctx.beginPath();
        ctx.moveTo(x - 3, chartArea.top);
        ctx.lineTo(x + 3, chartArea.top);
        ctx.lineTo(x,     chartArea.top + 4);
        ctx.closePath();
        ctx.fill();
      }
      ctx.restore();
    },
  };

  // Repaint Chart.js theming after a light/dark switch. Cheaper than
  // rebuilding the chart instances from scratch.
  function rethemeCharts() {
    if (!bwChart || !connChart) return;
    const ink3  = cssVar("--ink-3");
    const ink4  = cssVar("--ink-4");
    const rule2 = cssVar("--rule-2");
    const ink   = cssVar("--ink");
    const paper = cssVar("--paper");
    const cIn   = cssVar("--c-in");
    const cOut  = cssVar("--c-out");
    const cConn = cssVar("--c-conn");
    const cPkt  = cssVar("--c-pkt");

    Chart.defaults.color = ink3;
    [bwChart, connChart].forEach((ch) => {
      const { options } = ch;
      for (const k of Object.keys(options.scales || {})) {
        if (options.scales[k].grid)  options.scales[k].grid.color  = rule2;
        if (options.scales[k].ticks) options.scales[k].ticks.color = ink4;
        if (options.scales[k].title) options.scales[k].title.color = ink4;
      }
      if (options.plugins && options.plugins.tooltip) {
        Object.assign(options.plugins.tooltip, {
          backgroundColor: ink, titleColor: paper, borderColor: ink,
          bodyColor: cssVar("--ink-5"),
        });
      }
    });
    bwChart.data.datasets[0].borderColor   = cIn;
    bwChart.data.datasets[1].borderColor   = cOut;
    connChart.data.datasets[0].borderColor = cConn;
    connChart.data.datasets[1].borderColor = cPkt;
    bwChart.update("none");
    connChart.update("none");
    refreshSparklines();
  }

  function initCharts() {
    if (typeof Chart === "undefined") {
      console.error("Chart.js failed to load. KPIs will still update; charts disabled.");
      return;
    }

    // Pull theme colours from CSS variables - they shift with theme switches.
    const ink3   = cssVar("--ink-3")   || "#6b6657";
    const ink4   = cssVar("--ink-4")   || "#9a9582";
    const rule   = cssVar("--rule")    || "#d6d2c2";
    const rule2  = cssVar("--rule-2")  || "#e8e4d6";
    const ink    = cssVar("--ink")     || "#1a1815";
    const paper  = cssVar("--paper")   || "#faf8f3";

    Chart.defaults.color = ink3;
    Chart.defaults.borderColor = rule;
    Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.animation = false;

    const grid  = { color: rule2, drawTicks: false, tickLength: 0 };
    const ticks = { color: ink4,  padding: 8, font: { family: "'IBM Plex Mono', monospace", size: 10.5 } };

    const baseOpts = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: ink,
          titleColor: paper,
          bodyColor: cssVar("--ink-5") || "#c4bfae",
          borderColor: ink,
          borderWidth: 1,
          padding: 10,
          cornerRadius: 0,
          displayColors: true,
          boxWidth: 8, boxHeight: 8,
          titleFont: { family: "'IBM Plex Mono', monospace", weight: "500", size: 11 },
          bodyFont:  { family: "'IBM Plex Mono', monospace", size: 11 },
          callbacks: {
            title: (items) => {
              if (!items.length) return "";
              const x = items[0].parsed.x;
              return new Date(x).toLocaleTimeString();
            },
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          grid,
          ticks: {
            ...ticks,
            maxTicksLimit: 6,
            callback: (v) => new Date(v).toLocaleTimeString(),
          },
        },
        y: { type: "linear", beginAtZero: true, grid, ticks },
      },
      elements: {
        line: { borderWidth: 1.8, tension: 0.32 },
        point: { radius: 0, hoverRadius: 4, hitRadius: 12 },
      },
    };

    const cIn   = cssVar("--c-in")   || "#2c4159";
    const cOut  = cssVar("--c-out")  || "#8b2e2a";
    const cConn = cssVar("--c-conn") || "#a37016";
    const cPkt  = cssVar("--c-pkt")  || "#4a6b3a";

    // Bandwidth chart. Strokes only, no fills - editorial print-figure look.
    // The plugin appended below draws threshold guide lines + anomaly markers.
    bwChart = new Chart($("bw-chart").getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          dataset("Inbound  (Mbps)",  cIn,  null),
          dataset("Outbound (Mbps)",  cOut, null),
        ],
      },
      options: baseOpts,
      plugins: [annotationsPlugin],
    });

    connChart = new Chart($("conn-chart").getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          { ...dataset("Connections", cConn, null), yAxisID: "y" },
          { ...dataset("Packets / s", cPkt,  null), yAxisID: "y1" },
        ],
      },
      options: {
        ...baseOpts,
        scales: {
          ...baseOpts.scales,
          y:  { type: "linear", position: "left",  beginAtZero: true, grid, ticks,
                title: { display: true, text: "connections", color: ink4, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
          y1: { type: "linear", position: "right", beginAtZero: true, ticks, grid: { drawOnChartArea: false },
                title: { display: true, text: "packets/s",   color: ink4, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        },
      },
    });

    if (state.history.length) replayHistoryToCharts();
  }

  function dataset(label, color, fillColor) {
    return {
      label, data: [],
      borderColor: color,
      backgroundColor: fillColor,
      // Editorial figures: strokes only unless an explicit fill colour is given.
      fill: fillColor != null,
      pointRadius: 0,
      borderWidth: 1.6,
    };
  }

  function replayHistoryToCharts() {
    if (!bwChart || !connChart || state.paused) return;
    const hist = state.history;
    bwChart.data.datasets[0].data   = hist.map((s) => ({ x: s.ts * 1000, y: s.mbps_in }));
    bwChart.data.datasets[1].data   = hist.map((s) => ({ x: s.ts * 1000, y: s.mbps_out }));
    connChart.data.datasets[0].data = hist.map((s) => ({ x: s.ts * 1000, y: s.connections }));
    connChart.data.datasets[1].data = hist.map((s) => ({ x: s.ts * 1000, y: s.packets_per_s }));
    bwChart.update("none");
    connChart.update("none");
  }

  function pushSampleToCharts(sample) {
    if (!bwChart || !connChart || state.paused) return;
    const ts = sample.ts * 1000;
    const pkts = (sample.total_packets_sent_per_s || 0) + (sample.total_packets_recv_per_s || 0);
    const series = [
      [bwChart.data.datasets[0].data,   sample.mbps_in],
      [bwChart.data.datasets[1].data,   sample.mbps_out],
      [connChart.data.datasets[0].data, sample.connections_total ?? 0],
      [connChart.data.datasets[1].data, pkts],
    ];
    for (const [arr, y] of series) {
      arr.push({ x: ts, y });
      while (arr.length > MAX_POINTS) arr.shift();
    }
    bwChart.update("none");
    connChart.update("none");
  }

  // ---------- Sparklines & trend chips ----------
  // Tiny custom canvas line — we don't pull in a library for 36px-tall mini charts.
  function sparkColors() {
    return {
      in:    cssVar("--c-in")   || "#2c4159",
      out:   cssVar("--c-out")  || "#8b2e2a",
      conns: cssVar("--c-conn") || "#a37016",
      pkts:  cssVar("--c-pkt")  || "#4a6b3a",
    };
  }
  function drawSpark(canvasId, values, color) {
    const cv = $(canvasId);
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = cv.clientWidth || 200;
    const cssH = cv.clientHeight || 42;
    if (cv.width !== cssW * dpr || cv.height !== cssH * dpr) {
      cv.width = cssW * dpr; cv.height = cssH * dpr;
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    ctx.strokeStyle = cssVar("--rule-2") || "#e8e4d6";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, cssH - 0.5);
    ctx.lineTo(cssW, cssH - 0.5);
    ctx.stroke();

    if (!values || values.length < 2) return;

    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const range = (hi - lo) || 1;
    const pad = 5;
    const w = cssW;
    const h = cssH - pad * 2;

    const pts = values.map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = pad + (1 - (v - lo) / range) * h;
      return [x, y];
    });

    // Stroke only — no fill, no gradient. Print-figure aesthetic.
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.lineWidth = 1.3;
    ctx.strokeStyle = color;
    ctx.stroke();

    // Solid dot on the last sample so the eye knows which end is "now".
    const [lx, ly] = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(lx, ly, 2.2, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  function setTrend(id, current, previous) {
    const el = $(id);
    if (!el) return;
    if (previous == null || !isFinite(previous) || previous === 0) {
      el.textContent = "—";
      el.className = "k-trend mono mute";
      return;
    }
    const delta = current - previous;
    const pct = (delta / Math.max(Math.abs(previous), 1e-9)) * 100;
    if (Math.abs(pct) < 0.5) {
      el.textContent = "no change";
      el.className = "k-trend mono flat";
      return;
    }
    // Editorial: prose, not arrows.
    const dir  = delta > 0 ? "up" : "down";
    el.textContent = `${dir} ${Math.abs(pct).toFixed(1)}% vs avg`;
    el.className = `k-trend mono ${dir === "up" ? "up" : "down"}`;
  }

  function refreshSparklines() {
    if (state.paused) return;
    const recent = state.history.slice(-40);
    if (recent.length < 2) return;
    const C = sparkColors();
    drawSpark("spark-in",    recent.map((s) => s.mbps_in),       C.in);
    drawSpark("spark-out",   recent.map((s) => s.mbps_out),      C.out);
    drawSpark("spark-conns", recent.map((s) => s.connections),   C.conns);
    drawSpark("spark-pkts",  recent.map((s) => s.packets_per_s), C.pkts);

    // trend = current vs avg of the previous N-1 (excl. current)
    const last = recent[recent.length - 1];
    const prior = recent.slice(0, -1);
    const avg = (key) => prior.reduce((a, s) => a + (s[key] || 0), 0) / Math.max(prior.length, 1);
    setTrend("trend-mbps-in",  last.mbps_in,        avg("mbps_in"));
    setTrend("trend-mbps-out", last.mbps_out,       avg("mbps_out"));
    setTrend("trend-conns",    last.connections,    avg("connections"));
    setTrend("trend-pkts",     last.packets_per_s,  avg("packets_per_s"));
  }

  // ---------- DOM updates ----------
  function setText(id, text)         { const el = $(id); if (el) el.textContent = text; }
  function flash(id) {
    const el = $(id);
    if (!el) return;
    const wrap = el.parentElement;
    if (!wrap || !wrap.classList.contains("k-value")) return;
    wrap.classList.remove("flash"); void wrap.offsetWidth; wrap.classList.add("flash");
  }

  function updateKpis(sample, alertStats) {
    if (sample) {
      const mbpsIn  = (sample.mbps_in  ?? 0).toFixed(2);
      const mbpsOut = (sample.mbps_out ?? 0).toFixed(2);
      const conns   = sample.connections_total ?? 0;
      const pkts    = ((sample.total_packets_sent_per_s ?? 0)
                     + (sample.total_packets_recv_per_s ?? 0));
      if (mbpsIn  !== $("kpi-mbps-in").textContent)  flash("kpi-mbps-in");
      if (mbpsOut !== $("kpi-mbps-out").textContent) flash("kpi-mbps-out");
      setText("kpi-mbps-in",  mbpsIn);
      setText("kpi-mbps-out", mbpsOut);
      setText("kpi-conns",    fmtInt(conns));
      setText("kpi-pkts",     fmtInt(pkts));
      state.lastSample = sample;
      renderConnStates(sample.connections_by_state || {});
    }
    if (alertStats) {
      const fired = alertStats.fired_total ?? 0;
      const supp  = alertStats.suppressed_total ?? 0;
      if (String(fired) !== $("kpi-alerts").textContent) flash("kpi-alerts");
      setText("kpi-alerts",     fmtInt(fired));
      setText("kpi-suppressed", fmtInt(supp));
      state.alertsFired = fired;
      state.alertsSuppressed = supp;
    }
  }

  function renderConnStates(byState) {
    const ul = $("states-list");
    const entries = Object.entries(byState).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      ul.innerHTML = '<li class="empty"><em>Waiting for first sample…</em></li>';
      return;
    }
    const total = entries.reduce((acc, [, n]) => acc + n, 0) || 1;
    ul.innerHTML = "";
    for (const [name, count] of entries.slice(0, 10)) {
      const pct = (100 * count / total).toFixed(0);
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="state-name">${escapeHtml(name)}</span>
        <span class="state-bar"><span style="width:${pct}%"></span></span>
        <span class="state-count mono">${fmtInt(count)}</span>
      `;
      ul.appendChild(li);
    }
  }

  function renderConfig(cfg) {
    if (!cfg) return;
    const dl = $("config-dl");
    const rows = [
      ["Sample interval",  `${cfg.interval_seconds}s`],
      ["Window",           `${cfg.window_size} samples`],
      ["Interfaces",       (cfg.interfaces && cfg.interfaces.length) ? cfg.interfaces.join(", ") : "all (non-loopback)"],
      ["BW threshold in",  `${cfg.thresholds.bandwidth_mbps_in} Mbps`],
      ["BW threshold out", `${cfg.thresholds.bandwidth_mbps_out} Mbps`],
      ["Conns max",        fmtInt(cfg.thresholds.connections_total)],
      ["Z-score",          `> ${cfg.thresholds.zscore_threshold}σ`],
      ["Cooldown",         `${cfg.alerts.cooldown_seconds}s`],
      ["Handlers",         (cfg.alerts.handler_types || []).join(", ") || "(none)"],
    ];
    dl.innerHTML = "";
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.appendChild(dt); dl.appendChild(dd);
    }
  }

  function renderAlerts() {
    const ul = $("alert-list");
    const total = state.recentAnomalies.length;
    const visible = state.recentAnomalies.filter((a) => state.severityFilter[(a.severity || "info").toLowerCase()] !== false);
    const cnt = $("alert-count");
    if (cnt) cnt.textContent = `${visible.length}/${total}`;

    if (total === 0) {
      ul.innerHTML = `
        <li class="empty">
          <em>The feed is quiet.</em>
          <span>Press <strong>Simulate spike</strong> to fire the full pipeline end-to-end.</span>
        </li>`;
      return;
    }
    if (visible.length === 0) {
      ul.innerHTML = `
        <li class="empty">
          <em>${total} alert${total === 1 ? "" : "s"} hidden.</em>
          <span>Adjust the filters above to show alerts.</span>
        </li>`;
      return;
    }
    const items = visible.slice(-30).reverse();
    ul.innerHTML = "";
    for (const a of items) {
      const li = document.createElement("li");
      if (a._isNew) { li.classList.add("new"); a._isNew = false; }
      const ts = new Date((a.sample_ts || 0) * 1000).toLocaleTimeString();
      const sev = (a.severity || "info").toLowerCase();
      li.innerHTML = `
        <span class="sev sev-${escapeHtml(sev)}">${escapeHtml(sev)}</span>
        <span class="ts mono">${ts}</span>
        <span class="msg">${escapeHtml(a.message || "(no message)")}</span>
      `;
      ul.appendChild(li);
    }
  }

  function renderDiagnostics(checks) {
    const ul = $("diag-list");
    if (!checks || !checks.length) {
      ul.innerHTML = '<li class="empty"><em>No checks returned.</em></li>';
      return;
    }
    ul.innerHTML = "";
    for (const c of checks) {
      const li = document.createElement("li");
      const cls = c.ok ? "ok" : "fail";
      const tag = c.ok ? "OK" : "FAIL";
      li.innerHTML = `
        <span class="${cls}">${tag}</span>
        <span class="name">${escapeHtml(c.name)}</span>
        <span class="info">${escapeHtml(c.detail || "")}</span>
      `;
      ul.appendChild(li);
    }
  }

  // ---------- Status chip ----------
  function setConn(transport) {
    state.transport = transport;
    const pill  = $("conn-pill");
    const label = $("conn-label");
    pill.classList.remove("is-live", "is-warn", "is-down");
    if (transport === "ws")        { pill.classList.add("is-live"); label.textContent = "live"; }
    else if (transport === "poll") { pill.classList.add("is-warn"); label.textContent = "polling"; }
    else if (transport === "init") { label.textContent = "connecting"; }
    else                            { pill.classList.add("is-down"); label.textContent = "offline"; }
  }

  function tickStatusBar() {
    if (state.pid != null) setText("meta-pid", state.pid);
    setText("meta-samples", fmtInt(state.samplesProcessed));
    if (state.startedAt) {
      const sec = Math.max(0, Math.floor(Date.now() / 1000 - state.startedAt));
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      const parts = [];
      if (h) parts.push(`${h}h`);
      if (h || m) parts.push(`${m}m`);
      parts.push(`${s}s`);
      setText("meta-uptime", parts.join(" "));
    } else {
      setText("meta-uptime", "—");
    }
    if (state.lastSample) {
      const age = Math.max(0, Date.now() / 1000 - state.lastSample.ts);
      const fmt = age < 1 ? "just now" : `${age.toFixed(1)}s ago`;
      setText("meta-fresh", fmt);
      const pill = $("meta-fresh").parentElement;
      pill.style.color = age > FRESH_THRESHOLD_S ? "var(--amber)" : "";
    } else {
      setText("meta-fresh", "—");
    }
  }
  setInterval(tickStatusBar, 500);

  // ---------- Initial HTTP load ----------
  async function bootstrap() {
    setConn("init");
    try {
      const [cfg, status, history] = await Promise.all([
        fetch("/api/config").then((r) => r.json()),
        fetch("/api/status").then((r) => r.json()),
        fetch("/api/history").then((r) => r.json()),
      ]);
      state.config = cfg;
      state.pid              = status.pid              ?? state.pid;
      state.startedAt        = status.started_at       ?? state.startedAt;
      state.samplesProcessed = status.samples_processed ?? 0;
      state.recentAnomalies  = status.recent_anomalies || [];
      state.recentAnomalies.forEach((a) => state.seenAnomalyKeys.add(anomalyId(a)));
      state.history          = history.samples || [];

      renderConfig(cfg);
      updateKpis(status.last_sample || null, status.alert_stats || null);
      renderAlerts();
      replayHistoryToCharts();
      refreshSparklines();
      tickStatusBar();
    } catch (err) {
      console.error("initial load failed:", err);
      // Even on bootstrap failure, the rest of the UI still tries WS / poll.
    }
    connectWs();
  }

  // ---------- WebSocket ----------
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws`);
    } catch (e) {
      console.warn("WebSocket constructor failed:", e);
      startPolling();
      return;
    }
    state.ws = ws;

    ws.addEventListener("open", () => {
      setConn("ws");
      stopPolling();
      if (state.wsReconnectTimer) { clearTimeout(state.wsReconnectTimer); state.wsReconnectTimer = null; }
    });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleEvent(msg);
    });
    ws.addEventListener("close", () => {
      // The browser closes WS for many reasons (server restart, transient
      // network). Always retry, and start polling so the page never goes dark.
      setConn("poll");
      startPolling();
      state.wsReconnectTimer = setTimeout(connectWs, WS_RECONNECT_MS);
    });
    ws.addEventListener("error", () => {
      try { ws.close(); } catch {}
    });
  }

  function handleEvent(msg) {
    if (msg.type === "hello") {
      // We already loaded everything via HTTP; just refresh from authoritative WS state.
      if (msg.config) { state.config = msg.config; renderConfig(state.config); }
      if (msg.snapshot) {
        state.pid = msg.snapshot.pid;
        state.startedAt = msg.snapshot.started_at;
        state.samplesProcessed = msg.snapshot.samples_processed || state.samplesProcessed;
        const incoming = msg.snapshot.recent_anomalies || [];
        for (const a of incoming) {
          const id = anomalyId(a);
          if (!state.seenAnomalyKeys.has(id)) {
            state.seenAnomalyKeys.add(id);
            state.recentAnomalies.push(a);
          }
        }
        updateKpis(msg.snapshot.last_sample || null, msg.snapshot.alert_stats || null);
        renderAlerts();
      }
      if (msg.history && msg.history.length > state.history.length) {
        state.history = msg.history;
        replayHistoryToCharts();
        refreshSparklines();
      }
      return;
    }

    if (msg.type === "tick") {
      state.samplesProcessed = msg.samples_processed || (state.samplesProcessed + 1);

      // Process anomalies *first* so the chart's annotation plugin can draw
      // their markers in the same redraw.
      if (msg.anomalies && msg.anomalies.length) {
        for (const a of msg.anomalies) {
          const id = anomalyId(a);
          if (state.seenAnomalyKeys.has(id)) continue;
          state.seenAnomalyKeys.add(id);
          a._isNew = true;
          state.recentAnomalies.push(a);
        }
        if (state.recentAnomalies.length > 200) state.recentAnomalies = state.recentAnomalies.slice(-200);
        renderAlerts();
      }

      updateKpis(msg.sample, msg.alert_stats);
      pushSampleToCharts(msg.sample);

      if (msg.sample && msg.sample.interval > 0) {
        state.history.push({
          ts: msg.sample.ts,
          mbps_in: msg.sample.mbps_in,
          mbps_out: msg.sample.mbps_out,
          connections: msg.sample.connections_total,
          packets_per_s: (msg.sample.total_packets_sent_per_s || 0) + (msg.sample.total_packets_recv_per_s || 0),
          anomaly_count: (msg.anomalies || []).length,
        });
        if (state.history.length > MAX_POINTS) state.history.shift();
        refreshSparklines();
      }
    }
  }

  // ---------- HTTP polling fallback ----------
  function startPolling() {
    if (state.pollTimer) return;
    pollOnce();          // immediate
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  }
  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }
  async function pollOnce() {
    try {
      const [status, history] = await Promise.all([
        fetch("/api/status").then((r) => r.json()),
        fetch("/api/history").then((r) => r.json()),
      ]);
      state.pid = status.pid;
      state.startedAt = status.started_at;
      state.samplesProcessed = status.samples_processed || state.samplesProcessed;
      updateKpis(status.last_sample || null, status.alert_stats || null);

      // Merge in any new anomalies the snapshot exposes.
      const incoming = status.recent_anomalies || [];
      let added = false;
      for (const a of incoming) {
        const id = anomalyId(a);
        if (!state.seenAnomalyKeys.has(id)) {
          state.seenAnomalyKeys.add(id);
          a._isNew = true;
          state.recentAnomalies.push(a);
          added = true;
        }
      }
      if (added) renderAlerts();

      if ((history.samples || []).length) {
        state.history = history.samples;
        replayHistoryToCharts();
        refreshSparklines();
      }
    } catch (e) {
      console.warn("poll failed:", e);
      setConn("down");
    }
  }

  // ---------- Buttons ----------
  async function runDiagnostics() {
    const btn = $("btn-diag");
    const list = $("diag-list");
    btn.disabled = true;
    list.innerHTML = '<li class="empty"><em>Running checks…</em></li>';
    try {
      const data = await fetch("/api/diagnostics", { method: "POST" }).then((r) => r.json());
      renderDiagnostics(data.checks || []);
    } catch (e) {
      list.innerHTML = `<li class="empty"><em>Failed.</em><span>${escapeHtml(String(e))}</span></li>`;
    } finally {
      btn.disabled = false;
    }
  }

  async function simulateSpike() {
    const btn = $("btn-simulate");
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Injecting…";
    try {
      await fetch("/api/simulate", { method: "POST" });
    } catch (e) {
      console.error("simulate failed:", e);
    } finally {
      setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 700);
    }
  }

  // ---------- Helpers ----------
  function anomalyId(a) {
    return `${a.sample_ts || 0}|${a.key || a.metric || ""}|${a.message || ""}`;
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function fmtInt(n) {
    n = Number(n) || 0;
    return n.toLocaleString();
  }

  // ---------- Theme ----------
  function applyTheme(name) {
    const next = name === "dark" ? "dark" : "light";
    state.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    // swap toolbar icon visibility
    const moon = $("ico-moon"), sun = $("ico-sun");
    if (moon && sun) {
      const dark = next === "dark";
      moon.hidden = dark;
      sun.hidden = !dark;
    }
    rethemeCharts();
  }
  function toggleTheme() {
    applyTheme(state.theme === "dark" ? "light" : "dark");
  }
  function loadTheme() {
    let pref;
    try { pref = localStorage.getItem(THEME_KEY); } catch {}
    if (pref !== "light" && pref !== "dark") {
      pref = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    applyTheme(pref);
  }

  // ---------- Pause ----------
  function setPaused(p) {
    state.paused = !!p;
    document.body.classList.toggle("is-paused", state.paused);
    const btn   = $("btn-pause");
    const icoP  = $("ico-pause");
    const icoPL = $("ico-play");
    if (btn)   btn.classList.toggle("is-active", state.paused);
    if (icoP && icoPL) {
      icoP.hidden  = state.paused;
      icoPL.hidden = !state.paused;
    }
    if (!state.paused) {
      // Replay buffered history so charts catch up to "now".
      replayHistoryToCharts();
      refreshSparklines();
    }
  }

  // ---------- Severity filter ----------
  function bindFilterChips() {
    document.querySelectorAll(".filter-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const sev = chip.dataset.sev;
        const next = !chip.classList.contains("is-on");
        chip.classList.toggle("is-on", next);
        state.severityFilter[sev] = next;
        renderAlerts();
      });
    });
  }
  function toggleSeverity(sev) {
    const chip = document.querySelector(`.filter-chip[data-sev="${sev}"]`);
    if (chip) chip.click();
  }

  // ---------- Help ----------
  function setHelp(open) {
    const ov = $("help-overlay");
    if (!ov) return;
    ov.classList.toggle("is-open", !!open);
  }

  // ---------- Live clock ----------
  function tickClock() {
    const el = $("clock");
    if (!el) return;
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    el.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  setInterval(tickClock, 1000);

  // ---------- Keyboard ----------
  function bindShortcuts() {
    document.addEventListener("keydown", (e) => {
      // Ignore when the user is typing somewhere editable.
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.key === "?" || (e.shiftKey && e.key === "/")) { setHelp(true);  return e.preventDefault(); }
      if (e.key === "Escape") { setHelp(false); return; }

      const k = e.key.toLowerCase();
      if (k === "t")     { toggleTheme();         return e.preventDefault(); }
      if (k === "s")     { simulateSpike();       return e.preventDefault(); }
      if (k === "r")     { runDiagnostics();      return e.preventDefault(); }
      if (e.key === " ") { setPaused(!state.paused); return e.preventDefault(); }
      if (k === "1")     { toggleSeverity("info");     return e.preventDefault(); }
      if (k === "2")     { toggleSeverity("warning");  return e.preventDefault(); }
      if (k === "3")     { toggleSeverity("critical"); return e.preventDefault(); }
    });
  }

  // ---------- Init ----------
  function init() {
    loadTheme();
    initCharts();
    bindFilterChips();
    bindShortcuts();
    tickClock();

    $("btn-diag").addEventListener("click", runDiagnostics);
    $("btn-simulate").addEventListener("click", simulateSpike);
    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-pause").addEventListener("click", () => setPaused(!state.paused));
    $("btn-help").addEventListener("click",  (e) => { e.stopPropagation(); setHelp(true);  });
    $("btn-help-close").addEventListener("click", (e) => { e.stopPropagation(); setHelp(false); });
    // Click on the dimmed backdrop (but not inside the dialog itself) closes.
    $("help-overlay").addEventListener("click", (e) => {
      if (e.target.id === "help-overlay") setHelp(false);
    });

    // Re-theme charts if the OS theme changes and the user hasn't pinned one.
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
        let pinned;
        try { pinned = localStorage.getItem(THEME_KEY); } catch {}
        if (pinned !== "light" && pinned !== "dark") applyTheme(e.matches ? "dark" : "light");
      });
    }

    bootstrap();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
