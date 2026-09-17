const state = {
  token: "",
  bots: [],
  selected: null,
  nodeName: "",
  peers: [],
  isLoopback: false,
  voice: false,
  files: [],
  filesDir: "",
  workspaceId: "",
  pendingImages: [],
  lastRobotCommand: null,
  surface: "preview",
  catalog: [],
  browserW: 1280,
  browserH: 800,
  editorPath: "",
  notepadPath: "",
  previewPath: "",
  dev: { kind: "generic", commands: {} },
  tps: 0,
  timeline: { activeId: null, chats: [] },
  timelineQuery: "",
  timelineOpen: false,
  working: {},
  stopped: {},
  teelaBrainTaken: false,
};
window.deskState = state;
window.deskObserverReady = false;
const observerBotId = new URLSearchParams(window.location.search).get("observe") || "";
if (observerBotId) document.body.classList.add("observer-mode");

const terms = { shell: null, tui: null };
let browserTimer = 0;

function applyTheme(theme) {
  const next = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  document.body.classList.toggle("dark", next === "dark");
  localStorage.setItem("hermes-desk-theme", next);
  const btn = $("theme-toggle");
  if (btn) {
    btn.textContent = "◐";
    btn.title = next === "dark" ? "Switch to light" : "Switch to dark";
  }
}

function mqDrawer() {
  return window.matchMedia("(max-width: 900px)").matches;
}

function fitTerms() {
  for (const kind of ["shell", "tui"]) {
    const slot = terms[kind];
    if (!slot?.term) continue;
    const host = $(kind === "tui" ? "tui-term" : "shell-term");
    if (!host || host.clientHeight < 24 || host.clientWidth < 40) continue;
    let cols = slot.term.cols;
    let rows = slot.term.rows;
    try {
      const dims = slot.term._core?._renderService?.dimensions?.css?.cell;
      const cellW = Number(dims?.width) || 9;
      const cellH = Number(dims?.height) || 17;
      cols = Math.max(2, Math.floor(host.clientWidth / cellW));
      rows = Math.max(1, Math.floor(host.clientHeight / cellH));
      if (slot.term.cols !== cols || slot.term.rows !== rows) {
        slot.term.resize(cols, rows);
      }
    } catch {
      try {
        slot.fit?.fit();
        cols = slot.term.cols;
        rows = slot.term.rows;
      } catch {
        /* layout not ready */
      }
    }
    if (!state.selected || !cols || !rows) continue;
    const key = `${state.selected}:${cols}x${rows}`;
    if (slot._size === key) continue;
    slot._size = key;
    api(`/v1/bots/${state.selected}/${kind}/resize`, {
      method: "POST",
      body: JSON.stringify({ cols, rows }),
    }).catch(() => {});
  }
}

function clampScreenW(px) {
  const vw = window.innerWidth;
  if (mqDrawer()) {
    return Math.round(Math.min(vw * 0.92, Math.max(240, px)));
  }
  const hall = $("hallway")?.getBoundingClientRect().width || 252;
  const minChat = 280;
  const minS = 240;
  const maxS = Math.max(minS, vw - hall - minChat);
  return Math.round(Math.min(maxS, Math.max(minS, px)));
}

function applyScreenWidth(px, persist = true) {
  const w = clampScreenW(px);
  document.documentElement.style.setProperty("--screen-w", `${w}px`);
  document.documentElement.style.setProperty("--right-w", `${w}px`);
  if (persist) localStorage.setItem("hermes-desk-screen-w", String(w));
  return w;
}

function applyHallwayWidth(px, persist = true) {
  const w = Math.round(Math.min(420, Math.max(200, px)));
  document.documentElement.style.setProperty("--hallway-w", `${w}px`);
  document.documentElement.style.setProperty("--left-w", `${w}px`);
  if (persist) localStorage.setItem("hermes-desk-hallway-w", String(w));
  return w;
}

function applyLeftOpen(open, persist = true) {
  if (window.DeskUI?.toggleLeft) {
    DeskUI.toggleLeft(open);
    return;
  }
  $("app").classList.toggle("left-collapsed", !open);
  if (persist) localStorage.setItem("hermes-desk-left-open", open ? "1" : "0");
  requestAnimationFrame(() => {
    fitTerms();
    window.dispatchEvent(new Event("resize"));
  });
}

function isScreenOpen() {
  return !$("app").classList.contains("screen-closed") && !$("app").classList.contains("right-collapsed");
}

function updateScrim() {
  const show = mqDrawer() && (document.body.classList.contains("hallway-open") || isScreenOpen());
  if ($("scrim")) $("scrim").hidden = !show;
}

function setHallwayOpen(open) {
  document.body.classList.toggle("hallway-open", !!open);
  updateScrim();
}

function applyScreenOpen(open, persist = true) {
  if (window.DeskUI?.toggleRight) {
    DeskUI.toggleRight(open);
    return;
  }
  $("app").classList.toggle("screen-closed", !open);
  $("app").classList.toggle("right-collapsed", !open);
  if (persist) localStorage.setItem("hermes-desk-screen-open", open ? "1" : "0");
  if (open && mqDrawer()) setHallwayOpen(false);
  updateScrim();
  requestAnimationFrame(() => {
    fitTerms();
    window.dispatchEvent(new Event("resize"));
  });
}

function restoreLayout() {
  if (window.DeskUI) return;
  const savedW = parseInt(localStorage.getItem("hermes-desk-screen-w") || "", 10);
  const savedH = parseInt(localStorage.getItem("hermes-desk-hallway-w") || "", 10);
  if (Number.isFinite(savedW) && savedW > 0) applyScreenWidth(savedW, false);
  if (Number.isFinite(savedH) && savedH > 0) applyHallwayWidth(savedH, false);
  applyScreenOpen(localStorage.getItem("hermes-desk-screen-open") !== "0", false);
  if (localStorage.getItem("hermes-desk-left-open") === "0") applyLeftOpen(false, false);
}

function isLoopbackHost() {
  const h = (location.hostname || "").toLowerCase();
  return h === "127.0.0.1" || h === "localhost" || h === "[::1]" || h === "::1";
}

async function bootstrap() {
  const r = await fetch("/v1/bootstrap");
  const j = await r.json();
  state.token = j.token;
  if (j.listen_host) state.listenHost = j.listen_host;
  if (j.listen_port) state.listenPort = j.listen_port;
  if (j.access_url) state.accessUrl = j.access_url;
  if (j.node_name) state.nodeName = j.node_name;
  state.isLoopback = isLoopbackHost();
}

function headers(extra = {}) {
  return { Authorization: `Bearer ${state.token}`, ...extra };
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: headers(opts.body ? { "Content-Type": "application/json" } : {}),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const CHATTERBOX_TURBO_TAGS = new Set([
  "angry", "fear", "surprised", "whispering", "advertisement", "dramatic",
  "narration", "crying", "happy", "sarcastic", "clear throat", "sigh", "shush",
  "cough", "groan", "sniff", "gasp", "chuckle", "laugh",
]);
const CHATTERBOX_TAG_ALIASES = {
  laughs: "laugh", laughter: "laugh", chuckles: "chuckle", whisper: "whispering",
  sad: "crying", surprise: "surprised", shhh: "shush", shh: "shush", sush: "shush",
  clear_throat: "clear throat", clearthroat: "clear throat",
};

function chatterboxTagName(raw) {
  const key = String(raw || "").trim().toLowerCase().replace(/\s+/g, " ");
  return CHATTERBOX_TAG_ALIASES[key] || key;
}

function stripThinkTags(text) {
  let out = String(text ?? "");
  if (!/<\/?think\b/i.test(out)) return out;
  out = out
    .replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, " ")
    .replace(/<\/?think\b[^>]*>/gi, " ")
    .replace(/[ \t]+/g, " ");
  const parts = out.split(/(?<=[.!?])\s+/).map((p) => p.trim()).filter(Boolean);
  const collapsed = [];
  for (const part of parts) {
    if (collapsed.length && collapsed[collapsed.length - 1].toLowerCase() === part.toLowerCase()) continue;
    collapsed.push(part);
  }
  return collapsed.length ? collapsed.join(" ") : out;
}

function stripChatterboxTags(text) {
  // Do not trim() — stream merge calls this after every chunk, and eating a
  // trailing "\n\n" before the next "- Host" line flattened TUI markdown.
  return stripThinkTags(String(text ?? ""))
    .replace(/\[([^\[\]]+)\]/g, (m, inner) => (CHATTERBOX_TURBO_TAGS.has(chatterboxTagName(inner)) ? "" : m))
    .replace(/ {2,}/g, " ");
}

function keepChatterboxTags(text) {
  return String(text ?? "").replace(/\[([^\[\]]+)\]/g, (m, inner) => {
    const name = chatterboxTagName(inner);
    return CHATTERBOX_TURBO_TAGS.has(name) ? `[${name}]` : "";
  }).replace(/ {2,}/g, " ").trim();
}

function stripAssistantPadding(text) {
  // Match deskd.strip_assistant_padding: drop leading blank lines only.
  return stripChatterboxTags(String(text ?? "")).replace(/^[\n\r]+/, "");
}

const STREAM_RESTART_HEAD = 80;

function collapseRestartedAssistant(text, headN) {
  text = String(text ?? "");
  const n = headN || STREAM_RESTART_HEAD;
  if (text.length < n * 2) return text;
  const head = text.slice(0, n);
  const pos = text.indexOf(head, n);
  if (pos < 0) return text;
  const first = text.slice(0, pos);
  const second = text.slice(pos);
  let lcp = 0;
  const lim = Math.min(first.length, second.length);
  while (lcp < lim && first[lcp] === second[lcp]) lcp += 1;
  if (lcp < n) return text;
  return second.length >= first.length ? second : first;
}

function mergeAssistantStream(cur, incoming) {
  cur = String(cur ?? "");
  incoming = String(incoming ?? "");
  if (!incoming) return collapseRestartedAssistant(cur);
  if (!cur) return incoming;
  const inc = incoming.replace(/^[\n\r]+/, "") || incoming;
  if (incoming === cur || inc === cur) return cur;
  if (incoming.startsWith(cur)) return collapseRestartedAssistant(incoming);
  if (inc.startsWith(cur)) return collapseRestartedAssistant(inc);
  if (cur.startsWith(incoming) && incoming.length >= Math.min(32, cur.length)) return cur;
  if (cur.startsWith(inc) && inc.length >= Math.min(32, cur.length)) return cur;
  if (cur.length >= 64 && inc.includes(cur)) return collapseRestartedAssistant(inc);
  if (inc.length >= 64 && cur.includes(inc)) return cur;
  let lcp = 0;
  const lim = Math.min(cur.length, inc.length);
  while (lcp < lim && cur[lcp] === inc[lcp]) lcp += 1;
  if (lcp >= STREAM_RESTART_HEAD) {
    return collapseRestartedAssistant(inc.length >= cur.length ? inc : cur);
  }
  const minOverlap = 8;
  for (const src of [incoming, inc]) {
    const max = Math.min(cur.length, src.length);
    for (let k = max; k >= minOverlap; k--) {
      if (cur.endsWith(src.slice(0, k))) return collapseRestartedAssistant(cur + src.slice(k));
    }
  }
  return collapseRestartedAssistant(cur + incoming);
}

function mdInline(s) {
  s = s.replace(/!\[([^\]]*)\]\((https?:[^)\s]+)\)/g, (_, alt, href) => {
    return `<img src="${href}" alt="${alt}" class="md-img">`;
  });
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/`{1,3}([^`]+)`{1,3}/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+?)__/g, "<strong>$1</strong>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:!?)\]])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return s;
}

function renderCodeBlock(lang, code, open, fold) {
  const label = escapeHtml(lang || "code");
  const foldAttr = fold ? ` data-fold="${escapeHtml(fold)}"` : "";
  return `<details class="code-block${open ? " is-open" : ""}"${foldAttr}>
    <summary class="code-block-bar">
      <span class="code-lang">${label}</span>
      <button type="button" class="code-copy" title="Copy code">Copy</button>
    </summary>
    <pre><code>${escapeHtml(code)}</code></pre>
  </details>`;
}

function splitMarkdown(text) {
  const chunks = [];
  const lines = String(text).split("\n");
  let i = 0;
  let buf = [];
  const flushMd = () => {
    if (buf.length) chunks.push({ type: "md", text: buf.join("\n") });
    buf = [];
  };
  while (i < lines.length) {
    const fence = /^(```|~~~)([^\n]*)$/.exec(lines[i]);
    if (fence) {
      flushMd();
      const mark = fence[1];
      const lang = (fence[2] || "").trim();
      const body = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        if (lines[i].startsWith(mark)) {
          closed = true;
          i += 1;
          break;
        }
        body.push(lines[i]);
        i += 1;
      }
      chunks.push({ type: "code", lang, text: body.join("\n"), open: !closed });
      continue;
    }
    buf.push(lines[i]);
    i += 1;
  }
  flushMd();
  return chunks;
}

function mdBlocks(raw) {
  const lines = escapeHtml(raw).split("\n");
  let html = "";
  let para = [];
  let listType = null;
  let listItems = [];
  let tableRows = [];
  let quotes = [];

  const flushPara = () => {
    if (!para.length) return;
    html += `<p>${mdInline(para.join("\n")).replace(/\n/g, "<br>")}</p>`;
    para = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html += `<${listType}>${listItems.join("")}</${listType}>`;
    listItems = [];
    listType = null;
  };
  const flushQuote = () => {
    if (!quotes.length) return;
    html += `<blockquote>${quotes.map((q) => `<p>${mdInline(q)}</p>`).join("")}</blockquote>`;
    quotes = [];
  };
  const flushTable = () => {
    if (!tableRows.length) return;
    const rows = tableRows.filter((r) => !/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+$/.test(r.replace(/^\s*\|/, "").replace(/\|\s*$/, "")));
    const cells = rows.map((r) =>
      r
        .replace(/^\s*\|/, "")
        .replace(/\|\s*$/, "")
        .split("|")
        .map((c) => c.trim())
    );
    tableRows = [];
    if (!cells.length) return;
    const head = cells[0];
    const body = cells.slice(1);
    html += "<div class=\"md-table-wrap\"><table><thead><tr>" + head.map((c) => `<th>${mdInline(c)}</th>`).join("") + "</tr></thead>";
    if (body.length) {
      html += "<tbody>" + body.map((row) => `<tr>${row.map((c) => `<td>${mdInline(c)}</td>`).join("")}</tr>`).join("") + "</tbody>";
    }
    html += "</table></div>";
  };
  const listItemHtml = (text) => {
    const task = /^\[([ xX])\]\s+([\s\S]*)$/.exec(text);
    if (task) {
      const on = task[1] !== " ";
      return `<li class="task-item"><input type="checkbox" disabled${on ? " checked" : ""}> ${mdInline(task[2])}</li>`;
    }
    return `<li>${mdInline(text)}</li>`;
  };

  for (const line of lines) {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushPara();
      flushList();
      flushTable();
      flushQuote();
      const n = Math.min(4, heading[1].length);
      html += `<h${n}>${mdInline(heading[2])}</h${n}>`;
      continue;
    }
    if (/^\s*[-*_]{3,}\s*$/.test(line)) {
      flushPara();
      flushList();
      flushTable();
      flushQuote();
      html += "<hr>";
      continue;
    }
    if (/^\s*\|.+\|\s*$/.test(line) || (/^\s*\|?\s*:?-+:?\s*\|/.test(line) && tableRows.length)) {
      flushPara();
      flushList();
      flushQuote();
      tableRows.push(line);
      continue;
    }
    if (tableRows.length) flushTable();
    const quote = /^\s*&gt;\s?(.*)$/.exec(line);
    if (quote) {
      flushPara();
      flushList();
      quotes.push(quote[1]);
      continue;
    }
    if (quotes.length) flushQuote();
    const ul = /^\s*[-*+]\s+(.+)$/.exec(line);
    if (ul) {
      flushPara();
      if (listType && listType !== "ul") flushList();
      listType = "ul";
      listItems.push(listItemHtml(ul[1]));
      continue;
    }
    const ol = /^\s*\d+\.\s+(.+)$/.exec(line);
    if (ol) {
      flushPara();
      if (listType && listType !== "ol") flushList();
      listType = "ol";
      listItems.push(listItemHtml(ol[1]));
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  flushTable();
  flushQuote();
  return html;
}

function unpackPackedMarkdownTables(text) {
  // Models often emit "| a | b | |---|---| | c | d |" as one line. TUI wraps rows.
  return String(text || "").replace(/\|\s+\|/g, "|\n|");
}

function unpackPackedMarkdownLists(text) {
  // Live flatten or a spoken dump: "- **Host**: x - **Brain**: y" on one line.
  return String(text || "").replace(
    /(^|\n)([ \t]*[-*+][ \t]+\S[^\n]*)/g,
    (full, br, itemLine) => {
      const parts = itemLine.split(/(?=\s+[-*+][ \t]+\S)/);
      if (parts.length < 2) return full;
      return br + parts.map((p) => p.trim()).filter(Boolean).join("\n");
    }
  );
}

function renderMarkdown(src, foldPrefix) {
  const text = unpackPackedMarkdownLists(unpackPackedMarkdownTables(stripAssistantPadding(src || "")));
  if (!text) return "";
  let n = 0;
  return splitMarkdown(text)
    .map((c) => {
      if (c.type === "code") {
        n += 1;
        return renderCodeBlock(c.lang, c.text, c.open, foldPrefix ? `${foldPrefix}-code-${n}` : "");
      }
      return mdBlocks(c.text);
    })
    .join("");
}

async function copyText(text) {
  const value = String(text ?? "");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* fall through */
  }
  const ta = document.createElement("textarea");
  ta.value = value;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    ta.remove();
  }
}

let chatZoom = 1;
function isPhoneLayout() {
  return window.matchMedia("(max-width: 760px)").matches;
}
function wrapChatZoom(t) {
  if (!t) return null;
  let inner = t.querySelector(":scope > .chat-zoom-inner");
  if (!inner) {
    inner = document.createElement("div");
    inner.className = "chat-zoom-inner";
    while (t.firstChild) inner.appendChild(t.firstChild);
    t.appendChild(inner);
  }
  applyChatZoom(chatZoom);
  watchTranscriptSize(t);
  return inner;
}

function watchTranscriptSize(t) {
  if (!t || typeof ResizeObserver !== "function") return;
  if (!t._stickObs) {
    t._stickObs = new ResizeObserver(() => stickTranscript());
    t._stickObs.observe(t);
  }
  const inner = t.querySelector(":scope > .chat-zoom-inner");
  if (inner && t._stickInner !== inner) {
    if (t._stickInner) {
      try { t._stickObs.unobserve(t._stickInner); } catch { /* gone */ }
    }
    t._stickInner = inner;
    t._stickObs.observe(inner);
  }
}
function applyChatZoom(next) {
  chatZoom = Math.min(2.6, Math.max(1, Number(next) || 1));
  const t = $("transcript");
  if (!t) return;
  const inner = t.querySelector(":scope > .chat-zoom-inner");
  t.classList.toggle("is-zoomed", chatZoom > 1.02);
  if (inner) {
    if (chatZoom <= 1.02) {
      inner.style.zoom = "";
      inner.style.transform = "";
      inner.style.width = "";
      chatZoom = 1;
    } else {
      inner.style.zoom = String(chatZoom);
      inner.style.transformOrigin = "0 0";
    }
  }
}
function bindChatZoom(t) {
  if (!t || t._zoomBound) return;
  t._zoomBound = true;
  let startDist = 0;
  let startZoom = 1;
  t.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length !== 2 || !isPhoneLayout()) return;
      const a = e.touches[0];
      const b = e.touches[1];
      startDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      startZoom = chatZoom;
    },
    { passive: true }
  );
  t.addEventListener(
    "touchmove",
    (e) => {
      if (e.touches.length !== 2 || !startDist || !isPhoneLayout()) return;
      e.preventDefault();
      const a = e.touches[0];
      const b = e.touches[1];
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      applyChatZoom(startZoom * (dist / startDist));
    },
    { passive: false }
  );
  t.addEventListener("touchend", () => {
    if (chatZoom < 1.08) applyChatZoom(1);
    startDist = 0;
  });
  document.addEventListener(
    "touchmove",
    (e) => {
      if (!isPhoneLayout() || e.touches.length < 2) return;
      if (e.target.closest("#transcript")) return;
      e.preventDefault();
    },
    { passive: false }
  );
  ["gesturestart", "gesturechange", "gestureend"].forEach((type) => {
    document.addEventListener(type, (e) => {
      if (isPhoneLayout()) e.preventDefault();
    });
  });
}

function bindChatTranscript() {
  const t = $("transcript");
  if (!t || t._deskBound) return;
  t._deskBound = true;
  t.addEventListener("click", async (e) => {
    const btn = e.target.closest(".code-copy");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const code = btn.closest(".code-block")?.querySelector("pre code")?.textContent || "";
    const ok = await copyText(code);
    btn.textContent = ok ? "Copied" : "Failed";
    setTimeout(() => {
      if (btn.isConnected) btn.textContent = "Copy";
    }, 1400);
  });
  t.addEventListener("scroll", () => {
    if (t._sticking) return;
    chatStickBottom = nearTranscriptBottom(t);
    updateJumpLatest();
  }, { passive: true });
  $("jump-latest")?.addEventListener("click", (e) => {
    e.preventDefault();
    jumpToLatest();
  });
  window.addEventListener("resize", () => {
    stickTranscript();
    updateJumpLatest();
  });
  bindChatZoom(t);
  wrapChatZoom(t);
  updateJumpLatest();
}

let chatStickBottom = true;

function nearTranscriptBottom(t, slop = 160) {
  if (!t) return true;
  return t.scrollHeight - t.scrollTop - t.clientHeight <= slop;
}

function transcriptPinned() {
  const t = $("transcript");
  if (!t) return true;
  const sel = window.getSelection();
  if (sel && sel.toString() && t.contains(sel.anchorNode)) return false;
  return chatStickBottom;
}

function stickTranscript(force) {
  const t = $("transcript");
  if (!t) return;
  if (!force && !chatStickBottom) return;
  chatStickBottom = true;
  t._sticking = true;
  t.scrollTop = t.scrollHeight;
  requestAnimationFrame(() => {
    t.scrollTop = t.scrollHeight;
    requestAnimationFrame(() => {
      t.scrollTop = t.scrollHeight;
      t._sticking = false;
      updateJumpLatest();
    });
  });
}

function bindTranscriptMedia(t) {
  if (!t) return;
  t.querySelectorAll("img, video").forEach((el) => {
    if (el._stickBound) return;
    el._stickBound = true;
    const go = () => stickTranscript();
    el.addEventListener("load", go);
    el.addEventListener("loadeddata", go);
  });
}

function jumpToLatest() {
  stickTranscript(true);
}

function updateJumpLatest() {
  const btn = $("jump-latest");
  const t = $("transcript");
  if (!btn || !t) return;
  const gap = t.scrollHeight - t.scrollTop - t.clientHeight;
  const overflow = t.scrollHeight - t.clientHeight > 24;
  const away = overflow && gap > 80 && !chatStickBottom;
  btn.classList.toggle("is-visible", away);
  btn.setAttribute("aria-hidden", away ? "false" : "true");
  btn.tabIndex = away ? 0 : -1;
  syncMobileComposerPad();
}

function fitPhoneFrame() {
  const phone = window.matchMedia("(max-width: 760px)").matches;
  const root = document.documentElement;
  if (!phone) {
    root.style.removeProperty("--app-h");
    root.style.removeProperty("--app-top");
    return;
  }
  const vv = window.visualViewport;
  const h = Math.max(320, Math.round(vv?.height || window.innerHeight || 0));
  const top = Math.max(0, Math.round(vv?.offsetTop || 0));
  root.style.setProperty("--app-h", `${h}px`);
  root.style.setProperty("--app-top", `${top}px`);
}
window.fitPhoneFrame = fitPhoneFrame;

function autosizeComposer() {
  const ta = $("message");
  if (!ta) return;
  const phone = window.matchMedia("(max-width: 760px)").matches;
  const cap = phone ? 120 : 140;
  const min = phone ? 44 : 36;
  ta.style.height = "auto";
  ta.style.height = `${Math.max(min, Math.min(cap, ta.scrollHeight))}px`;
}

function syncMobileComposerPad() {
  fitPhoneFrame();
  const bodyEl = document.querySelector(".chat-body");
  if (bodyEl) bodyEl.style.paddingBottom = "";
}

function localEngineReady(b) {
  if (!b) return true;
  const list = modelsForBot(b);
  const m = list.find((x) => x.id === b.model);
  if (!m || !isLocalGpuModel(m)) return true;
  return isLocalRunning(m, list);
}

function isWorking(id) {
  id = id || state.selected;
  if (!id) return false;
  if (state.stopped[id]) return false;
  if (state.working[id]) return true;
  const b = state.bots.find((x) => x.id === id);
  if (!b) return false;
  const st = String(b.status || "");
  if (/Thinking|Working|Speaking|Using |Starting /i.test(st)) return true;
  return (b.messages || []).some((m) => m.open);
}

function updateSendButton() {
  const btn = $("send");
  if (!btn) return;
  const busy = isWorking() || (voiceChat.on && isTtsPlaying());
  const b = state.bots.find((x) => x.id === state.selected);
  const ready = localEngineReady(b);
  const hasDraft = !!(($("message")?.value || "").trim() || state.pendingImages.length);
  btn.classList.toggle("is-stop", busy);
  btn.disabled = !state.selected;
  if (busy) {
    btn.innerHTML = '<span class="stop-icon"></span>';
    btn.setAttribute("aria-label", "Stop");
    btn.title = hasDraft ? "Stop generating — send after this turn finishes" : "Stop generating";
  } else if (!ready) {
    btn.textContent = "➤";
    btn.setAttribute("aria-label", "Send");
    btn.title = "Local model will start, then this message sends";
  } else {
    btn.textContent = "➤";
    btn.setAttribute("aria-label", "Send");
    btn.title = "Send";
  }
  const mic = $("mic");
  if (mic) {
    mic.disabled = !state.selected;
    paintMic();
  }
}

function setWorking(id, on) {
  if (!id) return;
  if (on) state.working[id] = true;
  else delete state.working[id];
  if (id === state.selected) {
    updateSendButton();
    syncChatStatusLine(state.bots.find((x) => x.id === id));
  }
  if (voiceChat.on) paintMic();
  if (!on && id === state.selected) flushVoiceSend();
}

async function stopAgent() {
  const id = state.selected;
  if (!id || !isWorking(id)) return;
  state.stopped[id] = true;
  try {
    await api(`/v1/agent/${id}/cancel`, { method: "POST", body: "{}" });
  } catch {
    /* still settle the UI */
  }
  const b = state.bots.find((x) => x.id === id);
  if (b) {
    b.status = "Ready";
    for (const m of b.messages || []) {
      if (m.open) m.open = false;
    }
    clearHorizon(b);
    renderConversation(b);
  }
  setWorking(id, false);
  document.querySelector(".tps-stat")?.classList.remove("generating");
}

function formatWorked(ms) {
  const n = Number(ms) || 0;
  if (n < 1000) return `${Math.max(1, Math.round(n))}ms`;
  if (n < 60000) {
    const s = n / 1000;
    return s >= 10 ? `${Math.round(s)}s` : `${s.toFixed(1).replace(/\.0$/, "")}s`;
  }
  const m = Math.floor(n / 60000);
  const s = Math.round((n % 60000) / 1000);
  return `${m}m ${s}s`;
}

function thoughtDuration(m) {
  const t0 = Number(m.t0) || 0;
  const t1 = Number(m.t1) || 0;
  if (t0 && t1 && t1 >= t0) return formatWorked((t1 - t0) * 1000);
  if (m.open && t0) return formatWorked((Date.now() / 1000 - t0) * 1000);
  return "";
}

const TOOL_OUTPUT_MAX = 100000;

function flattenToolText(value, depth) {
  if (value == null || (depth || 0) > 8) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map((v) => flattenToolText(v, (depth || 0) + 1)).filter(Boolean).join("\n");
  }
  if (typeof value === "object") {
    for (const key of ["output_for_prompt", "OkayOutput", "text", "stdout", "stderr", "output"]) {
      if (key in value) {
        const got = flattenToolText(value[key], (depth || 0) + 1);
        if (got) return got;
      }
    }
    if ("content" in value) {
      const got = flattenToolText(value.content, (depth || 0) + 1);
      if (got) return got;
    }
    try {
      const dumped = JSON.stringify(value, null, 2);
      return dumped && dumped !== "{}" && dumped !== "[]" && dumped !== "null" ? dumped : "";
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function toolOutputFromUpdate(u) {
  const contentText = flattenToolText(u?.content);
  const rawText = flattenToolText(u?.rawOutput);
  let text = contentText || rawText || "";
  if (contentText && rawText && !rawText.includes(contentText) && !contentText.includes(rawText)) {
    text = `${contentText}\n${rawText}`;
  }
  text = String(text || "").trim();
  if (text.length > TOOL_OUTPUT_MAX) return `${text.slice(0, TOOL_OUTPUT_MAX)}\n…`;
  return text;
}

function toolCommandFromUpdate(u) {
  const meta = u?._meta?.["x.ai/tool"] || {};
  const bags = [meta.input, u?.rawInput, u?.input].filter((b) => b && typeof b === "object");
  for (const bag of bags) {
    for (const key of ["command", "path", "file", "pattern", "query", "url", "glob"]) {
      if (bag[key]) return String(bag[key]);
    }
  }
  const loc = (u?.locations || [])[0];
  return loc?.path ? String(loc.path) : "";
}

function todosFromUpdate(u) {
  const meta = u?._meta?.["x.ai/tool"] || {};
  const bags = [meta.input, u?.rawInput, u?.input].filter((b) => b && typeof b === "object");
  for (const bag of bags) {
    if (!Array.isArray(bag.todos)) continue;
    const rows = bag.todos
      .map((t) => ({
        id: String(t?.id || ""),
        content: String(t?.content || "").trim(),
        status: String(t?.status || "pending").toLowerCase().replace(/[\s-]+/g, "_"),
      }))
      .filter((t) => t.content);
    if (rows.length) return rows;
  }
  return [];
}

const WORKFLOW_RUN_LIMIT = 24;

function trackWorkflowRun(b, found, meta, u) {
  const inp = (meta?.input && typeof meta.input === "object" && meta.input) || u?.rawInput || u?.input || {};
  const src = inp?.source;
  let runName = "";
  if (src && typeof src === "object") {
    runName = String(src.name || "").trim();
    if (!runName && src.script_path) runName = String(src.script_path).split("/").pop().replace(/\.rhai$/i, "");
  } else if (typeof src === "string") {
    runName = src.trim();
  }
  if (!runName) {
    runName = String(found.title || found.detail || "workflow").replace(/^workflow\s*/i, "").trim() || "workflow";
  }
  const phase =
    { pending: "running", in_progress: "running", running: "running", completed: "completed", failed: "failed", cancelled: "cancelled" }[found.status] ||
    "running";
  b.workflow_runs = b.workflow_runs || [];
  let run = b.workflow_runs.find((r) => r.name === runName);
  if (!run) {
    run = { name: runName, hint: String(inp?.description || meta?.label || "Session run"), phase };
    b.workflow_runs.push(run);
    if (b.workflow_runs.length > WORKFLOW_RUN_LIMIT) b.workflow_runs.shift();
  } else {
    run.phase = phase;
  }
}

function toolLabel(m) {
  const kind = String(m.kind || "").toLowerCase();
  const name = String(m.name || m.title || m.text || "").toLowerCase();
  const detail = String(m.detail || m.command || m.text || "");
  if (kind === "execute" || /terminal|bash|shell|command|run_terminal/.test(name)) return "Bash";
  if (kind === "read" || name.includes("read_file") || name === "read") return "Read";
  if (kind === "edit" || /write|edit|search_replace/.test(name)) return "Edit";
  if (kind === "search" || /grep/.test(name)) return "Grep";
  if (/search_tool/.test(name)) return "Search";
  if (/web_search/.test(name)) return "Web Search";
  if (/glob|list_dir/.test(name)) return "Glob";
  if (/spawn_subagent/.test(name)) return "Subagent";
  if (/kill_command_or_subagent/.test(name)) return "Stop Task";
  if (name === "workflow") return "Workflow";
  if (/use_tool/.test(name)) {
    const inner = detail.replace(/^bot_desktop__/, "").replace(/^mcp__/, "");
    if (inner && inner !== "use_tool") return inner.replace(/_/g, " ");
    return "Use Tool";
  }
  if (m.title && m.title !== "Tool" && m.title !== "Run Command" && m.title !== "Use Tool") return m.title;
  return m.text || m.kind || "Tool";
}

function toolDetail(m) {
  const cmd = String(m.command || "").trim();
  if (cmd) return cmd.replace(/^Execute\s+`?/, "").replace(/`$/, "");
  return m.detail || m.text || "";
}

function prettyToolOutput(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  if (raw.startsWith("{") || raw.startsWith("[")) {
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        if (obj.spoken) {
          const extra = Array.isArray(obj.issues) && obj.issues.length
            ? "\n" + obj.issues.map((x) => String(x)).join("\n")
            : "";
          return String(obj.spoken) + extra;
        }
        if (typeof obj.stdout === "string" && obj.stdout) return obj.stdout;
        if (typeof obj.output === "string" && obj.output) return obj.output;
        return JSON.stringify(obj, null, 2);
      }
    } catch {
      /* keep raw */
    }
  }
  return raw;
}

function captureFolds(host) {
  const map = {};
  host.querySelectorAll("details[data-fold]").forEach((el) => {
    map[el.dataset.fold] = el.open;
  });
  return map;
}

function restoreFolds(host, map) {
  host.querySelectorAll("details[data-fold]").forEach((el) => {
    if (Object.prototype.hasOwnProperty.call(map, el.dataset.fold)) {
      el.open = map[el.dataset.fold];
    }
  });
}

function bindFold(el, m) {
  el.open = !!m.expanded;
  el.addEventListener("toggle", () => {
    m.expanded = el.open;
  });
}

function renderThoughtBlock(m, idx) {
  const running = !!m.open;
  const dur = thoughtDuration(m);
  const el = document.createElement("details");
  el.className = "gb-block gb-thought" + (running ? " is-running" : "");
  el.dataset.fold = `thought-${idx}`;
  el.innerHTML = `<summary><span class="gb-accent"></span><span class="gb-label">${running ? "Thinking" : "Thought"}</span>${dur ? `<span class="gb-time">${escapeHtml(dur)}</span>` : ""}</summary>
    <div class="gb-block-body">${escapeHtml(m.text || "")}</div>`;
  bindFold(el, m);
  if (running && m.expanded !== false) el.open = true;
  return el;
}

const TODO_MARKS = { completed: "✓", done: "✓", in_progress: "▶", pending: "☐", cancelled: "✕" };

function renderTodoBlock(m, idx) {
  const rows = m.todos || [];
  const done = rows.filter((t) => t.status === "completed" || t.status === "done").length;
  const status = m.status || (m.open ? "running" : "completed");
  const running = status === "running" || (m.open && status !== "completed" && status !== "cancelled");
  const el = document.createElement("details");
  el.className = "gb-block gb-todo" + (running ? " is-running" : "");
  el.dataset.fold = `todo-${idx}`;
  el.innerHTML = `<summary><span class="gb-accent"></span><span class="gb-label">Tasks</span><span class="gb-sub">${done}/${rows.length} done</span></summary>
    <div class="gb-block-body">${rows.map((t) => `<div class="todo-row st-${escapeHtml(t.status)}"><span class="todo-mark">${TODO_MARKS[t.status] || "☐"}</span><span class="todo-text">${escapeHtml(t.content)}</span></div>`).join("")}</div>`;
  bindFold(el, m);
  if (running && m.expanded !== false) el.open = true;
  return el;
}

function renderToolBlock(m, idx) {
  if (m.isTodos && (m.todos || []).length) return renderTodoBlock(m, idx);
  const status = m.status || (m.open ? "running" : "completed");
  const running = status === "running" || (m.open && status !== "completed" && status !== "cancelled");
  const failed = status === "failed";
  const label = toolLabel(m);
  const detail = toolDetail(m);
  const el = document.createElement("details");
  el.className = "gb-block gb-tool" + (running ? " is-running" : "") + (failed ? " is-failed" : "");
  el.dataset.fold = `tool-${m.id || idx}`;
  const cmd = m.command ? `<pre class="gb-cmd">${escapeHtml(toolDetail(m))}</pre>` : "";
  const pretty = prettyToolOutput(m.output);
  const out = pretty ? `<pre class="gb-out">${escapeHtml(pretty)}</pre>` : "";
  el.innerHTML = `<summary><span class="gb-accent"></span><span class="gb-label">${escapeHtml(label)}</span><span class="gb-sub">${escapeHtml(detail)}</span></summary>
    <div class="gb-block-body">${cmd}${out}</div>`;
  bindFold(el, m);
  return el;
}

function renderWorked(ms) {
  const el = document.createElement("div");
  el.className = "gb-worked";
  el.textContent = `Worked for ${formatWorked(ms)}`;
  return el;
}

function renderProgressLine(m) {
  const el = document.createElement("div");
  el.className = "gb-progress";
  el.setAttribute("aria-live", "polite");
  el.textContent = String(m.text || "");
  return el;
}

// Live in-chat status line: mirrors the header status (Working…, Thinking…,
// Waiting for you…) at the bottom of the conversation with an elapsed timer,
// so long local-brain turns are visible where the user is looking.
let _statusLineTimer = 0;
let _statusLineSince = 0;  // ms when the current busy period started
let _statusLineBot = "";   // bot that owns the current timer

function syncChatStatusLine(b) {
  const t = $("transcript");
  if (!t) return;
  const el = t.querySelector(":scope > .chat-status-line");
  const status = String(b?.status || "").trim();
  const busy = !!b && !!status && !/^ready$/i.test(status) &&
    (isWorking(b.id) || /waiting for you|error:/i.test(status));
  const botChanged = busy && b.id !== _statusLineBot;
  if (!busy) {
    if (el) el.remove();
    if (_statusLineTimer) {
      clearInterval(_statusLineTimer);
      _statusLineTimer = 0;
    }
    _statusLineSince = 0;
    _statusLineBot = "";
    return;
  }
  // Start the clock for a new busy period (first sight, or a different bot).
  // Re-renders of the same busy period keep the same start so the seconds
  // keep counting instead of snapping back to 1s on each stream chunk.
  if (_statusLineSince === 0 || botChanged) _statusLineSince = Date.now();
  _statusLineBot = b.id;
  if (!el) {
    el = document.createElement("div");
    el.className = "chat-status-line";
    el.setAttribute("aria-live", "polite");
    t.appendChild(el);
    if (!_statusLineTimer) {
      _statusLineTimer = setInterval(() => {
        const node = document.querySelector("#transcript .chat-status-line");
        const b = state.bots.find((x) => x.id === state.selected);
        if (!node || !b || !b.status || b.id !== _statusLineBot) return;
        const base = node.dataset.base || b.status;
        const secs = Math.max(1, Math.round((Date.now() - _statusLineSince) / 1000));
        node.textContent = base ? `${base}  ·  ${secs}s` : `${secs}s`;
        if (transcriptPinned()) stickTranscript();
      }, 1000);
    }
  }
  el.dataset.base = status;
  const secs = Math.max(1, Math.round((Date.now() - _statusLineSince) / 1000));
  el.textContent = status ? `${status}  ·  ${secs}s` : `${secs}s`;
  if (transcriptPinned()) stickTranscript();
}

function lastProgressIndex(b) {
  const ms = b.messages || [];
  for (let i = ms.length - 1; i >= 0; i--) if (ms[i].role === "progress") return i;
  return -1;
}

function applySessionTurn(b, u) {
  const kind = u.sessionUpdate;
  const text = u.content?.text || "";
  b.messages = b.messages || [];
  if (kind === "agent_progress") {
    const pi = lastProgressIndex(b);
    if (!text) {
      if (pi >= 0) b.messages.splice(pi, 1);
      return true;
    }
    if (pi >= 0) b.messages[pi].text = text;
    else b.messages.push({ role: "progress", text, ts: Date.now() / 1000 });
    return true;
  }
  if (kind === "agent_thought_chunk" && text) {
    const last = b.messages[b.messages.length - 1];
    if (last && last.role === "thought" && last.open) {
      last.text = mergeAssistantStream(last.text || "", text);
      last.t1 = Date.now() / 1000;
    } else {
      if (last && last.open) last.open = false;
      const now = Date.now() / 1000;
      b.messages.push({ role: "thought", text, open: true, ts: now, t0: now, t1: now });
    }
    window.DeskUI?.setEmotion(b.id, "thinking", { persist: false, pop: false });
    return true;
  }
  if (kind === "tool_call" || kind === "tool_call_update") {
    const tid = u.toolCallId || "";
    const meta = u._meta?.["x.ai/tool"] || {};
    const inp = meta.input || {};
    const command = toolCommandFromUpdate(u);
    const name = meta.name || u.title || "";
    let found = tid ? b.messages.find((m) => m.role === "tool" && m.id === tid) : null;
    if (!found) {
      const last = b.messages[b.messages.length - 1];
      if (last && last.open) last.open = false;
      found = {
        role: "tool",
        id: tid || `tool-${Date.now()}`,
        title: meta.label || u.title || "Tool",
        text: u.title || meta.label || "Tool",
        name,
        detail: u.title || inp.description || name || "",
        command,
        kind: u.kind || meta.kind || "",
        status: u.status || "running",
        ts: Date.now() / 1000,
        t0: Date.now() / 1000,
        open: true,
      };
      b.messages.push(found);
    } else {
      if (u.title) found.detail = u.title;
      if (command) found.command = command;
      if (meta.label) found.title = meta.label;
      if (name) found.name = name;
      if (u.kind) found.kind = u.kind;
      if (u.status) found.status = u.status;
      found.t1 = Date.now() / 1000;
    }
    const output = toolOutputFromUpdate(u);
    if (output) found.output = output;
    const todos = todosFromUpdate(u);
    if (todos.length) {
      found.todos = todos;
      found.isTodos = true;
    }
    if (name === "workflow") trackWorkflowRun(b, found, meta, u);
    if (["completed", "cancelled"].includes(found.status)) found.open = false;
    else if (found.status === "failed") found.open = true;
    window.DeskUI?.setEmotion(b.id, "working", { persist: false, pop: true });
    return true;
  }
  if (kind === "plan" || kind === "plan_update") {
    const entries = planEntriesFromUpdate(u);
    if (!entries.length) {
      clearHorizon(b);
      return true;
    }
    const prev = b.plan?.entries || [];
    const same = prev.length === entries.length && prev.every((e, i) => e.content === entries[i].content);
    b.plan = { entries, expanded: same ? Boolean(b.plan?.expanded) : false };
    renderHorizon(b);
    return true;
  }
  if (kind === "turn_completed" || kind === "response_completed") {
    const elapsed = u.elapsed_ms ?? u.usage?.apiDurationMs;
    for (let i = b.messages.length - 1; i >= 0; i--) {
      if (b.messages[i].role === "progress") b.messages.splice(i, 1);
    }
    const last = b.messages[b.messages.length - 1];
    if (text && last && last.role === "assistant") {
      last.text = collapseRestartedAssistant(text);
    }
    if (last && last.open) last.open = false;
    if (elapsed != null && elapsed > 0) {
      for (let i = b.messages.length - 1; i >= 0; i--) {
        if (["assistant", "tool", "thought"].includes(b.messages[i].role)) {
          b.messages[i].elapsed_ms = elapsed;
          break;
        }
      }
    }
    document.querySelector(".tps-stat")?.classList.remove("generating");
    window.DeskUI?.settleEmotion(b.id);
    setWorking(b.id, false);
    if (horizonAllDone(b)) scheduleHorizonHide(b);
    return true;
  }
  return false;
}

function planEntriesFromUpdate(u) {
  const bags = [u?.entries, u?.plan?.entries, u?.plan?.items, u?.items, Array.isArray(u?.plan) ? u.plan : null];
  let raw = [];
  for (const bag of bags) {
    if (Array.isArray(bag) && bag.length && typeof bag[0] === "object") {
      raw = bag;
      break;
    }
  }
  return raw.map((e) => ({
    content: String(e.content || e.title || e.text || "").trim(),
    status: String(e.status || "pending").toLowerCase().replace("-", "_"),
  })).filter((e) => e.content);
}

function horizonAllDone(b) {
  const entries = b?.plan?.entries || [];
  return entries.length > 0 && entries.every((e) => e.status === "completed" || e.status === "done");
}

function clearHorizon(b) {
  if (b) b.plan = null;
  const bar = $("horizon-bar");
  if (bar) bar.hidden = true;
}

function scheduleHorizonHide(b) {
  if (!b) return;
  clearTimeout(b._horizonHide);
  b._horizonHide = setTimeout(() => {
    if (b.plan) b.plan = null;
    if (state.selected === b.id) renderHorizon(b);
  }, 1200);
}

function renderHorizon(b) {
  const bar = $("horizon-bar");
  const list = $("horizon-list");
  const summary = $("horizon-summary");
  const progress = $("horizon-progress");
  const toggle = $("horizon-toggle");
  if (!bar || !list) return;
  if (!b || b.id !== state.selected) {
    if (!state.selected) bar.hidden = true;
    return;
  }
  const entries = b.plan?.entries || [];
  if (!entries.length) {
    bar.hidden = true;
    return;
  }
  const done = entries.filter((e) => e.status === "completed" || e.status === "done").length;
  const current = entries.find((e) => e.status === "in_progress" || e.status === "inprogress") || entries.find((e) => e.status !== "completed" && e.status !== "done") || entries[entries.length - 1];
  bar.hidden = false;
  bar.classList.toggle("is-open", Boolean(b.plan?.expanded));
  if (toggle) toggle.setAttribute("aria-expanded", b.plan?.expanded ? "true" : "false");
  if (summary) summary.textContent = current?.content || "Long horizon job";
  if (progress) progress.textContent = `${done}/${entries.length}`;
  list.hidden = !b.plan?.expanded;
  list.innerHTML = "";
  for (const e of entries) {
    const li = document.createElement("li");
    const st = e.status === "completed" || e.status === "done" ? "done" : (e.status === "in_progress" || e.status === "inprogress" ? "progress" : "pending");
    li.className = `horizon-item is-${st}`;
    li.innerHTML = `<span class="horizon-mark" aria-hidden="true"></span><span>${escapeHtml(e.content)}</span>`;
    list.appendChild(li);
  }
  if (horizonAllDone(b)) scheduleHorizonHide(b);
}

function wireHorizon() {
  const toggle = $("horizon-toggle");
  if (!toggle || toggle.dataset.wired === "true") return;
  toggle.dataset.wired = "true";
  toggle.addEventListener("click", () => {
    const b = state.bots.find((x) => x.id === state.selected);
    if (!b?.plan) return;
    b.plan.expanded = !b.plan.expanded;
    renderHorizon(b);
  });
}

function formatChatWhen(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) {
    return `Yesterday • ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString([], sameYear
    ? { month: "short", day: "numeric" }
    : { month: "short", day: "numeric", year: "numeric" });
}

function timelineGroupFor(iso) {
  const date = new Date(iso || Date.now());
  if (Number.isNaN(date.getTime())) return "Older";
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diff = Math.round((startToday - startDate) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return "This week";
  if (diff < 30) return "Earlier this month";
  return "Older";
}

function activeChatRecord(b) {
  const chats = state.timeline?.chats || [];
  const activeId = b?.chat_id || b?.chat?.id || state.timeline?.activeId;
  return chats.find((c) => c.id === activeId) || b?.chat || chats[0] || null;
}

function updateActiveChatMeta(b) {
  const title = $("active-chat-title");
  const when = $("active-chat-when");
  const newBtn = $("new-chat-btn");
  if (newBtn) newBtn.disabled = !state.selected;
  if (!b) {
    if (title) title.textContent = "Current chat";
    if (when) when.textContent = "Select a bot to see its chats";
    return;
  }
  const session = activeChatRecord(b);
  const count = session?.messageCount ?? (b.messages || []).length;
  if (title) title.textContent = session?.title || "Current chat";
  if (when) {
    const stamp = formatChatWhen(session?.updatedAt) || "Now";
    when.textContent = `${stamp} • ${count} message${count === 1 ? "" : "s"}`;
  }
}

function setTimelineOpen(open) {
  state.timelineOpen = !!open;
  $("conversation")?.classList.toggle("timeline-open", state.timelineOpen);
  $("timeline-toggle")?.classList.toggle("active", state.timelineOpen);
}

function toggleTimeline() {
  const next = !state.timelineOpen;
  setTimelineOpen(next);
  if (next) {
    refreshTimeline();
    setTimeout(() => $("timeline-search")?.focus(), 50);
  }
}

function renderChatTimeline() {
  const list = $("timeline-list");
  if (!list) return;
  const b = state.bots.find((x) => x.id === state.selected);
  updateActiveChatMeta(b);
  if (!state.selected) {
    list.innerHTML = '<div class="timeline-empty">Select a bot to browse its chats.</div>';
    return;
  }
  const chats = [...(state.timeline?.chats || [])];
  const activeId = state.timeline?.activeId || b?.chat_id;
  const query = (state.timelineQuery || "").trim();
  if (!chats.length) {
    list.innerHTML = query
      ? '<div class="timeline-empty">No older chats match that search.</div>'
      : '<div class="timeline-empty">No chats yet. Send a message or start a new one.</div>';
    return;
  }
  list.innerHTML = "";
  let lastGroup = "";
  for (const session of chats) {
    const group = timelineGroupFor(session.updatedAt || session.createdAt);
    if (group !== lastGroup) {
      const label = document.createElement("div");
      label.className = "timeline-group-label";
      label.textContent = group;
      list.appendChild(label);
      lastGroup = group;
    }
    const item = document.createElement("div");
    item.className = "timeline-item" + (session.id === activeId ? " active" : "");
    item.dataset.chatId = session.id;
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    const preview = session.match || session.preview || "Empty chat";
    item.innerHTML = `
      <span class="timeline-dot"></span>
      <span>
        <span class="timeline-item-title">${escapeHtml(session.title || "Untitled chat")}</span>
        <span class="timeline-item-preview">${escapeHtml(preview)}</span>
        <span class="timeline-item-when">${escapeHtml(formatChatWhen(session.updatedAt || session.createdAt))}</span>
      </span>
      <span class="timeline-item-actions">
        <button type="button" data-timeline-delete title="Delete chat">×</button>
      </span>`;
    item.addEventListener("click", (e) => {
      if (e.target.closest("[data-timeline-delete]")) return;
      openChatSession(session.id);
    });
    item.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("[data-timeline-delete]")) return;
      e.preventDefault();
      openChatSession(session.id);
    });
    item.querySelector("[data-timeline-delete]")?.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      deleteChatSession(session.id);
    });
    list.appendChild(item);
  }
}

function applyTimelinePayload(payload, bot) {
  if (!payload) return;
  if (payload.chats || payload.activeId) {
    state.timeline = {
      activeId: payload.activeId || payload.timeline?.activeId || null,
      chats: payload.chats || payload.timeline?.chats || [],
    };
  }
  if (payload.timeline) {
    state.timeline = {
      activeId: payload.timeline.activeId || null,
      chats: payload.timeline.chats || [],
    };
  }
  const nextBot = payload.bot || bot;
  if (nextBot?.id) {
    const i = state.bots.findIndex((x) => x.id === nextBot.id);
    if (i >= 0) state.bots[i] = { ...state.bots[i], ...nextBot };
    else state.bots.push(nextBot);
    if (state.selected === nextBot.id) renderConversation(i >= 0 ? state.bots[i] : nextBot);
  }
  renderChatTimeline();
}

async function refreshTimeline() {
  if (!state.selected) {
    state.timeline = { activeId: null, chats: [] };
    renderChatTimeline();
    return;
  }
  const q = (state.timelineQuery || "").trim();
  const path = `/v1/bots/${state.selected}/chats${q ? `?q=${encodeURIComponent(q)}` : ""}`;
  try {
    const j = await api(path);
    state.timeline = { activeId: j.activeId || null, chats: j.chats || [] };
    const b = state.bots.find((x) => x.id === state.selected);
    if (b) {
      b.chat_id = j.activeId || b.chat_id;
      const active = (j.chats || []).find((c) => c.id === j.activeId);
      if (active) b.chat = active;
    }
    renderChatTimeline();
  } catch {
    renderChatTimeline();
  }
}

async function startNewChat() {
  if (!state.selected) return;
  try {
    const j = await api(`/v1/bots/${state.selected}/chats`, { method: "POST", body: "{}" });
    applyTimelinePayload(j);
    $("message")?.focus();
  } catch (err) {
    alert(err.message || err);
  }
}

async function openChatSession(chatId) {
  if (!state.selected || !chatId) return;
  if (chatId === state.timeline?.activeId) {
    if (window.matchMedia("(max-width: 900px)").matches) setTimelineOpen(false);
    return;
  }
  try {
    const j = await api(`/v1/bots/${state.selected}/chats/${encodeURIComponent(chatId)}/open`, {
      method: "POST",
      body: "{}",
    });
    applyTimelinePayload(j);
    if (window.matchMedia("(max-width: 900px)").matches) setTimelineOpen(false);
  } catch (err) {
    alert(err.message || err);
  }
}

async function deleteChatSession(chatId) {
  if (!state.selected || !chatId) return;
  const session = (state.timeline?.chats || []).find((c) => c.id === chatId);
  const label = session?.title || "this chat";
  if (!confirm(`Delete “${label}”? This removes it from the timeline.`)) return;
  try {
    const j = await fetch(`/v1/bots/${state.selected}/chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
      headers: headers(),
    });
    const body = await j.json().catch(() => ({}));
    if (!j.ok) throw new Error(body.error || j.statusText);
    applyTimelinePayload(body);
  } catch (err) {
    alert(err.message || err);
  }
}

function bindTimeline() {
  $("timeline-toggle")?.addEventListener("click", toggleTimeline);
  $("close-timeline-panel")?.addEventListener("click", () => setTimelineOpen(false));
  $("new-chat-btn")?.addEventListener("click", startNewChat);
  let searchTimer = 0;
  $("timeline-search")?.addEventListener("input", (e) => {
    state.timelineQuery = e.target.value || "";
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refreshTimeline(), 80);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.timelineOpen) setTimelineOpen(false);
  });
}

function fileUrl(workspaceId, rel) {
  const id = workspaceId || state.workspaceId || "";
  return `/v1/workspaces/${id}/raw?file=${encodeURIComponent(rel)}`;
}

function openChatLightbox(src, title) {
  hideDeskMenu();
  $("chat-lightbox")?.remove();
  const wrap = document.createElement("div");
  wrap.id = "chat-lightbox";
  wrap.className = "chat-lightbox";
  const isVid = /\.(mp4|webm|mov|m4v)(\?|$)/i.test(src) || /video\//i.test(title || "");
  wrap.innerHTML = `<button type="button" class="chat-lightbox-close" aria-label="Close">✕</button>
    <div class="chat-lightbox-stage">${isVid ? "<video controls autoplay></video>" : "<img alt=\"\">"}
    <div class="chat-lightbox-cap"></div></div>`;
  const media = wrap.querySelector("img, video");
  media.src = src;
  wrap.querySelector(".chat-lightbox-cap").textContent = title || "";
  const close = () => wrap.remove();
  wrap.querySelector(".chat-lightbox-close").onclick = close;
  wrap.addEventListener("click", (e) => {
    if (e.target === wrap) close();
  });
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") {
      document.removeEventListener("keydown", esc);
      close();
    }
  });
  document.body.appendChild(wrap);
}

function iconFor(file) {
  if (file.dir) return "📁";
  if (file.kind === "image") return "🖼️";
  if (file.kind === "video") return "🎬";
  const ext = (file.path.split(".").pop() || "").toLowerCase();
  if (["mp4", "webm", "mov", "avi", "mkv", "m4v"].includes(ext)) return "🎬";
  if (["md", "txt"].includes(ext)) return "📝";
  if (["html", "htm"].includes(ext)) return "🌐";
  if (["py", "rs", "js", "ts"].includes(ext)) return "💻";
  if (ext === "toml" || ext === "json") return "⚙️";
  return "📄";
}

function inTrashPath(path) {
  const p = String(path || "");
  return p === "Trash" || p.startsWith("Trash/");
}

function hideDeskMenu() {
  $("desk-context-menu")?.remove();
}

function showDeskMenu(x, y, items) {
  hideDeskMenu();
  const menu = document.createElement("div");
  menu.id = "desk-context-menu";
  menu.className = "desk-context-menu";
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = item.label;
    if (item.danger) btn.classList.add("danger");
    btn.onclick = async () => {
      hideDeskMenu();
      try {
        await item.run();
      } catch (err) {
        alert(err.message || err);
      }
    };
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);
  const pad = 8;
  const w = menu.offsetWidth || 160;
  const h = menu.offsetHeight || 40;
  menu.style.left = `${Math.min(x, window.innerWidth - w - pad)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - h - pad)}px`;
}
document.addEventListener("click", hideDeskMenu);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideDeskMenu();
});

function nodeChipHTML(b) {
  // Host (teela-brain / teela-body) already lives in the chat header subtitle.
  return "";
}

function peerPlaceholderBots() {
  const peers = state.peers || [];
  const seen = new Set((state.bots || []).filter((b) => b.remote && b.node).map((b) => b.node));
  return peers
    .filter((p) => p && p.name && !seen.has(p.name))
    .map((p) => {
      const st = p.status || "offline";
      return {
        id: `peer:${p.name}`,
        name: p.name,
        status: st === "ok" ? "no bots yet" : st,
        node: p.name,
        remote: true,
        node_status: st,
        peer_stub: true,
        kind: "peer",
        avatar: { kind: "emoji", value: "◌", color: "#9ca3af", shape: "" },
      };
    });
}

function listedBots() {
  return [...(state.bots || []), ...peerPlaceholderBots()];
}

function mergeBot(prev, incoming) {
  const next = { ...(prev || {}), ...incoming };
  if (!Object.prototype.hasOwnProperty.call(incoming || {}, "messages") && prev?.messages) {
    next.messages = prev.messages;
  }
  return next;
}

function renderRoster() {
  const q = ($("search")?.value || "").trim().toLowerCase();
  const host = $("roster");
  if (!host) return;
  host.innerHTML = "";
  for (const b of listedBots()) {
    const hay = `${b.avatar?.value || ""} ${b.name} ${b.status} ${hostLabel(b)}`.toLowerCase();
    if (q && !hay.includes(q)) continue;
    const row = document.createElement("div");
    const stub = !!b.peer_stub;
    const offline = !!(b.node_status && b.node_status !== "ok");
    row.className = "agent-row" + (state.selected === b.id ? " active" : "") + (stub ? " peer-stub" : "") + (offline ? " is-offline" : "");
    row.dataset.agentId = b.id;
    row.title = stub ? `${b.name} · ${b.status || "offline"}` : b.name;
    const av = window.DeskUI ? DeskUI.avatarHTML(b) : `<div class="avatar">${escapeHtml(b.avatar?.value || "◉")}</div>`;
    const chip = nodeChipHTML(b);
    row.innerHTML = `${av}<div class="agent-main"><div class="agent-name">${escapeHtml(b.name)}${chip}</div><div class="agent-preview">${escapeHtml(b.status || "Ready")}</div></div>`;
    if (!stub) row.onclick = () => selectBot(b.id);
    host.appendChild(row);
  }
  const hint = $("peer-hint");
  if (hint) {
    const peers = state.peers || [];
    const auth = peers.filter((p) => p.status === "auth");
    const inbound = peers.filter((p) => p.inbound_seen && p.status !== "ok" && p.status !== "auth");
    hint.classList.toggle("is-offline", !!(auth.length || inbound.length));
    if (auth.length) {
      const who = auth.map((p) => p.name).join(", ");
      hint.hidden = false;
      hint.textContent =
        `${who} rejected the cluster token (auth). This computer and ${who} do not have the same secret. Open Cluster step 2 on both: fingerprints must match. On body, paste brain’s token again and click Save token.`;
    } else if (inbound.length) {
      hint.hidden = false;
      hint.textContent = inbound
        .map(
          (p) =>
            `${p.name} can reach this desk, but ${p.url} is closed from here. On that computer set Hermes Desk address to its LAN IP (not 127.0.0.1) and allow port 8742 in.`
        )
        .join(" ");
    } else {
      hint.hidden = true;
      hint.textContent = "";
    }
  }
  if (window.DeskUI) {
    const listed = listedBots();
    DeskUI.renderRail(listed, state.selected, selectBot);
    DeskUI.renderMobile(listed, selectBot);
  }
}

async function refreshBots() {
  const j = await api("/v1/bots");
  const incoming = j.bots || [];
  const prevById = Object.fromEntries((state.bots || []).map((b) => [b.id, b]));
  state.bots = incoming.map((b) => mergeBot(prevById[b.id], b));
  state.teelaBrainTaken = j.teela_brain_taken === true;
  if (j.node) state.nodeName = j.node;
  if (j.cluster_token_fp) state.clusterTokenFp = j.cluster_token_fp;
  state.peers = j.peers || [];
  renderRoster();
  if (state.selected) {
    const b = state.bots.find((x) => x.id === state.selected);
    if (b) renderConversation(b);
  }
  if (window.DeskUI?.hydrateLocalAvatars) {
    window.DeskUI.hydrateLocalAvatars(state.bots).catch(() => {});
  }
}

function hostLabel(b) {
  return (b?.node || state.nodeName || "").trim();
}

function conversationSubtitle(b) {
  const parts = [];
  const host = hostLabel(b);
  if (host) parts.push(host);
  if (b.status) parts.push(b.status);
  return parts.join(" · ") || "Ready";
}

function renderConversation(b) {
  if (window.DeskUI) DeskUI.paintHeaderAvatar(b);
  $("conv-name").textContent = b.name;
  $("conv-status").textContent = conversationSubtitle(b);
  $("screen-title").textContent = `${b.name}'s workspace`;
  $("screen-sub").textContent = "Agent Computer · live";
  if ($("takeover-agent-name")) $("takeover-agent-name").textContent = `${b.name} — Ubuntu Desktop`;
  window.DeskUI?.refreshOsLock?.();
  if ($("hermes-tui-model")) $("hermes-tui-model").textContent = modelLabel(b);
  if ($("message")) {
    $("message").disabled = false;
    $("message").placeholder = botIsAgent(b)
      ? `Message ${b.name}  ·  type / for commands`
      : `Message ${b.name}`;
  }
  $("undo").disabled = !b.can_undo && !(b.messages || []).some((m) => m.role === "user");
  updateSendButton();
  syncMoreMenu();
  renderHorizon(b);
  if ($("new-routine")) $("new-routine").hidden = false;
  $("takeover").hidden = b.control === "user_controlled";
  $("im-done").hidden = b.control !== "user_controlled";
  const banner = $("control-banner");
  if (b.control === "user_controlled") {
    banner.hidden = false;
    banner.textContent = `${b.name.toUpperCase()} — YOU HAVE CONTROL. Click the page and type (captcha / check-if-human).`;
    document.body.classList.add("desktop-takeover-active");
  } else {
    banner.hidden = true;
    document.body.classList.remove("desktop-takeover-active");
  }
  const t = $("transcript");
  const pin = transcriptPinned();
  const fromBottom = t.scrollHeight - t.scrollTop;
  const folds = captureFolds(t);
  t.innerHTML = "";
  const clock = document.createElement("div");
  clock.className = "time-divider";
  clock.id = "chat-clock";
  clock.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  t.appendChild(clock);
  const msgs = b.messages || [];
  msgs.forEach((m, idx) => {
    if (m.role === "progress") {
      t.appendChild(renderProgressLine(m));
      return;
    }
    if (m.role === "thought") {
      t.appendChild(renderThoughtBlock(m, idx));
      if (m.elapsed_ms && msgs[idx + 1]?.role === "user") t.appendChild(renderWorked(m.elapsed_ms));
      return;
    }
    if (m.role === "tool") {
      t.appendChild(renderToolBlock(m, idx));
      if (m.elapsed_ms && msgs[idx + 1]?.role === "user") t.appendChild(renderWorked(m.elapsed_ms));
      return;
    }
    if (m.role === "worked") {
      t.appendChild(renderWorked(m.elapsed_ms || 0));
      return;
    }
    const text = m.role === "assistant"
      ? stripAssistantPadding(collapseRestartedAssistant(m.text))
      : m.role === "user"
        ? visibleUserText(m.text)
        : m.text;
    if (m.role === "assistant" && !text && !(m.images || []).length && !(m.attachments || []).length) {
      if (m.elapsed_ms) t.appendChild(renderWorked(m.elapsed_ms));
      return;
    }
    const d = document.createElement("div");
    d.className = `bubble msg ${m.role === "user" ? "user" : m.role === "assistant" ? "assistant agent" : m.role}`;
    if (m.via === "tui" || m.via === "dm" || m.peer) {
      const tag = document.createElement("div");
      tag.className = "via";
      tag.textContent = m.via === "tui" ? "from TUI" : m.peer ? `from ${m.peer}` : m.via;
      d.appendChild(tag);
    }
    if (m.attachments?.length) {
      const wrap = document.createElement("div");
      wrap.className = "msg-attachments";
      for (const a of m.attachments) {
        const cell = document.createElement("div");
        cell.className = "msg-attachment";
        if (String(a.type || "").startsWith("video/")) {
          const v = document.createElement("video");
          v.src = a.url || (a.path ? fileUrl(b.workspace_id || state.workspaceId, a.path) : "");
          v.controls = true;
          v.addEventListener("click", (e) => {
            if (e.target === v && v.src) openChatLightbox(v.src, a.name || a.path || "video");
          });
          if (a.path) {
            v.addEventListener("contextmenu", (e) => {
              e.preventDefault();
              showDeskMenu(e.clientX, e.clientY, mediaContextItems(a.path));
            });
          }
          cell.appendChild(v);
        } else if (a.url || a.path) {
          const el = document.createElement("img");
          el.src = a.url || fileUrl(b.workspace_id, a.path);
          el.alt = a.name || a.path || "attachment";
          if (a.path) {
            el.addEventListener("contextmenu", (e) => {
              e.preventDefault();
              showDeskMenu(e.clientX, e.clientY, mediaContextItems(a.path));
            });
          }
          cell.appendChild(el);
        }
        wrap.appendChild(cell);
      }
      d.appendChild(wrap);
    }
    if (text) {
      if (m.role === "assistant") {
        const body = document.createElement("div");
        body.className = "msg-body md";
        body.innerHTML = renderMarkdown(text, `msg-${idx}`);
        if (body.querySelector(".code-block, table, ul, ol")) d.classList.add("has-rich");
        d.appendChild(body);
      } else {
        d.append(document.createTextNode(text));
      }
    }
    for (const img of uniqueChatImages(m.images) || []) {
      if (!img.path && !img.preview) continue;
      const el = document.createElement("img");
      const src = (img.path && b.workspace_id) ? fileUrl(b.workspace_id, img.path) : (img.preview || "");
      el.src = src || img.preview;
      el.alt = img.name || img.path || "image";
      el.title = img.name || img.path || "Open image";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        const view = (img.path && (b.workspace_id || state.workspaceId))
          ? fileUrl(b.workspace_id || state.workspaceId, img.path)
          : el.src;
        if (view) openChatLightbox(view, img.name || img.path || "");
      });
      if (img.path) {
        el.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          showDeskMenu(e.clientX, e.clientY, mediaContextItems(img.path));
        });
      }
      d.appendChild(el);
    }
    t.appendChild(d);
    if (m.role === "assistant" && m.elapsed_ms) t.appendChild(renderWorked(m.elapsed_ms));
  });
  restoreFolds(t, folds);
  wrapChatZoom(t);
  bindTranscriptMedia(t);
  if (pin) {
    stickTranscript(true);
  } else {
    t.scrollTop = Math.max(0, t.scrollHeight - fromBottom);
    updateJumpLatest();
  }
  syncChatStatusLine(b);
  renderMeta(b);
  updateActiveChatMeta(b);
}

function mergeChatImages(prev, next) {
  const out = (prev || []).map((img) => ({ ...img }));
  for (const n of next || []) {
    if (!n) continue;
    const byPath = n.path ? out.findIndex((p) => p.path === n.path) : -1;
    if (byPath >= 0) {
      out[byPath] = { ...out[byPath], ...n, preview: out[byPath].preview || n.preview };
      continue;
    }
    const open = out.findIndex((p) => !p.path && (p.preview || p.data));
    if (open >= 0 && n.path) {
      out[open] = { ...out[open], ...n, preview: out[open].preview || n.preview };
      continue;
    }
    out.push({ ...n });
  }
  return out;
}

// Mirror of deskd._SAVED_MEDIA_LINE / visible_user_text: the collapse only
// runs when a media line is actually removed, so pasted spacing round-trips.
const SAVED_MEDIA_LINE_RE = /^(?:The user pasted \d+ (?:image|video|file)s? into chat\.|Saved to (?:Pictures|Videos|Desktop)\/[^\n]*)$/m;
function visibleUserText(text) {
  const out = String(text || "").trim();
  if (!SAVED_MEDIA_LINE_RE.test(out)) return out;
  return out
    .replace(/^The user pasted \d+ (?:image|video|file)s? into chat\.\s*/gim, "")
    .replace(/(?:^|\n)Saved to (?:Pictures|Videos|Desktop)\/[^\n]*/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function uniqueChatImages(imgs) {
  const out = [];
  for (const img of imgs || []) {
    if (!img || (!img.path && !img.preview)) continue;
    const i = out.findIndex((x) => (img.path && x.path === img.path) || (!img.path && img.preview && x.preview === img.preview));
    if (i >= 0) out[i] = { ...out[i], ...img, preview: out[i].preview || img.preview };
    else out.push(img);
  }
  return out;
}

function fmtNum(n) {
  const x = Number(n) || 0;
  return x.toLocaleString();
}

function fmtCtx(n) {
  const x = Number(n) || 0;
  if (x >= 1000000) {
    const m = x / 1000000;
    return `${m >= 10 ? m.toFixed(0) : m.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (x >= 10000) return `${Math.round(x / 1000)}k`;
  return fmtNum(Math.round(x));
}

function modelLabel(b) {
  const hit = (b.models || []).find((m) => m.id === b.model);
  return hit?.name || b.model || "—";
}

function botHomeNode(b) {
  return (b && (b.node || state.nodeName)) || "";
}

function botIsRemote(b) {
  const home = botHomeNode(b);
  return !!(b && b.remote && home && state.nodeName && home !== state.nodeName);
}

function isCloudPickerRow(m) {
  if (!m) return false;
  if (m.local === false) return true;
  const id = String(m.id || "").toLowerCase();
  return id.startsWith("hermes");
}

function mergePickerModels(incoming, previous) {
  const rows = Array.isArray(incoming) ? incoming.map((m) => ({ ...m })) : [];
  const have = new Set(rows.map((m) => String(m.id || "")).filter(Boolean));
  for (const m of previous || []) {
    const id = String(m?.id || "");
    if (!id || have.has(id)) continue;
    if (!isCloudPickerRow(m)) continue;
    rows.push({
      ...m,
      local: false,
      available: m.available !== false,
      running: false,
      startable: false,
    });
    have.add(id);
  }
  return rows;
}

function modelsForBot(b) {
  if (b && Array.isArray(b.models) && b.models.length) return b.models;
  if (b && !botIsRemote(b)) return state.catalog || [];
  return [];
}

async function catalogForBot(b) {
  if (botIsRemote(b)) {
    const home = botHomeNode(b);
    return api(`/v1/cluster/peer-models?peer=${encodeURIComponent(home)}`);
  }
  const cat = await api("/v1/models");
  state.catalog = mergePickerModels(cat.models || [], state.catalog);
  return { ...cat, models: state.catalog };
}

function renderMeta(b) {
  const bar = $("composer-meta");
  if (!b) {
    if (bar) bar.hidden = true;
    return;
  }
  if (bar) bar.hidden = false;
  if ($("meta-model")) $("meta-model").textContent = `${modelLabel(b)} ▴`;
  const effortChip = $("effort-chip");
  if (effortChip) {
    const eff = b.kind === "hermes" ? currentEffort(b) : "";
    const show = !!(eff && eff !== "off");
    effortChip.hidden = !show;
    effortChip.textContent = show ? `thinking ${eff}` : "";
  }
  const used = Number(b.context_used) || 0;
  const max = Number(b.context_window) || 0;
  const pct = Math.min(100, Math.max(0, max > 0 && Number.isFinite(used) ? (used / max) * 100 : 0));
  const usage = $("context-usage");
  if (usage) {
    usage.textContent = max ? `${fmtCtx(used)} / ${fmtCtx(max)}` : fmtCtx(used);
    usage.title = `${used} / ${max} (${pct.toFixed(2)}%) · ${b.context_source || "unknown"}`;
  }
  const ctxStat = document.querySelector(".context-stat");
  if (ctxStat) {
    ctxStat.title = `Context ${used} / ${max} (${pct.toFixed(2)}%) · ${b.context_source || "unknown"}`;
    const pressure = window.WorkingMemory && typeof window.WorkingMemory.pressureFromUtilization === "function"
      ? window.WorkingMemory.pressureFromUtilization(max > 0 ? used / max : null)
      : (pct >= 90 ? "CRITICAL" : pct >= 80 ? "RED" : pct >= 65 ? "ORANGE" : pct >= 45 ? "YELLOW" : "GREEN");
    if (pressure) ctxStat.setAttribute("data-pressure", pressure);
    else ctxStat.removeAttribute("data-pressure");
  }
  if ($("hermes-tui-model")) $("hermes-tui-model").textContent = modelLabel(b);
  const tuiCtx = $("hermes-tui-context");
  if (tuiCtx) tuiCtx.textContent = `${fmtNum(used)} / ${fmtNum(max)}`;
  const tps = $("tps-counter");
  const tpsVal = Number(b.tps);
  const tpsSafe = Number.isFinite(tpsVal) && tpsVal >= 0 ? tpsVal : 0;
  if (tps) tps.textContent = tpsSafe.toFixed(1);
  const tpsStat = document.querySelector(".tps-stat");
  if (tpsStat) {
    const bits = [`${tpsSafe.toFixed(2)} tok/s`];
    if (b.speed_source) bits.push(b.speed_source);
    if (b.token_source) bits.push(b.token_source);
    tpsStat.title = bits.join(" · ");
  }
  if (window.WorkingMemory) WorkingMemory.syncFromBot(b);
  const sel = $("model-select");
  const list = modelsForBot(b);
  if (sel) {
    const current = sel.value;
    sel.innerHTML = "";
    const agentBuild = b.kind === "hermes";
    for (const m of list) {
      const opt = document.createElement("option");
      opt.value = m.id;
      const llama = String(m.family || m.id || "").toLowerCase().includes("flash-next")
        || m.family === "llamacpp";
      const cloud = m.local === false || String(m.id || "").toLowerCase().startsWith("hermes");
      const busy = m.available === false && !agentBuild && !cloud && !llama;
      opt.disabled = busy && m.id !== b.model;
      opt.textContent = busy ? `${m.name || m.id} — not running` : (m.name || m.id);
      if (m.unavailable_reason) opt.title = m.unavailable_reason;
      sel.appendChild(opt);
    }
    if ([...sel.options].some((o) => o.value === b.model)) sel.value = b.model;
    else if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
  }
  const lab = $("model-select-label");
  if (lab) lab.textContent = modelLabel(b);
  renderModelMenu(b);
  requestAnimationFrame(syncMobileComposerPad);
}

const MODEL_ICON_STOP = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1.5"/></svg>';
const MODEL_ICON_PLAY = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l10-5.5L4 2.5z"/></svg>';

function isLlamaCppModel(m) {
  if (!m) return false;
  if (m.family === "llamacpp") return true;
  const id = String(m.id || "").toLowerCase();
  const url = String(m.baseUrl || m.base_url || "");
  if (/flash-next|27b-q[45]|llama\.cpp/.test(id)) return true;
  return /:8080\b|:8081\b/.test(url);
}

function isLocalGpuModel(m) {
  if (!m) return false;
  if (isLlamaCppModel(m)) return true;
  if (m.local === true) return true;
  if (m.local === false) return false;
  if (m.weights) return true;
  const id = String(m.id || "").toLowerCase();
  const url = String(m.baseUrl || m.base_url || "");
  if (/127\.0\.0\.1|localhost/.test(url)) return true;
  return /qwen38|qwen3-vl|muse-glimmer|muse/.test(id);
}

function isExclusiveVllmModel(m) {
  return isLocalGpuModel(m) && !isLlamaCppModel(m);
}

function localFamily(m) {
  if (!m) return "";
  if (m.family) return m.family;
  if (isLlamaCppModel(m)) return "llamacpp";
  const id = String(m.id || "").toLowerCase();
  if (/muse/.test(id)) return "muse";
  if (/hybrid/.test(id)) return "hybrid";
  if (/vl-8b|qwen3-vl/.test(id)) return "vl8";
  if (/qwen/.test(id)) return "qwen";
  return id;
}

function runningLocalFamily(list) {
  const locals = (list || []).filter(isLocalGpuModel);
  const launching = locals.find((m) => m.busy === "starting");
  if (launching) return localFamily(launching);
  const marked = locals.filter((m) => m.running === true);
  if (marked.length) return localFamily(marked[0]);
  const down = locals.filter((m) => m.available === false);
  const up = locals.filter((m) => m.available !== false);
  if (!down.length || !up.length) return null;
  const families = [...new Set(up.map(localFamily))];
  if (families.includes("hybrid")) return "hybrid";
  return families[0] || null;
}

function gpuHeld(list) {
  const locals = (list || []).filter(isLocalGpuModel);
  if (locals.some((m) => m.busy === "starting" || m.busy === "stopping")) return true;
  return !!runningLocalFamily(list);
}

function isLocalRunning(m, list) {
  if (!isLocalGpuModel(m)) return false;
  if (m.running === true) return true;
  if (m.running === false && isLlamaCppModel(m)) return false;
  const fam = runningLocalFamily(list);
  return !!fam && localFamily(m) === fam;
}

function isLocalOccupying(m, list) {
  if (!isLocalGpuModel(m) || isLocalRunning(m, list)) return false;
  if (isLlamaCppModel(m)) {
    return (list || []).some((x) => isLlamaCppModel(x) && isLocalRunning(x, list));
  }
  if (isExclusiveVllmModel(m)) return gpuHeld(list);
  return false;
}



function placeModelMenu() {
  const wrap = document.querySelector(".model-select-wrap");
  const menu = $("model-menu");
  if (!wrap || !menu) return;
  const r = wrap.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.left = Math.max(8, r.left) + "px";
  menu.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + "px";
  menu.style.top = "auto";
  menu.style.zIndex = "5000";
}

function renderModelMenu(b) {
  const menu = $("model-menu");
  if (!menu) return;
  const list = modelsForBot(b);
  menu.innerHTML = "";
  for (const m of list) {
    const row = document.createElement("div");
    row.className = "model-row";
    const local = isLocalGpuModel(m);
    const running = isLocalRunning(m, list);
    const occupying = isLocalOccupying(m, list);
    const vllmLock = isExclusiveVllmModel(m) && occupying;
    const gpuBusy = list.some((x) => isExclusiveVllmModel(x) && x.busy);
    if (m.id === b.model) row.classList.add("current");
    if (occupying) {
      row.classList.add("model-unavailable");
      row.title = m.unavailable_reason || (isLlamaCppModel(m)
        ? "Select to move both GPUs to this model"
        : "Stop the running local model first");
    }
    if (m.busy === "starting") row.classList.add("model-starting");
    if (m.busy === "stopping") row.classList.add("model-stopping");
    const cw = m.context_window ? `${fmtNum(m.context_window)} ctx` : "";
    let extra = "";
    if (m.busy === "starting") extra = " · starting";
    else if (m.busy === "stopping") extra = " · stopping";
    else if (m.error) extra = " · failed";
    else if (occupying) extra = " · GPUs busy";
    else if (running) extra = " · running";
    if (m.error) row.title = m.error;
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "model-pick";
    pick.disabled = vllmLock || (gpuBusy && isExclusiveVllmModel(m) && !running);
    pick.innerHTML = `<div class="mid">${escapeHtml(m.name || m.id)}</div><div class="mcw">${escapeHtml(m.id)}${cw ? " · " + cw : ""}${extra}</div>`;
    pick.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (pick.disabled) return;
      await selectPickerModel(b, m, list);
    };
    row.appendChild(pick);
    if (isLocalGpuModel(m)) {
      const power = document.createElement("button");
      power.type = "button";
      const stopping = m.busy === "stopping";
      const starting = m.busy === "starting";
      if (running || stopping) {
        power.className = "model-power is-stop";
        power.innerHTML = MODEL_ICON_STOP;
        power.disabled = stopping;
        power.title = stopping ? "Stopping…" : "Stop local model";
        power.setAttribute("aria-label", "Stop local model");
        power.onclick = async (e) => {
          e.preventDefault();
          e.stopPropagation();
          await controlLocalLlm("stop", m.id, b);
        };
      } else {
        power.className = "model-power is-start";
        power.innerHTML = MODEL_ICON_PLAY;
        power.disabled = starting || vllmLock;
        power.title = starting
          ? "Starting…"
          : vllmLock
            ? "Stop the other local model first"
            : occupying
              ? "Switch both GPUs to this model"
              : "Start local model";
        power.setAttribute("aria-label", "Start local model");
        power.onclick = async (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (power.disabled) return;
          await controlLocalLlm("start", m.id, b);
        };
      }
      row.appendChild(power);
    }
    menu.appendChild(row);
  }
  if (!menu.hidden) placeModelMenu();
}

async function selectPickerModel(b, m, list) {
  if (!m) return;
  const rows = list || modelsForBot(b);
  if (isLocalGpuModel(m) && !isLocalRunning(m, rows)) {
    if (isExclusiveVllmModel(m) && isLocalOccupying(m, rows)) return;
    await controlLocalLlm("start", m.id, b);
    return;
  }
  closeModelMenu();
  if (m.id !== b.model) await switchBotModel(b, m.id);
}

async function applyBotModel(b, id, effort) {
  const list = modelsForBot(b);
  const hit = list.find((x) => x.id === id);
  if (hit && isLocalGpuModel(hit) && !isLocalRunning(hit, list)) {
    if (!(isExclusiveVllmModel(hit) && isLocalOccupying(hit, list))) {
      await controlLocalLlm("start", id, b);
    }
  }
  const body = { model: id };
  if (effort) body.effort = String(effort).toLowerCase();
  const j = await api(`/v1/bots/${b.id}/model`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  b.model = j.model;
  if (j.window) b.context_window = j.window;
  if (j.models && j.models.length) b.models = j.models;
  if (j.effort) b.effort = j.effort;
  else if (effort) b.effort = String(effort).toLowerCase();
  const chosen = (b.models || []).find((x) => x.id === b.model);
  if (chosen?.context_window) b.context_window = chosen.context_window;
  // context_used is kept: the desk re-seeds it from runtime session files and
  // pushes the value over the usage channel (TUI keeps last known context too).
  renderMeta(b);
  requestAnimationFrame(fitTerms);
  setTimeout(fitTerms, 120);
  return j;
}

async function switchBotModel(b, id) {
  if ($("meta-model")) $("meta-model").textContent = "Switching…";
  try {
    await applyBotModel(b, id);
  } catch (err) {
    if ($("meta-model")) $("meta-model").textContent = `${modelLabel(b)} ▴`;
    alert(err.message || err);
  }
}

async function refreshLocalModelCatalog(b) {
  const prev = b.models || [];
  try {
    const st = await api("/v1/local-llm/status");
    b.llmError = st.error || "";
    if (Array.isArray(st.models) && st.models.length) b.models = mergePickerModels(st.models, prev);
    else {
      const cat = await catalogForBot(b);
      if (Array.isArray(cat.models) && cat.models.length) b.models = mergePickerModels(cat.models, prev);
    }
  } catch {
    try {
      const cat = await catalogForBot(b);
      if (Array.isArray(cat.models) && cat.models.length) b.models = mergePickerModels(cat.models, prev);
    } catch { /* keep last catalog */ }
  }
  renderMeta(b);
  return b.llmError || "";
}

async function controlLocalLlm(action, modelId, b) {
  const menu = $("model-menu");
  const target = (b.models || []).find((x) => x.id === modelId);
  const exclusive = isExclusiveVllmModel(target);
  const llama = isLlamaCppModel(target);
  for (const m of b.models || []) {
    if (!isLocalGpuModel(m)) continue;
    const sibling = (exclusive && isExclusiveVllmModel(m)) || (llama && isLlamaCppModel(m));
    if (m.id !== modelId && !sibling) continue;
    if (action === "stop" && (m.id === modelId || sibling)) {
      m.busy = "stopping";
      m.running = false;
      m.startable = false;
    } else if (action === "start" && m.id === modelId) {
      m.busy = "starting";
      m.running = false;
      m.available = false;
      m.startable = false;
    } else if (action === "start" && sibling) {
      m.startable = false;
    }
  }
  renderModelMenu(b);
  if (menu) {
    menu.hidden = false;
    placeModelMenu();
  }
  try {
    await api("/v1/local-llm/control", {
      method: "POST",
      body: JSON.stringify({ action, model: modelId }),
    });
  } catch (err) {
    alert(err.message || err);
    await refreshLocalModelCatalog(b);
    if (menu && !menu.hidden) placeModelMenu();
    return;
  }
  const deadline = Date.now() + (action === "start" ? 360000 : 40000);
  const delay = action === "start" ? 2000 : 600;
  while (Date.now() < deadline) {
    await refreshLocalModelCatalog(b);
    if (menu && !menu.hidden) placeModelMenu();
    const row = (b.models || []).find((x) => x.id === modelId);
    const rowBusy = Boolean(row && row.busy);
    if (action === "stop" && row && !row.running && !rowBusy) break;
    if (action === "start" && row && row.running && !rowBusy) {
      if (modelId !== b.model) await switchBotModel(b, modelId);
      break;
    }
    if (action === "start" && !rowBusy && (b.llmError || row?.error)) break;
    await new Promise((r) => setTimeout(r, delay));
  }
  const doneErr = await refreshLocalModelCatalog(b);
  if (action === "start" && doneErr && !(b.models || []).some((x) => x.running)) {
    alert(doneErr);
  }
  if (menu && !menu.hidden) placeModelMenu();
}

function routineScheduleLabel(r) {
  const s = String(r.schedule || "");
  const m = s.match(/^(\d+(?:\.\d+)?)(mo|[mhdwy])$/i);
  if (m) {
    const labels = { m:"minute", h:"hour", d:"day", w:"week", mo:"month", y:"year" };
    const n = Number(m[1]);
    const word = labels[m[2].toLowerCase()] || "interval";
    return `Every ${n} ${word}${n === 1 ? "" : "s"}`;
  }
  if (r.schedulePreset === "weekdays" || s.startsWith("weekday")) return `Weekdays • ${r.time || "08:00"}`;
  return s || r.cron || "Scheduled";
}

function renderRoutines(list) {
  const host = $("routine-list");
  if (!host) return;
  host.innerHTML = "";
  if (!list || !list.length) {
    host.innerHTML = `<div class="empty-manager-state">No routines yet. Add one to schedule recurring work.</div>`;
    return;
  }
  for (const r of list) {
    const card = document.createElement("div");
    card.className = `manager-card ${r.enabled ? "" : "paused"}`;
    const next = r.next_run ? new Date(r.next_run * 1000).toLocaleString() : "Scheduled";
    card.innerHTML = `
      <div class="manager-card-top">
        <span class="manager-state-dot"></span>
        <div>
          <div class="manager-card-title">${escapeHtml(r.name)}</div>
          <div class="manager-card-meta">${escapeHtml(routineScheduleLabel(r))}<br>${r.enabled ? `Next: ${escapeHtml(next)}` : "Paused"}</div>
        </div>
        <div class="manager-card-actions">
          <button type="button" data-act="toggle">${r.enabled ? "Pause" : "Resume"}</button>
          <button type="button" data-act="menu">•••</button>
        </div>
      </div>
      <div class="manager-card-detail">${escapeHtml(r.instruction || "")}</div>`;
    card.querySelector('[data-act="toggle"]').onclick = async () => {
      await api(`/v1/bots/${state.selected}/routines/${r.id}`, {
        method: "POST",
        body: JSON.stringify({ enabled: !r.enabled }),
      });
      await loadWorkspace(state.bots.find((x) => x.id === state.selected));
    };
    card.querySelector('[data-act="menu"]').onclick = async () => {
      const choice = prompt("Routine action:\n1 = Run now\n2 = Edit\n3 = Duplicate\n4 = Delete", "1");
      if (choice === "1") {
        await api(`/v1/agent/${state.selected}/prompt`, {
          method: "POST",
          body: JSON.stringify({
            text: `[Routine: ${r.name} — run now]\n${r.instruction}`,
            page_id: PAGE_ID,
          }),
        });
        return;
      }
      if (choice === "2") {
        window.DeskUI?.openRoutineModal(r);
        return;
      }
      if (choice === "3") {
        await api(`/v1/bots/${state.selected}/routines`, {
          method: "POST",
          body: JSON.stringify({
            name: `${r.name} Copy`,
            instruction: r.instruction,
            schedule: r.schedule,
            type: r.type,
            timezone: r.timezone,
            time: r.time,
            cron: r.cron,
            schedulePreset: r.schedulePreset,
          }),
        });
        await loadWorkspace(state.bots.find((x) => x.id === state.selected));
        return;
      }
      if (choice === "4") {
        if (!confirm("Delete this routine?")) return;
        await fetch(`/v1/bots/${state.selected}/routines/${r.id}`, { method: "DELETE", headers: headers() });
        await loadWorkspace(state.bots.find((x) => x.id === state.selected));
      }
    };
    host.appendChild(card);
  }
}

function currentBot() {
  const bots = state.bots || [];
  let saved = "";
  try { saved = localStorage.getItem("hermes-desk-selected-bot") || ""; } catch { /* ignore */ }
  const ids = [state.selected, observerBotId, saved].filter(Boolean);
  for (const id of ids) {
    const hit = bots.find((x) => x.id === id || x.workspace_id === id);
    if (hit) return hit;
  }
  if (state.workspaceId) {
    const byWs = bots.find((x) => x.workspace_id === state.workspaceId || x.id === state.workspaceId);
    if (byWs) return byWs;
  }
  if (bots.length === 1) return bots[0];
  const local = bots.filter((b) => !b.remote);
  if (local.length === 1) return local[0];
  const pick = local[0] || bots[0] || null;
  if (pick) return pick;
  if (state.workspaceId) return { id: state.workspaceId, workspace_id: state.workspaceId };
  return null;
}

async function restoreSelectedBot() {
  if (state.selected && (state.bots || []).some((b) => b.id === state.selected)) return;
  const pick = currentBot();
  if (pick?.id) await selectBot(pick.id);
}

async function selectBot(id) {
  if (!id || String(id).startsWith("peer:")) return;
  state.selected = id;
  try { localStorage.setItem("hermes-desk-selected-bot", id); } catch { /* ignore */ }
  if (window.DeskUI?.setMobileView) DeskUI.setMobileView("chat");
  else if (mqDrawer()) setHallwayOpen(false);
  renderRoster();
  const [b, chats] = await Promise.all([
    api(`/v1/bots/${id}`),
    api(`/v1/bots/${id}/chats`).catch(() => ({ activeId: null, chats: [] })),
  ]);
  const i = state.bots.findIndex((x) => x.id === id);
  if (i >= 0) state.bots[i] = b;
  else state.bots.push(b);
  state.timeline = { activeId: chats.activeId || b.chat_id || null, chats: chats.chats || [] };
  if ($("timeline-search") && document.activeElement !== $("timeline-search")) {
    $("timeline-search").value = state.timelineQuery || "";
  }
  renderConversation(b);
  renderChatTimeline();
  if (window.WorkingMemory) WorkingMemory.onSelectBot(b);
  await loadWorkspace(b);
  hideAgentDesktopCursor();
  moveAgentDesktopCursor(b.desktop_cursor?.x ?? 500, b.desktop_cursor?.y ?? 500, false, false);
  for (const k of ["shell", "tui"]) {
    if (terms[k]) {
      terms[k].seq = 0;
      try {
        terms[k].term.reset();
      } catch {
        /* ignore */
      }
    }
  }
  syncRobotSimulatorForBot(b);
  if (botHasRobotSimulator(b)) connectVirtualBodyWs(id);
  else {
    try { virtualBodyWs?.close(); } catch { /* ignore */ }
    virtualBodyWs = null;
    virtualBodyWsBid = "";
  }
  setSurface(state.surface);
  window.DeskUI?.renderHourlyNotes();
}

async function loadWorkspace(b) {
  if (!b) return;
  state.workspaceId = b.workspace_id || b.id || state.workspaceId || "";
  const ws = await api(`/v1/workspaces/${b.workspace_id}`);
  state.files = ws.files || [];
  state.dev = ws.dev || { kind: "generic", commands: {} };
  renderDesktop(b, state.files);
  renderEditorFiles(b, state.files);
  renderDevSystem(state.dev);
  renderRoutines(ws.routines || []);
}

function prettyFilesPath(rel) {
  const clean = String(rel || "").replace(/^\/+|\/+$/g, "");
  return clean ? `~/${clean}` : "~";
}

function filesInFolder(files, dir) {
  const prefix = dir ? `${String(dir).replace(/\/+$/g, "")}/` : "";
  const seen = new Map();
  for (const f of files || []) {
    const path = String(f.path || "");
    if (!path || path.startsWith(".git") || path.startsWith(".hermes/") || path === ".hermes") continue;
    if (prefix) {
      if (path === dir) continue;
      if (!path.startsWith(prefix)) continue;
      const rest = path.slice(prefix.length);
      const name = rest.split("/")[0];
      if (!name) continue;
      const childPath = prefix + name;
      const isDir = f.dir || rest.includes("/");
      const prev = seen.get(name);
      if (!prev) seen.set(name, { path: childPath, name, dir: isDir, kind: isDir ? "dir" : f.kind });
      else if (isDir) prev.dir = true;
    } else {
      const name = path.split("/")[0];
      if (name === "Trash") continue;
      const isDir = f.dir || path.includes("/");
      const prev = seen.get(name);
      if (!prev) seen.set(name, { path: name, name, dir: isDir, kind: isDir ? "dir" : f.kind });
      else if (isDir) prev.dir = true;
    }
  }
  const out = [...seen.values()];
  out.sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name));
  return out;
}

function syncFilesSidebar(dir) {
  const here = String(dir || "");
  document.querySelectorAll("#files-sidebar [data-file-place]").forEach((btn) => {
    btn.classList.toggle("active", (btn.dataset.filePlace || "") === here);
  });
}

function browseFiles(dir) {
  state.filesDir = String(dir || "").replace(/^\/+|\/+$/g, "");
  const b = state.bots.find((x) => x.id === state.selected);
  renderDesktop(b, state.files);
}

window.deskBrowseFiles = browseFiles;

function renderDesktop(b, files) {
  const box = $("desktop-icons");
  if (!box) return;
  box.innerHTML = "";
  const dir = state.filesDir || "";
  if ($("desktop-files-path")) $("desktop-files-path").textContent = prettyFilesPath(dir);
  syncFilesSidebar(dir);
  if (dir) {
    const up = document.createElement("div");
    up.className = "fitem fitem-up";
    up.innerHTML = '<span class="glyph">↩</span>..';
    up.title = "Parent folder";
    up.onclick = () => {
      const parts = dir.split("/").filter(Boolean);
      parts.pop();
      browseFiles(parts.join("/"));
    };
    box.appendChild(up);
  }
  const kids = filesInFolder(files, dir);
  const emptyBtn = $("empty-trash-btn");
  if (emptyBtn) emptyBtn.hidden = dir !== "Trash";
  if (!kids.length) {
    const empty = document.createElement("div");
    empty.className = "files-empty";
    empty.textContent = "This folder is empty";
    box.appendChild(empty);
    return;
  }
  for (const f of kids) {
    const el = document.createElement("div");
    el.className = "fitem" + (f.dir ? " is-dir" : "");
    el.innerHTML = `<span class="glyph">${f.dir ? "📁" : iconFor(f)}</span>${escapeHtml(f.name)}`;
    el.title = f.path;
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", f.path);
      e.dataTransfer.effectAllowed = "move";
      el.classList.add("is-dragging");
    });
    el.addEventListener("dragend", () => el.classList.remove("is-dragging"));
    el.ondblclick = () => {
      if (f.dir) browseFiles(f.path);
      else if (b) openDeskFile(b, f);
    };
    el.onclick = () => {
      box.querySelectorAll(".fitem.selected").forEach((n) => n.classList.remove("selected"));
      el.classList.add("selected");
    };
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      box.querySelectorAll(".fitem.selected").forEach((n) => n.classList.remove("selected"));
      el.classList.add("selected");
      const items = inTrashPath(f.path)
        ? [{ label: "Delete permanently", danger: true, run: () => deleteWorkspacePath(f.path) }]
        : [
            { label: "Move to Trash", run: () => trashWorkspacePath(f.path) },
            { label: "Delete permanently", danger: true, run: () => deleteWorkspacePath(f.path) },
          ];
      showDeskMenu(e.clientX, e.clientY, items);
    });
    box.appendChild(el);
  }
}

async function trashWorkspacePath(path) {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b || !path) return;
  await api(`/v1/workspaces/${b.workspace_id}/trash`, {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  detachChatMedia(b, path);
  if (state.filesDir === path || String(state.filesDir || "").startsWith(`${path}/`)) browseFiles("");
  await loadWorkspace(b);
}

async function deleteWorkspacePath(path) {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b || !path) return;
  const name = String(path).split("/").pop();
  if (!confirm(`Permanently delete ${name}? This cannot be undone.`)) return;
  await api(`/v1/workspaces/${b.workspace_id}/delete`, {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  detachChatMedia(b, path);
  if (state.filesDir === path || String(state.filesDir || "").startsWith(`${path}/`)) browseFiles("");
  await loadWorkspace(b);
}

async function emptyTrash() {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b) return;
  if (!confirm("Empty Trash? Items will be permanently deleted.")) return;
  const j = await api(`/v1/workspaces/${b.workspace_id}/trash/empty`, { method: "POST", body: "{}" });
  for (const p of j.paths || []) detachChatMedia(b, p);
  if (String(state.filesDir || "") === "Trash" || String(state.filesDir || "").startsWith("Trash/")) browseFiles("Trash");
  await loadWorkspace(b);
}

function detachChatMedia(b, path) {
  if (!b || !path) return;
  const name = String(path).split("/").pop();
  let changed = false;
  for (const m of b.messages || []) {
    const nImg = (m.images || []).length;
    if (nImg) {
      m.images = m.images.filter((i) => i.path !== path && i.path !== name);
      if (m.images.length !== nImg) changed = true;
    }
    const nAtt = (m.attachments || []).length;
    if (nAtt) {
      m.attachments = m.attachments.filter((a) => a.path !== path && a.name !== name);
      if (m.attachments.length !== nAtt) changed = true;
    }
  }
  if (changed && state.selected === b.id) renderConversation(b);
  const frame = $("app-preview-frame");
  if (frame && (frame.src || "").includes(encodeURIComponent(path))) {
    loadRobotSimulator();
  }
}

function mediaContextItems(path) {
  if (!path) return [{ label: "Remove", run: async () => {} }];
  if (inTrashPath(path)) {
    return [{ label: "Delete permanently", danger: true, run: () => deleteWorkspacePath(path) }];
  }
  return [
    { label: "Move to Trash", run: () => trashWorkspacePath(path) },
    { label: "Delete permanently", danger: true, run: () => deleteWorkspacePath(path) },
  ];
}

function wireDesktopTrash() {
  const trash = $("desktop-trash");
  if (!trash || trash.dataset.wired === "true") return;
  trash.dataset.wired = "true";
  trash.addEventListener("dblclick", () => {
    window.DeskUI?.openWindow?.("files");
    browseFiles("Trash");
  });
  trash.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    e.stopPropagation();
    showDeskMenu(e.clientX, e.clientY, [
      { label: "Open Trash", run: async () => { window.DeskUI?.openWindow?.("files"); browseFiles("Trash"); } },
      { label: "Empty Trash", danger: true, run: emptyTrash },
    ]);
  });
  trash.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.types].includes("text/plain")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    trash.classList.add("is-drop");
  });
  trash.addEventListener("dragleave", () => trash.classList.remove("is-drop"));
  trash.addEventListener("drop", (e) => {
    e.preventDefault();
    trash.classList.remove("is-drop");
    const path = (e.dataTransfer.getData("text/plain") || "").trim();
    if (path) trashWorkspacePath(path).catch((err) => alert(err.message || err));
  });
  $("empty-trash-btn")?.addEventListener("click", () => emptyTrash().catch((err) => alert(err.message || err)));
  const grid = $("desktop-icons");
  if (grid && grid.dataset.ctxWired !== "true") {
    grid.dataset.ctxWired = "true";
    grid.addEventListener("contextmenu", (e) => {
      if (e.target.closest(".fitem")) return;
      if ((state.filesDir || "") !== "Trash") return;
      e.preventDefault();
      showDeskMenu(e.clientX, e.clientY, [{ label: "Empty Trash", danger: true, run: emptyTrash }]);
    });
  }
  document.querySelectorAll("#files-sidebar [data-file-place='Trash']").forEach((btn) => {
    btn.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      showDeskMenu(e.clientX, e.clientY, [
        { label: "Open Trash", run: async () => { window.DeskUI?.openWindow?.("files"); browseFiles("Trash"); } },
        { label: "Empty Trash", danger: true, run: emptyTrash },
      ]);
    });
  });
}

async function openDeskFile(b, f) {
  if (!b || !f || f.dir) return;
  const html = /\.html?$/i.test(f.path);
  if (html) {
    try {
      await api(`/v1/bots/${b.id}/browser/open`, {
        method: "POST",
        body: JSON.stringify({ path: f.path }),
      });
      window.DeskUI?.openWindow?.("browser");
      setSurface("browser");
    } catch {
      /* fall through to editor */
    }
    return;
  }
  if (f.kind === "image" || isImagePath(f.path)) {
    openMediaInPreview(b, f.path, "image");
    return;
  }
  if (f.kind === "video" || isVideoPath(f.path)) {
    openMediaInPreview(b, f.path, "video");
    return;
  }
  if (notepadCandidate(f.path)) {
    openNotepadFile(b, f.path).catch(() => {});
    return;
  }
  openEditorFile(b, f.path).catch(() => {});
}

function editorCandidate(f) {
  if (!f || f.dir) return false;
  if (f.kind === "image") return false;
  return !/\.(zip|gz|tgz|tar|pdf|mp4|mov|avi|webm|mp3|wav|ico|woff2?|ttf|bin|exe)$/i.test(f.path || "");
}

function renderEditorFiles(b, files) {
  const host = $("editor-file-list");
  if (!host) return;
  host.innerHTML = "";
  const list = (files || []).filter((f) => editorCandidate(f) && !f.path.startsWith(".git/") && !f.path.startsWith(".hermes/"));
  if (!list.length) {
    host.innerHTML = '<div class="minios-empty">No text/code files yet.</div>';
    return;
  }
  for (const f of list.slice(0, 300)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "minios-editor-file";
    btn.textContent = f.path;
    btn.title = f.path;
    btn.dataset.path = f.path;
    btn.onclick = () => openEditorFile(b, f.path);
    host.appendChild(btn);
  }
}

async function openEditorFile(b, path, suppliedText = null) {
  if (!b || !path) return;
  const editor = $("code-editor");
  const status = $("editor-status");
  if (!editor) return;
  window.DeskUI?.openWindow?.("editor");
  if (status) { status.className = "minios-editor-status"; status.textContent = `Opening ${path}…`; }
  let text = suppliedText;
  if (text == null) {
    const file = await api(`/v1/workspaces/${b.workspace_id}?file=${encodeURIComponent(path)}`);
    text = file.text || "";
  }
  state.editorPath = path;
  editor.value = text;
  editor.disabled = false;
  editor.dataset.clean = text;
  if ($("editor-path")) $("editor-path").textContent = path;
  if ($("editor-save")) $("editor-save").disabled = false;
  const isHtml = /\.html?$/i.test(path);
  if ($("editor-preview")) $("editor-preview").disabled = !isHtml;
  document.querySelectorAll(".minios-editor-file").forEach((el) => el.classList.toggle("active", el.dataset.path === path));
  if (status) status.textContent = `Editing real workspace file • ${text.length.toLocaleString()} characters`;
}

async function saveEditorFile() {
  const b = state.bots.find((x) => x.id === state.selected);
  const editor = $("code-editor");
  const status = $("editor-status");
  if (!b || !editor || !state.editorPath) return;
  if (status) { status.className = "minios-editor-status"; status.textContent = `Saving ${state.editorPath}…`; }
  try {
    await api(`/v1/workspaces/${b.workspace_id}/file`, {
      method: "POST",
      body: JSON.stringify({ path: state.editorPath, text: editor.value }),
    });
    editor.dataset.clean = editor.value;
    if (status) { status.className = "minios-editor-status ok"; status.textContent = `Saved ${state.editorPath}`; }
  } catch (e) {
    if (status) { status.className = "minios-editor-status error"; status.textContent = `Save failed: ${e.message}`; }
  }
}

function notepadCandidate(path) {
  return /\.(txt|md|markdown|rst|csv|log|ini|cfg|conf|text|nfo|asc)$/i.test(String(path || ""));
}

const notepadState = {
  path: "",
  clean: "",
  wrap: localStorage.getItem("minios-notepad-wrap") !== "0",
  font: Math.min(28, Math.max(11, Number(localStorage.getItem("minios-notepad-font")) || 14)),
  dialog: "",
  dialogDir: "Documents",
  dialogPick: null,
  dialogReplace: "",
  statusOn: localStorage.getItem("minios-notepad-status") !== "0",
};

const NOTEPAD_PLACES = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"];

function notepadBasename(path) {
  const clean = String(path || "").replace(/^\/+|\/+$/g, "");
  return clean.split("/").pop() || "Untitled";
}

function notepadJoin(dir, name) {
  const folder = String(dir || "").replace(/^\/+|\/+$/g, "");
  const file = String(name || "").replace(/^\/+|\/+$/g, "");
  return folder ? `${folder}/${file}` : file;
}

function notepadUntitledPath() {
  const used = new Set((state.files || []).map((f) => f.path));
  if (!used.has("Documents/Untitled.txt")) return "Documents/Untitled.txt";
  let n = 2;
  while (used.has(`Documents/Untitled ${n}.txt`)) n += 1;
  return `Documents/Untitled ${n}.txt`;
}

function notepadDirty() {
  const ta = $("notepad-editor");
  return Boolean(ta && ta.value !== notepadState.clean);
}

function notepadConfirmDiscard() {
  if (!notepadDirty()) return true;
  return window.confirm("Discard unsaved changes?");
}

function notepadSetStatus(text, kind = "") {
  const el = $("notepad-name");
  if (!el) return;
  if (text) {
    el.textContent = text;
    el.className = kind;
    return;
  }
  notepadUpdateStatus();
}

function notepadUpdateTitle() {
  const title = $("notepad-title");
  const name = notepadState.path ? notepadBasename(notepadState.path) : "Untitled";
  const dirty = notepadDirty() ? "*" : "";
  if (title) title.textContent = `Text Editor — ${name}${dirty}`;
  const label = $("notepad-name");
  if (label) {
    label.className = dirty ? "dirty" : "";
    label.textContent = dirty ? `${name} • unsaved` : (notepadState.path || "Untitled");
  }
}

function notepadUpdateStatus() {
  const ta = $("notepad-editor");
  if (!ta) return;
  const text = ta.value;
  const pos = ta.selectionStart || 0;
  const before = text.slice(0, pos);
  const line = before.split("\n").length;
  const col = before.length - before.lastIndexOf("\n");
  const words = (text.trim().match(/\S+/g) || []).length;
  if ($("notepad-pos")) $("notepad-pos").textContent = `Ln ${line}, Col ${col}`;
  if ($("notepad-counts")) $("notepad-counts").textContent = `${words.toLocaleString()} words • ${text.length.toLocaleString()} chars`;
  if ($("notepad-wrap-label")) $("notepad-wrap-label").textContent = notepadState.wrap ? "Wrap on" : "Wrap off";
  notepadUpdateTitle();
}

function notepadApplyPrefs() {
  const ta = $("notepad-editor");
  if (!ta) return;
  ta.style.fontSize = `${notepadState.font}px`;
  ta.classList.toggle("no-wrap", !notepadState.wrap);
  ta.wrap = notepadState.wrap ? "soft" : "off";
  const status = $("notepad-status");
  if (status) status.hidden = !notepadState.statusOn;
  localStorage.setItem("minios-notepad-wrap", notepadState.wrap ? "1" : "0");
  localStorage.setItem("minios-notepad-font", String(notepadState.font));
  localStorage.setItem("minios-notepad-status", notepadState.statusOn ? "1" : "0");
  notepadUpdateStatus();
}

function notepadCloseMenus() {
  document.querySelectorAll(".notepad-menu.open").forEach((el) => el.classList.remove("open"));
}

function notepadShowFind(mode) {
  const bar = $("notepad-find");
  if (!bar) return;
  bar.hidden = false;
  bar.dataset.mode = mode || "find";
  const focusId = mode === "goto" ? "notepad-goto" : mode === "replace" ? "notepad-find-q" : "notepad-find-q";
  $(focusId)?.focus();
  $(focusId)?.select?.();
}

function notepadHideFind() {
  const bar = $("notepad-find");
  if (bar) bar.hidden = true;
  $("notepad-editor")?.focus();
}

function notepadFind(dir = 1) {
  const ta = $("notepad-editor");
  const qEl = $("notepad-find-q");
  if (!ta || !qEl) return;
  const q = qEl.value;
  if (!q) return;
  const matchCase = Boolean($("notepad-find-case")?.checked);
  const hay = matchCase ? ta.value : ta.value.toLowerCase();
  const needle = matchCase ? q : q.toLowerCase();
  const from = dir > 0 ? ta.selectionEnd : Math.max(0, ta.selectionStart - 1);
  let idx = dir > 0 ? hay.indexOf(needle, from) : hay.lastIndexOf(needle, from);
  if (idx < 0) idx = dir > 0 ? hay.indexOf(needle) : hay.lastIndexOf(needle);
  if (idx < 0) {
    notepadSetStatus(`"${q}" not found`);
    return;
  }
  ta.focus();
  ta.setSelectionRange(idx, idx + q.length);
  notepadUpdateStatus();
}

function notepadReplaceOne() {
  const ta = $("notepad-editor");
  const q = $("notepad-find-q")?.value || "";
  const repl = $("notepad-repl")?.value ?? "";
  if (!ta || !q) return;
  const matchCase = Boolean($("notepad-find-case")?.checked);
  const selected = ta.value.slice(ta.selectionStart, ta.selectionEnd);
  const same = matchCase ? selected === q : selected.toLowerCase() === q.toLowerCase();
  if (same) ta.setRangeText(repl, ta.selectionStart, ta.selectionEnd, "end");
  notepadFind(1);
  notepadUpdateStatus();
}

function notepadReplaceAll() {
  const ta = $("notepad-editor");
  const q = $("notepad-find-q")?.value || "";
  const repl = $("notepad-repl")?.value ?? "";
  if (!ta || !q) return;
  const flags = $("notepad-find-case")?.checked ? "g" : "gi";
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), flags);
  const next = ta.value.replace(re, repl);
  const n = (ta.value.match(re) || []).length;
  ta.value = next;
  notepadUpdateStatus();
  notepadSetStatus(n ? `Replaced ${n} match${n === 1 ? "" : "es"}` : "No matches");
}

function notepadGoToLine() {
  const ta = $("notepad-editor");
  const n = Math.max(1, parseInt($("notepad-goto")?.value || "1", 10) || 1);
  if (!ta) return;
  const lines = ta.value.split("\n");
  const line = Math.min(n, lines.length);
  let pos = 0;
  for (let i = 0; i < line - 1; i++) pos += lines[i].length + 1;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  notepadUpdateStatus();
}

function notepadExec(cmd) {
  const ta = $("notepad-editor");
  if (!ta) return;
  ta.focus();
  try { document.execCommand(cmd); } catch { /* ignore */ }
  notepadUpdateStatus();
}

async function notepadPaste() {
  const ta = $("notepad-editor");
  if (!ta) return;
  ta.focus();
  try {
    const text = await navigator.clipboard.readText();
    ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
  } catch {
    notepadExec("paste");
  }
  notepadUpdateStatus();
}

function notepadInsertTime() {
  const ta = $("notepad-editor");
  if (!ta) return;
  ta.setRangeText(new Date().toLocaleString(), ta.selectionStart, ta.selectionEnd, "end");
  notepadUpdateStatus();
}

function notepadNew(force = false) {
  if (!force && !notepadConfirmDiscard()) return;
  const ta = $("notepad-editor");
  if (!ta) return;
  notepadState.path = "";
  state.notepadPath = "";
  notepadState.clean = "";
  ta.value = "";
  notepadHideDialog();
  notepadUpdateStatus();
  ta.focus();
}

function notepadHideDialog() {
  const dlg = $("notepad-dialog");
  if (dlg) dlg.hidden = true;
  notepadState.dialog = "";
  notepadState.dialogPick = null;
  notepadState.dialogReplace = "";
  notepadDialogError("");
}

function notepadDialogError(msg) {
  const el = $("notepad-dialog-error");
  if (!el) return;
  el.textContent = msg || "";
  el.hidden = !msg;
}

function notepadGoDir(dir) {
  notepadState.dialogDir = String(dir || "").replace(/^\/+|\/+$/g, "");
  notepadState.dialogPick = null;
  notepadState.dialogReplace = "";
  notepadDialogError("");
  renderNotepadDialog();
}

function notepadDialogItems() {
  const kids = filesInFolder(state.files, notepadState.dialogDir);
  if (notepadState.dialogDir) return kids;
  const names = new Set(kids.map((k) => k.name));
  for (const place of NOTEPAD_PLACES) {
    if (!names.has(place)) kids.push({ path: place, name: place, dir: true, kind: "dir" });
  }
  kids.sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name));
  return kids;
}

function notepadResolveDest(dir, name) {
  let file = String(name || "").trim().replace(/\\/g, "/");
  if (!file) return "";
  if (file.includes("..") || file.startsWith("/") || file.includes(":")) return null;
  file = file.replace(/^\/+|\/+$/g, "");
  if (file.includes("/")) {
    const first = file.split("/")[0];
    if (!dir || NOTEPAD_PLACES.includes(first)) return file;
    return notepadJoin(dir, file);
  }
  return notepadJoin(dir, file);
}

function renderNotepadDialog() {
  const list = $("notepad-dialog-list");
  const pathEl = $("notepad-dialog-path");
  if (!list) return;
  list.innerHTML = "";
  const dir = notepadState.dialogDir;
  if (pathEl) pathEl.textContent = prettyFilesPath(dir);
  document.querySelectorAll("#notepad-dialog-places [data-notepad-place]").forEach((btn) => {
    btn.classList.toggle("active", (btn.dataset.notepadPlace || "") === dir);
  });
  if (dir) {
    const up = document.createElement("div");
    up.className = "notepad-dialog-item is-dir";
    up.innerHTML = "<span>↩</span>..";
    up.title = "Parent folder";
    up.onclick = () => {
      const parts = dir.split("/").filter(Boolean);
      parts.pop();
      notepadGoDir(parts.join("/"));
    };
    list.appendChild(up);
  }
  const kids = notepadDialogItems();
  if (!kids.length) {
    const empty = document.createElement("div");
    empty.className = "notepad-dialog-item disabled";
    empty.textContent = "This folder is empty — pick a place on the left, then type a file name";
    list.appendChild(empty);
    return;
  }
  for (const f of kids) {
    const el = document.createElement("div");
    const openable = f.dir || notepadCandidate(f.path) || notepadState.dialog === "saveAs";
    el.className = "notepad-dialog-item" + (f.dir ? " is-dir" : "") + (openable ? "" : " disabled");
    el.innerHTML = `<span>${f.dir ? "📁" : "📄"}</span>${escapeHtml(f.name)}`;
    el.title = f.path;
    el.onclick = () => {
      if (f.dir) {
        notepadGoDir(f.path);
        return;
      }
      if (!openable) return;
      list.querySelectorAll(".selected").forEach((n) => n.classList.remove("selected"));
      el.classList.add("selected");
      notepadState.dialogPick = f;
      if ($("notepad-dialog-name")) $("notepad-dialog-name").value = f.name;
      notepadDialogError("");
    };
    el.ondblclick = () => {
      if (f.dir) notepadGoDir(f.path);
      else if (openable) {
        if ($("notepad-dialog-name")) $("notepad-dialog-name").value = f.name;
        notepadDialogConfirm();
      }
    };
    list.appendChild(el);
  }
}

function notepadShowDialog(mode) {
  const dlg = $("notepad-dialog");
  if (!dlg) return;
  notepadCloseMenus();
  notepadState.dialog = mode;
  notepadState.dialogPick = null;
  notepadState.dialogReplace = "";
  const currentDir = notepadState.path.includes("/") ? notepadState.path.split("/").slice(0, -1).join("/") : "Documents";
  notepadState.dialogDir = currentDir || "Documents";
  const title = $("notepad-dialog-title");
  const ok = $("notepad-dialog-ok");
  if (title) title.textContent = mode === "saveAs" ? "Save As" : "Open";
  if (ok) ok.textContent = mode === "saveAs" ? "Save" : "Open";
  if ($("notepad-dialog-name")) {
    $("notepad-dialog-name").value = notepadState.path
      ? notepadBasename(notepadState.path)
      : notepadBasename(notepadUntitledPath());
  }
  notepadDialogError("");
  dlg.hidden = false;
  renderNotepadDialog();
  requestAnimationFrame(() => {
    $("notepad-dialog-name")?.focus();
    $("notepad-dialog-name")?.select();
  });
}

async function notepadDialogConfirm() {
  const name = ($("notepad-dialog-name")?.value || "").trim();
  if (!name) {
    notepadDialogError("Type a file name, or click a folder to choose a location.");
    $("notepad-dialog-name")?.focus();
    return;
  }
  const destRaw = notepadResolveDest(notepadState.dialogDir, name);
  if (destRaw == null) {
    notepadDialogError("Use a name inside the workspace, without .. or / at the start.");
    return;
  }
  let dest = destRaw;
  if (notepadState.dialog === "open") {
    const b = currentBot();
    if (!b) {
      notepadDialogError("Select a bot first.");
      return;
    }
    const asDir = (state.files || []).some((f) => f.path === dest && f.dir);
    if (asDir) {
      notepadGoDir(dest);
      return;
    }
    try {
      await openNotepadFile(b, dest);
    } catch (e) {
      notepadDialogError(e.message || String(e));
    }
    return;
  }
  if (!/\.[A-Za-z0-9]+$/.test(dest)) dest += ".txt";
  await saveNotepadFile(dest, { fromDialog: true });
}

async function openNotepadFile(b, path) {
  b = b || currentBot();
  if (!b || !path) return;
  const ta = $("notepad-editor");
  if (!ta) return;
  if (notepadDirty() && notepadState.path !== path && !notepadConfirmDiscard()) return;
  window.DeskUI?.openWindow?.("notepad");
  notepadSetStatus(`Opening ${path}…`);
  let text = "";
  try {
    const file = await api(`/v1/workspaces/${b.workspace_id}?file=${encodeURIComponent(path)}`);
    text = file.text || "";
  } catch (e) {
    const msg = `Open failed: ${e.message}`;
    notepadSetStatus(msg, "dirty");
    if ($("notepad-dialog") && !$("notepad-dialog").hidden) notepadDialogError(msg);
    return;
  }
  notepadState.path = path;
  state.notepadPath = path;
  notepadState.clean = text;
  ta.value = text;
  notepadHideDialog();
  notepadApplyPrefs();
  ta.focus();
  notepadSetStatus(path);
}

async function saveNotepadFile(path = notepadState.path, opts = {}) {
  const fromDialog = Boolean(opts.fromDialog);
  const b = currentBot();
  const ta = $("notepad-editor");
  if (!ta) return false;
  const workspaceId = b?.workspace_id || b?.id || state.workspaceId;
  if (!b || !workspaceId) {
    const msg = "Select a bot to save";
    if (fromDialog) notepadDialogError(msg);
    else notepadSetStatus(msg, "dirty");
    return false;
  }
  if (!state.selected && b.id) state.selected = b.id;
  let dest = String(path || "").trim();
  if (!dest) {
    notepadShowDialog("saveAs");
    return false;
  }
  if (!/\.[A-Za-z0-9]+$/.test(dest)) dest += ".txt";
  const exists = (state.files || []).some((f) => f.path === dest && !f.dir);
  if (exists && dest !== notepadState.path) {
    if (fromDialog) {
      if (notepadState.dialogReplace !== dest) {
        notepadState.dialogReplace = dest;
        notepadDialogError(`"${notepadBasename(dest)}" already exists. Click Save again to replace it.`);
        return false;
      }
    } else if (!window.confirm(`Replace ${dest}?`)) {
      return false;
    }
  }
  if (fromDialog) notepadDialogError("");
  notepadSetStatus(`Saving ${dest}…`);
  try {
    await api(`/v1/workspaces/${workspaceId}/file`, {
      method: "POST",
      body: JSON.stringify({ path: dest, text: ta.value }),
    });
    notepadState.path = dest;
    state.notepadPath = dest;
    notepadState.clean = ta.value;
    notepadUpdateStatus();
    notepadSetStatus(`Saved ${dest}`);
    if (fromDialog) notepadHideDialog();
    await loadWorkspace(b);
    return true;
  } catch (e) {
    const msg = `Save failed: ${e.message}`;
    if (fromDialog) notepadDialogError(msg);
    notepadSetStatus(msg, "dirty");
    return false;
  }
}

function notepadCommand(cmd) {
  notepadCloseMenus();
  if (cmd === "new") notepadNew();
  else if (cmd === "open") notepadShowDialog("open");
  else if (cmd === "save") saveNotepadFile();
  else if (cmd === "saveAs") notepadShowDialog("saveAs");
  else if (cmd === "exit") {
    if (!notepadConfirmDiscard()) return;
    if (notepadDirty()) notepadNew(true);
    const win = document.querySelector('.app-window[data-window-app="notepad"]');
    if (win) window.DeskUI?.closeWindow?.(win);
  }
  else if (cmd === "undo") notepadExec("undo");
  else if (cmd === "redo") notepadExec("redo");
  else if (cmd === "cut") notepadExec("cut");
  else if (cmd === "copy") notepadExec("copy");
  else if (cmd === "paste") notepadPaste();
  else if (cmd === "delete") notepadExec("delete");
  else if (cmd === "selectAll") notepadExec("selectAll");
  else if (cmd === "find") notepadShowFind("find");
  else if (cmd === "findNext") { notepadShowFind("find"); notepadFind(1); }
  else if (cmd === "findPrev") notepadFind(-1);
  else if (cmd === "replace") notepadShowFind("replace");
  else if (cmd === "replaceOne") notepadReplaceOne();
  else if (cmd === "replaceAll") notepadReplaceAll();
  else if (cmd === "goto") notepadShowFind("goto");
  else if (cmd === "gotoGo") notepadGoToLine();
  else if (cmd === "findClose") notepadHideFind();
  else if (cmd === "timeDate") notepadInsertTime();
  else if (cmd === "wrap") { notepadState.wrap = !notepadState.wrap; notepadApplyPrefs(); }
  else if (cmd === "fontUp") { notepadState.font = Math.min(28, notepadState.font + 1); notepadApplyPrefs(); }
  else if (cmd === "fontDown") { notepadState.font = Math.max(11, notepadState.font - 1); notepadApplyPrefs(); }
  else if (cmd === "status") { notepadState.statusOn = !notepadState.statusOn; notepadApplyPrefs(); }
}

function wireNotepad() {
  const win = document.querySelector('.app-window[data-window-app="notepad"]');
  const ta = $("notepad-editor");
  if (!win || !ta || win.dataset.notepadWired === "true") return;
  win.dataset.notepadWired = "true";
  notepadApplyPrefs();
  notepadNew(true);
  win.querySelector('[data-window-action="close"]')?.addEventListener("click", (e) => {
    if (!notepadConfirmDiscard()) {
      e.preventDefault();
      e.stopImmediatePropagation();
      return;
    }
    if (notepadDirty()) notepadNew(true);
  }, true);
  win.querySelectorAll("[data-notepad-menu]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const menu = btn.closest(".notepad-menu");
      const wasOpen = menu?.classList.contains("open");
      notepadCloseMenus();
      if (menu && !wasOpen) menu.classList.add("open");
    });
  });
  win.querySelectorAll("[data-notepad-cmd]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      notepadCommand(btn.dataset.notepadCmd);
    });
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest?.(".notepad-menubar")) notepadCloseMenus();
  });
  ta.addEventListener("input", notepadUpdateStatus);
  ta.addEventListener("keyup", notepadUpdateStatus);
  ta.addEventListener("click", notepadUpdateStatus);
  ta.addEventListener("select", notepadUpdateStatus);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      ta.setRangeText("\t", ta.selectionStart, ta.selectionEnd, "end");
      notepadUpdateStatus();
    }
  });
  win.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("notepad-dialog")?.hidden) { e.preventDefault(); e.stopPropagation(); notepadHideDialog(); return; }
      if (!$("notepad-find")?.hidden) { e.preventDefault(); e.stopPropagation(); notepadHideFind(); return; }
      if (win.querySelector(".notepad-menu.open")) { e.preventDefault(); e.stopPropagation(); notepadCloseMenus(); return; }
      return;
    }
    if (e.key === "F3") { e.preventDefault(); notepadFind(e.shiftKey ? -1 : 1); return; }
    if (e.key === "F5") { e.preventDefault(); notepadInsertTime(); return; }
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const k = e.key.toLowerCase();
    if (k === "n") { e.preventDefault(); notepadNew(); }
    else if (k === "o") { e.preventDefault(); if ($("notepad-dialog")?.hidden) notepadShowDialog("open"); }
    else if (k === "s") {
      e.preventDefault();
      if (!$("notepad-dialog")?.hidden) notepadDialogConfirm();
      else if (e.shiftKey) notepadShowDialog("saveAs");
      else saveNotepadFile();
    }
    else if (k === "f") { e.preventDefault(); notepadShowFind("find"); }
    else if (k === "h") { e.preventDefault(); notepadShowFind("replace"); }
    else if (k === "g") { e.preventDefault(); notepadShowFind("goto"); }
    else if (k === "=" || k === "+") { e.preventDefault(); notepadCommand("fontUp"); }
    else if (k === "-") { e.preventDefault(); notepadCommand("fontDown"); }
  });
  $("notepad-dialog")?.addEventListener("click", (e) => e.stopPropagation());
  $("notepad-dialog")?.addEventListener("pointerdown", (e) => e.stopPropagation());
  document.querySelectorAll("#notepad-dialog-places [data-notepad-place]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      notepadGoDir(btn.dataset.notepadPlace || "");
    });
  });
  $("notepad-dialog-ok")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    notepadDialogConfirm().catch((err) => notepadDialogError(err.message || String(err)));
  });
  $("notepad-dialog-cancel")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    notepadHideDialog();
  });
  $("notepad-dialog-name")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      notepadDialogConfirm().catch((err) => notepadDialogError(err.message || String(err)));
    }
  });
  $("notepad-find-q")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); notepadFind(e.shiftKey ? -1 : 1); }
  });
  $("notepad-repl")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); notepadReplaceOne(); }
  });
  $("notepad-goto")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); notepadGoToLine(); }
  });
}

window.deskOpenNotepad = openNotepadFile;

const ROBOT_SIMULATOR_SRC = "/ui/robot-simulator.html?v=141";

let catrinaMeshBuf = null;
let catrinaMeshPromise = null;
function ensureCatrinaMesh() {
  if (catrinaMeshBuf) return Promise.resolve(catrinaMeshBuf);
  if (catrinaMeshPromise) return catrinaMeshPromise;
  catrinaMeshPromise = fetch("/ui/las-catrina.bin.gz?v=62")
    .then((r) => {
      if (!r.ok) throw new Error("mesh " + r.status);
      return r.arrayBuffer();
    })
    .then((buf) => {
      catrinaMeshBuf = buf;
      return buf;
    })
    .catch((err) => {
      catrinaMeshPromise = null;
      throw err;
    });
  return catrinaMeshPromise;
}

function sendCatrinaMesh() {
  const iframe = $("app-preview-frame");
  if (!iframe || !robotIframeReady() || !catrinaMeshBuf) return;
  try {
    iframe.contentWindow?.postMessage({ type: "catrina-mesh", buf: catrinaMeshBuf.slice(0) }, "*");
  } catch {
    /* sandbox may still be loading */
  }
}

function loadRobotSimulator() {
  if (!selectedBotHasRobotSimulator()) return;
  state.previewPath = "";
  const iframe = $("app-preview-frame");
  if (iframe) {
    iframe.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-downloads");
    iframe.src = ROBOT_SIMULATOR_SRC;
  }
  $("app-preview-frame")?.closest(".minios-preview-window")?.classList.add("robot-mode");
  if ($("preview-path")) $("preview-path").textContent = "Teela Robot Body Simulator";
  if ($("preview-browser")) $("preview-browser").disabled = true;
  ensureCatrinaMesh().then(() => sendCatrinaMesh()).catch(() => {});
}
window.loadRobotSimulator = loadRobotSimulator;

let hydrateGen = 0;
let robotCommandGen = 0;
let suppressWaveEchoUntil = 0;

function leftWaveLocally() {
  return Date.now() < suppressWaveEchoUntil;
}

async function hydrateRobotIframe() {
  const iframe = $("app-preview-frame");
  if (!iframe || !robotIframeReady()) return;
  if (!selectedBotHasRobotSimulator()) return;
  if (observerBotId) return;
  const gen = ++hydrateGen;
  let payload = null;
  if (state.selected) {
    try {
      const st = await api(`/v1/bots/${state.selected}/body/state`);
      if (gen !== hydrateGen) return;
      const joints = {};
      const src = st?.joints || {};
      Object.keys(src).forEach((k) => {
        const rec = src[k];
        joints[k] = (rec && typeof rec === "object" && rec.actual != null) ? rec.actual : rec;
      });
      if (st?.motion === "walking") {
        payload = { cmd: "walk", pose: "walk-cycle", motion: "walking", seq: st.revision, direction: st.walk_direction || "place" };
      } else if (Object.keys(joints).length) {
        payload = {
          cmd: "joints",
          joints,
          live: joints,
          pose: st.pose,
          motion: st.motion || "idle",
          waving: !!st.waving,
          seq: st.revision,
        };
      }
    } catch {
      /* keep last command */
    }
  }
  if (gen !== hydrateGen) return;
  if (!payload) payload = state.lastRobotCommand;
  if (!payload || payload.cmd === "status" || payload.cmd === "state") return;
  if ((payload.pose === "wave" || payload.motion === "waving") && leftWaveLocally()) return;
  const send = () => {
    try {
      iframe.contentWindow?.postMessage({ ...payload, source: "hydrate", type: "robot-command" }, "*");
    } catch {
      /* ignore */
    }
  };
  send();
}

function robotIframeReady() {
  const iframe = $("app-preview-frame");
  return !!(iframe && String(iframe.src || "").includes("robot-simulator.html"));
}

function postRobotCommand(msg) {
  if (!selectedBotHasRobotSimulator()) return;
  if (!robotIframeReady()) loadRobotSimulator();
  const iframe = $("app-preview-frame");
  if (!iframe) return;
  persistRobotEditGen++;
  const payload = { ...msg, source: "hermes-desk", type: "robot-command" };
  const gen = ++robotCommandGen;
  state.lastRobotCommand = payload;
  const send = () => {
    if (gen !== robotCommandGen) return; // superseded by a later robot command
    try {
      iframe.contentWindow?.postMessage(payload, "*");
    } catch {
      /* sandbox may still be loading */
    }
  };
  send();
  iframe.addEventListener("load", send, { once: true });
  setTimeout(send, 80);
  setTimeout(send, 250);
}

let persistRobotEditGen = 0;
let persistRobotEditTail = Promise.resolve();

async function persistRobotEdit(data) {
  if (!data) return;
  const liveOnly = String(data.cmd || "") === "live";
  // Observer still posts live 3D telemetry so chat can see the twin when the
  // main UI iframe is hidden. It must not persist slider/pose edits.
  if (observerBotId && !liveOnly) return;
  const mine = liveOnly ? persistRobotEditGen : ++persistRobotEditGen;
  if (!liveOnly) {
    hydrateGen++;
    robotCommandGen++;
    const pose = String(data.pose || "");
    const motion = String(data.motion || "");
    if (pose === "wave" || motion === "waving") suppressWaveEchoUntil = 0;
    else suppressWaveEchoUntil = Date.now() + 12000;
    state.lastRobotCommand = {
      type: "robot-command",
      source: "UI",
      cmd: data.cmd || "joints",
      pose: data.pose,
      joints: data.joints,
      motion: data.motion,
      seq: data.seq,
    };
  }
  const bid = state.selected || observerBotId;
  if (!bid) return;
  persistRobotEditTail = persistRobotEditTail.then(async () => {
    if (!liveOnly && mine !== persistRobotEditGen) return;
    try {
      await api(`/v1/bots/${bid}/desktop/action`, {
        method: "POST",
        body: JSON.stringify({
          action: "robot",
          cmd: data.cmd || "joints",
          pose: data.pose,
          joints: data.joints,
          live: data.live,
          motion: data.motion,
          waving: data.waving,
          phase: data.phase,
        }),
      });
    } catch {
      if (liveOnly) {
        try {
          $("app-preview-frame")?.contentWindow?.postMessage({ type: "robot-live-nack" }, "*");
        } catch {
          /* ignore */
        }
      }
    }
  });
  await persistRobotEditTail;
}

let virtualBodyWs = null;
let virtualBodyWsBid = "";
function virtualBodyWsUrl(bid) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/v1/bots/${bid}/virtual-body/ws`;
}
function connectVirtualBodyWs(bid) {
  if (!bid) return;
  const bot = state.bots.find((x) => x.id === bid);
  if (bot && !botHasRobotSimulator(bot)) return;
  if (virtualBodyWs && virtualBodyWsBid === bid && virtualBodyWs.readyState <= 1) return;
  try { virtualBodyWs?.close(); } catch { /* ignore */ }
  virtualBodyWsBid = bid;
  let ws;
  try {
    ws = new WebSocket(virtualBodyWsUrl(bid));
  } catch {
    return;
  }
  virtualBodyWs = ws;
  ws.onmessage = (ev) => {
    let msg = {};
    try { msg = JSON.parse(ev.data || "{}"); } catch { return; }
    if (msg.type === "body_action" || msg.type === "virtual-body-action") {
      $("app-preview-frame")?.contentWindow?.postMessage({
        type: "virtual-body-action",
        action_id: msg.action_id,
        skill: msg.skill,
        parameters: msg.parameters || msg,
      }, "*");
    }
  };
  ws.onclose = () => {
    if (virtualBodyWs === ws) virtualBodyWs = null;
    setTimeout(() => connectVirtualBodyWs(state.selected), 1500);
  };
}
function sendVirtualBodyToServer(obj) {
  if (virtualBodyWs && virtualBodyWs.readyState === 1) {
    try { virtualBodyWs.send(JSON.stringify(obj)); return; } catch { /* fall through */ }
  }
  const bid = state.selected;
  if (!bid) return;
  api(`/v1/bots/${bid}/virtual-body/result`, {
    method: "POST",
    body: JSON.stringify(obj),
  }).catch(() => {});
}

window.addEventListener("message", (e) => {
  const data = e.data || {};
  if (data.type === "action_result" || data.type === "body_state") {
    sendVirtualBodyToServer(data);
    return;
  }
  if (data.type === "robot-edit") {
    persistRobotEdit(data);
    return;
  }
  if (data.type === "catrina-mesh-request") {
    ensureCatrinaMesh().then(() => sendCatrinaMesh()).catch(() => {});
    return;
  }
  if (data.type === "robot-ready" || data.type === "robot-pull") {
    if (data.type === "robot-ready") ensureCatrinaMesh().then(() => sendCatrinaMesh()).catch(() => {});
    hydrateRobotIframe();
    postMicSettingsToSim();
  }
  if (data.type === "mic-settings") {
    applyMicSettings(data);
    return;
  }
  if (data.type === "mic-settings-request") {
    postMicSettingsToSim();
  }
});
$("app-preview-frame")?.addEventListener("load", () => {
  if (robotIframeReady()) {
    hydrateRobotIframe();
    ensureCatrinaMesh().then(() => sendCatrinaMesh()).catch(() => {});
  }
});
ensureCatrinaMesh().catch(() => {});

function isImagePath(path) {
  return /\.(png|jpe?g|gif|webp|heic|heif|svg)(\?|$)/i.test(String(path || ""));
}

function isVideoPath(path) {
  return /\.(mp4|webm|mov|m4v|avi|mkv)(\?|$)/i.test(String(path || ""));
}

function openMediaInPreview(b, path, kind) {
  if (!b || !path) return;
  window.DeskUI?.openWindow?.("preview");
  const frame = $("app-preview-frame");
  const src = fileUrl(b.workspace_id, path);
  if (frame) {
    frame.closest(".minios-preview-window")?.classList.remove("robot-mode");
    if (kind === "video" || isVideoPath(path)) {
      frame.removeAttribute("sandbox");
      const safe = String(src).replace(/"/g, "&quot;");
      frame.srcdoc = `<!doctype html><html><body style="margin:0;background:#111;display:flex;align-items:center;justify-content:center;height:100%"><video controls autoplay src="${safe}" style="max-width:100%;max-height:100%"></video></body></html>`;
    } else {
      frame.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-downloads");
      frame.removeAttribute("srcdoc");
      frame.src = src;
    }
  }
  if ($("preview-path")) $("preview-path").textContent = path;
}

function openPreview(path = state.editorPath) {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b || !path || !/\.html?$/i.test(path)) return;
  state.previewPath = path;
  const iframe = $("app-preview-frame");
  if (iframe) {
    iframe.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-downloads");
    iframe.src = fileUrl(b.workspace_id, path);
  }
  if ($("preview-path")) $("preview-path").textContent = path;
  if ($("preview-browser")) $("preview-browser").disabled = false;
  iframe?.closest(".minios-preview-window")?.classList.remove("robot-mode");
  window.DeskUI?.openWindow?.("preview");
}

function renderDevSystem(dev) {
  const commands = dev?.commands || {};
  state.dev = dev || { kind: "generic", commands: {} };
  if ($("dev-kind")) $("dev-kind").textContent = dev?.kind || "generic";
  for (const key of ["build", "test", "run"]) {
    const btn = $(`dev-${key}`);
    if (btn) {
      btn.disabled = key === "run" ? (!commands[key] && !dev?.process?.running) : !commands[key];
      btn.title = commands[key] || `No ${key} command detected`;
    }
  }
  if ($("dev-run")) {
    $("dev-run").textContent = dev?.process?.running ? "■ Stop App" : "▶ Run App";
    $("dev-run").classList.toggle("running", !!dev?.process?.running);
  }
  if (dev?.process?.output && $("dev-output") && !$("dev-output").textContent.trim()) {
    $("dev-output").textContent = `> ${dev.process.command}\n[${dev.process.running ? `running • pid ${dev.process.pid}` : `exit ${dev.process.returncode ?? "?"}`}]\n\n${dev.process.output}`;
  }
  if ($("dev-command") && !$("dev-command").value) $("dev-command").value = commands.test || commands.build || "";
}

async function runDevCommand(command, preset = "") {
  const b = state.bots.find((x) => x.id === state.selected);
  const out = $("dev-output");
  if (!b) return;
  window.DeskUI?.openWindow?.("dev");
  if (out) { out.className = "minios-dev-output"; out.textContent = `> ${command || preset}\n\nRunning…`; }
  try {
    const res = await api(`/v1/bots/${b.id}/dev/run`, {
      method: "POST",
      body: JSON.stringify({ command: command || "", preset, timeout: 120 }),
    });
    if (out) {
      out.className = `minios-dev-output ${res.ok ? "pass" : "fail"}`;
      out.textContent = `> ${res.command}\n[exit ${res.returncode} • ${res.elapsed}s]\n\n${res.output || "(no output)"}`;
    }
    return res;
  } catch (e) {
    if (out) { out.className = "minios-dev-output fail"; out.textContent = `Command failed: ${e.message}`; }
  }
}

async function toggleDevServer() {
  const b = state.bots.find((x) => x.id === state.selected);
  const out = $("dev-output");
  if (!b) return;
  window.DeskUI?.openWindow?.("dev");
  try {
    if (state.dev?.process?.running) {
      if (out) out.textContent = `> ${state.dev.process.command}\n\nStopping…`;
      const res = await api(`/v1/bots/${b.id}/dev/stop`, { method: "POST", body: "{}" });
      state.dev.process = res;
      renderDevSystem(state.dev);
      if (out) out.textContent = `> ${res.command || "app"}\n[stopped]\n\n${res.output || ""}`;
      return;
    }
    const command = state.dev?.commands?.run || "";
    if (!command) return;
    if (out) { out.className = "minios-dev-output"; out.textContent = `> ${command}\n\nStarting…`; }
    const res = await api(`/v1/bots/${b.id}/dev/start`, {
      method: "POST",
      body: JSON.stringify({ command, preset: "run" }),
    });
    state.dev.process = res;
    renderDevSystem(state.dev);
    if (out) out.textContent = `> ${res.command}\n[running • pid ${res.pid}]\n\n${res.output || "Waiting for output…"}`;
  } catch (e) {
    if (out) { out.className = "minios-dev-output fail"; out.textContent = `App process failed: ${e.message}`; }
  }
}

let agentCursorIdleTimer = 0;
const AGENT_CURSOR_IDLE_MS = 2000;

function hideAgentDesktopCursor() {
  const cursor = $("agentDesktopCursor");
  if (!cursor) return;
  clearTimeout(agentCursorIdleTimer);
  agentCursorIdleTimer = 0;
  cursor.classList.remove("is-active", "clicking");
  cursor.setAttribute("aria-hidden", "true");
  setTimeout(() => {
    if (cursor && !cursor.classList.contains("is-active")) cursor.hidden = true;
  }, 200);
}

function scheduleAgentCursorHide() {
  clearTimeout(agentCursorIdleTimer);
  agentCursorIdleTimer = setTimeout(hideAgentDesktopCursor, AGENT_CURSOR_IDLE_MS);
}

function moveAgentDesktopCursor(x, y, click = false, reveal = true) {
  const area = document.querySelector(".ubuntu-desktop-area");
  const cursor = $("agentDesktopCursor");
  if (!area || !cursor) return null;
  const nx = Math.max(0, Math.min(1000, Number(x) || 0));
  const ny = Math.max(0, Math.min(1000, Number(y) || 0));
  cursor.style.left = `${(nx / 1000) * area.clientWidth}px`;
  cursor.style.top = `${(ny / 1000) * area.clientHeight}px`;
  if (reveal) {
    cursor.hidden = false;
    cursor.setAttribute("aria-hidden", "false");
    cursor.classList.add("is-active");
    if (click) {
      cursor.classList.remove("clicking");
      void cursor.offsetWidth;
      cursor.classList.add("clicking");
    }
    scheduleAgentCursorHide();
  }
  return { area, clientX: area.getBoundingClientRect().left + (nx / 1000) * area.clientWidth, clientY: area.getBoundingClientRect().top + (ny / 1000) * area.clientHeight };
}

function agentBrowserClick(pos, clickCount = 1) {
  if (!pos) return;
  const mapped = mapBrowserXY({ clientX: pos.clientX, clientY: pos.clientY });
  setSurface("browser");
  grabScreenKeys();
  enqueueBrowser({ type: "mouseMoved", x: mapped.x, y: mapped.y, modifiers: 0 });
  enqueueBrowser({ type: "mousePressed", x: mapped.x, y: mapped.y, button: "left", clickCount, modifiers: 0 });
  enqueueBrowser({ type: "mouseReleased", x: mapped.x, y: mapped.y, button: "left", clickCount, modifiers: 0 });
}

function moveCursorToElement(el, click = false) {
  const area = document.querySelector(".ubuntu-desktop-area");
  if (!area || !el) return null;
  const ar = area.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const x = ((r.left + r.width / 2 - ar.left) / Math.max(1, ar.width)) * 1000;
  const y = ((r.top + r.height / 2 - ar.top) / Math.max(1, ar.height)) * 1000;
  return moveAgentDesktopCursor(x, y, click);
}

function semanticWindowAction(action, app) {
  const safeApp = String(app || "");
  const win = document.querySelector(`.app-window[data-window-app="${CSS.escape(safeApp)}"]`);
  if (!win) return;
  if (action === "focus_window") {
    moveCursorToElement(win.querySelector(".desktop-window-titlebar") || win);
    window.DeskUI?.focusWindow?.(win);
    return;
  }
  const control = action.replace("_window", "");
  const btn = win.querySelector(`[data-window-action="${control}"]`);
  if (btn) moveCursorToElement(btn, true);
  if (action === "minimize_window") window.DeskUI?.minimizeWindow?.(win);
  else if (action === "maximize_window") window.DeskUI?.maximizeWindow?.(win);
  else if (action === "close_window") window.DeskUI?.closeWindow?.(win);
}

function miniosViewState() {
  const viewer = document.querySelector("#ubuntuDesktopViewer");
  const area = document.querySelector(".ubuntu-desktop-area") || viewer;
  const r = (viewer || area)?.getBoundingClientRect();
  const dock = document.getElementById("dock");
  const dockEdge = ["left", "right", "top", "bottom"].find((edge) => dock?.classList.contains(`dock-${edge}`)) || "left";
  const windows = [...document.querySelectorAll(".app-window[data-window-app]")].map((w) => ({
    app: w.dataset.windowApp,
    open: w.dataset.open === "true" && !w.classList.contains("hidden-window"),
    minimized: w.classList.contains("minimized-window"),
    maximized: w.classList.contains("maximized-window"),
    focused: w.classList.contains("focused-window"),
  }));
  return {
    width: Math.round(r?.width || 0),
    height: Math.round(r?.height || 0),
    surface: state.surface,
    dock: dockEdge,
    wallpaper: area?.dataset.wallpaper || "",
    wallpaperStyle: area?.style?.background || "",
    windows,
  };
}

async function postMiniosView() {
  if (!state.selected || observerBotId) return;
  try {
    await api(`/v1/bots/${state.selected}/desktop/view`, {
      method: "POST",
      body: JSON.stringify(miniosViewState()),
    });
  } catch {
    /* ignore */
  }
}
window.postMiniosView = postMiniosView;

function applyDesktopAction(msg) {
  if (!msg || msg.bot_id !== state.selected) return;
  if (msg.action === "robot") {
    const cmd = String(msg.cmd || msg.command || "").toLowerCase();
    // Status polls emit desktop.action with the server's last pose. That must
    // not replay Wave after Arms Forward / Stop (teela setGesture waving=false).
    if (cmd === "status" || cmd === "state" || cmd === "live" || cmd === "telemetry" || cmd === "") return;
    if (cmd === "stop" || cmd === "stop_demo" || cmd === "neutral") {
      postRobotCommand({ cmd: "stop", pose: "neutral", motion: "idle", seq: msg.seq });
      return;
    }
    const pose = String(msg.pose || msg.name || "");
    if (pose === "wave" || msg.motion === "waving") {
      const last = state.lastRobotCommand || {};
      const local = String(last.pose || "");
      const localSrc = String(last.source || "");
      // Wave Right already started the overlay locally; don't replay it.
      if (local === "wave" && (localSrc === "UI" || localSrc === "hermes-desk")) return;
      const incomingSeq = Number(msg.seq);
      const localSeq = Number(last.seq);
      // Stale persist of an old wave must not restart after Arms Forward / Stop.
      if (local && local !== "wave") {
        if (Number.isFinite(incomingSeq) && Number.isFinite(localSeq) && incomingSeq <= localSeq) return;
        if (!Number.isFinite(incomingSeq)) return;
      }
      if (msg.motion === "idle") return;
      postRobotCommand({ cmd: "pose", pose: "wave", motion: "waving", seq: msg.seq });
      return;
    }
    const last = state.lastRobotCommand || {};
    const local = String(last.pose || "");
    const localSrc = String(last.source || "");
    if ((pose === "left_leg_out" || pose === "right_leg_out"
        || pose === "left_leg_raise" || pose === "right_leg_raise"
        || pose === "kneel_left" || pose === "kneel_right" || pose === "kneel_both")
      && pose === local
      && (localSrc === "UI" || localSrc === "hermes-desk")) {
      return;
    }
    postRobotCommand(msg);
    return;
  }
  if (msg.action === "open_app") {
    const app = String(msg.app || "");
    const icon = document.querySelector(`.dock-app[data-desktop-app="${CSS.escape(app)}"]`);
    if (icon) moveCursorToElement(icon, true);
    window.DeskUI?.openWindow?.(app);
    return;
  }
  if (["focus_window", "minimize_window", "maximize_window", "close_window"].includes(msg.action)) {
    semanticWindowAction(msg.action, String(msg.app || ""));
    return;
  }
  if (msg.action === "open_file") {
    const b = state.bots.find((x) => x.id === state.selected);
    const path = String(msg.path || "");
    const kind = String(msg.kind || "") || (isImagePath(path) ? "image" : isVideoPath(path) ? "video" : notepadCandidate(path) ? "notepad" : "file");
    if (b && path && (kind === "image" || kind === "video")) {
      const icon = document.querySelector('.dock-app[data-desktop-app="preview"]');
      if (icon) moveCursorToElement(icon, true);
      openMediaInPreview(b, path, kind);
      return;
    }
    const notepad = kind === "notepad" || notepadCandidate(path);
    const icon = document.querySelector(`.dock-app[data-desktop-app="${notepad ? "notepad" : "editor"}"]`);
    if (icon) moveCursorToElement(icon, true);
    if (b && path) {
      if (notepad) openNotepadFile(b, path).catch(() => {});
      else openEditorFile(b, path).catch(() => {});
    }
    return;
  }
  if (msg.action === "open_preview") {
    const b = state.bots.find((x) => x.id === state.selected);
    const icon = document.querySelector('.dock-app[data-desktop-app="preview"]');
    if (icon) moveCursorToElement(icon, true);
    if (b && msg.path) openEditorFile(b, String(msg.path)).then(() => openPreview(String(msg.path))).catch(() => {});
    return;
  }
  if (msg.action === "scroll") {
    const pos = moveAgentDesktopCursor(500, 500, false);
    if (state.surface === "browser" && pos) {
      const mapped = mapBrowserXY({ clientX:pos.clientX, clientY:pos.clientY });
      enqueueBrowser({ type:"mouseWheel", x:mapped.x, y:mapped.y, deltaX:Number(msg.dx)||0, deltaY:Number(msg.dy)||0, modifiers:0 });
    } else {
      document.querySelector('.app-window.focused-window .desktop-window-content')?.scrollBy?.({ left:Number(msg.dx)||0, top:Number(msg.dy)||0, behavior:"smooth" });
    }
    return;
  }
  const isClick = msg.action === "click" || msg.action === "double_click";
  const pos = moveAgentDesktopCursor(msg.x, msg.y, isClick);
  if (isClick && pos) {
    const target = document.elementFromPoint(pos.clientX, pos.clientY);
    if (target?.id === "browser-hit" || target?.id === "browser-keys" || target?.closest?.("#browser-stage")) {
      agentBrowserClick(pos, msg.action === "double_click" ? 2 : 1);
    } else {
      const clickable = target?.closest?.("button,a,input,textarea,select,[role=button],.desk-icon,.desktop-icon") || target;
      clickable?.focus?.({ preventScroll:true });
      if (msg.action === "double_click") {
        clickable?.click?.();
        clickable?.click?.();
        clickable?.dispatchEvent?.(new MouseEvent("dblclick", { bubbles:true, cancelable:true, clientX:pos.clientX, clientY:pos.clientY, button:0 }));
      } else {
        clickable?.click?.();
      }
    }
  } else if (msg.action === "type_text") {
    const text = String(msg.text || "");
    const wantNotepad = String(msg.app || "") === "notepad" || state.surface === "notepad";
    if (wantNotepad) {
      window.DeskUI?.openWindow?.("notepad");
      const ta = $("notepad-editor");
      if (ta) {
        ta.focus();
        const start = ta.selectionStart ?? ta.value.length;
        const end = ta.selectionEnd ?? start;
        ta.setRangeText(text, start, end, "end");
        ta.dispatchEvent(new Event("input", { bubbles:true }));
        return;
      }
    }
    const active = document.activeElement;
    if (active && (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement)) {
      const start = active.selectionStart ?? active.value.length;
      const end = active.selectionEnd ?? start;
      active.setRangeText(text, start, end, "end");
      active.dispatchEvent(new Event("input", { bubbles:true }));
    }
  }
}

function tickClock() {
  const el = $("task-clock");
  if (!el) return;
  el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const CHAT_IMAGE_MAX_EDGE = 2048;
const CHAT_VIDEO_MAX_BYTES = 40 * 1024 * 1024;

function guessMediaMime(file) {
  const typed = String(file?.type || "").split(";")[0].trim().toLowerCase();
  if (typed) return typed;
  const n = String(file?.name || "").toLowerCase();
  if (n.endsWith(".png")) return "image/png";
  if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
  if (n.endsWith(".webp")) return "image/webp";
  if (n.endsWith(".gif")) return "image/gif";
  if (n.endsWith(".heic")) return "image/heic";
  if (n.endsWith(".heif")) return "image/heif";
  if (n.endsWith(".mp4") || n.endsWith(".m4v")) return "video/mp4";
  if (n.endsWith(".mov")) return "video/quicktime";
  if (n.endsWith(".webm")) return "video/webm";
  return "";
}

function isMediaFile(file) {
  if (!file) return false;
  const mime = guessMediaMime(file);
  if (mime.startsWith("image/") || mime.startsWith("video/")) return true;
  return !mime && Number(file.size) > 32;
}

function mediaFilesFromClipboard(dt) {
  if (!dt) return [];
  const out = [];
  const seen = new Set();
  const add = (file) => {
    if (!file || seen.has(file) || !isMediaFile(file)) return;
    seen.add(file);
    out.push(file);
  };
  for (const file of dt.files || []) add(file);
  for (const item of dt.items || []) {
    if (item.kind === "file") add(item.getAsFile());
  }
  return out;
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      const comma = text.indexOf(",");
      resolve(comma >= 0 ? text.slice(comma + 1) : text);
    };
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsDataURL(blob);
  });
}

function fitChatImageSize(width, height, maxEdge) {
  const w = Number(width) || 0;
  const h = Number(height) || 0;
  if (w <= 0 || h <= 0) return { w: maxEdge, h: maxEdge };
  const scale = Math.min(1, maxEdge / Math.max(w, h));
  return { w: Math.max(1, Math.round(w * scale)), h: Math.max(1, Math.round(h * scale)) };
}

async function bitmapFromFile(file) {
  if (window.createImageBitmap) {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      try {
        return await createImageBitmap(file);
      } catch {
        /* fall through to HTMLImageElement */
      }
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("image load failed"));
      el.src = url;
    });
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function canvasToBlob(canvas, mime, quality) {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), mime, quality);
  });
}

async function prepareChatImage(file, mime) {
  try {
    const bmp = await bitmapFromFile(file);
    const srcW = bmp.width || bmp.naturalWidth || 0;
    const srcH = bmp.height || bmp.naturalHeight || 0;
    const { w, h } = fitChatImageSize(srcW, srcH, CHAT_IMAGE_MAX_EDGE);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bmp, 0, 0, w, h);
    if (bmp.close) bmp.close();
    const blob = await canvasToBlob(canvas, "image/jpeg", 0.88);
    if (!blob) throw new Error("encode failed");
    const data = await blobToBase64(blob);
    const base = String(file.name || "photo").replace(/\.[^.]+$/, "") || "photo";
    return {
      mime: "image/jpeg",
      data,
      url: URL.createObjectURL(blob),
      name: `${base}.jpg`,
      kind: "image",
    };
  } catch {
    const data = await blobToBase64(file);
    return {
      mime: mime || file.type || "image/jpeg",
      data,
      url: URL.createObjectURL(file),
      name: file.name || "paste.jpg",
      kind: "image",
    };
  }
}

function renderPasteTray() {
  const tray = $("paste-tray");
  tray.innerHTML = "";
  tray.hidden = state.pendingImages.length === 0;
  tray.classList.toggle("has-items", state.pendingImages.length > 0);
  state.pendingImages.forEach((img, i) => {
    const chip = document.createElement("div");
    chip.className = "paste-chip attachment-chip";
    if (img.kind === "video" && img.url) {
      chip.innerHTML = `<video muted></video><button type="button" class="remove-attachment">✕</button>`;
      chip.querySelector("video").src = img.url;
    } else {
      chip.innerHTML = `<img alt="paste"><button type="button" class="remove-attachment">✕</button>`;
      chip.querySelector("img").src = img.url || `data:${img.mime};base64,${img.data}`;
    }
    chip.querySelector("button").onclick = () => {
      if (img.url?.startsWith("blob:")) {
        try {
          URL.revokeObjectURL(img.url);
        } catch {
          /* ignore */
        }
      }
      state.pendingImages.splice(i, 1);
      renderPasteTray();
    };
    tray.appendChild(chip);
  });
}

async function addImageFile(file) {
  if (!isMediaFile(file)) return;
  const mime = guessMediaMime(file);
  const isVideo = mime.startsWith("video/");
  if (isVideo) {
    if (file.size > CHAT_VIDEO_MAX_BYTES) {
      alert("That video is too large to attach from the phone. Try a shorter clip.");
      return;
    }
    const data = await blobToBase64(file);
    state.pendingImages.push({
      mime: mime || "video/mp4",
      data,
      url: URL.createObjectURL(file),
      name: file.name || "clip.mp4",
      kind: "video",
    });
    renderPasteTray();
    return;
  }
  const prepared = await prepareChatImage(file, mime);
  state.pendingImages.push(prepared);
  renderPasteTray();
}

async function ingestClipboardEvent(e) {
  const files = mediaFilesFromClipboard(e.clipboardData);
  if (!files.length) return false;
  e.preventDefault();
  for (const file of files) await addImageFile(file);
  return true;
}

const AGENT_EFFORT = ["off", "low", "medium", "high", "xhigh"];
const AGENT_WORKFLOWS = [
  { id: "deep-research", hint: "Research a query with cited report" },
];
const AGENT_SLASH = [
  { cmd: "/help", hint: "List slash commands", send: true, source: "built-in" },
  { cmd: "/new", aliases: ["/clear"], hint: "Start a fresh chat", run: "new", source: "built-in" },
  { cmd: "/resume", hint: "Reload a previous session", send: true, source: "built-in" },
  { cmd: "/compact", hint: "Compress conversation history", argHint: "[context]", send: true, args: "optional", source: "built-in" },
  { cmd: "/context", hint: "Show context-window usage", send: true, source: "built-in" },
  { cmd: "/session-info", aliases: ["/status", "/info"], hint: "Show session details", send: true, source: "built-in" },
  { cmd: "/fork", hint: "Branch this session into a new agent", send: true, source: "built-in" },
  { cmd: "/undo", aliases: ["/rewind"], hint: "Rewind the last turn", run: "undo", source: "built-in" },
  { cmd: "/copy", hint: "Copy the last reply", argHint: "[n | path]", run: "copy", args: "optional", source: "built-in" },
  { cmd: "/export", hint: "Export the conversation", send: true, args: "optional", source: "built-in" },
  { cmd: "/rename", aliases: ["/title"], hint: "Rename this session", argHint: "<title | --auto>", send: true, args: true, fields: [{ value: "--auto", hint: "Unpin the title and resume auto-titling" }], source: "built-in" },
  { cmd: "/stop", hint: "Stop the current turn", run: "stop", source: "built-in" },
  { cmd: "/model", aliases: ["/m"], hint: "Show or switch model", argHint: "<name> [effort]", run: "model", args: true, fieldSource: "models", source: "built-in" },
  { cmd: "/effort", hint: "Set reasoning effort", argHint: "<off|low|medium|high|xhigh>", run: "effort", args: true, fieldSource: "effort", source: "built-in" },
  { cmd: "/always-approve", hint: "Skip permission prompts (toggle)", send: true, source: "built-in" },
  { cmd: "/auto", hint: "Auto-approve safe tools (toggle)", send: true, source: "built-in" },
  { cmd: "/plan", hint: "Enter plan mode", argHint: "[description]", send: true, args: "optional", source: "built-in" },
  { cmd: "/view-plan", aliases: ["/show-plan", "/plan-view"], hint: "Preview the current plan", send: true, source: "built-in" },
  { cmd: "/memory", aliases: ["/mem"], hint: "Browse or toggle memory", argHint: "[on|off]", send: true, args: "optional", fieldSource: "memory", source: "built-in" },
  { cmd: "/flush", hint: "Write session knowledge to memory now", send: true, source: "built-in" },
  { cmd: "/dream", hint: "Consolidate memories into topics", send: true, source: "built-in" },
  { cmd: "/remember", hint: "Save a note to memory", argHint: "<note>", send: true, args: true, source: "built-in" },
  { cmd: "/hooks", hint: "Manage hooks", send: true, source: "built-in" },
  { cmd: "/plugins", hint: "Manage plugins", argHint: "[list|install|uninstall|update|reload]", run: "plugins", args: "optional", fields: [
    { value: "list", hint: "List installed plugins" },
    { value: "install", hint: "Install a plugin", more: true },
    { value: "uninstall", hint: "Uninstall a plugin", more: true },
    { value: "update", hint: "Update plugins" },
    { value: "reload", hint: "Reload plugins" },
  ], source: "built-in" },
  { cmd: "/marketplace", hint: "Browse the plugin marketplace", send: true, source: "built-in" },
  { cmd: "/skills", hint: "List installed skills", send: true, source: "built-in" },
  { cmd: "/workflows", hint: "Browse saved workflows", send: true, source: "built-in" },
  { cmd: "/imagine", hint: "Generate an image", argHint: "<description>", send: true, args: true, source: "built-in" },
  { cmd: "/imagine-video", hint: "Generate a video", argHint: "<description>", send: true, args: true, source: "built-in" },
  { cmd: "/loop", hint: "Run a prompt on an interval", argHint: "[interval] <prompt>", send: true, args: true, fields: [
    { value: "30m", hint: "Every 30 minutes", more: true },
    { value: "1h", hint: "Every hour", more: true },
    { value: "1d", hint: "Every day", more: true },
  ], source: "built-in" },
  { cmd: "/goal", hint: "Set or manage an autonomous goal", argHint: "<objective | status|pause|resume|clear>", send: true, args: "optional", fields: [
    { value: "status", hint: "Show goal status" },
    { value: "pause", hint: "Pause the goal" },
    { value: "resume", hint: "Resume the goal" },
    { value: "clear", hint: "Clear the goal" },
  ], source: "built-in" },
  { cmd: "/deep-research", hint: "Kick off a background research workflow", argHint: "<query>", send: true, args: true, source: "built-in" },
  { cmd: "/workflow", hint: "Launch or manage a workflow", argHint: "<name | runs|pause|resume|stop|save>", send: true, args: "optional", fieldSource: "workflows", source: "built-in" },
  { cmd: "/theme", aliases: ["/t"], hint: "Switch light/dark theme", argHint: "[light|dark]", run: "theme", args: "optional", fields: [
    { value: "dark", hint: "Dark theme" },
    { value: "light", hint: "Light theme" },
  ], source: "built-in" },
  { cmd: "/feedback", hint: "Send feedback", argHint: "[message]", send: true, args: "optional", source: "built-in" },
  { cmd: "/btw", hint: "Ask a side question without interrupting", argHint: "<question>", send: true, args: true, source: "built-in" },
  { cmd: "/mcps", hint: "Manage MCP servers", send: true, source: "built-in" },
  { cmd: "/doctor", hint: "Diagnose session issues", argHint: "[fix]", send: true, args: "optional", fields: [{ value: "fix", hint: "List automatic fixes" }], source: "built-in" },
  { cmd: "/release-notes", aliases: ["/changelog"], hint: "View release notes", send: true, source: "built-in" },
  { cmd: "/docs", aliases: ["/howto", "/guides"], hint: "Open How-to Guides", argHint: "[web | title]", send: true, args: "optional", fields: [{ value: "web", hint: "Open docs.x.ai/build in the browser" }], source: "built-in" },
  { cmd: "/tutorial", aliases: ["/tour", "/onboarding"], hint: "Open the onboarding tutorial", send: true, source: "built-in" },
  { cmd: "/config-agents", aliases: ["/agents"], hint: "Manage agent definitions", send: true, source: "built-in" },
  { cmd: "/personas", hint: "Create or edit personas", send: true, source: "built-in" },
  { cmd: "/login", hint: "Log in or re-authenticate", send: true, source: "built-in" },
  { cmd: "/logout", hint: "Log out", send: true, source: "built-in" },
  { cmd: "/usage", aliases: ["/cost"], hint: "View credit usage", argHint: "[manage]", send: true, args: "optional", fields: [{ value: "manage", hint: "Manage billing" }], source: "built-in" },
  { cmd: "/privacy", hint: "Coding data, retention, and training", send: true, source: "built-in" },
  { cmd: "/settings", aliases: ["/config", "/prefs", "/preferences"], hint: "Open Desk settings", run: "settings", source: "built-in" },
  { cmd: "/timestamps", hint: "Toggle message timestamps", send: true, source: "built-in" },
];

let slashIndex = 0;
let slashHits = [];
let slashLevelKey = "";

function botIsAgent(b) {
  return (b?.kind || "") === "hermes";
}

function botHasRobotSimulator(b) {
  return Boolean(b) && !botIsAgent(b);
}
window.botHasRobotSimulator = botHasRobotSimulator;

function selectedBotHasRobotSimulator() {
  return botHasRobotSimulator(state.bots.find((x) => x.id === state.selected));
}

function unloadRobotSimulator() {
  const iframe = $("app-preview-frame");
  if (iframe) {
    iframe.removeAttribute("srcdoc");
    iframe.src = "about:blank";
  }
  iframe?.closest(".minios-preview-window")?.classList.remove("robot-mode");
  const win = document.querySelector('.app-window[data-window-app="preview"]');
  const title = win?.querySelector(".window-title-left strong");
  if (title) title.textContent = "App Preview";
  const dock = document.querySelector('.dock-app[data-desktop-app="preview"]');
  if (dock) {
    dock.title = "App Preview";
    const span = dock.querySelector("span");
    if (span) span.textContent = "🖥";
  }
}

function syncRobotSimulatorForBot(b) {
  const allow = botHasRobotSimulator(b);
  document.body.classList.toggle("minios-no-robot", !allow);
  const win = document.querySelector('.app-window[data-window-app="preview"]');
  const title = win?.querySelector(".window-title-left strong");
  const dock = document.querySelector('.dock-app[data-desktop-app="preview"]');
  if (allow) {
    if (title) title.textContent = "Robot Simulator";
    if (dock) {
      dock.hidden = false;
      dock.title = "Robot Simulator";
      const span = dock.querySelector("span");
      if (span) span.textContent = "🤖";
    }
    state.surface = "preview";
    window.DeskUI?.showRobotOnAgentDesktop?.();
    return;
  }
  if (title) title.textContent = "App Preview";
  if (dock) {
    dock.title = "App Preview";
    const span = dock.querySelector("span");
    if (span) span.textContent = "🖥";
  }
  if (robotIframeReady()) unloadRobotSimulator();
  if (win && win.dataset.open === "true" && !state.previewPath) {
    window.DeskUI?.closeWindow?.(win);
  }
  if (!state.previewPath) state.surface = "desktop";
}

function fieldsFromHint(hint) {
  const text = String(hint || "").trim();
  if (!text.includes("|")) return [];
  const out = [];
  const seen = new Set();
  for (const part of text.split("|")) {
    const raw = part.trim();
    if (!raw) continue;
    const tok = raw.replace(/^[\[(<]+/, "").split(/\s+/)[0].replace(/[\])>:,]+$/, "");
    if (!tok || tok.startsWith("<") || tok === "..." || seen.has(tok.toLowerCase())) continue;
    if (/^--?[a-z0-9]/.test(tok) || /^[a-z][a-z0-9_-]*$/i.test(tok)) {
      seen.add(tok.toLowerCase());
      out.push({ value: tok, hint: raw, more: /<|\[/.test(raw) && !/^\[?--/.test(raw) ? true : /<[^>]+>/.test(raw) });
    }
  }
  return out;
}

function slashNames(row) {
  return [row.cmd, ...(row.aliases || [])];
}

function slashCatalog(b) {
  const effort = currentEffort(b);
  const out = AGENT_SLASH.map((row) => {
    const copy = { ...row };
    if (effort && (copy.cmd === "/model" || copy.cmd === "/effort")) {
      copy.hint = `${copy.hint} · thinking ${effort}`;
    }
    return copy;
  });
  const seen = new Set(out.flatMap((r) => slashNames(r).map((c) => c.toLowerCase())));
  for (const raw of b?.slash_commands || []) {
    const name = String(raw.name || raw.command || "").trim().replace(/^\/*/, "");
    if (!name) continue;
    const cmd = `/${name}`;
    if (seen.has(cmd.toLowerCase())) continue;
    seen.add(cmd.toLowerCase());
    const hint = String(raw.hint || raw.description || (raw.input && raw.input.hint) || "Hermes Agent command");
    const argHint = String((raw.input && raw.input.hint) || raw.argHint || raw.argument_hint || "");
    const fields = Array.isArray(raw.fields) && raw.fields.length
      ? raw.fields
      : fieldsFromHint(argHint || hint);
    out.push({
      cmd,
      hint,
      argHint,
      send: true,
      args: true,
      fields,
      source: String(raw.source || "skill"),
    });
  }
  return out;
}

function parseSlashLine(typed) {
  const raw = String(typed || "");
  if (!raw.startsWith("/")) return null;
  const m = raw.match(/^(\/\S*)([\s\S]*)$/);
  const cmdTok = (m?.[1] || "/").toLowerCase();
  const rest = m?.[2] || "";
  const hasSpace = /^\s/.test(rest);
  const args = rest.trim();
  const tokens = args ? args.split(/\s+/) : [];
  return { raw, cmdTok, rest, hasSpace, args, tokens };
}

function matchSlashCommand(rows, cmdTok) {
  const q = String(cmdTok || "").toLowerCase();
  return rows.find((row) => slashNames(row).some((n) => n.toLowerCase() === q)) || null;
}

function workflowNames(b) {
  const out = AGENT_WORKFLOWS.map((w) => ({ ...w }));
  const seen = new Set(out.map((w) => w.id.toLowerCase()));
  for (const raw of b?.workflows || []) {
    const id = String(raw.id || raw.name || raw).trim();
    if (!id || seen.has(id.toLowerCase())) continue;
    seen.add(id.toLowerCase());
    out.push({ id, hint: String(raw.hint || raw.description || "Saved workflow") });
  }
  return out;
}

function fieldRow(cmd, hint, fill, extra) {
  return {
    kind: "field",
    cmd,
    hint,
    fill,
    source: "built-in",
    submit: false,
    more: false,
    ...(extra || {}),
  };
}

function headingRow(label) {
  return { kind: "heading", cmd: label, hint: "", fill: "", source: "built-in" };
}

function normalizeEffort(level) {
  const v = String(level || "").trim().toLowerCase();
  if (v === "none" || v === "disabled") return "off";
  return v;
}

function currentEffort(b) {
  const live = normalizeEffort(b?.effort);
  if (live) return live;
  const hit = (b?.models || []).find((m) => m.id === b.model);
  return normalizeEffort(hit?.reasoning_effort || hit?.effort);
}

function effortHint(level, raw) {
  const v = normalizeEffort(level);
  if (raw) return String(raw);
  return v === "off" ? "No thinking" : "Thinking";
}

function withOffLevel(rows) {
  const list = (Array.isArray(rows) ? rows : []).map((row) => {
    const value = normalizeEffort(row.value || row);
    return { value, hint: effortHint(value, row.hint) };
  }).filter((row) => row.value);
  if (!list.some((row) => row.value === "off")) {
    list.unshift({ value: "off", hint: "No thinking" });
  }
  return list;
}

function effortLevelsFor(b, modelId) {
  const hit = (b?.models || []).find((m) => m.id === (modelId || b?.model)) || {};
  const rows = hit.reasoning_efforts;
  if (Array.isArray(rows) && rows.length) {
    return withOffLevel(rows.map((r) => ({
      value: String(r.value || r.id || "").toLowerCase(),
      hint: String(r.label || r.description || ""),
    })));
  }
  if (hit.supports_reasoning_effort === false) return [];
  return withOffLevel(AGENT_EFFORT.map((level) => ({ value: level, hint: effortHint(level) })));
}

function effortFieldRows(prefix, extra, current) {
  const cur = normalizeEffort(current);
  const levels = extra?.levels;
  const rows = Array.isArray(levels) && levels.length
    ? withOffLevel(levels)
    : withOffLevel(AGENT_EFFORT.map((level) => ({ value: level, hint: effortHint(level) })));
  return rows.map((row) => {
    const level = normalizeEffort(row.value || row);
    const hint = effortHint(level, row.hint);
    const mark = level === cur ? " (current)" : "";
    return fieldRow(level, `${hint}${mark}`, `${prefix}${level}`, {
      submit: true,
      ...(extra || {}),
      levels: undefined,
      heading: undefined,
      current: level === cur,
    });
  });
}

function thinkingFieldRows(prefix, extra, current) {
  const title = extra?.heading || "thinking";
  return [headingRow(title), ...effortFieldRows(prefix, { ...(extra || {}), setting: true }, current)];
}

function staticFieldRows(parent, fields, prefix, extra) {
  return (fields || []).map((f) => {
    const value = f.value || f.cmd;
    const more = !!f.more;
    return fieldRow(value, f.hint || parent.hint || "", more ? `${prefix}${value} ` : `${prefix}${value}`, {
      submit: !more,
      more,
      ...(extra || {}),
    });
  });
}

function slashSubfields(b, parent, parsed) {
  const tokens = parsed?.tokens || [];
  const prefix = `${parent.cmd} `;
  const source = parent.fieldSource || "";
  if (source === "models") {
    const models = modelsForBot(b);
    const picked = tokens[0] || "";
    const hit = picked
      ? models.find((m) => String(m.id) === picked || String(m.name || "").toLowerCase() === picked.toLowerCase())
      : null;
    if (hit && (tokens.length > 1 || /\s$/.test(parsed.raw))) {
      const levels = effortLevelsFor(b, hit.id);
      if (!levels.length) return [];
      return thinkingFieldRows(
        `${prefix}${hit.id} `,
        { levels, heading: "thinking" },
        hit.id === b.model ? currentEffort(b) : String(hit.reasoning_effort || ""),
      );
    }
    return models.map((m) => {
      const isCur = m.id === b.model;
      const busy = m.available === false
        ? (String(m.unavailable_reason || "").includes("occupying") ? " (GPUs busy)" : " (not running)")
        : "";
      const label = m.name && m.name !== m.id ? m.name : "Model";
      const think = isCur && currentEffort(b) ? ` · thinking ${currentEffort(b)}` : "";
      const hasThink = effortLevelsFor(b, m.id).length > 0;
      return fieldRow(m.id, `${label}${isCur ? " (current)" : ""}${busy}${think}`, `${prefix}${m.id} `, {
        submit: !hasThink,
        more: hasThink,
        current: isCur,
      });
    });
  }
  if (source === "effort") {
    return thinkingFieldRows(prefix, { levels: effortLevelsFor(b, b?.model), heading: "thinking" }, currentEffort(b));
  }
  if (source === "memory") {
    return [
      fieldRow("on", "Enable memory", `${prefix}on`, { submit: true }),
      fieldRow("off", "Disable memory", `${prefix}off`, { submit: true }),
    ];
  }
  if (source === "workflows") {
    const verb = (tokens[0] || "").toLowerCase();
    const verbs = [
      { value: "runs", hint: "List this session's workflow runs", submit: true },
      { value: "pause", hint: "Pause a run by display name", more: true },
      { value: "resume", hint: "Resume a run by display name", more: true },
      { value: "stop", hint: "Stop a run by display name", more: true },
      { value: "save", hint: "Save a run's script", more: true },
    ];
    const flags = [
      { value: "--agent-budget", hint: "Cap child-agent calls (1–1024)", more: true },
      { value: "--effort", hint: "Child reasoning effort", more: true },
    ];
    if (verb === "--effort" || (tokens.length >= 2 && tokens[tokens.length - 2] === "--effort")) {
      return effortFieldRows(
        `${parent.cmd} ${tokens.slice(0, -1).join(" ")}${tokens.length ? " " : ""}`.replace(/\s+$/, " "),
        { levels: effortLevelsFor(b, b?.model) },
        currentEffort(b),
      );
    }
    if (["pause", "resume", "stop", "save"].includes(verb)) {
      const runs = b?.workflow_runs || [];
      if (!runs.length) return [];
      return runs.map((run) => {
        const id = String(run.name || run.id || run);
        return fieldRow(id, String(run.hint || run.phase || "Session run"), `${prefix}${verb} ${id}`, { submit: true });
      });
    }
    const names = workflowNames(b);
    if (verb && names.some((w) => w.id.toLowerCase() === verb)) {
      const name = names.find((w) => w.id.toLowerCase() === verb).id;
      if (tokens[1] === "--effort" || (/\s$/.test(parsed.raw) && tokens[tokens.length - 1] === "--effort")) {
        return effortFieldRows(`${prefix}${name} --effort `, { levels: effortLevelsFor(b, b?.model) }, currentEffort(b));
      }
      return flags.map((f) => fieldRow(f.value, f.hint, `${prefix}${name} ${f.value} `, { more: true }));
    }
    return [
      ...verbs.map((f) => fieldRow(f.value, f.hint, f.more ? `${prefix}${f.value} ` : `${prefix}${f.value}`, { submit: !!f.submit, more: !!f.more })),
      ...names.map((w) => fieldRow(w.id, w.hint, `${prefix}${w.id} `, { more: true })),
    ];
  }
  if (parent.fields && parent.fields.length) return staticFieldRows(parent, parent.fields, prefix);
  return [];
}

function filterSlash(b, typed) {
  const parsed = parseSlashLine(typed);
  const rows = slashCatalog(b);
  if (!parsed || parsed.cmdTok === "/") return rows;
  if (parsed.hasSpace) {
    const parent = matchSlashCommand(rows, parsed.cmdTok);
    if (!parent) return [];
    const fields = slashSubfields(b, parent, parsed);
    if (!fields.length) return [];
    const q = (parsed.tokens[parsed.tokens.length - 1] || "").toLowerCase();
    const trailing = /\s$/.test(parsed.raw);
    if (!q || trailing) return fields;
    return fields.filter((row) => (
      row.kind === "heading"
      || row.cmd.toLowerCase().startsWith(q)
      || row.cmd.toLowerCase().includes(q)
      || String(row.hint || "").toLowerCase().includes(q)
    ));
  }
  const q = parsed.cmdTok;
  return rows.filter((row) => {
    const names = slashNames(row);
    return names.some((n) => n.toLowerCase().startsWith(q) || n.toLowerCase().includes(q.slice(1)))
      || String(row.hint || "").toLowerCase().includes(q.slice(1));
  });
}

function hideSlashMenu() {
  const menu = $("slash-menu");
  if (menu) {
    menu.hidden = true;
    menu.innerHTML = "";
  }
  slashHits = [];
  slashIndex = 0;
  slashLevelKey = "";
}

function placeSlashMenu() {
  /* CSS pins the menu above .composer-wrap */
}

function slashHasSubfields(row) {
  return !!(row && (row.fieldSource || (row.fields && row.fields.length)));
}

function renderSlashMenu(b, typed) {
  const menu = $("slash-menu");
  if (!menu || !botIsAgent(b)) {
    hideSlashMenu();
    return;
  }
  const parsed = parseSlashLine(typed);
  const fieldStem = parsed && parsed.hasSpace
    ? parsed.tokens.slice(0, Math.max(0, parsed.tokens.length - (/\s$/.test(parsed.raw) ? 0 : 1))).join(" ")
    : "";
  const nextLevel = parsed ? `${parsed.cmdTok}|${fieldStem}` : "";
  const levelChanged = nextLevel !== slashLevelKey;
  if (levelChanged) {
    slashLevelKey = nextLevel;
    slashIndex = 0;
  }
  slashHits = filterSlash(b, typed);
  if (!slashHits.length) {
    hideSlashMenu();
    return;
  }
  if (levelChanged) {
    const cur = slashHits.findIndex((row) => row.current);
    if (cur >= 0) slashIndex = cur;
  }
  if (slashIndex >= slashHits.length) slashIndex = 0;
  if (slashIndex < 0) slashIndex = slashHits.length - 1;
  if (slashHits[slashIndex]?.kind === "heading") {
    const next = slashHits.findIndex((row, i) => i > slashIndex && row.kind !== "heading");
    slashIndex = next >= 0 ? next : slashHits.findIndex((row) => row.kind !== "heading");
    if (slashIndex < 0) slashIndex = 0;
  }
  menu.hidden = false;
  menu.innerHTML = "";
  slashHits.forEach((row, i) => {
    if (row.kind === "heading") {
      const head = document.createElement("div");
      head.className = "slash-heading";
      head.textContent = row.cmd;
      menu.appendChild(head);
      return;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "slash-item"
      + (i === slashIndex ? " is-active" : "")
      + (row.kind === "field" ? " is-field" : "")
      + (row.current ? " is-current" : "");
    btn.setAttribute("role", "option");
    const arg = row.argHint ? `<span class="slash-arg">${escapeHtml(row.argHint)}</span>` : "";
    const src = row.source ? `<span class="slash-src">${escapeHtml(row.source)}</span>` : "";
    btn.innerHTML = `<span class="slash-cmd">${escapeHtml(row.cmd)}</span>${arg}<span class="slash-hint">${escapeHtml(row.hint || "")}</span>${src}`;
    btn.onmousedown = (e) => {
      e.preventDefault();
      applySlashPick(row, b);
    };
    menu.appendChild(btn);
  });
  placeSlashMenu();
  menu.querySelector(".slash-item.is-active")?.scrollIntoView({ block: "nearest" });
}

function syncSlashMenu() {
  const b = state.bots.find((x) => x.id === state.selected);
  const typed = ($("message")?.value || "");
  if (!botIsAgent(b) || !typed.startsWith("/")) {
    hideSlashMenu();
    return;
  }
  const parsed = parseSlashLine(typed);
  if (parsed?.hasSpace) {
    const parent = matchSlashCommand(slashCatalog(b), parsed.cmdTok);
    if (!parent || !slashHasSubfields(parent)) {
      hideSlashMenu();
      return;
    }
    const fields = slashSubfields(b, parent, parsed);
    if (!fields.length && parsed.args) {
      hideSlashMenu();
      return;
    }
  }
  renderSlashMenu(b, typed);
}

function fillSlashComposer(text) {
  const ta = $("message");
  if (!ta) return;
  ta.value = text;
  autosizeComposer();
  updateSendButton();
  ta.focus();
}

async function applySlashPick(row, b, opts) {
  const ta = $("message");
  const tab = !!(opts && opts.tab);
  if (!row || !ta) return;
  if (row.kind === "heading") return;
  if (row.kind === "field") {
    const next = row.more || (tab && !row.submit) ? (String(row.fill).endsWith(" ") ? row.fill : `${row.fill} `) : row.fill;
    if (!tab && row.submit && !row.more) {
      hideSlashMenu();
      if (row.setting) {
        await handleSlash(String(next).trim(), b);
        ta.value = "";
        autosizeComposer();
        updateSendButton();
        ta.focus();
        return;
      }
      fillSlashComposer(next);
      $("composer")?.requestSubmit();
      return;
    }
    fillSlashComposer(next);
    syncSlashMenu();
    return;
  }
  const needsArgs = row.args === true || slashHasSubfields(row);
  if (tab || needsArgs) {
    fillSlashComposer(`${row.cmd} `);
    syncSlashMenu();
    return;
  }
  if (row.run) {
    hideSlashMenu();
    const ok = await handleSlash(row.cmd, b);
    if (ok) {
      ta.value = "";
      autosizeComposer();
      updateSendButton();
    }
    ta.focus();
    return;
  }
  hideSlashMenu();
  fillSlashComposer(row.cmd);
  $("composer")?.requestSubmit();
}

function nextSlashIndex(dir) {
  if (!slashHits.length) return 0;
  let i = slashIndex;
  for (let n = 0; n < slashHits.length; n++) {
    i = (i + dir + slashHits.length) % slashHits.length;
    if (slashHits[i]?.kind !== "heading") return i;
  }
  return slashIndex;
}

function slashMenuKey(e) {
  const menu = $("slash-menu");
  if (!menu || menu.hidden) {
    if (e.key === "Escape") hideSlashMenu();
    return false;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    slashIndex = nextSlashIndex(1);
    syncSlashMenu();
    return true;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    slashIndex = nextSlashIndex(-1);
    syncSlashMenu();
    return true;
  }
  if (e.key === "Tab") {
    e.preventDefault();
    const b = state.bots.find((x) => x.id === state.selected);
    const row = slashHits[slashIndex];
    if (row && b) applySlashPick(row, b, { tab: true });
    return true;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const b = state.bots.find((x) => x.id === state.selected);
    const row = slashHits[slashIndex];
    if (row && b) applySlashPick(row, b);
    return true;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    hideSlashMenu();
    return true;
  }
  return false;
}

async function handleSlash(text, b) {
  const parts = text.split(/\s+/);
  const cmd = (parts[0] || "").toLowerCase();
  const arg = parts.slice(1).join(" ").trim();
  const agentBuild = botIsAgent(b);
  const sys = (msg) => {
    b.messages = b.messages || [];
    b.messages.push({ role: "user", text });
    b.messages.push({ role: "system", text: msg });
    renderConversation(b);
  };
  if (cmd === "/help") {
    if (agentBuild) return false;
    sys("Commands: /undo — rewind last turn (kept in the log, dropped from chat, memory, and export). /model [id] — show or switch model. /help");
    return true;
  }
  if (cmd === "/new" || cmd === "/clear") {
    if (!agentBuild) return false;
    await startNewChat();
    return true;
  }
  if (cmd === "/stop") {
    if (isWorking(b.id)) stopAgent();
    return true;
  }
  if (cmd === "/copy") {
    const last = [...(b.messages || [])].reverse().find((m) => m.role === "assistant" && (m.text || "").trim());
    const body = last?.text || "";
    if (!body) {
      sys("Nothing to copy yet.");
      return true;
    }
    try {
      await navigator.clipboard.writeText(body);
      sys("Copied the last reply.");
    } catch (err) {
      sys(String(err.message || err));
    }
    return true;
  }
  if (cmd === "/settings" || cmd === "/config" || cmd === "/prefs" || cmd === "/preferences") {
    window.DeskUI?.openUserSettings?.();
    return true;
  }
  if (cmd === "/plugins") {
    if (arg && agentBuild) return false;
    window.DeskUI?.openPlugins?.();
    return true;
  }
  if (cmd === "/theme" || cmd === "/t") {
    const want = arg.toLowerCase();
    if (want === "dark" || want === "light") applyTheme(want);
    else applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    return true;
  }
  if (cmd === "/undo" || cmd === "/rewind") {
    if (!confirm("Undo the last turn? It leaves the live chat and memory. The log still keeps it; export will not.")) {
      return true;
    }
    try {
      const j = await api(`/v1/bots/${b.id}/undo`, { method: "POST", body: "{}" });
      if (j.bot) {
        const i = state.bots.findIndex((x) => x.id === b.id);
        if (i >= 0) state.bots[i] = j.bot;
        renderConversation(j.bot);
      }
    } catch (err) {
      sys(String(err.message || err));
    }
    return true;
  }
  if (cmd === "/effort") {
    const level = (arg.split(/\s+/)[0] || "").toLowerCase();
    if (!level) {
      fillSlashComposer("/effort ");
      syncSlashMenu();
      return true;
    }
    try {
      await applyBotModel(b, b.model, level);
    } catch (err) {
      sys(String(err.message || err));
    }
    return true;
  }
  if (cmd === "/model" || cmd === "/m") {
    const bits = arg.split(/\s+/).filter(Boolean);
    const modelId = (bits[0] || "").trim();
    const effort = (bits[1] || "").toLowerCase();
    if (!modelId) {
      fillSlashComposer("/model ");
      syncSlashMenu();
      return true;
    }
    try {
      await applyBotModel(b, modelId, effort || undefined);
    } catch (err) {
      sys(String(err.message || err));
    }
    return true;
  }
  return false;
}

async function undoLastTurn() {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b) return;
  if (!confirm("Undo the last turn? It leaves the live chat and memory. The log still keeps it; export will not.")) return;
  $("undo").disabled = true;
  try {
    const j = await api(`/v1/bots/${b.id}/undo`, { method: "POST", body: "{}" });
    if (j.bot) {
      const i = state.bots.findIndex((x) => x.id === b.id);
      if (i >= 0) state.bots[i] = j.bot;
      renderConversation(j.bot);
    }
  } catch (err) {
    $("undo").disabled = false;
    alert(err.message || err);
  }
}

$("undo").onclick = (e) => {
  e.preventDefault();
  undoLastTurn();
};

$("send")?.addEventListener("click", (e) => {
  if (!isWorking() && !(voiceChat.on && isTtsPlaying())) return;
  e.preventDefault();
  stopSpeak();
  stopAgent();
});

// ---- Voice: TTS output via /v1/tts (Jade on teela-body), STT input via /v1/stt ----
// Playback uses Web Audio (not new Audio().play()) so it is not blocked by
// mobile autoplay policy once the AudioContext is unlocked by a user gesture.
// Per-tab speaker identity: the page that sends a prompt claims audio for the
// reply, so two open devices don't both speak the same answer. sessionStorage
// keeps the id stable across reloads; each new tab gets its own.
const PAGE_ID = (() => {
  try {
    let p = sessionStorage.getItem("deskPageId");
    if (!p) {
      p = (window.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2));
      sessionStorage.setItem("deskPageId", p);
    }
    return p;
  } catch {
    return Math.random().toString(36).slice(2, 12);
  }
})();
const voice = {
  chunks: [], idx: 0, ctx: null, src: null, botId: null, lastSpoken: {}, errTimer: 0, gen: 0,
};

function cleanForSpeech(text) {
  let t = String(text || "");
  t = t.replace(/```[\s\S]*?```/g, " Code block is in the chat. ");
  t = t.replace(/`([^`]+)`/g, "$1");
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, "");
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
  t = t.replace(/https?:\/\/\S+/g, "");
  t = t.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, "");
  t = t.replace(/[#*_>~|]+/g, " ");
  t = keepChatterboxTags(t);
  t = t.replace(/\s+/g, " ").trim();
  if (t.length > 900) t = t.slice(0, 900).replace(/\s+\S*$/, "") + "…";
  return t;
}

function chunkSpeech(text) {
  const max = 220;
  const out = [];
  let cur = "";
  for (const s of String(text || "").split(/(?<=[.!?])\s+/)) {
    if (cur && (cur + " " + s).length > max) { out.push(cur); cur = s; }
    else cur = cur ? cur + " " + s : s;
  }
  if (cur) out.push(cur);
  return out.length ? out : [String(text || "")];
}

function lastUserTextForTts() {
  const bot = (state.bots || []).find((b) => b.id === (voice.botId || state.selected));
  const msgs = bot?.messages || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i]?.role === "user") return String(msgs[i].text || msgs[i].content || "");
  }
  return "";
}

async function fetchTtsBuffer(text) {
  const r = await fetch("/v1/tts", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ text, user_text: lastUserTextForTts() }),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "tts failed");
  return r.arrayBuffer();
}

function voiceCtx() {
  if (!voice.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error("Web Audio not supported");
    voice.ctx = new AC();
  }
  if (voice.ctx.state === "suspended") voice.ctx.resume().catch(() => {});
  return voice.ctx;
}

function isTtsPlaying() {
  return !!(voice.src || (voice.botId && voice.chunks.length && voice.idx < voice.chunks.length));
}

function stopSpeak() {
  voice.gen += 1;
  if (voice.src) {
    try { voice.src.onended = null; voice.src.stop(); } catch { /* already stopped */ }
    voice.src = null;
  }
  voice.chunks = [];
  voice.idx = 0;
  if (voice.botId) window.DeskUI?.settleEmotion(voice.botId);
  voice.botId = null;
}

function flashVoiceError(detail) {
  const btn = $("voice-toggle");
  if (detail) console.warn("[voice]", detail);
  if (!btn) return;
  btn.classList.add("error");
  btn.title = "Voice error — check device volume and the site's audio permission, then tap again";
  clearTimeout(voice.errTimer);
  voice.errTimer = setTimeout(() => paintVoiceToggle(), 2500);
}

function speakNextChunk(gen) {
  gen = gen ?? voice.gen;
  if (gen !== voice.gen) return;
  if (voice.idx >= voice.chunks.length) {
    voice.src = null;
    voice.botId = null;
    return;
  }
  const id = voice.botId;
  const chunk = voice.chunks[voice.idx];
  if (id) window.DeskUI?.setEmotion(id, "speaking", { persist: false, pop: false });
  fetchTtsBuffer(chunk)
    .then((buf) => {
      if (gen !== voice.gen || voice.botId !== id) return;
      let ctx;
      try {
        ctx = voiceCtx();
      } catch (e) {
        flashVoiceError(String(e));
        voice.idx += 1;
        speakNextChunk(gen);
        return;
      }
      ctx.decodeAudioData(buf)
        .then((audioBuf) => {
          if (gen !== voice.gen || voice.botId !== id) return;
          const src = ctx.createBufferSource();
          src.buffer = audioBuf;
          src.connect(ctx.destination);
          voice.src = src;
          src.onended = () => {
            if (voice.src !== src || gen !== voice.gen) return;
            voice.src = null;
            voice.idx += 1;
            speakNextChunk(gen);
          };
          src.start();
        })
        .catch((e) => {
          if (gen !== voice.gen || voice.botId !== id) return;
          flashVoiceError("decode failed: " + e);
          voice.idx += 1;
          speakNextChunk(gen);
        });
    })
    .catch((e) => {
      if (gen !== voice.gen || voice.botId !== id) return;
      flashVoiceError("tts fetch failed: " + e);
      voice.idx += 1;
      speakNextChunk(gen);
    });
}

// Play immediately on a user gesture (toggle tap) — unlocks autoplay and
// gives instant audible feedback that the voice path works.
function playConfirm(text) {
  stopSpeak();
  const gen = voice.gen;
  voice.chunks = [String(text || "Voice on.")];
  voice.idx = 0;
  voice.botId = state.selected || null;
  speakNextChunk(gen);
}

function teammateSpeech(text, via, peer) {
  if (via === "dm" || peer) return true;
  const t = String(text || "");
  return t.startsWith("Sent to ") || t.startsWith("✉️");
}

function speak(botId, text, meta) {
  if (teammateSpeech(text, meta?.via, meta?.peer)) return;
  const clean = cleanForSpeech(text);
  if (!state.voice || !clean) return;
  // Time-windowed dedup: blocks the same reply arriving via both the chat
  // event and the turn-completed event, but allows repeats after a few seconds.
  const prev = voice.lastSpoken[botId];
  if (prev && prev.text === clean && Date.now() - prev.t < 15000) return;
  voice.lastSpoken[botId] = { text: clean, t: Date.now() };
  stopSpeak();
  const gen = voice.gen;
  voice.chunks = chunkSpeech(clean);
  voice.idx = 0;
  voice.botId = botId;
  speakNextChunk(gen);
}

function paintVoiceToggle() {
  const btn = $("voice-toggle");
  if (!btn) return;
  btn.classList.toggle("off", !state.voice);
  const on = btn.querySelector(".icon-speaker-on");
  const off = btn.querySelector(".icon-speaker-off");
  if (on) on.hidden = !state.voice;
  if (off) off.hidden = !!state.voice;
  btn.title = state.voice ? "Voice on — Teela speaks replies. Tap to mute." : "Voice muted. Tap to enable.";
}

$("voice-toggle")?.addEventListener("click", async () => {
  state.voice = !state.voice;
  paintVoiceToggle();
  if (!state.voice) stopSpeak();
  try {
    const j = await window.deskSaveAccess({ voice: state.voice });
    if (typeof j?.voice === "boolean") { state.voice = j.voice; paintVoiceToggle(); }
  } catch { /* keep local state */ }
  if (state.voice) playConfirm();
});
paintVoiceToggle();

// Unlock the shared AudioContext on first interaction. Once running, Web Audio
// playback does not need further gestures, so bot replies that arrive later
// are not blocked by mobile autoplay policy.
function primeAudio() {
  try { voiceCtx(); } catch { /* retried on next gesture */ }
}
window.addEventListener("pointerdown", primeAudio, { passive: true });
window.addEventListener("keydown", primeAudio);
window.addEventListener("touchstart", primeAudio, { passive: true });

// ---- Mic: hands-free voice chat (tap once, talk, pause to send) ----
const voiceChat = {
  on: false,
  stream: null,
  rec: null,
  chunks: [],
  analyser: null,
  source: null,
  audioCtx: null,
  data: null,
  raf: 0,
  heard: false,
  heardAt: 0,
  lastLoud: 0,
  bargeAt: 0,
  noise: 0.012,
  sensitivity: 50,
  barge: 15,
  hangMs: 1200,
  eq: [30, 70, 90, 100, 90, 55, 25],
  freq: null,
  lastMicPost: 0,
  transcribing: false,
  sendLock: false,
  pendingSend: "",
  lastHeard: "",
  turn: 0,
  savedVoice: null,
  placeholder: "",
  pcm: [],
  pre: [],
  processor: null,
  mute: null,
};
let micHintTimer = 0;

function flashMicHint() {
  const m = $("message");
  if (!m) return;
  const original = voiceChat.placeholder || m.placeholder;
  m.placeholder = location.protocol === "https:"
    ? "Microphone unavailable — check browser permissions"
    : `Microphone needs HTTPS — open https://${location.hostname}:${Number(location.port || 8742) + 1}/ and accept the self-signed cert`;
  clearTimeout(micHintTimer);
  micHintTimer = setTimeout(() => { m.placeholder = original; }, 5000);
}

function micMime() {
  return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]
    .find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || "";
}

function rmsLevel(analyser, buf) {
  analyser.getFloatTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  return Math.sqrt(sum / buf.length);
}

const MIC_EQ_HZ = [125, 250, 500, 1000, 2000, 4000, 8000];
const MIC_EQ_DEFAULT = [30, 70, 90, 100, 90, 55, 25];

function clampMicEq(eq) {
  const src = Array.isArray(eq) ? eq : MIC_EQ_DEFAULT;
  return MIC_EQ_HZ.map((_, i) => Math.min(100, Math.max(0, Number(src[i] ?? MIC_EQ_DEFAULT[i]) || 0)));
}

function micBandStats(freq, binHz, hz) {
  const center = hz / binHz;
  const lo = Math.max(1, Math.floor(center / Math.SQRT2));
  const hi = Math.min(freq.length - 1, Math.ceil(center * Math.SQRT2));
  let peak = 0;
  let sum = 0;
  let n = 0;
  for (let i = lo; i <= hi; i++) {
    const v = freq[i];
    sum += v;
    if (v > peak) peak = v;
    n++;
  }
  return { peak, mean: n ? sum / n : 0 };
}

function micLogBins(freq, sr, fftSize, n = 24) {
  const nyquist = sr / 2;
  const fMin = 80;
  const binHz = sr / fftSize;
  const bins = [];
  const speech = [];
  for (let i = 0; i < n; i++) {
    const fa = fMin * Math.pow(nyquist / fMin, i / n);
    const fb = fMin * Math.pow(nyquist / fMin, (i + 1) / n);
    const ia = Math.max(1, Math.floor(fa / binHz));
    const ib = Math.min(freq.length - 1, Math.floor(fb / binHz));
    let peak = 0;
    for (let j = ia; j <= ib; j++) if (freq[j] > peak) peak = freq[j];
    bins.push(Math.round((peak / 255) * 100));
    speech.push(fb > 250 && fa < 3400);
  }
  return { bins, speech };
}

function micSpeechFrame() {
  const analyser = voiceChat.analyser;
  const rms = rmsLevel(analyser, voiceChat.data);
  if (!voiceChat.freq || voiceChat.freq.length !== analyser.frequencyBinCount) {
    voiceChat.freq = new Uint8Array(analyser.frequencyBinCount);
  }
  analyser.getByteFrequencyData(voiceChat.freq);
  const sr = voiceChat.audioCtx?.sampleRate || 48000;
  const binHz = sr / analyser.fftSize;
  const eq = voiceChat.eq || MIC_EQ_DEFAULT;
  const bands = [];
  let weighted = 0;
  let gainSum = 0;
  let speechRaw = 0;
  let totalRaw = 0;
  let fftPeak = 0;
  let peakHz = 0;
  for (let i = 0; i < MIC_EQ_HZ.length; i++) {
    const st = micBandStats(voiceChat.freq, binHz, MIC_EQ_HZ[i]);
    const gain = Math.max(0, Math.min(1, (eq[i] ?? MIC_EQ_DEFAULT[i]) / 100));
    const score = (st.mean / 255) * gain;
    bands.push({ hz: MIC_EQ_HZ[i], mean: st.mean, peak: st.peak, gain, score });
    weighted += score;
    gainSum += gain;
    totalRaw += st.mean;
    if (MIC_EQ_HZ[i] >= 250 && MIC_EQ_HZ[i] <= 4000) speechRaw += st.mean;
  }
  for (let i = 1; i < voiceChat.freq.length; i++) {
    if (voiceChat.freq[i] > fftPeak) {
      fftPeak = voiceChat.freq[i];
      peakHz = Math.round(i * binHz);
    }
  }
  const band = totalRaw > 0 ? speechRaw / totalRaw : 0;
  const eqScore = gainSum > 0 ? weighted / gainSum : 0;
  if (!voiceChat.heard && !voiceChat.transcribing && rms < 0.02) {
    voiceChat.noise = voiceChat.noise * 0.94 + rms * 0.06;
  }
  const noise = Math.max(0.003, voiceChat.noise);
  const sens = Math.max(0.25, (voiceChat.sensitivity ?? 50) / 50);
  const rmsNeed = Math.max(0.004, 0.012 / sens);
  const noiseNeed = 4.0 / sens;
  const scoreNeed = Math.max(0.035, 0.10 / sens);
  const bandNeed = Math.max(0.10, 0.22 - (sens - 1) * 0.06);
  const voiced = rms > noise * noiseNeed && rms > rmsNeed && eqScore > scoreNeed && band > bandNeed;
  return { rms, band, noise, voiced, eqScore, bands, peakHz };
}

function loadMicSettings() {
  try {
    const j = JSON.parse(localStorage.getItem("hermes-desk-mic") || "{}");
    if (Number.isFinite(+j.sensitivity)) voiceChat.sensitivity = Math.min(100, Math.max(0, +j.sensitivity));
    if (Number.isFinite(+j.barge)) voiceChat.barge = Math.min(100, Math.max(0, +j.barge));
    if (Number.isFinite(+j.hang)) voiceChat.hangMs = Math.min(2500, Math.max(400, +j.hang));
    if (Array.isArray(j.eq)) voiceChat.eq = clampMicEq(j.eq);
    if (!(Number(j.v) >= 2)) {
      if (!Number.isFinite(+j.barge) || +j.barge === 25) voiceChat.barge = 15;
      if (!Number.isFinite(+j.hang) || +j.hang === 1100) voiceChat.hangMs = 1200;
      saveMicSettings();
    }
  } catch { /* defaults */ }
}

function saveMicSettings() {
  try {
    localStorage.setItem("hermes-desk-mic", JSON.stringify({
      v: 2,
      sensitivity: voiceChat.sensitivity,
      barge: voiceChat.barge,
      hang: voiceChat.hangMs,
      eq: clampMicEq(voiceChat.eq),
    }));
  } catch { /* ignore */ }
}

function applyMicSettings(j) {
  if (!j || typeof j !== "object") return;
  if (j.sensitivity != null) voiceChat.sensitivity = Math.min(100, Math.max(0, Number(j.sensitivity)));
  if (j.barge != null) voiceChat.barge = Math.min(100, Math.max(0, Number(j.barge)));
  if (j.hang != null) voiceChat.hangMs = Math.min(2500, Math.max(400, Number(j.hang)));
  if (Array.isArray(j.eq)) voiceChat.eq = clampMicEq(j.eq);
  saveMicSettings();
}

function postMicAnalyzer(frame, force = false) {
  const now = performance.now();
  if (!force && now - (voiceChat.lastMicPost || 0) < 50) return;
  voiceChat.lastMicPost = now;
  const iframe = $("app-preview-frame");
  if (!iframe?.contentWindow) return;
  const src = voiceChat.freq;
  const sr = voiceChat.audioCtx?.sampleRate || 48000;
  const fftSize = voiceChat.analyser?.fftSize || 2048;
  const log = src && src.length ? micLogBins(src, sr, fftSize, 24) : { bins: [], speech: [] };
  const eqEnergy = (frame?.bands || []).map((b) => Math.round((b.mean / 255) * 100));
  try {
    iframe.contentWindow.postMessage({
      type: "mic-analyzer",
      on: !!voiceChat.on,
      rms: frame?.rms || 0,
      band: frame?.band || 0,
      noise: frame?.noise || voiceChat.noise,
      voiced: !!frame?.voiced,
      heard: !!voiceChat.heard,
      tts: isTtsPlaying(),
      working: !!state.working[state.selected],
      bins: log.bins,
      speechBins: log.speech,
      eqEnergy,
      peakHz: frame?.peakHz || 0,
      eqScore: frame?.eqScore || 0,
      sensitivity: voiceChat.sensitivity,
      barge: voiceChat.barge,
      hang: voiceChat.hangMs,
    }, "*");
  } catch { /* iframe not ready */ }
}

function postMicSettingsToSim() {
  const iframe = $("app-preview-frame");
  if (!iframe?.contentWindow) return;
  try {
    iframe.contentWindow.postMessage({
      type: "mic-settings",
      sensitivity: voiceChat.sensitivity,
      barge: voiceChat.barge,
      hang: voiceChat.hangMs,
      eq: clampMicEq(voiceChat.eq),
    }, "*");
  } catch { /* ignore */ }
}

loadMicSettings();

function concatFloat32(chunks) {
  let n = 0;
  for (const c of chunks) n += c.length;
  const out = new Float32Array(n);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

function downsamplePcm(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const x = i * ratio;
    const i0 = Math.floor(x);
    const frac = x - i0;
    const a = input[i0] || 0;
    const b = input[i0 + 1] || a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

function encodeWav(float32, sampleRate) {
  const samples = float32.length;
  const buf = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, "data");
  view.setUint32(40, samples * 2, true);
  let o = 44;
  for (let i = 0; i < samples; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

function voicePcmBlob() {
  if (!voiceChat.pcm.length || !voiceChat.audioCtx) return null;
  const raw = concatFloat32(voiceChat.pcm);
  const sr = voiceChat.audioCtx.sampleRate || 48000;
  const pcm = downsamplePcm(raw, sr, 16000);
  if (pcm.length < 16000 * 0.18) return null;
  return encodeWav(pcm, 16000);
}

function usableTranscript(text) {
  let t = String(text || "").replace(/\s+/g, " ").trim();
  if (t.length < 2) return "";
  if (/^(thanks for watching\.?|subtitle[s]? provided by.*|\(.+\))$/i.test(t)) return "";
  const repeated = t.match(/^(.{3,}?)(?:\s+\1)+$/i);
  if (repeated) t = repeated[1].trim();
  return t;
}

function paintMic() {
  const btn = $("mic");
  if (!btn) return;
  const busy = !!(voiceChat.on && (
    voiceChat.heard || voiceChat.transcribing || voiceChat.sendLock
    || state.working[state.selected] || isTtsPlaying()
  ));
  const off = btn.disabled;
  const ready = voiceChat.on && !off && !busy;
  btn.classList.toggle("ready", ready);
  btn.classList.toggle("processing", !off && busy);
  btn.classList.toggle("unavailable", off);
  btn.classList.remove("live", "recording", "thinking", "available");
  const micIcon = btn.querySelector(".icon-mic");
  const stopIcon = btn.querySelector(".icon-mic-stop");
  if (micIcon) micIcon.hidden = voiceChat.on;
  if (stopIcon) stopIcon.hidden = !voiceChat.on;
  btn.setAttribute("aria-pressed", voiceChat.on ? "true" : "false");
  if (off) {
    btn.title = "Voice chat unavailable";
    btn.setAttribute("aria-label", "Voice chat unavailable");
  } else if (busy) {
    btn.title = "Voice chat — processing. Tap to stop.";
    btn.setAttribute("aria-label", "Stop voice chat");
  } else if (voiceChat.on) {
    btn.title = "Ready to transcribe — talk, then pause. Tap to stop.";
    btn.setAttribute("aria-label", "Stop voice chat");
  } else {
    btn.title = "Voice chat — tap once, then just talk. Pause to send.";
    btn.setAttribute("aria-label", "Start voice chat");
  }
}

function flushVoiceSend() {
  const text = usableTranscript(voiceChat.pendingSend);
  if (!text || !voiceChat.on || pendingCheck) return;
  if (state.working[state.selected]) return;
  voiceChat.pendingSend = "";
  const m = $("message");
  if (m) {
    m.value = text;
    m.removeAttribute("disabled");
    autosizeComposer();
    updateSendButton();
  }
  $("composer")?.requestSubmit();
}

function stopVoiceChatRecorder() {
  const rec = voiceChat.rec;
  voiceChat.rec = null;
  if (!rec || rec.state === "inactive") return Promise.resolve();
  return new Promise((resolve) => {
    rec.addEventListener("stop", () => resolve(), { once: true });
    try { rec.stop(); } catch { resolve(); }
  });
}

function ensureVoiceChatRecording() {
  if (!voiceChat.on || !voiceChat.stream || voiceChat.rec || !voiceChat.heard) return;
  const mime = micMime();
  let rec;
  try {
    rec = new MediaRecorder(voiceChat.stream, mime ? { mimeType: mime } : {});
  } catch {
    return;
  }
  voiceChat.chunks = [];
  rec.ondataavailable = (e) => { if (e.data && e.data.size) voiceChat.chunks.push(e.data); };
  try { rec.start(); } catch { return; }
  voiceChat.rec = rec;
}

async function endUtteranceAndSend() {
  if (!voiceChat.on || voiceChat.transcribing || voiceChat.sendLock) return;
  voiceChat.transcribing = true;
  voiceChat.sendLock = true;
  const turn = ++voiceChat.turn;
  const speechMs = voiceChat.heard ? performance.now() - voiceChat.heardAt : 0;
  voiceChat.heard = false;
  paintMic();
  if (speechMs < 320) {
    voiceChat.chunks = [];
    voiceChat.pcm = [];
    await stopVoiceChatRecorder();
    voiceChat.transcribing = false;
    voiceChat.sendLock = false;
    paintMic();
    return;
  }
  const rec = voiceChat.rec;
  await stopVoiceChatRecorder();
  const wav = voicePcmBlob();
  voiceChat.pcm = [];
  voiceChat.pre = [];
  const chunks = voiceChat.chunks;
  voiceChat.chunks = [];
  const blob = wav || new Blob(chunks, { type: rec?.mimeType || "audio/webm" });
  if (!wav && blob.size < 400) {
    voiceChat.transcribing = false;
    voiceChat.sendLock = false;
    paintMic();
    return;
  }
  try {
    const r = await fetch("/v1/stt", {
      method: "POST",
      headers: headers({ "Content-Type": blob.type || (wav ? "audio/wav" : "audio/webm") }),
      body: blob,
    });
    const j = await r.json().catch(() => ({}));
    if (turn !== voiceChat.turn || !voiceChat.on) return;
    const text = usableTranscript(r.ok ? j.text : "");
    if (text) {
      voiceChat.pendingSend = text;
      voiceChat.lastHeard = text;
    }
  } catch {
    /* keep listening */
  } finally {
    if (turn === voiceChat.turn) {
      voiceChat.transcribing = false;
      voiceChat.sendLock = false;
      flushVoiceSend();
      paintMic();
    }
  }
}

function interruptTeela() {
  stopSpeak();
  if (isWorking()) stopAgent().catch(() => {});
}

function beginVoiceUtterance(now, { barge = false } = {}) {
  voiceChat.heard = true;
  voiceChat.heardAt = now;
  voiceChat.lastLoud = now;
  voiceChat.bargeAt = 0;
  voiceChat.pcm = barge ? [] : voiceChat.pre.slice(-4);
  ensureVoiceChatRecording();
}

function voiceChatTick() {
  if (!voiceChat.on) return;
  voiceChat.raf = requestAnimationFrame(voiceChatTick);
  if (!voiceChat.analyser || !voiceChat.data) return;
  const frame = micSpeechFrame();
  const { voiced } = frame;
  postMicAnalyzer(frame);
  const now = performance.now();
  if (pendingCheck || voiceChat.transcribing || voiceChat.sendLock) {
    paintMic();
    return;
  }
  const teelaBusy = isTtsPlaying() || !!state.working[state.selected];
  const bargeEase = Math.max(0, Math.min(1, (voiceChat.barge ?? 15) / 100));
  const bargeHoldMs = 280 + (1 - bargeEase) * 920;
  const bargeNeed = voiced
    && (frame.eqScore || 0) > (0.12 + (1 - bargeEase) * 0.18)
    && frame.rms > frame.noise * (4.2 + (1 - bargeEase) * 5.5);
  if (teelaBusy && !voiceChat.heard) {
    if (bargeNeed) {
      if (!voiceChat.bargeAt) voiceChat.bargeAt = now;
      else if (now - voiceChat.bargeAt >= bargeHoldMs) {
        interruptTeela();
        beginVoiceUtterance(now, { barge: true });
      }
    } else voiceChat.bargeAt = 0;
    paintMic();
    return;
  }
  voiceChat.bargeAt = 0;
  if (!voiceChat.heard) {
    if (voiced) beginVoiceUtterance(now);
  } else {
    if (voiced) voiceChat.lastLoud = now;
    const speechMs = now - voiceChat.heardAt;
    const silenceMs = now - voiceChat.lastLoud;
    const hang = voiceChat.hangMs || 1200;
    if ((speechMs >= 400 && silenceMs >= hang) || speechMs >= 12000) {
      endUtteranceAndSend();
    }
  }
  paintMic();
}

async function enterVoiceChat() {
  if (voiceChat.on) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    flashMicHint();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: true,
        channelCount: 1,
      },
    });
  } catch {
    flashMicHint();
    return;
  }
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx;
  try {
    ctx = new AC();
    if (ctx.state === "suspended") await ctx.resume();
  } catch {
    stream.getTracks().forEach((t) => t.stop());
    flashMicHint();
    return;
  }
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.32;
  source.connect(analyser);
  let processor = null;
  let mute = null;
  try {
    processor = ctx.createScriptProcessor(4096, 1, 1);
    mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(processor);
    processor.connect(mute);
    mute.connect(ctx.destination);
    processor.onaudioprocess = (ev) => {
      const copy = Float32Array.from(ev.inputBuffer.getChannelData(0));
      voiceChat.pre.push(copy);
      if (voiceChat.pre.length > 8) voiceChat.pre.shift();
      if (voiceChat.heard && !voiceChat.transcribing) {
        voiceChat.pcm.push(copy);
        const sr = voiceChat.audioCtx?.sampleRate || 48000;
        let n = 0;
        for (const c of voiceChat.pcm) n += c.length;
        while (n > sr * 6 && voiceChat.pcm.length > 1) n -= voiceChat.pcm.shift().length;
      }
    };
  } catch { /* wav capture optional */ }
  const m = $("message");
  voiceChat.placeholder = m?.placeholder || "Message…";
  voiceChat.savedVoice = state.voice;
  voiceChat.stream = stream;
  voiceChat.audioCtx = ctx;
  voiceChat.source = source;
  voiceChat.analyser = analyser;
  voiceChat.processor = processor;
  voiceChat.mute = mute;
  voiceChat.pcm = [];
  voiceChat.pre = [];
  voiceChat.data = new Float32Array(analyser.fftSize);
  voiceChat.freq = new Uint8Array(analyser.frequencyBinCount);
  voiceChat.heard = false;
  voiceChat.transcribing = false;
  voiceChat.sendLock = false;
  voiceChat.pendingSend = "";
  voiceChat.noise = 0.012;
  voiceChat.on = true;
  document.body.classList.add("voice-chat");
  if (!state.voice) {
    state.voice = true;
    paintVoiceToggle();
    try { await window.deskSaveAccess({ voice: true }); } catch { /* keep local */ }
  }
  voiceChat.raf = requestAnimationFrame(voiceChatTick);
  paintMic();
}

async function leaveVoiceChat() {
  if (!voiceChat.on && !voiceChat.stream) return;
  voiceChat.turn += 1;
  voiceChat.on = false;
  voiceChat.pendingSend = "";
  stopSpeak();
  if (isWorking()) stopAgent().catch(() => {});
  document.body.classList.remove("voice-chat");
  if (voiceChat.raf) cancelAnimationFrame(voiceChat.raf);
  voiceChat.raf = 0;
  await stopVoiceChatRecorder();
  voiceChat.chunks = [];
  voiceChat.heard = false;
  voiceChat.transcribing = false;
  voiceChat.sendLock = false;
  voiceChat.pendingSend = "";
  try { voiceChat.processor?.disconnect(); } catch { /* already closed */ }
  try { voiceChat.mute?.disconnect(); } catch { /* already closed */ }
  try { voiceChat.source?.disconnect(); } catch { /* already closed */ }
  try { if (voiceChat.audioCtx && voiceChat.audioCtx.state !== "closed") voiceChat.audioCtx.close(); } catch { /* ignore */ }
  (voiceChat.stream?.getTracks() || []).forEach((t) => t.stop());
  voiceChat.stream = null;
  voiceChat.analyser = null;
  voiceChat.processor = null;
  voiceChat.mute = null;
  voiceChat.pcm = [];
  voiceChat.pre = [];
  voiceChat.source = null;
  voiceChat.audioCtx = null;
  voiceChat.data = null;
  voiceChat.freq = null;
  const m = $("message");
  if (m && voiceChat.placeholder) m.placeholder = voiceChat.placeholder;
  if (voiceChat.savedVoice === false && state.voice) {
    state.voice = false;
    paintVoiceToggle();
    try { await window.deskSaveAccess({ voice: false }); } catch { /* keep local */ }
  }
  voiceChat.savedVoice = null;
  postMicAnalyzer({ rms: 0, band: 0, voiced: false, bands: [], peakHz: 0, eqScore: 0, noise: voiceChat.noise }, true);
  paintMic();
}

async function toggleVoiceChat() {
  if (voiceChat.on) await leaveVoiceChat();
  else await enterVoiceChat();
}

$("mic")?.addEventListener("click", () => { toggleVoiceChat().catch(() => {}); });

let pendingCheck = null;
let checkScopeToSend = "";
let skipCheckConfirm = false;
let askFocus = 0;

function hideCheckConfirm() {
  pendingCheck = null;
  askFocus = 0;
  if (voiceChat.on) paintMic();
  const box = $("check-confirm");
  if (box) box.hidden = true;
  const actions = $("check-confirm-actions");
  if (actions) actions.innerHTML = "";
}

function askSelectable() {
  return [...($("check-confirm-actions")?.querySelectorAll("[data-ask-i]") || [])];
}

function setAskFocus(i) {
  const rows = askSelectable();
  if (!rows.length) return;
  askFocus = (i + rows.length) % rows.length;
  rows.forEach((el, n) => {
    el.classList.toggle("is-focus", n === askFocus);
    const caret = el.querySelector(".gb-ask-caret");
    if (caret) caret.textContent = n === askFocus ? "❯" : " ";
  });
  const cur = rows[askFocus];
  if (cur?.classList.contains("gb-ask-other")) $("check-confirm-other-input")?.focus();
  else cur?.focus();
}

function activateAskRow(i) {
  const rows = askSelectable();
  const el = rows[i];
  if (!el) return;
  if (el.classList.contains("gb-ask-other")) {
    submitOtherMeaning();
    return;
  }
  const id = el.dataset.askId;
  const label = el.dataset.askLabel;
  if (pendingCheck?.clarifyId) answerClarify(id, label);
  else chooseCheckScope(id);
}

function isOtherOption(opt) {
  const id = String(opt?.id || "").toLowerCase();
  const lab = String(opt?.label || "").toLowerCase();
  return id === "other" || id === "something_else" || lab.startsWith("something else");
}

function pickOtherMeaning() {
  return String($("check-confirm-other-input")?.value || "").trim();
}

function submitOtherMeaning() {
  const other = pickOtherMeaning();
  if (!other) {
    $("check-confirm-other-input")?.focus();
    return;
  }
  if (pendingCheck?.clarifyId) {
    answerClarify("other", other);
    return;
  }
  const original = pendingCheck?.text || "";
  hideCheckConfirm();
  const box = $("message");
  if (box) box.value = original ? `${original}\n(I meant: ${other})` : other;
  skipCheckConfirm = true;
  $("composer")?.requestSubmit();
}

function showCheckConfirm(payload, text, extra) {
  pendingCheck = { text, payload, clarifyId: extra?.clarifyId || "" };
  const box = $("check-confirm");
  const q = $("check-confirm-q");
  if (q) q.textContent = payload.question || "Which did you mean?";
  const actions = $("check-confirm-actions");
  if (actions) {
    actions.innerHTML = "";
    const opts = payload.options || [];
    let n = 0;
    let otherDrawn = false;
    opts.forEach((opt) => {
      if (isOtherOption(opt)) {
        if (otherDrawn) return;
        otherDrawn = true;
        const row = document.createElement("div");
        row.className = "gb-ask-other";
        row.dataset.askI = String(n);
        row.dataset.askId = "other";
        row.tabIndex = -1;
        const caret = document.createElement("span");
        caret.className = "gb-ask-caret";
        caret.textContent = " ";
        const key = document.createElement("span");
        key.className = "gb-ask-num";
        key.textContent = "z";
        const field = document.createElement("div");
        field.className = "gb-ask-copy";
        const lab = document.createElement("span");
        lab.className = "gb-ask-label";
        lab.textContent = "Other";
        const input = document.createElement("input");
        input.id = "check-confirm-other-input";
        input.type = "text";
        input.placeholder = "what are you referring to?";
        input.autocomplete = "off";
        input.setAttribute("aria-label", "Other");
        input.addEventListener("focus", () => setAskFocus(Number(row.dataset.askI)));
        input.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter") {
            ev.preventDefault();
            submitOtherMeaning();
          }
        });
        field.appendChild(lab);
        field.appendChild(input);
        row.appendChild(caret);
        row.appendChild(key);
        row.appendChild(field);
        actions.appendChild(row);
        n += 1;
        return;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "gb-ask-row";
      btn.dataset.askI = String(n);
      btn.dataset.askId = opt.id || "";
      btn.dataset.askLabel = opt.label || opt.id || "";
      const caret = document.createElement("span");
      caret.className = "gb-ask-caret";
      caret.textContent = " ";
      const num = document.createElement("span");
      num.className = "gb-ask-num";
      num.textContent = String(n + 1);
      const copy = document.createElement("span");
      copy.className = "gb-ask-copy";
      const lab = document.createElement("span");
      lab.className = "gb-ask-label";
      lab.textContent = opt.label || opt.id;
      copy.appendChild(lab);
      if (opt.hint) {
        const hint = document.createElement("span");
        hint.className = "gb-ask-hint";
        hint.textContent = opt.hint;
        copy.appendChild(hint);
      }
      btn.appendChild(caret);
      btn.appendChild(num);
      btn.appendChild(copy);
      btn.addEventListener("mouseenter", () => setAskFocus(Number(btn.dataset.askI)));
      btn.addEventListener("click", () => activateAskRow(Number(btn.dataset.askI)));
      actions.appendChild(btn);
      n += 1;
    });
  }
  if (box) box.hidden = false;
  setAskFocus(0);
  if (voiceChat.on) paintMic();
}

async function answerClarify(choice, label) {
  const id = pendingCheck?.clarifyId;
  const bid = state.selected;
  hideCheckConfirm();
  if (!bid || !id) return;
  try {
    await api(`/v1/bots/${bid}/clarify`, {
      method: "POST",
      body: JSON.stringify({ clarify_id: id, choice, label }),
    });
  } catch (err) {
    const b = state.bots.find((x) => x.id === bid);
    if (b) {
      b.messages.push({ role: "system", text: String(err.message || err) });
      renderConversation(b);
    }
  }
}

async function maybeConfirmSystemCheck(text) {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b || b.kind !== "teela-brain" || !text) return false;
  try {
    const prev = await api(`/v1/bots/${b.id}/check-confirm`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    if (!prev?.confirm) return false;
    showCheckConfirm(prev, text);
    return true;
  } catch {
    return false;
  }
}

function chooseCheckScope(scope) {
  if (!pendingCheck?.text) {
    hideCheckConfirm();
    return;
  }
  checkScopeToSend = scope;
  hideCheckConfirm();
  $("composer")?.requestSubmit();
}

function dismissAskCard() {
  const id = pendingCheck?.clarifyId;
  const bid = state.selected;
  hideCheckConfirm();
  if (id && bid) {
    api(`/v1/bots/${bid}/clarify`, {
      method: "POST",
      body: JSON.stringify({ clarify_id: id, cancel: true }),
    }).catch(() => {});
  }
}

document.addEventListener("keydown", (e) => {
  const box = $("check-confirm");
  if (!box || box.hidden) return;
  const inOther = e.target === $("check-confirm-other-input");
  if (e.key === "Escape") {
    e.preventDefault();
    if (inOther && pickOtherMeaning()) {
      const inp = $("check-confirm-other-input");
      if (inp) inp.value = "";
      return;
    }
    dismissAskCard();
    return;
  }
  if (inOther && !["ArrowUp", "ArrowDown", "Enter", "Escape"].includes(e.key)) return;
  if (e.key === "ArrowDown" || e.key === "j") {
    e.preventDefault();
    setAskFocus(askFocus + 1);
  } else if (e.key === "ArrowUp" || e.key === "k") {
    e.preventDefault();
    setAskFocus(askFocus - 1);
  } else if (e.key === "Enter") {
    e.preventDefault();
    activateAskRow(askFocus);
  } else if (e.key === "z" && !inOther) {
    e.preventDefault();
    const rows = askSelectable();
    const oi = rows.findIndex((el) => el.classList.contains("gb-ask-other"));
    if (oi >= 0) setAskFocus(oi);
  } else if (/^[1-9]$/.test(e.key) && !inOther) {
    e.preventDefault();
    activateAskRow(Number(e.key) - 1);
  }
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.selected) return;
  if (isWorking()) {
    if (voiceChat.on) {
      const held = $("message")?.value.trim();
      if (held) voiceChat.pendingSend = held;
    }
    return;
  }
  const text = $("message").value.trim();
  if (!text && !state.pendingImages.length) return;
  // Preflight awaits confirmation/model startup before the turn becomes working.
  // Claim submission synchronously so a second submit cannot pass that gap.
  if (state.composerSubmitting) return;
  state.composerSubmitting = true;
  try {
  if (voiceChat.on) setWorking(state.selected, true);
  if (skipCheckConfirm) skipCheckConfirm = false;
  else if (!checkScopeToSend && !voiceChat.on && (await maybeConfirmSystemCheck(text))) return;
  const confirmedScope = checkScopeToSend;
  checkScopeToSend = "";
  const b0 = state.bots.find((x) => x.id === state.selected);
  if (b0 && !localEngineReady(b0)) {
    const list = modelsForBot(b0);
    const hit = list.find((x) => x.id === b0.model);
    if (hit && isExclusiveVllmModel(hit) && isLocalOccupying(hit, list)) {
      if (voiceChat.on) setWorking(state.selected, false);
      return;
    }
    setWorking(state.selected, true);
    await controlLocalLlm("start", b0.model, b0);
    if (!localEngineReady(state.bots.find((x) => x.id === state.selected) || b0)) {
      setWorking(state.selected, false);
      return;
    }
  }
  if (text.startsWith("/") && b0 && botIsAgent(b0)) {
    const parsed = parseSlashLine(text);
    const row = parsed && !parsed.args && matchSlashCommand(slashCatalog(b0), parsed.cmdTok);
    if (row && slashHasSubfields(row)) {
      fillSlashComposer(`${row.cmd} `);
      syncSlashMenu();
      if (voiceChat.on) setWorking(state.selected, false);
      return;
    }
  }
  if (text.startsWith("/") && b0 && (await handleSlash(text, b0))) {
    $("message").value = "";
    autosizeComposer();
    if (voiceChat.on) setWorking(state.selected, false);
    return;
  }
  delete state.stopped[state.selected];
  setWorking(state.selected, true);
  const images = state.pendingImages.slice();
  $("message").value = "";
  autosizeComposer();
  state.pendingImages = [];
  renderPasteTray();
  window.DeskUI?.setEmotion(state.selected, "thinking", { persist: false, pop: true });
  document.querySelector(".tps-stat")?.classList.add("generating");
  $("tps-counter") && ($("tps-counter").textContent = "0.0");
  if (b0) {
    b0.tps = 0;
    b0.speed_source = "";
    b0.token_source = "";
  }
  const b = state.bots.find((x) => x.id === state.selected);
  if (b) {
    b.messages = b.messages || [];
    const pics = images.filter((i) => i.kind !== "video");
    const clips = images.filter((i) => i.kind === "video");
    // echoPending: the deskd prompt handler re-broadcasts this message over WS
    // (normalized via visible_user_text). The chat handler consumes that echo
    // instead of appending a second bubble.
    b.messages.push({
      role: "user",
      text: text || (pics.length ? "(image)" : "(attachment)"),
      images: pics.map((i) => ({ path: null, preview: i.url || `data:${i.mime};base64,${i.data}` })),
      attachments: clips.map((i) => ({ type: i.mime, url: i.url, name: i.name })),
      echoPending: true,
    });
    chatStickBottom = true;
    renderConversation(b);
    $("undo").disabled = false;
    b.can_undo = true;
  }
  try {
    await api(`/v1/agent/${state.selected}/prompt`, {
      method: "POST",
      body: JSON.stringify({
        text,
        page_id: PAGE_ID,
        confirmed_scope: confirmedScope || undefined,
        voice_chat: voiceChat.on || undefined,
        images: images.filter((i) => i.kind !== "video" && i.data),
        attachments: images.filter((i) => i.kind === "video" && i.data).map((i) => ({
          mime: i.mime,
          data: i.data,
          name: i.name,
        })),
      }),
    });
  } catch (err) {
    setWorking(state.selected, false);
    if (b) {
      b.messages.push({ role: "system", text: String(err.message || err) });
      renderConversation(b);
    }
  }
  } finally {
    state.composerSubmitting = false;
  }
});

$("message").addEventListener("keydown", (e) => {
  if (slashMenuKey(e)) return;
  if (e.key === "Escape" && isWorking()) {
    e.preventDefault();
    stopAgent();
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (isWorking()) return;
    $("composer").requestSubmit();
  }
});

$("message").addEventListener("paste", (e) => {
  ingestClipboardEvent(e);
});
$("composer")?.addEventListener("paste", (e) => {
  if (e.target === $("message")) return;
  ingestClipboardEvent(e);
});
document.addEventListener("paste", (e) => {
  if (e.defaultPrevented) return;
  const target = e.target;
  if (target === $("message") || target?.closest?.("#composer")) return;
  if (target && (target.closest?.("input, textarea, [contenteditable='true']"))) return;
  ingestClipboardEvent(e);
});
$("composer-wrap")?.addEventListener("dragover", (e) => {
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
});
$("composer-wrap")?.addEventListener("drop", async (e) => {
  const files = [...(e.dataTransfer?.files || [])].filter(isMediaFile);
  if (!files.length) return;
  e.preventDefault();
  for (const file of files) await addImageFile(file);
});

$("file-input").addEventListener("change", async (e) => {
  for (const f of e.target.files || []) await addImageFile(f);
  e.target.value = "";
});

$("theme-toggle").onclick = () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
};
$("win-close")?.addEventListener("click", () => {
  if ($("desk-window")) $("desk-window").hidden = true;
});

window.deskCreateBot = async (payload) => {
  const home = (payload.home_node || payload.peer || "").trim();
  const localNode = state.nodeName || "";
  let j;
  if (home && localNode && home !== localNode) {
    const body = { ...payload, peer: home };
    delete body.home_node;
    j = await api("/v1/cluster/create", { method: "POST", body: JSON.stringify(body) });
  } else {
    const body = { ...payload };
    delete body.home_node;
    delete body.peer;
    j = await api("/v1/bots", { method: "POST", body: JSON.stringify(body) });
  }
  await refreshBots();
  const id = j.bot_id || j.bot?.id;
  if (id) await selectBot(id);
  return id;
};
window.deskLoadSoul = async (id) => {
  const j = await api(`/v1/bots/${id}/soul`);
  return j.body || "";
};
window.deskLoadAccess = async () => {
  const j = await api("/v1/settings");
  if (j.listen_host) state.listenHost = j.listen_host;
  if (j.listen_port) state.listenPort = j.listen_port;
  if (j.access_url) state.accessUrl = j.access_url;
  if (j.node_name) state.nodeName = j.node_name;
  if (j.cluster_token_fp) state.clusterTokenFp = j.cluster_token_fp;
  if (j.peers) state.peers = j.peers;
  if (typeof j.voice === "boolean") { state.voice = j.voice; paintVoiceToggle(); }
  return j;
};
window.deskSaveAccess = async (body) => {
  const payload = { ...body };
  if (!state.isLoopback && !isLoopbackHost()) {
    delete payload.node_name;
    delete payload.cluster_token;
    delete payload.cluster_token_confirm;
    delete payload.cluster_token_clear;
    delete payload.peers;
    delete payload.peers_loaded;
  }
  const j = await api("/v1/settings", { method: "POST", body: JSON.stringify(payload) });
  if (j.listen_host) state.listenHost = j.listen_host;
  if (j.listen_port) state.listenPort = j.listen_port;
  if (j.access_url) state.accessUrl = j.access_url;
  if (j.node_name) state.nodeName = j.node_name;
  if (j.cluster_token_fp) state.clusterTokenFp = j.cluster_token_fp;
  if (j.peers) state.peers = j.peers;
  if (typeof j.voice === "boolean") { state.voice = j.voice; paintVoiceToggle(); }
  return j;
};
window.deskClearChat = async (id) => {
  const j = await api(`/v1/bots/${id}/clear`, { method: "POST", body: "{}" });
  const bot = j.bot;
  if (bot) {
    const i = state.bots.findIndex((x) => x.id === bot.id);
    if (i >= 0) state.bots[i] = { ...state.bots[i], ...bot, messages: bot.messages || [] };
    if (state.selected === bot.id) renderConversation(i >= 0 ? state.bots[i] : bot);
  } else {
    const live = state.bots.find((x) => x.id === id);
    if (live) {
      live.messages = [];
      live.can_undo = false;
      if (state.selected === id) renderConversation(live);
    }
  }
  $("undo") && ($("undo").disabled = true);
  renderRoster();
  if (state.selected === id) refreshTimeline();
  return j;
};
window.deskUpdateBot = async (id, payload) => {
  const j = await api(`/v1/bots/${id}`, { method: "POST", body: JSON.stringify(payload) });
  const bot = j.bot;
  if (bot) {
    const i = state.bots.findIndex((x) => x.id === bot.id);
    if (i >= 0) state.bots[i] = { ...state.bots[i], ...bot };
    else state.bots.push(bot);
    if (state.selected === bot.id) renderConversation(i >= 0 ? state.bots[i] : bot);
  }
  renderRoster();
  return bot;
};
window.deskSaveRoutine = async (routine) => {
  if (!state.selected) return;
  const body = {
    name: routine.name,
    instruction: routine.instruction,
    schedule: routine.schedule,
    enabled: routine.enabled,
    type: routine.type,
    timezone: routine.timezone,
    time: routine.time,
    cron: routine.cron,
    schedulePreset: routine.schedulePreset,
  };
  if (routine.id) {
    await api(`/v1/bots/${state.selected}/routines/${routine.id}`, { method: "POST", body: JSON.stringify(body) });
  } else {
    await api(`/v1/bots/${state.selected}/routines`, { method: "POST", body: JSON.stringify(body) });
  }
  await loadWorkspace(state.bots.find((x) => x.id === state.selected));
};
window.deskReconnectDesktop = async () => {
  if (!state.selected) return;
  const url = $("url-bar")?.value?.trim();
  if (url) {
    await api(`/v1/bots/${state.selected}/browser/navigate`, {
      method: "POST",
      body: JSON.stringify({ url }),
    });
  }
  frameSeq = "";
  await refreshBrowserFrame();
  for (const k of ["shell", "tui"]) {
    if (terms[k]) {
      try {
        terms[k].fit?.fit();
      } catch {
        /* ignore */
      }
    }
  }
};

window.deskDeleteBot = async (id) => {
  const bid = id || state.selected;
  const b = state.bots.find((x) => x.id === bid);
  if (!b) return false;
  const where = b.remote && b.node ? `Deletes workspace on ${b.node}, not this machine. ` : "";
  if (!confirm(`Delete ${b.name}? ${where}This removes only that bot’s workspace, browser, memory, and chat. Other bots are untouched.`)) return false;
  await fetch(`/v1/bots/${b.id}`, { method: "DELETE", headers: headers() });
  state.bots = state.bots.filter((x) => x.id !== b.id);
  if (state.selected === b.id) {
    state.selected = null;
    $("conv-name").textContent = "Select a bot";
    $("transcript").innerHTML = "";
    syncMoreMenu();
    $("undo").disabled = true;
    updateJumpLatest();
  }
  $("more-menu").hidden = true;
  updateSendButton();
  renderRoster();
  return true;
};
$("edit-soul").onclick = async () => {
  if (!state.selected) return;
  $("more-menu").hidden = true;
  if (window.DeskUI?.openAgentModal) {
    DeskUI.openAgentModal(state.selected);
    return;
  }
  const j = await api(`/v1/bots/${state.selected}/soul`);
  $("soul-body").value = j.body;
  $("soul-dialog").showModal();
};

function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  let s = "";
  for (let i = 0; i < bytes.length; i += chunk) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(s);
}

async function downloadExport(fmt) {
  if (!state.selected) return;
  const r = await fetch(`/v1/bots/${state.selected}/export?format=${encodeURIComponent(fmt)}`, {
    headers: headers(),
  });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw new Error(j.error || r.statusText);
  }
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename="([^"]+)"/.exec(cd);
  const name = m ? m[1] : (fmt === "system" ? "bot-system.zip" : `conversation.${fmt === "md" ? "md" : "json"}`);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

function syncMoreMenu() {
  const picker = document.querySelector(".more-picker");
  picker?.classList.toggle("no-bot", !state.selected);
  if ($("more-btn")) $("more-btn").hidden = false;
  if (!state.selected && $("more-menu")) $("more-menu").hidden = true;
}

$("more-btn").onclick = (e) => {
  e.preventDefault();
  e.stopPropagation();
  $("more-menu").hidden = !$("more-menu").hidden;
};
$("more-menu").addEventListener("click", async (e) => {
  const fmtBtn = e.target.closest("button[data-fmt]");
  if (fmtBtn) {
    e.preventDefault();
    $("more-menu").hidden = true;
    try {
      await downloadExport(fmtBtn.dataset.fmt);
    } catch (err) {
      alert(err.message || err);
    }
    return;
  }
  if (e.target.closest("button")) $("more-menu").hidden = true;
});

$("import-file").addEventListener("change", async (e) => {
  const file = (e.target.files || [])[0];
  e.target.value = "";
  if (!file || !state.selected) return;
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b) return;
  let mode = $("import-file").dataset.replace === "1" ? "replace" : "append";
  if (mode === "replace") {
    if (!confirm(`Replace the current conversation on ${b.name} with “${file.name}”?`)) return;
  } else if ((b.messages || []).length) {
    if (!confirm(`Import “${file.name}” into ${b.name}? Existing messages stay; imported turns are appended.`)) return;
  }
  const zip = /\.zip$/i.test(file.name) || file.type === "application/zip";
  const payload = { filename: file.name, mode };
  try {
    if (zip) {
      payload.content_b64 = bufToB64(await file.arrayBuffer());
    } else {
      payload.text = await file.text();
    }
    const j = await api(`/v1/bots/${b.id}/import`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (j.bot) {
      const i = state.bots.findIndex((x) => x.id === b.id);
      if (i >= 0) state.bots[i] = j.bot;
    }
    const live = state.bots.find((x) => x.id === b.id);
    if (live) renderConversation(live);
    const n = j.imported || 0;
    const src = j.source || "file";
    alert(`Imported ${n} message${n === 1 ? "" : "s"} from ${src}.`);
  } catch (err) {
    alert(err.message || err);
  }
});
$("soul-cancel").onclick = () => $("soul-dialog").close();
$("soul-save").onclick = async (e) => {
  e.preventDefault();
  const j = await api(`/v1/bots/${state.selected}/soul`, {
    method: "PUT",
    body: JSON.stringify({ body: $("soul-body").value }),
  });
  $("soul-dialog").close();
  if (j.bot) {
    const i = state.bots.findIndex((x) => x.id === j.bot.id);
    if (i >= 0) state.bots[i] = { ...state.bots[i], ...j.bot };
    if (state.selected === j.bot.id) renderConversation(i >= 0 ? state.bots[i] : j.bot);
    renderRoster();
  }
};

$("takeover").onclick = async () => {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b) return;
  await api(`/v1/control/${b.workspace_id}`, {
    method: "POST",
    body: JSON.stringify({ state: "user_controlled" }),
  });
  window.DeskUI?.enterTakeOver();
  await selectBot(b.id);
  setSurface("browser");
  grabScreenKeys();
};
$("im-done").onclick = async () => {
  const b = state.bots.find((x) => x.id === state.selected);
  if (!b) return;
  driveScreen = false;
  $("chrome-window")?.classList.remove("is-driving");
  window.DeskUI?.exitTakeOver();
  await api(`/v1/control/${b.workspace_id}`, {
    method: "POST",
    body: JSON.stringify({ state: "agent_controlled" }),
  });
  await selectBot(b.id);
};
$("takeover-esc")?.addEventListener("click", () => {
  enqueueBrowser({ type: "keyDown", key: "Escape", code: "Escape", vk: 27, modifiers: 0 });
  enqueueBrowser({ type: "keyUp", key: "Escape", code: "Escape", vk: 27, modifiers: 0 });
});

$("search").addEventListener("input", renderRoster);

function closeModelMenu() {
  const menu = $("model-menu");
  if (menu) menu.hidden = true;
  $("model-select-btn")?.setAttribute("aria-expanded", "false");
}

function closeFloatingMenus() {
  closeModelMenu();
  const more = $("more-menu");
  if (more) more.hidden = true;
}

function toggleModelMenu() {
  const menu = $("model-menu");
  const btn = $("model-select-btn");
  const b = state.bots.find((x) => x.id === state.selected);
  if (!menu || !b) return;
  if (!menu.hidden) {
    closeModelMenu();
    return;
  }
  renderModelMenu(b);
  menu.hidden = false;
  if (btn) btn.setAttribute("aria-expanded", "true");
  placeModelMenu();
  refreshLocalModelCatalog(b).then(() => {
    if (!menu.hidden) {
      renderModelMenu(b);
      placeModelMenu();
    }
  }).catch(() => {});
}

$("model-select-btn")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  toggleModelMenu();
});

$("meta-model")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  toggleModelMenu();
});
document.addEventListener("pointerdown", (e) => {
  const el = e.target instanceof Element ? e.target : e.target?.parentElement;
  if (el?.closest(".model-select-wrap") || el?.closest(".model-picker") || el?.closest("#model-menu")) return;
  closeModelMenu();
  if (!el?.closest(".more-picker")) {
    const more = $("more-menu");
    if (more) more.hidden = true;
  }
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const menu = $("model-menu");
  const more = $("more-menu");
  if ((menu && !menu.hidden) || (more && !more.hidden)) {
    e.preventDefault();
    e.stopPropagation();
    closeFloatingMenus();
  }
}, true);
window.addEventListener("resize", () => {
  if ($("model-menu") && !$("model-menu").hidden) placeModelMenu();
});
window.addEventListener("blur", () => {
  closeFloatingMenus();
});

document.addEventListener("desk-app", (e) => {
  const surface = e.detail?.surface;
  if (!surface) return;
  state.surface = surface;
  if (surface === "shell") startTerm("shell");
  if (surface === "tui") startTerm("tui");
  if (surface === "browser") refreshBrowserFrame();
  requestAnimationFrame(fitTerms);
});

$("url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (!state.selected) return;
  const url = $("url-bar").value.trim();
  if (!url) return;
  driveScreen = false;
  setSurface("browser");
  const go = document.querySelector("#url-form .browser-go");
  if (go) go.disabled = true;
  try {
    const j = await api(`/v1/bots/${state.selected}/browser/navigate`, {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    if (j.url) $("url-bar").value = j.url;
  } catch (err) {
    alert(err.message || err);
    return;
  } finally {
    if (go) go.disabled = false;
  }
  frameSeq = "";
  await refreshBrowserFrame();
});

const VK = {
  Backspace: 8, Tab: 9, Enter: 13, Shift: 16, Control: 17, Alt: 18, Escape: 27,
  " ": 32, PageUp: 33, PageDown: 34, End: 35, Home: 36,
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  Insert: 45, Delete: 46, Meta: 91,
};
const inputQueue = [];
let lastClickAt = 0;
let lastClickXY = { x: 0, y: 0 };
let lastMoveAt = 0;

function browserMods(e) {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

function mapBrowserXY(e) {
  const img = $("browser-frame");
  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth || state.browserW || 1280;
  const nh = img.naturalHeight || state.browserH || 800;
  const vw = state.browserW || nw;
  const vh = state.browserH || nh;
  const scale = Math.min(rect.width / nw, rect.height / nh) || 1;
  const dw = nw * scale;
  const dh = nh * scale;
  const ox = rect.left + (rect.width - dw) / 2;
  const oy = rect.top + (rect.height - dh) / 2;
  const px = (e.clientX - ox) / scale;
  const py = (e.clientY - oy) / scale;
  return {
    x: Math.max(0, Math.min(vw, (px / nw) * vw)),
    y: Math.max(0, Math.min(vh, (py / nh) * vh)),
  };
}

function mouseButton(e) {
  if (e.button === 1) return "middle";
  if (e.button === 2) return "right";
  if (e.button === 3) return "back";
  if (e.button === 4) return "forward";
  return "left";
}

let inputBusy = false;

function enqueueBrowser(ev) {
  if (!state.selected) return;
  if (ev.type === "mouseMoved") {
    for (let i = inputQueue.length - 1; i >= 0; i--) {
      if (inputQueue[i].type === "mouseMoved") inputQueue.splice(i, 1);
    }
  } else if (ev.type === "insertText" && ev.text) {
    const last = inputQueue[inputQueue.length - 1];
    if (last && last.type === "insertText") {
      last.text += ev.text;
      pumpBrowserInput();
      return;
    }
  }
  inputQueue.push(ev);
  pumpBrowserInput();
}

function pumpBrowserInput() {
  if (inputBusy || !inputQueue.length || !state.selected) return;
  inputBusy = true;
  const events = inputQueue.splice(0, 32);
  const bid = state.selected;
  fetch(`/v1/bots/${bid}/browser/input`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ events }),
    keepalive: true,
  })
    .catch(() => {})
    .finally(() => {
      inputBusy = false;
      if (inputQueue.length) pumpBrowserInput();
    });
}

let driveScreen = false;

function browserKeySink() {
  return $("browser-keys") || $("browser-hit");
}

function stopDrivingScreen() {
  driveScreen = false;
  $("chrome-window")?.classList.remove("is-driving");
}

function grabScreenKeys() {
  driveScreen = true;
  $("chrome-window")?.classList.add("is-driving");
  $("url-bar")?.blur();
  $("message")?.blur();
  const keys = $("browser-keys");
  const hit = $("browser-hit");
  const sink = keys || hit;
  if (!sink) return;
  if (keys) keys.value = "";
  const focusEl = (el) => {
    if (!el) return;
    try {
      el.focus({ preventScroll: true });
    } catch {
      el.focus();
    }
  };
  focusEl(sink);
  if (document.activeElement !== sink) focusEl(hit);
}

function sendScreenKey(e, phase) {
  if (e.isComposing || e.key === "Process" || e.keyCode === 229) return;
  const mods = browserMods(e);
  const vk = e.keyCode || VK[e.key] || 0;
  const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
  if (phase === "down") {
    if (printable) {
      enqueueBrowser({
        type: "rawKeyDown",
        key: e.key,
        code: e.code,
        vk,
        modifiers: mods,
      });
      enqueueBrowser({ type: "insertText", text: e.key });
      return;
    }
    enqueueBrowser({
      type: "keyDown",
      key: e.key,
      code: e.code,
      text: e.key === "Enter" ? "\r" : "",
      vk,
      modifiers: mods,
    });
    return;
  }
  enqueueBrowser({
    type: "keyUp",
    key: e.key,
    code: e.code,
    vk,
    modifiers: mods,
  });
}

(function bindBrowserDrive() {
  const hit = $("browser-hit");
  const keys = $("browser-keys");
  const pad = keys || hit;
  if (!pad) return;
  const onPage = (t) => t === hit || t === keys || t?.closest?.("#browser-stage");
  pad.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    setSurface("browser");
    grabScreenKeys();
    e.preventDefault();
    try {
      pad.setPointerCapture(e.pointerId);
    } catch {
      /* capture is optional */
    }
    const { x, y } = mapBrowserXY(e);
    const now = Date.now();
    const close = Math.hypot(x - lastClickXY.x, y - lastClickXY.y) < 6 && now - lastClickAt < 400;
    const n = close ? 2 : 1;
    lastClickAt = now;
    lastClickXY = { x, y };
    const mods = browserMods(e);
    const button = mouseButton(e);
    enqueueBrowser({ type: "mouseMoved", x, y, modifiers: mods });
    enqueueBrowser({
      type: "mousePressed",
      x, y,
      button,
      clickCount: n,
      modifiers: mods,
    });
  });
  pad.addEventListener("pointermove", (e) => {
    const now = performance.now();
    if (!e.buttons && now - lastMoveAt < 24) return;
    lastMoveAt = now;
    const { x, y } = mapBrowserXY(e);
    enqueueBrowser({ type: "mouseMoved", x, y, modifiers: browserMods(e) });
  });
  const release = (e) => {
    const { x, y } = mapBrowserXY(e);
    enqueueBrowser({
      type: "mouseReleased",
      x, y,
      button: mouseButton(e),
      clickCount: 1,
      modifiers: browserMods(e),
    });
    setTimeout(grabScreenKeys, 0);
  };
  pad.addEventListener("pointerup", release);
  pad.addEventListener("pointercancel", release);
  pad.addEventListener("contextmenu", (e) => e.preventDefault());
  pad.addEventListener("wheel", (e) => {
    e.preventDefault();
    grabScreenKeys();
    const { x, y } = mapBrowserXY(e);
    enqueueBrowser({
      type: "mouseWheel",
      x, y,
      deltaX: e.deltaX,
      deltaY: e.deltaY,
      modifiers: browserMods(e),
    });
  }, { passive: false });

  document.addEventListener("pointerdown", (e) => {
    if (onPage(e.target)) return;
    stopDrivingScreen();
  });
  document.addEventListener("keydown", (e) => {
    if (!driveScreen) return;
    if ($("os-login") && !$("os-login").hidden) return;
    if (e.isComposing || e.key === "Process" || e.keyCode === 229) return;
    if (e.ctrlKey || e.metaKey) {
      if (e.key === "v" || e.key === "V") return;
    }
    const sink = browserKeySink();
    if (document.activeElement !== sink && document.activeElement !== hit) grabScreenKeys();
    e.preventDefault();
    e.stopPropagation();
    sendScreenKey(e, "down");
  }, true);
  document.addEventListener("keyup", (e) => {
    if (!driveScreen) return;
    if (e.isComposing || e.key === "Process" || e.keyCode === 229) return;
    e.preventDefault();
    sendScreenKey(e, "up");
  }, true);
  document.addEventListener("paste", (e) => {
    if (!driveScreen) return;
    const t = e.clipboardData?.getData("text") || "";
    if (!t) return;
    e.preventDefault();
    enqueueBrowser({ type: "insertText", text: t });
    if (keys) keys.value = "";
  }, true);
  keys?.addEventListener("input", () => {
    if (!driveScreen || !keys.value) return;
    enqueueBrowser({ type: "insertText", text: keys.value });
    keys.value = "";
  });
  $("url-bar")?.addEventListener("focus", stopDrivingScreen);
  $("message")?.addEventListener("focus", stopDrivingScreen);
})();

function setSurface(name) {
  state.surface = name;
  const appName = window.DeskUI?.appForSurface?.(name) || name;
  if (window.DeskUI?.openWindow) DeskUI.openWindow(appName);
  if (name === "shell") startTerm("shell");
  if (name === "tui") startTerm("tui");
  if (name === "browser") refreshBrowserFrame();
  requestAnimationFrame(fitTerms);
}

function browserWindowLive() {
  if (window.DeskUI?.isWindowLive) return DeskUI.isWindowLive("browser");
  return state.surface === "browser";
}

function b64ToBytes(b64) {
  const bin = atob(b64 || "");
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function startTerm(kind) {
  const host = $(kind === "tui" ? "tui-term" : "shell-term");
  if (!host) return;
  if (terms[kind]) {
    try {
      terms[kind].fit?.fit();
    } catch {
      /* layout not ready */
    }
    return;
  }
  const Term = window.Terminal;
  if (!Term) {
    host.textContent = "Terminal library failed to load. Refresh the page.";
    return;
  }
  try {
    const term = new Term({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "IBM Plex Mono, ui-monospace, monospace",
      theme: { background: "#0c0d10", foreground: "#e8e6e1" },
      convertEol: kind === "shell",
      scrollback: kind === "tui" ? 0 : 4000,
    });
    const FitCls = window.FitAddon?.FitAddon || window.FitAddon;
    const fit = FitCls ? new FitCls() : null;
    if (fit) term.loadAddon(fit);
    term.open(host);
    terms[kind] = { term, fit, seq: 0 };
    term.onData((d) => {
      if (!state.selected) return;
      api(`/v1/bots/${state.selected}/${kind}/input`, {
        method: "POST",
        body: JSON.stringify({ data: d }),
      }).catch(() => {});
    });
    window.addEventListener("resize", () => requestAnimationFrame(fitTerms));
    requestAnimationFrame(fitTerms);
    pollPty(kind);
  } catch (err) {
    host.textContent = String(err);
  }
}

async function pollPty(kind) {
  const slot = terms[kind];
  if (!slot || !state.selected) {
    setTimeout(() => pollPty(kind), 800);
    return;
  }
  try {
    const j = await api(`/v1/bots/${state.selected}/${kind}/pull?after=${slot.seq}`);
    const bytes = b64ToBytes(j.data || "");
    if (bytes.length) slot.term.write(bytes);
    slot.seq = j.seq;
  } catch {
    /* bot not selected / empty */
  }
  const live = window.DeskUI?.isWindowLive
    ? DeskUI.isWindowLive(kind === "tui" ? "hermes" : "terminal")
    : state.surface === kind;
  setTimeout(() => pollPty(kind), live ? 180 : 1500);
}

let frameSeq = "";
let frameObj = "";
let infoAt = 0;

async function refreshBrowserFrame() {
  if (!state.selected) return;
  const headers = { Authorization: `Bearer ${state.token}` };
  if (frameSeq) headers["If-None-Match"] = frameSeq;
  try {
    const r = await fetch(`/v1/bots/${state.selected}/browser/frame`, {
      headers,
      cache: "no-store",
    });
    const vw = parseInt(r.headers.get("X-View-Width") || "", 10);
    const vh = parseInt(r.headers.get("X-View-Height") || "", 10);
    if (vw) state.browserW = vw;
    if (vh) state.browserH = vh;
    if (r.status === 200) {
      const blob = await r.blob();
      if (blob.size > 24) {
        const url = URL.createObjectURL(blob);
        const img = $("browser-frame");
        const ready = new Image();
        ready.onload = () => {
          if (img) img.src = url;
          if (frameObj) URL.revokeObjectURL(frameObj);
          frameObj = url;
        };
        ready.onerror = () => URL.revokeObjectURL(url);
        ready.src = url;
        frameSeq = r.headers.get("ETag") || r.headers.get("X-Frame-Seq") || frameSeq;
      }
    }
  } catch {
    /* ignore */
  }
  const now = Date.now();
  if (now - infoAt > 1200) {
    infoAt = now;
    api(`/v1/bots/${state.selected}/browser/info`)
      .then((j) => {
        if (j.width) state.browserW = j.width;
        if (j.height) state.browserH = j.height;
        if (j.url && document.activeElement !== $("url-bar")) $("url-bar").value = j.url;
      })
      .catch(() => {});
  }
}

function loopBrowser() {
  const tick = async () => {
    if (browserWindowLive() && state.selected) await refreshBrowserFrame();
    browserTimer = setTimeout(tick, browserWindowLive() ? 250 : 1500);
  };
  tick();
}

let eventSource = null;
let eventReconnectTimer = 0;
let eventLastSeen = 0;      // Date.now() of the last SSE message (incl. pings)
let eventWatchdog = 0;      // interval id for the dead-stream watchdog
let liveBotPoller = 0;      // profile fallback for silent mobile SSE streams
const EVENT_WATCHDOG_MS = 45000;   // pings arrive every ~30s when idle
const EVENT_RECONNECT_AFTER_MS = 20000; // force-retry a dead stream sooner
function connectEvents() {
  if (eventReconnectTimer) {
    clearTimeout(eventReconnectTimer);
    eventReconnectTimer = 0;
  }
  if (eventSource) {
    try {
      eventSource.close();
    } catch {
      /* already closed */
    }
    eventSource = null;
  }
  eventLastSeen = Date.now();
  const es = new EventSource(`/v1/events`);
  eventSource = es;
  es.onmessage = (ev) => {
    eventLastSeen = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "ping") {
      // Server heartbeat (idle stream). Nothing to render; the watchdog
      // already used it as proof the stream is alive.
      return;
    }
    if (msg.type === "voice" && typeof msg.voice === "boolean" && msg.voice !== state.voice) {
      state.voice = msg.voice;
      paintVoiceToggle();
      if (!msg.voice) stopSpeak();
    }
    if (msg.type === "status") {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (b) {
        const incoming = String(msg.text || "");
        if (/^reconnecting/i.test(incoming) && b.status && !/^reconnecting/i.test(b.status) && !/^agent exited/i.test(b.status)) {
          /* keep the live turn status; a respawn storm must not blank the chat */
        } else {
          b.status = msg.text;
        }
        if (/Thinking|Working|Speaking|Using /i.test(String(b.status || ""))) {
          if (!state.stopped[b.id]) setWorking(b.id, true);
        } else setWorking(b.id, false);
        b.surface = msg.surface;
        b.control = msg.control || b.control;
        window.DeskUI?.syncEmotionFromStatus(b);
        if (String(b.status || "").toLowerCase().includes("ready")) {
          document.querySelector(".tps-stat")?.classList.remove("generating");
        }
        renderRoster();
        if (state.selected === b.id) {
          syncChatStatusLine(b);
          $("conv-status").textContent = conversationSubtitle(b);
          if (b.control === "user_controlled") {
            $("control-banner").hidden = false;
            $("control-banner").textContent = `${b.name.toUpperCase()} — YOU HAVE CONTROL. Click the page and type (captcha / check-if-human).`;
            $("takeover").hidden = true;
            $("im-done").hidden = false;
            document.body.classList.add("desktop-takeover-active");
          } else {
            $("control-banner").hidden = true;
            $("takeover").hidden = false;
            $("im-done").hidden = true;
            document.body.classList.remove("desktop-takeover-active");
          }
          if (observerBotId && msg.surface) {
            setSurface(msg.surface);
          }
          // User MiniOS windows stay as the user left them. Agent browser/tool
          // activity does not auto-open Browser; open it from the dock or URL bar.
        }
      }
    }
    if (msg.type === "session.update" && msg.bot_id === state.selected) {
      const u = msg.update || {};
      const kind = u.sessionUpdate;
      const text = u.content?.text || "";
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (!b) return;
      b.messages = b.messages || [];
      if (kind === "agent_message_chunk" && text) {
        const last = b.messages[b.messages.length - 1];
        const visible = stripAssistantPadding(text);
        const head = (s) => String(s || "").slice(0, STREAM_RESTART_HEAD);
        const sameReply = last && last.role === "assistant" && visible && (
          last.open
          || (last.text || "").startsWith(head(visible))
          || visible.startsWith(head(last.text))
        );
        if (sameReply) {
          last.text = stripAssistantPadding(mergeAssistantStream(last.text || "", text));
          last.open = true;
        } else if (visible) {
          if (last && last.open) last.open = false;
          b.messages.push({ role: "assistant", text: visible, open: true });
          window.DeskUI?.setEmotion(b.id, "speaking", { persist: false, pop: false });
        }
        document.querySelector(".tps-stat")?.classList.add("generating");
        renderConversation(b);
      } else if (applySessionTurn(b, u)) {
        renderConversation(b);
        if (kind === "turn_completed" || kind === "response_completed") {
          const finalMsg = b.messages[b.messages.length - 1];
          // Only the page that sent the prompt claims audio for the reply.
          // Teammate DMs stay in the transcript — Jade is Teela's user voice.
          if (msg.page_id === PAGE_ID && finalMsg?.role === "assistant" && finalMsg.text) {
            speak(b.id, finalMsg.text, finalMsg);
          }
        }
      }
    }
    if (msg.type === "desktop.observer" && msg.bot_id === state.selected) {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (b) {
        b.visual_observer = !!msg.ready;
        renderMeta(b);
      }
    }
    if (msg.type === "clarify" && msg.bot_id === state.selected) {
      setWorking(msg.bot_id, false);
      showCheckConfirm(
        { question: msg.question, options: msg.options || [], guess: msg.guess },
        "",
        { clarifyId: msg.clarify_id }
      );
    }
    if (msg.type === "desktop.action") {
      applyDesktopAction(msg);
    }
    if (msg.type === "virtual.body_action" && msg.bot_id === state.selected) {
      connectVirtualBodyWs(state.selected);
      $("app-preview-frame")?.contentWindow?.postMessage({
        type: "virtual-body-action",
        action_id: msg.action_id,
        skill: msg.skill,
        parameters: msg.parameters || msg,
      }, "*");
    }
    if (msg.type === "desktop.capture-request" && msg.bot_id === state.selected) {
      postMiniosView();
    }
    if (msg.type === "dev.result" && msg.bot_id === state.selected) {
      const out = $("dev-output");
      if (out) {
        out.className = `minios-dev-output ${msg.ok ? "pass" : "fail"}`;
        out.textContent = `> ${msg.command}\n[exit ${msg.returncode} • ${msg.elapsed}s]\n\n${msg.output || "(no output)"}`;
      }
    }
    if (msg.type === "dev.process" && msg.bot_id === state.selected) {
      state.dev = state.dev || { kind: "generic", commands: {} };
      state.dev.process = msg;
      renderDevSystem(state.dev);
      const out = $("dev-output");
      if (out) {
        out.className = `minios-dev-output ${msg.running ? "" : ((msg.returncode ?? 0) === 0 ? "pass" : "fail")}`;
        out.textContent = `> ${msg.command || "app"}\n[${msg.running ? `running • pid ${msg.pid}` : `exit ${msg.returncode ?? "?"}`}]\n\n${msg.output || "(no output yet)"}`;
      }
    }
    if (msg.type === "workspace" && msg.bot_id === state.selected) {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (b) loadWorkspace(b);
    }
    if (msg.type === "routines" && msg.bot_id === state.selected) {
      renderRoutines(msg.routines || []);
    }
    if (msg.type === "chat" && msg.bot_id === state.selected) {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (!b) return;
      b.messages = b.messages || [];
      const last = b.messages[b.messages.length - 1];
      if (last && last.role === msg.role) {
        const sameVia = (last.via || "") === (msg.via || "");
        if (last.text === msg.text && sameVia) {
          if (msg.images?.length) last.images = mergeChatImages(last.images, msg.images);
          if (msg.attachments?.length) last.attachments = msg.attachments;
          if (msg.role === "assistant") setWorking(b.id, false);
          delete last.echoPending;
          return;
        }
        // deskd re-broadcasts the user message normalized (visible_user_text
        // collapses 3+ newlines and drops media-save lines). Adopt the echo as
        // the authoritative text of our optimistic bubble instead of appending
        // a second one.
        const normEcho = (s) => visibleUserText(s).replace(/\n{3,}/g, "\n\n");
        const ownEcho = msg.role === "user" && sameVia && last.echoPending
          && (visibleUserText(last.text || "") === visibleUserText(msg.text || "")
              || normEcho(last.text || "") === normEcho(msg.text || ""));
        if (ownEcho) {
          last.text = msg.text;
          delete last.echoPending;
          if (msg.images?.length) last.images = mergeChatImages(last.images, msg.images);
          if (msg.attachments?.length) last.attachments = msg.attachments;
          renderConversation(b);
          if (state.timelineOpen) refreshTimeline();
          else updateActiveChatMeta(b);
          return;
        }
        const optimisticPaste = msg.role === "user" && !last.via && !msg.via
          && (last.images || []).length && (msg.images || []).length;
        if (optimisticPaste) {
          last.text = visibleUserText(msg.text) || last.text;
          last.images = mergeChatImages(last.images, msg.images);
          if (msg.attachments?.length) last.attachments = msg.attachments;
          renderConversation(b);
          if (state.timelineOpen) refreshTimeline();
          else updateActiveChatMeta(b);
          return;
        }
        if (last.role === "assistant" && (last.open || sameVia) && (msg.text === last.text || msg.text.startsWith(last.text) || last.text.startsWith(msg.text))) {
          if ((msg.text || "").length >= (last.text || "").length) last.text = msg.text;
          last.open = false;
          if (msg.via) last.via = msg.via;
          if (msg.peer) last.peer = msg.peer;
          document.querySelector(".tps-stat")?.classList.remove("generating");
          window.DeskUI?.setEmotion(b.id, window.DeskUI?.inferEmotion(last.text) || "happy", { persist: false, pop: true });
          setWorking(b.id, false);
          renderConversation(b);
          if (msg.page_id === PAGE_ID) speak(b.id, last.text, last);
          if (state.timelineOpen) refreshTimeline();
          else updateActiveChatMeta(b);
          return;
        }
      }
      b.messages.push({
        role: msg.role,
        text: msg.text,
        via: msg.via,
        peer: msg.peer,
        images: msg.images,
        attachments: msg.attachments,
      });
      document.querySelector(".tps-stat")?.classList.remove("generating");
      if (msg.role === "assistant") {
        const emo = window.DeskUI?.inferEmotion(msg.text) || "happy";
        window.DeskUI?.setEmotion(b.id, emo, { persist: false, pop: true });
        setWorking(b.id, false);
        renderConversation(b);
        if (msg.page_id === PAGE_ID) speak(b.id, msg.text, msg);
      } else {
        window.DeskUI?.settleEmotion(b.id);
        renderConversation(b);
      }
      if (state.timelineOpen) refreshTimeline();
      else updateActiveChatMeta(b);
    }
    if ((msg.type === "conversation.undone" || msg.type === "conversation.cleared") && msg.bot) {
      const i = state.bots.findIndex((x) => x.id === msg.bot.id);
      if (i >= 0) state.bots[i] = { ...state.bots[i], ...msg.bot };
      if (state.selected === msg.bot.id) {
        renderConversation(i >= 0 ? state.bots[i] : msg.bot);
        refreshTimeline();
      }
    }
    if (msg.type === "conversation.imported" && msg.bot) {
      const i = state.bots.findIndex((x) => x.id === msg.bot.id);
      if (i >= 0) state.bots[i] = { ...state.bots[i], ...msg.bot };
      if (state.selected === msg.bot.id) {
        renderConversation(i >= 0 ? state.bots[i] : msg.bot);
        refreshTimeline();
      }
    }
    if (msg.type === "chats.updated" && msg.bot_id === state.selected) {
      applyTimelinePayload(msg);
    }
    if (msg.type === "session.commands" && msg.bot_id) {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (b) b.slash_commands = msg.commands || [];
      if (state.selected === msg.bot_id) syncSlashMenu();
    }
    if ((msg.type === "bot.created" || msg.type === "bot.updated") && msg.bot) {
      const nb = msg.bot;
      const i = state.bots.findIndex((x) => x.id === nb.id);
      if (i >= 0) state.bots[i] = mergeBot(state.bots[i], nb);
      else state.bots.push(mergeBot(null, nb));
      renderRoster();
      if (msg.type === "bot.updated" && state.selected === nb.id) {
        renderConversation(i >= 0 ? state.bots[i] : nb);
      }
    }
    if (msg.type === "cluster.peer") {
      const name = msg.node;
      if (Array.isArray(state.peers)) {
        const row = state.peers.find((p) => p.name === name);
        if (row) row.status = msg.status;
      }
      refreshBots().then(() => {
        const b = state.bots.find((x) => x.id === state.selected);
        if (b && b.node === msg.node) selectBot(state.selected);
      }).catch(() => {});
    }
    if (msg.type === "usage") {
      const b = state.bots.find((x) => x.id === msg.bot_id);
      if (b) {
        if (typeof msg.used === "number" && Number.isFinite(msg.used) && msg.used >= 0) b.context_used = msg.used;
        if (msg.window) b.context_window = msg.window;
        if (msg.models) b.models = msg.models;
        if (msg.context_source) b.context_source = msg.context_source;
        if (typeof msg.tps === "number" && Number.isFinite(msg.tps) && msg.tps >= 0) b.tps = msg.tps;
        if (msg.speed_source) b.speed_source = msg.speed_source;
        if (msg.token_source) b.token_source = msg.token_source;
        if (msg.pressure) b.wm_pressure = msg.pressure;
        if (typeof msg.utilization === "number" && Number.isFinite(msg.utilization)) b.wm_utilization = msg.utilization;
        if (typeof msg.free_tokens === "number" && Number.isFinite(msg.free_tokens)) b.wm_free = msg.free_tokens;
        if (state.selected === b.id) renderMeta(b);
        if (window.WorkingMemory) WorkingMemory.onUsage(msg, b);
      }
    }
    if (msg.type === "models.updated" && Array.isArray(msg.models)) {
      state.catalog = mergePickerModels(msg.models, state.catalog);
      for (const b of state.bots) b.models = mergePickerModels(msg.models, b.models);
      const cur = state.bots.find((x) => x.id === state.selected);
      if (cur) renderMeta(cur);
    }
    if (msg.type === "bot.deleted") {
      state.bots = state.bots.filter((x) => x.id !== msg.bot_id);
      if (state.selected === msg.bot_id) {
        state.selected = null;
        state.timeline = { activeId: null, chats: [] };
        $("conv-name").textContent = "Select a bot";
        $("transcript").innerHTML = "";
        syncMoreMenu();
        $("undo").disabled = true;
        updateJumpLatest();
        renderChatTimeline();
      }
      renderRoster();
    }
  };
  es.onerror = () => {
    if (eventSource !== es) return;
    es.close();
    eventSource = null;
    if (eventReconnectTimer) return;
    // Genuine error (network down, server restart): reconnect fast.
    eventReconnectTimer = setTimeout(connectEvents, 2000);
  };
  startEventWatchdog();
}

// A silently dead SSE stream (Wi-Fi roam, NAT timeout, phone sleep) never
// fires onerror, so EventSource's own auto-reconnect never kicks in and the
// page stops receiving chat replies. The server pings every ~30s when idle,
// so if nothing (ping or real event) has arrived for EVENT_WATCHDOG_MS while
// the socket still looks open, force a reconnect.
function startLiveBotPoller() {
  if (liveBotPoller) return;
  liveBotPoller = setInterval(async () => {
    const bid = state.selected;
    if (!bid) return;
    try {
      const profile = await api(`/v1/bots/${bid}`);
      const b = state.bots.find((x) => x.id === bid);
      if (!b || !profile) return;
      const before = JSON.stringify({
        status: b.status,
        used: b.context_used,
        tps: b.tps,
        count: (b.messages || []).length,
      });
      const currentMessages = b.messages || [];
      const liveRoles = new Set(["progress", "thought", "tool"]);
      const currentPersisted = currentMessages.filter((m) => !liveRoles.has(m.role));
      const transient = currentMessages.filter((m) => liveRoles.has(m.role));
      Object.assign(b, profile);
      const serverMessages = Array.isArray(profile.messages) ? profile.messages : null;
      if (serverMessages && serverMessages.length >= currentPersisted.length) {
        b.messages = serverMessages.concat(transient);
      } else {
        b.messages = currentMessages;
      }
      const after = JSON.stringify({
        status: b.status,
        used: b.context_used,
        tps: b.tps,
        count: (b.messages || []).length,
      });
      if (before !== after) {
        if (/Thinking|Working|Speaking|Using /i.test(String(b.status || ""))) {
          if (!state.stopped[b.id]) setWorking(b.id, true);
        } else {
          setWorking(b.id, false);
        }
        renderRoster();
        syncChatStatusLine(b);
        renderMeta(b);
        renderConversation(b);
      }
    } catch {
      // SSE remains the primary path; polling is only a silent-stream fallback.
    }
  }, 1500);
}
function startEventWatchdog() {
  eventWatchdog = setInterval(() => {
    if (!eventSource) return;
    const ready = eventSource.readyState; // 0 CONNECTING, 1 OPEN, 2 CLOSED
    if (ready === 2) return; // onerror path handles closed streams
    if (Date.now() - eventLastSeen > EVENT_WATCHDOG_MS) {
      try {
        eventSource.close();
      } catch {
        /* already closing */
      }
      eventSource = null;
      if (!eventReconnectTimer) {
        eventReconnectTimer = setTimeout(connectEvents, EVENT_RECONNECT_AFTER_MS);
      }
    }
  }, 10000);
}

(async function init() {
  applyTheme(localStorage.getItem("hermes-desk-theme") || "light");
  restoreLayout();
  fitPhoneFrame();
  autosizeComposer();
  window.addEventListener("orientationchange", fitPhoneFrame);
  window.visualViewport?.addEventListener("resize", fitPhoneFrame);
  window.visualViewport?.addEventListener("scroll", fitPhoneFrame);
  await bootstrap();
  try {
    const cat = await api("/v1/models");
    state.catalog = cat.models || [];
  } catch {
    state.catalog = [];
  }
  await refreshBots();
  if (observerBotId) {
    const target = state.bots.find((b) => b.id === observerBotId);
    if (target) {
      await selectBot(observerBotId);
      const live = state.bots.find((b) => b.id === observerBotId) || target;
      if (botHasRobotSimulator(live)) {
        setSurface("preview");
        window.DeskUI?.showRobotOnAgentDesktop?.();
      } else {
        setSurface("desktop");
        window.DeskUI?.openWindow?.("files");
      }
      document.getElementById("os-boot") && (document.getElementById("os-boot").hidden = true);
      document.getElementById("os-login") && (document.getElementById("os-login").hidden = true);
      document.body.classList.add("live-desktop-entered");
      document.getElementById("ubuntuDesktopViewer")?.classList.add("desktop-fullscreen-overlay");
      // Give MiniOS one render turn before declaring the visual mirror ready.
      await new Promise((resolve) => setTimeout(resolve, 250));
      window.deskObserverReady = true;
      document.documentElement.dataset.observerReady = "1";
    }
  } else {
    await restoreSelectedBot();
  }
  bindTimeline();
  wireDesktopTrash();
  wireNotepad();
  wireHorizon();
  syncMoreMenu();
  bindChatTranscript();
  renderChatTimeline();
  connectEvents();
  startLiveBotPoller();
  loopBrowser();
  window.DeskUI?.renderHourlyNotes();
  $("editor-save")?.addEventListener("click", saveEditorFile);
  $("editor-preview")?.addEventListener("click", () => openPreview());
  $("code-editor")?.addEventListener("input", () => {
    const st = $("editor-status");
    if (st) { st.className = "minios-editor-status"; st.textContent = `Unsaved changes • ${state.editorPath || "file"}`; }
  });
  $("preview-robot")?.addEventListener("click", () => {
    loadRobotSimulator();
    window.DeskUI?.openWindow?.("preview");
  });
  $("preview-browser")?.addEventListener("click", async () => {
    const b = state.bots.find((x) => x.id === state.selected);
    if (!b || !state.previewPath) return;
    await api(`/v1/bots/${b.id}/browser/open`, { method:"POST", body:JSON.stringify({ path: state.previewPath }) });
    setSurface("browser");
  });
  for (const key of ["build", "test"]) {
    $(`dev-${key}`)?.addEventListener("click", () => runDevCommand(state.dev?.commands?.[key] || "", key));
  }
  $("dev-run")?.addEventListener("click", toggleDevServer);
  $("dev-command-run")?.addEventListener("click", () => runDevCommand($("dev-command")?.value || ""));
  $("dev-command")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); runDevCommand(e.currentTarget.value || ""); } });
  $("browser-reload")?.addEventListener("click", () => $("url-form")?.requestSubmit());
  $("browser-back")?.addEventListener("click", async () => {
    if (!state.selected) return;
    try {
      await api(`/v1/bots/${state.selected}/desktop/action`, {
        method: "POST",
        body: JSON.stringify({ action: "browser_back" }),
      });
    } catch {
      /* ignore */
    }
    frameSeq = "";
    refreshBrowserFrame();
  });
  let listenIdleTimer = 0;
  $("message")?.addEventListener("input", () => {
    autosizeComposer();
    updateSendButton();
    syncSlashMenu();
    if (!state.selected) return;
    if (window.DeskUI?.isBusy?.(state.selected)) return;
    window.DeskUI?.setEmotion(state.selected, "listening", { persist: false, pop: false });
    clearTimeout(listenIdleTimer);
    listenIdleTimer = setTimeout(() => {
      if (window.DeskUI?.isBusy?.(state.selected)) return;
      window.DeskUI?.setEmotion(state.selected, "idle", { persist: false, pop: false });
    }, 1400);
  });
})();


