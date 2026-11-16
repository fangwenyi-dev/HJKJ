"""v1.6.27 Web UI「星辰大海」重设计回归钉桩。

设计权威＝小程序记忆体标准条 0d2200c5（工程口径 d0c93032）+ 终版细节条
901a18df；本文件把标准里**可机器化**的硬约束钉进 CI：令牌逐值、分层规矩
（.page 无底色/.star-bg fixed z0/pointer-events:none）、流星只准向下且行程
1100px、星云一朵 45s 且宁淡、行星双层时钟解耦、固定种子确定性、旧靛紫
主题零残留、JS 用到的 class 在 CSS 必须全部有归属（防搬运式漏样式）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WWW = ROOT / "www"
CSS = (WWW / "css/huijian.css").read_text(encoding="utf-8")
INDEX = (WWW / "index.html").read_text(encoding="utf-8")
SKY = (WWW / "js/starsky.js").read_text(encoding="utf-8")
JS = (WWW / "js/huijian.js").read_text(encoding="utf-8")


def _code(css: str) -> str:
    """去块注释后的 CSS（标准里 #4f46e5 等旧色只准出现在说明文字）。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class TestDesignTokens:
    def test_standard_tokens_verbatim(self):
        code = re.sub(r"\s+", "", _code(CSS))
        # 底色为 ea92e36c 方案A「深蓝夜空」定案（小程序 98ebc5b 实证，用户
        # 2026-09 推广全仓）：#030712「过黑」→#071426、v1.0.42 #1d1750「过紫」
        # 亦作废；surface 随动 #0a1a30。其余令牌仍逐值照抄标准 0d2200c5
        for tok in ("--primary:#0ea5e9", "--primary-dark:#0284c7",
                    "--primary-light:#38bdf8", "--accent:#06b6d4",
                    "--secondary:#f59e0b", "--bg-main:#071426",
                    "--surface:#0a1a30", "--bg-card:rgba(255,255,255,.07)",
                    "--bg-elevated:rgba(255,255,255,.10)",
                    "--card-border:rgba(255,255,255,.14)",
                    "--text-primary:#f8fafc", "--text-secondary:#94a3b8"):
            assert tok in code, f"标准令牌缺失/走样: {tok}"

    def test_old_indigo_theme_dead(self):
        code = _code(CSS)
        for dead in ("#4f46e5", "#5b52f0", "#818cf8", "#4338ca", "#312e81",
                     "#eef2ff", "#f4f6fc", "#e6eaf2"):
            assert dead not in code, f"旧靛紫主题色值回潮: {dead}"

    def test_purple_confined_to_ambient(self):
        """紫只允许出现在氛围底色层；按钮/图标前景禁紫（标准原话）。"""
        code = _code(CSS)
        tilt = re.search(r"\.btn-tilt \{[^}]*\}", code).group(0)
        assert "#8b5cf6" not in tilt and "#6366f1" not in tilt and \
            "#818cf8" not in tilt, "内倒按钮禁紫（v1.6.26 旧紫渐变回潮）"


class TestLayerArchitecture:
    def test_star_bg_fixed_z0(self):
        code = _code(CSS)
        seg = code[code.index(".star-bg {"):code.index(".nebula {")]
        assert "position: fixed" in seg and "z-index: 0" in seg
        assert "pointer-events: none" in seg, "装饰层不得吞点击"
        assert "inset: 0" in seg

    def test_page_is_z1_without_background(self):
        """规矩①：内容根不得带底色，否则整块盖住星空（标准 9 页踩坑史）。"""
        code = _code(CSS)
        page = re.search(r"\.page \{([^}]*)\}", code).group(1)
        assert "position: relative" in page and "z-index: 1" in page
        assert "background" not in page

    def test_body_has_no_maxwidth(self):
        body = re.search(r"\nbody \{(.*?)\n\}", _code(CSS), re.S).group(1)
        assert "max-width" not in body, "body 限宽会截断星空（宽度已移到 .page）"

    def test_theme_color_matches_deep_space(self):
        # ea92e36c 方案A：状态栏底色=夜空底 #071426（对应定案②导航色四处同步）
        assert 'name="theme-color" content="#071426"' in INDEX


class TestSkyElements:
    def test_meteor_seven_diagonal_down_1100(self):
        """v1.7.24 用户点报"流星运行方向不对"——钉回母本斜坠几何：
        rotate(var(--mrot)) translateX(1100px)。默认 145° 斜左下 4 道
        （起点右半屏），.mr 35° 斜右下 3 道（起点左半屏）；只准向下
        （-35° 爬升版废弃，禁负角度/负位移）。"""
        assert INDEX.count('<span class="meteor') == 7, "流星恰 7 颗（标准）"
        assert INDEX.count(' mr"') == 3, ".mr 斜右下恰 3 道"
        for n in ("1", "2", "7"):
            assert re.search(rf'class="meteor meteor-{n}(?: m-\w+)? mr"', INDEX), \
                f"meteor-{n}（左半屏起点）应 .mr 斜右下"
        seg = CSS[CSS.index("@keyframes meteor-fall"):][:700]
        assert "rotate(var(--mrot)) translateX(1100px)" in seg, \
            "行程须 1100px 母本斜向延伸（官网 700px 手机竖屏截断）"
        for up in ("translateY(-", "rotate(-", "translateX(-"):
            assert up not in seg, f"流星只准向下，禁爬升几何回潮: {up}"
        assert "--mrot: 145deg" in CSS and ".meteor.mr { --mrot: 35deg; }" in CSS, \
            "双档角度：默认 145° 斜左下 / .mr 35° 斜右下"
        # v1.7.25 用户点报"流星只有上半屏有"——母本斜行程纵向分量≈631px 系按
        # 手机竖屏算的，桌面视口必须有起点在中高度的道次扫过下半屏
        assert "top: var(--my, -12%)" in CSS, "基类起点必须读 --my"
        mys = [float(m) for m in re.findall(
            r"\.meteor-\d \{[^}]*?--my: (-?[\d.]+)%", CSS)]
        assert len(mys) == 7, f"逐颗 --my 起点必须显式: {mys}"
        assert sum(1 for v in mys if v > 0) >= 3, \
            f"至少 3 道中高度起点（下半屏覆盖）: {mys}"
        durs = [float(m) for m in re.findall(
            r"\.meteor-\d \{[^}]*?--mdur: ([\d.]+)s", CSS)]
        assert len(durs) == 7 and all(10.0 <= d <= 16.0 for d in durs), \
            f"流星 10~16s 全周期节奏: {durs}"
        delays = [float(m) for m in re.findall(
            r"\.meteor-\d \{[^}]*?--mdelay: (-?[\d.]+)s", CSS)]
        assert len(delays) == 7 and all(d < 0 for d in delays), \
            f"流星必须负相位进场（正延迟=打开页面头几秒无星可看，用户实测反馈）: {delays}"

    def test_nebula_matches_scheme_a_master(self):
        """星云＝官网三团结构（45s/50s/60s aurora-move 独立漂移）；配色随
        ea92e36c 方案A⑤——主星云（蓝团）核提亮 rgba(56,189,248,.10) + 靛 .05
        中段 + 紫尾锥 .035（紫的第②通道）；青/金两团随「氛围退、主体进」退回
        官网克制原值 .08/.07（v1.0.42 的 .12/.10 提浓作废，禁回潮）。"""
        assert INDEX.count('class="nebula"') == 1 and 'class="nebula3"' in INDEX
        code = _code(CSS)
        # 主星云 ::before 左上、青团 ::after 右下、金团 .nebula3 中上
        b = re.search(r"\.nebula::before \{([^}]*)\}", code).group(1)
        a = re.search(r"\.nebula::after \{([^}]*)\}", code).group(1)
        g = re.search(r"\.nebula3 \{([^}]*)\}", code).group(1)
        assert ("rgba(56, 189, 248, .1) 0%" in b and "rgba(99, 102, 241, .05) 45%" in b
                and "rgba(139, 92, 246, .035) 65%" in b
                and "top: -30%" in b and "aurora-move1 45s" in b), f"方案A主星云核/尾锥: {b}"
        assert "rgba(6, 182, 212, .08)" in a and "bottom: -20%" in a and "aurora-move2 50s" in a
        assert "rgba(245, 158, 11, .07)" in g and "aurora-move3 60s" in g
        # 三团 peak α 均 ≤.10（方案A后「氛围退」上限；再浓即回潮"白光"）
        for blk in (b, a, g):
            alphas = [float(x) for x in re.findall(r"rgba\([\d, ]+, (\.[\d]+)\)", blk)]
            assert alphas and max(alphas) <= 0.10, f"星云团宁淡勿浓: {alphas}"
        # 三 keyframe 齐备（整体漂移，非内核缩放雾）
        for kf in ("@keyframes aurora-move1", "@keyframes aurora-move2", "@keyframes aurora-move3"):
            assert kf in code, f"缺官网漂移关键帧 {kf}"
        assert "nebula-drift" not in code, "旧单团 nebula-drift 应已移除"

    def test_reduced_motion_kill_switch(self):
        assert "@media (prefers-reduced-motion: reduce)" in CSS
    def test_logo_dual_animation(self):
        seg = re.search(r"\.brand-logo \{[^}]*\}", _code(CSS)).group(0)
        assert "hero-float" in seg and "hero-breathe" in seg, \
            "logo 呼吸+悬浮双动画并行（标准：品牌永不触零）"
        br = CSS[CSS.index("@keyframes hero-breathe"):][:200]
        assert re.search(r"opacity: \.?0?\.7", br), \
            "呼吸下限 .7（0.4 过暗被否，品牌不触零）"
        # v1.6.30 用户令：logo 要像小程序那样发光——双环光晕 + 本体 drop-shadow
        assert "0 0 56px 14px rgba(125, 211, 252, .22)" in seg, "远环氛围光晕"
        assert "drop-shadow(0 0 6px rgba(125, 211, 252, .6))" in CSS[CSS.index(".brand-logo img"):], \
            "logo 本体沿形发光（alpha 轮廓）"
        assert "border-radius: 50%" in seg, "v1.7.1 用户令：外框正圆"
        assert "border-radius: 50%" in CSS[CSS.index(".brand-logo img"):], "img 圆形裁切"
        assert "radial-gradient(circle at 50% 42%" in CSS[CSS.index(".brand-logo::before"):][:250], \
            "内层径向发光盘面"


class TestStarskyJs:
    def test_fixed_seed(self):
        assert "mulberry32" in SKY, "固定种子 PRNG（全站同一片天机制）"
        assert "0x5EA7C0DE" in SKY

    def test_planet_clock_contract(self):
        assert "i < 16" in SKY, "行星恰 16 颗"
        assert re.search(r"between\(55, 75\)", SKY), "漂移 55~75s"
        assert re.search(r"between\(3\.5, 7\)", SKY), "明灭 3.5~7s"
        assert "(i % 10) < 6" in SKY, "60% 星云区偏置"
        # 偏置区必须随官网三团落点重排（左上蓝/中上金/右下青），
        # 禁仍指旧单团左下（x2-52/y52-96）
        assert "between(2, 42)" in SKY and "between(55, 88)" in SKY and "between(60, 97)" in SKY, \
            "行星偏置区须对位官网三团"
        assert "between(52, 96)" not in SKY, "旧单团左下偏置区已作废"
        assert re.search(r"pick\(\[3, 3, 4, 4, 6\]\)", SKY), "3/4/6px 三档"
        # 双层独立时钟 = 两个 DOM 层各自 animation（漂移在 .orbit-wrap，
        # 明灭在 .planet）——CSS 侧耦合回潮即红
        orb = re.search(r"\.orbit-wrap \{[^}]*\}", CSS).group(0)
        pl = re.search(r"\.planet \{[^}]*\}", CSS).group(0)
        assert "planet-drift" in orb and "planet-pulse" not in orb
        assert "planet-pulse" in pl and "planet-drift" not in pl

    def test_star_field_matches_official(self):
        """v1.7.3 用户令「星星尺寸过小」——按官网母本 fillStars/index.html
        L139-144 校准：far 1.8~2.5px、near 2.5~3.2px（旧 0.6~2.2 微尘作废）、
        周期 3~7s、峰亮度 far .55~.75/near .8~.95、白/蓝/琥珀三色 + 大星白晕"""
        far = re.search(r"makeStars\('starsFar', 70, ([^)]+)\)", SKY).group(1)
        near = re.search(r"makeStars\('starsNear', 36, ([^)]+)\)", SKY).group(1)
        assert "1.8, 2.5" in far and "3, 7" in far, f"far 尺寸档须对位官网: {far}"
        assert "2.5, 3.2" in near and "3, 7" in near, f"near 尺寸档须对位官网: {near}"
        assert "0.6, 1.4" not in SKY and "1.2, 2.2" not in SKY, "旧微尘尺寸作废"
        # 正弦明暗：0/100% 下限、50% 峰（官网 star-twinkle），禁旧单峰触零
        kf = CSS[CSS.index("@keyframes star-twinkle"):][:230]
        assert "0%, 100%" in kf and "50%" in kf, "正弦双端"
        assert "var(--floor" in kf and "var(--peak" in kf
        assert "--floor:" in SKY and "s-blue" in SKY and "s-amber" in SKY and "s-glow" in SKY
        # 三色 + 白晕类样式在位
        assert "#38bdf8" in CSS[CSS.index(".s-blue"):], "官网蓝 #38bdf8"
        assert "#fbbf24" in CSS[CSS.index(".s-amber"):], "官网琥珀 #fbbf24"
        assert "box-shadow: 0 0 4px" in CSS[CSS.index(".s-glow"):], "大星 4px 白晕"

    def test_meteor_tricolor(self):
        """v1.7.4 用户令「流星还要官网黄/蓝流星」——官网 createMeteor 配比
        60%白/25%蓝/15%琥珀：7 道 = 白4 + m-blue×2 + m-amber×1，
        尾迹同色渐变头 + drop-shadow 辉光"""
        assert INDEX.count('m-blue') == 2 and INDEX.count('m-amber') == 1, \
            "7 道流星三色配比 4白/2蓝/1琥珀"
        mb = re.search(r"\.meteor\.m-blue \{([^}]*)\}", CSS).group(1)
        ma = re.search(r"\.meteor\.m-amber \{([^}]*)\}", CSS).group(1)
        assert "#38bdf8" in mb and "drop-shadow" in mb, "官网蓝流星 #38bdf8"
        assert "#fbbf24" in ma and "drop-shadow" in ma, "官网琥珀流星 #fbbf24"

    def test_star_clusters(self):
        """v1.7.4 用户令「多颗星星聚一团、明暗变化」（附官网截图）——
        官网 canvas Star 大星口径：4 团 6~8 颗聚簇、核心大星 8px 晕、
        正弦明暗；星团层同享 reduce 豁免"""
        assert 'id="clusters"' in INDEX, "星团容器在位"
        assert "function makeClusters()" in SKY and "makeClusters();" in SKY
        assert SKY.count("if (!host) return") >= 3, "星团层也须静默跳过不反噬"
        cl = re.search(r"\.stars-cluster i \{([^}]*)\}", CSS).group(1)
        assert "star-twinkle" in cl, "星团复用正弦明灭"
        glow = re.search(r"\.stars-cluster i\.s-glow \{([^}]*)\}", CSS).group(1)
        assert "0 0 8px 2px" in glow, "核心大星 8px 晕（canvas glow size×4 对应）"

    def test_decorative_layer_isolation(self):
        assert "fetch" not in SKY and "XMLHttpRequest" not in SKY, \
            "装饰层零网络"
        assert SKY.count("if (!host) return") >= 2, \
            "容器缺失必须静默跳过——装饰层永不反噬功能层"


class TestDomHookCoverage:
    """JS 动态模板 + 静态骨架用到的每个 class 必须在 CSS 有规则
    （重设计最容易翻车的就是漏样式导致裸元素浮在深空上）。"""

    def test_every_used_class_has_css_rule(self):
        used = set()
        for src in (JS, INDEX):
            for raw in re.findall(r'class="([^"{]+)"', src):
                for tok in raw.replace("'", " ").replace("?", " ").split():
                    if re.fullmatch(r"[A-Za-z][\w-]*", tok):
                        used.add(tok)
        # 白名单：无独立样式语义的钩子（position-slider 继承滑块主色；
        # *-value 三子类和 checkUpdateBtn 经基类 .slider-value/.btn 合成生效）
        skip = {"position-slider", "checkUpdateBtn",
                "position-value", "speed-value", "strength-value"}
        missing = {c for c in used - skip
                   if not re.search(rf"\.{re.escape(c)}\b", CSS)}
        assert not missing, f"重设计遗漏样式归属: {sorted(missing)}"

    def test_functions_called_by_onclick_survive(self):
        # 与 v1.6.25 三文件化守卫同族：骨架改动不得动到 onclick 函数面
        for fn in set(re.findall(r'onclick="(\w+)\(', INDEX)):
            assert re.search(rf"\bfunction {fn}\s*\(", JS), f"{fn}() 丢失"


class TestEmptySlotsTransparent:
    """用户校准的留空原则：网关/子设备区未添加处必须透出星空，
    玻璃只落在真实存在的悬浮件上（网关头、设备瓷砖各自自持）。"""

    def _rule(self, selector):
        m = re.search(re.escape(selector) + r" \{([^}]*)\}", _code(CSS))
        assert m, f"缺少规则 {selector}"
        return m.group(1)

    def test_gateway_card_flush(self):
        assert 'class="card card-flush"' in INDEX, "网关区须用留空卡"
        assert 'class="card card-flush card-dense"' in INDEX, \
            "服务状态板 v1.6.30 起同走留空（用户令：页头下方长玻璃取消）"
        r = self._rule(".card-flush")
        assert "background: transparent" in r and "backdrop-filter: none" in r

    def test_gateway_container_has_no_slab(self):
        r = self._rule(".gateway-item")
        assert "background" not in r, "网关容器铺底=空位也被磨成雾（违留空原则）"

    def test_page_header_glass_removed(self):
        """v1.7.2 用户令：取消顶部页头玻璃（截图那条带渐变底/模糊/边框/
        阴影的 bar）。留空原则扩到头栏——只留布局（logo/标题/按钮自身
        与间距），容器底与 ::after 装饰高光全清。注意与 .gateway-header
        （网关区头部，test_glass_lives_on_real_blocks 钉其必须自持玻璃）
        是不同元素，此处只约束 .header。"""
        r = self._rule(".header")
        assert "background: transparent" in r, "页头须透星空"
        assert "backdrop-filter: none" in r and "-webkit-backdrop-filter: none" in r
        assert "border: none" in r and "box-shadow: none" in r
        seg = CSS[CSS.index(".header::after"):][:200]
        assert "content: none" in seg, "::after 装饰高光须一并移除"

    def test_glass_lives_on_real_blocks(self):
        hdr = self._rule(".gateway-header")
        assert "--bg-card" in hdr and "backdrop-filter" in hdr, "网关头部须自持玻璃"
        dev = self._rule(".device-item")
        assert "--bg-card" in dev and "backdrop-filter" in dev, "设备瓷砖须真玻璃（含磨砂）"

    def test_empty_hint_spans_row(self):
        """empty-hint 是 .device-list 的子 <p>，grid 改造后必须横跨整行，
        否则"暂无子设备"提示被挤进首根 ≤340px 轨道左偏（v1.6.28 实测逮获）"""
        rule = self._rule(".device-list .empty-hint")
        assert "grid-column: 1 / -1" in rule

    def test_tiles_uniform_5005_height(self):
        """行内等高：v1.6.29 立「全部子设备玻璃按 5005 统一高度」，
        v2.0.0 用户令（手机/Web 比例不协调）改判保留**目的**、推翻**手段**。

        原手段 = grid stretch + min-height:262px 双保险。实测代价：每个格子
        底部 70~75px 纯死白（内容 192px / 卡片 262px，占 27%），桌面与 390px
        手机同现。新手段 = 只留 grid stretch：同一行仍然自动等高（目的不变），
        但基准改成该行内容最高的那张卡，不再凭空多出 70px。
        ⇒ 本断言此后守 stretch 与轨道封顶，不再守固定像素。"""
        grid = self._rule(".device-list")
        assert "align-items: start" not in grid, "hug 口径已作废，不得收缩"
        assert "minmax(280px, 340px)" in grid and "auto-fill" in grid, \
            "v1.7.9 用户令「web 端改回去」：恢复 v1.6.29 轨道封顶（单设备行右侧留空不拉伸）"
        dev = self._rule(".device-item")
        assert "min-height: 0" in dev, "应显式解除--而不是留着--固定高度锚"
        assert "min-height: 262px" not in dev, \
            "固定高度锚回潮：底部 70~75px 死白会原样复发（v2.0.0 实测数据）"
        assert "max-width" not in dev, "v1.7.8 的 680 封顶兜底已随回退作废"
        assert re.search(r"^\s*height:", dev, re.M) is None, "禁止固定高度"

    def test_reduced_motion_meteor_keeps_flowing(self):
        """v1.6.29 官网同款口径：减弱动画下流星**不停**——官网尾注注入
        duration !important 穿透其 reduce 守卫（用户同浏览器实证官网照见
        流星），母本行为即标准。流星走 slow 关键帧慢速流动、其余动画静止。
        禁回潮两版旧口径（全隐身 v1.6.28 / 静态冻结 v1.6.29 初版）"""
        seg = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        m = re.search(r"\.meteor \{([^}]*)\}", seg).group(1)
        assert "animation-name: meteor-fall-slow !important" in m
        assert "animation-duration: var(--mdur, 12s) !important" in m
        assert "animation-iteration-count: infinite !important" in m
        assert "translateY(260px)" not in seg, "静态冻结口径已作废"
        kf = CSS[CSS.index("@keyframes meteor-fall-slow"):][:280]
        assert "rotate(var(--mrot)) translateX(850px)" in kf and "opacity: .9" in kf and "opacity: .5" in kf, \
            "slow 关键帧温和参数（峰 .9 尾 .5 程 850px）且仍走母本斜向角"
        assert "translateY(" not in kf, "垂直下坠旧几何已作废"

    def test_reduced_motion_stars_and_planets_keep_twinkling(self):
        """v1.6.30 用户令"星云中的星星应该可以动并闪烁"：官网星星是 canvas
        rAF 驱动（reduce 拦不住），小程序标准双层独立时钟常动。CSS 对应物
        = 星点 var(--dur)/漂移 var(--drift)/明灭 var(--twk) 三层在 reduce 下
        全部恢复各自周期 + infinite；内容层转场不受豁免（合规）。"""
        seg = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        for sel, var in ((".stars-far i, .stars-near i, .stars-cluster i", "var(--dur, 4s)"),
                         (".orbit-wrap", "var(--drift, 60s)"),
                         (".planet", "var(--twk, 5s)")):
            rule = re.search(re.escape(sel) + r" \{([^}]*)\}", seg)
            assert rule, f"reduce 例外缺 {sel}"
            body = rule.group(1)
            assert f"animation-duration: {var} !important" in body, sel
            assert "animation-iteration-count: infinite !important" in body, sel
        assert "nebula3" in seg and "animation-duration: 45s !important" in seg, \
            "v1.7.1 用户令星云漂移也纳入豁免（静止态不可辨）"

    def test_sky_ambient_scheme_a(self):
        """ea92e36c 方案A「深蓝夜空」：夜空感靠底色自带蓝相（180° 深蓝渐变），
        不靠大面积染紫——v1.0.42 三档大紫云（靛紫 .22/薰衣草 .16/底靛 .24）
        随「过黑+过紫」被否，整条删除不回加；紫全站仅两通道（定案④）：
        右下小角 α.06（氛围层）+ 星云尾锥 α.035（星云钉在 nebula 测试）。
        星点/行星/按钮一律无紫口径不变。"""
        seg = re.search(r"\.star-bg \{.*?background:([^;]+);", CSS, re.S).group(1)
        assert "linear-gradient(180deg, #071426 0%, #0a1a30 55%, #0c2138 100%)" in seg, "定案①深蓝渐变底"
        assert "rgba(14, 165, 233, .14)" in seg, "左上蓝角云降档（.22→.14）"
        assert "rgba(139, 92, 246, .06)" in seg, "紫通道①右下小角 α.06"
        for dead in ("rgba(139, 92, 246, .22)", "rgba(167, 139, 250",
                     "rgba(99, 102, 241, .24)", "rgba(6, 182, 212, .12)"):
            assert dead not in seg, f"大面积染紫/靛路线回潮: {dead}"
        planet = re.search(r"\.planet \{[^}]*\}", CSS).group(0)
        assert "#8b5cf6" not in planet and "6366f1" not in planet, "紫仅限氛围层"
        assert "#8b5cf6" not in re.search(r"\.stars-far i[^}]*\}", CSS, re.S).group(0)

    def test_control_buttons_and_value_box_transparent(self):
        """v1.6.30 用户令：开/关/停/内倒四键 + 底部数值框透明化——
        ghost 描线承载色彩，.slider-value 无底无框"""
        seg = CSS[CSS.index(".control-row .btn-success, .control-row .btn-danger"):]
        head = seg[:seg.index("}") + 1]
        assert "background: transparent" in head and "border: 1px solid currentColor" in head
        sv = re.search(r"^\.slider-value \{([^}]*)\}", CSS, re.M | re.S).group(1)
        assert "background: transparent" in sv and "border: none" in sv

    def test_brand_logo_image(self):
        """页头＝慧尖真 logo（v1.6.29 用户指定，替换 📡 emoji），素材入 www/img
        随 Dockerfile COPY www/ 整目录进镜像"""
        assert 'class="brand-logo"><img src="img/logo.png"' in INDEX.replace("\n", " ").replace("  ", " ") or 'img/logo.png' in INDEX
        logo = Path(__file__).resolve().parents[1] / "www" / "img" / "logo.png"
        assert logo.is_file() and logo.stat().st_size > 10_000, "logo 素材随包"
        assert ".brand-logo img" in CSS, "img 铺盘样式在位"
