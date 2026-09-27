# -*- coding: utf-8 -*-
"""v1.7.47 面板「家庭成员」区：DOM 契约、单一渲染出口、纯函数与渲染**真跑**、路由等式。

两条纪律照本仓既有做法：
  · 结构钉只能证明"调用了"，证明不了"渲染对了" ⇒ `membersText` 与 `renderMembers`
    都在 node 里配最小假 DOM **真跑**（不打桩被测函数本身）；
  · 面板发出去的 path 必须逐字等于 `api.py` 注册的 url（去掉 `/api` 前缀）——
    漂移即 404，而 404 只会显示成"点了没反应"。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
HTML = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")
API = (ROOT / "custom_components" / "window_controller_gateway" / "api.py").read_text(encoding="utf-8")

IDS = ["hubMembersCount", "hubMembersEmpty", "hubMembers", "addMemberBtn",
       "hubMemberQr", "hubMemberQrBox", "hubMemberTip", "hubMemberCode", "hubMemberExp"]


def _single_func_body(src, name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", src)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
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
    raise AssertionError("函数 %s 花括号不配平" % name)


def _node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, "node 真跑失败：%s%s" % (r.stdout, r.stderr)


# ── DOM 契约 ────────────────────────────────────────────────────────
def test_member_dom_ids_all_present_and_inside_remote_card():
    card = HTML.split('id="remoteCard"', 1)[1]
    missing = [i for i in IDS if ('id="%s"' % i) not in HTML]
    assert not missing, "index.html 缺成员区 DOM：%s" % missing
    outside = [i for i in IDS if ('id="%s"' % i) not in card]
    assert not outside, "这些元素不在「远程控制（慧尖云）」卡内：%s（v1.7.38 用户令：别摆页头）" % outside


def test_member_css_present():
    for sel in (".hub-members", ".hub-member-list"):
        assert re.search(re.escape(sel) + r"\s*\{", CSS), "缺样式 %s（元素会没排版）" % sel


def test_add_member_button_wires_to_refresh_member_code():
    m = re.search(r'id="addMemberBtn"[^>]*onclick="(\w+)\(\)"', HTML, re.S)
    assert m, "「添加家人」按钮没有 onclick"
    assert m.group(1) == "refreshMemberCode", "按钮应调 refreshMemberCode，实得 " + m.group(1)
    assert "refreshMemberCode(" in JS, "refreshMemberCode 未定义（点了就是 ReferenceError）"


# ── 单一渲染出口 ────────────────────────────────────────────────────
def test_member_rendering_hangs_on_the_single_exit():
    # 3 处＝定义 + 失败分支清空 + 正常分支渲染（多出口＝总有一条路径漏渲染，所以数目要钉死）
    assert JS.count("renderMemberQr(") == 3, \
        "renderMemberQr 应为 定义+失败分支+正常分支 共 3 次，实得 %d" % JS.count("renderMemberQr(")
    assert JS.count("renderMembers(") == 3, \
        "renderMembers 应为 定义+失败分支+正常分支 共 3 次，实得 %d" % JS.count("renderMembers(")
    body = _single_func_body(JS, "applyHubStatus")
    for fn in ("renderMemberQr(", "renderMembers(", "hubMemberExp"):
        assert fn in body, "统一渲染出口漏了 %s" % fn


def test_member_qr_reuses_owner_payload_prefix():
    """载荷前缀必须与 owner 码相同：角色由 hub 查表决定，不写进载荷（载荷无签名，写了也能被改）。"""
    render = _single_func_body(JS, "renderMemberQr")
    assert "BIND_PAYLOAD_PREFIX + code" in render, "成员码二维码没用同一个前缀常量"
    # 数**带引号的字面量**：注释里也会出现 HUJIAN-BIND 字样，裸数会把注释算进去
    assert JS.count("'HUJIAN-BIND:1:'") == 1, \
        "前缀字面量只能有一处（常量），实得 %d" % JS.count("'HUJIAN-BIND:1:'")


# ── 纯函数真跑 ──────────────────────────────────────────────────────
def test_members_text_really_runs_in_node():
    _node(_single_func_body(JS, "membersReadFailed") + _single_func_body(JS, "membersText") + """
const cases = [
  [null, '—'],
  [{enabled:false}, '—'],
  [{enabled:true, membersSupported:false}, '云端版本过旧，暂不支持'],
  [{enabled:true, membersSupported:true, members:[]}, '只有你一人'],
  [{enabled:true, membersSupported:true, membersMax:8, members:[{openidMasked:'oFa…02',at:1}]}, '1 / 8 人'],
  [{enabled:true, membersSupported:true, membersMax:8, members:Array.from({length:8},(_,i)=>({openidMasked:'o'+i,at:1}))}, '8 / 8 人'],
  // 上限以服务端回的为准（hub 改了上限后，兜底的 8 只是兜底）
  [{enabled:true, membersSupported:true, membersMax:6, members:[{openidMasked:'o',at:1}]}, '1 / 6 人'],
  // 网络失败 ≠ 版本过旧：两句话必须分开，否则用户会去升级云端修一个网络问题
  [{enabled:true, membersSupported:true, lastOpError:'members_unavailable', members:[]}, '读取失败，稍后重试'],
  [{enabled:true, membersSupported:true, lastOpError:'members_rejected', members:[]}, '读取失败，稍后重试'],
];
for (const [info, want] of cases) {
  const got = membersText(info);
  if (got !== want) { console.log('FAIL', JSON.stringify(info), '->', got, 'want', want); process.exit(1) }
}
// 成员类的其它错误码（踢人失败等）不该被说成"读取失败"
if (membersText({enabled:true, membersSupported:true, lastOpError:'member_remove_failed', members:[]}) !== '只有你一人') {
  console.log('FAIL 非读取类错误不该改成员计数文案'); process.exit(1)
}
console.log('OK');
""")


def test_render_members_really_runs_in_node():
    """渲染真跑：满员禁用、老 hub 禁用、成员行带掩码与「移除」、读不到状态显示破折号。"""
    _node(_single_func_body(JS, "membersReadFailed") + _single_func_body(JS, "ttlMinutes")
          + _single_func_body(JS, "membersText") + _single_func_body(JS, "renderMembers") + """
const els = {};
function mk(id) {
  return els[id] = els[id] || {
    id, textContent: '', hidden: false, disabled: false, title: '', className: '',
    children: [], attrs: {},
    appendChild(c) { this.children.push(c) },
    setAttribute(k, v) { this.attrs[k] = v }
  };
}
const document = {
  getElementById: (id) => mk(id),
  createElement: () => mk('created-' + Math.random().toString(36).slice(2))
};
let bad = 0;
function want(c, m) { if (!c) { console.log('FAIL ' + m); bad++; } }
function reset() { for (const k of Object.keys(els)) delete els[k]; }

// ① 只有主人
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [] });
want(els.hubMembersCount.textContent === '只有你一人', '计数文案: ' + els.hubMembersCount.textContent);
want(els.hubMembersEmpty.hidden === false, '空态提示应可见');
want(els.addMemberBtn.disabled === false, '未满员应可点');
want(els.hubMembers.children.length === 0, '没有成员就不该有行');

// ② 一个成员：行里有掩码 + 「改名」+「移除」（v1.7.54 起 3 个子元素）
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8,
                members: [{ mid: 'a'.repeat(12), openidMasked: 'oFa…02', at: 1 }] });
want(els.hubMembers.children.length === 1, '应渲染 1 行，实得 ' + els.hubMembers.children.length);
const row = els.hubMembers.children[0];
want(row.children.length === 3, '一行应有 名称 + 改名 + 移除，实得 ' + row.children.length);
want(row.children[0].textContent === 'oFa…02', '掩码没渲染: ' + row.children[0].textContent);
want(row.children[1].textContent === '改名', '改名按钮文案: ' + row.children[1].textContent);
want(row.children[2].textContent === '移除', '按钮文案: ' + row.children[2].textContent);
want(row.children[2].attrs['data-mid'] === 'a'.repeat(12), 'data-mid 缺失（点了没法踢）');
want(typeof row.children[2].onclick === 'function', '移除按钮没接 onclick');
want(typeof row.children[1].onclick === 'function', '改名按钮没接 onclick');
want(els.hubMembersEmpty.hidden === true, '有成员时空态提示应隐藏');

// ③ 满员：按钮禁用且说清上限
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8,
                members: Array.from({ length: 8 }, (_, i) => ({ mid: 'm' + i, openidMasked: 'o' + i, at: 1 })) });
want(els.addMemberBtn.disabled === true, '满员必须禁用按钮');
want(els.addMemberBtn.title.includes('已满'), '禁用原因要写清: ' + els.addMemberBtn.title);
want(els.hubMembersCount.textContent === '8 / 8 人', '计数: ' + els.hubMembersCount.textContent);

// ④ 老 hub：禁用 + 说清是云端版本旧（不是"读取失败"）
reset();
renderMembers({ enabled: true, membersSupported: false, members: [] });
want(els.addMemberBtn.disabled === true, '老 hub 必须禁用');
want(els.hubMembersCount.textContent === '云端版本过旧，暂不支持', '文案: ' + els.hubMembersCount.textContent);
want(!/读取失败/.test(els.hubMembersEmpty.textContent), '不得显示成"读取失败"（会让人以为云通道坏了）');

// ⑤ 读不到状态（未启用/失败）：破折号 + 禁用，且不得残留上一次的成员行
reset();
renderMembers(null);
want(els.hubMembersCount.textContent === '—', '读不到时应为破折号: ' + els.hubMembersCount.textContent);
want(els.addMemberBtn.disabled === true, '读不到时应禁用');
want(els.hubMembers.children.length === 0, '不得残留成员行');

// ⑥ 成员读取失败（网络）：说清是读取失败、不是版本过旧，且按钮仍可点（瞬时故障不该像功能没了）
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [],
                lastOpError: 'members_unavailable' });
want(els.hubMembersCount.textContent === '读取失败，稍后重试', '文案: ' + els.hubMembersCount.textContent);
want(/读取失败/.test(els.hubMembersEmpty.textContent), '空态要说清读取失败: ' + els.hubMembersEmpty.textContent);
want(!/版本过旧/.test(els.hubMembersEmpty.textContent), '不得说成版本过旧');
want(els.addMemberBtn.disabled === false, '瞬时网络失败不该禁用按钮');

// ⑦ 按钮提示里的有效期跟服务端 TTL 走（硬编"10 分钟"在 hub 改 TTL 后就是假话）
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [], memberCodeTtlS: 300 });
want(/5 分钟有效/.test(els.addMemberBtn.title), 'TTL 应取服务端值: ' + els.addMemberBtn.title);
reset();
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [] });
want(/10 分钟有效/.test(els.addMemberBtn.title), '没给 TTL 才回落字面量: ' + els.addMemberBtn.title);

if (bad) { console.log(bad + ' 处不符'); process.exit(1) }
console.log('OK');
""")


# ── 路由等式钉 ──────────────────────────────────────────────────────
def test_member_routes_match_registered_views():
    urls = re.findall(r'url\s*=\s*"([^"]+)"', API)
    for want in ("/api/window_controller_gateway/hub/members",
                 "/api/window_controller_gateway/hub/members/remove"):
        assert want in urls, "api.py 未注册 %s；已注册=%s" % (want, urls)
    # 真值从面板源码抽，不是再抄一遍常量——否则这条钉只证明了常量等于自己
    m = re.search(r"haApi\('([^']+)'", _single_func_body(JS, "removeMember"))
    assert m, "removeMember 里找不到 haApi 调用锚点"
    assert m.group(1) == "/window_controller_gateway/hub/members/remove", \
        "移除路径 %s 与注册路由漂移（漂移即 404，用户只看到'点了没反应'）" % m.group(1)


def test_member_code_request_goes_through_bindcode_route_with_kind():
    body = _single_func_body(JS, "refreshMemberCode")
    m = re.search(r"haApi\('([^']+)',\s*'POST',\s*\{\s*kind:\s*'member'\s*\}\)", body)
    assert m, "refreshMemberCode 必须 POST 到换码路由并带 kind:'member'"
    urls = re.findall(r'url\s*=\s*"([^"]+)"', API)
    assert ("/api" + m.group(1)) in urls, "成员码路径 %s 未在 api.py 注册" % m.group(1)


# ── 文案口径 ────────────────────────────────────────────────────────
def test_copy_rules():
    for s in ("添加家人", "家庭成员", "只有你一人", "移除", "小慧语音"):
        assert s in JS or s in HTML, "缺文案：%s" % s
    assert "Matter" not in HTML and "Matter" not in JS, "文案口径：一律「LoRa 网关」，不得出现 Matter"
    assert "10 分钟" in HTML or "10 分钟" in JS, "成员码有效期必须写清（用户不知道该多快扫）"
