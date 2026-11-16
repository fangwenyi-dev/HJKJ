"""v1.9.9 设备设置抽屉（用户令）。

用户 2026-11-16 原话：「这个界面中的速度、力度和模式选择在手机上经常容易误
操作，是否可以在"改名"按钮左侧新增一个"设置"按钮或双击名称，有个对应设备的
弹出框中来设置"速度、力度和模式选择"三个参数？这样以后不容易误操作。」

本批要守住四件事：
① **卡片降噪**：速度/力度滑块与模式键必须真的离开卡片（否则"不容易误操作"
   只是嘴上说说），但"当前档位"的只读回显要留着——三家大厂卡片都留；
② **抽屉承接**：三条参数控件全部在抽屉里，且命令口径与卡片时代逐字一致
   （set_speed / set_strength / wind_lock_*，松手才发）；
③ **两条入口**：设置按钮（可发现）＋ 双击名称（加速器），缺一不算做完；
④ **破坏性动作隔离**：移除按钮离开卡片、进入抽屉，与改名之间隔开一层。

另有一条**浏览器规则**必须钉住：抽屉是 body 级单例，不能塞进 `#dev-*`。
`.device-item:hover` 带 transform，会给子元素造新的 position:fixed 包含块，
抽屉一旦嵌在卡片里就会被锁进那一小块并跑到视口外——这是肉眼难查、
改了 transform 才复发的坑，故用断言留证。
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")
INDEX = (ROOT / "www" / "index.html").read_text(encoding="utf-8")


def _js_body(name):
    """取函数体（与 test_audit_2026_10_05_round3_fixes 同款：重名即失败）。"""
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
    i = hits[0]
    j = JS.index("{", i)
    depth = 0
    for k in range(j, len(JS)):
        if JS[k] == "{":
            depth += 1
        elif JS[k] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:k + 1]
    raise AssertionError("函数 %s 花括号不配平" % name)


class TestCardSlimmedDown:
    """① 卡片只留高频主控＋状态，参数控件与破坏性动作搬走。"""

    def test_card_has_no_speed_or_strength_slider(self):
        """卡片里不许再有速度/力度滑块——两个"误操作"正主。"""
        body = _js_body("renderDevice")
        assert "speed-slider" not in body, "速度滑块还在卡片里（用户诉求未落地）"
        assert "strength-slider" not in body, "力度滑块还在卡片里（用户诉求未落地）"
        # 位置是主控件，必须留下：同米家窗帘卡片（连续量、天天用）
        assert "position-slider" in body, "位置滑块被顺手删了——它是卡片唯一主控件"

    def test_card_has_no_mode_buttons(self):
        """模式两键也搬进抽屉：它们与内倒键同色系、同尺寸，挤在一排最易误触。"""
        body = _js_body("renderDevice")
        assert "wind_lock_tilt" not in body, "内倒模式键还在卡片里"
        assert "wind_lock_flat" not in body, "平开模式键还在卡片里"

    def test_remove_button_left_the_card(self):
        """移除是破坏性操作（整台子设备实体全灭），不该待在卡片右上角。"""
        body = _js_body("renderDevice")
        assert not re.search(r"\\'remove\\'", body), "移除按钮还在卡片里——误触即删设备"
        # 改名留在卡片？不，它也进了抽屉；但绝不能顺带删掉 renameDevice 本身
        assert re.search(r"\bfunction renameDevice\s*\(", JS), "renameDevice 本体被删"

    def test_card_keeps_readonly_summary(self):
        """只读回显必须留：用户要能一眼看到当前档位，且滑动时改不掉它。"""
        body = _js_body("renderDevice")
        assert "device-hint" in body, "卡片丢了速度/力度只读摘要行"
        assert "paramTextOf(" in body, "摘要行没走统一取值出口（会与抽屉文案打架）"
        # 摘要行必须是**无副作用的按钮**（点它只开抽屉，不改值）
        hint = body[body.index("device-hint"):]
        assert "controlDevice" not in hint.split("</button>")[0], \
            "摘要行里挂了控制命令——只读回显变成了第二个误触源"

    def test_settings_button_sits_left_of_rename(self):
        """用户原话：「在"改名"按钮左侧新增一个"设置"按钮」。"""
        body = _js_body("renderDevice")
        i_set = body.index("openDeviceSettings(")
        i_ren = body.index("renameDevice(")
        assert i_set < i_ren, "「设置」不在「改名」左侧——与用户指定位置不符"

    def test_double_click_is_an_accelerator_not_the_only_path(self):
        """双击名称只是加速器：入口按钮必须同时存在（双击不可发现）。"""
        body = _js_body("renderDevice")
        assert "ondblclick=" in body and "openDeviceSettings(" in body, "双击加速器丢失"
        assert 'class="btn btn-slate btn-sm" title="设置：速度 / 力度 / 模式 / 移除"' in body, \
            "可发现的「设置」按钮丢了——只剩双击则新用户找不到参数"


class TestSheetCarriesEverything:
    """② 三条参数控件全部在抽屉里，命令口径不变。"""

    def test_sheet_renders_all_three_controls(self):
        body = _js_body("renderDeviceSettings")
        assert "speed-slider" in body and "set_speed" in body, "抽屉缺速度滑块"
        assert "strength-slider" in body and "set_strength" in body, "抽屉缺力度滑块"
        assert "wind_lock_tilt" in body and "wind_lock_flat" in body, "抽屉缺两档模式键"

    def test_sliders_commit_on_release_only(self):
        """松手才发（onchange），拖动中只打凭据——v1.7.18 的原缺陷不得回潮。"""
        body = _js_body("renderDeviceSettings")
        for cls in ("speed-slider", "strength-slider"):
            seg = body[body.index(cls):]
            seg = seg[:seg.index("</div>")]
            assert "markUserInput(this);" in seg, f"{cls} 缺交互凭据"
            assert "onchange=" in seg, f"{cls} 不是松手提交"
            assert "oninput=" in seg and "controlDevice" not in seg.split("onchange=")[0], \
                f"{cls} 在 oninput 里就发命令（拖动即发，正是误触根因）"

    def test_mode_highlight_has_a_declared_source(self):
        """v1.9.9 用户改令：「只做默认为内开内倒模式，所以默认是内开内倒模式
        高亮，后面按下其他的按钮后，对应的模式为高亮」。

        旧口径（本测试曾钉的"动词式、无 aria-*"）已被用户推翻，但推翻的是**结论**
        不是**理由**：面板确实读不到 wind_lock_mode，所以高亮必须有明确来源、
        且来源要说实话。这条守卫因此改钉"来源链"，比原来更难绕：
        ① 取值只走 windLockModeOf（不许渲染层自己猜）；
        ② 取值顺序是 实体真值 → 本机记忆 → 默认，且默认落在内倒；
        ③ 记忆只在下发**成功之后**写入（失败不许点亮）；
        ④ 文案必须标明"默认"还是"已记录"，不许把默认伪装成实测。"""
        body = _js_body("renderDeviceSettings")
        assert "windLockModeOf(devId)" in body, "高亮不取自唯一取值函数（渲染层在自己猜状态）"
        # 选中态互斥：同一时刻最多一个 is-active
        assert re.search(r"tiltOn \? ' is-active' : ''", body), "内倒选中态不是条件渲染"
        assert re.search(r"flatOn \? ' is-active' : ''", body), "平开选中态不是条件渲染"
        assert re.search(r"const effectiveMode = mode \|\| 'tilt'", body), \
            "无记录时未落默认内倒——会渲染成两颗都不亮（第三种状态，用户读不出当前模式）"
        assert re.search(r"const tiltOn = effectiveMode === 'tilt'", body), "未按有效模式判内倒"
        assert re.search(r"const flatOn = effectiveMode === 'flat'", body), "未按有效模式判平开"
        # 语义交给读屏，别只靠颜色
        assert "aria-pressed=" in body, "选中态只有颜色、没有 aria-pressed"

        picker = _js_body("windLockModeOf")
        i_real = picker.index("attributes.wind_lock_mode")
        i_memo = picker.index("WIND_LOCK_MEMO[devId]")
        assert i_real < i_memo, "本机记忆抢先于实体真值——将来集成补上真值也不会被采用"
        assert "return 'tilt';" in picker and "return 'flat';" in picker, "真值 0/1 未映射"
        assert re.search(r"return WIND_LOCK_MEMO\[devId\] \|\| null;", picker), \
            "无记录时必须返回 null（由调用方按默认处理），不能在这里偷填 'tilt'"

        # 成败判据：press 成功之后才记账
        ctl = _js_body("controlDevice")
        seg = ctl[ctl.index("wind_lock_tilt' || command === 'wind_lock_flat'"):]
        seg = seg[:seg.index("set_speed")] if "set_speed" in seg else seg
        i_ok = seg.index("if (!r.ok) throw")
        i_memo = seg.index("WIND_LOCK_MEMO[deviceId] =")
        assert i_ok < i_memo, "记账写在成功判定之前——下发失败也会点亮高亮"

    def test_mode_default_text_is_honest(self):
        """默认值不许伪装成实测值：文案要区分「默认」与「已记录」。"""
        body = _js_body("renderDeviceSettings")
        assert "默认内倒" in body, "未标注默认口径（用户会把它当成读回来的真值）"
        assert "已记录的最近一次下发" in body, "按过之后没有把来源切换成「已记录」"

    def test_mode_section_gated_on_5005(self):
        """非 5005 机型没有内倒/平开按键，抽屉不得给出必然失败的按钮。"""
        body = _js_body("renderDeviceSettings")
        assert "supportsWindLock" in body, "抽屉未按机型能力分档"
        assert re.search(r"startsWith\('5005'\)", body), "机型判据与卡片不一致"

    def test_sheet_reuses_card_commands_verbatim(self):
        """抽屉里的命令必须走同一个 controlDevice 调度器——另写一份必然漂移。"""
        body = _js_body("renderDeviceSettings")
        assert "controlDevice(" in body, "抽屉绕开了统一调度器"
        assert "haApi(" not in body, "抽屉自己拼 API 调用（应复用 controlDevice）"

    def test_remove_lives_in_the_sheet_with_confirmation(self):
        """移除搬进抽屉，且它原有的 confirm 双保险不得因搬家而丢失。"""
        body = _js_body("renderDeviceSettings")
        assert re.search(r"\\'remove\\'", body), "移除按钮没进抽屉"
        ctl = _js_body("controlDevice")
        assert "confirm(" in ctl, "controlDevice 的删除确认丢了——抽屉里点一下就删设备"


class TestDrawerIsBodyLevelSingleton:
    """浏览器规则：transform 造包含块，抽屉嵌在卡片里会被锁死。"""

    def test_drawer_appends_to_body(self):
        body = _js_body("ensureDeviceDrawer")
        assert "document.body.appendChild(root)" in body, \
            "抽屉没挂到 body——卡片的 transform 会把 position:fixed 锁进卡片"

    def test_drawer_is_a_singleton(self):
        """反复打开不叠加遮罩：先按 id 取，取到即复用。"""
        body = _js_body("ensureDeviceDrawer")
        assert "getElementById('devSettingsRoot')" in body, "没按 id 复用抽屉"
        assert "if (existing) return existing;" in body, "缺复用短路（会叠出多层遮罩）"

    def test_pointer_dismissal_is_click_only(self):
        """全仓约定（v1.7.22）：禁止劫持 touch/pointer 事件。"""
        assert "touchstart" not in JS and "pointerdown" not in JS, \
            "抽屉引入 touch/pointer 事件劫持——破 v1.7.22 约定"
        assert 'class="sheet-mask" onclick="closeDeviceSettings()"' in JS, \
            "遮罩未绑 onclick 关闭"

    def test_scroll_locked_while_open_and_released_on_close(self):
        body_open = _js_body("openDeviceSettings")
        body_close = _js_body("closeDeviceSettings")
        assert "sheet-locked" in body_open, "打开未锁底滚动（手指会滑出面板）"
        assert "sheet-locked" in body_close, "关闭未解锁——页面会永久不能滚"
        assert "remove('sheet-locked')" in body_close, "解锁写的是 add 不是 remove"

    def test_escape_key_closes(self):
        """桌面端惯例。keydown 绑在根节点上，只处理焦点在面板内的情况。"""
        body = _js_body("ensureDeviceDrawer")
        assert "event.key==='Escape'" in body, "缺 Esc 关闭"

    def test_both_close_paths_present(self):
        """右上角 ✕ 与点遮罩两条路径都要有——只有一个时用户会以为关不掉。"""
        body = _js_body("renderDeviceSettings")
        assert "sheet-close" in body and "closeDeviceSettings()" in body, "缺右上角关闭键"


class TestSharedExitDiscipline:
    """取值出口统一（v1.9.8 batteryTextOf 同款纪律）。"""

    def test_param_text_of_handles_dirty_states(self):
        """unknown/unavailable 一律回 '--'，绝不把英文状态摆给用户。"""
        body = _js_body("paramTextOf")
        assert "'unknown'" in body and "'unavailable'" in body, "没挡脏状态"
        assert "'--'" in body, "读不到时没回落占位"

    def test_clamp_slider_value_guards_null_and_empty(self):
        """Number(null)===0、Number('')===0——不挡就会把"没数据"画成量程起点。"""
        body = _js_body("clampSliderValue")
        assert "!numberEntity.state" in body, "空串/null state 未挡（会被算成 0）"
        assert "Number.isFinite" in body, "NaN/Infinity 未挡"

    def test_no_raw_voltage_regression(self):
        """v1.9.8 的电量百分比口径不得被本批顺手改回伏数。"""
        assert "batteryTextOf(" in _js_body("renderDevice"), "renderDevice 丢了电量统一出口"


class TestCssForNewHooks:
    """新 class 必须有样式归属（裸元素浮在深空上是重设计最常见的翻车）。"""

    def _rule(self, sel):
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
        assert m, f"缺少规则 {sel}"
        return m.group(1)

    def test_device_hint_styles(self):
        rule = self._rule(".device-hint")
        assert "cursor: pointer" in rule, "摘要行不可点，但没给可点感"
        assert "margin-top: auto" in rule, "摘要行没压到卡底（控件搬走后卡片下半截会空着）"

    def test_sheet_root_hidden_by_default(self):
        rule = self._rule(".sheet-root")
        assert "display: none" in rule, "抽屉默认可见——首屏会糊一层遮罩"
        assert "position: fixed" in rule, "抽屉不是定层"
        assert "z-index: 9000" in rule, "抽屉层级不对"

    def test_toast_stays_above_the_sheet(self):
        """.toast 的 z-index 是 9999——命令回执必须压在抽屉之上才看得见。"""
        toast = re.search(r"\.toast \{[^}]*\}", CSS).group(0)
        z = int(re.search(r"z-index: (\d+)", toast).group(1))
        sheet = int(re.search(r"z-index: (\d+)", self._rule(".sheet-root")).group(1))
        assert z > sheet, "toast 被抽屉盖住了：命令回执用户看不到"

    def test_sheet_panel_scrolls_instead_of_overflowing(self):
        rule = self._rule(".sheet-panel")
        assert "max-height" in rule and "overflow-y: auto" in rule, \
            "小屏上内容会溢出面版且无法滚动"
        assert "overscroll-behavior: contain" in rule, "滚动链会传给底层页面"

    def test_mobile_is_bottom_sheet(self):
        """手机＝底部抽屉：拇指够得到，且不遮住上半屏信息。"""
        # 按"含 .sheet-panel 的那个 640 块"定位，而不是取文件里最后一个 640 块
        # ——后者会被任何后续追加的 640 规则悄悄指错对象。
        blocks = [m for m in re.finditer(r"@media \(max-width: 640px\) \{", CSS)]
        target = None
        for m in blocks:
            seg = CSS[m.start():]
            depth, end = 0, len(seg)
            for k, ch in enumerate(seg):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = k
                        break
            body = seg[:end]
            if ".sheet-panel" in body:
                target = body
        assert target is not None, "缺手机端 .sheet-panel 规则"
        assert "bottom: 0" in target, "手机端抽屉没贴底"
        assert "border-radius: 18px 18px 0 0" in target, "手机端抽屉不是上圆下方"

    def test_reduced_motion_untouched(self):
        """本批不得往 reduce 块里塞动画（无障碍口径只在放行动画时才豁免）。"""
        seg = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        seg = seg[:seg.index("\n}\n") + 3]
        assert "sheet" not in seg, "抽屉动画写进了 reduce 块"


class TestCardStateColor:
    """v1.9.9 第二段（用户令「再次优化开窗器子设备的 UI 设计」）。

    状态色的价值是**减少阅读**，代价是"颜色说谎"的风险：一旦它与状态点各判
    一套，就会出现绿点配红边这类自相矛盾。故钉两条：判据同源、未知态不着色。
    """

    def test_card_state_is_derived_from_the_same_truth_as_the_dot(self):
        body = _js_body("loadDeviceState")
        #   从 cardEl 起切：data-devstate 那行是块的**末行**（写完计算才落属性），
        #   以它为起点会把整段判据切掉——切片基准错，断言就成了空转。
        seg = body[body.index("const cardEl = document.getElementById"):]
        seg = seg[:seg.index("data-devstate', cardState);")]
        # 离线优先：dot-offline 已在前面按"无新鲜上报"判过，这里必须复用它，
        # 而不是再判一次 cover.state——两套判据迟早分叉。
        assert "dot-offline" in seg, "状态色没有沿用状态点的离线判据（会出现绿点红边）"
        assert "dot-unknown" not in seg.replace("dot-offline", ""), \
            "状态色不该再判一次 dot-unknown——离线/未知的分档只归状态点一处"
        i_offline = seg.index("dot-offline")
        i_moving = seg.index("'moving'")
        assert i_offline < i_moving, "离线判定必须优先于动作中，否则断线时会卡在琥珀呼吸"

    def test_unknown_state_gets_no_color(self):
        """未知态不着色：首屏"还没读到"不能长得像"已关闭"（G-4 同族）。"""
        assert not re.search(r'data-devstate="unknown"\]::before', CSS), \
            "未知态被着色——与「已关闭」撞脸"
        body = _js_body("loadDeviceState")
        assert "let cardState = 'unknown';" in body, "默认态不是 unknown"

    def test_no_layout_pins_regressed(self):
        """状态色不得动用户令里的布局原值（v1.6.29 / v1.7.9）。"""
        dev = re.search(r"\.device-item \{([^}]*)\}", CSS).group(1)
        assert "min-height: 262px" in dev, "统一高度锚被改了"
        assert "max-width" not in dev, "又加回了 max-width 封顶"
        assert "height" not in dev.replace("min-height", ""), "动了固定高度"
        grid = re.search(r"\.device-list \{([^}]*)\}", CSS).group(1)
        assert "align-items: start" not in grid, "grid 对齐回潮"
        assert "minmax(280px, 340px)" in grid and "auto-fill" in grid, "轨道封顶丢失"

    def test_breathing_stops_under_reduced_motion(self):
        """全局 * 把 duration 压到 .01ms 不等于"不动"——无限循环会变高频闪烁，
        比原来的呼吸更晃眼。必须显式 animation: none。"""
        seg = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        assert re.search(r'data-devstate="moving"\]::before \{[^}]*animation: none', seg), \
            "减弱动画下呼吸没被真正停住（会变成闪烁）"

    def test_edge_bar_does_not_break_the_glass_card(self):
        """边条用 ::before 绝对定位，不占文档流——否则卡片会被推高一像素级。"""
        bar = re.search(r"\.device-item::before \{([^}]*)\}", CSS).group(1)
        assert "position: absolute" in bar, "边条进了文档流（会挤动卡片内容）"
        assert "width: 3px" in bar, "边条宽度不是 3px"


# ══════════════ node 真跑：抽屉内容渲染（不靠读源码猜结构） ══════════════
# 静态断言只能证明"代码里有这几个词"，证明不了"渲染出来的 HTML 是对的"。
# 这里有两条真实存量缺陷都恰好在静态断言下全绿：
#  ① 初值取错（speedMin + state - speedMin 这类算式写歪）不违反任何字符串断言；
#  ② unknown 状态被原样拼进面板，字符串里确实"有 paramTextOf 调用"。
# 故用 node 把仓库真代码切出来跑一遍（helper 一律用真实现，桩不放宽）。
_JS_PRELUDE = r"""
function escapeHtml(t) {
    if (t === null || t === undefined) return '';
    return String(t).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
}
function jsAttr(v) {
    return escapeHtml(String(v === null || v === undefined ? '' : v)
        .replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}
function deviceSnOf(dev) { return (dev && dev.sn) || null; }
function findEntityState(dev, domain, suffix) {
    var m = ((dev && dev.ents) || []).filter(function (e) {
        return e.domain === domain && e.suffix === suffix;
    });
    return m.length ? m[0] : null;
}
function markUserInput() {}
function controlDevice() { controlDevice.calls = (controlDevice.calls || 0) + 1; }
function closeDeviceSettings() {}
function renameDeviceFromSheet() {}

var DEVICE_CTX = {};
var WIND_LOCK_MEMO = {};
var _drawerDevId = null;
var _panel = { innerHTML: '' };
var document = {
    getElementById: function (id) {
        return id === 'devSettingsPanel' ? _panel : null;
    }
};
var bad = 0;
function want(cond, msg) {
    if (!cond) { console.log('FAIL ' + msg); bad++; }
}
"""

_JS_CHECKS = r"""
// ── 场景 1：5005 机型，速度/力度都有上报值 ──
DEVICE_CTX['d1'] = {
    entryId: 'e1', name: '阳台开窗器', states: [],
    dev: {
        id: 'd1', sn: '50051234',
        ents: [
            { domain: 'number', suffix: 'speed', state: '45',
              attributes: { min: 0, max: 100, unit_of_measurement: '%' } },
            { domain: 'number', suffix: 'strength', state: '70',
              attributes: { min: 0, max: 100, unit_of_measurement: '%' } }
        ]
    }
};
_drawerDevId = 'd1';
renderDeviceSettings();
var h = _panel.innerHTML;

want(h.indexOf('阳台开窗器') >= 0, '标题缺设备名');
want(h.indexOf('50051234') >= 0, '标题缺 SN');
want(h.indexOf('class="speed-slider"') >= 0, '缺速度滑块');
want(h.indexOf('class="strength-slider"') >= 0, '缺力度滑块');
want(h.indexOf('value="45"') >= 0, '速度初值未取实体值');
want(h.indexOf('value="70"') >= 0, '力度初值未取实体值');
want(h.indexOf('45%') >= 0, '速度右侧未回显带单位的当前值');
want(h.indexOf('70%') >= 0, '力度右侧未回显带单位的当前值');
want(h.indexOf('wind_lock_tilt') >= 0, '5005 缺内倒模式键');
want(h.indexOf('wind_lock_flat') >= 0, '5005 缺平开模式键');
want(h.indexOf('set_speed') >= 0, '缺 set_speed 接线');
want(h.indexOf('set_strength') >= 0, '缺 set_strength 接线');
// 默认内倒高亮（用户定案）：没有记忆时内倒 is-active + aria-pressed=true，平开反之。
var iTilt = h.indexOf('btn-mode-a');
var iFlat = h.indexOf('btn-mode-b');
want(iTilt >= 0 && iFlat >= 0, '缺模式按钮');
var tiltSeg = h.slice(iTilt, iFlat);
var flatSeg = h.slice(iFlat, h.indexOf('</button>', iFlat) + 9);
want(tiltSeg.indexOf('is-active') >= 0, '默认未高亮内倒（用户定案：默认内开内倒）');
want(tiltSeg.indexOf('aria-pressed="true"') >= 0, '内倒选中态没给读屏');
want(flatSeg.indexOf('is-active') < 0, '平开被同时高亮——两颗都亮等于没高亮');
want(flatSeg.indexOf('aria-pressed="false"') >= 0, '平开未选中时 aria-pressed 不是 false');
want(h.indexOf("'remove'") >= 0, '缺移除按钮');

['speed-slider', 'strength-slider'].forEach(function (cls) {
    var i = h.indexOf('class="' + cls + '"');
    var seg = h.slice(i, h.indexOf('</div>', i));
    var head = seg.slice(0, seg.indexOf('onchange='));
    want(i >= 0, cls + ' 未渲染');
    want(seg.indexOf('onchange=') >= 0, cls + ' 不是松手提交');
    want(head.indexOf('controlDevice') < 0, cls + ' 在 oninput 里就发命令（拖动即发）');
    want(head.indexOf('markUserInput(this);') >= 0, cls + ' 缺交互凭据');
});

// ── 场景 2：非 5005 机型 ⇒ 无模式段，速度/力度仍在，且无实体时不禁用 ──
_panel.innerHTML = '';
DEVICE_CTX['d2'] = {
    entryId: 'e1', name: '书房开窗器', states: [],
    dev: { id: 'd2', sn: '40011111', ents: [] }
};
_drawerDevId = 'd2';
renderDeviceSettings();
h = _panel.innerHTML;
want(h.indexOf('wind_lock') < 0, '非 5005 机型不该出现模式键');
want(h.indexOf('speed-slider') >= 0, '非 5005 机型也必须能调速度');
want(h.indexOf('value="0"') >= 0, '无实体时应落量程起点');
want(h.indexOf('disabled') < 0, '无实体时滑块被禁用（HA 侧 number 实体恒存在）');
want(h.indexOf('--') >= 0, '无实体时右侧应显占位而非空白');

// ── 场景 3：脏状态 unknown ⇒ 占位，绝不把英文状态摆给用户 ──
_panel.innerHTML = '';
DEVICE_CTX['d3'] = {
    entryId: 'e1', name: 'x', states: [],
    dev: { id: 'd3', sn: '50059999', ents: [
        { domain: 'number', suffix: 'speed', state: 'unknown', attributes: {} }
    ] }
};
_drawerDevId = 'd3';
renderDeviceSettings();
h = _panel.innerHTML;
want(h.indexOf('unknown') < 0, '把 unknown 原样摆给用户了');
want(h.indexOf('--') >= 0, '脏状态应显占位');

// ── 场景 4：越界值必须钳回区间（否则浏览器静默夹端点，显示与真值脱节）──
want(clampSliderValue({ state: '150', attributes: {} }, 0, 100) === 100, '上越界未钳');
want(clampSliderValue({ state: '-5', attributes: {} }, 0, 100) === 0, '下越界未钳');
want(clampSliderValue({ state: '55', attributes: {} }, 0, 100) === 55, '区间内被误改');
want(clampSliderValue({ state: 'unknown', attributes: {} }, 0, 100) === 0, '脏状态应落起点');
want(clampSliderValue({ state: '', attributes: {} }, 0, 100) === 0, '空串应落起点（Number("")===0 的坑）');
want(clampSliderValue(null, 0, 100) === 0, 'null 应落起点');

// ── 场景 5：paramTextOf 的单位与占位 ──
want(paramTextOf({ state: '40', attributes: { unit_of_measurement: '%' } }) === '40%', '正常值应带单位');
want(paramTextOf({ state: '40', attributes: {} }) === '40', '无单位时不该拼出 undefined');
want(paramTextOf({ state: 'unavailable', attributes: {} }) === '--', '脏状态应占位');
want(paramTextOf({ state: 'unknown', attributes: {} }) === '--', 'unknown 应占位');
want(paramTextOf(null) === '--', 'null 应占位');

// ── 场景 6：按过平开之后，高亮必须跟着走（用户定案「按下其他的按钮后，
//    对应的模式为高亮」）。只验默认态不够——那恰好是"根本没切换"也能过的绿。
WIND_LOCK_MEMO['d3'] = 'flat';
_panel.innerHTML = '';
renderDeviceSettings();
h = _panel.innerHTML;
iTilt = h.indexOf('btn-mode-a');
iFlat = h.indexOf('btn-mode-b');
want(iTilt >= 0 && iFlat >= 0, '5005 缺模式按钮');
want(h.slice(iTilt, iFlat).indexOf('is-active') < 0, '按过平开后内倒仍高亮');
want(h.slice(iFlat, h.indexOf('</button>', iFlat) + 9).indexOf('is-active') >= 0, '按过平开后平开未高亮');
want(h.indexOf('已记录的最近一次下发') >= 0, '按过之后文案仍说默认（来源没说真话）');

// ── 场景 7：实体真值优先于本机记忆（将来集成补上真值即自动采用）──
DEVICE_CTX['d4'] = {
    entryId: 'e1', name: 'y', states: [],
    dev: { id: 'd4', sn: '50058888', ents: [
        { domain: 'cover', suffix: 'cover', state: 'open',
          attributes: { wind_lock_mode: 0 } }
    ] }
};
WIND_LOCK_MEMO['d4'] = 'flat';   // 本机记着平开，但实体说内倒
_drawerDevId = 'd4';
_panel.innerHTML = '';
renderDeviceSettings();
h = _panel.innerHTML;
iTilt = h.indexOf('btn-mode-a');
iFlat = h.indexOf('btn-mode-b');
want(h.slice(iTilt, iFlat).indexOf('is-active') >= 0, '真值 0=内倒 未覆盖本机记忆');
want(h.slice(iFlat, h.indexOf('</button>', iFlat) + 9).indexOf('is-active') < 0, '真值说内倒，平开仍亮');

// ── 场景 8：真值 -1（未知）不得被当成"平开"或"内倒"──
DEVICE_CTX['d4'].dev.ents[0].attributes.wind_lock_mode = -1;
WIND_LOCK_MEMO['d4'] = 'flat';
_panel.innerHTML = '';
renderDeviceSettings();
h = _panel.innerHTML;
iTilt = h.indexOf('btn-mode-a');
iFlat = h.indexOf('btn-mode-b');
want(h.slice(iTilt, iFlat).indexOf('is-active') < 0, '-1 未知被当成内倒');
want(h.slice(iFlat, h.indexOf('</button>', iFlat) + 9).indexOf('is-active') >= 0,
    '-1 未知时未回落到本机记忆（不该凭空发明状态）');

if (bad) {
    console.log('v1.9.9 抽屉真跑: ' + bad + ' 处不符');
    process.exit(1);
}
console.log('OK');
"""


def _build_node_script():
    """真代码只有一份：从 JS 抽函数体拼进 harness，测试里不重写实现。"""
    real = "\n".join(_js_body(n) for n in ("paramTextOf", "clampSliderValue", "windLockModeOf"))
    return _JS_PRELUDE + "\n" + real + "\n" + _js_body("renderDeviceSettings") + "\n" + _JS_CHECKS


def test_sheet_renders_correctly_in_node():
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node 不可用，本批必须真跑渲染（静态断言抓不到初值/占位错）")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "t.js"
        p.write_text(_build_node_script(), encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "抽屉真跑失败：\nstdout=%s\nstderr=%s" % (r.stdout, r.stderr)
