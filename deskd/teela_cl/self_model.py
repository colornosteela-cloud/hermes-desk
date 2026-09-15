"""Machine-readable Teela self-model. Runtime truth, not Qwen guesses."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CapabilityFact:
    cap_id: str
    description: str
    available: bool = True
    reliability: float = 1.0
    kind: str = "software"  # software | primitive | sensor | actuator | permission
    family: str = ""
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass
class SelfModel:
    actuators: list[str]
    sensors: list[str]
    cameras: list[str]
    microphones: list[str]
    joints: list[str]
    motion_limits: dict[str, tuple[float, float]]
    software_tools: list[CapabilityFact]
    acp_capabilities: list[str]
    minios_capabilities: list[CapabilityFact]
    perception_systems: list[str]
    memory_systems: list[str]
    network_services: list[str]
    learned_skill_ids: list[str]
    permissions: dict[str, bool]
    unavailable: list[str]
    known_limitations: list[str]
    primitives: list[CapabilityFact]
    reliability: dict[str, float] = field(default_factory=dict)

    def available_primitive_ids(self) -> list[str]:
        return [p.cap_id for p in self.primitives if p.available]

    def available_tool_ids(self) -> list[str]:
        return [c.cap_id for c in self.software_tools + self.minios_capabilities if c.available]

    def has(self, cap_id: str) -> bool:
        cid = str(cap_id)
        if cid in self.unavailable:
            return False
        tails = {p.cap_id.rsplit(".", 1)[-1] for p in self.primitives if p.available}
        if cid in tails or cid in {p.cap_id for p in self.primitives if p.available}:
            return True
        if cid in self.available_tool_ids():
            return True
        return cid in self.learned_skill_ids

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_self_model(
    *,
    robot_state: dict[str, Any] | None = None,
    joints: dict[str, tuple[float, float]] | None = None,
    tools: list[str] | None = None,
    learned_skill_ids: list[str] | None = None,
    permissions: dict[str, bool] | None = None,
    hardware: bool = False,
    jetson: bool = False,
    cameras: list[str] | None = None,
    perception: list[str] | None = None,
) -> SelfModel:
    """Build a self-model from runtime facts. Absent hardware is unavailable."""
    from robot_sim import JOINTS  # local import: available on deskd path

    jlimits = joints or dict(JOINTS)
    tool_names = list(tools or [])
    minios = [
        CapabilityFact(
            "minios.desktop",
            "Open a workspace file or picture on the desktop; use notepad or the browser",
            True,
            0.9,
            "software",
        ),
        CapabilityFact("minios.browser", "Navigate MiniOS browser and search the web", True, 0.7, "software"),
        CapabilityFact("minios.body", "Command the MiniOS robot twin / virtual body", True, 0.9, "actuator"),
    ]
    software = [CapabilityFact(n, f"Tool {n}", True, 0.8, "software") for n in tool_names]
    software.extend(
        [
            CapabilityFact(
                "software.repo",
                "Figure out how a repository or codebase works by reading source files",
                True,
                0.8,
                "software",
            ),
            CapabilityFact(
                "software.service",
                "Restart a service, inspect a workflow, or learn how an interface behaves",
                True,
                0.8,
                "software",
            ),
            CapabilityFact(
                "software.research",
                "Search the web or acquire missing information about a procedure",
                True,
                0.7,
                "software",
            ),
            CapabilityFact(
                "software.shell",
                "Run a host shell command such as uname, hostname, df, free, lscpu, or nvidia-smi",
                True,
                0.9,
                "software",
            ),
            CapabilityFact(
                "software.system_check",
                "Check host system information: GPUs, llama, TTS, STT, memory, and services",
                True,
                0.9,
                "software",
            ),
            CapabilityFact(
                "software.grok_build",
                "Use hermes to run system info and host commands on this computer",
                True,
                0.9,
                "software",
            ),
        ]
    )
    prims = [
        CapabilityFact("balance", "Keep balance while standing", True, 0.9, "primitive", family="locomotion"),
        CapabilityFact("weight_shift", "Shift weight between feet", True, 0.85, "primitive", family="locomotion"),
        CapabilityFact("foot_slide", "Slide a foot along the ground", True, 0.7, "primitive", family="locomotion"),
        CapabilityFact("heel_raise", "Raise a heel", True, 0.8, "primitive", family="locomotion"),
        CapabilityFact("raise_arm", "Raise an arm", True, 0.95, "primitive", family="arm"),
        CapabilityFact("bend_elbow", "Bend an elbow", True, 0.95, "primitive", family="arm"),
        CapabilityFact("oscillate_wrist", "Rock a wrist", True, 0.9, "primitive", family="arm"),
        CapabilityFact("lower_arm", "Lower an arm", True, 0.95, "primitive", family="arm"),
        CapabilityFact("hold_shoulder", "Keep the upper arm or shoulder still", True, 0.9, "primitive", family="arm"),
        CapabilityFact("hold_elbow", "Keep the elbow still", True, 0.9, "primitive", family="arm"),
        CapabilityFact("orient_head", "Turn or tilt the head to look", True, 0.95, "primitive", family="head"),
        CapabilityFact("stand", "Stand upright", True, 0.95, "primitive", family="locomotion"),
        CapabilityFact("step", "Take a step", True, 0.9, "primitive", family="locomotion"),
    ]
    unavailable: list[str] = []
    if not hardware:
        unavailable.extend(["physical_motors", "wbc_hardware", "jetson"])
    if not jetson:
        unavailable.append("jetson_encoders")
    cams = list(cameras or [])
    if not cams:
        unavailable.append("head_camera")
    perms = dict(permissions or {"motors": False, "shell": False, "network": True, "workspace": True})
    return SelfModel(
        actuators=["virtual_html_twin", "minios_robot_sim"] + (["physical_mesh"] if hardware else []),
        sensors=["joint_command_echo"] + (["joint_encoders"] if jetson else []),
        cameras=cams,
        microphones=["stt_tunnel"] if perms.get("network") else [],
        joints=list(jlimits),
        motion_limits={k: (float(v[0]), float(v[1])) for k, v in jlimits.items()},
        software_tools=software,
        acp_capabilities=["glob", "grep", "read_file", "host-shell"] if perms.get("shell") else ["read_file"],
        minios_capabilities=minios,
        perception_systems=list(perception or []),
        memory_systems=["workspace_memory", "skill_store", "typed_memory"],
        network_services=["web_search"] if perms.get("network") else [],
        learned_skill_ids=list(learned_skill_ids or []),
        permissions=perms,
        unavailable=unavailable,
        known_limitations=[
            "No physical motors unless hardware permission is on.",
            "MiniOS Observer may be down; visual observe can fail.",
            "Virtual twin is not a perfect copy of a human dancer.",
        ],
        primitives=prims,
        reliability={"virtual_body": 0.9, "web_search": 0.7, "observer": 0.2 if not cams else 0.6},
    )
