#!/usr/bin/env python3
"""v1.6.21 真栈 E2E 驱动器——本地与 CI 单一事实源（第七轮评分补债）。

契约来源（2026-07 读 HA core 2026.7.1 源码实证，勿再试错猜 API）：
- POST /api/onboarding/users 现版本 required: name/username/password/
  client_id(str)/language；client_id 是任意字符串（create_auth_code 只写
  内存 store，不校验注册）；响应 {"auth_code": ...}，不再直发长期令牌
  （两轮 CI 盲打实锤 422 required key 后放弃猜测改读源码）。
- POST /auth/token grant_type=authorization_code 换 access_token（1h，
  对 E2E 足够）。client_id 非任意串：TokenView 经 indieauth
  .verify_client_id 强校验为 http(s) URL 形态（IndieAuth §3.2，本地
  真栈实锤 400 Invalid client id）；但无需预注册（auth_code store
  仅按该串取回 credential）。
- 其余全部走本集成自身契约：config flow → 真 MQTT 002 → devices 视图。

发布/订阅用 paho（HA 自带依赖，零新增环境要求）。仅 stdlib + paho。
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HA = os.environ.get("E2E_HA_URL", "http://127.0.0.1:8123").rstrip("/")
MQTT_HOST = os.environ.get("E2E_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("E2E_MQTT_PORT", "2022"))
GW_SN = "E2EGW0000001"
DEV_SN = "500700000001"     # v1.7.21：SN 前四位=机型码，5007 支持百分比
DEV_SN_5002 = "500200000001"  # 5002 平开窗：暂不支持百分比（走三态形态）
WS_PORT = int(os.environ.get("E2E_WS_PORT", "9001"))
CLIENT_ID = "https://e2e.local.test/"

TOKEN = None


def call(method, path, json_body=None, form=None, timeout=20, auth=True, raw_url=False):
    url = path if raw_url else HA + path
    headers = {}
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:400]}


def die(msg):
    print(f"!! E2E 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def step(n, msg):
    print(f"---- [{n}] {msg}")


# ---------- A. 等待 HA ----------
step("A", "等待 HA API 端口就绪")
deadline = time.time() + 300
ready = False
while time.time() < deadline:
    try:
        call("GET", "/api/", auth=False, timeout=5)
        ready = True  # 401 也算端口活（urlopen 抛 HTTPError 前已建立连接）
        break
    except urllib.error.HTTPError:
        ready = True
        break
    except Exception:
        time.sleep(3)
if not ready:
    die("HA 5 分钟未监听 " + HA)

# ---------- B/C. onboarding → auth_code → access_token ----------
step("B", "onboarding（client_id 任意串，现行 schema 源码实证）")
st, ob = call("POST", "/api/onboarding/users", json_body={
    "client_id": CLIENT_ID, "username": "e2e-admin",
    "password": "e2e-e2e-e2e", "name": "E2E Admin", "language": "en"},
    auth=False)
if st != 200 or not (isinstance(ob, dict) and "auth_code" in ob):
    die(f"onboarding HTTP {st}: {ob}")
step("C", "auth_code → access_token")
st, tk = call("POST", "/auth/token", form={
    "grant_type": "authorization_code", "code": ob["auth_code"],
    "client_id": CLIENT_ID}, auth=False)
if st != 200 or "access_token" not in tk:
    die(f"/auth/token HTTP {st}: {tk}")
TOKEN = tk["access_token"]
# 本地 harness 附加：token 落盘供后续复验脚本复用（一次性测试栈，CI 容器
# 跑完即焚零敏感；run_local.sh 同机可直接读 /tmp/ha_e2e_token）
try:
    with open("/tmp/ha_e2e_token", "w") as _f:
        _f.write(TOKEN)
except OSError:
    pass
step("C", "owner token 到手 ✓")

# ---------- D/E. MQTT 集成 entry ----------
step("D", "config flow 建立 HA MQTT 集成")
st, fl = call("POST", "/api/config/config_entries/flow",
              json_body={"handler": "mqtt", "show_advanced_options": False})
if st != 200 or "flow_id" not in fl:
    die(f"mqtt flow 启动 HTTP {st}: {fl}")
st, res = call("POST", f"/api/config/config_entries/flow/{fl['flow_id']}",
               json_body={"broker": MQTT_HOST, "port": str(MQTT_PORT)})
# 版本兼容（CI :stable 2026.8 实锤 vs 本地 2026.1 全绿）：新版 MQTT user
# step schema 要求 other_settings（advanced options 演进入 schema）——
# 报该键 required 时带空串重交，两代 HA 同码通过。
if isinstance(res, dict) and "other_settings" in json.dumps(res):
    st, res = call("POST", f"/api/config/config_entries/flow/{fl['flow_id']}",
                   json_body={"broker": MQTT_HOST, "port": str(MQTT_PORT),
                              # 26.8 OTHER_SETTINGS_SCHEMA（源码实证）：
                              # set_client_cert bool + set_ca_cert ∈
                              # off/auto/custom 为仅有两个无默认 Required
                              "other_settings": {"set_client_cert": False,
                                                 "set_ca_cert": "off"}})
if isinstance(res, dict) and res.get("type") == "form":
    # 二次 form（如高级项）→ 补交 other_settings 结束流程
    st, res = call("POST", f"/api/config/config_entries/flow/{res['flow_id']}",
                   json_body={"other_settings": {"set_client_cert": False,
                                                 "set_ca_cert": "off"}})
if not (isinstance(res, dict) and res.get("type") == "create_entry"):
    die(f"mqtt entry 创建失败: {res}")
step("E", "等待 mqtt loaded")
state = ""
for _ in range(40):
    st, els = call("GET", "/api/config/config_entries/entry")
    # 该端点返回裸 list（本地真栈实锤），兼容 {entries:[...]} 旧形态
    items = els if isinstance(els, list) else (els or {}).get("entries", [])
    state = next((e.get("state", "?") for e in items if e.get("domain") == "mqtt"), "?")
    if state == "loaded":
        break
    time.sleep(3)
if state != "loaded":
    die(f"mqtt 集成未 loaded（{state}）")
step("E", "mqtt loaded ✓")

# ---------- F. 慧尖 entry ----------
step("F", "config flow 建立慧尖网关条目")
st, fl = call("POST", "/api/config/config_entries/flow",
              json_body={"handler": "window_controller_gateway"})
if st != 200 or "flow_id" not in fl:
    die(f"huijian flow 启动 HTTP {st}: {fl}")
flow_id = fl["flow_id"]
st, res = call("POST", f"/api/config/config_entries/flow/{flow_id}",
               json_body={"gateway_sn": GW_SN, "gateway_name": "E2E网关"})
# 真栈实锤（本地 E2E 首轮）：user 步后有 confirm_add 二步——连通性测试
# 无响应（E2E 上报还没发）时以 confirm 复选框征询，必须真栈走完。
for _ in range(3):
    if not (isinstance(res, dict) and res.get("type") == "form"):
        break
    st, res = call("POST", f"/api/config/config_entries/flow/{res['flow_id']}",
                   json_body={"confirm": True})
entry = res.get("result") if isinstance(res, dict) else None
# 真栈实锤（本地第四轮）：本集成 flow 的 create_entry.result 直接是
# entry **对象**（HA 内建 mqtt 返回字符串 id）——两种形态统一收敛为 id
if isinstance(entry, dict):
    entry = entry.get("entry_id")
if not entry:
    die(f"huijian entry 未创建: {res}")
state = ""
for _ in range(40):
    # 单条详情端点形态存疑（第五轮本地实锤返回体无 state 键），
    # 复用已实证的列表端点按 entry_id 过滤（与 mqtt 轮询同法）
    st, items = call("GET", "/api/config/config_entries/entry")
    items = items if isinstance(items, list) else (items or {}).get("entries", [])
    state = next((e.get("state") for e in items if e.get("entry_id") == entry), "?")
    if state == "loaded":
        break
    time.sleep(3)
if state != "loaded":
    die(f"慧尖 entry 未 loaded（{state}）——真栈 setup 存在 mock 掩盖的问题！")
step("F", "慧尖 entry loaded ✓（真 HA setup 全链路）")

# ---------- G/H. 真 MQTT 002 上报 → devices 视图断言 ----------
import paho.mqtt.client as paho  # HA 自带依赖，零新增

step("G", "paho 经真 broker 发布 002 上报（并订阅 req 观察 ack）")
acks = []
ack_msgs = []   # (topic, payload)：v1.7.34 K/L 两臂需按主题甄别代答归属
pc = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="e2e-driver")
pc.connect(MQTT_HOST, MQTT_PORT, 30)


def _onmsg(_c, _u, msg):
    raw = msg.payload.decode(errors="replace")
    acks.append(raw)
    ack_msgs.append((msg.topic, raw))


pc.subscribe(f"gateway/{GW_SN}/req", qos=1)  # 现场实锤 ack 正常发出，
pc.on_message = _onmsg                       # QoS0 订挂竞态曾误报 0 条
pc.loop_start()
time.sleep(1)  # 让 SUB 报文过网再发布，观测才可靠


def publish_002(rid, rtravel):
    # v1.7.21：双机型同报——SN 前四位即机型码，能力逐机型不同。
    # 5007 后装开窗电机（支持百分比）+ 5002 平开窗（暂不支持，r_travel=255
    # 即实机"未校准"形态）。两条分支都必须在真栈上被证伪/证实。
    payload = {"head": {"cmdid": "002", "id": rid}, "ctype": "002", "id": rid,
               "sn": GW_SN, "data": {"status": 1, "devices": [
                   {"sn": DEV_SN, "model": "5007", "battery": 1210,
                    "r_travel": rtravel},
                   {"sn": DEV_SN_5002, "model": "5002", "battery": 1200,
                    "r_travel": 255}]}}
    pc.publish("gateway/rpt_rsp", json.dumps(payload)).wait_for_publish(timeout=5)


publish_002(9001, 50)

step("H", "轮询集成自带 devices 视图：gateway_online + 子设备")
deadline = time.time() + 60
online = found = False
while time.time() < deadline:
    st, devs = call("GET", f"/api/window_controller_gateway/devices?config_entry_id={entry}")
    if st == 200 and isinstance(devs, list) and devs:
        online = any(d.get("gateway_online") is True for d in devs)
        # v1.7.21：视图是扁平设备列表（网关 + 各子设备各自一条），
        # 按 identifiers 取 SN 集合判两台子设备都已注册
        _sns = {i[1] for d in devs for i in (d.get("identifiers") or [])
                if isinstance(i, list) and len(i) > 1}
        found = DEV_SN in _sns and DEV_SN_5002 in _sns
        if online and found:
            break
    time.sleep(3)
if not online:
    die("gateway_online 未在 60s 内转 true（connected 判定链路异常）")
if not found:
    die("002 子设备未进入设备注册表（_quick_add_device/registry 链路异常）")
step("H", f"MQTT→handler→registry→REST 全链路实证 ✓（ack 捕获 {len(acks)} 条→req）")

# ---------- H2. v1.7.20 HomeKit Window 契约（真栈实证） ----------
# 上游 homeassistant/components/homekit/type_covers.py 实证：Window
# accessory 判据 = device_class window + supported_features & SET_POSITION
# (4) + current_position 数值（"must support set_cover_position"）。
# 本段用真实 HA 核对该实体状态三输入，并走一次
# cover.set_cover_position 服务 → 真 broker req 主题捕获 004——即
# Apple Home 滑块拖动的完整链路（只差 HAP 协议层，那层由用户模板实证）。
step("H2", "cover 实体 HomeKit Window 输入面 + set_cover_position 真发 004")


def _cover_entities_by_sn(devs):
    """devices 视图 → {子设备SN: cover 实体}（identifiers 带 SN，零猜测）"""
    out = {}
    for d in devs or []:
        if not isinstance(d, dict):
            continue
        sns = [i[1] for i in (d.get("identifiers") or []) if isinstance(i, list) and len(i) > 1]
        if not sns:
            continue
        for e in (d.get("entities") or []):
            if e.get("domain") == "cover":
                out[sns[0]] = e["entity_id"]
    return out


_cover_map = _cover_entities_by_sn(devs)
_eid_5007 = _cover_map.get(DEV_SN)
_eid_5002 = _cover_map.get(DEV_SN_5002)
if not _eid_5007 or not _eid_5002:
    die(f"devices 视图未见两台子设备的 cover 实体（{_cover_map}）")


def _state_attrs(eid, want_position=None):
    """轮询实体状态直到出现期望形态（cover 实体异步创建）"""
    attrs, state = {}, {}
    deadline = time.time() + 20
    while time.time() < deadline:
        st, body = call("GET", f"/api/states/{eid}")
        if st == 200:
            attrs = (body or {}).get("attributes", {})
            state = body or {}
            if want_position is None or isinstance(attrs.get("current_position"), int):
                break
        time.sleep(2)
    return attrs, state


# --- 支持百分比的机型（5007）：Window 形态三输入 + 004 真发 ---
cov_attrs, cov_state = _state_attrs(_eid_5007, want_position=True)
if not cov_attrs.get("supported_features", 0) & 4:
    die(f"5007 supported_features 缺 SET_POSITION(4)（={cov_attrs.get('supported_features')}）")
if cov_attrs.get("device_class") != "window":
    die(f"5007 device_class != window（={cov_attrs.get('device_class')}）")
if cov_attrs.get("current_position") != 50:
    die(f"r_travel=50 上报后 current_position 应为 50（={cov_attrs.get('current_position')}）")
if cov_attrs.get("position_capable") is not True:
    die("5007 的 position_capable 属性应为 True")
# 状态口径钉死（v1.6.8 定案）：status 推导 open=50≠0；即便按位置分支计算
# 的 HA 旧版 state 逻辑，50>0 同判 open——两代口径下该断言恒成立。
if (cov_state or {}).get("state") != "open":
    die(f"cover.state 应为 open（={cov_state}）——位置暴露不得改变状态口径")
_n0 = len(acks)
st_scp, scp_resp = call("POST", "/api/services/cover/set_cover_position",
                        json_body={"entity_id": _eid_5007, "position": 37})
if st_scp >= 300:
    die(f"5007 cover.set_cover_position 服务调用失败: HTTP {st_scp} {scp_resp}")
_pos_seen = False
_dead = time.time() + 15
while time.time() < _dead and not _pos_seen:
    for raw in acks[_n0:]:
        try:
            j = json.loads(raw)
        except Exception:
            continue
        dat = j.get("data") or {}
        if (str(dat.get("value")) == "37"
                and dat.get("attribute") == "w_travel"
                and dat.get("sn") == DEV_SN):
            _pos_seen = True
            break
    time.sleep(0.5)
if not _pos_seen:
    die("req 主题未捕获 value=37/w_travel 的 004 报文（服务→MQTT 下发链路断）")

# --- 无百分比的机型（5002）：必须落回三态形态，且位置服务被拒 ---
cov2_attrs, cov2_state = _state_attrs(_eid_5002)
if cov2_attrs.get("supported_features", 0) & 4:
    die(f"5002 不应声明 SET_POSITION（={cov2_attrs.get('supported_features')}）——"
        "否则 HomeKit 出现假滑块且丢失三段式暂停")
if cov2_attrs.get("position_capable") is not False:
    die("5002 的 position_capable 属性应为 False")
if cov2_attrs.get("device_class") != "window":
    die(f"5002 device_class 应仍为 window（={cov2_attrs.get('device_class')}）")
st_rej, rej_body = call("POST", "/api/services/cover/set_cover_position",
                        json_body={"entity_id": _eid_5002, "position": 37})
if st_rej < 400:
    die(f"5002 的位置服务调用应被 HA 拒绝（HTTP {st_rej}）——无百分比硬件不得受理")
_n1 = len(acks)
time.sleep(2)
_leaked = [a for a in acks[_n1:] if DEV_SN_5002 in a and "w_travel" in a]
if _leaked:
    die(f"5002 的无效位置指令泄漏到 LoRa 空口：{_leaked[:1]}")
step("H2", "HomeKit 双机型实证：5007 Window(SET_POSITION+position=50+004 真发) / "
           "5002 三态(无位置位、位置服务被拒、无空口泄漏) ✓")

# ---------- K/L 共用助手 ----------
def _hj_entries():
    """本集成全部配置条目（列表端点形态经 F 段实证）。"""
    _st, items = call("GET", "/api/config/config_entries/entry")
    items = items if isinstance(items, list) else (items or {}).get("entries", [])
    return [e for e in items if e.get("domain") == "window_controller_gateway"]


def _ws_call(cmd_type, timeout=20):
    """经 HA WS API 发一条命令取 result（认证契约读 2026.9.3 源码实证）。

    为什么不用 REST：`GET /api/config/config_entries/flow` 在 HA 2026.9.3 是
    **405 Method Not Allowed**（`components/config/config_entries.py`
    `ConfigManagerFlowIndexView.get` 显式 `raise HTTPMethodNotAllowed`，真栈
    实锤）——在途发现流只有 WS `config_entries/flow/progress` 一个口子，且该
    命令会剔除 source=reconfigure/user 的流（发现流 source=discovery 在内）。
    WS 认证：首帧 `auth_required` → 发 `{"type":"auth","access_token":…}` →
    `auth_ok`（websocket_api/auth.py AUTH_MESSAGE_SCHEMA）。
    aiohttp 与 paho 同为 HA 自带依赖，零新增环境要求。
    """
    import asyncio

    try:
        import aiohttp
    except ImportError as e:
        die(f"WS 查询需要 aiohttp（HA 自带依赖）: {e}")

    async def _run():
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"{HA}/api/websocket") as ws:
                first = json.loads(await ws.receive_str())
                if first.get("type") != "auth_required":
                    raise RuntimeError(f"WS 首帧非 auth_required: {first}")
                await ws.send_json({"type": "auth", "access_token": TOKEN})
                auth = json.loads(await ws.receive_str())
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"WS 认证被拒: {auth}")
                await ws.send_json({"id": 1, "type": cmd_type})
                res = json.loads(await ws.receive_str())
                if not res.get("success"):
                    raise RuntimeError(f"WS 命令 {cmd_type} 失败: {res}")
                return res.get("result")

    return asyncio.run(asyncio.wait_for(_run(), timeout=timeout))


def _hj_flows(strict=True, last_err=None):
    """在途发现流列表；strict=False 时查询失败返回 None（轮询期容错）。

    失败**不得**静默当成"没有卡"——那会让 L 臂的"发现卡 0 张"变成假绿
    （K 臂首轮的 405 假红正是这么来的：REST 端点不通 ⇒ 恒空 ⇒ 两种断言都失真）。
    """
    try:
        flows = _ws_call("config_entries/flow/progress")
    except Exception as e:  # noqa: BLE001
        if isinstance(last_err, list):
            last_err.append(str(e))
        if strict:
            die(f"取在途发现流失败（WS config_entries/flow/progress）: {e}")
        return None
    return [f for f in (flows or [])
            if f.get("handler") == "window_controller_gateway"]


def _cards_for(sn, strict=True, last_err=None):
    flows = _hj_flows(strict=strict, last_err=last_err)
    if flows is None:
        return None
    return [f for f in flows
            if str((f.get("context") or {}).get("unique_id") or "").lower()
            == sn.lower()]


def _wait_acks(topic, since, want=1, timeout=30, settle=2.0):
    """等 topic 上出现 want 条代答后**再静置** settle 秒。

    仲裁门禁（v1.7.30）必须能抓到"多答"——只等到第一条就返回会假绿：
    第二个应答者往往晚几十毫秒到。
    """
    deadline = time.time() + timeout
    got = [m for m in ack_msgs[since:] if m[0] == topic]
    while time.time() < deadline and len(got) < want:
        time.sleep(0.5)
        got = [m for m in ack_msgs[since:] if m[0] == topic]
    time.sleep(settle)
    return [m for m in ack_msgs[since:] if m[0] == topic]


def _first_report(sn, msg_id):
    """网关首报 001 绑定请求（现场实锤形态：固件每 5s 重发直到收到应答）。"""
    return {"head": "$SH", "ctype": "001", "id": msg_id, "sn": sn,
            "data": {"vesion": "V3.55", "model": "YGZN_GW001",
                     "userid": 0, "familyid": 0}}


def _check_single_ack(acks_seen, sn, msg_id, label):
    """契约：一条 001 请求恰好换来一条同型 001 应答（多答=仲裁失效）。"""
    topic = f"gateway/{sn}/req"
    replies = []
    for _t, raw in acks_seen:
        try:
            if json.loads(raw).get("ctype") == "001":
                replies.append(raw)
        except Exception:
            continue
    if len(replies) != 1:
        die(f"{label}：{topic} 应恰好 1 条 001 代答（v1.7.30 仲裁），"
            f"实得 {len(replies)}: {[r[:120] for r in replies[:3]]}")
    body = json.loads(replies[0])
    if (body.get("head"), body.get("ctype"), body.get("id"), body.get("sn")) \
            != ("$SH", "001", msg_id, sn):
        die(f"{label}：代答报文与请求不同形 {body}")
    dat = body.get("data") or {}
    if dat.get("errcode") != 0 or not isinstance(dat.get("uuid"), str) \
            or len(dat["uuid"]) < 8:
        die(f"{label}：代答 data 缺 errcode:0 / uuid（v1.7.27 定稿同形）{dat}")
    return body


# ---------- K. 未配置网关首报 001：耳朵恰好一答 + 发现卡 ----------
# 现场断过两条链：①首报 001 无人应答 → 固件每 5s 重发永不停血（v1.7.26 起
# 耳朵代答止血）；②多耳并存时 1 请求 2~3 答（v1.7.30 仲裁收口）。本臂在真
# HA + 真 broker 上钉住应答面与发现面。
NEW_GW = "E2EGW0000002"
step("K", f"未配置网关 {NEW_GW} 首报 001 → 恰好一条同型代答 + 发现卡")
pc.subscribe(f"gateway/{NEW_GW}/req", qos=1)
time.sleep(1)                     # 让 SUB 报文过网再发布（G 段同款竞态教训）
_k0 = len(ack_msgs)
pc.publish("gateway/rpt_rsp", json.dumps(_first_report(NEW_GW, 7101))) \
    .wait_for_publish(timeout=5)
_k_acks = _wait_acks(f"gateway/{NEW_GW}/req", _k0)
_check_single_ack(_k_acks, NEW_GW, 7101, "K 未配置网关首报")

_card, _errs = None, []
_dead = time.time() + 40
while time.time() < _dead and _card is None:
    _c = _cards_for(NEW_GW, strict=False, last_err=_errs)
    _card = _c[0] if _c else None
    if _card is None:
        time.sleep(2)
if _card is None:
    _live = _hj_flows(strict=False, last_err=_errs) or []
    die(f"未配置网关 {NEW_GW} 的发现卡未在 40s 内出现（discovery 链断）；"
        f"在途流={[(f.get('context') or {}).get('unique_id') for f in _live]}；"
        f"WS 查询末两次错误={_errs[-2:]}")
step("K", f"首报 001 真栈实证 ✓（1 请求 1 答·含 uuid；发现卡 {_card.get('flow_id')[:8]} 已挂起）")

# ---------- L. 空 SN 等待条目 + 首报 → 弹卡确认（v1.7.62：取消零点击静默填充） ----------
# v1.7.11 的"首报即把 SN 静默填进等待条目"已按用户裁定取消（现场判为不可见
# 添加）：等待条目只当耳朵，首台网关同样弹发现卡；用户确认后由
# async_remove_awaiting_entries 清掉等待条目。本臂真栈走完整条链：卡必须挂起
# 且**条目不得被填充**（反向半边）→ REST 走完卡片流（discovery→user→可选
# confirm_add，连接测试窗内由后台 002 喂养）→ 新条目 loaded + devices 三真 +
# 等待条目被清理；并顺带把 v1.7.30 仲裁在"两耳并存"形态下钉死。
AUTO_GW = "E2EGW0000003"
AUTO_DEV = "500700000002"
step("L", "建空 SN 等待条目（发现代理同款 REST 路径）")
st, fl = call("POST", "/api/config/config_entries/flow",
              json_body={"handler": "window_controller_gateway"})
if st != 200 or "flow_id" not in fl:
    die(f"等待条目 flow 启动 HTTP {st}: {fl}")
st, res = call("POST", f"/api/config/config_entries/flow/{fl['flow_id']}",
               json_body={"gateway_sn": "", "gateway_name": ""})
if not (isinstance(res, dict) and res.get("type") == "create_entry"):
    die(f"空 SN 提交未创建等待条目: {res}")

# 真值面（第三轮 CI 实证）：REST `/api/config/config_entries/entry` 返回的是
# `entry.as_json_fragment`——**不含 data/unique_id**（HA config_entries.py 源码
# 实证：只有 entry_id/domain/title/state/source/disabled_by 等）。所以"SN 是否
# 填进该条目"不能用条目表判（第二轮曾因此假红：填充明明发生，driver 却恒读
# 不到 data.gateway_sn）——改用**产品级可观测面**：devices 视图里的网关设备
# 带 (DOMAIN, sn) identifier，且 gateway_online 只有 handler 真处理过上报才真。
_others = [e for e in _hj_entries() if e.get("entry_id") != entry]
if len(_others) != 1:
    die(f"等待条目形态异常：除已配置网关外应恰有 1 个条目，实得 {len(_others)}"
        f"（id={[str(e.get('entry_id'))[:8] for e in _others]}）")
awaiting_id = _others[0].get("entry_id")
_aw_state = "?"
_dead = time.time() + 60
while time.time() < _dead:
    _aw_state = next((e.get("state") for e in _hj_entries()
                      if e.get("entry_id") == awaiting_id), "?")
    if _aw_state == "loaded":
        break
    time.sleep(2)
if _aw_state != "loaded":
    die(f"等待条目未 loaded（{_aw_state}）——心跳耳无从挂载，自动发现必死")
step("L", f"等待条目 {awaiting_id} loaded ✓（心跳耳已挂，两耳并存）")

pc.subscribe(f"gateway/{AUTO_GW}/req", qos=1)
time.sleep(1)
_l0 = len(ack_msgs)
pc.publish("gateway/rpt_rsp", json.dumps(_first_report(AUTO_GW, 7202))) \
    .wait_for_publish(timeout=5)
_l_acks = _wait_acks(f"gateway/{AUTO_GW}/req", _l0)
_check_single_ack(_l_acks, AUTO_GW, 7202, "L 两耳并存首报")

# v1.7.62 新语义：等待条目在位时，周期 002 **不得**触发静默填充，必须挂发现卡。
# 后台持续 002（贴合真网关周期上报）：既喂卡片的连接测试窗，也当"不许静默
# 填充"的判据面——旧版会在几秒内把等待条目填掉、且不弹卡。
import threading
_stop = threading.Event()


def _auto_pub():
    _i = 7300
    while not _stop.is_set():
        pc.publish("gateway/rpt_rsp", json.dumps({
            "head": {"cmdid": "002", "id": _i}, "ctype": "002", "id": _i,
            "sn": AUTO_GW, "data": {"status": 1, "devices": [
                {"sn": AUTO_DEV, "model": "5007", "battery": 1210,
                 "r_travel": 60}]}}))
        _i += 1  # 每轮换 id：同 id 会被 5s 去重层吃掉（v1.7.34 L 臂既有钉）
        time.sleep(2)


threading.Thread(target=_auto_pub, daemon=True).start()
try:
    # (1) 发现卡必须挂起（60s 轮询）；等待条目**不得**出现网关设备
    _card, _dead = None, time.time() + 60
    while time.time() < _dead and _card is None:
        _cards = _cards_for(AUTO_GW) or []
        _card = _cards[0] if _cards else None
        if _card is None:
            time.sleep(2)
    if _card is None:
        _fl2 = _hj_flows(strict=False) or []
        _fl2_desc = [(f.get("handler"), (f.get("context") or {}).get("unique_id"))
                     for f in _fl2]
        die("v1.7.62 新语义：等待条目在位的首台网关必须弹发现卡（60s 未出现）；"
            f"在途流={_fl2_desc}")
    st, d2 = call("GET", f"/api/window_controller_gateway/devices"
                         f"?config_entry_id={awaiting_id}")
    _ids2 = {i[1] for d in (d2 or []) for i in (d.get("identifiers") or [])
             if isinstance(i, list) and len(i) > 1} if st == 200 else set()
    if AUTO_GW in _ids2:
        die("静默填充回潮：等待条目被自动填入 SN（v1.7.62 已取消该行为）")
    step("L", f"发现卡 {str(_card.get('flow_id'))[:8]} 已挂起、等待条目未被填充 ✓")

    # (2) REST 走完卡片流（＝用户在卡片上点确认）：discovery → user →（可选 confirm_add）
    _fid = _card.get("flow_id")
    _name = f"慧尖网关 {AUTO_GW[-4:]}"
    st, res = call("POST", f"/api/config/config_entries/flow/{_fid}",
                   json_body={"gateway_sn": AUTO_GW, "gateway_name": _name})
    for _round in range(6):
        if not isinstance(res, dict):
            die(f"卡片流响应异常: {res}")
        if res.get("type") == "create_entry":
            break
        if res.get("type") != "form":
            die(f"卡片流中止于 {res.get('type')}: "
                f"{res.get('reason') or res.get('errors')}")
        _sid = res.get("step_id")
        if _sid == "user":
            _body = {"gateway_sn": AUTO_GW, "gateway_name": _name}
        elif _sid == "confirm_add":
            _body = {"confirm": True}
        else:
            die(f"卡片流进入未知步骤 {_sid}: {res}")
        st, res = call("POST", f"/api/config/config_entries/flow/{_fid}",
                       json_body=_body)
    else:
        die(f"卡片流 6 轮未完成: {res}")
    step("L", "卡片流经 REST 确认完成（create_entry）✓")

    # (3) 新条目 loaded + devices 三真 + 等待条目被清理
    _new_id, _seen = None, {}
    _dead = time.time() + 60
    while time.time() < _dead and _new_id is None:
        _new = [e for e in _hj_entries()
                if e.get("entry_id") not in (entry, awaiting_id)]
        if len(_new) == 1:
            _new_id = _new[0].get("entry_id")
            st, d3 = call("GET", f"/api/window_controller_gateway/devices"
                                 f"?config_entry_id={_new_id}")
            if st == 200 and isinstance(d3, list):
                _ids3 = {i[1] for d in d3 for i in (d.get("identifiers") or [])
                         if isinstance(i, list) and len(i) > 1}
                _seen = {"条目数": len(d3), "含网关SN": AUTO_GW in _ids3,
                         "含子设备": AUTO_DEV in _ids3,
                         "网关在线": any(x.get("gateway_online") is True for x in d3)}
                if not all(_seen.values()):
                    _new_id = None
        if not _new_id:
            time.sleep(2)
    if not _new_id:
        _ents = [(str(e.get("entry_id"))[:8], e.get("state")) for e in _hj_entries()]
        die(f"确认添加后新条目/devices 三真未达标：devices={_seen}；条目快照={_ents}")
    _dead = time.time() + 10
    while time.time() < _dead:
        if not [e for e in _hj_entries() if e.get("entry_id") == awaiting_id]:
            break
        time.sleep(1)
    else:
        die(f"等待条目 {str(awaiting_id)[:8]} 未被清理——确认添加后的配套动作未生效")
finally:
    _stop.set()

step("L", f"首台弹卡确认链真栈实证 ✓（卡挂起→REST 确认→新条目 loaded→"
           f"子设备 {AUTO_DEV} 注册→等待条目已清理；两耳并存仍 1 请求 1 答）")


# ---------- I. WS 网关默认监听 ----------
step("I", f"WS 网关 {WS_PORT} 常听断言（v1.6.16 默认开语义守护）")
ok = False
for _ in range(10):
    try:
        with socket.create_connection(("127.0.0.1", WS_PORT), timeout=3):
            ok = True
        break
    except OSError:
        time.sleep(2)
if not ok:
    die(f"WS {WS_PORT} 未监听——默认开语义被破坏")
step("I", "WS 端口监听 ✓")

# v1.7.31（B-2 真栈门禁）：带合法子协议令牌但**非 WS 升级**的 GET 必须收到
# 显式 4xx 状态行。回归到"return 未 prepared 的 ws"形态时，aiohttp
# finish_response 二次 prepare 抛 HTTPBadRequest 逃逸成 Unhandled ERROR、
# 连接无状态行即被掐（0918 台架栈实锤）——这里以裸 socket 读原始响应验证。
req = (b"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
       b"Sec-WebSocket-Protocol: hIZ56jhQ-wzA3ENiP2xGzo55PXsewUWM\r\n"
       b"Connection: keep-alive\r\n\r\n") % WS_PORT
try:
    with socket.create_connection(("127.0.0.1", WS_PORT), timeout=5) as sk:
        sk.sendall(req)
        resp = sk.recv(256)
    head = resp.split(b"\r\n", 1)[0].decode(errors="replace")
    if not head.startswith("HTTP/1.1 4"):
        die(f"B-2 回归：GET(带令牌,非升级) 响应 {head!r}——应为 4xx 显式回复")
    step("I", f"非升级 GET 显式 {head.split()[1]} 回复 ✓（B-2 守护）")
except OSError as e:
    die(f"B-2 探测连接失败：{e}")

# ---------- J. soak ----------
step("J", "500 条 002 上报吞吐与稳定性 soak")
t0 = time.time()
for i in range(500):
    payload = {"head": {"cmdid": "002", "id": 10000 + i}, "ctype": "002",
               "id": 10000 + i, "sn": GW_SN, "data": {"status": 1, "devices": [
                   {"sn": DEV_SN, "model": "5007", "battery": 1210,
                    "r_travel": i % 101}]}}
    pc.publish("gateway/rpt_rsp", json.dumps(payload))
    if i % 50 == 49:
        time.sleep(0.2)  # 微节流：贴近真实心跳风暴而非 DoS
el = time.time() - t0 + 0.5
time.sleep(10)
st, states = call("GET", "/api/states", timeout=30)
if st != 200:
    die("soak 后 /api/states 不再 200（HA 被打挂/阻塞？）")
rate = 500 / el
print(f"soak 500 条 {el:.1f}s（~{rate:.0f}/s 注入），soak 后 HA 全响应正常 ✓")

summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("## E2E 真栈结果\n"
                "- onboarding/config flow/002 上报全链路真栈 ✓\n"
                f"- gateway_online + 子设备注册 + WS {WS_PORT} 常听 ✓\n"
                "- HomeKit 双机型：5007 Window(SET_POSITION/position/004 真发)、5002 三态(位置服务被拒) ✓\n"
                f"- 首报 001 代答：未配置网关 1 请求 1 答（含 uuid）+ 发现卡挂起 ✓\n"
                f"- 首台弹卡确认（v1.7.62）：等待条目不被静默填充 → 卡挂起 → "
                f"REST 确认 → {AUTO_GW} loaded → 子设备 {AUTO_DEV} 注册 → "
                f"等待条目已清理，两耳并存仍 1 答 ✓\n"
                f"- soak 500 条注入 ~{rate:.0f}/s，HA 全程可用\n")

pc.loop_stop()
print("E2E 全部断言通过 ✅")
