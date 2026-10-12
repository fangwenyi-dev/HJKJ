# -*- coding: utf-8 -*-
"""v1.5.3（N-M1）钉：前置闸话术不得与 _step_say 的「抱歉，」字头叠出复读机。

现网实锤（1.5.2 LAN 复验 09:2x）：「亮度调到200」命中 N2 百分比量程闸，闸话术自起
「抱歉，…只有 0 到 100…」，而 `_step_say` 无条件再加一个「抱歉，」⇒ 播报成
「抱歉，抱歉，亮度只有 0 到 100，「200」这个数我做不到」。
族谱：v1.1.38 修过 zh_error 自带字头（只剥全失败支）、1.5.1 修过"好的，好的"（成功支）
——**失败支模板 `_step_say` 本身**是第三处漏网，本钉把它钉死。
修法单点：_step_say 见到 reason 已以「抱歉」开头即不再叠字头（四道前置闸共用本模板）。
"""
from core.executor import Executor


def _say(reason, steps=1, idx=0):
    return Executor._step_say(idx, [None] * steps, reason)


def test_step_say_does_not_stack_apology_when_reason_has_it():
    """主案：reason 自带「抱歉，」⇒ 不得再叠（复读机回归钉）。"""
    r = _say("抱歉，亮度只有 0 到 100，「200」这个数我做不到")
    assert not r.startswith("抱歉，抱歉"), r
    assert r.count("抱歉") == 1, r
    assert "0 到 100" in r and "200" in r


def test_step_say_still_prefixes_when_reason_plain():
    """反证：reason 是裸事实句（无字头）⇒ 仍要补「抱歉，」（语气不丢）。"""
    r = _say("这一步没拿到执行回执，设备可能已经动作了")
    assert r.startswith("抱歉，"), r
    assert r.count("抱歉") == 1, r


def test_step_say_chain_forms_keep_step_order():
    """链式三形（首腿失败/中腿失败）步序与字头同时正确。"""
    r0 = _say("抱歉，亮度只有 0 到 100", steps=2, idx=0)
    assert "第 1 步没成功" in r0 and not r0.startswith("抱歉，抱歉"), r0
    r1 = _say("抱歉，亮度只有 0 到 100", steps=2, idx=1)
    assert "前面 1 步已完成" in r1 and r1.count("抱歉") == 1, r1
    r2 = _say("这一步没拿到执行回执", steps=3, idx=1)
    assert r2.startswith("抱歉，前面 1 步已完成"), r2


def test_all_front_gates_share_the_template():
    """四道前置闸（开关族能力/能力预裁/可用态/百分比量程）都走 _step_say ⇒ 单点修全族受益；
    这里把"都调用 _step_say"钉成契约，防将来某闸绕过模板自己拼字头。

    路径纪律：必须走 ROOT（＝本文件所在 tests/ 的上级＝huijian_voice），**不得用相对
    CWD 路径**——CI 在仓库根跑 pytest（`pytest huijian_voice/tests`），CWD≠huijian_voice，
    相对写法在 CI 上 FileNotFoundError（1.5.3 首跑实锤，本钉自己把发布挡了一轮）。"""
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    src = (ROOT / "core" / "executor.py").read_text(encoding="utf-8")
    n = src.count("_step_say(idx, steps,")
    assert n >= 4, f"前置闸调用 _step_say 处数={n}（应≥4：四道闸共用文案）"
