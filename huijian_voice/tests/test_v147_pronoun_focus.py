# -*- coding: utf-8 -*-
"""P5（v1.4.7·任务目标2·HomePod 焦点对齐）——代词句必须走**本地焦点栈**，不走引擎猜。

现场（部署版 1.4.5 级联日志逐字，10-11 线上探针）：
    [级联] '打开办公室射灯' → [klar] '办公室射灯开了'
    [级联] '关掉它'        → [klar] '办公室关了。'    ← 只动了一台射灯
引擎（klar）没有会话记忆，"它"落给引擎＝拿全户图谱猜指代——猜对了台、播错了范围
（"办公室关了"把单台泛化成整间房，P1"话术不得超额承诺"同族），猜错了就是
v1.1.35 钉过的"顶同类别另一台"。慧尖体验批 P2-10 早已建好 `_last_target[origin]`
焦点栈（TTL 90s、按发起设备分桶），但 `select_primary_plan` 里代词空目标计划恒输给
klar ⇒ **生产态下继承道从未被触发**。离线 3056 条全绿从未抓到，是因为金标 harness
（gold_retest.build）把 `klar.enabled` 设为 False、而离线钉的仲裁面又从未让
"kl 具名计划 × fp 代词计划"同台竞标——双臂全绿自欺，同一课第二次学费。

P5 的三条窄化（一条多放都不行）：
  · 只吃**缺明示目标**的计划——「再打开书房门」这类剥离后带明示目标的回指句照旧原仲裁；
  · 旗标用 fast_path 已裁定集（FLAG_PRONOUN_TARGET/FLAG_ANAPHORA_STRIPPED）或裸值
    调节形（Adjust/SetDeviceMode 无目标——本就靠继承/同族补），不复制第二份词表；
  · `context_ready` 默认 False＝逐字旧行为——既有钉零漂移（v1092 `fp=None` 单 kl
    形状的钉永不触发本闸）。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.nlu.fast_path import (FLAG_ANAPHORA_STRIPPED, FLAG_PRONOUN_TARGET,
                                Plan)
from core.pipeline import select_primary_plan

KL_EID = "light.bie_ren_de_deng"          # 引擎替"它"猜的另一台（故意不是射灯）


def _fp_pronoun(text="关掉它"):
    # 旗标由 trace 派生（_FLAG_TRACE_TOKENS 单点表），与真 fast_path 产物同形
    return Plan(intent="TurnDeviceOff", args={}, source="t0", utterance=text,
                trace=["代词目标:它→待上下文注入"])


def _kl_named(text="关掉它"):
    return Plan(intent="HassTurnOff", args={"entity_id": KL_EID},
                source="klar", utterance=text)


# ── ① 纯仲裁：焦点栈在位 ⇒ 继承道赢；缺位 ⇒ 逐字旧行为 ────────────
def test_pronoun_with_fresh_focus_beats_engine_guess():
    fp, kl = _fp_pronoun(), _kl_named()
    assert select_primary_plan(fp, kl) is kl, "默认参必须逐字旧行为（既有钉零漂移）"
    assert select_primary_plan(fp, kl, context_ready=True) is fp, \
        "P5 失效：新鲜焦点栈没压过引擎的猜"


def test_bare_adjust_wins_via_huijian_only_before_p5():
    """裸值调节形（「暗一点」）**由"慧尖独占恒胜 klar"闸在 P5 之前就赢**——
    Adjust/SetDeviceMode ∈ HUIJIAN_ONLY_INTENTS（pipeline.py:70）。P5 不重复这道
    判据（夹具实锤：默认参已回 fp）；本钉锁次序，防后来人把两处判据搅在一起。"""
    fp = Plan(intent="AdjustDeviceAttribute",
              args={"attribute": "brightness", "delta": "-20"},
              source="t0", utterance="暗一点", trace=[])
    kl = Plan(intent="HassLightSet", args={"entity_id": KL_EID, "brightness": "30"},
              source="klar", utterance="暗一点")
    assert select_primary_plan(fp, kl) is fp
    assert select_primary_plan(fp, kl, context_ready=True) is fp


def test_stale_or_absent_focus_keeps_engine_route():
    fp, kl = _fp_pronoun(), _kl_named()
    # 上下文关着（默认参）/明确传 False：句法再像代词也不偏袒空栈
    assert select_primary_plan(fp, kl, context_ready=False) is kl


# ── ② 边界：明示目标句本闸不碰 ────────────────────────────────────
def test_explicit_target_anaphora_untouched():
    # 「再打开书房灯」剥离成具名短句后带明示目标——裁决走原次序，klar 恒优先照旧。
    # known_areas 传"书房"让 kl 计划吃到 v1.0.92 目标证据（区域名=合法证据），
    # 免得测试把**既有证据闸**的否决误当 P5 的行为。
    fp = Plan(intent="TurnDeviceOn",
              args={"target": [{"area": "书房", "devices": [{"name": "灯",
                                                            "domains": ["light"]}]}]},
              source="t0", utterance="再打开书房灯",
              trace=["回指→打开书房灯"])
    assert FLAG_ANAPHORA_STRIPPED in fp.flags, "夹具旗标没派生，测试前提不成立"
    kl = _kl_named("再打开书房灯")
    assert select_primary_plan(fp, kl, ("书房",), None, None,
                               context_ready=True) is kl, \
        "P5 越界：明示目标句被继承闸改写了仲裁"


def test_scene_and_bulk_precedence_survive():
    # scene 契约恒最高优先（本闸不得排到它前面）
    fp_scene = Plan(intent="HassTriggerVoiceScene",
                    args={"trigger_phrase": "观影"}, source="scene",
                    utterance="观影", trace=[])
    assert select_primary_plan(fp_scene, _kl_named(), context_ready=True) is fp_scene
    # 集合形批量（P0′）同样先于本闸
    from core.nlu.fast_path import FLAG_AREA_BULK
    fp_bulk = Plan(intent="TurnDeviceOff", args={}, source="t0",
                   utterance="关闭办公室所有设备", trace=["区域批量"])
    assert FLAG_AREA_BULK in fp_bulk.flags
    assert select_primary_plan(fp_bulk, _kl_named("关闭办公室所有设备"),
                               context_ready=True) is fp_bulk


# ── ③ 真接线两轮：线上病灶形状的离线复现 ──────────────────────────
class Lane:
    def __init__(self, table=None, single=None):
        self.table, self.single = table or {}, single

    async def match(self, text, origin=""):
        return self.table.get(text, self.single)


class RecExecutor:
    def __init__(self):
        self.plans = []

    async def run(self, plan):
        self.plans.append(plan)
        return True, "好的，办好了"

    async def run_raw(self, plan):
        return await self.run(plan)


class NullQuery:
    async def answer(self, text):
        return None


class HA:
    def __init__(self):
        self._states = {}
        self._entity_area = {}
        self._entity_alias = {}

    async def fire_event(self, name, data):
        return None


class PSettings:
    def get(self, k, default=None):
        d = {"dialog.dedup_window_s": 2.0, "dialog.context_enabled": True,
             "dialog.chain_enabled": True, "dialog.confirm_risky": True,
             "spatial.satellite_areas": {}, "llm.history_rounds": 10,
             "nlu.textcnn_enabled": False,
             "dialog.fallback_text": "我还不太确定这个指令"}
        return d.get(k, default)


def _pipe(fp, kl, ex):
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    p.ha = HA()
    p.executor = ex
    p.agent = None
    p.query = NullQuery()
    p.fast_path = fp
    p.klar = kl
    p.scenes = None
    from collections import OrderedDict
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._vocab_ts = time.time()
    return p


def _tgt(area="办公室", name="射灯"):
    return {"target": [{"area": area,
                        "devices": [{"name": name, "domains": ["light"]}]}]}


def _arun(coro):
    return asyncio.run(coro)


def test_two_turn_pronoun_inherits_focus_not_engine_guess():
    """两轮真接线（两路 Lane 同台竞标）：第二句「关掉它」必须继承第一句的射灯，
    且**绝不许**执行引擎猜的那台。这条钉在 P5 前的树上必红——正是离线套件没见过
    的形状（harness klar 恒关、单测仲裁面从无 kl×代词同台）。"""
    fp = Lane(table={
        "打开办公室射灯": Plan(intent="TurnDeviceOn", args=_tgt(), source="t0",
                               utterance="打开办公室射灯", trace=[]),
        "关掉它": _fp_pronoun(),
    })
    kl = Lane(table={"关掉它": _kl_named()})      # 引擎只猜得出"另一台"，第一句它没接
    ex = RecExecutor()
    p = _pipe(fp, kl, ex)

    r1 = _arun(p.handle("打开办公室射灯", origin="satA"))
    assert r1.ok and ex.plans[0].source == "t0", (r1.text, ex.plans)
    assert p._last_target.get("satA"), "第一句成功后焦点栈没记账"

    ex.plans.clear()
    r2 = _arun(p.handle("关掉它", origin="satA"))
    final = ex.plans[-1]
    assert final.source == "t0", \
        f"P5 失效：代词句仍被引擎抢走（source={final.source} args={final.args}）"
    assert final.args.get("target") == _tgt()["target"], final.args
    assert KL_EID not in str(final.args), "引擎猜的那台混进来了"
    assert any("继承目标" in t for t in final.trace), final.trace


def test_two_turn_without_focus_engine_route_kept():
    """焦点栈缺位（TTL 过期/从未具名轮）⇒ 逐字旧行为（klar 赢，不新造拒答）。
    P5 只把"有栈"的句子领回，不改"没栈"的裁决——既有 v1092 契约的形状保持。"""
    fp = Lane(table={"关掉它": _fp_pronoun()})
    kl = Lane(table={"关掉它": _kl_named()})
    ex = RecExecutor()
    p = _pipe(fp, kl, ex)
    r = _arun(p.handle("关掉它", origin="satB"))
    assert r.ok, r.text
    assert ex.plans[-1].source == "klar", \
        f"无焦点栈时仲裁被改动：{ex.plans[-1].source}"


def test_stale_focus_does_not_trigger_p5():
    """栈在但**过期**（>TTL=90s）⇒ _focus_ready False ⇒ 旧行为。"""
    fp = Lane(table={"关掉它": _fp_pronoun()})
    kl = Lane(table={"关掉它": _kl_named()})
    ex = RecExecutor()
    p = _pipe(fp, kl, ex)
    p._last_target["satC"] = {"kind": "target", "target": _tgt()["target"],
                              "ts": time.time() - 91.0}
    _arun(p.handle("关掉它", origin="satC"))
    assert ex.plans[-1].source == "klar", \
        f"过期焦点仍触发继承：{ex.plans[-1].trace}"


def test_context_disabled_kill_switch_honored():
    """dialog.context_enabled=False 必须连 P5 一起关死（配置开关说到做到）。"""
    class OffSettings(PSettings):
        def get(self, k, default=None):
            if k == "dialog.context_enabled":
                return False
            return super().get(k, default)

    from core.pipeline import Pipeline
    fp = Lane(table={
        "打开办公室射灯": Plan(intent="TurnDeviceOn", args=_tgt(), source="t0",
                               utterance="打开办公室射灯", trace=[]),
        "关掉它": _fp_pronoun(),
    })
    kl = Lane(table={"关掉它": _kl_named()})
    ex = RecExecutor()
    p = Pipeline.__new__(Pipeline)
    p.settings = OffSettings()
    p.ha = HA()
    p.executor = ex
    p.agent = None
    p.query = NullQuery()
    p.fast_path = fp
    p.klar = kl
    p.scenes = None
    from collections import OrderedDict
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._vocab_ts = time.time()
    _arun(p.handle("打开办公室射灯", origin="satD"))
    ex.plans.clear()
    _arun(p.handle("关掉它", origin="satD"))
    assert ex.plans[-1].source == "klar", \
        "context_enabled=False 没关掉 P5（继承链没被配置开关连坐）"
