#!/usr/bin/env python3
"""Voice upstream resolver and deskd TTS/STT/health handlers."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deskd"))

import deskd as d  # noqa: E402
import voice_upstream as vu  # noqa: E402


def _wav_pcm16(seconds: float = 0.3, rate: int = 16000, freq: float = 440.0) -> bytes:
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        sample = int(8000 * __import__("math").sin(2 * 3.14159 * freq * i / rate))
        frames += struct.pack("<h", max(-32767, min(32767, sample)))
    data = bytes(frames)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    hdr += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(data)) + data
    return hdr


class TtsEmotionParameterTests(unittest.TestCase):
    def test_tag_selects_cloned_voice_curve(self) -> None:
        name, p = d.tts_emotion_params("[happy] Hello, I am Teela.")
        self.assertEqual(name, "happy")
        self.assertGreaterEqual(p["exaggeration"], 1.4)
        self.assertLess(p["cfg_weight"], 0.2)
        name, p = d.tts_emotion_params("[crying] I miss that.")
        self.assertEqual(name, "sad")
        self.assertLessEqual(p["exaggeration"], 0.05)
        name, p = d.tts_emotion_params("[angry] Stop that.")
        self.assertEqual(name, "angry")
        self.assertGreaterEqual(p["exaggeration"], 1.5)
        name, p = d.tts_emotion_params("Just the facts.")
        self.assertEqual(name, "neutral")
        self.assertGreater(p["exaggeration"], 0.2)
        self.assertLess(p["exaggeration"], 0.7)
        name, p = d.tts_emotion_params("[excited] Yes!")
        self.assertEqual(name, "excited")
        self.assertGreaterEqual(p["exaggeration"], 1.8)
        self.assertEqual(d.apply_tts_emotion_tag("I'm glad you're here.", "happy"), "[happy] I'm glad you're here.")
        self.assertEqual(d.apply_tts_emotion_tag("[angry] Stop.", "angry"), "[angry] Stop.")
        self.assertTrue(d.apply_tts_emotion_tag("Waiting.", "sad").startswith("[crying]"))

    def test_explicit_emotion_overrides_tags(self) -> None:
        name, p = d.tts_emotion_params("[happy] Hey.", requested="tired")
        self.assertEqual(name, "tired")
        self.assertLessEqual(p["exaggeration"], 0.05)

    def test_user_asked_voice_even_without_tag(self) -> None:
        name, p = d.tts_emotion_params(
            "I'm just so happy to be here with you!",
            user_text="Can you tell me something in a happy voice?",
        )
        self.assertEqual(name, "happy")
        self.assertGreaterEqual(p["exaggeration"], 1.4)
        name, p = d.tts_emotion_params(
            "Okay, so I finally found the screwdriver.",
            user_text="tell me a short story about anything in a frustrated voice.",
        )
        self.assertEqual(name, "frustrated")
        self.assertGreaterEqual(p["exaggeration"], 1.2)


class VoiceUpstreamResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = {k: os.environ.get(k) for k in (
            "TEELA_TTS_URL", "TEELA_STT_URL", "HERMES_DESK_TTS", "HERMES_DESK_STT", "TEELA_VOICE_LOCAL",
        )}

    def tearDown(self) -> None:
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_aliases_win(self) -> None:
        os.environ["TEELA_TTS_URL"] = "http://10.0.0.118:8090/"
        os.environ["HERMES_DESK_TTS"] = "http://127.0.0.1:8090"
        os.environ["TEELA_STT_URL"] = "http://10.0.0.118:8091"
        self.assertEqual(vu.tts_url(), "http://10.0.0.118:8090")
        self.assertEqual(vu.stt_url(), "http://10.0.0.118:8091")
        self.assertEqual(vu.host_label(vu.tts_url()), "teela-body")
        self.assertEqual(vu.host_label(vu.stt_url()), "teela-body")

    def test_grok_desk_alias_when_teela_unset(self) -> None:
        os.environ.pop("TEELA_TTS_URL", None)
        os.environ.pop("TEELA_STT_URL", None)
        os.environ["HERMES_DESK_TTS"] = "http://example.invalid:8090"
        os.environ["HERMES_DESK_STT"] = "http://example.invalid:8091"
        self.assertEqual(vu.tts_url(), "http://example.invalid:8090")
        self.assertEqual(vu.stt_url(), "http://example.invalid:8091")

    def test_local_override_falls_back_to_loopback(self) -> None:
        os.environ.pop("TEELA_TTS_URL", None)
        os.environ.pop("TEELA_STT_URL", None)
        os.environ.pop("HERMES_DESK_TTS", None)
        os.environ.pop("HERMES_DESK_STT", None)
        os.environ["TEELA_VOICE_LOCAL"] = "1"
        self.assertEqual(vu.tts_url(), vu.LOCAL_TTS)
        self.assertEqual(vu.stt_url(), vu.LOCAL_STT)
        self.assertEqual(vu.host_label(vu.tts_url()), "localhost")

    def test_teela_brain_defaults_to_body_lan(self) -> None:
        os.environ.pop("TEELA_TTS_URL", None)
        os.environ.pop("TEELA_STT_URL", None)
        os.environ.pop("HERMES_DESK_TTS", None)
        os.environ.pop("HERMES_DESK_STT", None)
        os.environ.pop("TEELA_VOICE_LOCAL", None)
        if socket.gethostname().strip().lower() != "teela-brain":
            self.skipTest("not on teela-brain")
        self.assertEqual(vu.tts_url(), "http://10.0.0.118:8090")
        self.assertEqual(vu.stt_url(), "http://10.0.0.118:8091")
        self.assertEqual(vu.host_label(vu.tts_url()), "teela-body")

    def test_ip_not_scattered(self) -> None:
        hits: list[str] = []
        for path in (ROOT / "deskd").rglob("*.py"):
            if path.name == "voice_upstream.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "10.0.0.118" in text:
                hits.append(str(path.relative_to(ROOT)))
        for path in (ROOT / "ui").glob("*.js"):
            if "10.0.0.118" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])
        src = (ROOT / "deskd" / "voice_upstream.py").read_text(encoding="utf-8")
        self.assertIn("10.0.0.118:8090", src)
        self.assertIn("10.0.0.118:8091", src)

    def test_fetch_timeout_does_not_hang(self) -> None:
        class Slow(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                time.sleep(8)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"late")

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        from typing import Any

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/tts"
        t0 = time.time()
        code, raw = vu.fetch("POST", url, data=b"{}", content_type="application/json", timeout=0.4)
        elapsed = time.time() - t0
        httpd.shutdown()
        httpd.server_close()
        self.assertLess(elapsed, 2.0)
        self.assertEqual(code, 502)
        self.assertIn(b"unreachable", raw)


class VoiceHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self._orig_desks = d.HERMES_DESKS
        d.HERMES_DESKS = home / "desks"
        d.HERMES_DESKS.mkdir()
        self._orig_token = d.TOKEN_PATH
        d.TOKEN_PATH = home / "run" / "token"
        d.TOKEN_PATH.parent.mkdir()
        d.TOKEN_PATH.write_text("voice-token", encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self._env = {k: os.environ.get(k) for k in ("TEELA_TTS_URL", "TEELA_STT_URL")}

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        d.HERMES_DESKS = self._orig_desks
        d.TOKEN_PATH = self._orig_token
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body: bytes | None = None, headers=None, timeout: float = 8):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        hdrs = {"Authorization": "Bearer voice-token"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), data

    def test_health_includes_host_and_status(self) -> None:
        os.environ["TEELA_TTS_URL"] = "http://10.0.0.118:8090"
        os.environ["TEELA_STT_URL"] = "http://10.0.0.118:8091"
        st, _, raw = self._req("GET", "/v1/voice/health")
        self.assertEqual(st, 200)
        rec = json.loads(raw)
        self.assertEqual(rec["tts"]["host"], "teela-body")
        self.assertEqual(rec["stt"]["host"], "teela-body")
        self.assertIn(rec["tts"]["status"], ("ready", "unavailable"))
        self.assertIn(rec["stt"]["status"], ("ready", "unavailable"))
        st2, _, raw2 = self._req("GET", "/v1/voice/health")
        self.assertEqual(st2, 200)
        rec2 = json.loads(raw2)
        self.assertEqual(rec["tts"]["host"], rec2["tts"]["host"])
        self.assertEqual(rec["tts"]["status"], rec2["tts"]["status"])

    def test_tts_fails_fast_when_upstream_down(self) -> None:
        os.environ["TEELA_TTS_URL"] = "http://127.0.0.1:1"
        t0 = time.time()
        st, _, raw = self._req(
            "POST",
            "/v1/tts",
            body=json.dumps({"text": "[happy] Hello, I am Teela."}).encode(),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        elapsed = time.time() - t0
        self.assertLess(elapsed, 4.0)
        self.assertIn(st, (502, 500))
        rec = json.loads(raw)
        self.assertTrue(rec.get("error"))
        payload = d.assemble_teela_executive_payload(
            d.Bot("b_voice00000001", "Teela", "", "", "qwen38-27b-q5", "x", kind="teela-brain"),
            "hi",
        )
        self.assertTrue(payload.get("messages"))

    def test_deskd_tts_stt_roundtrip_uses_teela_body(self) -> None:
        os.environ["TEELA_TTS_URL"] = "http://10.0.0.118:8090"
        os.environ["TEELA_STT_URL"] = "http://10.0.0.118:8091"
        self.assertEqual(vu.tts_url(), "http://10.0.0.118:8090")
        self.assertEqual(vu.stt_url(), "http://10.0.0.118:8091")
        st, ctype, wav = self._req(
            "POST",
            "/v1/tts",
            body=json.dumps({"text": "[happy] Hello, I am Teela."}).encode(),
            headers={"Content-Type": "application/json"},
            timeout=40,
        )
        self.assertEqual(st, 200, wav[:200])
        self.assertIn("audio", (ctype or "").lower())
        self.assertGreaterEqual(len(wav), 44)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        rate = struct.unpack_from("<I", wav, 24)[0]
        self.assertGreaterEqual(rate, 8000)
        st2, _, raw = self._req(
            "POST",
            "/v1/stt",
            body=wav,
            headers={"Content-Type": "audio/wav"},
            timeout=40,
        )
        self.assertEqual(st2, 200, raw[:300])
        rec = json.loads(raw)
        text = str(rec.get("text") or "").strip()
        self.assertTrue(text)
        st3, _, health = self._req("GET", "/v1/voice/health")
        self.assertEqual(st3, 200)
        h = json.loads(health)
        self.assertEqual(h["tts"]["host"], "teela-body")
        self.assertEqual(h["tts"]["status"], "ready")
        self.assertEqual(h["stt"]["host"], "teela-body")
        self.assertEqual(h["stt"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
