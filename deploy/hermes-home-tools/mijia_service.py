"""米家常驻服务（127.0.0.1:9393），供 Hermes 的 home_tools MCP 调用。

- 认证、设备目录、场景、MIoT 规格常驻缓存；不写死设备，名字按米家云真实目录匹配。
- 家规（禁用设备 / 场景、默认播报音箱）读 Hermes 侧 house-policy.json，与 mijiaAPI CLI 共用。
- 写入后按需读回，结果区分 done（云端确认）/ verified（读回一致）/ accepted（只确认收到）。
- 不做任何定时或自动关闭；定时交给 Hermes cron。
"""
import difflib
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from mijiaAPI import mijiaAPI
from mijiaAPI.devices import get_device_info

HOST, PORT = "127.0.0.1", int(os.environ.get("MIJIA_PORT", "9393"))
HERE = Path(__file__).resolve().parent
AUTH_PATH = Path(os.environ.get("MIJIA_AUTH", "/home/ubuntu/hermes/data/.config/mijia-api/auth.json"))
POLICY_PATH = Path(os.environ.get("MIJIA_POLICY", "/home/ubuntu/hermes/data/tools/mijia/house-policy.json"))
SNAPSHOT_PATH = Path(os.environ.get("MIJIA_SNAPSHOT", HERE / "catalog.json"))
REFRESH_ON_MISS_S = 30          # 名字匹配不到时，目录比这更旧就自动刷新一次
READBACK_DELAYS = (0.6, 1.2)    # 网关只回"已接收"时的读回间隔

logging.basicConfig(filename=os.environ.get("MIJIA_LOG", HERE / "service.log"), level=logging.INFO,
                    format="%(asctime)s %(message)s")
log = logging.getLogger("mj")
lock = threading.RLock()


# ---------------------------------------------------------------- 认证与云端调用

class Client(mijiaAPI):
    """不在每个请求前联网检查 token；失败时再刷新。家庭 owner 用缓存，省一次请求。"""

    def _refresh_token(self):
        return self.auth_data

    def _get_home_owner(self, home_id):
        for h in S.homes:
            if str(h["id"]) == str(home_id):
                return int(h["uid"])
        return mijiaAPI._get_home_owner(self, home_id)

    def force_refresh(self):
        self._available_cache = None
        self._available_cache_time = 0
        mijiaAPI._refresh_token(self)
        self.auth_data["expireTime"] = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
        self._save_auth_data()


class State:
    api: Client = None
    auth_mtime = 0.0
    homes: list = []
    devices: dict = {}          # did -> 设备摘要
    scenes: list = []
    updated_at = 0.0
    specs: dict = {}            # spec_type -> 规格
    started = None


S = State()


def load_api():
    S.api = Client(auth_data_path=str(AUTH_PATH))
    S.auth_mtime = AUTH_PATH.stat().st_mtime


def cloud(fn):
    """云端调用；非网络错误时先看 CLI 是否已刷新过认证文件，再自己刷新，各重试一次。"""
    try:
        return fn()
    except requests.RequestException:
        raise
    except Exception as e:
        log.warning("cloud call failed (%s: %s)", type(e).__name__, e)
        if AUTH_PATH.stat().st_mtime != S.auth_mtime:
            load_api()
            try:
                return fn()
            except requests.RequestException:
                raise
            except Exception as e2:
                log.warning("retry with reloaded auth failed (%s)", e2)
        S.api.force_refresh()
        S.auth_mtime = AUTH_PATH.stat().st_mtime
        return fn()


def refresher():
    while True:
        time.sleep(12 * 3600)
        try:
            with lock:
                left = (S.api.auth_data.get("expireTime", 0) / 1000 - time.time()) / 86400
                if left < 3:
                    S.api.force_refresh()
                    S.auth_mtime = AUTH_PATH.stat().st_mtime
                    log.info("proactive token refresh ok (%.1f days left before)", left)
                else:
                    log.info("token check: %.1f days left", left)
        except Exception as e:
            log.error("proactive refresh failed: %s", e)


# ---------------------------------------------------------------- 家规

_policy = {"mtime": None, "data": {}}


def policy() -> dict:
    try:
        m = POLICY_PATH.stat().st_mtime
        if m != _policy["mtime"]:
            _policy["data"] = json.loads(POLICY_PATH.read_text())
            _policy["mtime"] = m
    except Exception as e:
        log.error("policy load failed: %s", e)   # 读不到时沿用上一份
    return _policy["data"]


def blocked_dids() -> set:
    return {str(x["did"]) for x in policy().get("blocked_devices", [])}


def blocked_names() -> set:
    return {n for x in policy().get("blocked_devices", []) for n in x.get("names", [])}


def block_reason(did=None, text=None) -> str:
    """家规里禁止控制的设备：按 did 或用户说法里的名字命中时，返回家规写的原因。"""
    for x in policy().get("blocked_devices", []):
        if (did and str(x["did"]) == str(did)) or (text and any(n in text for n in x.get("names", []))):
            return x.get("reason") or "该设备已被用户禁止控制"
    return ""


def scene_block_reason(scene) -> str:
    if scene["name"] in set(policy().get("blocked_scenes", [])):
        return "该场景已被用户禁止运行"
    hit = set(scene["dids"]) & blocked_dids()
    if hit:
        names = "、".join(S.devices[d]["name"] if d in S.devices else d for d in hit)
        return f"该场景会操作被禁止控制的设备：{names}"
    return ""


class Refused(Exception):
    pass


class NotFound(Exception):
    def __init__(self, msg, candidates=None):
        super().__init__(msg)
        self.candidates = candidates or []


# ---------------------------------------------------------------- 目录与规格

def _kind(spec_type):
    m = re.match(r"urn:miot-spec-v2:device:([^:]+)", spec_type or "")
    return m.group(1) if m else "ir-remote"


def refresh_catalog() -> dict:
    """从米家云重新读取家庭、设备、场景；失败时抛出，旧目录保持不变。"""
    homes = cloud(lambda: S.api.get_homes_list())
    allowed_homes = set(policy().get("homes") or [])
    if allowed_homes:
        homes = [h for h in homes if h["name"] in allowed_homes or str(h["id"]) in allowed_homes]
    S.homes = homes
    home_ids = {str(h["id"]) for h in homes}
    loc = {}
    for h in homes:
        for r in h.get("roomlist", []) or []:
            for did in r.get("dids", []) or []:
                loc[did] = (h["name"], r["name"])
    raw = cloud(lambda: S.api.get_devices_list()) + cloud(lambda: S.api.get_shared_devices_list())
    devices = {}
    for d in raw:
        if allowed_homes and str(d.get("home_id")) not in home_ids:
            continue
        home, room = loc.get(d["did"], (None, None))
        devices[d["did"]] = {"did": d["did"], "name": d["name"], "model": d["model"],
                             "spec_type": d.get("spec_type"), "online": bool(d.get("isOnline")),
                             "home": home, "room": room, "home_id": str(d.get("home_id", ""))}
    scenes = []
    for s in cloud(lambda: S.api.get_scenes_list()):
        if allowed_homes and str(s.get("home_id")) not in home_ids:
            continue
        dids = []
        for a in (s.get("scene_action") or {}).get("actions", []) or []:
            did = str((a.get("payload_json") or {}).get("did") or "")
            if did and did not in dids:
                dids.append(did)
        scenes.append({"id": str(s["scene_id"]), "name": s["name"], "home_id": str(s["home_id"]), "dids": dids})

    old = {d: {"name": v["name"], "online": v["online"]} for d, v in S.devices.items()}
    if not old and SNAPSHOT_PATH.exists():
        try:
            old = json.loads(SNAPSHOT_PATH.read_text()).get("devices", {})
        except Exception:
            old = {}
    S.devices, S.scenes, S.updated_at = devices, scenes, time.time()
    for v in devices.values():
        try:
            spec(v)
        except Exception as e:
            log.warning("spec load failed for %s: %s", v["name"], e)

    diff = {"added": [v["name"] for d, v in devices.items() if d not in old],
            "removed": [o["name"] for d, o in old.items() if d not in devices],
            "renamed": [f"{old[d]['name']} → {v['name']}" for d, v in devices.items()
                        if d in old and old[d]["name"] != v["name"]],
            "went_offline": [v["name"] for d, v in devices.items() if d in old and old[d]["online"] and not v["online"]],
            "came_online": [v["name"] for d, v in devices.items() if d in old and not old[d]["online"] and v["online"]]}
    snap = {"updated_at": S.updated_at,
            "devices": {d: {"name": v["name"], "online": v["online"]} for d, v in devices.items()}}
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    tmp.replace(SNAPSHOT_PATH)
    log.info("catalog refreshed: %d devices, %d scenes, diff=%s", len(devices), len(scenes),
             json.dumps({k: v for k, v in diff.items() if v}, ensure_ascii=False))
    return diff


def spec(dev):
    """设备规格：{props: {name: prop}, actions: {name: action}}；红外等无规格设备返回 None。"""
    st = dev.get("spec_type")
    if not st:
        return None
    if st not in S.specs:
        info = get_device_info(st, cache_path=AUTH_PATH.parent)
        S.specs[st] = {"props": {p["name"]: p for p in info.get("properties", [])},
                       "actions": {a["name"]: a for a in info.get("actions", [])}}
    return S.specs[st]


def main_props(sp):
    """主服务（最小 siid）的可读属性，用作默认读数和目录摘要。"""
    readable = [p for p in sp["props"].values() if "r" in p["rw"]]
    if not readable:
        return []
    sid = min(p["method"]["siid"] for p in readable)
    return [p for p in readable if p["method"]["siid"] == sid]


def find_prop(sp, name, need):
    """按名字找属性；`on` 这类名字在只有 on-2 / on-13 时取 siid 最小的那个。"""
    props = sp["props"]
    key = str(name).strip().replace("_", "-")
    p = props.get(key)
    if p is None:
        variants = [q for n, q in props.items() if re.fullmatch(re.escape(key) + r"-\d+", n)]
        if variants:
            p = min(variants, key=lambda q: (q["method"]["siid"], q["method"]["piid"]))
    if p is None or need not in p["rw"]:
        usable = [describe_prop(q) for q in props.values() if need in q["rw"]]
        what = "可读" if need == "r" else "可写"
        raise NotFound(f"该设备没有{what}属性 {name}", usable)
    return p


def describe_prop(p):
    s = f"{p['name']}({p['type']},{p['rw']}"
    if p.get("value-list"):
        s += "," + "/".join(f"{v['value']}={v['description']}" for v in p["value-list"])
    elif p.get("range"):
        s += f",{p['range'][0]}~{p['range'][1]}"
    return s + ")"


def coerce(p, value):
    """按规格校验并转换取值；枚举也接受描述文字。"""
    t = p["type"]
    if p.get("value-list"):
        for v in p["value-list"]:
            if str(value).strip().lower() == str(v["description"]).lower():
                return v["value"]
    if t == "bool":
        s = str(value).strip().lower()
        if s in ("true", "1", "on", "开", "打开", "yes"):
            return True
        if s in ("false", "0", "off", "关", "关闭", "no"):
            return False
        raise ValueError(f"无效布尔值：{value}")
    if t in ("int", "uint"):
        v = int(float(value))
    elif t == "float":
        v = float(value)
    else:
        return str(value)
    rng = p.get("range")
    if rng and not (rng[0] <= v <= rng[1]):
        raise ValueError(f"{p['name']} 取值 {v} 超出范围 {rng[0]}~{rng[1]}")
    if p.get("value-list") and v not in [x["value"] for x in p["value-list"]]:
        raise ValueError(f"{p['name']} 取值无效，可选：" + "、".join(f"{x['value']}={x['description']}" for x in p["value-list"]))
    return v


def label(p, value):
    for v in p.get("value-list") or []:
        if v["value"] == value:
            return f"{value}（{v['description']}）"
    return value


# ---------------------------------------------------------------- 名字匹配

def norm(s):
    return re.sub(r"[\s\-_·,，.。、:：!！?？'\"“”()（）]|的", "", str(s)).lower()


def resolve(query, allow_refresh=True):
    q = str(query or "").strip()
    if not q:
        raise NotFound("没有给出设备名")
    reason = block_reason(text=q)
    if reason:
        raise Refused(reason)
    dev = _match(q)
    if dev is None and allow_refresh and time.time() - S.updated_at > REFRESH_ON_MISS_S:
        try:
            refresh_catalog()
        except Exception as e:
            log.warning("refresh on miss failed: %s", e)
        dev = _match(q)
    fresh = "目录刚刷新过" if time.time() - S.updated_at <= REFRESH_ON_MISS_S else f"目录更新于 {_ts(S.updated_at)}"
    if dev is None:
        names = [v["name"] for v in S.devices.values() if v["did"] not in blocked_dids()]
        close = difflib.get_close_matches(q, names, n=3, cutoff=0.3)
        raise NotFound(f"米家里没有叫“{q}”的设备（{fresh}）。候选只是名字相近或全部设备，"
                       "不能直接替用户换成候选去操作；先查记忆里的叫法，仍不确定就问用户", close or names)
    if dev["did"] in blocked_dids():
        raise Refused(block_reason(did=dev["did"]))
    return dev


def _match(q):
    if q in S.devices:
        return S.devices[q]
    nq = norm(q)
    devs = [v for v in S.devices.values() if v["did"] not in blocked_dids()]
    for pick in (lambda v: norm(v["name"]) == nq,
                 lambda v: v["room"] and norm(v["room"] + v["name"]) == nq,
                 lambda v: nq in norm(v["name"]) or (len(norm(v["name"])) >= 2 and norm(v["name"]) in nq)):
        hits = [v for v in devs if pick(v)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise NotFound(f"“{q}”对应多台设备，请说清是哪一台", [h["name"] + (f"（{h['room']}）" if h["room"] else "") for h in hits])
    return None


def in_room(dev, room):
    """按房间过滤：房间名或设备名包含用户说的房间（如“卧室”匹配房间为“出租屋”的“卧室灯”）。"""
    r = norm(room)
    return r in norm(dev["room"] or "") or r in norm(dev["name"])


def related_scenes(did):
    return [s["name"] for s in S.scenes if did in s["dids"] and not scene_block_reason(s)]


def need_spec(dev):
    sp = spec(dev)
    if sp is None:
        sc = related_scenes(dev["did"])
        hint = f"，请改用场景：{'、'.join(sc)}" if sc else "，没有找到操作它的场景"
        raise Refused(f"{dev['name']}是红外遥控设备，没有可读写的属性{hint}")
    return sp


# ---------------------------------------------------------------- 业务操作

def op_devices(b):
    if b.get("refresh") or not S.devices:
        refresh_catalog()
    room = b.get("room")
    out = []
    for v in S.devices.values():
        if room and not in_room(v, room):
            continue
        item = {"name": v["name"], "room": v["room"], "online": v["online"], "kind": _kind(v["spec_type"])}
        if v["did"] in blocked_dids():
            item["blocked"] = True
        else:
            try:
                sp = spec(v)
            except Exception:
                sp = None
            if sp:
                item["main"] = [p["name"] for p in main_props(sp)]
            else:
                item["scenes"] = related_scenes(v["did"])
        out.append(item)
    scenes = [{"name": s["name"], **({"blocked": True} if scene_block_reason(s) else {})} for s in S.scenes]
    return {"ok": True, "updated_at": _ts(S.updated_at), "devices": out, "scenes": scenes}


def op_refresh(b):
    diff = refresh_catalog()
    return {"ok": True, "updated_at": _ts(S.updated_at), "device_count": len(S.devices),
            "changes": {k: v for k, v in diff.items() if v} or "没有变化",
            "devices": [v["name"] for v in S.devices.values()]}


def op_get(b):
    names = b.get("devices") or ([b["device"]] if b.get("device") else [])
    room = b.get("room")
    results, targets = [], []
    if names:
        for n in names:
            try:
                targets.append(resolve(n))
            except (NotFound, Refused) as e:
                results.append(_err(n, e))
    else:
        # 全屋 / 按房间查询：红外等没有规格的设备没有状态可读，列出来并给出操作它的场景
        devs = [v for v in S.devices.values() if v["did"] not in blocked_dids() and (not room or in_room(v, room))]
        if not devs:
            rooms = sorted({v["room"] for v in S.devices.values() if v["room"] and v["did"] not in blocked_dids()})
            return {"ok": False, "results": [], "rooms": rooms,
                    "error": f"没有房间名或设备名包含“{room}”的可查询设备" if room else "目录里没有可查询的设备"}
        for v in devs:
            if v.get("spec_type"):
                targets.append(v)
            else:
                sc = related_scenes(v["did"])
                results.append({"device": v["name"], "room": v["room"], "kind": "ir-remote",
                                "note": "红外遥控设备，没有可读的状态" + (f"；控制用场景：{'、'.join(sc)}" if sc else "；没有操作它的场景")})
    plan, params = [], []
    for dev in targets:
        try:
            sp = need_spec(dev)
        except Refused as e:
            results.append(_err(dev["name"], e))
            continue
        if b.get("props"):
            props, missing = [], {}
            for pn in b["props"]:
                try:
                    props.append(find_prop(sp, pn, "r"))
                except NotFound as e:
                    missing[pn] = str(e)
            if missing and not props:
                results.append({"device": dev["name"], "ok": False, "error": "；".join(missing.values()),
                                "available": [describe_prop(q) for q in sp["props"].values() if "r" in q["rw"]]})
                continue
        else:
            props, missing = main_props(sp), {}
        plan.append((dev, props, missing))
        params += [{"did": dev["did"], "siid": p["method"]["siid"], "piid": p["method"]["piid"]} for p in props]
    got = {}
    if params:
        ret = cloud(lambda: S.api.get_devices_prop(params))
        for r in ret if isinstance(ret, list) else [ret]:
            got[(str(r.get("did")), r.get("siid"), r.get("piid"))] = r
    for dev, props, missing in plan:
        # 多台设备共用一组属性名时，某台没有其中某个属性是正常的，不当作错误
        values, errors, times = {}, (dict(missing) if len(plan) == 1 else {}), []
        for p in props:
            r = got.get((dev["did"], p["method"]["siid"], p["method"]["piid"]), {})
            if r.get("code") == 0 and "value" in r:
                values[p["name"]] = label(p, r["value"])
                if r.get("updateTime"):
                    times.append(r["updateTime"])
            else:
                errors[p["name"]] = f"读取失败（code {r.get('code')}）"
        item = {"device": dev["name"], "room": dev["room"], "ok": bool(values), "values": values}
        if times:
            item["sampled_at"] = _ts(max(times))
        if errors:
            item["errors"] = errors
        if not dev["online"]:
            item["note"] = "目录显示该设备离线，读数可能是旧值"
        results.append(item)
    return {"ok": any(r.get("ok") or r.get("kind") == "ir-remote" for r in results), "results": results}


def op_set(b):
    dev = resolve(b.get("device"))
    sp = need_spec(dev)
    p = find_prop(sp, b.get("prop"), "w")
    value = coerce(p, b.get("value"))
    q = {"did": dev["did"], "siid": p["method"]["siid"], "piid": p["method"]["piid"]}
    base = {"device": dev["name"], "prop": p["name"], "value": value}
    if b.get("dry_run"):
        return {"ok": True, "status": "dry_run", **base}
    r = cloud(lambda: S.api.set_devices_prop({**q, "value": value}))
    code = r.get("code")
    if code not in (0, 1):
        return {"ok": False, "status": "failed", **base, "error": f"米家返回错误 code {code}：{r.get('message', '')}",
                **({"note": "目录显示该设备离线"} if not dev["online"] else {})}
    if code == 0:
        return {"ok": True, "status": "done", **base, "message": "设备已确认执行"}
    if "r" not in p["rw"]:
        return {"ok": True, "status": "accepted", **base, "message": "网关已接收指令，该属性不可读，无法核实"}
    now = None
    for delay in READBACK_DELAYS:
        time.sleep(delay)
        rr = cloud(lambda: S.api.get_devices_prop(q))
        now = rr.get("value")
        if rr.get("code") == 0 and now == value:
            return {"ok": True, "status": "verified", **base, "message": "已读回确认"}
    return {"ok": True, "status": "accepted", **base, "current": now,
            "message": "网关已接收指令，但读回值还没变，可能尚未生效"}


def op_action(b):
    dev = resolve(b.get("device"))
    sp = need_spec(dev)
    name = str(b.get("action") or "").strip().replace("_", "-")
    a = sp["actions"].get(name)
    if a is None:
        raise NotFound(f"{dev['name']}没有动作 {b.get('action')}", list(sp["actions"]))
    params = b.get("params")
    if params is not None and not isinstance(params, list):
        params = [params]
    return _run_action(dev, a, params, b.get("dry_run"))


def _run_action(dev, a, params, dry_run):
    base = {"device": dev["name"], "action": a["name"]}
    if dry_run:
        return {"ok": True, "status": "dry_run", **base, "params": params}
    q = {"did": dev["did"], **a["method"]}
    if params:
        q["in"] = params
    r = cloud(lambda: S.api.run_action(q))
    code = r.get("code")
    if code == 0:
        return {"ok": True, "status": "done", **base, "message": "设备已确认执行"}
    if code == 1:
        return {"ok": True, "status": "accepted", **base, "message": "网关已接收指令，无法确认执行结果"}
    return {"ok": False, "status": "failed", **base, "error": f"米家返回错误 code {code}：{r.get('message', '')}"}


def op_scene(b):
    key = str(b.get("name") or "").strip()
    sc = _scene(key)
    if sc is None and time.time() - S.updated_at > REFRESH_ON_MISS_S:
        refresh_catalog()
        sc = _scene(key)
    if sc is None:
        raise NotFound(f"没有叫“{key}”的场景", [s["name"] for s in S.scenes if not scene_block_reason(s)])
    reason = scene_block_reason(sc)
    if reason:
        raise Refused(reason)
    affects = [S.devices[d]["name"] for d in sc["dids"] if d in S.devices]
    if b.get("dry_run"):
        return {"ok": True, "status": "dry_run", "scene": sc["name"], "affects": affects}
    ok = cloud(lambda: S.api.run_scene(sc["id"], sc["home_id"]))
    return {"ok": bool(ok), "status": "accepted" if ok else "failed", "scene": sc["name"], "affects": affects,
            "message": "米家已接受场景，设备状态未逐个核实" if ok else "米家返回失败"}


def _scene(key):
    if key in {s["id"] for s in S.scenes}:
        return next(s for s in S.scenes if s["id"] == key)
    nk = norm(key)
    for pick in (lambda s: norm(s["name"]) == nk, lambda s: nk and nk in norm(s["name"])):
        hits = [s for s in S.scenes if pick(s)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise NotFound(f"“{key}”对应多个场景", [h["name"] for h in hits])
    return None


def op_speak(b):
    text = str(b.get("text") or "").strip()
    if not text:
        raise NotFound("没有要播报的文字")
    if b.get("speaker"):
        dev = resolve(b["speaker"])
    else:
        sp_cfg = policy().get("default_speaker") or {}
        dev = S.devices.get(str(sp_cfg.get("did", "")))
        if dev is None:
            speakers = [v for v in S.devices.values() if "wifispeaker" in v["model"]]
            if len(speakers) != 1:
                raise NotFound("没有配置默认播报音箱，请指定音箱", [v["name"] for v in speakers])
            dev = speakers[0]
    sp = need_spec(dev)
    a = sp["actions"].get("play-text")
    if a is None:
        raise Refused(f"{dev['name']}不支持朗读文字")
    r = _run_action(dev, a, [text], b.get("dry_run"))
    if r.get("status") in ("done", "accepted"):
        r["message"] = "音箱已接收播报指令，是否已播完无法确认"
    return {**r, "text": text}


def _err(name, e):
    out = {"device": name, "ok": False, "error": str(e)}
    if isinstance(e, Refused):
        out["refused"] = True
    if getattr(e, "candidates", None):
        out["candidates"] = e.candidates
    return out


def _ts(t):
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else None


ROUTES = {"/devices": op_devices, "/refresh": op_refresh, "/get": op_get, "/set": op_set,
          "/action": op_action, "/scene": op_scene, "/speak": op_speak}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj, t0):
        ms = round((time.perf_counter() - t0) * 1000, 1)
        log.info("%s %s %d %.0fms %s", self.command, self.path, code, ms,
                 json.dumps(obj, ensure_ascii=False, default=str)[:300])
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        t0 = time.perf_counter()
        if self.path == "/health":
            left = (S.api.auth_data.get("expireTime", 0) / 1000 - time.time()) / 86400
            return self._send(200, {"ok": bool(S.devices), "devices": len(S.devices), "scenes": len(S.scenes),
                                    "catalog_updated_at": _ts(S.updated_at), "started": S.started,
                                    "token_days_left": round(left, 1)}, t0)
        self._send(404, {"ok": False, "error": "not found"}, t0)

    def do_POST(self):
        t0 = time.perf_counter()
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            return self._send(404, {"ok": False, "error": "not found"}, t0)
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"}, t0)
        try:
            with lock:
                return self._send(200, fn(b), t0)
        except Refused as e:
            return self._send(200, {"ok": False, "refused": True, "error": str(e)}, t0)
        except NotFound as e:
            return self._send(200, {"ok": False, "error": str(e), "candidates": e.candidates}, t0)
        except ValueError as e:
            return self._send(200, {"ok": False, "error": str(e)}, t0)
        except requests.RequestException as e:
            return self._send(200, {"ok": False, "error": f"连不上米家云：{type(e).__name__}"}, t0)
        except Exception as e:
            log.error(traceback.format_exc())
            return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"}, t0)


if __name__ == "__main__":
    t0 = time.perf_counter()
    load_api()
    for i in range(3):
        try:
            refresh_catalog()
            break
        except Exception as e:
            log.error("startup catalog refresh failed (%d): %s", i + 1, e)
            time.sleep(5)
    S.started = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info("startup %.0fms, %d devices, %d scenes", (time.perf_counter() - t0) * 1000, len(S.devices), len(S.scenes))
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
