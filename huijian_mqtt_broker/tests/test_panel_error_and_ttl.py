# -*- coding: utf-8 -*-
"""面板文案：有效期/上限一律优先用服务端回的权威值，硬编字面量只当兜底。

缺陷形态：面板把"10 分钟""8 人"写死在 HTML 与 JS 里，而这两个值的真相在 hub
（store.js 的 bindTtlMs / HUB_MEMBERS_MAX，经 expiresInSec / membersMax 回给加载项）。
hub 一改，面板就对着一张云端已作废的码继续倒计时、对已满的家庭说"还能加"。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
HTML = (ROOT / "www" / "index.html").read_text(encoding="utf-8")


def _fn(name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者）" % (name, len(hits))
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
    raise AssertionError("函数 %s 未闭合" % name)


def _body(name):
    """函数体（不含签名）——扫描型守卫必须用它，否则 `function foo(` 这一行
    自己就含 `foo(`，定义处会被当成调用处（守卫扫到自己＝恒红或恒绿的来源）。"""
    src = _fn(name)
    return src[src.index("{") + 1:]


def _node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, "node 真跑失败：%s%s" % (r.stdout, r.stderr)


# ── DOM：可被服务端值覆盖的文案点 ─────────────────────────────────────
def test_ttl_copy_has_a_server_driven_element_with_literal_fallback():
    card = HTML.split('id="remoteCard"', 1)[1]
    assert 'id="hubCodeTtl"' in card, "绑定码有效期文案没有可写的元素（只能硬编）"
    assert 'id="hubMemberTip"' in card, "成员码提示没有 id（JS 无法按服务端 TTL 改写）"
    # 静态兜底仍在：JS 没跑起来/老集成不回 TTL 时，页面不能空着
    assert re.search(r'id="hubCodeTtl">10 分钟</span>', HTML), "缺 10 分钟静态兜底"
    assert "10 分钟" in card, "卡片必须写清有效期（用户不知道该多快扫）"


def test_apply_hub_status_writes_both_ttl_copy_points():
    body = _fn("applyHubStatus")
    assert "hubCodeTtl" in body and "bindCodeTtlS" in body, "统一出口没写绑定码有效期文案"
    assert "hubMemberTip" in body and "memberCodeTtlS" in body, "统一出口没写成员码有效期文案"
    assert body.count("ttlMinutes(") >= 2


def test_no_hardcoded_member_cap_copy():
    """上限文案只能来自 membersMax（|| 8 只是兜底），不许出现写死的"最多 8 人"。"""
    for bad in ("最多 8 人", "8 人上限", "限 8 人"):
        assert bad not in JS and bad not in HTML, "面板写死了成员上限文案：%s" % bad
    assert "info.membersMax || 8" in _fn("membersText")
    assert "(usable && info.membersMax) || 8" in _fn("renderMembers")


# ── 纯函数真跑 ──────────────────────────────────────────────────────
def test_ttl_minutes_really_runs_in_node():
    _node(_fn("ttlMinutes") + """
const cases = [
  [600, 10, 10], [300, 10, 5], [90, 10, 2], [60, 10, 1], [1, 10, 1],
  [undefined, 10, 10], [null, 10, 10], [0, 10, 10], [-60, 10, 10],
  ['abc', 10, 10], [NaN, 10, 10], [Infinity, 10, 10], [{}, 10, 10],
];
for (const [got, fb, want] of cases) {
  const v = ttlMinutes(got, fb);
  if (v !== want) { console.log('FAIL', String(got), '->', v, 'want', want); process.exit(1) }
}
console.log('OK');
""")


def test_members_read_failed_really_runs_in_node():
    _node(_fn("membersReadFailed") + """
const yes = [{lastOpError:'members_unavailable'}, {lastOpError:'members_rejected'}];
const no = [null, {}, {lastOpError:null}, {lastOpError:'no_owner'},
            {lastOpError:'member_remove_failed'}, {lastOpError:'bindcode_failed'}];
for (const i of yes) { if (membersReadFailed(i) !== true) { console.log('FAIL 应判读取失败', JSON.stringify(i)); process.exit(1) } }
for (const i of no) { if (membersReadFailed(i) !== false) { console.log('FAIL 不该判读取失败', JSON.stringify(i)); process.exit(1) } }
console.log('OK');
""")


# ── 单一出口纪律 ────────────────────────────────────────────────────
def test_op_error_text_is_rendered_only_by_the_single_exit():
    """操作类错误只能在 hubErrorLine 里渲染，而 hubErrorLine 只能被 applyHubStatus 调用。

    v1.9.3 把出口拆成两层（一层定优先级、一层挂 DOM），不变量随之升级成"两层各只有一个"：
    多一个渲染点＝总有一条路径漏渲染或渲染旧值；hubErrorLine 多一个调用方＝同一句故障
    可能在两处按不同优先级显示——那正是本批要修的病的形状（症状压住病因）。
    扫描一律用 _body（不含签名），否则定义行自己含 `hubErrorLine(`，守卫会扫到自己。
    """
    callers = [n for n in re.findall(r"function (\w+)", JS)
               if n != "hubErrorLine" and "hubOpErrorText(" in _body(n)]
    assert callers == [], "除错误行出口外还有函数渲染操作类错误：%s" % callers
    assert "hubOpErrorText(" in _body("hubErrorLine")
    line_calls = [n for n in re.findall(r"function (\w+)", JS)
                  if "hubErrorLine(" in _body(n)]
    assert line_calls == ["applyHubStatus"], "hubErrorLine 的调用方不是唯一出口：%s" % line_calls
    # 成员区/二维码的渲染点数目同样钉死（定义 + 失败分支 + 正常分支）
    assert JS.count("renderMembers(") == 3, JS.count("renderMembers(")
    assert JS.count("renderMemberQr(") == 3, JS.count("renderMemberQr(")


def test_server_side_produces_every_code_the_panel_maps():
    """反向对账：面板映射的本地降级值必须真是加载项会产生的（否则是在验死码）。"""
    py = (ROOT / "custom_components" / "window_controller_gateway" / "hub_client.py").read_text(encoding="utf-8")
    for code in re.findall(r"case '([a-z_]+)':", _fn("hubOpErrorText")):
        if code in ("no_owner", "members_full", "rate_limited", "registry_full", "superseded",
                    "bad_secret", "unknown_instance", "unknown_member", "owner_cannot_leave"):
            continue                      # 这些是 hub 回的真实 err，由跨仓契约钉对账
        assert '"%s"' % code in py, "面板映射了加载项不会产生的码 %s（死映射）" % code


def test_transient_op_codes_are_all_network_degradation_sites():
    """让位名单必须与**产生点**对账：名单里每个码都得是某个 except 块里写下的。

    为什么不能只信注释：把语义类（no_owner 那种）误列进瞬态类，断连时"重试没用、得先做
    另一件事"这句就会被连接类顶掉——面板看着更顺眼，用户却少了唯一能自救的一句话；
    反过来列进一个已经不存在的死码，让位永不生效而没人发现。两种形态都靠这条当场红。
    """
    py = (ROOT / "custom_components" / "window_controller_gateway"
          / "hub_client.py").read_text(encoding="utf-8").splitlines()
    m = re.search(r"const HUB_TRANSIENT_OP_ERRORS = \[([^\]]*)\];", JS)
    assert m, "缺 HUB_TRANSIENT_OP_ERRORS 名单（v1.9.3 的让位判据）"
    listed = re.findall(r"'([a-z_]+)'", m.group(1))
    assert listed, "瞬态类名单为空＝让位永不生效（本批修法被静默拆掉）"
    for code in listed:
        hits = [n for n, line in enumerate(py)
                if "_set_op_error(" in line and '"%s"' % code in line]
        assert hits, "码 %s 没有任何 _set_op_error 产生点（死映射）" % code
        # 产生点必须落在 except 之后 10 行内——块内有界，不跨函数匹配（否则上一个
        # 方法的 except 会把本方法的正常分支也算成"异常降级"）
        assert any(any("except" in py[j] for j in range(max(0, n - 10), n + 1)) for n in hits), \
            "码 %s 的产生点不在异常处理块里＝它不是网络降级值，不该参与让位" % code


def test_panel_never_asserts_the_cloud_is_unreachable():
    """类面钉：面板全文不许再对用户说"云端暂不可达"那一类断言。

    为什么按类钉而不按条钉：v1.9.3 只改了错误行的**优先级**，三个瞬态码的文案里
    那句断言原样留着——于是"长连通着但这一次读失败"（现网实测形态：那台机器 DNS 在抖，
    而 hub 27 天没重启）仍然会对用户喊"云端暂不可达"，让他等一个不会自己好的东西。
    逐条改必然漏，所以钉**字样本身**：面板 JS 里出现次数必须为 0。
    判 JS 不判测试——测试注释里为了说明缺陷会引用这句话，那是散文不是产品面。
    """
    js = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    for phrase in ("云端暂不可达", "云端不可达", "云端挂了"):
        assert js.count(phrase) == 0, "面板仍在对用户断言「%s」（出现 %d 次）" % (
            phrase, js.count(phrase))
