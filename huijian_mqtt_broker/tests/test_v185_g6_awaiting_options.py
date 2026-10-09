"""v1.8.5 G-6：awaiting 分支对 entry.options 的零读取（保存静默无效）。

病灶（审计口径）：`async_setup_entry` 的等待分支（无 gateway SN）**从不读
`entry.options`**，而选项页对等待条目是**可达的**（config_flow `async_step_init`
的 E-1 菜单就是把 `options` 摆给无 SN 条目看的）。于是：

* `debug_logging`：唯一消费者在完整分支里，等待条目永远不进
  `_debug_logging_entries`、模块 logger 也永远不点亮——而"网关还没被发现"
  恰恰是最需要 DEBUG 的时刻（发现链断裂、心跳耳异常都只在这段可见）。
* `auto_discovery`：唯一消费者 `mqtt_handler._lifecycle._auto_discovery_enabled`
  挂在 `device_manager` 上，等待条目没有 mqtt_handler ⇒ 用户取消勾选后
  **发现卡照弹**。
* `discovery_interval`：唯一节拍源是 `mqtt_handler.check_connection()`，等待期
  本就不存在——这一条不修，只在表单/日志层说明（见分支内注释）。

本文件同时钉住两条**不许被顺手改坏**的既有语义：
  1. 代答 001（风暴止血）**不得**被 auto_discovery 门控——用户关掉的是"弹卡
     打扰"，不是"让固件每 5s 重发的风暴继续流血"。
  2. 字段缺失/读取异常一律回落到历史默认 True，绝不因为一次读取失败把用户
     已有网关的自动发现静默关掉。
"""

import asyncio
import logging
import pathlib
import re
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway as pkg
import custom_components.window_controller_gateway.discovery as disc
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import custom_components.window_controller_gateway.utils as utils
import custom_components.window_controller_gateway.ws_gateway as wsg
import homeassistant.components.mqtt as fake_mqtt
from homeassistant.core import FakeServices

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"

import enum


class _StrEnumState(enum.StrEnum):
    """HA 2024.4+ 形态：str(x) 即值。"""
    LOADED = "loaded"


GW = "100199999999"
EID = "E_AWAIT"

AWAIT_ANCHOR = "# ---- 无网关 SN：最小设置，等待后续配置 ----"
FULL_ANCHOR = "# ---- 有网关 SN：完整设置 ----"


def _awaiting_entry(options=None):
    return SimpleNamespace(
        entry_id=EID,
        data={},
        options={} if options is None else options,
        title="慧尖网关（等待配置）",
        state=_StrEnumState.LOADED,
        disabled_by=None,
        async_on_unload=lambda fn: None,
        add_update_listener=lambda fn: lambda: None,
    )


class _Hass:
    def __init__(self, entries=(), config_dir="/config"):
        self.data = {pkg.DOMAIN: {}}
        self.is_stopping = False
        self.loop = None
        self.config = SimpleNamespace(config_dir=config_dir)
        self.services = FakeServices()
        self._entries = list(entries)
        self.tasks = []
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(self._entries),
            flow=SimpleNamespace(async_progress=lambda: []),
        )

    def async_create_task(self, coro, name=None):
        t = asyncio.ensure_future(coro)
        self.tasks.append(t)
        return t

    def set_entries(self, entries):
        self._entries = list(entries)


class _Pub:
    def __init__(self):
        self.calls = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        import json
        self.calls.append((topic, json.loads(payload), qos, retain))


async def _arm(monkeypatch, hass, pub, discovered, entry):
    """跑真 awaiting 分支，抓真 _heartbeat_listener（判定逻辑不打桩）。"""
    captured = {}

    async def fake_subscribe(h, topic, cb, qos=0):
        captured["cb"] = cb
        return lambda: None

    async def noop_async(h):
        return None

    monkeypatch.setattr(fake_mqtt, "async_subscribe", fake_subscribe)
    monkeypatch.setattr(fake_mqtt, "async_publish", pub)
    monkeypatch.setattr(pkg, "is_mqtt_loaded", lambda h: True)
    monkeypatch.setattr(mb, "ensure_mqtt_connection", noop_async)
    monkeypatch.setattr(mb, "async_start_bootstrap_healer", lambda h: None)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop_async)
    monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)

    async def fake_discover(h, sn, name, *a, **k):
        discovered.append((sn, name))

    monkeypatch.setattr(disc, "async_discover_gateway", fake_discover)

    assert await pkg.async_setup_entry(hass, entry) is True
    assert "cb" in captured, "awaiting 分支必须挂上心跳监听器（耳朵本体）"
    assert hass.data[pkg.DOMAIN][entry.entry_id].get("_unsub_heartbeat"), \
        "台架完整性：监听器句柄必须真登记进 runtime（防空跑绿）"
    return captured["cb"]


def _report(sn=GW, ctype="001", msg_id=7):
    import json
    payload = {"head": "$SH", "ctype": ctype, "id": msg_id, "sn": sn,
               "data": {"vesion": "V3.55", "model": "YGZN_GW001"}}
    return SimpleNamespace(topic="gateway/rpt_rsp",
                           payload=json.dumps(payload).encode())


async def _drain(hass):
    for _ in range(3):
        pending = [t for t in hass.tasks if not t.done()]
        if pending:
            await asyncio.wait(pending)
        await asyncio.sleep(0)


@pytest.fixture
def debug_state():
    """保/还原模块级 debug 引用计数与 logger 级别（跨用例不串味）。"""
    saved_set = set(pkg._debug_logging_entries)
    saved_level = pkg._LOGGER.level
    yield
    pkg._debug_logging_entries.clear()
    pkg._debug_logging_entries.update(saved_set)
    pkg._LOGGER.setLevel(saved_level)


# ============ ① auto_discovery 在等待条目上真的生效 ============
class TestAwaitingAutoDiscoveryGate:
    @pytest.mark.asyncio
    async def test_auto_discovery_off_suppresses_the_discovery_card(self, monkeypatch):
        """主修面：等待条目上关掉自动发现 → 心跳耳不得再弹发现卡。"""
        pub, discovered = _Pub(), []
        hass = _Hass()
        entry = _awaiting_entry({"auto_discovery": False})
        cb = await _arm(monkeypatch, hass, pub, discovered, entry)
        hass.set_entries([entry])

        await cb(_report())
        await _drain(hass)

        assert discovered == [], \
            "auto_discovery=False 必须让等待条目的发现卡停下（G-6 主修面）"

    @pytest.mark.asyncio
    async def test_ack_still_sent_when_auto_discovery_off(self, monkeypatch):
        """反钉：关掉的是"弹卡打扰"，不是风暴止血——001 代答必须照发。

        用户关掉自动发现，固件每 5s 重发的 001 仍然无人应答；把代答一起门控掉
        等于让网关继续流血，属于把"少打扰"做成"少止血"。
        """
        pub, discovered = _Pub(), []
        hass = _Hass()
        entry = _awaiting_entry({"auto_discovery": False})
        cb = await _arm(monkeypatch, hass, pub, discovered, entry)
        hass.set_entries([entry])

        await cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1, f"代答不得被 auto_discovery 门控，实得 {pub.calls}"
        topic, payload, _, _ = pub.calls[0]
        assert topic == f"gateway/{GW}/req"
        assert payload["ctype"] == "001" and payload["data"]["errcode"] == 0

    @pytest.mark.asyncio
    async def test_auto_discovery_on_still_discovers(self, monkeypatch):
        """正向对照：显式 True 与缺省都保持历史行为（发现卡照弹）。"""
        for opts in ({"auto_discovery": True}, {}, None):
            pub, discovered = _Pub(), []
            hass = _Hass()
            entry = _awaiting_entry(opts)
            cb = await _arm(monkeypatch, hass, pub, discovered, entry)
            hass.set_entries([entry])

            await cb(_report())
            await _drain(hass)

            assert [sn for sn, _ in discovered] == [GW], \
                f"options={opts!r} 时必须保持历史行为（弹卡）"

    @pytest.mark.asyncio
    async def test_gate_defaults_to_true_on_read_failure(self, monkeypatch):
        """异常回落：读 options 抛错绝不能让自动发现被静默关掉。"""
        class _Boom:
            @property
            def options(self):
                raise RuntimeError("boom")

        assert pkg._auto_discovery_enabled(_Boom()) is True

    def test_gate_matches_lifecycle_semantics(self):
        """同语义钉：默认值与 mqtt_handler 侧那条必须一致。"""
        src = (PKG / "mqtt_handler" / "_lifecycle.py").read_text(encoding="utf-8")
        assert "options.get(CONF_AUTO_DISCOVERY, True)" in src, \
            "mqtt_handler 侧默认值变了——两侧口径必须同步（否则同一勾选两处行为不同）"
        mine = (PKG / "__init__.py").read_text(encoding="utf-8")
        assert "options.get(CONF_AUTO_DISCOVERY, True)" in mine, \
            "等待分支侧默认值必须同为 True"


# ============ ② debug_logging 在等待条目上真的生效 ============
class TestAwaitingDebugLogging:
    @pytest.mark.asyncio
    async def test_debug_logging_on_lights_up_the_module_logger(
            self, monkeypatch, debug_state):
        """主修面：等待条目上打开 debug_logging → 模块 logger 真进 DEBUG。"""
        pub, discovered = _Pub(), []
        hass = _Hass()
        entry = _awaiting_entry({"debug_logging": True})
        await _arm(monkeypatch, hass, pub, discovered, entry)

        assert pkg._LOGGER.level == logging.DEBUG, \
            "等待条目保存的 debug_logging 必须真点亮模块 logger（G-6 主修面）"
        assert EID in pkg._debug_logging_entries, "必须登记进引用计数集合"

    @pytest.mark.asyncio
    async def test_debug_logging_absent_leaves_logger_untouched(
            self, monkeypatch, debug_state, caplog):
        """反钉：默认（False 且未登记）不得动 logger，也不得每轮白打一行关闭日志。"""
        pub, discovered = _Pub(), []
        hass = _Hass()
        entry = _awaiting_entry({})
        before = pkg._LOGGER.level

        with caplog.at_level(logging.INFO, logger=pkg.__name__):
            await _arm(monkeypatch, hass, pub, discovered, entry)

        assert pkg._LOGGER.level == before, "缺省不得改动模块 logger 级别"
        assert EID not in pkg._debug_logging_entries
        assert "调试日志已关闭" not in caplog.text, \
            "缺省路径不得打「已关闭」（每条等待条目 setup 都刷一行是噪音）"

    @pytest.mark.asyncio
    async def test_debug_logging_off_after_on_restores_level(
            self, monkeypatch, debug_state):
        """双向：先开再关 → 集合清空后级别回落为 NOTSET（引用计数语义不变）。"""
        entry_on = _awaiting_entry({"debug_logging": True})
        await _arm(monkeypatch, _Hass(), _Pub(), [], entry_on)
        assert pkg._LOGGER.level == logging.DEBUG

        entry_off = _awaiting_entry({"debug_logging": False})
        await _arm(monkeypatch, _Hass(), _Pub(), [], entry_off)

        assert EID not in pkg._debug_logging_entries
        assert not pkg._debug_logging_entries, "只有集合清空才允许回落"
        assert pkg._LOGGER.level == logging.NOTSET, \
            "引用计数清空后必须恢复继承 HA logger 配置"

    @pytest.mark.asyncio
    async def test_two_entries_do_not_clobber_each_other(
            self, monkeypatch, debug_state):
        """引用计数原意：一条关掉不得把另一条还开着的 DEBUG 一起拉黑。"""
        a = _awaiting_entry({"debug_logging": True})
        a.entry_id = "E_A"
        await _arm(monkeypatch, _Hass(), _Pub(), [], a)

        b = _awaiting_entry({"debug_logging": True})
        b.entry_id = "E_B"
        await _arm(monkeypatch, _Hass(), _Pub(), [], b)
        assert pkg._LOGGER.level == logging.DEBUG

        a_off = _awaiting_entry({"debug_logging": False})
        a_off.entry_id = "E_A"
        await _arm(monkeypatch, _Hass(), _Pub(), [], a_off)

        assert "E_B" in pkg._debug_logging_entries and "E_A" not in pkg._debug_logging_entries
        assert pkg._LOGGER.level == logging.DEBUG, \
            "还有条目开着 DEBUG 时不得回落（P1 引用计数的原始动机）"


# ============ ③ 结构钉：等待分支必须真读 options ============
class TestAwaitingOptionsWiring:
    def _awaiting_segment(self):
        src = (PKG / "__init__.py").read_text(encoding="utf-8")
        i = src.index(AWAIT_ANCHOR)
        j = src.index(FULL_ANCHOR, i)
        return src, src[i:j]

    def test_awaiting_branch_reads_entry_options(self):
        """本次修复的本质：这段必须出现 options 读取（否则就是回到零读取）。"""
        _, seg = self._awaiting_segment()
        assert "entry.options" in seg, \
            "等待分支又回到对 entry.options 零读取（G-6 回潮）"
        assert "CONF_DEBUG_LOGGING" in seg, "等待分支必须消费 debug_logging"
        assert "_auto_discovery_enabled(" in seg, "等待分支必须消费 auto_discovery"

    def test_gate_sits_before_the_discovery_call(self):
        """门控必须在弹卡调用之前（顺序即语义）。"""
        _, seg = self._awaiting_segment()
        i_gate = seg.index("_auto_discovery_enabled(entry)")
        i_call = seg.index("await async_discover_gateway(")
        assert i_gate < i_call, "auto_discovery 门控必须在 async_discover_gateway 之前"

    def test_gate_is_not_applied_to_the_ack(self):
        """反钉：门控不得上移到代答之前（代答是风暴止血，不看这套选项）。"""
        _, seg = self._awaiting_segment()
        i_ack = seg.index("async_ear_ack_001_arbitrated")
        i_gate = seg.index("_auto_discovery_enabled(entry)")
        assert i_ack < i_gate, \
            "auto_discovery 门控不得罩住 001 代答（那会把止血做成流血）"

    def test_debug_block_is_shared_with_full_branch(self):
        """单一出口钉：两分支共用 _apply_debug_logging，不得各写一份 setLevel。"""
        src, seg = self._awaiting_segment()
        assert "_apply_debug_logging(" in seg, "等待分支必须走共用出口"
        assert not re.search(r"^\s+_LOGGER\.setLevel\(", seg, re.M), \
            "等待分支不得内联 setLevel（口径必须单一真源）"
        assert "_apply_debug_logging(" in src[src.index(FULL_ANCHOR):], \
            "完整分支也必须走同一出口"

    def test_discovery_interval_is_documented_as_not_consumed(self):
        """诚实钉：discovery_interval 在等待期无消费方，必须在代码里写明。"""
        _, seg = self._awaiting_segment()
        assert "discovery_interval" in seg, \
            "等待分支不消费 discovery_interval 这件事必须留在代码里（免得下一轮又当 bug 报）"
