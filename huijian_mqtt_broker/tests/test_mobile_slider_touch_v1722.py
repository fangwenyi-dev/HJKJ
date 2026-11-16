"""滑块命中区与可见轨道的分离契约（v1.7.22 立，v2.0.0 升格为全局）。

沿革：
- v1.7.22（用户报：三滑块距离过近、手动调节易误触）只为**触屏**补了
  30px 命中区，桌面端仍是 height:6px 即全部命中高度。
- v2.0.0（用户令：手机/Web 比例都不协调）把它升格为全局规则：
  ① 盒子高 = 命中区（--slider-hit），可见轨道 = --slider-track 由
     padding + background-clip: content-box 收窄，桌面与触屏同一套机制；
  ② 两端只覆盖**标尺变量**，不再各自重写具体宽高——此前桌面 6 处、
     手机 6 处各写一套数字，改一处另一处不跟随，正是比例漂移的成因；
  ③ 桌面命中区由 6px 提升到 32px（真误触根因），触屏 44px（HIG）。

本文件因此从"触屏专属"改判为"全端命中区契约"。原 v1.7.22 的三条
断言（触屏 padding 差 == 6px、触屏 thumb 26px、桌面基线 6px 不变）
全部随用户新令作废——不是放宽，而是被更高的要求取代。
"""
import pathlib
import re

CSS = (pathlib.Path(__file__).resolve().parents[1] / "www" / "css" / "huijian.css"
       ).read_text(encoding="utf-8")


def _touch_block() -> str:
    m = re.search(r"@media \(hover: none\) and \(pointer: coarse\) \{(.*?)\n\}", CSS, re.S)
    assert m, "缺少触摸能力查询块——手机端滑块防误触规则整体丢失"
    return m.group(1)


def _base_input() -> str:
    return re.search(r"^input\[type=\"range\"\] \{([^}]*)\}", CSS, re.M | re.S).group(1)


class TestScaleIsSingleSource:
    """标尺变量是全端唯一基准，两端只能覆盖变量。"""

    def test_root_defines_slider_scale(self):
        root = re.search(r":root \{(.*?)\n\}", CSS, re.S).group(1)
        for var, low, high in (("--slider-track", 6, 14),
                               ("--slider-thumb", 18, 32),
                               ("--slider-hit", 28, 48)):
            m = re.search(re.escape(var) + r":\s*(\d+)px", root)
            assert m, f"{var} 未在 :root 标尺中声明——尺寸又散回各处了"
            v = int(m.group(1))
            assert low <= v <= high, f"{var}={v}px 超出合理区间 {low}~{high}px"

    def test_hit_always_larger_than_track(self):
        """核心不变量：命中区必须显著大于可见轨道，否则细轨=细靶"""
        root = re.search(r":root \{(.*?)\n\}", CSS, re.S).group(1)
        track = int(re.search(r"--slider-track:\s*(\d+)px", root).group(1))
        hit = int(re.search(r"--slider-hit:\s*(\d+)px", root).group(1))
        assert hit >= track * 3, \
            f"命中区 {hit}px 不足轨道 {track}px 的 3 倍，细轨仍是细靶"

    def test_both_media_blocks_override_variables_not_geometry(self):
        """两端媒体块必须只覆盖 --slider-* 变量。
        若又在块内写死 height/width 具体值，就等于恢复了"两套几何"，
        比例漂移会原样复发——这是本批要根除的机制。"""
        for name, pat in (("640px", r"@media \(max-width: 640px\) \{(.*?)\n\}"),
                          ("touch", r"@media \(hover: none\) and \(pointer: coarse\) \{(.*?)\n\}")):
            m = re.search(pat, CSS, re.S)
            assert m, f"缺 {name} 媒体块"
            body = m.group(1)
            assert re.search(r"--slider-(hit|thumb)", body), \
                f"{name} 块未覆盖滑块标尺变量"
            assert not re.search(r"::-webkit-slider-thumb\s*\{[^}]*width:\s*\d+px", body), \
                f"{name} 块又写死了 thumb 宽度——应改标尺变量"


class TestDesktopHitArea:
    """桌面端必须是真命中区，不再是 6px 细线。"""

    def test_base_height_is_hit_area_not_track(self):
        rule = _base_input()
        m = re.search(r"height: var\(--slider-hit\)", rule)
        assert m, "基类高度必须是 --slider-hit（命中区），不能退回具体像素"

    def test_base_padding_centres_track(self):
        rule = _base_input()
        assert "calc((var(--slider-hit) - var(--slider-track)) / 2)" in rule, \
            "缺居中 padding——轨道会贴顶而不是落在命中区中线"

    def test_background_clip_after_background(self):
        """顺序陷阱：background 简写会把 background-clip 重置为 border-box，
        写在前面的 clip 会被静默吃掉，轨道渲染成整块色带。"""
        rule = _base_input()
        i_bg = rule.find("background: rgba(255, 255, 255, .12)")
        i_clip = rule.find("background-clip: content-box")
        assert i_bg != -1 and i_clip != -1, "缺 background 或 background-clip"
        assert i_bg < i_clip, \
            "background-clip 必须写在 background 之后，否则被简写重置"


class TestTouchErgonomics:
    def test_block_exists_and_is_ordered_before_reduce_motion(self):
        """插入位置契约：必须在 640px 块之后、reduce 块之前"""
        i_touch = CSS.index("@media (hover: none) and (pointer: coarse)")
        assert CSS.index("@media (max-width: 640px)") < i_touch
        assert i_touch < CSS.index("@media (prefers-reduced-motion: reduce)")

    def test_row_gap_gives_safety_band(self):
        body = _touch_block()
        m = re.search(r"\.slider-group \{ gap: (\d+)px; \}", body)
        assert m, "触摸块内缺 .slider-group 行距规则"
        assert int(m.group(1)) >= 14, f"行距 {m.group(1)}px 不足以形成相邻滑杆安全带"

    def test_touch_hit_area_meets_hig(self):
        """触屏命中区 ≥44px（Apple HIG 最小触控目标）"""
        body = _touch_block()
        m = re.search(r"--slider-hit:\s*(\d+)px", body)
        assert m, "触摸块未覆盖 --slider-hit"
        assert int(m.group(1)) >= 44, f"触屏命中区 {m.group(1)}px < 44px HIG 下限"


class TestNoBehaviorChange:
    def test_js_untouched_by_this_batch(self):
        """纯 CSS 批：JS 不得出现为滑块新增的 pointer/touch 事件劫持
        （松手才发令的 change 语义 + v1.7.21 位置合并是唯一提交闸口）"""
        js = (pathlib.Path(__file__).resolve().parents[1] / "www" / "js" / "huijian.js"
              ).read_text(encoding="utf-8")
        for slider in ("position-slider", "speed-slider", "strength-slider"):
            assert js.count(slider) >= 2, f"{slider} 渲染/静默更新引用丢失"
        assert "touchstart" not in js and "pointerdown" not in js, \
            "滑块新增触摸劫持与本批口径不符（误触根因是间距，不是事件）"