# Hermes Desk MiniOS 0.10.1-rc5

This corrective release restores the **0.9.1-rc3 frontend behavior** and applies the requested UI changes as cosmetic/layout changes instead of replacing the frontend structure.

## Phone/LAN clients no longer freeze on the event stream

Chatting from a phone on the LAN could leave the page frozen (no new messages until a refresh). When a client's connection stalled briefly (Wi-Fi wobble, screen off), the server kept queuing events for it; past 500 queued events the client was dropped **without closing the socket**, so the browser's `EventSource` saw keepalives but never another event and never reconnected.

- Dropping a slow SSE subscriber now closes its socket, so the browser reconnects with a fresh stream.
- SSE connections have a 20s write timeout: a fully stalled pipe self-heals instead of hanging forever.
- Verified live: a stalled client (tiny receive buffer, not reading) was disconnected by the server mid-burst while a healthy client kept receiving all events.

## Hermes Agent bots keep their conversation across restarts

A Hermes Agent bot used to start a brand-new Hermes Agent session every time its ACP agent process was (re)started — after every crash, deskd restart, model change, or chat operation — so the bot re-read the whole workspace context for every question. Hermes Agent already persists every session under `~/.hermes/bots/<id>/hermes-home/sessions/`; deskd now uses that:

- The live ACP session ID is recorded in `hermes-home/last_acp_session.json` (keyed to the active chat) and bound to the chat row as `agentSession`.
- When the agent process restarts (crash respawn, deskd restart, `ensure()`), deskd sends ACP `session/load` for the recorded session and only falls back to `session/new` when no session is recorded or the load fails. A null `session/load` result counts as success.
- Opening an older chat resumes that chat's bound session when one exists.
- Intentional fresh starts (new chat, clear chat, delete last chat, identity or model change, rewind fallback) still create new sessions.
- First run after the upgrade (no record file yet) bootstraps from the newest on-disk session that has content.

Verified end to end: a codeword given to the System bot was still answered correctly after the ACP child process was killed and respawned mid-conversation.

## Chrome DevTools MCP on Hermes Agent (Teela ACP fallback only)

Host MCP servers from `~/.hermes/config.toml` (Chrome DevTools) are now inherited by MiniOS bots:

- **Hermes Agent** bots get them in the child `HERMES_DESK_HOME` config and on every ACP `session/new`, including bots created later.
- **Teela Brain** gets them on the ACP fallback session only. The MiniOS llama.cpp body/desktop loop is unchanged and still uses `bot_browser` / desktop tools.

## Robot Simulator is Teela-only

Hermes Agent bots do not load the Robot Simulator and cannot drive the body (`403` on `robot` / virtual-body actions). Their MiniOS dock shows App Preview instead. Teela Brain still gets the simulator, and there is still only one Teela Brain per host.

## Chat UI: todos, progress, effort, and paste

- Todo lists and a live progress line render in chat; Hermes Agent shows a reasoning-effort chip.
- Local tok/s uses per-token stream timing instead of ACP burst snapshots.
- User bubbles keep pasted newlines (`white-space: pre-wrap`). Restarted assistant snapshots no longer duplicate the essay.

## Fixes: long ACP turns and login after sleep

- **ACP `session/prompt` timeout** is idle silence, not a 10-minute wall clock. Tokens, tools, thoughts, and local prefill ticks keep a coding turn alive. A dead local engine still fails in ~12s; a hung turn with no events still times out after 10 minutes of silence.
- **Host `hermes login` is shared**, not copied. Child ACP/TUI processes symlink `~/.hermes/auth.json` (and its lock) and set `GROK_AUTH_PATH`. Byte-copying forked the OIDC refresh token: the first bot that refreshed revoked every other copy, which cleared credentials and forced `/login` after sleep.

## Autostart on reboot (teela-brain)

Hermes Desk is a **user systemd unit** (`contrib/hermes-desk.service` → `~/.config/systemd/user/hermes-desk.service`). On teela-brain it is **enabled** with `Restart=always`, starts after `teela-qwen38-27b.service` (Qwen 3.8 27B on `:8081`), and comes up at boot when lingering is on for that user (`loginctl enable-linger`).

```bash
systemctl --user enable --now hermes-desk.service
loginctl enable-linger "$USER"
systemctl --user status hermes-desk.service
```

`./start.sh` is still valid for a one-shot foreground/detach run; if the unit is already active it will report the existing pid and exit. Manual `python3 deskd/deskd.py` is not required after a reboot.

## Conversational correction loop

User remarks about a recent attempt (“that wasn’t right”, “much better”, “keep the upper arm still”) are structured feedback, not a new goal. Teela binds them to the last `AttemptRecord`, diagnoses execution vs skill vs planning vs recurring system weakness, retries a corrected plan when she can, and only then updates skill versions. Repeated systemic failures create an RSI *observation*; they do not rewrite deskd. Positive validation marks the corrected skill preferred.

## Frontend compatibility correction

- Started from the 0.9.1-rc3 frontend rather than the RC4 rewritten layout.
- Existing workspace header controls, desktop shortcut controls, Hourly Notes dialog, Take Over, fullscreen, terminals, browser, editor, preview, build/test, and existing DOM IDs remain present so the previous frontend wiring is preserved.
- On desktop sizes the redundant workspace name/header, duplicate shortcut strip, and separate Hourly Notes section are hidden cosmetically rather than deleted.
- The bottom separator is aligned across Bots, Chat, and Live Desktop.
- The chat composer remains constrained to the center Chat column.
- A single Routines button sits below Live Desktop. The original routines manager is presented as a popup window.
- Routine schedules support minutes, hours, days, weeks, monthly, and yearly intervals.

## Semantic MiniOS Desktop Driver retained

The semantic driver from RC4 is retained as an additive backend/agent feature without replacing the RC3 UI. Hermes can use `desktop_state`, stable object/app/window IDs, semantic window/app/file/browser/build actions, and `desktop_click_object`; raw cursor actions remain available for unfamiliar interfaces.

The bot profile tells Hermes to use `desktop_state` first and not to reverse-engineer Hermes Desk source code just to operate its own MiniOS.

## Validation

- 76/76 Python tests pass, including 5 RC5 regression tests specifically checking RC3 frontend compatibility plus the semantic driver.
- Python compilation passes.
- `node --check ui/app.js` passes.
- `node --check ui/hermesbot-ui.js` passes.
- ZIP integrity is checked during packaging.

---

# Hermes Desk MiniOS 0.9.1-rc3

This release candidate improves the bot-controlled MiniOS pointer and keeps the persistent visual-awareness work from 0.9.0-rc2.

## New: standard desktop cursor + reliable pointing/clicking

- Replaced the stylized bot pointer with a conventional white OS-style arrow cursor with a dark outline and top-left hotspot.
- Cursor position is now stored per bot, so every bot keeps its own pointer location when you switch workspaces.
- `desktop_move_cursor` visibly points anywhere on the MiniOS using normalized 0-1000 desktop coordinates.
- `desktop_click` performs a real left-click at the pointed location.
- `desktop_double_click` was added for files/icons and browser content.
- Browser clicks now go directly through Hermes Desk's Chromium input queue (`mouseMoved` -> `mousePressed` -> `mouseReleased`) instead of relying on synthetic DOM pointer capture.
- Click feedback is a short ring around the cursor hotspot; the arrow itself stays visually stable.

## Persistent MiniOS vision retained

Each bot owns a headless MiniOS mirror and can use `desktop_observe` / `desktop_watch` to receive the current rendered JPEG plus semantic state. The visual state now also reports the bot's normalized cursor position.

## Intended interaction loop

`observe -> point/move cursor -> click -> watch screen change -> verify -> continue`

This works alongside Hermes Agent's native file/bash tools: native tools remain the efficient path for coding, while the cursor is used when visual interaction or verification matters.

## Validation

- 71/71 Python tests pass.
- Python compilation passes for daemon, MCP helpers, and tests.
- `node --check ui/app.js` passes.
- `node --check ui/hermesbot-ui.js` passes.

---

## Previous 0.9.0-rc2 notes

This release candidate adds **persistent MiniOS visual awareness** while keeping Hermes Agent as the agent runtime through ACP.

## New: persistent visual MiniOS mirror

Each bot now owns a second, headless Chromium surface dedicated to rendering its Hermes Desk MiniOS continuously. It loads Hermes Desk in observer mode (`?observe=<bot_id>`), follows that bot's active MiniOS surface, and maintains an actual JPEG screencast of the rendered desktop.

New Hermes Agent MiniOS tools:

- `desktop_observe` — returns current MiniOS semantic state **and the actual JPEG screen frame**.
- `desktop_watch` — waits for a newer MiniOS frame after an action and returns the changed frame.

The agent profile now instructs Hermes to visually verify rendered UI work before claiming it is correct. This makes the intended loop:

`build/edit -> open in MiniOS -> observe/watch -> visually verify -> report in chat`

The normal Hermes Desk UI shows a **Visual awareness** status in the live desktop header when the bot mirror is ready.

## Existing MiniOS developer features retained

- Real per-bot Linux workspace shared with Hermes Agent.
- Files and Code Editor.
- Terminal PTY and Hermes Agent TUI.
- Build & Test Center.
- Managed long-running app process.
- Sandboxed App Preview.
- Per-bot Chromium browser.
- Virtual bot cursor and keyboard control.
- Chat-vs-workspace behavior handled by the Hermes Agent agent rather than a second AI router.

## Security retained

- Resolved-path workspace containment.
- Sandboxed agent-created HTML previews without `allow-same-origin`.
- Long-lived UI token omitted from normal resource/SSE URLs.
- Baseline CSP/security headers.
- Per-bot workspace/browser/observer profiles.

## Validation

- `python3 -m unittest discover -s tests -v` — **69 tests passed**.
- `python3 -m py_compile deskd/*.py` — passed.
- `node --check ui/app.js` — passed.
- `node --check ui/hermesbot-ui.js` — passed.

A live observer Chromium process successfully launched and produced JPEG frames in the packaging environment. That environment applies a Chromium enterprise policy that blocks loopback/private-address navigation, so the final observer page could not reach the local Hermes Desk HTTP server there. On a normal Ubuntu Hermes Agent host, the observer uses `http://127.0.0.1:<desk-port>/` and does not require Internet access.

The packaging environment also does not include an authenticated Hermes Agent CLI session, so run the Hello World acceptance test on the target Hermes Agent machine.
