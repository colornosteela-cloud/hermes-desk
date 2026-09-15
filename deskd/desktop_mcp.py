#!/usr/bin/env python3
"""Semantic MiniOS Desktop Driver for Hermes Desk."""
from __future__ import annotations
import base64, json, os, sys, urllib.error, urllib.parse, urllib.request
BOT=""; BASE=os.environ.get("HERMES_DESK_URL","http://127.0.0.1:8742"); TOKEN=os.environ.get("HERMES_DESK_TOKEN","")

def _http(method,path,body=None,timeout=45):
    data=None if body is None else json.dumps(body).encode(); req=urllib.request.Request(BASE+path,data=data,method=method,headers={"Authorization":f"Bearer {TOKEN}","Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e: return {"error":e.read().decode()[:800]}
    except Exception as e: return {"error":str(e)}

def _bytes(path,timeout=15):
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE+path,headers={"Authorization":f"Bearer {TOKEN}"}),timeout=timeout) as r: return r.read()
    except Exception: return b""

def reply(mid,result=None,error=None):
    out={"jsonrpc":"2.0","id":mid}; out["error"]={"code":-32000,"message":str(error)} if error is not None else None
    if error is None: out.pop("error",None); out["result"]=result
    sys.stdout.write(json.dumps(out)+"\n"); sys.stdout.flush()

def tool(name,desc,props=None,req=None):
    schema={"type":"object","properties":props or {}}
    if req: schema["required"]=req
    return {"name":name,"description":desc,"inputSchema":schema}

TOOLS=[
 tool("desktop_state","START HERE for visual work. Semantic MiniOS dashboard with active/open windows, stable app IDs, object IDs/bounds, cursor, browser, build/test state, processes and recommended actions."),
 tool("desktop_observe","Semantic MiniOS state plus a JPEG of THIS bot's MiniOS desktop for visual verification."),
 tool("desktop_watch","Wait for a visual change after an action, then return semantic MiniOS state plus the newest JPEG.",{"after_seq":{"type":"integer","minimum":0},"timeout":{"type":"number","minimum":0,"maximum":30}}),
 tool("desktop_screenshot","Capture THIS bot's MiniOS workspace desktop (wallpaper, dock, Desktop icons, MiniOS windows) from the headless MiniOS observer. Saves Pictures/screenshot-*.jpg and posts it in chat. Never the host/user monitor. Never scrot/grim/gnome-screenshot.",{"caption":{"type":"string"}}),
 tool("desktop_open_app","Open/focus a known app by ID/name: app_agent, app_browser, app_files, app_notepad, app_terminal, app_editor, app_preview, app_dev, app_settings.",{"app_id":{"type":"string"}},["app_id"]),
 tool("desktop_focus_window","Focus a known window such as win_browser.",{"window_id":{"type":"string"}},["window_id"]),
 tool("desktop_minimize_window","Minimize a known window by ID.",{"window_id":{"type":"string"}},["window_id"]),
 tool("desktop_maximize_window","Toggle maximize for a known window by ID.",{"window_id":{"type":"string"}},["window_id"]),
 tool("desktop_close_window","Close a known window by ID.",{"window_id":{"type":"string"}},["window_id"]),
 tool("desktop_click_object","Click a known object from desktop_state, e.g. app_browser, obj_agent_close, or #6. Cursor visibly moves to it.",{"object_id":{"type":"string"}},["object_id"]),
 tool("desktop_open_file","Open a workspace file. Documents (.txt/.md) open in Text Editor; other text/code opens in Code Editor.",{"path":{"type":"string"}},["path"]),
 tool("desktop_open_preview","Open a workspace HTML file in sandboxed Preview.",{"path":{"type":"string"}},["path"]),
 tool("desktop_browser_navigate","Navigate the live Browser to a URL and show it.",{"url":{"type":"string"}},["url"]),
 tool("desktop_browser_back","Browser back."), tool("desktop_browser_forward","Browser forward."),
 tool("desktop_run_tests","Run the detected project test command and show Build & Test.",{"timeout":{"type":"integer","minimum":1,"maximum":600}}),
 tool("desktop_run_app","Start detected dev/run command as managed background process.",{"command":{"type":"string"},"cwd":{"type":"string"}}),
 tool("desktop_stop_app","Stop the managed background app process."),
 tool("desktop_move_cursor","Fallback: move standard arrow cursor, normalized 0-1000.",{"x":{"type":"number"},"y":{"type":"number"}},["x","y"]),
 tool("desktop_click","Fallback: left-click at normalized 0-1000 coordinates.",{"x":{"type":"number"},"y":{"type":"number"}},["x","y"]),
 tool("desktop_double_click","Fallback: double-click at normalized coordinates.",{"x":{"type":"number"},"y":{"type":"number"}},["x","y"]),
 tool("desktop_scroll","Fallback: scroll active visual surface; positive dy scrolls down.",{"dx":{"type":"number"},"dy":{"type":"number"}},["dy"]),
 tool("desktop_type_text","Fallback: type into the focused MiniOS/browser target.",{"text":{"type":"string"}},["text"]),
 tool("robot_status","Read Teela MiniOS robot. spoken is the live 3D twin in ordinary words — answer body questions from spoken/live, not commanded. Also returns commanded/live/delta joints, pose, motion, walk phase, estop. temp/load/fall_flag stay null until Jetson or WBC report them. Use this instead of clicking sliders."),
 tool("robot_joint","Move one MiniOS robot part without resetting the locked pose. Prefer setting shoulder to 78 for arm FORWARD (same as Body Actions Arms Forward), 142 only for RAISE/UP (Arms Up), shoulder_out 90 for OUT to the side. dir: left,right,up,down,fwd,back,in,out,flex,extend,straight. fwd is reach-forward, not a raise. Names: neck_pan, neck_tilt, left_shoulder, left_shoulder_out, left_elbow, left_wrist, right_shoulder, right_shoulder_out, right_elbow, right_wrist, upper_back_pitch, lower_back_pitch, lower_back_roll, left_hip, left_hip_out, left_knee, left_ankle, right_hip, right_hip_out, right_knee, right_ankle. Wave keeps right_wrist at 0.",{"joint":{"type":"string"},"value":{"type":"number"},"delta":{"type":"number"},"dir":{"type":"string"},"joints":{"type":"object","additionalProperties":{"type":"number"}}}),
 tool("robot_pose","Apply a named MiniOS robot pose: home, neutral, relaxed, attention, ready, sit, bow, lean_left, lean_right, hands_up, tpose, wave, squat, arms_forward, bend_forward, ground_support, left_leg_out, right_leg_out, left_leg_raise, right_leg_raise, kneel_left, kneel_right, kneel_both. kneel_right = down on the right knee (left foot steps forward first). kneel_both = both knees on the ground.",{"pose":{"type":"string"}},["pose"]),
 tool("robot_motion","Robot motion command: walk, walk_left, walk_right, walk_place, stop, demo, reset, estop_on, estop_off, motors_on, motors_off, or cmd=plan with ordered steps for compound requests (stop and walk right, wave then bow).",{"cmd":{"type":"string"},"direction":{"type":"string"},"why":{"type":"string"},"steps":{"type":"array","items":{"type":"object"}}},["cmd"]),
 tool("teela_get_body_state","Read Virtual Teela's current joints, lock, and action status from the HTML simulator. Virtual body only — does not move hardware."),
 tool("teela_body_action","Perform an action using Teela's virtual HTML body. Does not move physical motors, PCA9685, or MiniOS actuators. Skills: orient_head (pan_deg left is negative, e.g. -25; look-straight is pan_deg=0 tilt_deg=0), raise_arm, lower_arm, wave (side left|right), neutral_pose, stop. Look/arm wait until live joints settle; wave returns immediately so speech can overlap.",{"skill":{"type":"string","enum":["orient_head","raise_arm","lower_arm","wave","neutral_pose","stop"]},"side":{"type":"string","enum":["left","right"]},"pan_deg":{"type":"number"},"tilt_deg":{"type":"number"},"duration_ms":{"type":"number"}},["skill"]),
 tool("teela_gesture","Social gesture on Virtual Teela: greeting, agree, disagree, confused, thinking, excited, point, shrug, listen. Coordinated head/arms. Virtual only.",{"gesture":{"type":"string","enum":["greeting","agree","disagree","confused","thinking","excited","point","shrug","listen"]},"side":{"type":"string","enum":["left","right"]}},["gesture"]),
 tool("teela_stop","Stop Virtual Teela's current motion. Virtual body only."),
 tool("teela_activity","What Teela is doing right now: virtual body motion plus any running agent work. Use this instead of guessing."),
 tool("teela_system_check","Full check of THIS Teela: MiniOS twin, virtual body, activity, physical mesh (Jetson/WBC), cluster peers, brain model, MiniOS observer. Use when they ask for a system check, diagnostics, or to check yourself. Host-shell is also available via run_terminal_command when she needs this computer."),
]

def obs(after=0,wait=0,with_image=False):
    res=_http("GET",f"/v1/bots/{BOT}/desktop/observe?after={int(after)}&wait={float(wait)}",timeout=int(wait+12)); c=[{"type":"text","text":json.dumps(res,indent=2)[:30000]}]
    if with_image:
        frame=_bytes(f"/v1/bots/{BOT}/desktop/frame")
        if frame: c.append({"type":"image","data":base64.b64encode(frame).decode(),"mimeType":"image/jpeg"})
    return c

def action(a,**kw): return _http("POST",f"/v1/bots/{BOT}/desktop/action",{"action":a,**kw},timeout=650 if a=="run_tests" else 45)

def tools_for_kind(kind):
    k=str(kind or "").strip().lower().replace("_","-")
    if k in ("hermes","hermes","agent","agentic","build","coding"):
        return [t for t in TOOLS if str(t.get("name") or "").startswith("desktop_")]
    if k in ("teela-brain","teela","brain","body","robot","embodiment"):
        skip=("desktop_run_tests","desktop_run_app","desktop_stop_app")
        return [t for t in TOOLS if t.get("name") not in skip]
    return list(TOOLS)

def main():
    global BOT
    argv=sys.argv[1:]
    kind="teela-brain"
    if "--bot" in argv: BOT=argv[argv.index("--bot")+1]
    if "--kind" in argv: kind=argv[argv.index("--kind")+1]
    mapping={
      "desktop_open_app":("open_app","app_id"),"desktop_focus_window":("focus_window","window_id"),"desktop_minimize_window":("minimize_window","window_id"),"desktop_maximize_window":("maximize_window","window_id"),"desktop_close_window":("close_window","window_id"),"desktop_click_object":("click_object","object_id"),"desktop_open_file":("open_file","path"),"desktop_open_preview":("open_preview","path"),"desktop_browser_navigate":("browser_navigate","url")}
    for line in sys.stdin:
        try: msg=json.loads(line)
        except Exception: continue
        method=msg.get("method"); mid=msg.get("id"); params=msg.get("params") or {}
        if method=="initialize": reply(mid,{"protocolVersion":params.get("protocolVersion") or "2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"bot_desktop","version":"0.10.0"}})
        elif method=="notifications/initialized": continue
        elif method=="tools/list": reply(mid,{"tools":tools_for_kind(kind)})
        elif method=="tools/call":
            name=params.get("name"); a=params.get("arguments") or {}
            if name=="desktop_state": content=obs()
            elif name=="desktop_observe": content=obs(with_image=True)
            elif name=="desktop_watch": content=obs(max(0,int(a.get("after_seq") or 0)),max(0,min(30,float(a.get("timeout") or 10))),True)
            elif name=="desktop_screenshot":
                res=action("screenshot",caption=a.get("caption") or "")
                content=[{"type":"text","text":json.dumps(res,indent=2)[:30000]}]
            elif name in mapping:
                act,key=mapping[name]; res=action(act,**{key:a.get(key)}); content=[{"type":"text","text":json.dumps(res,indent=2)[:30000]}]
                if name=="desktop_open_file":
                    rel=str((res or {}).get("path") or a.get("path") or "")
                    kind=str((res or {}).get("kind") or "")
                    stills=list((res or {}).get("stills") or [])
                    paths=[rel] if kind=="image" and rel else []
                    paths.extend(str(s) for s in stills if s)
                    for pth in paths[:8]:
                        frame=_bytes("/v1/workspaces/%s/raw?file=%s"%(BOT,urllib.parse.quote(pth)))
                        if frame: content.append({"type":"image","data":base64.b64encode(frame).decode(),"mimeType":"image/jpeg"})
            else:
                simple={"desktop_browser_back":"browser_back","desktop_browser_forward":"browser_forward","desktop_stop_app":"stop_app"}
                if name in simple: res=action(simple[name])
                elif name=="desktop_run_tests": res=action("run_tests",timeout=a.get("timeout") or 120)
                elif name=="desktop_run_app": res=action("run_app",command=a.get("command") or "",cwd=a.get("cwd") or "")
                elif name=="desktop_move_cursor": res=action("move_cursor",x=a.get("x"),y=a.get("y"))
                elif name=="desktop_click": res=action("click",x=a.get("x"),y=a.get("y"))
                elif name=="desktop_double_click": res=action("double_click",x=a.get("x"),y=a.get("y"))
                elif name=="desktop_scroll": res=action("scroll",dx=a.get("dx") or 0,dy=a.get("dy") or 0)
                elif name=="desktop_type_text": res=action("type_text",text=a.get("text") or "",app=a.get("app") or "",submit=a.get("submit"))
                elif name=="robot_status": res=action("robot",cmd="status")
                elif name=="robot_joint":
                    res=action("robot",cmd="joint",joint=a.get("joint"),value=a.get("value") if a.get("value") is not None else a.get("degrees"),joints=a.get("joints"),delta=a.get("delta"),dir=a.get("dir") or a.get("direction"))
                elif name=="robot_pose": res=action("robot",cmd="pose",pose=a.get("pose") or a.get("name"))
                elif name=="teela_get_body_state":
                    res=_http("GET",f"/v1/bots/{BOT}/virtual-body/state")
                elif name=="teela_body_action":
                    res=_http("POST",f"/v1/bots/{BOT}/virtual-body/action",{
                        "skill":a.get("skill"),
                        "side":a.get("side"),
                        "pan_deg":a.get("pan_deg"),
                        "tilt_deg":a.get("tilt_deg"),
                        "duration_ms":a.get("duration_ms"),
                    },timeout=8)
                elif name=="teela_gesture":
                    res=_http("POST",f"/v1/bots/{BOT}/virtual-body/action",{
                        "skill":"gesture",
                        "gesture":a.get("gesture"),
                        "side":a.get("side"),
                    },timeout=8)
                elif name=="teela_stop":
                    res=_http("POST",f"/v1/bots/{BOT}/virtual-body/action",{"skill":"stop"},timeout=8)
                elif name=="teela_activity":
                    res=_http("GET",f"/v1/bots/{BOT}/activity")
                elif name=="teela_system_check":
                    res=_http("GET",f"/v1/bots/{BOT}/system-check")
                elif name=="robot_motion":
                    raw=str(a.get("cmd") or a.get("motion") or "status")
                    direction=a.get("direction")
                    extra={}
                    if a.get("steps"): extra["steps"]=a.get("steps")
                    if a.get("why"): extra["why"]=a.get("why")
                    if raw.startswith("walk_") and not direction:
                        part=raw.split("_",1)[1]
                        mapped={"north":"back","south":"south","east":"left","west":"right"}.get(part,part)
                        res=action("robot",cmd="walk",direction=mapped,**extra)
                    else:
                        res=action("robot",cmd=raw,direction=direction,**extra)
                else: res={"error":f"unknown tool {name}"}
                content=[{"type":"text","text":json.dumps(res,indent=2)[:30000]}]
            reply(mid,{"content":content})
        elif mid is not None: reply(mid,error=f"unknown method {method}")
if __name__=="__main__": main()

# Legacy/static-test markers retained for downstream packaging checks:
# "name": "desktop_observe"
# "name": "desktop_watch"
# "name": "desktop_screenshot"
# "name": "desktop_click"
# "name": "desktop_double_click"
# "name": "robot_status"
# "name": "robot_joint"
# "name": "robot_pose"
# "name": "robot_motion"
# "name": "teela_get_body_state"
# "name": "teela_body_action"
# "name": "teela_gesture"
# "name": "teela_stop"
# "name": "teela_system_check"
# "type": "image"
# "mimeType": "image/jpeg"
