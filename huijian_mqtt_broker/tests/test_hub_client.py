"""hub_client 单测：出站客户端的关键语义（重连退避 / 切片睡眠 / 值校验 / 命令回执 /
身份持久化 / 状态条目不回显凭据）。全部用假对象，不打网络。"""
import ast
import asyncio
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path

from custom_components.window_controller_gateway import hub_client as hc


# ── 纯函数 ────────────────────────────────────────────────────────
def test_reconnect_delay_is_exponential_capped():
    """指数阶梯 + 封顶不变；v1.7.50 起叠了 ±20% 抖动（防 hub pod 重启后多 agent 惊群），
    所以断言**区间**而不是精确值——但阶梯语义必须还在：后一次的中位数是前一次的两倍。"""
    for attempt, base in ((1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0)):
        draws = [hc.hub_reconnect_delay(attempt) for _ in range(200)]
        lo, hi = base * (1 - hc.HUB_RECONNECT_JITTER), base * (1 + hc.HUB_RECONNECT_JITTER)
        assert all(lo <= d <= hi for d in draws), (attempt, min(draws), max(draws))
        assert len(set(round(d, 3) for d in draws)) > 1, "第 %d 次没有抖动＝惊群没修" % attempt
    for _ in range(200):                                   # 封顶：抖动不许顶穿 MAX
        assert hc.hub_reconnect_delay(20) <= hc.HUB_RECONNECT_MAX_S
        assert hc.hub_reconnect_delay(20) >= hc.HUB_RECONNECT_MAX_S * (1 - hc.HUB_RECONNECT_JITTER)
    assert hc.hub_reconnect_delay(0) >= hc.HUB_RECONNECT_BASE_S * (1 - hc.HUB_RECONNECT_JITTER)
    assert all(hc.hub_reconnect_delay(n) > 0 for n in range(-3, 30)), "延迟必须恒 > 0"


def test_sleep_slices_and_yields_when_stopping(monkeypatch):
    monkeypatch.setattr(hc, "HUB_SLEEP_SLICE_S", 0.01)
    calls = {"n": 0}

    async def run():
        # 未停机：600s 只睡 3 片就人为停机（证明是切片睡、不是整睡）
        async def fake_sleep(_s):
            calls["n"] += 1
            if calls["n"] >= 3:
                state["stopping"] = True
        monkeypatch.setattr(hc.asyncio, "sleep", fake_sleep)
        state = {"stopping": False}
        await hc.interruptible_sleep(600, lambda: state["stopping"])
        assert calls["n"] == 3

    asyncio.run(run())


def test_cred_brief_never_echoes_plaintext():
    assert hc.cred_brief("31fe26cacbbefded") == "len=16 head=31"
    assert hc.cred_brief("") == "(空)"
    assert hc.cred_brief(None) == "(空)"
    assert "31fe" not in hc.cred_brief("31fe")[6:]                 # 只留首两字节


def test_validate_control_params_matches_lan_semantics():
    # 本批起"与 LAN 同语义"是由构造保证的（云侧直接委托 ws_gateway 的同一实现），
    # 这里逐条列的是**值域**判据本身。
    assert hc.validate_control_params("w_travel", "100") == "100"
    assert hc.validate_control_params("w_travel", 100) == "100"
    # v1.7.65 改判两条（理由见 tests/test_control_attr_domain.py 的 ⑥ 段）：
    # 四个出站属性值域全是整数，小数不再放行；-1 是视图层"从未收到上报"的哨兵。
    assert hc.validate_control_params("w_travel", 1.5) is None
    assert hc.validate_control_params("w_travel", -1) is None
    # 拒：未知属性 / 空串值 / bool / 容器 / nan / inf / 科学计数（设备不可解析）
    assert hc.validate_control_params("position", "100") is None
    assert hc.validate_control_params("", "100") is None
    assert hc.validate_control_params("w_travel", "") is None
    assert hc.validate_control_params("w_travel", None) is None
    assert hc.validate_control_params("w_travel", True) is None
    assert hc.validate_control_params("w_travel", {"a": 1}) is None
    assert hc.validate_control_params("w_travel", float("nan")) is None
    assert hc.validate_control_params("w_travel", float("inf")) is None
    assert hc.validate_control_params("w_travel", 1e308) is None


def test_identity_roundtrip_and_corrupt_file(tmp_path):
    d = str(tmp_path)
    assert hc.load_identity(d) == {}
    hc.save_identity(d, {"instanceId": "abc123", "secret": "s" * 48, "bindCode": "012345"})
    got = hc.load_identity(d)
    assert got["instanceId"] == "abc123" and got["bindCode"] == "012345"
    with open(hc.identity_path(d), "w", encoding="utf-8") as fh:
        fh.write("{ 坏 json")
    assert hc.load_identity(d) == {}                                  # 损坏按未注册
    assert os.path.exists(hc.identity_path(d) + ".bad")               # 且留证


# ── 假对象 ────────────────────────────────────────────────────────
class FakeManager:
    def __init__(self, devices=None, gateway_sn="GW1"):
        self.devices = devices if devices is not None else {
            "A1B2": {"attributes": {"r_travel": 30, "voltage": 12.3}},
        }
        self.gateway_sn = gateway_sn
        self.listeners = []

    def add_status_listener(self, cb):
        if cb not in self.listeners:
            self.listeners.append(cb)

    def remove_status_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)


class FakeWS:
    def __init__(self, incoming=None, hold=0.0):
        self.sent = []
        self._incoming = list(incoming or [])
        self._hold = hold

    async def send_json(self, obj):
        self.sent.append(obj)

    def __aiter__(self):
        async def gen():
            for item in self._incoming:
                yield item
            if self._hold:                      # 会话别立刻断：给上行协程留出发帧窗口
                await asyncio.sleep(self._hold)
        return gen()


class FakeCM:
    """真 aiohttp 的 ws_connect() 是 awaitable 且返回值本身可 async with——桩不能比真实现窄
    （`_open_ws` 先 await 再 async with，只实现 CM 就会 TypeError）。"""

    def __init__(self, ws):
        self.ws = ws

    def __await__(self):
        async def self_await():
            return self
        return self_await().__await__()

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, register_payload=None, ws=None):
        self.register_payload = register_payload or {
            "ok": True, "instanceId": "9f5e4f116b05b4c3",
            "secret": "31fe26cacbbefdedc6faa8f0eeec26ad202ed61f320a65ca", "bindCode": "251562",
        }
        self.ws = ws or FakeWS()
        self.posted = []
        self.closed = False

    def post(self, url, json=None):                                    # noqa: A002 - aiohttp 同签名
        self.posted.append((url, json))
        return FakeResp(self.register_payload)

    def ws_connect(self, url, headers=None, heartbeat=None):           # noqa: ARG002
        self.ws_url = url
        self.ws_headers = headers
        return FakeCM(self.ws)

    async def close(self):
        self.closed = True


def make_client(tmp_path, **kw):
    manager = kw.pop("manager", None)
    managers = kw.pop("managers", None)
    if managers is None:
        managers = [manager or FakeManager()]
    elif manager is not None:
        managers = [manager] + list(managers)
    session = kw.pop("session", None) or FakeSession()
    client = hc.HubClient(managers, config_dir=str(tmp_path), session=session, **kw)
    return client, (managers[0] if managers else None), session


# ── 行为 ──────────────────────────────────────────────────────────
def test_register_persists_identity_and_keeps_bind_code_out_of_logs(tmp_path, caplog):
    client, _, session = make_client(tmp_path)

    asyncio.run(client._ensure_registered())

    assert client.instance_id == "9f5e4f116b05b4c3"
    assert client.bind_code == "251562"
    assert session.posted[0][0].endswith("/agent/register")
    assert session.posted[0][1]["installKey"] == hc.HUB_DEFAULT_INSTALL_KEY
    saved = hc.load_identity(str(tmp_path))
    assert saved["secret"] == client._secret                            # 身份落盘（重启复用）
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "251562" not in logs and client._secret[:8] not in logs       # 凭据不回显


def test_register_not_repeated_when_identity_exists(tmp_path):
    hc.save_identity(str(tmp_path), {"instanceId": "abc", "secret": "s" * 32, "bindCode": "000111"})
    client, _, session = make_client(tmp_path)

    asyncio.run(client._ensure_registered())

    assert session.posted == []                                         # 有身份就不再注册
    assert client.instance_id == "abc"


def test_ws_url_switches_scheme_and_carries_no_credentials(tmp_path):
    """URL 只负责换 scheme：凭据一律走请求头，绝不进 query（中间层访问日志会留档明文）。"""
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    assert client._ws_url() == "wss://" + hc.HUB_DEFAULT_BASE[len("https://"):] + "/agent/ws"
    client.base = "http://127.0.0.1:8080"
    assert client._ws_url() == "ws://127.0.0.1:8080/agent/ws"
    assert "?" not in client._ws_url(), "长连 URL 不得带 query（secret 会进访问日志）"
    assert "def" not in client._ws_url() and "abc" not in client._ws_url()
    assert client._ws_headers() == {hc.WS_HEADER_INSTANCE_ID: "abc", hc.WS_HEADER_SECRET: "def"}
    assert hc.WS_HEADER_INSTANCE_ID == "x-hub-instance-id" and hc.WS_HEADER_SECRET == "x-hub-secret"


def test_state_snapshot_uses_injected_view_and_survives_bad_entries(tmp_path):
    manager = FakeManager(devices={"A1B2": {"attributes": {"wind_lock_mode": 1}}, "BAD": {}})

    def builder(sn, gw, dev):
        if sn == "BAD":
            raise ValueError("坏条目")
        return {"sn": sn, "gwSn": gw, "position": 30}

    client, _, _ = make_client(tmp_path, manager=manager, view_builder=builder)
    items, _auth = client.build_state_snapshot()
    # 坏条目被跳过、不炸；锁定模式随状态上行带过去（云通道没有 LAN 那路
    # device_update 实时推送，缺失即小程序永远显示"--"）
    assert items == [{"sn": "A1B2", "gwSn": "GW1", "position": 30, "windLockMode": 1}]


def test_state_snapshot_wind_lock_mode_unknown_stays_minus_one(tmp_path):
    cases = {
        "MISS": {},                                     # 无 attributes
        "NONE": {"attributes": {"wind_lock_mode": None}},
        "BOOL": {"attributes": {"wind_lock_mode": True}},   # bool 不是固件合法模式值
        "JUNK": {"attributes": {"wind_lock_mode": "abc"}},
        "INF": {"attributes": {"wind_lock_mode": float("inf")}},
        "OK0": {"attributes": {"wind_lock_mode": 0}},
    }
    manager = FakeManager(devices=cases)
    client, _, _ = make_client(tmp_path, manager=manager,
                               view_builder=lambda sn, gw, dev: {"sn": sn, "gwSn": gw})
    modes = {it["sn"]: it["windLockMode"] for it in client.build_state_snapshot()[0]}
    assert modes == {"MISS": -1, "NONE": -1, "BOOL": -1, "JUNK": -1, "INF": -1, "OK0": 0}


def test_keepalive_marks_state_dirty_even_without_status_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HUB_KEEPALIVE_S", 0.03)
    monkeypatch.setattr(hc, "HUB_SLEEP_SLICE_S", 0.01)
    client, _, _ = make_client(tmp_path)

    async def run():
        client._stopping = False
        client._state_dirty = asyncio.Event()
        task = asyncio.ensure_future(client._keepalive_loop())
        await asyncio.sleep(0.08)
        dirty = client._state_dirty.is_set()
        client._stopping = True                       # 停机闩锁 → 循环自行退出
        await asyncio.wait_for(task, timeout=1.0)
        return dirty

    assert asyncio.run(run()) is True


def test_handle_cmd_replies_and_normalizes_value(tmp_path):
    calls = []

    async def control(sn, attribute, value):
        calls.append((sn, attribute, value))
        return True

    client, _, _ = make_client(tmp_path, control_fn=control)

    ok = asyncio.run(client._handle_cmd({
        "t": "cmd", "cmdsn": "c1", "sn": "A1B2", "action": "control",
        "params": {"attribute": "w_travel", "value": 100},
    }))
    assert ok == {"ok": True, "data": {"published": True}}              # ok=已发布 broker
    assert calls == [("A1B2", "w_travel", "100")]                        # 值已规范化

    assert asyncio.run(client._handle_cmd({"action": "reboot", "sn": "A1B2"}))["err"] == "unknown_action"
    assert asyncio.run(client._handle_cmd({
        "action": "control", "sn": "", "params": {"attribute": "w_travel", "value": "100"},
    }))["err"] == "invalid_params"
    assert asyncio.run(client._handle_cmd({
        "action": "control", "sn": "A1B2", "params": {"attribute": "w_travel", "value": True},
    }))["err"] == "invalid_params"


def test_handle_cmd_without_control_fn_and_on_failure(tmp_path):
    client, _, _ = make_client(tmp_path)
    assert asyncio.run(client._handle_cmd({
        "action": "control", "sn": "A1B2", "params": {"attribute": "w_travel", "value": "100"},
    }))["err"] == "control_unavailable"

    async def boom(sn, attribute, value):                               # noqa: ARG001
        raise RuntimeError("boom")

    client2, _, _ = make_client(tmp_path, control_fn=boom)
    assert asyncio.run(client2._handle_cmd({
        "action": "control", "sn": "A1B2", "params": {"attribute": "w_travel", "value": "100"},
    }))["err"] == "control_failed"


def test_session_once_replies_cmd_result_and_flushes_state(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HUB_STATE_DEBOUNCE_S", 0.01)
    cmd = json.dumps({"t": "cmd", "cmdsn": "c9", "sn": "A1B2",
                      "action": "control", "params": {"attribute": "w_travel", "value": "0"}})

    class Msg:
        type = 1  # aiohttp.WSMsgType.TEXT

        def __init__(self, data):
            self.data = data

    ws = FakeWS(incoming=[Msg(cmd)], hold=0.3)

    async def control(sn, attribute, value):
        return False                                                    # 发布失败 → ok False

    session = FakeSession(ws=ws)
    client, _, _ = make_client(tmp_path, session=session, control_fn=control)

    async def run():
        client._stopping = False
        client._state_dirty = asyncio.Event()
        done = await client._session_once()
        assert done is True
        await asyncio.sleep(0.05)                                       # 让 flush 有机会跑
        return ws.sent

    sent = asyncio.run(run())
    kinds = [m.get("t") for m in sent]
    assert "cmd_result" in kinds                                        # 回执必发
    reply = next(m for m in sent if m.get("t") == "cmd_result")
    assert reply["cmdsn"] == "c9" and reply["ok"] is False
    assert "state" in kinds                                             # 上线即全量推一次
    state = next(m for m in sent if m.get("t") == "state")
    assert state["items"] and state["items"][0]["sn"] == "A1B2"


def test_status_view_exposes_bind_code_but_never_secret(tmp_path):
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret, client.bind_code = "abc", "S" * 48, "654321"
    view = client.status_view()
    assert view["bindCode"] == "654321"
    assert "S" * 48 not in json.dumps(view, ensure_ascii=False)          # secret 绝不外露
    assert view["connected"] is False and view["instanceId"] == "abc"
    assert view["gatewaySn"] == "GW1"                                    # 插件页"本机网关"用


def test_status_view_survives_missing_gateway_sn(tmp_path):
    class _Boom:
        @property
        def gateway_sn(self):
            raise RuntimeError("boom")

    client, _, _ = make_client(tmp_path, manager=_Boom())
    assert client.status_view()["gatewaySn"] == ""                        # 取 SN 抛错不得带崩视图


def test_stop_is_idempotent_and_detaches_listener(tmp_path):
    client, manager, session = make_client(tmp_path)
    asyncio.run(client.async_stop())                                    # 未启动也能停
    asyncio.run(client.async_start())
    assert manager.listeners                                                     # 已挂监听
    asyncio.run(client.async_stop())
    assert manager.listeners == []                                               # 已摘监听
    asyncio.run(client.async_stop())                                    # 幂等
    assert client.connected is False


# ── 绑定码：过期判定 / 轮换 / 自动补发（v1.7.41）────────────────────
def test_bind_code_expiry_math_and_view(tmp_path):
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client.bind_code, client._bind_code_at = "111111", time.time()
    assert 0 < client.bind_code_expires_in() <= hc.BIND_CODE_TTL_S
    view = client.status_view()
    assert view["bindCodeExpired"] is False and view["bindCodeExpiresIn"] > 0

    client._bind_code_at = time.time() - hc.BIND_CODE_TTL_S - 5      # 已过期
    assert client.bind_code_expires_in() < 0
    assert client.status_view()["bindCodeExpired"] is True

    client.bind_code = None                                           # 没码不算"过期"，算没有
    assert client.bind_code_expires_in() == -1
    assert client.status_view()["bindCodeExpired"] is False


def test_refresh_bind_code_persists_and_degrades(tmp_path, monkeypatch):
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client._bind_code_at = time.time() - 9999                         # 手上是死码
    calls = []

    async def ok_http(path, payload, timeout_s=None):
        calls.append((path, payload))
        return {"ok": True, "bindCode": "222222", "expiresInSec": 600}

    monkeypatch.setattr(client, "_http", ok_http)
    assert asyncio.run(client.refresh_bind_code()) is True
    assert calls[0][0] == "/agent/bindcode"
    assert calls[0][1]["instanceId"] == "abc" and calls[0][1].get("secret") == "def"
    assert client.bind_code == "222222" and client.bind_code_expires_in() > 500
    saved = hc.load_identity(str(tmp_path))
    assert saved["bindCode"] == "222222" and saved["bindCodeAt"] > 0   # 签发时刻落盘（重启后能判过期）

    async def reject_http(path, payload, timeout_s=None):                              # hub 拒绝（bad_secret 等）
        return {"ok": False, "err": "bad_secret"}

    monkeypatch.setattr(client, "_http", reject_http)
    assert asyncio.run(client.refresh_bind_code()) is False
    assert client.bind_code == "222222"                                # 失败不动手上的码

    async def boom_http(path, payload, timeout_s=None):                                # 旧 hub 没这条路由/网络断
        raise RuntimeError("net down")

    monkeypatch.setattr(client, "_http", boom_http)
    assert asyncio.run(client.refresh_bind_code()) is False


def test_renew_bind_code_if_stale_only_when_needed(tmp_path, monkeypatch):
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client.bind_code, client._bind_code_at = "111111", time.time()
    calls = []

    async def fake_http(path, payload, timeout_s=None):
        calls.append(path)
        return {"ok": True, "bindCode": "333333"}

    monkeypatch.setattr(client, "_http", fake_http)
    asyncio.run(client._renew_bind_code_if_stale())                    # 新鲜：一次都不该发
    assert calls == [] and client.bind_code == "111111"

    client._bind_code_at = time.time() - (hc.BIND_CODE_TTL_S - 10)     # 只剩 10s：该换
    asyncio.run(client._renew_bind_code_if_stale())
    assert calls == ["/agent/bindcode"] and client.bind_code == "333333"

    # 签发时刻未知＝按过期处理（v1.7.45 起换码尝试有最小间隔，这里要放行才能测到本意）
    client.bind_code, client._bind_code_at = "444444", 0
    client._bind_renew_at = 0.0
    asyncio.run(client._renew_bind_code_if_stale())
    assert len(calls) == 2 and calls[-1] == "/agent/bindcode"


def test_bindcode_failure_logs_the_actual_reason(tmp_path, caplog):
    """v1.7.45：这条告警曾只打 type(e).__name__，而消息里带的正是 HTTP 状态。

    真机 32s 内 8 条"hub 换绑定码失败（RuntimeError）"没人说得出是云端发布中（该重试）、
    凭据不认（该重注册）还是被墙（该看网络）——判据必须落到日志里。
    """
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client.bind_code, client._bind_code_at = "111111", 0

    async def bad_gateway(path, payload, timeout_s=None):
        raise RuntimeError("hub /agent/bindcode -> 502")

    client._http = bad_gateway
    caplog.set_level(logging.WARNING, logger=client._logger.name)
    assert asyncio.run(client.refresh_bind_code()) is False
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "502" in logs, "失败原因（HTTP 状态）必须进日志：%s" % logs
    # v1.7.50：换码失败是**操作类**错误，落 last_op_error（连接类槽留给身份/长连故障，
    # 否则一次瞬时换码失败就长期盖住 identity_rejected_loop 这条最有诊断价值的信息）
    assert client.last_op_error == "bindcode_failed"
    assert client.last_error is None


def test_bindcode_failure_never_echoes_credentials(tmp_path, caplog):
    """凭据不回显纪律：万一异常消息里带上含 secret 的 URL，也只能打类型名。"""
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "SUPERSECRETVALUE"
    client.bind_code, client._bind_code_at = "111111", 0

    async def leaky(path, payload, timeout_s=None):
        raise RuntimeError("POST /agent/bindcode?secret=SUPERSECRETVALUE failed")

    client._http = leaky
    caplog.set_level(logging.WARNING, logger=client._logger.name)
    asyncio.run(client.refresh_bind_code())
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "SUPERSECRETVALUE" not in logs, "日志回显了凭据：%s" % logs


def test_renew_throttles_after_failure(tmp_path):
    """失败后不得连击：自动补发挂在"上线自检 + 每个保活 tick"上，云端发布中的几十秒里
    会话会重连很多次（真机实发 8 次/32s），节流后同一窗口只发一次。"""
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client.bind_code, client._bind_code_at = "111111", 0
    n = {"calls": 0}

    async def failing(path, payload, timeout_s=None):
        n["calls"] += 1
        raise RuntimeError("hub /agent/bindcode -> 502")

    client._http = failing
    asyncio.run(client._renew_bind_code_if_stale())
    asyncio.run(client._renew_bind_code_if_stale())
    asyncio.run(client._renew_bind_code_if_stale())
    assert n["calls"] == 1, "失败后没节流＝对付费端点连击 + 同一条告警刷屏（%d 次）" % n["calls"]

    client._bind_renew_at = time.time() - hc.BIND_CODE_RENEW_MIN_INTERVAL_S - 1
    asyncio.run(client._renew_bind_code_if_stale())
    assert n["calls"] == 2, "超过最小间隔后必须还能再试（否则一次故障永久废掉补发）"


def test_panel_click_is_not_throttled(tmp_path):
    """节流只管自动补发：用户点二维码是显式意图，必须当场换码（旧码作废是刻意的）。"""
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret = "abc", "def"
    client._bind_renew_at = time.time()          # 刚刚"自动试过"
    calls = []

    async def ok(path, payload, timeout_s=None):
        calls.append(path)
        return {"ok": True, "bindCode": "654321"}

    client._http = ok
    assert asyncio.run(client.refresh_bind_code()) is True
    assert calls == ["/agent/bindcode"] and client.bind_code == "654321"


def test_clean_close_reconnects_at_the_floor_not_zero(tmp_path, monkeypatch):
    """连上过又被对端干净关掉 ⇒ 阶梯归零（证明端点是通的），但**不是零间隔**。

    旧实现是 `attempt=0; continue`＝立即重连：遇到"接受后立刻关"（被 replaced /
    网关抽风 / 云端发布中）就是无间隔风暴。反过来把退避按会话时长升级也会坏——
    跨仓 e2e 的 C 臂（hub 反复重启）会因此一路涨到 300s，把恢复拖成几分钟不可用。
    """
    client, _, _ = make_client(tmp_path)
    delays = []
    rounds = {"i": 0}

    async def fake_session():
        rounds["i"] += 1
        if rounds["i"] >= 4:
            client._stopping = True
        return True

    async def fake_sleep(seconds, is_stopping):
        delays.append(seconds)

    monkeypatch.setattr(client, "_session_once", fake_session)
    monkeypatch.setattr(hc, "interruptible_sleep", fake_sleep)
    asyncio.run(client._run_forever())
    assert delays == [hc.HUB_RECONNECT_FLOOR_S] * 3, \
        "干净断开应按地板间隔重连且不升级阶梯，实得 %s" % delays


def test_connect_failure_still_escalates(tmp_path, monkeypatch):
    """连都没连上（异常）⇒ 必须走指数阶梯，不能享受地板。抖动后断言区间。"""
    client, _, _ = make_client(tmp_path)
    delays = []
    rounds = {"i": 0}

    async def boom_session():
        rounds["i"] += 1
        if rounds["i"] >= 3:
            client._stopping = True
        raise OSError("connection refused")

    async def fake_sleep(seconds, is_stopping):
        delays.append(seconds)

    monkeypatch.setattr(client, "_session_once", boom_session)
    monkeypatch.setattr(hc, "interruptible_sleep", fake_sleep)
    asyncio.run(client._run_forever())
    assert len(delays) == 2, delays
    for got, base in zip(delays, (hc.HUB_RECONNECT_BASE_S, hc.HUB_RECONNECT_BASE_S * 2)):
        assert base * (1 - hc.HUB_RECONNECT_JITTER) <= got <= base * (1 + hc.HUB_RECONNECT_JITTER), \
            "连不上必须指数退避（%s±20%%），实得 %s" % (base, got)
    assert delays[1] > delays[0] * 0.9, "阶梯必须递增，实得 %s" % delays


def test_reconnect_floor_is_positive():
    """地板钉：防"风暴保护"被一句 `FLOOR_S = 0` 静默取消（那等于回到旧行为）。"""
    assert hc.HUB_RECONNECT_FLOOR_S > 0
    assert hc.HUB_RECONNECT_FLOOR_S < hc.HUB_RECONNECT_BASE_S


def test_renew_is_wired_into_keepalive_and_off_the_receive_path():
    """防死码凑数：自动补发必须真接在活路径（保活 task）上。

    v1.7.50 起换码自检从 `_session_once` 挪进 `_keepalive_loop`：那是 HTTP，最坏等
    HUB_HTTP_TIMEOUT_S=15s，挡在接收循环前＝上线首 15s 内下行命令一律不被处理。
    所以两条钉：保活循环里必须有它（否则补发变死码），接收循环那条路上必须没有它。
    """
    tree = ast.parse(inspect.getsource(hc))
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            bodies[node.name] = any(
                isinstance(n, ast.Attribute) and n.attr == "_renew_bind_code_if_stale"
                for n in ast.walk(node)
            )
    assert bodies.get("_keepalive_loop") is True, "保活循环没接自动补发＝死码（面板会显示过期码）"
    assert bodies.get("_session_once") is False, \
        "_session_once 又内联换码了：HTTP 最坏 15s，会挡住接收循环（上线首 15s 下行命令不处理）"
    # 保活 task 必须真被会话拉起来（否则上面那条"接在保活里"等于没接）
    session_src = inspect.getsource(hc.HubClient._session_once)
    assert "_keepalive_loop()" in session_src, "_session_once 没起保活 task＝补发路径断了"
    assert "gather(flush, keepalive" in session_src, "保活 task 没被回收＝孤儿 task"


# ── 身份被云端拒绝后的自愈（v1.7.42）────────────────────────────────
# 线上事故形态：云托管重新部署 ⇒ HUB_STORE=/data/store.json（容器本地盘）被抹，
# hub 不再认识本机 instanceId/secret。此前客户端只能看到 ServerDisconnectedError
# ＝与网络抖动同形 ⇒ 抱着死身份无限重连，面板显示的是当前 hub 从未签发的码，
# 小程序侧必然 code_invalid 且永不自愈。判据只认握手返回的 HTTP 状态。
import aiohttp


class ScriptedSession(FakeSession):
    """ws_connect 按脚本失败；注册响应逐次不同（才能证明"换了新身份"）。"""

    def __init__(self, script, payloads=None):
        super().__init__()
        self.script = list(script)
        self.payloads = list(payloads or [])
        self.ws_attempts = 0

    def post(self, url, json=None):                                    # noqa: ARG002
        self.posted.append((url, json))
        if "register" in url and self.payloads:
            return FakeResp(self.payloads.pop(0))
        return FakeResp(self.register_payload)

    def ws_connect(self, url, headers=None, heartbeat=None):           # noqa: ARG002
        self.ws_attempts += 1
        self.ws_url = url
        self.ws_headers = headers
        kind = self.script.pop(0) if self.script else None
        if kind is None:
            return FakeCM(self.ws)
        raise kind


def _hs(status):
    return aiohttp.WSServerHandshakeError(
        request_info=None, history=(), status=status, message=str(status), headers=None)


def _ident(payload):
    return {"ok": True, "instanceId": payload, "secret": payload * 2, "bindCode": payload[-6:]}


def test_401_on_handshake_clears_identity_and_reregisters(tmp_path):
    s = ScriptedSession([_hs(401)], payloads=[_ident("i0000000aaaa"), _ident("i1111111bbbb")])
    client, _, _ = make_client(tmp_path, session=s)
    assert asyncio.run(client._session_once()) is True
    assert len(s.posted) == 2 and s.ws_attempts == 2, "被拒后应重注册一次再连，且只补一次"
    assert client.instance_id == "i1111111bbbb", "仍抱旧身份＝线上永不自愈的那条路"
    assert client.bind_code == "i1111111bbbb"[-6:], "换身份后必须带出新绑定码"
    # 真文件面：清过身份要落盘，否则 HA 重启又把死身份捡回来
    assert hc.load_identity(str(tmp_path))["instanceId"] == "i1111111bbbb"


def test_403_clears_identity_too(tmp_path):
    s = ScriptedSession([_hs(403)], payloads=[_ident("i0000000aaaa"), _ident("i2222222cccc")])
    client, _, _ = make_client(tmp_path, session=s)
    assert asyncio.run(client._session_once()) is True
    assert client.instance_id == "i2222222cccc"
    assert hc.IDENTITY_REJECTED_HTTP == (401, 403), "状态集合与实现脱钩＝判据漂移"


def test_non_identity_http_failure_keeps_identity(tmp_path):
    """503/500 一类"云端还在但没答上"绝不能触发重注册：每次抖动都换码＝用户手上的活码被作废。"""
    for status in (429, 500, 502, 503):
        cfg = tmp_path / ("st%s" % status)          # 各自一份身份盘：上一轮的落盘会让本轮直接复用身份
        cfg.mkdir(parents=True, exist_ok=True)
        s = ScriptedSession([_hs(status)], payloads=[_ident("i0000000aaaa"), _ident("i9999999zzzz")])
        client, _, _ = make_client(cfg, session=s)
        try:
            asyncio.run(client._session_once())
        except aiohttp.WSServerHandshakeError:
            pass
        assert client.instance_id == "i0000000aaaa", "状态 %s 竟清了身份" % status
        assert len(s.posted) == 1, "状态 %s 竟多发了注册请求" % status


def test_transport_level_failure_keeps_identity(tmp_path):
    """裸断/连不上（旧 hub 或网络故障）：只能继续抱身份退避，不能猜成"被拒"。"""
    for n, err in enumerate((aiohttp.ServerDisconnectedError(message="reset"),
                             aiohttp.ClientOSError(113, "No route to host"))):
        cfg = tmp_path / ("net%s" % n)
        cfg.mkdir(parents=True, exist_ok=True)
        s = ScriptedSession([err], payloads=[_ident("i0000000aaaa"), _ident("i9999999zzzz")])
        client, _, _ = make_client(cfg, session=s)
        try:
            asyncio.run(client._session_once())
        except Exception:  # noqa: BLE001 - 交给外层退避，正是期望行为
            pass
        assert client.instance_id == "i0000000aaaa", "%s 竟清了身份" % type(err).__name__
        assert len(s.posted) == 1


def test_invalidate_identity_writes_cleared_file(tmp_path):
    hc.save_identity(str(tmp_path), {"instanceId": "old", "secret": "s" * 32, "bindCode": "123456",
                                     "bindCodeAt": 111.0})
    client, _, _ = make_client(tmp_path)
    client.instance_id, client._secret, client.bind_code, client._bind_code_at = "old", "s" * 32, "123456", 111.0
    asyncio.run(client._invalidate_identity("测试"))
    assert (client.instance_id, client.bind_code, client._bind_code_at) == (None, None, 0.0)
    assert not hc.load_identity(str(tmp_path)).get("instanceId"), "死身份还在盘上＝重启后复活"
    assert client.status_view()["bindCodeExpired"] is False, "无码时不得渲染成『刚过期』"


def test_identity_io_is_off_the_event_loop(tmp_path):
    """v1.7.49：身份文件读写不得阻塞事件循环（HA 阻塞 IO 检测真机点名过 hub_client 的 open）。

    三条钉：① 三个身份方法是协程（回退成同步 def 即红）；② 类体内对模块级 sync
    worker 的引用必须包在 asyncio.to_thread 里（in-loop 直调 load/save_identity 即红；
    用 lookbehind 排除 self._load_identity() 这类方法调用的子串误配）；③ 真跑一次
    _save_identity 确能落盘（证明 to_thread 路径有效、不是空协程）。
    """
    import inspect
    import re
    client, _, _ = make_client(tmp_path)
    for name in ("_load_identity", "_save_identity", "_invalidate_identity"):
        assert inspect.iscoroutinefunction(getattr(client, name)), \
            "%s 回退成同步＝身份 I/O 又跑回事件循环" % name
    src = inspect.getsource(type(client))
    for worker in ("load_identity", "save_identity"):
        # to_thread 传的是函数引用（无括号），故匹配裸 token；lookbehind 排除
        # self._load_identity() / def _load_identity( 这类带下划线前缀的子串。
        hits = list(re.finditer(r"(?<![._\w])%s\b" % worker, src))
        assert hits, "类体内找不到 sync worker %s 的引用＝解析锚点漂移" % worker
        for m in hits:
            line = src[:m.start()].splitlines()[-1]
            assert "to_thread" in line, "sync worker 被 in-loop 直调: %r" % line
    client.instance_id, client._secret = "i1", "s" * 32
    client.bind_code, client._bind_code_at = "123456", 1.0
    asyncio.run(client._save_identity())
    assert hc.load_identity(str(tmp_path))["instanceId"] == "i1", "to_thread 落盘路径失效"


def test_run_forever_survives_loop_body_crash(monkeypatch, tmp_path, caplog):
    """v1.7.49 看门狗：主循环体自身抛异常不得让长连任务静默死。

    真机形态（v1.7.48）：hub pod 换版后循环体（退避计算/切片睡眠）抛一次异常 ⇒ 任务退出
    且无人重启 ⇒ agentsOnline 永久 0、只能人工重启集成。内层 try 只护 _session_once，
    护不住循环体其余行。钉：注入 hub_reconnect_delay 首调抛错，循环必须记 ERROR 并**继续**
    （_session_once 仍被再次调用），而不是静默退出。
    """
    import logging
    client, _, _ = make_client(tmp_path)
    calls = {"n": 0}

    async def fake_session_once():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError   # 第 3 次干净退出，结束本测试
        return False

    monkeypatch.setattr(client, "_session_once", fake_session_once)
    state = {"raised": False}

    def boom_delay(attempt):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("injected loop-body crash")
        return 0.0

    monkeypatch.setattr(hc, "hub_reconnect_delay", boom_delay)

    async def fast_sleep(d, stop):
        await asyncio.sleep(0)

    monkeypatch.setattr(hc, "interruptible_sleep", fast_sleep)
    with caplog.at_level(logging.ERROR):
        try:
            asyncio.run(asyncio.wait_for(client._run_forever(), timeout=5))
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    assert calls["n"] >= 3, "循环在注入崩溃后没继续＝看门狗失效（任务静默死）"
    assert state["raised"], "注入的崩溃没触发＝变异没生效"
    assert any("主循环自身异常" in r.getMessage() for r in caplog.records), \
        "看门狗兜住了但没记 ERROR＝下次真机仍无从排查"


def test_open_ws_is_the_only_reject_site_and_reconnects_once():
    """接线钉：握手只允许经 _open_ws（旁路 ws_connect＝自愈失效），且重注册后只再连一次。"""
    src = inspect.getsource(hc.HubClient._session_once)
    assert "self._open_ws(session)" in src, "_session_once 又直连 ws_connect 了"
    assert "session.ws_connect" not in src, "_session_once 里出现旁路握手"
    body = inspect.getsource(hc.HubClient._open_ws)
    assert body.count("session.ws_connect") == 2, "首连 + 被拒后重连，多一次就是重试风暴"


# ── 重注册熔断（防止无限造孤儿实例）───────────────────────────────
def _hs401():
    return _hs(401)


def test_reregister_fuse_stops_after_consecutive_rejections(tmp_path, caplog):
    """病态形态：register 落到容器 A、握手落到容器 B（云托管多副本且注册表不共享）。
    不熔断就是每轮退避白造一个新实例 + 反复作废用户手上的绑定码。"""
    import logging
    s = ScriptedSession([_hs401()] * 20, payloads=[_ident("i%07d%d" % (n, n)) for n in range(20)])
    client, _, _ = make_client(tmp_path, session=s)
    for _ in range(6):
        try:
            asyncio.run(client._session_once())
        except Exception:  # noqa: BLE001 - 交给外层退避
            pass
    assert len(s.posted) == 4, "注册次数 %d＝熔断没生效，会无限造孤儿实例" % len(s.posted)
    assert client._rereg_streak == hc.HUB_REREGISTER_FUSE
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("停止自动重注册" in m for m in errs), "熔断没有可见的 ERROR（现场只会看到反复换码）"
    joined = " ".join(errs)
    # 指路必须与 hub v0.2.6 的 README 同口径：注册表跨重建存活靠**云开发数据库镜像**，
    # 而「存储挂载 → 对象存储」在云开发桶上实测必失败（cosfs endpoint 缺 scheme ⇒ 挂载钩子
    # exit 1 ⇒ Pod 起不来）。旧话术正是叫人去配挂载——照着做比原故障更糟。所以这里必须钉
    # "提到挂载＝为了否定它"：只判 `"存储挂载" in joined` 对**旧的有害话术同样为真**＝假绿。
    assert "mirror.enabled" in joined, "熔断话术没指向真正的存活条件（hub 的注册表镜像）"
    assert "实例数" in joined, "熔断话术没给出多副本这个根因，运维还得自己猜"
    assert re.search(r"不要[^。；]*存储挂载", joined), \
        "熔断话术没有明确否定「存储挂载」（旧话术叫人去配它，会把 hub 搞成 Pod 起不来）"


def test_fuse_counter_resets_after_a_successful_session(tmp_path):
    """连上过就等于"云端确实认识当前身份"，计数必须归零——否则一次故障永久废掉自愈能力。"""
    s = ScriptedSession([_hs401()] * 20, payloads=[_ident("i%07da" % n) for n in range(20)])
    client, _, _ = make_client(tmp_path, session=s)
    for _ in range(4):
        try:
            asyncio.run(client._session_once())
        except Exception:  # noqa: BLE001
            pass
    assert client._rereg_streak == hc.HUB_REREGISTER_FUSE
    del s.script[:]                          # 接下来握手一律成功
    assert asyncio.run(client._session_once()) is True
    assert client._rereg_streak == 0, "连上后没清零＝熔断只许触发一次"
    s.script.append(_hs401())
    before = len(s.posted)
    try:
        asyncio.run(client._session_once())
    except Exception:  # noqa: BLE001
        pass
    assert len(s.posted) == before + 1, "清零后仍不再自愈＝回到 v1.7.41 那个永不恢复的状态"


# ── 安装级多网关聚合（v1.7.43）────────────────────────────────────
# 背景：HubClient 原来是"每网关条目一个实例"，N 台网关就注册 N 个实例、
# 抢同一份 /config/huijian_hub_identity.json（互相覆盖），HA 重启后全部
# 用同一个 instanceId 去连 → hub 的 onAgent 把前一条顶掉 → 重连战争，
# 而小程序只看到其中一个网关。现在改成"一个 HA 安装一个实例"，实例内部
# 聚合全部条目的 manager。
def test_state_items_cover_every_gateway_not_just_the_first(tmp_path):
    m1 = FakeManager(gateway_sn="GW1", devices={"A1B2": {"attributes": {"r_travel": 30}}})
    m2 = FakeManager(gateway_sn="GW2", devices={"C3D4": {"attributes": {"r_travel": 70}}})
    client, _, _ = make_client(tmp_path, managers=[m1, m2])
    items, _auth = client.build_state_snapshot()
    sns = sorted(i["sn"] for i in items)
    assert sns == ["A1B2", "C3D4"], "只聚合到一条网关＝小程序永远看不到全部设备: %s" % sns
    gw = {i["sn"]: i.get("gwSn") for i in items}
    assert gw == {"A1B2": "GW1", "C3D4": "GW2"}, "gwSn 串了＝小程序按 gwSn 分桶会把两台网关混成一台: %s" % gw


def test_status_listener_attached_to_every_manager_and_detached(tmp_path):
    m1, m2 = FakeManager(gateway_sn="GW1"), FakeManager(gateway_sn="GW2")
    client, _, _ = make_client(tmp_path, managers=[m1, m2])
    client.attach_managers([m1, m2])
    assert client._on_device_status in m1.listeners and client._on_device_status in m2.listeners, \
        "监听器没挂满每个 manager＝第二台网关的状态变化不会触发上行"
    client.attach_managers([m1])          # 条目被卸载后再聚合
    assert client._on_device_status not in m2.listeners, "撤掉的 manager 没摘监听＝回调持死对象"
    client.attach_managers([m1])
    assert m1.listeners.count(client._on_device_status) == 1, "重复 ensure 不得把同一回调挂两次"


def test_status_view_lists_all_gateways_and_keeps_gateway_sn_compat(tmp_path):
    m1 = FakeManager(gateway_sn="GW1", devices={"A1": {}, "A2": {}})
    m2 = FakeManager(gateway_sn="GW2", devices={"B1": {}})
    client, _, _ = make_client(tmp_path, managers=[m1, m2])
    v = client.status_view()
    assert [g["sn"] for g in v["gateways"]] == ["GW1", "GW2"]
    assert [g["deviceCount"] for g in v["gateways"]] == [2, 1]
    assert v["gatewaySn"] == "GW1", "gatewaySn 是旧消费方的兼容字段，必须仍在"


def test_register_payload_reports_a_gateway_sn(tmp_path):
    m1 = FakeManager(gateway_sn="GW1")
    client, _, session = make_client(tmp_path, managers=[m1, FakeManager(gateway_sn="GW2")])
    asyncio.run(client._ensure_registered())
    sent = [p for (_u, p) in session.posted if p and "installKey" in p]
    assert sent and sent[0]["sn"] == "GW1", "注册载荷 sn 缺失＝hub 侧实例无从辨认"


def test_no_manager_means_empty_items_and_no_crash(tmp_path):
    """所有条目都在卸载中：宁可回空列表，也不能抛（上行协程抛错会打断长连）。

    而且这个空**不权威**——"一个 manager 都没挂上"不等于"家里没有设备"，
    把它当全量交给 hub 就是让云端把全部设备判成已删除。
    """
    client, _, _ = make_client(tmp_path, managers=[])
    items, authoritative = client.build_state_snapshot()
    assert items == []
    assert authoritative is False, "零 manager 的空快照不得当全量承诺交出去"
    assert client.status_view()["gateways"] == []


# ── 期望集：残缺集合不得当权威上行（v1.8.5 审计 G-2）──────────────
# 背景（hub 侧一手）：server.js:477 以 Array.isArray(msg.items) 判"全量快照"，
# store.js:755-772 的 opts.snapshot 分支 `it.states = merged` 逐字写着"没出现在
# 本批的 sn 一律淘汰"。⇒ 加载项把缺了整台网关的集合当权威交出去，云端会**真删**
# 那台网关的全部设备（所有已绑定家人的小程序里一起消失）。触发面是 reload 的
# unload 尾巴与冷启动分步 setup——两条都是 `_setup_complete` 尚未落地的正常时序。
def test_partial_manager_set_is_not_authoritative(tmp_path):
    """该挂 2 个、只挂上 1 个 ⇒ 不权威（否则云端把那台网关的设备整片删掉）。"""
    m1 = FakeManager(gateway_sn="GW1", devices={"A1B2": {"attributes": {"r_travel": 30}}})
    m2 = FakeManager(gateway_sn="GW2", devices={"C3D4": {"attributes": {"r_travel": 70}}})
    client, _, _ = make_client(tmp_path, managers=[m1], expected_managers_fn=lambda: 2)
    items, authoritative = client.build_state_snapshot()
    assert [i["sn"] for i in items] == ["A1B2"], "可见的那台仍要构造出来（供恢复后比对）"
    assert authoritative is False, (
        "只挂上 1/2 个条目却判权威＝hub 按本批键集淘汰，把 GW2 的设备真删掉"
    )


def test_full_manager_set_stays_authoritative(tmp_path):
    """挂齐了就必须权威——否则已删设备永远淘汰不掉（回到幽灵设备）。"""
    m1 = FakeManager(gateway_sn="GW1", devices={"A1B2": {"attributes": {"r_travel": 30}}})
    m2 = FakeManager(gateway_sn="GW2", devices={"C3D4": {"attributes": {"r_travel": 70}}})
    client, _, _ = make_client(tmp_path, managers=[m1, m2], expected_managers_fn=lambda: 2)
    items, authoritative = client.build_state_snapshot()
    assert sorted(i["sn"] for i in items) == ["A1B2", "C3D4"]
    assert authoritative is True, "挂齐了还不权威＝v1.7.59 幽灵设备修复被推翻"


def test_entry_truly_removed_shrinks_expectation_and_stays_authoritative(tmp_path):
    """条目**真被删除**时期望数同步下降 ⇒ 仍须权威（该淘汰的要淘汰得掉）。

    这是反向臂：修复不能做成"只要 manager 变少就不推"——那样云端永远收不到
    "最后一台已删除"，幽灵设备回归。
    """
    m1 = FakeManager(gateway_sn="GW1", devices={})
    seen = {"n": 2}
    client, _, _ = make_client(tmp_path, managers=[m1], expected_managers_fn=lambda: seen["n"])
    _, authoritative = client.build_state_snapshot()
    assert authoritative is False, "2 个条目只挂上 1 个（reload 中）——不权威"
    seen["n"] = 1                      # 用户真把第二台删了：期望数随之降为 1
    items, authoritative = client.build_state_snapshot()
    assert items == []
    assert authoritative is True, (
        "真删条目后必须回到权威空快照，否则云端永远淘汰不掉已删网关（幽灵设备）"
    )


def test_partial_set_warns_once_on_shrink(tmp_path, caplog):
    """收缩告警必须能区分 reload 与真删（真删不吵；reload 留痕一次）。"""
    m1 = FakeManager(gateway_sn="GW1")
    m2 = FakeManager(gateway_sn="GW2")
    client, _, _ = make_client(tmp_path, managers=[m1, m2], expected_managers_fn=lambda: 2)
    client.attach_managers([m1])       # 期望数仍 2 ⇒ reload 形态，必须留痕
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "变少" in logs or "未挂上" in logs, (
        "reload 形态的残缺聚合必须留痕，否则现场无从归因'云端设备少了'"
    )
    caplog.clear()
    client2, _, _ = make_client(tmp_path, managers=[m1, m2], expected_managers_fn=lambda: 1)
    client2.attach_managers([m1])      # 期望数已降到 1 ⇒ 真删形态，不告警
    logs2 = " ".join(r.getMessage() for r in caplog.records)
    assert "变少" not in logs2, "真删条目还刷告警＝把正常淘汰当故障报"


def test_expected_manager_fn_failure_falls_back_to_old_behavior(tmp_path):
    """期望集取不到时按"无缺口"退回旧行为——不许把上行永久锁死。"""
    def boom():
        raise RuntimeError("stub 没接好")

    m1 = FakeManager(gateway_sn="GW1")
    client, _, _ = make_client(tmp_path, managers=[m1], expected_managers_fn=boom)
    items, authoritative = client.build_state_snapshot()
    assert items and authoritative is True, (
        "期望数取不到就永不上行＝远程控制通道整条哑掉（比原缺陷更重）"
    )


def test_lan_and_cloud_count_manager_criteria_are_aligned(tmp_path):
    """云侧期望数口径必须与 __init__ 的聚合口径同源（本仓"两条通道判据一致"纪律）。

    源码级判据：ensure 构造 HubClient 时必须注入 expected_managers_fn，且该回调
    指向 _hub_expected_manager_count（同一处定义"该有几个"）。
    """
    src = (Path(__file__).resolve().parents[1] / "custom_components"
           / "window_controller_gateway" / "__init__.py").read_text(encoding="utf-8")
    assert "expected_managers_fn=" in src, "少了期望集注入＝reload 窗口又把残缺集合当全量"
    assert "_hub_expected_manager_count(hass)" in src, "注入的不是聚合口径同一真源"
    assert "def _hub_expected_manager_count(" in src, "期望数判据必须与 _hub_managers 同处定义"


# ── 畸形帧不得打掉整条云长连（v1.8.5 审计 G-8）─────────────────────
def test_oversized_numeric_value_is_rejected_not_raised():
    """5000 位数字串必须被**拒绝**（回 None），不得让 int() 抛 ValueError。

    Python≥3.11 的十进制转换上限是 4300 位；两道正则都放行超长串，LAN 侧有
    1024B 帧闸兜着，云通道没有（ws_connect 未设 max_msg_size）⇒ 抛出去就是
    整条云长连断开、cmd_result 永不回。
    """
    huge = "9" * 5000
    assert hc.validate_control_params("w_travel", huge) is None, (
        "超长数字串必须被拒（int() 抛 ValueError 会冒泡断连）"
    )
    assert hc.validate_control_params("w_travel", "-" + huge) is None
    # 边界：合法线值不得被误伤
    assert hc.validate_control_params("w_travel", "100") == "100"
    assert hc.validate_control_params("w_travel", 0) == "0"
    assert hc.validate_control_params("w_travel", "101") == "101"
    # 值域闸仍在（超长串不是被"长度"单独放行的旁路）
    assert hc.validate_control_params("w_travel", "150") is None


def test_session_once_contains_handler_exception_and_still_replies(tmp_path, monkeypatch):
    """**真驱动**：一条命令处理抛异常 ⇒ 回 ok:false 且 `_session_once` 正常返回。

    这是 G-8 后半的行为臂——旧写法异常冒泡出本协程，外层按"连接异常"整条断开；
    钉源码字面量防不住"try 写了但包错语句"，所以这里让它真跑一遍。
    """
    cmd = {"t": "cmd", "cmdsn": "c1", "action": "control",
           "sn": "A1B2", "params": {"attribute": "w_travel", "value": "30"}}

    class Msg:
        type = 1  # aiohttp.WSMsgType.TEXT

        def __init__(self, data):
            self.data = json.dumps(data)

    ws = FakeWS(incoming=[Msg(cmd)])
    client, _, session = make_client(tmp_path, managers=[FakeManager()], session=FakeSession(ws=ws))

    async def boom(_msg):
        raise ValueError("stub 内部炸了")

    monkeypatch.setattr(client, "_handle_cmd", boom)
    ok = asyncio.run(client._session_once())
    assert ok is True, "单条命令异常不得被外层当成连接异常（会让整条长连重连）"
    replies = [p for p in ws.sent if p.get("t") == "cmd_result"]
    assert replies and replies[0]["ok"] is False and replies[0]["err"] == "handler_error", (
        "异常必须折成 ok:false 回执，否则小程序干等"
    )
