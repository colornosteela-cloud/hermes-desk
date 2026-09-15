"""Live computer surfaces: Chromium screencast, workspace shell PTY, Hermes Agent TUI PTY."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pty
import shutil
import fcntl
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, quote_plus, urlparse

def resolve_chrome_bin() -> str:
    """Find a usable Chromium/Chrome binary without pinning a Playwright revision."""
    env = (os.environ.get("HERMES_DESK_CHROME") or "").strip()
    if env:
        return str(Path(env).expanduser())
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        hit = shutil.which(name)
        if hit:
            return hit
    cache = Path(os.path.expanduser("~/.cache/ms-playwright"))
    patterns = (
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    )
    for pattern in patterns:
        for p in sorted(cache.glob(pattern), reverse=True):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    # Keep a useful error path if nothing is installed yet.
    return str(cache / "chromium-*/chrome-linux64/chrome")


CHROME = resolve_chrome_bin()


def resolve_login_home() -> Path:
    """Home of the user who installed Hermes Agent, even if deskd was started with sudo."""
    sudo = (os.environ.get("SUDO_USER") or "").strip()
    if os.geteuid() == 0 and sudo and sudo != "root":
        try:
            import pwd

            return Path(pwd.getpwnam(sudo).pw_dir)
        except Exception:
            p = Path("/home") / sudo
            if p.is_dir():
                return p
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def resolve_agent_bin() -> str:
    env = (os.environ.get("HERMES_BIN") or "").strip()
    cands: list[Path] = []
    if env:
        cands.append(Path(env).expanduser())
    which = shutil.which("hermes")
    if which:
        cands.append(Path(which))
    home = resolve_login_home()
    process_home = Path(os.path.expanduser("~"))
    for h in (home, process_home):
        cands.append(h / ".local/bin/hermes")
        cands.append(h / ".hermes/bin/hermes")
    cands.extend([Path("/usr/local/bin/hermes"), Path("/usr/bin/hermes")])
    seen: set[str] = set()
    for p in cands:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        try:
            if p.is_file() and os.access(p, os.X_OK):
                return s
        except OSError:
            continue
    return env or str(home / ".local/bin/hermes")


HERMES_BIN = resolve_agent_bin()


def _ws_handshake(host: str, port: int, path: str) -> tuple[socket.socket, bytes]:
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=10)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("CDP websocket closed during handshake")
        buf += chunk
    _hdr, extra = buf.split(b"\r\n\r\n", 1)
    return sock, extra


def _ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    hdr = bytearray([0x80 | opcode])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr.extend(n.to_bytes(2, "big"))
    else:
        hdr.append(0x80 | 127)
        hdr.extend(n.to_bytes(8, "big"))
    sock.sendall(bytes(hdr) + mask + masked)


def _ws_recv(sock: socket.socket) -> bytes:
    """Read a complete (possibly fragmented) WebSocket text/binary message."""
    assembled = b""
    while True:
        hdr = _recv_exact(sock, 2)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        masked = bool(hdr[1] & 0x80)
        if ln == 126:
            ln = int.from_bytes(_recv_exact(sock, 2), "big")
        elif ln == 127:
            ln = int.from_bytes(_recv_exact(sock, 8), "big")
        mask = _recv_exact(sock, 4) if masked else b""
        data = _recv_exact(sock, ln)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:
            return b""
        if opcode == 0x9:
            _ws_send(sock, data, 0xA)
            continue
        if opcode == 0xA:
            continue
        assembled += data
        if fin:
            return assembled


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("socket closed")
        out += chunk
    return out


class BrowserSurface:
    def __init__(self, bot_id: str, profile: Path, port: int, view_w: int = 1280, view_h: int = 800) -> None:
        self.bot_id = bot_id
        self.profile = profile
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None
        self.sock: socket.socket | None = None
        self._id = 0
        self._wlock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.frame_jpeg: bytes = b""
        self.frame_seq = 0
        self.url = "about:blank"
        self.page_id = ""
        self.view_w = max(640, int(view_w or 1280))
        self.view_h = max(400, int(view_h or 800))
        self._last_click: tuple[float, float] | None = None
        self._last_frame_at = 0.0
        self._last_input_at = 0.0
        self._kick = threading.Event()
        self._shot_lock = threading.Lock()
        self._watch_started = False
        self._watch_gen = 0
        self.alive = False
        self._stop = threading.Event()
        self._reader_started = False
        self._reader_gen = 0
        self._rx = b""
        self._stale = False
        self._loaded = threading.Event()
        self._casting = False

    def healthy(self) -> bool:
        if not self.alive or not self.sock or self._stale:
            return False
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return True

    def _chrome_pids(self) -> list[int]:
        needle = f"--user-data-dir={self.profile}".encode()
        pids: list[int] = []
        me = os.getpid()
        try:
            for ent in Path("/proc").iterdir():
                if not ent.name.isdigit():
                    continue
                pid = int(ent.name)
                if pid == me:
                    continue
                try:
                    cmd = (ent / "cmdline").read_bytes()
                except OSError:
                    continue
                if needle in cmd and b"chrome" in cmd:
                    pids.append(pid)
        except OSError:
            pass
        return pids

    def _port_free(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            return s.connect_ex(("127.0.0.1", self.port)) != 0
        except OSError:
            return True
        finally:
            s.close()

    def _kill_profile_chrome(self) -> None:
        for pid in self._chrome_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.proc:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait(timeout=1)
            except Exception:
                pass
            self.proc = None
        self._casting = False
        for _ in range(40):
            if self._port_free() and not self._chrome_pids():
                return
            time.sleep(0.08)

    def start(self) -> None:
        if self.alive and self.healthy():
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        chrome = Path(CHROME)
        if not chrome.is_file():
            raise RuntimeError(f"Chrome not found at {chrome}")
        self._stop.clear()
        self._reader_started = False
        self._stale = False
        self._rx = b""
        self._watch_started = False
        self._kill_profile_chrome()
        self.proc = subprocess.Popen(
            [
                str(chrome),
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                "--headless=new",
                "--ozone-platform=headless",
                f"--ozone-override-screen-size={self.view_w},{self.view_h}",
                "--use-gl=angle",
                "--use-angle=swiftshader-webgl",
                "--enable-webgl",
                "--ignore-gpu-blocklist",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                f"--window-size={self.view_w},{self.view_h}",
                "--remote-allow-origins=*",
                "--noerrdialogs",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=BlockInsecurePrivateNetworkRequests,PaintHolding",
                "--hide-scrollbars",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        page = None
        for _ in range(80):
            if self.proc.poll() is not None:
                raise RuntimeError("Chrome exited before DevTools was ready")
            time.sleep(0.12)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1) as r:
                    pages = json.loads(r.read().decode())
                page = next((p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")), None)
                if page:
                    self.url = page.get("url") or self.url
                    break
            except Exception:
                continue
        if not page:
            self._kill_profile_chrome()
            raise RuntimeError("Chrome DevTools page not ready")
        self.alive = True
        self._bind_page(page)
        ping = self._cdp("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=2)
        if ping is None:
            self.alive = False
            self._kill_profile_chrome()
            raise RuntimeError("Chrome DevTools websocket is not answering")
        self.screenshot(force=True)

    def _devtools(self, path: str, timeout: float = 2, method: str = "GET") -> Any:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _open_ws(self, ws_url: str) -> None:
        u = urlparse(ws_url)
        old = self.sock
        self.sock = None
        if old:
            try:
                old.close()
            except Exception:
                pass
        time.sleep(0.02)
        new_sock, extra = _ws_handshake(u.hostname or "127.0.0.1", u.port or self.port, u.path)
        new_sock.settimeout(8)
        self._rx = extra
        self.sock = new_sock

    def _bind_page(self, page: dict[str, Any]) -> None:
        ws = page.get("webSocketDebuggerUrl")
        if not ws:
            raise RuntimeError("no debugger url")
        self.page_id = page.get("id") or ""
        if page.get("url"):
            self.url = page["url"]
        self._open_ws(ws)
        self._stale = False
        if not self._reader_started:
            self._reader_started = True
            gen = self._reader_gen
            threading.Thread(target=self._reader, args=(gen,), daemon=True).start()
        if self._cdp("Page.enable", {}, timeout=2) is None:
            self._stale = True
            raise RuntimeError("Page.enable failed")
        self._cdp("Runtime.enable", {}, timeout=2)
        self._cdp("Emulation.setFocusEmulationEnabled", {"enabled": True}, wait=False)
        self._cdp("Page.bringToFront", {}, wait=False)
        try:
            self._cdp("Page.setWebLifecycleState", {"state": "active"}, wait=False)
        except Exception:
            pass
        self._cdp(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            },
            wait=False,
        )
        self._cdp(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.view_w,
                "height": self.view_h,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
            wait=False,
        )
        self._start_cast(restart=True)
        if not self._watch_started:
            self._watch_started = True
            self._watch_gen += 1
            threading.Thread(target=self._cast_watch, args=(self._watch_gen,), daemon=True).start()

    def list_tabs(self) -> list[dict[str, Any]]:
        try:
            pages = self._devtools("/json/list")
        except Exception:
            return []
        if not isinstance(pages, list):
            return []
        out = []
        for p in pages:
            if not isinstance(p, dict) or p.get("type") != "page":
                continue
            out.append(
                {
                    "id": p.get("id") or "",
                    "url": p.get("url") or "",
                    "title": p.get("title") or "",
                    "active": (p.get("id") or "") == self.page_id,
                }
            )
        return out

    def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        target = url or "about:blank"
        q = quote(target, safe=":/?&=#")
        page = self._devtools(f"/json/new?{q}", timeout=5, method="PUT")
        if not isinstance(page, dict):
            raise RuntimeError("could not open tab")
        self._bind_page(page)
        return {"id": self.page_id, "url": self.url, "tabs": self.list_tabs()}

    def activate_tab(self, tid: str) -> dict[str, Any]:
        pages = self._devtools("/json/list")
        if not isinstance(pages, list):
            raise RuntimeError("no tabs")
        page = next((p for p in pages if isinstance(p, dict) and p.get("id") == tid), None)
        if not page:
            raise RuntimeError("tab not found")
        if tid != self.page_id:
            self._bind_page(page)
        return {"id": self.page_id, "url": self.url, "tabs": self.list_tabs()}

    def close_tab(self, tid: str | None = None) -> dict[str, Any]:
        tid = tid or self.page_id
        tabs = self.list_tabs()
        others = [t for t in tabs if t.get("id") != tid]
        if others and tid == self.page_id:
            self.activate_tab(others[0]["id"])
        try:
            self._devtools(f"/json/close/{tid}", method="PUT")
        except Exception:
            pass
        left = self.list_tabs()
        if not left:
            return self.new_tab("about:blank")
        if self.page_id == tid or not any(t.get("id") == self.page_id for t in left):
            self.activate_tab(left[0]["id"])
        return {"id": self.page_id, "url": self.url, "tabs": self.list_tabs()}

    def _attach_page(self) -> None:
        page = self._page_from_list()
        if page and page.get("url"):
            self.url = page["url"]

    def _read_n(self, sock: socket.socket, n: int) -> bytes:
        while len(self._rx) < n:
            chunk = sock.recv(max(4096, n - len(self._rx)))
            if not chunk:
                raise RuntimeError("socket closed")
            self._rx += chunk
        out = self._rx[:n]
        self._rx = self._rx[n:]
        return out

    def _recv_ws(self, sock: socket.socket) -> bytes:
        assembled = b""
        while True:
            hdr = self._read_n(sock, 2)
            fin = bool(hdr[0] & 0x80)
            opcode = hdr[0] & 0x0F
            ln = hdr[1] & 0x7F
            masked = bool(hdr[1] & 0x80)
            if ln == 126:
                ln = int.from_bytes(self._read_n(sock, 2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read_n(sock, 8), "big")
            mask = self._read_n(sock, 4) if masked else b""
            data = self._read_n(sock, ln)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                return b""
            if opcode == 0x9:
                _ws_send(sock, data, 0xA)
                continue
            if opcode == 0xA:
                continue
            assembled += data
            if fin:
                return assembled

    def _cdp(self, method: str, params: dict[str, Any], wait: bool = True, timeout: float = 5, stale_on_timeout: bool = True) -> dict[str, Any] | None:
        if not self.sock:
            self._stale = True
            return None
        ev = threading.Event()
        box: dict[str, Any] = {}
        with self._wlock:
            self._id += 1
            rid = self._id
            if wait:
                self._pending[rid] = (ev, box)
            try:
                _ws_send(self.sock, json.dumps({"id": rid, "method": method, "params": params}).encode())
            except Exception:
                self._pending.pop(rid, None)
                self._stale = True
                return None
        if not wait:
            return {}
        if not ev.wait(timeout):
            self._pending.pop(rid, None)
            if stale_on_timeout:
                self._stale = True
            return None
        if "error" in box:
            raise RuntimeError(box["error"])
        return box.get("result") or {}

    def _ping(self) -> bool:
        try:
            res = self._cdp("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=1.2)
            return res is not None
        except Exception:
            return False

    def _page_from_list(self) -> dict[str, Any] | None:
        try:
            pages = self._devtools("/json/list")
        except Exception:
            return None
        if not isinstance(pages, list):
            return None
        if self.page_id:
            hit = next((p for p in pages if isinstance(p, dict) and p.get("id") == self.page_id), None)
            if hit:
                return hit
        return next((p for p in pages if isinstance(p, dict) and p.get("type") == "page" and p.get("webSocketDebuggerUrl")), None)

    def _revive(self) -> None:
        page = self._page_from_list()
        if page:
            try:
                self._bind_page(page)
                if self._ping():
                    self._stale = False
                    self.alive = True
                    return
            except Exception:
                pass
        self.stop()
        time.sleep(0.15)
        self.start()

    def _reader(self, gen: int) -> None:
        while not self._stop.is_set() and self.alive and gen == self._reader_gen:
            sock = self.sock
            if not sock:
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select([sock], [], [], 0.4)
                if not ready or self.sock is not sock:
                    continue
                raw = self._recv_ws(sock)
            except Exception:
                time.sleep(0.05)
                continue
            if not raw:
                self._stale = True
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                ev, box = self._pending.pop(mid)
                box.update(msg)
                ev.set()
                continue
            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "Page.screencastFrame":
                self._on_cast_frame(params)
                continue
            elif method == "Page.loadEventFired" or method == "Page.domContentEventFired":
                self._loaded.set()
            elif method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if frame.get("url") and not frame.get("parentId"):
                    self.url = frame["url"]
            elif method == "Page.navigatedWithinDocument":
                if params.get("url"):
                    self.url = params["url"]

    def _on_cast_frame(self, params: dict[str, Any]) -> None:
        data = params.get("data") or ""
        if data:
            try:
                self.frame_jpeg = base64.b64decode(data)
                self.frame_seq += 1
                self._last_frame_at = time.time()
                self._casting = True
            except Exception:
                pass
        # Do not copy screencast JPEG size into view_w/view_h — that fights the
        # emulated viewport and makes the MiniOS img jump (1200x800 vs 1280x800).
        sid = params.get("sessionId")
        ack: dict[str, Any] = {}
        if sid is not None:
            ack["sessionId"] = sid
        self._cdp("Page.screencastFrameAck", ack, wait=False)

    def _start_cast(self, *, restart: bool = False) -> None:
        if self._casting and not restart:
            return
        if restart:
            try:
                self._cdp("Page.stopScreencast", {}, wait=False)
            except Exception:
                pass
            self._casting = False
        try:
            self._cdp(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": int(self.view_w or 1280),
                    "maxHeight": int(self.view_h or 800),
                    "everyNthFrame": 1,
                },
                wait=False,
            )
            self._casting = True
        except Exception:
            self._casting = False

    def navigate(self, url: str, wait: float = 8.0) -> str:
        if not self.healthy():
            self._revive()
        self.url = url
        self._loaded.clear()
        before = self.frame_seq
        res = self._cdp("Page.navigate", {"url": url}, timeout=12, stale_on_timeout=False)
        if res is None:
            res = self._cdp("Page.navigate", {"url": url}, timeout=12, stale_on_timeout=False)
        if not self._casting:
            self._start_cast()
        self._loaded.wait(timeout=max(1.0, wait))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self.frame_seq > before and self.frame_jpeg:
                break
            time.sleep(0.05)
        if self.frame_seq <= before or not self.frame_jpeg:
            self.screenshot(force=True)
        self._last_input_at = time.time()
        self._attach_page()
        return self.url

    def evaluate(self, expression: str) -> Any:
        try:
            res = self._cdp(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                timeout=1.5,
                stale_on_timeout=False,
            ) or {}
            return (res.get("result") or {}).get("value")
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        self._attach_page()
        info = self.evaluate(
            "(() => { const el = document.activeElement;"
            " const vals = [...document.querySelectorAll('input,textarea,select')]"
            ".map(e => (e.value || '')).filter(Boolean).join(' ');"
            " return {title: document.title || '', href: location.href || '',"
            " text: (((document.body && document.body.innerText) || '') + ' ' + vals).slice(0, 5000),"
            " active: el ? (el.tagName + (el.id ? '#' + el.id : '')) : ''}; })()"
        )
        if not isinstance(info, dict):
            info = {"title": "", "href": self.url, "text": "", "active": ""}
        self.screenshot()
        return {
            "url": info.get("href") or self.url,
            "title": info.get("title") or "",
            "text": info.get("text") or "",
            "active": info.get("active") or "",
        }

    def click_selector(self, selector: str) -> dict[str, Any]:
        sel = json.dumps(selector)
        out = self.evaluate(
            "(() => { const el = document.querySelector(" + sel + ");"
            " if (!el) return {ok: false, error: 'not found'};"
            " el.scrollIntoView({block:'center'});"
            " const r = el.getBoundingClientRect();"
            " try { el.focus({preventScroll:true}); } catch (e) { try { el.focus(); } catch (e2) {} }"
            " el.click();"
            " return {ok: true, tag: el.tagName, x: r.left + r.width/2, y: r.top + r.height/2}; })()"
        )
        if isinstance(out, dict) and out.get("ok"):
            try:
                self._last_click = (float(out.get("x") or 0), float(out.get("y") or 0))
            except (TypeError, ValueError):
                pass
        return out if isinstance(out, dict) else {"ok": False}

    def type_text(self, text: str, submit: bool = False) -> None:
        self._focus_page()
        if self._last_click:
            self._focus_point(self._last_click[0], self._last_click[1])
        self.insert_text(text)
        if text and text not in self._active_edit_value():
            self._dom_insert_text(text)
            self._kick_frame()
        if not submit:
            return
        try:
            for kind in ("keyDown", "keyUp"):
                self._cdp(
                    "Input.dispatchKeyEvent",
                    {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                    wait=False,
                )
        except Exception:
            pass

    def refresh_viewport(self) -> None:
        info = self.evaluate("({w: window.innerWidth, h: window.innerHeight})")
        if isinstance(info, dict):
            try:
                self.view_w = max(1, int(info.get("w") or self.view_w))
                self.view_h = max(1, int(info.get("h") or self.view_h))
            except (TypeError, ValueError):
                pass

    def dispatch_mouse(
        self,
        typ: str,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delta_x: float = 0,
        delta_y: float = 0,
        modifiers: int = 0,
    ) -> None:
        px = max(0.0, float(x))
        py = max(0.0, float(y))
        btn = button or "left"
        bit = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}.get(btn, 1)
        params: dict[str, Any] = {
            "type": typ,
            "x": px,
            "y": py,
            "modifiers": int(modifiers or 0),
        }
        if typ == "mouseMoved":
            params["button"] = "none"
            params["buttons"] = 0
        elif typ == "mousePressed":
            params["button"] = btn
            params["buttons"] = bit
            params["clickCount"] = max(1, int(click_count or 1))
            try:
                self._cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py, "button": "none", "buttons": 0, "modifiers": int(modifiers or 0)}, wait=False)
            except Exception:
                pass
        elif typ == "mouseReleased":
            params["button"] = btn
            params["buttons"] = 0
            params["clickCount"] = max(1, int(click_count or 1))
        elif typ == "mouseWheel":
            params["deltaX"] = float(delta_x or 0)
            params["deltaY"] = float(delta_y or 0)
        try:
            self._cdp("Input.dispatchMouseEvent", params, wait=False)
        except Exception:
            pass
        if typ in ("mousePressed", "mouseReleased", "mouseWheel"):
            if typ != "mouseWheel":
                self._last_click = (px, py)
            self._last_input_at = time.time()
            if typ == "mousePressed":
                self._focus_point(px, py)
            self._kick_frame()

    def _focus_page(self) -> None:
        try:
            self._cdp("Emulation.setFocusEmulationEnabled", {"enabled": True}, wait=False)
        except Exception:
            pass
        try:
            self._cdp("Page.bringToFront", {}, wait=False)
        except Exception:
            pass

    def _focus_point(self, x: float, y: float) -> None:
        """Focus the DOM node under a click so typing reaches inputs in the page body."""
        self._focus_page()
        self.evaluate(
            "(() => {"
            f" const x = {x:.2f}, y = {y:.2f};"
            " const pierce = (doc, px, py) => {"
            "   if (!doc) return null;"
            "   let el = doc.elementFromPoint(px, py);"
            "   if (!el) return null;"
            "   if (el.shadowRoot) {"
            "     const inner = el.shadowRoot.elementFromPoint(px, py);"
            "     if (inner) el = inner;"
            "   }"
            "   if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {"
            "     try {"
            "       const r = el.getBoundingClientRect();"
            "       const nested = pierce(el.contentDocument, px - r.left, py - r.top);"
            "       if (nested) return nested;"
            "     } catch (e) {}"
            "   }"
            "   return el;"
            " };"
            " const el = pierce(document, x, y);"
            " if (!el) return false;"
            " const t = el.closest && el.closest('input,textarea,select,[contenteditable=\"\"],[contenteditable=true],[contenteditable=plaintext-only]');"
            " const n = t || el;"
            " try { n.focus({preventScroll:true}); } catch (e) { try { n.focus(); } catch (e2) {} }"
            " return true;"
            "})()"
        )

    def dispatch_key(
        self,
        typ: str,
        key: str = "",
        code: str = "",
        text: str = "",
        vk: int = 0,
        modifiers: int = 0,
    ) -> None:
        params: dict[str, Any] = {
            "type": typ,
            "key": key or "",
            "code": code or "",
            "windowsVirtualKeyCode": int(vk or 0),
            "nativeVirtualKeyCode": int(vk or 0),
            "modifiers": int(modifiers or 0),
        }
        if text:
            params["text"] = text
            params["unmodifiedText"] = text
        try:
            self._cdp("Input.dispatchKeyEvent", params, wait=False)
        except Exception:
            pass

    def _active_edit_value(self) -> str:
        val = self.evaluate(
            "(() => {"
            " const el = document.activeElement;"
            " if (!el) return '';"
            " if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return String(el.value || '');"
            " if (el.isContentEditable) return String(el.innerText || '');"
            " return '';"
            "})()"
        )
        return val if isinstance(val, str) else ""

    def _dom_insert_text(self, text: str) -> bool:
        payload = json.dumps(text)
        out = self.evaluate(
            "(() => {"
            f" const text = {payload};"
            " const el = document.activeElement;"
            " if (!el) return false;"
            " const tag = (el.tagName || '').toUpperCase();"
            " if (tag === 'INPUT' || tag === 'TEXTAREA') {"
            "   const proto = tag === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;"
            "   const desc = Object.getOwnPropertyDescriptor(proto, 'value');"
            "   const start = el.selectionStart ?? (el.value || '').length;"
            "   const end = el.selectionEnd ?? start;"
            "   const cur = String(el.value || '');"
            "   const next = cur.slice(0, start) + text + cur.slice(end);"
            "   if (desc && desc.set) desc.set.call(el, next); else el.value = next;"
            "   const pos = start + text.length;"
            "   try { el.setSelectionRange(pos, pos); } catch (e) {}"
            "   try { el.dispatchEvent(new InputEvent('input', {bubbles:true, data:text, inputType:'insertText'})); }"
            "   catch (e) { el.dispatchEvent(new Event('input', {bubbles:true})); }"
            "   return true;"
            " }"
            " if (el.isContentEditable) {"
            "   try { return document.execCommand('insertText', false, text); } catch (e) { return false; }"
            " }"
            " return false;"
            "})()"
        )
        return bool(out)

    def insert_text(self, text: str) -> None:
        if not text:
            return
        self._last_input_at = time.time()
        try:
            self._cdp("Input.insertText", {"text": text}, wait=False)
        except Exception:
            pass
        self._kick_frame()

    def _kick_frame(self) -> None:
        self._kick.set()

    def _cast_watch(self, gen: int) -> None:
        """Keep the JPEG view moving. Prefer screencast; do not mix in full-page
        screenshots (those are a different aspect ratio and make MiniOS flicker)."""
        last_restart = 0.0
        while not self._stop.is_set() and gen == self._watch_gen:
            kicked = self._kick.wait(timeout=0.5)
            self._kick.clear()
            if self._stop.is_set() or gen != self._watch_gen or not self.alive or not self.sock:
                continue
            now = time.time()
            gap = now - (self._last_frame_at or 0.0)
            if kicked:
                time.sleep(0.08)
                if time.time() - (self._last_frame_at or 0.0) < 0.25:
                    continue
                if self._casting and self.frame_jpeg:
                    continue
                self.screenshot(force=True)
                continue
            if gap < 1.6:
                continue
            if now - last_restart > 4.0:
                last_restart = now
                try:
                    self._start_cast(restart=True)
                    self._cdp("Page.bringToFront", {}, wait=False)
                    self._cdp("Page.setWebLifecycleState", {"state": "active"}, wait=False)
                except Exception:
                    pass
                continue
            if gap > 3.0 and not self.frame_jpeg:
                self.screenshot(force=True)

    def screenshot(self, force: bool = False) -> bytes:
        if self.frame_jpeg and not force:
            return self.frame_jpeg
        if not self._shot_lock.acquire(blocking=False):
            return self.frame_jpeg
        try:
            res = self._cdp(
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 52},
                timeout=1.2,
                stale_on_timeout=False,
            ) or {}
            data = res.get("data") or ""
            if data:
                self.frame_jpeg = base64.b64decode(data)
                self.frame_seq += 1
                self._last_frame_at = time.time()
                self._stale = False
        except Exception:
            pass
        finally:
            self._shot_lock.release()
        return self.frame_jpeg

    def set_view_size(self, width: int, height: int) -> None:
        w = max(800, min(1920, int(width or self.view_w)))
        h = max(500, min(1200, int(height or self.view_h)))
        if abs(w - self.view_w) < 8 and abs(h - self.view_h) < 8:
            return
        self.view_w, self.view_h = w, h
        try:
            self._cdp(
                "Emulation.setDeviceMetricsOverride",
                {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
                timeout=1.5,
            )
        except Exception:
            pass

    def apply_minios_view(self, view: dict[str, Any] | None) -> None:
        payload = json.dumps(
            {
                "windows": (view or {}).get("windows") or [],
                "dock": (view or {}).get("dock") or "left",
                "wallpaper": (view or {}).get("wallpaper") or "",
                "wallpaperStyle": (view or {}).get("wallpaperStyle") or "",
            }
        )
        self.evaluate(
            """(() => {
              const view = %s;
              document.body.classList.add('observer-mode','live-desktop-entered');
              document.body.classList.remove('compact-layout','mobile-chat');
              document.body.dataset.layout = 'desktop';
              const app = document.getElementById('app');
              if (app) app.classList.remove('compact-layout','left-collapsed','right-collapsed','screen-closed');
              const dockEl = document.getElementById('dock');
              const edge = view.dock;
              const desk = document.querySelector('.ubuntu-desktop-area');
              if (dockEl && ['left','right','top','bottom'].indexOf(edge) >= 0) {
                dockEl.classList.remove('dock-left','dock-right','dock-top','dock-bottom');
                dockEl.classList.add('dock-'+edge);
                if (desk) {
                  desk.classList.remove('dock-edge-left','dock-edge-right','dock-edge-top','dock-edge-bottom');
                  desk.classList.add('dock-edge-'+edge);
                }
              }
              if (desk) {
                if (view.wallpaper) desk.dataset.wallpaper = String(view.wallpaper);
                if (view.wallpaperStyle) desk.style.background = view.wallpaperStyle;
              }
              (view.windows || []).forEach((spec) => {
                if (!spec || !spec.app) return;
                const win = document.querySelector('.app-window[data-window-app="'+spec.app+'"]');
                if (!win) return;
                if (!spec.open) {
                  win.dataset.open = 'false';
                  win.classList.add('hidden-window');
                  win.classList.remove('focused-window','maximized-window','minimized-window');
                  return;
                }
                win.dataset.open = 'true';
                win.classList.remove('hidden-window');
                win.classList.toggle('minimized-window', !!spec.minimized);
                if (spec.minimized) return;
                win.classList.toggle('maximized-window', !!spec.maximized);
                if (spec.focused) {
                  document.querySelectorAll('.app-window').forEach((w) => w.classList.remove('focused-window'));
                  win.classList.add('focused-window');
                  win.style.zIndex = '90';
                }
              });
              const area = document.querySelector('#ubuntuDesktopViewer');
              if (area) area.classList.add('desktop-fullscreen-overlay');
              window.dispatchEvent(new Event('resize'));
              return true;
            })()""" % payload
        )

    def screenshot_minios_desktop(self, prefer: str = "") -> bytes:
        """JPEG of this bot's MiniOS workspace desktop only (not the host display)."""
        box = self.evaluate(
            """(() => {
              const boot = document.getElementById('os-boot');
              const login = document.getElementById('os-login');
              if (boot) { boot.hidden = true; boot.style.opacity = ''; }
              if (login) { login.hidden = true; login.classList.remove('show'); }
              document.body.classList.add('observer-mode', 'live-desktop-entered');
              document.body.classList.remove('compact-layout', 'mobile-chat');
              document.body.dataset.layout = 'desktop';
              const el = document.querySelector('.ubuntu-desktop-area')
                || document.querySelector('#ubuntuDesktopViewer');
              if (!el) return null;
              document.querySelector('#ubuntuDesktopViewer')?.classList.add('desktop-fullscreen-overlay');
              const r = el.getBoundingClientRect();
              if (r.width < 8 || r.height < 8) return null;
              return {x: Math.max(0, r.x), y: Math.max(0, r.y), width: r.width, height: r.height};
            })()"""
        )
        params: dict[str, Any] = {"format": "jpeg", "quality": 70}
        if isinstance(box, dict) and box.get("width") and box.get("height"):
            params["clip"] = {
                "x": max(0.0, float(box["x"])),
                "y": max(0.0, float(box["y"])),
                "width": float(box["width"]),
                "height": float(box["height"]),
                "scale": 1,
            }
        if not self._shot_lock.acquire(timeout=2.0):
            return self.frame_jpeg
        try:
            res = self._cdp("Page.captureScreenshot", params, timeout=3.0, stale_on_timeout=False) or {}
            data = res.get("data") or ""
            if data:
                self.frame_jpeg = base64.b64decode(data)
                self.frame_seq += 1
                self._last_frame_at = time.time()
                self._stale = False
        except Exception:
            pass
        finally:
            self._shot_lock.release()
        return self.frame_jpeg

    def stop(self) -> None:
        self._reader_gen += 1
        self._watch_gen += 1
        self._watch_started = False
        self._stop.set()
        self._kick.set()
        self.alive = False
        self._stale = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        self._reader_started = False
        self._kill_profile_chrome()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return


class LocalSite:
    """Serve one bot workspace over HTTP so Chromium can show local pages."""

    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.alive = False

    def start(self) -> None:
        if self.alive:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        handler = partial(_QuietHandler, directory=str(self.root))
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.alive = True

    def url_for(self, rel: str = "") -> str:
        rel = (rel or "").lstrip("/")
        return f"http://127.0.0.1:{self.port}/{rel}"

    def stop(self) -> None:
        self.alive = False
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            self.httpd = None


def search_url(query: str) -> str:
    q = quote_plus((query or "").strip())
    return f"https://duckduckgo.com/?q={q}"


class PtySurface:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str]) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.fd: int | None = None
        self.pid: int | None = None
        self.alive = False
        self._buf = bytearray()
        self._cv = threading.Condition()
        self.seq = 0

    def start(self) -> None:
        if self.alive:
            return
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                os.execvpe(self.argv[0], self.argv, self.env)
            except Exception:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self.alive = True
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        except OSError:
            pass
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.fd is not None
        while self.alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.4)
                if not r:
                    continue
                data = os.read(self.fd, 8192)
                if not data:
                    break
                with self._cv:
                    self._buf.extend(data)
                    self.seq = len(self._buf)
                    self._cv.notify_all()
            except OSError:
                break
        self.alive = False

    def write(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            self.alive = False

    def resize(self, cols: int, rows: int) -> None:
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def pull(self, after: int, wait: float = 0.0) -> tuple[int, bytes]:
        """Return (new_seq, bytes_since_after). `after` is a byte offset."""
        with self._cv:
            if wait and len(self._buf) <= after:
                self._cv.wait(timeout=wait)
            data = bytes(self._buf)
            start = after if 0 <= after <= len(data) else 0
            if after > len(data):
                start = 0
            return len(data), data[start:]

    def stop(self) -> None:
        self.alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
