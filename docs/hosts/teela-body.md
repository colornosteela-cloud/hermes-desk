# teela-body

2× RTX 4060 Ti 16GB. Clone the same repo; `./start.sh`.

## Run as the user that has Hermes Agent — not root

```bash
# on teela-body, as the same user that ran hermes login (never sudo ./start.sh)
curl -fsSL https://x.ai/cli/install.sh | bash
hermes login
which hermes    # usually ~/.local/bin/hermes or ~/.hermes/bin/hermes
git clone https://github.com/colornosteela-cloud/Hermes-desk.git
cd Hermes-desk && git pull
./start.sh
```

`[Errno 2] '/root/.local/bin/hermes'` means deskd was started with sudo. Stop it, run `./start.sh` as the hermes user.

Saving a LAN peer (brain at `http://10.0.0.10:8742`) automatically sets `listen_host` to body’s RFC1918 IP and binds `0.0.0.0:8742`. If you still set Hermes Desk address by hand, use body’s **LAN IP** (for example `10.0.0.118`), not `127.0.0.1`. Otherwise brain cannot list or message body bots (connection refused). Check with `ss -ltn | grep 8742` — it should show `0.0.0.0:8742` or the LAN IP, not only `127.0.0.1:8742`.

## Cognition (v1)

Hermes 4.6 **cloud** is the default on this box's keys.

```bash
# on teela-body
hermes login          # writes ~/.hermes/auth.json on THIS host; deskd shares that file with bots (do not copy it)
```

`config.toml` default `hermes-4.6` (cloud, empty `base_url`). Cloud keys are not `cluster_token`.

**Offline backup:** `teela-llm-tunnel.service` forwards `127.0.0.1:8081` to teela-brain's Qwen 3.8 27B Q5 (brain llama stays loopback-only). Picker row `qwen38-27b-q5` is tagged `peer = "teela-brain"` so body will not try to load those weights or kill the tunnel. If xAI is down, set Body Bot's model to **Qwen 3.8 27B Q5**. Teela on brain still owns the GPUs — body shares that one slot.

## desk.json

See `examples/desk.teela-body.json`. At install replace `10.0.0.xx` with this box's RFC1918 IP.

- `node_name`: `teela-body`
- `listen_host`: that IP
- `cluster_token`: **paste** the token generated on teela-brain into **Cluster token** (first box). Leave Confirm blank. This is not the xAI key from `hermes login`.
- `peers`: name `teela-brain`, URL `http://10.0.0.10:8742` (and jetson later)

Open `http://127.0.0.1:8742/` **on teela-body**, Save, then Test. Full walkthrough: `docs/cluster.md`.

Optional later: CUDA vLLM on `:8000`. Existing `/v1/llm` alias/proxy on *this* deskd applies unchanged.

## Voice (Jade TTS / Whisper STT)

`./start.sh` does **not** start TTS or STT. Voice is separate user systemd on this box. teela-brain’s deskd then calls those ports over LAN (`TEELA_TTS_URL` / `TEELA_STT_URL`).

| Service | Port | GPU | In-repo launcher |
| --- | --- | --- | --- |
| Jade / Chatterbox-Turbo TTS | `8090` | GPU0 | **not in this repo** — keep the existing Chatterbox process; only change bind `127.0.0.1` → `0.0.0.0` |
| faster-whisper `small.en` STT | `8091` | GPU1 | `deskd/stt_server.py` via `examples/systemd/teela-stt.service` |

Do not change ports, models, or GPU assignment. Local `127.0.0.1:8090` / `:8091` must keep working after the bind change.

### STT (this repo)

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/teela-stt.service ~/.config/systemd/user/
# WorkingDirectory / ExecStart paths: adjust if the clone is not ~/hermes-desk-2
systemctl --user daemon-reload
systemctl --user enable --now teela-stt.service
```

`STT_HOST=0.0.0.0` is set in the unit. The Python default remains loopback so a accidental run on teela-brain stays local.

### TTS (existing Chatterbox on this machine)

Keep the current Chatterbox unit/script. Change only the listen address:

- If it reads `TTS_HOST`, set `TTS_HOST=0.0.0.0` (see `examples/systemd/teela-tts.service` as a template — replace `ExecStart=/bin/false` with the command that already works).
- Otherwise edit the bind from `127.0.0.1` to `0.0.0.0` and leave port `8090`.

Restart that unit. Do not replace the startup mechanism.

### Firewall (only teela-brain)

If UFW is active, allow TCP 8090/8091 from teela-brain only:

```bash
sudo ufw allow from 10.0.0.10 to any port 8090 proto tcp
sudo ufw allow from 10.0.0.10 to any port 8091 proto tcp
sudo ufw reload
sudo ufw status
```

Equivalent nftables/firewalld: source `10.0.0.10`, dports `8090,8091/tcp`.

### Verify on teela-body

```bash
ss -lntp | grep -E ':8090|:8091'
# expect 0.0.0.0:8090 and 0.0.0.0:8091 — not only 127.0.0.1
curl -sS http://127.0.0.1:8090/health
curl -sS http://127.0.0.1:8091/health
curl -sS -X POST http://127.0.0.1:8090/tts -H 'Content-Type: application/json' \
  -d '{"text":"[happy] Hello, I am Teela."}' -o /tmp/teela-hello.wav
```

Local Hermes Desk `{tts, stt, voice}` should stay ready. From teela-brain, `GET /v1/voice/health` should show `host: teela-body`.

## Motion / perception (not cognition)

This box is the **motor cortex + visual cortex**, not the MiniOS planner.

- GPU work: SAM 3.1 (segmentation/tracking), V-JEPA 2.1 (dense video features / world model), and GR00T/SONIC WBC (when loaded). Set `HERMES_DESK_WBC=1` only after the policy is actually serving.
- Stack: CUDA 12.8+ drivers, Python 3.12, PyTorch ≥ 2.10 (cu128 wheels), TensorRT 10.13 (SONIC desktop; other versions give wrong outputs). Clones: `facebookresearch/sam3`, `facebookresearch/vjepa2`, `NVlabs/GR00T-WholeBodyControl` + `NVIDIA/Isaac-GR00T`. Checkpoints: gated `facebook/sam3.1` (HF access + `hf auth login`), `nvidia/GEAR-SONIC`, `nvidia/GR00T-N1.5-3B`.
- VRAM layout on 2× 4060 Ti 16GB: GPU0 SAM 3.1 (≈10–12 GB image; video single-object on the other card), GPU1 V-JEPA 2.1 ViT-L 300M (ViT-g/2B don't fit a 16GB card) + SONIC decoder/GR00T inference.
- The 50 Hz SONIC loop stays on teela-jetson (TensorRT 10.7 / JetPack 6). Body hosts the 2.5 Hz GR00T PolicyServer side only.
- Cluster intake: `POST /v1/cluster/robot/wbc` (cluster token). Until WBC is loaded this returns `accepted: false`.
- Do not attach servos here. Joint tracking goes to teela-jetson.
- Do not run SAM/WBC on teela-brain.
