# -*- coding: utf-8 -*-
"""2026-10-08 第五轮复查 P1 批的专项钉（七刀，逐刀一钉一组）。

这一批全部来自"宣称已修/没人修"与"同类洞只修一处"两族，判据一律要求
**能红**：加载项侧走真函数行为臂（`FastPath.match` / `zh_error` 不需要 HA），
集成侧走 AST 读**实际调用的字面量**（本仓 venv 装不了 homeassistant，
按 `test_ota_firmware.py` / `test_audit4_fixes.py` 的先例；但绝不用源码字符串
匹配——注释里提到那个 bug 就能把文本钉满足，本仓记过这个坑）。

对应缺陷（编号同审计报告）：
  P1-1 assist 条目 reload 后全屋语音自动化停摆（钉在 test_audit4_fixes 里升级）
  P1-2 「打开热水器」恒失败：属性名 `operation_modes` 不存在
  P1-3 裸窗句（「关窗」「开窗」「打开所有窗户」）恒失败且拿不到正确引导
  P1-4 面板改/删自动化不重算跟踪实体 ⇒ 自动化静默失效而面板回「已更新」
  P1-5 LLM 工具通道不带原话 ⇒ 开关族能力闸整条失明
  P1-6 「湿度」漏出 T0 属性交替式 ⇒ 属性尾巴静默丢并按开关谎报成功
  P1-7 「把没关严窗户拉上」被整句否定判据误拒（状态定语不是拒绝）
"""
import ast
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core.executor import zh_error                      # noqa: E402
from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import (                        # noqa: E402
    FastPath, _T0_ATTR_TAIL, _T0_ATTR_WORD, is_refusal_imperative)

CC = os.path.join(ROOT, "custom_components", "huijian_ai")


class _NoScenes:
    def needs_blocking(self):
        return False

    async def refresh(self):
        return None

    def refresh_soon(self):
        return None

    def check(self, text):
        return None

    async def verify_or_refresh(self, plan):
        return True


HOME = {"light.d": {"attributes": {"friendly_name": "客厅灯"}},
        "humidifier.h": {"attributes": {"friendly_name": "加湿器"}},
        "cover.win": {"attributes": {"friendly_name": "客厅平开窗"}}}


@pytest.fixture(autouse=True)
def _restore_targets_vocab():
    saved = {k: getattr(T, k, None) for k in
             ("_dyn_vocab", "ALL_DEVICES", "ALL_SET", "_ALL_MIN2",
              "_dyn_domains", "_dyn_lookup", "_dyn_areas")}
    yield
    for k, v in saved.items():
        if hasattr(T, k):
            setattr(T, k, v)


def _fp():
    from test_experience_batch import PSettings

    T.sync_vocab(HOME)
    T.sync_areas(["客厅"])
    return FastPath(_NoScenes(), None, PSettings(), areas_of=lambda: ("客厅",))


def _fn_src(path, name, async_ok=True):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    kinds = (ast.AsyncFunctionDef, ast.FunctionDef) if async_ok else (ast.FunctionDef,)
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.name == name:
            return src, node
    raise AssertionError(f"{path} 里找不到 {name}（文件改名/函数没了＝接线断了）")


def _calls_in(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# ── P1-2 热水器属性名 ────────────────────────────────────────────────
def test_water_heater_reads_real_attribute_name():
    """`operation_modes` 在这台 HA 上永不存在 ⇒ 恒 raise ⇒ 「打开热水器」100% 失败。

    上游真名：home-assistant/core `water_heater/__init__.py:70`
    `ATTR_OPERATION_LIST = "operation_list"`（2026-10-08 gh api 取源码核对）。
    钉读**实际传给 .get() 的字面量**，不读注释。
    """
    src, fn = _fn_src(os.path.join(CC, "intent_turn.py"), "_handle_match_target")
    got = []
    for c in _calls_in(fn):
        if isinstance(c.func, ast.Attribute) and c.func.attr == "get" \
                and len(c.args) >= 1 and _const_str(c.args[0]) in (
                    "operation_modes", "operation_list"):
            got.append(_const_str(c.args[0]))
    assert got == ["operation_list"], (
        f"water_heater 分支读的属性名不对：{got}（HA 真名是 operation_list）")
    # 残留判据必须**读字面量**，不读源码文本：本轮修复的注释里就引用了旧错名，
    # 文本钉会被它自己描述的那个 bug 满足（本仓记过的假绿同型坑）。
    stale = [n.value for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Constant) and n.value == "operation_modes"]
    assert not stale, "旧错名还作为字面量存在＝同一函数两处读两种键"


# ── P1-3 裸窗句 ──────────────────────────────────────────────────────
def test_control_window_target_slot_is_optional():
    """target 缺失必须在**处理层**如实失败，不能在 slot 校验层抛英文 required 错——
    后者让下面那条友好支永远到不了，用户拿到泛化道歉而不是正确句式引导。"""
    src, fn = _fn_src(os.path.join(CC, "intent_window_control.py"), "slot_schema",
                      async_ok=False)
    keys = {}
    for c in _calls_in(fn):
        if isinstance(c.func, ast.Attribute) and c.func.attr in ("Required", "Optional") \
                and len(c.args) >= 1:
            keys[_const_str(c.args[0])] = c.func.attr
    assert keys.get("target") == "Optional", (
        f"ControlWindow 的 target 槽形态回到 {keys.get('target')}＝裸窗句又炸英文串")


def test_no_target_failure_gives_window_guidance():
    """行为臂：加载项话术层必须把这条失败翻成"带房间名+窗型"的引导。"""
    say = zh_error("No target specified")
    assert "窗" in say and ("房间" in say or "窗型" in say), say
    assert "No target" not in say, f"英文串被念出去：{say}"
    assert not say.startswith("抱歉，这一步没有执行成功"), f"落进兜底模板：{say}"


# ── P1-4 面板写自动化后重算跟踪实体 ────────────────────────────────────
@pytest.mark.parametrize("cls,method", [("AutomationDeleteView", "delete"),
                                        ("AutomationDeleteView", "put")])
def test_panel_automation_writes_refresh_tracked_entities(cls, method):
    """语音 CRUD 三条路早已重算，面板两条路一条都没调 ⇒ 面板改完触发传感器，
    新实体不进 `_tracked_entity_ids`，状态监听按它过滤 ⇒ 这条自动化再也不触发，
    而面板回「已更新」＝假成功。"""
    src = open(os.path.join(CC, "api.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    klass = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == cls), None)
    assert klass, f"视图类 {cls} 不见了（路由改名要同步本钉）"
    fn = next((n for n in klass.body
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name == method), None)
    assert fn, f"{cls}.{method} 不见了"
    names = [c.func.attr for c in _calls_in(fn)
             if isinstance(c.func, ast.Attribute)]
    assert "async_refresh_tracked_entities" in names, (
        f"{cls}.{method} 没重算跟踪实体集：面板写完监听面与存储层不同源")


def test_store_only_writes_are_not_enough():
    """反向不变量：只调 store 不重算的那条路必须被本钉认出——把上面的刷新行删掉
    就得红（防我把判据写成"文件里有这句就行"）。"""
    src = open(os.path.join(CC, "api.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    hits = 0
    for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if not klass.name.startswith("Automation"):
            continue
        for fn in klass.body:
            if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and fn.name in ("delete", "put"):
                store_writes = [c.func.attr for c in _calls_in(fn)
                                if isinstance(c.func, ast.Attribute)
                                and attr_starts(c.func.attr, ("update_automation",
                                                              "delete_automation"))]
                refresh = [c.func.attr for c in _calls_in(fn)
                           if isinstance(c.func, ast.Attribute)
                           and c.func.attr == "async_refresh_tracked_entities"]
                if store_writes:
                    hits += 1
                    assert refresh, f"{klass.name}.{fn.name} 落库了但没重算"
    assert hits == 2, f"面板写自动化入口应有两处（put+delete），实得 {hits}＝覆盖面漂移"


def attr_starts(a, prefixes):
    return any(a.startswith(p) for p in prefixes)


# ── P1-5 LLM 工具通道带原话 ───────────────────────────────────────────
def test_llm_tool_plan_carries_utterance():
    """`_turn_gate` 对 target 形 args 的**唯一**判据是原话里的窗型词；
    旧形 `Plan(... source="llm")` 不传 utterance ⇒ executor 拿到空串 ⇒ 恒放行
    ⇒ 往开窗器的 button/sensor/number 兄弟实体喂 turn_on 并播「办好了」。"""
    rec = _RecExec()
    ag = _agent(rec)
    asyncio.run(ag._tool("TurnDeviceOn",
                         {"target": [{"devices": [{"name": "平开窗"}]}]},
                         "别开客厅的平开窗"))
    assert rec.plans, "工具道没产计划，本钉失去判据对象"
    assert rec.plans[0].utterance == "别开客厅的平开窗", (
        f"原话没随计划下行：utterance={rec.plans[0].utterance!r}")


def test_agent_answer_passes_text_into_tool():
    """接线钉：`answer()` 的工具循环必须把用户原话递给 `_tool`（AST 数实参）。"""
    src, fn = _fn_src(os.path.join(ROOT, "core", "agent.py"), "answer")
    calls = [c for c in _calls_in(fn)
             if isinstance(c.func, ast.Attribute) and c.func.attr == "_tool"]
    assert calls, "answer() 里没有 _tool 调用＝工具道没了"
    for c in calls:
        assert len(c.args) >= 3, (
            f"agent.py:{c.lineno} _tool 只传了 {len(c.args)} 个实参——"
            "原话没递进去，执行侧几道吃 utterance 的闸又成瞎子")


class _RecExec:
    def __init__(self):
        self.plans = []

    async def run(self, plan):
        self.plans.append(plan)
        return True, "好的，办好了"


def _agent(rec):
    from core.agent import Agent

    class _S:
        def get(self, k, d=None):
            return {"llm.enabled": True, "llm.base_url": "http://x",
                    "dialog.confirm_risky": False,
                    "llm.allow_scene_write": True}.get(k, d)

    return Agent(_S(), None, rec)


# ── P1-6 湿度不再从属性交替式里漏掉 ───────────────────────────────────
def test_humidity_tail_is_not_dropped():
    """「打开加湿器湿度50」旧形落 TurnDeviceOn(加湿器)＋播「好的」——
    属性尾巴静默蒸发，正是本模块 M3 深审「绝不按开关谎报」所禁形态。"""
    fp = _fp()
    plan = asyncio.run(fp.match("打开加湿器湿度50"))
    assert plan is not None, "整条失配（连调湿度都不接了）"
    assert plan.intent != "TurnDeviceOn", (
        f"湿度值被吞、按开关执行：{plan.intent} {plan.args}")
    assert plan.args.get("attribute") == "humidity", plan.args
    assert "50" in str(plan.args.get("delta") or plan.args.get("value") or ""), plan.args


def test_t0_attr_alternation_is_derived_from_word_table():
    """结构不变量：交替式必须等于词典的键集——抄第二份表就是这次漏湿度的根因。"""
    for word in _T0_ATTR_WORD:
        assert word in _T0_ATTR_TAIL.pattern, f"{word} 在词典里却进不了尾巴正则（又抄了一份）"
    # 反向：正则里不许出现词典没有的词（否则 :1578 `_T0_ATTR_WORD[attr_word]` KeyError）
    body = _T0_ATTR_TAIL.pattern
    seg = body.split("(?P<attr>", 1)[1].split(")", 1)[0]
    for word in seg.split("|"):
        assert word in _T0_ATTR_WORD, f"交替式里的 {word} 词典里没有＝KeyError 形状"


# ── P1-7 状态定语不是拒绝 ─────────────────────────────────────────────
def test_state_modifier_clause_is_not_refused():
    for t in ("把没关严窗户拉上", "把没开完的窗继续开", "没关严的客厅窗关上"):
        assert not is_refusal_imperative(t, ("客厅",)), f"{t} 被当拒绝句误拒"


def test_strong_negation_still_refused():
    for t in ("不要把窗关上", "别开灯", "不要关窗", "窗帘不要拉到底",
              "别开客厅的灯", "别开灯，把电视关了"):
        assert is_refusal_imperative(t, ("客厅",)), f"{t} 被放行＝能做用户明说不要的动作"


def test_curtain_guard_no_longer_kills_the_whole_sentence():
    """形状钉：帘窗语序那道「不改写」分支里不许再 `_miss` 判死整句。

    第五轮复查指控 4 成立——P1-7 第一版只改了拒执行闸那一道，帘窗这道仍用整句判据
    `_miss`，等于同一病灶的第二处。修法＝只跳过改写、原话继续往下走。
    """
    src, fn = _fn_src(os.path.join(ROOT, "core", "nlu", "fast_path.py"), "match")
    seg = ast.get_source_segment(src, fn) or ""
    tail = seg.split("帘窗语序:否定句不改写", 1)[1].split("帘窗语序→", 1)[0]
    assert "_miss" not in tail, (
        "帘窗『不改写』分支又回到 return self._miss(...)——弱否定定语真命令被整句判死")


def test_state_modifier_with_window_type_produces_plan():
    """行为钉（能证的那一半）：带窗型的状态定语命令要出计划；强否定照旧不接管。

    如实标注边界：`把没关严窗户拉上`（泛称"窗户"、无区域）修完**仍不出计划**——
    那是"绝不猜房间/绝不冒按全区"的既有红线，不是本刀能宣称修好的东西；
    本刀只保证它不再被当成"用户拒绝"来拒（理由换了，终态没变）。
    """
    from test_experience_batch import PSettings
    T.sync_vocab({"cover.a": {"attributes": {"friendly_name": "客厅 平开窗"}},
                  "light.d": {"attributes": {"friendly_name": "客厅灯"}}})
    T.sync_areas(["客厅"])
    fp = FastPath(_NoScenes(), None, PSettings(), areas_of=lambda: ("客厅",))
    plan = asyncio.run(fp.match("把没关严平开窗关上"))
    assert plan is not None and plan.intent == "ControlWindow", plan
    for t in ("别把窗帘关上", "不要把窗关上"):
        assert asyncio.run(fp.match(t)) is None, f"{t} 被放行＝做用户明说不要的动作"


def test_bare_negation_weak_head_never_executes_alone():
    """裸「没」开头且整句就是拒绝（「没关灯」）时仍要否决——分档不是放宽。"""
    assert is_refusal_imperative("没关灯", ())
    assert is_refusal_imperative("没开过窗", ())
