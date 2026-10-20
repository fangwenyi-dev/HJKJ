# -*- coding: utf-8 -*-
"""2026-10-20 销售面批钉桩：装机者文档、客户可见文案口径、第三方许可清单。

这一批的由来是把加载项从"实验台"推到"卖给陌生人"。判的都不是代码正确性，
而是**客户一个人能不能装成、装坏了能不能自己说清坏在哪一段、以及我们对第三方
组件的许可说法是不是真的**。四条钉各对一个已经实发或机理成立的坑：

1. 客户面文档存在且非空——同类事故就在本仓：`huijian_voice/README.md` 是 0 字节
   空文件，Supervisor 的加载项文档页因此整页空白，直到销售面复核才发现。
   所以钉的判据是"字节数 > 0"，不是"路径存在"。
2. 「只能装 HAOS/Supervised」必须写明——本加载项是 Supervisor 加载项，
   HA Container/Core 根本没有加载项商店；不写出来，客户会在 Docker 版 HA 上
   反复尝试并把它当故障报障。判据用"必须出现 Container 这一被排除形态的名"，
   因为只写"需要 Home Assistant"这类空话满足不了任何检查。
3. 小程序搜索名与客户面措辞口径——仓内 UI 早就写「小慧语音」，而两份 README 还写
   微信「慧尖」小程序；客户照 README 在微信里搜"慧尖"搜不到东西。同批把
   「Matter 网关」这个入口称谓按定线改掉。
4. 面板承诺必须有出口——`huijian.js` 在 registry_full 时对客户说"请联系慧尖"，
   而全仓曾经零反馈渠道：那是张空头承诺，也是本批 DOCS.md 必须存在的最硬理由。
5. requirements.txt 的每个包都要在 THIRD-PARTY-NOTICES.md 有条目，并把
   zeroconf@0.132.0 = LGPL-2.1 这条**逐字取证**的结论钉住（常见误传是 Apache-2.0；
   上游 2026-08 才启动 LGPL→Apache 重许可，且写明"已发布的版本不换许可"）。
"""
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]          # huijian_mqtt_broker/
REPO = ADDON.parent                                   # 商店仓根

# 客户会读到的面（窄名单：历史 CHANGELOG 与审计文档不参与口径钉，它们必然含旧措辞）
CUSTOMER_FACING = {
    "根 README": REPO / "README.md",
    "加载项 README": ADDON / "README.md",
    "装机文档 DOCS.md": ADDON / "DOCS.md",
}


def _read(p):
    assert p.is_file(), "客户可见文档缺失：%s" % p
    return p.read_text(encoding="utf-8")


def test_customer_docs_exist_and_are_not_empty():
    """存在且非空。0 字节文件在 Supervisor 里表现为文档页整页空白、零报错。"""
    for label, p in CUSTOMER_FACING.items():
        assert p.is_file(), "%s 不存在：%s" % (label, p)
        size = p.stat().st_size
        assert size > 800, "%s 只有 %d 字节，等于没交付（判据 800）" % (label, size)
    notices = ADDON / "THIRD-PARTY-NOTICES.md"
    assert notices.is_file() and notices.stat().st_size > 800, \
        "第三方许可告知缺失或近乎空白：%s" % notices


def test_haos_prerequisite_is_stated_everywhere_a_buyer_looks():
    """必须写明"只有 HAOS/Supervised 能装"，并点名被排除的形态。

    反向判据为什么收 `Container`：只写"需要 Home Assistant"是空话，它在任何
    形态下都不报错——把被排除的那一类写出来，客户才能自查。
    """
    for label, p in CUSTOMER_FACING.items():
        t = _read(p)
        assert ("HAOS" in t) or ("Home Assistant OS" in t), \
            "%s 没写明必须 Home Assistant OS" % label
        assert "Container" in t, "%s 没点名 HA Container 装不了（空话式声明）" % label
        assert "Supervised" in t, "%s 没说明 Supervised 形态同样可用" % label
    # 加载项自己的 config.yaml 不许出现"任何 HA 都能装"这类暗示
    cfg = (ADDON / "config.yaml").read_text(encoding="utf-8")
    assert "所有 HA 版本通用" not in cfg and "任意 HA 均可" not in cfg


def test_docs_are_reachable_from_the_readmes():
    """DOCS.md 必须被两份 README 链到——藏在目录里等于没交付。"""
    for label in ("根 README", "加载项 README"):
        t = _read(CUSTOMER_FACING[label])
        assert "DOCS.md" in t, "%s 没有链到装机者文档 DOCS.md" % label


def test_customer_facing_wording_uses_the_real_mini_program_name():
    """客户面一律写「小慧语音」（微信搜索名），入口一律「LoRa 网关」。

    钉的是窄名单，不扫全仓：审计文档与 CHANGELOG 里历史措辞是事实记录，
    改它们等于伪造历史；扫描型守卫一旦扫到自己的历史散文就会逼人删证据。
    """
    banned = ("微信「慧尖」小程序", "「慧尖」小程序")
    for label, p in list(CUSTOMER_FACING.items()) + [("面板 index.html", ADDON / "www" / "index.html")]:
        t = _read(p)
        for b in banned:
            assert b not in t, "%s 仍在用微信「慧尖」小程序这个搜不到的名字：%s" % (label, b)
        if "小程序" in t:
            assert "小慧语音" in t, "%s 提到小程序却没写微信搜索名「小慧语音」" % label


def test_panel_promise_to_contact_has_an_exit_in_docs():
    """面板说"请联系慧尖"⇒ DOCS.md 必须有可执行的反馈章节（含渠道与三件套）。

    这是一条兑现性钉：把 DOCS.md 的「反馈怎么给」删掉必须转红。
    判据取 JS 里的**实际字符串**，不取硬编码清单——将来文案改了本钉跟着挪，
    不会变成"钉住一句已经不存在的旧文案"的死钉。
    """
    js = (ADDON / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    promises = [s for s in ("请联系慧尖", "联系慧尖") if s in js]
    if not promises:
        return          # 面板不再许诺联系 ⇒ 本钉自动让位，不留死判据
    docs = _read(ADDON / "DOCS.md")
    assert "反馈怎么给" in docs, "面板承诺%s，但 DOCS.md 没有反馈章节" % promises
    tail = docs.split("反馈怎么给", 1)[1]
    assert "渠道" in tail[:600], "反馈章节没写清走哪些渠道"
    # 判据必须收窄到"渠道"所在小节，不能拿整份文件喂绿：DOCS.md 别处本来就有
    # 迁仓地址等链接，用 `https:// in tail` 时把渠道地址整行删掉照样绿（实测）。
    seg = tail.split("\n## ", 1)[0]
    assert "https://" in seg, "反馈小节没给出可点开的地址（空喊「联系我们」等于没渠道）"
    for item in ("版本", "日志"):
        assert item in seg, "报障三件套缺%s——客户只截一句「没连上」无法定位" % item


def test_every_pinned_requirement_is_declared_in_notices():
    """requirements.txt 的每个包都必须在许可告知的**表格里**有行（防交付面漏项）。

    为什么判"表格行"而不是判全文：许可告知的正文里本来就反复提到 `zeroconf`
    （重许可那一段），把表里那一行整条删掉时全文判据照样绿——实测过。
    """
    req = ADDON / "requirements.txt"
    pkgs = [ln.split("==")[0].split(">=")[0].strip()
            for ln in req.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert pkgs, "requirements.txt 读不出包名，本钉空跑"
    notices = _read(ADDON / "THIRD-PARTY-NOTICES.md")
    rows = [ln for ln in notices.splitlines() if ln.lstrip().startswith("|")]
    assert rows, "THIRD-PARTY-NOTICES.md 没有表格，本钉无从判起"
    for name in pkgs:
        assert any(name in row for row in rows), \
            "许可告知的表格里没有 %s 这一行（requirements.txt 里钉着它）" % name


def test_zeroconf_license_finding_is_pinned_to_the_verified_value():
    """zeroconf@0.132.0 = LGPL-2.1，且这条取证结论要钉在**它自己那一行**上。

    为什么单独钉：常见误传是 Apache-2.0。上游 COPYING @ tag 0.132.0 正文即 LGPL v2.1、
    pyproject 写 license="LGPL"、PyPI classifiers 含 LGPLv2（三源交叉），而重许可
    流程自己写明"已发布的版本不换许可"。商用随整机分发时把许可说错比不说更糟。

    为什么判行不判全文：全文搜 "LGPL-2.1" 会被"关于 zeroconf 是 LGPL 这件事"那一段
    散文满足——实测把表格里的 LGPL 改成 Apache 也照样绿。判到行才算钉住结论。
    本钉同时钉版本：将来 bump zeroconf 必须重读那份 COPYING 再来改钉。
    """
    ver = None
    for ln in (ADDON / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("zeroconf"):
            ver = ln.split("==")[1].strip()
    assert ver, "requirements.txt 里没有 zeroconf==，本钉要跟着改（不许删钉）"
    notices = _read(ADDON / "THIRD-PARTY-NOTICES.md")
    row = next((ln for ln in notices.splitlines()
                if ln.lstrip().startswith("|") and "zeroconf" in ln), None)
    assert row, "许可告知表格里没有 zeroconf 那一行"
    assert ver in row, "zeroconf 那行的版本与 requirements 锁定值不一致（应为 %s）" % ver
    assert "LGPL-2.1" in row, "zeroconf 那行的许可结论不是取证到的 LGPL-2.1"
    assert "Apache" not in row, "zeroconf 那行不得出现 Apache（0.132.0 不是 Apache-2.0）"
    for src in ("COPYING", "pyproject"):
        assert src in row, "zeroconf 那行没写取证出处（缺 %s）" % src
