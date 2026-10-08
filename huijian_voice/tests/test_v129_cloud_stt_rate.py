"""v1.2.9 云 STT 按模型带率（表为唯一源、真重采样、不把 rate 当 form 字段发）。

现场形状：`_cloud_transcribe` 旧写法 `audio.pcm_to_wav(pcm_s16)` 恒按 16k 写头也恒只
发 16k 内容——不是标错率（诚实），而是**没有能力喂一个要求别的率的端点**。
厂商口径已核（2026-10-08）：
· 阿里云百炼非实时 ASR 逐字「支持任意采样率」，且 OpenAI 兼容示例里**没有**
  `sample_rate` 参数（它只存在于 DashScope 原生 `parameters.sample_rate`）
  ⇒ 默认不发 form 字段是必须，不是保守；
· 阿里云 ISI 一句话/实时/录音极速版逐字「采样率：8000 Hz 或 16000 Hz」、
  「模型类型：8000（电话）和 16000（非电话）」⇒ 8k 电话档才是真需要带率的地方。
表只放有出处的规则，其余一律不转（等价于现版）。
"""
import asyncio
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import asr, audio  # noqa: E402


class DSettings:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


class _Content:
    """按 `read(n)` 语义给字节（`_read_capped` 会循环读到空串为止）。"""

    def __init__(self, payload):
        self._p = payload

    async def read(self, n=-1):
        if n is None or n < 0:
            out, self._p = self._p, b""
            return out
        out, self._p = self._p[:n], self._p[n:]
        return out


class _Resp:
    status = 200
    content_type = "application/json"

    def __init__(self, payload):
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return b"{}"


class _Session:
    def __init__(self, recorder):
        self.rec = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, data=None, headers=None):
        self.rec["url"] = url
        self.rec["headers"] = headers or {}
        for name, value, _kw in getattr(data, "_fields", []):
            self.rec.setdefault("fields", {})[name] = value
        return _Resp(json.dumps({"text": "好"}).encode())


class _Form:
    """替身 aiohttp.FormData——只为把 add_field 的名字/字节抓下来判产物。"""

    def __init__(self):
        self._fields = []

    def add_field(self, name, value, **kw):
        self._fields.append((name, value if isinstance(value, bytes)
                             else str(value).encode(), kw))


@pytest.fixture
def cloud_probe(monkeypatch):
    """跑一次 `_cloud_transcribe`，返回 (form 字段表, 引擎)。"""
    def run(settings, pcm, cloud):
        rec = {}
        eng = asr.AsrEngine(DSettings(settings),
                            SimpleNamespace(model_dir_for=lambda key: Path("."),
                                            ensure_async=lambda key: None))
        monkeypatch.setattr("aiohttp.ClientSession", lambda **kw: _Session(rec))
        monkeypatch.setattr("aiohttp.FormData", _Form)
        text = asyncio.run(eng._cloud_transcribe(pcm, cloud))
        assert text == "好", f"云路本身没通：{text!r}"
        return rec, eng
    return run


def _pcm(seconds=0.4, rate=audio.const.SAMPLE_RATE, freq=3000.0):
    import numpy as np
    t = np.arange(int(rate * seconds)) / rate
    return audio.f32_to_pcm16((0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32))


def _wav_rate(data: bytes) -> int:
    """`fmt ` 块内偏移：块名 4B + 块长 4B + format 2B + channels 2B → rate 在 +12。"""
    i = data.find(b"fmt ")
    assert i >= 0, "拿到的不是 RIFF/WAVE"
    return struct.unpack("<I", data[i + 12:i + 16])[0]


def _wav_frames(data: bytes) -> int:
    i = data.find(b"data")
    return struct.unpack("<I", data[i + 4:i + 8])[0] // 2


# ── ① 显式率：真重采样 + 头随实际率 ──────────────────────────────
def test_explicit_rate_resamples_and_labels(cloud_probe):
    pcm = _pcm(seconds=0.4)
    rec, _ = cloud_probe({}, pcm, {"base_url": "http://x/v1", "model": "m",
                                   "sample_rate": 24000})
    wav = rec["fields"]["file"]
    assert _wav_rate(wav) == 24000, "WAV 头没跟着实际产出率走"
    n = _wav_frames(wav)
    assert abs(n - 0.4 * 24000) <= 24000 * 0.06, f"样本数没真按 1.5× 变（拿到 {n}）"


# ── ② 表驱动：不配字段也要按模型名带率（8k 电话档）──────────────
def test_table_drives_rate_for_telephony_model(cloud_probe):
    rec, _ = cloud_probe({}, _pcm(), {"base_url": "http://x/v1",
                                      "model": "paraformer-8k-v1"})
    assert _wav_rate(rec["fields"]["file"]) == 8000, \
        "8k 电话档仍被当 16k 发——ISI 那类只容 8000/16000 的端点会拿到错率"


# ── ③ 等价臂：默认/未命中 ⇒ 逐字节等于现版 ──────────────────────
@pytest.mark.parametrize("model", ["paraformer-v2", "whisper-1", "qwen3-asr-flash"])
def test_general_models_keep_current_bytes(cloud_probe, model):
    pcm = _pcm()
    rec, _ = cloud_probe({}, pcm, {"base_url": "http://x/v1", "model": model})
    assert rec["fields"]["file"] == audio.pcm_to_wav(pcm), \
        f"{model} 不该被转换（厂商自述支持任意采样率），现版行为必须逐字节保持"


# ── ④ 脏值/域外 ⇒ 消毒回表驱动并 WARN 点名 ──────────────────────
@pytest.mark.parametrize("bad", ["44.1k", 0, -1, 1, 999999, None, ""])
def test_bad_rate_falls_back_to_table_and_warns(cloud_probe, bad, caplog):
    with caplog.at_level("WARNING"):
        rec, _ = cloud_probe({}, _pcm(), {"base_url": "http://x/v1",
                                          "model": "paraformer-8k-v1",
                                          "sample_rate": bad})
    assert _wav_rate(rec["fields"]["file"]) == 8000, "脏值应消毒回表驱动，不是静默 16k"
    if bad not in (None, "", 0):
        msgs = " | ".join(r.getMessage() for r in caplog.records)
        assert "stt.cloud.sample_rate" in msgs, f"WARN 没点名是哪个配置项：{msgs[:160]}"


# ── ⑤ 显式覆盖优先于表 ──────────────────────────────────────────
def test_explicit_override_beats_table(cloud_probe):
    rec, _ = cloud_probe({}, _pcm(), {"base_url": "http://x/v1",
                                      "model": "paraformer-8k-v1", "sample_rate": 16000})
    assert _wav_rate(rec["fields"]["file"]) == 16000


# ── ⑥ 不把 sample_rate 当 form 字段发（OpenAI 兼容示例里没有这个参数）─
def test_rate_is_not_sent_as_form_field(cloud_probe):
    rec, _ = cloud_probe({}, _pcm(), {"base_url": "http://x/v1", "model": "m",
                                      "sample_rate": 24000})
    assert "sample_rate" not in rec["fields"], \
        "百炼/OpenAI 兼容 /audio/transcriptions 未定义该字段，默认发它=可能被拒"


# ── ⑦ 表是单一事实源：面板不得抄第二份 ──────────────────────────
def test_rate_table_is_single_source():
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    hits = [ln for ln in html.splitlines() if "paraformer-8k" in ln or "8k-v1" in ln]
    assert not hits, f"面板抄了第二份模型→率表：{hits[0][:90]}"
    src = (ROOT / "core" / "asr.py").read_text(encoding="utf-8")
    assert src.count("CLOUD_MODEL_RATES") >= 1, "表没落在 asr.py（唯一源）"


# ── ⑨ 表命中必须留痕（对抗复核：命中时 _model_rate 一条日志都不打）──
def test_table_hit_leaves_a_log(cloud_probe, caplog):
    with caplog.at_level("INFO"):
        cloud_probe({}, _pcm(), {"base_url": "http://x/v1", "model": "paraformer-8k-v1"})
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "paraformer-8k-v1" in msgs and "8000" in msgs, \
        f"表驱动改了发出去的音频率却没留痕：{msgs[:200]!r}"


def test_no_conversion_makes_no_noise(cloud_probe, caplog):
    """未命中（16k 不转）时不许刷这条日志——每轮一条 INFO 会把日志淹掉。"""
    with caplog.at_level("INFO"):
        cloud_probe({}, _pcm(), {"base_url": "http://x/v1", "model": "paraformer-v2"})
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "模型表带率" not in msgs, "不转换也不该报'带率'"


# ── ⑧ 面板接线三处齐（字段/回读/写回），漏一处就是"显示一档跑另一档" ──
def test_panel_wires_the_rate_field_end_to_end():
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert 'id="stt_cloud_rate"' in html, "云 STT 采样率字段没建出来"
    assert '("stt_cloud_rate").value = S.stt.cloud' in html \
        or '$("#stt_cloud_rate").value = S.stt.cloud' in html, "字段不回读现有配置"
    body = [ln for ln in html.splitlines() if "sample_rate:(parseInt" in ln]
    assert body, "保存体没带 stt.cloud.sample_rate——任意一次保存都会把它抹回默认"
    assert "||0)" in body[0], "空输入必须落成 0（＝自动），不能落 undefined"
