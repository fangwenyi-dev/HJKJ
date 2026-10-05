# -*- coding: utf-8 -*-
"""v1.7.59 行为钉：B-3 迁移类型校验真的能咬 + D-1 落盘失败真的走到面板。

两条都是"v1.7.56 只修了一半"的补口（见 CHANGELOG [1.7.56] B-3 / D-1 与本次复核）：

* **B-3**：v1.7.56 把跨条目查表管道接通了，但谓词读的是 `"type"`，而 `add_device`
  按产品口径恒把它强制成开窗器 ⇒ `!= 开窗器` 恒假，校验依旧一次都没生效过。
* **D-1 第三处**：注册成功、本机落盘失败时写的是**连接类槽** `last_error`，而那个槽
  ① 每轮尝试开头被清成 None、② 面板 `hubErrorText` 只认两个码 ⇒ 代码注释承诺的
  "必须让人看得见"在用户侧根本不成立。改走操作类槽（与同批 `bindcode_persist_failed` 同口径）。

本文件的判据纪律（就是上一批栽的地方）：**输入形态必须生产可达**。既有 B-3 三条钉是
直接往缓存里塞 `{"type": "some_other_type"}` —— 真实 `add_device` 永远写不出这个值，
于是"校验死了"与"校验活着"在那些钉眼里长得一样。下面所有用例都从 `add_device` 进，
不手塞字典；台架直接复用 `test_audit_2026_09_30_fixes`（同一份假件，避免两处各自漂移）。
"""
import ast
import asyncio

import test_audit_2026_09_30_fixes as prev

c = prev.c
hc = prev.hc
JS = (prev.ROOT / "www" / "js" / "huijian.js")


# ══════════════════════════════ B-3 ══════════════════════════════
def test_b3_add_device_keeps_reported_type_but_still_forces_type(tmp_path):
    """产品口径不变（实体仍按开窗器建），但上报原值必须留下——它是校验唯一的输入。"""
    mgr = prev._mgr(tmp_path)
    res = asyncio.run(mgr.add_device("50051", "窗帘控制器", c.DEVICE_TYPE_CURTAIN_CTR))
    assert res is None or isinstance(res, str), "add_device 返回形态别改坏"
    rec = mgr.devices.get("50051")
    assert rec is not None, "前置：设备没入库，下面无从判"
    assert rec["type"] == c.DEVICE_TYPE_WINDOW_OPENER, \
        "实体型号/命令表按 type 选，强制口径不许动"
    assert rec.get("reported_type") == c.DEVICE_TYPE_CURTAIN_CTR, \
        "上报原值被丢了 ⇒ 迁移校验重新退回恒真"


def test_b3_migration_blocks_non_opener_from_real_add_device(tmp_path):
    """真链路：旧网关缓存里那台非开窗器设备，迁移校验必须拦下来。"""
    new_mgr, old_mgr = prev._two_managers(tmp_path)
    asyncio.run(old_mgr.add_device("50051", "窗帘控制器", c.DEVICE_TYPE_CURTAIN_CTR))
    assert old_mgr.devices["50051"]["type"] == c.DEVICE_TYPE_WINDOW_OPENER, \
        "前置：读 type 永远拦不到，这条钉必须从 reported_type 走"
    res = asyncio.run(new_mgr._validate_migration(["50051"], "1001NEW"))
    assert res["valid"] is False, "非开窗器设备要真的拦下来（此前恒放行）"
    assert any("仅支持开窗器" in e for e in res["errors"]), res


def test_b3_migration_still_allows_real_window_opener(tmp_path):
    """反向半条：同一入口进来的开窗器不得被新逻辑误拦。"""
    new_mgr, old_mgr = prev._two_managers(tmp_path)
    asyncio.run(old_mgr.add_device("50051", "开窗器", c.DEVICE_TYPE_WINDOW_OPENER))
    res = asyncio.run(new_mgr._validate_migration(["50051"], "1001NEW"))
    assert all("类型不兼容" not in e for e in res["errors"]), res


def test_b3_record_without_reported_type_is_permissive(tmp_path):
    """反向半条 2：注册表回填的设备没有原值（HA 重启后），必须放行而非误拦。"""
    new_mgr, old_mgr = prev._two_managers(tmp_path)
    old_mgr.devices["50052"] = {"sn": "50052", "name": "回填设备",
                                "type": c.DEVICE_TYPE_WINDOW_OPENER,
                                "status": "unknown", "attributes": {},
                                "last_update": 0}
    res = asyncio.run(new_mgr._validate_migration(["50052"], "1001NEW"))
    assert all("类型不兼容" not in e for e in res["errors"]), res


def test_b3_first_report_wins_on_existing_device(tmp_path):
    """原值以首次上报为准：后续 002/003/迁移路径传的都是常量，覆盖会把真类型洗白。"""
    mgr = prev._mgr(tmp_path)
    asyncio.run(mgr.add_device("50051", "窗帘控制器", c.DEVICE_TYPE_CURTAIN_CTR))
    asyncio.run(mgr.add_device("50051", "窗帘控制器", c.DEVICE_TYPE_WINDOW_OPENER))
    assert mgr.devices["50051"].get("reported_type") == c.DEVICE_TYPE_CURTAIN_CTR, \
        "第二次上报（常量入参）把原值覆盖了 ⇒ 校验又变恒真"


def test_b3_capture_precedes_the_forcing_line():
    """结构钉（运行时不可达的形态，防回潮）：取原值必须排在强制之前。

    为什么用结构判据：把两行换个顺序，`reported_type` 就恒等于开窗器——从外面跑
    任何用例都看不出区别（正是本批要防的"恒真谓词"），只有行号关系能钉住。
    """
    src = (prev.PKG / "device_manager.py").read_text(encoding="utf-8")
    cap, force = _cap_and_force_lines(src)
    assert cap < force, \
        "取原值（%d 行）排在强制（%d 行）之后 ⇒ reported_type 恒为开窗器，校验重新变恒真" % (cap, force)


def test_b3_ordering_pin_catches_the_reorder_mutation():
    """自检上面那条结构钉真有区分度：把两行对调后它必须变红（不碰仓库，只在内存里改）。"""
    src = (prev.PKG / "device_manager.py").read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    cap, force = _cap_and_force_lines(src)
    force_line = lines[force - 1]
    mutant = "".join(lines[:cap - 1]) + force_line + "".join(lines[cap - 1:force - 1]) \
        + "".join(lines[force:])
    assert mutant != src, "变异没生效（行序对不上）——本自检与上面那条钉一起失效，必须重写"
    m_cap, m_force = _cap_and_force_lines(mutant)
    assert not (m_cap < m_force), \
        "对调两行后钉仍判通过 ⇒ 那条结构钉是假绿，必须重写"


def _cap_and_force_lines(src):
    """取 (reported_type 赋值行, device_type 被强制成常量的行)，都从 add_device 体内取。"""
    tree = ast.parse(src)
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "add_device"]
    assert len(defs) == 1, \
        "add_device 定义有 %d 处（重名＝后者遮蔽前者，本钉会验到死码）" % len(defs)
    cap = force = None
    for node in ast.walk(defs[0]):
        if isinstance(node, ast.Assign):
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "reported_type":
                cap = node.lineno
            if (isinstance(tgt, ast.Name) and tgt.id == "device_type"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "DEVICE_TYPE_WINDOW_OPENER"):
                force = node.lineno
    assert cap and force, "取原值/强制两行有一行不见了 ⇒ 本钉与 B-3 的修法一起失效，必须重写"
    return cap, force


# ══════════════════════════════ D-1 ══════════════════════════════
def test_d1_op_slot_survives_the_per_round_connection_slot_clear(tmp_path):
    """这条钉住"改槽"的意义本身：连接槽每轮被清，操作槽不会。"""
    cli = hc.HubClient([], config_dir=str(tmp_path), base="https://hub.invalid")
    cli._set_op_error(hc.OP_IDENTITY, "identity_persist_failed")
    cli.last_error = None          # ← _run_forever 每轮开头就是干这件事
    assert cli.last_op_error == "identity_persist_failed", \
        "落盘告警被清 ⇒ 面板永远来不及显示这一句"
    cli._clear_op_error(hc.OP_BINDCODE)   # 别家的清除不许顺手抹掉它（_clear 按 family 判等）
    assert cli.last_op_error == "identity_persist_failed"
    cli._clear_op_error(hc.OP_IDENTITY)
    assert cli.last_op_error is None, "成功即清这条路也要真的通"


def test_d1_identity_persist_failed_no_longer_written_to_connection_slot():
    """反向半条：不许再往 `last_error` 写这个值（写了也看不见，还会被下一轮清掉）。"""
    src = (prev.PKG / "hub_client.py").read_text(encoding="utf-8")
    assert 'self.last_error = "identity_persist_failed' not in src, \
        "回潮：连接槽又收到一个面板不映射的值"
    assert src.count("_set_op_error(hc.OP_IDENTITY") + src.count("_set_op_error(OP_IDENTITY") == 1, \
        "identity 家的落点应当恰好一处（多了＝又开始到处写操作槽）"


def test_d1_panel_renders_identity_persist_failed(tmp_path):
    """node 真跑：面板 hubOpErrorText 必须把这个码翻成人话（操作槽的唯一消费方）。"""
    text = _js_func("hubOpErrorText")
    script = text + """
let bad = 0;
function want(ok, msg) { if (!ok) { console.log('FAIL ' + msg); bad++; } }
const t = hubOpErrorText('identity_persist_failed');
want(typeof t === 'string' && t.length > 10, '必须翻成具体文案，实测: ' + JSON.stringify(t));
want(/重启/.test(t) && /绑定码/.test(t), '文案要说清"现在能用、重启才作废绑定码"这个时间差: ' + t);
want(hubOpErrorText('') === '', '空值不得被兜成一句话（那会把正常态说成故障）');
want(hubOpErrorText('some_unknown_code') === '', '未收录的码保持兜底，别顺手编文案');
process.exit(bad ? 1 : 0);
"""
    _run_node(script)


def test_d1_panel_case_is_not_duplicated():
    """计数钉：同一条 case 出现两次＝switch 里前者恒遮蔽后者（本仓吃过这类亏）。"""
    src = JS.read_text(encoding="utf-8")
    assert src.count("case 'identity_persist_failed'") == 1


# ══════════════════════════ 共用小工具 ══════════════════════════
def _js_func(name):
    src = JS.read_text(encoding="utf-8")
    i = src.index("function " + name)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("函数 %s 花括号不配平" % name)


def _run_node(script):
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    node = shutil.which("node") or shutil.which("node.exe")
    assert node, "node 不可用（装 node 或看 CI 的 JS 门）"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, "node 真跑不符：\n%s%s" % (r.stdout, r.stderr)
