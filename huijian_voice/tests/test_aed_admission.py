# -*- coding: utf-8 -*-
"""AED 时代的进程级资源闸（N1 解码准入 / N2 先卸后载 / N5 假护栏 reset）。

根因一句话：**AED 的代价是全局量（并发 × 每轮），而仓里所有闸都是每会话量。**
开发机 403 句实测（num_threads=2）：AED RTF 0.320 / 峰值 RSS 1415MB；CTC 0.167 / 864MB；
SenseVoice 0.022 / 358MB。于是同一份代码里：

· N1 STT 走 `run_in_executor(None, …)`＝asyncio 默认池（4 核机 min(32,cpu+4)=8 worker），
  全仓 STT 侧无任何并发/内存准入原语（grep Semaphore/max_workers 零命中）。8 轮同 30s
  ⇒ 排队 8×11s、瞬时 8×1.88GB≈15GB ≫ 靶机 8G。SenseVoice 时代 30s=0.66s、内存可忽略，
  "8" 从不是约束——是换引擎把它变成了约束。
· N2 `_load_one` 先 `_build_recognizer` 再 `self._rec = rec` ⇒ 构造那 3.29s 里新旧两档共驻：
  AED+CTC = 2.28GB 纯 STT（旧 SenseVoice↔CTC 只 1.2GB）。
· N5 `rec.reset(stream)`：1.13.7 `OfflineRecognizer` **没有 reset**（实测 dir() 只有
  create_stream/decode_stream(s)/from_*），每轮 AttributeError 被 `except Exception: pass`
  吞掉；而测试替身个个定义了 `reset(self, s)`（4 处）⇒ 替身把 API 补上了，死调用永远露不了馅。
  每轮本来就新建 stream，这行从来不做任何复位，不能当并发依据。
"""
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import asr
from core.asr import AsrEngine

ROOT = Path(__file__).resolve().parents[1]


class DSettings:
    def __init__(self, d=None):
        self.d = d or {}

    def get(self, k, default=None):
        return self.d.get(k, default)


def _store(tmp=None):
    return SimpleNamespace(model_dir_for=lambda key: Path(tmp or "."),
                           ensure_async=lambda key: None)


class GateRec:
    """真 1.13.7 形态的替身：**没有 reset**（有 reset 就是给死调用开后门）。"""

    def __init__(self, kind="firered_aed", delay=0.25, boom=False):
        self._hj_kind = kind
        self.delay = delay
        self.boom = boom
        self.live = 0
        self.max_live = 0
        self.n = 0
        self._m = threading.Lock()

    def create_stream(self):
        class S:
            result = SimpleNamespace(text="开 灯")

            def accept_waveform(self, rate, data):
                pass
        return S()

    def decode_stream(self, s):
        with self._m:
            self.live += 1
            self.n += 1
            self.max_live = max(self.max_live, self.live)
        try:
            time.sleep(self.delay)
            if self.boom:
                raise RuntimeError("模拟推理炸")
        finally:
            with self._m:
                self.live -= 1


def test_two_rounds_never_decode_simultaneously():
    """N1：两轮同时在飞时，解码必须串行（max_live==1），且两轮都拿到结果。"""
    eng = AsrEngine(DSettings(), _store())
    rec = GateRec()
    eng._rec = rec
    outs = []
    ts = [threading.Thread(target=lambda: outs.append(eng._local_transcribe(b"\x00" * 6400)))
          for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert rec.max_live == 1, f"两轮并发进了 AED 解码（max_live={rec.max_live}）⇒ 内存/线程无准入"
    assert rec.n == 2 and all(o == "开 灯" for o in outs), f"串行化把轮次吃掉了：{outs}"


def test_queue_beyond_wait_returns_named_reason_not_silence(monkeypatch):
    """N1：闸门被占满且排队超上限时，必须带回**具名分因**，不许与"用户没说话"同形。"""
    monkeypatch.setattr(asr, "_DECODE_QUEUE_WAIT_S", 0.2, raising=False)
    eng = AsrEngine(DSettings(), _store())
    rec = GateRec(delay=1.0)
    eng._rec = rec
    eng._decode_gate.acquire()          # 模拟另一轮正占着解码位
    try:
        sink = {}
        text = eng._local_transcribe(b"\x00" * 6400, reason_out=sink)
        assert text == ""
        reason = sink.get("reason", "")
        assert ("繁忙" in reason or "排队" in reason), f"排队失败没点名分因：{reason!r}"
        assert rec.n == 0, "排队超时的轮还是进了引擎"
    finally:
        eng._decode_gate.release()


def test_gate_is_released_when_the_engine_raises():
    """N1 反向：解码抛异常也必须归还准入位，否则一次故障就把引擎锁死到重启。"""
    eng = AsrEngine(DSettings(), _store())
    eng._rec = GateRec(boom=True)
    sink = {}
    assert eng._local_transcribe(b"\x00" * 6400, reason_out=sink) == ""
    assert "异常" in sink.get("reason", ""), sink
    assert eng._decode_gate.acquire(timeout=0.5), "解码异常后准入位没归还"
    eng._decode_gate.release()


def test_rebind_drops_the_old_engine_before_building_the_new():
    """N2：换绑（在载档≠主档）时，构造新档的瞬间旧档必须已经不在 self._rec 上。"""
    eng = AsrEngine(DSettings(), _store())
    eng._rec = GateRec(kind="firered_ctc")
    seen = {}

    def fake_build(kind, d):
        seen["rec_at_build"] = eng._rec
        return GateRec(kind=kind)

    eng._build_recognizer = fake_build
    assert eng.rebind_primary() is True
    assert seen.get("rec_at_build") is None, \
        "换绑时旧引擎还挂在 self._rec 上 ⇒ 构造期 AED+CTC 共驻 2.28GB"


def test_failed_rebind_restores_the_previous_engine():
    """N2 反向：新档加载失败不能把在载的旧档一起丢掉（切档失败≠这台机没引擎）。"""
    eng = AsrEngine(DSettings(), _store())
    old = GateRec(kind="firered_ctc")
    eng._rec = old
    calls = {"n": 0}

    def boom(kind, d):
        calls["n"] += 1
        if kind == "firered_aed":
            raise RuntimeError("模拟 encoder 损坏")
        return GateRec(kind=kind)

    eng._build_recognizer = boom
    sink = {}
    ok = eng._load_one("firered_aed", sink)
    assert ok is False
    assert calls["n"] >= 2, "只试了新档：旧档被丢掉后再没人恢复"
    assert eng._rec is not None, "换绑失败后 self._rec 为空——这台机现在没引擎"
    assert getattr(eng._rec, "_hj_kind", "") == "firered_ctc", eng._rec
    assert "回滚" in (sink.get("reason") or "") or "失败" in (sink.get("reason") or ""), sink


def test_local_transcribe_no_longer_calls_the_nonexistent_reset():
    """N5：`OfflineRecognizer` 在 1.13.7 没有 reset()，那行只会每轮抛 AttributeError
    并被 except-pass 吞掉。钉它不再出现——且**不许**靠替身补 API 来洗绿。

    判据只看代码不看散文（剥掉整行注释）：本仓有过"治假绿的钉被描述该 bug 的注释
    满足"的先例，反向同理——注释里提到那个调用不该把钉钉红，也不该替它开脱。
    """
    src = inspect.getsource(AsrEngine._local_transcribe)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert ".reset(" not in code, "asr 解码路径仍在调用库里不存在的 rec.reset(stream)"
    import sherpa_onnx
    assert not hasattr(sherpa_onnx.OfflineRecognizer, "reset"), \
        "上游若新增了 reset，本钉要连同上面的判断一起重写（别只删测试）"


# ── 刀2：保障循环 cadence（N3）与 TTS 阻塞下载（N6）─────────────────
import asyncio

from core import main as core_main
from core.tts import TtsEngine

ASR_KEY = "asr_firered_aed"
TTS_KEY = "tts_melo_zh_en"


class _LoopStore:
    def __init__(self):
        self.ready = set()
        self.pokes = []
        self.now = 0.0

    def is_ready(self, k):
        return k in self.ready

    def ensure_async(self, k, force=False):
        self.pokes.append((k, self.now))

    def ensure(self, k, force=False):
        raise AssertionError("保障循环里发生了阻塞下载")


class _LoopAsr:
    model_key = ASR_KEY
    fallback_key = "asr_firered_ctc"      # 真 AsrEngine 按 _DEFAULT_KIND/_FALLBACK_KIND 派生

    def __init__(self, loaded=""):
        self.loaded = loaded
        self.loaded_calls = 0
        self.rebind_calls = 0

    def ready(self):
        return bool(self.loaded)

    def stale_kind(self):
        return bool(self.loaded) and self.loaded != "firered_aed"

    def ensure_loaded(self, sink=None):
        self.loaded_calls += 1
        return True

    def rebind_primary(self):
        self.rebind_calls += 1
        return True


class _LoopTts:
    def __init__(self):
        self.loaded_calls = 0

    def model_key(self):
        return TTS_KEY

    def ready_for_current_provider(self):
        # 默认 False：预热判据是"当前档引擎在载"（v1.1.5），冷态就该去载
        return False

    def ensure_loaded(self):
        self.loaded_calls += 1
        return True


def _svc(store, a, t):
    return SimpleNamespace(
        settings=DSettings({"stt.provider": "local_paraformer",
                            "tts.provider": "local_melo",
                            "nlu.textcnn_enabled": False}),
        store=store, asr=a, tts=t,
        textcnn=SimpleNamespace(_ensure=lambda: None))


def test_models_pass_notices_a_landed_model_on_the_next_check():
    """N3：模型落盘后**下一次看盘**就要预热/换绑。

    旧形态把"看盘节奏"和"补下载退避"绑成同一个 sleep：1.23GB 的 AED 跨境慢滴会把
    backoff 抬到 900s，而 `rebind_primary` 的唯一调用点就在 pend-空分支里 ⇒
    面板切档/首载最长 15 分钟无人执行（显示新档、跑旧档）。看盘是磁盘 stat，便宜，
    不该陪着下载退避一起睡。
    """
    store, a, t = _LoopStore(), _LoopAsr(), _LoopTts()
    svc = _svc(store, a, t)
    st: dict = {}
    d1 = asyncio.run(core_main.Service._models_pass(svc, st, now=0.0))
    assert {k for k, _ in store.pokes} == {ASR_KEY, TTS_KEY}, store.pokes
    assert 0 < d1 <= 120.0, (
        f"看盘间隔本轮返回 {d1}s。旧形态把「看盘」和「补下载退避」绑成同一个 sleep，"
        "1.23GB 慢滴会把 backoff 抬到 900s ⇒ 落盘/切档最长 15 分钟没人执行。"
        "这里刻意用**独立字面量**判，不引用被测代码自己的常数——引用它等于让代码给"
        "自己出题（变异臂把 _MODELS_CHECK_S 改成 900 时本条照样绿，是实打实的假绿）")
    store.ready = {ASR_KEY, TTS_KEY}
    store.pokes.clear()
    asyncio.run(core_main.Service._models_pass(svc, st, now=d1))
    assert {k for k, _ in store.pokes} <= {"asr_firered_ctc"}, \
        f"主档已落盘还在重复触发主档下载：{store.pokes}"   # 回落档被排进来是 N4 的正当期行为
    assert a.loaded_calls == 1 and t.loaded_calls == 1, \
        "落盘后当轮没预热（旧形态要等 parked backoff 睡醒）"


def test_models_pass_rebinds_a_stale_engine_promptly():
    """N3：在载档≠主档时，pend 为空的那一轮就要换绑（不是等下个 900s）。"""
    store = _LoopStore()
    store.ready = {ASR_KEY, TTS_KEY}
    a = _LoopAsr(loaded="firered_ctc")
    svc = _svc(store, a, _LoopTts())
    d = asyncio.run(core_main.Service._models_pass(svc, {}, now=0.0))
    assert a.rebind_calls == 1, f"在载 CTC、主档 AED，却没人换绑（delay={d}）"


def test_models_pass_paces_download_retriggers_per_key():
    """N3 反向：看盘每 60s 一次，但**补下载触发**必须按键退避，不许每轮轰炸跨境源。"""
    store, a, t = _LoopStore(), _LoopAsr(), _LoopTts()
    svc = _svc(store, a, t)
    st: dict = {}
    now = 0.0
    for _ in range(8):
        store.now = now
        d = asyncio.run(core_main.Service._models_pass(svc, st, now=now))
        now += d
    per = {}
    for k, ts in store.pokes:
        per.setdefault(k, []).append(ts)
    assert per.get(ASR_KEY), store.pokes
    assert len(store.pokes) < 8 * 2, f"每轮都重触发（共 {len(store.pokes)} 次/8 轮）＝无退避"
    gaps = [b - a_ for a_, b in zip(per[ASR_KEY], per[ASR_KEY][1:])]
    assert gaps == sorted(gaps), f"同一键的补下载间隔没有单调拉开：{gaps}"


class _TtsStore:
    def __init__(self):
        self.kicked = []

    def model_dir_for(self, key):
        return None

    def is_ready(self, key):
        return False

    def ensure_async(self, key, force=False):
        self.kicked.append(key)

    def ensure(self, key, force=False):
        raise AssertionError("TTS 载入路径在做阻塞下载")


def test_tts_cold_load_kicks_background_download_instead_of_parking_the_caller():
    """N6：STT 在 2026-09-26 已改成"只查在盘＋ensure_async＋快败"，TTS 侧原封没跟上——
    `_ensure_loaded_inner` 直接 `store.ensure(key)`（per-key 锁是**阻塞等待**语义，
    见同文件 R2 #6 注释自述），而 `_loop_models` 每轮 await 它 ⇒ 冷下载几分钟里整条
    保障循环被 parked，期间 STT 预热/换绑全停（与 N3 相乘）。
    """
    st = _TtsStore()
    eng = TtsEngine(DSettings({"tts.provider": "local_melo"}), st)
    t0 = time.perf_counter()
    ok = eng._ensure_loaded_inner()
    assert ok is False
    assert time.perf_counter() - t0 < 1.0, "冷载仍在原地等下载"
    assert st.kicked == [TTS_KEY], f"没把下载交给后台（kicked={st.kicked}）"


def test_tts_inner_no_longer_calls_the_blocking_store_ensure():
    """N6 静态钉：两个调用点（冷载＋换绑备料）都不许再用阻塞 ensure。剥注释只判代码。"""
    src = inspect.getsource(TtsEngine._ensure_loaded_inner)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "self.store.ensure(" not in code, \
        "TTS 载入路径仍有阻塞下载调用点（会把 _loop_models parked 成分钟级）"


def test_admission_and_cadence_numbers_are_pinned():
    """值钉＋算式关系（不假装能钉住没测的东西）。

    `_DECODE_QUEUE_WAIT_S + 单轮解码上界 ≤ const.STT_RESULT_BUDGET_S` 这条端到端算式
    依赖**靶机** RTF（现只有开发机 num_threads=2 的 0.364@30s），4核8G 上未实测，
    所以这里只钉两条无条件成立的关系，靶机那条留在欠账里——不写成一个"看着像
    对账其实是空转"的假钉。
    """
    from core import const
    assert asr._DECODE_CONCURRENCY == 1, "并发上限被抬＝两档 ORT 线程一起压 4 核"
    assert 0 < asr._DECODE_QUEUE_WAIT_S < const.STT_RESULT_BUDGET_S, \
        "排队上限若不小于会话预算，被排队的轮永远等不到自己的分因"
    assert core_main._MODELS_CHECK_S <= 60.0
    assert core_main._POKE_COOLDOWN_S <= core_main._MODELS_CHECK_S, \
        "首键冷却长于看盘节奏 ⇒ 落盘后还要空等一轮才有人预热"
    assert core_main._POKE_COOLDOWN_MAX_S >= core_main._POKE_COOLDOWN_S


def test_budget_arithmetic_holds_on_the_measured_target_rtf():
    """三层预算算式必须用**靶机实测** RTF 算，不许用开发机那条 0.32 背书。

    10-08 实测（.91 4核8G、线上 1.2.6、AED 在载、8 句 2.7s 真实命令，走 stt 通道）：
    端到端 RTF 均值 0.887、最差 **1.190**（含 LAN+ws 往返，是上界）≈ 开发机 3 倍，
    且已过 1.0（比实时还慢）。我上一轮写进注释的"30s→11s→排队30s仍<52s"用的正是
    开发机数——按实测最坏值重算，30s 轮要 35.7s，再叠 30s 排队＝65.7s > 52s，
    等于把"wait_for 截不断 executor 线程"那个病请回来一点。所以这里钉真实关系：
        排队上限 + 整轮上限×靶机最坏RTF ≤ 会话预算 − 余量
    改任何一个数（含靶机 RTF 复测后的新值）都要重算，别只改一处。
    """
    from core import const
    from core.session import SttSession
    rt_worst = 1.19                      # 实测上界（含往返），不是估算
    margin = 5.0                         # 与既有对账链留的 2s 级余量同量级
    total = (asr._DECODE_QUEUE_WAIT_S
             + SttSession._MAX_UTTER_SEC * rt_worst)
    assert total <= const.STT_RESULT_BUDGET_S - margin, (
        f"排队 {asr._DECODE_QUEUE_WAIT_S}s + 整轮 {SttSession._MAX_UTTER_SEC}s×{rt_worst}"
        f" = {total:.1f}s，超出会话预算 {const.STT_RESULT_BUDGET_S}s 减余量 "
        f"{margin}s——超预算那轮的回执会与在飞解码脱钩（旧病形状）")


# ── N4：回落档预取（P0-2 收口）──────────────────────────────────
def test_fallback_arm_is_prefetched_once_the_primary_lands():
    """N4：AED 起不来时回落目标是 CTC，而 `need` 过去只含主档 ⇒ 新装机盘上根本没有
    CTC，fail-open 当场无路可落（10-08 取证：线上 .91 的 CTC 是"当过默认档"留下的，
    新机不会再有）。修法：主档一落盘就把回落档纳入补取集合。
    """
    store = _LoopStore()
    store.ready = {ASR_KEY, TTS_KEY}              # 主档与默认 TTS 已在盘
    svc = _svc(store, _LoopAsr(), _LoopTts())
    asyncio.run(core_main.Service._models_pass(svc, {}, now=0.0))
    assert "asr_firered_ctc" in [k for k, _ in store.pokes], \
        f"主档就绪后没把回落档纳入预取：{store.pokes}"


def test_prefetch_does_not_delay_the_first_warm_load():
    """N4 的反面陷阱：预取**不许**把预热/换绑拖到回落档下完。

    旧循环用 `not pend` 当预热门，直接把回落档塞进 need 会让首装用户在
    776MB 下载期间一直"模型未就绪"——那不是兜底，是把可用功能换成兜底。
    所以 need 分成两条：下载集合（含回落档）与预热门（只看主档＋默认 TTS）。
    """
    store = _LoopStore()
    store.ready = {ASR_KEY, TTS_KEY}              # 回落档永远不下（is_ready 恒 False）
    a = _LoopAsr()
    svc = _svc(store, a, _LoopTts())
    asyncio.run(core_main.Service._models_pass(svc, {}, now=0.0))
    assert a.loaded_calls == 1, "回落档没下完就把预热拖走了＝新机首装全程哑"


def test_fallback_arm_is_not_prefetched_before_the_primary_lands():
    """首装下载面不许变大：主档还没落盘时，回落档不排队（一次只抢一条跨境带宽）。"""
    store = _LoopStore()
    svc = _svc(store, _LoopAsr(), _LoopTts())
    asyncio.run(core_main.Service._models_pass(svc, {}, now=0.0))
    assert "asr_firered_ctc" not in [k for k, _ in store.pokes], \
        f"主档未就绪就抢下回落档（首装下载面翻倍）：{store.pokes}"


def test_fallback_key_follows_the_chain_rule_not_a_hardcoded_name():
    """`fallback_key` 必须与 `ensure_loaded` 的链判据同源：默认档语境才有回落目标。

    写死键名会出现两件事：显式选 CTC 的用户被白排一份 776MB（回落目标＝主档自己），
    以及换默认时这里没人跟着改——那正是本仓"回落档当主档跑"那类裂口的来源。
    """
    from core.asr import AsrEngine
    blank = SimpleNamespace()
    dft = AsrEngine(DSettings({"stt.local_model": "firered_aed"}), blank)
    assert dft.fallback_key == "asr_firered_ctc"
    for prov, why in (("sensevoice", "显式档没有回落链"),
                      ("firered_ctc", "回落目标＝主档自己，重复排队")):
        eng = AsrEngine(DSettings({"stt.local_model": prov}), blank)
        assert eng.fallback_key == "", f"{prov}：{why}，不该返回键名"


def test_progress_pct_is_clamped_at_100(tmp_path, monkeypatch):
    """N9：lock 声明体积小于真包时进度条会冲过头——AED 的 `size_mb` 曾写 800，
    而真字节 838,589,068 ⇒ 104%。这轮同时改了两件事：锁值改 839（十进制 MB，与
    CTC 那条 520↔520,516,278B 的既有口径一致），以及 `pct` 钳位。这里钉**钳位行为**：
    服务字节介于声明量与 1.5× 上浮上限之间时，pct 不许 >100，下载仍要成功
    （只改锁值不钳位＝下一个谎报体积的包又冲一次）。
    """
    import json as _json
    from core import model_store as ms

    class _S:
        def get(self, k, dv=None):
            return {"power.auto_download": True}.get(k, dv)

    entry = {"tarball": "m.tar.bz2", "sha256": "", "size_mb": 1,
             "urls": ["https://x.invalid/m"]}
    lock = tmp_path / "lock.json"
    lock.write_text(_json.dumps({"mk": entry}), encoding="utf-8")
    store = ms.ModelStore(_S(), lock_path=lock, models_dir=tmp_path / "models",
                          status_file=tmp_path / "st.json")

    class _Resp:
        def __init__(self):
            self.n = 0

        def read(self, k):
            self.n += 1
            return b"x" * 600_000 if self.n <= 2 else b""     # 1.2MB：> 1MB 声明，< 1.5MB 上限

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    seen: list = []
    orig = store._set_status
    store._set_status = lambda *a, **kw: (seen.append(kw.get("pct")), orig(*a, **kw))[1]
    ok = store._download_any({**entry, "_key": "mk"}, tmp_path / "m.tar.bz2")
    assert ok is True, "1.2MB 在 1.5× 上浮上限内，不该被字节闸拒掉"
    nums = [p for p in seen if p is not None]
    assert nums and max(nums) == 100, f"进度没到 100（{nums}）"
    assert all(0 <= p <= 100 for p in nums), f"进度条冲过 100%：{nums}"


def test_lock_declares_the_aed_package_size_in_decimal_mb():
    """锁值口径钉：`size_mb` 全仓按**十进制 MB**用（`* 1e6`），CTC 那条 520 对应
    520,516,278B 就是这个口径的既有先例。AED 曾写 800（真 838.6MB）⇒ 进度 104%、
    字节上浮上限也跟着算小。改成 839，并钉住它不许再回到"按 MiB 心算"的那一侧。"""
    import json as _json
    lk = _json.loads((ROOT / "models.lock.json").read_text(encoding="utf-8"))
    aed = lk["asr_firered_aed"]
    assert aed["size_mb"] == 839, f"AED 声明体积漂移：{aed['size_mb']}"
    assert aed["size_mb"] * 1e6 >= 838_589_068, "声明量不许小于真字节，否则进度条必冲过头"
    assert aed["size_mb"] * 1e6 <= 838_589_068 * 1.05, "声明量虚高太多＝字节上浮上限被无谓放大"


# ── N8：云档超时可配且不越界 ────────────────────────────────────
def test_cloud_timeout_is_clamped_and_never_raises():
    """`timeout` 是用户能手改的 settings.json 值：0 让云永远立刻失败、10000 把整轮
    拖过设备 20s 与会话 52s 预算、写成 "12s" 旧形直抛 ValueError 打死本轮
    （klar_client `_num_opt` 那条"非数值别直抛"是同族先例）。

    钳位上界 30s 的链式理由：云失败还要回落本地再解码（靶机 AED 实测单轮 2.1~3.3s），
    云侧留得比 30s 更多就挤不进 52s。
    """
    from core.asr import _cloud_timeout
    assert _cloud_timeout({"timeout": 12}) == 12.0
    assert _cloud_timeout({}) == 12.0
    assert _cloud_timeout({"timeout": 0}) == 3.0, "0 秒＝每次立刻失败，云档形同禁用却不点名"
    assert _cloud_timeout({"timeout": -5}) == 3.0
    assert _cloud_timeout({"timeout": 10000}) == 30.0
    assert _cloud_timeout({"timeout": "12s"}) == 12.0        # 不抛
    assert _cloud_timeout({"timeout": None}) == 12.0         # 不抛
    assert _cloud_timeout({"timeout": float("nan")}) == 12.0
    from core import settings as st
    assert st.DEFAULTS["stt"]["cloud"].get("timeout") == 12, "默认值要与引擎缺省同源"
