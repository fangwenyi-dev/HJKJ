"""v1.2.5 接入 FireRedASR2-AED（用户 2026-10-07 点名下载并放进加载项）。

为什么这一档的钉比一般多：它与**现役默认档同源权重**、又与 **Paraformer 回落档逐字同名**
（encoder.int8.onnx / decoder.int8.onnx / tokens.txt）。分派错不报错，只会拿错模型跑错声学
——这类"错但静默"正是本仓反复记过的形态，所以每格都要有反向判据。

  ① 键位/标签/离线集合齐，且**非默认**（不进 E2E 就绪门必检集，否则新装首启多下 800MB）；
  ② 构造参数落在 1.13.7 真签名内：`from_fire_red_asr` **没有 sample_rate**（照 SenseVoice
     传＝TypeError→现场"模型加载失败"），也**没有 hotwords**（别以为换了档就有偏置）；
  ③ 取数目录必须是它自己的 lock 键（与 Paraformer 同文件名 ⇒ 目录错＝静默拿错模型）；
  ④ 显式选它而构建失败**不回落**（对比档被顶替＝实测作废，与 firered_ctc 当年同判例）；
  ⑤ lock 的 required_files 覆盖代码实际打开的每个文件（导出方改名→缺文件报错，不是空串）；
  ⑥ UI 选项 / 回填白名单 / 引擎键位三处等集。
"""
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import asr as asr_mod                       # noqa: E402
from core.asr import (AsrEngine, KEY_FA, KEY_FR, KEY_PF, KEY_SV)  # noqa: E402

HERE = Path(__file__).resolve().parents[1]


class S:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, default=None):
        return self.d.get(k, default)


class Store:
    def __init__(self, ready_keys=()):
        self.ready = set(ready_keys)
        self.base = Path("/models")
        self.async_calls = []

    def model_dir_for(self, key):
        return self.base / key if key in self.ready else None

    def ensure_async(self, key, force=False):
        self.async_calls.append(key)


class OffStream:
    def __init__(self):
        self.n = 0

    def accept_waveform(self, rate, data):
        self.n += len(data)

    result = type("R", (), {"text": "把客厅的灯打开"})()


class OffRec:
    def __init__(self):
        self.stream = None

    def create_stream(self):
        self.stream = OffStream()
        return self.stream

    def decode_stream(self, s):
        pass

    def reset(self, s):
        pass


def _fake_sherpa(calls):
    def _mk(name):
        def f(**kw):
            calls.append((name, kw))
            return OffRec()
        return f
    return types.SimpleNamespace(
        OfflineRecognizer=types.SimpleNamespace(
            from_fire_red_asr=_mk("from_fire_red_asr"),
            from_fire_red_asr_ctc=_mk("from_fire_red_asr_ctc"),
            from_sense_voice=_mk("from_sense_voice")),
        OnlineRecognizer=types.SimpleNamespace(
            from_paraformer=_mk("from_paraformer")))


def _kwargs(kind):
    calls = []
    old = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = _fake_sherpa(calls)
    try:
        AsrEngine(S(), Store())._build_recognizer(kind, Path("/models") / asr_mod._KIND_KEY[kind])
    finally:
        if old is not None:
            sys.modules["sherpa_onnx"] = old
        else:
            sys.modules.pop("sherpa_onnx", None)
    assert calls, f"{kind} 分支没调用工厂函数"
    return calls[-1][0], calls[-1][1]


# ──  键位与非默认 ─────────────────────────────────────────────
def test_aed_registered_and_not_default():
    assert asr_mod._KIND_KEY["firered_aed"] == KEY_FA == "asr_firered_aed"
    assert asr_mod._KIND_LABEL.get("firered_aed") == "FireRedASR2-AED"
    assert len({KEY_SV, KEY_PF, KEY_FR, KEY_FA}) == 4, "四档键位不得重叠"
    assert "firered_aed" in asr_mod._OFFLINE_KINDS, "AED 是 OfflineRecognizer，归错集合＝空串"
    eng = AsrEngine(S(**{"stt.local_model": "firered_aed"}), Store())
    assert eng.model_key == KEY_FA
    assert AsrEngine(S(), Store()).model_key == KEY_FR, "接 AED 不得动默认档（默认仍是 CTC）"


# ── ② 构造参数形状 ─────────────────────────────────────────────
def test_aed_kwargs_match_real_signature():
    name, kw = _kwargs("firered_aed")
    assert name == "from_fire_red_asr"
    assert kw["num_threads"] == 2 and kw["provider"] == "cpu"
    assert "sample_rate" not in kw, "该工厂函数没有 sample_rate 形参，传了是 TypeError"
    assert "hotwords" not in kw, "AED 档不吃热词（1.13.7 实测 12 参里没有）"
    assert kw["encoder"].endswith("encoder.int8.onnx")
    assert kw["decoder"].endswith("decoder.int8.onnx")
    assert kw["tokens"].endswith("tokens.txt")
    pytest.importorskip("sherpa_onnx")
    import sherpa_onnx as so
    real = inspect.signature(so.OfflineRecognizer.from_fire_red_asr).parameters
    assert set(kw) <= set(real), f"传了签名外的参数：{sorted(set(kw) - set(real))}"


def _paths(kw):
    """只取"像路径"的实参——provider/hotwords 也是字符串，混进来会把 'cpu' 当文件。"""
    not_paths = {"provider", "hotwords", "decoding_method"}
    return {Path(v) for k, v in kw.items() if isinstance(v, str) and k not in not_paths}


def test_aed_reads_only_its_own_lock_dir():
    """与 Paraformer 逐字同名 ⇒ 目录是唯一隔离面，必须钉死。"""
    _, kw = _kwargs("firered_aed")
    dirs = {p.parent.name for p in _paths(kw)}
    assert dirs == {KEY_FA}, dirs
    _, kw_pf = _kwargs("paraformer")
    same = {p.name for p in _paths(kw_pf)} & {p.name for p in _paths(kw)}
    assert same, "前提变了（两档不再同名）→ 本钉失去对象，删掉它别留假承重"


# ── ③④ 离线分派与不回落 ────────────────────────────────────────
def _engine(settings, store, fail_kinds=()):
    eng = AsrEngine(settings, store)

    def fake_build(kind, d):
        if kind in fail_kinds:
            raise RuntimeError(f"boom {kind}")
        rec = OffRec()
        rec._hj_kind = kind
        return rec
    eng._build_recognizer = fake_build
    return eng


def test_aed_decodes_offline_single_pass():
    eng = _engine(S(**{"stt.local_model": "firered_aed"}), Store(ready_keys={KEY_FA}))
    assert eng.ensure_loaded() is True
    assert eng._local_transcribe(b"\x01\x00" * 8000) == "把客厅的灯打开"
    assert eng._rec.stream.n == 8000, "离线档不得补 1s 尾静音"


def test_aed_build_failure_does_not_fall_back():
    """AED 既不是默认档也不在回落位 ⇒ 起不来就如实失败，不许被别的档顶替。"""
    store = Store(ready_keys={KEY_FA, KEY_FR, KEY_SV, KEY_PF})
    eng = _engine(S(**{"stt.local_model": "firered_aed"}), store, fail_kinds=("firered_aed",))
    assert eng.ensure_loaded() is False, "对比档失败必须如实失败"
    assert eng.loaded_kind() == ""
    assert store.async_calls == [], "回落链不该被对比档触发"
    assert "firered_aed" in (eng.last_reason or ""), eng.last_reason


# ── ⑤ lock 条目 ────────────────────────────────────────────────
def test_lock_entry_aed_measured_non_default_and_covers_opened_files():
    lock = json.loads((HERE / "models.lock.json").read_text(encoding="utf-8"))
    e = lock[KEY_FA]
    assert e["default_provider"] is False, \
        "进必检集＝新装首启多下 800MB，且生产侧根本不会下它（v1.2.4 E2E 超时同型坑）"
    assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
    assert e["sha256"] == "43015b3f1643a5688b4821e8ed323473d38b798c4ec291471fe00df1bcfc4f1c", \
        "sha256 是 2026-10-07 本机 gh-proxy 直下实测值，改它必须重新实测"
    assert e["tarball"] == f"{e['top_dir']}.tar.bz2"
    assert e["urls"][0].startswith("https://gh-proxy.com/")
    assert e["size_mb"] == 800, "tar 实测 838,589,068B ⇒ 下载字节闸按它算"
    _, kw = _kwargs("firered_aed")
    opened = {p.name for p in _paths(kw)}
    assert opened <= set(e["required_files"]), \
        f"代码打开的文件未登记：{sorted(opened - set(e['required_files']))}"


# ── ⑥ UI 三处等集 ──────────────────────────────────────────────
def test_www_options_and_kind_list_match_engine():
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    sel = re.search(r'<select id="stt_local_model">(.*?)</select>', html, re.S)
    opts = set(re.findall(r'<option value="([^"]+)"', sel.group(1)))
    wl = re.search(r'const _STT_KINDS = \[([^\]]*)\]', html)
    kinds = set(re.findall(r'"([a-z0-9_]+)"', wl.group(1)))
    assert opts == kinds == set(asr_mod._KIND_KEY), \
        f"opts={sorted(opts)} kinds={sorted(kinds)} engine={sorted(asr_mod._KIND_KEY)}"
