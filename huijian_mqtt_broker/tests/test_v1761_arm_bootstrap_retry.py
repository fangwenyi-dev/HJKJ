"""v1.7.61 现场追加：等待条目的武装循环必须顺带重试 MQTT 引导。

现场（2026-10-05，.184）：HA 的 MQTT 集成没就绪 → 心跳耳无限干等（日志
"MQTT 集成仍未就绪（累计 120s）"，__init__.py:551 行号只在 v1.7.60 成立）
→ 网关上报没人听 → "首台自动添加"没有真正成功。
代码缺口：_arm_heartbeat_when_mqtt_ready 只轮询 is_mqtt_loaded，**从不重试
ensure_mqtt_connection**；重试只存在于 healer（且 healer 无标记时转核验不重建）。
引导标记在位时（加载项每次启动都重写）本可自愈，旧实现却宁可无限干等。
"""
import asyncio
import logging
from types import SimpleNamespace

import homeassistant.components.mqtt as fake_mqtt
import custom_components.window_controller_gateway as pkg
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import custom_components.window_controller_gateway.utils as pkg_utils
from custom_components.window_controller_gateway.const import DOMAIN


def _hass(tmp_path, mqtt_loaded=False):
    from homeassistant.core import FakeServices
    from homeassistant.helpers import entity_registry as er

    subs = []

    async def asub(hass, topic, cb, qos=0):
        subs.append((topic, cb))
        return lambda: None

    fake_mqtt.async_subscribe = asub
    fake_mqtt.async_publish = lambda *a, **k: asyncio.sleep(0)
    er.async_get = lambda h: SimpleNamespace(entities={})

    runtime = {DOMAIN: {}}
    if mqtt_loaded:
        runtime["mqtt"] = object()
    hass = SimpleNamespace(
        data=runtime,
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
    return hass, subs


class _Entry:
    data, options, entry_id, title = {}, {}, "EA", "慧尖网关"

    def async_on_unload(self, fn):
        pass

    def add_update_listener(self, fn):
        return lambda: None


def test_arm_loop_retries_bootstrap_while_waiting(tmp_path, monkeypatch, caplog):
    """一轮未就绪（wait 返回 False）时必须调用一次 ensure_mqtt_connection；
    第二轮就绪（True）后照常订阅。旧实现 ensure 调用数为 0。"""
    import custom_components.window_controller_gateway.ws_gateway as wsg

    async def noop(h):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", noop)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop)

    calls = {"ensure": 0}

    async def fake_ensure(hass):
        calls["ensure"] += 1

    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)

    seq = [False, True]   # 第一轮未就绪 → 触发重试；第二轮就绪 → 出循环订阅

    async def fake_wait(hass, timeout=120.0, interval=0.5):
        return seq.pop(0) if seq else True

    monkeypatch.setattr(pkg_utils, "async_wait_mqtt_loaded", fake_wait)

    hass, subs = _hass(tmp_path)
    entry = _Entry()

    async def go():
        hass.loop = asyncio.get_running_loop()
        await pkg.async_setup_entry(hass, entry)
        await asyncio.sleep(0.3)      # 让后台武装任务跑完两种形态

    with caplog.at_level(logging.WARNING,
                         logger="custom_components.window_controller_gateway"):
        asyncio.run(go())

    assert calls["ensure"] >= 1, \
        "等待 MQTT 就绪期间必须顺带重试引导（标记在位时这是唯一的自愈点）"
    assert subs and subs[0][0] == "gateway/rpt_rsp", "就绪后照常武装订阅"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "MQTT 集成仍未就绪" in text or "未就绪" in text, "留痕不许丢"


def test_arm_loop_still_patient_forever(tmp_path, monkeypatch):
    """反向臂：一直不就绪 ⇒ 一直等（v1.7.28 的"无限期耐心"语义不许被修没）。"""
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
        return rounds["n"] > 3        # 前 3 轮都未就绪

    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)
    monkeypatch.setattr(pkg_utils, "async_wait_mqtt_loaded", fake_wait)

    hass, subs = _hass(tmp_path)
    entry = _Entry()

    async def go():
        hass.loop = asyncio.get_running_loop()
        await pkg.async_setup_entry(hass, entry)
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert rounds["n"] >= 3, "未就绪必须持续等待，不得提前放弃"
    assert ensure_calls["n"] >= 2, "每一轮未就绪都要重试引导（不是只催一次）"
