# -*- coding: utf-8 -*-
"""v1.2.10 现场实锤：「(区域)所有窗户/灯/设备」批量口令的三处洞。

现场（办公 .91，线上 v1.2.9，2026-10-08 17:21:01 逐字）：

    [执行] ControlWindow {'target': [{'area': '展厅', 'devices': [{'name': '窗户', 'domains': []}]}],
                          'action': 'close'} → 成功 | 好的，「窗户」我没找到

集成侧**真的**关了展厅三扇窗（悬窗/推拉窗/内开窗 的「③ 关闭」按钮时间戳
09:20:59–09:21:00Z，与日志同刻），而加载项判据面 `Executor._leg_truth` 按
"friendly_name 名字子串"查证据——展厅没有任何实体名里含「窗户」二字
（它们叫「悬窗 开窗器」），于是泛称被判"查无此名"，把成功回执改写成失败话术。
**病灶是判据面与执行面对同一个 name 的语义不同源**：执行面（intent_window_control
的 `is_bare_window_name` 分支）刻意把裸窗字升级成"本区域全部窗"，判据面却拿它当设备名。

同句少一个「户」字（ASR 常态）更糟：t0 把区域名「展厅」当设备名，
`domains=[media_player]` —— 指的是展厅那台音箱，不是窗。
"""
import asyncio
import pathlib
import re
from types import SimpleNamespace

import pytest

from conftest import FakeHAClient
from core import capability
from core.executor import Executor
from core.nlu import targets as T
from core.nlu.fast_path import FLAG_AREA_BULK, FastPath, Plan

# ── 生产地形（.91 真实 friendly_name 逐字，区域按设备注册表继承）──────────
AREA_NAMES = ("展厅", "办公室")


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state,
            "attributes": {"friendly_name": name}}


# (entity_id, state, friendly_name, 所属区域)——.91 逐字切片。
# 办公室刻意带上「射灯 确认」(button) 与「射灯 固件」(update)：反向钉要的是
# "点名所有灯时不许冒出未动台数"这条**有牙**的判据——办公室若只有灯，
# 撤掉门槛突变也产不出注，钉会为错误的理由变绿（变异自检 M7 实测抓到过）。
_ROWS = [
    ("cover.kai_chuang_qi_123f_020a_02_kai_chuang_qi", "closed", "悬窗 开窗器", "展厅"),
    ("cover.kai_chuang_qi_123f_041e_03_kai_chuang_qi", "closed", "推拉窗 开窗器", "展厅"),
    ("cover.kai_chuang_qi_123f_0259_04_kai_chuang_qi", "closed", "内开窗 开窗器", "展厅"),
    ("media_player.zhan_ting", "idle", "展厅", "展厅"),
    ("sensor.kai_chuang_qi_123f_020a_02_zhuang_tai", "closed", "悬窗 状态", "展厅"),
    ("button.kai_chuang_qi_123f_020a_02_3_guan_bi", "2026-10-08T09:20:59+00:00",
     "悬窗 ③ 关闭", "展厅"),
    ("number.kai_chuang_qi_123f_020a_02_li_du", "100.0", "悬窗 力度", "展厅"),
    ("light.ban_gong_shi_she_deng", "off", "射灯", "办公室"),
    ("button.ban_gong_shi_she_deng_que_ren", "unknown", "射灯 确认", "办公室"),
    ("update.ban_gong_shi_she_deng_gu_jian", "unknown", "射灯 固件", "办公室"),
]


def snapshot():
    return {eid: _ent(eid, s, fn) for eid, s, fn, _a in _ROWS}


def entity_area(states):
    return {eid: a for eid, _s, _fn, a in _ROWS if eid in states}



BULK_ARGS = {"target": [{"area": "展厅",
                         "devices": [{"name": "窗户", "domains": []}]}],
             "action": "close"}
# 集成侧全窗分支的真实返回形制（intent_window_control._all_window_result）
ALL_WINDOW_OK = {"success": True, "message": "已展厅所有窗户关闭",
                 "buttons": [{"action": "close", "entity_id": "button.x"}]}


class Ha(FakeHAClient):
    def states_stale(self):
        """与 ha_client:555 同形（**同步**方法）——替身写成 async 会让
        executor 的 `str(_st() or "")` 拿到协程对象，判据在测试里走样。"""
        return ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # 与 .91 注册表实况同形（_bench/probe_huijian_exposure_1008.py 逐字核过）：
        # 卫星自家实体 platform=huijian_ai，其余按平台命名（xiaomi/kai_chuang_qi…）。
        self._entity_platform = {
            eid: (capability.BULK_SELF_PLATFORM if ".huijian_" in eid else "other")
            for eid in (self._states or {})}
        # 表由夹具外置（_run(devices=…)）：缺省空表＝每条实体自成一摊，与生产
        # "注册表拉不到 device_id" 的降级态同形（见 executor._bulk_skipped_note）。
        self._entity_device: dict[str, str] = {}


def _run(states, plan, results, areas=None, devices=None):
    ha = Ha(results=results, states=states,
            entity_area=areas if areas is not None else entity_area(states))
    if devices:
        ha._entity_device = dict(devices)
    return asyncio.run(Executor(ha).run(plan))


@pytest.fixture(autouse=True)
def _static_vocab():
    T.clear_vocab()
    yield
    T.clear_vocab()


# ══ A. 判据面与执行面同源（现场那条播报）═══════════════════════════
def test_generic_window_bulk_success_is_not_reported_missing():
    """执行成功 + 名字是裸窗泛称 ⇒ 播报里不得出现「我没找到」。

    夹具纪律：区域表必须真收进这三扇 cover，否则 _leg_truth 会先被区域护栏
    挡回 ("", "")，钉会为错误的理由变绿（同 test_leg_truth_all_steps 的教训）。
    """
    st = snapshot()
    plan = Plan(intent="ControlWindow", args=BULK_ARGS, source="t0",
                utterance="关闭展厅所有窗户")
    ok, reply = _run(st, plan, {"ControlWindow": ALL_WINDOW_OK})
    assert ok is True, reply
    assert "没找到" not in reply, reply


def test_bare_window_word_is_a_class_selector_not_a_device_name():
    """单源谓词：裸窗字/显式全窗泛称＝类别选择器，不是设备名。"""
    for w in ("窗户", "窗", "窗子", "所有窗户", "全部窗"):
        assert capability.is_generic_window_name(w), w
    # 反向：具名窗型与修饰段构成的真名字**必须**仍可被点名（「点名查无」闸的原料）
    for w in ("推拉窗", "内开窗", "会飞的窗", "2号窗", "射灯"):
        assert not capability.is_generic_window_name(w), w


def test_named_absent_window_is_still_named_missing():
    """反向钉：修泛称不得把"点了家里没有的窗名"一起豁免掉。"""
    st = snapshot()
    args = {"target": [{"area": "展厅",
                        "devices": [{"name": "会飞的窗", "domains": ["cover"]}]}],
            "action": "close"}
    ok, reply = _run(st, Plan(intent="ControlWindow", args=args, source="t0",
                               utterance="关闭展厅会飞的窗"),
                     {"ControlWindow": ALL_WINDOW_OK})
    assert ok is True
    assert "会飞的窗" in reply and "没找到" in reply, reply


def test_core_window_class_table_equals_integration_side():
    """跨包手抄必漂移（v1.1.27 `_UNTOGGLEABLE_DOMAINS` 两表少抄半张的教训）。

    core（加载项容器）与 custom_components（HA 内）不能互相 import，
    所以这条钉按 AST 取两边的**字面量集合**逐字比——执行面升级全窗用的
    是集成侧那张表，判据面豁免用的是 core 侧这张表，两者不等即"同源"破产。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    core_src = (root / "core" / "capability.py").read_text(encoding="utf-8")
    integ_src = (root / "custom_components" / "huijian_ai" /
                 "intent_window_const.py").read_text(encoding="utf-8")

    def literal_tuple(src, name):
        m = re.search(rf"^{name}\s*=\s*\(([^)]*)\)", src, re.M)
        assert m, f"字面量表 {name} 找不到（改名即断）"
        return tuple(re.findall(r'"([^"]+)"', m.group(1)))

    core_bare = literal_tuple(core_src, "BARE_WINDOW_NAMES")
    integ_bare = literal_tuple(integ_src, "_BARE_WINDOW_NAMES")
    assert core_bare == integ_bare, (core_bare, integ_bare)
    core_generic = set(literal_tuple(core_src, "GENERIC_WINDOW_NAMES"))
    integ_generic = set(re.findall(r'"([^"]+)"', re.search(
        r"GENERIC_WINDOW_NAMES\s*=\s*\[(.*?)\]", integ_src, re.S).group(1)))
    assert core_generic == integ_generic, (core_generic, integ_generic)


# ══ B. t0 接管「(区域)所有+类别词」═══════════════════════════════
def _fp():
    class SC:
        def needs_blocking(self):
            return False

        def refresh_soon(self):
            pass

        async def refresh(self, force=False):
            pass

        def check(self, text):
            return None

    st = snapshot()
    T.sync_vocab(st, entity_area(st))
    T.sync_areas(AREA_NAMES)
    from core.settings import Settings
    import os
    return FastPath(SC(), None, Settings(
        pathlib.Path(os.environ["HUIJIAN_DATA"]) / f"v1210-{os.getpid()}.json"))


def _m(text, fp=None):
    return asyncio.run((fp or _fp()).match(text))


def test_area_all_windows_is_controlwindow_not_media_player():
    """现场同句：区域+所有+窗户 ⇒ ControlWindow，且区域必须是硬约束。"""
    p = _m("关闭展厅所有窗户")
    assert p is not None and p.intent == "ControlWindow", p
    tgt = p.args["target"][0]
    assert tgt["area"] == "展厅" and p.args["action"] == "close"
    assert capability.is_generic_window_name(tgt["devices"][0]["name"])


def test_area_all_windows_bare_word_not_hijacked_by_device_name():
    """ASR 丢「户」：今天 t0 产出 TurnDeviceOff{name=展厅,domains=[media_player]}
    ——把音箱当窗关。裸「窗」必须同样落 ControlWindow + 区域。"""
    p = _m("关闭展厅所有窗")
    assert p is not None, "区域+所有+裸窗不得落空"
    assert p.intent == "ControlWindow", (p.intent, p.args)
    assert p.args["target"][0]["area"] == "展厅", p.args
    doms = p.args["target"][0]["devices"][0].get("domains") or []
    assert "media_player" not in doms, p.args


def test_area_all_lights_targets_light_domain_only():
    """v1.2.10 修订：具名类别词要把**用户说的词**一起带下去（见
    `test_named_class_bulk_carries_the_spoken_word` 的 P0 理由）——本钉旧形断
    `name==""` 正是在给那条 P0 背书，改成断 name=灯 + domains=[light]。"""
    p = _m("打开展厅所有灯")
    assert p is not None and p.intent == "TurnDeviceOn", p
    slot = p.args["target"][0]
    assert slot["area"] == "展厅"
    dev = slot["devices"][0]
    assert dev["domains"] == ["light"] and dev["name"] == "灯", dev
    assert FLAG_AREA_BULK in p.flags


def test_area_all_devices_uses_toggleable_whitelist_without_lock():
    """「(区域)所有设备」＝只动可开关域；lock 刻意不在名单里
    （批量拔锁与 v1.0.71 跨区误绑同族不可逆）。"""
    p = _m("关闭展厅所有设备")
    assert p is not None and p.intent == "TurnDeviceOff", p
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "" and dev["domains"] == list(
        capability.BULK_TOGGLEABLE_DOMAINS), dev
    assert "lock" not in dev["domains"]
    assert FLAG_AREA_BULK in p.flags


def test_area_all_trailing_verb_form():
    p = _m("把展厅所有灯关掉")
    assert p is not None and p.intent == "TurnDeviceOff", p
    assert p.args["target"][0]["area"] == "展厅"


def test_whole_house_all_devices_still_refused():
    """反向钉：**全屋**「所有设备」维持整单拒绝（v1.0.41 F13 的裁决不动）——
    新道只接"有区域收窄"的批量形。"""
    assert _m("打开所有设备") is None


def test_negation_and_compound_are_not_hijacked():
    """反向钉：否定腿与并列句不得被新道劫持（10-08 P1-7「归一道吃否定前缀」同族）。"""
    for s in ("别关闭展厅所有灯", "不要打开展厅所有窗户",
              "关闭展厅所有灯和打开办公室射灯"):
        p = _m(s)
        assert p is None or FLAG_AREA_BULK not in p.flags, (s, p)


def test_unknown_area_is_not_taken():
    """区域不在表 ⇒ 不接管（宁交上层，绝不把没登记的房间当批量目标）。"""
    assert _m("关闭天台所有灯") is None


def test_parameter_and_inward_tilt_sentences_are_not_hijacked():
    """反向钉：内倒/百分比开度/档位句各有自己的车道（`_position_plan`、
    `_ACTION_PATTERNS` 的内倒项、`_tail_window_action`）——批量道伸手会**丢掉动作**：
    「内倒」折成 close 是真事故（用户要内倒、窗整扇打开）。"""
    for s in ("关闭展厅所有窗户内倒", "关闭展厅所有窗户开到50%",
              "关闭展厅所有灯调到最亮", "内倒展厅所有窗户"):
        p = _m(s)
        assert p is None or FLAG_AREA_BULK not in p.flags, (s, p and p.args)


def test_ac_and_fan_classes_route_to_their_own_domains():
    """反向自伤钉：参数标记必须按**词形**扫类别段，不能按单字扫整句——
    否则「所有空调」被自己的"调"字拦掉、「所有设备」被"设"字拦掉（本轮实测踩过）。
    类别词→域仍走 query.class_of 单源，不在本道手抄。"""
    p = _m("关闭展厅所有空调")
    # 反向自伤的判据必须是**本道产物**（area_bulk 旗 + 该域 + 名字带住）——原车道也会
    # 给出 domains=['climate']，只看域会让这条钉为别人背书（M9 突变实测漏网过一次）。
    assert p is not None and FLAG_AREA_BULK in p.flags, p and (p.flags, p.args)
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "空调" and dev["domains"] == ["climate"], p.args
    p2 = _m("打开展厅所有风扇")
    assert p2 is not None and FLAG_AREA_BULK in p2.flags, p2 and p2.flags
    assert p2.args["target"][0]["devices"][0]["domains"] == ["fan"], p2.args


def test_curtain_bulk_must_not_reach_window_openers():
    """对抗复核推翻的 P0（我自己引入）：把「窗帘」折成 `domains=["cover"]` 就完了。

    这台 HA 的 cover 域**全是开窗器**（悬窗/推拉窗/内开窗 开窗器，零台帘）⇒
    只带域、不带名字的批量目标 = 用户说关帘、真按了三扇窗，还播「好的，展厅 3 台都关了」。
    改前那条句走原车道 `name="窗帘"` → 集成严格匹配+包含回捞都不中 → 如实
    「没找到符合条件的设备」。域不是类别：具名类别词必须把**名字一起带下去**
    （集成 `_contains_name_states` 按 同域+名称包含 回捞，才既接得住真帘、又不碰开窗器）。
    """
    p = _m("关闭展厅所有窗帘")
    assert p is not None, p
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "窗帘", (dev, "只带域=会打到开窗器")
    assert dev["domains"] == ["cover"], dev
    p2 = _m("关闭展厅所有纱窗")
    assert p2.args["target"][0]["devices"][0]["name"] == "纱窗", p2.args


def test_named_class_bulk_carries_the_spoken_word():
    """同族第二处：`domains=["light"]` 单独下会吃掉「办公室空调 Indicator Light」
    （真实地形：办公室 2 台 light，一台是空调指示灯）——名字带住才只打真灯。
    「所有设备」是唯一该按域展开的口径（它就是"全部可开关设备"，另如实报没动的台数）。"""
    p = _m("关闭办公室所有灯")
    assert p is not None, p
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "灯" and dev["domains"] == ["light"], dev
    # 设备口径仍保持空名+白名单域（播报侧靠它算"没动的台数"）
    p2 = _m("关闭展厅所有设备")
    dev2 = p2.args["target"][0]["devices"][0]
    assert dev2["name"] == "", dev2
    assert dev2["domains"] == list(capability.BULK_TOGGLEABLE_DOMAINS), dev2


def test_inward_tilt_homophone_reaches_action_a_not_close():
    """声学实测形（「…窗户内倒」被听成「…窗户内道」）必须落 action='a'。

    根因不是同音表缺一条（corrector 只在**词界**替换，句中「…窗|内道」刻意不生效），
    而是窗型动作两张表不同源：头位收 9 档、尾位扫描只收 3 档 ⇒ 尾位扫不到就退到
    `关` 字档，用户要内倒、窗整扇关（说的 A 做的 B）。同时钉住我的批量道不许抢它。
    """
    for w in ("内道", "内达", "内藻", "内倒"):
        p = _m(f"关闭展厅所有窗户{w}")
        assert p is not None, w
        assert FLAG_AREA_BULK not in p.flags, (w, p.args)   # 归原车道逐窗处理
        assert p.intent == "ControlWindow", (w, p.intent, p.args)
        assert p.args.get("action") == "a", (w, p.args)


def test_inward_tilt_tail_scan_does_not_eat_device_names():
    """反向钉（刻意不对称的代价面）：尾位不收 内打/内大——「关闭内大开窗器」这类
    以它们起头的设备名不许被判成"内倒"（那是反向误执行，比扫不到更糟）。"""
    p = _m("关闭内大开窗器")
    assert p is not None, p
    assert p.intent == "ControlWindow", (p.intent, p.args)
    assert p.args.get("action") == "close", p.args


def test_possessive_residual_is_not_hijacked():
    """cat 中间带「的」说明目标不是纯类别词（「所有灯的罩」≠ 灯）⇒ 本道不接管。

    句子刻意压到长度闸之内（cat="灯的罩" 3 字），否则这条钉会靠"太长"蒙对。
    """
    for s in ("关闭展厅所有灯的罩", "关闭展厅所有窗的把手"):
        p = _m(s)
        assert p is None or FLAG_AREA_BULK not in p.flags, (s, p and p.args)


# ══ C. 批量回执话术══════════════════════════════════════════════
def test_bulk_kept_note_names_only_real_risky_domains():
    """「N 台为防误动」后面那个括号只能写**真在场**的那一类：
    锁在场才说门锁；只有遥控器时还说"门锁这类"就是编因果。"""
    args = {"target": [{"area": "展厅",
                        "devices": [{"name": "", "domains": list(
                            capability.BULK_TOGGLEABLE_DOMAINS)}]}]}
    ctl = [{"name": "悬窗 开窗器", "area": "展厅", "success": True}]

    def one(extra_eid, extra_name):
        st = snapshot()
        st[extra_eid] = _ent(extra_eid, "off", extra_name)
        amap = entity_area(st)
        amap[extra_eid] = "展厅"
        return _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                             utterance="关闭展厅所有设备"),
                    {"TurnDeviceOff": {"success": True, "control_targets": ctl}},
                    areas=amap)[1]

    with_lock = one("lock.da_men", "大门锁")
    assert "门锁这类" in with_lock, with_lock
    only_remote = one("remote.zhan_ting", "展厅遥控器")
    assert "门锁" not in only_remote, only_remote
    assert "1 台为防误动" in only_remote, only_remote

def test_noop_label_uses_the_resolved_device_name_not_the_class_word():
    """P0 修复的下游同族坑：具名类别词带下去后，空操作点名别再念类别词。

    「关闭办公室所有灯」在 .91 只命中一台 `light.ban_gong_shi_she_deng`（射灯），
    播报该是「射灯本来就在要求的状态上」；念「灯」是把用户自己的词当设备名回给他，
    而纯批量形（「所有设备」，name="")没有可比名字，仍走「区域 + N 台」。
    """
    st = snapshot()
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "灯", "domains": ["light"]}]}]}
    ok, reply = _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                              utterance="关闭办公室所有灯"),
                     {"TurnDeviceOff": {"success": True,
                                        "control_targets": [
                                            {"name": "射灯", "area": "办公室",
                                             "success": True}]}})
    assert ok is True, reply
    assert "射灯" in reply, reply
    assert "「灯」" not in reply, reply


# ── 集成侧替身：HA 实体注册表条目的最小形制（直调真函数，不 mock 真函数）──
class _S:
    """HA State：批量面只读 entity_id 与 attributes.friendly_name。"""

    def __init__(self, eid, fname, state="on"):
        self.entity_id, self.name = eid, fname
        self.state = state
        self.attributes = {"friendly_name": fname}


class _E:
    """注册表条目：platform / config_entry_id / device_id / entity_category。"""

    def __init__(self, platform=None, cid=None, device_id=None, category=None):
        self.aliases, self.name = [], None
        self.area_id, self.device_id = None, device_id
        self.platform, self.config_entry_id = platform, cid
        self.entity_category = category


class _Reg:
    def __init__(self, m):
        self.m = m

    def async_get(self, k):
        return self.m.get(k)


class _Hass:
    states = None
    config_entries = None
    data = {}


def _integ_pick(rows, requested_name=None, order=None):
    """把注册表条目喂给集成侧真函数，返回它**真正选中**的 entity_id（按入参序）。

    `mod.er/ar/dr` 是模块级全局替换 ⇒ 每次调用都重装箱，不依赖上一条钉留下的状态
    （跨文件顺序一变就假绿/假红）。区域查不到返回 None 是本夹具的预期（area_id
    全 None，与 .91 注册表实况同形）。
    """
    from tests.test_v1125_contains_name_fallback import _load_helper

    mod = _load_helper()
    ents = dict(rows)
    keys = list(ents) if order is None else list(order)
    hass = _Hass()
    hass.config_entries = SimpleNamespace(
        async_entries=lambda d: [SimpleNamespace(entry_id="entry-hj")]
        if d == "huijian_ai" else [])
    reg = _Reg(ents)
    mod.er = mod.ar = mod.dr = type(
        "M", (), {"async_get": staticmethod(lambda _h: reg)})
    item = mod.StateWithAreaConstraint(
        states=[_S(k, k) for k in keys], unset_area_constraint=False,
        requested_name=requested_name)
    return [e.state.entity_id for e in mod._build_entities_for_item(hass, item, reg)]


def test_bulk_target_never_selects_the_satellite_itself():
    """集成侧（真正选设备的那一层）：批量展开必须跳过语音卫星自己的实体。

    客户把卫星登记在哪个房间，那句「关闭客厅所有设备」就会把它的麦克风开关/播放器
    一起关掉 ⇒ 自杀式静音，下一句再没人听得见。2026-10-08 用户明令"不要关 esp 相关"。
    两条证据路各钉一次（注册表 platform / config entry 归属），并钉住**具名句仍可动**
    （用户点名「关闭小智音箱」不许被这条挡掉）。
    """
    ents = {"cover.kai_chuang_qi": _E(),                             # 开窗器：真设备
            "switch.huijian_1f04_mai_ke_feng_kai_guan": _E("huijian_ai"),  # 卫星麦克风
            "media_player.huijian_1f04_mei_ti_bo_fang_qi": _E("huijian_ai")}
    # platform 缺失、只靠 config entry 归属的那台（老 HA 形态）
    ents["switch.huijian_32b8_lian_xu_dui_hua"] = _E(None, "entry-hj")

    bulk = _integ_pick(ents)
    assert bulk == ["cover.kai_chuang_qi"], bulk        # 三台卫星实体（两条证据路）都不在
    named = _integ_pick(ents, "麦克风开关")
    assert len(named) == len(ents), named               # 具名句一台都不许凭空消失


# ══ E. 集成侧「(区域)所有设备」＝一台设备只动它的电源位═══════════════
# 办公室空调（.91 注册表实测名下 16 条实体，_bench/probe_bulk_office_1008.py 逐字取类）：
# 本体是 climate，其余 7 条 switch 是 ECO/上下摆风/左右摆风/辅热/干燥/睡眠/提示音这些
# **功能位**，且它们 `entity_category`·`capabilities` 全是 None ⇒ 只有域能判。
_AC = "dev_xiaomi_mc9_aeaf"
_OFFICE_ROWS = {
    "climate.xiaomi_mc9_aeaf_air_conditioner": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_eco": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_shang_xia_bai_feng": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_zuo_you_bai_feng": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_fu_re": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_gan_zao": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_shui_mian": _E(device_id=_AC),
    "switch.xiaomi_mc9_aeaf_ti_shi_yin": _E(device_id=_AC),
    "light.xiaomi_mc9_aeaf_indicator_light": _E(device_id=_AC, category="config"),
    "button.xiaomi_mc9_aeaf_xin_xi": _E(device_id=_AC, category="diagnostic"),
    # 智能插座：本来就是一路开关，折叠后**必须留下**（别把"电源位"折成"零台"）
    "switch.aqara_zhi_neng_cha_zuo": _E(device_id="dev_plug"),
    # 开窗器：cover 是本体、number 力度是零件，同一台设备
    "number.kai_chuang_qi_123f_020a_02_li_du": _E(device_id="dev_win"),
    "cover.kai_chuang_qi_123f_020a_02_kai_chuang_qi": _E(device_id="dev_win"),
    # 没有 device_id 的孤儿实体（现网成片存在）：不许被并成"同一台设备"
    "light.wu_device_a": _E(),
    "light.wu_device_b": _E(),
}

_BULK_KEEP = ["climate.xiaomi_mc9_aeaf_air_conditioner",
              "cover.kai_chuang_qi_123f_020a_02_kai_chuang_qi",
              "light.wu_device_a", "light.wu_device_b",
              "switch.aqara_zhi_neng_cha_zuo"]


def test_bulk_devices_touch_only_each_devices_power_entity():
    """「关闭办公室所有设备」：一台设备一条，功能位/指示灯/信息按钮都不许被按。

    这是用户 2026-10-08 的原话口径——"空调只需要关闭空调电源就行吧"。一条口令把
    ECO+上下摆风+左右摆风+辅热+干燥+睡眠+提示音 全按一遍，等于替他把遥控器摸了一遍，
    而且回执会说"8 台都关了"（其实是 1 台）。
    """
    assert sorted(_integ_pick(_OFFICE_ROWS)) == _BULK_KEEP


def test_bulk_pick_does_not_depend_on_registry_order():
    """同一批实体，注册表遍历序倒过来 ⇒ 选中的必须**逐条相同**。

    折叠若写成"保留最后一条"之类，结果会跟着 HA 启动顺序漂——同一句话今天关这台、
    明天关那台，是本类判据最难查的一种活缺陷。
    """
    rev = list(reversed(list(_OFFICE_ROWS)))
    assert sorted(_integ_pick(_OFFICE_ROWS, order=rev)) == _BULK_KEEP


def test_bulk_entities_without_device_id_are_not_merged():
    """反向钉：无 device_id 的实体各自成摊，**夹具必须跨域**。

    折叠键若图省事写成常量（`"@"`），现网一大片孤儿实体会被当成"同一台设备"，
    「所有设备」只动其中一条却报成功。这里刻意放两个域——单域那一摊根本进不了折叠
    （见 `test_single_domain_bulk_group_is_not_collapsed`），只放两条 light 的话
    本钉会对着一段永不执行的分支空跑（M21 在新守卫下实测漏网过一次）。
    """
    rows = {"light.wu_device_a": _E(), "light.wu_device_b": _E(),
            "climate.wu_kong_tiao": _E()}
    assert sorted(_integ_pick(rows)) == ["climate.wu_kong_tiao",
                                         "light.wu_device_a", "light.wu_device_b"]


def test_named_target_is_never_collapsed():
    """反向钉：折叠只属于批量（空名目标）。点名「空调 ECO」必须还能打到功能位。

    用户自己圈定的范围不能被他没说过的话替他做主——这条同时挡住"把卫星/零件位
    豁免也顺手扩到具名面"的下一次改动。
    """
    got = _integ_pick(_OFFICE_ROWS, requested_name="ECO")
    assert len(got) == len(_OFFICE_ROWS), got
    assert "switch.xiaomi_mc9_aeaf_eco" in got, got


def test_single_domain_bulk_group_is_not_collapsed():
    """反向钉：整组只有一个域 ⇒ 不许折叠（双路灯的两盏都是客户要的那两盏）。

    「开灯」经 klar/LLM 传下来是 `name="" + domains=[light]`——空名但**单域**。
    这种组里"哪条是本体、哪条是零件"没有任何判据可依，硬按设备折叠＝把一盏灯
    悄悄关掉不报（比空调那个"多按了功能位"更坏：那是多动，这是少动还谎报成功）。
    """
    rows = {"light.shuang_lu_deng_a": _E(device_id="dev_lamp"),
            "light.shuang_lu_deng_b": _E(device_id="dev_lamp")}
    assert sorted(_integ_pick(rows)) == ["light.shuang_lu_deng_a",
                                         "light.shuang_lu_deng_b"]
    # 同一夹具加一条别的域（空调本体）⇒ 跨域混装才触发折叠判据；此时双路灯的第二路
    # 按"一台设备只动电源位"口径让位给第一路（entity_id 定序，与遍历序无关）
    rows["climate.kong_tiao"] = _E(device_id="dev_ac")
    assert sorted(_integ_pick(rows)) == ["climate.kong_tiao", "light.shuang_lu_deng_a"]


def test_bulk_auxiliary_check_reads_enum_value():
    """HA 的 entity_category 是枚举不是裸串：判据必须取 `.value`。

    夹具刻意让这台指示灯**单独成组**（别的域没有它），折叠判据碰不到它 ⇒
    只有 config 档能挡掉它。枚举用非 str 的 `Enum`，取不到 `.value` 就漏。
    """
    from enum import Enum

    class _Cat(Enum):
        CONFIG = "config"

    rows = {"light.dan_du_zhi_shi_deng": _E(device_id="dev_a", category=_Cat.CONFIG),
            "cover.kai_chuang_qi": _E(device_id="dev_b")}
    assert _integ_pick(rows) == ["cover.kai_chuang_qi"]
    assert len(_integ_pick(rows, requested_name="指示灯")) == 2


def test_bulk_domain_priority_covers_core_whitelist():
    """两包表的字面量漂移守卫：集成侧的域优先级表必须**正好覆盖** core 的批量白名单。

    core 加一档（比如 `humidifier` 之外再放一档）而集成侧忘了跟上，那一档就落到
    表尾垫底——单实体设备仍会留，混装设备会被同台的另一个域顶掉，且**测试全绿**。
    两包不能互相 import（集成 const.py 引 awesomeversion），所以照仓库惯例用 AST 钉。
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "custom_components" / "huijian_ai" / "intent_helper.py"
           ).read_text(encoding="utf-8")
    got = None
    for node in ast.parse(src).body:
        tgts = node.targets if isinstance(node, ast.Assign) else \
            ([node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == "_BULK_DOMAIN_PRIORITY" for t in tgts):
            got = ast.literal_eval(node.value)
    assert got, "_BULK_DOMAIN_PRIORITY 找不到（改名即断）"
    assert set(got) == set(capability.BULK_TOGGLEABLE_DOMAINS), (
        "core 的白名单与集成侧优先级表覆盖面已分叉")
    assert got[-1] == "switch", got            # 插座类垫后：家电本体优先于一路开关



def test_own_platform_literal_agrees_across_packages():
    """三处字面量必须同值：core 的注释表、集成的复刻、集成真正的 const.DOMAIN。
    core 与集成不能互相 import（集成 const.py 还引 awesomeversion），所以靠钉守。"""
    import pathlib
    import re as _re
    root = pathlib.Path(__file__).resolve().parents[1]
    integ = (root / "custom_components" / "huijian_ai" / "intent_helper.py"
             ).read_text(encoding="utf-8")
    const = (root / "custom_components" / "huijian_ai" / "const.py"
             ).read_text(encoding="utf-8")
    def one(src, name):
        m = _re.search(rf'^{name}\s*=\s*"([^"]+)"', src, _re.M)
        assert m, f"{name} 找不到（改名即断）"
        return m.group(1)

    assert one(integ, "OWN_INTEGRATION_DOMAIN") == one(const, "DOMAIN")
    assert capability.BULK_SELF_PLATFORM == one(const, "DOMAIN")


def test_client_parse_registry_wires_the_platform_map():
    """生产接缝必须自己带钉：`_entity_platform`/`_entity_device` 由谁填。

    上一版这条表只由测试替身自己拼出来 ⇒ 把 `_parse_platforms` 改成恒空表，
    播报那条注的钉照样绿（变异 M16 实测漏网）。这里直调真函数 + 真 `_apply_registries`，
    顺带钉三件事：停用/隐藏实体不进表（与 `_parse_registry` 同判据）、
    `_parse_registry` 的三元组契约不许被我这次改动碰坏、两张新表走同一条接缝。
    """
    from core.ha_client import HAClient

    rows = [
        {"entity_id": "switch.huijian_1f04_mai_ke_feng_kai_guan",
         "platform": "huijian_ai", "device_id": "dev_sat"},
        {"entity_id": "media_player.huijian_1f04_mei_ti_bo_fang_qi",
         "platform": "huijian_ai", "device_id": "dev_sat"},
        {"entity_id": "cover.kai_chuang_qi", "platform": "kai_chuang_qi",
         "device_id": "dev_win"},
        {"entity_id": "light.hidden_one", "platform": "huijian_ai",
         "hidden_by": "user", "device_id": "dev_x"},
        {"entity_id": "light.off_one", "platform": "huijian_ai",
         "disabled_by": "user", "device_id": "dev_x"},
        {"entity_id": "light.no_platform"},
        {"entity_id": "light.no_device", "platform": "hue"},
    ]
    assert HAClient._parse_platforms(rows) == {
        "switch.huijian_1f04_mai_ke_feng_kai_guan": "huijian_ai",
        "media_player.huijian_1f04_mei_ti_bo_fang_qi": "huijian_ai",
        "cover.kai_chuang_qi": "kai_chuang_qi",
        "light.no_device": "hue"}
    assert HAClient._parse_devices(rows) == {
        "switch.huijian_1f04_mai_ke_feng_kai_guan": "dev_sat",
        "media_player.huijian_1f04_mei_ti_bo_fang_qi": "dev_sat",
        "cover.kai_chuang_qi": "dev_win"}     # 无 device_id 不进表；停用/隐藏同判据
    ent_map, alias, dc = HAClient._parse_registry(rows, {})     # 仍是三元组
    assert (ent_map, alias, dc) == ({}, {}, {})
    cli = HAClient.__new__(HAClient)
    HAClient._apply_registries(cli, {}, {}, None, None,
                               HAClient._parse_platforms(rows),
                               HAClient._parse_devices(rows))
    assert cli._entity_platform.get("switch.huijian_1f04_mai_ke_feng_kai_guan") == "huijian_ai"
    assert cli._entity_device.get("cover.kai_chuang_qi") == "dev_win"
    assert cli._entity_device.get("light.no_device") is None


def test_bulk_note_names_the_satellite_it_kept_off():
    """core 播报侧要如实说出"卫星自己没动"——集成挡掉了，注却说它动了就是假账。"""
    st = snapshot()
    st["switch.huijian_1f04_mai_ke_feng_kai_guan"] = _ent(
        "switch.huijian_1f04_mai_ke_feng_kai_guan", "on", "HUIJIAN-1F04 麦克风开关")
    amap = entity_area(st)
    amap["switch.huijian_1f04_mai_ke_feng_kai_guan"] = "展厅"
    args = {"target": [{"area": "展厅",
                        "devices": [{"name": "", "domains": list(
                            capability.BULK_TOGGLEABLE_DOMAINS)}]}]}
    ctl = [{"name": "悬窗 开窗器", "area": "展厅", "success": True}]
    ok, reply = _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                               utterance="关闭展厅所有设备"),
                     {"TurnDeviceOff": {"success": True, "control_targets": ctl}},
                     areas=amap)
    assert ok is True, reply
    assert "1 台是语音卫星自身" in reply, reply
    # 它同时不该被算进"不支持开关"那档（switch 域是有开关动作的）：
    # 展厅那 3 条才是真不支持（悬窗 状态 sensor / ③ 关闭 button / 力度 number）
    assert "另有 3 个不支持开关、1 台是语音卫星自身" in reply, reply


def test_bulk_reply_counts_and_never_claims_missing():
    """批量成功：按台数如实报（不逐台念名），且不得出现「我没找到」。"""
    st = snapshot()
    args = {"target": [{"area": "展厅",
                        "devices": [{"name": "", "domains": list(
                            capability.BULK_TOGGLEABLE_DOMAINS)}]}]}
    ctl = [{"name": fn, "area": "展厅", "success": True}
           for fn in ("悬窗 开窗器", "推拉窗 开窗器", "内开窗 开窗器", "展厅")]
    plan = Plan(intent="TurnDeviceOff", args=args, source="t0",
                utterance="关闭展厅所有设备").mark(FLAG_AREA_BULK)
    ok, reply = _run(st, plan, {"TurnDeviceOff": {"success": True,
                                                  "control_targets": ctl}})
    assert ok is True, reply
    assert "没找到" not in reply, reply
    assert "展厅 4 台都关了" in reply, reply


def test_bulk_all_devices_says_what_was_left_untouched():
    """「所有设备」必须如实交代没碰的台——悄悄跳过门锁不报＝让用户以为门也动了。"""
    st = snapshot()
    args = {"target": [{"area": "展厅",
                        "devices": [{"name": "", "domains": list(
                            capability.BULK_TOGGLEABLE_DOMAINS)}]}]}
    ctl = [{"name": fn, "area": "展厅", "success": True}
           for fn in ("悬窗 开窗器", "推拉窗 开窗器", "内开窗 开窗器")]
    ok, reply = _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                               utterance="关闭展厅所有设备"),
                      {"TurnDeviceOff": {"success": True, "control_targets": ctl}})
    assert ok is True, reply
    # 展厅的 悬窗 状态(sensor)/③ 关闭(button)/力度(number) 是设备上的零件，
    # 没有开关动作——报数不报机器名，并且单位必须是「个」不是「台」
    assert "另有 3 个不支持开关" in reply, reply


def test_bulk_note_reports_function_slots_as_parts_not_devices():
    """折叠之后播报的单位口径必须换成"设备"：功能位用「个」，别混进「台」。

    办公室真机地形（.91：一台空调名下 climate + 7 条功能位 switch + 指示灯 + 传感器）
    ⇒ 集成侧折叠后回执那句"2 台"是**设备**，而注册表里这一摊有 9 条可开关实体。
    注若继续按实体数「台」，整句会念成"2 台都关了，另有 16 台没动"——客户屋里
    只有 2 台设备，等于凭空给他变出 14 台，而这正是他用来判断"有没有漏关"的数。
    """
    st = {"climate.x_ac": _ent("climate.x_ac", "on", "办公室空调"),
          "switch.x_ac_eco": _ent("switch.x_ac_eco", "on", "办公室空调 ECO"),
          "switch.x_ac_bai_feng": _ent("switch.x_ac_bai_feng", "on", "办公室空调 上下摆风"),
          "light.x_ac_led": _ent("light.x_ac_led", "on", "办公室空调 指示灯"),
          "sensor.x_ac_temp": _ent("sensor.x_ac_temp", "26.5", "办公室空调 温度"),
          "cover.kai_chuang_qi": _ent("cover.kai_chuang_qi", "closed", "悬窗 开窗器"),
          # 语音卫星两台实体同属一台设备 ⇒ 卫星那一档也必须按"台"数（.91 实况：
          # 一台卫星挂着麦克风开关/播放器/连续对话/回声 4 条可开关实体）
          "switch.huijian_1f04_mai_ke_feng_kai_guan": _ent(
              "switch.huijian_1f04_mai_ke_feng_kai_guan", "on", "HUIJIAN-1F04 麦克风开关"),
          "media_player.huijian_1f04_mei_ti_bo_fang_qi": _ent(
              "media_player.huijian_1f04_mei_ti_bo_fang_qi", "idle", "HUIJIAN-1F04 媒体播放器"),
          # 防误动那一档：一台门禁设备上挂两条不同域的可开关实体（锁 + 报警面板）
          "lock.da_men": _ent("lock.da_men", "unlocked", "大门锁"),
          "alarm_control_panel.aomen": _ent("alarm_control_panel.aomen", "disarmed", "安防面板")}
    amap = {k: "办公室" for k in st}
    devices = {"climate.x_ac": "dev_ac", "switch.x_ac_eco": "dev_ac",
               "switch.x_ac_bai_feng": "dev_ac", "light.x_ac_led": "dev_ac",
               "sensor.x_ac_temp": "dev_ac", "cover.kai_chuang_qi": "dev_win",
               "switch.huijian_1f04_mai_ke_feng_kai_guan": "dev_sat",
               "media_player.huijian_1f04_mei_ti_bo_fang_qi": "dev_sat",
               "lock.da_men": "dev_sec", "alarm_control_panel.aomen": "dev_sec"}
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "", "domains": list(
                            capability.BULK_TOGGLEABLE_DOMAINS)}]}]}
    ctl = {"TurnDeviceOff": {"success": True, "control_targets": [
        {"name": "办公室空调", "area": "办公室", "success": True},
        {"name": "悬窗 开窗器", "area": "办公室", "success": True}]}}

    def go(with_devices):
        plan = Plan(intent="TurnDeviceOff", args=args, source="t0",
                    utterance="关闭办公室所有设备")
        return _run(st, plan, ctl, areas=amap,
                    devices=devices if with_devices else None)[1]

    got = go(True)
    assert "3 个功能位" in got, got        # 同台的 ECO/摆风/指示灯（本体之外 3 条）
    assert "1 个不支持开关" in got, got    # 温度传感器
    assert "1 台（报警面板、门锁这类）为防误动" in got, got  # 一台门禁两条实体＝一台
    assert "1 台是语音卫星自身" in got, got                   # 同理，卫星是一台不是两台
    assert "台功能位" not in got and "台不支持开关" not in got, got
    # 反向臂：device 表拉不到（老 HA/注册表失败）⇒ 每条自成一摊，
    # 功能位那一档一个数都编不出来，宁可不提也绝不"另多报 3 台"
    assert "功能位" not in go(False), go(False)


def test_bulk_named_class_does_not_mention_other_classes():
    """反向钉：点名「所有灯」时把别的类别数进来是噪音——只有"所有设备"口径才注。"""
    st = snapshot()
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "", "domains": ["light"]}]}]}
    ok, reply = _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                               utterance="关闭办公室所有灯"),
                      {"TurnDeviceOff": {"success": True,
                                         "control_targets": [
                                             {"name": "射灯", "area": "办公室",
                                              "success": True}]}})
    assert ok is True, reply
    assert "没动" not in reply, reply


def test_bulk_noop_never_reads_out_an_entity_id():
    """批量形全空操作：旧写法退回 `cands[0].entity_id` 当标签，把机器名念进 TTS
    （线上地形实锤：办公室两台灯都 off ⇒「light.ban_gong_shi_she_deng 本来就在…」）。
    无名时按「区域 + N 台」给数量真值。"""
    st = {"light.a": _ent("light.a", "off", "射灯"),
          "light.b": _ent("light.b", "off", "筒灯")}
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "", "domains": ["light"]}]}]}
    ok, reply = _run(st, Plan(intent="TurnDeviceOff", args=args, source="t0",
                               utterance="关闭办公室所有灯"),
                     {"TurnDeviceOff": {"success": True,
                                        "control_targets": [
                                            {"name": "射灯", "area": "办公室",
                                             "success": True},
                                            {"name": "筒灯", "area": "办公室",
                                             "success": True}]}},
                     areas={"light.a": "办公室", "light.b": "办公室"})
    assert ok is True, reply
    assert "本来就在要求的状态上" in reply, reply    # 空操作这条真信号不能丢
    for noise in ("light.", "_", "deng"):
        assert noise not in reply, (noise, reply)
    assert "2 台" in reply, reply
