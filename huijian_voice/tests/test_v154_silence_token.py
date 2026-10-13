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
"""
import io

from core.asr import _STRIP_CTRL, _STRIP_TAGS


def _clean(raw: str) -> str:
    """复刻生产出口的两级剥壳（竖线形 + 控制符），只测剥壳语义不碰引擎。"""
    t = _STRIP_TAGS.sub("", raw)
    t = _STRIP_CTRL.sub("", t)
    return t.replace("　", "").strip()


def test_silence_tokens_collapse_to_empty():
    """现网真实日志形：剥完必须塌成空串（""），否则又会播出「我不会」。"""
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
    """接线钉（防假绿的两半）：剥壳必须在真转写函数里、且发生在读 stream.result.text 之后。
    只加正则不接线＝没修；接在别的函数＝真路径照旧播「我不会」。"""
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent          # 路径纪律：不吃 CWD（1.5.3 教训）
    src = (ROOT / "core" / "asr.py").read_text(encoding="utf-8")
    fn = src[src.index("def _local_transcribe"):]
    fn = fn[:fn.index("\n    def ")] if "\n    def " in fn else fn
    assert "_STRIP_CTRL.sub" in fn, "控制符剥壳没接进 _local_transcribe（正则成了摆设）"
    i_raw = fn.index("stream.result.text")
    i_strip = fn.index("_STRIP_CTRL.sub")
    assert i_raw < i_strip, "剥壳发生在取 result.text 之前＝剥的是空气"
