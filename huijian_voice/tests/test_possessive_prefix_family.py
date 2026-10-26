# -*- coding: utf-8 -*-
"""属格范围前缀族（我们家/我家/咱家/家里…）的两处落刀钉。

现网活体形状（2026-10-20 首抓于 vo-1.6.9；2026-10-26 在出货字节 vo-1.7.5 上端到端复现）：
  「打开我们家的灯」      → 谎报「没有找到对应的设备『家的灯』」
  「我家客厅的灯开一下」  → 谎报「没有找到对应的设备『我家的灯』」
  「我家客厅的灯打开」    → 区域槽整段吞下「我家客厅」⇒「『我家客厅』这个房间在 HA 里不存在」
  「咱家卧室关灯」（无「的」）→ 同形，走的是前缀回捞那条道，不查区域表
根因两条：①区域侧的尾字启发式（厅/室/房…）不查表就认区域；②点名侧把"属格头＋裸类别词"
的残段当成点名的设备。两处闸本身做得对（没点名的设备不许动），**话说的原因是假的**。

形制按本仓量具课：**调生产真函数**、正反两向都钉，且三条析出道各自单独钉——
只钉其中一条时，摘掉另两支的变异臂会"全绿"＝测空气（2026-10-20 实发记过）。
红线一条：属格后跟的**不是裸类别词**时闸不许弃权（v1.1.35「没点名的设备任何方向都不动」，
放宽即丢修饰语顶同类别那台并回显成功）。
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.nlu import targets as T                        # noqa: E402
from core.pipeline import (Plan, _phrase_is_not_a_name,   # noqa: E402
                           _plan_named_absent_target)

# 现网真形状：客厅/办公室两台同名「射灯」，区域表 客厅/办公室/…（area_shape_probe 读数）
NOUNS = frozenset({"灯", "射灯", "台灯", "筒灯", "灯带"})
DEVICE_NAMES = ("射灯", "台灯", "客厅 All Lights", "办公室空调")
KNOWN_AREAS = ("客厅", "办公室", "会议室", "卧室", "厨房", "展厅", "开发")
REAL_AREAS = KNOWN_AREAS


def _t0(utterance, name):
    """t0 实得产物形状（name 为解析出的目标词，无 entity_id）。"""
    return Plan("TurnDeviceOn",
                {"target": [{"devices": [{"name": name, "domains": ["light"]}]}]},
                source="t0", utterance=utterance)


# ══ 落点①：点名侧——闸的弃权面认属格范围修饰语 ══════════════════════
def test_possessive_modifier_segment_abstains():
    """「(我们)家(的) + 裸类别词」是**范围修饰语**，不是点名的一台设备 ⇒ 闸弃权。"""
    for seg in ("家的灯", "我家的灯", "我们家的灯", "我家灯", "家里的灯", "我家的"):
        assert _phrase_is_not_a_name(seg, NOUNS) is True, f"属格修饰段仍被当设备名：{seg}"


def test_possessive_exemption_does_not_widen_the_safety_gate():
    """反向（红线）：属格后面跟的**不是裸类别词**时闸照旧拦——
    「我家的会飞的灯」若被弃权，引擎会丢掉修饰语顶同类别那台并回显成功
    （v1.1.35 立闸的 .91 实锤案）。"""
    for seg in ("我家的会飞的灯", "我们家的不亮的灯", "家里的阳台灯"):
        assert _phrase_is_not_a_name(seg, NOUNS) is False, f"真点名被属格豁免放过了：{seg}"


def test_named_absent_gate_stops_refusing_house_possessive():
    """端到端：现网那两句不得再以属格残段按名查无。"""
    assert not _plan_named_absent_target(_t0("打开我们家的灯", "灯"),
                                         DEVICE_NAMES, KNOWN_AREAS, REAL_AREAS)
    assert not _plan_named_absent_target(_t0("我家客厅的灯开一下", "灯"),
                                         DEVICE_NAMES, KNOWN_AREAS, REAL_AREAS)


def test_named_absent_gate_still_refuses_a_real_missing_name():
    """同形反向：真·点了没这台，必须仍然拒——本刀不许把闸整体放宽。"""
    assert _plan_named_absent_target(_t0("打开会飞的灯", "会飞的灯"),
                                     DEVICE_NAMES, KNOWN_AREAS, REAL_AREAS)


# ══ 落点②：区域侧——三条析出道各自钉死 ════════════════════════════
def test_area_of_prefix_strips_possessive_head_tailchar_branch():
    """道一：`_area_like` 尾字启发式命中（厅/室/房/…）这一支要先剥属格头。"""
    assert T._area_of_prefix("我家客厅") == "客厅"
    assert T._area_of_prefix("我们家办公室") == "办公室"
    assert T._area_of_prefix("咱家卧室") == "卧室"
    assert T._area_of_prefix("我家客厅的") == "客厅"


def test_area_of_prefix_strips_head_on_prefix_rescue_branch():
    """道二：**不带「的」**那句走的是 `extract_prefix` 前缀回捞（该函数只看前缀尾字
    ∈室厅房间…，从不查区域表）。只钉道一时，摘掉这一支的变异臂仍全绿。"""
    assert T._area_of_prefix("咱家卧室关") == "卧室"


def test_area_of_prefix_strips_head_for_area_without_region_suffix():
    """道三：区名不带区域尾字（阳台/主卧/次卧/玄关），尾字启发式与前缀回捞都够不着，
    只能剥头后**问表**；表里没有就照旧不回（宁缺不猜房间）。"""
    orig = T._dyn_areas
    try:
        T._dyn_areas = ()                   # 冷启动：sync_areas 还没跑
        assert T._area_of_prefix("家里阳台") == "阳台"
        assert T._area_of_prefix("我家主卧的") == "主卧"
        assert T._area_of_prefix("我们家次卧") == "次卧"
    finally:
        T._dyn_areas = orig
    assert T._dyn_areas == orig, "全局区域表没还原"


def test_area_of_prefix_does_not_eat_jia_inside_a_name():
    """反向：「家」在词里（智能家居）或整段就是「家」时不许剥——
    剥了会把设备名前缀误析成残段、把「家门口」吞成「门口」。"""
    assert not T._area_of_prefix("智能家居")
    assert not T._area_of_prefix("家")
    assert not T._area_of_prefix("家门口")


def test_exact_registry_area_named_possessively_keeps_priority():
    """门槛的另一半写成声明：**区域表里真有这个名字**才享现状优先。
    把剥属格无条件放在前面，装了「我家客厅」这个区域的实例就永远匹配不上自己的房间。
    夹具改模块级 `_dyn_areas`（sync_areas 的落点），跑完必须逐值还原。"""
    orig = T._dyn_areas
    try:
        T._dyn_areas = ("我家客厅",)
        assert T._area_of_prefix("我家客厅的") == "我家客厅"
    finally:
        T._dyn_areas = orig
    assert T._dyn_areas == orig, "全局区域表没还原"


def test_depossessive_area_keeps_a_non_area_remainder():
    """剥完不像区域就原样交回，不造臆造区名（「我家电器」不得回「电器」）。"""
    assert T._depossessive_area("我家电器") == "我家电器"


def test_depossessive_head_is_head_anchored_only():
    """"家"落在**词中**时不许剥——锚定不是装饰：未锚定时「宜家客厅」会被剥成
    「宜客厅」（尾巴仍是区域尾字 ⇒ 一路通过），等于凭空造出一个不存在的房间。
    变异自证：去掉 `_POSSESSIVE_HEAD_RE` 的 `^` ⇒ 本条必须红
    （上一版只钉「智能家居」，而它剥完不像区域、走不到那一步＝没牙的钉）。"""
    assert T._depossessive_area("宜家客厅") == "宜家客厅"
    assert T._depossessive_area("智能家居") == "智能家居"


# ══ parse_target 端到端族（现网话术形）════════════════════════════
def test_parse_target_family_end_to_end():
    for utterance, want_area, want_name in (
        ("我家客厅的灯打开", "客厅", "灯"),
        ("我家客厅灯打开", "客厅", "灯"),
        ("我家书房窗帘拉上", "书房", "窗帘"),
        ("咱家卧室关灯", "卧室", "灯"),
        ("我们家客厅的空调调到26度", "客厅", "空调"),
        ("家里阳台的窗帘拉上", "阳台", "窗帘"),
        # 阳性对照：无属格的既有行为一字不变（v1.7.4 那刀的现场也在内）
        ("客厅的灯打开", "客厅", "灯"),
        ("厨房的灯关掉", "厨房", "灯"),
        ("办公室射灯亮度调到百分之十", "办公室", "射灯"),
    ):
        area, name, _score = T.parse_target(utterance)
        assert area == want_area, f"{utterance!r} 区域槽={area!r}，应为 {want_area!r}"
        assert name == want_name, f"{utterance!r} 目标名={name!r}，应为 {want_name!r}"


def test_parse_target_does_not_invent_an_area_from_a_word_containing_jia():
    """反向（同族的另一头）：「智能家居的灯打开」里"家"是词中字，不许析出任何区域。"""
    area, name, _score = T.parse_target("智能家居的灯打开")
    assert area is None
    assert name == "灯"


# ══ 落点③：入口归一（对抗复核实发的两处同类漏网，2026-10-26）══════════
# 复核口径：只钉 parse_target/_area_of_prefix 这些**中间函数**不够——客户听到的是
# FastPath 与 QueryZone 的产物。同形 A/B（当前树 vs HEAD 工作树）实测：
#   「家里阳台的窗帘拉上」出货字节走 `全屋显式:阳台窗帘→domains=['cover']`，
#     area 被静默丢弃 ⇒ **全家窗帘一起拉**＝跨区误动（v1.0.41 S12 只挡了区域在标记
#     之前的语序，"标记在区域之前"这半一直没挡）。
#   「我们家办公室的灯开着吗」查询族在注册表未命中时，启发式 `{1,4}?` 会把
#     「我们家办公室」削成「们家办公室」——连"我"都丢，抽出一个谁也没说过的区域名。
import pytest                                                     # noqa: E402
from core.nlu import query as QModule                            # noqa: E402
from core.nlu.fast_path import FastPath                           # noqa: E402
from core.nlu.query import QueryZone                              # noqa: E402
from core.nlu.textcnn import TextCNN                              # noqa: E402
from golden_gen import FakeScenes, FakeHa, S                      # noqa: E402


@pytest.fixture(scope="module")
def fp_engine():
    tc = TextCNN(Path(os.environ["HUIJIAN_NLU_DATA"]))
    tc._ensure()
    orig = T._dyn_areas
    try:
        T.sync_areas(("客厅", "卧室", "书房", "办公室", "厨房", "阳台"))
        yield FastPath(FakeScenes(), tc, S())
    finally:
        T._dyn_areas = orig


def _target_of(plan):
    return (plan.args.get("target") or [{}])[0]


def test_fast_path_entry_strips_head_only_when_area_is_registered(fp_engine):
    """入口归一的**两面**：在册区域 ⇒ 剥头并按区域处理；不是区域 ⇒ 一律不剥。"""
    p = asyncio.run(fp_engine.match("家里阳台的窗帘拉上"))
    t = _target_of(p)
    assert t.get("area") == "阳台", f"「家里阳台的窗帘」应落 area=阳台，实得 {t}"
    assert (t.get("devices") or [{}])[0].get("name") == "窗帘", t
    assert getattr(p, "whole_house", False) is False, \
        "「家里阳台的窗帘拉上」绝不许走 whole_house——那会拉全家窗帘（本钉守的就是这一格）"

    # 反向：「家里的灯」里"家里"就是范围本身，后面没有房间名 ⇒ 必须仍按全屋走
    p2 = asyncio.run(fp_engine.match("家里的灯打开"))
    t2 = _target_of(p2)
    assert "area" not in t2, f"「家里的灯」不该有区域，实得 {t2}"
    assert getattr(p2, "whole_house", False) is True, "「家里的灯打开」的全屋语义不许被剥头弄丢"

    # 反向：「智能家居」的"家"在词中，入口那道锚定正则不该碰
    p3 = asyncio.run(fp_engine.match("智能家居的灯打开"))
    assert "智能家居" not in str(_target_of(p3).get("area") or ""), _target_of(p3)


def test_query_area_extraction_strips_head_before_matching():
    """查询族（`QueryZone._find_area`）单点前置剥头——**注册表没到**那一支才是真形。
    夹具刻意用空 `_areas`：这正是冷启动窗口里启发式独自决定区域的形状（生产可达）。"""
    qz = QModule.QueryZone(FakeHa(), S())
    qz.ha = SimpleNamespace(_areas={})           # 空表 ⇒ 走启发式那一支（已在册的支路另有钉）
    assert qz._find_area("我家客厅的灯开着吗") == "客厅"
    assert qz._find_area("我们家办公室的灯开着吗") == "办公室", \
        "旧形在这里会返回「们家办公室」（{1,4}? 窗口把'我'削掉）＝谁也没说过的区域名"
    assert qz._find_area("家里的灯开着吗") in (None, ""), "「家里的灯」是问全屋，不许编出区域"
    assert qz._find_area("客厅的灯开着吗") == "客厅", "阳性对照：无属格时行为一字不变"
