# -*- coding: utf-8 -*-
"""刀3·A3：延时到期轮与真人对话态的隔离钉（审计 2026-10-17 发布树实锤入账）。

现场（三向互踩，外部深审+本会话复验）：`_fire_delay` 到期重入**裸 handle()**，
而 Pipeline 是常驻单实例、`_confirm/_turns/_last_target` 是按 origin 索引的
实例级共享字典 ⇒ 无人值守的定时器与真人对话腿互踩：

  A 吃环：真人挂起风险确认环等回答，延时腿到期正文撞 `_confirm_answer`——
    旧形改口支把真人挂起的环 **pop 掉**，用户答"确认"时环已没了，
    真人指令没做也没说为什么。
  B 挂环：风险正文到期**静默**写 `_confirm[origin]`（到期不播报＝环无声挂起），
    用户下一句随口的「好的」落 `_CONFIRM_YES` → 从未问出口的计划**真执行**。
    载体用锁族形状（词法拒层没见过的"把大门开一下"接地成 HassUnlock＝纵深支）；
    审计口径：现网 lock 0 台锁形被 v1.5.0 预审拦是假警报、删除族真形走创建承接
    不带环（那是 B/C 组另一条账，不在刀3 射程，本批只钉"_confirm 绝不被延时轮写"）。
  C 污染：到期轮写 `_turns`（挤 LLM 历史）/`_last_target`（顶掉真人焦点栈），
    代词轮按执行时刻重解析 → 开/关用户根本没指的那台（实测 F2）。
  D 承接顺序洞（本批新钉钓出的新发现）：级联里 `_voice_creation` 跑在
    `_delay_gate` **之前**，「1秒后把办公室的自动化删掉」被创建承接吃掉——
    **延时前缀被静默剥掉、删除当场执行**（v1.5.8"先于链切分"同款病灶的创建支
    复现）。修＝带延时形状的整句一律不进创建承接，交回延时闸。

修法（双闸）：
  排程拒层（_delay_body_refusal）：锁族/删除族与零描述代词正文当场如实拒、不排；
  到期隔离层（_DELAY_TURN ContextVar）：延时轮不吃/不挂环、不写对话态、
  不读真人焦点栈；歧义/风险/离线/区域四道执行闸一视同仁照常过。
延时无特权＝纪律不变；对话态隔离性质不同：四道闸问"这条该不该做"（到期照问），
对话态问"用户此刻在不在对话"（到期轮确定不在，隔离而非拒）。

夹具说明（第一跑定性轮实测教训）：句型刻意全部走**风险环正道**（锁族），
不碰创建承接支（_scene_delete 会读 self.scenes.triggers——删除句型配 None 夹具
当场 AttributeError＝量具锅，已在 FakeScenes 替身兜住）；每次命中给**新 Plan**
（Plan.trace 可变，跨轮复用互相污染断言）；不同轮用不同句面（2s dedup 窗）。
"""
import asyncio
import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.nlu.fast_path import Plan                       # noqa: E402
from core.pipeline import Pipeline, _delay_turn_isolated  # noqa: E402
from test_experience_batch import (HA, Lane, NullQuery, PSettings,  # noqa: E402
                                   RecExecutor)
import core.pipeline as P                                 # noqa: E402

ORIGIN = "sat-office-51c0"


class FakeScenes:
    """创建承接支替身：最小可用（triggers 表 + 无触发词命中）。"""
    triggers = ["晚安", "我回来了"]

    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass


def _on():
    return Plan("TurnDeviceOn",
                {"target": [{"area": "办公室", "devices": [{"name": "射灯"}]}]},
                source="t0", utterance="打开办公室射灯")


def _desk():
    return Plan("TurnDeviceOn",
                {"target": [{"area": "办公室", "devices": [{"name": "台灯"}]}]},
                source="t0", utterance="打开台灯")


def _un():
    """锁族形状（风险环正道载体，理由见模块头 B）。"""
    return Plan("HassUnlock",
                {"target": [{"devices": [{"name": "大门", "domains": ["lock"]}]}]},
                source="t0", utterance="解锁大门")


def _bare():
    """空目标句（「再开一下」形）：_apply_context 会走到读焦点栈那一路——
    注入闸的判据力就靠这颗 plan 撑（有明示目标的句子在 :2305 早退，碰不到注入）。"""
    return Plan("TurnDeviceOn", {}, source="t0", utterance="再开一下")


def _tables():
    """每次新 dict + 新 Plan（可变 trace 不跨轮复用）。"""
    return {"解锁大门": _un(), "把大门开一下": _un(),
            "打开办公室射灯": _on(), "打开台灯": _desk(),
            "关掉它": _on(), "再开一下": _bare()}


def _mk(table=None, single=None):
    """与 test_v158_delay_scheduling._mk 同款手工装配夹具。"""
    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    p.ha = HA({})
    p.executor = RecExecutor()
    p.agent = None
    p.query = NullQuery()
    p.fast_path = Lane(table=table or {}, single=single)
    p.klar = Lane()
    p.scenes = FakeScenes()
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._delay_tasks = set()
    p._vocab_ts = time.time()
    return p


# ── ① 排程拒层：不可无人值守的正文当场拒、不排 ────────────────────────
@pytest.mark.parametrize("text", [
    "十秒后删除语音场景晚安",
    "1秒后把办公室的自动化删掉",   # 创建承接洞载体：修后由拒层收口（不再被剥词直删）
    "3秒后解锁大门",
    "两秒后把门锁上",              # 「锁」字宽口径：动宾齐不齐都拒
    "十秒后关掉它",                # 零描述代词正文
    "十秒后把它打开",
])
def test_risky_or_pronoun_body_refused_at_scheduling(text):
    p = _mk(table=_tables())
    r = asyncio.run(p.handle(text, ORIGIN))
    assert r.source == "delay" and not r.ok, f"{text!r} 未拒排: {r.text}"
    assert not p._delay_tasks, f"{text!r} 被排上了（拒层失守）"
    assert not p.executor.plans, f"{text!r} 立即执行了（剥词跑老路/承接抢跑）"


def test_creation_takeover_never_swallows_delay_prefix():
    """D 承接顺序洞行为钉：延时句**绝不**以 creation 源收口、绝不当场执行。"""
    for t in ["1秒后把办公室的自动化删掉", "1秒后删除场景晚安"]:
        p = _mk(table=_tables())
        r = asyncio.run(p.handle(t, ORIGIN))
        assert r.source != "creation", f"{t!r} 仍被创建承接吃掉（剥掉延时前缀）"
        assert not p.executor.plans, f"{t!r} 删除被当场执行"


def test_legit_body_still_schedules():
    """反向保护：合法正文照排（拒层不许扩大化到"什么都别排"）。"""
    async def scenario():
        p = _mk(table=_tables())
        r = await p.handle("1秒后打开办公室射灯", ORIGIN)
        assert r.source == "delay" and r.ok, r.text
        assert len(p._delay_tasks) == 1
        await asyncio.sleep(1.3)
    asyncio.run(scenario())


def test_named_with_demonstrative_not_refused():
    """「那个射灯」是带真名的指示词，不是零描述代词——不许误拒（过度拒＝少做）。"""
    p = _mk(table={"打开那个射灯": _on()})
    r = asyncio.run(p.handle("5秒后打开那个射灯", ORIGIN))
    assert r.source == "delay" and r.ok, f"带名指示词被误拒: {r.text}"


# ── ② 吃环闸（场景 A）：延时腿到期不得消费/撤销真人挂起的环 ──────────
def test_fire_leg_never_eats_human_confirm_ring():
    """时序要害：**先排延时、真人后挂环**，让到期撞环。
    （反序不构成本病灶：真人挂环期间再说新指令＝既有"改口撤环"设计语义，
    排程句是真人话、pop 得有道理——第一版场景把两者摆反，侦听实测 pop 来自
    _confirm_answer 改口支的排程轮而非到期轮，此处如实纠正时序。）"""
    async def scenario():
        p = _mk(table=_tables())
        r0 = await p.handle("2秒后打开台灯", ORIGIN)
        assert r0.source == "delay" and r0.ok, r0.text
        r1 = await p.handle("解锁大门", ORIGIN)
        assert r1.source == "confirm" and ORIGIN in p._confirm, \
            f"真人轮未挂环（夹具失效）: {r1.source}"
        await asyncio.sleep(2.4)               # 到期：环仍挂着的窗口内
        assert ORIGIN in p._confirm, "延时腿把真人挂起的环吃了/改了——吃环闸失守"
        assert [x.intent for x in p.executor.plans] == ["TurnDeviceOn"], \
            "延时腿没执行（隔离不应改变执行面）"
        # 环还活着：用户答"确认"照常执行真人那条
        r3 = await p.handle("确认", ORIGIN)
        assert r3.source == "confirm_exec" and r3.ok
        assert p.executor.plans[-1].intent == "HassUnlock"
    asyncio.run(scenario())


# ── ③ 挂环闸（场景 B）：词法没见过的风险形状，到期也不挂环、拒执行零下发 ─
def test_risky_shape_bypassing_lex_filter_never_arms_ring():
    """「把大门开一下」不含锁字面 ⇒ 过排程拒层；级联接地成 HassUnlock ⇒
    纵深必须收口：不写 _confirm、不执行、如实拒。"""
    async def scenario():
        p = _mk(table=_tables())
        r = await p.handle("1秒后把大门开一下", ORIGIN)
        assert r.source == "delay" and r.ok, r.text     # 词法放行（拒层不背此锅）
        await asyncio.sleep(1.3)
        assert not p._confirm, f"延时轮把风险环挂上了: {dict(p._confirm)}"
        assert p.executor.plans == [], "无确认环的风险计划被直接执行"
        # 且此刻用户随口一句「好的」**不能**触发解锁（环从未存在）
        r2 = await p.handle("好的", ORIGIN)
        assert r2.source != "confirm_exec", "凭空冒出的环被执行了"
    asyncio.run(scenario())


# ── ④ 污染闸（场景 C）：到期轮不写对话账/焦点栈，也不读真人焦点栈 ──────
def test_fire_leg_leaves_dialogue_state_untouched():
    async def scenario():
        p = _mk(table=_tables())
        # 真人轮先立焦点栈与对话账
        await p.handle("打开办公室射灯", ORIGIN)
        ts0 = p._last_target[ORIGIN]["ts"]
        turns0 = len(p._turns[ORIGIN])
        p.executor.plans.clear()
        # 排 1 秒延时并到期执行（换句面「打开台灯」——2s dedup 窗不吃同文本重放）
        await p.handle("1秒后打开台灯", ORIGIN)
        await asyncio.sleep(1.3)
        assert len(p.executor.plans) == 1, "到期没执行（隔离改变了执行面）"
        assert p._last_target[ORIGIN]["ts"] == ts0, "延时轮顶写了真人焦点栈"
        assert len(p._turns[ORIGIN]) == turns0, "延时轮把对话账挤了一行"
    asyncio.run(scenario())


def test_fire_leg_does_not_inherit_human_focus():
    """空目标正文（_bare 无明示目标，_apply_context 真走到读焦点栈那一路）：
    延时轮也不许吃 _last_target 注入——注入闸失守则 trace 出现「继承目标」。"""
    async def scenario():
        p = _mk(table=_tables())
        await p.handle("打开办公室射灯", ORIGIN)     # 真人立焦点
        await p._fire_delay("再开一下", 0, ORIGIN)   # 直驱到期路径（绕词面拒层）
        assert len(p.executor.plans) >= 1            # 执行照常发生（隔离≠拒做）
        tr = p.executor.plans[-1].trace or []
        assert not any("继承" in t or "上下文" in t for t in tr), \
            f"延时轮吃了真人焦点栈: {tr}"
    asyncio.run(scenario())


# ── ⑤ 反向保护：真人轮一切逐字不变（隔离只认延时轮） ─────────────────
def test_human_ring_answer_still_works():
    """真人挂环→「好的」→执行：_confirm_answer 的隔离闸绝不能误伤真人路径。"""
    async def scenario():
        p = _mk(table=_tables())
        r1 = await p.handle("解锁大门", ORIGIN)
        assert r1.source == "confirm", r1.source
        r2 = await p.handle("好的", ORIGIN)
        assert r2.source == "confirm_exec" and r2.ok
        assert p.executor.plans and \
            p.executor.plans[-1].intent == "HassUnlock"
    asyncio.run(scenario())


def test_plain_and_chain_sentences_untouched():
    for t, expect in [("打开办公室射灯", ("t0",)),
                      ("关闭办公室射灯关闭办公室平开窗", ("chain", "t0")),
                      ("打开台灯", ("t0",)),
                      ("把没关紧的窗关上", ("t0",))]:
        p = _mk(single=_on())
        r = asyncio.run(p.handle(t, ORIGIN))
        assert r.source in expect and r.ok, f"{t!r} 主链被扰动: {r.source}"
        assert not p._delay_tasks


def test_flag_defaults_false_outside_delay():
    """无旗上下文＝False（真人轮零改动）。"""
    assert _delay_turn_isolated() is False


# ── ⑥ 接线钉（v1.54 教训：加了闸没接线＝没修；判代码形状不判注释词） ──
def test_wiring_fire_delay_sets_and_resets_flag():
    import inspect
    src = inspect.getsource(Pipeline._fire_delay)
    assert "_DELAY_TURN.set(True)" in src, "_fire_delay 未置旗"
    assert "finally:" in src and "_DELAY_TURN.reset(token)" in src, "旗未 finally 复位（粘旗风险）"
    assert "停机/主动撤单" not in src, "注释复辟——主动撤单不存在，注释必须只写停机"


def test_wiring_four_consumers_guarded():
    import inspect
    for fn, needle in ((Pipeline._confirm_answer, "if _delay_turn_isolated():"),
                       (Pipeline._note_target, "if _delay_turn_isolated():"),
                       (Pipeline._remember_turn, "if _delay_turn_isolated():"),
                       (Pipeline._confirm_ask, "if _delay_turn_isolated():"),
                       (Pipeline._ambiguity_ask, "if _delay_turn_isolated():"),
                       (Pipeline._apply_context, "if _delay_turn_isolated():"),
                       (Pipeline._voice_creation, "delay.has_delay_shape(")):
        s = inspect.getsource(fn)
        assert needle in s, f"{fn.__name__} 未接线（隔离闸/承接闸缺失）"
    # 吃环闸必须在读 _confirm 之前（先闸后读，次序反了＝照吃）
    ca = inspect.getsource(Pipeline._confirm_answer)
    assert ca.index("_delay_turn_isolated()") < ca.index("_confirm.get("), \
        "吃环闸位置错误：必须在触碰 _confirm 之前"
    # 两处挂环闸都必须在写 _confirm 之前（次序反了＝照挂）
    # （歧义挂环支的地形构造跨三层收窄证据，v155 作者同样放弃拼地形、
    #   走源码位置钉——本批沿用该先例，如实注明判据层级。）
    for name in ("_confirm_ask", "_ambiguity_ask"):
        ck = inspect.getsource(getattr(Pipeline, name))
        assert ck.index("_delay_turn_isolated()") < ck.index("self._confirm[origin]"), \
            f"挂环闸位置错误（{name}）：必须在写环之前"
    # 注入读闸必须在"读 _last_target"那一支之前，且不许吞掉同句 seed 路
    ac = inspect.getsource(Pipeline._apply_context)
    assert ac.index("_delay_turn_isolated()") < ac.index("self._last_target.get("), \
        "注入读闸位置错误：必须先于读焦点栈"


def test_chain_branch_pop_must_stay_unconditional():
    """反向钉（v1.6.2 定性轮自纠入账）：链分句 confirm 退回支的
    `_confirm.pop(origin or "panel", None)` **不得**加延时旗守卫。

    该 pop 摘的是上一行 _ambiguity_ask 刚挂的环（自己挂自己摘＝无条件安全）。
    若有人以"保护真人环"为名加 `if not _delay_turn_isolated():`，延时轮会
    带着残留环活到 TTL 后被真人一句「好的」吃掉——恰好把本批要修的 A3-B
    事故放回来。撤守卫比加守卫安全，这条纪律钉死（源码判据：pop 行前
    紧邻不得出现旗标条件）。"""
    import re
    src = (Path(__file__).resolve().parents[1] / "core" / "pipeline.py").read_text(
        encoding="utf-8")
    m = re.search(r"(if not _delay_turn_isolated\(\):\s*\n\s*)"
                  r"self\._confirm\.pop\(origin or \"panel\", None\)", src)
    assert m is None, "链分支 pop 被加了旗守卫＝引入残留环洞（见本钉 docstring）"
