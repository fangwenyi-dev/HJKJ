# -*- coding: utf-8 -*-
"""v1.7.46 钉桩：面板统一渲染出口 `applyHubStatus` 的**行为**（假 DOM 里 node 真跑）。

为什么不能只靠结构钉：删掉 `loadRemoteControl` 的重复定义后，`applyHubStatus` 第一次
成为"页面加载 + 30s 无感刷新"的真实路径（此前它只在点二维码那条 POST 路径可达）。
结构钉只能证明"调用了"，证明不了"渲染对了"——而这条路径现在每次打开页面都走。

做法沿用本仓既有纪律（test_v1743_hub_singleton.py 对 hubGatewayText 的 node 真跑）：
把被测函数**连同它调用的渲染函数**原样抽出来，配一个最小假 document，在 node 里真跑
各种场景，断言用户看到的文本。不打桩被测函数本身（打桩＝把要判的东西判掉）。

⚠️ 维护提醒：`applyHubStatus` 是唯一渲染出口，它每多渲染一块（v1.7.47 加了成员区），
这里的假 DOM 与被抽取的函数集合就要跟着补——否则 node 会以 ReferenceError 崩掉。
这崩得响，是好事：说明"出口又长胖了"这件事不会被静默放过。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")


def _body(name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（应为 1）" % (name, len(hits))
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


# applyHubStatus 会调到的渲染函数，一个都不能少（少了 node 会 ReferenceError 崩）
DEPS = ("hubGatewayText", "hubErrorText", "hubOpErrorText", "hubOpErrorIsTransient",
        "hubErrorLine", "ttlMinutes", "membersReadFailed",
        "membersText", "renderMemberQr", "renderMembers")

HARNESS_HEAD = """
// ── 最小假 DOM：只有面板真实存在的那几个 id 会被返回，其余回 null ──
let _bindCode = '';
const qrCalls = [];
const memberQrCalls = [];
function renderBindQr(code) { qrCalls.push(code); }
const KNOWN = ['hubDot', 'hubStatus', 'hubCode', 'hubGateway', 'hubGwDot', 'hubCodeExp', 'hubError',
               'hubCodeTtl', 'hubMembersCount', 'hubMembersEmpty', 'hubMembers', 'addMemberBtn',
               'hubMemberQr', 'hubMemberQrBox', 'hubMemberCode', 'hubMemberExp', 'hubMemberTip'];
const els = {};
function mk(id) {
  return els[id] = els[id] || {
    id: id, textContent: '', className: '', hidden: false, disabled: false, title: '',
    children: [], attrs: {},
    appendChild: function (c) { this.children.push(c) },
    setAttribute: function (k, v) { this.attrs[k] = v }
  };
}
const document = {
  getElementById: (id) => (KNOWN.indexOf(id) >= 0 ? mk(id) : null),
  createElement: () => ({
    textContent: '', className: '', type: '', children: [], attrs: {}, onclick: null,
    appendChild: function (c) { this.children.push(c) },
    setAttribute: function (k, v) { this.attrs[k] = v }
  })
};
const window = { HjQr: { render: (box, text) => { memberQrCalls.push(text) } } };
"""

HARNESS_TAIL = """
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }
function reset() { for (const k of Object.keys(els)) delete els[k]; qrCalls.length = 0; memberQrCalls.length = 0; _bindCode = ''; }

// 场景 1：正常 + 两台网关 + 码还新鲜 + 一个成员（多网关聚合文案、剩余分钟、成员区都要出来）
reset();
applyHubStatus({
  enabled: true, connected: true, bindCode: '123456', bindCodeExpiresIn: 300,
  bindCodeExpired: false, gatewaySn: 'GW1',
  gateways: [{ sn: 'GW1', deviceCount: 2 }, { sn: 'GW2', deviceCount: 3 }],
  lastError: null,
  memberCode: '654321', memberCodeExpiresIn: 420, memberCodeExpired: false,
  members: [{ mid: 'a'.repeat(12), openidMasked: 'oFa…02', at: 1 }],
  membersCount: 1, membersMax: 8, membersSupported: true, ownerMasked: 'oFa…01'
}, false);
want(els.hubStatus.textContent === '已连接', '连接态: ' + els.hubStatus.textContent);
want(els.hubCode.textContent === '123456', '绑定码: ' + els.hubCode.textContent);
want(els.hubGateway.textContent === '2 台 · 5 个子设备（GW1、GW2）',
     '多网关文案（旧渲染器只会显示单个 SN）: ' + els.hubGateway.textContent);
want(els.hubCodeExp.textContent.indexOf('剩余 5 分钟') === 0, '剩余时间: ' + els.hubCodeExp.textContent);
want(els.hubError.hidden === true && els.hubError.textContent === '', '无故障时原因行必须隐藏');
want(qrCalls.length === 1 && qrCalls[0] === '123456', '应渲染一次二维码: ' + JSON.stringify(qrCalls));
want(els.hubDot.className.indexOf('dot-ok') >= 0, '连接时圆点应是 ok 态: ' + els.hubDot.className);
want(els.hubMembersCount.textContent === '1 / 8 人', '成员计数: ' + els.hubMembersCount.textContent);
want(els.hubMembers.children.length === 1, '应渲染 1 行成员');
want(els.hubMemberQr.hidden === false, '有成员码时应摆出二维码');
want(memberQrCalls.length === 1 && memberQrCalls[0] === 'HUJIAN-BIND:1:654321',
     '成员码载荷必须与 owner 码同格式（小程序才能解析）: ' + JSON.stringify(memberQrCalls));
want(els.hubMemberExp.textContent.indexOf('剩余 7 分钟') === 0, '成员码剩余: ' + els.hubMemberExp.textContent);

// 场景 2：未连接 + 码已过期 + 有故障原因（这正是旧渲染器完全不显示的两块）
reset();
applyHubStatus({
  enabled: true, connected: false, bindCode: '654321', bindCodeExpiresIn: -10,
  bindCodeExpired: true, gatewaySn: '', gateways: [], lastError: null,
  lastOpError: 'bindcode_failed',
  members: [], membersMax: 8, membersSupported: true
}, false);
want(els.hubStatus.textContent === '未连接', '连接态: ' + els.hubStatus.textContent);
want(els.hubCodeExp.textContent.indexOf('已过期') === 0, '过期文案: ' + els.hubCodeExp.textContent);
want(els.hubCodeExp.className.indexOf('expired') >= 0, '过期要有区分样式: ' + els.hubCodeExp.className);
want(els.hubError.hidden === false && els.hubError.textContent.length > 4,
     '故障原因必须显示: ' + JSON.stringify(els.hubError.textContent));
want(els.hubGateway.textContent === '—', '无网关时应为破折号: ' + els.hubGateway.textContent);
want(els.hubMemberQr.hidden === true, '没有成员码时不得摆空白二维码块');

// 场景 3：读取失败（必须与"未启用"分开显示，故障伪装成正常态会让排障从第一步就走错）
reset();
applyHubStatus(null, true);
want(els.hubStatus.textContent === '读取失败', '失败态: ' + els.hubStatus.textContent);
want(els.hubCode.textContent === '------', '失败时码要清空: ' + els.hubCode.textContent);
want(els.hubError.hidden === true, '失败时不得残留上一次的故障原因');
want(qrCalls.length === 1 && qrCalls[0] === '', '失败时必须清掉二维码（残留过期码比不显示更坏）');
want(els.hubMembersCount.textContent === '—', '失败时成员计数应为破折号: ' + els.hubMembersCount.textContent);
want(els.addMemberBtn.disabled === true, '失败时「添加家人」必须禁用');

// 场景 4：未启用（老集成没有这条路由）
reset();
applyHubStatus({ enabled: false }, false);
want(els.hubStatus.textContent === '未启用', '未启用态: ' + els.hubStatus.textContent);
want(els.hubDot.className.indexOf('dot-unknown') >= 0, '未启用应是 unknown 态: ' + els.hubDot.className);
want(els.hubError.hidden === true, '未启用时不显示故障原因');

// 场景 5（2026-10-20 销售面批改判据）：未知**非空**连接码 → 不许摆裸异常类名，
// 但也不许整行藏掉。原判据是"未知码必须不显示"，那是把"不泄露噪声"和"不给任何
// 交代"混成了一件事：客户实际看到的是孤零零一句"未连接"，分不清云端在发布、
// 自家断网还是加载项坏了，只能报障。现判据拆成两条：
//   5a 有值 ⇒ 显示通用兜底，且文案里不得出现原异常文本（噪声仍被挡住）
//   5b 无值 ⇒ 整行隐藏（没故障时不许凭空长出一行红字）
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: 'RuntimeError',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(els.hubError.hidden === false && els.hubError.textContent.length > 4,
     '未知连接码必须给一句交代: ' + JSON.stringify(els.hubError.textContent));
want(els.hubError.textContent.indexOf('RuntimeError') < 0,
     '兜底文案不得回显裸异常类名: ' + els.hubError.textContent);
want(/局域网|本地/.test(els.hubError.textContent) && /不受影响/.test(els.hubError.textContent),
     '兜底要说清"局域网控制不受影响"（否则客户会以为窗也控不了了）: ' + els.hubError.textContent);
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 members: [], membersMax: 8, membersSupported: true }, false);
want(els.hubError.hidden === true && els.hubError.textContent === '',
     '没有故障时错误行必须隐藏: ' + JSON.stringify(els.hubError.textContent));

// 场景 6：老 hub（membersSupported=false）——要说清是云端版本旧，且禁用按钮
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 members: [], membersMax: 8, membersSupported: false }, false);
want(els.hubMembersCount.textContent === '云端版本过旧，暂不支持', '老 hub 文案: ' + els.hubMembersCount.textContent);
want(els.addMemberBtn.disabled === true, '老 hub 必须禁用「添加家人」');
want(els.hubError.hidden === true, '老 hub 不该在错误行喊话（成员区自己说清了）: ' + els.hubError.textContent);

// 场景 7：成员读取失败（网络）≠ 云端版本过旧——两句话必须分开，且按钮不禁用
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 lastOpError: 'members_unavailable',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(els.hubMembersCount.textContent === '读取失败，稍后重试', '网络失败文案: ' + els.hubMembersCount.textContent);
want(/读取失败/.test(els.hubMembersEmpty.textContent), '成员区要说清是读取失败: ' + els.hubMembersEmpty.textContent);
want(!/版本过旧/.test(els.hubMembersCount.textContent + els.hubMembersEmpty.textContent),
     '网络失败不得说成"云端版本过旧"（用户会去升级云端修一个网络问题）');
want(els.addMemberBtn.disabled === false, '瞬时网络失败不该禁用按钮');
want(els.hubError.hidden === false && /读取失败/.test(els.hubError.textContent),
     '错误行要给出成员读取失败的原因: ' + els.hubError.textContent);

// 场景 7b：读取失败**但手上还留着上次成功的列表**——不许同屏自相矛盾。
// 此前占位行被强制显示"成员列表读取失败"，而下面照常渲染出成员行（还带可用的「移除」
// 按钮），人数也被"读取失败"顶掉 ⇒ 用户既看到失败又看到名单，还不知道家里有几个人。
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 lastOpError: 'members_unavailable',
                 members: [{ mid: 'a'.repeat(12), openidMasked: 'oFa…02', at: 1 },
                           { mid: 'b'.repeat(12), openidMasked: 'oFa…03', at: 2 }],
                 membersCount: 2, membersMax: 8, membersSupported: true }, false);
want(els.hubMembersCount.textContent === '2 / 8 人（列表可能已过期）',
     '有陈旧列表时人数不该被"读取失败"顶掉: ' + els.hubMembersCount.textContent);
want(els.hubMembersEmpty.hidden === true,
     '占位行必须隐藏：下面正渲染着 2 行成员，再叠一句"读取失败"就是同屏自相矛盾');
want(els.hubMembers.children.length === 2,
     '陈旧的成员行仍应照常渲染（比一片空白更有用）: ' + els.hubMembers.children.length);
want(els.addMemberBtn.disabled === false, '瞬时网络失败不该禁用按钮');

// 场景 8：操作类错误优先于连接类；hub 的真实 err（no_owner）必须有自己的话
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: 'identity_rejected', lastOpError: 'no_owner',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(/还没有主人/.test(els.hubError.textContent), 'no_owner 必须指路"先自己扫码成为主人": ' + els.hubError.textContent);
want(!/重新注册/.test(els.hubError.textContent), '操作类错误应优先显示: ' + els.hubError.textContent);

// 场景 9：只有连接类错误时照常显示（操作类槽空不得把它吃掉）
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: 'identity_rejected_loop', lastOpError: null,
                 members: [], membersMax: 8, membersSupported: true }, false);
want(/反复拒绝/.test(els.hubError.textContent), '连接类错误仍要显示: ' + els.hubError.textContent);

// 场景 10：有效期文案跟服务端 TTL 走（硬编"10 分钟"在 hub 改 TTL 后就是假话）
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 200,
                 bindCodeExpired: false, bindCodeTtlS: 300, memberCodeTtlS: 300,
                 gatewaySn: 'GW1', gateways: [], lastError: null,
                 memberCode: '222222', memberCodeExpiresIn: 200, memberCodeExpired: false,
                 members: [], membersMax: 8, membersSupported: true }, false);
want(els.hubCodeTtl.textContent === '5 分钟', 'TTL 文案应取服务端值: ' + els.hubCodeTtl.textContent);
want(/5 分钟内有效/.test(els.hubMemberTip.textContent), '成员码提示应取服务端 TTL: ' + els.hubMemberTip.textContent);
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 200,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 members: [{ mid: 'a'.repeat(12), openidMasked: 'oFa…02', at: 1 }],
                 membersMax: 6, membersSupported: true }, false);
want(els.hubCodeTtl.textContent === '10 分钟', '服务端没给 TTL 才回落字面量: ' + els.hubCodeTtl.textContent);
want(els.hubMembersCount.textContent === '1 / 6 人',
     '成员上限也要取服务端值（hub 改了上限后 8 就是假话）: ' + els.hubMembersCount.textContent);

// ── v1.9.3 错误行优先级（瞬态类让位连接类）与"未同步"态 ──────────────────
const NET_RAW = 'Cannot connect to host huijian-hub-x:443 ssl:default [DNS server returned answer with no data]';

// 场景 11（本缺陷主案）：断连 + 粘滞的成员读取失败 ⇒ 主句必须是连接类，
// 因为唯一说得出"可能是本机网络不通、局域网不受影响、本机会自动重试"的是那一句。
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: NET_RAW, lastOpError: 'members_unavailable',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(/本机网络不通/.test(els.hubError.textContent),
     '断连时主句必须是连接类文案（说得出本机网络不通）: ' + els.hubError.textContent);
want(!/家庭成员读取失败/.test(els.hubError.textContent),
     '瞬态成员错误在断连时不得再当主因: ' + els.hubError.textContent);
want(!/云端暂不可达/.test(els.hubError.textContent),
     '主句不得把方向指到云端（现网取证过：云端 27 天没重启，坏的是本机解析）');

// 场景 12（反向半条）：同一份 op 值、长连是通的 ⇒ 必须照常显示成员失败，
// 不许把"让位"做成"断连才管、通则一起藏"之外的第三种（那是把真实错误也藏了）。
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: null, lastOpError: 'members_unavailable',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(/读取失败/.test(els.hubError.textContent),
     '长连通时成员读取失败仍是主句（错误真实，不许藏）: ' + els.hubError.textContent);

// 场景 13（语义类不许被降级）：断连时 no_owner 仍排第一——重试永远不会成功的话
// 必须比"本机会自动重试"先说，否则用户会一直点。
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: NET_RAW, lastOpError: 'no_owner',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(/还没有主人/.test(els.hubError.textContent),
     '语义类操作错误在断连时也必须优先: ' + els.hubError.textContent);

// 场景 14（v1.9.4 收紧）：退避每一圈开头 lastError 会被清空——这一窗里既不能空屏，
// 也不许把瞬态类原文当主句顶上来（那正是"叫用户等云端"的那句）。主句必须是连接类兜底。
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: null, lastOpError: 'members_unavailable',
                 members: [], membersMax: 8, membersSupported: true }, false);
want(els.hubError.hidden === false && !!els.hubError.textContent,
     'lastError 为空的退避窗里不得空屏');
want(/本机网络不通|云端暂时连不上/.test(els.hubError.textContent),
     '该窗的主句必须是连接类兜底: ' + els.hubError.textContent);
want(!/家庭成员读取失败/.test(els.hubError.textContent),
     '该窗不得把瞬态类原文当主因: ' + els.hubError.textContent);

// 场景 16（v1.9.4 反向半条）：一切正常时不许凭空喊"连不上"。
// tier3 写成无条件兜底就会在这里挂出一行红字。
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: null, lastOpError: null,
                 members: [], membersMax: 8, membersSupported: true,
                 membersLoadedAt: 1793695483 }, false);
want(els.hubError.hidden === true && els.hubError.textContent === '',
     '无故障时错误行必须整条隐藏: ' + JSON.stringify(els.hubError.textContent));

// 场景 17（v1.9.4 文案类面）：长连通着时瞬态类照常是主句（错误真实），
// 但那句话**不许指认云端坏了**——现网取证过云端 27 天没重启、坏的是本机解析。
for (const op of ['members_unavailable', 'bindcode_failed', 'member_remove_failed']) {
  reset();
  applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                   bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                   lastError: null, lastOpError: op,
                   members: [], membersMax: 8, membersSupported: true }, false);
  want(!!els.hubError.textContent && els.hubError.hidden === false,
       '连通时瞬态类仍要显示（op=' + op + '）');
  want(!/不可达|云端坏了|云端暂时/.test(els.hubError.textContent),
       '瞬态类文案不得指认云端故障（op=' + op + '）: ' + els.hubError.textContent);
}

// 场景 15：从未读到过成员 ≠ 只有你一人。空列表在这里的含义是"还不知道"。
reset();
applyHubStatus({ enabled: true, connected: false, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [],
                 lastError: NET_RAW, lastOpError: null,
                 members: [], membersMax: 8, membersSupported: true }, false);
want(!/只有你一人/.test(els.hubMembersCount.textContent + els.hubMembersEmpty.textContent),
     '本进程从未成功读过成员时不得断言"只有你一人": ' + els.hubMembersCount.textContent);
want(/未同步/.test(els.hubMembersCount.textContent),
     '要说清是还没同步: ' + els.hubMembersCount.textContent);
reset();
applyHubStatus({ enabled: true, connected: true, bindCode: '111111', bindCodeExpiresIn: 60,
                 bindCodeExpired: false, gatewaySn: 'GW1', gateways: [], lastError: null,
                 members: [], membersMax: 8, membersSupported: true,
                 membersLoadedAt: 1793695483 }, false);
want(els.hubMembersCount.textContent === '只有你一人',
     '真读到过一次且确实没人时才配说只有你一人: ' + els.hubMembersCount.textContent);

if (bad) { console.log('applyHubStatus 真跑: ' + bad + ' 处不符'); process.exit(1) }
console.log('OK');
"""


def _render_script():
    # renderMemberQr 用到 BIND_PAYLOAD_PREFIX 常量：从源码抽**真值**注入，而不是在测试里
    # 再抄一遍字面量（抄的那份会与实现漂移；且它被 try/catch 包着，缺了会静默不渲染）。
    m = re.search(r"const BIND_PAYLOAD_PREFIX = '([^']+)'", JS)
    assert m, "huijian.js 里找不到 BIND_PAYLOAD_PREFIX 常量（改名了？harness 要跟着改）"
    const = "const BIND_PAYLOAD_PREFIX = '%s';\n" % m.group(1)
    # 同理 harness 必须带上渲染出口引用的兜底常量：applyHubStatus 现在引用
    # HUB_CONN_GENERIC_TEXT，harness 里没有它会让 node 直接 ReferenceError 崩掉
    # （不是"钉红了"，是量具坏了——所以缺常量时响亮断言，指向该改 harness）。
    g = re.search(r"const HUB_CONN_GENERIC_TEXT =([\s\S]*?);", JS)
    assert g, "huijian.js 里找不到 HUB_CONN_GENERIC_TEXT（改名了？harness 要跟着带上）"
    generic = "const HUB_CONN_GENERIC_TEXT =%s;\n" % g.group(1).strip()
    # 同理：瞬态类操作码的名单也是出口引用的常量，缺了 node 直接 ReferenceError
    t = re.search(r"const HUB_TRANSIENT_OP_ERRORS = (\[[^\]]*\]);", JS)
    assert t, "huijian.js 里找不到 HUB_TRANSIENT_OP_ERRORS（改名了？harness 要跟着带上）"
    transient = "const HUB_TRANSIENT_OP_ERRORS = %s;\n" % t.group(1)
    return (HARNESS_HEAD + const + generic + transient + "\n".join(_body(n) for n in DEPS)
            + "\n" + _body("applyHubStatus") + "\n" + HARNESS_TAIL)


def _run_node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑渲染出口")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "render.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "applyHubStatus 真跑失败：\n%s%s" % (r.stdout, r.stderr)


def test_apply_hub_status_renders_every_state_in_node():
    _run_node(_render_script())


def test_single_gateway_copy_still_shows_sn():
    """一台网关时沿用旧面板"一眼看到本机网关"的好处：直接给 SN · N 个子设备。"""
    _run_node(_body("hubGatewayText") + """
const one = hubGatewayText({ gateways: [{ sn: 'GW1', deviceCount: 4 }], gatewaySn: 'GW1' });
if (one !== 'GW1 · 4 个子设备') { console.log('FAIL ' + one); process.exit(1) }
const legacy = hubGatewayText({ gatewaySn: 'GW-OLD' });      // 老集成只回 gatewaySn
if (legacy !== 'GW-OLD') { console.log('FAIL legacy ' + legacy); process.exit(1) }
const none = hubGatewayText({});
if (none !== '—') { console.log('FAIL none ' + none); process.exit(1) }
console.log('OK');
""")
