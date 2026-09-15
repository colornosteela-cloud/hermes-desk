/* Working Memory Manifest UI. Classic script — no ES modules. */
(function () {
  const PRESSURE_BANDS = [
    [0.45, "GREEN"],
    [0.65, "YELLOW"],
    [0.80, "ORANGE"],
    [0.90, "RED"],
  ];
  const SECTION_LABELS = {
    core: "Core",
    body_state: "Body State",
    safety_state: "Safety State",
    world_state: "World State",
    conversation: "Conversation",
    active_task: "Active Task",
    retrieved_memory: "Retrieved Memory",
    skills: "Skills",
    perception: "Perception",
    sensorimotor: "Sensorimotor Memory",
    tools: "Tools",
    workspace: "Workspace",
    other: "Other",
    unknown: "Unknown",
  };
  const MEMORY_FILTERS = [
    ["", "All"],
    ["episodic", "Episodic"],
    ["semantic", "Semantic"],
    ["procedural", "Procedural"],
    ["self", "Self"],
    ["task", "Tasks"],
    ["corrections", "Corrections"],
  ];

  const ui = {
    open: false,
    tab: "overview",
    summary: null,
    items: null,
    memories: null,
    retrieval: null,
    history: null,
    search: null,
    memFilter: "",
    memOffset: 0,
    selectedMemory: null,
    lastFetch: 0,
    boundBotId: "",
    fetchGen: 0,
  };

  function pressureFromUtilization(u) {
    if (u == null || !Number.isFinite(Number(u))) return null;
    const x = Number(u);
    for (const [upper, name] of PRESSURE_BANDS) {
      if (x < upper) return name;
    }
    return "CRITICAL";
  }

  function fmtK(n) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    if (x >= 1000000) {
      const m = x / 1000000;
      return `${m >= 10 ? m.toFixed(0) : m.toFixed(1).replace(/\.0$/, "")}M`;
    }
    if (x >= 10000) return `${Math.round(x / 1000)}K`;
    if (x >= 1000) return `${Math.round(x / 1000)}K`;
    return String(Math.round(x));
  }

  function fmtInt(n) {
    if (n == null || !Number.isFinite(Number(n))) return "Unavailable";
    return Number(n).toLocaleString();
  }

  function formatCompact(used, cap, pressure) {
    if (used == null || cap == null || !Number.isFinite(Number(used)) || !Number.isFinite(Number(cap))) {
      return "🧠 Unavailable";
    }
    const p = pressure || pressureFromUtilization(Number(used) / Number(cap));
    return `🧠 ${fmtK(used)} / ${fmtK(cap)} · ${p || "—"}`;
  }

  function statusUnavailable(obj) {
    return obj && typeof obj === "object" && obj.status === "unavailable";
  }

  function showVal(v, fallback) {
    if (v == null || v === "") return fallback || "Unavailable";
    if (statusUnavailable(v)) return v.reason || "Unavailable";
    if (typeof v === "object") return fallback || "Unavailable";
    return String(v);
  }

  function badge(inCtx) {
    return inCtx
      ? '<span class="wm-badge in">IN CONTEXT</span>'
      : '<span class="wm-badge stored">STORED ONLY</span>';
  }

  function botId() {
    return (typeof state !== "undefined" && state.selected) || "";
  }

  function botRecord() {
    const id = botId();
    const bots = (typeof state !== "undefined" && state.bots) || [];
    return bots.find((x) => x.id === id) || null;
  }

  function pathFor(rest) {
    const id = botId();
    const tail = rest ? `/${rest.replace(/^\//, "")}` : "";
    return `/v1/bots/${encodeURIComponent(id)}/working-memory${tail}`;
  }

  async function get(rest, query) {
    const id = botId();
    if (!id || typeof api !== "function") return null;
    const gen = ui.fetchGen;
    const q = query ? `?${query}` : "";
    const data = await api(pathFor(rest) + q);
    if (ui.fetchGen !== gen || botId() !== id) return null;
    return data;
  }

  function resetBoundData(id) {
    ui.summary = null;
    ui.items = null;
    ui.memories = null;
    ui.retrieval = null;
    ui.history = null;
    ui.search = null;
    ui.selectedMemory = null;
    ui.boundBotId = id || botId();
  }

  function onSelectBot(bot) {
    const id = (bot && bot.id) || botId();
    if (ui.boundBotId && ui.boundBotId !== id) {
      ui.fetchGen += 1;
      resetBoundData(id);
      paintHead();
      if (ui.open) {
        const el = $("wm-body");
        if (el) el.innerHTML = `<p class="wm-empty">Loading ${escapeHtml((bot && bot.name) || "this bot")}…</p>`;
      }
    } else if (!ui.boundBotId) {
      ui.boundBotId = id;
    }
    setIndicator(bot);
    if (ui.open) refreshAll();
  }

  function setIndicator(bot) {
    const ctx = typeof $ === "function" ? $("context-stat") : document.getElementById("context-stat");
    if (!ctx) return;
    if (!bot) {
      ctx.removeAttribute("data-pressure");
      return;
    }
    const source = bot.context_source || "";
    const used = source ? Number(bot.context_used) : (bot.wm_used != null ? Number(bot.wm_used) : null);
    const cap = Number(bot.context_window) || null;
    const util = bot.wm_utilization != null ? Number(bot.wm_utilization) : (cap && used != null ? used / cap : null);
    const pressure = bot.wm_pressure || pressureFromUtilization(util);
    const known = source && used != null && Number.isFinite(used) && cap && Number.isFinite(cap) && cap > 0;
    ctx.setAttribute("data-pressure", pressure || "");
    ctx.title = known
      ? `${Number(used).toLocaleString()} / ${Number(cap).toLocaleString()} · ${((util || 0) * 100).toFixed(1)}% · ${pressure}`
      : "Context accounting: Unavailable";
  }

  function syncFromBot(bot) {
    setIndicator(bot);
  }

  function onUsage(msg, bot) {
    if (bot) setIndicator(bot);
    if (ui.open && msg && msg.bot_id === botId()) {
      const now = Date.now();
      if (now - ui.lastFetch > 800) refreshSummary(false);
    }
  }

  function openDrawer() {
    const drawer = $("wm-drawer");
    if (!drawer) return;
    drawer.hidden = false;
    ui.open = true;
    document.body.classList.add("wm-open");
    refreshAll();
  }

  function closeDrawer() {
    const drawer = $("wm-drawer");
    if (drawer) drawer.hidden = true;
    ui.open = false;
    document.body.classList.remove("wm-open");
  }

  function setTab(name) {
    ui.tab = name;
    document.querySelectorAll(".wm-tab").forEach((btn) => {
      btn.classList.toggle("is-active", btn.getAttribute("data-wm-tab") === name);
    });
    renderBody();
    loadTab(name);
  }

  async function refreshSummary(render) {
    const id = botId();
    const gen = ui.fetchGen;
    ui.lastFetch = Date.now();
    try {
      const data = await get("");
      if (botId() !== id || ui.fetchGen !== gen) return;
      if (data == null) return;
      ui.summary = data;
    } catch {
      if (botId() !== id || ui.fetchGen !== gen) return;
      ui.summary = { used_status: "unavailable", pressure_status: "unavailable" };
    }
    paintHead();
    if (render !== false && ui.tab === "overview") renderBody();
    const b = (state.bots || []).find((x) => x.id === botId());
    if (b && ui.summary) {
      if (typeof ui.summary.used_tokens === "number") b.context_used = ui.summary.used_tokens;
      if (typeof ui.summary.capacity_tokens === "number") b.context_window = ui.summary.capacity_tokens;
      if (ui.summary.pressure) b.wm_pressure = ui.summary.pressure;
      if (typeof ui.summary.utilization === "number") b.wm_utilization = ui.summary.utilization;
      setIndicator(b);
    }
  }

  async function refreshAll() {
    await refreshSummary(false);
    await loadTab(ui.tab);
  }

  async function loadTab(name) {
    const id = botId();
    const gen = ui.fetchGen;
    try {
      if (name === "inspect" || name === "active") {
        const data = await get("items", "limit=100");
        if (botId() !== id || ui.fetchGen !== gen) return;
        if (data != null) ui.items = data;
      }
      if (name === "memory") {
        const filter = ui.memFilter;
        const qs = [`limit=50`, `offset=${ui.memOffset}`];
        if (filter === "corrections") qs.push("filter=corrections");
        else if (filter) qs.push(`type=${encodeURIComponent(filter)}`);
        const data = await get("memories", qs.join("&"));
        if (botId() !== id || ui.fetchGen !== gen) return;
        if (data != null) ui.memories = data;
      }
      if (name === "retrieval") {
        const data = await get("retrieval");
        if (botId() !== id || ui.fetchGen !== gen) return;
        if (data != null) ui.retrieval = data;
      }
      if (name === "history") {
        const data = await get("history");
        if (botId() !== id || ui.fetchGen !== gen) return;
        if (data != null) ui.history = data;
      }
    } catch (err) {
      if (botId() !== id || ui.fetchGen !== gen) return;
      ui.error = String(err && err.message ? err.message : err);
    }
    if (botId() !== id || ui.fetchGen !== gen) return;
    renderBody();
  }

  function paintHead() {
    const bot = botRecord();
    const title = $("wm-title");
    if (title) title.textContent = bot && bot.name ? `${bot.name} — Working Memory` : "Working Memory";
    const kicker = document.querySelector(".wm-kicker");
    if (kicker) {
      const kind = bot && bot.kind === "hermes" ? "BUILD" : "TEELA";
      kicker.textContent = `🧠 ${kind} — WORKING MEMORY`;
    }
    const meta = $("wm-head-meta");
    if (!meta) return;
    const s = ui.summary || {};
    if (s.used_tokens == null || s.capacity_tokens == null) {
      meta.textContent = bot && bot.name
        ? `${bot.name} · Context accounting: Unavailable`
        : "Context accounting: Unavailable";
      return;
    }
    const pct = s.capacity_percent != null ? Number(s.capacity_percent).toFixed(1) : "—";
    const age = s.last_updated ? ` · updated ${relTime(s.last_updated)}` : "";
    const who = bot && bot.name ? `${bot.name} · ` : "";
    meta.textContent = `${who}${fmtInt(s.used_tokens)} / ${fmtInt(s.capacity_tokens)} · ${pct}% · ${s.pressure || "—"}`;
    meta.appendChild(document.createTextNode(age));
  }

  function relTime(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 1) return `${Math.round(s * 1000)} ms ago`;
    if (s < 10) return `${s.toFixed(1)} sec ago`;
    if (s < 60) return `${Math.round(s)} sec ago`;
    return `${Math.round(s / 60)} min ago`;
  }

  function renderBody() {
    const el = $("wm-body");
    if (!el) return;
    const q = ($("wm-search") && $("wm-search").value || "").trim();
    if (q && ui.search) {
      el.innerHTML = renderSearch(ui.search);
      return;
    }
    if (ui.tab === "overview") el.innerHTML = renderOverview();
    else if (ui.tab === "active") el.innerHTML = renderActive();
    else if (ui.tab === "inspect") el.innerHTML = renderInspect();
    else if (ui.tab === "memory") el.innerHTML = renderMemory();
    else if (ui.tab === "retrieval") el.innerHTML = renderRetrieval();
    else if (ui.tab === "history") el.innerHTML = renderHistory();
    else if (ui.tab === "runtime") el.innerHTML = renderRuntime();
    bindBody(el);
  }

  function renderOverview() {
    const s = ui.summary;
    if (!s) return `<p class="wm-empty">Loading…</p>`;
    const usedUnknown = s.used_tokens == null;
    const capUnknown = s.capacity_tokens == null;
    const utilPct = s.capacity_percent != null ? Number(s.capacity_percent).toFixed(1) : null;
    const pressure = s.pressure || "Unavailable";
    const barW = utilPct != null ? Math.min(100, Math.max(0, Number(utilPct))) : 0;
    const occ = s.occupied_percents || {};
    const sections = s.sections || {};
    const mem = s.memory || {};
    const rows = Object.keys(SECTION_LABELS)
      .filter((k) => sections[k] != null)
      .map((k) => {
        const n = sections[k];
        const p = occ[k] != null ? Number(occ[k]).toFixed(1) + "%" : "—";
        return `<div class="wm-row"><span>${escapeHtml(SECTION_LABELS[k])}</span><span>${fmtInt(n)} <span class="wm-muted">${p}</span></span></div>`;
      })
      .join("");
    const occupiedTotal = Object.values(sections).reduce((a, b) => a + (Number(b) || 0), 0);
    return `
      <div class="wm-split">
        <div>
          <h3>ACTIVE NOW</h3>
          <div>${usedUnknown ? "Unavailable" : fmtInt(s.used_tokens) + " tokens"}</div>
          <div class="wm-muted">Qwen can currently attend to this</div>
        </div>
        <div>
          <h3>STORED</h3>
          <div>${mem.status === "unavailable" ? "Disconnected" : fmtInt(mem.stored_total) + " records"}</div>
          <div class="wm-muted">${fmtInt(mem.currently_loaded)} currently loaded</div>
        </div>
        <div>
          <h3>RUNTIME</h3>
          <div>${escapeHtml(s.model || "—")}</div>
          <div class="wm-muted">${s.context_source || "source unknown"}</div>
        </div>
      </div>
      <div class="wm-section">Context Utilization</div>
      <div>${usedUnknown || capUnknown ? "Unavailable" : `${fmtInt(s.used_tokens)} / ${fmtInt(s.capacity_tokens)} tokens`}</div>
      <div class="wm-bar"><span style="width:${barW}%"></span></div>
      <div class="wm-row"><span>Pressure</span><span class="wm-pressure-${pressure}">${escapeHtml(pressure)}</span></div>
      <div class="wm-row"><span>Free</span><span>${s.free_tokens == null ? "Unavailable" : fmtInt(s.free_tokens) + " tokens"}</span></div>
      <div class="wm-row"><span>Capacity used</span><span>${utilPct == null ? "Unavailable" : utilPct + "%"}</span></div>
      ${s.used_estimated ? `<p class="wm-muted">Occupancy marked estimated (tokenizer fallback).</p>` : ""}
      <div class="wm-section">ACTIVE CONTEXT</div>
      ${s.sections_status === "unavailable" ? `<p class="wm-empty">Section breakdown: Unavailable — no assembled turn yet</p>` : rows + `<div class="wm-row"><strong>TOTAL (occupied)</strong><strong>${fmtInt(occupiedTotal)}</strong></div><p class="wm-muted">Percents are of currently occupied context, not of capacity.${s.sections_estimated ? " Section counts are local-tokenizer estimates." : ""}</p>`}
      ${renderTaskCard(s.active_task)}
      ${renderBodyCard(s.body_state)}
      ${renderSafetyCard(s.safety_state)}
      ${renderWorldCard(s.world_state)}
      ${renderPerceptionCard(s.perception)}
      ${renderSensorimotorCard(s.sensorimotor)}
      ${renderCorrectionsCard(s.corrections)}
    `;
  }

  function renderTaskCard(task) {
    if (statusUnavailable(task) || !task) {
      return `<div class="wm-section">ACTIVE TASK</div><p class="wm-empty">${escapeHtml((task && task.reason) || "No active task")}</p>`;
    }
    return `<div class="wm-section">ACTIVE TASK</div>
      <div class="wm-card">
        <dl class="wm-kv">
          <dt>Goal</dt><dd>${escapeHtml(task.goal || "—")}</dd>
          <dt>State</dt><dd>${escapeHtml(task.state || "—")}</dd>
          <dt>Current step</dt><dd>${escapeHtml(task.current_step || "—")}</dd>
          <dt>Checkpoint</dt><dd>${escapeHtml(task.checkpoint || "—")}</dd>
          <dt>Context cost</dt><dd>${task.context_cost_tokens == null ? "Unavailable" : fmtInt(task.context_cost_tokens) + (task.context_cost_estimated ? " (estimated)" : "")}</dd>
        </dl>
      </div>`;
  }

  function renderBodyCard(body) {
    if (statusUnavailable(body) || !body) {
      return `<div class="wm-section">BODY STATE</div><p class="wm-empty">${escapeHtml((body && body.reason) || "Body state: Unavailable")}</p>`;
    }
    const stale = body.status === "stale";
    const pan = statusUnavailable(body.head && body.head.pan) ? "Unavailable" : (body.head && body.head.pan != null ? Number(body.head.pan).toFixed(1) + "°" : "Unavailable");
    const tilt = statusUnavailable(body.head && body.head.tilt) ? "Unavailable" : (body.head && body.head.tilt != null ? Number(body.head.tilt).toFixed(1) + "°" : "Unavailable");
    const gaze = statusUnavailable(body.gaze) ? "Unavailable" : showVal(body.gaze);
    return `<div class="wm-section">BODY STATE ${stale ? "· STALE" : ""}</div>
      <div class="wm-card">
        <dl class="wm-kv">
          <dt>Pose</dt><dd>${escapeHtml(body.pose || "—")}</dd>
          <dt>Head pan</dt><dd>${escapeHtml(pan)}</dd>
          <dt>Head tilt</dt><dd>${escapeHtml(tilt)}</dd>
          <dt>Gaze</dt><dd>${escapeHtml(gaze)}</dd>
          <dt>Motion</dt><dd>${escapeHtml(body.motion || "—")}</dd>
          <dt>Revision</dt><dd>${escapeHtml(String(body.revision ?? "—"))}</dd>
          <dt>Updated</dt><dd>${body.freshness_ms == null ? "Unavailable" : (body.freshness_ms + " ms ago")}${stale ? " · " + escapeHtml(body.reason || "Stale") : ""}</dd>
        </dl>
      </div>`;
  }

  function renderWorldCard(world) {
    if (statusUnavailable(world) || !world) {
      const reason = (world && world.reason) || "World state: Unavailable";
      return `<div class="wm-section">WORLD STATE</div><p class="wm-empty">${escapeHtml(reason)}</p>`;
    }
    const objs = world.objects || {};
    const people = world.people || {};
    const names = Object.keys(objs).concat(Object.keys(people));
    const bits = names.length ? names.map((k) => escapeHtml(k)).join(", ") : "empty scene";
    return `<div class="wm-section">WORLD STATE</div>
      <div class="wm-card">
        <dl class="wm-kv">
          <dt>Revision</dt><dd>${escapeHtml(String(world.revision ?? "—"))}</dd>
          <dt>Fresh</dt><dd>${escapeHtml(bits)}</dd>
        </dl>
      </div>`;
  }

  function renderPerceptionCard(p) {
    const tokens = (ui.summary && ui.summary.sections && ui.summary.sections.perception) || null;
    if (statusUnavailable(p) || !p) {
      const reason = (p && p.reason) || "Perception: Unavailable";
      return `<div class="wm-section">PERCEPTION</div>
      <div class="wm-row"><span>Active context</span><span>${tokens == null ? "Unavailable" : fmtInt(tokens) + " tokens"}</span></div>
      <p class="wm-empty">${escapeHtml(reason)}</p>`;
    }
    return `<div class="wm-section">PERCEPTION</div>
      <div class="wm-row"><span>Events</span><span>${fmtInt(p.event_count)}</span></div>
      <div class="wm-row"><span>Dropped frames</span><span>${fmtInt(p.dropped_frames)}</span></div>
      <div class="wm-row"><span>Active context</span><span>${tokens == null ? "Unavailable" : fmtInt(tokens) + " tokens"}</span></div>`;
  }

  function renderSafetyCard(s) {
    if (statusUnavailable(s) || !s) {
      const reason = (s && s.reason) || "Safety state: Unavailable";
      return `<div class="wm-section">SAFETY STATE</div><p class="wm-empty">${escapeHtml(reason)}</p>`;
    }
    return `<div class="wm-section">SAFETY STATE</div>
      <div class="wm-card"><dl class="wm-kv">
        <dt>Status</dt><dd>${escapeHtml(String(s.status || "nominal"))}</dd>
        <dt>E-stop</dt><dd>${escapeHtml(String(s.emergency_stop))}</dd>
        <dt>Motion allowed</dt><dd>${escapeHtml(String(s.motion_allowed))}</dd>
      </dl></div>`;
  }

  function renderSensorimotorCard(s) {
    if (statusUnavailable(s) || !s) {
      const reason = (s && s.reason) || "Sensorimotor memory: Unavailable";
      return `<div class="wm-section">SENSORIMOTOR MEMORY</div><p class="wm-empty">${escapeHtml(reason)}</p>`;
    }
    return `<div class="wm-section">SENSORIMOTOR MEMORY</div>
      <div class="wm-row"><span>Retrieved</span><span>${fmtInt((s.retrieved && s.retrieved.length) || s.stored || 0)}</span></div>`;
  }

  function renderCorrectionsCard(c) {
    const loaded = (c && c.loaded) || [];
    if (!loaded.length) {
      return `<div class="wm-section">CORRECTIONS</div><p class="wm-muted">Loaded Corrections: 0</p>`;
    }
    const bits = loaded.map((x) => `✓ ${escapeHtml(x.title || x.id || "correction")}`).join("<br>");
    return `<div class="wm-section">CORRECTIONS</div>
      <p>Loaded Corrections: ${loaded.length}</p>
      <div class="wm-card">${bits}</div>`;
  }

  function renderActive() {
    const s = ui.summary || {};
    const items = (ui.items && ui.items.items) || [];
    if (s.sections_status === "unavailable" && !items.length) {
      return `<p class="wm-empty">Active context: Unavailable — no assembled turn yet</p>`;
    }
    const groups = {};
    for (const it of items) {
      const sec = it.section || "unknown";
      (groups[sec] = groups[sec] || []).push(it);
    }
    let html = `<p class="wm-muted">ACTIVE / IN MIND — items currently loaded for Qwen</p>`;
    for (const sec of Object.keys(SECTION_LABELS)) {
      const list = groups[sec];
      if (!list || !list.length) continue;
      html += `<div class="wm-section">${escapeHtml(SECTION_LABELS[sec])}</div>`;
      for (const it of list) html += renderItemCard(it, false);
    }
    return html || `<p class="wm-empty">No active context items recorded</p>`;
  }

  function renderInspect() {
    const items = (ui.items && ui.items.items) || [];
    if (ui.items && ui.items.status === "unavailable") {
      return `<p class="wm-empty">${escapeHtml(ui.items.reason || "Unavailable")}</p>`;
    }
    if (!items.length) return `<p class="wm-empty">No context items to inspect</p>`;
    return items.map((it) => renderItemCard(it, true)).join("");
  }

  function renderItemCard(it, detail) {
    const pin = it.pinned ? '<span class="wm-pin">📌</span>' : "";
    const codes = (it.reason_codes || []).map((c) => `<code>${escapeHtml(c)}</code>`).join(" ");
    return `<div class="wm-card" data-mem="${escapeHtml(it.id || "")}">
      <div class="wm-item-title">${pin}${escapeHtml(it.title || it.id || "item")} ${badge(true)}</div>
      <dl class="wm-kv">
        <dt>Id</dt><dd>${escapeHtml(it.id || "—")}</dd>
        <dt>Type</dt><dd>${escapeHtml(String(it.type || "unknown").toUpperCase())}</dd>
        <dt>Tokens</dt><dd>${fmtInt(it.token_count)}${it.estimated ? " (estimated)" : ""}</dd>
        <dt>Loaded</dt><dd>${escapeHtml(it.loaded_at || "—")}</dd>
        <dt>Source</dt><dd>${escapeHtml(it.source || "unknown")}</dd>
        ${it.store ? `<dt>Store</dt><dd>${escapeHtml(it.store)}</dd>` : ""}
        ${it.record ? `<dt>Record</dt><dd>${escapeHtml(it.record)}</dd>` : ""}
        <dt>Priority</dt><dd>${escapeHtml(it.priority || "unknown")}</dd>
        <dt>Pinned</dt><dd>${it.pinned ? "Yes — protected from normal eviction" : "No"}</dd>
        <dt>Why loaded</dt><dd>${escapeHtml(it.why_loaded || "Unknown — runtime did not record a retrieval reason.")}</dd>
        ${detail ? `<dt>Reason codes</dt><dd>${codes || "—"}</dd>` : ""}
        ${it.relevance_score != null ? `<dt>Score</dt><dd>${Number(it.relevance_score).toFixed(2)}</dd>` : ""}
        ${it.confidence != null ? `<dt>Confidence</dt><dd>${Number(it.confidence).toFixed(2)}</dd>` : ""}
        <dt>Last used</dt><dd>${escapeHtml(it.last_used || it.loaded_at || "—")}</dd>
      </dl>
      ${it.record ? `<button type="button" class="small-action" data-view-mem="${escapeHtml(it.record)}">View Memory</button>` : ""}
    </div>`;
  }

  function renderMemory() {
    const m = ui.memories;
    const s = ui.summary || {};
    const types = (m && m.types) || (s.memory && s.memory.types) || {};
    const filters = MEMORY_FILTERS.map(([id, label]) =>
      `<button type="button" data-mem-filter="${id}" class="${ui.memFilter === id ? "is-on" : ""}">${label}</button>`
    ).join("");
    const stored = s.memory && s.memory.stored_total;
    const loaded = s.memory && s.memory.currently_loaded;
    let html = `<p class="wm-muted">STORED / AVAILABLE FOR RECALL — not necessarily visible to Qwen</p>
      <div class="wm-row"><span>Total records</span><span>${stored == null ? "Unavailable" : fmtInt(stored)}</span></div>
      <div class="wm-row"><span>Currently loaded</span><span>${loaded == null ? "Unavailable" : fmtInt(loaded)}</span></div>
      <div class="wm-section">Types</div>
      ${["episodic","semantic","procedural","relational","self","task","unknown"].map((k) =>
        `<div class="wm-row"><span>${k[0].toUpperCase()+k.slice(1)}</span><span>${types[k] == null ? "—" : fmtInt(types[k])}</span></div>`
      ).join("")}
      <div class="wm-filters">${filters}</div>`;
    if (ui.selectedMemory) html += renderMemoryDetail(ui.selectedMemory);
    const recs = (m && m.records) || [];
    if (!recs.length) html += `<p class="wm-empty">No stored memories in this page</p>`;
    for (const rec of recs) {
      html += `<div class="wm-card" data-open-mem="${escapeHtml(rec.id)}">
        <div class="wm-item-title">${escapeHtml(rec.title || rec.id)} ${badge(!!rec.currently_in_context)}</div>
        <div class="wm-muted">${escapeHtml(rec.type || "unknown")} · ${escapeHtml(rec.id)}</div>
      </div>`;
    }
    const total = (m && m.total) || 0;
    html += `<div class="wm-pager">
      <button type="button" class="small-action" data-mem-page="-1" ${ui.memOffset <= 0 ? "disabled" : ""}>Prev</button>
      <span class="wm-muted">${ui.memOffset + 1}–${Math.min(ui.memOffset + recs.length, total)} of ${total}</span>
      <button type="button" class="small-action" data-mem-page="1" ${ui.memOffset + recs.length >= total ? "disabled" : ""}>Next</button>
    </div>`;
    return html;
  }

  function renderMemoryDetail(d) {
    if (d.status === "not_found") return `<p class="wm-empty">Memory not found</p>`;
    return `<div class="wm-card">
      <div class="wm-item-title">${escapeHtml(d.id)} ${badge(!!d.currently_in_context)}</div>
      <dl class="wm-kv">
        <dt>Type</dt><dd>${escapeHtml(d.type || "UNKNOWN")}</dd>
        <dt>Created</dt><dd>${escapeHtml(d.created || "—")}</dd>
        <dt>Updated</dt><dd>${escapeHtml(d.updated || "—")}</dd>
        <dt>Importance</dt><dd>${d.importance == null ? "Unavailable" : d.importance}</dd>
        <dt>Confidence</dt><dd>${d.confidence == null ? "Unavailable" : Number(d.confidence).toFixed(2)}</dd>
        <dt>Status</dt><dd>${escapeHtml(d.status || "—")}</dd>
        <dt>Currently in context</dt><dd>${d.currently_in_context ? "YES" : "NO"}</dd>
        <dt>Associated</dt><dd>${escapeHtml((d.associated || []).join(", ") || "—")}</dd>
        <dt>Tags</dt><dd>${escapeHtml((d.tags || []).join(" ") || "—")}</dd>
        <dt>Content</dt><dd>${escapeHtml(d.content || "—")}</dd>
        <dt>Provenance</dt><dd>${escapeHtml(d.provenance || "—")}</dd>
        <dt>Last retrieved</dt><dd>${escapeHtml(d.last_retrieved || "—")}</dd>
        ${d.why_loaded ? `<dt>Why loaded</dt><dd>${escapeHtml(d.why_loaded)}</dd>` : ""}
      </dl>
    </div>`;
  }

  function renderRetrieval() {
    const r = ui.retrieval;
    if (!r || r.status === "unavailable") {
      return `<p class="wm-empty">${escapeHtml((r && r.reason) || "No retrieval has been recorded yet")}</p>`;
    }
    const selected = (r.selected || []).map((x) =>
      `<div class="wm-row"><span>${escapeHtml(x.id)} · ${escapeHtml(x.title || "")}</span><span>score ${x.score == null ? "—" : Number(x.score).toFixed(2)}</span></div>`
    ).join("");
    const rejected = (r.rejected || []).slice(0, 5).map((x) =>
      `<div class="wm-row wm-muted"><span>${escapeHtml(x.id)} · ${escapeHtml(x.title || "")}</span><span>score ${x.score == null ? "—" : Number(x.score).toFixed(2)}</span></div>`
    ).join("");
    return `<div class="wm-section">MEMORY RETRIEVAL</div>
      <div class="wm-row"><span>Time</span><span>${escapeHtml(r.timestamp || "—")}</span></div>
      <div class="wm-row"><span>Query intent</span><span>${escapeHtml(r.query_intent || r.query || "—")}</span></div>
      <div class="wm-row"><span>Candidates examined</span><span>${fmtInt(r.candidates_examined)}</span></div>
      <div class="wm-row"><span>Selected</span><span>${fmtInt((r.selected || []).length)}</span></div>
      <div class="wm-row"><span>Injected tokens</span><span>${r.injected_tokens == null ? "Unavailable" : fmtInt(r.injected_tokens)}${r.injected_tokens_estimated ? " (estimated)" : ""}</span></div>
      <div class="wm-section">Top selected</div>${selected || "<p class='wm-empty'>None</p>"}
      <div class="wm-section">Rejected examples</div>${rejected || "<p class='wm-empty'>None</p>"}`;
  }

  function renderHistory() {
    const h = ui.history || {};
    const hist = h.history || [];
    const events = h.events || [];
    let svg = "";
    if (hist.length >= 2) {
      const vals = hist.map((x) => Number(x.tokens_used) || 0);
      const max = Math.max(...vals, 1);
      const w = 400, ht = 80;
      const pts = vals.map((v, i) => {
        const x = (i / (vals.length - 1)) * w;
        const y = ht - (v / max) * (ht - 8) - 4;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      svg = `<svg class="wm-graph" viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points="${pts}"/></svg>`;
    }
    const lines = hist.slice(-40).reverse().map((x) => {
      const t = (x.timestamp || "").slice(11, 16);
      const mark = x.compaction ? " ← compaction" : (x.retrieval ? " ← retrieval" : "");
      return `<div class="wm-row"><span>${escapeHtml(t || "—")} · ${fmtK(x.tokens_used)} · ${escapeHtml(x.pressure || "—")}</span><span class="wm-muted">${mark}</span></div>`;
    }).join("");
    const ev = events.slice(-30).reverse().map((e) => {
      return `<div class="wm-card"><div class="wm-item-title">${escapeHtml(e.type)}</div>
        <div class="wm-muted">${escapeHtml(e.ts || "")}</div>
        ${e.before != null ? `<div>Before: ${escapeHtml(String(e.before))}</div>` : ""}
        ${e.after != null ? `<div>After: ${escapeHtml(String(e.after))}</div>` : ""}
        ${e.preserved ? `<div>Preserved: ${escapeHtml((e.preserved || []).join(", "))}</div>` : ""}
        ${e.removed ? `<div>Removed: ${escapeHtml((e.removed || []).join(", "))}</div>` : ""}
        ${e.query_intent ? `<div>Query: ${escapeHtml(e.query_intent)}</div>` : ""}
      </div>`;
    }).join("");
    return `<div class="wm-section">CONTEXT HISTORY</div>${svg || "<p class='wm-muted'>Need two occupancy samples for a graph</p>"}
      ${lines || "<p class='wm-empty'>No occupancy samples yet</p>"}
      <div class="wm-section">CONTEXT EVENTS</div>
      ${ev || "<p class='wm-empty'>No context-manager events yet</p>"}`;
  }

  function renderRuntime() {
    const s = ui.summary || {};
    const rt = s.runtime || {};
    const kv = s.kv;
    const ctx = rt.context || {};
    return `<div class="wm-section">QWEN RUNTIME</div>
      <dl class="wm-kv">
        <dt>Model</dt><dd>${escapeHtml(rt.model || s.model || "—")}</dd>
        <dt>Context</dt><dd>${ctx.used == null || ctx.capacity == null ? "Unavailable" : `${fmtInt(ctx.used)} / ${fmtInt(ctx.capacity)}`}</dd>
        <dt>Prompt processing</dt><dd>${showVal(rt.prompt_processing_tok_s)}</dd>
        <dt>Generation</dt><dd>${typeof rt.generation_tok_s === "number" ? Number(rt.generation_tok_s).toFixed(1) + " tok/s" : showVal(rt.generation_tok_s)}</dd>
        <dt>VRAM</dt><dd>${showVal(rt.vram)}</dd>
        <dt>KV</dt><dd>${showVal(rt.kv)}</dd>
        <dt>Session</dt><dd>${escapeHtml(rt.session || "—")}</dd>
      </dl>
      <div class="wm-section">KV CACHE</div>
      <p class="wm-empty">${escapeHtml(statusUnavailable(kv) ? kv.reason : "Runtime does not expose this metric")}</p>`;
  }

  function renderSearch(res) {
    const rows = (res && res.results) || [];
    if (!rows.length) return `<p class="wm-empty">No matches for ${escapeHtml(res && res.query || "")}</p>`;
    return `<div class="wm-section">SEARCH</div>` + rows.map((r) =>
      `<div class="wm-card" data-open-mem="${escapeHtml(r.id || "")}">
        <div class="wm-item-title">${escapeHtml(r.title || r.id || "")} ${badge(!!r.in_context)}</div>
        <div class="wm-muted">${escapeHtml(r.type || "")} · ${escapeHtml(r.source || "")}</div>
      </div>`
    ).join("");
  }

  function bindBody(el) {
    el.querySelectorAll("[data-mem-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        ui.memFilter = btn.getAttribute("data-mem-filter") || "";
        ui.memOffset = 0;
        loadTab("memory");
      });
    });
    el.querySelectorAll("[data-mem-page]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const dir = Number(btn.getAttribute("data-mem-page") || 0);
        ui.memOffset = Math.max(0, ui.memOffset + dir * 50);
        loadTab("memory");
      });
    });
    el.querySelectorAll("[data-open-mem], [data-view-mem]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-open-mem") || btn.getAttribute("data-view-mem");
        if (!id) return;
        try {
          ui.selectedMemory = await get("memories/" + encodeURIComponent(id));
          ui.tab = "memory";
          document.querySelectorAll(".wm-tab").forEach((t) => t.classList.toggle("is-active", t.getAttribute("data-wm-tab") === "memory"));
          renderBody();
        } catch { /* ignore */ }
      });
    });
  }

  async function runSearch(q) {
    if (!q) {
      ui.search = null;
      renderBody();
      return;
    }
    try {
      ui.search = await get("search", `q=${encodeURIComponent(q)}&scope=all`);
    } catch {
      ui.search = { query: q, results: [] };
    }
    renderBody();
  }

  async function copySnapshot() {
    const snap = await get("snapshot");
    const text = JSON.stringify(snap, null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
  }

  async function exportSnapshot() {
    const snap = await get("snapshot");
    const blob = new Blob([JSON.stringify(snap, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `teela-working-memory-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function wire() {
    const ctx = $("context-stat") || document.querySelector(".context-stat");
    if (ctx && !ctx.dataset.wmWired) {
      ctx.dataset.wmWired = "true";
      ctx.addEventListener("click", () => { if (!ui.open) openDrawer(); else closeDrawer(); });
      ctx.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (!ui.open) openDrawer(); else closeDrawer(); }
      });
    }
    const close = $("wm-close");
    if (close && !close.dataset.wired) {
      close.dataset.wired = "true";
      close.addEventListener("click", closeDrawer);
    }
    const scrim = $("wm-scrim");
    if (scrim && !scrim.dataset.wired) {
      scrim.dataset.wired = "true";
      scrim.addEventListener("click", closeDrawer);
    }
    document.querySelectorAll(".wm-tab").forEach((btn) => {
      if (btn.dataset.wired) return;
      btn.dataset.wired = "true";
      btn.addEventListener("click", () => {
        ui.search = null;
        const box = $("wm-search");
        if (box) box.value = "";
        setTab(btn.getAttribute("data-wm-tab") || "overview");
      });
    });
    const refresh = $("wm-refresh");
    if (refresh && !refresh.dataset.wired) {
      refresh.dataset.wired = "true";
      refresh.addEventListener("click", () => refreshAll());
    }
    const copy = $("wm-copy");
    if (copy && !copy.dataset.wired) {
      copy.dataset.wired = "true";
      copy.addEventListener("click", () => copySnapshot());
    }
    const exp = $("wm-export");
    if (exp && !exp.dataset.wired) {
      exp.dataset.wired = "true";
      exp.addEventListener("click", () => exportSnapshot());
    }
    const search = $("wm-search");
    if (search && !search.dataset.wired) {
      search.dataset.wired = "true";
      let t = 0;
      search.addEventListener("input", () => {
        clearTimeout(t);
        const q = search.value.trim();
        t = setTimeout(() => runSearch(q), 200);
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && ui.open) closeDrawer();
    });
  }

  const g = typeof window !== "undefined" ? window : globalThis;
  g.WorkingMemory = {
    pressureFromUtilization,
    formatCompact,
    fmtK,
    syncFromBot,
    onSelectBot,
    onUsage,
    open: openDrawer,
    close: closeDrawer,
    wire,
  };
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
    else wire();
  }
})();
