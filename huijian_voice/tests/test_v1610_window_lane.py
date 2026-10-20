# -*- coding: utf-8 -*-
"""v1.6.10 窗道批次（刀P0-1/P1-1/P1-2/P1-3）判据面守卫钉。

钉面=行为（import 真实现直调 FastPath），每条都配反向臂——本批改的是**动词与
目标的取法**，最坏的失效形态不是"听不懂"，而是"说 A 动 B"与"要关却开"。

四刀的现场实锤（离线复现，基线树 v1.6.9 逐字复算）：
  刀P0-1 状态定语被当命令动词（反极性下发，P0）：
      把开着的灯关掉      → TurnDeviceOn(灯)      用户要关、灯真开
      把开着的窗关上      → ControlWindow open     同上（窗）
      把客厅开着的窗关上  → TurnDeviceOn 设备名=客厅
      窗户开着呢关上      → ControlWindow open
  刀P1-1 动词头表「关上」三缺一 → 关上没关紧的窗 残出「上窗」
  刀P1-2 「窗子」在语序归一表内、不在动作表 → 把窗子关上 残成单字「子」→ 整句落空
  刀P1-3 裸窗句产**无 target** 的 ControlWindow，集成 2026-10-08 立的"如实失败"
          ⇒ 这个产品的头号动词（关窗/开窗/把窗关上）听到的是失败
"""
import asyncio
import copy
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1610wl_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import targets as T                      # noqa: E402
from core.nlu.fast_path import (                        # noqa: E402
    FastPath, _BARE_DEV_HEADS, _ACTION_PATTERNS, _strip_heads,
    _split_state_attributive)
from core.nlu.textcnn import TextCNN                   # noqa: E402
from core.settings import DEFAULTS                     # noqa: E402

AREAS = ("办公室", "展厅", "客厅", "主卧", "阳台")


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class _Scenes:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    async def verify_or_refresh(self, phrase):
        return True


_TC = None


@pytest.fixture(scope="module")
def tc():
    global _TC
    if _TC is None:
        _TC = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
        _TC._ensure()
    return _TC


@pytest.fixture()
def fp(tc):
    T.sync_areas(list(AREAS))
    return FastPath(_Scenes(), tc, _S(), areas_of=lambda: AREAS)


def _m(fp, text):
    return asyncio.run(fp.match(text))


def _names(plan):
    out = []
    for entry in (plan.args or {}).get("target") or []:
        for dev in (entry or {}).get("devices") or []:
            out.append(str(dev.get("name") or ""))
    return out


# ── 刀P0-1：状态定语不得供出动词极性 ──────────────────────────────
POLARITY = [
    ("把开着的灯关掉", "TurnDeviceOff", None),
    ("把开着的窗关上", "ControlWindow", "close"),
    ("把开着的窗户关上", "ControlWindow", "close"),
    ("把客厅开着的窗关上", "ControlWindow", "close"),
    ("窗户开着呢关上", "ControlWindow", "close"),
    ("把没关紧的窗关上", "ControlWindow", "close"),
    ("把没开完的窗打开", "ControlWindow", "open"),
    ("关上没关紧的窗", "ControlWindow", "close"),
    ("把没关严窗户拉上", "ControlWindow", "close"),
]


@pytest.mark.parametrize("text,intent,action", POLARITY)
def test_state_attributive_does_not_supply_verb(fp, text, intent, action):
    """状态定语（开着/没关紧）是设备"现在什么样"，不是要做的动作。"""
    p = _m(fp, text)
    assert p is not None, f"{text!r} 落空（本刀要治的正是这句）"
    assert p.intent == intent, f"{text!r} → {p.intent}（错意图）"
    if action:
        got = str((p.args or {}).get("action") or "")
        assert got == action, f"{text!r} action={got!r}≠{action!r}——反极性下发"


@pytest.mark.parametrize("text,area", [("把客厅开着的窗关上", "客厅")])
def test_state_strip_leaves_no_residue_in_area(fp, text, area):
    """剥状态词后区域必须还是那间房——「窗的客厅」这类残渣区域=查无+谎报温床。"""
    p = _m(fp, text)
    assert p is not None
    areas = [str((e or {}).get("area") or "") for e in (p.args or {}).get("target") or []]
    assert area in areas, f"{text!r} area={areas!r}≠{area!r}"
    assert not any("着" in a or "窗的" in a for a in areas), f"区域被写坏:{areas!r}"


@pytest.mark.parametrize("text", [
    "客厅的灯开着呢？",      # 状态疑问：问一句绝不动设备（v1.2.11 A2 红线）
    "别开灯",               # 否定祈使：拒执行（v1.1.27 红线）
    "把没关紧的窗关上呢？",   # 疑问尾同样不许被本刀改写成命令
])
def test_safety_guards_still_refuse(fp, text):
    """反向臂：本刀只改动词取法，三道安全闸吃的仍是原话。"""
    assert _m(fp, text) is None, f"{text!r} 被本刀改成了命令（危险）"


@pytest.mark.parametrize("text,want", [
    ("开着的灯关掉", "灯关掉"),        # 生产码路喂进来的是剥把字后的形
    ("把开着的灯关掉", "把灯关掉"),     # 直呼本函数时的形
    ("灯开着", None),                  # 无尾动词⇒不改写（v1.2.11 A2 弃权面）
    ("开着窗帘", None),                # 同上：只剩词根不裁决
])
def test_split_state_attributive_shape(text, want):
    """单点判据自证：剥什么、什么不许剥，都得能被指着看。"""
    assert _split_state_attributive(text) == want


# ── 刀P1-1/P1-2：动词头与名词表的"三缺一" ─────────────────────────
def test_close_head_contains_guanshang():
    """`关上` 必须在关闭动词头表里（A1 注释自陈它是尾动词，表里却没收=漂移）。"""
    srcs = [p.pattern for p, intent, _v in _ACTION_PATTERNS
            if intent == "TurnDeviceOff"]
    assert any("关上" in s for s in srcs), "关闭动词头表缺「关上」"


def test_window_action_heads_cover_zi_and_hu():
    """语序归一表认「窗子」，动作表就必须也认——两张表名词覆盖面必须齐。"""
    srcs = [p.pattern for p, intent, _v in _ACTION_PATTERNS if intent == "ControlWindow"]
    joined = "".join(srcs)
    for w in ("窗户", "窗子"):
        assert w in joined, f"窗族动作头表缺「{w}」"


@pytest.mark.parametrize("text", ["把窗子关上", "打开窗子", "关窗子"])
def test_window_zi_forms_not_stranded(fp, text):
    p = _m(fp, text)
    assert p is not None, f"{text!r} 落空"
    assert p.intent == "ControlWindow", f"{text!r} → {p.intent}"
    assert not [n for n in _names(p) if n in ("子", "户", "")], f"残出单字名:{_names(p)}"
    act = str((p.args or {}).get("action") or "")
    assert act == ("open" if "打开" in text else "close"), f"{text!r} 方向错:{act}"


# ── 刀P1-3：裸窗句必须带目标（集成对空 target 是"如实失败"）────────
BARE_WINDOW = [
    ("把窗关上", "close"), ("把窗打开", "open"), ("关窗", "close"),
    ("开窗", "open"), ("窗户关上", "close"), ("关上窗户", "close"),
    ("关闭窗", "close"), ("窗关一下", "close"),
]


@pytest.mark.parametrize("text,action", BARE_WINDOW)
def test_bare_window_sentence_carries_target(fp, text, action):
    p = _m(fp, text)
    assert p is not None, f"{text!r} 落空——用户口吻=没听懂"
    assert p.intent == "ControlWindow", f"{text!r} → {p.intent}"
    assert (p.args or {}).get("target"), f"{text!r} 无 target⇒集成拒收(本刀病灶)"
    assert str(p.args.get("action")) == action, f"{text!r} 方向错"
    assert "帘" not in "".join(_names(p)), f"{text!r} 误吃帘族:{_names(p)}"


def test_bare_window_head_is_a_strip_candidate():
    """机制钉：裸「窗」进 ②③ 剥离头，缺它「窗关上」三张表都够不到。"""
    assert "窗" in _BARE_DEV_HEADS
    assert "窗" in _strip_heads()


# ── 反向：不得把帘族/窗器/参数道从旁边踩过去 ──────────────────────
@pytest.mark.parametrize("text,intent", [
    ("阳台窗帘调成50%", "AdjustDeviceAttribute"),   # 裸窗头不许劈开「窗帘」
    ("窗帘拉上", "TurnDeviceOff"),
    ("打开窗帘开合器", "TurnDeviceOn"),              # 开合器不进窗道
    ("开窗器速度设为80", "ControlWindow"),           # 窗器参数道
    ("关闭办公室平开窗", "ControlWindow"),           # 具名窗型仍走具名
])
def test_neighbour_lanes_untouched(fp, text, intent):
    p = _m(fp, text)
    assert p is not None, f"{text!r} 被本批弄落空"
    assert p.intent == intent, f"{text!r} → {p.intent}≠{intent}"
    if text == "阳台窗帘调成50%":
        assert "窗帘" in "".join(_names(p)), f"帘被劈开:{_names(p)}"
        assert str(p.args.get("position")) == "50" or p.args.get("delta"), p.args
    if text == "开窗器速度设为80":
        assert p.args.get("speed") == 80, p.args


def test_no_residue_device_names_across_batch(fp):
    """总反向钉：本批修的是"残渣进目标名"，任何一句都不许再出现这些形。"""
    BAD = re.compile(r"^(着的|开着的|没关紧的|没关严|上|子|个|扇)")
    corpus = ([t for t, _i, _a in POLARITY] + [t for t, _a in BARE_WINDOW]
              + ["把窗子关上"])
    for text in corpus:
        p = _m(fp, text)
        for n in _names(p):
            assert not BAD.match(n), f"{text!r} 目标名残出 {n!r}"


# ── 刀P0-4：方位短语的中心语（说 A 动 B 的 P0 族）──────────────────
# 基线（v1.6.9 树逐字复算，_myaudit/ab_out.txt 留痕）：
#   把窗户旁边的灯打开   → ControlWindow open(窗户)   灯没动、窗真开
#   把窗帘旁边的灯打开   → TurnDeviceOn(窗帘 cover)   帘动了
#   关掉电视柜上方的灯带 → TurnDeviceOff(电视)        电视真关
#   关掉洗衣机上面的灯  → TurnDeviceOff(洗衣机)      洗衣机被切
LANDMARK = [
    ("把窗户旁边的灯打开", "TurnDeviceOn", "灯"),
    ("把窗帘旁边的灯打开", "TurnDeviceOn", "灯"),
    ("关掉电视柜上方的灯带", "TurnDeviceOff", "灯带"),
    ("关掉洗衣机上面的灯", "TurnDeviceOff", "灯"),
    ("关掉空调旁边的电视", "TurnDeviceOff", "电视"),
    ("关掉床头柜上的台灯", "TurnDeviceOff", "台灯"),
    ("关掉门后面的灯", "TurnDeviceOff", "灯"),
]


@pytest.mark.parametrize("text,intent,name", LANDMARK)
def test_locative_landmark_never_becomes_target(fp, text, intent, name):
    """参照物位上的窗/电器词一律不得成为目标（P0：说 A 动 B）。"""
    p = _m(fp, text)
    assert p is not None, f"{text!r} 落空"
    assert p.intent == intent, f"{text!r} → {p.intent}≠{intent}（方位短语被窗道抢走）"
    got = _names(p)
    assert got == [name], f"{text!r} 目标={got!r}≠[{name!r}]"


def test_registered_area_survives_locative(fp):
    """让位给中心语时，**在册**区域必须留住（客厅≠参照物，不许一起丢）。"""
    p = _m(fp, "把客厅窗户旁边的灯打开")
    assert p is not None
    entries = (p.args or {}).get("target") or []
    assert entries and entries[0].get("area") == "客厅", entries
    assert entries[0]["devices"][0]["name"] == "灯", entries


def test_unregistered_landmark_is_not_promoted_to_area(fp):
    """反向臂：参照物（洗衣机）不许被"二次前缀回捞"升格成房间名——坏 area=查无。"""
    p = _m(fp, "关掉洗衣机上面的灯")
    entries = (p.args or {}).get("target") or [{}]
    assert not entries[0].get("area"), f"参照物被当区域:{entries}"


def test_locative_table_single_source_and_shape():
    """机制钉：方位表只收空间关系词，动词一律不入（入表会把中心语让掉）。"""
    assert "旁边" in T._POS_REL_WORDS and "上方" in T._POS_REL_WORDS
    for verb in ("打开", "关闭", "关掉", "调", "设"):
        assert verb not in T._POS_REL_WORDS, f"动词 {verb} 混进方位表"
    assert T.locative_center_wins("窗户旁边的灯") is True
    assert T.locative_center_wins("窗户") is False        # 无方位词=照旧
    assert T.locative_center_wins("窗户旁边的") is False   # 方位后无中心语=照旧
    assert T.locative_center_wins("灯") is False


# ── 刀P1-4：裸窗句只**收窄**到说话卫星所在房间（不扩作用面）────────
def _fp_with(settings_over, areas=AREAS):
    T.sync_areas(list(areas))
    tc_ = _tc_singleton()
    over = copy.deepcopy(DEFAULTS)
    for k, v in settings_over.items():
        cur = over
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v

    class _SO:
        def get(self, dotted, default=None):
            cur = copy.deepcopy(over)
            for kk in dotted.split("."):
                if not isinstance(cur, dict) or kk not in cur:
                    return default
                cur = cur[kk]
            return cur

    return FastPath(_Scenes(), tc_, _SO(), areas_of=lambda: tuple(areas))


def _tc_singleton():
    global _TC
    if _TC is None:
        _TC = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
        _TC._ensure()
    return _TC


def test_satellite_area_narrows_bare_window():
    """配了卫星区域 ⇒ 裸窗句作用面收到那间房（集成对无区域泛称是如实失败）。"""
    fp = _fp_with({"spatial.satellite_areas": {"10.0.0.7": "展厅"}})
    p = asyncio.run(fp.match("关窗", origin="10.0.0.7"))
    assert p is not None and p.intent == "ControlWindow"
    got = (p.args or {}).get("target") or [{}]
    assert got[0].get("area") == "展厅", f"未收窄:{got}"
    assert str(p.args.get("action")) == "close"


def test_no_mapping_never_invents_area():
    """反向臂（关键）：映射为空＝出厂默认，绝不无中生有造区域、更不全屋扇按。"""
    fp = _fp_with({})
    for text in ("关窗", "把窗关上", "窗户关上"):
        p = asyncio.run(fp.match(text, origin="10.0.0.9"))
        assert p is not None, text
        got = (p.args or {}).get("target") or [{}]
        assert not got[0].get("area"), f"{text!r} 被塞了臆造区域:{got}"


def test_unregistered_satellite_area_is_not_attached():
    """反向臂：映射里那个区名**不在本台注册表**时不写（把一个查无换成另一个查无＝没修）。"""
    fp = _fp_with({"spatial.satellite_areas": {"10.0.0.7": "阁楼"}})
    p = asyncio.run(fp.match("关窗", origin="10.0.0.7"))
    got = (p.args or {}).get("target") or [{}]
    assert not got[0].get("area"), f"未在册区名被写入:{got}"


def test_named_window_type_never_gets_satellite_area():
    """反向臂：具名窗型（平开窗）走具名道，本刀一律不伸手（具名不扩大）。"""
    fp = _fp_with({"spatial.satellite_areas": {"10.0.0.7": "展厅"}})
    p = asyncio.run(fp.match("关闭平开窗", origin="10.0.0.7"))
    assert p is not None
    got = (p.args or {}).get("target") or [{}]
    assert not got[0].get("area"), f"具名窗型被收窄改写:{got}"
