"""v1.7.29 A+B+C 钉桩：bootstrap 持久自愈 + 可见修复入口 + Web UI「HA MQTT 通道」。

A：async_start_bootstrap_healer——标记存活期间每轮重试 ensure；标记删除/
    无慧尖条目/宿主停机三种出口；每 hass 单实例幂等；ConfigEntryNotReady
    吞掉交下一轮（现场"mqtt not ready 长期滞留"的根治）。
B：config_flow.async_step_repair + issue 文案键（strings/zh-CN 双侧对称）。
C：index.html 第 4 状态项 + huijian.js 检测 + .status-grid auto-fit。
"""
import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.mqtt_bootstrap as mb
from custom_components.window_controller_gateway.const import DOMAIN

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"
WWW = HERE.parent / "www"


class _Hass:
    def __init__(self, entries=()):
        self.data = {DOMAIN: {}}
        self.is_stopping = False
        self.config = SimpleNamespace(
            path=lambda f: str(pathlib.Path("/tmp") / f))
        self.config_entries = SimpleNamespace(
            async_entries=lambda d: list(entries))

    def async_create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


def _mk(monkeypatch, marker_seq, ensure_effects=None):
    """marker_seq：has_bootstrap_marker 逐轮返回；ensure_effects：ensure 逐轮行为。
    返回 calls 计数与 markers 消耗表。"""
    calls = {"ensure": 0, "report": 0, "clear": 0}
    markers = list(marker_seq)
    effects = list(ensure_effects or [])

    async def fake_has_marker(hass):
        return markers.pop(0) if markers else False

    async def fake_ensure(hass):
        eff = effects.pop(0) if effects else None
        calls["ensure"] += 1
        if isinstance(eff, Exception):
            raise eff
        return eff

    monkeypatch.setattr(mb, "has_bootstrap_marker", fake_has_marker)
    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)
    monkeypatch.setattr(mb, "_report_takeover_issue",
                        lambda h: calls.__setitem__("report", calls["report"] + 1))
    monkeypatch.setattr(mb, "_clear_takeover_issue",
                        lambda h: calls.__setitem__("clear", calls["clear"] + 1))
    monkeypatch.setattr(mb, "BOOTSTRAP_RETRY_INTERVAL", 0.0)

    # v1.7.63（C-6）：healer 健康即**常驻**（不再收尾退出）——本组旧钉用
    # "首轮巡查后按停机收尾"把它收口，计数语义不变（退出改由 sleep/停机驱动）。
    async def _stop_after_one(hass, delay):
        # 只对**常驻核验**的睡（1800s 档）返回停机；重试路径 delay=0 照常继续，
        # 否则旧钉的"三轮 ensure"会被第一轮的停机桩掐断。
        return delay < 60

    monkeypatch.setattr(mb, "_interruptible_sleep", _stop_after_one)
    return calls


def _run_healer(hass, extra_starts=0):
    """在 running loop 内启动 healer（async_create_task 需要事件循环），
    可选重复 start 验证幂等。"""
    box = []

    async def main():
        mb.async_start_bootstrap_healer(hass)
        first = hass.data[DOMAIN]["_bootstrap_healer"]
        box.append(first)
        for _ in range(extra_starts):
            mb.async_start_bootstrap_healer(hass)
            assert hass.data[DOMAIN]["_bootstrap_healer"] is first, \
                "运行中重复 start 不得替换/追加任务（单实例）"
        await asyncio.wait_for(first, timeout=5)
    asyncio.run(main())


class TestHealerLifecycle:
    def test_lands_when_marker_gone(self, monkeypatch):
        hass = _Hass(entries=[SimpleNamespace(entry_id="E1")])
        calls = _mk(monkeypatch, [True, False])
        _run_healer(hass)
        assert calls["ensure"] == 1 and calls["clear"] == 1 and calls["report"] == 0
        assert hass.data[DOMAIN]["_bootstrap_healer"] is None, \
            "退出必须复位单实例位（finally）"

    def test_reports_issue_while_pending(self, monkeypatch):
        """两轮未落地：每轮挂修复条目；第三轮落地清除退出。
        标记消耗序：r1(顶检T,尾检T)→report；r2(顶检T,尾检T)→report；
        r3(顶检T,尾检F)→clear 退出。"""
        hass = _Hass(entries=[SimpleNamespace(entry_id="E1")])
        calls = _mk(monkeypatch, [True, True, True, True, True, False])
        _run_healer(hass)
        assert calls["ensure"] == 3 and calls["report"] == 2 and calls["clear"] == 1

    def test_not_ready_swallowed_next_round(self, monkeypatch):
        """ConfigEntryNotReady（broker 未起）不杀循环，下一轮续跑落地。
        r1(顶T,ensure 抛,尾T)→report；r2(顶T,ensure 成,尾F)→clear 退出。"""
        hass = _Hass(entries=[SimpleNamespace(entry_id="E1")])
        calls = _mk(monkeypatch, [True, True, True, False],
                    ensure_effects=[mb.ConfigEntryNotReady("broker 未起"), True])
        _run_healer(hass)
        assert calls["ensure"] == 2, "异常轮必须被吞并进入下一轮"
        assert calls["clear"] == 1

    def test_exits_without_entries(self, monkeypatch):
        """慧尖条目全卸：立即退出，不跑 ensure。"""
        hass = _Hass(entries=[])
        calls = _mk(monkeypatch, [True])
        _run_healer(hass)
        assert calls["ensure"] == 0

    def test_no_marker_no_op(self, monkeypatch):
        """标记本就不存在（HACS 独立安装等）：清条目后直接退出。"""
        hass = _Hass(entries=[SimpleNamespace(entry_id="E1")])
        calls = _mk(monkeypatch, [False])
        _run_healer(hass)
        assert calls["ensure"] == 0 and calls["clear"] == 1

    def test_single_instance_per_hass(self, monkeypatch):
        """运行中重复 start 幂等：不产生第二个任务。"""
        hass = _Hass(entries=[SimpleNamespace(entry_id="E1")])
        _mk(monkeypatch, [True, False])
        _run_healer(hass, extra_starts=3)  # 4 次 start，仅 1 个 task
        assert hass.data[DOMAIN]["_bootstrap_healer"] is None


class TestWiring:
    def test_both_branches_start_healer(self):
        src = (PKG / "__init__.py").read_text(encoding="utf-8")
        assert src.count("async_start_bootstrap_healer(hass)") == 2, \
            "awaiting 与完整设置两分支都必须拉起自愈"

    def test_repair_flow_defined(self):
        src = (PKG / "config_flow.py").read_text(encoding="utf-8")
        # v1.7.63（对抗复核 C-5）：HA 的修复流**不启动**集成 config flow（只查
        # repairs 平台）——旧入口 config_flow.async_step_repair 是死代码，已删除；
        # 真入口迁到 repairs.py（async_create_fix_flow + confirm 步真动作）。
        rep_src = (PKG / "repairs.py").read_text(encoding="utf-8")
        assert "async def async_create_fix_flow" in rep_src
        assert "async_step_confirm" in rep_src and "still_broken" in rep_src
        assert "async_step_repair" not in src, "死代码必须清掉（假宣称的一部分）"


class TestStringsSymmetry:
    KEYS = [
        # v1.7.63：修复流翻译随入口迁到 issues.<id>.fix_flow（HA 修复流契约）
        ("issues", "mqtt_bootstrap_pending", "fix_flow", "step", "confirm", "description"),
        ("issues", "mqtt_bootstrap_pending", "fix_flow", "error", "still_broken"),
        ("issues", "mqtt_channel_broken", "fix_flow", "step", "confirm", "description"),
        ("issues", "mqtt_bootstrap_pending", "title"),
        ("issues", "mqtt_bootstrap_pending", "description"),
    ]

    @pytest.mark.parametrize("rel", ["strings.json", "translations/zh-CN.json"])
    def test_keys_present(self, rel):
        data = json.loads((PKG / rel).read_text(encoding="utf-8"))
        for path in self.KEYS:
            node = data
            for k in path:
                assert k in node, f"{rel} 缺键 {'/'.join(path)}"
                node = node[k]


class TestWebChannelRow:
    def test_index_has_fourth_status_item(self):
        html = (WWW / "index.html").read_text(encoding="utf-8")
        assert 'id="haMqttChannelStatus"' in html and "HA MQTT 通道" in html

    def test_js_checks_channel(self):
        js = (WWW / "js/huijian.js").read_text(encoding="utf-8")
        assert "api/ha/config/config_entries/entry" in js
        assert "haMqttChannelStatus" in js
        assert "e.domain === 'mqtt'" in js

    def test_grid_autofit(self):
        css = (WWW / "css/huijian.css").read_text(encoding="utf-8")
        assert "repeat(auto-fit, minmax(220px, 1fr))" in css
