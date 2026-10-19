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
    相对写法在 CI 上 FileNotFoundError（1.5.3 首跑实锤，本钉自己把发布挡了一轮）。

    v1.6.5（刀5-c 量具假绿收口）：旧形是 `src.count("_step_say(idx, steps,") >= 4`——
    **源码文本计数钉**，run() 里调用点实有 5 处（四闸＋失败腿），`>=4` 的余量让"摘掉
    任意一整道闸的调用点"（例如 N2 量程闸整块删掉，5→4）照样绿＝假绿在册（10-08 复核）。
    改判据：**逐闸核**——四道闸各自的裁决变量（gate/rng/cap/avail）都必须作为
    `_step_say(...)` 的实参出现，且该闸的早退 return 走 `self._named(False, ...)`。
    摘掉任一闸的接线（把 rng 换成裸拼串、或整块删除）本钉当场红，无余量可躲。"""
    # v1.6.6 刀12：全文 substring/regex 换成 **ast 判据**——旧形两种躲法都在：
    #   ① 在同文件注释里留一串 `gate = self._turn_gate`／`_step_say(idx, steps, rng)`
    #      就把断言喂绿（本仓「治假绿的钉会被它描述的那个 bug 的注释满足」实发同族）；
    #   ② 只核"接了线"不核**闸序**——刀3·B3 修的就是量程闸必须排在能力/可用态**之后**
    #      （真因先报），把 rng 挪回最前，旧钉照样全绿而谎报复发。
    # 现在：真赋值节点 → 真调用实参 → 四道闸的行号次序，三件一起核。
    import ast as _ast
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    src = (ROOT / "core" / "executor.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)

    def _gate_line(attr: str):
        """找 `x = [await] self.<attr>(…)` 的赋值，返回 (变量名, 行号)。"""
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Assign) or len(node.targets) != 1:
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, _ast.Name):
                continue
            val = node.value
            if isinstance(val, _ast.Await):
                val = val.value
            fn = getattr(val, "func", None)
            if (isinstance(fn, _ast.Attribute) and fn.attr == attr
                    and isinstance(fn.value, _ast.Name) and fn.value.id == "self"):
                return tgt.id, node.lineno
        raise AssertionError(f"{attr}：源里找不到它的裁决赋值节点（整道闸没了？）")

    order = [("gate", "_turn_gate"), ("cap", "_capability_refuse"),
             ("avail", "_availability_refuse"), ("rng", "_adjust_range_refuse")]
    got = {}
    for var, attr in order:
        name, line = _gate_line(attr)
        assert name == var, f"{attr} 的裁决变量改名了（{name}≠{var}，本钉需同步）"
        got[var] = line

    # ① 接线：每道闸的裁决变量必须作为某次 _step_say(...) 的实参出现（真节点）
    for var in got:
        wired = False
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Call)
                    and (getattr(node.func, "id", None)
                         or getattr(node.func, "attr", None)) == "_step_say"
                    and any(isinstance(a, _ast.Name) and a.id == var for a in node.args)):
                wired = True
                assert node.lineno > got[var],                     f"{var}：_step_say 调用在裁决之前（先念话术后裁决＝空转）"
        assert wired, f"{var}：闸话术没走 _step_say 模板（绕过＝字头叠字回归可复发）"

    # ② 闸序（刀3·B3 承重）：能力/可用态两道必须排在量程闸之前——真因先报
    assert got["cap"] < got["rng"] and got["avail"] < got["rng"],         f"量程闸又跑到能力/可用态之前了（{got}）＝「不支持亮度的灯」被念成「只有0到100」"
