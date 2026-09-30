"""v1.7.34 钉桩：三态门 → 四态门（entry_state_for_sn 补「条目未加载」态）。

修的盲区（v1.7.31 A-3 遗留）：命中条目处于 setup_error/setup_retry/not_loaded
时旧实现照样返回 "configured" ⇒ 两只耳朵一起静默"让位"一个根本没订阅的
handler：网关 001 无人应答、固件每 5s 重发永不停血、日志**零留痕**。用户视角
比禁用态更难归因——条目明明就在 设置→设备与服务 列表里。

守门面（CLAUDE.md 教训：静默失效面必须双向判）：
- 正向：未加载条目命中 → 耳朵照答止血 + 节流 WARNING 留痕 + **不弹**发现卡
  （条目已在列表里，async_discover_gateway 第 3 步命中同 SN 本就早退）。
- 反向：loaded / setup_in_progress 等"在飞或将答"形态必须**继续静默**——
  否则与正式 handler 双答（v1.7.30 仲裁只收编耳朵之间的重复，管不住
  耳朵 vs handler），把 v1.7.30 台架实锤的"1 请求 2~3 答"噪声面放回来。
- 状态未知（无 state 属性的替身/老 core）按 configured 处理：既有语义零变化。
- 行为证据用真物件跑：心跳耳经 async_setup_entry 的 awaiting 分支真抓
  _heartbeat_listener；_protocol 耳经真 WindowControllerMQTTHandler 真抓
  handle_gateway_response——判定逻辑不打桩。
"""
import asyncio
import enum
import json
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
from custom_components.window_controller_gateway.const import (
    DOMAIN, CONF_GATEWAY_SN)
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler)
from custom_components.window_controller_gateway.utils import (
    entry_state_for_sn, entry_state_name, ENTRY_STATES_UNANSWERED)

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"

GW = "100199999999"      # 上报中的网关（被测对象）
OWN = "100188888888"     # _protocol 耳所属的另一台已加载网关
UNLOADED_HINT = "未加载"  # 两耳 WARNING 的共同关键词（留痕断言锚）


# ============ 状态形态替身 ============
class _StrEnumState(enum.StrEnum):
    """HA 2024.4+ 形态：str(x) 即值。"""
    LOADED = "loaded"
    SETUP_RETRY = "setup_retry"


class _LegacyState(str, enum.Enum):
    """旧 core str+Enum 混血：str(x) 得限定名，.value 才是裸名。"""
    LOADED = "loaded"
    SETUP_ERROR = "setup_error"
    NOT_LOADED = "not_loaded"


class _QualifiedState:
    """限定名兜底形态（.value 本身带类名前缀）。"""
    value = "ConfigEntryState.setup_retry"


def _entry(sn, state=None, disabled=None, entry_id=None):
    e = SimpleNamespace(
        entry_id=entry_id or ("E_" + (sn or "AWAIT")),
        data=({CONF_GATEWAY_SN: sn} if sn else {}),
        disabled_by=disabled,
        options={},
        title="慧尖网关",
    )
    if state is not None:
        e.state = state
    return e


class _Hass:
    def __init__(self, entries=(), config_dir="/config"):
        self.data = {DOMAIN: {}}
        self.is_stopping = False
        self.loop = None
        self.config = SimpleNamespace(config_dir=config_dir)
        # 审计 2026-09-30 C-1 配套：真 HA 的 hass.services 恒在，而本替身此前没有
        # 这一面——async_setup_entry 补上"域级服务重注册"后，桩比真实现窄就直接
        # 把条目 setup 判成失败（本仓纪律：测试替身不得窄于真实现）。
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
        self.calls.append((topic, json.loads(payload), qos, retain))


class _AwaitingEntry:
    """等待条目（空 SN）：心跳耳的宿主。"""
    entry_id = "E_AWAIT"
    data = {}
    options = {}
    title = "慧尖网关（等待配置）"
    state = _StrEnumState.LOADED
    disabled_by = None

    def async_on_unload(self, fn):
        return None

    def add_update_listener(self, fn):
        return lambda: None


# ============ ① 四态门单元判定 ============
class TestGateFourStates:
    @staticmethod
    def _hass(entries):
        return _Hass(entries)

    @pytest.mark.parametrize("state", [
        "not_loaded", "setup_retry", "setup_error",
        _LegacyState.NOT_LOADED, _LegacyState.SETUP_ERROR,
        _StrEnumState.SETUP_RETRY, _QualifiedState,
    ])
    def test_unanswered_states_yield_not_loaded(self, state):
        assert entry_state_for_sn(self._hass([_entry(GW, state)]), GW) \
            == "not_loaded", f"{state!r} 必须判为未加载（耳朵顶上止血）"

    @pytest.mark.parametrize("state", [
        "loaded", "setup_in_progress", "unload_in_progress",
        "migration_in_progress", _StrEnumState.LOADED, _LegacyState.LOADED,
    ])
    def test_inflight_states_stay_configured(self, state):
        """反钉：在飞/已加载必须继续静默，否则与正式 handler 双答。"""
        assert entry_state_for_sn(self._hass([_entry(GW, state)]), GW) \
            == "configured", f"{state!r} 是应答者在飞形态，不得让耳朵插嘴"

    def test_state_absent_keeps_legacy_configured(self):
        """状态未知（替身/老 core 无 state）⇒ 既有语义零变化。"""
        assert entry_state_for_sn(self._hass([_entry(GW)]), GW) == "configured"

    def test_disabled_wins_over_unanswered(self):
        """禁用条目本身也是 not_loaded 态——禁用决策优先（A-3 口径不倒退）。"""
        assert entry_state_for_sn(
            self._hass([_entry(GW, "setup_retry", disabled="user")]), GW) \
            == "disabled"

    def test_one_loaded_sibling_masks_unanswered(self):
        """同 SN 多条目：只要有一个在飞/已加载即 configured（有人在答）。"""
        assert entry_state_for_sn(self._hass([
            _entry(GW, "setup_error", entry_id="E_BAD"),
            _entry(GW, "loaded", entry_id="E_OK"),
        ]), GW) == "configured"

    def test_all_siblings_unanswered(self):
        assert entry_state_for_sn(self._hass([
            _entry(GW, "setup_error", entry_id="E_BAD1"),
            _entry(GW, "not_loaded", entry_id="E_BAD2"),
        ]), GW) == "not_loaded"

    def test_none_still_none(self):
        assert entry_state_for_sn(self._hass([]), GW) == "none"

    def test_unanswered_set_is_pinned(self):
        """真源集合钉：三态齐全，且不含任何"在飞"态。"""
        assert ENTRY_STATES_UNANSWERED == {"not_loaded", "setup_error",
                                          "setup_retry"}
        for inflight in ("loaded", "setup_in_progress", "unload_in_progress",
                         "migration_in_progress"):
            assert inflight not in ENTRY_STATES_UNANSWERED, \
                f"{inflight} 进集合即造成与正式 handler 双答"


class TestStateNameNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("loaded", "loaded"),
        ("NOT_LOADED", "not_loaded"),
        (_StrEnumState.SETUP_RETRY, "setup_retry"),
        (_LegacyState.SETUP_ERROR, "setup_error"),
        (_QualifiedState, "setup_retry"),
    ])
    def test_forms(self, raw, expected):
        assert entry_state_name(SimpleNamespace(state=raw)) == expected

    def test_missing_or_unusable(self):
        assert entry_state_name(SimpleNamespace()) == ""
        assert entry_state_name(SimpleNamespace(state=None)) == ""
        assert entry_state_name(SimpleNamespace(state=42)) == ""


# ============ ② 心跳耳行为（真 async_setup_entry awaiting 分支） ============
async def _arm_heartbeat_ear(monkeypatch, hass, pub, discovered):
    """跑真 awaiting 分支，抓出真 _heartbeat_listener（判定逻辑不打桩）。"""
    captured = {}

    async def fake_subscribe(h, topic, cb, qos=0):
        captured["cb"] = cb
        captured["topic"] = topic
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

    assert await pkg.async_setup_entry(hass, _AwaitingEntry()) is True
    assert "cb" in captured, "awaiting 分支必须挂上心跳监听器（耳朵本体）"
    assert hass.data[DOMAIN][_AwaitingEntry.entry_id].get("_unsub_heartbeat"), \
        "台架完整性：监听器句柄必须真登记进 runtime（防空跑绿）"
    return captured["cb"]


def _report(sn=GW, ctype="001", msg_id=7, data=None):
    payload = {"head": "$SH", "ctype": ctype, "id": msg_id, "sn": sn,
               "data": {"vesion": "V3.55", "model": "YGZN_GW001"}
               if data is None else data}
    return SimpleNamespace(topic="gateway/rpt_rsp",
                           payload=json.dumps(payload).encode())


async def _drain(hass):
    """让代答/看守协程跑完（看守延时已压成 0）。"""
    for _ in range(3):
        pending = [t for t in hass.tasks if not t.done()]
        if pending:
            await asyncio.wait(pending)
        await asyncio.sleep(0)


class TestHeartbeatEar:
    @pytest.mark.asyncio
    async def test_unanswered_entry_gets_ack_and_warning(self, monkeypatch,
                                                         caplog):
        """主修面：条目 setup 失败/重试中 → 耳朵代答止血 + WARNING 留痕。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass()
        cb = await _arm_heartbeat_ear(monkeypatch, hass, pub, discovered)
        hass.set_entries([_AwaitingEntry(), _entry(GW, "setup_retry")])

        await cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1, f"必须恰好一条代答，实得 {pub.calls}"
        topic, payload, qos, retain = pub.calls[0]
        assert topic == f"gateway/{GW}/req"
        assert payload["ctype"] == "001" and payload["id"] == 7
        assert payload["data"]["errcode"] == 0 and payload["data"]["uuid"]
        warns = [r for r in caplog.records
                 if r.levelno >= logging.WARNING and UNLOADED_HINT in r.getMessage()]
        assert len(warns) == 1, f"未加载必须留 WARNING 痕，实得 {caplog.text}"
        assert discovered == [], "条目已在列表里，不得再弹发现卡"

    @pytest.mark.asyncio
    async def test_loaded_entry_stays_silent(self, monkeypatch, caplog):
        """反钉：健康条目形态耳朵必须整体静默（防与正式 handler 双答）。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass()
        cb = await _arm_heartbeat_ear(monkeypatch, hass, pub, discovered)
        hass.set_entries([_AwaitingEntry(), _entry(GW, "loaded")])

        await cb(_report())
        await _drain(hass)

        assert pub.calls == [], "已加载条目由正式 handler 应答，耳朵不得插嘴"
        assert discovered == []
        assert UNLOADED_HINT not in caplog.text

    @pytest.mark.asyncio
    async def test_unconfigured_still_discovers(self, monkeypatch, caplog):
        """既有语义不倒退：无条目 → 代答 + 发现链照走。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass()
        cb = await _arm_heartbeat_ear(monkeypatch, hass, pub, discovered)
        hass.set_entries([_AwaitingEntry()])

        await cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1
        assert [sn for sn, _ in discovered] == [GW], "未配置网关必须弹发现卡"

    @pytest.mark.asyncio
    async def test_storm_acks_every_request_but_throttles_log(self, monkeypatch,
                                                              caplog):
        """固件 5s 重发风暴：每条请求都得答（换 id），留痕只放一条。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass()
        cb = await _arm_heartbeat_ear(monkeypatch, hass, pub, discovered)
        hass.set_entries([_AwaitingEntry(), _entry(GW, "setup_error")])

        for i in range(6):
            await cb(_report(msg_id=100 + i))
        await _drain(hass)

        assert len(pub.calls) == 6, f"6 条请求必须 6 答，实得 {len(pub.calls)}"
        warns = [r for r in caplog.records
                 if r.levelno >= logging.WARNING and UNLOADED_HINT in r.getMessage()]
        assert len(warns) == 1, "留痕必须节流（10 分钟/SN），不得刷屏"
        assert discovered == []

    @pytest.mark.asyncio
    async def test_recovers_to_silent_after_setup_succeeds(self, monkeypatch,
                                                           caplog):
        """setup 重试成功 → 同一条耳朵立刻转静默（状态实时读，无缓存）。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass()
        cb = await _arm_heartbeat_ear(monkeypatch, hass, pub, discovered)
        bad = _entry(GW, "setup_retry")
        hass.set_entries([_AwaitingEntry(), bad])

        await cb(_report(msg_id=1))
        await _drain(hass)
        assert len(pub.calls) == 1

        bad.state = "loaded"          # HA 重试 setup 成功
        await cb(_report(msg_id=2))
        await _drain(hass)
        assert len(pub.calls) == 1, "转正后不得再代答（正式 handler 接管）"


# ============ ③ _protocol 耳行为（真 handler + 真回调） ============
async def _arm_protocol_ear(monkeypatch, hass, pub, discovered):
    captured = {}

    async def fake_subscribe(h, topic, cb, qos=0):
        captured["cb"] = cb
        return lambda: None

    monkeypatch.setattr(fake_mqtt, "async_subscribe", fake_subscribe)
    monkeypatch.setattr(fake_mqtt, "async_publish", pub)
    monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)

    async def fake_discover(h, sn, name, *a, **k):
        discovered.append((sn, name))

    monkeypatch.setattr(disc, "async_discover_gateway", fake_discover)

    hass.loop = asyncio.get_running_loop()
    handler = WindowControllerMQTTHandler(hass, OWN, None)
    assert await handler._do_subscribe_topics() is True
    assert "cb" in captured
    return captured["cb"], handler


class TestProtocolEar:
    @pytest.mark.asyncio
    async def test_unanswered_other_gateway_gets_ack_and_warning(self,
                                                                 monkeypatch,
                                                                 caplog):
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass([_entry(OWN, "loaded"), _entry(GW, "setup_error")])
        cb, handler = await _arm_protocol_ear(monkeypatch, hass, pub, discovered)

        cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1, f"他网关未加载 → 本耳必须代答，实得 {pub.calls}"
        topic, payload, _, _ = pub.calls[0]
        assert topic == f"gateway/{GW}/req"
        assert payload["data"]["errcode"] == 0 and payload["data"]["uuid"]
        warns = [r for r in caplog.records
                 if r.levelno >= logging.WARNING and UNLOADED_HINT in r.getMessage()]
        assert len(warns) == 1, f"未加载必须留 WARNING 痕，实得 {caplog.text}"
        assert discovered == [], "条目已在列表里，不得再弹发现卡"
        assert handler._unsub_rsp is not None, \
            "台架完整性：本耳必须是真 _do_subscribe_topics 挂上的订阅回调"

    @pytest.mark.asyncio
    async def test_loaded_other_gateway_stays_silent(self, monkeypatch, caplog):
        """反钉：他网关条目健康 ⇒ 由它自己的 handler 应答，本耳静默。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass([_entry(OWN, "loaded"), _entry(GW, "loaded")])
        cb, _ = await _arm_protocol_ear(monkeypatch, hass, pub, discovered)

        cb(_report())
        await _drain(hass)

        assert pub.calls == []
        assert discovered == []
        assert UNLOADED_HINT not in caplog.text

    @pytest.mark.asyncio
    async def test_unconfigured_other_gateway_still_discovers(self, monkeypatch,
                                                              caplog):
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass([_entry(OWN, "loaded")])
        cb, _ = await _arm_protocol_ear(monkeypatch, hass, pub, discovered)

        cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1
        assert [sn for sn, _ in discovered] == [GW]

    @pytest.mark.asyncio
    async def test_disabled_beats_unanswered(self, monkeypatch, caplog):
        """禁用+未加载同体：禁用文案优先，且不弹卡（A-3 口径不倒退）。"""
        caplog.set_level(logging.WARNING)
        pub, discovered = _Pub(), []
        hass = _Hass([_entry(OWN, "loaded"),
                      _entry(GW, "setup_retry", disabled="user")])
        cb, _ = await _arm_protocol_ear(monkeypatch, hass, pub, discovered)

        cb(_report())
        await _drain(hass)

        assert len(pub.calls) == 1, "禁用态照答止血（风暴在禁用侧无解）"
        assert discovered == []
        assert "禁用" in caplog.text and UNLOADED_HINT not in caplog.text


# ============ ④ 接线位置结构钉（两耳对称） ============
class TestWiringPins:
    def _seg(self, path, start_anchor, end_anchor):
        src = (PKG / path).read_text(encoding="utf-8")
        i = src.index(start_anchor)
        j = src.index(end_anchor, i)
        return src, i, j

    def test_heartbeat_ordering(self):
        """configured 早退 → not_loaded 留痕 → 代答 → 弹卡前短路。"""
        src, i_cfg, _ = self._seg(
            "__init__.py", 'if _st == "configured":', "心跳监听器发现新网关")
        i_warn = src.index('if _st == "not_loaded":', i_cfg)
        i_ack = src.index("async_ear_ack_001_arbitrated", i_warn)
        i_short = src.index('if _st in ("disabled", "not_loaded"):', i_ack)
        i_disc = src.index("心跳监听器发现新网关")
        assert i_cfg < i_warn < i_ack < i_short < i_disc, \
            "留痕必须在代答之前、短路必须在弹卡之前（顺序即语义）"
        assert '"_hb_unloaded_logged"' in src[i_warn:i_ack], \
            "未加载留痕必须走独立节流桶（与 disabled 桶分开，互不遮蔽）"

    def test_protocol_ordering(self):
        src, i_cfg, _ = self._seg(
            "mqtt_handler/_protocol.py", 'if _st == "configured":',
            "from ..discovery import async_discover_gateway")
        i_warn = src.index('if _st == "not_loaded":', i_cfg)
        i_ack = src.index("async_ear_ack_001_arbitrated", i_warn)
        i_short = src.index('if _st in ("disabled", "not_loaded"):', i_ack)
        i_disc = src.index("from ..discovery import async_discover_gateway")
        assert i_cfg < i_warn < i_ack < i_short < i_disc, \
            "_protocol 耳必须与心跳耳同序（两耳对称是 v1.7.30 的定案）"
        assert '"_proto_unloaded_logged"' in src[i_warn:i_ack]

    def test_both_ears_share_the_gate(self):
        """判定必须经单一真源（不得各写一份内联 state 比较）。"""
        for name in ("__init__.py", "mqtt_handler/_protocol.py"):
            src = (PKG / name).read_text(encoding="utf-8")
            assert "entry_state_for_sn(" in src, f"{name} 必须走四态门"
            assert '"not_loaded"' in src, f"{name} 必须处理第四态"
            assert 'in ("disabled", "not_loaded")' in src, \
                f"{name} 的弹卡短路必须覆盖两态"

    def test_gate_is_the_only_state_reader(self):
        """反钉：加载态判定收敛到 utils（config_flow 的 MQTT 排队判定是另一
        语义面，允许保留自己的 pending_states）。"""
        src = (PKG / "utils.py").read_text(encoding="utf-8")
        assert "ENTRY_STATES_UNANSWERED = frozenset" in src
        assert 'def entry_state_name(entry' in src
        for name in ("__init__.py", "mqtt_handler/_protocol.py"):
            body = (PKG / name).read_text(encoding="utf-8")
            assert "ENTRY_STATES_UNANSWERED" not in body, \
                f"{name} 不得自带加载态集合（判定口径必须单一真源）"
            assert not re.search(r"\b(?:entry|e|_entry|cfg)\.state\b", body), \
                f"{name} 不得直接读条目 .state（一律走 entry_state_for_sn）"
