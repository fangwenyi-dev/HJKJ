# -*- coding: utf-8 -*-
"""v1.7.54 家庭成员「改名」：称呼只存本机面板侧，键是 hub 给的 mid。

背景（核过三仓，不是猜的）：hub 的成员记录字面上就是 `{openid, at}` 两个字段，
全仓没有 nickname / avatar / 昵称 任何概念；小程序侧也从来没有成员列表（它只有
"我的角色：主人/家人"一句话）。所以面板上那行 `oeh…zS` 是用户唯一的线索——
那是掩码 openid，对家人来说等于不可读。

三条纪律决定了这批测试长什么样：
  · **本地存的是称呼，不是成员关系**——api.py 那条"成员关系真相在云端、本地不留副本"
    仍然成立：`self.members` 保持 hub 原样透传，alias 只并进渲染视图；
  · 面板是自由文本的入口 ⇒ 渲染一律 textContent，且两侧规范化必须同口径
    （跨语言两份实现，用同一批用例逐条对账，不靠"看起来一样"）；
  · 结构钉只证明"调用了" ⇒ 渲染与规范化都在 node 里真跑，不打桩被测函数。
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from custom_components.window_controller_gateway import api, hub_client as hc

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
HTML = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")
PY = (ROOT / "custom_components" / "window_controller_gateway" / "hub_client.py").read_text(encoding="utf-8")
API = (ROOT / "custom_components" / "window_controller_gateway" / "api.py").read_text(encoding="utf-8")

MID_A = "a" * 12
MID_B = "b" * 12


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


def _client(tmp_path, members=None):
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id, c._secret = "inst-1", "sec-1"
    c.connected = True
    c.members = members if members is not None else [
        {"mid": MID_A, "openidMasked": "oeh…zS", "at": 1},
        {"mid": MID_B, "openidMasked": "oFa…02", "at": 2},
    ]
    return c


def _rows(client):
    return {m["mid"]: m for m in client.status_view()["members"]}


# ── 规范化：跨语言逐条对账 ──────────────────────────────────────────
ALIAS_CASES = [
    "爸爸",
    "  妈妈  ",
    "\n奶奶\t",
    "a\u0000b\u001fc\u007fd",       # 控制符换成空格（不是删掉：删了会把 "a b" 粘成 "ab"）
    "",
    "   ",
    "一二三四五六七八九十一二三",   # 超上限：两侧都必须截到同一个长度
    "  首尾各留一个空格 ",
    "带 空格 的名字",
]


def test_max_len_is_one_value_written_twice_and_pinned():
    """上限在 Python 与 JS 各写一份（面板要先截一次），漂移＝用户看到存进去的名字比填的短。"""
    py_n = re.search(r"MEMBER_ALIAS_MAX_LEN = (\d+)", PY)
    js_n = re.search(r"const MEMBER_ALIAS_MAX = (\d+);", JS)
    assert py_n and js_n, "抽取锚点漂移：任一侧不再以字面量写上限"
    assert py_n.group(1) == js_n.group(1), \
        "称呼上限两侧不一致：Python=%s JS=%s" % (py_n.group(1), js_n.group(1))


def test_python_and_js_normalize_identically():
    """同一批用例喂两侧，逐条比结果——"看着一样"不是证据。"""
    js_max = re.search(r"const MEMBER_ALIAS_MAX = (\d+);", JS).group(1)
    script = ("const MEMBER_ALIAS_MAX = %s;\n" % js_max) + _single_func_body(JS, "cleanMemberAlias") + """
const cases = %s;
console.log('OK ' + JSON.stringify(cases.map(cleanMemberAlias)));
""" % json.dumps(ALIAS_CASES, ensure_ascii=False)
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "n.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0 and "OK " in r.stdout, "node 真跑失败：%s%s" % (r.stdout, r.stderr)
    got = json.loads(r.stdout.split("OK ", 1)[1].strip())
    want = [hc.clean_member_alias(c) for c in ALIAS_CASES]
    for raw, g, w in zip(ALIAS_CASES, got, want):
        assert g == w, "规范化两侧不一致：入参=%r JS=%r PY=%r" % (raw, g, w)


def test_clean_member_alias_clamps_length_and_drops_non_str():
    long_name = "一二三四五六七八九十" + "一二三"        # 13 个字，上限 12
    assert hc.clean_member_alias(long_name) == "一二三四五六七八九十" + "一二"
    assert len(hc.clean_member_alias("名" * 40)) == hc.MEMBER_ALIAS_MAX_LEN
    for bad in (None, 5, True, ["爸爸"], {"a": 1}):
        assert hc.clean_member_alias(bad) == "", "非字符串入参必须回空串（%r）" % (bad,)


# ── 落盘 ────────────────────────────────────────────────────────────
def test_alias_file_round_trip(tmp_path):
    hc.save_member_aliases(str(tmp_path), {MID_A: "爸爸"})
    assert hc.load_member_aliases(str(tmp_path)) == {MID_A: "爸爸"}


def test_missing_or_broken_alias_file_is_no_alias(tmp_path):
    assert hc.load_member_aliases(str(tmp_path)) == {}, "没有文件＝没起过名字，不是错误"
    (tmp_path / hc.HUB_MEMBER_ALIAS_FILE).write_text("{不是 json", encoding="utf-8")
    assert hc.load_member_aliases(str(tmp_path)) == {}, "坏文件也不许抛（面板不能因此白屏）"
    (tmp_path / hc.HUB_MEMBER_ALIAS_FILE).write_text("[1,2]", encoding="utf-8")
    assert hc.load_member_aliases(str(tmp_path)) == {}, "顶层不是对象一律按空表"


def test_dirty_entries_dropped_on_load(tmp_path):
    """手工编辑/半写损坏的脏条目不能混进内存：值非字符串会一路穿到面板渲染。"""
    (tmp_path / hc.HUB_MEMBER_ALIAS_FILE).write_text(json.dumps({
        MID_A: "爸爸", MID_B: 50, "": "没键名的", MID_A + "x": "   ", "c": None,
    }), encoding="utf-8")
    assert hc.load_member_aliases(str(tmp_path)) == {MID_A: "爸爸"}


def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    hc.save_member_aliases(str(tmp_path), {MID_A: "爸爸"})
    left = [p.name for p in tmp_path.iterdir()]
    assert left == [hc.HUB_MEMBER_ALIAS_FILE], "残留临时文件＝上次写入没清干净：%s" % left


# ── set_member_alias 行为 ───────────────────────────────────────────
def test_rename_writes_file_and_shows_in_view(tmp_path):
    client = _client(tmp_path)
    assert asyncio.run(client.set_member_alias(MID_A, "  爸爸  ")) is True
    assert hc.load_member_aliases(str(tmp_path)) == {MID_A: "爸爸"}
    assert _rows(client)[MID_A]["alias"] == "爸爸"
    assert "alias" not in _rows(client)[MID_B], "没起过名字的行不得凭空多出 alias 键"


def test_rename_never_touches_hub(tmp_path):
    """改名是纯本机操作：多打一次云端就是白吃一次限流（hub 根本没有名称字段）。"""
    client = _client(tmp_path)

    async def never(*a, **kw):
        raise AssertionError("改名不该调 hub")

    client._http = never
    assert asyncio.run(client.set_member_alias(MID_A, "爸爸")) is True


def test_hub_payload_stays_the_relation_truth(tmp_path):
    """`self.members` 必须还是 hub 原样的那几条——视图里并 alias，内存里不改。

    这条钉的是"本地只存称呼、不存关系"的边界：一旦有人图省事往 self.members 里
    写别名，成员关系就有了第二份副本，与 hub 分叉时没人说得清该信谁。
    """
    client = _client(tmp_path)
    before = [dict(m) for m in client.members]
    asyncio.run(client.set_member_alias(MID_A, "爸爸"))
    assert client.members == before, "self.members 被改了（本地留了副本）"
    assert _rows(client)[MID_A]["alias"] == "爸爸"


def test_blank_name_clears_alias_back_to_mask(tmp_path):
    client = _client(tmp_path)
    asyncio.run(client.set_member_alias(MID_A, "爸爸"))
    assert asyncio.run(client.set_member_alias(MID_A, "   ")) is True
    assert hc.load_member_aliases(str(tmp_path)) == {}, "留空＝清掉，不是存一个空串"
    assert "alias" not in _rows(client)[MID_A]


def test_rename_unknown_mid_is_rejected_and_speaks(tmp_path):
    """只认当前列表里的 mid：HTTP 边界要自己把关，而面板只会给看得见的行挂「改名」。"""
    client = _client(tmp_path)
    assert asyncio.run(client.set_member_alias("f" * 12, "爸爸")) is False
    assert client.last_op_error == "member_rename_rejected"
    assert not (tmp_path / hc.HUB_MEMBER_ALIAS_FILE).exists(), "被拒的请求不许留下文件"
    asyncio.run(client.set_member_alias("", "爸爸"))
    assert client.last_op_error == "member_rename_rejected"


def test_write_failure_does_not_pretend_to_have_remembered(tmp_path, monkeypatch):
    """落盘失败就不许改内存：否则面板当场显示新名字、重启后打回掩码。"""
    client = _client(tmp_path)

    def boom(config_dir, mapping):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(hc, "save_member_aliases", boom)
    assert asyncio.run(client.set_member_alias(MID_A, "爸爸")) is False
    assert client.member_aliases == {}, "写失败了还在内存里假装记住了"
    assert client.last_op_error == "member_rename_failed"


def test_alias_survives_client_recreation(tmp_path):
    asyncio.run(_client(tmp_path).set_member_alias(MID_A, "爸爸"))
    fresh = _client(tmp_path)                      # 等同 HA 重启后新建的 hub 单例
    assert asyncio.run(fresh._load_aliases()) is None
    assert _rows(fresh)[MID_A]["alias"] == "爸爸"


def test_alias_keyed_on_mid_survives_rebind(tmp_path):
    """mid 只随微信账号走（hub 侧 = sha256(openid) 前 12 位）：踢掉再扫回来还是那个人。"""
    client = _client(tmp_path)
    asyncio.run(client.set_member_alias(MID_A, "爸爸"))
    client.members = [{"mid": MID_A, "openidMasked": "oeh…zS", "at": 99}]   # 重新绑定
    assert _rows(client)[MID_A]["alias"] == "爸爸"


def test_status_view_counts_rows_it_returned(tmp_path):
    """membersCount 必须与 members 同源，否则非 dict 条目会让"3 / 8 人"和行数对不上。"""
    client = _client(tmp_path)
    client.members = [{"mid": MID_A, "openidMasked": "o", "at": 1}, "脏条目", None]
    view = client.status_view()
    assert len(view["members"]) == view["membersCount"] == 1


# ── api 层 ──────────────────────────────────────────────────────────
_NO_BODY = object()


class _FakeClient:
    def __init__(self, rename_ok=True):
        self.calls = []
        self.rename_ok = rename_ok

    async def set_member_alias(self, mid, name):
        self.calls.append(("set_member_alias", mid, name))
        return self.rename_ok

    async def remove_member(self, mid):
        self.calls.append(("remove_member", mid))
        return True

    async def list_members(self):
        self.calls.append(("list_members",))
        return True

    def status_view(self):
        return {
            "connected": True, "instanceId": "inst-1", "bindCode": "162193",
            "bindCodeExpiresIn": 120, "bindCodeExpired": False, "gatewaySn": "GW1",
            "gateways": [{"sn": "GW1", "deviceCount": 2}], "hub": "https://hub",
            "lastError": None, "lastOpError": None, "memberCode": "654321",
            "members": [{"mid": MID_A, "openidMasked": "oeh…zS", "at": 1, "alias": "爸爸"}],
            "membersCount": 1, "membersMax": 8, "membersSupported": True,
            "ownerMasked": "oFa…01",
        }


class _FakeHass:
    def __init__(self, client=None):
        from custom_components.window_controller_gateway.const import DOMAIN, HUB_DATA_KEY
        self.data = {DOMAIN: ({} if client is None else {HUB_DATA_KEY: client})}
        self.registered = []
        self.http = self

    def register_view(self, view):
        self.registered.append(view)


class _FakeRequest:
    def __init__(self, hass, payload=_NO_BODY):
        self.app = {"hass": hass}
        self._payload = payload

    async def json(self):
        if self._payload is _NO_BODY:
            raise ValueError("no body")
        return self._payload


def _view(cls, captured):
    v = cls()
    v.json = lambda payload: (captured.update(payload), payload)[1]
    return v


def test_rename_route_registered():
    urls = re.findall(r'url\s*=\s*"([^"]+)"', API)
    assert "/api/window_controller_gateway/hub/members/rename" in urls, urls
    hass = _FakeHass()
    api.async_setup_api(hass)
    registered = {getattr(v, "url", None) for v in hass.registered}
    assert "/api/window_controller_gateway/hub/members/rename" in registered, \
        "只定义不注册＝没接线（面板点了就是 404）"


def test_rename_view_passes_mid_and_name():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRenameView, out).post(
        _FakeRequest(_FakeHass(client), {"mid": MID_A, "name": "爸爸"})))
    assert client.calls == [("set_member_alias", MID_A, "爸爸")], client.calls
    assert out["renameOk"] is True and out["enabled"] is True


def test_rename_view_reports_rejection():
    client = _FakeClient(rename_ok=False)
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRenameView, out).post(
        _FakeRequest(_FakeHass(client), {"mid": MID_A, "name": "爸爸"})))
    assert out["renameOk"] is False, "没保存必须如实回 False（假成功比失败更坏）"


def test_rename_view_without_client_or_body():
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRenameView, out).post(_FakeRequest(_FakeHass())))
    assert out == {"enabled": False, "renameOk": False}, out
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRenameView, out).post(_FakeRequest(_FakeHass(client))))
    assert client.calls == [("set_member_alias", "", None)], \
        "无体时必须原样把空 mid/None 交给客户端去拒，而不是在视图里编一个（%s）" % client.calls


@pytest.mark.parametrize("cls,post_body", [
    (api.WindowGatewayHubMemberRenameView, {"mid": MID_A, "name": "爸爸"}),
    (api.WindowGatewayHubMemberRemoveView, {"mid": MID_A}),
])
def test_member_op_views_return_the_full_status_view(cls, post_body):
    """成员写操作的回包必须是完整视图。

    喂窄包（只回 members）时，面板唯一渲染出口会把 connected/bindCode/memberCode 当缺失
    ⇒ 用户点完「移除」，面板就在一张刚还在倒计时的有效码旁边显示"未连接 / ------"。
    """
    client = _FakeClient()
    out = {}
    asyncio.run(_view(cls, out).post(_FakeRequest(_FakeHass(client), post_body)))
    for key in ("connected", "bindCode", "bindCodeExpiresIn", "memberCode", "gateways"):
        assert key in out, "回包缺 %s：面板会把它渲染成「未连接 / 无码」（%s）" % (key, cls.__name__)


def test_member_op_views_read_the_singleton_not_entries():
    for cls in (api.WindowGatewayHubMemberRenameView, api.WindowGatewayHubMemberRemoveView):
        names = set(cls.post.__code__.co_names)
        assert "_hub_client" in names, "%s 没走 _hub_client 单例取值" % cls.__name__
        assert "async_entries" not in names, "%s 不得遍历条目" % cls.__name__


# ── 面板：DOM / 渲染真跑 ────────────────────────────────────────────
def test_alias_note_dom_and_css():
    card = HTML.split('id="remoteCard"', 1)[1]
    assert 'id="hubMemberNote"' in card, "称呼说明不在「远程控制」卡内"
    assert "不上传云端" in HTML, "必须说清称呼只存本机（换 HA 安装要重起，别让人以为会同步）"
    assert re.search(r"\.hub-member-note\s*\{", CSS), "缺 .hub-member-note 样式"
    assert re.search(r"\.hub-member-list li \.hub-member-name\s*\{", CSS), \
        "缺 .hub-member-name 排版（长名字会把「移除」挤出卡外）"


def test_render_members_alias_really_runs_in_node():
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

// ① 起了名字：显示名字，掩码退到 title（排障时仍认得出是谁）
mk('hubMembers'); mk('hubMembersEmpty'); mk('hubMembersCount'); mk('addMemberBtn');
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [
  { mid: 'aaaaaaaaaaaa', openidMasked: 'oeh…zS', at: 1, alias: '爸爸' }] });
const named = els.hubMembers.children[0].children;
want(named[0].textContent === '爸爸', '称呼没渲染: ' + named[0].textContent);
want(/oeh…zS/.test(named[0].title), '有称呼时 title 要还留得下掩码: ' + named[0].title);
want(named[1].textContent === '改名', '第二个按钮应是改名: ' + named[1].textContent);
want(named[1].attrs['data-mid'] === 'aaaaaaaaaaaa', '改名按钮缺 data-mid');
want(typeof named[1].onclick === 'function', '改名按钮没接 onclick');

// ② 没起名字：回落掩码（与 v1.7.47 行为一致），且 title 指路怎么改名
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [
  { mid: 'bbbbbbbbbbbb', openidMasked: 'oFa…02', at: 1 }] });
const plain = els.hubMembers.children[1].children;
want(plain[0].textContent === 'oFa…02', '没称呼应显示掩码: ' + plain[0].textContent);
want(/改名/.test(plain[0].title), '没称呼时 title 要指路: ' + plain[0].title);

// ③ 称呼是用户手打的自由文本 ⇒ 只能按文本渲染，含标签/换行都不许变成结构
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [
  { mid: 'cccccccccccc', openidMasked: 'o', at: 1, alias: '<img src=x onerror=alert(1)>' }] });
const evil = els.hubMembers.children[2].children;
want(evil[0].textContent === '<img src=x onerror=alert(1)>', '称呼被改写了: ' + evil[0].textContent);
want(evil[0].children.length === 0, '称呼里长出了子元素（等于把文本当 HTML 解析了）');

// ④ 空串/非字符串 alias 都按"没起名字"处理，不许渲染成空白行
renderMembers({ enabled: true, membersSupported: true, membersMax: 8, members: [
  { mid: 'dddddddddddd', openidMasked: 'oD…9', at: 1, alias: '' },
  { mid: 'eeeeeeeeeeee', openidMasked: 'oE…9', at: 1, alias: 5 }] });
want(els.hubMembers.children[3].children[0].textContent === 'oD…9', '空称呼没回落掩码');
want(els.hubMembers.children[4].children[0].textContent === 'oE…9', '非字符串称呼没回落掩码');

if (bad) { console.log(bad + ' 处不符'); process.exit(1) }
console.log('OK');
""")


def test_member_row_render_never_uses_innerhtml():
    """真跑之外再补一道语法闸：innerHTML 一旦混进来，称呼就是注入点。"""
    body = _single_func_body(JS, "renderMembers")
    assert "innerHTML" not in body, "成员行不得使用 innerHTML（称呼是用户输入的自由文本）"
    assert "textContent" in body


def test_rename_member_posts_to_registered_route():
    body = _single_func_body(JS, "renameMember")
    m = re.search(r"haApi\('([^']+)',\s*\n?\s*'POST',\s*\{\s*mid:\s*mid,\s*name:\s*name\s*\}\)", body)
    assert m, "renameMember 必须 POST 到改名路由并带 {mid, name}"
    urls = re.findall(r'url\s*=\s*"([^"]+)"', API)
    assert ("/api" + m.group(1)) in urls, "改名路径 %s 未在 api.py 注册（点了就是 404）" % m.group(1)
    assert "applyHubStatus(" in body, "改名后必须走统一渲染出口，别另起一份渲染"


def test_rename_cancel_does_nothing():
    body = _single_func_body(JS, "renameMember")
    assert re.search(r"raw === null\) return", body), \
        "点「取消」必须直接返回（否则取消＝把称呼清成空串）"


def test_rename_error_codes_have_copy_on_both_sides():
    """加载项会产生的改名类错误码，面板必须有中文文案（漏一个＝用户点了没反应）。"""
    body = _single_func_body(JS, "hubOpErrorText")
    for code in ("member_rename_failed", "member_rename_rejected"):
        assert '"%s"' % code in PY, "加载项已不再产生 %s（映射表在验死码）" % code
        assert "'%s'" % code in body, "hubOpErrorText 漏映射 %s" % code
        s = re.search(r"case '%s':\s*\n?\s*return '([^']+)'" % code, body)
        assert s and len(s.group(1)) >= 4 and code not in s.group(1), \
            "%s 的文案不得落回原码（等于没映射）" % code


def test_rename_errors_do_not_pollute_member_count_copy():
    """改名失败不能被说成"成员读取失败"：那是两件事，混了用户会去等云端恢复。"""
    _node(_single_func_body(JS, "membersReadFailed") + """
for (const i of [{lastOpError:'member_rename_failed'}, {lastOpError:'member_rename_rejected'}]) {
  if (membersReadFailed(i) !== false) { console.log('FAIL 改名类错误不该判成读取失败', JSON.stringify(i)); process.exit(1) }
}
console.log('OK');
""")


def test_copy_rules():
    assert "改名" in JS, "缺「改名」按钮文案"
    assert "Matter" not in HTML and "Matter" not in JS, "文案口径：一律「LoRa 网关」，不得出现 Matter"
