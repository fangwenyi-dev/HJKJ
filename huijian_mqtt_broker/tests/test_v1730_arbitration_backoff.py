"""v1.7.30 钉桩：② 代答单点仲裁 / ③ 代答未转正 WARNING 留痕 / ④ healer 指数退避。

台架实锤（2026-09-17 真栈 A/B，档案 5aebbb63）驱动：
- ② 同一 001 被 2~3 个耳朵各自代答（倍数=应答者数：每配置条目一分支+每等待
  条目一心跳耳），ws_peek2 复证 N=2（无等待条目+2 handler）。仲裁以
  (sn,id) 认领收口为 1 答/请求。
- ③ "代答后永不出设备"过去只有 INFO，现场 WARNING+ 采集不可见（取证铁律）；
  看守 30s：无条目且无待确认卡片才告警，10 分钟 per-SN 去重。
- ④ v1.7.29 healer 恒频 300s 重跑 ensure（B_s4c 实锤 t+300s 恰一条重跑、
  A_s4c 对照组 9min 零重跑）；恒频把 takeover 破坏面钉死在 5 分钟节奏上，
  改指数退避封顶 1h + 首次触顶一条 WARNING。
"""
import asyncio
import json
import logging
import pathlib
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.utils as utils
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import homeassistant.components.mqtt as fake_mqtt
from custom_components.window_controller_gateway.const import (
    DOMAIN, CONF_GATEWAY_SN)
from custom_components.window_controller_gateway.utils import (
    ear_ack_claim, async_ear_ack_001_arbitrated)

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"

SN = "100199999999"


class _Hass:
    """带 config_entries/flow/async_create_task 的运行时替身。"""

    def __init__(self, entries=(), flows=(), config_dir="."):
        self.data = {}
        self.is_stopping = False
        self.config = SimpleNamespace(config_dir=config_dir)
        self._flows = list(flows)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(entries),
            flow=SimpleNamespace(async_progress=lambda: list(self._flows)),
        )

    def async_create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


class _Pub:
    def __init__(self):
        self.calls = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        self.calls.append((topic, json.loads(payload)))


def _entry(sn):
    return SimpleNamespace(entry_id="E_" + sn, data={CONF_GATEWAY_SN: sn})


def _flow(unique_id):
    return {"handler": DOMAIN, "context": {"source": "discovery",
                                           "unique_id": unique_id}}


# ============ ② 仲裁认领 ============
class TestClaim:
    def test_first_wins_second_suppressed(self):
        hass = _Hass()
        assert ear_ack_claim(hass, SN, 15) is True
        assert ear_ack_claim(hass, SN, 15) is False, "同 (sn,id) 后来者必须抑制"

    def test_new_id_gets_answer(self):
        """固件重试换新 id → 新键放行（止血语义不变：每次请求恰一答）。"""
        hass = _Hass()
        assert ear_ack_claim(hass, SN, 15) is True
        assert ear_ack_claim(hass, SN, 16) is True

    def test_case_insensitive_sn(self):
        hass = _Hass()
        assert ear_ack_claim(hass, "1001ABCDEF", 1) is True
        assert ear_ack_claim(hass, "1001abcdef", 1) is False

    def test_ttl_expiry_releases(self, monkeypatch):
        """同 id 超 TTL（丢包保险）后可再答一次。"""
        monkeypatch.setattr(utils, "EAR_ACK_CLAIM_TTL", 0.0)
        hass = _Hass()
        assert ear_ack_claim(hass, SN, 15) is True
        assert ear_ack_claim(hass, SN, 15) is True

    def test_capacity_gate(self):
        """畸形流量下认领表不无界增长（容量闸）。"""
        hass = _Hass()
        for i in range(utils.EAR_ACK_CLAIM_MAX + 50):
            ear_ack_claim(hass, SN, i)
        assert len(hass.data[DOMAIN]["_ear_ack_claims"]) <= utils.EAR_ACK_CLAIM_MAX


class TestArbitratedEntry:
    @pytest.mark.asyncio
    async def test_one_publish_per_request(self, monkeypatch):
        """仲裁后的端到端：同一 (sn,id) 两耳先后调用只发布一条报文。"""
        pub = _Pub()
        monkeypatch.setattr(fake_mqtt, "async_publish", pub)
        hass = _Hass()
        r1 = await async_ear_ack_001_arbitrated(hass, SN, 444)
        r2 = await async_ear_ack_001_arbitrated(hass, SN, 444)
        assert (r1, r2) == (True, False)
        assert len(pub.calls) == 1
        topic, payload = pub.calls[0]
        assert topic == f"gateway/{SN}/req"
        assert payload["id"] == 444 and payload["data"]["errcode"] == 0

    @pytest.mark.asyncio
    async def test_publish_failure_no_watch(self, monkeypatch):
        class _Boom:
            async def __call__(self, *a, **k):
                raise RuntimeError("broker down")
        monkeypatch.setattr(fake_mqtt, "async_publish", _Boom())
        hass = _Hass()
        assert await async_ear_ack_001_arbitrated(hass, SN, 1) is False
        assert hass.data[DOMAIN].get("_ear_promotion_watched", {}) == {}, \
            "发布失败不起看守（本就没代答成功，无从谈转正）"


# ============ ③ 转正看守 ============
async def _drain(hass):
    """让已排队的看守协程跑完（watch sleep 已被压成 0）。"""
    tasks = list(hass.data[DOMAIN].get("_ear_watch_tasks", ()))
    if tasks:
        await asyncio.wait(tasks)
    await asyncio.sleep(0)


class TestPromotionWatch:
    @pytest.mark.asyncio
    async def test_warns_when_neither_entry_nor_card(self, monkeypatch, caplog):
        pub = _Pub()
        monkeypatch.setattr(fake_mqtt, "async_publish", pub)
        monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)
        hass = _Hass()  # 无条目、无卡片
        caplog.set_level(logging.WARNING, logger=utils.__name__)
        assert await async_ear_ack_001_arbitrated(hass, SN, 1) is True
        await _drain(hass)
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "未转为配置条目" in r.getMessage()]
        assert len(warns) == 1, "③：代答后无条目无卡片必须恰好一条 WARNING 留痕"
        assert SN in warns[0].getMessage()

    @pytest.mark.asyncio
    async def test_silent_when_promoted(self, monkeypatch, caplog):
        pub = _Pub()
        monkeypatch.setattr(fake_mqtt, "async_publish", pub)
        monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)
        hass = _Hass(entries=[_entry(SN)])
        caplog.set_level(logging.WARNING, logger=utils.__name__)
        await async_ear_ack_001_arbitrated(hass, SN, 1)
        await _drain(hass)
        assert not [r for r in caplog.records if "未转为配置条目" in r.getMessage()], \
            "已转正静默（A_s3 秒级转正主形态不许误报）"

    @pytest.mark.asyncio
    async def test_silent_when_card_pending(self, monkeypatch, caplog):
        pub = _Pub()
        monkeypatch.setattr(fake_mqtt, "async_publish", pub)
        monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)
        hass = _Hass(flows=[_flow(SN)])
        caplog.set_level(logging.WARNING, logger=utils.__name__)
        await async_ear_ack_001_arbitrated(hass, SN, 1)
        await _drain(hass)
        assert not [r for r in caplog.records if "未转为配置条目" in r.getMessage()], \
            "发现卡片挂起等用户确认＝正常形态，不许误报刷屏"

    @pytest.mark.asyncio
    async def test_dedup_per_sn_window(self, monkeypatch, caplog):
        """同 SN 连发 3 个 id 也只起一个看守（10 分钟去重窗）。"""
        pub = _Pub()
        monkeypatch.setattr(fake_mqtt, "async_publish", pub)
        monkeypatch.setattr(utils, "EAR_PROMOTION_WATCH_SECONDS", 0.0)
        hass = _Hass()
        caplog.set_level(logging.WARNING, logger=utils.__name__)
        for i in (1, 2, 3):
            await async_ear_ack_001_arbitrated(hass, SN, i)
        await _drain(hass)
        warns = [r for r in caplog.records if "未转为配置条目" in r.getMessage()]
        assert len(warns) == 1 and len(pub.calls) == 3


# ============ ④ healer 指数退避 ============
class TestBackoff:
    def test_formula_capped(self, monkeypatch):
        monkeypatch.setattr(mb, "BOOTSTRAP_RETRY_INTERVAL", 1.0)
        monkeypatch.setattr(mb, "BOOTSTRAP_RETRY_MAX_INTERVAL", 8.0)
        assert [mb._retry_delay(i) for i in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_production_constants(self):
        assert mb.BOOTSTRAP_RETRY_INTERVAL == 300.0
        assert mb.BOOTSTRAP_RETRY_MAX_INTERVAL == 3600.0
        assert mb._retry_delay(1) == 300.0
        assert mb._retry_delay(4) == 2400.0
        assert mb._retry_delay(5) == 3600.0
        assert mb._retry_delay(99) == 3600.0

    def test_healer_rhythm_and_single_cap_warning(self, monkeypatch, caplog):
        """六轮未落地：sleep 序列 1,2,4,8,8；触顶 WARNING 恰好一条。

        v1.7.63（C-6）升级：标记落地后 healer 不再退出，转**常驻核验**——
        旧断言"落地即退出"随语义取消（旧形态在 resident 分支上会永不返回）。
        本用例以"启用条目清零"驱动收尾：既保住节奏/单条封顶告警的原不变量，
        又钉住常驻期进入切片睡且"清零即退、不悬挂"（对应生产：用户禁用/删光
        慧尖条目）。
        """
        monkeypatch.setattr(mb, "BOOTSTRAP_RETRY_INTERVAL", 1.0)
        monkeypatch.setattr(mb, "BOOTSTRAP_RETRY_MAX_INTERVAL", 8.0)
        entries = [SimpleNamespace(entry_id="E1")]
        hass = SimpleNamespace(
            data={DOMAIN: {}}, is_stopping=False,
            config_entries=SimpleNamespace(async_entries=lambda d: list(entries)),
            async_create_task=lambda coro, name=None: asyncio.ensure_future(coro),
        )
        markers = [True, True, True, True, True, True, True, True, True, True,
                   True, False]
        real_sleep = asyncio.sleep
        delays = []

        async def fake_sleep(d, *a, **k):
            delays.append(d)
            # 标记已落地、节奏段走完 ⇒ 首个 30s 切片即模拟"禁用/删光条目"，
            # 由 _interruptible_sleep 的片间复检驱动收尾
            if d >= 30 and entries:
                entries.clear()
            await real_sleep(0)

        async def fake_verify(h):
            return "ok"

        async def fake_mdns(h):
            return None

        monkeypatch.setattr(mb.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(mb, "verify_builtin_channel", fake_verify)
        monkeypatch.setattr(mb, "_mdns_guard", fake_mdns)
        monkeypatch.setattr(mb, "has_bootstrap_marker",
                            lambda hass: asyncio.ensure_future(_pop(markers)))
        monkeypatch.setattr(mb, "ensure_mqtt_connection",
                            lambda hass: asyncio.ensure_future(_none()))
        monkeypatch.setattr(mb, "_report_takeover_issue", lambda h: None)
        monkeypatch.setattr(mb, "_clear_takeover_issue", lambda h: None)
        caplog.set_level(logging.WARNING, logger=mb.__name__)

        async def main():
            mb.async_start_bootstrap_healer(hass)
            await asyncio.wait_for(hass.data[DOMAIN]["_bootstrap_healer"], timeout=5)
        asyncio.run(main())

        rhythm = [d for d in delays if d < 30]
        assert rhythm == [1.0, 2.0, 4.0, 8.0, 8.0], "轮间隔必须按 2 倍退避封顶"
        resident_chunks = [d for d in delays if d >= 30]
        assert len(resident_chunks) == 1, \
            "常驻核验期必须进入一次切片睡（清零复检即退，不许悬挂）"
        caps = [r for r in caplog.records if "封顶" in r.getMessage()]
        assert len(caps) == 1, "首次触顶只许一条 WARNING，此后不刷屏"
        assert hass.data[DOMAIN]["_bootstrap_healer"] is None


async def _pop(seq):
    return seq.pop(0) if seq else False


async def _none():
    return None


# ============ 接线反钉（本文件与 1726 钉互补） ============
class TestGuardPins:
    def test_utils_exports_single_entry(self):
        src = (PKG / "utils.py").read_text(encoding="utf-8")
        assert "def ear_ack_claim(" in src
        assert "async def async_ear_ack_001_arbitrated(" in src
        # 仲裁先行语义：claim 在任何 publish 之前
        seg = src[src.index("async def async_ear_ack_001_arbitrated"):]
        assert seg.index("ear_ack_claim(") < seg.index("async_ack_gateway_001(")

    def test_healer_no_fixed_interval_sleep(self):
        src = (PKG / "mqtt_bootstrap.py").read_text(encoding="utf-8")
        assert "await asyncio.sleep(BOOTSTRAP_RETRY_INTERVAL)" not in src, \
            "④：恒频 sleep 反钉——必须走 _retry_delay(rounds)"
