/* Gufo Dashboard front-end. No build step; talks only to /api/*. */
(() => {
  "use strict";

  const STATUS_MS = 3000;
  const DATA_MS = 10000;
  const FEED_MS = 3000;
  const $ = (id) => document.getElementById(id);

  const store = {
    get(k, d) { try { return localStorage.getItem("gd." + k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("gd." + k, v); } catch { /* ignore */ } },
  };

  const state = {
    range: store.get("range", "24h"),
    model: null, // null = not chosen yet (defaults to current model)
    filter: store.get("filter", "all"),
    metric: store.get("metric", "decode_tps"),
    insight: store.get("insight", "spec"),
    paused: false,
    status: null,
    summary: null,
    selectedId: null,
    feed: { items: [], newest: null, oldest: null, done: false, loading: false, pendingNew: 0 },
    inflightReq: 0,
    lastUpdated: null,
    refreshErrors: new Set(),
    dataGeneration: 0,
  };

  // ------------------------------------------------------------------ utils
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const DASH = "—";
  const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const fInt = (v) => (isNum(v) ? nf0.format(v) : DASH);
  const fTps = (v) => (isNum(v) ? nf1.format(v) : DASH);
  const fPct = (v, d = 1) => (isNum(v) ? (d ? nf1 : nf0).format(v * 100) + "%" : DASH);
  function fCompact(v) {
    if (!isNum(v)) return DASH;
    const a = Math.abs(v);
    if (a >= 1e9) return nf2.format(v / 1e9) + "B";
    if (a >= 1e6) return nf2.format(v / 1e6) + "M";
    if (a >= 1e4) return nf1.format(v / 1e3) + "k";
    return nf0.format(v);
  }
  function fMs(v) {
    if (!isNum(v)) return DASH;
    if (v < 10) return nf2.format(v) + " ms";
    if (v < 1000) return nf0.format(v) + " ms";
    if (v < 60000) return nf2.format(v / 1000) + " s";
    return fDur(v);
  }
  function fDur(ms) {
    if (!isNum(ms)) return DASH;
    let s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  }
  function fBytes(v) {
    if (!isNum(v)) return DASH;
    if (v >= 1 << 30) return nf2.format(v / (1 << 30)) + " GiB";
    if (v >= 1 << 20) return nf1.format(v / (1 << 20)) + " MiB";
    if (v >= 1 << 10) return nf1.format(v / (1 << 10)) + " KiB";
    return nf0.format(v) + " B";
  }
  const pad = (n) => String(n).padStart(2, "0");
  function fTime(ms) {
    const d = new Date(ms);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function fDateTime(ms) {
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${fTime(ms)}`;
  }
  function fAxisTime(ms, spanMs) {
    const d = new Date(ms);
    if (spanMs > 3 * 86400000) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    if (spanMs > 86400000) return `${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const ENDPOINT_SHORT = {
    "/v1/chat/completions": "chat",
    "/v1/completions": "completions",
    "/v1/responses": "responses",
    "/v1/messages": "messages",
    "/completion": "completion",
  };

  // ------------------------------------------------------------------- fetch
  const refreshInd = $("refresh-ind");
  function renderFreshness() {
    const failed = state.refreshErrors.size > 0;
    const age = state.lastUpdated ? `Updated ${fDur(Date.now() - state.lastUpdated)} ago` : "Waiting for data";
    $("refresh-text").textContent = state.paused ? `Paused · ${age}` : failed ? `Refresh failed · ${age}` : age;
    $("refresh-text").classList.toggle("warn", failed || state.paused);
    $("retry-refresh").hidden = !failed;
    refreshInd.classList.toggle("err", failed);
    refreshInd.title = failed ? "Some data could not be refreshed; displayed values may be stale." : age;
  }
  function setBusy(delta) {
    state.inflightReq += delta;
    refreshInd.classList.toggle("busy", state.inflightReq > 0);
  }
  async function api(path, opts) {
    const resource = path.split("?")[0];
    const monitored = !resource.startsWith("/api/requests/");
    setBusy(1);
    try {
      const r = await fetch(path, opts);
      if (!r.ok) throw new Error(`${r.status}`);
      const data = await r.json();
      if (monitored) state.refreshErrors.delete(resource);
      return data;
    } catch (e) {
      if (monitored) state.refreshErrors.add(resource);
      throw e;
    } finally {
      setBusy(-1);
      renderFreshness();
    }
  }
  function qs(extra) {
    const p = new URLSearchParams({ range: state.range, ...extra });
    if (state.model) p.set("model", state.model);
    return p.toString();
  }

  // ------------------------------------------------------------------ header
  const proxyUrl = `${location.protocol}//${location.host}/v1`;
  $("proxy-url").textContent = proxyUrl;
  $("copy-url").addEventListener("click", async () => {
    const btn = $("copy-url");
    try {
      await navigator.clipboard.writeText(proxyUrl);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = proxyUrl; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      ta.remove();
    }
    btn.classList.add("copied");
    setTimeout(() => btn.classList.remove("copied"), 1200);
  });

  function renderStatus(s) {
    const dot = $("status-dot");
    dot.className = "dot " + (s.online ? "dot-ok" : "dot-bad");
    $("status-text").textContent = s.online ? "online" : "offline";
    $("status-text").className = s.online ? "" : "muted";
    $("cur-model").textContent = s.model || DASH;
    $("cur-model").title = s.model || "";
    $("cur-ctx").textContent = isNum(s.context_length) ? fInt(s.context_length) : DASH;
    $("ready-for").textContent = isNum(s.observed_ready_ms) ? fDur(s.observed_ready_ms) : DASH;

    const inf = s.in_flight || { count: 0, items: [] };
    const scoped = state.model ? (inf.per_model?.[state.model] || 0) : inf.count;
    setCard("inflight", fInt(scoped), inf.items.length ? `oldest ${fDur(Math.max(...inf.items.map((i) => i.elapsed_ms)))}` : state.model && inf.count !== scoped ? `${inf.count} total` : "idle");
    if (state.insight === "gufo") renderInsights();
  }

  // ------------------------------------------------------------------- cards
  function setCard(key, value, sub, cls) {
    const v = $("c-" + key);
    v.innerHTML = value;
    v.className = "value num" + (cls ? " " + cls : "");
    $("c-" + key + "-sub").innerHTML = sub || "&nbsp;";
  }
  const unit = (u) => `<span class="unit">${u}</span>`;

  function pctLabel(s) {
    if (state.range !== "all" || !s.percentile_window_start_ms) return "";
    const days = Math.max(1, Math.ceil((Date.now() - s.percentile_window_start_ms) / 86400000));
    return ` (last ${days}d)`;
  }

  function renderCards(s) {
    setCard("requests", fInt(s.requests), `${fInt(s.streaming)} stream · ${fInt(s.vision)} vision`);
    const errCls = s.errors > 0 ? "bad" : "";
    setCard("errors", s.requests ? fPct(s.error_rate) : DASH, `${fInt(s.errors)} errors · ${fInt(s.cancelled)} cancelled`, errCls);
    setCard("gen", fCompact(s.generated_tokens), `reasoning ${fCompact(s.reasoning_tokens)}`);
    setCard("prompt", fCompact(s.prompt_tokens), `cached ${fCompact(s.cached_tokens)}`);

    $("model-hint").hidden = !!state.model;
    if (!state.model) {
      $("c-ttft").parentElement.querySelector(".label").textContent = "TTFT p50";
      for (const k of ["decode", "prefill", "ttft", "cache", "draft"]) setCard(k, DASH, "", "na");
      return;
    }
    const pl = pctLabel(s);
    const cov = (n, of) => (isNum(n) && isNum(of) ? ` · n=${fInt(n)}/${fInt(of)}` : "");
    setCard("decode", isNum(s.decode_tps_weighted) ? fTps(s.decode_tps_weighted) + unit("tok/s") : DASH,
      isNum(s.decode_tps_p50) ? `p50 ${fTps(s.decode_tps_p50)}${pl}${cov(s.decode_n, s.requests)}` : `no decode data${cov(s.decode_n, s.requests)}`,
      isNum(s.decode_tps_weighted) ? "" : "na");
    setCard("prefill", isNum(s.prefill_tps_weighted) ? fTps(s.prefill_tps_weighted) + unit("tok/s") : DASH,
      isNum(s.prefill_tps_p50) ? `p50 ${fTps(s.prefill_tps_p50)}${pl}${cov(s.prefill_n, s.requests)}` : `no prefill data${cov(s.prefill_n, s.requests)}`,
      isNum(s.prefill_tps_weighted) ? "" : "na");
    let ttftSub = isNum(s.ttft_p95_ms) ? `p95 ${fMs(s.ttft_p95_ms)}${pl}` : "no TTFT data";
    ttftSub += cov(s.ttft_n, s.requests);
    if (s.ttft_proxy_stream_n && s.ttft_proxy_stream_n < s.ttft_n) ttftSub += ` · ${fPct(s.ttft_proxy_stream_n / s.ttft_n, 0)} proxy`;
    else if (s.ttft_proxy_stream_n && s.ttft_proxy_stream_n === s.ttft_n) ttftSub += " · proxy-measured";
    $("c-ttft").parentElement.querySelector(".label").textContent = "TTFT p50" + pl;
    setCard("ttft", fMs(s.ttft_p50_ms), ttftSub, isNum(s.ttft_p50_ms) ? "" : "na");
    setCard("cache", fPct(s.cache_hit_rate),
      s.cache_known_n ? `${fInt(s.cache_hits)} hits${cov(s.cache_known_n, s.requests)}` : `no cache data${cov(s.cache_known_n, s.requests)}`,
      isNum(s.cache_hit_rate) ? "" : "na");
    setCard("draft", fPct(s.draft_acceptance),
      s.draft_tokens ? `${fCompact(s.draft_accepted)}/${fCompact(s.draft_tokens)}${cov(s.draft_requests, s.requests)}` : `no draft data${cov(s.draft_requests, s.requests)}`,
      isNum(s.draft_acceptance) ? "" : "na");
  }

  // ------------------------------------------------------------------ charts
  const css = getComputedStyle(document.documentElement);
  const cv = (n) => css.getPropertyValue(n).trim();
  const SERIES = [cv("--accent"), cv("--series-2"), cv("--series-3"), cv("--warn"), cv("--ok")];
  Chart.defaults.color = cv("--muted");
  Chart.defaults.font.family = cv("--sans");
  Chart.defaults.font.size = 11;
  Chart.defaults.borderColor = "rgba(152,163,179,.10)";
  Chart.defaults.animation = false;

  let lastSpan = 3600000;
  const timeAxis = () => ({
    type: "linear",
    grid: { display: false },
    ticks: { maxTicksLimit: 7, maxRotation: 0, callback: (v) => fAxisTime(v, lastSpan) },
  });
  const tooltipTitle = (items) => (items.length ? fDateTime(items[0].parsed.x) : "");

  const primary = new Chart($("chart-primary"), {
    data: { datasets: [
      { type: "bar", label: "requests", data: [], yAxisID: "y", backgroundColor: "rgba(106,169,255,0.45)", borderWidth: 0, barPercentage: 1, categoryPercentage: 0.9 },
      { type: "line", label: "generated tokens", data: [], yAxisID: "y1", borderColor: cv("--series-2"), backgroundColor: cv("--series-2"), borderWidth: 2, pointRadius: 1.5, tension: 0.2, spanGaps: true },
    ] },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: tooltipTitle } } },
      scales: {
        x: timeAxis(),
        y: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 }, title: { display: false } },
        y1: { beginAtZero: true, position: "right", grid: { display: false }, ticks: { maxTicksLimit: 5, callback: (v) => fCompact(v) } },
      },
    },
  });

  const METRIC_FMT = {
    decode_tps: { label: "Weighted decode per bucket", fmt: fTps },
    prefill_tps: { label: "Weighted prefill per bucket", fmt: fTps },
    ttft_p50_ms: { label: "TTFT p50", fmt: fMs },
    draft_acceptance: { label: "draft acceptance", fmt: (v) => fPct(v), pct: true },
    cache_hit_rate: { label: "cache hit rate", fmt: (v) => fPct(v), pct: true },
  };
  const secondary = new Chart($("chart-secondary"), {
    type: "line",
    data: { datasets: [] },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        title: tooltipTitle,
        label: (c) => `${c.dataset.label}: ${METRIC_FMT[state.metric].fmt(c.parsed.y)}`,
        afterLabel: () => /tps$/.test(state.metric) ? "1000 × Σ tokens / Σ ms in this bucket" : "",
      } } },
      scales: { x: timeAxis(), y: { beginAtZero: true, ticks: { maxTicksLimit: 5 } } },
    },
  });

  let lastTs = null;
  function renderCharts(ts) {
    lastTs = ts;
    lastSpan = ts.end_ms - ts.start_ms;
    const half = ts.bucket_ms / 2;
    const xs = ts.t.map((t) => t + half);
    primary.data.datasets[0].data = xs.map((x, i) => ({ x, y: ts.requests[i] }));
    primary.data.datasets[1].data = xs.map((x, i) => ({ x, y: ts.requests[i] ? ts.generated_tokens[i] : null }));
    for (const ax of [primary.options.scales.x, secondary.options.scales.x]) {
      ax.min = ts.start_ms; ax.max = ts.end_ms;
    }
    primary.update();
    renderSecondary();
  }
  function renderSecondary() {
    if (!lastTs) return;
    const m = METRIC_FMT[state.metric];
    const half = lastTs.bucket_ms / 2;
    const names = Object.keys(lastTs.per_model).sort();
    secondary.data.datasets = names.map((name, i) => ({
      label: name,
      data: lastTs.t.map((t, j) => ({ x: t + half, y: lastTs.per_model[name][state.metric][j] })),
      borderColor: SERIES[i % SERIES.length],
      backgroundColor: SERIES[i % SERIES.length],
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 4,
      pointHitRadius: 10,
      spanGaps: false,
      tension: 0.15,
    }));
    secondary.options.scales.y.ticks.callback = m.pct ? (v) => Math.round(v * 100) + "%" : (v) => (m.fmt === fMs ? fMs(v) : fCompact(v));
    secondary.options.scales.y.max = m.pct ? 1 : undefined;
    secondary.update();
    const legend = $("secondary-legend");
    legend.innerHTML = names.length > 1 || !state.model
      ? names.map((n, i) => `<i class="sw" style="background:${SERIES[i % SERIES.length]}"></i>${esc(n.length > 24 ? n.slice(0, 22) + "…" : n)}`).join(" ")
      : `<span>${esc(m.label)}${lastTs.source === "rollup" ? " · daily" : ""}</span>`;
  }

  function setupTabs(containerId, attr, key, onChange) {
    const box = $(containerId);
    const isTablist = box.getAttribute("role") === "tablist";
    const panel = containerId === "chart-tabs" ? $("chart-secondary").parentElement : containerId === "insight-tabs" ? $("insight-body") : null;
    if (panel) { panel.id ||= `${containerId}-panel`; panel.setAttribute("role", "tabpanel"); }
    const sync = () => box.querySelectorAll("button").forEach((b) => {
      const on = b.dataset[attr] === state[key];
      b.classList.toggle("on", on);
      b.setAttribute(isTablist ? "aria-selected" : "aria-pressed", String(on));
      if (isTablist) {
        b.tabIndex = on ? 0 : -1;
        b.id ||= `${containerId}-${b.dataset[attr]}`;
        if (panel) {
          b.setAttribute("aria-controls", panel.id);
          if (on) panel.setAttribute("aria-labelledby", b.id);
        }
      }
    });
    if (isTablist) box.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      const buttons = [...box.querySelectorAll("button")];
      const index = buttons.indexOf(document.activeElement);
      if (index < 0) return;
      e.preventDefault();
      const next = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : (index + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus(); buttons[next].click();
    });
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state[key] = b.dataset[attr];
      store.set(key, state[key]);
      sync();
      onChange();
    });
    sync();
  }
  setupTabs("chart-tabs", "metric", "metric", renderSecondary);

  // ---------------------------------------------------------------- insights
  let spec = null, cache = null;
  function kvRows(rows) {
    return `<table class="kvt">${rows.map(([k, v, title]) => `<tr${title ? ` title="${esc(title)}"` : ""}><th>${esc(k)}</th><td>${v}</td></tr>`).join("")}</table>`;
  }
  function modelHead(name, many) {
    return many ? `<div class="model-h" title="${esc(name)}">${esc(name || "(unknown model)")}</div>` : "";
  }
  function renderInsights() {
    const body = $("insight-body");
    if (state.insight === "spec") {
      if (!spec) { body.innerHTML = ""; return; }
      const list = spec.per_model;
      if (!list.length) { body.innerHTML = `<p class="note">No requests in this range.</p>`; return; }
      body.innerHTML = list.map((m) => modelHead(m.model, list.length > 1) + kvRows([
        ["Draft acceptance", fPct(m.draft_acceptance)],
        ["Drafted tokens", fInt(m.draft_tokens)],
        ["Accepted tokens", fInt(m.draft_accepted)],
        ["Requests with draft stats", `n=${fInt(m.draft_requests)}/${fInt(m.requests)}`],
        ["Weighted decode tok/s", fTps(m.decode_tps_weighted)],
      ])).join("") + `<p class="note">Acceptance = Σ accepted / Σ drafted, per model.</p>`;
    } else if (state.insight === "cache") {
      if (!cache) { body.innerHTML = ""; return; }
      const list = cache.per_model;
      if (!list.length) { body.innerHTML = `<p class="note">No requests in this range.</p>`; return; }
      body.innerHTML = list.map((m) => {
        const reasons = Object.entries(m.miss_reasons || {}).sort((a, b) => b[1] - a[1]);
        return modelHead(m.model, list.length > 1) + kvRows([
          ["Hit rate", `${fPct(m.hit_rate)}`],
          ["Hits / misses", `${fInt(m.hits)} / ${fInt(m.misses)}`],
          ["Requests with cache data", `n=${fInt(m.known_n)}/${fInt(m.requests)}`],
          ["Cached tokens", fInt(m.cached_tokens)],
          ["Cached-token share", fPct(m.cached_token_share)],
          ["Avg restore (hits)", fMs(m.restore_avg_ms)],
          ["Saved prefill (estimate)", isNum(m.saved_prefill_s_estimate) ? fMs(m.saved_prefill_s_estimate * 1000) : DASH,
            "Σ cached tokens ÷ this model's weighted prefill tok/s. An estimate."],
        ]) + (reasons.length ? `<h3 class="insight-subhead">MISS REASONS</h3>${kvRows(reasons.map(([r, n]) => [r, fInt(n)]))}` : "");
      }).join("") + `<p class="note">Saved prefill is an estimate.${state.range === "all" ? " Miss reasons cover retained rows only." : ""}</p>`;
    } else {
      const s = state.status;
      if (!s) { body.innerHTML = ""; return; }
      const c = s.counters || {};
      const u = s.unattributed || {};
      const p = s.pipeline || {};
      let un = DASH, unNote = "";
      if (!u.available) unNote = "Waiting for an idle moment to take a baseline.";
      else if (s.in_flight.count > 0) unNote = "Hidden while requests are in flight.";
      else if (u.visible) {
        un = `${fInt(u.prompt_tokens)} prompt / ${fInt(u.completion_tokens)} gen`;
        unNote = "Likely direct :8080 traffic. Other explanations: dropped or failed stats rows, cancelled requests, requests still in flight.";
      } else un = "none";
      body.innerHTML = kvRows([
        ["prompt_tokens_total", fInt(c.prompt)],
        ["tokens_predicted_total", fInt(c.predicted)],
        ["Last request prefill tok/s (Gufo)", fTps(s.gauges?.last_request_prefill_tps)],
        ["Last request decode tok/s (Gufo)", fTps(s.gauges?.last_request_decode_tps)],
        ["Unattributed Gufo tokens", un, u.available ? `since ${fDateTime(u.baseline_ms)}` : ""],
        ["In flight", fInt(s.in_flight.count)],
        ["Baseline", u.available ? esc(fDateTime(u.baseline_ms)) : "waiting for idle"],
        ["Cancelled (since baseline)", fInt(u.cancelled_in_window)],
        ["Stats dropped (queue full)", fInt(p.stats_dropped_queue_full)],
        ["Extraction errors", fInt(p.stats_extraction_errors)],
        ["Write errors", fInt(p.stats_write_errors)],
        ["Rows written", fInt(p.stats_written)],
      ]) + (unNote ? `<p class="note${u.visible ? " warn" : ""}">${esc(unNote)}</p>` : "")
        + `<div class="insight-actions"><button type="button" class="btn danger" id="clear-btn">Clear stats…</button></div>`;
      $("clear-btn").addEventListener("click", () => $("clear-dialog").showModal());
    }
  }
  setupTabs("insight-tabs", "tab", "insight", renderInsights);

  $("clear-dialog").addEventListener("close", async () => {
    if ($("clear-dialog").returnValue !== "clear") return;
    try {
      await api("/api/stats/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: "clear" }) });
    } catch { /* indicator shows error */ }
    resetFeed();
    refreshAll();
  });

  // -------------------------------------------------------------------- feed
  const feedList = $("feed-list");
  function statusDot(r) {
    if (r.finish_reason === "client_cancelled") return `<span class="dot dot-warn" role="img" aria-label="Cancelled by client" title="cancelled by client"></span>`;
    if ((r.http_status || 0) >= 400 || r.error_code) return `<span class="dot dot-bad" role="img" aria-label="HTTP ${esc(r.http_status)} error ${esc(r.error_code || "")}" title="${esc(r.http_status)} ${esc(r.error_code || "")}"></span>`;
    return `<span class="dot dot-ok" role="img" aria-label="HTTP ${esc(r.http_status || "unknown")} success" title="${esc(r.http_status)}"></span>`;
  }
  const EVENT_TEXT = {
    gufo_up: ["up", "ev-up", () => "Gufo ready"],
    gufo_down: ["down", "ev-down", (e) => "Gufo unreachable / not ready" + (e.detail ? ` (${e.detail})` : "")],
    model_changed: ["model", "ev-info", (e) => `Model changed to ${e.model}` + (e.detail ? ` (was ${e.detail})` : "")],
    counter_reset: ["reset", "ev-warn", () => "Gufo counters reset (restart)"],
    stats_cleared: ["cleared", "ev-warn", () => "Stats cleared"],
  };
  function rowHtml(it) {
    if (it.type === "event") {
      const [tag, cls, text] = EVENT_TEXT[it.kind] || [it.kind, "ev-info", () => it.kind];
      return `<div class="feed-row event" role="listitem" data-cursor="${esc(it.cursor)}"><span class="t num">${fTime(it.ts)}</span>`
        + `<span class="what"><span class="ev-kind ${cls}">${esc(tag)}</span><span class="mdl" title="${esc(text(it))}">${esc(text(it))}</span></span><span></span><span></span><span></span><span></span></div>`;
    }
    const badges = [];
    if (it.is_streaming) badges.push(`<span class="badge">STREAM</span>`);
    if (it.is_vision) badges.push(`<span class="badge" title="${esc(it.image_count)} image(s)">VISION</span>`);
    if (it.cache_hit === 1) badges.push(`<span class="badge hit">HIT</span>`);
    else if (it.cache_hit === 0) badges.push(`<span class="badge miss">MISS</span>`);
    if (it.error_code) badges.push(`<span class="badge err">${esc(it.error_code)}</span>`);
    const ep = ENDPOINT_SHORT[it.endpoint] || it.endpoint;
    const tok = isNum(it.prompt_tokens) || isNum(it.completion_tokens)
      ? `${fCompact(it.prompt_tokens)}<span class="arrow">→</span>${fCompact(it.completion_tokens)}` : DASH;
    return `<div role="listitem"><button type="button" class="feed-row${it.id === state.selectedId ? " sel" : ""}" title="${esc(it.model || "unknown model")} · HTTP ${esc(it.http_status || "unknown")}" data-id="${it.id}" data-cursor="${esc(it.cursor)}" aria-pressed="${it.id === state.selectedId}" aria-label="Inspect ${esc(ep)} request ${it.id}, ${esc(it.model || "unknown model")}, ${fDateTime(it.completed_at_ms)}, ${fMs(it.total_request_ms)}, HTTP ${esc(it.http_status || "unknown")}" tabindex="-1">`
      + `<span class="t num">${fTime(it.completed_at_ms)}</span>`
      + `<span class="what"><span class="ep">${esc(ep)}</span>${badges.join("")}${!state.model ? `<span class="mdl" title="${esc(it.model)}">${esc(it.model || "")}</span>` : ""}</span>`
      + `<span class="tok">${tok}</span>`
      + `<span class="r c-tps">${fTps(it.completion_tokens_per_second)}</span>`
      + `<span class="r c-dur">${fMs(it.total_request_ms)}</span>`
      + statusDot(it) + `</button></div>`;
  }
  function renderFeed() {
    const f = state.feed;
    $("feed-endpoint-label").textContent = state.model ? "endpoint" : "endpoint · model";
    feedList.classList.toggle("all-models", !state.model);
    let html = "";
    if (f.pendingNew > 0) html += `<button type="button" class="feed-new" id="feed-new">Show ${f.pendingNew} new entries</button>`;
    html += f.items.map(rowHtml).join("");
    if (f.error) html += `<p class="empty">Activity could not be refreshed. <button class="btn" id="retry-feed" type="button">Retry activity</button></p>`;
    else if (!f.items.length && f.done) {
      const filtered = state.filter !== "all" || !!state.model;
      const filterLabel = { errors: "errors", vision: "vision requests", streaming: "streaming requests", all: "requests", content: "requests with content", partial: "incomplete captures" }[state.filter];
      html += filtered ? `<p class="empty">No ${filterLabel} match ${state.model ? "this model and filter" : "this filter"}.<br>Try another model or activity filter.</p>` : `<p class="empty">No activity yet.<br>Send requests to <code>${esc(proxyUrl)}</code> to start recording.</p>`;
    }
    else if (f.loading) html += `<div class="feed-more">loading…</div>`;
    else if (f.done) html += `<div class="feed-more">end of retained history</div>`;
    const hadFocus = feedList.contains(document.activeElement);
    const focusedId = document.activeElement?.dataset?.id;
    feedList.innerHTML = html;
    if (hadFocus) (focusedId ? feedList.querySelector(`[data-id="${focusedId}"]`) : null)?.focus({ preventScroll: true });
    if (hadFocus && !feedList.contains(document.activeElement)) feedList.focus({ preventScroll: true });
  }
  function feedParams(extra) {
    const p = new URLSearchParams({ filter: state.filter, ...extra });
    if (state.model) p.set("model", state.model);
    return p.toString();
  }
  function resetFeed() {
    closeDetail(false);
    state.feed = { items: [], newest: null, oldest: null, done: false, loading: false, pendingNew: 0 };
    feedList.scrollTop = 0;
    loadOlder();
  }
  async function loadOlder() {
    const f = state.feed;
    if (f.loading || f.done) return;
    f.loading = true;
    renderFeed();
    const gen = f;
    try {
      const extra = { limit: 100 };
      if (f.oldest) extra.before = f.oldest;
      const res = await api("/api/activity?" + feedParams(extra));
      if (gen !== state.feed) return;
      f.error = false;
      f.items.push(...res.items);
      if (res.items.length) {
        f.oldest = res.items[res.items.length - 1].cursor;
        if (!f.newest) f.newest = res.items[0].cursor;
      }
      if (!res.has_more) f.done = true;
    } catch { f.error = true; } finally {
      f.loading = false;
      if (gen === state.feed) renderFeed();
    }
  }
  async function loadNewer() {
    const f = state.feed;
    if (f.loading) return;
    if (!f.newest) { if (f.done || !f.items.length) { f.done = false; return loadOlder(); } return; }
    const gen = f;
    let res;
    try {
      do {
        res = await api("/api/activity?" + feedParams({ limit: 200, after: f.newest }));
        f.error = false;
        if (gen !== state.feed || !res.items.length) break;
        f.newest = res.items[0].cursor;
        const atTop = feedList.scrollTop < 8;
        f.items.unshift(...res.items);
        if (!atTop) f.pendingNew += res.items.length;
      } while (res.has_more);
    } catch { if (gen === state.feed) { f.error = true; renderFeed(); } return; }
    if (gen === state.feed && res) {
      const prevH = feedList.scrollHeight, prevTop = feedList.scrollTop;
      renderFeed();
      if (prevTop >= 8) feedList.scrollTop = prevTop + (feedList.scrollHeight - prevH);
    }
  }
  feedList.addEventListener("scroll", () => {
    if (feedList.scrollTop + feedList.clientHeight > feedList.scrollHeight - 200) loadOlder();
    if (feedList.scrollTop < 8 && state.feed.pendingNew) { state.feed.pendingNew = 0; $("feed-new")?.remove(); }
  });
  feedList.addEventListener("click", (e) => {
    if (e.target.id === "retry-feed") { state.feed.done = false; loadOlder(); return; }
    if (e.target.id === "feed-new") { state.feed.pendingNew = 0; feedList.scrollTop = 0; renderFeed(); return; }
    const row = e.target.closest(".feed-row[data-id]");
    if (!row) return;
    select(Number(row.dataset.id));
  });
  feedList.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target === feedList) {
      const first = state.feed.items.find((i) => i.type === "request");
      if (first) { e.preventDefault(); select(first.id); }
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const reqs = state.feed.items.filter((i) => i.type === "request");
    if (!reqs.length) return;
    e.preventDefault();
    let idx = reqs.findIndex((r) => r.id === state.selectedId);
    idx = idx < 0 ? 0 : Math.min(reqs.length - 1, Math.max(0, idx + (e.key === "ArrowDown" ? 1 : -1)));
    select(reqs[idx].id);
    feedList.querySelector(`.feed-row[data-id="${reqs[idx].id}"]`)?.scrollIntoView({ block: "nearest" });
  });
  setupTabs("feed-filter", "filter", "filter", resetFeed);

  // ------------------------------------------------------------------ detail
  const detail = $("detail");
  function closeDetail(returnFocus = true) {
    state.selectedId = null;
    $("detail-tabs").hidden = true;
    $("content-body").hidden = true;
    $("content-body").replaceChildren();
    $("detail-body").hidden = false;
    $("detail-id").textContent = "";
    $("detail-body").innerHTML = `<p class="empty">Select a request to inspect its timings, tokens, and scheduler metrics.<br><span class="muted">Text capture is optional. Open Content to inspect a captured question and answer.</span></p>`;
    $("close-detail").hidden = true;
    feedList.querySelectorAll(".feed-row.sel").forEach((r) => { r.classList.remove("sel"); r.setAttribute("aria-pressed", "false"); });
    if (returnFocus) feedList.focus({ preventScroll: true });
  }
  $("close-detail").addEventListener("click", () => closeDetail());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.selectedId !== null && !$("clear-dialog").open) { e.preventDefault(); closeDetail(); }
  });
  async function select(id) {
    state.selectedId = id;
    $("detail-tabs").hidden = false;
    setDetailView("metrics");
    feedList.querySelectorAll(".feed-row[data-id]").forEach((r) => {
      const selected = Number(r.dataset.id) === id;
      r.classList.toggle("sel", selected); r.setAttribute("aria-pressed", String(selected));
    });
    $("detail-id").textContent = `#${id}`;
    $("detail-body").innerHTML = `<p class="empty" role="status">Loading request…</p>`;
    $("close-detail").hidden = false;
    try {
      const r = await api(`/api/requests/${id}`);
      if (state.selectedId === id) renderDetail(r);
    } catch (e) {
      if (state.selectedId !== id) return;
      $("detail-body").innerHTML = `<p class="empty" role="status">${e.message === "404" ? "Request not found (pruned or cleared)." : "Request could not be loaded. Return to activity and select it to retry."}</p>`;
    }
  }
  let contentLoadGeneration = 0;
  function setDetailView(view) {
    contentLoadGeneration++;
    $("detail-body").hidden = view !== "metrics";
    $("content-body").hidden = view !== "content";
    $("content-body").replaceChildren();
    $("detail-tabs").querySelectorAll("button").forEach((b) => { b.setAttribute("aria-pressed", String(b.dataset.view === view)); b.classList.toggle("on", b.dataset.view === view); });
    if (view === "content" && state.selectedId !== null) loadContent(state.selectedId);
  }
  $("detail-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) setDetailView(b.dataset.view);
  });
  async function loadContent(id) {
    const generation = ++contentLoadGeneration;
    const target = $("content-body");
    target.textContent = "Loading captured text…";
    try {
      const response = await fetch(`/api/requests/${id}/content`, { cache: "no-store" });
      if (!response.ok) throw new Error();
      const content = await response.json();
      if (state.selectedId !== id || target.hidden || generation !== contentLoadGeneration) return;
      target.replaceChildren();
      const labels = { complete: "Complete", partial: "Partial / incomplete answer", unsupported: "No supported text content", disabled: "Capture was disabled for this request", unavailable: "Content unavailable for this older request", deleted: "Content deleted", expired: "Content expired", dropped: "Content dropped: capture queue was full" };
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = labels[content.status] || "Content unavailable";
      target.append(note);
      for (const [key, label] of [["question", "Latest user message"], ["answer", "Generated answer"]]) {
        const heading = document.createElement("h3");
        heading.textContent = label + (content[key + "_truncated"] ? " · truncated" : "");
        const text = document.createElement("pre");
        text.className = "captured-text";
        text.textContent = content[key] ?? "No text captured.";
        target.append(heading, text);
      }
      if (content.question || content.answer) {
        const button = document.createElement("button");
        button.className = "btn danger";
        button.textContent = "Delete this content";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const result = await fetch(`/api/requests/${id}/content`, { method: "DELETE" });
            if (!result.ok) throw new Error();
            if (state.selectedId === id && !target.hidden) await loadContent(id);
            resetFeed();
          } catch { button.disabled = false; note.textContent = "Deletion failed. Try again."; }
        });
        target.append(button);
      }
    } catch {
      if (state.selectedId === id && !target.hidden && generation === contentLoadGeneration) target.textContent = "Content could not be loaded. Select Content to retry.";
    }
  }
  $("clear-content").addEventListener("click", () => $("content-clear-dialog").showModal());
  $("content-clear-dialog").addEventListener("close", async () => {
    if ($("content-clear-dialog").returnValue !== "clear") return;
    try {
      const result = await fetch("/api/content/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: "clear" }) });
      if (!result.ok) throw new Error();
      $("content-body").replaceChildren();
      if (state.selectedId !== null && !$("content-body").hidden) await loadContent(state.selectedId);
      resetFeed();
    } catch { $("capture-status").textContent = "Content clear failed. Try again."; }
  });
  function timelineHtml(r) {
    const q = r.queue_ms, p = r.prefill_ms, d = r.decode_ms;
    if (![q, p, d].some(isNum)) {
      // No Gufo timings: show what the proxy saw.
      if (!isNum(r.total_request_ms)) return "";
      const total = Math.max(1, r.total_request_ms);
      const mark = (v, label) => (isNum(v) ? `<div class="tl-marker" style="left:${Math.min(100, (100 * v) / total)}%" title="${label} ${fMs(v)}"></div>` : "");
      return `<section class="timeline" aria-label="Request timing"><h3>Request timing</h3><div class="tl-bar"><div class="tl-seg tl-decode" style="width:100%;opacity:.35"></div>${mark(r.ttft_ms, "TTFT")}</div>`
        + `<div class="tl-legend"><span>Proxy duration only; queue, prefill, decode unavailable.</span>`
        + `<span>TTFB ${fMs(r.proxy_ttfb_ms)}</span>${isNum(r.ttft_ms) ? `<span>TTFT ${fMs(r.ttft_ms)}</span>` : ""}<span>total ${fMs(r.total_request_ms)}</span></div></section>`;
    }
    const segs = [[q, "tl-queue", "queue"], [p, "tl-prefill", "prefill"], [d, "tl-decode", "decode"]];
    const knownTotal = segs.reduce((a, [v]) => a + (isNum(v) ? v : 0), 0);
    const total = Math.max(knownTotal, r.total_request_ms || 0, r.ttft_ms || 0, 1);
    const bar = segs.filter(([v]) => isNum(v) && v > 0).map(([v, cls, n]) => `<div class="tl-seg ${cls}" style="width:${(100 * v) / total}%" title="${n} ${fMs(v)}"></div>`).join("");
    const marker = isNum(r.ttft_ms) ? `<div class="tl-marker" style="left:calc(${Math.min(100, (100 * r.ttft_ms) / total)}% - 1px)" title="TTFT ${fMs(r.ttft_ms)}"></div>` : "";
    return `<section class="timeline" aria-label="Request timing"><h3>Request timing</h3><div class="tl-bar">${bar}${marker}</div><div class="tl-legend">`
      + `<span><i style="background:var(--faint)"></i>queue ${isNum(q) ? fMs(q) : "unavailable"}</span>`
      + `<span><i style="background:var(--warn)"></i>prefill ${isNum(p) ? fMs(p) : "unavailable"}</span>`
      + `<span><i style="background:var(--accent)"></i>decode ${isNum(d) ? fMs(d) : "unavailable"}</span>`
      + `<span>TTFT ${fMs(r.ttft_ms)}</span><span>Total ${fMs(r.total_request_ms)}</span></div>${[q, p, d].some((v) => !isNum(v)) ? `<p class="note">Only reported segments are drawn; unavailable durations are unknown.</p>` : ""}</section>`;
  }
  function renderDetail(r) {
    $("detail-id").textContent = `#${r.id}`;
    const acc = isNum(r.draft_tokens) && r.draft_tokens > 0 && isNum(r.draft_tokens_accepted) ? r.draft_tokens_accepted / r.draft_tokens : null;
    const ctx = isNum(r.context_used_pct) && isNum(r.context_length)
      ? `<div class="ctxbar" role="img" aria-label="Context ${esc(r.context_used_pct)} percent used"><div style="width:${Math.max(0, Math.min(100, r.context_used_pct))}%"></div></div><div class="note">${nf2.format(r.context_used_pct)}% of ${fInt(r.context_length)} context</div>` : "";
    const extra = Object.entries(r.extra_metrics || {}).filter(([k, v]) => /^[a-z][a-z0-9_]*$/.test(k) && (isNum(v) || typeof v === "boolean")).sort(([a], [b]) => a.localeCompare(b));
    const section = (name, rows, suffix = "") => `<section><h3>${name}</h3>${kvRows(rows)}${suffix}</section>`;
    const optional = (name, fields, rows) => fields.some((key) => r[key] !== null && r[key] !== undefined) ? section(name, rows) : "";
    const hitTxt = r.cache_hit === true ? `<span class="badge hit">HIT</span>` : r.cache_hit === false ? `<span class="badge miss">MISS</span>` : DASH;
    $("detail-body").innerHTML = `
      <div class="status-line"><span class="mono">${esc(r.endpoint)}</span><span>HTTP ${fInt(r.http_status)}</span>
        ${r.is_streaming ? `<span class="badge">STREAM</span>` : ""}${r.is_vision ? `<span class="badge">VISION ×${esc(r.image_count)}</span>` : ""}
        <span class="muted">${fDateTime(r.started_at_ms)}</span></div>
      ${timelineHtml(r)}
      <div class="detail-grid">
        ${section("Identity / status", [
          ["Request ID", fInt(r.id)], ["Gufo request ID", `<span class="mono">${esc(r.gufo_request_id || DASH)}</span>`],
          ["Model", `<span class="mono">${esc(r.model || DASH)}</span>`],
          ["HTTP status", fInt(r.http_status)], ["Error code", esc(r.error_code || DASH)],
          ["Finish reason", esc(r.finish_reason || DASH)], ["Streaming", r.is_streaming ? "Yes" : "No"],
          ["Vision / images", r.is_vision ? `Yes / ${fInt(r.image_count)}` : "No"], ["TTFT source", esc(r.ttft_source || DASH)]])}
        ${section("Tokens", [
          ["Prompt", fInt(r.prompt_tokens)], ["Cached", fInt(r.cached_tokens)], ["Prefilled", fInt(r.prefill_tokens)],
          ["Generated", fInt(r.completion_tokens)], ["Reasoning", fInt(r.reasoning_tokens)]], ctx)}
        ${section("Speed", [
          ["Prefill tok/s", fTps(r.prompt_tokens_per_second)], ["Decode tok/s", fTps(r.completion_tokens_per_second)],
          ["Mean inter-token", fMs(r.mean_inter_token_ms)], ["Max inter-token", fMs(r.max_inter_token_ms)],
          ["TTFT", fMs(r.ttft_ms)], ["Proxy TTFB", fMs(r.proxy_ttfb_ms)],
          ["Proxy first token", fMs(r.proxy_first_token_ms)], ["Total duration", fMs(r.total_request_ms)]])}
        ${optional("Speculative", ["draft_tokens", "draft_tokens_accepted"], [
          ["Drafted", fInt(r.draft_tokens)], ["Accepted", fInt(r.draft_tokens_accepted)], ["Acceptance", fPct(acc)]])}
        ${optional("Cache", ["cache_hit", "cache_miss_reason", "cache_common_prefix_tokens", "cache_restore_ms", "cache_restore_bytes"], [
          ["Result", hitTxt], ["Miss reason", esc(r.cache_miss_reason || DASH)], ["Common prefix", fInt(r.cache_common_prefix_tokens)],
          ["Restore", fMs(r.cache_restore_ms)], ["Restore size", fBytes(r.cache_restore_bytes)]])}
        ${optional("Scheduler", ["execution_plan", "queue_ms", "queue_depth_at_submit", "client_queue_depth_at_submit", "resident_requests_at_admission", "requested_logical_concurrency", "physical_execution_width", "prefill_chunks"], [
          ["Execution plan", `<span class="mono">${esc(r.execution_plan || DASH)}</span>`], ["Queue", fMs(r.queue_ms)],
          ["Queue depth at submit", fInt(r.queue_depth_at_submit)], ["Client queue depth", fInt(r.client_queue_depth_at_submit)],
          ["Resident requests", fInt(r.resident_requests_at_admission)], ["Logical concurrency", fInt(r.requested_logical_concurrency)],
          ["Physical execution width", fInt(r.physical_execution_width)], ["Prefill chunks", fInt(r.prefill_chunks)]])}
        ${extra.length ? section("Other metrics", extra.map(([k, v]) => [k, typeof v === "boolean" ? String(v) : /bytes$/.test(k) ? fBytes(v) : /_ms$/.test(k) ? fMs(v) : nf2.format(v).replace(/\.00$/, "")])) : ""}
      </div>`;
  }

  // --------------------------------------------------------------- controls
  function setupRange() {
    const box = $("range");
    const sync = () => box.querySelectorAll("button").forEach((b) => {
      const on = b.dataset.range === state.range;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
    });
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state.range = b.dataset.range;
      store.set("range", state.range);
      sync();
      resetData();
      refreshData();
    });
    sync();
  }
  setupRange();

  const modelSel = $("model-filter");
  $("choose-model").addEventListener("click", () => { modelSel.focus(); });
  modelSel.addEventListener("change", () => {
    state.model = modelSel.value || "";
    resetData();
    refreshData();
    resetFeed();
    if (state.status) renderStatus(state.status);
  });
  async function refreshModels() {
    const res = await api("/api/models");
    const names = res.models;
    if (state.model === null) state.model = res.current || "";
    if (state.model && !names.includes(state.model)) names.push(state.model);
    const opts = [`<option value="">All models</option>`].concat(names.map((n) => `<option value="${esc(n)}"${n === res.current ? " data-current" : ""}>${esc(n)}${n === res.current ? " (current)" : ""}</option>`));
    const html = opts.join("");
    if (modelSel.dataset.html !== html) { modelSel.innerHTML = html; modelSel.dataset.html = html; }
    modelSel.value = state.model;
  }

  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").setAttribute("aria-pressed", String(state.paused));
    $("pause").textContent = state.paused ? "Resume" : "Pause";
    refreshInd.classList.toggle("paused", state.paused);
    refreshInd.title = state.paused ? "Refresh paused" : "Refreshing";
    renderFreshness();
    if (!state.paused) refreshAll();
  });
  $("retry-refresh").addEventListener("click", () => { refreshAll(); loadNewer(); });

  // ---------------------------------------------------------------- refresh
  function resetData() {
    state.dataGeneration++;
    state.summary = null;
    state.lastUpdated = null;
    spec = null; cache = null; lastTs = null;
    for (const key of ["requests", "errors", "gen", "prompt", "decode", "prefill", "ttft", "cache", "draft"]) setCard(key, DASH, "", "na");
    $("c-ttft").parentElement.querySelector(".label").textContent = "TTFT p50";
    $("model-hint").hidden = !!state.model;
    primary.data.datasets.forEach((d) => { d.data = []; }); primary.update();
    secondary.data.datasets = []; secondary.update();
    $("secondary-legend").textContent = "";
    $("insight-body").innerHTML = `<p class="note">Loading metrics…</p>`;
    renderFreshness();
  }
  async function refreshStatus() {
    try {
      state.status = await api("/api/status");
      renderStatus(state.status);
      const capture = state.status.content_capture;
      $("capture-status").className = capture?.enabled ? "capture-on" : "muted";
      $("capture-status").textContent = capture?.enabled ? `● CONTENT CAPTURE ON · ${capture.retention_days}d` : "Text capture off";
    } catch {
      $("status-dot").className = "dot dot-unknown";
      $("status-text").textContent = "dashboard unreachable";
    }
  }
  async function refreshData() {
    const generation = ++state.dataGeneration;
    const q = qs();
    const [s, ts, sp, ca] = await Promise.allSettled([
      api("/api/summary?" + q),
      api("/api/timeseries?" + qs({ bucket: "auto" })),
      api("/api/speculative?" + q),
      api("/api/cache?" + q),
    ]);
    if (generation !== state.dataGeneration || q !== qs()) return;
    if (s.status === "fulfilled") { state.summary = s.value; renderCards(s.value); }
    if (ts.status === "fulfilled") renderCharts(ts.value);
    if (sp.status === "fulfilled") spec = sp.value;
    if (ca.status === "fulfilled") cache = ca.value;
    if ([s, ts, sp, ca].every((result) => result.status === "fulfilled")) state.lastUpdated = Date.now();
    renderInsights();
    renderFreshness();
  }
  async function refreshAll() {
    await Promise.allSettled([refreshStatus(), refreshModels()]);
    await refreshData();
  }

  function every(ms, fn) {
    const tick = async () => {
      if (!state.paused && !document.hidden) { try { await fn(); } catch { /* ignore */ } }
      setTimeout(tick, ms);
    };
    setTimeout(tick, ms);
  }

  (async () => {
    try { await refreshModels(); } catch { state.model = ""; }
    await refreshStatus();
    await refreshData();
    resetFeed();
    every(STATUS_MS, refreshStatus);
    every(DATA_MS, async () => { await refreshModels().catch(() => {}); await refreshData(); });
    every(FEED_MS, loadNewer);
  })();
  setInterval(renderFreshness, 1000);
})();
