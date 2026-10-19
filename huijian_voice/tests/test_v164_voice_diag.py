# -*- coding: utf-8 -*-
"""v1.6.4 定案面 + 诚实闸钉。

本版两处改动都不赌嗓音根因（那要部署后 /healthz 定案），只交付两件事并各自钉死：
① model_store.extras_diag / tts._self_test_voice→voice_diag / ws_server._readiness
   暴露：远程一次 GET 能在「声码器字节坏」与「字节对、库谎报产物坏」间二分。
② 集成 http.py 固件三级回退：握手档加 rd.available 前置闸（v1.6.1「实时」在
   断连/重启窗口谎标的诚实修复）。
"""
import hashlib
import json
import sys
import threading
from pathlib import Path

import numpy as np

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


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _mk(tmp_path, *, vocos=GOOD, drop=False):
    models = tmp_path / "models"
    (models / "tts_x").mkdir(parents=True)
    lock = tmp_path / "models.lock.json"
    lock.write_text(json.dumps({
        "tts_x": {
            "tarball": "tts_x.tar.bz2", "sha256": "deadbeef", "top_dir": "",
            "required_files": ["model.onnx", "vocos-16khz-univ.onnx"],
            "urls": [], "extra_files": [
                {"file": "vocos-16khz-univ.onnx", "sha256": _sha(GOOD),
                 "urls": ["u://x"]}]},
    }), encoding="utf-8")
    d = models / "tts_x"
    (d / "model.onnx").write_bytes(b"x")
    if not drop:
        (d / "vocos-16khz-univ.onnx").write_bytes(vocos)
    (d / ".extracted_ok").write_text("tts_x.tar.bz2|deadbeef", encoding="utf-8")
    return models, lock, d


# ── ① extras_diag：实际 vs 登记 sha，好/坏/缺失三形 ─────────────────
def test_extras_diag_good_match(tmp_path):
    models, lock, d = _mk(tmp_path)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "s.json")
    out = st.extras_diag("tts_x")
    assert out and out[0]["match"] is True
    assert out[0]["expected"] == _sha(GOOD)[:12]
    assert out[0]["actual"] == _sha(GOOD)[:12]
    # 定案面只出 sha 前 12 位（不泄全串）
    assert len(out[0]["actual"]) == 12


def test_extras_diag_bad_mismatch(tmp_path):
    models, lock, d = _mk(tmp_path, vocos=BAD)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "s.json")
    out = st.extras_diag("tts_x")
    assert out[0]["match"] is False
    assert out[0]["actual"] == _sha(BAD)[:12]
    # 关键：诊断**只读不改**——坏字节文件必须还在（摘除是 verify_extras 的活，
    # 定案面不得把现场弄得更糟，也不得因诊断顺带删）
    assert (d / "vocos-16khz-univ.onnx").exists()


def test_extras_diag_missing(tmp_path):
    models, lock, d = _mk(tmp_path, drop=True)
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "s.json")
    out = st.extras_diag("tts_x")
    assert out[0]["actual"] is None and out[0]["match"] is False


def test_extras_diag_never_throws(tmp_path):
    # 附属登记了空文件名/怪结构不得炸诊断面
    models = tmp_path / "models"; (models / "tts_y").mkdir(parents=True)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"tts_y": {
        "tarball": "t.tar.bz2", "sha256": "x", "top_dir": "",
        "required_files": ["a.onnx"],
        "extra_files": [{"file": ""}, {"nope": 1}]}}), encoding="utf-8")
    st = ModelStore(_S({"power.auto_download": False}), lock_path=lock,
                    models_dir=models, status_file=tmp_path / "s.json")
    assert st.extras_diag("tts_y") == []            # 空 file 跳过，不抛


# ── ② voice_diag：verdict 三态分离（这把尺必须真能分辨） ────────────
def _make_engine(prov, extras_ret):
    from core.tts import TtsEngine

    class _Store:
        def extras_diag(self, key):
            return extras_ret

    class _Audio:
        # 坏嗓代理：16k 名义下塞 >4.1k 能量（正弦对折进高频）
        def __init__(self, bad):
            sr = 16000
            t = np.arange(int(1.5 * sr)) / sr
            if bad:
                x = np.sin(2 * np.pi * 6000 * t)          # 6kHz：>4.1k 高占比
            else:
                x = np.sin(2 * np.pi * 300 * t) + 0.3 * np.sin(2 * np.pi * 900 * t)
            self.samples = x.astype(np.float32)
            self.sample_rate = sr

    class _Tts:
        def __init__(self, bad):
            self._bad = bad

        def generate(self, text, sid=0, speed=1.0):
            return _Audio(self._bad)

    e = TtsEngine.__new__(TtsEngine)
    e._tts = _Tts(bad=(extras_ret is not None and extras_ret
                       and not extras_ret[0].get("match")))
    # v1.6.6 刀2：夹具补生产同款并发件（_lock/_gen_lock/_busy）。哨兵自 刀2 起走
    # `generate_for_selftest` 的受锁路径，夹具缺件会被外层 except 吞成 skipped，
    # 这三条三态分离钉就变成"钉不出东西也算过"的假绿。
    e._lock = threading.Lock()
    e._gen_lock = threading.Lock()
    e._busy = 0
    e.store = _Store()
    e.settings = _S({"tts.provider": prov, "tts.sid": 0, "tts.speed": 1.05})
    e._loaded_prov = prov
    e._custom_sids = {}
    e._custom_fp = ""
    e._cloud_fail_ts = 0.0
    return e


def test_voice_diag_ok_when_bytes_good_and_hf_low():
    eng = _make_engine("local_matcha", [{"file": "v", "match": True}])
    eng._self_test_voice()
    d = eng.voice_diag()
    assert d["verdict"] == "ok", d
    assert d["reported_sr"] == 16000 and d["hf_pct"] < 2


def test_voice_diag_bytes_mismatch_when_hf_high_and_bytes_bad():
    eng = _make_engine("local_matcha", [{"file": "v", "match": False}])
    eng._self_test_voice()
    d = eng.voice_diag()
    assert d["verdict"] == "bytes-mismatch", d     # 指向"重下载/import 能修"


def test_voice_diag_octave_lib_suspect_when_hf_high_but_bytes_good():
    # 字节对（match True）却产物坏（hf 高）＝库行为嫌疑，重下载无用
    e = _make_engine("local_matcha", [{"file": "v", "match": True}])
    e._tts = e._tts.__class__(bad=True)             # 强制坏嗓，字节仍标 good
    e._self_test_voice()
    d = e.voice_diag()
    assert d["verdict"] == "octave-lib-suspect", d  # 指向"按平台/版本定位"


def test_voice_diag_never_throws_and_shape():
    e = _make_engine("local_matcha", None)
    e._tts = None                                    # 引擎未载：skipped，不抛
    e._self_test_voice()
    assert e.voice_diag()["verdict"] == "skipped"
    # store.extras_diag 抛异常也不得连累自测/诊断
    e2 = _make_engine("local_matcha", [{"file": "v", "match": True}])

    class _Boom:
        def extras_diag(self, key):
            raise RuntimeError("盘坏")

    e2.store = _Boom()
    e2._self_test_voice()
    assert e2.voice_diag().get("extras") == "err"


# ── ③ _readiness 暴露：健康面带出 voice_diag/runtime，且观测异常不弄炸探针 ──
def test_readiness_carries_voice_diag_and_runtime():
    import types
    from core.ws_server import _readiness
    store = types.SimpleNamespace(snapshot=lambda: {})
    asr = types.SimpleNamespace(ready=lambda: True, model_key="x",
                                loaded_kind=lambda: "k",
                                stale_kind=lambda: False, last_reason="")

    class _Tts:
        def ready(self):
            return True

        def voice_diag(self):
            return {"verdict": "bytes-mismatch", "hf_pct": 28.5}

    ctx = types.SimpleNamespace(store=store, asr=asr, tts=_Tts(), sessions=set())
    out = _readiness(ctx)
    assert out["ok"] is True                         # 既有语义未动
    assert out["voice_diag"]["verdict"] == "bytes-mismatch"
    assert "machine" in out["runtime"] and "sherpa_onnx" in out["runtime"]


def test_readiness_survives_diag_blowup():
    import types
    from core.ws_server import _readiness
    store = types.SimpleNamespace(snapshot=lambda: {})
    asr = types.SimpleNamespace(ready=lambda: True, model_key="x",
                                loaded_kind=lambda: "k",
                                stale_kind=lambda: False, last_reason="")

    class _BoomTts:
        def ready(self):
            return True

        def voice_diag(self):
            raise RuntimeError("观测件炸")

    ctx = types.SimpleNamespace(store=store, asr=asr, tts=_BoomTts(), sessions=set())
    out = _readiness(ctx)
    assert out["ok"] is True and "voice_diag" not in out   # 炸了也不把健康探针弄 500


# ── ④ 集成侧诚实闸（本 venv 装不了 HA，按仓内先例走源码形态钉）────────
def test_fw_handshake_gated_on_available():
    """v1.6.1「实时」谎标修复的形态钉：握手档取值必须以 rd.available 为前置。

    病灶（10-17 现网活证：两台 online=False 却 fw_source=实时）：集成/HA 重启窗口
    async_load_from_store 灌回持久化 device_info 快照（loaded≠connected），
    async_on_disconnect 只置 available=False 不清快照 ⇒ 断连期间快照值被谎标
    「实时」。钉形制而非只钉注释：取值行必须活在 `if rd is not None and
    fw_available:` 卫哨之内（摘掉卫哨=本钉红），且 fw_available 的定义早于取值。
    """
    src = (HERE / "custom_components" / "huijian_ai" / "huijian" / "http.py").read_text(
        encoding="utf-8")
    i = src.index("class HuijianSatellitesView")
    block = src[i:i + 6200]
    assert 'fw_available = bool(getattr(rd, "available", False))' in block, \
        "必须以 rd.available 定义在线门（不得裸信快照）"
    assert "if rd is not None and fw_available:" in block, \
        "握手取值必须在 available 卫哨内（断连窗口不得标实时）"
    assert block.index("fw_available = bool(") < block.index('getattr(di, "project_version"'), \
        "定义必须早于取值（顺序钉，同三级次序钉同族）"
    # 反向保护：三级次序与实时语义不得被这波改动破坏
    assert "if fw_handshake:" in block and \
        block.index("if fw_handshake:") < block.index("elif fw_live:"), \
        "三级回退次序不得倒置（v1.1.40 契约仍成立）"
    assert '"实时"' in block and '"入驻时"' in block, \
        "两档如实标签都必须在（诚实语义不丢）"


def test_fw_ledger_tier_also_gated_on_available():
    """v1.6.6 刀X：1.6.4 的在线门只套在握手档，**台账档漏了**。speak_id 台账是本运行期
    CMD20 入驻 POST 的真值，但"入驻时在线、此刻已断"的窗口里它不再是现读值，照旧标
    「实时」＝断连期谎标只剩半条（加载项据此还能把陈旧值算成「已最新/可升级」）。
    钉形制：台账档的标签必须是 fw_available 的三元式，且 ungated 的旧写法不得复活。
    """
    src = (HERE / "custom_components" / "huijian_ai" / "huijian" / "http.py").read_text(
        encoding="utf-8")
    i = src.index("class HuijianSatellitesView")
    block = src[i:i + 6200]
    assert 'fw, fw_source = fw_live, ("实时" if fw_available else "台账")' in block, \
        "台账档没吃在线门（断连期仍可谎标实时）"
    assert 'fw, fw_source = fw_live, "实时"' not in block, \
        "ungated 的旧写法回来了（1.6.4 半条修复的原形）"
    assert block.index("if fw_handshake:") < block.index("elif fw_live:"), \
        "三级次序不得倒置（v1.1.40 契约）"
    # 诚实≠藏起来：离线时版本号照显示，只是来源如实落「台账」，不得清空或退回入驻快照
    assert 'else "台账"' in block, "离线档必须如实命名来源"
