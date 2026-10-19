# -*- coding: utf-8 -*-
"""v1.6.8 刀14：`resolve_candidates` 的区域闸不得被"名字里带房间词"绕过。

本函数是全仓"按区域挑台"的唯一底层（executor 两处 + pipeline 一处共 3 个消费点），
旧形 `if area and entity_area.get(eid) != area and area not in fn: continue` 里那半句
`area not in fn` 是逃生口：**注册在别的房间、只是 friendly_name 里含这个房间词**的台
照样放行。本机复现：

    light.a 注册=办公室 名字「客厅 灯带」 / light.b 注册=客厅 名字「灯带」
    查「客厅 灯带」→ 旧形命中 [a, b]（a 是别间）；查「办公室 灯带」→ [a]

与刀13（同名改指跨区）同一族：**用户点名了房间，就不能动他没点名的那间**。

新口径：注册表认得的台以**注册区域**为准（同窗控侧
`intent_window_const._candidate_area_tier` 的"确凿别区永不放行"）；只有该台
**没有注册区域**时才退回名字兜底——现网 34 台里 32 台是「客厅 吸顶灯」这种
"名字带房间词"的真形制，那一档不能砍，否则是把 bug 换成不可用。

现网清单实测（办公 .91，2026-10-19 19:4x，走 `config/entity_registry/list` +
`config/device_registry/list` 设备级区域继承，与加项同源口径）：542 实体 / 143 带区域 /
5 个房间，**"名字含别区词且自身有注册区域"的台 = 0** ⇒ 本条收紧在现网零附带改动。
"""
from __future__ import annotations

import sys
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1]
if str(UNIT) not in sys.path:
    sys.path.insert(0, str(UNIT))

from core.capability import resolve_candidates  # noqa: E402


def _e(eid, name, state="off"):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


STATES = {
    "light.a": _e("light.a", "客厅 灯带"),          # 注册在办公室，名字却带"客厅"
    "light.b": _e("light.b", "灯带"),               # 注册在客厅
    "light.c": _e("light.c", "客厅 灯带", "unavailable"),
}
AREA = {"light.a": "办公室", "light.b": "客厅", "light.c": "客厅"}


def _ids(area, states=STATES, entity_area=AREA, name="灯带"):
    got = resolve_candidates(states, entity_area,
                             [{"area": area, "devices": [{"name": name,
                                                          "domains": ["light"]}]}])
    return [g["entity_id"] for g in got]


def test_registered_area_wins_over_name_substring():
    """点名客厅 ⇒ 注册在办公室的那台不得进候选（哪怕名字里写着"客厅"）。"""
    ids = _ids("客厅")
    assert "light.b" in ids, ids
    assert "light.a" not in ids, f"别区台被名字兜底放行了：{ids}"


def test_the_other_room_still_finds_it():
    """反向：那台仍能被"办公室"查到——本刀是把它挪回自己该在的房间，不是删掉它。"""
    assert _ids("办公室") == ["light.a"], _ids("办公室")


def test_unzoned_device_keeps_name_fallback():
    """没有注册区域的台（现网 32/34 那种「客厅 吸顶灯」形制）必须仍按名字兜底命中。"""
    ea = dict(AREA)
    ea.pop("light.a")                     # light.a 名字带"客厅"但没注册区域
    ids = _ids("客厅", entity_area=ea)
    assert "light.a" in ids, f"把无区域台的名字兜底砍了：{ids}"


def test_no_area_in_query_is_untouched():
    """用户没点名房间（area 空）＝全屋按名字找，本刀不得改变这一档。"""
    ids = _ids("")
    assert set(ids) == {"light.a", "light.b", "light.c"}, ids


def test_offline_sinking_cannot_promote_a_cross_room_twin():
    """A2 的放大面：本区唯一台离线时，别间同名台不得靠"离线沉底排序"顶上来。

    口径：`resolve_candidates` 只做"候选＋排序"，离线台**仍在候选里**（拒答由
    executor 的可用性闸负责）——所以这条判的是"别间那台不在表里、且离线本区台
    被沉到末位"，不是判空表。
    """
    st = {"light.b": _e("light.b", "灯带", "unavailable"),
          "light.a": _e("light.a", "客厅 灯带")}
    ids = _ids("客厅", states=st, entity_area={"light.a": "办公室", "light.b": "客厅"})
    assert ids == ["light.b"], f"候选表形态不符（别间台被顶上来？）：{ids}"


# ── 刀14-C1：`Pipeline._best_candidate` ② 不得在"确凿别区"上收敛 ──────────
def _bc(cands, tgt, ea):
    from core.pipeline import Pipeline
    return Pipeline._best_candidate(cands, tgt, None, ea, ("light",))


def test_best_candidate_refuses_confirmed_other_room():
    """用户点名客厅，候选只有注册在办公室的台 ⇒ 按不收敛处理（旧形会收敛出那台名字）。"""
    cands = [{"entity_id": "light.a", "state": "off",
              "attributes": {"friendly_name": "射灯"}}]
    tgt = [{"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]}]
    assert _bc(cands, tgt, {"light.a": "办公室"}) is None, "跨区收敛未被拦"


def test_best_candidate_keeps_unknown_area():
    """反向：该台没注册区域 ⇒ 不参与"确凿别区"判断，沿用旧口径（与刀13 同律）。"""
    cands = [{"entity_id": "light.a", "state": "off",
              "attributes": {"friendly_name": "射灯"}}]
    tgt = [{"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]}]
    assert _bc(cands, tgt, {}) == "射灯"


# ── 刀14-A3：⑦ 拼音档的 `("", …)` —— 已扫，**不在本版修** ────────────────
# 审计指控：⑦ 救援命中时把 area 硬写成 ""，用户说的房间整段丢。
# 本机实测（同词表、5 个形状）：`泰腾`→('','台灯',5)、`客厅泰腾`→('客厅','台灯',6)、
# `办公室泰腾`→('办公室','台灯',6)、`阳台泰腾`→('','台灯',5)、`客厅夜等`→('','夜灯',5)
# —— 带房间词的形**全部由 ⑥/泛称档（`_area_of_prefix(stripped[:idx])`）先接管**，
# ⑦ 那条 append 走不到；把它改成回捞前缀，5 个形状的产物一字不变（变异不红＝零承重）。
# 按本仓"不发零承重的修"的口径：撤改动、留此记录，`targets.py` 与 HEAD 逐字相同。
# 真正还会丢区域的是**未注册成 HA 区域的房间词**（本案"阳台"）——那是
# `_area_like`/`sync_areas` 的覆盖面问题，另案处理，不在本版赌。
