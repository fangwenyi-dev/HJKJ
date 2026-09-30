"""v1.6.12 第五轮审计修复批的钉桩测试。

对应 findings（全部经父代理逐条读码实证）：
- #1 005 毒消息：attrs 非列表/元素 null → ack 必达（防网关无限重传）
- #2 002 属性转换 TypeError 吞噬 + gather 异常静默
- #3 新配对取代陈旧 bind 记账（v1.6.11 #2 的残留窗口收口）
- #4 auto_discovery 选项真实接线（002 自动添加门控）
- #5 cover 注册/摘除设备状态回调（对齐 number/sensor，v1.6.8 定案的实时性）
- #6 via_device/config_entry_ids 死属性读簇根治 + 静态扫描防复发
      （v1.6.0 "entity" 字面量同族教训：假 mock 带真机没有的属性骗过全部测试）
- #7 sensor 时效契约：改读设备缓存 last_update，陈旧值转 unknown
- #8 button 基础按钮清理脱离删除按钮总闸
- #9 options 表单 gateway_sn 死控件移除（schema 键=真实消费键）
- #10 persist：主文件缺失走 .bak 救援；字段类型畸形丢弃不逃逸
- Web：abort reason 必须是 Error（字符串 reason 规范下原值 reject，
      e.message 恒 undefined）；fetchT 超时须覆盖 body 读取
- infra：config.yaml ssl 映射移除；strings/zh-CN options 真实步骤文案

真实设备（DeviceEntry）形态约束：假设备一律**不带** via_device 属性，
只有 via_device_id（str 或旧版 tuple (entry_id, device_id)）。
"""
import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import tokenize
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.mqtt_handler as mh_mod
import custom_components.window_controller_gateway.button as button_mod
import custom_components.window_controller_gateway.cover as cover_mod
import custom_components.window_controller_gateway.utils as gw_utils
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)
from custom_components.window_controller_gateway.device_manager import (
    WindowControllerDeviceManager,
)
from custom_components.window_controller_gateway.persist import (
    load_persistent_data,
    PERSISTENT_DATA_FILE,
)
from custom_components.window_controller_gateway.sensor import (
    WindowControllerBatterySensor,
    WindowControllerStatusSensor,
)
from custom_components.window_controller_gateway.config_flow import OptionsFlow
from custom_components.window_controller_gateway import const as c

GW_SN = "100122501207"
DEV_SN = "5002DEV00001"
HERE = Path(__file__).resolve().parent


class _MockDM:
    """镜像真实 device_manager 被触到的契约面；刻意提供 entry 属性（真实 DM 有）"""

    def __init__(self, options=None):
        self.devices = {}
        self._manually_removed_devices = set()
        self.status_updates = []
        self.device_updates = []
        self.added = []
        self._next_number = 1
        self.entry = SimpleNamespace(options=options if options is not None else {})

    def get_device(self, sn):
        return self.devices.get(sn)

    def get_all_devices(self):
        return list(self.devices.values())

    def _notify_status_listeners(self, sn):
        # v1.6.17 联审F2：003 绑定确认后新增的 WS device_update 通知
        pass

    def is_device_manually_removed(self, sn):
        return sn in self._manually_removed_devices

    def allocate_device_number(self):
        n = self._next_number
        self._next_number += 1
        return n

    async def add_device(self, sn, name, typ=None, force=False, is_manual_pairing=False):
        self.added.append(sn)
        self.devices[sn] = {"sn": sn, "name": name, "status": "connected", "attributes": {}}
        return sn

    async def update_gateway_status(self, status):
        self.status_updates.append(status)

    async def update_device_status(self, sn, status, attributes=None):
        self.device_updates.append((sn, status, dict(attributes or {})))

    def get_gateway_info(self):
        return {"name": "GW", "status": "online"}


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
    """假 mqtt.async_publish：按协议 JSON 解析并记录"""

    def __init__(self):
        self.published = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))


def _payload(cmd_id, device_sn, errcode=0):
    return {
        "head": c.PROTOCOL_HEAD,
        "ctype": "003",
        "id": cmd_id,
        "sn": GW_SN,
        "data": {"sn": device_sn, "errcode": errcode, "devtype": "curtain_ctr"},
    }


# ============ #1 005 毒消息 → ack 必达 ============
class Test005PoisonMessageAlwaysAcks:
    @pytest.mark.asyncio
    async def test_non_list_attrs_ack_still_sent(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, _MockDM())
        payload = {"head": c.PROTOCOL_HEAD, "ctype": "005", "id": 12, "sn": GW_SN,
                   "data": {"sn": DEV_SN, "attrs": 5}}  # 数字 attrs → for 循环 TypeError
        await handler._handle_ctype_005(payload, "005", payload["data"])  # 不得抛
        acks = [p for _, p in pub.published if p.get("ctype") == "005"]
        assert len(acks) == 1, "毒 005 必须恰好 ack 一次（防网关无限重传）"
        assert acks[0]["id"] == 12 and acks[0]["data"]["errcode"] == 0

    @pytest.mark.asyncio
    async def test_null_attr_element_ack_still_sent(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, _MockDM())
        payload = {"head": c.PROTOCOL_HEAD, "ctype": "005", "id": 13, "sn": GW_SN,
                   "data": {"sn": DEV_SN, "attrs": [None]}}  # 元素 null → .get AttributeError
        await handler._handle_ctype_005(payload, "005", payload["data"])
        assert sum(1 for _, p in pub.published if p.get("ctype") == "005") == 1

    @pytest.mark.asyncio
    async def test_non_dict_data_ack_still_sent(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, _MockDM())
        payload = {"head": c.PROTOCOL_HEAD, "ctype": "005", "id": 14, "sn": GW_SN,
                   "data": 42}  # data 非 dict → 首行 data.get 即崩，仍需 ack
        await handler._handle_ctype_005(payload, "005", payload["data"])
        assert sum(1 for _, p in pub.published if p.get("ctype") == "005") == 1

    @pytest.mark.asyncio
    async def test_good_005_acks_exactly_once(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        dm = _MockDM()
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm)
        payload = {"head": c.PROTOCOL_HEAD, "ctype": "005", "id": 15, "sn": GW_SN,
                   "data": {"sn": DEV_SN, "attrs": [{"attribute": "r_travel", "value": 65}]}}
        await handler._handle_ctype_005(payload, "005", payload["data"])
        assert len(pub.published) == 1, "正常路径也恰好一次 ack（防重构后双发）"
        assert dm.device_updates == [(DEV_SN, c.DEVICE_STATUS_OPEN, {"r_travel": 65})]


# ============ #2 002 转换 TypeError + gather 日志 ============
class Test002ConversionHardening:
    @pytest.mark.asyncio
    async def test_null_battery_does_not_swallow_r_travel(self):
        dm = _MockDM()
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm)
        # battery=null：float(None) TypeError——修复前炸掉整个协程，
        # r_travel 与其后的状态更新连带丢失且被 gather 静默
        await handler._update_device_attributes(DEV_SN, {"battery": None, "r_travel": "65"})
        assert dm.device_updates == [(DEV_SN, c.DEVICE_STATUS_OPEN, {"r_travel": 65})], \
            "battery 畸形不得连带丢失 r_travel 更新（真实参数断言）"

    @pytest.mark.asyncio
    async def test_batch_logs_subtask_exceptions(self, caplog):
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, _MockDM())

        async def boom():
            raise TypeError("fake silent failure")

        with caplog.at_level(logging.WARNING):
            await handler._batch_process_tasks([boom()], "更新设备状态")
        assert any("子任务异常" in r.getMessage() and "TypeError" in r.getMessage()
                   for r in caplog.records), "gather 的异常内容必须落日志，不得只计数"


# ============ #3 新配对取代陈旧 bind 记账 ============
class TestNewPairingSupersedesStaleBind:
    @pytest.mark.asyncio
    async def test_second_pairing_purges_stale_and_late_ack_keeps_session(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        dm = _MockDM()
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm)
        handler.connected = True

        assert await handler.send_command(GW_SN, "start_pairing") is True
        id1 = pub.published[-1][1]["id"]
        assert handler._bind_ops[id1] == ("bind", None)
        assert await handler.send_command(GW_SN, "start_pairing") is True
        id2 = pub.published[-1][1]["id"]
        assert id1 not in handler._bind_ops, "新配对必须取代陈旧 bind 记账（None 设备SN永不清理的死账）"
        assert handler._bind_ops[id2] == ("bind", None)

        # 会话 2 进行中：会话 1 的迟到确认到达
        handler.pairing_active = True
        cancelled = []
        handler.pairing_timeout_handle = SimpleNamespace(cancel=lambda: cancelled.append(1))
        p = _payload(id1, "5002LATE0001")
        await handler._handle_ctype_003(p, "003", p["data"])
        assert dm.added == ["5002LATE0001"], "绑定确认事实仍须添加设备"
        assert handler.pairing_active is True, "陈旧会话的迟到确认不得掐掉当前会话"
        assert cancelled == []
        assert dm.status_updates == []


# ============ #4 auto_discovery 门控 ============
def _payload_002(dev_sn):
    return {
        "head": c.PROTOCOL_HEAD, "ctype": "002", "id": 3, "sn": GW_SN,
        "data": {"devices": [{"sn": dev_sn, "model": "win", "vesion": "1.0",
                              "r_travel": "65"}]},
    }


class TestAutoDiscoveryGate:
    @pytest.mark.asyncio
    async def test_off_skips_auto_add_on_002(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        dm = _MockDM(options={c.CONF_AUTO_DISCOVERY: False})
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm)
        await handler._handle_ctype_002(*(_args(_payload_002("5002NEW00099"))))
        assert dm.added == [], "auto_discovery=False 时 002 未知设备不得自动添加"
        assert sum(1 for _, p in pub.published if p.get("ctype") == "002") == 1, \
            "跳过添加仍须 ack（否则网关重传风暴）"

    @pytest.mark.asyncio
    async def test_default_true_preserves_behavior(self, monkeypatch):
        pub = _Publisher()
        monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
        dm = _MockDM()  # options 无该键 → 默认 True（历史行为）
        handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm)
        await handler._handle_ctype_002(*(_args(_payload_002("5002NEW00099"))))
        assert dm.added == ["5002NEW00099"]
        assert dm.device_updates == [("5002NEW00099", c.DEVICE_STATUS_OPEN, {"r_travel": 65})]


def _args(payload):
    return (payload, payload["ctype"], payload["data"])


# ============ #6 via_device 兼容层 + 真实形态行为测试 + 静态防复发 ============
class TestViaDeviceCompat:
    def test_get_via_device_id_forms(self):
        assert gw_utils.get_via_device_id(SimpleNamespace(via_device_id="abc")) == "abc"
        assert gw_utils.get_via_device_id(
            SimpleNamespace(via_device_id=("entry1", "abc"))) == "abc"
        assert gw_utils.get_via_device_id(SimpleNamespace(via_device_id=None)) is None
        # 真实 DeviceEntry 形态：只有 via_device_id，没有 via_device
        assert gw_utils.get_via_device_id(SimpleNamespace()) is None

    def test_get_device_config_entry_ids_dual(self):
        assert gw_utils.get_device_config_entry_ids(
            SimpleNamespace(config_entries={"a", "b"})) == {"a", "b"}
        assert gw_utils.get_device_config_entry_ids(
            SimpleNamespace(config_entry_id="c")) == {"c"}
        assert gw_utils.get_device_config_entry_ids(
            SimpleNamespace(config_entries={"a"}, config_entry_id="b")) == {"a", "b"}
        assert gw_utils.get_device_config_entry_ids(SimpleNamespace()) == set()

    @pytest.mark.asyncio
    async def test_gateway_child_lookup_with_real_device_shapes(self):
        """_get_gateway_devices_from_registry：修复前恒返回 []（死属性）。

        假设备只带 via_device_id（真实 DeviceEntry 属性名）——新旧两代值形态
        （str / tuple）都必须命中；挂在其他网关下的设备不得混入。
        """
        gw = SimpleNamespace(id="GWID", identifiers={(c.DOMAIN, GW_SN)})
        other = SimpleNamespace(id="OTHER", identifiers={(c.DOMAIN, "100199999999")})
        child_new = SimpleNamespace(id="D1", identifiers={(c.DOMAIN, "5002A0000001")},
                                    via_device_id="GWID")
        child_old_tuple = SimpleNamespace(id="D2", identifiers={(c.DOMAIN, "5002A0000002")},
                                          via_device_id=("some_entry", "GWID"))
        child_foreign = SimpleNamespace(id="D3", identifiers={(c.DOMAIN, "5002A0000003")},
                                        via_device_id="OTHER")
        devices = {d.id: d for d in (gw, other, child_new, child_old_tuple, child_foreign)}

        class _Reg:
            def __init__(self):
                self.devices = devices

            def async_get_device(self, identifiers=None):
                for d in devices.values():
                    if identifiers and identifiers <= d.identifiers:
                        return d
                return None

        dm = WindowControllerDeviceManager.__new__(WindowControllerDeviceManager)
        reg = _Reg()

        async def fake_get_registry():
            return reg

        dm._get_device_registry = fake_get_registry
        result = await dm._get_gateway_devices_from_registry(GW_SN)
        assert sorted(result) == ["5002A0000001", "5002A0000002"], \
            "本网关子设备（str 与 tuple 两代 via_device_id 形态）必须全部命中，他网关的必须排除"

    def test_no_dead_registry_attribute_reads_in_integration(self):
        """静态钉桩：集成源码禁止再以 `.via_device`/`.config_entry_ids` 属性形态
        或字符串字面量读取（v1.6.0 "entity" 字面量同族教训——假 mock 骗过全部
        测试的根因是代码读真机不存在的属性）。tokenize 扫描：注释/文档串豁免，
        只看代码 token；`via_device=` 入参（async_get_or_create 合法形参）不在
        匹配模式内。v1.7.31 追加豁免：紧随 `:` 的 `"via_device"` 是 dict 键
        （utils.via_device_kwargs 旧 HA 双形态出口），非 getattr 死属性读取——
        后者后随 token 为 `,`/`)`，仍然禁。
        """
        integration_dir = HERE.parent / "custom_components" / "window_controller_gateway"
        bad = []
        forbidden_names = {"via_device", "config_entry_ids"}
        for py in sorted(integration_dir.glob("*.py")):
            src = py.read_bytes()
            toks = list(tokenize.tokenize(io.BytesIO(src).readline))
            for i, t in enumerate(toks):
                if t.type == tokenize.OP and t.string == "." and i + 1 < len(toks):
                    nxt = toks[i + 1]
                    if nxt.type == tokenize.NAME and nxt.string in forbidden_names:
                        bad.append(f"{py.name}:{t.start.line}: .{nxt.string}")
                elif t.type == tokenize.STRING:
                    literal = t.string.strip()
                    for q in ('"', "'", '"""', "'''"):
                        if literal.startswith(q) and literal.endswith(q):
                            literal = literal[len(q):-len(q)]
                            break
                    if literal in forbidden_names:
                        nxt = toks[i + 1] if i + 1 < len(toks) else None
                        is_dict_key = (nxt is not None and nxt.type == tokenize.OP
                                       and nxt.string == ":")
                        if is_dict_key and literal == "via_device":
                            continue  # via_device_kwargs 双形态出参（合法入参名）
                        bad.append(f"{py.name}:{t.start.line}: {t.string}")
        assert bad == [], f"死属性读回潮（用 utils.get_via_device_id / get_device_config_entry_ids）: {bad}"


# ============ #7 sensor 时效契约 ============
class TestSensorStalenessContract:
    @staticmethod
    def _dm_with(dev):
        class _DM:
            def get_device(self, sn):
                return dev
        return _DM()

    def test_battery_fresh_value_shown(self):
        import time as _t
        dev = {"attributes": {"voltage": 10.5}, "last_update": _t.time()}
        s = WindowControllerBatterySensor(None, self._dm_with(dev), GW_SN, DEV_SN, "D")
        assert s._attr_native_value == 10.5

    def test_battery_stale_value_expires(self):
        import time as _t
        dev = {"attributes": {"voltage": 10.5},
               "last_update": _t.time() - (c.SENSOR_TIMEOUT_MINUTES * 60 + 60)}
        s = WindowControllerBatterySensor(None, self._dm_with(dev), GW_SN, DEV_SN, "D")
        assert s._attr_native_value is None, \
            "超过 SENSOR_TIMEOUT_MINUTES 无上报必须转 unknown（修复前读缓存即刷新时效戳，永不过期）"

    def test_status_stale_value_expires(self):
        import time as _t
        dev = {"status": "open", "attributes": {"r_travel": 65},
               "last_update": _t.time() - (c.SENSOR_TIMEOUT_MINUTES * 60 + 60)}
        s = WindowControllerStatusSensor(None, self._dm_with(dev), GW_SN, DEV_SN, "D")
        assert s._attr_native_value is None

    def test_status_fresh_value_shown(self):
        import time as _t
        dev = {"status": "open", "attributes": {}, "last_update": _t.time()}
        s = WindowControllerStatusSensor(None, self._dm_with(dev), GW_SN, DEV_SN, "D")
        assert s._attr_native_value == "open"


# ============ #5 cover 状态回调注册/摘除 ============
class TestCoverStatusCallback:
    @pytest.mark.asyncio
    async def test_setup_add_and_remove_lifecycle(self, monkeypatch):
        calls = {"added": [], "removed": []}

        class _MH:
            def add_status_callback(self, sn, cb):
                calls["added"].append(sn)

            def remove_status_callback(self, sn, cb):
                calls["removed"].append(sn)

        dev = {"sn": DEV_SN, "name": "W1", "type": c.DEVICE_TYPE_WINDOW_OPENER,
               "status": "closed", "attributes": {}}

        class _DM:
            def __init__(self):
                self._added_cb = None
                self._removed_cb = None

            def get_all_devices(self):
                return [dict(dev)]

            def get_device(self, sn):
                return dev if sn == DEV_SN else None

            def set_device_added_callback(self, cb):
                self._added_cb = cb

            def set_device_removed_callback(self, cb):
                self._removed_cb = cb

        # 注册表查重回退 None → 新设备视为未存在，走创建路径
        async def fake_eid(hass, domain, uid):
            return None

        monkeypatch.setattr(gw_utils, "async_get_entity_id", fake_eid)

        dm = _DM()
        hass = SimpleNamespace(data={c.DOMAIN: {"e1": {"device_manager": dm,
                                                       "mqtt_handler": _MH()}}})
        entry = SimpleNamespace(entry_id="e1", data={c.CONF_GATEWAY_SN: GW_SN},
                                options={})
        added_entities = []

        await cover_mod.async_setup_entry(hass, entry, lambda ents: added_entities.extend(ents))
        assert calls["added"] == [DEV_SN], "启动循环创建的 cover 必须注册设备状态回调（v1.6.8 实时性定案）"

        # 动态添加路径（新 SN——本会话已创建设备会被 created_covers 幂等短路，
        # 恰证 created 跟踪有效）
        DEV2 = "5002DEV00002"
        await dm._added_cb(DEV2, "W2", c.DEVICE_TYPE_WINDOW_OPENER)
        assert DEV2 in calls["added"], "on_device_added 路径同样必须注册状态回调"

        # 移除路径：先摘回调再清注册表
        await dm._removed_cb(DEV2, "W2", c.DEVICE_TYPE_WINDOW_OPENER)
        assert calls["removed"] == [DEV2]


# ============ #8 button 基础按钮清理不再寄生删除按钮总闸 ============
class TestButtonCleanupGate:
    @pytest.mark.asyncio
    async def test_base_buttons_removed_without_remove_tracking(self, monkeypatch):
        removed = []

        async def fake_eid(hass, domain, uid):
            # 删除按钮与 wind_lock 按钮视为不存在；基础按钮存在
            if any(k in uid for k in ("_remove_", "wind_lock")):
                return None
            return f"button.{uid}"

        async def fake_call_reg(method, *args, **kwargs):
            removed.append(args[0])

        monkeypatch.setattr(gw_utils, "async_get_entity_id", fake_eid)
        monkeypatch.setattr(gw_utils, "call_registry_method", fake_call_reg)
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        fake_registry = SimpleNamespace(async_remove=lambda eid: None)
        monkeypatch.setitem(vars(er_mod), "async_get", lambda h: fake_registry)

        await button_mod._remove_device_buttons(None, GW_SN, DEV_SN, "W1")
        assert len(removed) == 4, "open/stop/close/a 四个基础按钮必须无条件按 unique_id 幂等清理"
        assert all(f"{GW_SN}_{DEV_SN}_" in e for e in removed)


# ============ #9 options 表单 schema 与真实消费面对齐 ============
class TestOptionsFlowSchema:
    @pytest.mark.asyncio
    async def test_options_step_has_no_dead_gateway_sn(self, monkeypatch):
        captured = {}

        def fake_show_form(self, **kw):  # 真实 HA 中 async_show_form 是同步方法
            captured.update(kw)
            return {"type": "form"}

        monkeypatch.setattr(OptionsFlow, "async_show_form", fake_show_form, raising=False)
        flow = OptionsFlow(SimpleNamespace(entry_id="e", data={c.CONF_GATEWAY_SN: GW_SN},
                                           options={}))
        await flow.async_step_options(None)
        keys = [k.schema if hasattr(k, "schema") else k
                for k in captured["data_schema"].schema.keys()]
        assert "gateway_sn" not in keys, "gateway_sn 死控件（写入 options 零消费）必须移除"
        # v1.6.15：新增 ws_gateway_* 三字段——真实消费方为
        # ws_gateway.ws_gateway_wanted（options 三键逐一被读，非死控件）
        assert keys == ["discovery_interval", "auto_discovery", "debug_logging",
                        c.CONF_EXPOSE_COVER_AS_CURTAIN,
                        c.CONF_WS_GATEWAY_ENABLED, c.CONF_WS_GATEWAY_PORT,
                        c.CONF_WS_GATEWAY_TOKEN], \
            "字段必须与 __init__/mqtt_handler/ws_gateway 真实消费面一一对应"


# ============ #10 persist 加固 ============
class _PHass:
    def __init__(self, config_dir, initial=None):
        self.config = SimpleNamespace(config_dir=config_dir)
        self.data = {c.DOMAIN: initial if initial is not None else {}}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class TestPersistHardening:
    def test_missing_main_file_restores_from_bak(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, PERSISTENT_DATA_FILE + ".bak"), "w",
                      encoding="utf-8") as f:
                json.dump({"schema_version": 1,
                           "device_to_gateway_mapping": {"dev1": "gw1"},
                           "manually_removed_devices": ["dev9"]}, f)
            hass = _PHass(tmp, {c.DEVICE_TO_GATEWAY_MAPPING: {},
                                c.GLOBAL_MANUALLY_REMOVED_DEVICES: set()})
            asyncio.run(load_persistent_data(hass))
            assert hass.data[c.DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING] == {"dev1": "gw1"}, \
                "主文件缺失时 .bak 必须救援（修复前直接 return，备份形同虚设）"
            assert hass.data[c.DOMAIN][c.GLOBAL_MANUALLY_REMOVED_DEVICES] == {"dev9"}

    def test_malformed_fields_discarded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, PERSISTENT_DATA_FILE), "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1,
                           "device_to_gateway_mapping": None,
                           "manually_removed_devices": 42,
                           "device_setpoints": {"d": {"speed": 50}}}, f)
            hass = _PHass(tmp, {c.DEVICE_TO_GATEWAY_MAPPING: {"keep": "gw"},
                                c.GLOBAL_MANUALLY_REMOVED_DEVICES: set()})
            asyncio.run(load_persistent_data(hass))  # 修复前：len(None)/set(42) 抛异常逃逸 setup
            assert hass.data[c.DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING] == {"keep": "gw"}
            assert hass.data[c.DOMAIN][c.GLOBAL_MANUALLY_REMOVED_DEVICES] == set()
            assert hass.data[c.DOMAIN][c.DEVICE_SETPOINTS] == {"d": {"speed": 50}}, \
                "合法字段照常加载，非法字段丢弃"

    def test_non_string_entries_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, PERSISTENT_DATA_FILE), "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "manually_removed_devices": ["ok", 7, None]}, f)
            hass = _PHass(tmp, {})
            asyncio.run(load_persistent_data(hass))
            assert hass.data[c.DOMAIN][c.GLOBAL_MANUALLY_REMOVED_DEVICES] == {"ok"}


# ============ Web 超时契约（静态钉桩：JS 无法在 pytest 执行） ============
class TestWebTimeoutContract:
    def setup_method(self):
        # v1.6.25 三文件化：index.html 的内联 script 已字节无损搬至 www/js/huijian.js。
        # 检索面 = 三文件按拆分前顺序拼接（与原单文件搜索空间一致），断言语义不变。
        www = HERE.parent / "www"
        self.html = "".join((www / p).read_text(encoding="utf-8") for p in
                            ("index.html", "css/huijian.css", "js/huijian.js"))

    def test_abort_reason_must_be_error(self):
        assert "abort(new Error(" in self.html, \
            "abort 必须传 Error——字符串 reason 按规范原值 reject，消费侧 e.message 恒 undefined"
        assert "abort('" not in self.html and 'abort("' not in self.html, \
            "禁止以字符串字面量作为 abort reason"

    def test_fetchT_guard_covers_body_read(self):
        i = self.html.index("function fetchT")
        body = self.html[i:self.html.index("\n        }", i)]
        assert "resp.json = " in body and "clearTimer" in body, \
            "超时须覆盖 body 读取：json()/text() 结算后才清定时器（silentRefresh 防重入自愈依赖此）"
        assert ".finally(() => clearTimeout(timer))" not in body, \
            "旧的『响应头到达即清 timer』模式禁止回潮"


# ============ infra 静态钉桩 ============
class TestInfraHygiene:
    def test_config_yaml_no_ssl_map(self):
        cfg = (HERE.parent / "config.yaml").read_text(encoding="utf-8")
        assert "\n  - ssl" not in cfg, "ssl 映射全仓零消费（无 TLS），权限最小化移除"
        assert "homeassistant_config:rw" in cfg

    def test_options_translation_contract(self):
        for name in ("strings.json", "translations/zh-CN.json"):
            data = json.loads((HERE.parent / "custom_components" /
                               "window_controller_gateway" / name).read_text(encoding="utf-8"))
            steps = data["options"]["step"]
            assert "options" in steps and "add_gateway" in steps, f"{name}: 真实渲染的步骤必须有文案"
            # 审计 2026-09-30 E-1 改钉：本钉原文是"init 步骤从不渲染（async_step_init
            # 分流），死文案移除"——那正是缺陷本身：无 SN 条目被无条件分流到
            # add_gateway，选项步骤永远不可达，而 awaiting 分支已用**公开默认令牌**
            # 起了 9001 监听。init 现在渲染成菜单（两个步骤都可达），所以文案必须
            # 在位；判据从"不得存在"翻成"必须存在且两个菜单项都有中文名"，
            # 裸英文 step id 泄漏 UI 依旧判红。
            assert "init" in steps, f"{name}: init 已渲染成菜单，文案不得缺"
            menu = steps["init"].get("menu_options", {})
            assert set(menu) == {"add_gateway", "options"}, \
                f"{name}: init 菜单项文案必须与 async_step_init 的 menu_options 一一对应"
            for _k, _v in menu.items():
                assert isinstance(_v, str) and _v.strip() and any(
                    "\u4e00" <= ch <= "\u9fff" for ch in _v), f"{name}: 菜单项 {_k} 文案非法"
            assert "gateway_sn" not in steps["options"].get("data", {}), \
                f"{name}: 死控件的文案残留同步清理"
            errs = set(data["options"]["error"])
            assert {"required", "invalid_sn_format", "already_configured"} <= errs, \
                f"{name}: add_gateway 的 errors 键必须有文案（裸英文 key 泄漏 UI）"
