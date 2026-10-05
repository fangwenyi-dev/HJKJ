"""v1.7.63 对抗复核 C-5+C-9：修复卡必须有真动作（repairs.py），空操作是假宣称。

HA 契约（2024.12 源码逐字核验）：
- `components/repairs/issue_handler.py:73`：集成没有 repairs 平台 ⇒ `ConfirmRepairFlow()`
  （通用确认框）；`:90-91`：确认后**只删卡**，不执行任何动作。
- `components/repairs/models.py`：`class RepairsFlow(data_entry_flow.FlowHandler)`，
  带 `issue_id` / `data`；平台必须提供 `async_create_fix_flow(hass, issue_id, data)`。
- 翻译形制（照 `components/assist_pipeline/strings.json` 实例）：
  `issues.<issue_id>.fix_flow.step.<step>.{title,description}`。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.repairs as rep_mod

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"
TAKEOVER = "mqtt_bootstrap_pending"
CHANNEL = "mqtt_channel_broken"


def test_repairs_platform_exists_with_real_contract():
    """平台文件与入口签名（HA 会 hasattr 校验，签错即 HomeAssistantError）。"""
    src = (PKG / "repairs.py").read_text(encoding="utf-8")
    assert "async def async_create_fix_flow(" in src
    assert "from homeassistant.components.repairs import RepairsFlow" in src, \
        "必须用真基类（models.py 契约：FlowHandler 子类 + issue_id/data）"
    # 认领面按模块真值判（常量在导入处，字面量不在源码里）
    import custom_components.window_controller_gateway.mqtt_bootstrap as mb
    assert set(rep_mod._FIXABLE_ISSUES) == {mb.TAKEOVER_ISSUE_ID, mb.CHANNEL_ISSUE_ID}, \
        "两处可修 issue 必须都被认领（多/少都会让修复按钮落空或报未知）"


def test_unknown_issue_raises_loud():
    """未知 issue 必须响亮抛（照 esphome/repairs.py 先例）：静默返回空流
    会让 HA 以为有修复动作而实际什么都不做。"""
    with pytest.raises(ValueError):
        asyncio.run(rep_mod.async_create_fix_flow(
            SimpleNamespace(), "no_such_issue", {}))


class _Recorder:
    """只桩"表单面"——流实例本身是真的（step 接线也要在测）。"""

    def __init__(self):
        self.forms = []
        self.created = []
        self.aborted = []
        self.actions = {"ensure": 0, "verify": 0, "marker": 0}

    def show_form(self, step_id=None, data_schema=None, errors=None,
                  description_placeholders=None):
        self.forms.append({"step_id": step_id, "errors": errors})
        return {"type": "form", "step_id": step_id, "errors": errors}

    def create_entry(self, title="", data=None):
        self.created.append(data or {})
        return {"type": "create_entry"}


def _drive(issue_id, *, ensure_ok=True, verdict="ok", marker_gone=True,
           submit=True):
    """按 HA 真形驱动修复流：init data={"issue_id":…} 当 user_input 传首步
    （data_entry_flow.py:342 + repairs/websocket_api.py:130-132 逐字核验）。"""
    rec = _Recorder()

    async def fake_ensure(hass):
        rec.actions["ensure"] += 1
        if not ensure_ok:
            raise RuntimeError("broker 未起")

    async def fake_verify(hass):
        rec.actions["verify"] += 1
        return verdict

    async def fake_marker(hass):
        rec.actions["marker"] += 1
        return not marker_gone

    # 注意：repairs.py 是模块级 from-import，必须打 rep_mod 上的绑定
    #（打 mb.* 对已绑定名字无效——本仓"桩打错层"的经典坑）
    old = (rep_mod.ensure_mqtt_connection, rep_mod.verify_builtin_channel,
           rep_mod.has_bootstrap_marker)
    rep_mod.ensure_mqtt_connection = fake_ensure
    rep_mod.verify_builtin_channel = fake_verify
    rep_mod.has_bootstrap_marker = fake_marker
    try:
        flow = rep_mod.GatewayRepairFlow()
        flow.hass = SimpleNamespace()
        flow.issue_id = issue_id
        flow.data = {}
        flow.async_show_form = rec.show_form
        flow.async_create_entry = rec.create_entry
        asyncio.run(flow.async_step_init({"issue_id": issue_id}))   # 首步（HA 真形）
        if submit:
            asyncio.run(flow.async_step_confirm({"submit": True}))  # 用户点提交
        return rec
    finally:
        (rep_mod.ensure_mqtt_connection, rep_mod.verify_builtin_channel,
         rep_mod.has_bootstrap_marker) = old


def test_init_step_shows_confirm_form():
    """首步必须是 confirm 表单（HA repairs 前端按 step 渲染翻译）。"""
    stub = _drive(TAKEOVER)
    assert stub.forms and stub.forms[0]["step_id"] == "confirm"


def test_init_step_does_not_run_repair_on_card_click():
    """F3（对抗复核，本机复现）：HA 把 init data({"issue_id": …}) 当 user_input
    传首步——首步若转发 user_input，"点修复"在建流请求里直接执行、确认步被
    绕过（HA 自家 ConfirmRepairFlow 首步即不传参）。修后：建流瞬间只出确认
    表单、零动作；用户提交后才执行。"""
    stub = _drive(CHANNEL, submit=False)
    assert stub.forms and stub.forms[0]["step_id"] == "confirm", \
        "点修复瞬间必须只出确认表单"
    assert stub.actions == {"ensure": 0, "verify": 0, "marker": 0}, \
        "建流瞬间不得执行任何修复动作（旧实现直接跑 ensure+核验）"
    assert stub.created == []


def test_channel_issue_fix_reenables_and_creates_entry():
    """通道卡：提交 → 重跑引导 + 核验通过 → create_entry（HA 由此删卡）。"""
    stub = _drive(CHANNEL, verdict="ok")
    assert stub.created == [{}], "通过必须走 create_entry（HA finish_flow 才删卡）"
    assert not stub.forms or stub.forms[-1]["errors"] is None


def test_channel_issue_still_broken_shows_error():
    """反向臂：核验仍不过 ⇒ 回显错误、**不**删卡（继续可见可修）。"""
    stub = _drive(CHANNEL, verdict="mismatch")
    assert stub.created == []
    assert stub.forms[-1]["errors"] == {"base": "still_broken"}


def test_takeover_issue_marker_semantics():
    """接管卡：成功判据是标记消失；未消失 ⇒ 回显错误。"""
    ok = _drive(TAKEOVER, marker_gone=True)
    assert ok.created == [{}]
    stuck = _drive(TAKEOVER, marker_gone=False)
    assert stuck.created == [] and stuck.forms[-1]["errors"] == {"base": "still_broken"}


def test_ensure_failure_does_not_crash_flow():
    """ensure 抛（broker 未起）不许炸穿修复流：按"仍坏"回显。"""
    stub = _drive(CHANNEL, ensure_ok=False, verdict="disconnected")
    assert stub.created == [] and stub.forms[-1]["errors"] == {"base": "still_broken"}


def test_fix_flow_translations_present():
    """C-9：三份 JSON 都要有 fix_flow.step.confirm（含 still_broken 错误键），
    ConfirmRepairFlow 时代确认框文案取不到的老问题一并收口。"""
    for rel in ("strings.json", "translations/zh-CN.json", "translations/zh-Hans.json"):
        d = json.loads((PKG / rel).read_text(encoding="utf-8"))
        for issue in (TAKEOVER, CHANNEL):
            node = d["issues"][issue]
            step = (node.get("fix_flow") or {}).get("step") or {}
            assert step.get("confirm", {}).get("description"), \
                f"{rel}: {issue} 缺 fix_flow.step.confirm.description"
            err = (node.get("fix_flow") or {}).get("error") or {}
            assert err.get("still_broken"), f"{rel}: {issue} 缺 fix_flow.error.still_broken"


def test_dead_repair_step_removed():
    """C-5 配套：config_flow 的 async_step_repair（HA 永不启动它）与三个旧文案键
    必须整体移除——留着就是"看起来能修实则空操作"的死代码。"""
    cf_src = (PKG / "config_flow.py").read_text(encoding="utf-8")
    assert "async_step_repair" not in cf_src
    for rel in ("strings.json", "translations/zh-CN.json", "translations/zh-Hans.json"):
        d = json.loads((PKG / rel).read_text(encoding="utf-8"))
        assert "repair" not in (d["config"].get("step") or {}), rel
        assert "mqtt_bootstrap_still_pending" not in d["config"]["error"], rel
        assert "mqtt_bootstrap_fixed" not in d["config"]["abort"], rel
