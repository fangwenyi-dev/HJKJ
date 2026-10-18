# -*- coding: utf-8 -*-
"""v1.5.4（HomePod 对标批）钉：AED 控制 token 不得把静音轮变成"对空气说话"。

现网实锤（10-13 加载项日志）：FireRedASR2-AED 对纯静音轮吐**尖括号形**控制符 `<sil>`（另见
`<unk>SPK<unk>`），而既有 _STRIP_TAGS 只剥 SenseVoice 时代的竖线形 `<|zh|>` ⇒ token 原样漏出成
**非空**转写：既越过 assist 的空文本判据，又绕过 pipeline.handle 的 `if not text` 空闸
（`<sil>`≠""），一路 cascade 到 fallback ⇒ **每 ~30s 对着空气播「这句话我还不会」**
（12:26–12:30 连条、12:30:44 一条，日志逐字在册）。

HomePod 口径：遇静音是**安静收轮**。修法单点＝asr 出口把 AED 控制 token 剥干净，纯静音轮塌成 ""，
落回**既有、已测**的空转写静默契约。

本钉双向：
①行为面：真实日志形全部塌空；一屏真人话术**逐字不动**（过剥会吃掉真指令）；
②接线面：剥壳调用必须在真转写函数（_local_transcribe）里、在取 result.text 之后——
  防"正则加了没人调"与"将来重构丢调用"两种假绿。

v1.6.5（刀5-c 量具假绿收口）：①原来在测试里**复刻** `_clean`（两级 sub 抄一遍），
从不调生产函数 ⇒ 变异 `new_text = _STRIP_CTRL.sub(...)`（剥完不返回、把旧 text 返回）
语义钉照样绿——测的是复刻，不是修好的东西。现在①直调 `asr._clean_transcript_text`
（＝_local_transcribe 出口真调的那一个函数），复刻删除。②的"调用序"判据保留，
并加一条**反向变异自证**：接线挪走（_local_transcribe 不再调本单点）⇒ 必须红。
"""
import io

from core.asr import _clean_transcript_text as _clean


def test_silence_tokens_collapse_to_empty():
    """现网真实日志形：剥完必须塌成空串（""），否则又会播出「我不会」。
    v1.6.5（刀5-c）：_clean＝生产出口真函数（非复刻）。"""
    for raw in ("<sil>", "<unk>SPK<unk>", " </s><s> ", "<SPOKEN_NOISE>",
                "<unk>SPK<unk> ", "<sil><sil>"):
        assert _clean(raw) == "", (raw, _clean(raw))


def test_real_speech_survives_byte_for_byte():
    """过剥比漏剥更糟：真人指令里绝不允许出现被剥剩的字。一屏金标级话术逐字不动。"""
    for s in ("现在几点", "客厅多少度", "打开办公室的灯", "亮度调到50%",
              "你好小智", "把窗帘打开", "turn on the office light",
              "speak", "spk", "SPKx", "xSPK", "现在有几件事", "语音场景"):
        assert _clean(s) == s, (s, _clean(s))


def test_real_words_keep_aed_tags_stripped_only():
    """带前缀真话（「你好小智」混在 token 后）：token 剥掉、真话留全。"""
    assert _clean("<sil>你好小智") == "你好小智"
    assert _clean("打开办公室的灯<unk>") == "打开办公室的灯"


def test_wired_into_local_transcribe_after_result_text():
    """接线钉（防假绿的两半）：真转写函数必须**调用出口单点**、且发生在读
    stream.result.text 之后。只加正则不接线＝没修；接在别的函数＝真路径照旧播
    「我不会」。v1.6.5（刀5-c）：接线对象＝_clean_transcript_text（①直调的同一
    个函数）⇒ "语义钉测的东西"与"接线钉测的路径"现在是**同一个真函数**，
    变异"剥完不返回"当场红（旧形测复刻，两条都咬不住）。"""
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent          # 路径纪律：不吃 CWD（1.5.3 教训）
    src = (ROOT / "core" / "asr.py").read_text(encoding="utf-8")
    fn = src[src.index("def _local_transcribe"):]
    fn = fn[:fn.index("\n    def ")] if "\n    def " in fn else fn
    assert "_clean_transcript_text(" in fn, \
        "剥壳单点没接进 _local_transcribe（正则成了摆设）"
    # 序判据（刀5-c 改形）：旧形两级 sub 摊开写，靠子串位置证"先取 text 后剥壳"；
    # 现在剥壳是**嵌套调用**——参数先求值、函数后剥，嵌套形本身即序证明。钉死
    # "单点直接吃 stream.result.text"＝接线在出口且没有中间人（旧 text 变量旁路）。
    assert "_clean_transcript_text(stream.result.text" in fn, \
        "剥壳没接在取 result.text 的出口上（剥的是空气或别的变量）"
    # 出口单点自身必须两级都剥（防"单点被抽空成 return raw"的静默回归）
    cf = src[src.index("def _clean_transcript_text"):]
    cf = cf[:cf.index("\n\n\n")] if "\n\n\n" in cf else cf
    assert "_STRIP_TAGS.sub" in cf and "_STRIP_CTRL.sub" in cf, \
        "出口单点丢了某一级剥壳"


def test_clean_is_the_real_export_no_duplication():
    """刀5-c 同源钉：①语义与②接线之间不许再有**复刻第二份**。
    若有人在测试里重新抄一份 _clean（复刻漂移＝假绿回潮的起点），本钉红：
    asr._clean_transcript_text 与本文件导入的 _clean 必须是同一对象。"""
    import core.asr as A
    g = globals()["_clean"]
    assert g is A._clean_transcript_text, "本钉的 _clean 必须是生产函数本体"
