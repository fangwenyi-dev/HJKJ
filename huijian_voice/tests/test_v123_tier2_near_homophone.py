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


# ── v1.5.6 第四类：方言 n↔l（同部位异方法，须同韵母、恰一差、唯一命中）──────
def test_tier2_resolves_n_l_same_final():
    """`内开累倒`(lei 位) → 本家 `内开内倒`（用户真形『打开内开累倒』）。
    nei↔lei：声母 n/l 同址（都是舌尖中/齿龈的鼻音↔边音）、韵母同为 ei ⇒ 恰一位可容差。"""
    prior = ("内开内倒",)
    out, pairs = homophone.rewrite("打开内开累倒", prior)
    assert out == "打开内开内倒" and pairs == [("内开累倒", "内开内倒")], (out, pairs)


def test_tier2_resolves_l_n_same_final():
    """`推拿窗` → 本家 `推拉窗`（反向 l→n）。tuan/tui 韵母差——**不该折**；
    真正同址的是 `扭拉窗`↔`牛拉窗` 类。这里钉的是拉位 l→n：`推那窗`(na↔la)。"""
    prior = ("推拉窗",)
    out, pairs = homophone.rewrite("打开推那窗", prior)   # na ↔ la，韵母同 a
    assert out == "打开推拉窗" and pairs == [("推那窗", "推拉窗")], (out, pairs)


def test_tier2_n_l_refuses_different_final():
    """n↔l 只在**同韵母**才容（`nian`↔`lan` 韵母差＝两个特征，不许改）。"""
    prior = ("内开窗",)
    out, pairs = homophone.rewrite("打开连开窗", prior)   # lian↔nei 差声母+介音两处
    assert pairs == [], f"n/l 韵母不同仍被折：{out} {pairs}"


def test_tier2_n_l_refuses_when_ambiguous():
    """本家同时有 `内开内倒` 与 `内开累倒`（都真装了）⇒ `内开累倒` 字面已在本家，
    红线 3「不许动说对的」优先，一个字都不改。"""
    prior = ("内开内倒", "内开累倒")
    out, pairs = homophone.rewrite("打开内开累倒", prior)
    assert out == "打开内开累倒" and pairs == [], (out, pairs)


def test_tier2_n_l_never_fabricates_absent_target():
    """本家没有『内开内倒』只有『内开窗』时，『内开累倒』不得被凭空造出『内开内倒』
    ——射程收窄方向＝宁漏改不多改（v1.1.17『摄像机→洗碗机』同族红线）。"""
    prior = ("内开窗", "平开窗")
    out, pairs = homophone.rewrite("打开内开累倒", prior)
    assert out == "打开内开累倒" and pairs == [], (out, pairs)


# ── v1.6.6 刀3：方言 n↔l 的短名门槛（类守卫，例子是生成的不是手写的）──────
# 审计复现（2026-10-19，生产 `rewrite` 当场跑）：本家有邻名时，两字名的 n/l 听岔
# 会把用户没说过的设备静默点走——`打开南窗`→`打开蓝窗`、`按门宁`→`按门铃`、
# `打开蓝阳台`→`打开南阳台`（1.5.6 前这些形落查无）。送气对没这个问题：那是
# "同一个字丢特征"，n/l 是"两个都成立、语义不同的字"。
def test_nl_short_name_class_never_folds():
    """任意**两字**名，只要与另一字的差只在 n↔l 声母（韵母同）⇒ 一律不得折。

    候选对由汉字池**生成后过滤**（`_syl`+`_split` 判声母/韵母），不是手写例子——
    手写清单正是 1.5.6 那轮回扫的盲区（只造在册名的伪形，从不测不在册常用词）。
    宇宙塌缩（找不到一对）＝量具失效，当场红。
    """
    from core.nlu import homophone as hp

    pool = ("南蓝宁铃楼牛磊内窗台梯灯门明分风飞非芳黄航黑河红"
            "花华水谁书树数路六绿李梨理里手收首走租组做作")
    syl = {}
    for ch in pool:
        s = hp._syl(ch)
        if s:
            syl[ch] = s[0]
    pairs = []
    for a, sa in syl.items():
        for b, sb in syl.items():
            if a >= b:
                continue
            ia, fa = hp._split(sa)
            ib, fb = hp._split(sb)
            if frozenset((ia, ib)) == frozenset(("n", "l")) and fa == fb:
                pairs.append((a, b))
    assert pairs, "n/l 同韵母汉字对为空＝本钉扫描面失效（先修量具再谈结论）"

    for a, b in pairs:
        # 尾巴一律单字 ⇒ 生成的名字恒为**两音节**（门槛线在 3，三字名是另一档，
        # 由 test_nl_gate_boundary_is_declared_not_accidental 显式钉住意图）。
        for tail in ("窗", "灯", "台", "门", "花"):
            spoken, other = a + tail, b + tail
            if spoken == other:
                continue
            prior = (other,)                      # 本家只有"另一个"名字
            out, changed = hp.rewrite("打开" + spoken, prior)
            assert changed == [], (
                f"两字名 {spoken!r} 被 n↔l 折到 {other!r}＝静默改目标（门槛失守）")


def test_nl_gate_keeps_long_name_wins_and_blocks_short_ones():
    """点名四形（审计证据）＋ 三音节及以上战果逐字不动（门槛不许顺手少接正主）。"""
    from core.nlu import homophone as hp

    prior = ("蓝窗", "门铃", "内开内倒", "推拉窗", "平开窗", "六号灯")
    blocked = ("打开南窗", "按门宁")                        # 两字名：不折
    rescued = (("把内开累倒关上", "把内开内倒关上"),
               ("打开推拿窗", "打开推拉窗"),
               ("关闭凭开窗", "关闭平开窗"))                # ≥3 音节：照旧救
    for text in blocked:
        out, changed = hp.rewrite(text, prior)
        assert out == text and changed == [], (text, out, changed)
    for said, want in rescued:
        out, _ = hp.rewrite(said, prior)
        assert out == want, (said, out, want)


def test_gate_table_is_wired_into_the_decision_point():
    """接线钉：门槛表必须真被 `_near_one`（生产判定单点）消费——只加表不接线＝空转。"""
    import ast
    import inspect
    from core.nlu import homophone as hp
    body = inspect.getsource(hp._near_one)
    assert "_MIN_SYLLABLES_BY_PAIR" in body, "判定单点没接门槛表（门槛形同虚设）"
    src = ast.parse(inspect.getsource(hp))
    names = {n.targets[0].id for n in src.body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    assert "_MIN_SYLLABLES_BY_PAIR" in names
    table = hp._MIN_SYLLABLES_BY_PAIR
    assert table.get(frozenset(("n", "l"))) == 3, \
        f"n↔l 门槛档不对（射程变了要同步本注与 _confusable 文档）：{table}"

def test_nl_gate_boundary_is_declared_not_accidental():
    """门槛线（3 音节）两侧各钉一条，把"这是刻意射程"钉成判据而不是巧合。

    为什么是 3：本批两个战果都在 3~4 音节（推拿窗↔推拉窗、内开累倒↔内开内倒），
    而语义不同字的短名危险族集中在 2 音节（南/蓝窗、门/宁铃）。把线画在 3 是
    "疗效保留、误纠清零"的最低位置——挪线必须同时改本注与 `_MIN_SYLLABLES_BY_PAIR`。
    """
    from core.nlu import homophone as hp
    prior = ("蓝阳台",)
    out, changed = hp.rewrite("打开南阳台", prior)      # 3 音节：折（射程内）
    assert out == "打开蓝阳台" and changed == [("南阳台", "蓝阳台")], (out, changed)
    prior2 = ("蓝窗",)
    out2, changed2 = hp.rewrite("打开南窗", prior2)      # 2 音节：不折（门槛外）
    assert out2 == "打开南窗" and changed2 == [], (out2, changed2)

