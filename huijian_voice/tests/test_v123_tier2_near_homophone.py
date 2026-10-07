# -*- coding: utf-8 -*-
"""v1.2.3 L1-2 第二刀：Tier A 加"近音档"——同长度、**恰好一个音节**之差，
且那一个差必须属远场可解释的三类混淆，否则一律不改。

为什么这一刀要做（现网实证，非猜测）：远场低 SNR 下先丢的是**擦音/送气/鼻音尾**这类
低能量特征（本仓实测 4–8 kHz 擦音段在 SNR≈0 档直接丢字），于是 SenseVoice 会"流利地"
写出 `频开窗`（本家叫 `平开窗`）、`带灯`（本家叫 `台灯`）——**拼音不同但只差一个特征**，
同音档（Tier A 第一档，要求拼音串完全相同）够不着，而 ⑦ 选择侧又被 `if not _hit_dev`
挡在泛称形之外。这一档就补这一格。

红线（四条，逐条有钉，全部是"宁漏改不多改"方向）：
  1. **只容许一个音节差**，且差值必须是：同发音部位的送气↔不送气（b/p、d/t、g/k、
     z/c、zh/ch、j/q）、鼻音尾 ↔ 无尾（-n/-ng/∅）、介音丢（i/u/ü 有无）三选一。
     跨发音部位（`tai` vs `kai`、`hong` vs `ping`）**不改**——那已经不是"听岔一个特征"，
     是另一个词。
  2. **唯一命中才改**：两个本家名都落在射程内 ⇒ 整条作废（歧义不改，与第一档同规）。
  3. **不许动已经说对的**：片段字面在本家清单里 ⇒ 原样。
  4. **长度不变**：只在同长度的名之间比 ⇒ 位移与覆盖标记恒成立（第一档同款不变量）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nlu import homophone  # noqa: E402


# ── 正向：三类可解释混淆各钉一条 ────────────────────────────────────
def test_tier2_resolves_aspiration_pair_same_place():
    """d↔t 同部位送气差：`带灯` → 本家 `台灯`。"""
    prior = ("台灯", "客厅主灯")
    out, pairs = homophone.rewrite("把带灯打开", prior)
    assert out == "把台灯打开", (out, pairs)
    assert pairs == [("带灯", "台灯")], pairs


def test_tier2_resolves_nasal_coda_drop_or_add():
    """-n ↔ -ng 鼻音尾差：`频开窗` → 本家 `平开窗`（现网日志真形）。"""
    prior = ("平开窗", "推拉窗")
    out, pairs = homophone.rewrite("打开频开窗", prior)
    assert "平开窗" in out, (out, pairs)


def test_tier2_resolves_missing_medial():
    """介音丢失（同初始、同主元音，只差 i/u/ü 有无）：`干连器`(gan lian qi) → `关联器`(guan lian qi)。
    位置 2、3 逐字相同，只有位置 1 差一个介音 ⇒ 恰容一差。"""
    prior = ("关联器",)
    out, pairs = homophone.rewrite("把干连器开一下", prior)
    assert out == "把关联器开一下" and pairs == [("干连器", "关联器")], (out, pairs)


def test_tier2_refuses_different_main_vowel():
    """`旋称`(xuan cheng) 与 `悬窗`(xuan chuang)：第二音节 cheng/chuang 差的是**韵母主体**
    （不是 -n/-ng 也不是介音）⇒ 两特征之差，必须不改。"""
    prior = ("悬窗",)
    out, pairs = homophone.rewrite("把旋称关掉", prior)
    assert pairs == [], f"韵母主体之差被当成可容混淆：{out} {pairs}"


# ── 反向：跨发音部位／多音节差／歧义／已说对 ────────────────────────
def test_tier2_refuses_cross_place_confusion():
    """`tai` vs `kai` 不是同部位送气对 ⇒ **不许改**。这条是防"什么都能糊成设备"的闸。"""
    prior = ("平开窗",)
    out, pairs = homophone.rewrite("打开平台窗", prior)
    assert pairs == [], f"跨发音部位被糊过去了：{out} {pairs}"


def test_tier2_refuses_when_ambiguous():
    """本家同时有 `台灯` 与 `抬登`（都与 `带灯` 射程内）⇒ 歧义，一个字都不改。"""
    prior = ("台灯", "抬登")
    out, pairs = homophone.rewrite("把带灯打开", prior)
    assert out == "把带灯打开" and pairs == [], (out, pairs)


def test_tier2_never_touches_a_correctly_spoken_name():
    prior = ("射灯", "台灯")
    for spoken in ("关掉射灯", "打开台灯", "射灯台灯都要"):
        out, pairs = homophone.rewrite(spoken, prior)
        assert out == spoken and pairs == [], (spoken, out, pairs)


# ── 不变量：长度与改写次数 ─────────────────────────────────────────
def test_tier2_preserves_length_and_single_rewrite_per_span():
    prior = ("台灯", "平开窗")
    for spoken in ("把带灯打开", "打开频开窗", "把带灯和频开窗都关上"):
        out, pairs = homophone.rewrite(spoken, prior)
        assert len(out) == len(spoken), (spoken, out)        # 同长度替换 ⇒ 位移不变
        srcs = [a for a, _ in pairs]
        assert len(srcs) == len(set(srcs)), f"同一片段被改了两遍：{pairs}"
        for a, b in pairs:
            assert len(a) == len(b), (a, b)


def test_tier2_is_off_when_no_inventory():
    """拿不到清单 ⇒ 整层不生效（与第一档同规，行为逐值回落到没有本层之前）。"""
    out, pairs = homophone.rewrite("把带灯打开", ())
    assert out == "把带灯打开" and pairs == []


# ── 接线：仍然只有级联入口这一个改写点，且档次要可观测 ──────────────
def test_tier2_hooked_at_the_same_single_point():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "core", "pipeline.py"), encoding="utf-8").read()
    assert src.count("homophone.rewrite(") == 1, "改写点必须仍是全链单点"
    hs = open(os.path.join(here, "..", "core", "nlu", "homophone.py"),
              encoding="utf-8").read()
    assert "near_index" in hs or "_NEAR" in hs, "近音档没实现（只是文档）"
