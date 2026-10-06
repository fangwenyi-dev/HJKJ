# -*- coding: utf-8 -*-
"""v1.1.39 第三轮复查（`_audit_v1138_newfindings.md`）的逐条钉。

流程：每条先由我用现役真函数复现（`_goldtest/repro_audit_1139_newfindings.py`，
修后另存 `_goldtest/repro_1139_AFTER.log`），成立才修；全部 10 条成立。

钉的档次写清楚（不拿"源形状钉"冒充端到端）：
- **行为钉**：R5(§1.2) / R1 / R2 / R3 / R4 / R6 / R7 / R10 走真函数真入口；
- **源形状钉（块内有界）**：F8(text.py 回退 blocking) / R8(firmware 索引半边) /
  R9(音色唯一临时名) ——这三条的端到端要么要 edge-tts 合成、要么要真网络源，
  本机夹具做不了，列入未验清单（台架/真机补）。
"""
import asyncio
import sys
import urllib.request
import threading
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeHAClient                                     # noqa: E402
from core import admin_api                                            # noqa: E402
from core import mdns                                                 # noqa: E402
from core import model_store                                          # noqa: E402
from core.model_store import ModelStore                               # noqa: E402
from core.nlu import targets as T                                     # noqa: E402
from core.nlu.fast_path import FastPath, Plan                         # noqa: E402
from core.nlu.query import QueryZone, is_query_like                   # noqa: E402
from core.pipeline import Pipeline                                    # noqa: E402
from test_experience_batch import (HA, Lane, NullQuery, PSettings,    # noqa: E402
                                   RecExecutor)
from test_v1127_transport_creds_ui import CC, _func_src               # noqa: E402

BED = {"a1": "卧室"}


def _qha(states=None, extra=None):
    st = states if states is not None else {
        "light.bed_tai_deng": {"entity_id": "light.bed_tai_deng", "state": "on",
                               "attributes": {"friendly_name": "卧室台灯"}},
        "light.bed_she_deng": {"entity_id": "light.bed_she_deng", "state": "off",
                               "attributes": {"friendly_name": "卧室射灯"}},
        "light.bed_deng_dai": {"entity_id": "light.bed_deng_dai", "state": "on",
                               "attributes": {"friendly_name": "卧室灯带"}},
    }
    st = dict(st)
    st.update(extra or {})
    return FakeHAClient(states=st, areas=BED, entity_area={k: "卧室" for k in st})


def _ask(text, ha=None):
    return asyncio.run(QueryZone(ha or _qha(), None).answer(text))


# ══ R5 §1.2（本批最重）：降级态里 T0 腿也必须有"点名查无"裁决 ═════════
T.sync_vocab({
    "light.ban_gong_shi_she_deng": {"attributes": {"friendly_name": "射灯"}},
    "light.she_deng": {"attributes": {"friendly_name": "射灯"}},
}, {})
T.sync_areas(["办公室"])


class _NoScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


class _NoSettings:
    def get(self, dotted, default=None):
        return default


def _pipe_no_klar():
    p = Pipeline.__new__(Pipeline)
    p.settings = PSettings()
    ha = HA({"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}}})
    ha._areas = {"o": "办公室"}
    p.ha, p.executor, p.agent = ha, RecExecutor(), None
    p.query, p.scenes = NullQuery(), None
    p.fast_path = FastPath(_NoScenes(), None, _NoSettings())
    p.klar = Lane({})                       # ← klar 关闭/未接地的降级态
    p._last, p._turns, p._last_target = OrderedDict(), {}, {}
    p._origin_ts, p._confirm, p._pending = {}, {}, set()
    p._vocab_ts = time.time()
    return p


def test_degraded_t0_leg_still_refuses_absent_named_device():
    for utt in ("关掉会飞的灯", "打开会飞的灯"):
        p = _pipe_no_klar()
        r = asyncio.run(p.handle(utt, origin="o"))
        assert p.executor.plans == [], f"{utt} 降级态真下发了：{[x.intent for x in p.executor.plans]}"
        assert r.source == "no_such_device", f"{utt} 没走查无支：{r.source} {r.text}"
        assert "会飞的灯" in r.text, f"没点名说没找到：{r.text}"


def test_degraded_t0_leg_normal_sentences_still_execute():
    """反向不变量：闸扩到 T0 之后，日常句一条都不许多拦。"""
    for utt in ("关掉办公室的射灯", "打开射灯"):
        p = _pipe_no_klar()
        r = asyncio.run(p.handle(utt, origin="o"))
        assert p.executor.plans, f"{utt} 被查无闸误拒：source={r.source} {r.text}"
        assert not r.text.startswith("没有找到"), r.text


def test_chain_leg_t0_also_refused():
    """链里同样：T0 腿点了查无设备 ⇒ 整链不执行（此前只有 klar 腿被拦）。"""
    p = _pipe_no_klar()
    reply, merged, plans, flag = asyncio.run(
        p._chain_decide("打开办公室的射灯然后关掉会飞的灯", "o"))
    assert merged is None and reply is not None, "链内 T0 查无腿被放走（会谎报都办妥了）"
    assert "会飞的灯" in reply.text, reply.text


# ══ R1 疑问代词补进疑问表后，计数**应答**式同步扩 ═══════════════════
def test_interrogative_determiners_get_answers():
    for utt in ("哪盏灯亮着", "哪台灯开着"):
        assert is_query_like(utt), utt
        ans = _ask(utt)
        assert ans and "灯" in ans, f"{utt} 被让路给查询却无人应答：{ans!r}"


def test_attribution_question_is_not_answered_by_counting():
    """反向：「谁把灯开了」计数答不了，宁回 None，不许编一个"是谁"。"""
    assert _ask("谁把灯开了") is None


# ══ R2 计数必须按用户说的**具体设备词**过滤 ═════════════════════════
def test_count_respects_specific_device_word():
    ans = _ask("卧室哪些射灯开着")
    assert ans and "台灯" not in ans and "灯带" not in ans, \
        f"问射灯却答台灯/灯带（v1.1.18 原则破了）：{ans!r}"
    ans2 = _ask("卧室哪些吊灯开着")
    assert ans2 and "吊灯" in ans2 and "没找到" in ans2, \
        f"家里没吊灯也照报开着的灯：{ans2!r}"


def test_count_generic_word_unchanged():
    """反向不变量：泛称「哪些灯开着」仍是整域计数，一字不许变。"""
    assert _ask("卧室哪些灯开着") == "卧室的开着2盏灯：卧室台灯、卧室灯带。"
    assert _ask("哪些设备开着") is not None


# ══ R3 计数不许把 unavailable/unknown 说成"关着" ════════════════════
UNCERTAIN = {
    "light.b_a": {"entity_id": "light.b_a", "state": "off",
                  "attributes": {"friendly_name": "射灯A"}},
    "light.b_b": {"entity_id": "light.b_b", "state": "unavailable",
                  "attributes": {"friendly_name": "射灯B"}},
    "light.b_c": {"entity_id": "light.b_c", "state": "unknown",
                  "attributes": {"friendly_name": "射灯C"}},
}


def test_count_names_uncertain_states():
    ans = _ask("有几盏灯亮着", _qha(states=UNCERTAIN))
    assert ans and "1盏灯都关着呢" in ans and "不在线或状态未知" in ans, \
        f"把离线/未知折成『都关着呢』：{ans!r}"


def test_count_all_off_without_uncertain_keeps_old_wording():
    """反向不变量：没有未知态时不许多出半句（与 v1.1.18 定的文案一致）。"""
    only_off = {"light.b_a": dict(UNCERTAIN["light.b_a"])}
    assert _ask("有几盏灯亮着", _qha(states=only_off)) == "1盏灯都关着呢。"


# ══ R4 admin_api `_scene_row` 对非 list 的 actions 永不抛 ════════════
def test_scene_row_survives_dirty_actions():
    for acts in ({"foo": 1}, 12, True, "xx", None, []):
        out = admin_api._scene_row({"name": "观影", "actions": acts})
        assert isinstance(out, dict) and out.get("action_count") is not None, acts


def test_scene_row_clean_actions_unchanged():
    """反向：正常 list 形态的计数/摘要行为不变。"""
    out = admin_api._scene_row({"name": "观影",
                                "actions": [{"intent": "TurnDeviceOn"}] * 3})
    assert out["action_count"] == 3, out


# ══ R6 模型下载必须有总时限（滴流源不得钉死单飞线程）════════════════
class _DripResp:
    """read() 永远只吐 1 字节、不 EOF——正是"每 60s 读窗内仍吐极少字节"的形态。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        time.sleep(0.01)
        return b"x"


def test_download_any_has_wall_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "_DL_WALL_TIMEOUT_S", 2)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _DripResp())
    st = ModelStore.__new__(ModelStore)
    st.abort = threading.Event()
    st._status = {}
    st._set_status = lambda *a, **k: None
    entry = {"urls": ["http://drip.invalid/model.bin"], "sha256": "",
             "size_mb": 0, "_key": "probe", "tarball": "model.bin"}
    t0 = time.monotonic()
    ok = st._download_any(entry, tmp_path / "model.bin")
    el = time.monotonic() - t0
    assert ok is False, "滴流源本该判超总时限换源/失败"
    assert el < 20, f"线程被钉死（{el:.1f}s 没返回）——总时限没生效"


def test_download_any_normal_source_still_succeeds(tmp_path, monkeypatch):
    """反向不变量：正常一次读完的源不许被总时限误杀，也不许变慢。"""
    payload = b"huijian-model-bytes"
    
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

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: R())
    st = ModelStore.__new__(ModelStore)
    st.abort = threading.Event()
    st._set_status = lambda *a, **k: None
    entry = {"urls": ["http://ok.invalid/m.bin"], "sha256": "",
             "size_mb": 0, "_key": "k", "tarball": "m.bin"}
    dest = tmp_path / "m.bin"
    assert st._download_any(entry, dest) is True and dest.read_bytes() == payload


# ══ R7 孤儿清扫必须递归（嵌套层的 .part.* 也要清）═══════════════════
def test_sweep_orphans_reaches_nested_dirs(tmp_path):
    st = ModelStore.__new__(ModelStore)
    st.models_dir = tmp_path
    nested = tmp_path / "sensevoice" / "model" / "sub"
    nested.mkdir(parents=True)
    junk = nested / "am-x.part.ab12cd34"
    junk.write_bytes(b"x" * 10)
    top = tmp_path / "top.part.ef56"
    top.write_bytes(b"y")
    real = nested / "real.bin"                      # 解包树里的真文件
    real.write_bytes(b"keepme")
    removed = st.sweep_orphans()
    assert removed == 2, f"只清了顶层（removed={removed}）"
    assert not junk.exists() and not top.exists()
    assert real.exists(), "把非 .part 的真文件删了"


# ══ R10 mDNS close：拆池必须无条件执行 ══════════════════════════════
def test_mdns_close_still_closes_zeroconf_when_unregister_fails():
    calls = []

    class Zco:
        def unregister_service(self, info):
            calls.append("unregister")
            raise RuntimeError("网络栈正在拆")

        def close(self):
            calls.append("close")

    pub = mdns.Publisher.__new__(mdns.Publisher)
    pub._zco = Zco()
    pub._info = object()
    pub.close()
    assert calls == ["unregister", "close"], f"unregister 抛错连坐了 close：{calls}"


def test_mdns_close_normal_path_closes_both():
    calls = []

    class Zco:
        def unregister_service(self, info):
            calls.append("unregister")

        def close(self):
            calls.append("close")

    pub = mdns.Publisher.__new__(mdns.Publisher)
    pub._zco = Zco()
    pub._info = object()
    pub.close()
    assert calls == ["unregister", "close"], calls


# ══ 形状钉（块内有界）：F8 / R8 / R9 ════════════════════════════════
def test_tts_fallback_play_media_is_blocking():
    """主通道 v1.1.27 因同一病灶改了 blocking=True，回退路必须同口径。

    端到端要真 edge-tts 合成＋媒体播放器，本机做不了 ⇒ 台架补，见模块头。"""
    body = _func_src(CC / "text.py", "_play_tts")
    assert "blocking=False" not in body, "回退播报路又回到 blocking=False（假成功复发）"
    assert body.count("blocking=True") >= 1


def test_firmware_index_failure_does_not_claim_total_failure():
    fw = _func_src(ROOT / "core/firmware_store.py", "download")
    i = fw.index("os.replace(tmp, dest)")
    seg = fw[i:i + 1600]
    assert "索引写入失败" in seg, f"索引写失败没被单独兜住：{seg[:220]}"
    # 认**具体返回值**而不是裸 `return True`：落盘成功后必须回 True＋具名待重建提示，
    # 不许折回「所有源失败或校验不符」
    assert 'return True, f"已落盘' in seg, \
        f"落盘成功却被报成失败：{seg[-320:]}"
    assert 'return False, "所有源失败或校验不符"' in fw, "全源真失败的口径不许一起改掉"


def test_voice_upload_uses_unique_tmp_name():
    src = (ROOT / "core/tts_voices_api.py").read_text(encoding="utf-8")
    assert 'f".{name}.bin.tmp.{uuid.uuid4().hex[:8]}"' in src, \
        "音色上传回到固定 .tmp 名（并发可把半写文件转正）"
    assert src.count('".bin.tmp"') == 0
