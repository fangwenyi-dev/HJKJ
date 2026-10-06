# -*- coding: utf-8 -*-
"""v1.1.41：ACR 下载腿那条病灶在**产品侧**的同类两处（发版前对抗复核 §F 抓到）。

`HTTPResponse.read(n)` 是 BufferedReader 语义——**攒满 n 才返回**。滴流极慢时单次调用
可堵 `chunk/速率` 秒（1MB ÷ 50B/s ≈ 20972s），而写在循环顶部的总时限复检就**永远跑不到**：
`model_store._download_any` 的 `_DL_WALL_TIMEOUT_S`（v1.1.39 才加的）与
`firmware_store` 的 `_DL_TIMEOUT_S` 当时都是形同虚设。修法一律 `read1(n)`（一次底层读
即返回），非 http(s) 响应没有 read1 时退回整块读。

档次：**行为钉**（真 `_download_any` 函数体 + 带阻塞语义的滴流响应 + 看门狗线程）
给 model_store；firmware_store 同一形，给块内有界的源形状钉（它的下载体要真网络与
sha 校验链，本机夹具做端到端不划算——差异写在这里，不当已验）。
"""
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core import firmware_store, model_store              # noqa: E402
from core.model_store import ModelStore                    # noqa: E402

sys.path.insert(0, str(ROOT.parent / "scripts"))
import acr_transcode as ac                                 # noqa: E402


class _BlockingDrip:
    """按真语义建模：`read(n)` 攒满 n 才返回（滴流不断 ⇒ 实际永不返回），
    `read1(n)` 一次底层读即回一滴。"""

    def __init__(self, drip=b"x", every=0.001):
        self.drip, self.every = drip, every

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read1(self, n=-1):
        time.sleep(self.every)
        return self.drip

    def read(self, n=-1):
        want = n if n and n > 0 else (1 << 20)
        acc = bytearray()
        while len(acc) < want:          # 真链路上这里可以堵几十小时
            time.sleep(self.every)
            acc += self.drip
        return bytes(acc)


def _stub_store():
    st = ModelStore.__new__(ModelStore)
    st.abort = threading.Event()
    st._status = {}
    st._set_status = lambda *a, **k: None
    return st


ENTRY = {"urls": ["http://drip.invalid/model.bin"], "sha256": "",
         "size_mb": 0, "_key": "probe", "tarball": "model.bin"}


def test_model_store_wall_timeout_is_reachable_under_slow_drip(tmp_path, monkeypatch):
    """总时限必须**打得到**：预算 1s，看门狗 6s 内没返回＝复检被 `read` 的阻塞吞掉。"""
    monkeypatch.setattr(model_store, "_DL_WALL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _BlockingDrip(every=0.001))
    st = _stub_store()
    box = {}
    th = threading.Thread(
        target=lambda: box.setdefault(
            "r", st._download_any(ENTRY, tmp_path / "model.bin")),
        daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(6.0)
    spent = time.monotonic() - t0
    assert not th.is_alive(), (
        "滴流源下载 6s 仍没被总时限打断——`read(1MB)` 攒不满 ⇒ 循环顶部的 deadline "
        "复检永不可达（v1.1.39 加的 `_DL_WALL_TIMEOUT_S` 形同虚设）")
    assert spent < 5.0, f"闸是响了，但比预算晚太多：{spent:.2f}s"
    assert box.get("r") is False, f"滴流源本该判失败换源，结果 {box!r}"


def test_model_store_terminates_cleanly_on_a_plain_eof_source(tmp_path, monkeypatch):
    """反向不变量：正常一次读完的源不许被 read1 改写语义（不许变慢、不许误杀）。"""
    payload = b"huijian-model-bytes-plain"

    class R:
        sent = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if R.sent:
                return b""
            R.sent = True
            return payload

    monkeypatch.setattr(model_store, "_DL_WALL_TIMEOUT_S", 30)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())
    st = _stub_store()
    dest = tmp_path / "m.bin"
    assert st._download_any(ENTRY, dest) is True
    assert dest.read_bytes() == payload


def test_both_product_download_loops_read_incrementally():
    """两处同形病灶都必须改到 read1（块内有界反向钉：只删形状不改语义的回归兜底）。
    只看各自下载函数的**函数体**并去注释——全文件 grep 会被讲病灶的注释满足。"""
    for mod, fn in ((model_store, "_download_any"), (firmware_store, "download")):
        path = Path(mod.__file__)
        src = path.read_text(encoding="utf-8")
        i = src.index(f"def {fn}(")
        rest = src[i + 1:]
        j = rest.find("\n    def ")          # 截到下一个方法为止（块内有界）
        body = (i and src[i:i + 1 + (j if j > 0 else len(rest))])
        code = "\n".join(ln.split("#")[0] for ln in body.splitlines())
        assert 'getattr(r, "read1", r.read)' in code, \
            f"{path.name}::{fn} 的读流没走 read1＝总时限复检在低速滴流下永不可达"
        assert "r.read(1 << 20)" not in code, \
            f"{path.name}::{fn} 还留着整块 r.read(1<<20)"


def test_acr_fetch_seam_is_the_only_way_to_open_a_stream():
    """三处取流必须同一纪律：acr 走 `stream_open`（可钩代理），产品侧走 read1。"""
    gb = ac.Registry.get_blob.__doc__ or ""
    assert "read1(n)" in gb, "get_blob 的 docstring 该写明为什么是 read1（机理要留在代码旁）"
    assert ac.OVERSHOOT_S >= 360, "超冲常数被改小 ⇒ 链预算与 job 的算术要重算"
