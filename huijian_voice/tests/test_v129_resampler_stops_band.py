"""v1.2.9 A 刀：`resample_pcm16` / `_StreamResampler` 的阻带抑制必须随抽取比自适应。

现场动因：MeloTTS 原生 **44100Hz**（本机实测 `num_speakers=1 / sample_rate=44100`），
而它是 v1.1.10 起的**默认播报档**——44.1k→16k 是 2.756× 抽取。旧实现把窗函数的
抽头数写死成 ±12 **个源样本**，比率越大窗越先被硬截断（|t| 只走到 ≈8.4 就断），
Hamming 的旁瓣地板因此从 ≈−42dB 抬到实测 −22dB：
    9000→7000 −21.9dB / 12000→4000 −28.5dB / 15000→1000 −37.0dB
折进 1–7kHz 的正是人声"存在度"频段——这是"默认档听着像换了个人"的可测成分。

判据用纯音直接打两个实现（整包与流式是同一公式的双落点，历史上"同类洞只修一半"
在本仓今天已抓过两次），不拿真实模型当量具：模型产出不可复现、也测不到核。
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import audio  # noqa: E402
from core.tts import _StreamResampler  # noqa: E402

SR = 16000


def _tone(freq: float, rate: int, secs: float = 0.25, amp: float = 0.3) -> bytes:
    n = int(rate * secs)
    t = np.arange(n) / rate
    s = amp * np.sin(2 * np.pi * freq * t)
    return audio.f32_to_pcm16(np.asarray(s, dtype=np.float32))


def _peak_db(pcm16: bytes, rate: int) -> tuple[float, float]:
    """返回 (主峰 Hz, 归一化到满幅的峰值 dB)。"""
    x = audio.pcm16_to_f32(pcm16)
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))) * 2.0 / len(x)
    f = np.fft.rfftfreq(len(x), 1.0 / rate)
    i = int(np.argmax(sp))
    mag = float(sp[i])
    return float(f[i]), (20 * math.log10(mag) if mag > 0 else -240.0)


def _rel_db(pcm16: bytes, rate: int, baseline_peak: float) -> float:
    """同一输入幅度下，相对通带基准峰的 dB。"""
    _f, db = _peak_db(pcm16, rate)
    return db - baseline_peak


def _whole(pcm: bytes, src: int, dst: int = SR) -> bytes:
    return audio.resample_pcm16(pcm, src, dst)


def _streamed(pcm: bytes, src: int, dst: int = SR, chunk: int = 4000) -> bytes:
    r = _StreamResampler(src, dst)
    out = b"".join(r.feed(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))
    return out + r.flush()


@pytest.fixture(scope="module")
def cloudless_baseline() -> float:
    """通带基准峰值 dB：取 1000Hz@44.1k 整包重采样后的主峰。

    与被测频率**不同源**是这条钉的要点——旧写法拿 3000Hz 当基准又同时测 3000Hz，
    等于同一个表达式自比（恒 0.0，对抗复核抓出的恒真臂）。
    """
    return _peak_db(_whole(_tone(1000.0, 44100), 44100), SR)[1]


# ── ① 整包实现：44.1k→16k 的阻带 ────────────────────────────────
# 阈值选取纪律（对抗复核抓出"修前也绿＝没牙"后重定）：每个臂必须**同时**满足
# 旧核过不了、新核有 ≥10dB 余量。旧核算术值：9000 −21.9｜12000 −28.5｜15000 −37.0｜
# 18000 −41.4｜21000 −37.2；新核：−40.8｜−57.8｜−81.2｜−64.0｜−81.9。
@pytest.mark.parametrize("freq,min_db", [(9000.0, 32.0), (12000.0, 45.0),
                                         (15000.0, 45.0), (18000.0, 50.0),
                                         (21000.0, 45.0)])
def test_melo_rate_downsample_rejects_stopband(cloudless_baseline, freq, min_db):
    """44.1k 原生进 16k：8k 以上成分必须被**滤除**，不许折回通带。"""
    out = _whole(_tone(freq, 44100), 44100)
    pk, rel = _peak_db(out, SR)[0], _rel_db(out, SR, cloudless_baseline)
    assert rel <= -min_db, (
        f"44.1k 的 {freq:.0f}Hz（>8k，必须整体滤除）折回 {pk:.0f}Hz 只剩 {rel:.1f}dB "
        f"抑制，要求 ≥{min_db:.0f}dB")


# ── ② 通带不得因为加长窗而变闷（否则修过头）────────────────────
# 基准取 1000Hz：旧写法拿 3000Hz 当基准又同时测 3000Hz，是**同一个表达式自比**
# ⇒ rel 恒 0.0（对抗复核抓出的恒真臂，已废）。基准与被测频率必须不同源。
@pytest.mark.parametrize("freq", [3000.0, 5000.0, 7000.0])
def test_melo_rate_passband_stays_flat(freq):
    base = _peak_db(_whole(_tone(1000.0, 44100), 44100), SR)[1]
    rel = _rel_db(_whole(_tone(freq, 44100), 44100), SR, base)
    assert rel >= -1.5, f"通带 {freq:.0f}Hz 相对跌落 {rel:.1f}dB，滤波器不能这么钝"


def test_kokoro_rate_downsample_not_regressed():
    """24k→16k（Kokoro 回落档、1.5×）同判据。

    ⚠ 旧写法用 `_tone(12000, 24000)`——那正是 24k 的**奈奎斯特频点**，
    sin(2π·12000·n/24000)=sin(πn)≡0 ⇒ 输入是纯静音（实测 max|x|=1.1e-12），
    任何实现都能"抑制 −223dB"，这条钉当时**完全空转**（对抗复核抓出，已复现）。
    改用 11000/10000：旧核算术值 −41.6/−39.4，新核 −184.6/−62.9。
    """
    base = _peak_db(_whole(_tone(1000.0, 24000), 24000), SR)[1]
    for freq in (11000.0, 10000.0):
        rel = _rel_db(_whole(_tone(freq, 24000), 24000), SR, base)
        assert rel <= -50.0, f"24k 的 {freq:.0f}Hz 只有 {rel:.1f}dB 抑制（必须 ≥50dB）"


def test_stream_and_whole_packet_agree_at_melo_rate():
    """流式孺核与整包核必须逐式同构：44.1k 抽取比下也一致（防止只修一侧）。

    输入打在 **12kHz 阻带音**上而不是通带音：通带音上两侧的差异 <0.02，
    变异"流式跨度退回写死"照样绿（实测如此）；阻带音上旧流式是 −34dB、
    修后整包是 −58dB，差 24dB——只有打在阻带上这条钉才有牙。
    """
    raw = _tone(12000.0, 44100, secs=0.4)
    one, many = _whole(raw, 44100), _streamed(raw, 44100)
    n = min(len(one), len(many))
    assert len(one) == len(many), f"输出点数不一致 {len(one)} vs {len(many)}"
    diff = np.abs(audio.pcm16_to_f32(one[:n]) - audio.pcm16_to_f32(many[:n]))
    assert float(diff.max()) < 5e-4, f"流式与整包最大差 {diff.max():.5f}（同式同跨度应到 1e-4 量级）"


def test_stream_stopband_matches_whole_packet():
    """流式一侧的阻带同样要达标——它是云 TTS 任意率→16k 的必经落点。"""
    base = _peak_db(_streamed(_tone(3000.0, 44100), 44100), SR)[1]
    rel = _rel_db(_streamed(_tone(12000.0, 44100), 44100), SR, base)
    assert rel <= -38.0, f"流式重采样 12kHz 分量只剩 {rel:.1f}dB 抑制"
