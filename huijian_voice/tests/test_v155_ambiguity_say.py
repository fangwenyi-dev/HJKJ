# -*- coding: utf-8 -*-
"""v1.5.5 歧义话术真实性 + 可执行分叉（HomePod 对标"不确定就不动"半吊子收口）。

现网实锤（10-13 金标 #4『打开办公室的窗』，.91 真机）：办公室有两台窗（『平开窗
开窗器』+『内开内倒 开窗器』），用户点了"窗"这个类、又带了区域，歧义闸正确地
clarify 零下发（宁不猜＝安全侧，这条不动）。**但话术有两处对用户不实/不可照做**：

① **台数把一台窗的零件数成了"名字相近的设备"**：旧调用点传全候选池
   （button ①开启/②暂停/③关闭 + number 速度/力度 + sensor 电池/状态 全含），
   于是播成"家里有 9 台设备名字相近（平开窗 ① 开启…）"——那是 2 台窗 + 一堆
   同机零件，不是 9 台设备。HomePod 不会把一台设备的按钮报成"另一台设备"。
   修：clarify / confirm 两分支的台数与列名一律用**主域收窄池**（cover 那一层）。

② **澄清让用户重复他已经说过的东西**：用户已带"办公室"，话术却让他"带上房间名
   再说一次（比如『打开办公室的射灯』）"——照着做还是死循环。HomePod 式澄清给
   的是**可执行分叉**：已带区域 ⇒ 直接列真候选名让他挑名字；未带区域 ⇒ 才引导
   补房间名，且示例用真实候选名而非硬编码射灯。

钉层纪律（同 v1.1.28）：每条断言对真机事实负责，宁漏勿错侧写；既有三级证据
（全等名>同区域>主域）与"收不住不代挑"的原判据一字不许放宽。
"""
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v155_"))

from core.pipeline import Pipeline                      # noqa: E402
from core.nlu.fast_path import Plan                     # noqa: E402


def _pipe(states, entity_area, extra=None):
    class _S:
        def __init__(self, d):
            self.d = d

        def get(self, k, default=None):
            return self.d.get(k, default)

    p = Pipeline.__new__(Pipeline)
    p.settings = _S(extra or {})
    p._confirm = {}
    p._origin_ts = {}
    p.ha = types.SimpleNamespace(_states=dict(states), _entity_area=dict(entity_area))
    return p


# 复现 .91 #4 现场：办公室两台 cover + 平开窗机的一堆兄弟零件（button/number/sensor）
# 全部命中"窗"前缀；旧口径把它们全数进"名字相近的设备"。
_OFFICE_WIN = {
    "cover.pk": {"entity_id": "cover.pk", "state": "open",
                 "attributes": {"friendly_name": "平开窗 开窗器"}},
    "cover.nknd": {"entity_id": "cover.nknd", "state": "open",
                   "attributes": {"friendly_name": "内开内倒 开窗器"}},
    "button.pk_1": {"entity_id": "button.pk_1", "state": "2026-01-01T00:00:00",
                    "attributes": {"friendly_name": "平开窗 ① 开启"}},
    "button.pk_2": {"entity_id": "button.pk_2", "state": "unknown",
                    "attributes": {"friendly_name": "平开窗 ② 暂停"}},
    "button.pk_3": {"entity_id": "button.pk_3", "state": "2026-01-01T00:00:00",
                    "attributes": {"friendly_name": "平开窗 ③ 关闭"}},
    "number.pk_su": {"entity_id": "number.pk_su", "state": "31.0",
                     "attributes": {"friendly_name": "平开窗 速度"}},
    "number.pk_li": {"entity_id": "number.pk_li", "state": "unknown",
                     "attributes": {"friendly_name": "平开窗 力度"}},
    "sensor.pk_dian": {"entity_id": "sensor.pk_dian", "state": "11.3",
                       "attributes": {"friendly_name": "平开窗 电池电压"}},
    "sensor.pk_zhuang": {"entity_id": "sensor.pk_zhuang", "state": "open",
                         "attributes": {"friendly_name": "平开窗 状态"}},
}
_OFFICE_AREA = {k: "办公室" for k in _OFFICE_WIN}


def _plan4():
    # t0 泛窗纠正后落地的形状：area=办公室、name=窗、域并集（ControlWindow）
    return Plan(
        intent="ControlWindow",
        args={"target": [{"area": "办公室",
                          "devices": [{"name": "窗", "domains": []}]}],
              "action": "open"},
        source="t0", utterance="打开办公室的窗")


def test_clarify_counts_only_real_devices_not_parts():
    """①真机 #4：话术台数必须是**窗那一层**（2 台），不得把按钮/数值/传感器数进来。

    旧形＝9 台；修后＝2 台。这是安全侧收口，不放宽任何判据：仍然 clarify 零下发、
    仍然不替用户挑一台。"""
    p = _pipe(_OFFICE_WIN, _OFFICE_AREA)
    r = p._ambiguity_ask(_plan4(), "o")
    assert r is not None and r.source == "clarify" and r.ok is False, r
    txt = r.text or ""
    # 台数：只数 cover 池 ⇒ 2 台，绝不是含零件的 9
    assert "2 台" in txt, f"台数未按主域收窄池（含零件＝9 的不实陈述回来了）：{txt}"
    assert "9 台" not in txt, txt
    # 列名只列真设备（两台窗），一台窗的按钮不得作为"另一台设备"出现
    assert "平开窗 开窗器" in txt and "内开内倒 开窗器" in txt, txt
    assert "① 开启" not in txt and "速度" not in txt and "电池" not in txt, \
        f"同机零件被报成独立设备：{txt}"


def test_clarify_gives_actionable_fork_when_area_already_said():
    """②用户已带"办公室" ⇒ 不得再让他"带上房间名再说一次"（那是死循环）。

    HomePod 式：改成列真候选名让他挑。断言"带上房间名"不出现、且给出两台真名。"""
    p = _pipe(_OFFICE_WIN, _OFFICE_AREA)
    r = p._ambiguity_ask(_plan4(), "o")
    txt = r.text or ""
    assert "带上房间名再说一次" not in txt, \
        f"已带区域仍引导补房间名＝让用户重复他说过的话：{txt}"
    # 给出可照做的分叉：直接指两台窗的名字
    assert "哪一台" in txt and "平开窗 开窗器" in txt, txt
    # 零下发、不挂执行桩（安全侧原样保留）
    assert p._confirm == {}, "未收敛却挂了执行桩"


def test_confirm_branch_also_counts_narrowed_pool():
    """同律：确认环那句也按主域收窄池报台数，不得拿全池 distinct。

    地形：两台窗同名孪生风险场景下能唯一定位到一台 ⇒ 走 confirm 挂环。这里构造
    "有主域证据但收窄后仍两台异名"的挂环形态，钉那句念的是 2 台窗而非含零件的数。"""
    p = _pipe(_OFFICE_WIN, _OFFICE_AREA)
    # name 与某台全等会走静默收窄；这里故意用一个收不住但候选≥2 的点名词触发挂环
    # 分支——用 _ambiguity_clarify_say/confirm 共用的报数函数面直接验更稳，故经
    # 单元级断言（见下），不靠拼地形。此钉确保调用点确实用 pool 计数而非 cands。
    import inspect
    src = inspect.getsource(Pipeline._ambiguity_ask)
    # clarify 与 confirm 两条腿都不许再用 len(cands) / distinct 作为对外报数
    assert "_ambiguity_clarify_say(pool_names" in src, src
    assert "len(conf_names)" in src, "确认环仍拿全池 distinct 计数"
    assert "家里有 {len(distinct)}" not in src, "旧全池报数模板残留"


def test_say_unit_area_and_no_area_branches():
    """话术函数单元：带 area ⇒ 挑名字、不带 area ⇒ 引导补房间名（示例含真候选名）。"""
    s_dev = Pipeline._ambiguity_clarify_say(["平开窗 开窗器", "内开内倒 开窗器"],
                                            2, area="办公室")
    assert "2 台" in s_dev and "哪一台" in s_dev and "带上房间名再说一次" not in s_dev
    s_narea = Pipeline._ambiguity_clarify_say(["射灯", "氛围灯"], 2, area="")
    assert "带上房间名再说一次" in s_narea and "射灯" in s_narea
    # 永不抛、空候选也有兜底句式（不得拼出「打开None」这类）
    s_empty = Pipeline._ambiguity_clarify_say([], 0, area="")
    assert "带上房间名" in s_empty and "None" not in s_empty
