# teela-jetson

Jetson Orin Nano Super (ARM). Clone the same repo; `./start.sh`.

## Install notes

- System Python 3.10+, aarch64 `hermes` binary
- No Intel XPU container, no x86 `pip install torch` wheels
- **Do not install x86 Playwright**

Chromium for the browser surface is optional. Chat + DM work if Chrome fails to start. Point at a native binary:

```bash
export HERMES_DESK_CHROME=/usr/bin/chromium-browser
# or:
export HERMES_DESK_CHROME=/usr/bin/chromium
```

Do not add code that detects `aarch64` and disables `BrowserSurface`. Docs only.

## Cognition (v1)

Same as body: Hermes 4.6 cloud via **this** host's `~/.hermes/auth.json` (`hermes login` on the Jetson). That is not `cluster_token`. Jetson bots only list this host's `config.toml` — brain's Qwen is not in the picker.

## desk.json

See `examples/desk.teela-jetson.json`. At install replace `10.0.0.yy` with this box's RFC1918 IP.

- `node_name`: `teela-jetson`
- `listen_host`: that IP (also written automatically when you save a LAN peer)
- `cluster_token`: **paste** the token generated on teela-brain into **Cluster token**. Leave Confirm blank. Not the xAI key.
- `peers`: `http://10.0.0.10:8742` and body

Open `http://127.0.0.1:8742/` on the Jetson, Save, then Test. Full walkthrough: `docs/cluster.md`.

## Motor loop

This node is the **spinal cord**: servo I/O, joint limits, watchdog, E-stop. It is not a neural WBC host.

- Cluster intake: `POST /v1/cluster/robot/execute` (STATIC poses/joints from MiniOS).
- Reject `walk` / other DYNAMIC cmds — those belong on teela-body WBC.
- Set `HERMES_DESK_MOTORS=1` only when the real motor daemon is attached. Until then the endpoint acks as `backend: stub`.
- If brain/body disappear, hold / safe pose locally. Never keep moving on stale WBC packets.
