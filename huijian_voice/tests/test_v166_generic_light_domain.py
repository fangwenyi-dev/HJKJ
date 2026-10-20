# -*- coding: utf-8 -*-
"""v1.6.6 泛称折域批：金标 14 号「打开办公室的灯」不再拿**区域名当设备名**。

现场（2026-10-19 现网实弹金标 14 号，dry/real 两臂同产物，发布版 1.6.5 就在册）：

    t0 计划 `target=[{'devices': [{'name': '办公室', 'domains': ['climate']}]}]`
    → 候选折到两台空调（『办公室空调 Air Conditioner』『办公室 All Thermostats』）
    → 撞歧义闸播「家里有 2 台设备名字相近…带上房间名再说一次」，**灯一盏没开**。

根因（与工单描述**不同**，证据＝本文件 ① 组 `test_parse_target_live_vocab_returns_area_and_generic`
与 `targets.py` ⑥ 支）：错位不在 `parse_target` 之后的计划装配，而在 `parse_target`
的 ⑥ 贪心档本身。`sync_vocab` 把 friendly_name 的 CJK token 派生成设备词
（`targets._name_tokens`），办公室那两台「办公室 All Lights」/「办公室 All Thermostats」
于是把**区域名「办公室」本身**送进了设备词表；⑥ 按长度倒序扫子串，「办公室」在 idx=0
命中即 break（该处「idx==0 一律不裁」护栏是为 HA 实体自带区域的注册表全名『客厅空调』
立的，见 targets.py 同支注释），句尾泛称「灯」连同它的 light 域一起没被取到，
区域整段也没了。`_build_plan` 只是把 `name='办公室'` 照抄进 `devices[].name`。
工单那句「实测 parse_target('办公室的灯') 回 ('办公室','灯',6)」跑的是**静态词表面**
（`clear_vocab`，单测世界）；现网词表下修复前的真实回值是 `(None,'办公室',6)`——
同一句、两张词表、两个答案，这正是"表间漂移"的实发病根。

修法（单点，`core/nlu/targets.py` ⑥）：命中词落在 idx=0 且**整串＝区域名+(的|里)?+
唯一域泛称字**时 `continue` 让位，交既有单字泛称车道承接（与 `_area_split_wins` 同机制、
同"不新建表"纪律）；`parse_target` 于是回 `('办公室','灯')`，`_build_plan` 走它一直就有的
`area + domains` 支（对照面：「打开客厅的灯」本就是这一形，见 test_fast_path）。
泛称宇宙由 `_GENERIC_FAMILIES` **派生**（只取"泛称→域一对一"的字，现网＝灯）：窗/门
各跨 ControlWindow 与 lock 车道，把它们的泛称也折成域过滤会从既有车道手里抢走句子
（金标 4 号「打开办公室的窗」的歧义 clarify 口径不许动）。

判别力（本文件跑过的两个变异，结果见仓库报告）：
  M1 常量式退回旧形 `if False and …` → ① 组四条正向钉 + ③ 组接线钉当场红（15 红 17 绿）；
  M2 过度伸手（泛称宇宙手抄成 `("灯", "窗", "门")`）→ ② 组窗/门反向钉 + ③ 组派生/
     helper 钉当场红（4 红 28 绿），① 组仍绿（正向面不该被反向钉误伤）。
"""
import asyncio
import copy
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core import capability                                  # noqa: E402
from core.nlu import homophone as H                          # noqa: E402
from core.nlu import targets as T                            # noqa: E402
from core.nlu.fast_path import FLAG_AREA_BULK, FastPath      # noqa: E402
from core.nlu.textcnn import TextCNN                         # noqa: E402
from core.settings import DEFAULTS                           # noqa: E402


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class FakeScenes:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass


# ── 现网 .91 切片（entity_id, state, friendly_name, 生效区域）──────────
# 关键地形（缺一条本批判据就测不出来）：
#   · 区域灯组与区域温控组的友好名都把**区域名本身**派生成设备词 token；
#   · 具名灯「办公室射灯」与它同区，长词必须先于区域名命中（反向保护的靶）；
#   · 客厅那两台是**别区**的灯，其中一台常 unavailable（区域硬约束的靶）。
ROWS = [
    ("light.linus_dashboard_all_lights_area_ban_gong_shi", "on", "办公室 All Lights", "办公室"),
    ("light.ban_gong_shi_she_deng", "off", "办公室射灯", "办公室"),
    ("light.linus_dashboard_all_lights_area_hui_yi_shi", "off", "会议室 All Lights", "会议室"),
    ("light.she_deng", "unavailable", "客厅射灯", "客厅"),
    ("light.linus_dashboard_all_lights_area_ke_ting", "unavailable", "客厅 All Lights", "客厅"),
    ("climate.xiaomi_mc9_aeaf_air_conditioner", "off", "办公室空调 Air Conditioner", "办公室"),
    ("climate.linus_dashboard_all_climates_area_ban_gong_shi", "unavailable",
     "办公室 All Thermostats", "办公室"),
    ("cover.linus_dashboard_window_area_ban_gong_shi", "unknown", "办公室 窗", "办公室"),
    ("media_player.linus_dashboard_speaker_area_ban_gong_shi", "idle", "办公室 扬声器", "办公室"),
]
AREAS = ("办公室", "会议室", "展厅", "客厅", "卧室", "厨房", "开发")

STATES = {eid: {"state": st, "attributes": {"friendly_name": fn}}
          for eid, st, fn, _area in ROWS}
# 区域只挂在**设备**上（本台 `config/entity_registry/list` 的 area_id 全为 null，
# 加载项 `_ws_registries` 只拉实体注册表 ⇒ `_entity_area` 恒空）。候选面按 HA 的
# "生效区域"口径给，另有一条**空区域表**的退化面钉（拿不到区域证据时也不许折到空调）。
EFFECTIVE_AREA = {eid: area for eid, _st, _fn, area in ROWS}


@pytest.fixture(autouse=True)
def _live_vocab():
    """本文件的词表＝现网注册表派生形（金标现场的那张表），用完清回复位。"""
    T.sync_vocab(STATES, {})
    T.sync_areas(AREAS)
    yield
    T.clear_vocab()


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc):
    return FastPath(FakeScenes(), tc, _S())


PRIOR = H.prior_names([fn for _eid, _st, fn, _area in ROWS])


def _route(fp, text):
    """复刻生产单点顺序：先验改写 → fast_path.match（与 test_v157/test_a1 同量具）。"""
    out, _ = H.rewrite(text, PRIOR)
    return asyncio.run(fp.match(out))


def _slot(plan):
    return (plan.args.get("target") or [{}])[0]


# ── ① 正向：区域是硬约束、泛称折成域、区域名绝不冒名设备 ───────────────
LIGHT_FORMS = [
    ("打开办公室的灯", "TurnDeviceOn", "办公室"),
    ("关闭办公室的灯", "TurnDeviceOff", "办公室"),
    ("打开客厅的灯", "TurnDeviceOn", "客厅"),
    ("打开会议室的灯", "TurnDeviceOn", "会议室"),
    ("打开办公室灯", "TurnDeviceOn", "办公室"),      # 无属格的同族形
    ("办公室灯打开", "TurnDeviceOn", "办公室"),       # SOV 形（t0_prefix_strip 车道）
]


@pytest.mark.parametrize("text,intent,area", LIGHT_FORMS)
def test_area_generic_light_keeps_area_and_light_domain(fp, text, intent, area):
    p = _route(fp, text)
    assert p is not None, f"{text!r} 落空（本批是修缺陷，不是砍车道）"
    assert p.intent == intent, f"{text!r} → {p.intent}（错意图）"
    slot = _slot(p)
    assert slot.get("area") == area, (
        f"{text!r} 区域丢了：slot={slot}（区域必须是硬约束槽，不能靠实体名前缀猜）")
    dev = (slot.get("devices") or [{}])[0]
    assert dev.get("domains") == ["light"], (
        f"{text!r} domains={dev.get('domains')}（泛称「灯」必须折成 light 域过滤）")
    assert not p.whole_house, f"{text!r} 不许打全屋标（绝不全屋扇出）"


@pytest.mark.parametrize("text", [t for t, _i, _a in LIGHT_FORMS])
def test_area_name_never_masquerades_as_device_name(fp, text):
    """金标 14 号的产物形：`devices[].name == 区域名`——本钉把它判死。"""
    p = _route(fp, text)
    assert p is not None, text
    for slot in (p.args.get("target") or []):
        for dev in (slot.get("devices") or []):
            nm = str(dev.get("name") or "")
            assert nm not in AREAS, (
                f"{text!r} 区域名「{nm}」又冒名设备（现场病灶：折到两台空调）：{p.args}")


def test_parse_target_live_vocab_returns_area_and_generic():
    """根因定位钉：现网词表下修复前 parse_target 回 `(None,'办公室',6)`。

    工单里那句「('办公室','灯',6)」是**静态词表面**（`clear_vocab`）的答案——两张面都钉，
    且必须同解（只钉静态面＝把量具当结论；只钉现网面＝下一句"区域名在册成设备词"
    的形没人守）。
    """
    area, name, score = T.parse_target("办公室的灯")
    assert (area, name) == ("办公室", "灯"), f"现网词表回值仍是 {(area, name)}（score={score}）"
    assert T.turn_domains(name) == ["light"], name
    T.clear_vocab()                    # 冷启动/单测面：区域名不在设备词表里，本就同解
    try:
        assert T.parse_target("办公室的灯")[:2] == (area, name), "同一句两张词表两个答案＝表间漂移"
    finally:
        T.sync_vocab(STATES, {})
        T.sync_areas(AREAS)


def test_candidates_fold_to_office_light_only(fp):
    """执行面判据（缺陷的可后果）：计划折到的候选必须是办公室那盏灯，一台不多。

    修复前这条计划的候选是两台空调（`climate.*`）——金标就是在这里判 FAIL 的。
    两口径都核：生效区域表（HA/集成口径）+ 空区域表（加载项 `_entity_area` 恒空的退化面）。
    """
    p = _route(fp, "打开办公室的灯")
    assert p is not None and _slot(p).get("area") == "办公室", p and p.args
    tgt = p.args["target"]
    got = {c["entity_id"] for c in capability.resolve_candidates(STATES, EFFECTIVE_AREA, tgt)}
    assert got == {"light.ban_gong_shi_she_deng"}, f"候选折错：{sorted(got)}"
    loose = {c["entity_id"] for c in capability.resolve_candidates(STATES, {}, tgt)}
    assert not any(e.startswith("climate.") for e in loose), (
        f"区域证据缺席时折到空调＝谎报温床：{sorted(loose)}")
    assert all(e.startswith("light.") for e in loose), sorted(loose)


# ── ② 反向保护：无区域裸令 / 批量道 / 具名设备 / 窗门泛称 逐字不变 ──────
BARE_CASES = {
    "打开灯": {"target": [{"devices": [{"name": "灯", "domains": ["light"]}]}]},
    "关灯": {"target": [{"devices": [{"name": "灯", "domains": ["light"]}]}]},
    "关掉所有的灯": {"target": [{"devices": [{"name": "", "domains": ["light"]}]}]},
}


@pytest.mark.parametrize("text,args", sorted(BARE_CASES.items()))
def test_bare_light_commands_shape_unchanged(fp, text, args):
    p = _route(fp, text)
    assert p is not None, f"{text!r} 裸令本应接管，修复后落空＝少做也是回归"
    assert p.args == args, f"{text!r} args={p.args} 期望逐字 {args}"
    assert "area" not in _slot(p), f"{text!r} 无区域句不许凭空造区域"
    if text == "关掉所有的灯":
        assert p.whole_house, "显式全屋句的全屋标不许丢"


BULK_CASES = {
    "把办公室的灯都打开": ("TurnDeviceOn", "办公室", "灯"),
    "办公室灯全部打开": ("TurnDeviceOn", "办公室", "灯"),
    "关闭展厅所有灯": ("TurnDeviceOff", "展厅", "灯"),
}


@pytest.mark.parametrize("text,intent,area,cat",
                         [(k, *v) for k, v in sorted(BULK_CASES.items())])
def test_area_bulk_lane_unchanged(fp, text, intent, area, cat):
    """`_area_bulk` 道（v1.2.10/v1.2.11 批量口令）产物逐字不变——本批不伸手那条道。"""
    p = _route(fp, text)
    assert p is not None and p.intent == intent, f"{text!r} → {p and p.intent}"
    assert FLAG_AREA_BULK in p.flags, f"{text!r} 批量标丢了：flags={p.flags}"
    assert "区域批量" in "".join(p.trace), p.trace
    slot = _slot(p)
    assert slot == {"area": area, "devices": [{"name": cat, "domains": ["light"]}]}, slot


def test_area_bulk_device_words_unchanged(fp):
    """「所有设备」批量道逐字不变（域白名单 + 空名，本批一个字节都没碰它）。"""
    p = _route(fp, "关闭办公室所有设备")
    assert p is not None and FLAG_AREA_BULK in p.flags, p
    slot = _slot(p)
    assert slot["area"] == "办公室"
    assert slot["devices"][0]["name"] == ""
    assert slot["devices"][0]["domains"] == list(capability.BULK_TOGGLEABLE_DOMAINS), (
        slot["devices"])


NAMED_CASES = {
    "打开办公室射灯": ("TurnDeviceOn", "办公室射灯", ["light"]),
    "打开办公室空调": ("TurnDeviceOn", "办公室空调", ["climate"]),
}


@pytest.mark.parametrize("text,intent,name,doms",
                         [(k, *v) for k, v in sorted(NAMED_CASES.items())])
def test_named_device_after_area_unchanged(fp, text, intent, name, doms):
    """具名设备（区域是名字的一部分，不是区域槽）一律走原车道，本批不动。"""
    p = _route(fp, text)
    assert p is not None and p.intent == intent, f"{text!r} → {p and p.intent}"
    dev = (_slot(p).get("devices") or [{}])[0]
    assert dev.get("name") == name and dev.get("domains") == doms, f"{text!r} {dev}"
    assert "area" not in _slot(p), f"{text!r} 具名设备不许被拆成区域+泛称：{_slot(p)}"


# 区域名之后的**其它名词**一律不折：窗/门泛称各跨 ControlWindow 与 lock 车道，折过去
# 会抢句子（金标 4 号「打开办公室的窗」的歧义 clarify 口径、关窗族的车道归属）；
# 台灯/空调是具名类别词，本来就该按名字找实体。
# 这四条形制（修复前后逐字相同）仍是"区域名被当成设备名"——同族遗留面，另案在册，
# 不在本刀范围（本刀只收"灯"这条能被域唯一确定的泛称；判据不达标就宁可不动）。
NO_FOLD_CASES = {
    "打开办公室的窗": ("TurnDeviceOn", "办公室", ["climate"]),
    "关闭办公室的门": ("TurnDeviceOff", "办公室", ["climate"]),
    "打开办公室的台灯": ("TurnDeviceOn", "办公室", ["climate"]),
    "打开办公室的空调": ("TurnDeviceOn", "办公室", ["climate"]),
}


@pytest.mark.parametrize("text,intent,name,doms",
                         [(k, *v) for k, v in sorted(NO_FOLD_CASES.items())])
def test_other_nouns_after_area_not_folded(fp, text, intent, name, doms):
    p = _route(fp, text)
    assert p is not None and p.intent == intent, f"{text!r} → {p and p.intent}"
    dev = (_slot(p).get("devices") or [{}])[0]
    assert dev.get("name") == name and dev.get("domains") == doms, (
        f"{text!r} 越界改写：{dev}（窗/门/具名类别各有车道，本批只折唯一域泛称）")
    assert "area" not in _slot(p), f"{text!r} 不许把区域塞进 slot：{_slot(p)}"


def test_english_office_light_shape_unchanged(fp):
    """金标 15 号（'turn on the office light'，PASS）的并集产物逐字不变。"""
    p = _route(fp, "turn on the office light")
    assert p is not None, "英文桥那条不能被本批砍掉"
    assert p.args["target"][1] == {"area": "办公室",
                                   "devices": [{"name": "灯", "domains": ["light"]}]}, p.args


def test_window_lane_untouched(fp):
    """窗道反向钉：本批不得让任何窗句长出 light 域或 area+灯 形。"""
    for text in ("打开平开窗", "关闭办公室的窗", "打开办公室所有窗"):
        p = _route(fp, text)
        if p is None:
            continue
        doms = [d for slot in p.args.get("target") or []
                for dev in (slot.get("devices") or []) for d in (dev.get("domains") or [])]
        assert "light" not in doms, f"{text!r} 窗句长出 light 域：{p.args}"


# ── ③ 结构钉：泛称宇宙是**派生**的，不是第二张手抄表 ──────────────────
def test_generic_universe_derived_from_single_table():
    import ast
    src = (HERE / "core" / "nlu" / "targets.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "_UNIQUE_DOMAIN_GENERIC"), None)
    assert node is not None, "_UNIQUE_DOMAIN_GENERIC 整表消失（或不在顶层）"
    unparsed = ast.unparse(node.value)
    comps = [n for n in ast.walk(node.value)
             if isinstance(n, (ast.GeneratorExp, ast.ListComp, ast.SetComp))]
    assert comps and "_GENERIC_FAMILIES" in ast.unparse(comps[0]), (
        f"泛称宇宙被手抄成字面量（第二张泛称表＝本仓反复强调的实发病根）：{unparsed}")
    assert "len(fam) == 1" in unparsed, f"派生判据（泛称→域一对一）被改掉：{unparsed}"
    assert set(T._UNIQUE_DOMAIN_GENERIC) == {
        g for g, fam in T._GENERIC_FAMILIES.items() if len(fam) == 1}
    assert T._UNIQUE_DOMAIN_GENERIC == ("灯",), T._UNIQUE_DOMAIN_GENERIC
    assert "窗" not in T._UNIQUE_DOMAIN_GENERIC and "门" not in T._UNIQUE_DOMAIN_GENERIC


def test_bare_generic_helper_shape():
    """残段判据本体：只认「(的|里)?+唯一域泛称字」，别的一律空串（宁可不折）。"""
    assert T._bare_generic("的灯") == "灯"
    assert T._bare_generic("灯") == "灯"
    assert T._bare_generic("里的灯") == "灯"
    for bad in ("的窗", "的门", "的射灯", "的灯带", "灯打开", "的台灯", "", "办公室"):
        assert T._bare_generic(bad) == "", f"{bad!r} 不该被折成域"


def test_guard_wired_into_device_substring_lane():
    """接线钉（v1.54 教训：加函数不接线＝没修）：护栏必须在 ⑥ 命中支的 append 之前。"""
    src = (HERE / "core" / "nlu" / "targets.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    hits = [i for i, l in enumerate(lines) if "_bare_generic(stripped[len(d):])" in l]
    assert len(hits) == 1, f"_bare_generic 调用点应恰 1 处，实 {len(hits)}（{hits}）"
    i = hits[0]
    guard = lines[i]
    assert "if" in guard and "if False" not in guard, f"护栏被常量式失效：{guard}"
    assert "continue" in "".join(lines[i + 1:i + 3]), f"护栏没有让位：{lines[i:i + 3]}"
    # v1.6.10 刀P0-4（方位参照物让位）把 ⑥ 取区域那一步换成"方位段只认在册整名"
    # 的两分支（area_pre），旧钉按字面找 `_area_of_prefix(stripped[:idx])` 因而红。
    # 判据本体（护栏在 append 之前 + 恰一处 _bare_generic + 必查 _area_like）一字
    # 未动，只把"被接线的那一行"的形状改成盘上真实交付形状（两条合法出口都算接线）。
    import re as _re
    append_pat = _re.compile(
        r"candidates\.append\(\((?:area_pre|_area_of_prefix\(stripped\[:idx\]\)), d, 5\)\)")
    hits_app = [n for n in range(i, i + 20) if append_pat.search(lines[n])]
    assert hits_app, f"⑥ 命中支的 append 找不到（护栏后 20 行内）:{lines[i:i + 20]}"
    append = hits_app[0]
    assert "_area_like(d)" in guard, "护栏没查区域判据（会吞掉真具名设备）"
    # 必须在 ⑥ 的 append **之前**（append 之后 = 死代码，形同没修）
    assert i < append
    # 反向臂（v1.6.10 补）：⑥ 循环体内护栏**之前**不许出现该 append 的另一种写法，
    # 否则"加了护栏但命中支早走了"——本钉原本只查顺序，没查前面有没有出口。
    earlier = [n for n in range(max(0, i - 14), i) if append_pat.search(lines[n])]
    assert not earlier, f"append 出现在护栏之前（护栏成死码）:{earlier}"
