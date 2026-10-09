"""v1.7.21：机型百分比能力分流 + 位置命令合并（机制二）。

实机背景（用户 2026-09-10 提供）：
- Apple 窗子磁贴**拖动期持续写** TargetPosition（broker 日志 34→46→47，
  间隔约 250ms），旧实现每条直发 004 → 一次拖动十几条报文全压到 LoRa 空口；
- 权威机型矩阵：5001/5003/5005/5006/5007 支持百分比；5002 平开窗暂不支持
  （用户："以后有可能支持"）。对 5002 声明 SET_POSITION 会造成
  ①"拖了没反应"的假滑块 ②丢失 WindowCoveringBasic 的三段式暂停
  （上游 homekit/type_covers.py：>70 开 / <30 关 / 中间 stop）。

合并语义（用户拍板机制二）：首发立即发（保住 v1.6.9 failfast），
窗口内只记最后值，静默 POSITION_COALESCE_SECONDS 后补发一条。
"""
import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.window_controller_gateway.cover import WindowControllerCover
from custom_components.window_controller_gateway.const import (
    DEVICE_STATUS_CLOSED,
    DEVICE_STATUS_OPEN,
    POSITION_CAPABLE_SN_PREFIXES,
    POSITION_COALESCE_SECONDS,
)

CAPABLE_MASK = 1 | 2 | 4 | 8   # OPEN | CLOSE | SET_POSITION | STOP
NO_POSITION_MASK = 1 | 2 | 8   # OPEN | CLOSE | STOP


class RecHandler:
    """可编程 mqtt_handler 替身：记录下发参数，result/exc 控制成败。"""

    def __init__(self, result=True, exc=None):
        self.gateway_sn = "GW1"
        self._result = result
        self._exc = exc
        self.calls = []

    async def send_command(self, sn, command, params=None):
        self.calls.append((sn, command, params))
        if self._exc is not None:
            raise self._exc
        return self._result


class FakeLoop:
    """可控 loop：捕获 call_later，测试自行触发回调。"""

    def __init__(self):
        self.timers = []

    def call_later(self, delay, cb):
        handle = SimpleNamespace(delay=delay, cb=cb, cancelled=False)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        self.timers.append(handle)
        return handle


class FakeHass:
    def __init__(self):
        self.data = {}
        self.loop = FakeLoop()
        self.tasks = []

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class FakeDeviceManager:
    def __init__(self, status=None, attributes=None):
        self._device = None
        if status is not None:
            self._device = {"sn": "x", "status": status, "attributes": attributes or {}}

    def get_device(self, device_sn):
        return self._device


def _cover(sn, handler=None, hass=None, status=None, attributes=None):
    return WindowControllerCover(
        hass=hass,
        device_manager=FakeDeviceManager(status, attributes),
        mqtt_handler=handler,
        gateway_sn="GW1",
        device_sn=sn,
        device_name="窗",
    )


def _live_timers(hass):
    return [t for t in hass.loop.timers if not t.cancelled]


async def _fire_trailing(hass):
    """触发最后一个未取消的合并定时器，并驱动其 create_task"""
    live = _live_timers(hass)
    assert live, "没有存活的合并定时器"
    live[-1].cb()
    if hass.tasks:
        await asyncio.gather(*hass.tasks)


class TestModelCapability:
    """SN 前四位 = 机型码 → 百分比能力（const.POSITION_CAPABLE_SN_PREFIXES）"""

    def test_position_attr_only_for_capable_models(self):
        """v1.8.6（审计 G-4 第二条）：无能力机型的属性里**不得出现 position**。

        旧实现 `cover.py` 无条件写 `attrs["position"]=钳制(r_travel)`，而
        `current_cover_position`（:267）与 Web 面板都按能力判——于是 5002 这类
        机型常态并排显示「不支持百分比定位」＋「位置: 65%」，且这条矛盾**不需要
        /states 失败**就存在。反向臂（有能力的仍要带 position）是判据另一半：
        只禁无能力侧，改成"谁都不写"也能过。
        """
        capable = _cover("500700000001", status=DEVICE_STATUS_OPEN,
                         attributes={"r_travel": 65, "voltage": 12.0})
        plain = _cover("500200000001", status=DEVICE_STATUS_OPEN,
                       attributes={"r_travel": 65, "voltage": 12.0})
        assert capable.extra_state_attributes.get("position") == 65, \
            capable.extra_state_attributes
        assert "position" not in plain.extra_state_attributes, \
            "无能力机型还在写 position：%r" % (plain.extra_state_attributes,)
        # 原始行程仍要无条件持久化（v1.6.26 B-2：区分"真 100%"与"未校准 255 被钳成 100"）
        assert plain.extra_state_attributes.get("r_travel_raw") == 65, \
            plain.extra_state_attributes

    @pytest.mark.parametrize("prefix", sorted(POSITION_CAPABLE_SN_PREFIXES))
    def test_capable_models_declare_set_position(self, prefix):
        feats = _cover(f"{prefix}00000001")._attr_supported_features
        assert feats & 4, f"{prefix} 应声明 SET_POSITION（HomeKit 才有真位置滑块）"
        assert feats == CAPABLE_MASK

    def test_5002_flat_window_has_no_position(self):
        """5002 平开窗：无百分比硬件 → 不声明 SET_POSITION。

        于是 HomeKit 落回 WindowCoveringBasic（Apple 三段式：>70 开 /
        <30 关 / 中间停=暂停），不会再出现假滑块。
        """
        feats = _cover("500200000001")._attr_supported_features
        assert not feats & 4
        assert feats == NO_POSITION_MASK

    @pytest.mark.parametrize("sn", ["100020003001", "9999X", "", "5", "500"])
    def test_unknown_prefix_defaults_to_no_position(self, sn):
        """未知前缀按"不支持"兜底：功能退化为三态而非假动作"""
        feats = _cover(sn)._attr_supported_features
        assert not feats & 4, f"{sn!r} 未知机型不得声明百分比能力"

    def test_real_position_only_for_capable(self):
        assert _cover("500700000001", status=DEVICE_STATUS_OPEN,
                      attributes={"r_travel": 65}).current_cover_position == 65
        assert _cover("500200000001", status=DEVICE_STATUS_OPEN,
                      attributes={"r_travel": 65}).current_cover_position is None

    def test_endpoint_fallback_only_for_capable(self):
        # 已校准机型未报告位置（255）时按开/关端点兜底（v1.7.20 方案 a 不变）
        assert _cover("500700000001", status=DEVICE_STATUS_OPEN,
                      attributes={"r_travel": 255}).current_cover_position == 100
        # 无百分比机型恒 None（位置由 Apple 依 state 自行映射）
        assert _cover("500200000001", status=DEVICE_STATUS_CLOSED,
                      attributes={"r_travel": 255}).current_cover_position is None

    @pytest.mark.asyncio
    async def test_set_position_rejected_for_non_capable(self):
        """HA 核心服务层已按 feature 拦截；实体层兜底防内部误用，
        绝不把无效 w_travel 指令打到 LoRa 空口。"""
        handler = RecHandler()
        cover = _cover("500200000001", handler=handler, hass=FakeHass())
        with pytest.raises(HomeAssistantError):
            await cover.async_set_cover_position(position=50)
        assert handler.calls == []

    def test_capability_exposed_in_attributes(self):
        assert _cover("500700000001").extra_state_attributes["position_capable"] is True
        assert _cover("500200000001").extra_state_attributes["position_capable"] is False


class TestPositionCoalescing:
    """机制二：首发立即 + 窗口内只发最终值"""

    @pytest.mark.asyncio
    async def test_leading_call_sends_immediately(self):
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        await cover.async_set_cover_position(position=34)
        assert handler.calls == [("500700000001", "set_position", {"position": 34})]
        assert hass.loop.timers == []  # 首发路径不建定时器

    @pytest.mark.asyncio
    async def test_burst_sends_first_and_last_only(self):
        """实机拖动序列 34→46→47：应只发 34（立即）与 47（补发）"""
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        await cover.async_set_cover_position(position=34)
        await cover.async_set_cover_position(position=46)
        await cover.async_set_cover_position(position=47)
        assert [c[2]["position"] for c in handler.calls] == [34], "窗口内不得直发"
        await _fire_trailing(hass)
        assert [c[2]["position"] for c in handler.calls] == [34, 47], "补发必须是最后值"
        # 每次窗口内调用都重置定时器：只有最后一个是活的
        assert len(_live_timers(hass)) == 0 or _live_timers(hass)[-1].delay == POSITION_COALESCE_SECONDS

    @pytest.mark.asyncio
    async def test_leading_again_after_window(self):
        """窗口之外的调用仍是首发立即（自动化/服务调用不受合并延迟影响）"""
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        await cover.async_set_cover_position(position=10)
        cover._last_position_send -= (POSITION_COALESCE_SECONDS + 0.1)
        await cover.async_set_cover_position(position=90)
        assert [c[2]["position"] for c in handler.calls] == [10, 90]
        assert hass.loop.timers == []

    @pytest.mark.asyncio
    async def test_failed_leading_still_raises_and_keeps_immediate(self):
        """failfast 契约（v1.6.9）在合并机制下不得丢失：未送达同步抛错，
        且失败不占用合并窗口（下一次调用仍立即发）。"""
        handler = RecHandler(result=False)
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        with pytest.raises(HomeAssistantError):
            await cover.async_set_cover_position(position=20)
        with pytest.raises(HomeAssistantError):
            await cover.async_set_cover_position(position=30)
        assert len(handler.calls) == 2, "失败后不得把后续调用吞进合并窗口"

    @pytest.mark.asyncio
    async def test_trailing_failure_logged_not_raised(self):
        """补发路径无调用方可抛：失败只记 warning（机制二的契约边界）"""
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        await cover.async_set_cover_position(position=40)
        handler._result = False
        await cover.async_set_cover_position(position=60)
        await _fire_trailing(hass)  # 不抛异常
        assert [c[2]["position"] for c in handler.calls] == [40, 60]

    @pytest.mark.asyncio
    async def test_remove_cancels_pending(self):
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        await cover.async_set_cover_position(position=10)
        await cover.async_set_cover_position(position=80)
        await cover.async_will_remove_from_hass()
        assert all(t.cancelled for t in hass.loop.timers)
        assert cover._pending_position is None

    @pytest.mark.asyncio
    async def test_no_hass_drops_pending(self):
        """实体已移除（hass=None）时窗口内调用直接放弃，不留悬挂 pending"""
        handler = RecHandler()
        cover = _cover("500700000001", handler=handler, hass=None)
        await cover.async_set_cover_position(position=10)   # 首发立即（无需 hass）
        await cover.async_set_cover_position(position=70)   # 窗口内但无 hass
        assert cover._pending_position is None
        assert [c[2]["position"] for c in handler.calls] == [10]

    @pytest.mark.asyncio
    async def test_invalid_value_rejected_before_coalescing(self):
        handler = RecHandler()
        hass = FakeHass()
        cover = _cover("500700000001", handler=handler, hass=hass)
        for bad in (-1, 101, None, "abc"):
            with pytest.raises(HomeAssistantError):
                await cover.async_set_cover_position(position=bad)
        assert handler.calls == []
        assert hass.loop.timers == []
