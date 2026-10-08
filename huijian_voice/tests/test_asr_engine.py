"""ASR 引擎选择/回落/换绑/输出路径测试（默认 FireRedASR2-AED + 回落档 FireRedASR2-CTC）。

钉桩点（换默认档后的行为契约；2026-10-07 删 Paraformer 档后为两级语义）：
  ① stt.local_model 默认 firered_aed，model_key 随配置解析（_loop_models 消费）；
  ② 默认档缺失/构建失败 → 自动回落 FireRed-CTC（fail-open），回落态 stale_kind=True；
     显式档（含显式 SenseVoice）起不来 → 不回落；
  ③ 主档补就绪后 rebind_primary 原地换绑；推理在飞必须跳过（不断会话）；
  ④ 全部引擎为离线整句解码：剥离 <|zh|> 类标签、不补尾静音（原流式收流分支已于
     2026-10-07 随 Paraformer 档一并删除）。
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import asr as asr_mod                      # noqa: E402
from core.asr import AsrEngine, KEY_FA, KEY_FR, KEY_SV  # noqa: E402


class S:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, default=None):
        return self.d.get(k, default)


class Store:
    def __init__(self, ready_keys=(), ensure_noop=True):
        self.ready = set(ready_keys)
        self.ensure_calls = []
        self.async_calls = []
        self.base = Path("/models")

    def model_dir_for(self, key):
        return self.base / key if key in self.ready else None

    def ensure(self, key):
        self.ensure_calls.append(key)
        return key in self.ready

    def ensure_async(self, key, force=False):
        self.async_calls.append(key)


class SvStream:
    def __init__(self):
        self.accepted = 0

    def accept_waveform(self, rate, data):
        self.accepted += len(data)

    result = type("R", (), {"text": "<|zh|><|NEUTRAL|><|Speech|><|woitn|>打开客厅的射灯"})()


class SvRec:
    """离线形态替身（全库引擎现在都是 OfflineRecognizer，一个替身够用）。"""

    def __init__(self):
        self.stream = None

    def create_stream(self):
        self.stream = SvStream()
        return self.stream

    def decode_stream(self, s):
        pass


def make_engine(settings, store, fail_kinds=()):
    eng = AsrEngine(settings, store)

    def fake_build(kind, d):
        if kind in fail_kinds:
            raise RuntimeError(f"boom {kind}")
        rec = SvRec()
        rec._hj_kind = kind
        return rec

    eng._build_recognizer = fake_build
    return eng


PCM = b"\x01\x00" * 8000   # 0.5s s16le@16k


def test_default_kind_and_model_key():
    eng = make_engine(S(), Store())
    assert eng._primary_kind() == "firered_aed", "2026-10-07 晚起默认档＝FireRedASR2-AED"
    assert eng.model_key == KEY_FA
    eng2 = make_engine(S(**{"stt.local_model": "firered_ctc"}), Store())
    assert eng2.model_key == KEY_FR, "CTC 转回落档（存量显式选它的用户不得被换档）"
    eng2b = make_engine(S(**{"stt.local_model": "paraformer"}), Store())
    assert eng2b.model_key == KEY_FA, \
        "存量配置里的 paraformer 档已删（2026-10-07）＝未知值，安全回当前默认档"
    eng3 = make_engine(S(**{"stt.local_model": "瞎写的值"}), Store())
    assert eng3.model_key == KEY_FA, "未知值必须安全回**当前默认档**（回旧默认＝默认档换档时静默裂口）"
    eng4 = make_engine(S(**{"stt.local_model": "sensevoice"}), Store())
    assert eng4.model_key == KEY_SV, "存量 settings.json 显式存了 sensevoice 的用户不得被换档"


def test_default_arm_loads_when_present():
    store = Store(ready_keys={KEY_FA, KEY_FR})
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() is True
    assert eng.loaded_kind() == "firered_aed"
    assert store.ensure_calls == []


def test_fallback_to_ctc_when_default_arm_broken():
    """回落链唯一一级：默认 AED 起不来 → FireRed-CTC（上一代默认、盘上最可能在）。"""
    store = Store(ready_keys={KEY_FA, KEY_FR})
    eng = make_engine(S(), store, fail_kinds=("firered_aed",))
    assert eng.ensure_loaded() is True, "主档坏了也必须能识别（fail-open）"
    assert eng.loaded_kind() == "firered_ctc"
    assert eng.stale_kind() is True


def test_explicit_kind_broken_does_not_fall_sideways():
    """显式档（含显式 CTC/SenseVoice）起不来 → 不回落。

    原第三级 Paraformer 已于 2026-10-07 删除；显式选择被别的档顶替＝用户的实测
    作废（FireRed 对比档当年的同判例），故如实失败而不是落默认档。
    """
    store = Store(ready_keys={KEY_SV, KEY_FR})
    eng = make_engine(S(**{"stt.local_model": "sensevoice"}), store,
                      fail_kinds=("sensevoice",))
    assert eng.ensure_loaded() is False
    assert eng.loaded_kind() == ""
    assert "sensevoice" in (eng.last_reason or "") \
        and "加载失败" in (eng.last_reason or ""), eng.last_reason
    assert store.async_calls == [], "全档在盘时不该推任何下载"
    # 显式 CTC（现回落档）坏了同样不许被顶替——回落链只认"主档==默认档"这个位置
    eng2 = make_engine(S(**{"stt.local_model": "firered_ctc"}),
                       Store(ready_keys={KEY_FR, KEY_FA}),
                       fail_kinds=("firered_ctc",))
    assert eng2.ensure_loaded() is False and eng2.loaded_kind() == ""


def test_missing_primary_dir_defers_download_and_fails_open():
    """v1.1.13（F3 根修）：主档目录缺失此前走**同步** ensure——在请求热路径的
    executor 线程里跑分钟级跨境下载，设备侧 T_AWAITING=20s 先超时 ⇒ 无应答也无报错。
    现只查在盘，缺失转交后台补取（main._loop_models 本就在带退避重下），当轮快败。
    fail-open 的既有行为必须原样保住：默认档缺失但回落档在盘 ⇒ 回落档顶上。"""
    store = Store(ready_keys={KEY_FR})          # 默认档不在盘、回落档在盘
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() is True, "主档缺失仍要能识别（fail-open 不回归）"
    assert store.ensure_calls == [], "热路径不得再触发同步下载（分钟级挂死的根因）"
    # 回落链：缺的档逐个转交后台补取（AED 缺失即推；CTC 在盘不推）
    assert store.async_calls == [KEY_FA], "缺失必须转交后台补取，否则永远没人下"
    assert eng.loaded_kind() == "firered_ctc"


def test_hot_path_does_not_block_on_slow_download():
    """反向钉（比调用序列更贴近"别挂死"这个真实诉求）：把同步 ensure 换成"睡 5 秒"，
    主档+回落档目录都缺 ⇒ 快败路径必须远早于 5s 返回。若有人把同步下载接回热路径，
    本钉在时限处直接红（家网实锤形态：每轮语音挂在该线程，面板与语音侧同哑）。"""
    store = Store(ready_keys=set())
    store.ensure = lambda key: (time.sleep(5.0), key in store.ready)[1]
    eng = make_engine(S(), store)
    t0 = time.perf_counter()
    assert eng.ensure_loaded() is False, "链上两档都不在盘＝本轮无结果"
    took = time.perf_counter() - t0
    assert took < 1.0, f"热路径被同步下载挂死（{took:.1f}s）——F3 回归"
    assert store.async_calls == [KEY_FA, KEY_FR], "快败仍要把链上每一档都推给后台补下"


def test_both_dirs_missing_sets_named_reason():
    """空结果必须带得出具名分因：设备侧此前只能看到 text:""，与真静音逐字节同形。"""
    store = Store(ready_keys=set())
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() is False
    assert "模型资产缺失" in eng.last_reason and KEY_FA in eng.last_reason, eng.last_reason


def test_rebind_primary_after_main_arrives():
    """回落档在载 → 默认档到盘 → 换绑回默认档（默认＝AED）。

    中间那步"rebind 只认主档在盘"是这条链的承重墙：否则回落档一下载就把默认档的
    下载成果挤掉，用户永远看不到 AED 在跑。
    """
    store = Store(ready_keys={KEY_FR})
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() and eng.loaded_kind() == "firered_ctc"
    eng._busy = 0
    reason_before = eng.last_reason
    queued_before = list(store.async_calls)     # 首载链已把缺失的 AED 推过后台
    assert eng.rebind_primary() is False, "默认档不在盘时不换绑（rebind 只认主档）"
    assert eng.loaded_kind() == "firered_ctc"
    # guard 真正守的是**副作用**：少了这道 guard，_load_one 会替缺失的主档再 ensure_async
    # 一遍并把 last_reason 写成"模型资产缺失"——而此刻识别正在回落档上好好跑着，
    # 面板/回执读到的分因就成了谎话（与"空结果与静音同形"同一类陷阱）。
    assert store.async_calls == queued_before, \
        f"rebind 失败不该再推下载：{queued_before} → {store.async_calls}"
    assert eng.last_reason == reason_before, \
        f"rebind 失败不该改写分因：{reason_before!r} → {eng.last_reason!r}"
    store.ready.add(KEY_FA)                     # 默认档下载完成
    eng._busy = 1
    assert eng.rebind_primary() is False, "推理在飞不得换绑"
    eng._busy = 0
    assert eng.rebind_primary() is True
    assert eng.loaded_kind() == "firered_aed"
    assert eng.stale_kind() is False


def test_sv_transcribe_strips_tags_and_no_tail_pad():
    eng = make_engine(S(**{"stt.local_model": "sensevoice"}), Store(ready_keys={KEY_SV}))
    eng.ensure_loaded()
    text = eng._local_transcribe(PCM)
    assert text == "打开客厅的射灯"
    assert "<|" not in text and "|" not in text
    assert eng._rec.stream.accepted == 8000, "离线整句馈入：不补 1s 尾静音"


def test_lock_entry_pins_sensevoice_int8():
    lock = json.loads((Path(__file__).resolve().parents[1] / "models.lock.json")
                      .read_text(encoding="utf-8"))
    e = lock["asr_sensevoice_small"]
    assert e["required_files"] == ["model.int8.onnx", "tokens.txt"], "绝不钉 fp32 model.onnx"
    assert e["sha256"] == "f6b2a72ebcb1ac7a764d4cfccd886e6bcb2a95c4657c2199d0ba95ed4b9ea71a"
    assert e["urls"][0].startswith("https://gh-proxy.com/"), "第一源必须国内可达"
    assert "asr_paraformer_bilingual" not in lock, \
        "中英双语流式档 2026-10-07 已删（用户点名），不得回魂"


def test_settings_defaults_local_model_present_for_merge():
    """存量 settings.json 无 local_model 键——升级后必须靠 DEFAULTS 深合并拿到默认。

    v1.2.4 换默认档：这条钉同时是"面板/引擎/就绪门三处同链"的唯一锚点——
    DEFAULTS 的字面值必须等于 `asr._DEFAULT_KIND`，且必须等于 lock 里
    `default_provider:true` 的那个**主**档，否则会出现"引擎跑 A、门检 B"。
    """
    from core import asr as _a
    from core.settings import DEFAULTS
    assert DEFAULTS["stt"]["local_model"] == "firered_aed"
    assert DEFAULTS["stt"]["local_model"] == _a._DEFAULT_KIND, \
        "DEFAULTS 与 asr._DEFAULT_KIND 分叉＝换默认时只改一处（回落判据会跟着失真）"
    assert _a._FALLBACK_KIND in _a._KIND_KEY
    lock = json.loads((Path(__file__).resolve().parents[1] / "models.lock.json")
                      .read_text(encoding="utf-8"))
    must_be_on_disk = {k for k, v in lock.items()
                       if isinstance(v, dict) and v.get("default_provider")}
    # 必检集＝**只有默认档**（＋默认 TTS）。回落档一律 false：生产侧 `_loop_models` 只下
    # "当下选的那档"，把回落档放进必检集＝门等一个没人下的模型直到超时
    # ——v1.2.4 的 E2E 就是这么红的（该 run Release 腿被跳过，等于没发出去）。
    assert {m for m in must_be_on_disk if m.startswith("asr_")} == {
        _a._KIND_KEY[_a._DEFAULT_KIND]}, \
        f"ASR 必检集必须＝{{默认档}}：{sorted(must_be_on_disk)}"
    assert _a._KIND_KEY[_a._FALLBACK_KIND] not in must_be_on_disk, \
        "回落档进了必检集＝E2E 就绪门会等一个没人下的模型（v1.2.4 实伤回归）"
    assert DEFAULTS["stt"]["provider"] == "local_paraformer", "provider 值空间兼容 pin 不得改"


def _readiness_snippet(code_text, rel):
    """从 shell 脚本里摘出就绪判据那段 `python -c '...'` 本体（不复制逻辑）。"""
    import re
    m = re.search(r"'(import json,os,sys.*?print\(\"yes\".*?\)\s*)'", code_text, re.S)
    assert m, f"{rel}: 摘不到就绪判据片段（脚本结构变了要同步本钉）"
    return m.group(1)


def _run_readiness(snippet, health, lock):
    """按脚本同样的方式真跑一次：health JSON 走 stdin、HJ_LOCK 指临时 lock。"""
    import json as _json
    import os as _os
    import subprocess as _sp
    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as lf:
        _json.dump(lock, lf)
        lock_path = lf.name
    try:
        env = dict(_os.environ, HJ_LOCK=lock_path)
        out = _sp.run([sys.executable, "-c", snippet],
                      input=_json.dumps(health), capture_output=True,
                      text=True, encoding="utf-8", env=env, timeout=30)
        return (out.stdout or "").strip()
    finally:
        _os.unlink(lock_path)


def test_e2e_need_is_derived_from_lock_not_hardcoded():
    """两 e2e 脚本的 models 就绪等待集必须**从 models.lock 派生**（default_provider:true）。

    v1.1.10 补把它写死成 melo 字面量，同一版的 CHANGELOG 却声称"对默认档翻转免疫"
    ——换个默认档就又漂一次（本次审计实证）。派生后同时与代码主档对账：
    ASR=KEY_FA、TTS=PROVIDER_MODEL_KEYS[DEFAULTS.tts.provider]。"""
    import json
    root = Path(__file__).resolve().parents[1]
    from core.settings import DEFAULTS
    from core.tts import PROVIDER_MODEL_KEYS
    lock = json.loads((root / "models.lock.json").read_text(encoding="utf-8"))
    want = {k for k, v in lock.items()
            if isinstance(v, dict) and v.get("default_provider")}
    assert want == {KEY_FA, PROVIDER_MODEL_KEYS[DEFAULTS["tts"]["provider"]]}, \
        f"就绪门必检集＝{{默认 ASR 档, 默认 TTS}}（回落档不进，v1.2.4 的 E2E 超时就是这么来的）：{sorted(want)}"
    for rel in ("tests/e2e/run_e2e.sh", "tests/e2e/run_local.sh"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "default_provider" in src, f"{rel}: need 集未从 lock 派生（写死的键会漂）"
        for hard in (KEY_SV, KEY_FR, "tts_melo_zh_en", "tts_kokoro_multilang"):
            assert hard not in src, f"{rel}: need 集仍写死 {hard}"
        # v1.1.36（2026-10-01）：就绪门必须看**引擎真装态**。
        # ⚠ 这条第一轮写成 `assert "asr_loaded" in src` —— 被我自己写的注释逐字
        # 满足，把整段闸表达式删掉全量仍零红（独立复核实证 M11 全绿＝假绿）。
        # 现改为**跑判据本体**：把脚本里那段 python 片段抽出来，喂两种 health JSON，
        # 只有真装态才许放行。文本匹配一律只在剥掉注释的代码行上做。
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        snippet = _readiness_snippet(code, rel)
        cases = [
            ({"models_ready": {k: True for k in want},
              "asr_loaded": False, "tts_loaded": True}, "", "包解好但引擎没装载必等"),
            ({"models_ready": {k: True for k in want},
              "asr_loaded": True, "tts_loaded": False}, "", "同上（TTS 侧）"),
            ({"models_ready": {k: True for k in want},
              "asr_loaded": True, "tts_loaded": True}, "yes", "真装态才放行"),
        ]
        for health, expect, why in cases:
            got = _run_readiness(snippet, health, lock)
            assert got == expect, f"{rel}: {why}（实得 {got!r}）"


def test_e2e_scripts_wait_on_need_not_full_lock():
    """v1.0.28 CI 实红（run 34364440982，E2E 25min 超时）：两 e2e 脚本的 models
    就绪等待集必须是「运行期 need」——回落/对比档新装根本不主动下载，
    等 lock 全清单 all() 恒假。

    v1.1.17 随脚本改派生形同步改写：need 由 models.lock 的 default_provider 派生
    （见 test_e2e_need_is_derived_from_lock_not_hardcoded），本钉只守**不得退回全清单等待**。"""
    root = Path(__file__).resolve().parents[1]
    for rel in ("tests/e2e/run_e2e.sh", "tests/e2e/run_local.sh"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "HJ_LOCK" in src and "models.lock.json" in src, \
            f"{rel}: need 派生接线丢失（等待集又变成本地常量？）"
        # 只判**代码行**：注释里记着这句历史（"all(models_ready.values()) 恒 false"），
        # 扫到注释会把钉弄红一次、红过的钉就会被删掉（本仓 v1.1.12 的教训）。
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "models_ready.values()" not in code and "m.values()" not in code, \
            f"{rel}: 退回『等 lock 全清单』旧式（新装永不就绪）"
