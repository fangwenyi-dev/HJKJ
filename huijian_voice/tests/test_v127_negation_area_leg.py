# -*- coding: utf-8 -*-
"""2026-10-08 P0：否定闸的**房间名盲区**——双向判 + 等价臂 + 真入口端到端。

病灶（本机全链探针实得，非推断）：`_NEGATION_CMD` 的尾闸
`(?![^，。！？,、]{0,2}的)` 是 v1.1.27-r2 为救「没关紧的窗关上」那 6 例补语形，
从"动词后紧邻的"放宽到"0~2 字内的"。代价是**两字房间名恰好全落在窗口里**：

    别开客厅的灯      → 旧判据 False → klar 计划照常执行 → 播「好的，办好了」
    不要开卧室的空调  → 同上
    不要关阳台的灯    → 同上
    别开灯 / 别开台灯 → True（正确弃执行）

用户说"别开"，设备真开——这是本项目最贵的一类（对照 v1.1.27「别开台灯」真机案、
v1.0.90 假成功风暴）。收口=动词后那段是不是**本家真注册的区域名**
（`pipeline._real_areas()`），与"位置词豁免只认表"（v1.1.36 复核⑥）同一条纪律；
补语（紧/好/严/完）不是房间名 ⇒ 仍走定语小句豁免。**表为空 ⇒ 本臂整条不启用**，
现网逐值不变。

既有钉为什么放走了它：`test_v1127*` / `test_v1136*` 只收无「的」的形
（别关灯 / 别开灯 / 别开台灯）——单向不变量没配反向。本文件把两侧一起钉，
并给"摘掉接线"留一条元钉（新判据写在别处时会静默空跑报绿）。
"""
import ast
import asyncio
import os
import sys
import time
from collections import OrderedDict

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import (                        # noqa: E402
    FastPath, Plan, _NEGATION_CMD, _NEG_PREP_FLOW,
    is_bare_negation_imperative, is_negation_imperative)
from core.pipeline import Pipeline                      # noqa: E402
from test_experience_batch import (                     # noqa: E402
    HA, Lane, NullQuery, PSettings, RecExecutor)

AREAS = ("客厅", "卧室", "阳台")

# 正说对的日常句：带房间名的否定祈使，一律必须被认出来
NEGED = ("别开客厅的灯", "不要开卧室的空调", "不要关阳台的灯",
         "别开客厅的灯呀", "莫开卧室的灯")
# 反例：动词后是**补语**而非房间名——这是 v1.1.27-r2 当初放宽的理由，
# 新臂一旦把它们吃成否定祈使，就是"真命令落 MISS"的回归
# （「把没关严窗户拉上」不在此列：它被**字面表** :1302 的非 bare 判据整句拒，
#   是同族另一处既存病灶，与本轮的尾闸盲区不同落点，另案处理）
COMPLEMENT = ("没关紧的窗关上", "没关好的灯关掉", "没关严的窗关上",
              "没开完的窗继续开")
# 同族另一形（2026-10-08 一起实锤）：**设备名里嵌着单字动词**（空「调」/
# 落地「扇」），旧形把剥段后的剩余判成"还有肯定动作"⇒ 整条不否决。
# 与房间名无关，不带表也必须是红的——这条钉的是既存的第二个洞。
NOUN_EMBEDDED = ("别开空调", "不要开空调", "别开落地扇", "莫开新风机")


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


HOME = {"light.lvs": {"attributes": {"friendly_name": "客厅灯"}},
        "light.wos": {"attributes": {"friendly_name": "卧室灯"}},
        "light.yl": {"attributes": {"friendly_name": "阳台灯"}},
        "cover.yl": {"attributes": {"friendly_name": "阳台窗"}}}

# 本文件要 sync_vocab/sync_areas 才让判据吃到"本家有这间房/这台设备"——那是
# **进程级全局态**，不还原就会污染同一次全量跑里的别的用例（本仓记过的同型坑）。
_VOCAB_GLOBALS = ("_dyn_vocab", "ALL_DEVICES", "ALL_SET", "_ALL_MIN2",
                  "_dyn_domains", "_dyn_lookup", "_dyn_areas")


@pytest.fixture(autouse=True)
def _restore_targets_vocab():
    saved = {k: getattr(T, k, None) for k in _VOCAB_GLOBALS}
    yield
    for k, v in saved.items():
        if hasattr(T, k):
            setattr(T, k, v)


def _pipe(ha, kl=None, fp=None, ex=None, settings=None):
    """手工装配（与 `_pipe` 同形），但区域表按**生产口径**接到 FastPath 上。"""
    p = Pipeline.__new__(Pipeline)
    p.settings = settings or PSettings()
    p.ha = ha
    p.executor = ex or RecExecutor()
    p.agent = None
    p.query = NullQuery()
    p.fast_path = fp or FastPath(_NoScenes(), None, p.settings,
                                 areas_of=p._real_areas)
    p.klar = kl or Lane({}, single=None)
    p.scenes = None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._vocab_ts = time.time()          # 抑制动态词表同步（单测独立性）
    return p


def _ha():
    ha = HA(HOME)
    ha._areas = {"a1": "客厅", "a2": "卧室", "a3": "阳台"}
    return ha


# ── ① 正向：带房间名的否定祈使必须被认出来 ────────────────────────────
def test_area_named_negation_is_imperative():
    for t in NEGED:
        assert is_negation_imperative(t, AREAS), f"{t} 仍不被认成否定祈使"
        assert is_bare_negation_imperative(t, AREAS), f"{t} 整句判据没跟上"


# ── ② 反向：补语形不许被新臂吃成否定（当初放宽的理由必须原样保住）──────
def test_complement_tail_stays_exempt():
    for t in COMPLEMENT:
        assert not is_negation_imperative(t, AREAS), f"{t} 被误判为否定祈使"
        assert not is_bare_negation_imperative(t, AREAS), f"{t} 整句判据被吃"


# ── ②b 同族第二洞：设备名里嵌动作字（空调的「调」）不许被当肯定动作 ──────
def test_verb_embedded_device_noun_is_still_a_refusal():
    """「别开空调」在旧形里连**不带表**都不否决——用户说别开，空调开了。"""
    for t in NOUN_EMBEDDED:
        assert is_negation_imperative(t), f"{t} 连否定祈使都没认出"
        assert is_bare_negation_imperative(t), f"{t} 仍被放行执行"
    # 反向（v1.1.37 红线）：半句否定不许打死整句——剩余段有真动词时仍不否决
    assert not is_bare_negation_imperative("关空调，别开空调")
    assert not is_bare_negation_imperative("别开空调，把电视打开")
    assert not is_bare_negation_imperative("不用开灯，把窗帘拉上就行")


# ── ③ 等价臂：不传表 ⇒ 逐值回落到旧判据（现网零漂移的证据，不是注释）────
def test_no_table_is_byte_identical_to_old_judge():
    corpus = list(NEGED) + list(COMPLEMENT) + [
        "别开灯", "不要关灯", "别开台灯", "把灯打开", "开一下客厅的灯",
        "关掉阳台的窗", "不要拉窗帘到底", "窗帘不要拉到底"]
    for t in corpus:
        old = bool(_NEGATION_CMD.search(t) or _NEG_PREP_FLOW.search(t))
        assert is_negation_imperative(t) is old, f"默认参数漂移：{t}"
        assert is_negation_imperative(t, ()) is old, f"空表漂移：{t}"
        assert is_negation_imperative(t, None) is old, f"None 表漂移：{t}"


# ── ④ 字面表侧：fp 自己也要吃区域表（只修裁决面=半道闸）────────────────
def test_fast_path_self_veto_with_area_table():
    fp = FastPath(_NoScenes(), None, PSettings(), areas_of=lambda: AREAS)
    for t in NEGED:
        assert asyncio.run(fp.match(t)) is None, f"{t} 字面表仍出计划"
    # 对照臂：同一句、同一表，摘掉 areas_of ⇒ 旧行为（出计划或不接管由别的分支定）
    fp0 = FastPath(_NoScenes(), None, PSettings())
    assert fp0._areas() == (), "默认必须无表"
    # 取表抛错不得把字面表弄崩（观测面故障不殃及判据）
    boom = FastPath(_NoScenes(), None, PSettings(), areas_of=lambda: (_ for _ in ()).throw(RuntimeError()))
    assert boom._areas() == ()


# ── ⑤ 端到端：真入口 + klar 接地计划 ⇒ 一步都不许下发 ──────────────────
def test_end_to_end_area_negation_executes_nothing():
    T.sync_vocab(HOME)
    T.sync_areas(list(AREAS))
    kl_plan = Plan(intent="HassTurnOn",
                   args={"target": [{"area": "客厅", "devices": [{"name": "灯"}]}]},
                   source="klar", utterance="别开客厅的灯")
    ex = RecExecutor()
    p = _pipe(_ha(), kl=Lane({"别开客厅的灯": kl_plan}), ex=ex)
    reply = asyncio.run(p.handle("别开客厅的灯", origin="o"))
    assert ex.plans == [], f"说别开却下发了：{ex.plans}"
    assert "办好了" not in reply.text, f"拒执行却播成功：{reply.text!r}"


def test_end_to_end_ac_negation_executes_nothing():
    """端到端坐实同族第二洞：「别开空调」经 klar 计划也不许下发一步。"""
    T.sync_vocab(HOME)
    T.sync_areas(list(AREAS))
    kl_plan = Plan(intent="HassTurnOn",
                   args={"entity_id": "climate.kt"},
                   source="klar", utterance="别开空调")
    ex = RecExecutor()
    p = _pipe(_ha(), kl=Lane({"别开空调": kl_plan}), ex=ex)
    reply = asyncio.run(p.handle("别开空调", origin="o"))
    assert ex.plans == [], f"说别开空调却下发了：{ex.plans}"
    assert "办好了" not in reply.text, f"拒执行却播成功：{reply.text!r}"


def test_end_to_end_plain_negation_still_refused():
    """对照臂：无房间名的「别开灯」本来就拦得住——新臂不得把它放开。"""
    T.sync_vocab(HOME)
    T.sync_areas(list(AREAS))
    kl_plan = Plan(intent="HassTurnOn",
                   args={"target": [{"area": "客厅", "devices": [{"name": "灯"}]}]},
                   source="klar", utterance="别开灯")
    ex = RecExecutor()
    p = _pipe(_ha(), kl=Lane({"别开灯": kl_plan}), ex=ex)
    asyncio.run(p.handle("别开灯", origin="o"))
    assert ex.plans == [], f"旧支被放开：{ex.plans}"


# ── ⑥ 半句否定不许打死整句（v1.1.37 红线在新臂下仍然成立）──────────────
def test_chain_other_leg_still_executes():
    T.sync_vocab(HOME)
    T.sync_areas(list(AREAS))
    on = Plan(intent="HassTurnOn",
              args={"target": [{"area": "客厅", "devices": [{"name": "灯"}]}]},
              source="klar", utterance="打开客厅的灯")
    refused = Plan(intent="HassTurnOn",
                   args={"target": [{"area": "卧室", "devices": [{"name": "灯"}]}]},
                   source="klar", utterance="别开卧室的灯")
    ex = RecExecutor()
    p = _pipe(_ha(), kl=Lane({"打开客厅的灯": on, "别开卧室的灯": refused},
                             single=None), ex=ex)
    asyncio.run(p.handle("打开客厅的灯，别开卧室的灯", origin="o"))
    acted = [(pl.intent, pl.args) for pl in ex.plans]
    assert len(acted) == 1, f"整链被拒腿打死或两腿都动：{acted}"
    assert acted[0][1]["target"][0]["area"] == "客厅", acted


# ── ⑦ 接线元钉：判据有了表，还得有人把表递到它嘴边 ─────────────────────
def test_pipeline_wires_area_table_into_every_call_site():
    """行为三关只覆盖得了走到的分支；这里按源码扫全部调用点，防"新判据写了没人传"。

    豁免/裁决两侧都必须带表（豁免面不带表 ⇒ 房间名否定腿会被查无闸当"点了家里
    没这台"答错话）。允许跨行续写：判据是"取表点到目标行括号深度不转负"，
    不是数逗号——本钉用 AST，直接读每个调用位置的实参个数。
    """
    src = open(os.path.join(ROOT, "core", "pipeline.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    judged = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("is_negation_imperative",
                                     "is_bare_negation_imperative"):
            judged.append((node.lineno, len(node.args),
                           [k.arg for k in node.keywords]))
    assert judged, "pipeline 里已经没有任何否定判据调用点=接线整条没了"
    for lineno, npos, names in judged:
        assert npos >= 2 or "areas" in names, \
            f"core/pipeline.py:{lineno} 递表没递到（第 2 参或 areas= 至少一个）"
    # FastPath 装配必须带 areas_of，否则字面表侧那道闸永远吃不到表
    wired = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "FastPath"
             and any(k.arg == "areas_of" for k in n.keywords)]
    assert wired, "Pipeline 装配 FastPath 时没传 areas_of=（字面表侧新臂成死码）"
