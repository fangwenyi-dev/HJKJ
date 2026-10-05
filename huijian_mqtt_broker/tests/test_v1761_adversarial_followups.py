"""v1.7.61 对抗复核后续（代理抓 7 处，我逐条复现后收编；本文件自包含，不跨测试 import）。

被推翻/部分的指控与处置：
- C1-c：**HA 源码实证**（2024.12.0 config_entries.py:1285-1289）单实例闸对
  SOURCE_USER 流不统计 ignore 条目 ⇒ "只有被忽略条目"时建条本可成功。初版
  修法用早退"保留标记"把唯一自愈出口关掉了——改为 loud 点名后**继续走创建**。
- C1-b：config_flow 的 MQTT 就绪门禁仍把 ignore 条目当"已有线索" → 换用
  `_usable_mqtt_entries`。
- C2-c：武装循环重试退避化（120→240→…封顶 3600s），与 v1.7.30 施压纪律同向。
- S1/S2/S3/S4：四处同型 `int()` 漏 OverflowError（身份 bindCodeAt / 核验读条目
  port / 端点文件 port / 引导标记 port），其中 S1 会炸面板 status_view 与保活。
"""
import asyncio
import json
import types
from types import SimpleNamespace

import custom_components.window_controller_gateway as pkg
import custom_components.window_controller_gateway.config_flow as cf_mod
import custom_components.window_controller_gateway.hub_client as hc
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import custom_components.window_controller_gateway.utils as pkg_utils


# ---------- 自包含夹具（照 test_v1761_ignore_source_and_i18n 的形态） ----------

class _EnsureHass:
    def __init__(self, marker_path, mqtt_entries, flow_results, mqtt_loaded=False):
        self._marker_path = marker_path
        entries = list(mqtt_entries)
        self.data = {"mqtt": object()} if mqtt_loaded else {}
        self.config = types.SimpleNamespace(
            path=lambda name: str(self._marker_path) if name == mb.BOOTSTRAP_FILENAME
            else f"/config/{name}")
        self.flow = types.SimpleNamespace(calls=[], _results=list(flow_results))

        async def _init(domain, context=None):
            self.flow.calls.append(("init", domain))
            return self.flow._results.pop(0)

        async def _configure(flow_id, user_input=None):
            self.flow.calls.append(("configure", flow_id, user_input))
            return self.flow._results.pop(0)

        async def _abort(flow_id):
            self.flow.calls.append(("abort", flow_id))

        self.flow.async_init = _init
        self.flow.async_configure = _configure
        self.flow.async_abort = _abort
        self.reload_calls = []

        async def _reload(eid):
            self.reload_calls.append(eid)

        def _update(entry, data=None, **kw):
            entry.data.update(data or {})

        async def _remove(eid):
            pass

        self.config_entries = types.SimpleNamespace(
            async_entries=lambda dom: entries if dom == "mqtt" else [],
            flow=self.flow, async_reload=_reload, async_update_entry=_update,
            async_remove=_remove)

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def _marker(tmp_path, **over):
    data = {"broker": "127.0.0.1", "port": 2022, "username": "ha_mqtt",
            "password": "pw"}
    data.update(over)
    p = tmp_path / mb.BOOTSTRAP_FILENAME
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _mk_entry(source, disabled_by=None, eid="E1", **data_over):
    data = {"broker": "127.0.0.1", "port": 2022, "username": "ha_mqtt",
            "password": "pw"}
    data.update(data_over)
    return SimpleNamespace(data=data, disabled_by=disabled_by, source=source,
                           entry_id=eid)


def _flow_results():
    from homeassistant.data_entry_flow import FlowResultType
    return [{"flow_id": "f1", "type": FlowResultType.FORM},
            {"type": FlowResultType.CREATE_ENTRY}]


def _arm_hass(tmp_path):
    from homeassistant.components import mqtt as fake_mqtt
    from homeassistant.core import FakeServices
    from homeassistant.helpers import entity_registry as er

    async def asub(hass, topic, cb, qos=0):
        return lambda: None

    fake_mqtt.async_subscribe = asub
    fake_mqtt.async_publish = lambda *a, **k: asyncio.sleep(0)
    er.async_get = lambda h: SimpleNamespace(entities={})

    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(config_dir=str(tmp_path),
                               path=lambda *p: str(tmp_path / p[0])),
        loop=None,
        services=FakeServices(),
        async_create_task=lambda coro, name=None, **kw: asyncio.ensure_future(coro),
        async_add_executor_job=None,
        config_entries=SimpleNamespace(
            async_entries=lambda dom: [SimpleNamespace(entry_id="EA", data={},
                                                       options={}, disabled_by=None,
                                                       state="loaded", unique_id=None)],
            async_update_entry=lambda *a, **k: None,
            async_forward_entry_setups=lambda e, p: asyncio.sleep(0),
        ),
        bus=SimpleNamespace(async_listen_once=lambda ev, cb: (lambda: None)),
    )
    return hass


class _Entry:
    data, options, entry_id, title = {}, {}, "EA", "慧尖网关"

    def async_on_unload(self, fn):
        pass

    def add_update_listener(self, fn):
        return lambda: None


# ============ C1-c 修正：只有 ignore 条目时必须**继续走创建** ============

def test_ignored_only_still_attempts_creation(tmp_path):
    """HA 源码实证 ignore 不挡 user 流 ⇒ 初版"早退保标记"是自伤：必须发起流。
    建条落地（CREATE_ENTRY + 客户端就绪）⇒ 删标记（引导完成语义成立）。"""
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("ignore")],
                       flow_results=_flow_results(), mqtt_loaded=True)
    result = asyncio.run(mb.ensure_mqtt_connection(hass))
    assert hass.flow.calls, "必须真的发起 MQTT 建条流（旧早退=自愈出口关闭）"
    assert hass.flow.calls[0][0] == "init"
    assert not marker.exists(), "建条落地 ⇒ 标记删除"
    assert result is True


def test_disabled_only_still_keeps_marker(tmp_path, caplog):
    """反向臂：**禁用**条目真的挡 HA 单实例闸（include_ignore=False 计禁用）
    ⇒ 保持 v1.7.18 BUG-5 语义：不代劳、保留标记、不发流。"""
    import logging
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("user", disabled_by="user")],
                       flow_results=[], mqtt_loaded=False)
    with caplog.at_level(logging.WARNING,
                         logger="custom_components.window_controller_gateway"):
        result = asyncio.run(mb.ensure_mqtt_connection(hass))
    assert marker.exists() and result is False
    assert hass.flow.calls == [], "禁用条目不代劳（不发流）"
    assert any("禁用" in r.getMessage() for r in caplog.records)


def test_ignored_plus_disabled_keeps_marker(tmp_path):
    """并存形态：禁用那条件才是拦截源 ⇒ 保留标记、不发流。"""
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("ignore", eid="I1"),
                                _mk_entry("user", disabled_by="user", eid="D1")],
                       flow_results=[], mqtt_loaded=False)
    result = asyncio.run(mb.ensure_mqtt_connection(hass))
    assert marker.exists() and result is False and hass.flow.calls == []


# ============ C1-b：就绪门禁不把 ignore 条目当线索 ============

def test_gate_reports_not_available_for_ignored_only(tmp_path, monkeypatch):
    """ignore-only + 无标记：必须快败 mqtt_not_available（旧口径会白等宽限窗
    后误报 broker_not_ready，把"那条被忽略的条目"这一根因埋掉）。"""
    async def no_marker(h):
        return False

    monkeypatch.setattr(cf_mod, "has_bootstrap_marker", no_marker)
    monkeypatch.setattr(cf_mod, "is_mqtt_loaded", lambda h: False)

    class _Self:
        hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=lambda dom: [_mk_entry("ignore")]),
        )

    errors = {}
    ok = asyncio.run(cf_mod.ConfigFlow._async_gate_mqtt_ready(_Self(), errors))
    assert ok is False and errors.get("base") == "mqtt_not_available"


# ============ S1：身份 bindCodeAt 非有限数 ============

def test_identity_nonfinite_bindcode_at_is_sanitized(tmp_path):
    (tmp_path / hc.HUB_IDENTITY_FILE).write_text(
        json.dumps({"instanceId": "i1", "secret": "s1", "bindCode": "B",
                    "bindCodeAt": 1e999, "bindCodeTtl": 600}),
        encoding="utf-8")
    c = hc.HubClient([], config_dir=str(tmp_path))
    asyncio.run(c._load_identity())
    assert c._bind_code_at == 0.0, "非有限落盘时刻必须归零（否则 int(inf) 炸）"
    assert c.bind_code_expires_in() == -1, "面板路径不许抛"
    c.status_view()   # 面板 API 的组装面：旧实现此处 OverflowError → 500


def test_bind_code_expires_in_never_raises_on_garbage():
    """出口守卫：即使内存里被塞进 inf（迁移/并发），函数也不许抛。"""
    c = hc.HubClient([], config_dir=".")
    c.bind_code = "B"
    c._bind_code_at = float("inf")
    assert c.bind_code_expires_in() == -1


# ============ S2/S3/S4：三个读 port 的 int() 缺口 ============

def test_endpoint_file_nonfinite_port_falls_back(tmp_path):
    """S3：端点文件 port=1e999 → 回落内置口（旧实现整份文件被判读不到 ⇒
    通道核验静默关闭）。"""
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 1e999}), encoding="utf-8")
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: str(tmp_path / p[0])))
    data = asyncio.run(mb.async_read_endpoint(hass))
    assert data == {"broker": "127.0.0.1", "port": mb.BUILTIN_PORT}


def test_verify_nonfinite_entry_port_no_raise(tmp_path):
    """S2：条目 data 的 port=1e999 时核验不抛（旧实现 healer 无 try ⇒ 巡检
    任务静默死亡），归入 mismatch。"""
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 2022}), encoding="utf-8")
    entry = _mk_entry("user", port=1e999)
    hass = SimpleNamespace(data={},
                           config=SimpleNamespace(path=lambda *p: str(tmp_path / p[0])),
                           config_entries=SimpleNamespace(
                               async_entries=lambda dom: [entry]))
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "mismatch"


def test_marker_nonfinite_port_no_raise(tmp_path):
    """S4：引导标记 port=1e999 时 ensure 不抛（旧实现溢出被上层吞成
    "cannot_connect" 假归因），按默认口继续建条。"""
    marker = _marker(tmp_path, port=1e999)
    hass = _EnsureHass(marker, [], flow_results=_flow_results(), mqtt_loaded=True)
    result = asyncio.run(mb.ensure_mqtt_connection(hass))
    assert hass.flow.calls and result is True, "溢出不许掐断建条链"


# ============ C2-c：重试退避（不是每轮都催） ============

def test_arm_retry_backs_off(tmp_path, monkeypatch):
    """6 轮未就绪：ensure 只应在第 1/2/4 轮各调一次（120→240→480s 退避），
    而不是每轮都催——与 v1.7.30"接管破坏面不恒频施压"同向。"""
    import custom_components.window_controller_gateway.ws_gateway as wsg

    async def noop(h):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", noop)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop)

    rounds = {"n": 0}
    ensure_calls = {"n": 0}

    async def fake_ensure(hass):
        ensure_calls["n"] += 1

    async def fake_wait(hass, timeout=120.0, interval=0.5):
        rounds["n"] += 1
        return rounds["n"] > 6

    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)
    monkeypatch.setattr(pkg_utils, "async_wait_mqtt_loaded", fake_wait)

    hass = _arm_hass(tmp_path)
    entry = _Entry()

    async def go():
        hass.loop = asyncio.get_running_loop()
        await pkg.async_setup_entry(hass, entry)
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert rounds["n"] >= 6
    # 期望构成：async_setup_entry 等待分支自己先催 1 次（落点之一，非本钉主题）
    # + 武装循环按 120→240→480s 退避在 r1/r2/r4 各催 1 次 = 4；若写成"每轮都催"
    # 会是 1+6=7。
    assert ensure_calls["n"] == 4, f"期望 1(安装期)+3(退避) 次，实得 {ensure_calls['n']}"
