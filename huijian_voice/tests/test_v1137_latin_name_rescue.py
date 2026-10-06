# -*- coding: utf-8 -*-
"""修⑧：近音救援不得被拉丁短名打穿（2026-10-05 办公 .91 真机实锤）。

现网留痕（升级到 v1.1.36 后第一手复验，`_goldtest/live_check_1136.py`）：

    [执行] HassTurnOff {'entity_id': 'light.ban_gong_shi_she_deng'} → 成功 | 好的，「射灯」本来就在要求的状态上
    [级联] '关掉会飞的灯' → [klar] '好的，「射灯」本来就在要求的状态上' (94ms)

即"点了家里没有的灯"仍然真下发了那台灯。把现网的**全量在装清单**（`_device_names()`
取所有实体的 friendly_name，共 234 条，含 `Sun`）喂进同一判据，本地立刻复现：

    关掉会飞的灯 | 无证据= False  查无名= ''      ← 该拦没拦
    关掉阳台的灯 | 无证据= False  查无名= ''      ← 猜房间复现

机制：`_near_homophone_in_home` 只比**字符长度**相等，再数 `zip(音节表)` 的差异个数。
`lazy_pinyin('Sun')` 返回 `['Sun']`（1 个元素，不是 3 个音节），于是 zip 只对齐 1 对、
差异数恒 ≤1 ⇒ 任何 3 字的"查无名"都撞上 `Sun`/`TV` 这类拉丁名被判"ASR 听岔"。
一条短英文名就能把整道闸在真机上短路——这正是本仓记过的"钉在≠行为在"，
区别是这次被短路的是我自己刚发出去的修复。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                  # noqa: E402
from core.pipeline import (_near_homophone_in_home, select_primary_plan,  # noqa: E402
                           _klar_named_absent_target)

LAMP = "light.ban_gong_shi_she_deng"
CEID = "cover.ban_gong_shi_ping_kai_chuang"
# 现网 .91 真实形态：两台同名「射灯」+ 拉丁名实体 + 开窗器按钮长名
LIVE = ("射灯", "射灯", "Sun", "TV", "平开窗 ① 开启", "内开窗 ④ 内倒",
        "办公室空调 Indicator Light")


def _kl(utt, eid, intent="HassTurnOff"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


# ── ① 承重：拉丁名不得再当近音救援 ────────────────────────────────
def test_latin_name_is_not_a_homophone_rescue():
    assert _near_homophone_in_home("会飞灯", ("Sun",)) is False
    assert _near_homophone_in_home("阳台灯", ("Sun", "TV")) is False
    assert _klar_named_absent_target(_kl("关掉会飞的灯", LAMP), LIVE,
                                     ("办公室",), ("办公室",)) == "会飞的灯"
    # 回显取"用户自己说的那截"（原话形态），不是内部拼的「阳台灯」
    assert _klar_named_absent_target(_kl("关掉阳台的灯", LAMP), LIVE,
                                     ("办公室",), ("办公室",)) == "阳台的灯"


def test_live_shapes_are_refused_by_adjudication():
    for utt in ("关掉会飞的灯", "关掉阳台的灯", "把会飞的门锁上"):
        eid, intent = ((CEID, "HassTurnOff") if "窗" in utt else
                       ("lock.foo", "HassLock") if "锁" in utt else (LAMP, "HassTurnOff"))
        assert select_primary_plan(None, _kl(utt, eid, intent),
                                   known_areas=("办公室",), device_names=LIVE,
                                   real_areas=("办公室",)) is None, f"{utt} 仍下发"


# ── ② 正向不变量：汉字名的 ASR 听岔救援一条都不许丢 ────────────────
def test_hanzi_homophone_rescue_intact():
    assert _near_homophone_in_home("社灯", ("射灯",)) is True
    assert _near_homophone_in_home("催拉窗", ("推拉窗",)) is True
    assert select_primary_plan(None, _kl("打开社灯", LAMP),
                               known_areas=("办公室",), device_names=LIVE,
                               real_areas=("办公室",)) is not None
    assert _klar_named_absent_target(_kl("关掉催拉窗", CEID), ("推拉窗",),
                                     ("办公室",), ("办公室",)) == ""


def test_real_area_and_real_name_still_pass():
    """真注册区域 + 真在装名 两个正向口子不许被这次收紧波及。"""
    assert select_primary_plan(None, _kl("关掉办公室的灯", LAMP),
                               known_areas=("办公室",), device_names=LIVE,
                               real_areas=("办公室",)) is not None
    assert select_primary_plan(None, _kl("关闭平开窗", CEID),
                               known_areas=("办公室",), device_names=LIVE,
                               real_areas=("办公室",)) is not None


def test_purely_nonhan_spoken_is_not_rescued_by_length_trick():
    """混形态名（一字多音节/非汉字）一律不参与近音比对。"""
    assert _near_homophone_in_home("会飞灯", ("LED",)) is False
    assert _near_homophone_in_home("会飞灯", ("射灯",)) is False   # 长度不同
