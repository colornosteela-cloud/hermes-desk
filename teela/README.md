# Teela local GPU runtime (teela-brain)

Scripts that launch vLLM on this host's GPUs (NVIDIA CUDA, AMD ROCm, or Intel Arc XPU). After a reimage, copy this folder to `~/teela` (or set `HERMES_DESK_TEELA` to this path).

```bash
cp -a teela ~/teela
# or: export HERMES_DESK_TEELA=$HOME/hermes-desk/teela
~/teela/start.sh qwen     # auto: CUDA / ROCm / Intel; 8GB → VL-8B, 16GB+ → 27B GPTQ
~/teela/status.sh
~/teela/stop.sh
```

`start.sh` sets `TEELA_BACKEND=auto`: NVIDIA (`nvidia-smi`) → CUDA, AMD (`/dev/kfd`) → ROCm, else Intel XPU. Override with `TEELA_BACKEND=cuda|rocm|xpu`.

Same MiniOS names (`qwen38`, `qwen3-vl-8b`) on every vendor. The **weights** that fit depend on VRAM, not the logo on the card.

| VRAM per GPU | `start.sh qwen` serves | Image |
| --- | --- | --- |
| **8 GB class** (RTX 5060, small Radeon) | **Qwen3-VL-8B** as `qwen38` + `qwen3-vl-8b` | CUDA or ROCm vLLM |
| **16 GB class** (5060 Ti, 16 GB AMD) | Qwen 3.8 27B GPTQ-Int4, TP=2 | CUDA or ROCm vLLM |
| **24 GB class** (Arc B60, 7900 XTX) | 27B GPTQ (Intel llm-scaler on Arc; CUDA/ROCm vLLM elsewhere). Muse optional | vendor image |

Weights are **not** in git. Put them under `~/models/` (or `MODELS_DIR`):

- `~/models/Qwen3-VL-8B-Instruct-FP8` or `…-Instruct` — **always copy this** (8 GB NVIDIA/AMD and hybrid vision)
- `~/models/Qwen3.8-27B-GPTQ-Int4` — only if each GPU is ~16 GB+
- `~/models/Muse-Glimmer-30B` — ~24 GB+/GPU only

## Mesh reference

The MiniOS twin uses the compact mesh `ui/las-catrina.bin.gz` (committed).

`Las_Catrina.obj` is ~240 MB and exceeds GitHub's 100 MB file limit, so it is **not** in this repo. Keep a local/offline copy if you still have the CAD OBJ.

Reference that **is** in git:

- `teela/teela_v2.html` — the visual/CAD HTML reference for the body
- `ui/las-catrina.bin.gz` — runtime articulated mesh the simulator loads
- `ui/build_catrina_mesh.py` — rebuilds the `.bin.gz` from an OBJ if you restore `Las_Catrina.obj` to `~/teela/` or `teela/Las_Catrina.obj`

Do not commit `.inner.sh` (generated), `*.bak`, Docker volumes, or model weights.
