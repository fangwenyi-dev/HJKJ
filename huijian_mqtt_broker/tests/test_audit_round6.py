"""v1.6.19 第六轮审计修复批钉桩测试（四路只读审计 + 父代理一手复核）。

对应 findings（全部经读码实证，修复批见 CHANGELOG [1.6.19]）：
- A-HIGH1 显式 "data":null → 001 在 ack 前崩 → 网关无限重传毒报文：
  dispatch 入口归一化（非 dict → {}+告警），经 _subscribe_topics 闭包
  （生产同款注入路径）驱动验证。
- A-HIGH2 电压 "1e999"→float("inf")→int(inf) OverflowError 逃逸：
  _update_device_attributes / device_ws_view battery / _as_int 三面钉桩。
- A-LOW7 入站 >64KB 载荷拒收（闭包驱动）。
- A-MED1 _closing 闩锁：_schedule_reconnect 清理期不再拉起 task。
- A-LOW4 _norm_cmd_id 归一（"42"/42.0/True/None 全形态）。
- B-LOW11 set_position 非法/越界拒绝下发（不再静默回退 0=关窗反向动作）。
- A-MED2 _cmd_unbind sleep 后重解析条目 + 删除失败如实 ack（结构钉桩）。
- A-MED3/D-F3 _persist_token 三分支（命中回灌/无命中回滚/异常回滚）。
- A-LOW5 握手计数 finally 递减（CancelledError 不再泄漏槽位，结构钉桩）。
- A-LOW6 广播 task 登记 _bg_tasks + stop 取消（结构钉桩）。
- B-MED1 忽略按钮：SN 从 user_input 取（HA ignore_flow 另起新流实证），
  发现流补 unique_id。
- B-LOW7 add_gateway：options 保留 / 不再双 reload / 写 unique_id /
  uid 撞车 ValueError 回显。
- B-LOW8 无 SN 安装查重；B-LOW9 ensure 任意异常不阻塞安装。
- B-LOW10 WS 端口拒绝本栈保留口；strings 四文案补齐。
- B-MED2 persist 内层类型过滤（mapping 值 str、setpoints 值 dict）。
- B-MED3 cover is_closed 15 分钟时效闸（与 sensor 同判据）。
- B-LOW6 cover/number/sensor 移除 unique_id 优先（结构钉桩）。
- B-LOW4 duration schema 10-300；infra：config.yaml 主源回退 nju、
  ci.yaml warm arm64 映射/判空/单轮复查。

harness 形态沿袭 test_audit_round5（假 DM 只暴露真实被触到的契约面）。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.mqtt_handler as mh_mod
import custom_components.window_controller_gateway.config_flow as cf_mod
import custom_components.window_controller_gateway.discovery as disc_mod
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)
from custom_components.window_controller_gateway.ws_gateway import (
    WsGatewayServer,
    device_ws_view,
    _as_int,
)
from custom_components.window_controller_gateway.persist import (
    load_persistent_data,
    PERSISTENT_DATA_FILE,
)
from custom_components.window_controller_gateway.cover import (
    WindowControllerCover,
)
from custom_components.window_controller_gateway import const as c

GW_SN = "100122501207"
DEV_SN = "5005DEV00002"
HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"


class _MockDM:
    def __init__(self):
        self.devices = {}
        self.device_updates = []
        self.status_updates = []
        self.added = []
        self._next_number = 1
        self.entry = SimpleNamespace(options={})

    def get_device(self, sn):
        return self.devices.get(sn)

    def get_all_devices(self):
        return list(self.devices.values())

    def is_device_manually_removed(self, sn):
        return False

    def _notify_status_listeners(self, sn):
        pass

    def allocate_device_number(self):
        n = self._next_number
        self._next_number += 1
        return n

    async def add_device(self, sn, name, typ=None, force=False, is_manual_pairing=False):
        self.added.append(sn)
        self.devices[sn] = {"sn": sn, "name": name, "status": "connected",
                            "attributes": {}, "last_update": 0}
        return sn

    async def update_gateway_status(self, status):
        self.status_updates.append(status)

    async def update_device_status(self, sn, status, attributes=None):
        self.device_updates.append((sn, status, dict(attributes or {})))


class _Hass:
    def __init__(self, loop=None):
        self.data = {c.DOMAIN: {}}
        self.loop = loop
        self.config = SimpleNamespace(config_dir=".")

    def async_create_task(self, coro):
        if self.loop is not None and self.loop.is_running():
            return self.loop.create_task(coro)
        coro.close()
        return None

    def add_job(self, job, *args):
        if callable(job):
            return job(*args)
        return None


class _Publisher:
    def __init__(self):
        self.published = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))


def _mk_handler(monkeypatch):
    pub = _Publisher()
    monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
    handler = WindowControllerMQTTHandler(
        _Hass(loop=asyncio.get_running_loop()), GW_SN, _MockDM())
    handler.gateway_sn = GW_SN
    return handler, pub


async def _settle(rounds: int = 8):
    """让 _schedule_async_task 挂起的派发链跑完（任务→publish 至少 2 跳）。"""
    for _ in range(rounds):
        await asyncio.sleep(0.01)


async def _capture_rsp_cb(handler, monkeypatch):
    """从 _subscribe_topics 抓出生产闭包（handle_gateway_response）。"""
    captured = {}

    async def fake_sub(hass, topic, cb, qos):
        captured["cb"] = cb
        return lambda: None

    monkeypatch.setattr(mh_mod.mqtt, "async_subscribe", fake_sub)
    await handler._subscribe_topics()
    return captured["cb"]


# ============ A-HIGH1 / A-LOW7：闭包级入站闸与 data 归一化 ============
class TestInboundChokePoint:

    @pytest.mark.asyncio
    async def test_oversized_payload_rejected_without_publish(self, monkeypatch):
        handler, pub = _mk_handler(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        big = b'{"head":"' + b"x" * (70 * 1024) + b'"}'
        cb(SimpleNamespace(payload=big))          # 不得抛、不得下发任何东西
        await _settle()
        assert pub.published == []

    @pytest.mark.asyncio
    async def test_data_null_001_still_acks(self, monkeypatch):
        """显式 "data": null → 归一为 {}，001 走完并 ack（毒报文重传链封死）。"""
        handler, pub = _mk_handler(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"head": c.PROTOCOL_HEAD, "ctype": "001", "id": 7, "sn": GW_SN,
               "data": None}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        acks = [p for _, p in pub.published if p.get("ctype") == "001"]
        assert len(acks) == 1, "001 必须 ack 一次，否则网关无限重传"
        assert acks[0]["id"] == 7

    @pytest.mark.asyncio
    async def test_data_list_normalized_at_choke(self, monkeypatch):
        handler, pub = _mk_handler(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"head": c.PROTOCOL_HEAD, "ctype": "005", "id": 9, "sn": GW_SN,
               "data": []}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        acks = [p for _, p in pub.published if p.get("ctype") == "005"]
        assert len(acks) == 1


# ============ A-HIGH2：inf 电压簇 ============
class TestInfinityHardening:

    @pytest.mark.asyncio
    async def test_attr_update_inf_battery_and_travel(self, monkeypatch):
        handler, _ = _mk_handler(monkeypatch)
        dm = handler.device_manager
        dm.devices[DEV_SN] = {"sn": DEV_SN, "name": "d", "status": "unknown",
                              "attributes": {}, "last_update": 0}
        # 旧代码：float("1e999")=inf 合法通过，int(inf) OverflowError 逃逸
        await handler._update_device_attributes(
            DEV_SN, {"battery": "1e999", "r_travel": "1e999"})
        assert dm.device_updates == [], "inf 电压与 inf 位置都必须丢弃"
        # 同报文里合法字段不受毒值连累（round5 #2 契约保持）
        await handler._update_device_attributes(
            DEV_SN, {"battery": "1e999", "r_travel": "60"})
        assert dm.device_updates[-1] == (DEV_SN, "open", {"r_travel": 60})

    def test_as_int_inf_yields_minus_one(self):
        # 固件 -1=未知约定：inf（JSON 1e999 解析形态）不得炸穿
        assert _as_int(float("inf")) == -1
        assert _as_int("1e999") == -1
        assert _as_int(12.7) == 12
        assert _as_int(None) == -1

    def test_ws_view_battery_inf(self):
        dev = {"sn": DEV_SN, "status": "connected", "last_update": 0,
               "attributes": {"voltage": float("inf"), "r_travel": 50}}
        view = device_ws_view(DEV_SN, GW_SN, dev)
        assert view["battery"] == -1          # inf 电压 → -1 未知，不崩
        assert view["position"] == 50

    def test_ws_view_battery_string_inf(self):
        dev = {"sn": DEV_SN, "status": "connected", "last_update": 0,
               "attributes": {"voltage": "1e999", "r_travel": 0}}
        view = device_ws_view(DEV_SN, GW_SN, dev)
        assert view["battery"] == -1
        assert view["state"] == 0


# ============ A-MED1：_closing 闩锁 ============
class TestClosingLatch:

    @pytest.mark.asyncio
    async def test_schedule_reconnect_noop_when_closing(self, monkeypatch):
        handler, _ = _mk_handler(monkeypatch)
        handler._closing = True
        started = []
        real_create = asyncio.create_task

        def spy(coro, **kw):
            coro.close()
            started.append(kw.get("name"))
            return None

        monkeypatch.setattr("asyncio.create_task", spy)
        handler._schedule_reconnect()
        assert started == [], "_closing 后不得拉起重连任务"
        monkeypatch.setattr("asyncio.create_task", real_create)

    def test_cleanup_sets_closing_first(self):
        import inspect
        src = inspect.getsource(WindowControllerMQTTHandler.cleanup)
        assert "_closing = True" in src
        code_lines = [ln for ln in src.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        first_await = next((i for i, ln in enumerate(code_lines) if "await " in ln), None)
        close_line = next(i for i, ln in enumerate(code_lines) if "_closing = True" in ln)
        assert first_await is None or close_line < first_await, \
            "闩锁必须先于任何 await 让出点置位"


# ============ A-LOW4：_norm_cmd_id ============
class TestCmdIdNormalize:

    def test_table(self):
        f = WindowControllerMQTTHandler._norm_cmd_id
        assert f(12) == 12
        assert f(12.0) == 12
        assert f("12") == 12
        assert f("0012") == 12
        assert f(True) is None          # v1.7.18（BUG-14）：bool 归 None——原样透传时
        assert f(False) is None         # pop(True)==pop(1) 会命中 _bind_ops 键 1 的记账
        assert f(None) is None
        assert f("abc") == "abc"        # 不可归一 → 原样返回，pop 自然 miss
        assert f(12.5) == 12.5          # 非整 float 不猜测，原样交给 miss 分支

    @pytest.mark.asyncio
    async def test_003_float_id_echo_matches_bind_record(self, monkeypatch):
        handler, _ = _mk_handler(monkeypatch)
        handler._record_bind_op(42, "bind")
        # 网关以 JSON 浮点 echo id=42.0 —— 归一后命中记账
        await handler._handle_ctype_003(
            {"head": c.PROTOCOL_HEAD, "ctype": "003", "id": 42.0, "sn": GW_SN},
            "003", {"sn": DEV_SN, "errcode": 0, "bind": 1})
        assert DEV_SN in handler.device_manager.added


# ============ B-LOW11：控制参数拒绝面 ============
class TestControlParamReject:

    @pytest.mark.asyncio
    async def test_set_position_invalid_rejected(self, monkeypatch):
        handler, pub = _mk_handler(monkeypatch)
        assert await handler.send_command(DEV_SN, "set_position",
                                          {"position": "abc"}) is False
        assert await handler.send_command(DEV_SN, "set_position",
                                          {"position": float("inf")}) is False
        assert await handler.send_command(DEV_SN, "set_position",
                                          {"position": 150}) is False
        assert pub.published == [], "非法位置一律不得下发"
        assert await handler.send_command(DEV_SN, "set_position",
                                          {"position": 65}) is True
        assert pub.published[-1][1]["data"]["value"] == "65"

    @pytest.mark.asyncio
    async def test_speed_inf_rejected(self, monkeypatch):
        handler, pub = _mk_handler(monkeypatch)
        assert await handler.send_command(DEV_SN, "set_speed",
                                          {"speed": "1e999"}) is False
        assert pub.published == []

    @pytest.mark.asyncio
    async def test_speed_strength_out_of_range_rejected_not_clamped(self, monkeypatch):
        """v1.7.65：speed/strength 的越界由"裁剪后下发"改成拒绝——与本类 set_position
        的 B-LOW11 定案同口径。留裁剪等于留一条"用户要 150、设备收到 100、回执成功"的
        静默反向动作，而且与本仓新域表（越界即拒）在同一个仓库里判据分裂。
        """
        handler, pub = _mk_handler(monkeypatch)
        for command, key in (("set_speed", "speed"), ("set_strength", "strength")):
            assert await handler.send_command(DEV_SN, command, {key: 150}) is False, command
            assert await handler.send_command(DEV_SN, command, {key: -5}) is False, command
        assert pub.published == [], "越界不得下发（裁剪成 100/0 再发就是反向动作）"
        # 反向半条：合法值照常下发，且**不裁剪**（否则"一律拒"也能过上面几条）
        assert await handler.send_command(DEV_SN, "set_speed", {"speed": 0}) is True
        assert pub.published[-1][1]["data"]["value"] == "0"
        assert await handler.send_command(DEV_SN, "set_speed", {"speed": 100}) is True
        assert pub.published[-1][1]["data"]["value"] == "100"
        assert await handler.send_command(DEV_SN, "set_strength", {"strength": 37}) is True
        assert pub.published[-1][1]["data"]["attribute"] == c.ATTRIBUTE_WINACT_STRENGTH
        assert pub.published[-1][1]["data"]["value"] == "37"


# ============ A-MED3 / D-F3：_persist_token 三分支 ============
class TestPersistToken:

    def _srv(self, entries):
        hass = SimpleNamespace(
            data={c.DOMAIN: {}},
            config_entries=SimpleNamespace(
                async_entries=lambda d: entries,
                async_update_entry=lambda e, options=None: entries_updates.append(
                    (e.entry_id, options)),
            ),
        )
        srv = WsGatewayServer(hass, port=9001, token="old")
        return srv

    @pytest.mark.asyncio
    async def test_no_enabled_entry_rolls_back(self):
        global entries_updates
        entries_updates = []
        e = SimpleNamespace(entry_id="e1",
                            options={c.CONF_WS_GATEWAY_ENABLED: False})
        srv = self._srv([e])
        srv._token = "new"
        await srv._persist_token("new", "old")
        assert srv._token == "old", "零命中必须回滚（D-F3）"
        assert entries_updates == []

    @pytest.mark.asyncio
    async def test_success_writes_back_memory(self):
        global entries_updates
        entries_updates = []
        e = SimpleNamespace(entry_id="e1",
                            options={c.CONF_WS_GATEWAY_ENABLED: True,
                                     c.CONF_WS_GATEWAY_TOKEN: "old"})
        srv = self._srv([e])
        srv._token = "new"
        await srv._persist_token("new", "old")
        assert srv._token == "new"
        assert entries_updates and entries_updates[0][1][c.CONF_WS_GATEWAY_TOKEN] == "new"

    @pytest.mark.asyncio
    async def test_equal_options_still_syncs_memory(self):
        """早退分支（options 已等于新值）也必须回灌内存——热同步覆写窗口。"""
        global entries_updates
        entries_updates = []
        e = SimpleNamespace(entry_id="e1",
                            options={c.CONF_WS_GATEWAY_ENABLED: True,
                                     c.CONF_WS_GATEWAY_TOKEN: "new"})
        srv = self._srv([e])
        srv._token = "stale-by-hotsync"
        await srv._persist_token("new", "old")
        assert srv._token == "new"
        assert entries_updates == []   # 等值不重复写

    @pytest.mark.asyncio
    async def test_update_raises_rolls_back(self):
        def boom(entry, options=None):
            raise RuntimeError("registry down")
        hass = SimpleNamespace(
            data={c.DOMAIN: {}},
            config_entries=SimpleNamespace(
                async_entries=lambda d: [SimpleNamespace(
                    entry_id="e1", options={c.CONF_WS_GATEWAY_ENABLED: True})],
                async_update_entry=boom,
            ),
        )
        srv = WsGatewayServer(hass, port=9001, token="x")
        srv._token = "new"
        await srv._persist_token("new", "old")
        assert srv._token == "old"


# ============ A-MED2 / A-LOW5 / A-LOW6：结构钉桩（驱动需真实 aiohttp/条目栈） ============
class TestWsStructuralPins:

    def _src(self):
        return (PKG / "ws_gateway.py").read_text(encoding="utf-8")

    def test_unbind_reresolves_after_sleep(self):
        src = self._src()
        seg = src[src.index("async def _cmd_unbind"):src.index("async def _cmd_set_token")]
        assert "data2 = self._find_entry(gw_sn)" in seg
        assert seg.index("asyncio.sleep(GATEWAY_READY_DELAY)") < seg.index("data2 = self._find_entry")
        assert '"ok": False, "msg": "local delete failed"' in seg

    def test_handshake_count_in_finally(self):
        src = self._src()
        seg = src[src.index("_pending_handshakes += 1"):src.index("_clients.add(ws)")]
        assert "except BaseException" in seg and "raise" in seg, \
            "CancelledError 路径必须显式归还预约计数"

    def test_broadcast_tasks_tracked(self):
        src = self._src()
        assert "self._bg_tasks.add(task)" in src
        assert "task.add_done_callback(self._bg_tasks.discard)" in src
        seg = src[src.index("async def async_stop"):src.index("def _attach_listeners")]
        assert "_t.cancel()" in seg


# ============ B-MED1：忽略流程 SN 通道 ============
class TestIgnoreFlow:

    @pytest.mark.asyncio
    async def test_ignore_reads_user_input_unique_id(self, monkeypatch):
        called = {}

        async def fake_ignore(hass, sn):
            called["sn"] = sn

        monkeypatch.setattr(disc_mod, "async_ignore_gateway", fake_ignore)
        flow = object.__new__(cf_mod.ConfigFlow)
        flow.context = {}                      # 新 ignore 流的真实形态：context 为空
        flow.hass = SimpleNamespace()

        async def fake_uid(uid, raise_on_progress=True):
            flow.context["unique_id"] = uid

        flow.async_set_unique_id = fake_uid
        flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
        res = await flow.async_step_ignore({"unique_id": DEV_SN})
        assert called.get("sn") == DEV_SN, "user_input.unique_id 必须被采纳"
        assert isinstance(res, dict) and res.get("reason") == "ignored"

    def test_discovery_branch_sets_unique_id(self):
        src = (PKG / "config_flow.py").read_text(encoding="utf-8")
        seg = src[src.index("# 没有已配置的网关，进入添加流程"):
                  src.index("async def async_step_ignore")]
        assert "async_set_unique_id(gateway_sn.lower()" in seg

    @pytest.mark.asyncio
    async def test_context_fallback_still_works(self, monkeypatch):
        called = {}

        async def fake_ignore(hass, sn):
            called["sn"] = sn

        monkeypatch.setattr(disc_mod, "async_ignore_gateway", fake_ignore)
        flow = object.__new__(cf_mod.ConfigFlow)
        flow.context = {"gateway_sn": "CTX-SN-0001"}
        flow.hass = SimpleNamespace()

        async def fake_uid(uid, raise_on_progress=True):
            pass

        flow.async_set_unique_id = fake_uid
        flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
        await flow.async_step_ignore(None)
        assert called.get("sn") == "CTX-SN-0001"


# ============ B-LOW7：add_gateway 三连修 ============
class TestAddGateway:

    def _flow(self, monkeypatch, update_entry, clash=None):
        flow = object.__new__(cf_mod.OptionsFlow)
        entry = SimpleNamespace(entry_id="e1", data={}, entry_id_="e1",
                                options={"ws_gateway_port": 9100,
                                         "ws_gateway_token": "MyTok_12345678"})
        flow._config_entry = entry
        updated = {}

        def fake_update(e, *, data=None, unique_id=None):
            updated["data"] = data
            updated["unique_id"] = unique_id
            if update_entry is not None:
                update_entry(e, data=data, unique_id=unique_id)

        reload_calls = []
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda d: [],
                async_update_entry=fake_update,
                async_reload=reload_calls.append,
                # v1.6.26（D-2）：add_gateway 的 unique_id 撞车**前置判重**
                # 走此 API（真 HA 2026.x 的 async_update_entry 对重复 uid
                # 不抛异常——except ValueError 兜底永不触发，源码实证）。
                # clash=None 表示无撞车；用例注入 SimpleNamespace(entry_id=…)
                async_entry_for_domain_unique_id=lambda d, uid: clash,
            )
        )
        flow._reload_calls = reload_calls
        flow.async_create_entry = lambda title, data: {"type": "create", "data": data}
        flow.async_show_form = lambda **kw: {"type": "form", **kw}
        return flow, updated

    @pytest.mark.asyncio
    async def test_options_preserved_and_uid_written(self, monkeypatch):
        flow, updated = self._flow(monkeypatch, None)
        res = await flow.async_step_add_gateway(
            {c.CONF_GATEWAY_SN: "100122501208", c.CONF_GATEWAY_NAME: ""})
        assert res["type"] == "create"
        assert res["data"] == {"ws_gateway_port": 9100,
                               "ws_gateway_token": "MyTok_12345678"}, \
            "create_entry 必须原样保留 options（B-LOW7①）"
        assert updated["unique_id"] == "100122501208"
        assert updated["data"][c.CONF_GATEWAY_SN] == "100122501208"
        assert flow._reload_calls == [], "不得显式 reload（update listener 已覆盖，B-LOW7②）"

    @pytest.mark.asyncio
    async def test_uid_conflict_shows_error(self):
        """v1.6.26（D-2 订正钉桩）：真 HA 2026.x 撞车**不抛 ValueError**
        （_async_update_entry 只 error 日志后照写，源码实证）——查重必须
        由 async_entry_for_domain_unique_id 前置拦下。另一条目（entry_id
        不同）已占该 uid 时回显 already_configured 且**不写 update_entry**。"""
        written = []

        def spy_update(e, *, data=None, unique_id=None):
            written.append(unique_id)
        flow, _ = self._flow(None, spy_update,
                             clash=SimpleNamespace(entry_id="other-entry"))
        res = await flow.async_step_add_gateway(
            {c.CONF_GATEWAY_SN: "100122501208", c.CONF_GATEWAY_NAME: "x"})
        assert res["type"] == "form"
        assert res["errors"][c.CONF_GATEWAY_SN] == "already_configured"
        assert written == [], "撞车条目绝不允许被写入 uid/data"

    @pytest.mark.asyncio
    async def test_uid_clash_on_self_entry_passes(self):
        """撞车方就是本条目（重复提交同 SN）——前置判重按 entry_id 排除，
        不误伤正常保存路径（update listener 重载语义不受影响）。"""
        flow, updated = self._flow(
            None, None, clash=SimpleNamespace(entry_id="e1"))
        res = await flow.async_step_add_gateway(
            {c.CONF_GATEWAY_SN: "100122501208", c.CONF_GATEWAY_NAME: "x"})
        assert res["type"] == "create"
        assert updated["unique_id"] == "100122501208"


# ============ B-LOW8/9：无 SN 分支 ============
class TestNoSnInstall:

    @pytest.mark.asyncio
    async def test_second_no_sn_entry_aborts(self):
        flow = object.__new__(cf_mod.ConfigFlow)
        flow.context = {}
        # v1.7.12（CF-F1）：空 SN 分支现在创建/abort 前先清 unique_id——补桩
        async def _set_uid(uid, raise_on_progress=True):
            return None
        flow.async_set_unique_id = _set_uid
        existing = SimpleNamespace(entry_id="e0", data={}, options={})
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda d: [existing]))
        flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
        res = await flow.async_step_user({c.CONF_GATEWAY_SN: "",
                                          c.CONF_GATEWAY_NAME: ""})
        assert res == {"type": "abort", "reason": "already_configured"}

    @pytest.mark.asyncio
    async def test_ensure_any_exception_does_not_block(self, monkeypatch):
        async def boom(hass):
            raise RuntimeError("exploded")
        monkeypatch.setattr(cf_mod, "ensure_mqtt_connection", boom)
        flow = object.__new__(cf_mod.ConfigFlow)
        flow.context = {}
        # v1.7.12（第 6 轮审计 CF-F1 钉桩）：空 SN 引导条目创建前必须清除流
        # 实例上的 unique_id——否则"输 SN→测试失败→返回修改→清空提交"会造出
        # data={} 但继承该 SN unique_id 的幽灵占坑条目，SN 后续发现/手动添加
        # 永久 already_configured 且界面无从看出
        cleared = []
        async def _set_uid(uid, raise_on_progress=True):
            cleared.append(uid)
        flow.async_set_unique_id = _set_uid
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda d: []))
        flow.async_create_entry = lambda title, data: {"type": "create", "data": data}
        res = await flow.async_step_user({c.CONF_GATEWAY_SN: "",
                                          c.CONF_GATEWAY_NAME: ""})
        assert res["type"] == "create", "引导异常不得打穿『不阻塞安装』承诺"
        assert cleared == [None], "空 SN 分支必须 async_set_unique_id(None)（CF-F1）"


# ============ B-LOW10：端口保留位 + strings ============
class TestReservedPortsAndStrings:

    def test_const_set(self):
        assert {2022, 10998, 8123, 1883} == set(c.WS_RESERVED_PORTS)

    @pytest.mark.asyncio
    async def test_options_rejects_reserved_port(self):
        flow = object.__new__(cf_mod.OptionsFlow)
        flow._config_entry = SimpleNamespace(
            options={}, data={}, entry_id="e1")
        flow.async_show_form = lambda **kw: {"type": "form", **kw}
        flow.async_create_entry = lambda title, data: {"type": "create", "data": data}
        res = await flow.async_step_options({
            "discovery_interval": 300, "auto_discovery": True,
            "debug_logging": False,
            c.CONF_WS_GATEWAY_ENABLED: True,
            c.CONF_WS_GATEWAY_PORT: 2022,
            c.CONF_WS_GATEWAY_TOKEN: c.DEFAULT_WS_GATEWAY_TOKEN,
        })
        assert res["type"] == "form"
        assert res["errors"][c.CONF_WS_GATEWAY_PORT] == "ws_port_reserved"

    def test_strings_have_new_error_keys(self):
        keys = {"invalid_ws_token", "ws_port_reserved", "required",
                "already_configured"}
        for rel in ("strings.json", "translations/zh-CN.json"):
            s = json.loads((PKG / rel).read_text(encoding="utf-8"))
            missing = keys - set(s["config"]["error"])
            assert not missing, f"{rel} 缺错误文案: {missing}"


# ============ B-MED2：persist 内层过滤 ============
class TestPersistInnerTypes:

    def _load(self, tmp_path, data):
        (tmp_path / PERSISTENT_DATA_FILE).write_text(
            json.dumps(data), encoding="utf-8")

        class _PH:
            def __init__(self, config_dir):
                self.config = SimpleNamespace(config_dir=str(config_dir))
                self.data = {c.DOMAIN: {}}

            async def async_add_executor_job(self, fn, *args):
                return fn(*args)

        hass = _PH(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(load_persistent_data(hass))
        finally:
            loop.close()
        return hass

    def test_mapping_and_setpoints_filtered(self, tmp_path):
        hass = self._load(tmp_path, {
            "device_to_gateway_mapping": {"G1": "100122501207", "G2": 50},
            "manually_removed_devices": ["G3", None],
            "device_setpoints": {"G1": {"speed": 50}, "G2": 50, "G3": None},
        })
        m = hass.data[c.DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING]
        assert m == {"G1": "100122501207"}
        sp = hass.data[c.DOMAIN][c.DEVICE_SETPOINTS]
        assert sp == {"G1": {"speed": 50}}
        assert hass.data[c.DOMAIN][c.GLOBAL_MANUALLY_REMOVED_DEVICES] == {"G3"}


# ============ B-MED3：cover 时效闸 ============
class TestCoverStalenessGate:

    class _DM:
        def __init__(self, dev):
            self._d = dev

        def get_device(self, sn):
            return self._d

    def _cover(self, status, last_update):
        return WindowControllerCover(
            hass=None, device_manager=self._DM(
                {"sn": DEV_SN, "status": status, "attributes": {},
                 "last_update": last_update}),
            mqtt_handler=None, gateway_sn="GW1", device_sn=DEV_SN,
            device_name="窗")

    def test_fresh_report_honored(self):
        import time as _t
        assert self._cover("closed", _t.time()).is_closed is True
        assert self._cover("open", _t.time()).is_closed is False

    def test_stale_report_gates_to_unknown(self):
        old = 1000.0     # 远古时间戳 ≫ 15 分钟
        assert self._cover("closed", old).is_closed is None
        assert self._cover("open", old).is_closed is None

    def test_no_timestamp_treated_fresh(self):
        assert self._cover("closed", None).is_closed is True
        assert self._cover("open", 0).is_closed is False


# ============ B-LOW4 / B-LOW5 / B-LOW6：静态钉桩簇 ============
class TestStaticPins:

    def test_duration_schema_range(self):
        src = (PKG / "services.py").read_text(encoding="utf-8")
        anchor = src.index('vol.Optional("duration", default=GATEWAY_PAIRING_TIMEOUT)')
        seg = src[anchor:anchor + 400]
        assert "vol.Range(min=10, max=300)" in seg
        assert "cv.positive_int" in seg

    def test_yaml_honest_descriptions(self):
        src = (PKG / "services.yaml").read_text(encoding="utf-8")
        assert "空操作" in src and "本地兜底超时" in src

    def test_removal_unique_id_first(self):
        cases = [
            ("cover.py", 'await _aget_eid(hass, "cover"'),
            ("number.py", 'await _aget_eid(hass, "number"'),
            ("sensor.py", 'await _aget_eid(hass, "sensor"'),
        ]
        for fname, call in cases:
            src = (PKG / fname).read_text(encoding="utf-8")
            assert call in src, f"{fname} 移除路径缺 unique_id 优先定位"
            assert "双路径均未命中" in src, f"{fname} 缺双落空留痕告警"

    def test_config_image_domain_in_proven_candidate_set(self):
        src = (HERE.parent / "config.yaml").read_text(encoding="utf-8")
        img = [ln for ln in src.splitlines() if ln.startswith("image:")][0]
        # 钉"域名 ∈ 实测候选集"而非钉具体主源——主源定案随网络环境演变
        # （nju→1ms→nju→源站→v1.7.17 起 1ms），具体主源以 config.yaml
        # 「镜像主源演变史」注释为准；本测试只拦"漂到从未实测过的野域名"
        # 与路径模板丢失（旧版把 v1.6.20 定案写死，导致每次迁主源都要改测试）。
        assert any(d in img for d in
                   ("ghcr.1ms.run/", "ghcr.nju.edu.cn/", "ghcr.io/")), \
            f"image 域超出实测候选集: {img}"
        assert "/fangwenyi-dev/{arch}-huijian-mqtt-broker" in img, \
            "image 路径模板漂移（{arch} 占位必须保留）"
        import re as _re
        assert _re.search(r'^version: "\d+\.\d+\.\d+"$', src, _re.M), \
            "config.yaml 缺规范 version 字段（v1.6.25 起动态断言，bump 不再改本测试）"

    def test_ci_warm_arm64_mapping_and_guards(self):
        src = (HERE.parent.parent / ".github" / "workflows" / "ci.yaml").read_text(
            encoding="utf-8")
        assert 'echo arm64 || echo "$ARCH"' in src
        seg = src.split("warm-mirrors")[1]
        assert 'ms[0]["digest"]' not in seg, "盲取 ms[0] 回退代码必须移除"
        assert '[ -z "$MAN" ]' in src and '[ -z "$BLOBS" ]' in src

    def test_version_files_consistent(self):
        import re as _re
        # 权威源=config.yaml 的 version（v1.6.25 起动态提取，防 bump 遗漏）
        cfg = (HERE.parent / "config.yaml").read_text(encoding="utf-8")
        ver = _re.search(r'^version: "(\d+\.\d+\.\d+)"$', cfg, _re.M).group(1)
        assert ver in (HERE.parent / "www" / "version.json").read_text(encoding="utf-8")
        assert f"CURRENT_VERSION = '{ver}'" in (HERE.parent / "www" / "index.html").read_text(encoding="utf-8")
        mf = json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))
        assert mf["version"] == ver

    def test_claude_wire_values_fixed(self):
        src = (HERE.parent.parent / "CLAUDE.md").read_text(encoding="utf-8")
        assert '| `open` | "100" |' in src
        assert '| `close` | "0" |' in src
        assert '| `stop` | "101" |' in src
