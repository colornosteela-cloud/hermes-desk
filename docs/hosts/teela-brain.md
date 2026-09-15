# teela-brain

GPUs may be Intel Arc, NVIDIA, or AMD. Typical UI origin: `http://10.0.0.10:8742/`. `~/teela/start.sh qwen` detects the vendor and picks a model that fits VRAM (8 GB → Qwen3-VL-8B; 16 GB+ → 27B GPTQ). See `docs/REIMAGE.md` and `teela/README.md`.

## desk.json

See `examples/desk.teela-brain.json`. At install:

- `listen_host`: `10.0.0.10` (also written automatically when you save a LAN peer such as teela-body)
- `listen_port`: `8742`
- `node_name`: `teela-brain`
- `cluster_token`: **Generate once** on this host (Settings → LAN cluster, from `http://127.0.0.1:8742/`). Copy it. Do not put it in Confirm. Paste that same string into Cluster token on body and jetson.
- `peers`: real RFC1918 literals for body and jetson (replace `10.0.0.xx` / `10.0.0.yy`)

Full walkthrough: `docs/cluster.md`. Phones keep `http://10.0.0.10:8742/` for chat and cannot edit the mesh.

## Models

`~/.hermes/config.toml` default is `qwen38-27b-q5` with `base_url=http://127.0.0.1:8081/v1` (llama.cpp, rewritten through this deskd `/v1/llm` when the picker uses a catalog id). Brain bots list this file's models: **Qwen 3.8 27B Q5** (local GPU) plus Hermes 4.6 / 4.5 (cloud via `hermes login`). Occupied GPU rows are grayed; Hermes cloud stays selectable. Body's catalog is not shown here.

Daily driver: Qwen3.8-27B `UD-Q5_K_XL` tensor-split across both 5060 Ti 16 GB cards, native 262 144 context (q4 KV), mmproj vision, MTP draft on CUDA1. Thinking is off (`llama-server --reasoning off`).

Flash-Next on `:8080` is **optional and off by default**. It cannot share the two 16 GB cards with 27B (the 27B KV grows with long ACP turns and OOMs Flash-Next). Leave `teela-flash-next.service` disabled unless you have stopped 27B first (`~/bin/teela-llm flash`).

vLLM / Intel Arc hybrid (`:8000` / `:8001`) is not the current NVIDIA layout.

## Local LLM engines (current layout: 2× RTX 5060 Ti 16 GB)

llama.cpp loopback servers and Hermes Desk, user systemd (linger on so they start at boot without a login):

| Engine | Port | Service | Role |
| --- | --- | --- | --- |
| Qwen3.8-27B Q5 + MTP + vision | `8081` | `teela-qwen38-27b.service` (**enabled**) | Teela brain, picker `qwen38-27b-q5` |
| Hermes Desk MiniOS | `8742` / `8743` | `hermes-desk.service` (**enabled**, after 27B) | UI, Teela executive, cluster |
| Qwen3.8-Flash-Next | `8080` | `teela-flash-next.service` (**disabled**) | optional fast MoE; stop 27B first |

```bash
~/bin/teela-llm 27b      # daily brain on :8081
~/bin/teela-llm flash    # only after 27B is stopped
~/bin/teela-llm status
systemctl --user status hermes-desk.service
```

Hermes Desk autostart: `contrib/hermes-desk.service` installed as `~/.config/systemd/user/hermes-desk.service`, `systemctl --user enable --now hermes-desk.service`, and `loginctl enable-linger` so it (and the 27B unit) come up on reboot. `./start.sh` is a no-op while that unit holds `:8742`.

Short voice-chat replies go through the 27B lane while it holds the GPUs. deskd refuses a second daemon (pidfile + flock in `$XDG_RUNTIME_DIR/hermes-desk/deskd.pid`).

**MiniOS tool loop (not Hermes Agent):** Teela body/desktop turns call llama.cpp with a small OpenAI tool list (`teela_*`, `robot_*`, `desktop_*`, `memory_write` / `memory_retrieve`) and run those tools in deskd. Session facts from `workspace/.memory/` are injected into the system prompt (Q5 budget 16k memory tokens on a 262k window). No `search_tool` / `use_tool`. Hermes Agent ACP is only the fallback if the local engine is down. Body Bot stays on Hermes Agent.

**Correction loop:** after a body/learn attempt, Teela stores an `AttemptRecord`. Follow-ups like “that wasn’t right” or “yes, that’s it” are interpreted as feedback on that attempt (not a new unknown skill). Skill defects get a revised candidate version; only repeated system-level misses become RSI observations.

## Voice (Jade in / out)

UI has two round buttons in the composer: 🔊/🔇 (voice on/off, persisted in `desk.json` `voice`) and 🎤 (tap-to-talk).

- **Out**: browser → `POST /v1/tts` → deskd → Chatterbox-Turbo on **teela-body** (`TEELA_TTS_URL`, default LAN `:8090`). Jade is Teela-brain's user voice only — teammate DMs are not spoken. Spoken replies may include official Turbo tags (`[happy]`, `[chuckle]`, `[laugh]`, `[surprised]`, `[gasp]`, `[sigh]`, …). Chat hides the tags; TTS keeps them.
- **In**: browser MediaRecorder (webm/opus) → `POST /v1/stt` → deskd → faster-whisper `small.en` on **teela-body** (`TEELA_STT_URL`, default LAN `:8091`). Brain's own `teela-stt.service` stays disabled so Whisper does not steal GPU0 from 27B.
- **Model awareness**: every ACP turn is prefixed with `[Voice mode: ON/OFF]` so the bot writes for the ear when voice is on.
- **Teammate DMs**: Teela records inbound DMs in chat and does **not** start an ACP turn (ping only). Body Bot (hermes) still ACP-replies to DMs. Jade never reads DMs or “Sent to …” lines.
- **One speaker per reply**: voice on/off is global, but playback is per-page. The prompt POST carries a per-tab `page_id`; deskd tags assistant-chat and turn-completed events with it, and each page only speaks when the tag matches its own id. Two open devices don't both talk. Overlapping TTS fetches are generation-token cancelled.
- **UI files are served `no-store`** (and `app.js` / `hermesbot-ui.js` carry a `?v=` bust). Hard-refresh after a pull so a stale tab cannot speak DMs.
- `GET /v1/voice/health` → `{tts, stt, voice}`.
- **Phones/other PCs**: mic capture needs a secure context, so deskd also serves **HTTPS on `listen_port+1`** (8743) with a self-signed cert in `~/.hermes/certs/` (accept it once per device). TTS playback works over plain HTTP.

## Motion plane

MiniOS on this host is the motion authority (digital twin + intent). It does **not** run WBC or SAM.

- STATIC (wave, look, pose, joints) → MiniOS twin, then cluster POST `/v1/cluster/robot/execute` to **teela-jetson**
- DYNAMIC (walk, pick from floor, kneel) → MiniOS twin only until WBC is loaded, then POST `/v1/cluster/robot/wbc` to **teela-body**
- Safety (E-stop) → Jetson execute + body WBC halt
- 50 Hz tracking never goes through deskd HTTP

## Do not

- Share this host's GPU with body/jetson
- Put `cluster_token` or `auth.json` in git
- Proxy `/v1/llm` to peers
- Run GR00T / SONIC / SAM on Arc
