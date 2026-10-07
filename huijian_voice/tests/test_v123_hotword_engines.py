"""v1.2.3 接入两档「解码期吃热词」的 STT：qwen3_asr / funasr_nano（用户 2026-10-07 点名）。

钉桩点（每格都对应一个会现场炸或现场骗人的形态）：
  ① 两档必须解析到**独立 lock 键**，且默认档仍是 sensevoice（这次接线不许翻转现网）；
  ② 两档都是 **OfflineRecognizer**，归离线集合（归错集合＝按流式收流调用，结果空串）；
  ③ 显式选它而构建失败**不得静默回落 Paraformer**（否则回落成绩被记成新档的成绩，
     用户的实测直接失效——与 firered_ctc 同判例）；
  ④ 构造参数必须落在本机 sherpa-onnx 的真实签名内，且 `hotwords` 只从
     `_hotwords_csv()` 来（别处硬编码词表＝两套口径漂移）；
  ⑤ 热词取数与级联改写层**同一个口**（`pipeline.prior_universe`），接线走真装配点；
  ⑥ lock 的 required_files 必须覆盖代码引用的每个文件名（导出方改名→缺文件报错，
     不是静默空串）；`default_provider=false` 让消费者（E2E 就绪门）不把 GB 级包拉进
     必检集；
  ⑦ Web 下拉与 `_STT_KINDS`、`_KIND_KEY` **双向**等集（加档忘改 UI、或 UI 有档引擎不认，
     两个方向都会静默回落默认档＝用户以为切了实际没切）。
"""
import json
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import asr as asr_mod                       # noqa: E402
from core.asr import AsrEngine, KEY_FN, KEY_PF, KEY_QA, KEY_SV  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
PCM = b"\x01\x00" * 8000      # 0.5s s16le@16k → 8000 采样
NEW_KINDS = ("qwen3_asr", "funasr_nano")
NEW_KEYS = {"qwen3_asr": "asr_qwen3_asr_06b", "funasr_nano": "asr_funasr_nano"}


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

    result = type("R", (), {"text": "打开展厅的悬窗"})()


class OffRec:
    """离线形态替身：只有 create_stream/decode_stream/reset，没有流式收流接口。"""

    def __init__(self):
        self.stream = None

    def create_stream(self):
        self.stream = OffStream()
        return self.stream

    def decode_stream(self, s):
        pass

    def reset(self, s):
        pass


def _engine(settings, store, kind, fail_kinds=()):
    eng = AsrEngine(settings, store)

    def fake_build(k, d):
        if k in fail_kinds:
            raise RuntimeError(f"boom {k}")
        rec = OffRec()
        rec._hj_kind = kind
        return rec

    eng._build_recognizer = fake_build
    return eng


# ── ① 键位与默认档 ────────────────────────────────────────────────
def test_hotword_kinds_have_own_lock_keys_and_default_untouched():
    for kind in NEW_KINDS:
        key = asr_mod._KIND_KEY.get(kind)
        assert key == NEW_KEYS[kind], f"{kind} 键名不符：{key}"
        assert asr_mod._KIND_LABEL.get(kind), f"{kind} 缺中文标签（加载日志要认得出是哪档）"
        assert kind in asr_mod._HOTWORD_KINDS, f"{kind} 未登记为吃热词档"
    assert len({KEY_SV, KEY_PF, KEY_QA, KEY_FN, asr_mod.KEY_FR}) == 5, "五档键位不得重叠"
    # 默认档判据要落在**真 DEFAULTS** 上：只喂空 S() 的话，这条钉对"把默认值改掉"
    # 这种变异完全不承重（实测假绿）。存量 settings.json 无该键时走的也是这个值。
    from core.settings import DEFAULTS
    assert DEFAULTS["stt"]["local_model"] == "sensevoice", "接线不得翻转默认档"
    assert AsrEngine(S(**{"stt.local_model": DEFAULTS["stt"]["local_model"]}),
                     Store()).model_key == KEY_SV
    assert AsrEngine(S(), Store()).model_key == KEY_SV


# ── ② 离线分派：整句一次喂入、不补尾静音 ─────────────────────────
@pytest.mark.parametrize("kind", NEW_KINDS)
def test_hotword_kinds_decode_offline_single_pass(kind):
    key = NEW_KEYS[kind]
    assert kind in asr_mod._OFFLINE_KINDS, f"{kind} 归错集合会走流式收流＝空串"
    eng = _engine(S(**{"stt.local_model": kind}), Store(ready_keys={key}), kind)
    assert eng.ensure_loaded() is True
    assert eng.loaded_kind() == kind
    assert eng._local_transcribe(PCM) == "打开展厅的悬窗"
    assert eng._rec.stream.n == 8000, "离线档不得补 1s 尾静音"


# ── ③ 失败不回落 ─────────────────────────────────────────────────
@pytest.mark.parametrize("kind", NEW_KINDS)
def test_hotword_build_failure_does_not_silently_fall_back(kind):
    key = NEW_KEYS[kind]
    store = Store(ready_keys={key, KEY_SV, KEY_PF})
    eng = _engine(S(**{"stt.local_model": kind}), store, kind, fail_kinds=(kind,))
    assert eng.ensure_loaded() is False, "所选档起不来必须如实失败"
    assert eng.loaded_kind() != "paraformer", "静默回落＝用户的引擎对比实测失效"
    assert kind in (eng.last_reason or "") and "加载失败" in eng.last_reason, \
        f"分因要点出所选那档：{eng.last_reason!r}"


# ── ④ 构造参数落进真实签名，且 hotwords 只从单口来 ────────────────
def _fake_sherpa(calls):
    def _mk(name):
        def f(**kw):
            calls.append((name, kw))
            return OffRec()
        return f

    return types.SimpleNamespace(
        OfflineRecognizer=types.SimpleNamespace(
            from_qwen3_asr=_mk("from_qwen3_asr"),
            from_funasr_nano=_mk("from_funasr_nano"),
            from_sense_voice=_mk("from_sense_voice"),
            from_fire_red_asr_ctc=_mk("from_fire_red_asr_ctc"),
        ),
        OnlineRecognizer=types.SimpleNamespace(
            from_paraformer=_mk("from_paraformer")),
    )


def _build_with_fake(kind, provider=None):
    calls = []
    old = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = _fake_sherpa(calls)
    try:
        eng = AsrEngine(S(), Store())
        if provider is not None:
            eng.set_hotwords_provider(provider)
        eng._build_recognizer(kind, Path("/models") / NEW_KEYS[kind])
    finally:
        if old is not None:
            sys.modules["sherpa_onnx"] = old
        else:
            sys.modules.pop("sherpa_onnx", None)
    assert calls, f"{kind} 分支没调用对应工厂函数"
    return calls[-1][1]


def test_qwen3_kwargs_and_hotwords_reach_constructor():
    kw = _build_with_fake("qwen3_asr", lambda: ("射灯", "悬窗", "展厅"))
    assert kw["hotwords"] == "射灯,悬窗,展厅", kw.get("hotwords")
    assert kw["num_threads"] == 2 and kw["provider"] == "cpu"
    assert kw["sample_rate"] == 16000, "上下行都钉死 16k"
    assert kw["encoder"].endswith("encoder.int8.onnx")
    assert kw["decoder"].endswith("decoder.int8.onnx")
    assert kw["conv_frontend"].endswith("conv_frontend.onnx")
    assert kw["tokenizer"].endswith("tokenizer")


def test_funasr_nano_kwargs_and_hotwords_reach_constructor():
    kw = _build_with_fake("funasr_nano", lambda: ("射灯", "悬窗"))
    assert kw["hotwords"] == "射灯,悬窗", kw.get("hotwords")
    assert kw["num_threads"] == 2 and kw["provider"] == "cpu"
    assert kw["encoder_adaptor"].endswith("encoder_adaptor.int8.onnx")
    assert kw["llm"].endswith("llm.int8.onnx")
    assert kw["embedding"].endswith("embedding.int8.onnx")
    assert kw["tokenizer"].endswith("Qwen3-0.6B"), "tokenizer 目录名由导出包决定，须与 required_files 同"


def test_hotwords_empty_when_provider_missing_or_empty():
    """没挂取数口 / 清单拿不到 / 取数口抛异常 ⇒ hotwords=""＝不喂偏置，行为与 v1.2.2 逐值相同。

    第三条必须是**可读失败**而不是 error：偏置词表面是加分项，它塌了不能把识别路径打穿。
    """
    assert _build_with_fake("qwen3_asr")["hotwords"] == ""
    assert _build_with_fake("funasr_nano", lambda: ())["hotwords"] == ""

    def boom():
        raise RuntimeError("ha 没起来")
    try:
        got = _build_with_fake("qwen3_asr", boom)["hotwords"]
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"取数口异常打穿了识别路径（兜底被摘？）：{type(e).__name__}: {e}")
    assert got == ""


def test_hotwords_csv_semantics_dedupe_order_and_comma_guard():
    """含逗号的名字整条丢弃：上游格式是 `"a,b,c"`，名字带逗号会把一台设备切成两个热词。"""
    eng = AsrEngine(S(), Store())
    eng.set_hotwords_provider(lambda: ("射灯", " 射灯 ", "悬窗", None, "", "A,B", 12))
    assert eng._hotwords_csv() == "射灯,悬窗,12", eng._hotwords_csv()


def test_hotwords_only_via_single_door():
    src = (HERE / "core" / "asr.py").read_text(encoding="utf-8")
    body = src[src.index("def _build_recognizer("):src.index("def _load_one(")]
    assert body.count("hw = self._hotwords_csv()") == 1
    assert len(re.findall(r"hotwords=", body)) == 2, "两档各传一次，且值只能来自 _hotwords_csv()"
    for m in re.finditer(r"hotwords=([^,\n\)]+)", body):
        assert m.group(1).strip() == "hw", f"hotwords 实参不是单口取的值：{m.group(0)}"


# ── ⑤ 与改写层同源、接线走真装配点 ───────────────────────────────
def test_hotword_source_is_the_rewrite_prior_universe():
    main_src = (HERE / "core" / "main.py").read_text(encoding="utf-8")
    assert main_src.count("set_hotwords_provider(self.pipeline.prior_universe)") == 1, \
        "偏置词表必须挂在装配点上，且只挂一次"
    pipe_src = (HERE / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert pipe_src.count("def prior_universe(") == 1
    # 改写层与偏置面同口：改写调用里只能看到 prior_universe()
    assert "homophone.rewrite(text, self.prior_universe())" in pipe_src


# ── ⑥ lock 条目 ─────────────────────────────────────────────────
def _lock():
    return json.loads((HERE / "models.lock.json").read_text(encoding="utf-8"))


def test_lock_entries_shape_and_non_default():
    lock = _lock()
    for kind in NEW_KINDS:
        key = NEW_KEYS[kind]
        e = lock[key]
        assert e["default_provider"] is False, \
            "默认 true 会把 GB 级第三方包拉进 E2E 就绪门的必检集"
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), "sha256 必须是实测 64 位小写"
        assert e["urls"][0].startswith("https://gh-proxy.com/")
        assert e["urls"][1] == e["urls"][0].replace("https://gh-proxy.com/", "", 1)
        assert e["tarball"] == f"{e['top_dir']}.tar.bz2"
        assert e["size_mb"] > 0


def test_lock_required_files_cover_every_name_the_code_opens():
    """代码实际打开的每个文件都必须被 required_files 守住：导出方改名/拆包时现场是
    "缺文件→加载失败分因"，而不是静默空串。
    判据从**真构造调用**里取（替身录下的 kwargs 路径），不靠源码切片——切片正则会被
    相邻分支串味，且保不住"形状"以外的东西。"""
    lock = _lock()
    not_paths = {"provider", "hotwords"}
    for kind in NEW_KINDS:
        kw = _build_with_fake(kind, lambda: ("射灯",))
        opened = {Path(v).name for k, v in kw.items()
                  if isinstance(v, str) and k not in not_paths}
        assert opened, f"{kind} 没录到任何模型路径——这条钉在空转"
        req = set(lock[NEW_KEYS[kind]]["required_files"])
        missing = opened - req
        assert not missing, f"{kind} 代码打开的文件未登记进 required_files：{sorted(missing)}"
        # 每条路径都必须落在该档自己的 lock 键目录下（写错键＝读到别的档的文件）
        dirs = {Path(v).parent.name for k, v in kw.items()
                if isinstance(v, str) and k not in not_paths}
        assert dirs == {NEW_KEYS[kind]}, f"{kind} 取数目录不是自己的 lock 键：{sorted(dirs)}"


# ── ⑦ UI 与引擎三处等集 ─────────────────────────────────────────
def test_www_options_and_kind_list_match_engine_exactly():
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    sel = re.search(r'<select id="stt_local_model">(.*?)</select>', html, re.S)
    assert sel, "找不到本地模型下拉框"
    opts = set(re.findall(r'<option value="([^"]+)"', sel.group(1)))
    wl = re.search(r'const _STT_KINDS = \[([^\]]*)\]', html)
    assert wl, "找不到 loadSettings 的白名单（回退成二选一三元式＝切了显示没切）"
    kinds = set(re.findall(r'"([a-z0-9_]+)"', wl.group(1)))
    assert opts == kinds == set(asr_mod._KIND_KEY), \
        f"UI 选项/回填白名单/引擎键位必须同集合：opts={sorted(opts)} kinds={sorted(kinds)} " \
        f"engine={sorted(asr_mod._KIND_KEY)}"


# ── ⑧ 版本宇宙事实：到底哪些工厂函数吃 hotwords ─────────────────
def test_only_hotword_capable_arms_receive_hotwords_in_this_build():
    """把『谁吃热词』钉在 sherpa-onnx 的真实签名上：
    给不吃热词的档传 hotwords 是 TypeError（现场＝模型加载失败，不是没生效）。"""
    pytest.importorskip("sherpa_onnx")
    import inspect

    import sherpa_onnx as so
    off = so.OfflineRecognizer
    assert "hotwords" in inspect.signature(off.from_qwen3_asr).parameters
    assert "hotwords" in inspect.signature(off.from_funasr_nano).parameters
    for fn, name in ((off.from_sense_voice, "from_sense_voice"),
                     (off.from_whisper, "from_whisper"),
                     (so.OnlineRecognizer.from_paraformer, "online.from_paraformer")):
        assert "hotwords" not in inspect.signature(fn).parameters, \
            f"{name} 现在有了 hotwords——本文件的『唯二』口径要重核，别继续按老结论接线"
