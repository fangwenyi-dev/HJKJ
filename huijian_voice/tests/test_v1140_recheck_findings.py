# -*- coding: utf-8 -*-
"""v1.1.40 第四轮复验（`_audit_v1139_recheck.md`）确证真 bug 的逐条钉。

复现记录：`_goldtest/repro_audit_v1139_recheck.py`（修前 `..._BEFORE.log`、
修后 `..._AFTER.log`）。报告 §1 那 10 条"上轮是否真落地"的判定不在此重复钉
（已有 `test_v1139_audit3_findings.py`），本文件只钉本轮的三条：

- **N1（P2，v1.1.39 修 §3[P1] 时新引入）**：`_count_answer` 全屋回退命中后
  `prefix` 仍挂用户说的房间 ⇒「卧室的开着1盏灯：客厅射灯」。行为钉（真入口）。
- **P3-a**：`FFmpegProxyView.get` 裸 `filename.rsplit(".")` 解包 + `defaultdict`
  下标写键，两条都走在 `requires_auth = False` 的 URL 面上。行为钉
  （AST 抠出 `get` 真函数体 exec）+ 块内有界的反向源形状钉。
- **P3-b**：`async_step_qrcode` 里 `internal.split("//")[1]` 裸下标。
  **源形状钉（块内有界）+ 替换进来的 helper 逐形态行为钉**——那一步要整个 HA
  运行时才走得进，本机夹具起不来，端到端列入未验清单。

**发版前对抗复核（`_goldtest/adv_verify_1140.py`）追加三条，全认全修**：
- **本批自引入**：`cross_room` 那句「卧室没有叫「射灯」的设备」在 `_entity_area`
  为空（房间映射拿不到）时是**假阴性**——`find_entities(area=…)` 会把本区那台
  一起滤掉。已加 `_area_binding_known()` 闸（`_sensor_answer`/`_presence_answer`
  同形 fail-open），降级只说"全屋"。
- **既存同族另一半**：`_state_answer` 无类别词 + 本区无可开关设备时把字面
  `None` 念进播报（「阳台没有叫「None」的设备；客厅灯开着。」，`answer()` 端到端
  可复现）。:771 的注释承诺"别把 None 念进话术"只挡住了查无那半边。
- **同类漏网**：`model_store._download_any` 两处 `url.split('/')[2]`（urls 来自
  `models.lock.json`，无 scheme 即 IndexError），其中一处写在
  `raise TimeoutError(...)` 的 f-string 里 ⇒ 异常类型被顶掉。改走 `_src_host()`。

反向不变量与既有正确形态逐字钉住：本轮修法都不许多改文案。
"""
import ast
import asyncio
import re
import sys
import textwrap
from collections import defaultdict
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

web = pytest.importorskip("aiohttp.web")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeHAClient                                  # noqa: E402
from core.nlu.query import QueryZone                               # noqa: E402

CC = ROOT / "custom_components" / "huijian_ai"
FFM_PY = CC / "ffmpeg_proxy.py"
CF_PY = CC / "config_flow.py"
MS_PY = ROOT / "core" / "model_store.py"


# ══════════════════════════════════════════════════════════════════
#  工装
# ══════════════════════════════════════════════════════════════════
def _code(path: Path) -> str:
    """源码去掉行内注释——钉"病灶写法已消失"时不许被描述病灶的注释满足。"""
    return "\n".join(ln.split("#")[0] for ln in path.read_text(encoding="utf-8").splitlines())


def _block(path: Path, name: str, cls: str = "") -> str:
    """按函数/方法名抽出**块内**源码（去注释）。全文件 grep 会撞同名标识符。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    scope = tree
    if cls:
        scope = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == cls), None)
        assert scope is not None, f"{path.name} 找不到类 {cls}（钉桩失去对象）"
    node = next((n for n in ast.walk(scope)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)
    assert node is not None, f"{path.name} 找不到函数 {name}（钉桩失去对象）"
    body = textwrap.dedent(ast.get_source_segment(src, node))
    return "\n".join(ln.split("#")[0] for ln in body.splitlines())


def _exec_fn(path: Path, name: str, cls: str = "", extra: dict | None = None):
    """AST 抠出函数体并 exec（同 test_integration_config_flow 的 _ensure_lan_port_fn 先例）。
    仓内不装 homeassistant，config_flow/ffmpeg_proxy 整模块 import 不进来。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    scope = tree
    if cls:
        scope = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.ClassDef) and n.name == cls)
    node = next((n for n in ast.walk(scope)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)
    assert node is not None, f"{path.name} 找不到 {cls or '<module>'}.{name}（钉桩失去对象）"
    ns = dict(extra or {})
    exec(compile(ast.Module(body=[node], type_ignores=[]), f"<{path.name}:{name}>",
                 "exec"), ns)                                    # noqa: S102
    return ns[name]


# ══════════════════════════════════════════════════════════════════
#  N1：计数跨房间回退后，前缀必须跟着换（不许继续挂用户说的房间）
# ══════════════════════════════════════════════════════════════════
def _ha(spec):
    """spec = [(entity_id, friendly_name, state, area), ...] —— 两区地形。"""
    states, areas, ea = {}, {}, {}
    for i, (eid, name, state, area) in enumerate(spec):
        areas[f"a{i}"] = area
        ea[eid] = area
        states[eid] = {"entity_id": eid, "state": state,
                       "attributes": {"friendly_name": name}}
    return FakeHAClient(states=states, areas=areas, entity_area=ea)


BED2_LIVING1 = [
    ("light.bed_tai_deng", "卧室台灯", "on", "卧室"),
    ("light.bed_deng_dai", "卧室灯带", "on", "卧室"),
    ("light.live_she_deng", "客厅射灯", "on", "客厅"),
]
BED1_LIVING1_OFF = [
    ("light.bed_tai_deng", "卧室台灯", "on", "卧室"),
    ("light.live_she_deng", "客厅射灯", "off", "客厅"),
]
BED1_LIVING1_DEAD = [
    ("light.bed_tai_deng", "卧室台灯", "on", "卧室"),
    ("light.live_she_deng", "客厅射灯", "unavailable", "客厅"),
]


def _ask(ha, text):
    return asyncio.run(QueryZone(ha, None).answer(text))


LEAD = "卧室没有叫「射灯」的设备；"


def test_cross_room_count_on_branch_has_no_contradicting_area():
    """"卧室哪些射灯开着"（卧室压根没射灯，客厅有一盏且开着）。"""
    for utt in ("卧室哪些射灯开着", "卧室有几盏射灯亮着"):
        ans = _ask(_ha(BED2_LIVING1), utt)
        assert ans, f"{utt} 答不上来"
        assert not ans.startswith("卧室的"), f"前缀仍挂用户说的房间：{ans!r}"
        assert ans.startswith(LEAD), f"没先讲本区没有这台（同 _state_answer 口径）：{ans!r}"
        assert "客厅射灯" in ans, f"没点名实际那台：{ans!r}"


def test_cross_room_count_off_branch_not_attributed_to_asked_area():
    """"都不开"支最坏：旧形「卧室的1盏灯都关着呢」既没点名、又把客厅那台算进卧室。"""
    ans = _ask(_ha(BED1_LIVING1_OFF), "卧室哪些射灯开着")
    assert ans and ans.startswith(LEAD), f"{ans!r}"
    assert "都关着呢" in ans and "全屋" in ans, f"跨房间回退后计数口径丢了：{ans!r}"


def test_cross_room_count_uncertain_branch_keeps_honest_state():
    """离线那台：既要跨房间纠前缀，也要保住 v1.1.39 的"不在线/未知不许算关着"。"""
    ans = _ask(_ha(BED1_LIVING1_DEAD), "卧室哪些射灯开着")
    assert ans and ans.startswith(LEAD), f"{ans!r}"
    assert "说不上状态" in ans and "都关着呢" not in ans, f"{ans!r}"


def test_cross_room_count_over_three_branch_keeps_lead():
    """「多则只报数」支同样吃这个前缀——四支出口（on/off/uncertain/>3）漏跟一支就是
    报告 §5.1 讲的「判据表扩了、应答侧没跟上」，逐支钉死。"""
    spec = [("light.bed_tai_deng", "卧室台灯", "on", "卧室")] + [
        (f"light.live_she_{i}", f"客厅射灯{i}", "on", "客厅") for i in range(1, 5)]
    ans = _ask(_ha(spec), "卧室哪些射灯开着")
    assert ans and ans.startswith(LEAD), f"{ans!r}"
    assert "全屋开着4盏灯" in ans, f"{ans!r}"


def test_cross_room_count_and_state_answer_same_discipline():
    """跨房间纪律必须三处消费点同口径（本轮 N1 正是"补了 `_by_name` 漏了前缀"）。

    钉的是**机制**不是文案：状态支与计数支在同一夹具、同一设备词下，都必须先讲
    "本区没有叫「X」的设备"。日后谁给其中一支换法，这一条就红。
    """
    ha = _ha(BED2_LIVING1)
    q = QueryZone(ha, None)
    st = asyncio.run(q._state_answer("卧室", "射灯"))
    ct = asyncio.run(q._count_answer("卧室", "卧室哪些射灯开着"))
    assert st and ct, (st, ct)
    lead = "卧室没有叫「射灯」的设备"
    assert st.startswith(lead) and ct.startswith(lead), f"两支跨房间口径分叉：{st!r} / {ct!r}"


# ── 反向不变量：本轮不许动到的既有正确形态（逐字） ──────────────────
def test_in_area_and_generic_count_answers_verbatim():
    ha = _ha(BED2_LIVING1)
    assert _ask(ha, "卧室哪些台灯开着") == "卧室的开着1盏灯：卧室台灯。"
    assert _ask(ha, "卧室哪些灯开着") == "卧室的开着2盏灯：卧室台灯、卧室灯带。"
    assert _ask(ha, "哪些设备开着") == "开着3个设备：卧室台灯、卧室灯带、客厅射灯。"


def test_absent_everywhere_still_says_so():
    assert _ask(_ha(BED2_LIVING1), "卧室哪些吊灯开着") == "没找到叫「吊灯」的设备。"


def test_generic_or_off_table_word_never_filters():
    """泛称/表外词整域计数的**承重判据**是 `class_of()` 对表外词回 None，
    不是 `_count_answer` 里那道 `_DEVICE_WORDS.get(word)` 冗余闸（变异臂 A4：
    摘掉它本机零差异）。所以钉在契约这一层：表外词必须回 None。"""
    from core.nlu.query import class_of
    assert class_of("哪些设备开着")[0] is None
    assert class_of("客厅有哪些电器开着")[0] is None
    assert class_of("卧室哪些射灯开着")[0] == "射灯"
    ha = _ha(BED2_LIVING1)
    assert _ask(ha, "哪些设备开着") == "开着3个设备：卧室台灯、卧室灯带、客厅射灯。"
    assert LEAD not in _ask(ha, "卧室哪些灯开着"), "泛称被当成跨房间回退"


# ══════════════════════════════════════════════════════════════════
#  P3-a：ffmpeg 代理视图（requires_auth=False 的 URL 面）
# ══════════════════════════════════════════════════════════════════
class _Data:
    def __init__(self):
        self.conversions = defaultdict(list)


class _View:
    def __init__(self, data):
        self.proxy_data = data
        self.manager = None


def _get_fn():
    """exec 出的 `get` 没有模块作用域 ⇒ 它调的 helper 由同文件真函数注入
    （不是替身：`_split_convert_filename` 就是从 ffmpeg_proxy.py 抠出来的本体）。"""
    return _exec_fn(FFM_PY, "get", "FFmpegProxyView",
                    extra={"web": web, "HTTPStatus": HTTPStatus,
                           "_split_convert_filename":
                               _exec_fn(FFM_PY, "_split_convert_filename")})


def _conversions():
    d = _Data()
    d.conversions["dev1"] = [SimpleNamespace(convert_id="abc", media_format="mp3",
                                             proc=None, is_finished=False,
                                             media_url="http://x/y.mp3",
                                             rate=None, channels=None, width=None)]
    return d


def test_proxy_filename_split_never_raises_and_keeps_last_dot():
    f = _exec_fn(FFM_PY, "_split_convert_filename")
    assert f("abc.mp3") == ("abc", "mp3")
    assert f("a.b.mp3") == ("a.b", "mp3")     # 最后一段才是格式
    assert f("abc") == ("", "")               # 无点：交调用方走 400，不抛
    assert f("") == ("", "")


def test_unauthenticated_malformed_url_gets_400_not_traceback():
    """现役 `get` 真函数体：畸形 filename 不许穿出异常（旧形 ValueError ⇒ 500）。"""
    get = _get_fn()
    view = _View(_conversions())
    resp = asyncio.run(get(view, None, "dev1", "a.b.mp3"))
    assert resp.status == HTTPStatus.BAD_REQUEST, f"畸形文件名没落到 400：{resp.status}"
    resp2 = asyncio.run(get(view, None, "dev1", "no-dot"))
    assert resp2.status == HTTPStatus.BAD_REQUEST, f"无点文件名没落到 400：{resp2.status}"


def test_unknown_device_id_404_without_writing_state():
    """陌生 device_id：404 语义不变，但**不许往 defaultdict 里落键**（未认证可无限堆）。"""
    get = _get_fn()
    data = _conversions()
    view = _View(data)
    resp = asyncio.run(get(view, None, "ghost-device", "abc.mp3"))
    assert resp.status == HTTPStatus.NOT_FOUND, resp.status
    assert "ghost-device" not in data.conversions, \
        "未认证请求把 device_id 写进了进程状态（defaultdict 下标形）"


def test_ffmpeg_view_body_has_no_naked_unpack_or_subscript():
    """反向源形状钉（块内有界）：两条病灶写法都不许回到 `get` 体内。
    全文件 grep 会被 helper docstring 里"讲病灶"的同名引用满足 ⇒ 只看函数体、去注释。"""
    body = _block(FFM_PY, "get", "FFmpegProxyView")
    assert "filename.rsplit" not in body, "裸 rsplit 解包回到 get 体内"
    assert "conversions[device_id]" not in body, "defaultdict 下标写键回到 get 体内"
    assert "_split_convert_filename(filename)" in body, "拆分没走永不抛的 helper"


def test_split_helper_is_reachable_from_view_get():
    """接线钉：helper 必须是 `get` 真调用到的那个（定义了就没人用 = 假修）。"""
    src = FFM_PY.read_text(encoding="utf-8")
    assert "def _split_convert_filename" in src
    assert _block(FFM_PY, "get", "FFmpegProxyView").count("_split_convert_filename(") == 1
    # 生成侧仍是"一个点"的 URL 形状 —— helper 的语义前提
    assert "convert_id = secrets.token_urlsafe(16)" in src, \
        "convert_id 字母表若改变（含点），最后一段才是格式的判据要重估"


# ══════════════════════════════════════════════════════════════════
#  P3-b：配网二维码步的局域网主机名（源形状钉 + helper 行为钉）
# ══════════════════════════════════════════════════════════════════
def test_qrcode_step_no_longer_index_splits():
    """反向源形状钉：`haip` 不许再用裸下标切 URL（无 scheme ⇒ IndexError）。"""
    body = _block(CF_PY, "async_step_qrcode", "ConfigFlowHandler")
    assert 'split("//")' not in body, "裸切 '//' 回到扫码步"
    assert "_default_voice_host(self.hass, internal)" in body, "没换成永不抛的 helper"
    # 整行钉（不钉裸 `or internal`：上一行 `external = get_url(...) or internal`
    # 就含同一串，会被它满足成假绿）
    assert re.search(r"haip\s*=\s*_default_voice_host\(\s*self\.hass,\s*internal\s*\)"
                     r"\s+or\s+internal", body), \
        "解析不出 hostname 时要回原文兜底（tip 空白=用户看不到任何地址）"
    assert "haip = " in body, "扫码步不再产出 haip（tip 文案断源）"


def test_default_voice_host_never_raises_shapes():
    """替换进来的 helper 逐形态行为：畸形/IPv6/空串一律不抛。"""
    h = _exec_fn(CF_PY, "_default_voice_host")
    h.__globals__["_ensure_lan_port"] = _exec_fn(CF_PY, "_ensure_lan_port")
    assert h(None, "http://192.168.1.91:8123") == "192.168.1.91"
    assert h(None, "https://ha.example.com") == "ha.example.com"
    assert h(None, "http://[fe80::1]:8123") == "fe80::1"     # 旧裸切给的是 "[fe80"
    assert h(None, "192.168.1.91:8123") == ""                # 旧裸切 IndexError
    assert h(None, "") == ""


def test_same_pathology_not_left_elsewhere():
    """残留复核：两条病灶写法在**本仓跟踪的产品代码**里都不许还有第二处。
    标记取**语句形**（不取裸标识符）——新 helper 的 docstring 里正当引用了
    `filename.rsplit(".")` 用于讲病灶，钉裸标识符会被自己的注释满足。
    口径说清楚：扫描域=`huijian_voice/`（ROOT）；`yyjicheng/` 是 gitignored 的
    商店仓工作副本，本仓明文"不作为测试对象"（`test_v1034_fixes.py:19`），
    CI 打包用的是本仓这一份 ⇒ 它的两处同形病灶记在未收口清单，不在本钉范围。
    `model_store` 的 `url.split('/')[2]` 也不进全局扫（`_src_host` 的 docstring
    正当引用同一串），由 `test_download_any_no_longer_indexes_url` 块内钉。"""
    hits = []
    for p in sorted(ROOT.rglob("*.py")):
        if "tests" in p.parts or "__pycache__" in p.parts or ".worktrees" in p.parts:
            continue
        code = _code(p)
        for marker in ('split("//")[1]', "= filename.rsplit("):
            if marker in code:
                hits.append(f"{p.name}:{marker}")
    assert not hits, f"同类病灶仍在：{hits}"


# ══════════════════════════════════════════════════════════════════
#  发版前对抗复核抓出的两条（一条是本批自引入，一条是既存同族的另一半）
# ══════════════════════════════════════════════════════════════════
def test_degraded_area_binding_makes_no_negative_claim():
    """房间映射拿不到时，"本区没有这台"就是猜的——**本批初版自己带的假阴性**。

    `_entity_area` 为空（注册表 404/权限缺失/实体既没绑房间也没绑设备）时
    `find_entities(area=卧室)` 把卧室那台一起滤掉 ⇒ 全屋回退命中 ≠ 卧室没有。
    红线是"不把不知道的说成知道"：降级只许说"全屋"，状态支退成只报实体本身。
    （v1.1.40 对抗复核 A 段；`_sensor_answer`/`_presence_answer` 早有同形 fail-open。）
    """
    st = {"light.she": {"entity_id": "light.she", "state": "on",
                        "attributes": {"friendly_name": "射灯"}}}
    ha = FakeHAClient(states=st, areas={"a1": "卧室"}, entity_area={})
    ans = _ask(ha, "卧室哪些射灯开着")
    assert ans and "没有叫" not in ans, f"降级态断言本区没有：{ans!r}"
    assert ans.startswith("全屋"), f"降级态前缀不诚实：{ans!r}"
    stt = asyncio.run(QueryZone(ha, None)._state_answer("卧室", "射灯"))
    assert stt and "没有叫" not in stt, f"状态支降级态同样不许断言：{stt!r}"
    assert "射灯" in stt and "None" not in stt, stt


def test_binding_known_still_says_area_absent():
    """反向：映射可用时"本区没有"照说——不许把闸做成"永远不说"（那会退化成假绿）。"""
    assert _ask(_ha(BED2_LIVING1), "卧室哪些射灯开着").startswith(LEAD)


def test_bare_area_question_never_speaks_none():
    """「阳台关了吗」（无类别词 + 本区无可开关设备）不许念出字面 None。

    `_state_answer`:771 的注释早就写明"别把 None 念进话术"，但只挡住了查无
    （`not wider`）那半边；回退命中那半边产出过
    **「阳台没有叫「None」的设备；客厅灯开着。」**——`answer('阳台关了吗')`
    端到端可复现（探针 `_goldtest/adv_verify_1140.py`）。
    """
    st = {"light.ke_ting": {"entity_id": "light.ke_ting", "state": "on",
                            "attributes": {"friendly_name": "客厅灯"}},
          "light.chu_fang": {"entity_id": "light.chu_fang", "state": "off",
                             "attributes": {"friendly_name": "厨房灯"}}}
    ha = FakeHAClient(states=st, areas={"a0": "客厅", "a1": "厨房", "a2": "阳台"},
                      entity_area={"light.ke_ting": "客厅", "light.chu_fang": "厨房"})
    for utt in ("阳台关了吗", "阳台现在都关着吗", "阳台那些设备都关了没"):
        ans = _ask(ha, utt)
        assert "None" not in (ans or ""), f"{utt} 把 None 念进播报：{ans!r}"
        if ans:
            assert "阳台" in ans, f"{utt} 换了话题：{ans!r}"


def test_name_bypass_entity_does_not_trigger_cross_room():
    """真客户端 `ha_client.py:594` 的区域过滤是「绑区不中 **或** 名字含区域词」两路旁路，
    `FakeHAClient` 只有前一路 ⇒ 本钉自带一个按真形过滤的替身。
    不变量：名字里带用户说的区域那台（哪怕实体实际绑在别区）必须走**原路**，
    不许被跨房间回退改口——否则「卧室的开着1盏灯：卧室射灯」这类越区孪生回答
    会从"照用户说的房间念"变成"说本区没有"，那是本批不该碰的既有语义。"""
    class _RealishFilter:
        _areas = {"a0": "卧室", "a1": "客厅"}

        def __init__(self, ents, entity_area):
            self._ents, self._entity_area = ents, entity_area

        async def states(self):
            return dict(self._ents)

        async def get_config(self):
            return {"time_zone": "Asia/Shanghai"}

        async def find_entities(self, area="", domains=(), name_contains=""):
            out = []
            for eid, e in self._ents.items():
                dom = eid.split(".", 1)[0]
                if domains and dom not in domains:
                    continue
                fn = (e.get("attributes") or {}).get("friendly_name") or ""
                if area and self._entity_area.get(eid) != area and area not in fn:
                    continue
                out.append(e)
            return out

    ents = {"light.bf": {"entity_id": "light.bf", "state": "on",
                         "attributes": {"friendly_name": "卧室射灯"}},
            "light.kt": {"entity_id": "light.kt", "state": "on",
                         "attributes": {"friendly_name": "客厅灯"}}}
    ha = _RealishFilter(ents, {"light.bf": "客厅", "light.kt": "客厅"})   # 名字说卧室、绑区说客厅
    ans = asyncio.run(QueryZone(ha, None)._count_answer("卧室", "卧室哪些射灯开着"))
    assert ans == "卧室的开着1盏灯：卧室射灯。", f"越区孪生被跨房间回退改口：{ans!r}"


def test_src_host_never_raises_and_prefers_hostname():
    f = _exec_fn(MS_PY, "_src_host")
    assert f("https://ghcr.io/x/y") == "ghcr.io"
    assert f("http://[fe80::1]:8080/a") == "fe80::1"
    assert f("mirror.org/models/a.tar") == "mirror.org/models/a.tar"   # 无 scheme 回原文
    assert f("") == ""


def test_deadline_message_builds_for_scheme_less_url():
    """对照钉：旧形在同一个 f-string 位置**确实会抛**，新形给出可读源名。
    （旧形那处写在 `raise TimeoutError(f"…{url.split('/')[2]}…")` 里——IndexError
    顶掉 TimeoutError，"超总时限→换源"这条判据连日志一起失效。）"""
    f = _exec_fn(MS_PY, "_src_host")
    url = "internal-mirror/sensevoice.tar"        # 漏写 scheme 的 lock 条目形态
    with pytest.raises(IndexError):
        assert url.split('/')[2]                  # 旧形：下标越界（异常类型被顶掉）
    msg = f"下载超总时限 1800s（源 {f(url)} 已收 0 字节）→ 换源"
    assert "internal-mirror/sensevoice.tar" in msg


def test_download_any_no_longer_indexes_url():
    body = _block(MS_PY, "_download_any")
    assert "split('/')[2]" not in body, "无 scheme 即 IndexError 的裸下标回到下载路径"
    assert body.count("_src_host(url)") == 2, \
        "两处源名展示必须都走永不抛的 helper（漏一处=该处仍能改异常类型）"
