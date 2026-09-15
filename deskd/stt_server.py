#!/usr/bin/env python3
"""Local STT for Hermes Desk: POST audio → text.

Default bind is loopback (127.0.0.1:8091). On teela-body set STT_HOST=0.0.0.0
(see examples/systemd/teela-stt.service) so teela-brain can reach :8091 on the LAN
while localhost clients still work.

Backends (STT_BACKEND):
  whisper — faster-whisper (default: small.en)
  nemo    — NVIDIA NeMo ASR (default: parakeet-unified-en-0.6b)

Browser MediaRecorder webm/opus (or wav/mp4/ogg) is decoded with PyAV into
16 kHz mono float32. One in-flight request (serialized). deskd proxies /v1/stt.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

BACKEND = os.environ.get("STT_BACKEND", "whisper").strip().lower()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")
NEMO_MODEL = os.environ.get(
    "STT_NEMO_MODEL",
    os.environ.get("NEMO_MODEL", "nvidia/parakeet-unified-en-0.6b"),
)
DEVICE = os.environ.get("WHISPER_DEVICE", os.environ.get("STT_DEVICE", "cuda"))
COMPUTE = os.environ.get("WHISPER_COMPUTE", "float16")
HOST = os.environ.get("STT_HOST", "127.0.0.1")
PORT = int(os.environ.get("STT_PORT", "8091"))
MAX_SECONDS = float(os.environ.get("STT_MAX_SECONDS", "180"))

_model = None
_backend = BACKEND
_model_id = WHISPER_MODEL if BACKEND == "whisper" else NEMO_MODEL
_model_lock = threading.Lock()
_work_lock = threading.Lock()


def decode_audio(data: bytes) -> np.ndarray:
    """Any container PyAV knows (webm/opus, wav, mp4, ogg) → float32 mono 16 kHz."""
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    frames = []
    with av.open(io.BytesIO(data)) as container:
        if container.streams.audio is None or not container.streams.audio:
            raise ValueError("no audio stream")
        for frame in container.decode(audio=0):
            for res in resampler.resample(frame):
                arr = res.to_ndarray()
                frames.append(arr[0] if arr.ndim == 2 else arr)
    if not frames:
        raise ValueError("no audio decoded")
    audio = np.concatenate(frames)
    if audio.ndim > 1:
        audio = audio.reshape(-1)
    return audio.astype(np.float32)


def _write_wav16(path: str, audio: np.ndarray) -> None:
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def get_model():
    global _model, _backend, _model_id
    with _model_lock:
        if _model is not None:
            return _model
        if BACKEND == "nemo":
            import nemo.collections.asr as nemo_asr
            import torch

            print(f"[stt] loading NeMo {NEMO_MODEL} on {DEVICE} …", flush=True)
            model = nemo_asr.models.ASRModel.from_pretrained(model_name=NEMO_MODEL)
            if DEVICE.startswith("cuda") and torch.cuda.is_available():
                model = model.to(torch.device("cuda"))
            model.eval()
            try:
                from omegaconf import OmegaConf, open_dict

                if getattr(model.cfg, "validation_ds", None) is None:
                    with open_dict(model.cfg):
                        model.cfg.validation_ds = OmegaConf.create({"use_start_end_token": False})
            except Exception as e:
                print(f"[stt] validation_ds patch skipped: {e}", flush=True)
            _model = model
            _backend = "nemo"
            _model_id = NEMO_MODEL
            try:
                with torch.inference_mode():
                    _ = model.transcribe(np.zeros(8000, dtype=np.float32), batch_size=1, num_workers=0, verbose=False)
                print("[stt] nemo warmup ok", flush=True)
            except Exception as e:
                print(f"[stt] nemo warmup skipped: {e}", flush=True)
            return _model

        from faster_whisper import WhisperModel

        print(f"[stt] loading whisper {WHISPER_MODEL} on {DEVICE}/{COMPUTE} …", flush=True)
        _model = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE)
        _backend = "whisper"
        _model_id = WHISPER_MODEL
        return _model


def _nemo_text(result) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if hasattr(result, "text"):
        return str(result.text or "").strip()
    if isinstance(result, dict) and "text" in result:
        return str(result.get("text") or "").strip()
    if isinstance(result, (list, tuple)) and result:
        return _nemo_text(result[0])
    return str(result).strip()


def trim_audio(audio: np.ndarray, sr: int = 16000, thresh: float = 0.008, pad_ms: int = 120) -> np.ndarray:
    if audio.size == 0:
        return audio
    hop = max(1, sr // 100)
    env = np.maximum.reduceat(np.abs(audio), np.arange(0, len(audio), hop))
    on = np.flatnonzero(env > thresh)
    if on.size == 0:
        return audio[: sr // 4]
    pad = sr * pad_ms // 1000
    start = max(0, int(on[0]) * hop - pad)
    end = min(len(audio), (int(on[-1]) + 1) * hop + pad)
    return audio[start:end]


def transcribe_audio(audio: np.ndarray) -> tuple[str, str]:
    """Return (text, language)."""
    model = get_model()
    if _backend == "nemo":
        import torch

        clipped = trim_audio(audio)
        with torch.inference_mode():
            out = model.transcribe(clipped, batch_size=1, num_workers=0, verbose=False)
        return _nemo_text(out), "en"

    segments, info = model.transcribe(
        audio,
        beam_size=1,
        language="en",
        vad_filter=False,
        condition_on_previous_text=False,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, getattr(info, "language", "en") or "en"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: ARG002
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            ready = _model is not None
            self._send(200, {
                "ready": ready,
                "backend": _backend,
                "model": _model_id,
                "device": DEVICE,
                "compute": COMPUTE if _backend == "whisper" else "fp16",
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/transcribe":
            self._send(404, {"error": "not found"})
            return
        if _model is None:
            self._send(503, {"error": "model not loaded"})
            return
        partial = (self.headers.get("X-STT-Partial") or "").strip() == "1"
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        if length > 32 * 1024 * 1024:
            self._send(413, {"error": "audio too large (max 32 MB)"})
            return
        data = self.rfile.read(length)
        t0 = time.time()
        if partial:
            got = _work_lock.acquire(blocking=False)
            if not got:
                self._send(409, {"error": "busy", "partial": True})
                return
        else:
            _work_lock.acquire()
        text = ""
        seconds = 0.0
        language = "en"
        try:
            try:
                audio = decode_audio(data)
            except Exception as e:
                self._send(400, {"error": f"decode failed: {e}"})
                return
            seconds = len(audio) / 16000.0
            if seconds > MAX_SECONDS:
                self._send(413, {"error": f"audio too long ({seconds:.0f}s > {MAX_SECONDS:.0f}s)"})
                return
            if partial and seconds < 0.4:
                self._send(200, {"text": "", "partial": True, "seconds": round(seconds, 2)})
                return
            try:
                text, language = transcribe_audio(audio)
            except Exception as e:
                self._send(500, {"error": f"transcribe failed: {e}"})
                return
        finally:
            _work_lock.release()
        print(f"[stt] {_backend} {length}B {seconds:.2f}s -> {len(text)} chars {text[:80]!r}", flush=True)
        self._send(200, {
            "text": text,
            "seconds": round(seconds, 2),
            "load_s": round(time.time() - t0, 2),
            "language": language,
            "partial": partial,
            "backend": _backend,
            "model": _model_id,
        })


def main() -> None:
    print(f"[stt] backend={BACKEND} model={_model_id} device={DEVICE}", flush=True)
    t0 = time.time()
    get_model()
    print(f"[stt] ready in {time.time() - t0:.1f}s on {HOST}:{PORT}", flush=True)
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
