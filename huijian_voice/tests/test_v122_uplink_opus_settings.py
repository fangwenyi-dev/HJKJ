# -*- coding: utf-8 -*-
"""v1.2.2 刀2：上行 opus 编码器的显式设置必须**真的**换来擦音段，而不是只多两行赋值。

现场：`custom_components/huijian_ai/stt.py` 构造 `opuslib.Encoder(16k, mono, VOIP)` 之后
**从来没有设过码率**（本机读回 libopus 默认＝bitrate 17000 / complexity 9 / 平均实码率
14560 bps）。远场错形里最要命的一族是 `悬窗↔旋窗/玄窗/宣窗/选窗`、`推拉↔催拉`，区分度住在
**4–8 kHz 擦音段**，所以这一跳该量的是"那段能量还剩多少"，不是"配置项写没写"。

判据取向（为什么不是文本钉）：测试**从生产源码里读出那两个值**，再把它们施加到真编码器上，
用真语音（`tests/e2e/assets/0.wav`，16k/mono/s16/10s）做 编码→解码 往返，量 4–8 kHz 相对
原信号的增益差。⇒ 数字被改小时转红的是**声学结果**，不是字符串相等。
对照臂另跑"什么都不设"的默认形状，必须**更差**——否则这条钉守的是一个不存在的差异。

不在本钉射程内的东西（写清楚，别当成疗效）：3 m 的距离损失是 ~26 dB 量级，本刀实测只赚
0.74 dB。`application` 保持 VOIP——换 AUDIO 的实测差 0.08 dB，落在测量噪声内。
"""
import os
import re
import wave

import pytest

np = pytest.importorskip("numpy")
opuslib = pytest.importorskip("opuslib_next")

HERE = os.path.dirname(os.path.abspath(__file__))
STT = os.path.join(HERE, "..", "custom_components", "huijian_ai", "stt.py")
WAV = os.path.join(HERE, "e2e", "assets", "0.wav")
RATE, FRAME = 16000, 960                     # 60 ms @16k，与 stt.py 的帧形一致


def _settings_from_production():
    """读生产源码里的两个赋值（不抄常量——抄了就变成第二个会漂移的地方）。"""
    src = open(STT, encoding="utf-8").read()
    m = re.search(r"self\.opus_encoder\.bitrate\s*=\s*(\d+)", src)
    c = re.search(r"self\.opus_encoder\.complexity\s*=\s*(\d+)", src)
    assert m and c, "stt.py 里找不到上行 bitrate/complexity 的显式赋值"
    return int(m.group(1)), int(c.group(1))


def _speech():
    w = wave.open(WAV, "rb")
    assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    w.close()
    return x[: len(x) // FRAME * FRAME]


def _fricative_delta_db(bitrate=None, complexity=None):
    """往返后 4–8 kHz 能量相对原信号的差(dB)。None＝不动那个参数(libopus 默认)。"""
    enc = opuslib.Encoder(RATE, 1, opuslib.APPLICATION_VOIP)
    if bitrate is not None:
        enc.bitrate = bitrate
    if complexity is not None:
        enc.complexity = complexity
    dec = opuslib.Decoder(RATE, 1)
    x = _speech()
    rec = []
    for i in range(0, len(x), FRAME):
        pkt = enc.encode(np.clip(x[i:i + FRAME], -32768, 32767).astype(np.int16).tobytes(), FRAME)
        rec.append(np.frombuffer(dec.decode(pkt, FRAME), dtype=np.int16).astype(np.float64))
    rec = np.concatenate(rec)
    n = min(len(x), len(rec))

    def band(y, lo, hi):
        sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        f = np.fft.rfftfreq(len(y), 1.0 / RATE)
        m = (f >= lo) & (f < hi)
        return 10.0 * np.log10(float(np.sum(sp[m] ** 2)) + 1e-9)

    return round(band(rec[:n], 4000, 8000) - band(x[:n], 4000, 8000), 2)


def test_uplink_encoder_settings_are_read_from_production():
    """形状前置：那两个值确实在 stt.py 里、且方向是"抬"不是"压"。
    libopus 在 16k 单声道 VOIP 下的默认 bitrate＝17000（本机读回值，写在这条断言的
    比较式里而不是测试里另抄一份——见 `_fricative_delta_db()` 用默认臂自证）。"""
    bitrate, complexity = _settings_from_production()
    assert bitrate > _fricative_delta_bitrate_default(), (
        f"上行 bitrate={bitrate} 没有超过 libopus 默认")
    assert 8 <= complexity <= 10, f"complexity={complexity} 越界（8~10 才是音质档）"


def _fricative_delta_bitrate_default():
    enc = opuslib.Encoder(RATE, 1, opuslib.APPLICATION_VOIP)
    return int(enc.bitrate)


def test_explicit_settings_actually_recover_the_fricative_band():
    """行为钉（正向）：按生产源码里的值配置后，4–8 kHz 擦音段的损失必须收到 −1.0 dB 以内。"""
    bitrate, complexity = _settings_from_production()
    got = _fricative_delta_db(bitrate, complexity)
    assert got >= -1.0, f"显式 {bitrate}bps/cx{complexity} 后擦音段仍丢了 {got} dB"


def test_default_hop_is_measurably_worse_than_the_configured_one():
    """对照臂：什么都不设（旧生产形状）必须**更差**，否则上一条钉守的是不存在的差异。
    本机实测 默认 −1.42 dB vs 配置后 −0.68 dB。"""
    default = _fricative_delta_db()
    bitrate, complexity = _settings_from_production()
    configured = _fricative_delta_db(bitrate, complexity)
    assert configured > default, (
        f"配置没有换来改善：默认={default} dB, 配置后={configured} dB")
    assert default <= -1.0, f"默认臂不像实测那样差({default})——地形或编码器变了，两条判据都失去对象"
