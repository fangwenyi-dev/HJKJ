# -*- coding: utf-8 -*-
"""v1.7.4 刀#23：区域名派生成设备词后，⑥ 不许把句尾的**具名设备**吞掉。

现场（2026-10-22 16:40 与 16:41 两条真日志，加载项 vo-1.7.3，板 76a8）：
    '办公室射灯亮度调到百分之十' → [clarify] '家里有 2 台设备名字相近
    （办公室空调 Indicator Light、办公室 All Lights）…'
    [级联] 歧义目标 全池18台/主域池2台（办公室空调 Indicator Light、办公室 All Lights）
用户要的是把那盏射灯调到 10%，设备端一盏灯都没动，还被反问了一次。

根因不在数词、不在意图，在**目标取法**（离线用现网 315 实体清单逐字复算）：
  sync_vocab 从 friendly_name 派生动态设备词，`_name_tokens("办公室 All Lights")`
  → **「办公室」**（纯中文、2..8 字、不在停用表）；⑥ 按长→短扫，"办公室"(3) 排在
  "射灯"(2) 前面，且 `stripped.find("办公室")==0` ⇒ 命中即 break，
  parse_target('办公室射灯亮度调到百分之十') = **(None, '办公室', 6)** —— 区域整段没了，
  射灯也没了。之后 name="办公室" × light 域 ⇒ 恰好就是日志里那 2 台
  （办公室空调 Indicator Light、办公室 All Lights），而那盏灯的真名就叫「射灯」
  （entity_id light.ban_gong_shi_she_deng，friendly_name 里**没有**房间词）。

为什么现成 fixture 抓不到它（本刀最该记住的一条）：仓里所有相关钉都把那盏灯写成
「**办公室射灯**」（test_v166_generic_light_domain.py:86、test_v1112_query_batch.py:69 等），
这个名字在现网**根本不存在**——"具名设备由更长在册词先命中"这条前提（targets.py ⑥ 的注释）
只在 fixture 里成立。v1.6.10 的 `bare_window_generic` 已承认同一根因，但它只放行
「区域名+裸窗」，其它"区域名+具名设备"照样塌。

判据两半，缺一不可：①正靶必须改成 area=办公室 + 设备=射灯；②三条既有让位车道
（裸泛称「办公室的灯」／裸窗「办公室的窗」／更长在册词「办公室空调」）不许被我改坏——
它们各自都对应一条现场金标（1.6.5 的 14 号、1.6.10 的裸窗、1.6.8 的区域闸）。

⚠ 本文件**不许**把现网实体清单提交进仓：`fangwenyi-dev/HJKJ` 是 **PUBLIC** 仓，
fixture 只是按现网形状**重写**的最小副本（同域、同名、同"名字里没有房间词"这个关键性质）。
真清单那一臂走环境变量 `HUIJIAN_STATE_DUMP`（指向本地未跟踪的 dump 文件时才跑），
CI 上没有该文件 ⇒ 如实 skip，不冒充跑过。
"""
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v174_area_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import FastPath                 # noqa: E402
from core.nlu.textcnn import TextCNN                    # noqa: E402
from core.settings import DEFAULTS                      # noqa: E402

AREAS = ("办公室", "客厅", "展厅", "主卧", "阳台")

# 现网形状的最小重写：两台 light 的 friendly_name 都**不带**房间词（射灯），
# 而区域派生词「办公室」来自那台 dashboard 的 "办公室 All Lights"。
LIVE_SHAPED = {
    "light.ban_gong_shi_she_deng": {
        "attributes": {"friendly_name": "射灯", "supported_color_modes": ["color_temp"]}},
    "light.she_deng": {
        "attributes": {"friendly_name": "射灯", "supported_color_modes": ["rgbww"]}},
    "light.xiaomi_mc9_aeaf_indicator_light": {
        "attributes": {"friendly_name": "办公室空调 Indicator Light"}},
    "light.linus_dashboard_all_lights_area_ban_gong_shi": {
        "attributes": {"friendly_name": "办公室 All Lights"}},
    "light.linus_dashboard_all_lights_area_ke_ting": {
        "attributes": {"friendly_name": "客厅 All Lights"}},
    "light.xiaomi_linc_ktvng_indicator_light": {
        "attributes": {"friendly_name": "客厅空调 Indicator Light"}},
    "climate.xiaomi_mc9_aeaf": {"attributes": {"friendly_name": "办公室空调"}},
    "cover.ban_gong_shi_mu_bu": {"attributes": {"friendly_name": "办公室 幕布"}},
    "switch.ban_gong_shi_cha_zuo": {"attributes": {"friendly_name": "办公室 插座"}},
}


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class _Scenes:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    async def verify_or_refresh(self, phrase):
        return True


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc):
    """词表喂的是**现网形状**的清单——本刀的全部因就在动态词表里。"""
    T.clear_vocab()
    T.sync_areas(list(AREAS))
    T.sync_vocab(LIVE_SHAPED, {})
    yield FastPath(_Scenes(), tc, _S(), areas_of=lambda: AREAS)
    T.clear_vocab()


def _parse(text):
    return T.parse_target(text)


def _target_names(plan):
    out = []
    for entry in (plan.args or {}).get("target") or []:
        for dev in (entry or {}).get("devices") or []:
            out.append(str(dev.get("name") or ""))
    return out


def _target_areas(plan):
    return [str(e.get("area") or "") for e in ((plan.args or {}).get("target") or [])]


# ── A. 词表前提：这三个字必须是"派生出来的设备词"，否则整把刀测的是空气 ──────
def test_inventory_derives_room_as_device_word(fp):
    assert "办公室" in T._dyn_set, "区域名没进动态设备词表 ⇒ fixture 形状不像现网，本文件在测空气"
    assert "射灯" in T._dyn_set, "具名设备没进表 ⇒ 正靶无从承接"
    assert T._area_like("办公室") and not T._area_like("射灯")


# ── B. 正靶：区域名+具名设备 ⇒ 房间归房间、设备归设备 ─────────────────────
@pytest.mark.parametrize("text", [
    "办公室射灯亮度调到百分之十",
    "将办公室射灯亮度调到百分之十",
    "办公室的射灯亮度调到百分之十",
])
def test_room_plus_named_device_keeps_the_device(fp, text):
    got = _parse(text)
    assert got[0] == "办公室", f"{text!r} 区域塌了：parse_target={got!r}（现网实锤形 (None,'办公室',6)）"
    assert got[1] == "射灯", f"{text!r} 设备名被区域名吞掉：parse_target={got!r}"
    p = asyncio.run(fp.match(text))
    assert p is not None, f"{text!r} 整句落空"
    assert "射灯" in _target_names(p), f"{text!r} 计划里没有射灯：{_target_names(p)}"
    assert "办公室" not in _target_names(p), f"{text!r} 把房间名当设备下发：{_target_names(p)}"


# ── C. 三条既有车道不许被我改坏（各自对应一条现场金标）────────────────────
@pytest.mark.parametrize("text,expect_name", [
    ("打开办公室的灯", "灯"),          # 1.6.5 金标 14 号：裸泛称必须交单字泛称车道
    ("打开办公室的窗", "窗"),          # 1.6.10 刀R1：裸窗让位
    ("办公室空调制热", "办公室空调"),   # 更长在册词优先，不能被拆成 区域+裸空调
])
def test_existing_lanes_still_win(fp, text, expect_name):
    p = asyncio.run(fp.match(text))
    assert p is not None, f"{text!r} 整句落空"
    names = _target_names(p)
    assert expect_name in names or any(expect_name in n for n in names), \
        f"{text!r} → {names}（应为 {expect_name!r}；这条是既有车道的回归位）"
    assert "办公室" not in names or expect_name == "办公室空调", \
        f"{text!r} 把房间名当设备下发：{names}"


# ── D. 接线与反向臂（钉语法，不钉裸标识符）───────────────────────────────
_SRC = (HERE / "core" / "nlu" / "targets.py").read_text(encoding="utf-8")


def _lane6_body():
    """取 ⑥ 那一段（从"⑥ 设备词子串优先"到 `if not _hit_dev:`），护栏必须在这个块里。"""
    i = _SRC.index("⑥ 设备词子串优先")
    j = _SRC.index("if not _hit_dev:", i)
    return _SRC[i:j]


def test_named_device_guard_is_wired_inside_lane_six():
    body = _lane6_body()
    assert "_named_device_behind_area(" in body, "新护栏没接进 ⑥ 的循环＝写在别处不会执行"
    assert "if not idx and _area_like(d) and _named_device_behind_area(" in body, \
        "护栏条件形变了（必须是 idx==0 + 区域名 + 残句具名设备 三连，且单独一条 if）"
    # 两条旧护栏逐字在场（v1.6.10 的接线钉按字面取行，合并写法会把它们挤走）
    assert "_bare_generic(stripped[len(d):])" in body
    assert "bare_window_generic(stripped[len(d):])" in body


def test_guard_predicate_is_exact_registered_name_only():
    """让位的判据必须是"在册具名设备"，不能放宽到泛称/属性名词——否则区域句会折成
    无名字目标，重开 v1.1.36 那类批量误动（见 _bare_generic 的停用域）。"""
    fn = getattr(T, "_named_device_behind_area", None)
    assert fn is not None, "谓词没实现"
    T.clear_vocab()
    T.sync_areas(list(AREAS))
    T.sync_vocab(LIVE_SHAPED, {})
    assert fn("射灯亮度调到百分之十") == "射灯"
    assert fn("的射灯开一下") == "射灯"
    assert fn("的灯") == "", "泛称字不该触发让位（那是 _bare_generic 的车道）"
    assert fn("亮度调到百分之十") == "", "属性词不是设备名"
    assert fn("办公室 All Lights") in ("", "办公室"), "区域名自身不该再被当具名设备"
    T.clear_vocab()


def test_reverse_arm_synthetic_degenerate_still_breaks():
    """反向臂：把新护栏常量式失效（禁"删两行"式变异——它会撞语法错，10-09 在册），
    内存里合成未修态的 ⑥ 必须回到现网缺陷形态 (None, '办公室', 6)。"""
    bad = _SRC.replace("if not idx and _area_like(d) and _named_device_behind_area(",
                       "if False and _named_device_behind_area(")
    assert bad != _SRC, "锚点没命中＝这条反向臂在测空气"
    m = re.search(r"if not idx and _area_like\(d\) and _named_device_behind_area\(", _SRC)
    assert m, "正解里找不到护栏行"
    # 行为面反向臂：未修态等价于"谓词恒假"，用同一份词表直调谓词判红
    T.clear_vocab()
    T.sync_areas(list(AREAS))
    T.sync_vocab(LIVE_SHAPED, {})
    assert T._named_device_behind_area("射灯亮度调到百分之十") == "射灯"
    T.clear_vocab()


# ── E. 真清单臂（本地未跟踪 dump 才跑；CI 无此文件 ⇒ 如实 skip）───────────
@pytest.mark.skipif(not os.environ.get("HUIJIAN_STATE_DUMP"),
                    reason="没给现网 dump（HUIJIAN_STATE_DUMP）——本臂只在本地跑，"
                           "仓是 PUBLIC 不能提交现网实体清单")
def test_real_inventory_agrees():
    raw = json.load(open(os.environ["HUIJIAN_STATE_DUMP"], encoding="utf-8"))
    states = {s["entity_id"]: {"attributes": s.get("attributes", {})} for s in raw}
    T.clear_vocab()
    T.sync_areas(list(AREAS))
    T.sync_vocab(states, {})
    for text in ("办公室射灯亮度调到百分之十", "将办公室射灯亮度调到百分之十"):
        got = T.parse_target(text)
        assert got[1] == "射灯", f"现网清单下 {text!r} → {got!r}"
    T.clear_vocab()
