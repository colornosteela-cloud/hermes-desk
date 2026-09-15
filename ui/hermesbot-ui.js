/* HermesBot chrome: window manager, avatars, layout, settings, plugins, mobile.
   Live chat/browser/TUI/shell stay in app.js. */
(function () {
  const app = document.querySelector("#app");
  const LEFT_MIN = 210;
  const LEFT_MAX = 520;
  const RIGHT_MIN = 240;
  const RIGHT_MAX = 620;
  const CENTER_MIN = 360;
  const LIVE_DESKTOP_RATIO = 0.5;
  const PHONE_MAX = 760;
  const COMPACT_MAX = 1280;
  const COLLAPSE_DRAG_PX = 96;
  const RIGHT_SPLIT_KEY = "hermes-desk-right-split";
  const AVATAR_COLORS = ["#8b5cf6", "#2f91f2", "#21b96b", "#ff7817", "#f03f55", "#ef4d98", "#5b61e8", "#3ac991", "#a97044"];

  const EMOTION_META = {
    idle: { emoji: "●", label: "Online" },
    listening: { emoji: "⌕", label: "Listening" },
    thinking: { emoji: "🧠", label: "Thinking" },
    working: { emoji: "⚙", label: "Working" },
    speaking: { emoji: "➤", label: "Replying" },
    happy: { emoji: "●", label: "Happy" },
    excited: { emoji: "↗", label: "Excited" },
    love: { emoji: "⌂", label: "Loved that" },
    sad: { emoji: "▴", label: "Sympathetic" },
    worried: { emoji: "↻", label: "On it" },
    confused: { emoji: "🧭", label: "Unsure" },
    focused: { emoji: "◈", label: "Focused" },
    celebrating: { emoji: "⛶", label: "Celebrating" },
    sleeping: { emoji: "◌", label: "Idle" },
  };

  const EMOTION_RULES = [
    { state: "celebrating", words: ["congrats", "congratulations", "shipped", "done!", "we did it", "celebrate", "won", "nailed"] },
    { state: "love", words: ["love", "❤️", "❤", "heart", "adorable", "cute"] },
    { state: "excited", words: ["wow", "amazing", "awesome", "let's go", "lets go", "fire", "yay", "!!"] },
    { state: "happy", words: ["thanks", "thank you", "great", "good job", "perfect", "nice", "sweet", "glad"] },
    { state: "sad", words: ["sad", "sorry", "unfortunately", "failed", "missed", "disappointed"] },
    { state: "worried", words: ["urgent", "error", "issue", "problem", "broken", "stuck", "help", "down", "bug", "fail"] },
    { state: "confused", words: ["what", "huh", "wait", "unclear", "confused", "why", "how come", "?"] },
    { state: "focused", words: ["plan", "schedule", "priority", "focus", "review", "check"] },
  ];

  const SURFACE_FOR_APP = { hermes: "tui", browser: "browser", files: "desktop", terminal: "shell", editor: "editor", preview: "preview", dev: "dev" };
  const APP_FOR_SURFACE = { tui: "hermes", browser: "browser", desktop: "files", shell: "terminal", editor: "editor", preview: "preview", dev: "dev" };

  const DEFAULT_PLUGINS = [
    { id: "web-search", name: "Web Search", icon: "🌐", description: "Search the public web from an agent workspace.", installed: true },
    { id: "browser", name: "Browser", icon: "🧭", description: "Interactive browser access for research and web tasks.", installed: true },
    { id: "github", name: "GitHub", icon: "◈", description: "Read repositories, issues, pull requests, and code.", installed: false },
    { id: "filesystem", name: "Filesystem", icon: "📁", description: "Expanded workspace file actions and local file access.", installed: true },
    { id: "calendar", name: "Calendar", icon: "📅", description: "Calendar lookups, planning, and scheduling.", installed: false },
    { id: "memory", name: "Agent Memory", icon: "🧠", description: "Persistent bot-specific memory and recall.", installed: true },
  ];

  const DEFAULT_USER_SETTINGS = {
    profile: { displayName: "Roni", initials: "R", agentHome: "~/.hermes" },
    defaultModel: "qwen38-27b-q5",
    models: [
      { key: "qwen38-27b-q5", model: "Qwen3.8-27B", name: "Qwen 3.8 27B Q5", baseUrl: "http://127.0.0.1:8081/v1", apiBackend: "chat_completions", contextWindow: 262144, maxCompletionTokens: 32768, apiKey: "", weights: "/home/roni/models/Qwen3.8-27B/Qwen3.8-27B-UD-Q5_K_XL.gguf" },
      { key: "grok-4.6", model: "grok-4.6", name: "Grok 4.6", baseUrl: "", apiBackend: "responses", contextWindow: 500000, maxCompletionTokens: 65536, apiKey: "" },
      { key: "grok-4.5", model: "grok-4.5", name: "Grok 4.5", baseUrl: "", apiBackend: "responses", contextWindow: 256000, maxCompletionTokens: 65536, apiKey: "" },
    ],
    environment: { modelsBaseUrl: "", modelsListUrl: "", xaiApiKey: "", agentCodeApiKey: "", defaultModel: "", configPath: "" },
    rawTomlOverride: "",
  };

  function $(id) {
    return document.getElementById(id);
  }
  function clamp(v, min, max) {
    return Math.min(max, Math.max(min, v));
  }
  function cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
  }
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function escapeAttr(value = "") {
    return escapeHtml(value).replace(/`/g, "&#096;");
  }
  function shellQuote(value) {
    return `'${String(value).replace(/'/g, `'\\''`)}'`;
  }

  function loadJson(key, fallback) {
    try {
      const stored = JSON.parse(localStorage.getItem(key) || "null");
      return stored == null ? fallback : stored;
    } catch {
      return fallback;
    }
  }

  function hashHue(id) {
    let h = 0;
    for (const ch of String(id || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return AVATAR_COLORS[h % AVATAR_COLORS.length];
  }

  const avatarStore = loadJson("hermes-desk-avatars", {});
  const emotionStore = {};
  const emotionBusy = {};
  const emotionTimers = {};
  const BUSY_STATES = new Set(["thinking", "working", "speaking", "listening"]);
  const REACTION_STATES = new Set(["happy", "excited", "love", "sad", "worried", "confused", "focused", "celebrating"]);

  function avatarMeta(bot) {
    if (!bot) return { color: AVATAR_COLORS[0], shape: "", state: "idle" };
    const saved = avatarStore[bot.id] || {};
    const av = bot.avatar && typeof bot.avatar === "object" ? bot.avatar : {};
    return {
      color: av.color || bot.avatar_color || saved.color || hashHue(bot.id),
      shape: av.shape || bot.avatar_shape || saved.shape || "",
      state: emotionStore[bot.id] || "idle",
    };
  }

  function saveAvatar(botId, patch) {
    const prev = { ...(avatarStore[botId] || {}) };
    delete prev.state;
    const next = { ...prev, ...(patch || {}) };
    delete next.state;
    avatarStore[botId] = next;
    localStorage.setItem("hermes-desk-avatars", JSON.stringify(avatarStore));
  }

  const hydratingAvatars = new Set();
  async function hydrateLocalAvatars(bots) {
    if (!window.deskUpdateBot) return;
    for (const bot of bots || []) {
      if (!bot?.id || bot.remote || hydratingAvatars.has(bot.id)) continue;
      const saved = avatarStore[bot.id] || {};
      const av = bot.avatar && typeof bot.avatar === "object" ? bot.avatar : {};
      const serverColor = String(av.color || bot.avatar_color || "").trim();
      const serverShape = String(av.shape || bot.avatar_shape || "").trim();
      const patch = {};
      if (!serverColor && saved.color) patch.avatar_color = saved.color;
      if (!serverShape && saved.shape) patch.avatar_shape = saved.shape;
      if (!Object.keys(patch).length) continue;
      hydratingAvatars.add(bot.id);
      try {
        await window.deskUpdateBot(bot.id, patch);
      } catch {
        /* keep localStorage; try again on the next roster refresh */
      } finally {
        hydratingAvatars.delete(bot.id);
      }
    }
  }

  function inferEmotion(text = "") {
    const value = String(text).toLowerCase();
    if (!value.trim()) return "happy";
    for (const rule of EMOTION_RULES) {
      if (rule.words.some((word) => value.includes(word))) return rule.state;
    }
    if ((value.match(/!/g) || []).length >= 2) return "excited";
    if (value.includes("?")) return "confused";
    return "happy";
  }

  function popAvatarEmotion(root) {
    if (!root) return;
    root.classList.remove("pop-emotion");
    void root.offsetWidth;
    root.classList.add("pop-emotion");
    const pop = root.querySelector(".emotion-pop");
    if (pop) {
      const meta = EMOTION_META[root.dataset.state] || {};
      pop.textContent = meta.emoji || "";
    }
  }

  function avatarHTML(bot, cls = "avatar glance") {
    const meta = avatarMeta(bot);
    const emo = (EMOTION_META[meta.state] || {}).emoji || "";
    return `<div class="${cls} ${meta.shape || ""}" data-state="${meta.state || "idle"}" style="--c:${meta.color}">
      <span class="shine"></span>
      <span class="mouth"></span>
      <span class="emotion-pop">${emo}</span>
    </div>`;
  }

  function paintEmotionOn(root, state, pop) {
    if (!root) return;
    root.dataset.state = state || "idle";
    if (!root.querySelector(".mouth")) root.insertAdjacentHTML("beforeend", '<span class="mouth"></span><span class="emotion-pop"></span>');
    const badge = root.querySelector(".emotion-pop");
    if (badge) badge.textContent = (EMOTION_META[state] || {}).emoji || "";
    if (pop) popAvatarEmotion(root);
    else root.classList.remove("pop-emotion");
  }

  function setAgentEmotion(botId, state, { persist = false, pop = true, hold } = {}) {
    const next = state || "idle";
    clearTimeout(emotionTimers[botId]);
    emotionStore[botId] = next;
    if (next === "thinking" || next === "working" || next === "speaking") emotionBusy[botId] = true;
    else if (next !== "listening") emotionBusy[botId] = false;

    const header = $("headerAvatar");
    const selected = window.deskState?.selected;
    if (header && selected === botId) paintEmotionOn(header, next, pop);
    document.querySelectorAll("#agentIconRail .rail-agent, #roster .agent-row, #mobile-featured .featured-card, #mobile-list .mobile-row").forEach((wrap) => {
      if (wrap.dataset.agentId !== botId && wrap.dataset.agent !== botId) return;
      const avatar = wrap.classList.contains("avatar") || wrap.classList.contains("mini-avatar") ? wrap : wrap.querySelector(".avatar, .mini-avatar");
      if (!avatar) return;
      paintEmotionOn(avatar, next, pop);
    });

    if (REACTION_STATES.has(next)) {
      const ms = typeof hold === "number" ? hold : 2600;
      emotionTimers[botId] = setTimeout(() => {
        if (emotionBusy[botId]) return;
        setAgentEmotion(botId, "idle", { persist: false, pop: false });
      }, ms);
    }
  }

  function settleEmotion(botId, delay = 1100) {
    emotionBusy[botId] = false;
    clearTimeout(emotionTimers[botId]);
    emotionTimers[botId] = setTimeout(() => {
      if (emotionBusy[botId]) return;
      setAgentEmotion(botId, "idle", { persist: false, pop: false });
    }, delay);
  }

  function isEmotionBusy(botId) {
    return Boolean(emotionBusy[botId]);
  }

  function syncEmotionFromStatus(bot) {
    if (!bot?.id) return;
    const text = String(bot.status || "").toLowerCase();
    if (text.includes("working") || text.includes("routine")) {
      setAgentEmotion(bot.id, "working", { persist: false, pop: true });
      return;
    }
    if (text.startsWith("error")) {
      setAgentEmotion(bot.id, "worried", { persist: false, pop: true });
      return;
    }
    if (text.includes("ready") || text.includes("undid") || text === "online" || !text) {
      settleEmotion(bot.id);
    }
  }

  function paintHeaderAvatar(bot) {
    const header = $("headerAvatar");
    if (!header || !bot) return;
    const meta = avatarMeta(bot);
    header.className = `mini-avatar glance ${meta.shape || ""}`;
    header.style.setProperty("--c", meta.color);
    header.dataset.state = meta.state || "idle";
    if (!header.querySelector(".shine")) header.insertAdjacentHTML("afterbegin", '<span class="shine"></span>');
    if (!header.querySelector(".mouth")) header.insertAdjacentHTML("beforeend", '<span class="mouth"></span><span class="emotion-pop"></span>');
    const pop = header.querySelector(".emotion-pop");
    if (pop) pop.textContent = (EMOTION_META[meta.state] || {}).emoji || "";
  }

  /* Layout */
  function availableForSidePanels() {
    return Math.max(0, window.innerWidth - CENTER_MIN - 10);
  }
  function getLayoutMode() {
    if (document.body.classList.contains("observer-mode") || new URLSearchParams(location.search).get("observe")) {
      return "desktop";
    }
    if (window.matchMedia(`(max-width: ${PHONE_MAX}px)`).matches) return "phone";
    if (window.matchMedia(`(max-width: ${COMPACT_MAX}px)`).matches) return "compact";
    return "desktop";
  }
  function persistPanelCollapse() {
    return getLayoutMode() !== "phone";
  }
  function isRightCollapsed() {
    return app.classList.contains("right-collapsed") || app.classList.contains("screen-closed");
  }
  function remainingAfterLeft() {
    const total = app.getBoundingClientRect().width || window.innerWidth;
    const leftCollapsed = app.classList.contains("left-collapsed");
    const left = leftCollapsed ? 0 : (parseFloat(getComputedStyle(app).getPropertyValue("--left-w")) || 0);
    const resizers = (leftCollapsed ? 0 : 5) + 5;
    return Math.max(0, total - left - resizers);
  }
  function rightPaneMax() {
    const total = app.getBoundingClientRect().width || window.innerWidth;
    const halfScreen = Math.round(total * LIVE_DESKTOP_RATIO);
    const maxByChat = Math.max(RIGHT_MIN, remainingAfterLeft() - CENTER_MIN);
    return Math.max(RIGHT_MIN, Math.min(halfScreen, maxByChat));
  }
  function currentRightWidth() {
    if (isRightCollapsed()) return 0;
    const col = (app.style.getPropertyValue("--right-col") || "").trim();
    const fromCol = parseFloat(col);
    if (Number.isFinite(fromCol)) return fromCol;
    const pane = app.querySelector(".workspace-column");
    const measured = pane ? pane.getBoundingClientRect().width : 0;
    if (measured > 0) return measured;
    return parseFloat(getComputedStyle(app).getPropertyValue("--right-w")) || 0;
  }
  function rightSplitMode() {
    return app.dataset.rightSplit || localStorage.getItem(RIGHT_SPLIT_KEY) || "equal";
  }
  function setLeftWidth(px, persist = true, opts = {}) {
    const right = currentRightWidth();
    const dynamicMax = Math.max(LEFT_MIN, Math.min(LEFT_MAX, availableForSidePanels() - right));
    const min = opts.allowCollapse ? 0 : LEFT_MIN;
    const width = clamp(px, min, dynamicMax);
    app.style.setProperty("--left-w", `${width}px`);
    document.documentElement.style.setProperty("--left-w", `${width}px`);
    document.documentElement.style.setProperty("--hallway-w", `${width}px`);
    if (persist && width >= LEFT_MIN) {
      localStorage.setItem("teela-left-width", String(width));
      localStorage.setItem("hermes-desk-hallway-w", String(width));
    }
    return width;
  }
  function isLiveDesktopEntered() {
    return document.body.classList.contains("live-desktop-entered");
  }
  function liveDesktopPaneWidth() {
    return rightPaneMax();
  }
  function applyLiveDesktopWidth() {
    if (isRightCollapsed()) {
      app.style.removeProperty("--right-col");
      app.style.setProperty("--right-w", "0px");
      document.documentElement.style.setProperty("--right-w", "0px");
      document.documentElement.style.setProperty("--screen-w", "0px");
      return 0;
    }
    app.dataset.rightSplit = "equal";
    localStorage.setItem(RIGHT_SPLIT_KEY, "equal");
    app.style.removeProperty("--right-col");
    const width = liveDesktopPaneWidth();
    app.style.setProperty("--right-w", `${width}px`);
    document.documentElement.style.setProperty("--right-w", `${width}px`);
    document.documentElement.style.setProperty("--screen-w", `${width}px`);
    return width;
  }
  function setRightWidth(px, persist = true, opts = {}) {
    const max = rightPaneMax();
    const min = opts.allowCollapse ? 0 : RIGHT_MIN;
    const width = clamp(px, min, max);
    app.style.setProperty("--right-w", `${width}px`);
    app.style.setProperty("--right-col", `min(${width}px, 50%)`);
    document.documentElement.style.setProperty("--right-w", `${width}px`);
    document.documentElement.style.setProperty("--screen-w", `${width}px`);
    if (width >= RIGHT_MIN) {
      app.dataset.rightSplit = "custom";
      if (persist) {
        localStorage.setItem(RIGHT_SPLIT_KEY, "custom");
        localStorage.setItem("teela-right-width", String(width));
        localStorage.setItem("hermes-desk-screen-w", String(width));
      }
    }
    return width;
  }
  function constrainPanelWidths(persist = false) {
    if (window.innerWidth <= COMPACT_MAX) return;
    if (isRightCollapsed()) {
      app.style.removeProperty("--right-col");
    } else if (rightSplitMode() === "custom") {
      const saved = parseFloat(localStorage.getItem("hermes-desk-screen-w") || localStorage.getItem("teela-right-width"));
      const inline = parseFloat(app.style.getPropertyValue("--right-col"));
      const right = Number.isFinite(saved) ? saved : inline;
      if (Number.isFinite(right)) setRightWidth(right, persist);
    } else {
      app.style.removeProperty("--right-col");
      const width = liveDesktopPaneWidth();
      app.style.setProperty("--right-w", `${width}px`);
      document.documentElement.style.setProperty("--right-w", `${width}px`);
      document.documentElement.style.setProperty("--screen-w", `${width}px`);
    }
    if (!app.classList.contains("left-collapsed")) {
      const left = parseFloat(getComputedStyle(app).getPropertyValue("--left-w")) || 315;
      setLeftWidth(Math.round(left), persist);
    }
  }
  function restorePanelWidths() {
    const savedLeft = parseFloat(localStorage.getItem("hermes-desk-hallway-w") || localStorage.getItem("teela-left-width"));
    if (Number.isFinite(savedLeft)) app.style.setProperty("--left-w", `${savedLeft}px`);
    const split = localStorage.getItem(RIGHT_SPLIT_KEY);
    const savedRight = parseFloat(localStorage.getItem("hermes-desk-screen-w") || localStorage.getItem("teela-right-width"));
    if (split === "custom" && Number.isFinite(savedRight)) {
      app.dataset.rightSplit = "custom";
      setRightWidth(savedRight, false);
    } else {
      app.dataset.rightSplit = "equal";
    }
    constrainPanelWidths(false);
  }
  function updatePanelHandles() {
    $("reopen-left")?.classList.toggle("hidden", !app.classList.contains("left-collapsed"));
    $("reopen-right")?.classList.toggle("hidden", !(app.classList.contains("right-collapsed") || app.classList.contains("screen-closed")));
  }
  function setupResizer(el, side) {
    if (!el) return;
    const onPointerMove = (e) => {
      if (window.innerWidth <= COMPACT_MAX) return;
      if (side === "left") setLeftWidth(e.clientX - app.getBoundingClientRect().left, false, { allowCollapse: true });
      else setRightWidth(app.getBoundingClientRect().right - e.clientX, false, { allowCollapse: true });
    };
    const end = (e) => {
      el.classList.remove("dragging");
      document.body.classList.remove("resizing");
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", end);
      if (side === "left") {
        const width = e.clientX - app.getBoundingClientRect().left;
        if (width < COLLAPSE_DRAG_PX) toggleLeft(false);
        else setLeftWidth(width, true);
      } else {
        const width = app.getBoundingClientRect().right - e.clientX;
        if (width < COLLAPSE_DRAG_PX) toggleRight(false);
        else setRightWidth(width, true);
      }
      constrainPanelWidths(true);
      window.dispatchEvent(new Event("resize"));
    };
    el.addEventListener("pointerdown", (e) => {
      if (window.innerWidth <= COMPACT_MAX) return;
      e.preventDefault();
      if (side === "left" && app.classList.contains("left-collapsed")) return;
      if (side === "right" && (app.classList.contains("right-collapsed") || app.classList.contains("screen-closed"))) return;
      el.setPointerCapture?.(e.pointerId);
      el.classList.add("dragging");
      document.body.classList.add("resizing");
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", end);
    });
    el.addEventListener("dblclick", () => {
      if (side === "left") setLeftWidth(315);
      else applyLiveDesktopWidth();
    });
  }

  let lastLayoutMode = null;
  function applyResponsiveLayout() {
    const mode = getLayoutMode();
    document.body.dataset.layout = mode;
    app.classList.toggle("compact-layout", mode !== "desktop");
    if (mode === "desktop") {
      document.body.classList.remove("mobile-chat", "workspace-open", "hallway-open");
      if (lastLayoutMode && lastLayoutMode !== "desktop") {
        app.classList.toggle("left-collapsed", localStorage.getItem("teela-left-collapsed") === "1" || localStorage.getItem("hermes-desk-left-open") === "0");
        const rightClosed = localStorage.getItem("teela-right-collapsed") === "1" || localStorage.getItem("hermes-desk-screen-open") === "0";
        app.classList.toggle("right-collapsed", rightClosed);
        app.classList.toggle("screen-closed", rightClosed);
      }
      constrainPanelWidths(false);
    } else if (mode === "compact") {
      document.body.classList.remove("mobile-chat", "workspace-open");
      if (lastLayoutMode !== "compact") {
        app.classList.add("left-collapsed");
        document.body.classList.remove("hallway-open");
        if (lastLayoutMode === "phone") {
          app.classList.remove("right-collapsed", "screen-closed");
        }
      }
    } else if (lastLayoutMode !== "phone") {
      app.classList.add("left-collapsed", "right-collapsed", "screen-closed");
      document.body.classList.remove("workspace-open", "hallway-open");
      if (window.deskState?.selected) document.body.classList.add("mobile-chat");
      else document.body.classList.remove("mobile-chat");
    }
    lastLayoutMode = mode;
    updatePanelHandles();
    $("layoutBackdrop")?.classList.toggle("is-on", mode !== "desktop" && !app.classList.contains("left-collapsed"));
  }

  function toggleLeft(forceOpen = null) {
    if (getLayoutMode() === "phone") {
      setMobileView(forceOpen === false ? "chat" : "agents");
      return;
    }
    const shouldOpen = forceOpen === null ? app.classList.contains("left-collapsed") : forceOpen;
    app.classList.toggle("left-collapsed", !shouldOpen);
    if (persistPanelCollapse()) {
      localStorage.setItem("teela-left-collapsed", shouldOpen ? "0" : "1");
      localStorage.setItem("hermes-desk-left-open", shouldOpen ? "1" : "0");
    }
    updatePanelHandles();
    window.dispatchEvent(new Event("resize"));
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
  function toggleRight(forceOpen = null) {
    if (getLayoutMode() === "phone") {
      const open = forceOpen === null ? !document.body.classList.contains("workspace-open") : forceOpen;
      document.body.classList.toggle("workspace-open", open);
      if (open) document.body.classList.add("mobile-chat");
      app.classList.toggle("right-collapsed", !open);
      app.classList.toggle("screen-closed", !open);
      window.dispatchEvent(new Event("resize"));
      return;
    }
    const shouldOpen = forceOpen === null ? app.classList.contains("right-collapsed") || app.classList.contains("screen-closed") : forceOpen;
    app.classList.toggle("right-collapsed", !shouldOpen);
    app.classList.toggle("screen-closed", !shouldOpen);
    if (shouldOpen) {
      if (getLayoutMode() === "compact") app.classList.add("left-collapsed");
      if (rightSplitMode() === "custom") {
        const saved = parseFloat(localStorage.getItem("hermes-desk-screen-w") || localStorage.getItem("teela-right-width"));
        if (Number.isFinite(saved)) setRightWidth(saved, false);
        else app.style.removeProperty("--right-col");
      } else {
        app.style.removeProperty("--right-col");
      }
    } else {
      app.style.removeProperty("--right-col");
    }
    if (persistPanelCollapse()) {
      localStorage.setItem("teela-right-collapsed", shouldOpen ? "0" : "1");
      localStorage.setItem("hermes-desk-screen-open", shouldOpen ? "1" : "0");
    }
    updatePanelHandles();
    window.dispatchEvent(new Event("resize"));
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
  function closeCompactDrawers() {
    app.classList.add("left-collapsed");
    document.body.classList.remove("hallway-open");
    updatePanelHandles();
  }
  function setMobileView(view) {
    const mode = getLayoutMode();
    if (mode === "desktop") return;
    if (mode === "compact") {
      if (view === "agents") toggleLeft(true);
      else if (view === "chat") closeCompactDrawers();
      else if (view === "workspace") toggleRight(true);
      return;
    }
    if (view === "agents") {
      document.body.classList.remove("mobile-chat", "workspace-open");
      app.classList.add("left-collapsed", "right-collapsed", "screen-closed");
    } else if (view === "chat") {
      document.body.classList.add("mobile-chat");
      document.body.classList.remove("workspace-open");
      app.classList.add("right-collapsed", "screen-closed");
    } else if (view === "workspace") {
      document.body.classList.add("mobile-chat", "workspace-open");
      app.classList.remove("right-collapsed", "screen-closed");
    }
    requestAnimationFrame(() => {
      window.fitPhoneFrame?.();
      if (view === "chat") {
        const sc = $("transcript");
        if (sc) sc.scrollTop = sc.scrollHeight;
      }
      if (view === "workspace") window.dispatchEvent(new Event("resize"));
    });
  }

  /* Window manager */
  let desktopZCounter = 30;
  const desktopWindowState = {};
  function getDesktopArea() {
    return document.querySelector(".ubuntu-desktop-area");
  }
  function getAppWindow(name) {
    return document.querySelector(`.app-window[data-window-app="${name}"]`);
  }
  function rememberWindowGeometry(win) {
    if (!win || win.classList.contains("maximized-window")) return;
    desktopWindowState[win.dataset.windowApp] = {
      left: win.style.left,
      top: win.style.top,
      width: win.style.width,
      height: win.style.height,
    };
  }
  function restoreWindowGeometry(win) {
    const saved = desktopWindowState[win?.dataset.windowApp];
    if (!saved) return;
    win.style.left = saved.left;
    win.style.top = saved.top;
    win.style.width = saved.width;
    win.style.height = saved.height;
  }
  function isWindowLive(name) {
    const win = getAppWindow(name);
    return Boolean(win && win.dataset.open === "true" && !win.classList.contains("hidden-window") && !win.classList.contains("minimized-window"));
  }
  function updateDesktopDockState() {
    document.querySelectorAll(".dock-app[data-desktop-app]").forEach((btn) => {
      const win = getAppWindow(btn.dataset.desktopApp);
      const open = Boolean(win && win.dataset.open === "true");
      const visible = Boolean(open && !win.classList.contains("hidden-window") && !win.classList.contains("minimized-window"));
      btn.classList.toggle("running", open);
      btn.classList.toggle("active", visible && win.classList.contains("focused-window"));
    });
    document.querySelectorAll(".desktop-shortcut[data-desktop-app]").forEach((btn) => {
      const appName = btn.dataset.desktopApp === "workspace" ? "files" : btn.dataset.desktopApp;
      const win = getAppWindow(appName);
      btn.classList.toggle("active", Boolean(win && win.classList.contains("focused-window") && !win.classList.contains("minimized-window")));
    });
  }
  function focusDesktopWindow(win) {
    if (!win) return;
    desktopZCounter += 1;
    if (desktopZCounter > 80) desktopZCounter = 31;
    document.querySelectorAll(".app-window").forEach((w) => w.classList.remove("focused-window"));
    win.classList.add("focused-window");
    win.style.zIndex = String(desktopZCounter);
    updateDesktopDockState();
    const surface = SURFACE_FOR_APP[win.dataset.windowApp];
    if (surface) document.dispatchEvent(new CustomEvent("desk-app", { detail: { app: win.dataset.windowApp, surface } }));
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
  function selectedHasRobotSimulator() {
    const st = window.deskState;
    const bot = (st?.bots || []).find((x) => x.id === st.selected);
    if (typeof window.botHasRobotSimulator === "function") return window.botHasRobotSimulator(bot);
    return Boolean(bot) && bot.kind !== "hermes";
  }
  function openDesktopWindow(name, { focus = true } = {}) {
    if (name === "workspace") name = "files";
    const win = getAppWindow(name);
    if (!win) return;
    win.dataset.open = "true";
    win.classList.remove("hidden-window", "minimized-window");
    if (name === "preview" && selectedHasRobotSimulator() && typeof window.loadRobotSimulator === "function") {
      const frame = $("app-preview-frame");
      const src = frame?.getAttribute("src") || "";
      if (!src || src === "about:blank") window.loadRobotSimulator();
    }
    if (focus) focusDesktopWindow(win);
    else updateDesktopDockState();
  }
  function minimizeDesktopWindow(win) {
    if (!win) return;
    rememberWindowGeometry(win);
    win.classList.add("minimized-window");
    win.classList.remove("focused-window");
    document.getElementById("model-menu")?.setAttribute("hidden", "");
    document.getElementById("model-select-btn")?.setAttribute("aria-expanded", "false");
    document.getElementById("more-menu")?.setAttribute("hidden", "");
    updateDesktopDockState();
    const visible = [...document.querySelectorAll(".app-window")].filter((w) => w.dataset.open === "true" && !w.classList.contains("hidden-window") && !w.classList.contains("minimized-window"));
    if (visible.length) {
      visible.sort((a, b) => (Number(b.style.zIndex) || 0) - (Number(a.style.zIndex) || 0));
      focusDesktopWindow(visible[0]);
    }
  }
  function closeDesktopWindow(win) {
    if (!win) return;
    rememberWindowGeometry(win);
    win.dataset.open = "false";
    win.classList.add("hidden-window");
    win.classList.remove("minimized-window", "focused-window", "maximized-window");
    updateDesktopDockState();
    const visible = [...document.querySelectorAll(".app-window")].filter((w) => w.dataset.open === "true" && !w.classList.contains("hidden-window") && !w.classList.contains("minimized-window"));
    if (visible.length) {
      visible.sort((a, b) => (Number(b.style.zIndex) || 0) - (Number(a.style.zIndex) || 0));
      focusDesktopWindow(visible[0]);
    }
  }
  function toggleMaximizeDesktopWindow(win) {
    if (!win) return;
    if (win.classList.contains("maximized-window")) {
      win.classList.remove("maximized-window");
      restoreWindowGeometry(win);
    } else {
      rememberWindowGeometry(win);
      win.classList.add("maximized-window");
    }
    focusDesktopWindow(win);
  }
  function ensureMaximizedDesktopWindow(win) {
    if (!win) return;
    win.dataset.open = "true";
    win.classList.remove("hidden-window", "minimized-window");
    if (!win.classList.contains("maximized-window")) {
      rememberWindowGeometry(win);
      win.classList.add("maximized-window");
    }
    focusDesktopWindow(win);
  }
  function showRobotOnAgentDesktop() {
    if (!selectedHasRobotSimulator()) return;
    const win = getAppWindow("preview");
    if (!win) return;
    if (typeof window.loadRobotSimulator === "function") {
      const frame = $("app-preview-frame");
      const src = frame?.getAttribute("src") || "";
      if (!src || src === "about:blank") window.loadRobotSimulator();
    }
    ensureMaximizedDesktopWindow(win);
  }
  function currentDockEdge() {
    const dock = document.getElementById("dock");
    return ["left", "right", "top", "bottom"].find((edge) => dock?.classList.contains(`dock-${edge}`)) || "left";
  }
  function syncDockEdgeClass() {
    const area = document.querySelector(".ubuntu-desktop-area");
    const dock = document.getElementById("dock");
    if (!area || !dock) return;
    area.classList.remove("dock-edge-left", "dock-edge-right", "dock-edge-top", "dock-edge-bottom");
    area.classList.add(`dock-edge-${currentDockEdge()}`);
  }
  function wireDockSnap() {
    const dock = document.getElementById("dock");
    const area = document.querySelector(".ubuntu-desktop-area");
    if (!dock || !area || dock.dataset.snapWired === "true") return;
    dock.dataset.snapWired = "true";
    const saved = localStorage.getItem("minios-dock-edge");
    if (["left", "right", "top", "bottom"].includes(saved)) {
      dock.classList.remove("dock-left", "dock-right", "dock-top", "dock-bottom");
      dock.classList.add(`dock-${saved}`);
    }
    syncDockEdgeClass();
    const THRESHOLD = 6;
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let origLeft = 0;
    let origTop = 0;
    let lastX = 0;
    let lastY = 0;
    const areaPoint = (clientX, clientY) => {
      const ar = area.getBoundingClientRect();
      return { x: clientX - ar.left, y: clientY - ar.top, ar };
    };
    const finish = () => {
      if (!dragging) return;
      dragging = false;
      if (!moved) return;
      dock.classList.remove("dragging");
      const { x, y, ar } = areaPoint(lastX, lastY);
      const distances = {
        left: Math.max(0, x),
        right: Math.max(0, ar.width - x),
        top: Math.max(0, y),
        bottom: Math.max(0, ar.height - y),
      };
      const edge = Object.keys(distances).reduce((a, b) => (distances[a] <= distances[b] ? a : b));
      dock.style.left = "";
      dock.style.top = "";
      dock.style.right = "";
      dock.style.bottom = "";
      dock.style.flexDirection = "";
      dock.style.transform = "";
      dock.classList.remove("dock-left", "dock-right", "dock-top", "dock-bottom");
      dock.classList.add(`dock-${edge}`);
      localStorage.setItem("minios-dock-edge", edge);
      syncDockEdgeClass();
      window.postMiniosView?.();
      $("dock-hint")?.classList.add("is-hidden");
      dock.addEventListener("click", (ev) => ev.stopPropagation(), { capture: true, once: true });
      sizeLiveDesktopArea();
    };
    dock.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      dragging = true;
      moved = false;
      startX = e.clientX;
      startY = e.clientY;
      lastX = e.clientX;
      lastY = e.clientY;
      const ar = area.getBoundingClientRect();
      const r = dock.getBoundingClientRect();
      origLeft = r.left - ar.left;
      origTop = r.top - ar.top;
    });
    window.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      lastX = e.clientX;
      lastY = e.clientY;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (!moved && (Math.abs(dx) > THRESHOLD || Math.abs(dy) > THRESHOLD)) {
        moved = true;
        dock.classList.add("dragging");
        dock.classList.remove("dock-left", "dock-right", "dock-top", "dock-bottom");
        dock.style.flexDirection = Math.abs(dy) >= Math.abs(dx) ? "row" : "column";
        dock.style.transform = "none";
        dock.style.right = "";
        dock.style.bottom = "";
        try { dock.setPointerCapture(e.pointerId); } catch { /* optional */ }
      }
      if (moved) {
        const ar = area.getBoundingClientRect();
        const maxL = Math.max(0, ar.width - dock.offsetWidth);
        const maxT = Math.max(0, ar.height - dock.offsetHeight);
        dock.style.left = `${Math.min(maxL, Math.max(0, origLeft + dx))}px`;
        dock.style.top = `${Math.min(maxT, Math.max(0, origTop + dy))}px`;
      }
    });
    window.addEventListener("pointerup", finish);
    dock.addEventListener("pointerup", finish);
    dock.addEventListener("lostpointercapture", finish);
  }
  function wireDockMagnify() {
    const dock = document.getElementById("dock");
    if (!dock || dock.dataset.magnifyWired === "true") return;
    dock.dataset.magnifyWired = "true";
    const icons = () => [...dock.querySelectorAll(".dock-app")];
    const metrics = () => {
      const icon = parseFloat(getComputedStyle(document.querySelector(".ubuntu-desktop-area") || dock).getPropertyValue("--dock-icon")) || 46;
      return { extra: 0.55, spread: Math.max(26, icon * 0.95), pop: Math.max(7, icon * 0.26) };
    };
    const reset = () => {
      icons().forEach((btn) => {
        btn.style.transform = "";
        btn.style.zIndex = "";
        btn.removeAttribute("data-dock-hot");
      });
    };
    const apply = (e) => {
      if (dock.classList.contains("dragging")) return;
      const { extra: MAX_EXTRA, spread: SPREAD, pop: POP } = metrics();
      const vertical = dock.classList.contains("dock-left") || dock.classList.contains("dock-right");
      const mouseCoord = vertical ? e.clientY : e.clientX;
      let hot = null;
      let hotDist = Infinity;
      icons().forEach((btn) => {
        const r = btn.getBoundingClientRect();
        const center = vertical ? r.top + r.height / 2 : r.left + r.width / 2;
        const dist = mouseCoord - center;
        const scale = 1 + MAX_EXTRA * Math.exp(-(dist * dist) / (2 * SPREAD * SPREAD));
        const extra = (scale - 1) * POP;
        let translate = "translateY(-" + extra.toFixed(1) + "px)";
        if (dock.classList.contains("dock-left")) translate = "translateX(" + extra.toFixed(1) + "px)";
        else if (dock.classList.contains("dock-right")) translate = "translateX(-" + extra.toFixed(1) + "px)";
        else if (dock.classList.contains("dock-top")) translate = "translateY(" + extra.toFixed(1) + "px)";
        btn.style.transform = `${translate} scale(${scale.toFixed(3)})`;
        btn.style.zIndex = String(Math.round(scale * 100));
        if (Math.abs(dist) < hotDist) {
          hotDist = Math.abs(dist);
          hot = btn;
        }
      });
      icons().forEach((btn) => {
        if (btn === hot && hotDist < SPREAD * 1.4) btn.setAttribute("data-dock-hot", "true");
        else btn.removeAttribute("data-dock-hot");
      });
    };
    dock.addEventListener("pointermove", apply);
    dock.addEventListener("pointerleave", reset);
    dock.addEventListener("pointercancel", reset);
  }
  function wireDesktopWindowManager() {
    const area = getDesktopArea();
    if (!area || area.dataset.windowManagerWired === "true") return;
    area.dataset.windowManagerWired = "true";
    document.querySelectorAll(".app-window").forEach((win) => {
      win.addEventListener("pointerdown", () => focusDesktopWindow(win));
      const handle = win.querySelector(".window-drag-handle");
      if (handle) {
        handle.addEventListener("pointerdown", (e) => {
          if (e.target.closest(".window-controls")) return;
          if (win.classList.contains("maximized-window")) return;
          e.preventDefault();
          focusDesktopWindow(win);
          handle.setPointerCapture?.(e.pointerId);
          const areaRect = area.getBoundingClientRect();
          const winRect = win.getBoundingClientRect();
          const startX = e.clientX;
          const startY = e.clientY;
          const startLeft = winRect.left - areaRect.left;
          const startTop = winRect.top - areaRect.top;
          area.classList.add("window-dragging");
          const move = (ev) => {
            const edge = currentDockEdge();
            const dockEl = document.getElementById("dock");
            const pad = dockEl
              ? Math.round(((edge === "left" || edge === "right") ? dockEl.offsetWidth : dockEl.offsetHeight) + 8)
              : 40;
            const minLeft = edge === "left" ? pad : 0;
            const minTop = edge === "top" ? pad : 0;
            const maxLeft = Math.max(minLeft, area.clientWidth - win.offsetWidth - (edge === "right" ? pad : 0));
            const maxTop = Math.max(minTop, area.clientHeight - win.offsetHeight - (edge === "bottom" ? pad : 8));
            win.style.left = `${Math.round(Math.min(maxLeft, Math.max(minLeft, startLeft + (ev.clientX - startX))))}px`;
            win.style.top = `${Math.round(Math.min(maxTop, Math.max(minTop, startTop + (ev.clientY - startY))))}px`;
          };
          const end = () => {
            area.classList.remove("window-dragging");
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", end);
            rememberWindowGeometry(win);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", end, { once: true });
        });
        handle.addEventListener("dblclick", (e) => {
          if (e.target.closest(".window-controls")) return;
          toggleMaximizeDesktopWindow(win);
        });
      }
      win.querySelectorAll("[data-window-action]").forEach((btn) => {
        btn.addEventListener("pointerdown", (e) => e.stopPropagation());
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const action = btn.dataset.windowAction;
          if (action === "minimize") minimizeDesktopWindow(win);
          else if (action === "maximize") toggleMaximizeDesktopWindow(win);
          else if (action === "close") closeDesktopWindow(win);
        });
      });
    });
    document.querySelectorAll("[data-desktop-app]").forEach((btn) => {
      btn.addEventListener("click", () => {
        let name = btn.dataset.desktopApp;
        if (name === "workspace") {
          openDesktopWindow("files");
          const title = getAppWindow("files")?.querySelector(".window-title-left strong");
          if (title) title.textContent = "Files — Agent Workspace";
          return;
        }
        const win = getAppWindow(name);
        if (!win) return;
        if (btn.classList.contains("dock-app") && win.classList.contains("focused-window") && !win.classList.contains("minimized-window") && !win.classList.contains("hidden-window")) {
          minimizeDesktopWindow(win);
        } else {
          openDesktopWindow(name);
        }
      });
    });
    showRobotOnAgentDesktop();
  }
  let lastDesktopW = 0;
  let lastDesktopH = 0;
  let desktopResizeTimer = 0;
  let fittingDesktop = false;
  function sizeLiveDesktopArea() {
    const viewer = $("ubuntuDesktopViewer");
    const section = document.querySelector(".live-desktop-section");
    const area = document.querySelector(".ubuntu-desktop-area");
    const dock = $("dock");
    if (!viewer || !section || !area) return;
    if (document.body.classList.contains("desktop-takeover-active")) return;
    const phone = window.matchMedia("(max-width: 760px)").matches;
    const header = section.querySelector(".right-section-header");
    const topbar = viewer.querySelector(".ubuntu-topbar");
    const chrome = (header?.offsetHeight || 0) + (topbar?.offsetHeight || 0);
    const viewerH = viewer.clientHeight || 0;
    const availH = Math.max(80, Math.round(
      viewerH > 40 ? viewerH - (topbar?.offsetHeight || 0) : section.clientHeight - chrome
    ));
    if (phone) {
      const width = section.getBoundingClientRect().width;
      const fromWidth = Math.round(width * 0.72);
      const target = Math.max(160, Math.min(availH, fromWidth, 390));
      area.style.height = `${target}px`;
    } else {
      area.style.width = "100%";
      area.style.minHeight = "0";
      area.style.flex = "1 1 0";
      area.style.height = `${availH}px`;
    }
    const w = Math.max(1, Math.round(area.clientWidth));
    const h = Math.max(1, Math.round(area.clientHeight));
    area.style.setProperty("--desktop-res-w", `${w}px`);
    area.style.setProperty("--desktop-res-h", `${h}px`);
    area.dataset.resolution = `${w}x${h}`;
    const short = Math.max(1, Math.min(w, h));
    const icon = Math.round(Math.max(22, Math.min(46, short * 0.09)));
    area.style.setProperty("--dock-icon", `${icon}px`);
    area.style.setProperty("--dock-gap", `${Math.max(4, Math.round(icon * 0.18))}px`);
    if (lastDesktopW && lastDesktopH && (w !== lastDesktopW || h !== lastDesktopH)) {
      const sx = w / lastDesktopW;
      const sy = h / lastDesktopH;
      document.querySelectorAll(".app-window").forEach((win) => {
        if (win.classList.contains("hidden-window") || win.classList.contains("minimized-window") || win.classList.contains("maximized-window")) return;
        const left = parseFloat(win.style.left) || 0;
        const top = parseFloat(win.style.top) || 0;
        win.style.left = `${Math.round(left * sx)}px`;
        win.style.top = `${Math.round(top * sy)}px`;
        if (/%|calc\(/.test(`${win.style.width || ""} ${win.style.height || ""}`)) return;
        const pw = parseFloat(win.style.width);
        const ph = parseFloat(win.style.height);
        const minWinW = Math.max(120, Math.round(w * 0.22));
        const minWinH = Math.max(72, Math.round(h * 0.22));
        if (Number.isFinite(pw)) win.style.width = `${Math.max(minWinW, Math.round(pw * sx))}px`;
        if (Number.isFinite(ph)) win.style.height = `${Math.max(minWinH, Math.round(ph * sy))}px`;
      });
    }
    const resolutionChanged = w !== lastDesktopW || h !== lastDesktopH;
    lastDesktopW = w;
    lastDesktopH = h;
    const edge = currentDockEdge();
    const dockPad = dock
      ? Math.round(((edge === "left" || edge === "right") ? dock.offsetWidth : dock.offsetHeight) + 8)
      : Math.round(icon + 16);
    document.querySelectorAll(".app-window").forEach((win) => {
      if (win.classList.contains("hidden-window") || win.classList.contains("minimized-window") || win.classList.contains("maximized-window")) return;
      const minLeft = edge === "left" ? dockPad : 0;
      const minTop = edge === "top" ? dockPad : 0;
      const maxLeft = Math.max(minLeft, area.clientWidth - win.offsetWidth - (edge === "right" ? dockPad : 0));
      const maxTop = Math.max(minTop, area.clientHeight - win.offsetHeight - (edge === "bottom" ? dockPad : 8));
      win.style.left = `${Math.max(minLeft, Math.min(maxLeft, parseFloat(win.style.left) || 0))}px`;
      win.style.top = `${Math.max(minTop, Math.min(maxTop, parseFloat(win.style.top) || 0))}px`;
    });
    if (resolutionChanged && !fittingDesktop) {
      fittingDesktop = true;
      clearTimeout(desktopResizeTimer);
      desktopResizeTimer = setTimeout(() => {
        window.dispatchEvent(new Event("resize"));
        fittingDesktop = false;
      }, 40);
    }
  }
  function setupResponsiveDesktop() {
    const section = document.querySelector(".live-desktop-section");
    const area = document.querySelector(".ubuntu-desktop-area");
    if (!section || section.dataset.resizeWired === "true") return;
    section.dataset.resizeWired = "true";
    const resize = () => sizeLiveDesktopArea();
    if ("ResizeObserver" in window) {
      const ro = new ResizeObserver(resize);
      ro.observe(section);
      if (area) ro.observe(area);
    } else window.addEventListener("resize", resize);
    sizeLiveDesktopArea();
  }

  function enterDesktopTakeOver() {
    const viewer = $("ubuntuDesktopViewer");
    if (!viewer) return;
    document.body.classList.add("desktop-takeover-active");
    viewer.classList.remove("desktop-fullscreen-overlay");
    const bot = (window.deskState?.bots || []).find((b) => b.id === window.deskState?.selected);
    const label = $("takeover-agent-name");
    if (label) label.textContent = `${bot?.name || "Agent"} — Ubuntu Desktop`;
    requestAnimationFrame(() => {
      viewer.focus();
      const area = getDesktopArea();
      const visible = [...document.querySelectorAll(".app-window")].filter((w) => w.dataset.open === "true" && !w.classList.contains("hidden-window") && !w.classList.contains("minimized-window"));
      visible.forEach((win, index) => {
        if (win.classList.contains("maximized-window")) return;
        if (!win.dataset.preTakeoverGeometry) {
          win.dataset.preTakeoverGeometry = JSON.stringify({ left: win.style.left, top: win.style.top, width: win.style.width, height: win.style.height });
        }
        if (!area) return;
        win.style.left = `${70 + index * 36}px`;
        win.style.top = `${28 + index * 30}px`;
        win.style.width = `${Math.min(area.clientWidth - 120, Math.max(480, Math.round(area.clientWidth * 0.62)))}px`;
        win.style.height = `${Math.min(area.clientHeight - 100, Math.max(300, Math.round(area.clientHeight * 0.58)))}px`;
      });
      focusDesktopWindow(document.querySelector(".app-window.focused-window") || getAppWindow("browser") || getAppWindow("hermes"));
    });
  }
  function exitDesktopTakeOver() {
    document.body.classList.remove("desktop-takeover-active");
    document.querySelectorAll(".app-window").forEach((win) => {
      if (!win.dataset.preTakeoverGeometry) return;
      try {
        const g = JSON.parse(win.dataset.preTakeoverGeometry);
        win.style.left = g.left;
        win.style.top = g.top;
        win.style.width = g.width;
        win.style.height = g.height;
      } catch {
        /* ignore */
      }
      delete win.dataset.preTakeoverGeometry;
    });
    setupResponsiveDesktop();
  }

  /* Settings */
  let isGeneratingToml = false;
  let userSettings = (() => {
    try {
      const stored = JSON.parse(localStorage.getItem("hermes-desk-user-settings") || localStorage.getItem("teela-user-settings") || "null");
      if (!stored) return cloneJson(DEFAULT_USER_SETTINGS);
      const models = Array.isArray(stored.models) && stored.models.length ? stored.models : cloneJson(DEFAULT_USER_SETTINGS.models);
      const have = new Set(models.map((m) => m.key));
      for (const def of DEFAULT_USER_SETTINGS.models) {
        if (!have.has(def.key)) {
          models.push(cloneJson(def));
          have.add(def.key);
        }
      }
      for (const m of models) {
        if ((m.key === "qwen38-27b" || /qwen/i.test(String(m.model || ""))) && /127\.0\.0\.1:8080\b/.test(String(m.baseUrl || ""))) {
          m.baseUrl = "http://127.0.0.1:8000/v1";
        }
      }
      return {
        ...cloneJson(DEFAULT_USER_SETTINGS),
        ...stored,
        profile: { ...DEFAULT_USER_SETTINGS.profile, ...(stored.profile || {}) },
        environment: { ...DEFAULT_USER_SETTINGS.environment, ...(stored.environment || {}) },
        models,
      };
    } catch {
      return cloneJson(DEFAULT_USER_SETTINGS);
    }
  })();

  function refreshProfileUi() {
    const name = userSettings.profile.displayName || "User";
    const initials = (userSettings.profile.initials || name.slice(0, 1) || "U").toUpperCase();
    if ($("profileDisplayName")) $("profileDisplayName").textContent = name;
    if ($("profileInitials")) $("profileInitials").textContent = initials;
    if ($("mobile-user-btn")) $("mobile-user-btn").textContent = initials;
  }
  function switchSettingsTab(tabId) {
    document.querySelectorAll("[data-settings-tab]").forEach((btn) => btn.classList.toggle("active", btn.dataset.settingsTab === tabId));
    document.querySelectorAll("[data-settings-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.settingsPanel === tabId));
    if (tabId === "toml") {
      const editor = $("raw-toml-editor");
      if (editor && !editor.dataset.userEdited) editor.value = generateTomlFromForm();
    }
    if (tabId === "environment") updateEnvironmentPreview();
    if (tabId === "cluster") clusterTabOpened = true;
  }

  let clusterTabOpened = false;
  let clusterPeers = [];
  let clusterTokenSet = false;
  let clusterClearRequested = false;
  let clusterGeneratedToken = "";

  function deskIsLoopback() {
    return Boolean(window.deskState?.isLoopback);
  }

  async function deskApi(path, opts = {}) {
    const r = await fetch(path, {
      ...opts,
      headers: {
        Authorization: `Bearer ${window.deskState?.token || ""}`,
        ...(opts.body ? { "Content-Type": "application/json" } : {}),
      },
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  function fillHomeNodeSelect(selected) {
    const sel = $("new-agent-home");
    if (!sel) return;
    const local = window.deskState?.nodeName || "this-node";
    const peers = window.deskState?.peers || [];
    const prev = selected || sel.value;
    sel.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = local;
    opt.textContent = `${local} (this host)`;
    sel.appendChild(opt);
    for (const p of peers) {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.status && p.status !== "ok" ? `${p.name} (${p.status})` : p.name;
      sel.appendChild(o);
    }
    const want = prev || local;
    if (want && ![...sel.options].some((o) => o.value === want)) {
      const extra = document.createElement("option");
      extra.value = want;
      extra.textContent = want;
      sel.appendChild(extra);
    }
    sel.value = want;
  }

  async function fillCreateModels(home, currentId) {
    const sel = $("new-agent-model");
    if (!sel) return;
    const local = window.deskState?.nodeName;
    let models = [];
    let defaultId = "";
    if (home && local && home !== local) {
      const j = await deskApi(`/v1/cluster/peer-models?peer=${encodeURIComponent(home)}`);
      models = j.models || [];
      defaultId = j.default || "";
    } else {
      try {
        const cat = await deskApi("/v1/models");
        models = cat.models || [];
        defaultId = cat.default || "";
        if (window.deskState) window.deskState.catalog = models;
      } catch {
        models = (window.deskState?.catalog || []).map((m) => ({ ...m, id: m.id, name: m.name || m.id }));
        defaultId = userSettings.defaultModel || "";
      }
    }
    sel.innerHTML = "";
    if (!models.length) {
      const o = document.createElement("option");
      o.value = "";
      o.disabled = true;
      o.textContent = "No models in that host's config.toml";
      sel.appendChild(o);
      return;
    }
    const agentBuild = selectedAgentKind() === "hermes";
    for (const m of models) {
      const o = document.createElement("option");
      o.value = m.id;
      const llama = String(m.family || m.id || "").toLowerCase().includes("flash-next")
        || String(m.family || "") === "llamacpp"
        || /:8080\b/.test(String(m.base_url || m.baseUrl || ""));
      const cloud = m.local === false || String(m.id || "").toLowerCase().startsWith("hermes");
      const busy = m.available === false && !agentBuild && !cloud && !llama;
      o.disabled = busy && m.id !== currentId;
      o.textContent = busy ? `${m.name || m.id} — not running` : (m.name || m.id);
      if (m.unavailable_reason) o.title = m.unavailable_reason;
      sel.appendChild(o);
    }
    const prefer = currentId || defaultId;
    if (prefer && [...sel.options].some((o) => o.value === prefer && !o.disabled)) sel.value = prefer;
    else {
      const firstOk = [...sel.options].find((o) => !o.disabled);
      if (firstOk) sel.value = firstOk.value;
      else if (sel.options.length) sel.selectedIndex = 0;
    }
  }

  function setClusterEditable(editable) {
    const banner = $("cluster-lan-banner");
    if (banner) banner.hidden = editable;
    ["settings-node-name", "settings-cluster-token", "settings-cluster-token-confirm", "cluster-token-reveal-value"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = !editable;
    });
    ["generate-cluster-token", "clear-cluster-token", "add-cluster-peer", "copy-cluster-token", "copy-cluster-token-reveal", "toggle-cluster-token", "cluster-save-name", "cluster-save-token", "cluster-save-peers", "cluster-role-create", "cluster-role-paste", "cluster-token-paste"].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = !editable;
    });
  }

  async function saveClusterOnly(patch) {
    if (!deskIsLoopback()) {
      throw new Error("Open http://127.0.0.1:8742/ on this machine to save cluster settings.");
    }
    if (!window.deskSaveAccess) throw new Error("desk save is not ready");
    return window.deskSaveAccess(patch);
  }

  function setClusterRole(role) {
    const create = $("cluster-create-pane");
    const paste = $("cluster-paste-pane");
    if (create) create.hidden = role !== "create";
    if (paste) paste.hidden = role !== "paste";
    $("cluster-role-create")?.classList.toggle("primary", role === "create");
    $("cluster-role-paste")?.classList.toggle("primary", role === "paste");
  }

  function updateClusterWizard() {
    const name = ($("settings-node-name")?.value || "").trim();
    const peers = readClusterPeersFromDom();
    const tokenOk = clusterTokenSet;
    const peersOk = peers.length > 0;
    const mark = (id, done, now) => {
      const el = $(id);
      if (!el) return;
      el.classList.toggle("is-done", done);
      el.classList.toggle("is-now", now);
    };
    mark("cluster-step-1", Boolean(name), !name);
    mark("cluster-step-2", tokenOk, Boolean(name) && !tokenOk);
    mark("cluster-step-3", peersOk, tokenOk && !peersOk);
    mark("cluster-step-4", tokenOk && peersOk, tokenOk && peersOk);
    const n1 = $("cluster-next-1");
    if (n1) n1.textContent = name ? `Saved as ${name}. Next: step 2.` : "Type the name of this computer, then Save name.";
    const n2 = $("cluster-next-2");
    if (n2) {
      const fp = accessFp();
      n2.textContent = tokenOk
        ? `Token is saved${fp ? " (fingerprint " + fp + ")" : ""}. This fingerprint must match on teela-body and teela-brain. Next: step 3.`
        : "On teela-brain click “create the token”. On teela-body click “paste the token”, paste, Save token. Fingerprints must match.";
    }
    const n3 = $("cluster-next-3");
    if (n3) {
      n3.textContent = peersOk
        ? `${peers.length} peer saved. Next: step 4 — Test connection (do this on both computers).`
        : "Example on body: name teela-brain, URL http://10.0.0.10:8742. Then Save peers.";
    }
  }

  function renderClusterPeers() {
    const host = $("cluster-peer-list");
    if (!host) return;
    const editable = deskIsLoopback();
    host.innerHTML = "";
    (clusterPeers || []).forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "cluster-peer-row";
      const st = p.status || "";
      const lat = p.latency_ms != null ? `${p.latency_ms} ms` : "—";
      row.innerHTML = `
        <input class="peer-name" data-i="${i}" value="${escapeAttr(p.name || "")}" placeholder="teela-body" ${editable ? "" : "disabled"} />
        <input class="peer-url" data-i="${i}" value="${escapeAttr(p.url || "")}" placeholder="http://10.0.0.xx:8742" ${editable ? "" : "disabled"} />
        <span class="peer-status muted">${escapeHtml(st)} · ${escapeHtml(String(lat))}${p.hello_node ? " · hello " + escapeHtml(p.hello_node) : ""}</span>
        <button type="button" class="small-action danger peer-remove" data-i="${i}" ${editable ? "" : "disabled"}>Remove</button>`;
      host.appendChild(row);
    });
    host.querySelectorAll(".peer-name").forEach((el) => {
      el.addEventListener("input", () => {
        const i = Number(el.dataset.i);
        if (clusterPeers[i]) clusterPeers[i].name = el.value.trim();
        updateClusterTokenState();
      });
    });
    host.querySelectorAll(".peer-url").forEach((el) => {
      el.addEventListener("input", () => {
        const i = Number(el.dataset.i);
        if (clusterPeers[i]) clusterPeers[i].url = el.value.trim();
        updateClusterTokenState();
      });
    });
    host.querySelectorAll(".peer-remove").forEach((el) => {
      el.addEventListener("click", () => {
        const i = Number(el.dataset.i);
        clusterPeers.splice(i, 1);
        renderClusterPeers();
      });
    });
    updateClusterWizard();
  }

  function readClusterPeersFromDom() {
    return (clusterPeers || []).map((p) => ({ name: (p.name || "").trim(), url: (p.url || "").trim() })).filter((p) => p.name && p.url);
  }

  async function populateClusterForm(access) {
    const a = access || {};
    clusterTokenSet = Boolean(a.cluster_token_set);
    clusterClearRequested = false;
    clusterGeneratedToken = "";
    if ($("settings-node-name")) $("settings-node-name").value = a.node_name || window.deskState?.nodeName || "";
    if ($("settings-cluster-token")) {
      $("settings-cluster-token").value = "";
      $("settings-cluster-token").type = "password";
      $("settings-cluster-token").placeholder = clusterTokenSet ? "••••  (already saved — leave blank unless replacing)" : "Generate on brain, or paste the copied token here";
    }
    if ($("toggle-cluster-token")) $("toggle-cluster-token").textContent = "Show";
    if ($("settings-cluster-token-confirm")) $("settings-cluster-token-confirm").value = "";
    hideClusterTokenReveal();
    clusterPeers = Array.isArray(a.peers) ? a.peers.map((p) => ({ ...p })) : [];
    if (!clusterPeers.length) clusterPeers = [{ name: "", url: "http://" }];
    renderClusterPeers();
    setClusterEditable(deskIsLoopback());
    const node = ($("settings-node-name")?.value || "").toLowerCase();
    setClusterRole(clusterTokenSet || node.includes("brain") ? "create" : "paste");
    updateClusterTokenState();
    const testOut = $("cluster-self-test");
    if (testOut) {
      testOut.hidden = true;
      testOut.textContent = "";
    }
  }

  function accessFp() {
    return window.deskState?.clusterTokenFp || "";
  }

  function updateClusterTokenState() {
    const el = $("cluster-token-state");
    if (!el) return;
    const n = (clusterPeers || []).filter((p) => (p.name || "").trim() && (p.url || "").trim()).length;
    const parts = [];
    const fp = accessFp();
    parts.push(clusterTokenSet ? `Token: saved on this host${fp ? " · fingerprint " + fp : ""}` : "Token: not saved on this host — Generate on brain, or paste that token here, then Save");
    parts.push(n ? `Peers in form: ${n}` : "Peers: none — add the other host by LAN IP, then Save");
    if (!deskIsLoopback()) parts.push("Read-only here. Use http://127.0.0.1:8742/ on this machine to Save.");
    el.textContent = parts.join(" · ");
    const confirmWrap = $("cluster-confirm-wrap");
    const clearBtn = $("clear-cluster-token");
    if (confirmWrap) confirmWrap.hidden = true;
    if (clearBtn) clearBtn.hidden = !clusterTokenSet;
    updateClusterWizard();
  }
  function openUserSettings() {
    $("settings-modal")?.classList.remove("hidden");
    $("settings-modal")?.setAttribute("aria-hidden", "false");
    switchSettingsTab("profile");
    populateSettingsForm();
    setTimeout(() => $("settings-display-name")?.focus(), 50);
  }
  function closeUserSettings() {
    $("settings-modal")?.classList.add("hidden");
    $("settings-modal")?.setAttribute("aria-hidden", "true");
  }
  function hideClusterTokenReveal() {
    const box = $("cluster-token-reveal");
    const val = $("cluster-token-reveal-value");
    if (box) box.hidden = true;
    if (val) val.value = "";
  }

  function showClusterTokenReveal(token) {
    const box = $("cluster-token-reveal");
    const val = $("cluster-token-reveal-value");
    const field = $("settings-cluster-token");
    if (field) {
      field.type = "text";
      field.value = token;
    }
    if ($("toggle-cluster-token")) $("toggle-cluster-token").textContent = "Hide";
    if (box) box.hidden = false;
    if (val) {
      val.value = token;
      val.focus();
      val.select();
    }
  }

  async function copyClusterToken() {
    const token = clusterGeneratedToken || $("cluster-token-reveal-value")?.value || $("settings-cluster-token")?.value || "";
    if (!token) {
      flashSettingsStatus("Generate a token first — saved tokens are write-only");
      return false;
    }
    await copyText(token);
    const val = $("cluster-token-reveal-value");
    if (val && val.value === token) {
      val.focus();
      val.select();
    }
    return true;
  }

  function flashSettingsStatus(message, ms = 1800, kind = "ok") {
    const el = $("settings-save-status");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("is-error", kind === "error");
    clearTimeout(flashSettingsStatus.timer);
    flashSettingsStatus.timer = setTimeout(() => {
      el.textContent = "";
      el.classList.remove("is-error");
    }, ms);
  }
  function readProfileForm() {
    return {
      displayName: $("settings-display-name")?.value.trim() || "User",
      initials: ($("settings-initials")?.value.trim() || "U").toUpperCase(),
      agentHome: $("settings-hermes-home")?.value.trim() || "~/.hermes",
    };
  }
  function readEnvironmentForm() {
    return {
      modelsBaseUrl: $("env-models-base-url")?.value.trim() || "",
      modelsListUrl: $("env-models-list-url")?.value.trim() || "",
      xaiApiKey: $("env-xai-api-key")?.value || "",
      agentCodeApiKey: $("env-grok-code-api-key")?.value || "",
      defaultModel: $("env-default-model")?.value.trim() || "",
      configPath: $("env-config-path")?.value.trim() || "",
    };
  }
  function generateEnvExports() {
    const env = readEnvironmentForm();
    const lines = [];
    if (env.modelsBaseUrl) lines.push(`export HERMES_MODELS_BASE_URL=${shellQuote(env.modelsBaseUrl)}`);
    if (env.modelsListUrl) lines.push(`export HERMES_MODELS_LIST_URL=${shellQuote(env.modelsListUrl)}`);
    if (env.xaiApiKey) lines.push(`export XAI_API_KEY=${shellQuote(env.xaiApiKey)}`);
    if (env.agentCodeApiKey) lines.push(`export HERMES_CODE_API_KEY=${shellQuote(env.agentCodeApiKey)}`);
    if (env.defaultModel) lines.push(`export HERMES_DEFAULT_MODEL=${shellQuote(env.defaultModel)}`);
    if (env.configPath) lines.push(`export HERMES_CONFIG_PATH=${shellQuote(env.configPath)}`);
    return lines.length ? lines.join("\n") : "# No environment overrides configured";
  }
  function updateEnvironmentPreview() {
    const preview = $("env-export-preview");
    if (preview) preview.textContent = generateEnvExports();
  }
  function tomlString(value) {
    return `"${String(value ?? "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
  }
  function tomlKey(name) {
    const key = String(name ?? "").trim();
    return /^[A-Za-z0-9_-]+$/.test(key) ? key : tomlString(key);
  }
  function generateTomlFromForm() {
    if (isGeneratingToml) return $("raw-toml-editor")?.value || "";
    isGeneratingToml = true;
    try {
      syncSettingsModelsFromDom();
      const defaultModel = $("settings-default-model")?.value || userSettings.defaultModel || userSettings.models[0]?.key || "";
      const lines = ["[models]", `default = ${tomlString(defaultModel)}`, ""];
      userSettings.models.forEach((m) => {
        const key = (m.key || "custom").trim();
        lines.push(`[model.${tomlKey(key)}]`);
        lines.push(`model = ${tomlString(m.model || key)}`);
        if (m.baseUrl) lines.push(`base_url = ${tomlString(m.baseUrl)}`);
        if (m.name) lines.push(`name = ${tomlString(m.name)}`);
        if (m.apiBackend) lines.push(`api_backend = ${tomlString(m.apiBackend)}`);
        if (Number(m.contextWindow) > 0) lines.push(`context_window = ${Math.trunc(Number(m.contextWindow))}`);
        if (Number(m.maxCompletionTokens) > 0) lines.push(`max_completion_tokens = ${Math.trunc(Number(m.maxCompletionTokens))}`);
        if (m.weights) lines.push(`weights = ${tomlString(m.weights)}`);
        if (m.apiKey) lines.push(`api_key = ${tomlString(m.apiKey)}`);
        lines.push("");
      });
      return lines.join("\n").trimEnd() + "\n";
    } finally {
      isGeneratingToml = false;
    }
  }
  function markTomlStale() {
    if (isGeneratingToml) return;
    const editor = $("raw-toml-editor");
    if (editor && !editor.dataset.userEdited) editor.value = generateTomlFromForm();
  }
  function syncSettingsModelsFromDom() {
    document.querySelectorAll(".model-config-card").forEach((card) => {
      const index = Number(card.dataset.modelIndex);
      const model = userSettings.models[index];
      if (!model) return;
      card.querySelectorAll("[data-model-field]").forEach((input) => {
        const field = input.dataset.modelField;
        let value = input.value;
        if (field === "contextWindow" || field === "maxCompletionTokens") value = Number(value || 0);
        model[field] = value;
      });
    });
    if (!isGeneratingToml) markTomlStale();
  }
  function renderSettingsModels() {
    const list = $("settings-model-list");
    const defaultSelect = $("settings-default-model");
    if (!list || !defaultSelect) return;
    list.innerHTML = "";
    defaultSelect.innerHTML = "";
    userSettings.models.forEach((model, index) => {
      const option = document.createElement("option");
      option.value = model.key;
      option.textContent = model.name || model.key;
      defaultSelect.appendChild(option);
      const card = document.createElement("div");
      card.className = "model-config-card";
      card.dataset.modelIndex = String(index);
      card.innerHTML = `
        <div class="model-config-header">
          <div class="model-config-title">
            <strong>${escapeHtml(model.name || model.key)}</strong>
            <small>[model.${escapeHtml(model.key)}]</small>
          </div>
          <button type="button" class="model-delete" title="Remove model">Remove</button>
        </div>
        <div class="model-config-body">
          <label>Config key<input data-model-field="key" value="${escapeAttr(model.key)}" /></label>
          <label>Model ID sent to API<input data-model-field="model" value="${escapeAttr(model.model || "")}" /></label>
          <label class="span-2">Display name<input data-model-field="name" value="${escapeAttr(model.name || "")}" /></label>
          <label class="span-2">Base URL<input data-model-field="baseUrl" value="${escapeAttr(model.baseUrl || "")}" placeholder="http://127.0.0.1:8000/v1" /></label>
          <label>API backend
            <select data-model-field="apiBackend">
              <option value="chat_completions"${model.apiBackend === "chat_completions" ? " selected" : ""}>chat_completions</option>
              <option value="responses"${model.apiBackend === "responses" ? " selected" : ""}>responses</option>
              <option value="messages"${model.apiBackend === "messages" ? " selected" : ""}>messages</option>
            </select>
          </label>
          <label>Context window<input data-model-field="contextWindow" type="number" min="1" value="${Number(model.contextWindow || 0)}" /></label>
          <label>Max completion tokens<input data-model-field="maxCompletionTokens" type="number" min="1" value="${Number(model.maxCompletionTokens || 0)}" /></label>
          <label>API key (optional)<input data-model-field="apiKey" type="password" value="${escapeAttr(model.apiKey || "")}" /></label>
          <label class="span-2">Local weights dir (optional)<input data-model-field="weights" value="${escapeAttr(model.weights || "")}" placeholder="~/models/MyNewModel or Qwen3-VL-8B-Instruct-FP8" /></label>
        </div>
        <div class="model-config-footer">
          <button type="button" class="model-delete-files" title="Delete the weights folder under ~/models">Delete files</button>
        </div>`;
      card.querySelectorAll("[data-model-field]").forEach((input) => {
        input.addEventListener("input", syncSettingsModelsFromDom);
        input.addEventListener("change", syncSettingsModelsFromDom);
      });
      card.querySelector(".model-delete").addEventListener("click", () => {
        syncSettingsModelsFromDom();
        if (userSettings.models.length <= 1) {
          flashSettingsStatus("Keep at least one model in the picker.", 6000, "error");
          return;
        }
        const removed = userSettings.models.splice(index, 1)[0];
        if (userSettings.defaultModel === removed.key) userSettings.defaultModel = userSettings.models[0].key;
        renderSettingsModels();
        markTomlStale();
      });
      card.querySelector(".model-delete-files")?.addEventListener("click", async () => {
        syncSettingsModelsFromDom();
        const row = userSettings.models[index];
        if (!row) return;
        const id = row.key || row.model;
        if (!row.weights) {
          flashSettingsStatus("Set a weights dir first, Save, then delete files.", 8000, "error");
          return;
        }
        if (!window.confirm(`Delete weights for ${id} from disk? This cannot be undone.`)) return;
        try {
          const j = await deskApi("/v1/local-llm/control", {
            method: "POST",
            body: JSON.stringify({ action: "delete", id, delete_weights: true }),
          });
          if (Array.isArray(j.models) && j.models.length) {
            userSettings.models = j.models;
            userSettings.defaultModel = j.default_model || j.default || userSettings.models[0]?.key;
          } else if (userSettings.models.length > 1) {
            userSettings.models.splice(index, 1);
          }
          renderSettingsModels();
          markTomlStale();
          flashSettingsStatus(j.deleted_weights ? `Deleted ${j.deleted_weights}` : `Removed ${id}`, 8000);
        } catch (err) {
          flashSettingsStatus(String(err.message || err), 10000, "error");
        }
      });
      list.appendChild(card);
    });
    defaultSelect.value = userSettings.defaultModel;
    if (!defaultSelect.value && userSettings.models.length) {
      userSettings.defaultModel = userSettings.models[0].key;
      defaultSelect.value = userSettings.defaultModel;
    }
    defaultSelect.onchange = () => {
      userSettings.defaultModel = defaultSelect.value;
      markTomlStale();
    };
  }
  function updateDeskAccessHint() {
    const hint = $("settings-desk-hint");
    if (!hint) return;
    const host = ($("settings-desk-host")?.value || "127.0.0.1").trim() || "127.0.0.1";
    const port = Number($("settings-desk-port")?.value || 8742) || 8742;
    const loopback = /^(127\.0\.0\.1|localhost)$/i.test(host);
    hint.textContent = loopback
      ? `Default 127.0.0.1 stays on this computer only. Saving a LAN cluster peer automatically binds this desk on your LAN IP so the other host can list your bots (http://<lan-ip>:${port}/).`
      : `Other devices on the network can open http://${host}:${port}/ — this computer still works at http://127.0.0.1:${port}/. LAN cluster peers reach this desk at that address.`;
  }
  async function populateSettingsForm() {
    $("settings-display-name").value = userSettings.profile.displayName || "";
    $("settings-initials").value = userSettings.profile.initials || "";
    $("settings-hermes-home").value = userSettings.profile.agentHome || "~/.hermes";
    $("settings-profile-avatar").textContent = (userSettings.profile.initials || "U").toUpperCase();
    $("settings-desk-host").value = window.deskState?.listenHost || "127.0.0.1";
    $("settings-desk-port").value = String(window.deskState?.listenPort || 8742);
    try {
      if (window.deskLoadAccess) {
        const access = await window.deskLoadAccess();
        if (access?.listen_host) $("settings-desk-host").value = access.listen_host;
        if (access?.listen_port) $("settings-desk-port").value = String(access.listen_port);
        if (Array.isArray(access.models) && access.models.length) {
          userSettings.models = access.models.map((m) => ({
            key: m.key || m.id,
            model: m.model || m.key || m.id,
            name: m.name || m.key || m.id,
            baseUrl: m.baseUrl || m.base_url || "",
            apiBackend: m.apiBackend || m.api_backend || "chat_completions",
            contextWindow: m.contextWindow || m.context_window || 0,
            maxCompletionTokens: (() => {
              const n = Number(m.maxCompletionTokens || m.max_completion_tokens || 0);
              if (n > 0) return n;
              const id = String(m.key || m.id || m.model || "").toLowerCase();
              return id.startsWith("hermes") ? 65536 : 0;
            })(),
            apiKey: m.apiKey || m.api_key || "",
            weights: m.weights || "",
          }));
          userSettings.defaultModel = access.default_model || userSettings.models[0]?.key || "";
        }
        await populateClusterForm(access);
      } else {
        await populateClusterForm({});
      }
    } catch {
      await populateClusterForm({});
    }
    updateDeskAccessHint();
    const env = userSettings.environment || {};
    $("env-models-base-url").value = env.modelsBaseUrl || "";
    $("env-models-list-url").value = env.modelsListUrl || "";
    $("env-xai-api-key").value = env.xaiApiKey || "";
    $("env-grok-code-api-key").value = env.agentCodeApiKey || "";
    $("env-default-model").value = env.defaultModel || "";
    $("env-config-path").value = env.configPath || "";
    renderSettingsModels();
    updateEnvironmentPreview();
    const editor = $("raw-toml-editor");
    editor.dataset.userEdited = "";
    editor.value = userSettings.rawTomlOverride || generateTomlFromForm();
  }
  function importSimpleToml(text) {
    const lines = String(text).split(/\r?\n/);
    let section = "";
    let defaultModel = "";
    const imported = [];
    const getOrCreate = (key) => {
      let found = imported.find((m) => m.key === key);
      if (!found) {
        found = { key, model: key, name: key, baseUrl: "", apiBackend: "chat_completions", contextWindow: 200000, maxCompletionTokens: 8192, apiKey: "" };
        imported.push(found);
      }
      return found;
    };
    for (const raw of lines) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const sec = line.match(/^\[([^\]]+)\]$/);
      if (sec) {
        section = sec[1];
        continue;
      }
      const kv = line.match(/^([A-Za-z0-9_]+)\s*=\s*(.+)$/);
      if (!kv) continue;
      const key = kv[1];
      let value = kv[2].trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\");
      }
      if (section === "models" && key === "default") {
        defaultModel = value;
        continue;
      }
      const modelSec = section.match(/^model\.(.+)$/);
      if (modelSec) {
        const m = getOrCreate(modelSec[1]);
        if (key === "model") m.model = value;
        else if (key === "base_url") m.baseUrl = value;
        else if (key === "name") m.name = value;
        else if (key === "api_backend") m.apiBackend = value;
        else if (key === "context_window") m.contextWindow = Number(value) || m.contextWindow;
        else if (key === "max_completion_tokens") m.maxCompletionTokens = Number(value) || m.maxCompletionTokens;
        else if (key === "api_key") m.apiKey = value;
      }
    }
    if (imported.length) {
      userSettings.models = imported;
      userSettings.defaultModel = defaultModel || imported[0].key;
      renderSettingsModels();
    }
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
  }
  function downloadTextFile(name, text, type = "text/plain") {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
  }

  /* Plugins */
  let pluginCatalog = loadJson("hermes-desk-plugins", null) || loadJson("teela-plugins", null) || DEFAULT_PLUGINS;
  function savePlugins() {
    localStorage.setItem("hermes-desk-plugins", JSON.stringify(pluginCatalog));
    localStorage.setItem("teela-plugins", JSON.stringify(pluginCatalog));
  }
  function renderPluginCatalog(filter = "") {
    const catalog = $("plugin-catalog");
    if (!catalog) return;
    catalog.innerHTML = "";
    pluginCatalog
      .filter((p) => p.name.toLowerCase().includes(filter.toLowerCase()))
      .forEach((plugin) => {
        const card = document.createElement("div");
        card.className = "plugin-card";
        card.innerHTML = `
          <div class="plugin-card-header">
            <div class="plugin-icon">${plugin.icon || "🔌"}</div>
            <div><b>${escapeHtml(plugin.name)}</b></div>
          </div>
          <p>${escapeHtml(plugin.description || plugin.endpoint || "Custom plugin")}</p>
          <div class="plugin-card-footer">
            <span class="plugin-status ${plugin.installed ? "installed" : ""}">${plugin.installed ? "Installed" : "Not installed"}</span>
            <button class="small-action ${plugin.installed ? "" : "primary"}">${plugin.installed ? "Remove" : "Add"}</button>
          </div>`;
        card.querySelector("button").addEventListener("click", () => {
          plugin.installed = !plugin.installed;
          savePlugins();
          renderPluginCatalog($("plugin-search")?.value || "");
        });
        catalog.appendChild(card);
      });
  }
  function openPlugins() {
    $("plugins-modal")?.classList.remove("hidden");
    $("plugins-modal")?.setAttribute("aria-hidden", "false");
    renderPluginCatalog();
    setTimeout(() => $("plugin-search")?.focus(), 50);
  }
  function closePlugins() {
    $("plugins-modal")?.classList.add("hidden");
    $("plugins-modal")?.setAttribute("aria-hidden", "true");
    $("custom-plugin-form")?.classList.add("hidden");
  }

  /* Create / edit agent */
  let selectedNewAgentColor = "#8b5cf6";
  function refreshAgentPreview() {
    const preview = $("agent-preview-avatar");
    if (!preview) return;
    preview.className = `avatar glance agent-preview-large ${$("new-agent-shape")?.value || ""}`;
    preview.dataset.state = "idle";
    preview.style.setProperty("--c", selectedNewAgentColor);
  }
  function setAgentSwatch(color) {
    selectedNewAgentColor = color || "#8b5cf6";
    document.querySelectorAll(".color-swatch").forEach((s) => s.classList.toggle("active", s.dataset.color === selectedNewAgentColor));
    refreshAgentPreview();
  }
  function selectedAgentKind() {
    const hit = document.querySelector('input[name="agent-kind"]:checked');
    return hit && hit.value === "hermes" ? "hermes" : "teela-brain";
  }
  function localTeelaBrainTaken() {
    const node = window.deskState?.nodeName || "";
    if (window.deskState?.teelaBrainTaken === true) return true;
    return (window.deskState?.bots || []).some((b) => {
      if (!b || b.remote) return false;
      if (node && b.node && b.node !== node) return false;
      const k = String(b.kind || "").toLowerCase();
      if (k === "hermes") return false;
      return k === "teela-brain" || k === "teela" || k === "hybrid" || k === "";
    });
  }
  function setAgentKind(kind, { locked = false } = {}) {
    const editing = Boolean($("agent-edit-id")?.value);
    const taken = !editing && localTeelaBrainTaken();
    const picked = taken || kind === "hermes" ? "hermes" : "teela-brain";
    const picker = $("agent-kind-picker");
    picker?.classList.toggle("is-locked", locked);
    picker?.classList.toggle("teela-taken", taken);
    document.querySelectorAll('input[name="agent-kind"]').forEach((radio) => {
      const isTeela = radio.value === "teela-brain";
      radio.checked = radio.value === picked;
      radio.disabled = locked || (taken && isTeela);
      const card = radio.closest(".agent-kind-card");
      if (!card) return;
      card.classList.toggle("is-selected", radio.value === picked);
      card.classList.toggle("is-locked", locked && radio.value !== picked);
      card.classList.toggle("is-taken", taken && isTeela);
      card.hidden = Boolean(taken && isTeela && !editing);
    });
    syncAgentKindCopy();
  }
  function syncAgentKindCopy() {
    const kind = selectedAgentKind();
    const soul = $("new-agent-soul");
    const note = $("agent-modal-note");
    const editing = Boolean($("agent-edit-id")?.value);
    if (soul && !editing && !soul.value.trim()) {
      soul.placeholder =
        kind === "hermes"
          ? "Hermes Agent agent: same tools and answers as a Hermes TUI session. No robot body."
          : "Teela Brain: feel the live body, then move the Robot Simulator. First person, short replies.";
    }
    if (note && !editing) {
      if (localTeelaBrainTaken()) {
        note.textContent =
          "This computer already has a Teela Brain. Extra agents are Hermes Agent so physical robot control stays unique.";
      } else {
        note.textContent =
          kind === "hermes"
            ? "This agent is a regular Hermes Agent TUI session (files, shell, search, browser, skills, subagents). Chat output matches the TUI. It will not control the Robot Simulator."
            : "This agent is the one Teela Brain on this computer: proprioception, Robot Simulator, and physical-robot control. Extra agents must be Hermes Agent.";
      }
    }
    if (note && editing) {
      note.textContent =
        kind === "hermes"
          ? "Type is locked: Hermes Agent (same as Hermes TUI). Saving updates name and SOUL; chat history stays."
          : "Type is locked: Teela Brain (Robot Simulator body). Saving updates name and SOUL; chat history stays.";
    }
  }
  async function openAgentModal(editId) {
    const editing = Boolean(editId);
    $("agent-edit-id").value = editId || "";
    $("agent-modal-title").textContent = editing ? "Edit Agent" : "Create Agent";
    const copy = $("agent-modal-copy");
    if (copy) {
      copy.textContent = editing
        ? "Update this bot’s name, description, and SOUL. Agent type and home node were set at create and cannot be changed."
        : "Create a bot with its own identity, model, home node, and isolated workspace.";
    }
    const note = $("agent-modal-note");
    if (note) {
      note.textContent = editing
        ? "Saving applies name and SOUL right away. Chat history stays; the live session reloads the new identity."
        : "A dedicated workspace, browser, and memory will be created for this agent.";
    }
    const submit = $("agent-submit");
    if (submit) submit.textContent = editing ? "Save changes" : "Create agent";
    const formEdit = $("agent-edit-form-actions");
    if (formEdit) formEdit.hidden = !editing;
    const editActions = $("agent-edit-actions");
    if (editActions) editActions.hidden = !editing;
    fillWorkspaceShareList(editing ? editId : "");
    $("agent-modal")?.classList.remove("hidden");
    $("agent-modal")?.setAttribute("aria-hidden", "false");

    const homeNote = $("agent-home-note");
    if (editing) {
      const bot = (window.deskState?.bots || []).find((b) => b.id === editId);
      $("new-agent-name").value = bot?.name || "";
      $("new-agent-description").value = bot?.description || "";
      $("new-agent-model").value = bot?.model || "";
      setAgentKind(bot?.kind || "teela-brain", { locked: true });
      const meta = avatarMeta(bot || { id: editId });
      $("new-agent-shape").value = meta.shape || "";
      setAgentSwatch(meta.color);
      const home = bot?.node || window.deskState?.nodeName || "";
      fillHomeNodeSelect(home);
      try {
        await fillCreateModels(home, bot?.model);
      } catch {
        /* keep existing model options */
      }
      if (bot?.model) $("new-agent-model").value = bot.model;
      if (homeNote) {
        homeNote.hidden = false;
        homeNote.textContent = home
          ? `Tied to ${home}. Home node is set at create and cannot be moved.`
          : "Home node is tied to this agent.";
      }
      $("new-agent-soul").value = "Loading SOUL…";
      try {
        const soul = window.deskLoadSoul ? await window.deskLoadSoul(editId) : "";
        $("new-agent-soul").value = soul || "";
      } catch (err) {
        $("new-agent-soul").value = "";
        alert(err.message || err);
      }
    } else {
      $("new-agent-name").value = "";
      $("new-agent-description").value = "";
      $("new-agent-soul").value = "";
      $("new-agent-shape").value = "";
      setAgentKind(localTeelaBrainTaken() ? "hermes" : "teela-brain", { locked: false });
      setAgentSwatch("#8b5cf6");
      fillHomeNodeSelect();
      if (homeNote) {
        homeNote.hidden = false;
        homeNote.textContent = "New agents run on the selected computer. This is stored with the bot and cannot be changed later.";
      }
      const home = $("new-agent-home")?.value;
      try {
        await fillCreateModels(home);
      } catch {
        /* leave the host catalog as fillCreateModels left it */
      }
    }
    const homeWrap = $("new-agent-home");
    if (homeWrap) homeWrap.disabled = editing;
    refreshAgentPreview();
    setTimeout(() => $("new-agent-name")?.focus(), 50);
  }
  function fillWorkspaceShareList(editId) {
    const block = $("workspace-share-block");
    const list = $("workspace-share-list");
    const reqs = $("workspace-share-requests");
    if (!block || !list) return;
    if (!editId) {
      block.hidden = true;
      list.innerHTML = "";
      if (reqs) { reqs.hidden = true; reqs.innerHTML = ""; }
      return;
    }
    block.hidden = false;
    const bots = window.deskState?.bots || [];
    const bot = bots.find((b) => b.id === editId);
    const granted = new Set(bot?.workspace_share_with || []);
    const others = bots.filter((b) => b.id !== editId && !b.remote);
    if (!others.length) {
      list.innerHTML = '<p class="field-hint">No other local bots to share with yet.</p>';
    } else {
      list.innerHTML = others.map((b) => {
        const checked = granted.has(b.id) ? " checked" : "";
        return `<label><input type="checkbox" data-share-with="${escapeHtml(b.id)}"${checked}/> ${escapeHtml(b.name)}</label>`;
      }).join("");
    }
    const pending = bot?.workspace_share_requests || [];
    if (reqs) {
      if (!pending.length) {
        reqs.hidden = true;
        reqs.innerHTML = "";
      } else {
        reqs.hidden = false;
        reqs.innerHTML = pending.map((r) => {
          const id = escapeHtml(r.from || "");
          const name = escapeHtml(r.name || r.from || "Bot");
          return `<div class="workspace-share-request"><span>${name} asked to read this desk</span><button type="button" class="small-action" data-share-approve="${id}">Allow</button></div>`;
        }).join("");
        reqs.querySelectorAll("[data-share-approve]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const id = btn.getAttribute("data-share-approve");
            const box = list.querySelector(`[data-share-with="${CSS.escape(id)}"]`);
            if (box) box.checked = true;
            btn.closest(".workspace-share-request")?.remove();
            if (!reqs.querySelector(".workspace-share-request")) reqs.hidden = true;
          });
        });
      }
    }
  }
  function selectedWorkspaceShareIds() {
    return [...document.querySelectorAll("#workspace-share-list [data-share-with]:checked")].map((el) => el.getAttribute("data-share-with")).filter(Boolean);
  }
  function closeAgentModal() {
    $("agent-modal")?.classList.add("hidden");
    $("agent-modal")?.setAttribute("aria-hidden", "true");
    if ($("agent-edit-id")) $("agent-edit-id").value = "";
    fillWorkspaceShareList("");
    const formEdit = $("agent-edit-form-actions");
    if (formEdit) formEdit.hidden = true;
    const editActions = $("agent-edit-actions");
    if (editActions) editActions.hidden = true;
  }

  /* Hourly notes */
  function hourlyKey() {
    return `hermes-desk-hourly:${window.deskState?.selected || "none"}`;
  }
  function loadHourlyNotes() {
    try {
      const raw = JSON.parse(localStorage.getItem(hourlyKey()) || "[]");
      return (raw || []).map((n) => ({
        id: n.id,
        title: n.title || n.text || "Note",
        text: n.text || "",
        interval: Number(n.interval || 1),
        enabled: n.enabled !== false,
        nextCheck: n.nextCheck || (n.enabled === false ? "Paused" : `In ${n.interval || 1} hour${Number(n.interval || 1) === 1 ? "" : "s"}`),
      }));
    } catch {
      return [];
    }
  }
  function saveHourlyNotes(list) {
    localStorage.setItem(hourlyKey(), JSON.stringify(list));
  }
  function renderHourlyNotes() {
    const host = $("hourly-note-list");
    if (!host) return;
    if (!window.deskState?.selected) {
      host.innerHTML = `<div class="empty-manager-state">Select a bot to keep hourly notes.</div>`;
      return;
    }
    const notes = loadHourlyNotes();
    if (!notes.length) {
      host.innerHTML = `<div class="empty-manager-state">No hourly notes yet. Add one for recurring checks.</div>`;
      return;
    }
    host.innerHTML = "";
    notes.forEach((note) => {
      const card = document.createElement("div");
      card.className = `manager-card ${note.enabled ? "" : "paused"}`;
      card.innerHTML = `
        <div class="manager-card-top">
          <span class="manager-state-dot"></span>
          <div>
            <div class="manager-card-title">${escapeHtml(note.title)}</div>
            <div class="manager-card-meta">Every ${Number(note.interval) === 1 ? "hour" : `${note.interval} hours`}<br>${note.enabled ? `Next: ${escapeHtml(note.nextCheck || "In 1 hour")}` : "Paused"}</div>
          </div>
          <div class="manager-card-actions">
            <button type="button" data-action="edit">Edit</button>
            <button type="button" data-action="toggle">${note.enabled ? "Pause" : "Resume"}</button>
            <button type="button" data-action="delete">Delete</button>
          </div>
        </div>
        <div class="manager-card-detail">${escapeHtml(note.text || "")}</div>`;
      card.querySelector('[data-action="edit"]').onclick = () => openHourlyNoteModal(note.id);
      card.querySelector('[data-action="toggle"]').onclick = () => {
        const list = loadHourlyNotes();
        const hit = list.find((n) => n.id === note.id);
        if (!hit) return;
        hit.enabled = !hit.enabled;
        hit.nextCheck = hit.enabled ? `In ${hit.interval} hour${Number(hit.interval) === 1 ? "" : "s"}` : "Paused";
        saveHourlyNotes(list);
        renderHourlyNotes();
      };
      card.querySelector('[data-action="delete"]').onclick = () => {
        if (!confirm("Delete this hourly note?")) return;
        saveHourlyNotes(loadHourlyNotes().filter((n) => n.id !== note.id));
        renderHourlyNotes();
      };
      host.appendChild(card);
    });
  }
  function openHourlyNoteModal(id = null) {
    const note = id ? loadHourlyNotes().find((n) => n.id === id) : null;
    $("hourly-modal-title").textContent = note ? "Edit Hourly Note" : "Add Hourly Note";
    $("hourly-edit-id").value = note?.id || "";
    $("hourly-title").value = note?.title || "";
    $("hourly-text").value = note?.text || "";
    $("hourly-interval").value = String(note?.interval || 1);
    $("hourly-enabled").checked = note ? note.enabled : true;
    $("hourly-modal")?.classList.remove("hidden");
    $("hourly-modal")?.setAttribute("aria-hidden", "false");
  }
  function closeHourlyNoteModal() {
    $("hourly-modal")?.classList.add("hidden");
    $("hourly-modal")?.setAttribute("aria-hidden", "true");
  }

  function scheduleToDesk(unit, every, advanced) {
    const custom = (advanced || "").trim();
    if (custom) return custom;
    const n = Math.max(1, Math.floor(Number(every) || 1));
    const map = { minutes: "m", hours: "h", days: "d", weeks: "w", monthly: "mo", yearly: "y" };
    return `${n}${map[unit] || "h"}`;
  }
  function openRoutineModal(routine = null) {
    if (!window.deskState?.selected && !routine) return;
    $("routines")?.classList.remove("routines-opened");
    $("routines")?.setAttribute("aria-hidden", "true");
    $("routine-modal-title").textContent = routine ? "Edit Routine" : "Add Routine";
    $("routine-edit-id").value = routine?.id || "";
    $("routine-name").value = routine?.name || "";
    $("routine-type").value = routine?.type || "agent_prompt";
    $("routine-instruction").value = routine?.instruction || "";
    const sched = String(routine?.schedule || "1h");
    const m = sched.match(/^(\d+(?:\.\d+)?)(mo|[mhdwy])$/i);
    const unitMap = { m: "minutes", h: "hours", d: "days", w: "weeks", mo: "monthly", y: "yearly" };
    $("routine-every").value = m ? String(Math.max(1, Number(m[1]) || 1)) : "1";
    $("routine-unit").value = m ? (unitMap[m[2].toLowerCase()] || "hours") : "hours";
    $("routine-cron").value = m ? "" : (routine?.cron || routine?.schedule || "");
    $("routine-timezone").value = routine?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";
    $("routine-enabled").checked = routine ? routine.enabled !== false : true;
    $("routine-modal")?.classList.remove("hidden");
    $("routine-modal")?.setAttribute("aria-hidden", "false");
  }
  function closeRoutineModal() {
    $("routine-modal")?.classList.add("hidden");
    $("routine-modal")?.setAttribute("aria-hidden", "true");
  }

  function renderAgentRail(bots, selectedId, onSelect) {
    const rail = $("agentIconRail");
    if (!rail) return;
    rail.innerHTML = "";
    (bots || []).forEach((bot) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const stub = !!bot.peer_stub;
      const offline = !!(bot.node_status && bot.node_status !== "ok");
      btn.className = "rail-agent" + (bot.id === selectedId ? " active" : "") + (stub ? " peer-stub" : "") + (offline ? " is-offline" : "");
      const kindLabel = stub ? (bot.status || "offline") : bot.kind === "hermes" ? "Hermes Agent" : "Teela Brain";
      const node = bot.node ? ` · ${bot.node}` : "";
      btn.title = `${bot.name} · ${kindLabel}${node}`;
      btn.dataset.agentId = bot.id;
      btn.innerHTML = avatarHTML(bot) + (bot.unread ? '<span class="rail-unread"></span>' : "");
      if (!stub) {
        btn.addEventListener("click", () => {
          onSelect(bot.id);
          setMobileView("chat");
        });
      }
      rail.appendChild(btn);
    });
    const add = document.createElement("button");
    add.type = "button";
    add.className = "rail-add";
    add.title = "Create agent";
    add.textContent = "+";
    add.addEventListener("click", () => openAgentModal());
    rail.appendChild(add);
  }
  function renderMobileHome(bots, onSelect) {
    const featured = $("mobile-featured");
    const list = $("mobile-list");
    if (!featured || !list) return;
    const all = bots || [];
    featured.innerHTML = all
      .slice(0, 3)
      .map((a) => {
        return `<div class="featured-card${a.peer_stub ? " peer-stub" : ""}" data-agent="${a.id}" data-agent-id="${a.id}" data-peer-stub="${a.peer_stub ? "1" : ""}">
        ${avatarHTML(a)}
        <div class="agent-name">${escapeHtml(a.name)}</div>
      </div>`;
      })
      .join("");
    list.innerHTML = all
      .slice(3)
      .map((a) => {
        return `<div class="mobile-row${a.peer_stub ? " peer-stub" : ""}" data-agent="${a.id}" data-agent-id="${a.id}" data-peer-stub="${a.peer_stub ? "1" : ""}">
        ${avatarHTML(a)}
        <div class="agent-main"><div class="agent-name">${escapeHtml(a.name)}</div><div class="agent-preview">${escapeHtml(a.status || "Ready")}</div></div>
      </div>`;
      })
      .join("");
    [...featured.querySelectorAll("[data-agent]"), ...list.querySelectorAll("[data-agent]")].forEach((el) => {
      el.addEventListener("click", () => {
        if (el.dataset.peerStub === "1") return;
        onSelect(el.dataset.agent);
        setMobileView("chat");
      });
    });
  }

  function formatOsClock(d = new Date()) {
    const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let h = d.getHours();
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    const min = String(d.getMinutes()).padStart(2, "0");
    return {
      top: `${days[d.getDay()]} ${h}:${min} ${ampm}`,
      time: `${h}:${min} ${ampm}`,
      date: `${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}`,
    };
  }
  function updateUbuntuClock() {
    const stamp = formatOsClock();
    const clock = $("task-clock");
    const chat = $("chat-clock");
    const loginClock = $("os-login-clock");
    const loginDate = $("os-login-date");
    if (clock) clock.textContent = stamp.top;
    if (chat) chat.textContent = stamp.time;
    if (loginClock) loginClock.textContent = stamp.time;
    if (loginDate) loginDate.textContent = stamp.date;
  }
  function currentBotLockIdentity() {
    const bot = (window.deskState?.bots || []).find((b) => b.id === window.deskState?.selected);
    const name = (bot?.name || "Bot").trim() || "Bot";
    const initial = (name.match(/[A-Za-z0-9]/) || ["B"])[0].toUpperCase();
    return { name, initial };
  }
  function miniosLockPassword() {
    return (localStorage.getItem("minios-lock-password") || "").trim();
  }
  function refreshOsLock() {
    const { name, initial } = currentBotLockIdentity();
    if ($("os-login-name")) $("os-login-name").textContent = name;
    if ($("os-login-avatar")) $("os-login-avatar").textContent = initial;
    const locked = Boolean(miniosLockPassword());
    const pass = $("os-login-pass");
    if (pass) {
      pass.hidden = !locked;
      if (!locked) pass.value = "";
    }
    const hint = $("os-login-hint");
    if (hint) {
      hint.hidden = !locked;
      hint.textContent = locked ? "Enter the MiniOS password from Settings" : "";
    }
    const err = $("os-login-error");
    if (err) err.hidden = true;
  }
  function wireOsSession() {
    const boot = $("os-boot");
    const login = $("os-login");
    const pass = $("os-login-pass");
    const btn = $("os-login-btn");
    const power = $("os-power-btn");
    if (!boot || !login) return;
    const signedKey = "minios-session-on";
    const skip = document.body.classList.contains("observer-mode")
      || Boolean(new URLSearchParams(location.search).get("observe"));
    const hideOverlays = () => {
      boot.hidden = true;
      login.hidden = true;
      boot.style.opacity = "";
    };
    const showDesktop = () => {
      sessionStorage.setItem(signedKey, "1");
      login.classList.remove("show");
      hideOverlays();
      showRobotOnAgentDesktop();
    };
    const tryUnlock = () => {
      refreshOsLock();
      const expected = miniosLockPassword();
      const given = pass?.value || "";
      const err = $("os-login-error");
      if (expected && given !== expected) {
        if (err) {
          err.hidden = false;
          err.textContent = "Wrong password";
        }
        pass?.select();
        return;
      }
      if (err) err.hidden = true;
      showDesktop();
    };
    const showLogin = () => {
      boot.hidden = true;
      login.hidden = false;
      if (pass) pass.value = "";
      refreshOsLock();
      updateUbuntuClock();
      setTimeout(() => {
        if (miniosLockPassword()) pass?.focus();
        else btn?.focus();
      }, 50);
    };
    const runBoot = (thenLogin) => {
      if (skip) {
        hideOverlays();
        return;
      }
      refreshOsLock();
      boot.hidden = false;
      boot.style.opacity = "1";
      login.hidden = true;
      setTimeout(() => {
        boot.style.opacity = "0";
        setTimeout(() => {
          boot.hidden = true;
          boot.style.opacity = "";
          if (thenLogin) showLogin();
          else showDesktop();
        }, 500);
      }, 1400);
    };
    btn?.addEventListener("click", tryUnlock);
    pass?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        tryUnlock();
      }
    });
    power?.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      sessionStorage.removeItem(signedKey);
      document.querySelectorAll(".app-window").forEach((win) => {
        if (win.dataset.windowApp === "preview") {
          win.classList.remove("minimized-window", "hidden-window");
          win.dataset.open = "true";
        }
      });
      runBoot(true);
    });
    if (skip) {
      document.body.classList.add("observer-mode", "live-desktop-entered");
      $("ubuntuDesktopViewer")?.classList.add("desktop-fullscreen-overlay");
      showDesktop();
    } else if (sessionStorage.getItem(signedKey) === "1") hideOverlays();
    else runBoot(true);
  }
  function wireOsSettings() {
    const input = $("os-settings-password");
    const save = $("os-settings-save");
    const status = $("os-settings-status");
    if (!save) return;
    const fill = () => {
      if (input) input.value = miniosLockPassword();
      if (status) status.textContent = "";
    };
    fill();
    document.querySelectorAll('[data-desktop-app="settings"]').forEach((btn) => {
      btn.addEventListener("click", () => fill());
    });
    save.addEventListener("click", () => {
      const next = input?.value ?? "";
      if (next) localStorage.setItem("minios-lock-password", next);
      else localStorage.removeItem("minios-lock-password");
      refreshOsLock();
      if (status) {
        status.textContent = next ? "Password saved" : "Lock password removed";
        setTimeout(() => { if (status.textContent) status.textContent = ""; }, 2500);
      }
    });
    const WALLPAPERS = {
      1: "radial-gradient(circle at 15% 10%, rgba(233,84,32,.30), transparent 40%), radial-gradient(circle at 85% 85%, rgba(120,50,110,.45), transparent 45%), linear-gradient(160deg,#3b0f2b 0%,#5e2750 40%,#a34a2f 100%)",
      2: "radial-gradient(circle at 15% 10%, rgba(65,90,119,.35), transparent 40%), radial-gradient(circle at 85% 85%, rgba(27,38,59,.55), transparent 45%), linear-gradient(160deg,#0d1b2a 0%,#1b263b 45%,#415a77 100%)",
      3: "radial-gradient(circle at 15% 10%, rgba(74,107,58,.35), transparent 40%), radial-gradient(circle at 85% 85%, rgba(39,70,39,.5), transparent 45%), linear-gradient(160deg,#132213 0%,#274627 45%,#4a6b3a 100%)",
    };
    const applyWallpaper = (id) => {
      const key = String(id || "1");
      const area = document.querySelector(".ubuntu-desktop-area");
      if (area) {
        area.dataset.wallpaper = key;
        if (WALLPAPERS[key]) area.style.background = WALLPAPERS[key];
      }
      document.querySelectorAll(".os-swatch").forEach((s) => s.classList.toggle("active", s.dataset.bg === key));
      localStorage.setItem("minios-wallpaper", key);
    };
    document.querySelectorAll(".os-swatch").forEach((s) => {
      s.addEventListener("click", () => applyWallpaper(s.dataset.bg));
    });
    applyWallpaper(localStorage.getItem("minios-wallpaper") || "1");
  }

  function wireChrome() {
    restorePanelWidths();
    setupResizer($("left-resizer"), "left");
    setupResizer($("screen-resizer"), "right");
    $("nav-toggle")?.addEventListener("click", () => toggleLeft());
    $("hallway-close")?.addEventListener("click", () => toggleLeft(false));
    $("reopen-left")?.addEventListener("click", () => toggleLeft(true));
    $("screen-open")?.addEventListener("click", () => toggleRight(true));
    $("screen-toggle")?.addEventListener("click", () => toggleRight(false));
    $("reopen-right")?.addEventListener("click", () => toggleRight(true));
    $("workspace-close")?.addEventListener("click", () => toggleRight(false));
    $("workspace-collapse")?.addEventListener("click", () => toggleRight(false));
    $("workspace-settings")?.addEventListener("click", openUserSettings);
    $("workspace-plugins")?.addEventListener("click", openPlugins);
    $("menu-settings")?.addEventListener("click", openUserSettings);
    $("menu-plugins")?.addEventListener("click", openPlugins);
    $("layoutBackdrop")?.addEventListener("click", closeCompactDrawers);
    $("scrim")?.addEventListener("click", closeCompactDrawers);
    $("mobile-back-agents")?.addEventListener("click", (e) => {
      e.stopPropagation();
      setMobileView("agents");
    });
    $("mobile-open-workspace")?.addEventListener("click", (e) => {
      e.stopPropagation();
      setMobileView("workspace");
    });
    $("mobile-back-chat")?.addEventListener("click", (e) => {
      e.stopPropagation();
      setMobileView("chat");
    });
    $("mobile-add-agent")?.addEventListener("click", () => openAgentModal());
    $("mobile-user-btn")?.addEventListener("click", openUserSettings);
    $("mobile-search-btn")?.addEventListener("click", () => {
      const term = prompt("Search agents", $("search")?.value || "");
      if (term === null) return;
      if ($("search")) {
        $("search").value = term;
        $("search").dispatchEvent(new Event("input"));
      }
      const q = term.trim().toLowerCase();
      document.querySelectorAll("#mobile-featured [data-agent], #mobile-list [data-agent]").forEach((el) => {
        el.style.display = !q || (el.querySelector(".agent-name")?.textContent || "").toLowerCase().includes(q) ? "" : "none";
      });
    });

    $("new-bot")?.addEventListener("click", () => openAgentModal());
    $("edit-agent")?.addEventListener("click", () => {
      if (!window.deskState?.selected) return;
      $("more-menu") && ($("more-menu").hidden = true);
      openAgentModal(window.deskState.selected);
    });
    $("edit-agent-header")?.addEventListener("click", () => {
      if (!window.deskState?.selected) return;
      openAgentModal(window.deskState.selected);
    });
    $("user-btn")?.addEventListener("click", openUserSettings);
    $("plugins-btn")?.addEventListener("click", openPlugins);
    $("close-settings")?.addEventListener("click", closeUserSettings);
    $("cancel-settings")?.addEventListener("click", closeUserSettings);
    $("settings-modal")?.addEventListener("click", (e) => {
      if (e.target === $("settings-modal")) closeUserSettings();
    });
    document.querySelectorAll("[data-settings-tab]").forEach((btn) => btn.addEventListener("click", () => switchSettingsTab(btn.dataset.settingsTab)));
    $("settings-initials")?.addEventListener("input", (e) => {
      $("settings-profile-avatar").textContent = (e.target.value || "U").toUpperCase();
    });
    ["env-models-base-url", "env-models-list-url", "env-xai-api-key", "env-grok-code-api-key", "env-default-model", "env-config-path"].forEach((id) => {
      $(id)?.addEventListener("input", () => {
        updateEnvironmentPreview();
        markTomlStale();
      });
    });
    document.querySelectorAll(".secret-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = $(btn.dataset.secretTarget);
        if (!input) return;
        const showing = input.type === "text";
        input.type = showing ? "password" : "text";
        btn.textContent = showing ? "Show" : "Hide";
      });
    });
    $("raw-toml-editor")?.addEventListener("input", (e) => {
      e.target.dataset.userEdited = "1";
    });
    $("regenerate-toml-btn")?.addEventListener("click", () => {
      const editor = $("raw-toml-editor");
      editor.dataset.userEdited = "";
      editor.value = generateTomlFromForm();
    });
    $("copy-toml-btn")?.addEventListener("click", async () => {
      await copyText($("raw-toml-editor")?.value || generateTomlFromForm());
      flashSettingsStatus("config.toml copied");
    });
    $("download-toml-btn")?.addEventListener("click", () => {
      downloadTextFile("config.toml", $("raw-toml-editor")?.value || generateTomlFromForm(), "text/plain");
      flashSettingsStatus("config.toml downloaded");
    });
    $("copy-env-exports")?.addEventListener("click", async () => {
      await copyText(generateEnvExports());
      flashSettingsStatus("Environment exports copied");
    });
    $("load-toml-btn")?.addEventListener("click", () => $("load-toml-input")?.click());
    $("load-toml-input")?.addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      const editor = $("raw-toml-editor");
      editor.value = text;
      editor.dataset.userEdited = "1";
      importSimpleToml(text);
      e.target.value = "";
      flashSettingsStatus(`Loaded ${file.name}`);
    });
    $("add-settings-model")?.addEventListener("click", () => {
      syncSettingsModelsFromDom();
      const base = `custom-${Date.now().toString().slice(-5)}`;
      userSettings.models.push({
        key: base,
        model: base,
        name: "New local model",
        baseUrl: "http://127.0.0.1:8000/v1",
        apiBackend: "chat_completions",
        contextWindow: 131072,
        maxCompletionTokens: 16384,
        apiKey: "",
        weights: "",
      });
      renderSettingsModels();
      markTomlStale();
    });
    $("settings-desk-host")?.addEventListener("input", updateDeskAccessHint);
    $("settings-desk-port")?.addEventListener("input", updateDeskAccessHint);
    $("add-cluster-peer")?.addEventListener("click", () => {
      if (!deskIsLoopback()) return;
      clusterPeers.push({ name: "", url: "http://" });
      renderClusterPeers();
    });
    $("cluster-role-create")?.addEventListener("click", () => {
      setClusterRole("create");
      updateClusterWizard();
    });
    $("cluster-role-paste")?.addEventListener("click", () => {
      setClusterRole("paste");
      $("cluster-token-paste")?.focus();
      updateClusterWizard();
    });
    $("cluster-save-name")?.addEventListener("click", async () => {
      try {
        const name = ($("settings-node-name")?.value || "").trim();
        if (!name) throw new Error("Type a name for this computer first.");
        const j = await saveClusterOnly({ node_name: name });
        if (j.node_name) $("settings-node-name").value = j.node_name;
        if (window.deskState) window.deskState.nodeName = j.node_name || name;
        updateClusterWizard();
        flashSettingsStatus(`Name saved as ${j.node_name || name}. Next: step 2.`, 8000);
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 10000, "error");
      }
    });
    $("cluster-save-token")?.addEventListener("click", async () => {
      try {
        const tok = ($("cluster-token-paste")?.value || "").trim();
        if (!tok) throw new Error("Paste the token from teela-brain first.");
        const j = await saveClusterOnly({ cluster_token: tok });
        clusterTokenSet = Boolean(j.cluster_token_set);
        clusterGeneratedToken = tok;
        if (j.cluster_token_fp && window.deskState) window.deskState.clusterTokenFp = j.cluster_token_fp;
        updateClusterTokenState();
        const n2 = $("cluster-next-2");
        if (n2) n2.textContent = "Token saved on this computer. Next: step 3 — add teela-brain as http://10.0.0.10:8742, then Save peers.";
        flashSettingsStatus("Token saved on this computer. Next: step 3 — add the other computer, Save peers.", 10000);
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 10000, "error");
      }
    });
    $("cluster-save-peers")?.addEventListener("click", async () => {
      try {
        const peers = readClusterPeersFromDom();
        if (!peers.length) throw new Error("Fill a peer name and http://IP:8742 first.");
        const j = await saveClusterOnly({ peers, peers_loaded: true });
        if (Array.isArray(j.peers)) clusterPeers = j.peers.map((p) => ({ ...p }));
        if (!clusterPeers.length) clusterPeers = peers;
        clusterTokenSet = j.cluster_token_set != null ? Boolean(j.cluster_token_set) : clusterTokenSet;
        if (j.listen_host && $("settings-desk-host")) {
          $("settings-desk-host").value = j.listen_host;
          updateDeskAccessHint();
        }
        renderClusterPeers();
        updateClusterTokenState();
        flashSettingsStatus(`Saved ${readClusterPeersFromDom().length} peer(s). This desk now listens on the LAN so those hosts can see your bots. Next: step 4 Test connection — do this on both computers.`, 10000);
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 10000, "error");
      }
    });
    $("settings-node-name")?.addEventListener("input", () => updateClusterWizard());
    $("generate-cluster-token")?.addEventListener("click", async () => {
      if (!deskIsLoopback()) {
        flashSettingsStatus("Open http://127.0.0.1:8742/ on this machine to generate a token.", 8000, "error");
        return;
      }
      try {
        const j = await deskApi("/v1/cluster/token-new", { method: "POST", body: "{}" });
        const token = j.cluster_token || "";
        if (!token) throw new Error("server did not return a token");
        clusterGeneratedToken = token;
        showClusterTokenReveal(token);
        const saved = await saveClusterOnly({ cluster_token: token });
        clusterTokenSet = Boolean(saved.cluster_token_set);
        if (saved.cluster_token_fp && window.deskState) window.deskState.clusterTokenFp = saved.cluster_token_fp;
        updateClusterTokenState();
        try {
          await copyText(token);
          const n2 = $("cluster-next-2");
          if (n2) {
            n2.textContent =
              "New token saved on this computer. Copy it, then on teela-body: paste → Save token. Fingerprints must match. Old token on body will stop working until you paste this one.";
          }
          flashSettingsStatus(
            "Token reset and saved here. Copy it, paste on teela-body (step 2), Save token, then Test.",
            12000
          );
          updateClusterWizard();
        } catch {
          flashSettingsStatus("Token is visible — Copy it, then paste on the other computer in step 2.", 12000);
        }
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 8000, "error");
      }
    });
    $("copy-cluster-token")?.addEventListener("click", async () => {
      if (await copyClusterToken()) flashSettingsStatus("Cluster token copied", 4000);
    });
    $("copy-cluster-token-reveal")?.addEventListener("click", async () => {
      if (await copyClusterToken()) flashSettingsStatus("Cluster token copied", 4000);
    });
    $("cluster-token-reveal-value")?.addEventListener("click", (e) => {
      e.target.select();
    });
    $("clear-cluster-token")?.addEventListener("click", async () => {
      if (!deskIsLoopback()) return;
      try {
        await saveClusterOnly({ cluster_token_clear: true });
        clusterClearRequested = false;
        clusterGeneratedToken = "";
        clusterTokenSet = false;
        if (window.deskState) window.deskState.clusterTokenFp = "";
        if ($("settings-cluster-token")) {
          $("settings-cluster-token").value = "";
          $("settings-cluster-token").type = "password";
        }
        if ($("toggle-cluster-token")) $("toggle-cluster-token").textContent = "Show";
        hideClusterTokenReveal();
        updateClusterTokenState();
        flashSettingsStatus("Token cleared on this computer. Generate a new one, then paste it on the other hosts.", 10000);
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 10000, "error");
      }
    });
    $("test-cluster")?.addEventListener("click", async () => {
      const el = $("cluster-self-test");
      const btn = $("test-cluster");
      if (btn) btn.disabled = true;
      try {
        const j = await deskApi("/v1/cluster/self-test", { method: "POST", body: "{}" });
        const lines = [];
        lines.push(`This host: ${j.node || "?"}`);
        lines.push(`Token saved: ${j.token_set ? "yes" : "NO"}`);
        lines.push(`Saved peers: ${j.peer_count ?? (j.results || []).length}`);
        if (j.hint) lines.push("", j.hint);
        const rows = j.results || [];
        if (!rows.length && !j.hint) {
          lines.push("", "Nothing to test — add a peer, Save, then Test again.");
        }
        for (const r of rows) {
          lines.push("");
          lines.push(`${r.name}  ${r.url}`);
          lines.push(`  forward ${r.forward}   reverse ${r.reverse}   reverse_config ${r.reverse_config}`);
          if (r.warning) lines.push(`  warning: ${r.warning}`);
        }
        if (el) {
          el.hidden = false;
          el.textContent = lines.join("\n") + "\n\n" + JSON.stringify(j, null, 2);
        }
        const failed = !j.token_set || !rows.length || rows.some((r) => r.forward !== "ok");
        const n4 = $("cluster-next-4");
        if (n4) {
          if (!j.token_set) n4.textContent = "Next: finish step 2 — save the token on this computer.";
          else if (!rows.length) n4.textContent = "Next: finish step 3 — Save peers (name + http://IP:8742).";
          else if (rows.some((r) => r.forward === "auth")) n4.textContent = "Tokens do not match. Paste the SAME token from brain onto this host (step 2 → paste → Save token).";
          else if (rows.some((r) => r.forward === "offline")) n4.textContent = "Peer is offline. Check the IP:8742, that deskd is running there, and Hermes Desk address is that machine’s LAN IP.";
          else if (rows.some((r) => r.reverse !== "ok")) n4.textContent = "This host can see the peer, but the peer has not added this host yet. On the other computer, add this host as a peer (step 3) and Save.";
          else n4.textContent = "Mesh hello ok. You can chat from either desk. Phones still use the brain URL.";
        }
        flashSettingsStatus(
          j.hint || (failed ? "Test finished — read the blue Next box in step 4." : "Connected. See step 4."),
          10000,
          failed ? "error" : "ok"
        );
        updateClusterWizard();
      } catch (err) {
        if (el) {
          el.hidden = false;
          el.textContent = String(err.message || err);
        }
        flashSettingsStatus(String(err.message || err), 10000, "error");
      } finally {
        if (btn) btn.disabled = false;
      }
    });
    $("new-agent-home")?.addEventListener("change", async () => {
      try {
        await fillCreateModels($("new-agent-home").value);
      } catch (err) {
        alert(err.message || err);
      }
    });
    $("save-settings")?.addEventListener("click", async () => {
      syncSettingsModelsFromDom();
      userSettings.profile = readProfileForm();
      userSettings.environment = readEnvironmentForm();
      userSettings.defaultModel = $("settings-default-model")?.value || userSettings.models[0]?.key || "";
      const editor = $("raw-toml-editor");
      userSettings.rawTomlOverride = editor?.dataset.userEdited ? editor.value : "";
      localStorage.setItem("hermes-desk-user-settings", JSON.stringify(userSettings));
      localStorage.setItem("teela-user-settings", JSON.stringify(userSettings));
      refreshProfileUi();
      try {
        if (window.deskSaveAccess) {
          const payload = {
            listen_host: $("settings-desk-host")?.value.trim() || "127.0.0.1",
            listen_port: Number($("settings-desk-port")?.value || 8742),
            models: userSettings.models,
            default_model: userSettings.defaultModel,
          };
          const wantCluster = clusterTabOpened;
          if (wantCluster && !deskIsLoopback()) {
            flashSettingsStatus(
              "Cluster token/peers were not saved. Open http://127.0.0.1:8742/ on this machine (not the LAN IP or a phone).",
              12000,
              "error"
            );
            return;
          }
          if (deskIsLoopback() && clusterTabOpened) {
            payload.node_name = $("settings-node-name")?.value.trim() || "";
            const confirm = $("settings-cluster-token-confirm")?.value || "";
            const tok = $("cluster-token-paste")?.value.trim() || $("settings-cluster-token")?.value || clusterGeneratedToken || "";
            if (clusterClearRequested) {
              payload.cluster_token_clear = true;
              if (confirm) payload.cluster_token_confirm = confirm;
            } else if (tok && (!clusterTokenSet || confirm)) {
              payload.cluster_token = tok;
              if (confirm) payload.cluster_token_confirm = confirm;
            }
            payload.peers = readClusterPeersFromDom();
            payload.peers_loaded = true;
          }
          const j = await window.deskSaveAccess(payload);
          clusterClearRequested = false;
          if (j.cluster_token_set != null) clusterTokenSet = Boolean(j.cluster_token_set);
          if (Array.isArray(j.peers)) clusterPeers = j.peers.map((p) => ({ ...p }));
          if (j.listen_host && $("settings-desk-host")) {
            $("settings-desk-host").value = j.listen_host;
            updateDeskAccessHint();
          }
          renderClusterPeers();
          updateClusterTokenState();
          const n = (j.peers || []).length;
          const bits = [];
          if (clusterTabOpened) {
            bits.push(clusterTokenSet ? "token saved" : "token still empty");
            bits.push(`${n} peer${n === 1 ? "" : "s"}`);
            if (!n) bits.push("add teela-brain http://10.0.0.10:8742 then Save again");
          }
          const url = j.access_url || "";
          flashSettingsStatus(
            ["Saved", bits.join(", "), url ? `desk ${url}` : ""].filter(Boolean).join(" — "),
            10000,
            clusterTabOpened && !clusterTokenSet ? "error" : "ok"
          );
        } else {
          flashSettingsStatus("Saved");
        }
      } catch (err) {
        flashSettingsStatus(String(err.message || err), 10000, "error");
        return;
      }
    });

    $("close-plugins-modal")?.addEventListener("click", closePlugins);
    $("plugins-modal")?.addEventListener("click", (e) => {
      if (e.target === $("plugins-modal")) closePlugins();
    });
    $("plugin-search")?.addEventListener("input", () => renderPluginCatalog($("plugin-search").value));
    $("add-custom-plugin")?.addEventListener("click", () => {
      $("custom-plugin-form")?.classList.remove("hidden");
      $("custom-plugin-name")?.focus();
    });
    $("cancel-custom-plugin")?.addEventListener("click", () => $("custom-plugin-form")?.classList.add("hidden"));
    $("save-custom-plugin")?.addEventListener("click", () => {
      const name = $("custom-plugin-name").value.trim();
      const endpoint = $("custom-plugin-endpoint").value.trim();
      if (!name) {
        $("custom-plugin-name").focus();
        return;
      }
      pluginCatalog.push({ id: `custom-${Date.now()}`, name, endpoint, description: endpoint ? `Custom plugin: ${endpoint}` : "Custom plugin", icon: "🔌", installed: true });
      savePlugins();
      $("custom-plugin-name").value = "";
      $("custom-plugin-endpoint").value = "";
      $("custom-plugin-form").classList.add("hidden");
      renderPluginCatalog($("plugin-search")?.value || "");
    });

    $("close-agent-modal")?.addEventListener("click", closeAgentModal);
    $("cancel-agent-create")?.addEventListener("click", closeAgentModal);
    $("clear-agent-chat")?.addEventListener("click", async () => {
      const id = $("agent-edit-id")?.value || window.deskState?.selected;
      if (!id || !window.deskClearChat) return;
      const bot = (window.deskState?.bots || []).find((b) => b.id === id);
      const name = bot?.name || "this bot";
      if (!confirm(`Clear the live chat with ${name}? The log still keeps it. Export and the agent session start empty.`)) return;
      const btn = $("clear-agent-chat");
      if (btn) btn.disabled = true;
      try {
        await window.deskClearChat(id);
      } catch (err) {
        alert(err.message || err);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
    $("agent-import-chat")?.addEventListener("click", (e) => {
      const id = $("agent-edit-id")?.value || window.deskState?.selected;
      if (!id) return;
      const picker = $("import-file");
      if (!picker) return;
      picker.dataset.replace = e.shiftKey ? "1" : "";
      picker.click();
    });
    $("agent-delete-bot")?.addEventListener("click", async () => {
      const id = $("agent-edit-id")?.value || window.deskState?.selected;
      if (!id || !window.deskDeleteBot) return;
      const ok = await window.deskDeleteBot(id);
      if (ok) closeAgentModal();
    });
    $("agent-modal")?.addEventListener("click", (e) => {
      if (e.target === $("agent-modal")) closeAgentModal();
    });
    document.querySelectorAll(".color-swatch").forEach((swatch) => {
      swatch.addEventListener("click", () => setAgentSwatch(swatch.dataset.color));
    });
    document.querySelectorAll('input[name="agent-kind"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        syncAgentKindCopy();
        const home = $("new-agent-home")?.value;
        fillCreateModels(home, $("new-agent-model")?.value).catch(() => {});
      });
    });
    $("new-agent-shape")?.addEventListener("change", refreshAgentPreview);
    $("create-agent-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = $("new-agent-name").value.trim();
      if (!name) return;
      const payload = {
        name,
        description: $("new-agent-description").value.trim(),
        soul: $("new-agent-soul").value,
        model: $("new-agent-model").value,
        kind: selectedAgentKind(),
        emoji: "◉",
        avatar_color: selectedNewAgentColor,
        avatar_shape: $("new-agent-shape")?.value || "",
        home_node: $("new-agent-home")?.value || "",
      };
      const editId = $("agent-edit-id")?.value || "";
      const btn = e.submitter || $("agent-submit");
      if (btn) btn.disabled = true;
      try {
        if (editId) {
          if (!window.deskUpdateBot) throw new Error("Update is not available");
          const update = { ...payload };
          delete update.home_node;
          delete update.kind;
          update.workspace_share_with = selectedWorkspaceShareIds();
          await window.deskUpdateBot(editId, update);
          saveAvatar(editId, { color: selectedNewAgentColor, shape: $("new-agent-shape").value });
        } else {
          if (!window.deskCreateBot) return;
          const botId = await window.deskCreateBot(payload);
          if (botId) saveAvatar(botId, { color: selectedNewAgentColor, shape: $("new-agent-shape").value, state: "idle" });
        }
        closeAgentModal();
      } catch (err) {
        alert(err.message || err);
      } finally {
        if (btn) btn.disabled = false;
      }
    });

    $("add-hourly-note")?.addEventListener("click", () => {
      if (!window.deskState?.selected) return;
      openHourlyNoteModal();
    });
    $("close-hourly-modal")?.addEventListener("click", closeHourlyNoteModal);
    $("cancel-hourly")?.addEventListener("click", closeHourlyNoteModal);
    $("hourly-modal")?.addEventListener("click", (e) => {
      if (e.target === $("hourly-modal")) closeHourlyNoteModal();
    });
    $("hourly-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!window.deskState?.selected) return;
      const id = $("hourly-edit-id").value || (crypto.randomUUID ? crypto.randomUUID() : `note-${Date.now()}`);
      const interval = Number($("hourly-interval").value || 1);
      const note = {
        id,
        title: $("hourly-title").value.trim(),
        text: $("hourly-text").value.trim(),
        interval,
        enabled: $("hourly-enabled").checked,
        nextCheck: $("hourly-enabled").checked ? `In ${interval} hour${interval === 1 ? "" : "s"}` : "Paused",
      };
      const list = loadHourlyNotes();
      const idx = list.findIndex((n) => n.id === id);
      if (idx >= 0) list[idx] = note;
      else list.unshift(note);
      saveHourlyNotes(list);
      closeHourlyNoteModal();
      renderHourlyNotes();
    });

    const openRoutinesPanel = () => openRoutineModal();
    const closeRoutinesPanel = () => {
      $("routines")?.classList.remove("routines-opened");
      $("routines")?.setAttribute("aria-hidden", "true");
      closeRoutineModal();
    };
    $("routines-open")?.addEventListener("click", () => openRoutineModal());
    $("close-routines-panel")?.addEventListener("click", closeRoutinesPanel);

    $("new-routine")?.addEventListener("click", () => openRoutineModal());
    $("close-routine-modal")?.addEventListener("click", closeRoutineModal);
    $("cancel-routine")?.addEventListener("click", closeRoutineModal);
    $("routine-modal")?.addEventListener("click", (e) => {
      if (e.target === $("routine-modal")) closeRoutineModal();
    });
    $("routine-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!window.deskSaveRoutine) return;
      const type = $("routine-type").value;
      let instruction = $("routine-instruction").value.trim();
      if (type === "shell_command") instruction = `Run this shell command in the workspace and report the result:\n${instruction}`;
      else if (type === "script") instruction = `Run this script in the workspace and report the result:\n${instruction}`;
      else if (type === "http_request") instruction = `Perform this HTTP request from the workspace and summarize the response:\n${instruction}`;
      await window.deskSaveRoutine({
        id: $("routine-edit-id").value || "",
        name: $("routine-name").value.trim(),
        instruction,
        schedule: scheduleToDesk($("routine-unit").value, $("routine-every").value, $("routine-cron").value),
        enabled: $("routine-enabled").checked,
        type,
        timezone: $("routine-timezone").value.trim(),
        cron: $("routine-cron").value.trim(),
        schedulePreset: $("routine-unit").value,
      });
      openRoutineModal();
    });

    const setLiveDesktopEntered = (entered) => {
      const viewer = $("ubuntuDesktopViewer");
      if (!viewer) return;
      const phone = getLayoutMode() === "phone";
      viewer.classList.toggle("desktop-fullscreen-overlay", entered);
      document.body.classList.toggle("live-desktop-entered", entered);
      const enterBtn = $("desktop-fullscreen");
      if (enterBtn) enterBtn.title = entered ? "Exit Live Desktop" : "Enter Live Desktop";
      if (entered) {
        app.classList.remove("right-collapsed", "screen-closed");
        if (!phone) {
          if (!app.dataset.preLiveRightW) {
            app.dataset.preLiveRightW = String(parseFloat(getComputedStyle(app).getPropertyValue("--right-w")) || 365);
          }
          applyLiveDesktopWidth();
        }
        viewer.focus();
        showRobotOnAgentDesktop();
      } else if (!phone && app.dataset.preLiveRightW) {
        const prev = parseFloat(app.dataset.preLiveRightW);
        delete app.dataset.preLiveRightW;
        if (Number.isFinite(prev)) setRightWidth(prev, false);
      }
      requestAnimationFrame(() => {
        sizeLiveDesktopArea();
        window.dispatchEvent(new Event("resize"));
      });
    };
    $("desktop-fullscreen")?.addEventListener("click", () => {
      const viewer = $("ubuntuDesktopViewer");
      setLiveDesktopEntered(!viewer?.classList.contains("desktop-fullscreen-overlay"));
    });
    $("live-desktop-exit")?.addEventListener("click", () => setLiveDesktopEntered(false));
    $("ubuntuDesktopViewer")?.addEventListener("keydown", (e) => {
      if (e.target.closest("input,textarea,select")) return;
      if (!e.altKey) return;
      const apps = { 1: "hermes", 2: "browser", 3: "files", 4: "terminal", 5: "editor", 6: "preview", 7: "dev", 8: "settings", 9: "notepad" };
      if (apps[e.key]) {
        e.preventDefault();
        openDesktopWindow(apps[e.key]);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("settings-modal")?.classList.contains("hidden")) return closeUserSettings();
      if (!$("plugins-modal")?.classList.contains("hidden")) return closePlugins();
      if (!$("agent-modal")?.classList.contains("hidden")) return closeAgentModal();
      if (!$("routine-modal")?.classList.contains("hidden")) return closeRoutineModal();
      if ($("routines")?.classList.contains("routines-opened")) return closeRoutinesPanel();
      if (!$("hourly-modal")?.classList.contains("hidden")) return closeHourlyNoteModal();
      if ($("ubuntuDesktopViewer")?.classList.contains("desktop-fullscreen-overlay")) {
        setLiveDesktopEntered(false);
        return;
      }
      if (document.body.classList.contains("desktop-takeover-active") && !document.getElementById("chrome-window")?.classList.contains("is-driving")) {
        e.preventDefault();
        $("im-done")?.click();
        return;
      }
      if (getLayoutMode() !== "desktop") closeCompactDrawers();
    });

    document.querySelectorAll("[data-file-place]").forEach((btn) => {
      btn.addEventListener("click", () => {
        window.deskBrowseFiles?.(btn.dataset.filePlace || "");
      });
    });

    wireDesktopWindowManager();
    wireDockSnap();
    wireDockMagnify();
    wireOsSession();
    wireOsSettings();
    setupResponsiveDesktop();
    applyResponsiveLayout();
    updateUbuntuClock();
    setInterval(updateUbuntuClock, 30000);
    refreshProfileUi();
    window.addEventListener("resize", () => {
      if (typeof window.fitPhoneFrame === "function") {
        window.fitPhoneFrame();
      } else if (window.matchMedia("(max-width: 760px)").matches) {
        const vv = window.visualViewport;
        const h = Math.max(320, Math.round(vv?.height || window.innerHeight || 0));
        const top = Math.max(0, Math.round(vv?.offsetTop || 0));
        document.documentElement.style.setProperty("--app-h", `${h}px`);
        document.documentElement.style.setProperty("--app-top", `${top}px`);
      } else {
        document.documentElement.style.removeProperty("--app-h");
        document.documentElement.style.removeProperty("--app-top");
      }
      applyResponsiveLayout();
      setupResponsiveDesktop();
      sizeLiveDesktopArea();
    });
  }

  wireChrome();

  window.DeskUI = {
    avatarHTML,
    avatarMeta,
    saveAvatar,
    hydrateLocalAvatars,
    paintHeaderAvatar,
    setEmotion: setAgentEmotion,
    settleEmotion,
    isBusy: isEmotionBusy,
    syncEmotionFromStatus,
    inferEmotion,
    renderRail: renderAgentRail,
    renderMobile: renderMobileHome,
    openWindow: openDesktopWindow,
    focusWindow: focusDesktopWindow,
    minimizeWindow: minimizeDesktopWindow,
    maximizeWindow: toggleMaximizeDesktopWindow,
    ensureMaximized: ensureMaximizedDesktopWindow,
    closeWindow: closeDesktopWindow,
    openAgentModal,
    openUserSettings,
    openPlugins,
    isWindowLive,
    showRobotOnAgentDesktop,
    appForSurface: (surface) => APP_FOR_SURFACE[surface] || surface,
    surfaceForApp: (name) => SURFACE_FOR_APP[name] || name,
    enterTakeOver: enterDesktopTakeOver,
    exitTakeOver: exitDesktopTakeOver,
    renderHourlyNotes,
    openRoutineModal,
    userSettings: () => userSettings,
    refreshOsLock,
    toggleLeft,
    toggleRight,
    setMobileView,
    updateHandles: updatePanelHandles,
    scheduleToDesk,
  };
})();
