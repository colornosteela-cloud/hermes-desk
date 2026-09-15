#!/usr/bin/env python3
"""MiniOS motion router: STATIC → Jetson, DYNAMIC → teela-body WBC.

Brain MiniOS always updates the digital twin first. Hardware is optional and
never on the 50 Hz path — cluster HTTP is intent only.
"""
from __future__ import annotations

import os
import re
from typing import Any

JETSON_PEER = os.environ.get("HERMES_DESK_JETSON_NODE", "teela-jetson")
BODY_PEER = os.environ.get("HERMES_DESK_BODY_NODE", "teela-body")
EXECUTE_PATH = "/v1/cluster/robot/execute"
WBC_PATH = "/v1/cluster/robot/wbc"
CAP_PATH = "/v1/cluster/robot/capabilities"
MESH_TIMEOUT = 0.45

# Locomotion / balance. MiniOS may animate the twin; Jetson must not servo these
# until teela-body WBC is loaded.
DYNAMIC_CMDS = {
    "walk",
    "start_walk",
    "walk_left",
    "walk_right",
    "walk_place",
    "walk_back",
    "walk_north",
    "walk_south",
    "walk_east",
    "walk_west",
}
SAFETY_CMDS = {"estop_on", "estop_off", "motors_off"}

_VISUAL_REF = re.compile(
    r"\b(?:"
    r"look\s+at|"
    r"point\s+(?:at|to)\s+(?:the|this|that|him|her|it)|"
    r"(?:this|that)\s+(?:screenshot|image|photo|picture|video|clip|object|cup|bottle|person)|"
    r"the\s+(?:cup|bottle|person|object|screenshot|image|photo|picture|video)|"
    r"mimic|copy this|watch this|do this(?:\s+move)?"
    r")\b",
    re.IGNORECASE,
)


def node_role(name: str) -> str:
    n = (name or "").strip().lower()
    if "jetson" in n:
        return "jetson"
    if n.endswith("-body") or n == "body" or "teela-body" in n:
        return "body"
    if "brain" in n:
        return "brain"
    return "unknown"


def needs_vision(text: str) -> bool:
    """True when a motor request depends on pixels (see, then move)."""
    t = " ".join((text or "").split())
    return bool(t and _VISUAL_REF.search(t))


def balance_mode(cmd: str, text: str = "") -> str:
    c = (cmd or "").strip().lower()
    if c in SAFETY_CMDS:
        return "safety"
    if c in DYNAMIC_CMDS:
        return "dynamic"
    blob = f"{c} {text or ''}".lower()
    if re.search(r"\b(?:pick|grasp|get)\b.*\b(?:floor|ground|up)\b", blob):
        return "dynamic"
    if re.search(r"\b(?:kneel|get\s+up|crawl|recover|balance)\b", blob):
        return "dynamic"
    return "static"


def capabilities(node_name: str) -> dict[str, Any]:
    role = node_role(node_name)
    wbc = os.environ.get("HERMES_DESK_WBC", "").strip() in {"1", "true", "yes"}
    motors = os.environ.get("HERMES_DESK_MOTORS", "").strip() in {"1", "true", "yes"}
    return {
        "node": node_name,
        "role": role,
        "sim": True,
        "wbc": bool(wbc and role == "body"),
        "sam": bool(role == "body"),
        "motors": bool(motors and role == "jetson"),
        "note": {
            "brain": "cognition + MiniOS twin; no WBC/SAM",
            "body": "WBC/SAM/V-JEPA host; CUDA only",
            "jetson": "motor loop + E-stop; no neural WBC",
            "unknown": "intent mesh node",
        }.get(role, ""),
    }


def _peer_post(cluster: Any, name: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if cluster is None:
        return {"ok": False, "skipped": True, "reason": "no cluster"}
    post = getattr(cluster, "post_named", None)
    if not callable(post):
        return {"ok": False, "skipped": True, "reason": "cluster cannot post"}
    try:
        return post(name, path, body, timeout=MESH_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": str(e), "peer": name}


def _peer_get(cluster: Any, name: str, path: str) -> dict[str, Any]:
    if cluster is None:
        return {"ok": False, "skipped": True, "reason": "no cluster"}
    get = getattr(cluster, "get_named", None)
    if not callable(get):
        return {"ok": False, "skipped": True, "reason": "cluster cannot get"}
    try:
        return get(name, path, timeout=MESH_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": str(e), "peer": name}


def probe_mesh(cluster: Any, *, origin: str = "") -> dict[str, Any]:
    """Read-only jetson + body health. Never issues a motion command."""
    origin = origin or (str(getattr(cluster, "node_name", "") or "") if cluster is not None else "")
    local = capabilities(origin) if origin else {"role": "unknown", "sim": True}
    jetson = _peer_get(cluster, JETSON_PEER, CAP_PATH)
    wbc = _peer_get(cluster, BODY_PEER, CAP_PATH)
    jetson_live = bool((jetson or {}).get("ok")) and bool(
        (jetson or {}).get("motors") or (jetson or {}).get("accepted")
    )
    wbc_live = bool((wbc or {}).get("ok")) and bool(
        (wbc or {}).get("wbc") or (wbc or {}).get("accepted")
    )
    hardware = bool(jetson_live or wbc_live)
    notes: list[str] = []
    if (jetson or {}).get("skipped"):
        notes.append(str((jetson or {}).get("reason") or "jetson peer not configured"))
    elif (jetson or {}).get("ok") is False:
        notes.append(str((jetson or {}).get("error") or "jetson peer unreachable"))
    if (wbc or {}).get("skipped"):
        notes.append(str((wbc or {}).get("reason") or "body peer not configured"))
    elif (wbc or {}).get("ok") is False:
        notes.append(str((wbc or {}).get("error") or "body peer unreachable"))
    if not hardware:
        notes.append("MiniOS twin only — physical motors not attached")
    return {
        "mode": "probe",
        "hardware": hardware,
        "local": local,
        "jetson": jetson,
        "wbc": wbc,
        "reason": "; ".join(dict.fromkeys(notes)) if notes else "",
    }


def dispatch(
    cluster: Any,
    command: dict[str, Any],
    *,
    origin: str = "",
    bot_id: str = "",
    wbc_loaded: bool | None = None,
) -> dict[str, Any]:
    """Fan intent to Jetson (STATIC/safety) and/or body WBC (DYNAMIC). Never raises."""
    cmd = str(command.get("cmd") or "")
    if cmd in {
        "walk_left",
        "walk_right",
        "walk_place",
        "walk_back",
        "walk_north",
        "walk_south",
        "walk_east",
        "walk_west",
    }:
        part = cmd.split("_", 1)[1]
        mapped = {"north": "back", "south": "south", "east": "left", "west": "right"}.get(part, part)
        command = {**command, "cmd": "walk", "direction": command.get("direction") or mapped}
        cmd = "walk"
    mode = balance_mode(cmd)
    out: dict[str, Any] = {
        "mode": mode,
        "hardware": False,
        "jetson": None,
        "wbc": None,
    }
    payload = {
        "origin_node": origin,
        "bot_id": bot_id,
        "cmd": cmd,
        "pose": command.get("pose"),
        "joint": command.get("joint"),
        "value": command.get("value"),
        "joints": command.get("joints"),
        "dir": command.get("dir"),
        "delta": command.get("delta"),
        "direction": command.get("direction"),
        "task": command.get("task") or cmd,
    }
    if mode == "safety":
        out["jetson"] = _peer_post(cluster, JETSON_PEER, EXECUTE_PATH, payload)
        out["wbc"] = _peer_post(cluster, BODY_PEER, WBC_PATH, {**payload, "halt": True})
        out["hardware"] = bool((out["jetson"] or {}).get("ok"))
        return out
    if mode == "dynamic":
        loaded = wbc_loaded
        if loaded is None:
            loaded = os.environ.get("HERMES_DESK_WBC", "").strip() in {"1", "true", "yes"}
        if not loaded:
            out["reason"] = "wbc not loaded; dynamic motion stays in the MiniOS twin"
            out["wbc"] = _peer_post(cluster, BODY_PEER, WBC_PATH, payload)
            return out
        out["wbc"] = _peer_post(cluster, BODY_PEER, WBC_PATH, payload)
        out["hardware"] = bool((out["wbc"] or {}).get("ok"))
        return out
    out["jetson"] = _peer_post(cluster, JETSON_PEER, EXECUTE_PATH, payload)
    out["hardware"] = bool((out["jetson"] or {}).get("accepted") or (out["jetson"] or {}).get("ok"))
    if (out["jetson"] or {}).get("skipped"):
        out["reason"] = "jetson peer not configured; MiniOS twin only"
        out["hardware"] = False
    return out


def handle_execute(node_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Jetson (or any node) receives STATIC intent. No 50 Hz loop here."""
    caps = capabilities(node_name)
    cmd = str(body.get("cmd") or "")
    if cmd in DYNAMIC_CMDS:
        return {
            "ok": False,
            "accepted": False,
            "backend": "rejected",
            "error": "dynamic motion must go to teela-body WBC, not Jetson servos",
            **caps,
        }
    if caps["role"] != "jetson" or not caps["motors"]:
        return {
            "ok": True,
            "accepted": False,
            "backend": "stub",
            "cmd": cmd,
            "note": "motor daemon not attached; ack only",
            **caps,
        }
    return {"ok": True, "accepted": True, "backend": "jetson", "cmd": cmd, **caps}


def handle_wbc(node_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """teela-body WBC intake. SAM/WBC stay off teela-brain."""
    caps = capabilities(node_name)
    if caps["role"] != "body":
        return {
            "ok": False,
            "accepted": False,
            "backend": "wrong-node",
            "error": "WBC/SAM run on teela-body only",
            **caps,
        }
    if body.get("halt"):
        return {"ok": True, "accepted": True, "backend": "wbc-halt", "halt": True, **caps}
    if not caps["wbc"]:
        return {
            "ok": False,
            "accepted": False,
            "backend": "none",
            "error": "WBC not loaded",
            **caps,
        }
    return {"ok": True, "accepted": True, "backend": "wbc", "task": body.get("task"), **caps}
