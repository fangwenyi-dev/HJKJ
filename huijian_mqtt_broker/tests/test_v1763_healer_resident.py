"""v1.7.63 次批：healer 真常驻（C-6）/ 端点三态（C-7）/ 停用清卡（C-10）。

背景（对抗复核第二轮的判定）：
- C-6：healer docstring/CHANGELOG 宣称"每 30 分钟复查、此后被抢走也复查"，
  旧实现 verdict∈(ok,no_endpoint) 即 `return`（一次性核验；mDNS 卡也因此只
  被查一次）。
- C-7：`_read_endpoint_sync` 对"缺失/损坏/无 broker 字段"一律 None，
  `verify_builtin_channel` 又把条目表读取异常也归 `no_endpoint`，而两处调用
  点把 no_endpoint 当"通过"清卡 ⇒ 判定面坏了反而清卡并关核验。
- C-10：healer 在"启用条目清零"时直接 return 不清卡；`async_remove_entry`
  也无清理 ⇒ 禁用/删光条目后僵尸卡永留。
"""
import asyncio
import json
from types import SimpleNamespace

import homeassistant.helpers.issue_registry as ir
import custom_components.window_controller_gateway.mqtt_bootstrap as mb


class _Hass:
    def __init__(self, tmp_path, mqtt_entries=None):
        self.data = {}
        self.config = SimpleNamespace(path=lambda *p: str(tmp_path / p[0]))
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: ([SimpleNamespace(entry_id="E1")]
                                       if dom == mb.DOMAIN else list(mqtt_entries or [])))

    def async_create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


def _endpoint(tmp_path, content):
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(content, encoding="utf-8")


def _run_healer(hass, monkeypatch, verdicts, sleeps):
    """跑 healer：verdict 逐轮出（用尽后重复最后一个）；sleep 逐轮出（False=停）。"""
    seq = list(verdicts)
    seen = {"verify": 0, "sleep": 0}

    async def fake_verify(h):
        seen["verify"] += 1
        item = seq[min(seen["verify"] - 1, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_sleep(h, delay):
        seen["sleep"] += 1
        return sleeps[min(seen["sleep"] - 1, len(sleeps) - 1)]

    async def no_marker(h):
        return False

    async def fake_mdns(h):
        return None

    monkeypatch.setattr(mb, "has_bootstrap_marker", no_marker)
    monkeypatch.setattr(mb, "verify_builtin_channel", fake_verify)
    monkeypatch.setattr(mb, "_interruptible_sleep", fake_sleep)
    monkeypatch.setattr(mb, "_mdns_guard", fake_mdns)

    async def go():
        mb.async_start_bootstrap_healer(hass)
        task = hass.data[mb.DOMAIN]["_bootstrap_healer"]
        await asyncio.wait_for(task, timeout=5)
        return task

    asyncio.run(go())
    return seen


def test_healer_stays_resident_when_healthy(tmp_path, monkeypatch):
    """C-6：健康也继续低频复查（旧实现健康即收尾 ⇒ 一次核验）。"""
    ir.ISSUES_CREATED.clear()
    ir.ISSUES_DELETED.clear()
    _endpoint(tmp_path, json.dumps({"broker": "127.0.0.1", "port": 2022}))
    hass = _Hass(tmp_path)
    seen = _run_healer(hass, monkeypatch, ["ok"], [True, True, False])
    assert seen["verify"] >= 3, \
        f"健康必须常驻复查（旧实现只看一次），实得 {seen['verify']}"
    assert (mb.DOMAIN, mb.CHANNEL_ISSUE_ID) in ir.ISSUES_DELETED


def test_healer_detects_takeover_after_healthy(tmp_path, monkeypatch):
    """C-6 核心承诺：先健康、后条目被改走 ⇒ 必须复查出卡（旧实现早已收尾）。"""
    ir.ISSUES_CREATED.clear()
    _endpoint(tmp_path, json.dumps({"broker": "127.0.0.1", "port": 2022}))
    hass = _Hass(tmp_path)
    _run_healer(hass, monkeypatch, ["ok", "mismatch"], [True, False])
    created = [i for i in ir.ISSUES_CREATED if i["issue_id"] == mb.CHANNEL_ISSUE_ID]
    assert created, "健康之后被抢走必须被发现（常驻核验的意义）"


def test_healer_clears_cards_when_entries_all_disabled(tmp_path, monkeypatch):
    """C-10 + v1.8.5 G-3：启用条目清零 ⇒ 收尾前**三张卡全清**（防僵尸卡）。"""
    ir.ISSUES_DELETED.clear()
    hass = _Hass(tmp_path)
    # config_entries 对本域返回空 ⇒ 计数为 0
    hass.config_entries.async_entries = lambda dom: []

    async def no_marker(h):
        return False

    monkeypatch.setattr(mb, "has_bootstrap_marker", no_marker)

    async def go():
        mb.async_start_bootstrap_healer(hass)
        await asyncio.wait_for(hass.data[mb.DOMAIN]["_bootstrap_healer"], timeout=5)

    asyncio.run(go())
    assert (mb.DOMAIN, mb.CHANNEL_ISSUE_ID) in ir.ISSUES_DELETED
    assert (mb.DOMAIN, mb.MDNS_ISSUE_ID) in ir.ISSUES_DELETED
    # v1.8.5（审计 G-3）：takeover 卡此前漏清——healer 已退出，卡片还写着
    # "系统自动重试"，用户只能重启 HA 才消失。
    assert (mb.DOMAIN, mb.TAKEOVER_ISSUE_ID) in ir.ISSUES_DELETED, (
        "收尾不清 takeover 卡＝僵尸卡（G-3 原缺陷）"
    )


def test_clear_huijian_issues_is_the_single_cleanup_exit():
    """G-3 结构钉：域级清卡只有一个出口，且出口必须列全三张卡。

    为什么钉结构而不是只钉行为：原缺陷的形态就是"各调用点手写清单、漏抄一张"，
    C-10 那次修复自己还把这个形态写进了注释。只要还有人手写两张卡的清单，
    下次新增第四张卡就会再漏一次——所以判据是"出口唯一 + 清单完整"。
    """
    from pathlib import Path as _Path
    src = (_Path(__file__).resolve().parents[1] / "custom_components"
           / "window_controller_gateway" / "mqtt_bootstrap.py").read_text(encoding="utf-8")
    assert "def _clear_huijian_issues(" in src, "域级清卡出口必须存在（唯一出口）"
    fn_start = src.index("def _clear_huijian_issues(")
    fn_body = src[fn_start:fn_start + 700]
    for name in ("_clear_takeover_issue", "_clear_channel_issue", "_clear_mdns_issue"):
        assert name + "(hass)" in fn_body, f"唯一出口漏了 {name}＝僵尸卡会重现"
    # 反向臂：**出口函数体之外**再手写"两张卡清单"都算回潮（那份清单只能有一处）。
    # 判据要排除出口自身——否则钉红的是合法实现（首版就踩了这个坑）。
    import re as _re
    fn_end = fn_start + 700
    outside = src[:fn_start] + src[fn_end:]
    handwritten = _re.findall(
        r"_clear_channel_issue\(hass\)\s*\n\s*_clear_mdns_issue\(hass\)", outside)
    assert not handwritten, (
        "又出现手写的两张卡清单＝第 N 次漏抄的温床，请改走 _clear_huijian_issues"
    )


def test_sleep_exit_also_clears_cards(tmp_path, monkeypatch):
    """G-3 第二处：`_interruptible_sleep` 的退出支也必须清卡。

    旧写法在 sleep 里直接 `return False` ⇒ 循环顶部的清卡分支被绕开，三张卡
    一张都不清（比 C-10 修的还漏得多）。本条钉 sleep 出口自身的行为。
    """
    ir.ISSUES_DELETED.clear()
    hass = _Hass(tmp_path)
    hass.config_entries.async_entries = lambda dom: []      # 条目清零
    monkeypatch.setattr(mb, "HEALER_SLEEP_CHUNK", 0.01)

    async def go():
        return await mb._interruptible_sleep(hass, 60)

    assert asyncio.run(go()) is False, "条目清零时 sleep 必须即刻收束"
    for iid in (mb.TAKEOVER_ISSUE_ID, mb.CHANNEL_ISSUE_ID, mb.MDNS_ISSUE_ID):
        assert (mb.DOMAIN, iid) in ir.ISSUES_DELETED, (
            f"sleep 出口漏清 {iid}＝healer 已退出而卡片永留（G-3）"
        )


def test_healer_survives_verify_exception(tmp_path, monkeypatch):
    """F2（对抗复核）：核验本体逃逸（如 is_connected 结构缺失的 KeyError）
    不得杀死常驻 healer（v1.7.61 S2"巡检任务静默死亡"同族）——按无结论
    继续复查。反向半边：异常那一轮不得把卡清掉（按"未通过"留痕）。"""
    ir.ISSUES_CREATED.clear()
    _endpoint(tmp_path, json.dumps({"broker": "127.0.0.1", "port": 2022}))
    hass = _Hass(tmp_path)
    seen = _run_healer(hass, monkeypatch, [RuntimeError("boom"), "ok"],
                       [True, False])
    assert seen["verify"] >= 2, "异常后必须仍在复查（旧形态：任务被炸死）"


# ============ C-7 端点三态 / 探针异常 ============

def test_verify_endpoint_broken_is_named(tmp_path):
    """端点文件存在但解析不出 ⇒ endpoint_broken（旧实现=no_endpoint ⇒ 被当通过清卡）。"""
    _endpoint(tmp_path, "{not-json")
    hass = _Hass(tmp_path, mqtt_entries=[])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "endpoint_broken"


def test_probe_exception_is_probe_error_not_no_endpoint(tmp_path, monkeypatch):
    """F5（对抗复核）：端点探针本体逃逸（executor 抛错）必须按无结论
    （probe_error）——旧实现折成 no_endpoint 会被 healer 当"通过"清卡，
    与 C-7"判定面坏不误清"自相矛盾。"""
    _endpoint(tmp_path, json.dumps({"broker": "127.0.0.1", "port": 2022}))
    hass = _Hass(tmp_path, mqtt_entries=[])

    def boom(fn):
        raise RuntimeError("executor down")

    hass.async_add_executor_job = boom
    state, data = asyncio.run(mb._probe_endpoint(hass))
    assert state == "probe_error" and data is None


def test_verify_endpoint_missing_still_no_endpoint(tmp_path):
    """反向臂：文件缺失＝真的无判定依据（HACS 场景语义不变）。"""
    hass = _Hass(tmp_path, mqtt_entries=[])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "no_endpoint"


def test_verify_probe_error_when_entries_read_raises(tmp_path, monkeypatch):
    """条目表读取抛错 ⇒ probe_error（旧实现归 no_endpoint ⇒ 探针坏了反倒清卡）。"""
    _endpoint(tmp_path, json.dumps({"broker": "127.0.0.1", "port": 2022}))
    hass = _Hass(tmp_path)

    def boom(dom):
        raise RuntimeError("registry busy")

    hass.config_entries.async_entries = boom
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "probe_error"


def test_guard_wording_covers_new_verdicts():
    src = (mb.__file__ and open(mb.__file__, encoding="utf-8").read()) or ""
    assert "endpoint_broken" in src and "probe_error" in src
