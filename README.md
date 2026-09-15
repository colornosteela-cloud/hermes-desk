# Hermes Desk MiniOS — Hermes Agent Frontend

Hermes Desk is a Linux-first graphical frontend/runtime shell for **xAI Hermes Agent**. Hermes Agent remains the agent brain and communicates with the desk through its ACP stdio interface (`hermes acp`). Hermes Desk adds chat, per-bot visual workspaces, browser/cursor control, code viewing/editing, terminals, build/test controls, app preview, and optional LAN bot federation.

## Architecture

Each bot gets its own real workspace at `~/hermes-desks/<id>/workspace` and its own Hermes state under `~/.hermes/bots/<id>/`. The MiniOS does **not** maintain a fake copy of the filesystem: Hermes Agent, the editor, file explorer, terminal, preview, and build/test center all operate on the same workspace.

```text
User ↔ Hermes Desk Chat ↔ ACP ↔ hermes acp (Hermes Agent)
                              │
                              ▼
                       Bot real workspace
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
          Files/Editor     Terminal/Tests    Browser/Preview
              └───────────────┼────────────────┘
                              ▼
                         MiniOS desktop
                         + bot cursor
```

Hermes Agent decides whether a request only needs a chat response or requires workspace/tool activity. Hermes Desk does not place a second AI router in front of the Hermes agent.

## MiniOS features

- **Files** — graphical view of the bot's real workspace.
- **Code Editor** — view and edit text/code files in the same workspace Hermes Agent uses.
- **Terminal** — live PTY shell rooted in the bot workspace.
- **Build & Test Center** — detects Node, Python, Rust, CMake, and Make projects; build/test jobs report exit status and output.
- **Run App** — starts/stops a long-running per-bot development process without blocking chat.
- **App Preview** — sandboxed preview for workspace HTML.
- **Chromium Browser** — isolated browser profile for each bot with existing browser MCP controls.
- **Bot Cursor** — a standard OS-style arrow pointer, independent of the user's physical mouse, with per-bot position plus point/click/double-click control.
- **Persistent Visual Mirror** — a dedicated headless Chromium mirror continuously renders each bot's MiniOS and exposes actual JPEG frames to Hermes Agent through `desktop_observe` and `desktop_watch`.
- **Hermes Agent TUI** — optional live TUI surface alongside ACP chat.

The bot can use Hermes Agent's native file/bash tools for efficient work and MiniOS tools when visual interaction or demonstration is useful.

### Semantic Desktop Driver

For known MiniOS controls, Hermes starts with `desktop_state` and uses stable app/window/object IDs such as `app_browser`, `win_browser`, and `obj_agent_close`. Semantic actions visibly drive the same MiniOS cursor/window manager, while raw coordinate cursor control remains the fallback for unfamiliar webpages and newly built interfaces. The agent profile explicitly tells Hermes not to inspect Hermes Desk source code merely to learn how to operate its own desktop.

## Requirements

- Linux
- Python 3.11+
- Current Hermes Agent CLI with `hermes acp`
- A Chromium/Chrome binary for the browser surface

Hermes Desk probes the installed Hermes CLI and enables optional flags only when they are advertised by that build. Runtime capability information is available at `/v1/runtime/hermes`.

Browser discovery checks `HERMES_DESK_CHROME`, common system Chrome/Chromium commands, and installed Playwright Chromium caches. You can force a browser binary with:

```bash
export HERMES_DESK_CHROME=/path/to/chromium
```

## Reimage / new GPUs

After wiping the box, follow [docs/REIMAGE.md](docs/REIMAGE.md). This tree includes the Teela vLLM launcher (`teela/`) and the Catrina runtime mesh. Model weights and `hermes login` are not in git.

## Start

Run Hermes Desk as the same normal user that installed and authenticated Hermes Agent:

```bash
./start.sh
```

On teela-brain it is also a user systemd unit that **starts on reboot** (see [RELEASE_NOTES.md](RELEASE_NOTES.md) and [docs/hosts/teela-brain.md](docs/hosts/teela-brain.md)):

```bash
cp contrib/hermes-desk.service ~/.config/systemd/user/hermes-desk.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-desk.service
loginctl enable-linger "$USER"
```

Then open:

```text
http://127.0.0.1:8742/
```

To expose the desk on your LAN, change **Hermes Desk address** in **User & Hermes Agent Settings** to the host's LAN address, or save a LAN cluster peer — deskd then binds `0.0.0.0:8742` and writes this host's RFC1918 IP into `listen_host` so the other desk can list your bots. Do not expose Hermes Desk directly to the public Internet. Set `HERMES_DESK_LOOPBACK=1` to force loopback-only for tests.

`start.sh` honors these optional environment variables:

```bash
HERMES_BIN=/path/to/hermes
HERMES_DESK_PORT=8742
HERMES_DESKS=$HOME/hermes-desks
HERMES_DESK_CHROME=/path/to/chromium
HERMES_DESK_SANDBOX=off
HERMES_DESK_LLM=http://127.0.0.1:8081
HERMES_DESK_MODEL=Qwen3.8-27B
HERMES_DESK_LOOPBACK=1
```

The local-model upstream variables were renamed from `HERMES_DESK_VLLM*` (the upstream is not always vLLM — llama.cpp works too). The old names are still accepted as fallbacks.

## Chat vs workspace behavior

The per-bot agent profile tells Hermes Agent to answer normally in chat when no environment evidence is needed, and to use its real workspace/tools when the user asks it to create, modify, inspect, run, build, test, browse, debug, or verify something. It must verify work before claiming a build/test/UI succeeded.

MiniOS presentation tools supplied to the ACP session include `desktop_state`, `desktop_observe`, `desktop_watch`, `desktop_open_app`, `desktop_move_cursor`, `desktop_click`, `desktop_double_click`, and `desktop_type_text`. `desktop_observe`/`desktop_watch` return the continuously rendered MiniOS JPEG frame so the bot can visually verify what is actually on its screen. These supplement rather than replace Hermes Agent's own coding tools.

## Hello World visual acceptance test

After Hermes Agent is installed/authenticated and Hermes Desk is running, send a bot:

```text
Build a simple Hello World webpage in your workspace. Create the actual files, open the finished page in your MiniOS, and visually verify what is rendered before telling me it is done.
```

Expected result: the file exists in the bot workspace, it can be opened in Code/Files, the rendered **Hello World** page remains visible in the MiniOS Browser/Preview, and the agent performs `desktop_observe` or `desktop_watch` before reporting success.

## Security boundaries

Workspace paths are resolved with real path containment checks. Agent-created HTML previews run in sandboxed iframes without `allow-same-origin`. The UI authentication token is sent in an HttpOnly SameSite cookie for browser resources/SSE instead of being embedded in normal resource URLs, and the daemon adds baseline browser security headers.

Each bot's MiniOS is intended to stay scoped to that bot's workspace and browser profile. Hermes Desk's control plane remains separate from content created by an agent.

## Tests

Run the complete suite with explicit discovery:

```bash
python3 -m unittest discover -s tests -v
```

Also run syntax checks when modifying the frontend/backend:

```bash
python3 -m py_compile deskd/*.py
node --check ui/app.js
node --check ui/hermesbot-ui.js
```

## Cluster

The existing multi-host cluster support remains available. Clone the same Hermes Desk tree to each LAN host; each daemon uses that machine's own Hermes Agent installation, credentials, GPU/runtime, bot states, and workspaces. See `docs/cluster.md` and `docs/hosts/`.

Do not commit `~/.hermes/`, `desk.json`, `auth.json`, runtime tokens, `.env` files, browser profiles, or bot workspaces containing secrets.

## Bot cursor

Each bot has its own normal OS-style arrow cursor. Hermes can point with `desktop_move_cursor`, click with `desktop_click`, and double-click with `desktop_double_click`. Browser clicks are forwarded to the bot's Chromium surface.
