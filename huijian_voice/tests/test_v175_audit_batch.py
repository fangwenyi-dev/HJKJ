# -*- coding: utf-8 -*-
"""v1.7.5 逐行审收口批（10-24）的钉：每格一条真行为判据，再加"改回缺陷态"的反向臂。

来源＝10-24 那次逐行审在盘上复现成立的格：
  STT 空结果带 reason 全仓无人消费（现场读来"叫了没反应"）／区域扇出只靠词法闸
  （现网 10:26「推拉窗」被自家"先剔纱窗再找窗"洗成无窗句）／离线同名改指的第二半
  （目标侧区域未知时完全不约束）／原生场景为首腿时 UnboundLocalError（部分执行被藏起来）／
  锁语义两处两套判据／climate 实体被硬喂 humidifier.set_humidity／last_run 实例级单点
  被并发轮互脏／TTS 切坏包塌成全体静音／端点三出口与 mDNS 用启动时一次性采样地址／
  firmware.lock 的 min_compatible 登记了却零消费者。

三条贯穿本文件的规矩（都是本仓实发过的坑）：
  ①判据宇宙必须与真实出口同构：reason 那一格光测 recognize 的分支不够，还要钉线上对象
    真能把这个键取出来（`huijian/__init__.py::Dict.__getattr__`）——漏最后一层＝测了个
    不存在的面。
  ②判"字样在场"之前先剥注释：本批多处修法的注释里逐字写着被治的那个缺陷形（`self._tts = None`
    就在注释里），不剥注释的钉会被自己的说明书喂绿（10-08 同类实发）。
  ③"看不见就不拒"这类**故意保守**的支路要单独钉死：新闸最容易犯的错是把合法路径一起打死，
    那比原缺陷更坏。

跑法：cd huijian_voice && python -m pytest tests/test_v175_audit_batch.py -q
"""
import ast
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v175_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from conftest import FakeHAClient                        # noqa: E402
from core import mdns as MDNS                            # noqa: E402
from core import ota_api as OTA_API                      # noqa: E402
from core import ws_server as WSS                        # noqa: E402
from core.executor import Executor, _lock_domain_target  # noqa: E402
from core.firmware_store import FirmwareStore            # noqa: E402
from core.nlu.fast_path import Plan                      # noqa: E402

CC = HERE / "custom_components" / "huijian_ai" / "huijian"


def _raw(*rel) -> str:
    return io.open(os.path.join(str(HERE), *rel), "rb").read().decode("utf-8", "replace")


def _pync(src: str) -> str:
    """剥 # 行注释（字符串里的 # 不管——本文件只用来判代码形状）。"""
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


SRC_STT = _raw("custom_components", "huijian_ai", "huijian", "stt_transport.py")
SRC_TTS = _raw("core", "tts.py")
SRC_EXEC = _raw("core", "executor.py")
SRC_EXEC_NC = _pync(SRC_EXEC)
SRC_TTS_NC = _pync(SRC_TTS)
SRC_MAIN_NC = _pync(_raw("core", "main.py"))
SRC_ADMIN_NC = _pync(_raw("core", "admin_api.py"))
SRC_OTA_API_NC = _pync(_raw("core", "ota_api.py"))
HTML = _raw("www", "index.html")


# ══ T1 STT「空结果带因」：故障不再塌成 SUCCESS ═══════════════════════════
try:
    import anyio as _ANYIO
except Exception:                                        # pragma: no cover
    _ANYIO = None
_NO_ANYIO = pytest.mark.skipif(_ANYIO is None,
                               reason="集成侧运行期依赖 anyio（CI 装 HA 才有）")


def _recognize_from(src: str):
    """按 AST 取出 SttTransport.recognize 并以最小命名空间执行（与 test_v1064_batch456 同法：
    该文件顶层 import homeassistant，本机与加载项 Lint 的 requirements 里没有它）。"""
    tree = ast.parse(src)
    c = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "SttTransport")
    f = next(n for n in c.body
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "recognize")
    code = "\n".join(l[4:] if l.startswith("    ") else l
                     for l in ast.get_source_segment(src, f).splitlines())
    code = code.replace("_SEND_TIMEOUT_S", "0.3").replace("_FIRST_CHUNK_TIMEOUT_S", "30")
    ns = {"asyncio": asyncio, "anyio": _ANYIO, "logging": logging,
          "_LOGGER": logging.getLogger("t1")}
    exec(compile(code, "<recognize>", "exec"), ns)       # noqa: S102
    return ns["recognize"]


class _Reader:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._items.pop(0)


def _drive(msgs, src: str = None):
    async def _ok(*a, **kw):
        return True

    async def _noop(*a, **kw):
        return None

    self = SimpleNamespace(
        _request_lock=asyncio.Lock(), _drain_stale=lambda: 0,
        ensure_connected=_ok, send_hello=_ok, send_message=_ok,
        restart_connection=_noop, _recv_reader=_Reader(msgs),
        logger=logging.getLogger("t1"), _crossed_total=0)

    async def chunks():
        yield b"\x00" * 8

    fn = _recognize_from(src if src is not None else SRC_STT)
    return asyncio.run(fn(self, chunks()))


def _stt_msg(text, reason=None):
    d = {"type": "stt", "text": text, "rid": 0}
    if reason is not None:
        d["reason"] = reason
    return SimpleNamespace(**d)


@_NO_ANYIO
def test_stt_empty_with_reason_is_error():
    """服务端故障档（模型未就绪/解码失败/超预算/整轮拒收）不得再回"空转写成功"。"""
    text, err = _drive([_stt_msg("", "识别超预算 30s")])
    assert text is None, "带因空串仍被当成功：HA 什么都不播，客户读来就是叫了没反应"
    assert err == "识别超预算 30s"


@_NO_ANYIO
def test_stt_empty_without_reason_is_true_silence():
    """契约里的真静音（text:"" 且不带 reason）照旧是成功——不该为"啊一声没说话"播道歉。"""
    assert _drive([_stt_msg("")]) == ("", None)


@_NO_ANYIO
def test_stt_nonempty_wins_even_if_reason_present():
    """有字就不算失败：这一档补的只是"字面空串＋服务端明确说它不是静音"。"""
    assert _drive([_stt_msg("开灯", "边角噪声")]) == ("开灯", None)


@_NO_ANYIO
def test_stt_reason_is_capped_like_the_server_contract():
    _t, err = _drive([_stt_msg("", "超" * 300)])
    assert err is not None and len(err) == 160, \
        "截断上限与 core/session.py::_reply_stt 的 reason[:160] 同口径，别两边各定一个数"


@_NO_ANYIO
def test_stt_reverse_arm_old_shape_loses_the_error():
    """反向臂：把 `if not text and reason` 收成恒假（＝v1.7.4 缺陷态），带因空串必须重新
    塌成 ("", None)——证明上面那枚判的确实是这一支，而不是碰巧绿。"""
    mut = SRC_STT.replace("            if not text and reason:\n", "            if False:\n", 1)
    assert mut != SRC_STT, "锚点漂移＝本臂在测空气"
    assert _drive([_stt_msg("", "识别超预算 30s")], src=mut) == ("", None)


def test_stt_reason_is_readable_on_the_real_wire_object():
    """最后一层：`data` 是 huijian/__init__.py 的 Dict(dict)。键不存在时 __getattr__ 回
    None（不是抛）⇒ `str(getattr(...) or "")` 折叠成空串；键存在时必须真取到值。
    不钉这一层，上面四枚验的是提取体，线上却可能根本拿不到这个键。"""
    src = _raw("custom_components", "huijian_ai", "huijian", "__init__.py")
    # 该文件顶层 import homeassistant（本机与加项 Lint 的 requirements 里都没有）⇒ 按 AST 只取那个类
    tree = ast.parse(src)
    cd = next(x for x in ast.walk(tree) if isinstance(x, ast.ClassDef) and x.name == "Dict")
    ns = {"json": json}
    exec(compile(ast.get_source_segment(src, cd), "<Dict>", "exec"), ns)   # noqa: S102
    Dict = ns["Dict"]
    d = Dict({"type": "stt", "text": "", "reason": "解码失败 3 帧"})
    assert str(getattr(d, "reason", "") or "") == "解码失败 3 帧"
    d2 = Dict({"type": "stt", "text": ""})
    assert str(getattr(d2, "reason", "") or "") == "", "缺键必须折叠成空串，不许冒出 'None'"


# ══ T2 区域扇出预演：词法闸治不了的那一类 ═══════════════════════════════
def _fan(area_map, states, args, name="HassTurnOn"):
    ex = Executor(FakeHAClient(states=states, entity_area=area_map), None)
    return asyncio.run(ex._area_fanout_refuse(name, args))


def test_fanout_refuses_area_with_unsupported_domain_present():
    """现场形态：区域里混着 button/sensor（开窗器内部实体）⇒ 扇出必成错误风暴 ⇒ 拒。"""
    area = {"button.kc_open": "展厅", "sensor.kc_batt": "展厅", "light.xd": "展厅"}
    st = {"button.kc_open": {}, "sensor.kc_batt": {}, "light.xd": {}}
    assert _fan(area, st, {"area": "展厅"}) is not None


def test_fanout_refuses_area_with_two_toggleable_candidates():
    area = {"light.a": "展厅", "light.b": "展厅"}
    assert _fan(area, {"light.a": {}, "light.b": {}}, {"area": "展厅"}) is not None


def test_fanout_passes_area_with_one_toggleable_and_no_foreign_domain():
    """v1.2.10 那条合法区域批量句（一间屋就一台灯）不许被新闸打死。"""
    assert _fan({"light.a": "展厅"}, {"light.a": {}}, {"area": "展厅"}) is None


def test_fanout_does_not_refuse_unseen_area():
    """看不见这个区域＝不拒：区域表未同步或该区域零实体时，把"看不见"当"危险"会
    连带打死全部合法区域句——那是比原缺陷更坏的过度反应。"""
    assert _fan({}, {}, {"area": "从未同步的屋子"}) is None
    assert _fan({"light.a": "客厅"}, {"light.a": {}}, {"area": "展厅"}) is None


def test_fanout_ignores_named_targets_and_other_intents():
    area = {"light.a": "展厅", "button.b": "展厅"}
    st = {"light.a": {}, "button.b": {}}
    assert _fan(area, st, {"area": "展厅", "entity_id": ["light.a"]}) is None, \
        "已点到台归逐台能力闸管，本闸不抢（否则两闸对同一条句各拒一次）"
    assert _fan(area, st, {"area": "展厅"}, name="HassLock") is None


def test_fanout_refusal_sentence_is_shared_with_the_lexical_gate():
    """两道闸一句文案：本仓"两处各写一份就总有一处漏"记过不止一次。"""
    msg = _fan({"button.b": "展厅", "light.a": "展厅"},
               {"button.b": {}, "light.a": {}}, {"area": "展厅"})
    assert msg and "不敢把整屋设备冒按" in msg
    assert SRC_EXEC.count("不敢把整屋设备冒按") == 2, \
        "一句文案两处各写一份＝两处会漂；本钉把计数钉死（闸本体那条 + 新预演那条）"


class HaSvc(FakeHAClient):
    """FakeHAClient 补 call_service 留痕（服务直调通道）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}


def test_fanout_is_wired_into_run_and_blocks_both_channels():
    """接线走真入口：拦下时 intent 与服务直调两通道都必须零外发。"""
    ha = HaSvc(states={"button.b": {}, "light.a": {}},
               entity_area={"button.b": "展厅", "light.a": "展厅"})
    ok, msg = asyncio.run(Executor(ha, None).run(
        Plan(intent="HassTurnOn", args={"area": "展厅"}, source="klar",
             utterance="打开展厅推拉")))
    assert ok is False and "不敢把整屋设备冒按" in msg
    assert ha.calls == [] and ha.svc_calls == []
    # 反向臂：摘掉 run() 里那次调用 ⇒ 行为判据必红（这里自证它确实会外发）
    mut = SRC_EXEC_NC.replace("fan = await self._area_fanout_refuse(name, args)",
                              "fan = None", 1)
    assert mut != SRC_EXEC_NC and "await self._area_fanout_refuse(name, args)" not in mut


# ── T2b（10-24 复核 BUG-4/5 追加）：覆盖面必须跨两种 args 形状 ──────────────
_NATIVE = lambda area, doms=None, name="": {
    "target": [{"area": area,
                "devices": [{"name": name,
                             "domains": list(doms or [])}]}]}
_DANGER = {"button.b": "展厅", "light.a": "展厅", "light.z": "展厅"}
_DANGER_ST = {"button.b": {}, "light.a": {}, "light.z": {}}


def test_fanout_covers_native_turndeviceon_shape():
    """BUG-4 的前半：自有开关意图族的**整区展开形**必须进闸。
    旧实现只读 `args["area"]` ⇒ `TurnDeviceOn{target:[{area,…}]}` 全族走不到闸
    （v1.0.90 把自有形补进 `_TURN_FAMILY` 时记过同形教训，我这次重犯了一半）。"""
    for name in ("TurnDeviceOn", "TurnDeviceOff", "ToggleDevice"):
        got = _fan(_DANGER, _DANGER_ST, _NATIVE("展厅"), name=name)
        assert got is not None, "%s 的整区展开形没进闸＝覆盖面只有一半" % name
        assert "不敢把整屋设备冒按" in got


def test_fanout_named_only_leg_is_deliberately_not_pre_simulated():
    """BUG-4 的后半，方向相反的那一半：**只带名称约束**的自有形不预演，是设计而不是漏。
    集成按"同域＋名称包含"回捞（`_area_bulk_target` 的具名类别词道），我们复刻不出那个
    匹配器；拿整区唯一性判据去拒它，打死的是 v1.2.10/审计 P1 都在保的合法句
    ——「打开客厅的灯」正是被这样误拒过一次（`test_p1_chain_first_leg_uses_own_clause`）。
    这一形的危险面由词法闸 `_turn_gate` 与逐台能力闸 `_capability_refuse` 守。"""
    assert _fan(_DANGER, _DANGER_ST, _NATIVE("展厅", name="展厅"),
                name="TurnDeviceOff") is None


def test_fanout_native_with_domain_list_does_not_refuse_multi_toggleable():
    """v1.2.10 的合法区域批量句（「关闭展厅所有灯」带 domains=[light]）不许被新闸打死：
    清单内多台是用户明说的批量语义，唯一性判据只对"整区冒按"那一形成立。"""
    assert _fan(_DANGER, _DANGER_ST, _NATIVE("展厅", doms=["light"]),
                name="TurnDeviceOn") is None


_MIXED = {"sensor.t1": "展厅", "light.a": "展厅", "light.z": "展厅", "button.b": "展厅"}
_MIXED_ST = {"sensor.t1": {}, "light.a": {}, "light.z": {}, "button.b": {}}


def test_fanout_native_refuses_when_the_requested_domain_is_not_toggleable():
    """带域清单时改判「清单自身」：请求的域里含非可开关域＝这条真会去 turn_on 一个传感器。
    ⚠ 本臂的 fixture 里必须有那个域的真实体——第一版我拿"区域里没有 sensor"的表去请求
    sensor 域，闸按「清单与本区域无交集＝没看见」放行，测的是另一条形。"""
    got = _fan(_MIXED, _MIXED_ST, _NATIVE("展厅", doms=["sensor", "light"]),
               name="TurnDeviceOn")
    assert got is not None and "不敢把整屋设备冒按" in got
    # 同一块区域、只请求 light ⇒ 合法批量句，不许被拒（上一条与这一条互为反向臂）
    assert _fan(_MIXED, _MIXED_ST, _NATIVE("展厅", doms=["light"]),
                name="TurnDeviceOn") is None
    # 清单里唯一的域就是不可开关域 ⇒ 必拒
    assert _fan(_MIXED, _MIXED_ST, _NATIVE("展厅", doms=["sensor"]),
                name="TurnDeviceOn") is not None


def test_fanout_refuses_all_sensor_area():
    """BUG-5：旧实现拿 `toggleable == 0` 当"不认识这个区域"，于是**全传感器区域**
    （温湿度/电量/门磁）被当成不认识而放行——那恰是 docstring 第一条要拒的形态。
    现口径："没看见"＝该区域在状态缓存里 matched==0，与可开关台数无关。"""
    area = {"sensor.t1": "展厅", "sensor.t2": "展厅"}
    st = {"sensor.t1": {}, "sensor.t2": {}}
    got = _fan(area, st, {"area": "展厅"})
    assert got is not None, "全传感器区域被放行＝谎报成功族残留（0 台可开关也可能被展开喂 turn_on）"


def test_fanout_still_passes_truly_unseen_area():
    """与上一条形同、语义相反的那一侧仍要成立：区域表里**完全没有这个区域**＝不拒。
    （展厅有两个传感器＝看得见 ⇒ 拒；"从未同步的屋子"零实体＝看不见 ⇒ 不拒。）"""
    assert _fan({"sensor.t1": "客厅"}, {"sensor.t1": {}},
                {"area": "从未同步的屋子"}) is None


def test_fanout_shape_pins_on_source():
    """形状钉＋反向臂：判据必须读两个形状、且"没看见"必须是 matched==0。"""
    src = _raw("core", "executor.py")
    i = src.find("def _fanout_scope")
    assert i > 0
    head = src[i:i + 2600]
    assert 'args["area"]' in head or '.get("area")' in head, "Hass 形那一半不许丢"
    assert '.get("target")' in head, "自有形那一半不许丢（BUG-4 的回归位）"
    j = src.find("async def _area_fanout_refuse")
    body = src[j:j + 5200]
    assert "if matched == 0:" in body, "把「没看见」退回 toggleable==0＝BUG-5 回来"
    assert "if toggleable == 0:" not in body, "旧口径还留在场＝两口径并存，下一次改动挑哪个都行"
    # 反向臂：摘掉 target 半边 ⇒ 上面那条形同判据必须转红（证明它确实盯着这一半）
    mut = head.replace('for slot in ((args or {}).get("target") or []):',
                       'for slot in []:', 1)
    assert mut != head and '.get("target")' not in mut


def test_tts_no_lone_pass_left_by_the_fix():
    """BUG-6：清掉那句 `self._tts = None` 后留下的空 `pass`（行为安全，但它是"这里以前
    有件事、现在没人负责"的误导形状）。"""
    src = _raw("core", "tts.py")
    seg = src[src.find("引擎换绑：%s → %s") - 3000:src.find("引擎换绑：%s → %s")]
    assert "\n                pass\n" not in seg, "空 pass 又回来了"


# ══ T3 原生场景为首腿：不再 UnboundLocalError ════════════════════════════
def _run_body() -> str:
    tree = ast.parse(SRC_EXEC)
    c = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Executor")
    f = next(n for n in c.body
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    return ast.get_source_segment(SRC_EXEC, f)


def test_scene_leg_initialises_direct_before_the_branches():
    body = _run_body()
    i_init = body.find("direct = None")
    i_scene = body.find('if name == "TriggerHaScene":')
    assert 0 <= i_init < i_scene, \
        "direct 过去只在 else 支里赋值 ⇒ 场景为首腿时循环尾的日志读未定义名"
    assert 'direct = ("scene", "turn_on", {"entity_id": _eid})' in body, \
        "场景支也要走同一条 direct 形状（它本来就是服务直调，日志标 intent 是第二处不实）"


def test_scene_leg_reverse_arm_deleting_the_init_is_caught():
    body = _run_body()
    mut = body.replace("            direct = None\n", "", 1)
    assert mut != body and "\ndirect = None" not in "\n" + mut, "本臂的锚点必须真能删掉那一行"


def test_scene_first_chain_runs_second_leg_without_raising():
    """行为臂：「观影场景然后关灯」两步链——场景腿真直调、后续腿照执行、永不抛。"""
    ha = HaSvc(states={"light.ks": {}})
    ex = Executor(ha, None)
    plan = Plan(intent="TriggerHaScene", args={"entity_id": "scene.guan_ying"},
                source="klar", utterance="观影场景然后关灯",
                extra_steps=[{"name": "HassTurnOff",
                              "args": {"entity_id": ["light.ks"]}}])
    ok, _msg = asyncio.run(ex.run(plan))
    assert ("scene", "turn_on", {"entity_id": "scene.guan_ying"}) in ha.svc_calls, \
        "场景腿没真下去＝这条链第一步就是空的"
    assert ok is not None                                 # 返回了＝没抛 UnboundLocalError


# ══ T4 离线同名改指的第二半 ═════════════════════════════════════════════
_OFF = {"state": "unavailable", "attributes": {"friendly_name": "射灯"}}
_ON = {"state": "on", "attributes": {"friendly_name": "射灯"}}


def _repoint(entity_area, target_key="light.a1", cand_key="light.a2"):
    ha = FakeHAClient(states={target_key: dict(_OFF), cand_key: dict(_ON)},
                      entity_area=entity_area)
    args = {"entity_id": [target_key]}
    asyncio.run(Executor(ha, None)._repoint_offline_twin(args))
    return args["entity_id"]


def test_repoint_documented_tradeoff_is_not_quietly_tightened():
    """10-24 我自己把这里判成"同一缺陷的第二半"并收紧过，**盘上有反证 ⇒ 已回退**，这枚钉记住结论：
    目标侧区域未知、可用同名台在办公室时**照旧救回**（v1.1.15 明示口径："那不是猜台……
    跨区才是确证的错"），而 v1.1.17 要求播报**点名改指到哪一间**——这是已披露的权衡，不是谎报。
    要再收紧它，先拿出"披露了区域仍被误动"的现场证据；否则 `tests/test_offline_twin_repoint.py::
    test_repoint_note_names_the_area`（那台灯正是这个形状）会先红。"""
    assert _repoint({"light.a2": "办公室"}) == ["light.a2"]
    src = _raw("core", "executor.py")
    assert "跨区才是确证的错" in src, "口径注释被删＝这条权衡又变成没人认领的暗改动"
    _tight = 'if not str(area_map.get(c) or "").strip()]'
    assert _tight not in src, "那个被回退的收紧形又回来了（无现场证据）"


def test_repoint_still_rescues_when_both_sides_area_unknown():
    """两侧都不知道区域：同名全等＋同域＋恰好一台仍是硬条件 ⇒ 照旧救回（不过度收）。"""
    assert _repoint({}) == ["light.a2"]


def test_repoint_still_rescues_within_the_same_area():
    assert _repoint({"light.a1": "办公室", "light.a2": "办公室"}) == ["light.a2"]


def test_repoint_still_refuses_known_cross_area():
    assert _repoint({"light.a1": "客厅", "light.a2": "办公室"}) == ["light.a1"]


# ══ T5 锁语义：播报与执行同一套判据 ═════════════════════════════════════
def test_lock_speech_follows_domain_not_the_word_suo():
    """实体名叫"大门"的锁：动作按 D7 真去 lock/unlock，话不许播"大门关了"（与动作相反）。"""
    ex = Executor(None, None)
    got = ex._targets_speech(
        Plan(intent="TurnDeviceOn", args={"entity_id": "lock.da_men"},
             source="klar", utterance="打开大门"),
        [{"name": "大门"}])
    assert "已上锁" in got, got


def test_lock_judge_shares_the_execution_predicate():
    """结构臂：文案侧必须调执行侧那一个函数；名字含「锁」只留兜底。"""
    assert "lockish = bool(_lock_domain_target(args)) or any(" in SRC_EXEC_NC
    mut = SRC_EXEC_NC.replace("lockish = bool(_lock_domain_target(args)) or any(",
                              "lockish = False or any(", 1)
    assert mut != SRC_EXEC_NC                            # 反向臂的锚点在位


def test_lock_domain_target_basics():
    assert bool(_lock_domain_target({"entity_id": "lock.a"})) is True
    assert bool(_lock_domain_target({"entity_id": ["light.a"]})) is False,         "非锁域折**假**而不是 None——两处消费点用 `is None` 与 `not x` 会走不同分支，判据换形时这里先红"


# ══ T6 湿度意图按 grounded 实体的域选服务 ════════════════════════════════
def test_humidity_picks_climate_service_for_climate_entity():
    got = Executor(ha=None)._klar_direct("HassClimateSetHumidity",
                                         {"entity_id": "climate.ac_1", "humidity": 55})
    assert got == ("climate", "set_humidity",
                   {"entity_id": "climate.ac_1", "humidity": 55}), \
        "HA 里 set_humidity 两域都有；钉死 humidifier 时落到空调上必败"


def test_humidity_still_uses_humidifier_for_humidifier_entity():
    got = Executor(ha=None)._klar_direct("HassClimateSetHumidity",
                                         {"entity_id": "humidifier.h", "humidity": 60})
    assert (got[0], got[1]) == ("humidifier", "set_humidity")


def test_climate_set_humidity_is_in_the_real_service_contract_table():
    """代码能发出的服务必须同批进"HA 真实服务核对表"，否则那张表漏一格、下次改动没人核它。"""
    src = _raw("tests", "test_window_position.py")
    assert '("climate", "set_humidity")' in src
    from tests.test_window_position import test_klar_service_table_names_are_valid as _t
    _t()                                                  # 既有钉不许被我改坏


# ══ T7 last_run 并发轮隔离 ══════════════════════════════════════════════
def test_last_run_is_per_task_not_per_instance():
    """Executor 是进程单例：并发两轮各读各的，后写不得覆盖先写。
    ⚠ 整段跑在**一条 asyncio 任务**里（含开头的种值）——property 的写会落进当前上下文，
    在 pytest 的进程主上下文里写就等于给**后面所有测试**种了一个陈旧桶
    （v1.7.5 实发：本文件先跑、test_audit4_p2 的 indeterminate 钉随后 KeyError）。"""
    ex = Executor(FakeHAClient(states={}), None)
    seen = {}

    async def inner():
        ex.last_run = {"tag": "inner"}
        await asyncio.sleep(0)
        seen["inner"] = ex.last_run["tag"]

    async def main():
        ex.last_run = {"tag": "outer"}                 # 本任务先写自己的桶
        t = asyncio.create_task(inner())               # 子任务复制上下文后再改写自己的
        await t
        seen["outer"] = ex.last_run["tag"]             # 外层读到的是外层写的
        return seen

    out = asyncio.run(main())
    assert out["inner"] == "inner"
    assert out["outer"] == "outer",         "外层读到 inner 的桶＝并发轮互脏（pipeline 复议安全闸吃的正是这个读数）"


def test_last_run_falls_back_to_instance_slot_for_a_fresh_task():
    """新任务没写过 ⇒ 必须回落实例槽（面板读"最近一轮"的旧语义不许丢）。"""
    ex = Executor(FakeHAClient(states={}), None)

    async def seed_then_read():
        ex.last_run = {"tag": "written-by-a-turn"}      # 在任务上下文里写
        return ex.last_run["tag"]

    async def fresh_reader():                           # 另一条任务：本上下文没写过
        return ex.last_run["tag"]

    assert asyncio.run(seed_then_read()) == "written-by-a-turn"
    assert asyncio.run(fresh_reader()) == "written-by-a-turn",         "回落实例槽失效＝面板/遥测在新任务里读到空桶"


def test_last_run_reads_the_latest_turn_from_a_foreign_context():
    """本批**自己踩到**的坑（由 test_audit4_p2 的 indeterminate 钉抓出，这里固化成独立判据）：
    `__init__` 若经 property 写，零轮初值就被种进装配任务自己的上下文，此后任何没在本任务
    跑过轮的读方（另一条 asyncio.run 腿、面板、遥测）读到的都是那枚初值而不是最近一轮
    ——getter 那条"没写过就回落实例槽"的兜底路被它自己的构造堵死。
    跨任务隔离这条改动本身是对的，但它**新增**了一个失败面：只有这根钉能记住它。"""
    ha = FakeHAClient(results={"TurnDeviceOn": {"success": False,
                                                "message": "HA 通道异常",
                                                "indeterminate": True}})
    ex = Executor(ha, None)
    asyncio.run(ex.run_raw(Plan("TurnDeviceOn", {"entity_id": "light.x"}, "t0")))
    assert ex.last_run["indeterminate"] is True,         "读到的是 __init__ 种的零轮桶＝跨上下文兜底失效（连接类失败的不确定性被丢）"
    assert ex.last_run["steps"] == 1 and ex.last_run["applied"] == 0


def test_last_run_wiring_shape():
    assert "_LAST_RUN: ContextVar" in SRC_EXEC_NC
    i = SRC_EXEC_NC.find("@property")
    assert "_LAST_RUN.get()" in SRC_EXEC_NC[i:i + 900]
    # 反向臂：property 退成实例属性（并发互脏的原始形态）——判据读不到 ContextVar 取值点
    mut = SRC_EXEC_NC.replace("_LAST_RUN.get()", "self._last_run_store", 1)
    assert mut != SRC_EXEC_NC


# ══ T8 TTS 切坏包不再塌成全体静音（判代码，注释里写着缺陷形）═══════════
def _method_of(src_nc: str, needle: str) -> str:
    """取 needle 所在的那个**方法体**。用魔数窗长会把邻居圈进来（`unload()` 里那句
    `self._tts = None` 是合法的），窗开太小又会漏判——方法边界才是这件事的真形状。"""
    pos = src_nc.find(needle)
    if pos < 0:
        raise AssertionError("地标缺失：%r（本臂在测空气）" % needle[:40])
    starts = [m.start() for m in re.finditer(r"^    (?:async )?def ", src_nc, re.M)]
    prev = [x for x in starts if x < pos]
    nxt = [x for x in starts if x > pos]
    a = max(prev) if prev else 0
    b = min(nxt) if nxt else len(src_nc)
    return src_nc[a:b]


def test_tts_switch_and_failure_no_longer_null_the_engine():
    body = _method_of(SRC_TTS_NC, "引擎换绑：%s → %s")
    assert "_ensure_loaded_inner" in body.splitlines()[0], body.splitlines()[0][:60]
    assert "self._tts = None" not in body, "换绑段清了旧档＝坏包直接全体静音"
    assert "self._tts = tts" in body, "替换只能发生在构造全部成功之后"


def test_tts_failure_branch_also_keeps_the_old_engine():
    """except 段（[TTS] 加载失败）同属这个方法；单独点名是为了"以后有人只修换绑段"时
    能看出红在哪一条腿。"""
    body = _method_of(SRC_TTS_NC, "[TTS] 加载失败")
    i = body.find("[TTS] 加载失败")
    assert "self._tts = None" not in body[i:i + 600]
    assert "return False" in body[i:i + 600]


def test_tts_reverse_arm_restoring_the_clear_is_caught():
    body = _method_of(SRC_TTS_NC, "引擎换绑：%s → %s")
    mut = body.replace("            self._cache.clear()",
                       "            self._tts = None\n            self._cache.clear()", 1)
    assert mut != body, "锚点漂移＝本臂在测空气"
    assert "self._tts = None" in mut        # 缺陷形必须真能被上面那条抓到


# ══ T9 mDNS / 端点三出口：实时取址 ══════════════════════════════════════
def test_lan_ip_folds_loopback_to_empty():
    old = MDNS.local_ip
    try:
        MDNS.local_ip = lambda: "127.0.0.1"
        assert MDNS.lan_ip() == "", "回环地址发给设备＝对方必然连不上还看不出为什么"
        MDNS.local_ip = lambda: "192.168.1.63"
        assert MDNS.lan_ip() == "192.168.1.63"
        MDNS.local_ip = lambda: ""
        assert MDNS.lan_ip() == ""
    finally:
        MDNS.local_ip = old


def test_republish_only_when_address_actually_changed():
    old = MDNS.local_ip
    p = MDNS.Publisher(port=8000, props={"ver": "x"}, version="x")
    p._zco, p._info, p._ip = object(), object(), "192.168.1.10"
    calls = []
    p.close = lambda: calls.append("close")
    p.start = lambda: calls.append("start")
    try:
        MDNS.local_ip = lambda: "192.168.1.10"
        assert p.republish_if_changed() is False and calls == [], "无变化必须零副作用"
        MDNS.local_ip = lambda: "192.168.1.99"
        assert p.republish_if_changed() is True
        assert calls == ["close", "start"], \
            "重广播走拆+注册：属性只读那条路实测不生效（见 mdns.py 里 v1.1.27 那段注）"
        MDNS.local_ip = lambda: ""
        p._ip = ""
        assert p.republish_if_changed() is False, "取不到地址时不许把广播拆成空"
    finally:
        MDNS.local_ip = old


def test_unstarted_publisher_republish_is_inert():
    assert MDNS.Publisher().republish_if_changed() is False, "没起过广播就没有可拆的（不得抛）"


class _Disc:
    def __init__(self, require=False, token="tok123"):
        self.d = {"security": {"require_token": require, "ws_token": token}}

    def get(self, k, default=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def endpoint_urls(self, host):
        return {"stt_endpoint": "ws://%s:8000/xiaozhi/v1/stt?token=tok123" % host,
                "tts_endpoint": "ws://%s:8000/xiaozhi/v1/tts" % host,
                "llm_endpoint": "ws://%s:8000/xiaozhi/v1/llm" % host}


def _http_get(port, path, loop):
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:%d%s" % (port, path)) as r:
                return r.status, await r.json()
    return asyncio.run_coroutine_threadsafe(go(), loop).result(15)


async def _serve(app, loop_box):
    import aiohttp
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    await aiohttp.web.TCPSite(runner, "127.0.0.1", 0).start()
    loop_box["runner"] = runner
    return runner.addresses[0][1]


def test_discover_uses_live_ip_not_the_one_time_snapshot():
    """/discover 是设备拿端点的地方：ctx.host 的冻结值必须被实时地址顶掉。"""
    import aiohttp
    old = MDNS.local_ip
    ctx = WSS.AppContext(settings=_Disc(), host="10.0.0.5")     # 启动时采样的旧地址
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
                     daemon=True).start()
    box = {}
    try:
        MDNS.local_ip = lambda: "192.168.1.77"
        port = asyncio.run_coroutine_threadsafe(_serve(WSS.make_ws_app(ctx), box),
                                                loop).result(15)
        st, j = _http_get(port, "/discover", loop)
        assert st == 200
        assert j["endpoints"]["stt"].startswith("ws://192.168.1.77:8000/"), j["endpoints"]
        assert "10.0.0.5" not in json.dumps(j), "冻结的启动采样仍然在对外端点里"
        loop.call_soon_threadsafe(loop.stop)
    finally:
        MDNS.local_ip = old


def test_other_two_exits_use_the_live_ip_too():
    """另两个出口（endpoints.json 事实文件、:8002 /api/endpoints）同一口径——
    三处各写一份就总有一处漏（本批审出的正是"OTA 那条已实时取址、剩下半截没取"）。"""
    assert "endpoint_urls(lan_ip() or self.host)" in SRC_MAIN_NC
    assert "host = lan_ip() or ctx.host" in SRC_ADMIN_NC
    # 反向臂：任一处退回一次性采样 ⇒ 上面两条字样判据必红（这里自证锚点真能摘掉）
    for src, needle in ((SRC_MAIN_NC, "endpoint_urls(lan_ip() or self.host)"),
                        (SRC_ADMIN_NC, "host = lan_ip() or ctx.host")):
        assert needle not in src.replace(needle, "endpoint_urls(self.host)", 1)


def test_models_gate_hooks_republish():
    """接线臂：地址巡检不是写了没人调——60s 保障循环里真调，且只在变了时重写事实文件。"""
    i = SRC_MAIN_NC.find("if self.mdns.republish_if_changed():")
    assert i > 0
    assert "_write_endpoints()" in SRC_MAIN_NC[i:i + 200]
    assert "return _MODELS_CHECK_S" in SRC_MAIN_NC[i:i + 800], \
        "巡检必须挂在模型保障循环里（否则每轮都拆重建一次广播）"
    mut = SRC_MAIN_NC.replace("if self.mdns.republish_if_changed():", "if False:", 1)
    assert mut != SRC_MAIN_NC


# ══ T10 firmware.lock 的 min_compatible：从"登记"变成"执行" ═════════════
def _fake_app_image(size=8192):
    """过形态闸的最小 app 镜像形：首字节 0xE9（ESI 头）＋ 0x20 处 esp_app_desc 魔数。"""
    b = bytearray(size)
    b[0] = 0xE9
    b[0x20:0x24] = b"\x32\x54\xcd\xab"
    return bytes(b)


def _store_with_lock(tmp, min_compatible="2.2.9", version="2.3.11"):
    root = Path(tmp) / "fw"
    lock = Path(tmp) / "firmware.lock.json"
    body = _fake_app_image()
    (root / "public").mkdir(parents=True, exist_ok=True)
    fname = "huijian-s3-%s.bin" % version
    (root / "public" / fname).write_bytes(body)
    lock.write_text(json.dumps({"releases": [{
        "version": version, "file": fname,
        "sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
        "urls": {"github": "https://example.invalid/a.bin"},
        "min_compatible": min_compatible, "notes_zh": "测试用"}]}, indent=1), encoding="utf-8")
    return FirmwareStore(root=root, lock_path=lock), version


def test_lock_min_compatible_lands_on_the_row():
    with tempfile.TemporaryDirectory() as tmp:
        st, v = _store_with_lock(tmp)
        row = next(r for r in st.versions() if r["version"] == v)
        assert row["min_compatible"] == "2.2.9", "锁里登记的下限必须进账目面，否则又是个死字段"


def test_min_compatible_gate_refuses_older_device_and_reports_why():
    with tempfile.TemporaryDirectory() as tmp:
        st, v = _store_with_lock(tmp)
        why = st.ota_block_reason(v, "2.1.79")
        assert "要求设备现版本 ≥ v2.2.9" in why and "USB 双段" in why, why
        assert st.issue(v, "aa:bb:cc", "2.1.79") is None


def test_min_compatible_gate_does_not_fire_without_a_reading():
    """拿不到实时读数≠"这台一定能升"，也不该把近场兜底签发堵死——按旧行为放行。"""
    with tempfile.TemporaryDirectory() as tmp:
        st, v = _store_with_lock(tmp)
        assert st.ota_block_reason(v, "") == "", st.ota_block_reason(v, "")
        assert st.ota_block_reason(v, "2.3.9") == ""
        assert st.issue(v, "aa:bb:cc", "2.3.9") is not None


def test_min_compatible_is_older_of_the_two_sides_not_invented():
    """键恒在（面板/判据读它不该关心这行来自锁还是投递口）；锁里没有的版本不许凭空造下限。"""
    with tempfile.TemporaryDirectory() as tmp:
        st = FirmwareStore(root=Path(tmp) / "fw2",
                           lock_path=Path(tmp) / "missing.lock.json")
        body = _fake_app_image(2048)
        f = st.import_dir / "huijian-s3-9.9.9.bin"      # 投递口包：文件名必须带 x.y.z
        f.write_bytes(body)
        stale = __import__("time").time() - 30
        os.utime(f, (stale, stale))                     # mtime<2s 被反竞态闸当"还在拷"跳过
        rows = st.versions()
        assert rows, "构造的投递包必须出现在清单面（否则下面几条在测空气）"
        for r in rows:
            assert "min_compatible" in r, "键必须恒在：读它的人不该关心这行来自锁还是投递口"
            assert r["min_compatible"] == "", "无锁登记 ⇒ 没有下限可继承，不许凭空造一个"


def test_capacity_and_shape_gates_still_run_after_the_new_gate():
    """新闸排在既有闸之后判，但**不许顶掉**它们：大体积/坏形态仍要各自给因。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "fw3"
        lock = Path(tmp) / "lock.json"
        body = bytearray(_fake_app_image(2048))
        body[0x20:0x24] = b"\x00\x00\x00\x00"            # 合并镜像形：0x20 处不是 esp_app_desc
        (root / "public").mkdir(parents=True, exist_ok=True)
        (root / "public" / "merged.bin").write_bytes(bytes(body))
        lock.write_text(json.dumps({"releases": [{
            "version": "2.4.0", "file": "merged.bin",
            "sha256": hashlib.sha256(bytes(body)).hexdigest(), "size": len(body),
            "urls": {}, "min_compatible": "2.2.9"}]}, indent=1), encoding="utf-8")
        st = FirmwareStore(root=root, lock_path=lock)
        assert "要求设备现版本" not in st.ota_block_reason("2.4.0", "2.3.9")


# ══ T11 OTA 面板：incompatible 档与"只认实时读数" ════════════════════════
class _SatHa(FakeHAClient):
    def __init__(self, rows):
        super().__init__()
        self._r = {"/api/huijian-ai/satellites": {"devices": rows}}

    async def rest_get(self, path, timeout=6.0):
        return self._r.get(path)


def test_live_device_version_only_accepts_realtime_reading():
    ctx = SimpleNamespace(ha=None)
    rows_live = [{"mac": "aabb", "fw_source": "实时", "fw_version": "2.3.10"}]
    rows_snap = [{"mac": "aabb", "fw_source": "入驻时", "fw_version": "2.3.10"}]

    async def go(rows):
        c = SimpleNamespace(ha=_SatHa(rows))
        return await OTA_API._live_device_version(c, "aabb")

    assert asyncio.run(go(rows_live)) == "2.3.10"
    assert asyncio.run(go(rows_snap)) == "", \
        "「入驻时」快照可能是几周前的版本，拿它做否决＝用不可信的值断言（1.6.1/1.6.4 两次实锤）"
    assert asyncio.run(go([])) == ""
    assert asyncio.run(OTA_API._live_device_version(SimpleNamespace(ha=None), "aabb")) == ""


def test_devices_route_marks_incompatible_instead_of_updatable():
    """判据单点在服务端、面板只渲染：低于锁里下限的设备不得再被摆成"可升级"。"""
    import aiohttp
    with tempfile.TemporaryDirectory() as tmp:
        st, v = _store_with_lock(tmp)
        ctx = SimpleNamespace(firmware=st, ha=_SatHa(
            [{"mac": "aabbcc", "fw_source": "实时", "fw_version": "2.1.79", "name": "sat"}]),
            settings=_Disc(), host="1.2.3.4", started_at=0.0, sessions=set())
        app = aiohttp.web.Application()
        OTA_API.setup(app, ctx)
        loop = asyncio.new_event_loop()
        threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
                         daemon=True).start()
        box = {}
        port = asyncio.run_coroutine_threadsafe(_serve(app, box), loop).result(15)
        st_code, j = _http_get(port, "/api/devices", loop)
        loop.call_soon_threadsafe(loop.stop)
        assert st_code == 200, j
        dev = j["devices"][0]
        assert dev["fw_state"] == "incompatible", j
        assert dev["updatable"] is False, "面板 pill 由 fw_state 渲染，updatable 仍为真＝两口不一致"


def test_devices_route_still_marks_updatable_for_a_modern_device():
    import aiohttp
    with tempfile.TemporaryDirectory() as tmp:
        st, v = _store_with_lock(tmp, min_compatible="2.2.9")
        ctx = SimpleNamespace(firmware=st, ha=_SatHa(
            [{"mac": "aabbcc", "fw_source": "实时", "fw_version": "2.3.4", "name": "sat"}]),
            settings=_Disc(), host="1.2.3.4", started_at=0.0, sessions=set())
        app = aiohttp.web.Application()
        OTA_API.setup(app, ctx)
        loop = asyncio.new_event_loop()
        threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
                         daemon=True).start()
        box = {}
        port = asyncio.run_coroutine_threadsafe(_serve(app, box), loop).result(15)
        _st, j = _http_get(port, "/api/devices", loop)
        loop.call_soon_threadsafe(loop.stop)
        assert j["devices"][0]["fw_state"] == "updatable", j


def test_panel_has_the_incompatible_pill_rendered_from_state():
    assert 'if(s === "incompatible")' in HTML and "低于该版下限" in HTML
    mut = HTML.replace('if(s === "incompatible") return pill("低于该版下限·需USB双段重刷","bad");',
                       "", 1)
    assert mut != HTML and 'if(s === "incompatible")' not in mut


def test_issue_and_dispatch_pass_the_reading_down_with_a_default_first():
    """接线臂：两个入口都把 dev_ver 传下去，且默认值建在 try 之前
    （签发前任何早退都要拿得到它——这条形状本批实发过两次）。"""
    # 3 处＝_issue 的"指定版"与"latest 兜底"两条分支 + _dispatch 一条；少一处＝那条腿没带读数
    assert SRC_OTA_API_NC.count("store.issue, version, mac, dev_ver") == 3
    assert SRC_OTA_API_NC.count("store.ota_block_reason, version, dev_ver") == 2
    assert SRC_OTA_API_NC.count("await _live_device_version(ctx, mac)") == 2
    for fn in ("async def _issue", "async def _dispatch"):
        seg = SRC_OTA_API_NC[SRC_OTA_API_NC.find(fn):]
        assert seg.find("dev_ver = str(") < seg.find("        try:"), \
            "%s：dev_ver 在 try 之后才建＝早退分支读到未定义名" % fn


# ══ T12 版本血统 ════════════════════════════════════════════════════════
def test_version_bearing_sources_agree():
    """商店读 config.yaml、集成读 manifest、面板读 version.json——三处不齐就是
    "发出去了但读数不一致"（本仓发版四件套之外的第三道账）。"""
    ver = re.search(r'^version:\s*"?([\d.]+)"?', _raw("config.yaml"), re.M).group(1)
    man = json.loads(_raw("custom_components", "huijian_ai", "manifest.json"))
    wv = json.loads(_raw("www", "version.json"))
    assert ver == man["version"] == wv["addon_version"] == wv["integration_version"],         "面板那枚文件是两个键（addon_version/integration_version），不是 version"
    assert ver == "1.7.5", "本批的钉与代码同批出版本号（旧号＝钉在测上一版的源码）"
