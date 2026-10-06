# -*- coding: utf-8 -*-
"""v1.1.42 远场 L1 Tier A：清单驱动的**同音改写**（`core/nlu/homophone.py`）。

现场（2026-10-06 办公 .91 日志，14:28–14:34，24 轮逐字仅 9/24）：
    「关闭展厅悬窗」被写成「关闭展厅**旋窗**」→ klar 顶到 area 扇出 → 能力闸拦下；
    同句另一轮「关闭展厅**悬窗**」逐字对 → 真执行。同一台设备、六分钟内有成有败。
    另有 玄窗/宣窗/选窗 三形同族，且 **手工纠错表里一个都没有**
    （`corrector.py` 窗族只有 平开闯/平台窗/平抬窗/平胎窗/平台商/平盖窗/平改窗/催拉窗）
    ⇒ 本层的贡献面与既有车道不重叠。

机制约束（本机 sherpa-onnx 1.13.7 实测，不是转述）：SenseVoice 构造函数里**没有
hotwords**；`ys_log_probs` 返回空数组；`modified_beam_search` 被实现直接拒绝
（"Only greedy_search is supported at present"）⇒ 既拿不到解码期偏置，也拿不到多假设
与置信度。唯一可落地的先验位置是**解码后的词表改写**。

判据口径：本文件的"清单"是夹具里显式传入的名字集合，与生产同源
（`pipeline._device_names()` ∪ `_real_areas()`）。**全篇不改任何进程级全局态**——
首版用 `T.sync_vocab/clear_vocab` 铺地形，被全量回归证明会随收集顺序时好时坏
（正是 `_device_names` 注释里记过的"全局态泄漏"同型坑）。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu import homophone as H                                   # noqa: E402
from core.nlu import targets as T                                     # noqa: E402
from core.nlu.fast_path import Plan, FastPath                          # noqa: E402
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)       # noqa: E402


def _arun(coro):
    return asyncio.run(coro)


# 日志真名派生的地形（友好名拆词后含 悬窗/开窗器/推拉窗/内开窗/平开窗/展厅）
HOME = {
    "cover.xuan": {"attributes": {"friendly_name": "悬窗 开窗器"}},
    "cover.tuila": {"attributes": {"friendly_name": "推拉窗 开窗器"}},
    "cover.neikai": {"attributes": {"friendly_name": "内开窗 开窗器"}},
    "cover.pk1": {"attributes": {"friendly_name": "平开窗 ① 开启"}},
    "cover.pk2": {"attributes": {"friendly_name": "平开窗 ② 开启"}},
    "media_player.zt": {"attributes": {"friendly_name": "展厅"}},
}
XUAN = "cover.xuan"
PRIOR = T.tokens_of([e["attributes"]["friendly_name"] for e in HOME.values()])


def _plan(utt, intent="ControlWindow"):
    return Plan(intent=intent, args={"entity_id": XUAN}, source="klar", utterance=utt)


def _run(text, table=None, home=None):
    """走真入口 `handle()`；先验由传进去的 ha 状态决定，不碰任何全局态。"""
    kl = Lane(table or {}, single=None)
    ex = RecExecutor()
    _arun(_pipe(kl=kl, ex=ex, ha=HA(HOME if home is None else home))
          .handle(text, origin="o"))
    return kl, ex


# ── ① 单元：同音唯一命中 → 改写（按类扫日志四形，不许只钉一例）──────────
def test_homophone_forms_all_reach_canonical_spelling():
    names = ("悬窗", "开窗器", "推拉窗", "内开窗", "平开窗", "展厅")
    for spoken, note in (("旋窗", "日志 14:30:21/14:31:15"),
                         ("玄窗", "日志 14:32:58"),
                         ("宣窗", "日志 14:31:41"),
                         ("选窗", "日志 14:31:53")):
        out, ch = H.rewrite(f"关闭展厅{spoken}", names)
        assert out == "关闭展厅悬窗", f"{spoken}（{note}）未归一到规范写法：{out}"
        assert ch == [(spoken, "悬窗")], f"改写痕迹要点名原片段与规范名: {ch}"


def test_area_name_homophone_also_reachable():
    out, ch = H.rewrite("打开展厅推拉窗", ("推拉窗", "展厅"))
    assert (out, ch) == ("打开展厅推拉窗", [])          # 本字正确 ⇒ 零改写
    assert H.rewrite("打开战厅推拉窗", ("推拉窗", "展厅"))[0] == "打开展厅推拉窗"


# ── ② 反向钉：护栏逐条摘一条漏一类事故 ────────────────────────────────
def test_ambiguous_homophones_are_never_rewritten():
    """两个本家名同音、而话里说的是第三种写法 ⇒ 整条拼音键作废，一个字不许动。

    首版这条钉是**假绿**（变异 M2 摘掉歧义丢弃后全绿）：当时把「旋窗」也传进了 names，
    被"字面在本家"守卫先拦下，测的根本不是歧义。现网地形真会塌缩：
    `平开窗 ① 开启` 与 `平开窗 ② 开启` 拆词后序号（单字）被长度下限吃掉。
    """
    idx = H.homophone_index(("悬窗", "玄窗", "开窗器"))
    assert sorted(idx[("xuan", "chuang")]) == ["悬窗", "玄窗"], "歧义档必须留在索引里可见"
    for names in (("悬窗", "玄窗"), ("悬窗", "玄窗", "宣窗")):
        out, ch = H.rewrite("关闭旋窗", names)          # 「旋窗」谁也不是 ⇒ 不许赌
        assert out == "关闭旋窗" and ch == [], f"歧义同音被赌成一台：{out}"
    # 等价臂显式固化：只有一写法时必须照改，证明上面转红来自"两个"而非"整条没接"
    out, ch = H.rewrite("关闭旋窗", ("悬窗",))
    assert out == "关闭悬窗" and ch == [("旋窗", "悬窗")]


def test_literal_present_in_home_is_untouched():
    """用户真说的就是本家那个名字时，不得被同音邻居顶掉。"""
    assert H.rewrite("打开凯窗", ("凯窗", "开窗器")) == ("打开凯窗", [])


def test_substring_of_well_spoken_name_is_untouched():
    """「开窗器」字面正命中 ⇒ 它的真子串「开窗」不许被同音名「凯窗」顶掉。"""
    assert H.rewrite("打开窗器", ("开窗器", "凯窗")) == ("打开窗器", [])


def test_non_han_names_do_not_participate():
    """拉丁/数字短名（`Sun` 3 字符 1 音节）两侧都不进比对。

    承重闸门是 `_HAN`（索引侧与片段侧各一道）；`_syl()` 的长度等式与之互为冗余带
    （变异 M4 显示单摘其一如不转红），保留理由是它同时是"改写两侧等长"的来源。
    2026-10-05 现网清单实证过：只比字符长度会让音节差被假读成 0、整道闸短路。
    """
    assert H.homophone_index(("Sun", "悬窗")).get(("xuan", "chuang")) == {"悬窗"}
    assert H.homophone_index(("Sun",)) == {}, "拉丁名不得进索引"
    assert H.rewrite("打开Sun窗", ("Sun", "悬窗")) == ("打开Sun窗", [])


def test_replacement_never_shifts_positions():
    """改写两侧必须等长——覆盖标记与位移的正确性全押在这条不变量上。"""
    names = ("悬窗", "展厅", "推拉窗")
    src = "关闭展厅旋窗并打开玄窗"
    out, ch = H.rewrite(src, names)
    assert len(out) == len(src), f"改写改变了长度 ⇒ 位移与覆盖表全部失效: {out}"
    assert out == "关闭展厅悬窗并打开悬窗" and len(ch) == 2, (out, ch)


def test_rewrite_is_idempotent():
    names = ("悬窗", "开窗器", "展厅")
    once, ch1 = H.rewrite("关闭展厅旋窗", names)
    assert ch1 == [("旋窗", "悬窗")]
    assert H.rewrite(once, names) == (once, []), "canonical 模块的幂等纪律同样适用本层"


def test_fail_open_without_pypinyin():
    """拼音库不可用 ⇒ 原样返回（现网行为逐值不变，绝不因缺依赖改判）。"""
    keep = H._lazy_pinyin
    H._lazy_pinyin = False
    try:
        assert H.rewrite("关闭展厅旋窗", ("悬窗", "展厅")) == ("关闭展厅旋窗", [])
    finally:
        H._lazy_pinyin = keep


# ── ③ 接线：真入口 + 先验来源与查无子闸同源 ───────────────────────────
def test_pipeline_rewrites_before_matching_and_executes():
    kl, ex = _run("关闭展厅旋窗", {"关闭展厅悬窗": _plan("关闭展厅悬窗")})
    assert kl.seen == ["关闭展厅悬窗"], f"送去匹配的必须是改写后的文本: {kl.seen}"
    assert ex.plans, "同音归一后这轮应真执行（旧形在此是能力闸拦下/查无）"


def test_pipeline_leaves_correct_utterance_untouched():
    kl, ex = _run("关闭展厅悬窗", {"关闭展厅悬窗": _plan("关闭展厅悬窗")})
    assert kl.seen == ["关闭展厅悬窗"] and ex.plans


def test_no_inventory_means_no_rewrite():
    """清单拿不到 ⇒ 本层整条不生效，首句行为逐值不变（与 `_device_names` 同纪律）。"""
    kl, _ = _run("关闭展厅旋窗", {"关闭展厅旋窗": _plan("关闭展厅旋窗")}, home={})
    assert kl.seen == ["关闭展厅旋窗"], f"无清单却被改写: {kl.seen}"


def test_static_generic_words_are_never_the_prior():
    """本家没装「门锁」时，静态表里那三个字不许当先验凭空造出设备。

    对照臂证明「门所」在本层能力范围内（否则上一条断言只是"根本没接"的空转）；
    这正是 `_admissible` v1.1.27「打开摄像机→洗碗机」事故的同族防线。
    """
    kl, _ = _run("关掉门所", {"关掉门所": _plan("关掉门所", intent="HassTurnOff")})
    assert kl.seen == ["关掉门所"], f"本家没有门锁却把「门所」归一成门锁: {kl.seen}"
    assert H.rewrite("关掉门所", ("门锁",))[1] == [("门所", "门锁")], "对照臂失灵=本层没接"


def test_prior_source_is_the_same_inventory_the_gate_uses():
    """先验必须来自 `_device_names()`（友好名按生产规则拆词），不是别的快照。"""
    assert "悬窗" in PRIOR and "开窗器" in PRIOR, f"友好名拆词后应含 悬窗: {PRIOR}"
    assert not any(len(t) == 1 for t in PRIOR), "单字/序号档不进先验（长度下限）"


# ── ④ 金标盲区补位：级联入口这一层不得让终审契约变档 ──────────────────
# 金标本体只钉 fp（字面表/剥壳/前缀/同音）与查询族两棵确定性引擎，**不经 `_cascade`**
# ⇒ 看不见同音改写层。首版这里钉的是"82 句原文不许被改写"——那是我自己发明的更严要求，
# 方向也错：真正的契约是 **(intent, source) 不变档**。
GOLDEN_PRIOR = ("空调", "门锁", "电视")          # 最坏地形：本家真装了这三个


def test_golden_contract_survives_rewrite():
    """被同音改写过的金标句，落到 fp 后必须仍是同一 (intent, source)。"""
    import os
    from core.nlu.textcnn import TextCNN
    from test_golden_set import ROWS
    from golden_gen import FakeScenes, S

    tc = TextCNN(Path(os.environ.get("HUIJIAN_NLU_DATA", "nlu_data")))
    tc._ensure()
    fp = FastPath(FakeScenes(), tc, S())
    rewritten = []
    for row in ROWS:
        out, ch = H.rewrite(row["text"], GOLDEN_PRIOR)
        if not ch:
            continue
        rewritten.append(row["text"])
        if row["intent"] is None:
            continue                             # 负样本行的"不许冒接"由金标本体守
        plan = _arun(fp.match(out))
        got = (plan.intent, plan.source) if plan is not None else (None, None)
        assert got == (row["intent"], row["source"]), (
            f"{row['text']!r} 改写为 {out!r} 后契约变档："
            f"{(row['intent'], row['source'])} → {got}")
    assert rewritten, "本地形没有一句被改写 ⇒ 这条钉空转，须换地形或删钉"


# ── ⑤ 结构钉：接线位置（只保形状，行为由 ③ 保）────────────────────────
def test_hook_position_after_creation_before_compound():
    src = (ROOT / "core/pipeline.py").read_text(encoding="utf-8")
    i_create = src.index("created = await self._voice_creation(text, origin)")
    i_hook = src.index("homophone.rewrite(")
    i_chain = src.index("chain = await self._try_compound(text, origin)")
    assert i_create < i_hook < i_chain, (
        "改写点必须排在创建承接之后（创建句存用户原话）、复合切分之前（链两腿同口径）")
    assert src.count("homophone.rewrite(") == 1, "改写点全链单点，多处调用会各自漂移"
    assert "T.tokens_of(self._device_names())" in src, "先验来源必须与查无子闸同一份清单"
