"""常驻通道核验必须留痕——否则"healer 到底还在不在跑"从日志不可判。

现网教训（2026-10-27，v1.8.9 上线后）：修完"常驻任务顶穿 HA 启动收尾"之后，要确证
那个每 30 分钟复查 MQTT 通道的 healer **还活着**，发现盘上没有任何一手证据可取：
健康路径（verdict=ok）一行都不打，异常才打；而"沉默"既可能是"健康"也可能是
"任务早已静默死亡"（v1.7.61 S2「巡检任务静默死亡」正是本仓反复栽过的形态）。
判据口径：**每一圈出一条 INFO**，含圈号、verdict、卡片状态与节拍值。

反向半边同样钉死：留痕只能一圈一条——不得随网关上报刷（上报节奏 ≤10s，会刷屏），
也不得降级成 DEBUG（那样在默认日志级别下等于没打，本判据按 INFO 抓）。
"""
import asyncio
import logging
from types import SimpleNamespace

import custom_components.window_controller_gateway.mqtt_bootstrap as mb

LOGGER_NAME = "custom_components.window_controller_gateway.mqtt_bootstrap"


class _Hass:
    def __init__(self):
        self.data = {}
        self.is_stopping = False
        self.config = SimpleNamespace(config_dir=".")
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: [SimpleNamespace(entry_id="E1")]
            if dom == mb.DOMAIN else [])

    def async_create_task(self, coro, name=None, eager_start=True):
        return asyncio.ensure_future(coro)

    def async_create_background_task(self, coro, name, eager_start=True):
        # 与真 HA 同签名（name 必需）；桶差异见 test_startup_blocking_resident_task
        return asyncio.ensure_future(coro)


def _run_two_circles(monkeypatch, verdicts, caplog):
    """让 healer 跑满两圈后收手：每圈的睡用可控替身（第一次放行、第二次退出）。"""
    gate = asyncio.Event()
    state = {"slept": 0, "verify": 0}
    seq = list(verdicts)

    async def fake_sleep(hass, delay):
        state["slept"] += 1
        if state["slept"] == 1:
            await gate.wait()        # 第一圈：等 gate 放行
            return True
        return False                 # 第二圈结束：收手

    async def no_marker(hass):
        return False

    async def counting_verify(hass):
        idx = state["verify"]
        state["verify"] += 1
        return seq[min(idx, len(seq) - 1)]

    async def fake_mdns(hass):
        return None

    monkeypatch.setattr(mb, "has_bootstrap_marker", no_marker)
    monkeypatch.setattr(mb, "verify_builtin_channel", counting_verify)
    monkeypatch.setattr(mb, "_mdns_guard", fake_mdns)
    monkeypatch.setattr(mb, "_interruptible_sleep", fake_sleep)

    async def go():
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            mb.async_start_bootstrap_healer(_hass_holder["h"])
            healer = _hass_holder["h"].data[mb.DOMAIN]["_bootstrap_healer"]
            await asyncio.sleep(0)          # 让 healer 走到第一圈的 sleep
            gate.set()                      # 放行 → 进第二圈
            await asyncio.wait_for(healer, timeout=5)   # 第二圈返回 False ⇒ 自然收尾
        return healer

    _hass_holder["h"] = _Hass()
    asyncio.run(go())
    return [r for r in caplog.records
            if r.name == LOGGER_NAME and r.levelno >= logging.INFO], state


_hass_holder = {}


def test_each_verify_circle_leaves_one_info_trace(monkeypatch, caplog):
    """核心：健康（verdict=ok）也必须每圈留一条痕——否则"还在跑"不可证。"""
    records, state = _run_two_circles(monkeypatch, ["ok", "ok"], caplog)
    traces = [r for r in records if "[通道核验] 第" in r.getMessage()]
    assert len(traces) == 2, (
        "跑了两圈却只留 %d 条痕（旧形态：健康路径完全静默 ⇒ 无法从日志判 healer 是否"
        "还在巡查，与『巡检任务静默死亡』同族不可观测）%s"
        % (len(traces), [r.getMessage() for r in records]))
    assert state["slept"] == 2
    msgs = [r.getMessage() for r in traces]
    assert all("ok" in m for m in msgs), f"痕里必须带 verdict：{msgs}"
    assert all(("%d" % (i + 1)) in m for i, m in enumerate(msgs)), (
        f"痕里必须带圈号，否则数不清跑了几圈（判 healer 是否卡在同一圈就靠它）：{msgs}")


def test_trace_is_not_emitted_inside_a_sleep_chunk(monkeypatch, caplog):
    """反向半边：留痕的节拍是"一圈一条"，不是"每 30s 一条"也不是"每条上报一条"。"""
    records, _state = _run_two_circles(monkeypatch, ["ok", "mismatch"], caplog)
    traces = [r for r in records if "[通道核验] 第" in r.getMessage()]
    assert len(traces) == 2, (
        "两圈应恰两条痕；实得 %d 条＝节拍写进了 sleep 片或上报路径（会刷屏）"
        % len(traces))
    # 第二圈 verdict=mismatch ⇒ 痕里必须把"卡片在挂"这一态也带上（只报 ok 等于没报异常）
    assert any("mismatch" in r.getMessage() for r in traces), \
        "异常 verdict 也要留痕，且要能看出是哪一个判定"


def test_trace_reports_the_configured_interval_not_a_hardcoded_number(monkeypatch, caplog):
    """节拍数值必须取自 CHANNEL_VERIFY_INTERVAL（改常量，痕就跟着改）。

    本仓栽过"注释/文案写 30 分钟、代码封顶另一值"的形态，所以判据要能吃杠杆：
    把 CHANNEL_VERIFY_INTERVAL 改成别的值，痕里的秒数必须变。
    """
    monkeypatch.setattr(mb, "CHANNEL_VERIFY_INTERVAL", 777.0)
    records, _state = _run_two_circles(monkeypatch, ["ok", "ok"], caplog)
    traces = [r.getMessage() for r in records if "[通道核验] 第" in r.getMessage()]
    assert traces, "没有留痕"
    assert all("777" in t for t in traces), (
        f"痕里的节拍不是 CHANNEL_VERIFY_INTERVAL 的现值（写死了＝改了常量文案还骗人）：{traces}")
