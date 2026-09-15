"""Explicit cognitive profiles and capability flags.

Do not infer embodiment from a bot display name when an explicit profile exists.
Kind is only a default when cognitive_profile is unset.
"""

from __future__ import annotations

from typing import Any

PROFILE_BUILD = "build_agent"
PROFILE_EMBODIED = "embodied_agent"

SHARED_CAPS = {
    "persistent_memory": True,
    "conversation_memory": True,
    "semantic_memory": True,
    "episodic_memory": True,
    "procedural_memory": True,
    "task_memory": True,
}

BUILD_ONLY = {
    "project_memory": True,
    "code_context": True,
    "tool_memory": True,
    "body_state": False,
    "world_state": False,
    "continuous_perception": False,
    "spatial_memory": False,
    "sensorimotor_memory": False,
    "motor_learning": False,
    "safety_state": False,
}

EMBODIED_ONLY = {
    "project_memory": True,
    "code_context": True,
    "tool_memory": True,
    "body_state": True,
    "world_state": True,
    "continuous_perception": True,
    "spatial_memory": True,
    "sensorimotor_memory": True,
    "motor_learning": True,
    "safety_state": True,
}

EMBODIED_CAPS = {
    "body_state",
    "world_state",
    "continuous_perception",
    "spatial_memory",
    "sensorimotor_memory",
    "motor_learning",
    "safety_state",
}

_KIND_EMBODIED = {"teela-brain", "teela", "robot", "embodiment"}
_KIND_BUILD = {"hermes", "build", "hermesbuild"}


def _normalize_kind(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def profile_name(bot: Any) -> str:
    explicit = getattr(bot, "cognitive_profile", None)
    if isinstance(explicit, str) and explicit.strip():
        name = explicit.strip().lower().replace("-", "_")
        if name in {PROFILE_BUILD, PROFILE_EMBODIED}:
            return name
    caps = getattr(bot, "capabilities", None)
    if isinstance(caps, dict):
        raw = caps.get("cognitive_profile")
        if isinstance(raw, str) and raw.strip():
            name = raw.strip().lower().replace("-", "_")
            if name in {PROFILE_BUILD, PROFILE_EMBODIED}:
                return name
    kind = _normalize_kind(getattr(bot, "kind", None))
    if kind in _KIND_BUILD:
        return PROFILE_BUILD
    if kind in _KIND_EMBODIED:
        return PROFILE_EMBODIED
    # Legacy empty kind occupies the Teela slot; do not use the display name.
    if not kind:
        return PROFILE_EMBODIED
    return PROFILE_BUILD


def default_capabilities(profile: str) -> dict[str, bool]:
    out = dict(SHARED_CAPS)
    if profile == PROFILE_EMBODIED:
        out.update(EMBODIED_ONLY)
    else:
        out.update(BUILD_ONLY)
    return out


def capabilities(bot: Any) -> dict[str, bool]:
    profile = profile_name(bot)
    out = default_capabilities(profile)
    extra = getattr(bot, "capabilities", None)
    if isinstance(extra, dict):
        for key, val in extra.items():
            if key == "cognitive_profile":
                continue
            if isinstance(val, bool):
                out[str(key)] = val
    return out


def has_cap(bot: Any, name: str) -> bool:
    return bool(capabilities(bot).get(name))


def is_embodied(bot: Any) -> bool:
    return profile_name(bot) == PROFILE_EMBODIED and has_cap(bot, "body_state")


def is_build(bot: Any) -> bool:
    return profile_name(bot) == PROFILE_BUILD
