# Teela embodiment

Qwen is cognition. MiniOS / WBC / Jetson remain control. This document is the architecture for a persistent body self-model: intend → imagine → validate → move → feel → compare.

Phase 1 (this change) is **body mirror only**. No new Qwen tools, no autonomous movement, no MuJoCo rollout yet.

---

## 1. Current architecture (body control)

```
Qwen3.8-Flash-Next (llama.cpp :8080)
        │  OpenAI chat + tools via deskd /v1/llm
        ▼
Hermes ACP  (hermes acp)
        │  MCP bot_desktop__robot_{status,pose,joint,motion}
        ▼
deskd MiniOS twin  (robot_sim.py + ui/robot-simulator.html)
        │  commanded joints + live 3D overlay
        ▼
motion.dispatch
   STATIC  → POST /v1/cluster/robot/execute  → teela-jetson (servos; stub until HERMES_DESK_MOTORS=1)
   DYNAMIC → POST /v1/cluster/robot/wbc      → teela-body   (WBC; stub until HERMES_DESK_WBC=1)
   SAFETY  → both (E-stop)
```

Frequencies already match the target split: Qwen is event-driven; MiniOS is the planner; Jetson would own the 50 Hz loop (never through deskd HTTP). Qwen never writes PWM / I2C.

## 2. Existing components to reuse

| Piece | Where | Role |
| --- | --- | --- |
| Canonical joints + limits | `deskd/robot_sim.py` `JOINTS` / `ALIASES` | 22 named DoF, clamp |
| Named poses | `robot_sim.POSES` | Class B skill targets |
| Commanded vs live | `state["joints"]` vs `state["live"]`, `_ingest_live` | Physical truth channel |
| Tracking delta | `tracking_snapshot` | Prediction error seed |
| Semantic I-feel | `describe_body`, `public_status.spoken` | Compact proprioception |
| Sagittal COM | `sagittal_support` | Balance sketch |
| MCP body tools | `desktop_mcp.py` robot_* | Current Qwen interface (Phase 2 will add teela_*) |
| Motion plane | `motion.py` | STATIC / DYNAMIC / SAFETY routing |
| Cluster execute/wBC | `/v1/cluster/robot/*` | Hardware intake (stub today) |
| SOUL / BODY.md | `DEFAULT_SOUL`, workspace `BODY.md` | Persistent identity + lookbook |
| MiniOS 3D twin | `ui/robot-simulator.html` | Visual mirror; posts `cmd=live` |

## 3. Missing components

- Typed **intended / expected / simulated / observed** layers (today: commanded + live only).
- Body-state encoder that is not a raw 22-joint dump for the LLM.
- Virtual twin object that **only** mirrors observed state (REAL→VIRTUAL). MiniOS 3D is visualization, not a predictive plant.
- Action gate (`teela_body_action`) with simulate → collision → limits → balance → MiniOS. Qwen currently infers pose names and calls `robot_pose`.
- World model (persistent `bottle_03`, frames, V-JEPA).
- MuJoCo plant (Phase 4).
- Confirmed-result contract: tool return is MiniOS `ok`, not encoder confirmation from Jetson.
- Hard safety independent of Qwen (Jetson stub until motors attach).

## 4. Proposed directory / module layout

```
deskd/embodiment/          # cognition-facing body service (this package)
  schema.py                # body-state + teela_body_action JSON (typed dicts)
  encoder.py               # raw joints/sensors → compact body concepts
  twin.py                  # Virtual Teela; kinematic now, MuJoCo later
  service.py               # ingest observe, snapshot, prediction error
docs/embodiment.md         # this file
tests/test_embodiment.py
```

Later phases (do not create yet): `skills.py` (look_at, reach, …), `gate.py` (Class B/C validation), `world.py`, `mujoco_plant.py`.

Transport stays HTTP on deskd for now. `EmbodimentService` has no HTTP types so it can sit behind WebSocket / ROS 2 / ZMQ later without changing Qwen's tool names.

## 5. Body-state JSON schema (Phase 1)

Four layers. **Observed is authoritative.** Simulated is a mirror of observed. Intended is last command. Expected is what we thought would happen (Phase 1: copy of intended).

```json
{
  "layers": {
    "intended":  { "source": "intended",  "joints": {}, "pose": "wave", "motion": "waving", "seq": 3, "ts": 0 },
    "expected":  { "source": "expected",  "joints": {}, "pose": "wave", "motion": "waving", "seq": 3, "ts": 0 },
    "simulated": { "source": "simulated", "joints": {}, "pose": "wave", "motion": "waving", "seq": 3, "ts": 0 },
    "observed":  { "source": "observed",  "joints": {}, "pose": "wave", "motion": "waving", "seq": 3, "ts": 0 }
  },
  "body": {
    "balance": { "stable": true, "support": "both_feet" },
    "head": { "pan_deg": 0, "tilt_deg": 0 },
    "right_arm": { "state": "wave_hold", "shoulder_pitch_deg": 24, "elbow_deg": 118 },
    "left_arm": { "state": "down", "shoulder_pitch_deg": 0, "elbow_deg": 5 },
    "right_hand": { "state": "open", "object": null },
    "left_hand": { "state": "open", "object": null },
    "contact": { "right_hand": false, "left_foot": true, "right_foot": true },
    "active_action": { "skill": null, "state": "idle" },
    "health": { "joints_ok": true, "emergency_stop": false, "motors": true }
  },
  "prediction_error": [],
  "spoken": "I'm waving with my right hand in front of my chest…",
  "rule": "REAL→VIRTUAL mirrors observed. VIRTUAL→REAL is rejected until the action gate exists."
}
```

Qwen (Phase 2) gets `body` + `spoken` + `prediction_error`, not 50 Hz encoder samples.

## 6. Proposed `teela_body_action` schema (Phase 3+)

Not executed in Phase 1. Frozen so Qwen's future tool does not change:

```json
{
  "skill": "orient_head",
  "effector": "head",
  "target": "person_01",
  "speed": "cautious",
  "constraints": { "maintain_balance": true, "avoid_collision": true }
}
```

Skills: `look_at`, `orient_head`, `orient_torso`, `reach`, `retract`, `grasp`, `release`, `lift`, `place`, `point`, `handover`, `stand`, `sit`, `turn`, `step`, `walk_to`, `stabilize`, `stop`.

Pipeline (enforced in code, not by Qwen): request → validate current → resolve target → generate skill → simulate if Class C → collision / limits / balance / safety → MiniOS / WBC → read sensors → return **confirmed** result. Rejected actions cannot be bypassed.

## 7. llama.cpp tool-calling integration (Phase 2+)

Keep current MCP names until Phase 2 so MiniOS body still works.

Phase 2 adds four tools on the deskd OpenAI shim (`ensure_motor_tools` / `desktop_mcp.py`):

| Tool | Maps to |
| --- | --- |
| `teela_get_body_state` | `EmbodimentService.snapshot` compact form |
| `teela_get_world_state` | world model (empty until Phase 6) |
| `teela_body_action` | action gate (Phase 3) |
| `teela_stop` | Class A/B stop → MiniOS `stop` + Jetson E-stop path |

Qwen still must not emit `servo_7=42`. Desk continues to rewrite empty/prose motor replies into structured tools.

## 8. Physical ↔ virtual synchronization

```
PHYSICAL (encoders / MiniOS live overlay / IMU later)
    → EmbodimentService.observe(live joints, contacts, …)
    → robot_sim._ingest_live  (does not overwrite commanded)
    → VirtualTwin.mirror_observed
```

- **REAL → VIRTUAL = actual state.** Twin joints are set only from observe.
- **VIRTUAL → REAL** is not implemented in Phase 1 (`virtual_drive` returns rejected). Later: validated action plans only, through `motion.dispatch`.
- MiniOS 3D remains the on-screen twin. VirtualTeela is the kinematic (later MuJoCo) plant used for prediction, not a second GUI.

## 9. MiniOS integration point

- Live overlay already POSTs `/v1/bots/{id}/desktop/action` `{action:robot, cmd:live|joints, live:{...}}`.
- `apply_robot` already calls `robot_sim.apply`, which `_ingest_live`s.
- Phase 1 hook: after apply, `bot.embodiment.sync(robot_state)` so GET `/v1/bots/{id}/embodiment/state` matches MiniOS.
- New observe URL is an alias for the same ingest (Jetson / WBC can POST here later without MiniOS).
- Commanded poses still go through existing `robot_pose` / `robot_joint` / `robot_motion`. No second motor path.

## 10. Safety boundary

| Layer | Owns |
| --- | --- |
| Class A reflex | Jetson motor daemon / E-stop. Independent of Qwen. Stub until `HERMES_DESK_MOTORS=1`. |
| Class B verified skill | MiniOS `robot_sim.apply` joint limits + `motion.dispatch` STATIC. |
| Class C novel | Future MuJoCo rollout + gate. Qwen cannot skip. |
| Qwen | Intent only. No shell, no I2C, no PWM, no raw servo tools. |

Qwen claiming motion without `ok` + observed confirmation remains forbidden (existing motor follow-up rules).

## 11. Phase 1 implementation plan

1. Add `deskd/embodiment/` with schema, encoder, kinematic twin, service.
2. Snapshot four layers from existing `robot_state`.
3. Sync on every MiniOS robot apply (live and commanded).
4. HTTP GET state + POST observe + GET schema.
5. Tests: live ingest mirrors virtual; commanded is not overwritten by virtual; compact encoder; prediction_error when live lags intended.
6. Success: move MiniOS twin (or POST live joints) → embodiment `observed` and `simulated` match live, `intended` stays commanded.

Not in Phase 1: `teela_body_action` execution, MuJoCo, V-JEPA, world objects, look_at, arms, walk.

## 12. Files created or modified

| File | Change |
| --- | --- |
| `docs/embodiment.md` | This architecture |
| `deskd/embodiment/__init__.py` | Package exports |
| `deskd/embodiment/schema.py` | Joints, skills, body-state + action dict builders |
| `deskd/embodiment/encoder.py` | Compact `body` + spoken from observed |
| `deskd/embodiment/twin.py` | Kinematic VirtualTeela (MuJoCo hook) |
| `deskd/embodiment/service.py` | observe / sync / snapshot / reject virtual drive |
| `deskd/deskd.py` | Bot.embodiment, sync after apply_robot, HTTP |
| `deskd/deskd.py` `DEFAULT_SOUL` | Intended vs observed; no claim without confirmation |
| `tests/test_embodiment.py` | Phase 1 tests |

---

## Later phases (not this change)

- **2** Qwen `teela_get_body_state`, compact I-feel from observed only.
- **3** First safe action: `orient_head` / `look_at` through the gate.
- **4** Short MuJoCo rollouts; expected layer from simulation.
- **5–7** Arms, objects, whole body.
- Optional later: small Body Action Model between Qwen and WBC.
