# -*- coding: utf-8 -*-
"""v1.7.46 钉桩：面板 JS 的**重名钉**与"云通道故障原因"接线。

为什么必须有重名钉（本仓第四次同类假绿）：v1.7.41～v1.7.45 线上一直带着一个缺陷——
`www/js/huijian.js` 里 `loadRemoteControl` 被定义了两次（`:170` v1.7.41 新版走统一渲染
出口 `applyHubStatus`、`:207` v1.7.37 旧版残留）。JS 函数声明**后者覆盖前者** ⇒ 页面加载
与 30s 无感刷新跑的全是旧版：绑定码过期文案（`hubCodeExp`）永不显示、「纳管网关 N 台 ·
M 个子设备」退回只显示 `gatewaySn`；新版只在"点二维码"那条 POST 路径可达。

而 940 条测试全绿——因为所有结构钉用 `index("function loadRemoteControl(")` **只取第一处**，
钉到的是那个永不执行的版本，连"除统一出口外不得直调 renderBindQr"的反钉也一起失效
（旧版有三处直调）。教训：**钉"某函数必须做 X"的前提是"该函数只有一个"**。

本文件三件事：
  1) 重名钉：扫全部函数名，每个名字恰好出现一次（含元钉：扫到 0 个即红，防空扫假绿）；
  2) 单一渲染出口：`loadRemoteControl` 必须走 `applyHubStatus`，且旧渲染器指纹消失；
  3) `hubErrorText`：DOM/CSS/接线齐备 + **node 真跑**映射（只映射已知码，未知一律不显示）。
"""
import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
HTML = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")

_FUNC_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?function[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(", re.M)


def _names(src):
    return _FUNC_RE.findall(src)


def _single_func_body(src, name):
    """取唯一那个函数的花括号全体；0 处或多处都报错（绝不静默取第一处）。"""
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", src)]
    assert len(hits) == 1, \
        "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
    i = hits[0]
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("函数 %s 花括号不配平（解析锚点失效）" % name)


# ── 1) 重名钉 ────────────────────────────────────────────────────────
def test_no_duplicate_function_definitions():
    names = _names(JS)
    assert names, "解析锚点失效：一个函数都没抽到（守卫会假绿）"      # 元钉：防空扫
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, "huijian.js 存在重复定义的函数（后者覆盖前者＝前者是死码）：%s" % dupes


def test_extractor_sees_every_definition_site():
    """元钉：抽取正则必须能看到全部定义点，否则重名钉本身会假绿。

    做法：拿 `function` 关键字的裸计数与正则抽到的名字数对账（允许 async 前缀）。
    """
    raw = len(re.findall(r"\bfunction\s+[A-Za-z_$][\w$]*\s*\(", JS))
    assert raw == len(_names(JS)), \
        "抽取正则漏了定义点：裸计数 %d vs 抽到 %d" % (raw, len(_names(JS)))
    assert raw >= 10, "面板函数数量异常（%d），锚点可能已漂移" % raw


# ── 2) 单一渲染出口 ──────────────────────────────────────────────────
def test_load_remote_control_goes_through_single_exit():
    body = _single_func_body(JS, "loadRemoteControl")
    assert "applyHubStatus(" in body, "loadRemoteControl 必须走统一渲染出口 applyHubStatus"
    assert "renderBindQr(" not in body, "不得绕过统一出口直调 renderBindQr"
    assert "hubCodeExp" not in body, "过期文案由 applyHubStatus 统一写，别在这里各写一份"


def test_old_renderer_fingerprints_are_gone():
    """旧版渲染器的指纹必须消失（只判"不存在"是单侧钉，所以配上一条正向钉）。"""
    assert JS.count("async function loadRemoteControl(") == 1, "loadRemoteControl 只能有一份定义"
    assert "info.gatewaySn || '—'" not in JS, \
        "旧版直写 gatewaySn 的残留还在（多网关用户会以为只管一台，应走 hubGatewayText）"


# ── 3) 云通道故障原因 ────────────────────────────────────────────────
def test_hub_error_element_wired():
    assert 'id="hubError"' in HTML, "index.html 缺 #hubError（lastError 有值也没处显示）"
    card = HTML.split('id="remoteCard"', 1)[1]
    assert 'id="hubError"' in card, "#hubError 必须在「远程控制（慧尖云）」卡内"
    assert re.search(r"\.hub-err\s*\{", CSS), "缺 .hub-err 样式（显示出来也没排版）"
    body = _single_func_body(JS, "applyHubStatus")
    assert "hubErrorText(" in body, "统一渲染出口没有渲染故障原因"
    assert "errEl.hidden = true" in body, "未启用/读取失败分支必须把原因藏起来（别留旧值）"


def test_hub_error_slots_are_split_conn_vs_op():
    """v1.7.50：错误槽拆成两个——连接类（lastError）与操作类（lastOpError）。

    为什么必须拆：混在一个槽里时，一次瞬时换码失败会长期盖住 identity_rejected_loop
    这条最有诊断价值的信息（面板只有一句"稍后再试"），而且成功路径都不清它。
    渲染时**操作类优先**（那是用户刚点那一下的结果），仍只走唯一出口。
    """
    apply_body = _single_func_body(JS, "applyHubStatus")
    assert "hubOpErrorText(info.lastOpError)" in apply_body, "统一出口没渲染操作类错误"
    assert re.search(r"hubOpErrorText\(info\.lastOpError\)\s*\|\|\s*hubErrorText\(info\.lastError\)",
                     apply_body), "操作类必须优先于连接类（顺序反了＝用户看到的永远是后台状态）"
    conn = _single_func_body(JS, "hubErrorText")
    op = _single_func_body(JS, "hubOpErrorText")
    for code in ("identity_rejected", "identity_rejected_loop"):
        assert "'%s'" % code in conn, "hubErrorText 漏映射连接类码 %s" % code
    for code in ("bindcode_failed", "no_owner", "members_full"):
        assert "'%s'" % code not in conn, "操作类码 %s 不该在连接类映射里（两个槽会互相污染）" % code
    for code in ("identity_rejected", "identity_rejected_loop"):
        assert "'%s'" % code not in op, "连接类码 %s 不该在操作类映射里" % code
    for fn, body in (("hubErrorText", conn), ("hubOpErrorText", op)):
        assert "default:" in body and "return ''" in body, "%s 未知码必须回空串（不显示）" % fn


def _str_consts_in_slot_arg(node):
    """只取"会作为码值落进操作槽"的字符串字面量：直接常量，或 `X or "码"` 的尾项。
    `data.get("err")` 里的 `"err"` 是字典键不是码——按尾项/直接常量收窄。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        last = node.values[-1]
        if isinstance(last, ast.Constant) and isinstance(last.value, str):
            return {last.value}
    return set()


def _codes_the_plugin_can_emit():
    """操作槽里可能出现的码，一律从**生产代码**派生（AST），不再抄手写清单。

    为什么必须派生：手写清单等于没测。v1.7.64 让 `_http` 新产生 `bad_body`（经调用方
    `e.err or "…"` 落进操作槽），它不在任何手写清单里，而面板对未知码走
    `default: return ''`（上面 conn/op 那条钉刻意要求的行为）⇒ 用户点完「添加家人」
    毫无反应，而全量 1481 条当时全绿。
    """
    codes = set()
    for p in sorted((ROOT / "custom_components" / "window_controller_gateway").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fname = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            if fname == "_set_op_error" and len(n.args) >= 2:
                codes |= _str_consts_in_slot_arg(n.args[1])
            elif fname == "HubHttpError" and len(n.args) >= 3:
                # 第三实参 = err；调用方普遍写 `e.err or "<兜底码>"`，err 非空时直接进槽
                codes |= _str_consts_in_slot_arg(n.args[2])
    return codes


def test_op_error_covers_every_code_the_plugin_can_emit():
    """操作类映射必须覆盖加载项真会产生的每个值：漏一个＝用户点了没反应。

    取值来源两类：① hub 的真实 err（no_owner/members_full/rate_limited/registry_full/
    superseded/bad_secret/unknown_instance/unknown_member/owner_cannot_leave）——此前被压成
    一个笼统的 bindcode_rejected，面板只能说"稍后再试"，而 no_owner 的正解是"先自己扫码
    成为主人"，重试永远不会成功；② 本地降级值（网络失败/老 hub）+ 本地形状闸产生的码。
    ② 现在从生产代码 AST 派生（见 `_codes_the_plugin_can_emit` 为什么要派生）。
    """
    body = _single_func_body(JS, "hubOpErrorText")
    derived = _codes_the_plugin_can_emit()
    assert derived, "派生集为空＝扫描锚点漂移，本钉在扫空气"
    for code in sorted(derived):
        if code == "members_unsupported":
            # 唯一例外：它是**持续状态**而不是故障，由成员区自己说清
            # （#hubError 在 owner 码区域，把成员类的话摆那儿正是本批 P2-3(a) 修的那个形态）
            assert "'members_unsupported'" not in body, \
                "members_unsupported 不该进 #hubError（那是 owner 码区域，成员区已自己说明）"
            continue
        assert "'%s'" % code in body, \
            "hubOpErrorText 漏映射加载项会产生的码 %s（未知码走 default 回空串＝用户点了没反应）" % code
    for code in ("no_owner", "members_full", "rate_limited", "registry_full", "superseded",
                 "bad_secret", "unknown_instance", "unknown_member", "owner_cannot_leave"):
        assert "'%s'" % code in body, "hubOpErrorText 漏映射 hub 错误码 %s（用户会看到点了没反应）" % code


def test_hub_error_text_really_runs_in_node():
    """纯函数真跑（照 test_v1743_hub_singleton.py 的 hubGatewayText 写法）：不打桩判断。"""
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑 hubErrorText")
    script = (_single_func_body(JS, "hubErrorText") + "\n" + _single_func_body(JS, "hubOpErrorText") + """
const conn = ['identity_rejected', 'identity_rejected_loop'];
for (const c of conn) {
  const s = hubErrorText(c);
  if (!s || s.length < 4) { console.log('FAIL 连接类已知码没有文案:', c, JSON.stringify(s)); process.exit(1) }
  if (!/[\\u4e00-\\u9fa5]/.test(s)) { console.log('FAIL 文案必须是中文:', c, s); process.exit(1) }
}
const op = ['no_owner', 'members_full', 'rate_limited', 'registry_full', 'superseded',
            'bad_secret', 'unknown_instance', 'unknown_member', 'owner_cannot_leave',
            'bindcode_failed', 'bindcode_rejected', 'hub_too_old_for_member_code',
            'members_unavailable', 'members_rejected',
            'member_remove_failed', 'member_remove_rejected'];
for (const c of op) {
  const s = hubOpErrorText(c);
  if (!s || s.length < 4) { console.log('FAIL 操作类已知码没有文案:', c, JSON.stringify(s)); process.exit(1) }
  if (!/[\\u4e00-\\u9fa5]/.test(s)) { console.log('FAIL 文案必须是中文:', c, s); process.exit(1) }
  if (s.includes(c)) { console.log('FAIL 文案不得落回原码（等于没映射）:', c, s); process.exit(1) }
}
// no_owner 的正解是"先自己成为主人"，不是"稍后再试"——重试永远不会成功
if (!/成为主人/.test(hubOpErrorText('no_owner'))) {
  console.log('FAIL no_owner 必须指路"先自己扫码成为主人"'); process.exit(1)
}
if (/稍后再试|稍后重试/.test(hubOpErrorText('no_owner'))) {
  console.log('FAIL no_owner 不得只说"稍后再试"（那是骗用户重试）'); process.exit(1)
}
for (const c of ['RuntimeError', 'ClientConnectorError', '', null, undefined, 'members_unsupported']) {
  if (hubErrorText(c) !== '') { console.log('FAIL 未知连接码必须回空串:', c); process.exit(1) }
  if (hubOpErrorText(c) !== '') { console.log('FAIL 未知操作码必须回空串:', c); process.exit(1) }
}
// 两个映射表不得互相串味
for (const c of conn) { if (hubOpErrorText(c) !== '') { console.log('FAIL 连接码不该有操作文案:', c); process.exit(1) } }
for (const c of op) { if (hubErrorText(c) !== '') { console.log('FAIL 操作码不该有连接文案:', c); process.exit(1) } }
if (hubErrorText('identity_rejected').indexOf('重新扫码') < 0) {
  console.log('FAIL identity_rejected 必须指路重新扫码'); process.exit(1)
}
console.log('OK');
""")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "errtext.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "node 真跑失败：%s%s" % (r.stdout, r.stderr)
