# -*- coding: utf-8 -*-
"""面板状态渲染的**行为**钉（把内联函数摘出来交给 node 真跑，不再 `"xx" in html`）。

钉桩动机：本仓四轮复审查到的面板假绿全是同一形制——判据写在服务端或数据里，
前端却自己另判一遍或干脆不认那个字段，而守卫只钉"这串字在 HTML 里出现过"。
字在、分支不到，测试照样绿（v1.6.5 面板延时裁决＝后端 `delay` 字段发出去了、
`www/index.html` 里零个消费点；v1.6.4 徽章＝显示层加了 `available` 门、
`updatable` 与「已最新」pill 照旧吃陈旧快照）。

这里改成量真产物：按函数名摘出定义 → 在 node 里用桩 `pill()` 执行 →
断言**渲染出来的字符串**。判据形制：
  ① 每种状态各自一条正向（该说什么）；
  ② 谎报形各一条反向（不得出现「已最新」/「可升级」/「排程」的错配）；
  ③ 接线钉：渲染循环里必须只剩调用点，不许留第二份判据。
node 缺席即 skip（CI=ubuntu-latest 自带；与 test_web_inline_js_syntax 同口径）。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机无 node，面板行为跑不起来")


def _fn(name: str) -> str:
    """从 www/index.html 摘出 `function name(...) {...}` 整块。

    收束条件是**列首**的 `}`（本仓面板函数一律顶格收尾），所以块内有界：
    函数体里的缩进 `}` 不会提前截断，也不会越界吃到下一个函数。
    """
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    m = re.search(rf"\nfunction {name}\([^\n]*\)\{{.*?\n\}}\n", html, re.S)
    assert m, f"面板里没有顶格的 {name} 定义＝判据没做成单点"
    block = m.group(0)
    assert len(block) < 4000, f"{name} 摘出来的块异常大（正则越界）：{len(block)}"
    return block


def _node_js(body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write("const pill=(t,c)=>`${c}|${t}`;\n" + body)
        path = f.name
    try:
        r = subprocess.run([NODE, path], capture_output=True, text=True,
                           timeout=60, encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, f"node 执行失败：{r.stderr[-500:]}"
    return r.stdout


# ── 刀A：升级徽章只认服务端 fw_state ────────────────────────────────
_BADGE_JS = """
let _otaLatest="2.9.7";
__FN__
const cases=[
  ["updatable",          {fw_state:"updatable",  fw_version:"2.2.6", fw_source:"实时"}, false],
  ["up-to-date 远程",     {fw_state:"up-to-date", fw_version:"2.2.9", fw_source:"实时"}, true],
  ["up-to-date 近场",     {fw_state:"up-to-date", fw_version:"2.2.9", fw_source:"实时"}, false],
  ["no-package",         {fw_state:"no-package", fw_version:"2.2.9", fw_source:"实时"}, true],
  ["unverified 有读数",   {fw_state:"unverified", fw_version:"2.2.6", fw_source:"入驻时"}, true],
  ["unverified 无读数",   {fw_state:"unverified", fw_version:"", fw_source:""}, false],
  ["字段缺席不可判",       {fw_version:"2.2.6", fw_source:"实时"}, true],
];
for(const [label, d, remote] of cases) console.log(label+" ⇒ "+fwBadge(d, remote));
"""


def test_badge_state_table():
    out = _node_js(_BADGE_JS.replace("__FN__", _fn("fwBadge")))
    got = dict(line.split(" ⇒ ", 1) for line in out.strip().splitlines())
    assert got["updatable"] == "warn|可升级→2.9.7", got
    assert got["up-to-date 远程"] == "ok|已最新·可远程"
    assert got["up-to-date 近场"] == "ok|已最新"
    assert got["no-package"] == "mut|无可升级包"
    # 反向钉：出处不是实时的两形，加字段整体缺席那一形，一种文案都不许背两种锅——
    # 既不许谎报「已最新」，也不许摆一个点了必然空转的「可升级」。
    for label in ("unverified 有读数", "unverified 无读数", "字段缺席不可判"):
        assert "已最新" not in got[label], f"{label} 仍在谎报已最新：{got[label]}"
        assert "可升级" not in got[label], f"{label} 仍在给陈旧读数排程：{got[label]}"
    assert "2.2.6" in got["unverified 有读数"], "得把拿不准的那个读数念出来，用户才知道差在哪"
    assert got["字段缺席不可判"] == got["unverified 有读数"], "不可判就落待证，不在前端留第二份判据"


def test_badge_wired_at_single_point():
    """渲染循环里只许留调用点：判据抄第二份＝两处口径可以各说各话。"""
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert html.count("const badge = fwBadge(") == 1, "徽章判定必须只有一个落点"
    loop = re.search(r"\n  for\(const d of devs\)\{.*?\n  \}\n", html, re.S)
    assert loop, "摘不到卫星表渲染循环"
    assert 'pill("已最新' not in loop.group(0), "循环里还留着一份「已最新」判据"
    assert "d.updatable" not in loop.group(0), "循环里还在自己判 updatable"


# ── 刀B：调试面板必须把内核的延时族裁决照说出来 ────────────────────
_DEBUG_JS = """
__FN_DELAY__
__FN_CASCADE__
const cases=[
  ["schedule", {delay:true, delay_action:"schedule", delay_seconds:10,
                delay_body:"关闭办公室射灯",
                final:"（面板预演，不会真排）将排程：10秒后执行「关闭办公室射灯」"}],
  ["nosched",  {delay:true, delay_action:"nosched",
                final:"这句里的时间安排我还把握不准，请一次只说一件"}],
  ["refuse",   {delay:true, delay_action:"refuse",
                final:"删除这类不可逆操作不能无人值守，请现在说一次"}],
  ["cancel",   {delay:true, delay_action:"cancel",
                final:"（面板预演，不会真撤）这句会撤掉面板源在途的 1 条定时指令"}],
  ["未登记档",  {delay:true, delay_action:"wat"}],
  ["快速通道",  {plan:{intent:"TurnDeviceOn", args:{}, source:"t0", trace:["t0"]}}],
  ["纯兜底",    {final:"这句话我还不会"}],
  ["空出参",    {}],
];
for(const [label, c] of cases) console.log("<<"+label+">>"+cascadeDebugLines(c).replace(/\\n/g,"\\\\n"));
"""


def _debug_lines() -> dict:
    js = (_DEBUG_JS.replace("__FN_DELAY__", _fn("delayVerdictLine"))
                    .replace("__FN_CASCADE__", _fn("cascadeDebugLines")))
    out = _node_js(js)
    rows = {}
    for line in out.strip().splitlines():
        m = re.match(r"<<(.+?)>>(.*)", line)
        assert m, f"面板出参行形制没对上（渲染改了形？）：{line[:80]}"
        rows[m.group(1)] = m.group(2)
    assert len(rows) == 8, rows
    return rows


def test_delay_verdict_reaches_the_panel():
    """1.6.5 的洞＝内核把 delay/delay_action 发进 JSON，前端零消费点：延时句被贴
    「快速通道未命中 → 将播固定兜底」。四档各自点名，且一律不得再念那句反话。"""
    got = _debug_lines()
    assert "会排程" in got["schedule"] and "10s" in got["schedule"], got["schedule"]
    assert "关闭办公室射灯" in got["schedule"], "正文必须念出来，否则预演无从核对"
    assert "同一单点" in got["schedule"], "得说清判据来自线上那一版裁决"
    assert "不排程" in got["nosched"]
    assert "拒排" in got["refuse"]
    assert "撤单专令" in got["cancel"] and "不吞别的设备" in got["cancel"]
    for label in ("schedule", "nosched", "refuse", "cancel"):
        assert "将播固定兜底" not in got[label], f"{label} 仍被兜底话术盖过去：{got[label]}"
        assert "快速通道未命中" not in got[label], f"{label} 仍谎报未命中：{got[label]}"


def test_debug_lines_other_arms_untouched():
    """反向保护：非延时句的两条既有分支逐字不变（面板改动不许顺手动别的腿）。"""
    got = _debug_lines()
    assert got["快速通道"].startswith("命中快速通道 → TurnDeviceOn"), got["快速通道"]
    assert got["纯兜底"] == ("快速通道未命中。\\n→ 将播固定兜底：这句话我还不会\\n"), got["纯兜底"]
    assert got["空出参"].startswith("快速通道未命中"), got["空出参"]


def test_unknown_delay_action_names_itself():
    """内核将来加新档位时，面板不得静默走兜底那一支（新形制要响亮现形）。"""
    got = _debug_lines()
    assert "未登记档位 wat" in got["未登记档"], got["未登记档"]
    assert "将播固定兜底" not in got["未登记档"]


def test_debug_renderer_is_single_point():
    """接线钉：onclick 里只许留调用点；三段判据不许在处理器里再抄一份。

    判据串一律取**码形专属**那一串（带反引号/箭头的实参形），不取散文里也会出现的
    词——本仓「注释可满足」前科：作者把被钉的字样写进注释，字面钉就永远绿。
    """
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert html.count("out += cascadeDebugLines(c);") == 1
    assert html.count("`命中快速通道 →") == 1, "分支判据被抄成第二份（面板两处可以各说各话）"
    assert html.count("`→ 将播固定兜底：") == 1, "兜底话术只剩单点一份"


def test_offline_version_source_is_labelled_truthfully():
    """v1.6.6 刀X 面板那半条：非「实时」的来源一律如实标注。旧形只标「入驻时」一档，
    于是断连期的台账值以裸版本号上场，用户看不出它不是现读值（而徽章已说「待证」，
    两处口径就分叉了）。钉的是**码形**（注释里写同样的字不算数：整串三元式必须唯一）。
    """
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert html.count('d.fw_source&&d.fw_source!=="实时"') == 1, "来源标注必须是泛化式且单点"
    assert 'd.fw_source==="入驻时"' not in html, "旧的只标一档写法不得复活"



