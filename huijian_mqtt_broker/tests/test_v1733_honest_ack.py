"""v1.7.33 全量审计修复批（B 组）：如实 ack / 能力闸 / 让出点重解析。

钉三处"假成功与陈旧引用"缺陷（均为只读审计实锤、修复在批 B）：
1. WS `_cmd_control` 在零条目时循环空转仍回 `ok:true` —— 小程序收到"已下发"
   而一条报文都没发出；`_cmd_pair` 同属空集假成功；
2. `window_controller_gateway.set_position` 服务无机型百分比能力闸 —— Web
   面板滑块/REST/自动化绕过实体侧校验，向无百分比硬件打固件未定义的
   w_travel 位置指令并回 200；
3. 「移除设备」按钮在 `await asyncio.sleep()` 让出点之后仍用构造期捕获的
   device_manager 引用 —— reload 窗内本地删除整体 no-op → 手动删除名单未
   登记 → 幽灵设备下次 002 复活（v1.6.19 A-MED2 在 WS 通道修过同案）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.window_controller_gateway import const as c
from custom_components.window_controller_gateway.ws_gateway import WsGatewayServer
from custom_components.window_controller_gateway.services import (
    ServiceValidationError,
    handle_set_position,
)

GW_SN = "100122501207"
DEV_CAPABLE = "500500000001"      # 5005 → 有百分比能力
DEV_INCAPABLE = "500200000001"    # 5002 → 无百分比能力（const 前缀表外）


def _call(**data):
    return SimpleNamespace(data=data)


# ==================== 1. WS 空集如实 ack ====================

class _FakeHass:
    def __init__(self, domain_data):
        self.data = {c.DOMAIN: domain_data}
        self.loop = None
        self.config = SimpleNamespace(config_dir=".")

    def async_create_task(self, coro, **kwargs):
        if asyncio.iscoroutine(coro):
            coro.close()
        return None


def _server(entries=None):
    """entries: {gw_sn: (handler, dm)}；零条目=空 dict（半开口径现场形态）。"""
    data = {}
    for gw, (handler, dm) in (entries or {}).items():
        data[f"entry_{gw}"] = {
            "_setup_complete": True,
            "gateway_sn": gw,
            "mqtt_handler": handler,
            "device_manager": dm,
        }
    return WsGatewayServer(_FakeHass(data), host="127.0.0.1", port=9999,
                           token="tok12345")


class _FakeHandler:
    def __init__(self, fail=False):
        self.gateway_sn = GW_SN
        self.connected = True
        self.fail = fail
        self.raw004 = []
        self.commands = []

    async def send_ws_raw_004(self, device_sn, attribute, value):
        self.raw004.append((device_sn, attribute, value))
        return not self.fail

    async def send_command(self, sn, command, params=None):
        self.commands.append((sn, command))
        return True


class _FakeDM:
    def __init__(self):
        self.devices = {}


class TestWsHonestAckOnEmptyRegistry:
    @pytest.mark.asyncio
    async def test_control_with_zero_entries_is_honest_failure(self):
        srv = _server()  # 半开口径：9001 常听但无任何已完成设置条目
        ack = await srv._cmd_control({"cmd": "control", "gwSn": GW_SN, "devSn": DEV_CAPABLE,
                                      "attribute": "w_travel", "value": "100"})
        assert ack["type"] == "control_ack"
        assert ack["ok"] is False, "空集上回 ok:true 是假成功（小程序显示已下发）"
        assert ack["msg"] == "no gateway registered"

    @pytest.mark.asyncio
    async def test_pair_with_zero_entries_is_honest_failure(self):
        srv = _server()
        ack = await srv._cmd_pair({"cmd": "pair"})
        assert ack["ok"] is False and ack["msg"] == "no gateway registered"

    @pytest.mark.asyncio
    async def test_control_broadcast_still_ok_when_entries_exist(self):
        """有已完成设置条目时，广播语义不变（固件 P2 定式，不被本批误伤）。"""
        h = _FakeHandler()
        srv = _server({GW_SN: (h, _FakeDM())})
        ack = await srv._cmd_control({"cmd": "control", "gwSn": GW_SN, "devSn": DEV_CAPABLE,
                                      "attribute": "w_travel", "value": "100"})
        assert ack["ok"] is True and h.raw004 == [(DEV_CAPABLE, "w_travel", "100")]

    @pytest.mark.asyncio
    async def test_pair_broadcast_still_ok_when_entries_exist(self):
        h = _FakeHandler()
        srv = _server({GW_SN: (h, _FakeDM())})
        ack = await srv._cmd_pair({"cmd": "pair"})
        assert ack["ok"] is True and h.commands == [(GW_SN, "start_pairing")]

    @pytest.mark.asyncio
    async def test_control_publish_failure_is_honest(self):
        h = _FakeHandler(fail=True)
        srv = _server({GW_SN: (h, _FakeDM())})
        ack = await srv._cmd_control({"cmd": "control", "gwSn": GW_SN, "devSn": DEV_CAPABLE,
                                      "attribute": "w_travel", "value": "100"})
        assert ack["ok"] is False and ack["msg"] == "send failed"


# ==================== 2. set_position 能力闸 ====================

class _SvcHandler:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    async def send_command(self, sn, command, params=None):
        self.calls.append((sn, command, dict(params or {})))
        return self.result


def _svc_hass(device_sn, handler):
    dm = SimpleNamespace(get_all_devices=lambda: [{"sn": device_sn,
                                                   "name": "窗",
                                                   "type": c.DEVICE_TYPE_WINDOW_OPENER}],
                         get_device=lambda sn: {"sn": sn})
    return SimpleNamespace(data={c.DOMAIN: {
        "entry_1": {"gateway_sn": GW_SN, "device_manager": dm,
                    "mqtt_handler": handler}}})


class TestSetPositionCapabilityGate:
    @pytest.mark.asyncio
    async def test_incapable_model_rejected_before_publish(self):
        h = _SvcHandler()
        hass = _svc_hass(DEV_INCAPABLE, h)
        with pytest.raises(ServiceValidationError) as ei:
            await handle_set_position(hass, _call(device_id=DEV_INCAPABLE, position=50))
        assert "不支持百分比定位" in str(ei.value)
        assert h.calls == [], "无能力机型绝不允许下发（假成功+空口无效指令）"

    @pytest.mark.asyncio
    async def test_capable_model_passes_and_publishes(self):
        h = _SvcHandler()
        hass = _svc_hass(DEV_CAPABLE, h)
        await handle_set_position(hass, _call(device_id=DEV_CAPABLE, position=65))
        assert h.calls == [(DEV_CAPABLE, "set_position", {"position": 65})]

    @pytest.mark.asyncio
    async def test_gate_precedes_handler_lookup(self):
        """能力闸先于 handler 存在性判定——无能力机型不该因缺 handler 换文案。"""
        hass = _svc_hass(DEV_INCAPABLE, None)
        hass.data[c.DOMAIN]["entry_1"].pop("mqtt_handler")
        with pytest.raises(ServiceValidationError) as ei:
            await handle_set_position(hass, _call(device_id=DEV_INCAPABLE, position=50))
        assert "不支持百分比定位" in str(ei.value)


# ==================== 3. 移除按钮让出点重解析 ====================

class _RemoveDM:
    def __init__(self):
        self.removed = []

    async def remove_device(self, device_sn, is_manual=True):
        self.removed.append(device_sn)


class _RemoveHandler:
    async def unbind_device(self, device_sn):
        return None


def _remove_button(hass, dm, entry_id):
    from custom_components.window_controller_gateway.gateway import (
        GatewayDeviceRemoveButton)
    return GatewayDeviceRemoveButton(
        hass=hass, device_manager=dm, mqtt_handler=_RemoveHandler(),
        gateway_sn=GW_SN, gateway_name="LoRa 网关", device_sn=DEV_CAPABLE,
        device_name="窗", entry_id=entry_id)


class TestRemoveButtonReResolvesManager:
    @pytest.mark.asyncio
    async def test_uses_reloaded_manager(self, monkeypatch):
        """reload 后 data 里的新 manager 必须被采用（旧引用不得再用）。"""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        old_dm, new_dm = _RemoveDM(), _RemoveDM()
        entry = SimpleNamespace(entry_id="e1")
        hass = SimpleNamespace(
            data={c.DOMAIN: {"e1": {"device_manager": new_dm}}},
            config_entries=SimpleNamespace(async_entries=lambda d: [entry]),
        )
        await _remove_button(hass, old_dm, "e1").async_press()
        assert new_dm.removed == [DEV_CAPABLE], "应采用重解析后的 manager"
        assert old_dm.removed == [], "旧（已 cleanup 的）manager 不得再被使用"

    @pytest.mark.asyncio
    async def test_reload_window_refuses_loudly_instead_of_noop(self, monkeypatch):
        """条目仍在、data 未就绪（reload 让出窗）→ 如实拒绝，不得静默 no-op。

        2026-10-26 判据升级：旧实装只 `_LOGGER.warning` 后 `return`，
        `button.press` 照样回 200 ⇒ 面板弹**绿色**「删除命令已发送」，而本地删除一件没做
        （改令牌/改选项/升级触发条目 reload 的秒级窗口内点「删除」必中）。
        拒绝写在 HA 日志里＝客户看不见，等于谎报。现在必须抛 HomeAssistantError
        （HA 转 400＋message，面板渲染出口把真话摆出来）。"""
        from homeassistant.exceptions import HomeAssistantError

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        old_dm = _RemoveDM()
        entry = SimpleNamespace(entry_id="e1")
        hass = SimpleNamespace(
            data={c.DOMAIN: {}},                       # 让出窗内 data 被清
            config_entries=SimpleNamespace(async_entries=lambda d: [entry]),
        )
        with pytest.raises(HomeAssistantError) as ei:
            await _remove_button(hass, old_dm, "e1").async_press()
        assert "重载" in str(ei.value) and "没有执行" in str(ei.value), \
            f"拒因必须点名『正在重载、删除没做成』，实得 {str(ei.value)!r}"
        assert old_dm.removed == [], "重载窗内不得拿旧引用做删除（幽灵设备燃料）"

    @pytest.mark.asyncio
    async def test_entry_gone_still_performs_local_cleanup(self, monkeypatch):
        """条目确已删除 → 设备随条目收口，本地清理照做（幂等自清理语义不变）。"""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        dm = _RemoveDM()
        hass = SimpleNamespace(
            data={c.DOMAIN: {}},
            config_entries=SimpleNamespace(async_entries=lambda d: []),
        )
        await _remove_button(hass, dm, "e1").async_press()
        assert dm.removed == [DEV_CAPABLE]

    @pytest.mark.asyncio
    async def test_probe_failure_falls_back_to_original_behavior(self, monkeypatch):
        """配置面探测不了（无 config_entries 替身）→ 退回原行为，不阻断用户。"""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        dm = _RemoveDM()
        hass = SimpleNamespace(data={c.DOMAIN: {}})
        await _remove_button(hass, dm, "e1").async_press()
        assert dm.removed == [DEV_CAPABLE]


async def _no_sleep(_seconds):
    return None
