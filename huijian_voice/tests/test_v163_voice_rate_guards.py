# -*- coding: utf-8 -*-
"""v1.6.3 两刀（TTS 嗓音八度降调事故根修批）· 红基线钉。

现场（2026-10-17 现网取证，频谱+时域双实锤）：面板选 Matcha（官方单女声包，本机同包
双版本库实测 F0 302Hz/1.78s 女声），现网合成口同一句实测 **F0 148Hz、时长 ×2.02、
>4.1kHz 能量占比按出货判据形制复测 25–30%（本机对照 0.5–0.9%、melo 真重采样链
0.6%；早期取证记 7.2%＝另一测法）**——三比值互相咬合
唯一解＝32k 音频被当 16k 播＝被降整整一个八度。女声被拉成"男声"。

根因通路（①组钉死）：`model_store.is_ready/model_dir_for` 对 `extra_files`（vocos
声码器）**只查存在、从不查哈希**；哈希校验只活在 `_ensure_extra_files`，而它只在
ensure() 真走到时才被调——stamp 在 ⇒ `_ensure_locked` 首行 is_ready 即真直接 return。
**一份文件名正确、字节陈旧的 vocos（早期手动投放/换包窗口遗留）可永久隐身**。
修复＝启动期显式复核（verify_extras）：坏字节且**确有重取来源**（import/ 或
auto_download 开）→ 摘除交既有 pend→ensure 链重取；无重取来源（离线+关自动下载）
→ 只点名不销毁（M11 家规：判据动作不得把现场弄得更糟——删了唯一副本=从"音高不对"
恶化成"彻底哑"）。

②组＝热路径纪律 + 合成金句自测率（频谱哨兵）：
· is_ready 高频面（5s 状态循环/healthz/每次加载判）不许背哈希——复核是显式调用；
· 引擎加载后合成固定金句量 >4.1kHz 占比，超阈 WARN 点名——这病从"用户耳朵发现"
  变"加载日志必留痕"。阈值 2%＝真值标定（出货判据形制复测：归档坏样 25–30% / 好 matcha 0.5–0.9% / melo 真链路
  0.6%，安全垫 3.3 倍；kokoro 未标定，哨兵只 WARN 不改行为，误报代价=一行日志）。
"""
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from core.model_store import ModelStore               # noqa: E402


class _S:
    def __init__(self, d=None):
        self.d = {"power.auto_download": True}
        self.d.update(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


GOOD = b"GOOD-VOCODER-BYTES-0123456789"
BAD = b"BAD-STALE-VOCOS-0000"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _mk(tmp_path, *, vocos=GOOD, size_mb=None, drop=False):
    """造"主包就绪＋附属 vocos 在位"的隔离 ModelStore（真 lock 结构缩微版）。"""
    models = tmp_path / "models"
    (models / "tts_x").mkdir(parents=True)
    lock = tmp_path / "models.lock.json"
    ef = {"file": "vocos-16khz-univ.onnx", "sha256": _sha(GOOD), "urls": ["u://x"]}
    if size_mb is not None:
        ef["size_mb"] = size_mb
    lock.write_text(json.dumps({
        "tts_x": {
            "tarball": "tts_x.tar.bz2", "sha256": "deadbeef",
            "top_dir": "", "required_files": ["model.onnx", "vocos-16khz-univ.onnx"],
            "urls": [], "extra_files": [ef],
        }}), encoding="utf-8")
    d = models / "tts_x"
    (d / "model.onnx").write_bytes(b"x")
    if not drop:
        (d / "vocos-16khz-univ.onnx").write_bytes(vocos)
    (d / ".extracted_ok").write_text("tts_x.tar.bz2|deadbeef", encoding="utf-8")
    return models, lock, d


# ── ① 红基线：陈旧字节附属必须判不就绪（启动复核摘除） ───────────────
def test_stale_extra_file_must_not_pass_readiness(tmp_path, monkeypatch):
    models, lock, d = _mk(tmp_path, vocos=BAD)     # 名字对、字节错（八度案现场形制）
    # v1.6.6 刀Y 随迁：本条钉的是"在线现场坏字节必须摘掉重取"，所以要模拟探活通过。
    # 必须**类级**patch——ModelStore 的 ctor 自己就跑启动复核，实例级 patch 晚了一步
    # （第一版栽在这：构造时按真 URL 探网失败 ⇒ 副本被保留 ⇒ is_ready 仍 True）。
    # 不摘的那一半由 test_unreachable_source_keeps_the_only_copy 钉，两侧合起来才
    # 是 M11「不得把现场弄得更糟」。
    monkeypatch.setattr(ModelStore, "_net_reachable", lambda self, ef: True)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    # 修复前的洞：stamp+存在性全过 ⇒ 判就绪（本断言在未接复核的树上必红）
    assert st.is_ready("tts_x") is False, (
        "陈旧字节的 vocos 被就绪判定放行＝matcha 八度降调案的通路还在")


def test_no_refetch_source_keeps_file_but_flags(tmp_path):
    """离线+关自动下载：无重取来源时**不许销毁唯一副本**（M11 家规），只点名。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False, "坏字节必须被点名"
    assert (d / "vocos-16khz-univ.onnx").exists(), \
        "无重取来源时销毁唯一副本＝把'音高不对'恶化成'彻底哑'（违 M11 家规）"


def test_import_source_allows_removal(tmp_path):
    """import/ 放了好包 ⇒ 坏字节必须被摘除（重取路径存在，安全）。
    离线（auto_download 关）时 import/ 就是唯一来源，摘除是安全的；若开
    auto_download 则 urls 也是来源（_mk 默认配了 "u://x"）。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "status.json")
    (models / "import").mkdir(exist_ok=True)
    (st.import_dir / "vocos-16khz-univ.onnx").write_bytes(GOOD)
    assert st.verify_extras("tts_x") is False
    assert not (d / "vocos-16khz-univ.onnx").exists(), "有重取来源却不摘除＝坏字节永久隐身"
    # 走 ensure 后 import/ 好包顶上（端到端自愈闭环）
    assert st.ensure("tts_x") is True
    assert (d / "vocos-16khz-univ.onnx").read_bytes() == GOOD


def test_verify_extras_good_untouched(tmp_path):
    models, lock, d = _mk(tmp_path)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is True
    assert (d / "vocos-16khz-univ.onnx").read_bytes() == GOOD   # 好字节不许被动


def test_verify_extras_size_fast_path(tmp_path):
    """lock 带 size_mb 时尺寸不符即坏（免读 54MB 的快判）。"""
    models, lock, d = _mk(tmp_path, vocos=BAD, size_mb=54)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False


def test_verify_extras_missing_extra_flagged(tmp_path):
    """附属干脆缺失（半截投放）：复核也必须点名。"""
    models, lock, d = _mk(tmp_path, drop=True)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False


# ── ② 热路径纪律：is_ready 不许背哈希（5s 循环/healthz 高频调用面） ──
def test_is_ready_stays_cheap_no_hash_on_hot_path(tmp_path, monkeypatch):
    models, lock, d = _mk(tmp_path)                 # 好 vocos
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    calls = {"n": 0}
    real = ModelStore._sha_ok

    def spy(path, expected):
        calls["n"] += 1
        return real(path, expected)

    monkeypatch.setattr(ModelStore, "_sha_ok", staticmethod(spy))
    assert st.is_ready("tts_x") is True
    assert st.model_dir_for("tts_x") is not None
    assert calls["n"] == 0, "is_ready 热路径混进了哈希（5s 状态循环会烧穿数据盘）"


# ── ③ 合成金句自测率（频谱哨兵）：函数体 + 接线 + 端到端 ─────────────
def _voiced(n=1.5, sr=16000, f0=150.0, harmonics=12, amp=1.0):
    t = np.arange(int(n * sr)) / sr
    return sum(amp * np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, harmonics + 1))


def _good_samples(sr=16000):
    """正常 16k 语音代理品：谐波列只到 1.8kHz，4k 上无内容。"""
    return _voiced(1.5, sr), sr


def _octave_drop_samples(sr=16000):
    """八度降调损坏代理品（取证形制）：32k 语音（谐波到 3.6k）+ 擦音带（10-15kHz
    @32k 标度），错标 16k 播出＝整体折半＋擦音折进 5-7.5kHz。

    量具修正（10-17，刀5 量具假红家族）：旧形用 白噪×|sin(6200t)|⁴ 调制包络
    "造带"，调制谱近全带平坦——能量并未真正集中到 10-15kHz，折半后 >4.1k
    占比仅 ~0.5%，代理品不忠实导致两枚哨兵钉假红。现改 FFT 真带通把擦音
    能量集中进 10-15kHz@32k（＝折半后 5-7.5kHz），与现网坏样的频谱
    形态同构。判据与阈值一字未动。"""
    n = 2500                                    # 32k 域样本数（=16k 播放 5000 样本）
    t = np.arange(n) / 32000.0
    voiced = sum(np.sin(2 * np.pi * 300 * k * t) / k for k in range(1, 13))
    voiced = voiced / max(1e-12, np.sqrt((voiced ** 2).mean()))
    rng = np.random.default_rng(7)
    w = rng.standard_normal(n)
    sp = np.fft.rfft(w)
    f = np.fft.rfftfreq(n, d=1.0 / 32000.0)
    sp[~((f >= 10000) & (f <= 15000))] = 0.0    # 真带通 10-15kHz@32k
    fric = np.fft.irfft(sp, n)
    fric = fric / max(1e-12, np.sqrt((fric ** 2).mean()))
    x = voiced + 0.55 * fric                    # 擦音能量占比 ~23% ⇒ 折半后稳超阈
    return np.repeat(x, 2)[: 2 * n], sr         # 每样本重复 2 次＝按 16k 标称吐出


def test_voice_sentinel_detects_octave_drop():
    from core.tts import _hf_ratio_excess
    rg = _hf_ratio_excess(*_good_samples())
    rb = _hf_ratio_excess(*_octave_drop_samples())
    assert rg is not None and rg < 0.01, f"正常 16k 谐波列被误判（ratio={rg}）——阈值过紧会天天误报"
    assert rb is not None and rb >= 0.02, f"半速率降调品没被抓（ratio={rb}）——归档坏样本形制实测 25–30%"
    assert _hf_ratio_excess(np.zeros(8000), 16000) is None       # 全静音不装判
    assert _hf_ratio_excess(np.zeros(100), 16000) is None        # 样本不足 None


def test_voice_sentinel_wired_after_load():
    """接线钉（v1.54 教训：只加函数不接线＝没修）。

    调用点必须在 `ensure_loaded` 的 `ok` 分支（锁外）——`_synth`/generate 要拿引擎
    `_lock`，而 `_ensure_loaded_inner` 的加载体在 `with self._lock:` 里，放里面＝自死锁。"""
    import inspect
    from core.tts import TtsEngine
    src = inspect.getsource(TtsEngine.ensure_loaded)
    assert "self._voice_selftest()" in src, "引擎加载成功路径（锁外收束点）未接自测率"
    inner = inspect.getsource(TtsEngine._ensure_loaded_inner)
    assert "with self._lock:" in inner and "_voice_selftest" not in inner, \
        "自测不得放进 _ensure_loaded_inner 的持锁段（generate 重入 _lock＝死锁）"
    st = inspect.getsource(TtsEngine._self_test_voice)
    assert "except" in st, "哨兵本体必须永不抛（它挂了不能连累播报）"
    wt = inspect.getsource(TtsEngine._voice_selftest)
    assert "_selftest_at" in wt, "自测必须有每 provider 时间预算（省电档反复重载不重复烧合成）"


def test_voice_sentinel_warns_on_live_engine():
    """端到端（假引擎替身，喂降调品）：WARN 必须点名，且不抛。"""
    import logging
    from core.tts import TtsEngine
    x, sr = _octave_drop_samples()

    class _Audio:
        samples = (x / max(1.0, np.abs(x).max()) * 0.5).astype(np.float32)
        sample_rate = sr

    class _Tts:
        def generate(self, text, sid=0, speed=1.0):
            return _Audio()

    e = TtsEngine.__new__(TtsEngine)
    e._tts = _Tts()
    # v1.6.6 刀2：夹具补生产同款并发件——旧夹具没有 _lock/_gen_lock/_busy，是因为
    # 哨兵当年裸调 generate 根本不碰它们；现在走受锁路径，夹具缺件＝AttributeError
    # 被外层 except 吞掉 ⇒ 本条测试会在"没点名"的情况下误判（假绿的新形态）。
    e._lock = threading.Lock()
    e._gen_lock = threading.Lock()
    e._busy = 0
    e.settings = _S({"tts.provider": "local_matcha", "tts.sid": 0, "tts.speed": 1.05})
    e._loaded_prov = "local_matcha"
    warns = []

    class _H(logging.Handler):
        def emit(self, rec):
            warns.append(rec.getMessage())

    logger = logging.getLogger("huijian.tts")
    h = _H()
    logger.addHandler(h)
    try:
        e._self_test_voice()          # 永不抛
    finally:
        logger.removeHandler(h)
    assert any(("八度" in m or "半速率" in m or "4.1kHz" in m) for m in warns), \
        f"降调引擎加载后哨兵未点名 WARN：{warns}"


# ══ v1.6.6 刀2：哨兵必须走与生产同一把并发闸（行为钉，不是字面钉）══════
def _eng(tts_obj, busy=0):
    from core.tts import TtsEngine
    e = TtsEngine.__new__(TtsEngine)
    e._tts = tts_obj
    e._lock = threading.Lock()
    e._gen_lock = threading.Lock()
    e._busy = busy
    e.settings = _S({})
    return e


class _GoodAudio:
    samples = property(lambda self: __import__("numpy").zeros(1600, dtype="float32"))
    sample_rate = 16000


def test_selftest_generate_holds_gen_lock_and_counts_busy():
    """核心指控：旧形 `self._tts.generate(...)` 裸调——同代并发 generate 本仓明载
    "重则 C++ 层崩溃打死整个容器"，而 try/except 挡不住 C++ abort；且不计 _busy ⇒
    省电档 reaper 会在自测在飞时卸掉当代引擎。本钉在 generate 内部**当场取证**：
    锁必须已被本线程持有、_busy 必须已计入。"""
    seen = {}

    class _Tts:
        def generate(self, text, sid=0, speed=1.0):
            # 锁此刻不可再次获取＝本线程正持有（生产口径）
            seen["lock_held"] = not e._gen_lock.acquire(blocking=False)
            if not seen["lock_held"]:
                e._gen_lock.release()
            seen["busy"] = e._busy
            return _GoodAudio()

    e = _eng(_Tts())
    got = e.generate_for_selftest("金句", sid=0, speed=1.05)
    assert got is not None
    assert seen.get("lock_held") is True, "自测合成没持 _gen_lock＝与播报并发，C++ 崩面 reopen"
    assert seen.get("busy", 0) >= 1, "自测没计 _busy＝卸载避让对它失明（F1 跨代析构）"
    assert e._busy == 0, "收束后必须归还计数（否则永不被卸载=内存钉死）"
    assert e._gen_lock.acquire(blocking=False), "收束后必须释放锁"


def test_selftest_yields_rather_than_stalling_announce(monkeypatch):
    """播报在飞时自测**让路**：等锁上限必须远短于播报侧 _GEN_WAIT_S，且拿不到锁
    ＝本轮不判（返回 None），绝不把"没测"记成"测过且没问题"。"""
    from core import tts as ttsmod
    monkeypatch.setattr(ttsmod, "_SELFTEST_GEN_WAIT_S", 0.05)
    monkeypatch.setattr(ttsmod, "_GEN_WAIT_S", 30.0)
    e = _eng(object())
    assert e._gen_lock.acquire(blocking=True)      # 模拟前手播报占锁
    t0 = time.monotonic()
    try:
        assert e.generate_for_selftest("金句", sid=0, speed=1.0) is None
    finally:
        e._gen_lock.release()
    waited = time.monotonic() - t0
    assert waited < 1.0, f"自测陪等播报锁 {waited:.2f}s＝诊断件给播报链加了尾延"
    assert e._busy == 0


def test_selftest_skipped_does_not_burn_provider_budget(monkeypatch):
    """判不成⇒不退预算（旧形先记账后判：一次锁忙＝此后 1h 不再自测，把自己盲掉）。"""
    from core import tts as ttsmod
    monkeypatch.setattr(ttsmod, "_SELFTEST_GEN_WAIT_S", 0.01)
    e = _eng(object())
    e._selftest_at = {}
    e.active_provider = lambda: "local_matcha"
    e._gen_lock.acquire(blocking=True)             # 锁被占＝本轮判不成
    try:
        e._voice_selftest()
    finally:
        e._gen_lock.release()
    assert e._selftest_at.get("local_matcha") is None, \
        "没判成却烧了预算＝接下来 1h 哨兵失明（诊断件的谎报形态）"
    # 反向：真判成必须记账（省电档反复重载不许反复烧合成）
    e._selftest_at = {}
    monkeypatch.setattr(e, "_self_test_voice", lambda: True)
    e._voice_selftest()
    assert "local_matcha" in e._selftest_at, "判成了不记账＝每次重载都重烧一次合成"


def test_selftest_no_bare_generate_call_left():
    """反向结构钉：判据异常/裸调都不得回到 _self_test_voice 里（单点=受锁那条）。"""
    import inspect
    from core.tts import TtsEngine
    body = inspect.getsource(TtsEngine._self_test_voice)
    assert "self._tts.generate(" not in body, "自测里又出现裸 generate＝并发闸被绕过"
    assert "generate_for_selftest(" in body, "自测没接在受锁单点上"
    guarded = inspect.getsource(TtsEngine.generate_for_selftest)
    assert "_gen_lock.acquire" in guarded and "_busy += 1" in guarded, \
        "受锁单点自身缺任一半（锁或计数）都等于没修"


# ══ v1.6.6 刀Y：配置有源 ≠ 现场真取得到（摘唯一副本前先探活）════════
def test_unreachable_source_keeps_the_only_copy(tmp_path, monkeypatch):
    """现网形制：自动下载开着（配置有源）而此刻断网/DNS 不通。旧形照样摘掉在场
    唯一副本，随后重取全失败 ⇒ 从"音高不对"恶化成"彻底哑"（M11 禁止的自毁）。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)
    monkeypatch.setattr(ModelStore, "_probe_host", lambda self, host, port: False)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False, "坏字节仍须点名"
    assert (d / "vocos-16khz-univ.onnx").exists(), \
        "够不到替代件却照摘＝把唯一逃生通道砸了"


def test_reachable_source_removes_for_refetch(tmp_path, monkeypatch):
    """对照面：探活通过 ⇒ 仍按 1.6.3 的设计摘除让路给重取（新门不得变成永不修）。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)
    monkeypatch.setattr(ModelStore, "_net_reachable", lambda self, ef: True)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False
    assert not (d / "vocos-16khz-univ.onnx").exists(), "在线现场坏字节必须被摘除重取"


def test_local_rescue_skips_the_probe(tmp_path, monkeypatch):
    """import/ 有好包＝本地就有来源，不必探网（离线自救这条路不能被新门挡回去）。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "status.json")
    (models / "import").mkdir(exist_ok=True)
    (st.import_dir / "vocos-16khz-univ.onnx").write_bytes(GOOD)
    calls = []
    monkeypatch.setattr(st, "_probe_host",
                        lambda host, port: calls.append(host) or False)
    assert st.verify_extras("tts_x") is False
    assert not (d / "vocos-16khz-univ.onnx").exists(), "有救急包时不摘＝坏字节永久隐身"
    assert calls == [], "本地来源齐了还去探网＝给启动白加一次外网连接"


def test_probe_runs_once_per_startup(tmp_path, monkeypatch):
    """一次启动只探一轮：多附属/多 key 共用一个结果（探活跑在 ctor 同步路径上）。

    ctor 自己就跑了复核并把结果缓存，所以先把缓存清成"未探"再验缓存语义——
    否则本钉测的是"缓存已被填过"，不是"会缓存"。
    """
    models, lock, d = _mk(tmp_path, vocos=BAD)
    calls = []
    monkeypatch.setattr(ModelStore, "_probe_host",
                        lambda self, host, port: calls.append(host) or False)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    st._net_ok = None
    before = len(calls)          # ctor 已消耗一轮（host 'x'），本钉要核的是"再探也只一轮"
    ef = {"urls": ["https://a.example/x.bin", "https://b.example/x.bin"]}
    assert st._net_reachable(ef) is False
    assert st._net_reachable(ef) is False
    assert st._net_reachable(ef) is False
    assert len(calls) - before == 2, \
        f"探测次数失控（三个 url 该探到 b 即停、且整体只一轮）：{calls[before:]}"


def test_probe_error_counts_as_unreachable(tmp_path, monkeypatch):
    """判据件不得成为新故障面：探测抛异常＝不可达（保守＝不摘），且 verify 不抛。"""
    models, lock, d = _mk(tmp_path, vocos=BAD)

    def _boom(self, host, port):
        raise RuntimeError("探测件坏了")

    monkeypatch.setattr(ModelStore, "_probe_host", _boom)
    st = ModelStore(_S(), lock_path=lock, models_dir=models,
                    status_file=tmp_path / "status.json")
    assert st.verify_extras("tts_x") is False
    assert (d / "vocos-16khz-univ.onnx").exists()


def test_probe_timeout_is_bounded_and_wired():
    """接线与预算钉：探活必须有短上限，且真被摘除分支消费（只加常量不接线＝空转）。"""
    import inspect
    from core import model_store as ms
    assert ms._NET_PROBE_TIMEOUT_S <= 2.0, \
        f"探活上限 {ms._NET_PROBE_TIMEOUT_S}s 太长——它跑在启动同步路径上"
    body = inspect.getsource(ms.ModelStore.verify_extras)
    assert "_net_reachable" in body and "local_rescue" in body, \
        "摘除分支没吃探活/本地救急包两个条件"
    assert "socket.create_connection" in inspect.getsource(ms.ModelStore._probe_host), \
        "探活必须是真连接，不是 DNS 名字解析"
