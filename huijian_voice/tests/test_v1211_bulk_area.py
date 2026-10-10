# -*- coding: utf-8 -*-
"""v1.2.11 批：「(区域)所有设备」按**区域证据**展开 + 播报同源 + 三条同族补口。

现场两条实锤（.91 真注册表切片 + 生产码路，全程只读复算）：

    「关闭办公室所有设备」→ t0 计划正确（area=办公室 + 9 域白名单），
    但执行面交给 HA `async_match_targets(area_name=…)`——它对"区域"只认注册表归属，
    而两台开窗器整台设备 area=None ⇒ 只剩空调/射灯，播「已关闭」而窗纹丝不动；
    **同房间说「所有窗户」却能成**——窗控那条走的是 `intent_window_const` 的三档
    区域证据。两条车道对"区域"两个口径，就是这份文件要收口的东西。

另三条同批补口（都是本轮实测复现的"只修一半/挂账"）：
  · 后置标记语序（「办公室设备全部打开」「客厅灯都打开」）此前 t0 不接管 → 落 LLM
    只认灯；与 N1「客厅灯全部打开」同一个洞，一条道收。
  · 未点名区域的「所有设备」+ 本卫星登记了区域 ⇒ 折**本区域**批量（缩小作用域，
    不是放大）；拿不到区域/词不是设备泛称一律维持原样不接管。
  · A2 状态进行体收尾（「开着呢？」「关着吧？」「空调开着呢」）在批量道/全屋道
    **之前**弃权——问一句动一次设备是 v1.1.2 红线，当年只补了字面表一侧。
  · A7 同音误改（「我室→卧室」猜房间、「管灯→关灯」反极性）在 corrector 表+闸与
    homophone 层二同闸（钉在 test_corrector.py，本文件补端到端一条）。
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1211_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench          # noqa: E402  HA 替身 harness
from conftest import FakeHAClient                   # noqa: E402
from core import capability                         # noqa: E402
from core.executor import Executor, _bulk_all_devices_slot  # noqa: E402
from core.nlu import targets as T                   # noqa: E402
from core.nlu.fast_path import (FLAG_AREA_BULK, FastPath)   # noqa: E402
from core.settings import Settings                  # noqa: E402

CC = HERE / "custom_components" / "huijian_ai"

# ── 集成侧：三档区域证据的批量展开（真函数，替身注册表）─────────────
bench._install_ha_stubs()
_pkg = types.ModuleType("hjbulk1211_pkg")
_pkg.__path__ = [str(CC)]
sys.modules.setdefault("hjbulk1211_pkg", _pkg)
_spec = importlib.util.spec_from_file_location(
    "hjbulk1211_pkg.intent_window_const", CC / "intent_window_const.py")
IWC = importlib.util.module_from_spec(_spec)
sys.modules["hjbulk1211_pkg.intent_window_const"] = IWC
_spec.loader.exec_module(IWC)

AREA_NAMES = ("办公室", "展厅")
# (entity_id, state, friendly_name, 设备所属区域)——.91 逐字切片。
# 关键地形：两台开窗器**整台没挂区域**（现场常态），展厅三扇挂了「展厅」。
ROWS = [
    ("climate.xiaomi_ac", "off", "办公室空调 Air Conditioner", "办公室"),
    ("switch.xiaomi_ac_eco", "off", "办公室空调 ECO节能模式", "办公室"),
    ("light.ban_gong_shi_she_deng", "off", "射灯", "办公室"),
    ("cover.win_ping_kai", "closed", "平开窗 开窗器", None),
    ("cover.win_ce_suo", "closed", "厕所推拉门 开窗器", None),
    ("cover.win_xuan", "closed", "悬窗 开窗器", "展厅"),
    ("media_player.zhan_ting", "idle", "展厅", "展厅"),
    ("light.other_unzoned", "off", "走廊感应灯", None),
    ("cover.win_offline", "unavailable", "内开窗 开窗器", None),
]


def _entry(eid, did, area_id):
    return types.SimpleNamespace(entity_id=eid, device_id=did, area_id=area_id,
                                 aliases=(), name=None, entity_category=None)


def make_hass(area_override=None, dev_name=None, extra_rows=()):
    """区域只挂**设备**（实体级 area_id 全 None）——与真机同形。

    `dev_name`：{entity_id: 设备显示名}，用来造"设备名里写着别屋"的现场形态。
    `extra_rows`：追加地形（P3 前置用它造"卫星自己的一台设备"），默认空 ⇒ 既有用例逐字不变。
    """
    State = bench._State
    states, entries, devices = [], [], {}
    for i, row in enumerate(list(ROWS) + list(extra_rows)):
        eid, st, fn, area = row[0], row[1], row[2], row[3]
        # 第 5 位可选：显式指定 device_id——真机上「assist_satellite 实体」与它的
        # 麦克风开关/连续对话挂在**同一台设备**下，而本夹具默认"一行一台设备"。
        # 不给就按老行为 dev{i}（既有用例逐字不变）。
        did = str(row[4]) if len(row) > 4 else f"dev{i}"
        states.append(State(eid, fn, {"friendly_name": fn}, st))
        entries.append(_entry(eid, did, None))
        a = area if area_override is None else area_override.get(did, area)
        devices[did] = types.SimpleNamespace(name=(dev_name or {}).get(eid) or f"设备{i}",
                                             name_by_user=None,
                                             area_id=None if not a else f"area_{a}")
    er_by_id = {x.entity_id: x for x in entries}
    return types.SimpleNamespace(
        states=types.SimpleNamespace(
            async_all=lambda: list(states),
            get=lambda e: next((s for s in states if s.entity_id == e), None)),
        # 查表走**同一张 dict**（不是闭包里的 list 快照）：用例要按实体/设备改写归属，
        # 闭包快照会让改写看不见——替身必须与真注册表一样"现取现用"。
        _er=types.SimpleNamespace(by_id=er_by_id, async_get=er_by_id.get),
        _dr=types.SimpleNamespace(by_id=devices, async_get=devices.get),
        _ar=types.SimpleNamespace(
            async_get_area_by_name=lambda n: types.SimpleNamespace(
                id=f"area_{n}", name=n, aliases=set()) if n in AREA_NAMES else None,
            async_list_areas=lambda: [types.SimpleNamespace(
                id=f"area_{n}", name=n, aliases=set()) for n in AREA_NAMES]))


WL = list(capability.BULK_TOGGLEABLE_DOMAINS)


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()
    T.clear_vocab()
    yield
    T.clear_vocab()


def test_bulk_evidence_shadows_unzoned_when_room_has_evidence():
    """主修本体（保守那一侧）：本房间有区域实锤 ⇒ 只动实锤，未登记的不冒按。

    决定性实测（.91 全量切片）：未登记区域的可开关候选里除了办公室两扇开窗器，还有
    **厕所推拉门**与**小米小爱音箱**——把它们算进「关闭办公室所有设备」＝一句口令
    跨房关门＋关掉别处音箱，正撞「不敢把整屋设备冒按」与「绝不猜房间」两条红线。
    区域归属只能来自注册表，代码补不出来。所以本道换的是**匹配判据**（不再要求 HA
    严格区域匹配 ⇒ 名字/设备名回声明的具名设备自此可达），**没换**的是
    "没有任何区域证据就不算在这间房里"。
    """
    got = IWC.find_bulk_entities_by_area(make_hass(), "办公室", WL)
    assert "climate.xiaomi_ac" in got, got                     # 区域实锤
    assert "light.ban_gong_shi_she_deng" in got, got
    assert "cover.win_ping_kai" not in got, got                # 未登记 ⇒ 被遮掉
    assert "cover.win_ce_suo" not in got, got
    assert "light.other_unzoned" not in got, got
    assert not any(x.startswith("cover.win_xuan") for x in got), got   # 确凿别区
    assert "media_player.zhan_ting" not in got, got
    assert "cover.win_offline" not in got, got                  # unavailable 不收
    # 一台一条由 intent_helper 折叠，本函数不重复那份判据（单源纪律）
    assert got.count("climate.xiaomi_ac") == 1, got


def test_bulk_evidence_falls_back_only_when_there_is_no_evidence_at_all():
    """兜底放行只在"这间房一条证据都没有"时发生——**名字回声也算证据**。

    两支合起来才是现场真正的那道门（本轮实测把我自己最初的推断推翻了一次）：
    ① 即使把每台设备的区域全清掉，只要友好名/设备名里带着「办公室」三字，就是
       tier 1 实锤 ⇒ hard 非空 ⇒ 未登记的开窗器**仍被遮掉**。所以现场唯一能把
       开窗器带进「所有设备」的办法是在 HA 里给它挂区域，光换判据不够——
       这条必须写死，否则下一个人会以为换了判据就自动解决了。
    ② 一间注册表解析不出、名字里也没有任何区域词的房间（＝口述了个没登记的房名）
       ⇒ 一条 hard 都没有 ⇒ 按 tier 2 兜底放行（小家庭/全没挂区域的现场不至于
       把功能闸死，与窗控 v1.0.71 那条纪律同形）。这时所有无证据候选都进来，
       播报同样点名（`_bulk_unzoned_segs` 的「一起动了」那一支）。
    """
    all_devs = {f"dev{i}": None for i in range(len(ROWS))}
    got = IWC.find_bulk_entities_by_area(make_hass(all_devs), "办公室", WL)
    assert "climate.xiaomi_ac" in got, got                     # 名字回声＝实锤
    assert "cover.win_ping_kai" not in got, got                # 仍未登记 ⇒ 被遮掉
    got2 = IWC.find_bulk_entities_by_area(make_hass(all_devs), "阁楼", WL)
    assert "cover.win_ping_kai" in got2 and "cover.win_ce_suo" in got2, got2


def test_broadcast_names_shadowed_unzoned_devices():
    """被遮掉必须说出来：用户只看到"窗没动"时无法自救，点名 + 给修法才能收敛。"""
    _sync()
    from core.nlu.fast_path import Plan
    states = {e: {"entity_id": e, "state": "on",
                  "attributes": {"friendly_name": fn}}
              for e, _s, fn, _a in ROWS if e != "cover.win_offline"}
    area_map = {e: a for e, _s, _fn, a in ROWS if a}       # 开窗器无归属，同真机
    ha = Ha(results={"TurnDeviceOff": {"success": True, "control_targets": [
        {"name": "办公室空调 Air Conditioner", "area": "办公室",
         "entity_id": "climate.xiaomi_ac"},
        {"name": "射灯", "area": "办公室", "entity_id": "light.ban_gong_shi_she_deng"},
    ]}}, states=states, entity_area=area_map, areas={"a1": "办公室", "a2": "展厅"})
    ha._entity_device = {"climate.xiaomi_ac": "devAC",
                         "light.ban_gong_shi_she_deng": "devL",
                         "cover.win_ping_kai": "devW1",
                         "cover.win_ce_suo": "devW2",
                         "light.other_unzoned": "devC"}
    plan = Plan(intent="TurnDeviceOff",
                args={"target": [{"area": "办公室",
                                  "devices": [{"name": "", "domains": WL}]}]},
                source="t0", utterance="关闭办公室所有设备").mark(FLAG_AREA_BULK)
    ok, speech = asyncio.run(Executor(ha).run(plan))
    assert ok is True, speech
    assert "没登记房间" in speech and "没跟着动" in speech, speech
    assert "平开窗 开窗器" in speech, speech
    assert "归到「办公室」" in speech, speech


def test_bulk_evidence_never_raises():
    """注册表形态异常 ⇒ 空表，调用方退回 HA 严格匹配（宁缺勿滥）。"""
    assert IWC.find_bulk_entities_by_area(object(), "办公室", WL) == []
    assert IWC.find_bulk_entities_by_area(make_hass(), "办公室", []) == []


# ── core 侧：句形、卫星折算、注册表逐字、疑问让路 ──────────────────
SNAP = json.loads((pathlib.Path(__file__).resolve().parents[2] / "_bench" /
                   "snapshot_091_1008.json").read_text(encoding="utf-8")) \
    if (pathlib.Path(__file__).resolve().parents[2] / "_bench" /
        "snapshot_091_1008.json").exists() else None


class SC:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None


def _fp(sat=None, areas=AREA_NAMES):
    st = Settings(pathlib.Path(os.environ["HUIJIAN_DATA"]) / f"v1211-{os.getpid()}.json")
    fp = FastPath(SC(), None, st)
    if sat is not None:
        fp.settings._data["spatial"] = {"satellite_areas": sat}
    fp.areas_of = lambda: tuple(areas)
    return fp


def _sync():
    rows = [("light.ban_gong_shi_she_deng", "off", "射灯", "办公室"),
            ("climate.xiaomi_ac", "off", "办公室空调 Air Conditioner", "办公室"),
            ("cover.win_xuan", "closed", "悬窗 开窗器", "展厅"),
            ("cover.win_ping_kai", "closed", "平开窗 开窗器", "办公室")]
    states = {e: {"entity_id": e, "state": s,
                  "attributes": {"friendly_name": fn}} for e, s, fn, _a in rows}
    areas = {e: a for e, _s, _fn, a in rows}
    T.sync_vocab(states, areas)
    T.sync_areas(AREA_NAMES)


def test_trailing_marker_word_order_is_taken():
    """「办公室设备全部打开」类后置标记语序：此前 t0 不接管 → 落 LLM 只认灯。"""
    _sync()
    fp = _fp()
    for s in ("办公室设备全部打开", "办公室设备都关掉", "展厅灯全部打开",
              "把办公室灯都关闭"):
        p = asyncio.run(fp.match(s))
        assert p is not None and FLAG_AREA_BULK in p.flags, (s, p and p.args)
        slot = p.args["target"][0]
        assert slot["area"] in AREA_NAMES, (s, slot)
    p = asyncio.run(fp.match("办公室设备全部打开"))
    assert p.intent == "TurnDeviceOn"
    assert p.args["target"][0]["devices"][0]["domains"] == WL, p.args


def test_trailing_marker_lane_does_not_invent_area():
    """注册表里没有这间房 ⇒ 不接管（收敛"区域宣称过头"：宣称与判据同源）。"""
    _sync()
    fp = _fp(areas=("办公室",))          # 展厅不在本台注册表
    p = asyncio.run(fp.match("展厅设备全部打开"))
    assert p is None or FLAG_AREA_BULK not in p.flags, p and p.args


def test_bare_all_devices_folds_into_satellite_area_only_when_registered():
    """未点名区域的「所有设备」：卫星登记了区域才折本区域；拿不到 ⇒ 维持不接管。"""
    _sync()
    p = asyncio.run(_fp({"sat-A": "办公室"}).match("关闭所有设备", "sat-A"))
    assert p is not None and FLAG_AREA_BULK in p.flags, p and (p.args, p.trace)
    slot = p.args["target"][0]
    assert slot["area"] == "办公室" and slot["devices"][0]["name"] == "", slot
    assert slot["devices"][0]["domains"] == WL, slot
    # 没登记卫星区域 / 没传 origin ⇒ 逐字等于旧行为（绝不冒然全屋全动）
    assert asyncio.run(_fp({}).match("关闭所有设备", "sat-A")) is None
    assert asyncio.run(_fp({"sat-A": "办公室"}).match("关闭所有设备")) is None
    # 折的区域不在本台注册表 ⇒ 不接管（绝不猜房间）
    assert asyncio.run(_fp({"sat-A": "阁楼"}, areas=("办公室",))
                       .match("关闭所有设备", "sat-A")) is None
    # 非设备泛称的全屋口径（「所有灯」）不归本支，走原全屋道
    p2 = asyncio.run(_fp({"sat-A": "办公室"}).match("关闭所有灯", "sat-A"))
    assert p2 is not None and getattr(p2, "whole_house", False), p2 and p2.args


def test_state_question_never_reaches_bulk_or_wholehouse_lane():
    """A2：进行体收尾的疑问/陈述句在批量道与全屋道**之前**让路（问一句不动设备）。"""
    _sync()
    fp = _fp()
    for s in ("办公室的灯开着呢？", "客厅的灯关着吧？", "卧室空调开着呢",
              "办公室所有设备开着呢？"):
        assert asyncio.run(fp.match(s)) is None, s
    # 祈使面一寸不让：这三条必须照旧接管
    assert asyncio.run(fp.match("把灯关掉")).intent == "TurnDeviceOff"
    assert asyncio.run(fp.match("关灯吧")).intent == "TurnDeviceOff"
    p = asyncio.run(fp.match("打开所有灯好吗？"))
    assert p is not None and p.intent == "TurnDeviceOn", p and p.args


def test_homophone_no_longer_guesses_the_room_in_bulk_lane():
    """A7 端到端：「我室」不再被折成在册区域「卧室」（表键删了、两层闸都在）。"""
    _sync()
    p = asyncio.run(_fp().match("我室设备全部打开"))
    assert p is None or "卧室" not in json.dumps(p.args, ensure_ascii=False), \
        p and p.args


# ── 播报面：旗标优先 + 回执点名"没登记房间" ────────────────────────
def test_all_devices_slot_flag_first_and_named_bulk_excluded():
    """B2 同源：具名批量（「所有灯」即便域集铺满白名单）不得被判成"所有设备"口径。

    判据两件都在才算：**空名**（设备面的形状本身）＋**旗标或集合覆盖**（这条是不是
    区域批量）。少一件就是第七轮审计 B2 的错案方向。
    """
    named = {"target": [{"area": "办公室",
                         "devices": [{"name": "灯", "domains": WL}]}]}
    assert _bulk_all_devices_slot(named, frozenset()) is False, named
    assert _bulk_all_devices_slot(named, frozenset({FLAG_AREA_BULK})) is False, named
    dev = {"target": [{"area": "办公室",
                       "devices": [{"name": "", "domains": WL}]}]}
    assert _bulk_all_devices_slot(dev, frozenset({FLAG_AREA_BULK})) is True
    assert _bulk_all_devices_slot(dev, frozenset()) is True       # 链里次腿不带旗标
    light_only = {"target": [{"area": "办公室",
                              "devices": [{"name": "", "domains": ["light"]}]}]}
    assert _bulk_all_devices_slot(light_only, frozenset()) is False, light_only


class Ha(FakeHAClient):
    def states_stale(self):
        return ""


def test_broadcast_names_unzoned_devices_from_receipt():
    """动了"没登记房间"的设备 ⇒ 必须点名（判据只用回执，不重算 tier）。"""
    _sync()
    states = {e: {"entity_id": e, "state": "on",
                  "attributes": {"friendly_name": fn}}
              for e, _s, fn, _a in ROWS if e != "cover.win_offline"}
    area_map = {e: a for e, _s, _fn, a in ROWS if a}     # 开窗器无归属，同真机
    ha = Ha(results={"TurnDeviceOff": {"success": True, "control_targets": [
        {"name": "办公室空调 Air Conditioner", "area": "办公室",
         "entity_id": "climate.xiaomi_ac"},
        {"name": "平开窗 开窗器", "area": "", "entity_id": "cover.win_ping_kai"},
    ]}}, states=states, entity_area=area_map,
        areas={"a1": "办公室", "a2": "展厅"})
    ha._entity_device = {"climate.xiaomi_ac": "devAC", "cover.win_ping_kai": "devW"}
    from core.nlu.fast_path import Plan
    plan = Plan(intent="TurnDeviceOff",
                args={"target": [{"area": "办公室",
                                  "devices": [{"name": "", "domains": WL}]}]},
                source="t0", utterance="关闭办公室所有设备").mark(FLAG_AREA_BULK)
    ok, speech = asyncio.run(Executor(ha).run(plan))
    assert ok is True, speech
    assert "没登记房间" in speech and "平开窗 开窗器" in speech, speech
    assert "归好区域" in speech, speech


def test_unregistered_area_is_not_taken_by_the_head_form():
    """前置标记形同样吃注册表逐字：家里没这间房 ⇒ 批量道不接管（第七轮 §3 收敛）。"""
    _sync()
    fp = _fp(areas=("办公室",))
    p = asyncio.run(fp.match("关闭厨房所有灯"))
    assert p is None or FLAG_AREA_BULK not in p.flags, p and p.args
    p2 = asyncio.run(fp.match("关闭办公室所有灯"))
    assert p2 is not None and FLAG_AREA_BULK in p2.flags, p2 and p2.args


def test_uninstalled_switch_whole_name_never_flips_direction():
    """A1-a：以「开关」结尾但**不在册**的整名，撤掉动作表护栏就会反向成 On。

    这条是第六轮审计 A1 的护栏（`开(?!窗器|合器|关)`）唯一的承重证据——在册整名
    走的是整名剥离那条路（A1-b/A6），测不到这道护栏；只有不在册的短形才由它兜底。
    断言只要求"不许反向"：要么不接管（交上层），要么判成 Off。
    """
    _sync()
    fp = _fp()
    for s in ("把书房灯开关关掉", "书房的灯开关关闭"):
        p = asyncio.run(fp.match(s))
        assert p is None or p.intent == "TurnDeviceOff", (s, p and (p.intent, p.args))


def test_plural_subject_without_target_inherits_previous_target():
    """A3 的**正向**语义钉：`pipeline.py:2414` 那份 `"们"` 管的是"无明示目标句"的
    目标继承——「关闭卧室空调」之后说「我们都关掉」必须沿用空调，不是退成全屋。

    同时钉住早退分支：有明示目标的「我们把灯打开」不得被上一句目标顶掉
    （第七轮审计 A3 建议删这份 `们` 时给的后果正是反的，删了会砸掉继承这一支）。
    """
    from core.pipeline import Pipeline, _has_explicit_target
    from core.nlu.fast_path import Plan

    class _S:
        def __init__(self, d=None):
            self.d = dict(d or {})

        def get(self, k, default=None):
            return self.d.get(k, default)

    def pipe():
        p = Pipeline.__new__(Pipeline)
        p.settings = _S({"dialog.context_enabled": True, "dialog.context_ttl_s": 300,
                         "spatial.satellite_areas": {}})
        p._last_target = {"o": {"kind": "target", "ts": __import__("time").time(),
                                "target": [{"area": "卧室",
                                            "devices": [{"name": "空调",
                                                         "domains": ["climate"]}]}]}}
        p._confirm = {}
        p._origin_ts = {}
        p._last_list = {}
        p._turns = {}
        return p

    _sync()
    # ① 明示目标句：走早退，绝不继承
    plan = asyncio.run(_fp().match("我们把灯打开"))
    assert plan is not None and _has_explicit_target(plan.args)
    r = pipe()._apply_context(plan, "我们把灯打开", "o")
    assert json.dumps(r.args, ensure_ascii=False).count("灯") == 1, r.args
    assert "空调" not in json.dumps(r.args, ensure_ascii=False), r.args
    # ② 无明示目标的复数主语句：继承上一句目标（这份 `们` 的真实职责）。
    #    source 用慧尖形：klar 平铺的继承另有一条支（要求 args 里有 target 键），
    #    本支钉的是"空 args 的代词/残句形"这一路（`_build_plan` 就产这个形状）。
    bare = Plan(intent="TurnDeviceOff", args={}, source="t0", utterance="我们都关掉")
    r2 = pipe()._apply_context(bare, "我们都关掉", "o")
    assert "空调" in json.dumps(r2.args, ensure_ascii=False), (r2.args, r2.trace)
    assert any("继承目标" in t for t in r2.trace), r2.trace


def _extract_func_src(path, name):
    """按 AST 取集成侧纯函数的源码（本仓既有手法：不装整套 HA，也能真跑判据）。"""
    import ast
    import textwrap
    src = pathlib.Path(path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{path} 找不到函数 {name}")


def test_bulk_evidence_eligible_requires_the_device_face():
    """触发面必须**三件齐全**（空名＋点名区域＋域集覆盖白名单）。

    少第三件就是越界：LLM/klar 的 `{area:客厅, name:"", domains:["light"]}` 会被
    拖进区域证据展开 ⇒ 整台没登记区域的灯被当成"客厅的灯"一起开（第七轮审计自查）。
    """
    ns = {"_BULK_DOMAIN_PRIORITY": ("climate", "water_heater", "humidifier", "vacuum",
                                    "cover", "fan", "light", "media_player", "switch")}
    exec(compile(_extract_func_src(CC / "intent_helper.py",
                                   "_bulk_evidence_eligible"), "ih", "exec"), ns)
    ok = ns["_bulk_evidence_eligible"]
    assert ok(None, "办公室", WL) is True                     # 设备面
    assert ok(None, "", WL) is False                          # 无区域＝全屋句归原路
    assert ok("灯", "办公室", WL) is False                     # 具名批量归原路
    assert ok(None, "客厅", ["light"]) is False               # 单域空名 ⇒ 绝不展开
    assert ok(None, "客厅", ["light", "cover"]) is False
    # 域集由 core 的白名单原样下发（HA 域恒小写；`_expand_domains` 在上游已归一），
    # 这里不再二次宽容大小写——否则"表漂移"会伪装成"匹配成功"。
    assert ok(None, "客厅", [d.upper() for d in WL]) is False
    assert ok(None, "客厅", WL + ["lock"]) is True       # 多一个域仍是设备面
    assert ok(None, "客厅", None) is False


def test_evidence_predicate_is_wired_at_the_call_site():
    """接线钉（第七轮审计的教训形状）：上一例只单测**纯谓词**，把调用点直接退回
    `requested_name is None and raw_area` 时纯谓词照旧正确 ⇒ 16 条全绿，洞在。
    这里用 AST 钉住"**这条 if 的判断必须走 `_bulk_evidence_eligible`**"，
    守的是控制流而不是字面量。
    """
    import ast
    src = (CC / "intent_helper.py").read_text(encoding="utf-8")
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_match_with_constraints"), None)
    assert fn is not None, "找不到 _match_with_constraints（改名了？那这条钉要一起改）"
    hits = [n for n in ast.walk(fn)
            if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
            and getattr(n.test.func, "id", "") == "_bulk_evidence_eligible"]
    assert len(hits) == 1, f"证据支应恰好一处、且必须过谓词，实得 {len(hits)}"
    # 且那一支里唯一的能力调用就是 find_bulk_entities_by_area（防"过了谓词但没走展开"）
    body = ast.dump(ast.Module(body=hits[0].body, type_ignores=[]))
    assert "find_bulk_entities_by_area" in body, hits[0].lineno
    inline = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
              and getattr(n.left, "id", "") == "requested_name"
              and "raw_area" in ast.dump(n)]
    assert not inline, "调用点旁又手写了一份 raw_area 比较判据 ⇒ 两份判据会漂移"


def test_broadcast_says_nothing_when_receipt_has_no_entity_id():
    """数不出来 ⇒ 绝不编数：旧形回执（无 entity_id）不产生"没登记房间"这句。"""
    _sync()
    states = {"climate.xiaomi_ac": {"entity_id": "climate.xiaomi_ac", "state": "on",
                                    "attributes": {"friendly_name": "办公室空调"}},
              "light.ban_gong_shi_she_deng": {
                  "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
                  "attributes": {"friendly_name": "射灯"}}}
    ha = Ha(results={"TurnDeviceOff": {"success": True, "control_targets": [
        {"name": "办公室空调", "area": "办公室"}]}},   # 旧集成形制：无 entity_id
        states=states, entity_area={}, areas={"a1": "办公室"})
    from core.nlu.fast_path import Plan
    plan = Plan(intent="TurnDeviceOff",
                args={"target": [{"area": "办公室",
                                  "devices": [{"name": "", "domains": WL}]}]},
                source="t0", utterance="关闭办公室所有设备").mark(FLAG_AREA_BULK)
    ok, speech = asyncio.run(Executor(ha).run(plan))
    assert ok is True, speech
    assert "没登记房间" not in speech, speech


# ══ P3 前置（10-10）：语音卫星自己绝不进设备面批量 ═════════════════════
# 病灶（真机因果臂顺带挖出来的，全程见 `docs\语音线v14x_归位与假成功_实测账` 十七节）：
# v1.2.10 那道"绝不关语音卫星自己"的守卫（`intent_helper._is_own_integration`）按
# **归属集成**判（platform/config_entry==huijian_ai），可现网的
# `switch.huijian_1f04_mai_ke_feng_kai_guan` 是 **ESPHome 侧实体** ⇒ 够不到；
# 而真正承接「(区域)所有设备」的**设备面道**（本文件的 `find_bulk_entities_by_area`）
# 此前**根本没有这道守卫**。它们今天侥幸没被关掉，只因为**没登记区域**；用户一旦为
# "这间房"好使把它们归进房间（P3b），「关闭办公室所有设备」就会**把麦克风关掉**，
# 下一句再没人听得见（自杀式静音，`fast_path.py:1025` 早警告过）。
# 修法只认一件事：**该实体所属设备上有没有 `assist_satellite.*`**（与归属集成无关），
# 且只作用于**空名批量目标**——点名「关掉小智音箱」这类具名句照旧能动它。
SAT_ROWS = [
    # (entity_id, state, friendly_name, 设备区域, 设备 id)——三行**同一台设备**，与真机同形
    ("assist_satellite.huijian_1f04_sat", "idle", "HUIJIAN-1F04 语音助手卫星", "办公室", "dev_sat"),
    ("switch.huijian_1f04_mai_ke_feng_kai_guan", "on", "HUIJIAN-1F04 麦克风开关", "办公室", "dev_sat"),
    ("switch.huijian_1f04_lian_xu_dui_hua", "off", "HUIJIAN-1F04 连续对话", "办公室", "dev_sat"),
    ("light.office_desk_lamp", "off", "台灯", "办公室", "dev_lamp"),
]


def _sat_ids(hass):
    return IWC.satellite_self_device_ids(hass)


def test_p3_satellite_self_ids_are_found_by_device_not_by_integration():
    """判据按设备取：卫星那台设备（含 assist_satellite 实体）被认出来。"""
    hass = make_hass(extra_rows=SAT_ROWS)
    ids = _sat_ids(hass)
    assert ids, "一台都没认出 ⇒ 守卫形同不存在"
    # 卫星自己那台设备的两条 switch 必须同属一个 device_id
    er = hass._er
    assert (er.async_get("switch.huijian_1f04_mai_ke_feng_kai_guan").device_id
            == er.async_get("switch.huijian_1f04_lian_xu_dui_hua").device_id
            == er.async_get("assist_satellite.huijian_1f04_sat").device_id)
    assert ids == {er.async_get("assist_satellite.huijian_1f04_sat").device_id}, ids


def test_p3_device_face_bulk_never_includes_the_speaker_itself():
    """P3b 那一格：卫星实体**已登记进办公室**时，设备面批量也不得把它带进去。"""
    hass = make_hass(extra_rows=SAT_ROWS)
    got = IWC.find_bulk_entities_by_area(hass, "办公室", WL)
    assert "switch.huijian_1f04_mai_ke_feng_kai_guan" not in got, \
        f"批量口令会把说话的那台设备关掉（自杀式静音）：{got}"
    assert "switch.huijian_1f04_lian_xu_dui_hua" not in got, got
    assert "light.office_desk_lamp" in got, \
        f"守卫过头把客户的正常设备也剔了：{got}"


def test_p3_zero_change_when_home_has_no_satellite_registered():
    """反向零漂移：家里没有 assist_satellite 实体 ⇒ 判据回空集，批量面逐字不变。"""
    hass = make_hass()
    assert _sat_ids(hass) == set(), "无卫星的家凭空多出排除集"
    got = IWC.find_bulk_entities_by_area(hass, "办公室", WL)
    assert "light.ban_gong_shi_she_deng" in got, got


def test_p3_helper_never_raises_on_broken_registry():
    """判据故障 ⇒ 空集（宁多带一台也不凭空剔客户设备，与原守卫同纪律）。"""
    bad = types.SimpleNamespace(states=types.SimpleNamespace(
        async_all=lambda: (_ for _ in ()).throw(RuntimeError("注册表炸了"))))
    assert IWC.satellite_self_device_ids(bad) == set()
    assert IWC.find_bulk_entities_by_area(bad, "办公室", WL) == []
