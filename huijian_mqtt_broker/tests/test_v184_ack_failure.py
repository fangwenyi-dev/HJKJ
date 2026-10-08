"""v1.8.4 审计 B-3：002 的 ack 发布失败，不得吃掉这一帧的处理。

`_send_ack` 里是 `await mqtt.async_publish(...)`——broker 未就绪/发布异常时会向上抛。
旧写法把它排在 `try` **之外**，于是一次 ack 失败＝这一帧的 `update_gateway_status`、
全量 devices 批处理、属性更新**一行都不做**，与紧邻注释承诺的"此处只表达已收到，
与处理结果无关（异常另有日志）"正好相反。

顺序契约（ack 先于批处理，v1.7.33 定案）本文件不动，只补"失败不传染"。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from custom_components.window_controller_gateway import const as c
import custom_components.window_controller_gateway.mqtt_handler as mh_mod
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)

GW_SN = "100122501207"


class _DM:
    def __init__(self):
        self.gateway_status = []
        self.device_updates = []

    async def update_gateway_status(self, status):
        self.gateway_status.append(status)

    async def update_device_status(self, sn, status, attributes=None):
        self.device_updates.append((sn, status, attributes))

    def _notify_status_listeners(self, sn):
        pass


class _Hass:
    def __init__(self, loop):
        self.data = {c.DOMAIN: {}}
        self.loop = loop
        self.config = SimpleNamespace(config_dir=".")

    def async_create_task(self, coro):
        if self.loop is not None and self.loop.is_running():
            return self.loop.create_task(coro)
        coro.close()
        return None

    def add_job(self, job, *args):
        return job(*args) if callable(job) else None


def _publisher(mode):
    """mode: "raise" 让发布抛（模拟 broker 未就绪）；"ok" 正常记账。"""
    calls = []

    async def _pub(hass, topic, payload, qos=0, retain=False):
        p = json.loads(payload)
        calls.append((topic, p))
        if mode == "raise":
            raise RuntimeError("MQTT not ready")
    return _pub, calls


@pytest.mark.asyncio
async def test_ack_failure_does_not_skip_the_frame(monkeypatch, caplog):
    pub, calls = _publisher("raise")
    monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
    dm = _DM()
    handler = WindowControllerMQTTHandler(_Hass(asyncio.get_running_loop()), GW_SN, dm)
    handler.gateway_sn = GW_SN

    payload = {"head": c.PROTOCOL_HEAD, "ctype": "002", "id": 5,
               "sn": GW_SN, "data": {"status": "online"}}
    # 关键：这一句不得抛——ack 发不出去是本帧的坏运气，不是这一帧的死刑
    await handler._handle_ctype_002(payload, "002", payload["data"])

    assert dm.gateway_status == ["online"], \
        "ack 失败把整帧处理跳过了（B-3 原形：状态/批处理一行不做）"
    assert calls, "至少真的试过一次发布"


@pytest.mark.asyncio
async def test_ack_failure_is_left_in_the_log(monkeypatch, caplog):
    pub, _calls = _publisher("raise")
    monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
    dm = _DM()
    handler = WindowControllerMQTTHandler(_Hass(asyncio.get_running_loop()), GW_SN, dm)
    handler.gateway_sn = GW_SN

    payload = {"head": c.PROTOCOL_HEAD, "ctype": "002", "id": 6,
               "sn": GW_SN, "data": {"status": "online"}}
    with caplog.at_level("WARNING"):
        await handler._handle_ctype_002(payload, "002", payload["data"])

    text = " | ".join(r.getMessage() for r in caplog.records)
    assert "ack 发布失败" in text, f"ack 失败必须留痕（否则线上只看到状态没变）：{text}"
    assert "MQTT not ready" in text, "留痕要带上失败原因"


@pytest.mark.asyncio
async def test_ack_still_precedes_batch_processing(monkeypatch):
    """顺序契约不许被这刀改松（v1.7.33 定案：先应答后处理，否则满负载跨 5s 去重窗
    会把同一条 002 整批重跑）。"""
    order = []

    async def _pub(hass, topic, payload, qos=0, retain=False):
        order.append(("publish", json.loads(payload).get("ctype")))

    monkeypatch.setattr(mh_mod.mqtt, "async_publish", _pub)
    dm = _DM()
    handler = WindowControllerMQTTHandler(_Hass(asyncio.get_running_loop()), GW_SN, dm)
    handler.gateway_sn = GW_SN

    async def _record(status):
        order.append(("status", status))

    dm.update_gateway_status = _record
    payload = {"head": c.PROTOCOL_HEAD, "ctype": "002", "id": 7,
               "sn": GW_SN, "data": {"status": "online"}}
    await handler._handle_ctype_002(payload, "002", payload["data"])

    kinds = [k for k, _v in order]
    assert kinds.index("publish") < kinds.index("status"), \
        f"ack 又排到处理之后了：{order}"


@pytest.mark.asyncio
async def test_batch_failure_still_acked_and_never_escapes(monkeypatch, caplog):
    """契约的另一半：**处理**异常不得影响"002 必 ack"。

    002 处理器是全吞形态（`except Exception: _LOGGER.error`）——这不是缺陷：处理器跑在
    `_dispatch_with_dedup` 里，往上抛会连带把去重记账回滚、让网关重发的同一条整批重跑。
    所以这里判的是"不逃逸 + 已 ack + 顺序不变"，不是"会抛"。
    """
    order = []

    async def _pub(hass, topic, payload, qos=0, retain=False):
        p = json.loads(payload)
        order.append(("publish", p.get("ctype")))

    monkeypatch.setattr(mh_mod.mqtt, "async_publish", _pub)
    dm = _DM()
    handler = WindowControllerMQTTHandler(_Hass(asyncio.get_running_loop()), GW_SN, dm)
    handler.gateway_sn = GW_SN

    async def _boom(status):
        order.append(("status", status))
        raise RuntimeError("registry exploded")

    dm.update_gateway_status = _boom
    payload = {"head": c.PROTOCOL_HEAD, "ctype": "002", "id": 8,
               "sn": GW_SN, "data": {"status": "online"}}
    await handler._handle_ctype_002(payload, "002", payload["data"])  # 不得外抛

    assert ("publish", "002") in order, "处理炸了也必须已 ack（否则网关无限重发）"
    assert order.index(("publish", "002")) < order.index(("status", "online"))
