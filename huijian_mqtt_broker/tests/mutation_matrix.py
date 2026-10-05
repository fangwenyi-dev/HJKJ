#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已知回退形态的变异矩阵：把产品改坏之后，**必须有钉当场红**。

为什么要有这个文件（第三轮对抗复核 F-9 的建议①，2026-09-30）：形状/字样型判据永远追得上
新的绕过形态——`assert isinstance(x, object)`、恒真守卫插在 sleep 之前、吞取消上移一层……
逐条补判据是打地鼠。唯一能机器化的是**反向**的：把这些已经抓到的回退形态固化成臂，
每次提交都重放一遍，任何一条"产品改坏却全绿"就响亮失败。

纪律（本仓踩过坑的都在这里）：
  * 每条变异都先过语法闸（`bash -n` / `py_compile`）。"红"必须是行为红，
    上一批 `D-1c` 那种"摘掉 try 留下孤儿缩进行"的红只证明文件解析不了。
  * 每条臂都跑一次**未变异基线**（必须绿）。少文件/环境不齐造成的红不算抓得住。
  * 整树复制到临时目录：判据里的 ROOT 由 `__file__` 推导，副本自洽（把 .github/docs/
    CHANGELOG 一并带上，否则 cross_repo / F-4 那类读仓外文件的钉会假红）。
  * 计数下限自检在**本脚本自己**身上（同 ci_contract_pins 的规矩：入口不许把
    "一条都没跑"报成通过）。
  * 等价臂（expect="green"）：产品后果与变异前**可证等价**的形态，钉必须继续保持绿——
    它守的是"别把优化误当缺陷钉死"，也是反向半条。

臂形制：`(id, 被改文件, old 字面量, new 字面量, -k 选择器, 预期 red/green, 缘由[, 判据文件])`
——第 8 位缺省是 `tests/test_audit_2026_09_30_fixes.py`（前 29 臂的判据都在那里），
新批次把判据写在别的文件时必须显式带上第 8 位，否则 `-k` 命不中＝空跑报绿。

用法：
    python3 tests/mutation_matrix.py [--floor N] [--only id[,id]] [--list]
退出码：0=全部符合预期；非 0=有臂失守（响亮，不静默跳过）。
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent          # huijian_mqtt_broker/tests
PKG_ROOT = HERE.parent                                   # huijian_mqtt_broker
AUDIT = "tests/test_audit_2026_09_30_fixes.py"
GHOST = "tests/test_v1759_ghost_device.py"
RUNSH = "run.sh"
INIT = "custom_components/window_controller_gateway/__init__.py"
HUBC = "custom_components/window_controller_gateway/hub_client.py"
DMGR = "custom_components/window_controller_gateway/device_manager.py"
WSGW = "custom_components/window_controller_gateway/ws_gateway.py"
DOCKER = "Dockerfile"
E2E = "tests/e2e/bridge_coexist_e2e.sh"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
TIMEOUT = 900

LAUNCH = "grep -q 'mdns_publisher\\.py'"
SCAN_HEAD = '    for _mdns_pid in $(ls "${MDNS_PROC_ROOT:-/proc}" 2>/dev/null); do\n'
CMD_BLOCK = (
    '        _mdns_cmd="${MDNS_PROC_ROOT:-/proc}/${_mdns_pid}/cmdline"\n'
    '        [ -r "${_mdns_cmd}" ] || continue\n'
    "        if tr '\\0' ' ' 2>/dev/null < \"${_mdns_cmd}\" \\\n"
    "           | grep -q 'mdns_publisher\\.py'; then\n")
WD_TAIL = "    while true; do\n        sleep 20\n"
ECHO_BLOCK = ('    if [ -n "${MDNS_PID}" ] && [ "${_mdns_seen}" = 0 ]; then\n'
              '        echo "[mDNS] /proc 扫描未列出任何条目：publisher 可能残留，'
              '检查 MDNS_PROC_ROOT 是否被上层设置"\n    fi\n')
C6_ORIG = ("    if mqtt_handler:\n        try:\n            await mqtt_handler.cleanup()\n"
           "        except Exception as e:\n            _LOGGER.debug(\"清理MQTT处理器异常: %s\", e)\n")
C6_SUPPRESS = ("    if mqtt_handler:\n        with contextlib.suppress(asyncio.CancelledError, Exception):\n"
               "            await mqtt_handler.cleanup()\n")
ASSERT_LAUNCH = '    assert launch, "找不到 mDNS 启动命令行（本钉需重写）"'
ASSERT_SARGS = '    assert sargs == ["20", "20", "20"], \\'


def _anchors():
    """从当前盘上取动态锚（`trap cleanup_mdns EXIT` 在注释里也出现一次；
    `) &` 全文多处；调用点续接行的缩进手抄必错；/proc 扫描块被我加过 `_mdns_seen`
    记账行 ⇒ 字面量块会漂，一律现场取）。"""
    ls = (PKG_ROOT / RUNSH).read_text(encoding="utf-8").split("\n")
    trap = [i for i, l in enumerate(ls) if "trap cleanup_mdns EXIT" in l and not l.strip().startswith("#")]
    assert len(trap) == 1, "trap 锚不唯一"
    f5_old = "\n".join(ls[trap[0]:trap[0] + 2])
    wd = [i for i, l in enumerate(ls) if l == ") &"
          and "nginx 存活看门狗已启动" in ls[i + 1]]
    assert len(wd) == 1, "看门狗收尾锚不唯一"
    f8_old = "\n".join(ls[wd[0]:wd[0] + 2])
    il = (PKG_ROOT / INIT).read_text(encoding="utf-8").split("\n")
    call = [i for i, l in enumerate(il)
            if "await _cleanup_partial_setup(mqtt_handler" in l and not l.strip().startswith("#")]
    assert len(call) == 2, "cleanup 调用点应有两处，实得 %d" % len(call)
    f7_old = "\n".join(il[call[0]:call[0] + 2])
    # /proc 扫描整块：起点=for，终点=它自己的 done（**块内**有界，不许跨函数）
    src = (PKG_ROOT / RUNSH).read_text(encoding="utf-8")
    m = re.search(r'(    for _mdns_pid in \$\(ls \S.*?\n    done\n)', src, re.S)
    assert m and src.count(m.group(1)) == 1, "扫描块锚不唯一/丢失"
    scan = m.group(1)
    m2 = re.search(r'^\(\n    NGINX_RESTART_COUNT=0.*?\n\) &', src, re.M | re.S)
    assert m2 and src.count(m2.group(0)) == 1, "看门狗整块锚不唯一/丢失"
    return f5_old, f8_old, f7_old, scan, m2.group(0)


# (id, 文件, 旧, 新, -k 目标判据, 预期, 说明)
ARMS = [
    # ── C-7：/proc 扫描（噪声根修 + 四臂行为钉）──
    ("c7_pattern_dead", RUNSH, LAUNCH, "grep -q 'mdns_publisher_NEVER\\.py'",
     "c7_cleanup_mdns or c7_scan_pattern", "red", "扫描串改成永不相交：看着在扫其实一只不杀"),
    ("c7_pattern_overbroad", RUNSH, LAUNCH,
     "grep -q 'mdns_publisher\\.py\\|mosquitto'", "c7_cleanup_mdns", "red",
     "pattern 松到误伤 broker"),
    ("c7_scan_commented", "DYN:scancomment", "", "", "c7_cleanup_mdns", "red",
     "整段注释化（注释满足）"),
    ("c7_redir_order_noisy", RUNSH, CMD_BLOCK,
     '        _mdns_cmd="${MDNS_PROC_ROOT:-/proc}/${_mdns_pid}/cmdline"\n'
     "        if tr '\\0' ' ' < \"${_mdns_cmd}\" 2>/dev/null \\\n"
     "           | grep -q 'mdns_publisher\\.py'; then\n", "c7_cleanup_mdns", "red",
     "退回 v1.7.56 上线时的真实形态（无 `[ -r ]` + 重定向次序错）⇒ 停机噪声淹关停现场。"
     "注意：**只翻次序、留着 `[ -r ]`** 在本机 msys 上观测不到（msys 允许 open 目录，"
     "错误改由 tr 自己吐出并已被 2>/dev/null 吞掉），Linux 上 EISDIR 仍会漏 ⇒ 这条臂按"
     "历史形态打，才与线上那 4 行报错同源"),
    ("c7_lever_hardcoded", RUNSH,
     '        _mdns_cmd="${MDNS_PROC_ROOT:-/proc}/${_mdns_pid}/cmdline"\n',
     '        _mdns_cmd="/proc/${_mdns_pid}/cmdline"\n', "c7_cleanup_mdns_kills_both", "red",
     "杠杆退回硬编码：改钉后的反向半条"),
    ("c7_ls_root_default_broken", RUNSH, SCAN_HEAD,
     '    for _mdns_pid in $(ls "${MDNS_PROC_ROOT:-/proc/1}" 2>/dev/null); do\n',
     "c7_scan_root", "red", "只改 ls 那处默认值：生产唯一会走的臂，改坏后一只不杀"),
    ("c7_silence_restored", RUNSH, ECHO_BLOCK, "", "c7_scan_root_asks_proc", "red",
     "删掉『扫不到条目』的回声 ⇒ 故障不可见"),
    ("c7_dockerfile_env_lever", DOCKER, "", "ENV MDNS_PROC_ROOT=/proc/1\n",
     "no_test_lever_env", "red", "生产镜像设测试杠杆 ⇒ 真路径被顶掉且无测试能看见"),
    ("c7_mdns_pid_flip", RUNSH, '    if [ -n "${MDNS_PID}" ]; then',
     '    if [ -z "${MDNS_PID}" ]; then', "c7_cleanup_mdns_really_kills_both_pids", "red",
     "子 shell 判据翻转（钉把 MDNS_PID 写死成空时曾完全看不出来）"),
    ("c7_kill_subshell_dropped", RUNSH, '        kill "${MDNS_PID}" 2>/dev/null || true\n',
     '        : "${MDNS_PID}"\n', "c7_cleanup_mdns", "red",
     "不杀看门狗子 shell ⇒ 10s 后 publisher 复活"),
    ("c7_case_guard_dropped", RUNSH,
     '        case "${_mdns_pid}" in\n            \'\'|*[!0-9]*) continue ;;\n        esac\n',
     "", "c7_cleanup_mdns_really_kills_both_pids", "red",
     "删非数字条目闸：self 的 cmdline 恰好命中 publisher 时会被当 PID 递给 kill"),
    ("c7_dash_r_dropped", RUNSH, '        [ -r "${_mdns_cmd}" ] || continue\n', "",
     "c7_cleanup_mdns", "green",
     "等价臂：噪声由重定向次序治，`[ -r ]` 只是省 fork ⇒ 钉必须继续绿（别把优化当缺陷钉死）"),
    ("c7_trap_emptied", "DYN:trap", "", "", "wired_to_the_exit", "red",
     "EXIT 陷阱清空 ⇒ cleanup_mdns 变死代码（全仓原本没有一条钉提 trap）"),
    # ── F-2：nginx 看门狗 ──
    ("f2_no_sleep", RUNSH, WD_TAIL, "    while true; do\n", "f2_", "red",
     "删节拍 sleep ⇒ 忙轮询"),
    ("f2_while_to_if", "DYN:while2if", "", "", "f2_", "red",
     "while→if ⇒ 首启探一次终身不管（F-2 原缺陷形状；整块改，done 一起换成 fi 才语法合法）"),
    ("f2_guard_continue_first", RUNSH, WD_TAIL,
     "    while true; do\n        [ \"${NGINX_PROCS:-0}\" -ge 0 ] && continue\n        sleep 20\n",
     "watchdog_ticks_and_probes", "red", "恒真守卫插在 sleep 之前 ⇒ 一圈都不睡不探 + 烧核"),
    ("f2_sleep_zero", RUNSH, WD_TAIL, "    while true; do\n        sleep 0\n",
     "watchdog_ticks_and_probes", "red", "sleep 0 也算『有节拍的 sleep』：字样判据的盲区"),
    ("f2_watchdog_foreground", "DYN:wd", "", "", "watchdog_is_backgrounded", "red",
     "` ) &` → `)`：run.sh 卡死在 3a，broker/mDNS/UI 全不启动（一个字符，全仓零反应）"),
    # ── C-6：停机取消（cleanup 本体的两条臂在 round-2 矩阵里，见 docs 附录 A）──
    ("c6_caller_swallow", INIT, C6_ORIG,
     "    if mqtt_handler:\n        try:\n            await mqtt_handler.cleanup()\n"
     "        except asyncio.CancelledError:\n            pass\n"
     "        except Exception as e:\n            _LOGGER.debug(\"清理MQTT处理器异常: %s\", e)\n",
     "c6_cleanup_callers", "red", "调用点捕 CancelledError 不传"),
    ("c6_contextlib_suppress", INIT, C6_ORIG, C6_SUPPRESS,
     "c6_cleanup_callers", "red", "with contextlib.suppress(CancelledError)：不产生 Try 节点"),
    ("c6_swallow_one_level_up", "DYN:call", "", "", "c6_cleanup_callers", "red",
     "吞取消上移一层（await 的名字不是 Attribute，旧判据整条跳过）"),
    # ── run.sh 重定向同类 ──
    ("redir_e2e_revert", E2E,
     'while read -r xpid; do kill "$xpid" 2>/dev/null; done 2>/dev/null < "$L/.pids"',
     'while read -r xpid; do kill "$xpid" 2>/dev/null; done < "$L/.pids" 2>/dev/null',
     "input_redirect_before_stderr", "red", "输入侧同族回潮（e2e teardown）"),
    ("redir_crossline", RUNSH, CMD_BLOCK, CMD_BLOCK.replace(
        "        if tr '\\0' ' ' 2>/dev/null < \"${_mdns_cmd}\" \\\n",
        "        if tr '\\0' ' ' < \"${_mdns_cmd}\" \\\n"
        "           2>/dev/null | grep -q 'mdns_publisher\\.py'; then\n")
        .replace("           | grep -q 'mdns_publisher\\.py'; then\n", ""),
     "input_redirect_before_stderr", "red", "跨行写的同形缺陷：证明续接那一步真在起作用"),
    # ── 元钉：把测试自己改瘪 ──
    ("taut_assert_true", AUDIT, ASSERT_LAUNCH, '    assert True, "x"', "tautological", "red",
     "恒真常量"),
    ("taut_or_true", AUDIT, ASSERT_LAUNCH, '    assert launch or True, "x"',
     "tautological", "red", "X or True"),
    ("taut_isinstance_object", AUDIT, ASSERT_LAUNCH, '    assert isinstance(launch, object), "x"',
     "tautological", "red", "isinstance(x, object) 恒真"),
    ("taut_excluded_middle", AUDIT, ASSERT_LAUNCH, '    assert launch or not launch, "x"',
     "tautological", "red", "排中律恒真"),
    ("taut_all_or_true", AUDIT, ASSERT_LAUNCH, '    assert all([launch or True]), "x"',
     "tautological", "red", "藏在 all([...]) 里的 or True"),
    ("taut_degenerate_empty", AUDIT, ASSERT_SARGS,
     '    assert sargs == ["20", "20", "20"] or sargs == [], \\', "tautological", "red",
     "同一个量既判相等又判空集（杀不到也算过）"),
    # ── v1.7.59 幽灵设备链：删除要变成一次可被淘汰的全量快照 ──
    # 这八条的共同形状：产品"看着还在认真上行状态"，但删掉的设备永远留在云端。
    ("ghost_flush_drops_empty", HUBC,
     "            items, authoritative = self.build_state_snapshot()\n",
     "            items, authoritative = self.build_state_snapshot()\n"
     "            if not items:\n                continue\n",
     "test_last_device_deleted_still_pushes_an_empty_snapshot", "red",
     "退回 v1.7.58 的 `if not items: continue`：删掉最后一台时全量快照不再上行，"
     "hub 的 merge-only 状态表永久留着那个 sn", GHOST),
    ("ghost_zero_manager_treated_as_full", HUBC,
     "        if not self._managers:\n            authoritative = False\n", "",
     "test_zero_manager_batch_is_not_pushed", "red",
     "零 manager 的『空』当成全量交出去：启动未完成/条目全在卸载时把云端整表清空"
     "（比幽灵设备更糟的反方向）", GHOST),
    ("ghost_unbuildable_claimed_full", HUBC,
     "                if view is None:\n                    authoritative = False\n"
     "                    continue\n",
     "                if view is None:\n                    continue\n",
     "test_unbuildable_from_the_very_first_round_is_not_authoritative", "red",
     "首轮就构造不出视图（没有回退位）还判权威 ⇒ hub 把这台看不见的设备判成已删除",
     GHOST),
    ("ghost_fallback_resurrects", HUBC,
     "        self._last_views = fresh\n",
     "        for _k, _v in self._last_views.items():\n"
     "            if _k not in fresh:\n                items.append(_v)\n"
     "        self._last_views = fresh\n",
     "test_fallback_cache_never_resurrects_a_deleted_device", "red",
     "把回退位当数据源（而不是失败兜底）＝已删设备每轮被自己写回快照，加固本身变成"
     "幽灵设备的制造者", GHOST),
    ("ghost_remove_no_notify", DMGR,
     '        # "1:1 复刻 app_ws_gateway.c"的纪律；小程序 LAN 列表照旧由 get_devices 刷新。\n'
     "        self._notify_status_listeners(device_sn)\n",
     '        # "1:1 复刻 app_ws_gateway.c"的纪律；小程序 LAN 列表照旧由 get_devices 刷新。\n',
     "test_remove_device_notifies_status_listeners_after_the_cache_drop", "red",
     "删除不标脏（本批用户报障的原形）：云端要等下一次 002 上报或 5 分钟保活才知道少了设备",
     GHOST),
    ("ghost_notify_before_cache_drop", DMGR,
     '        # "1:1 复刻 app_ws_gateway.c"的纪律；小程序 LAN 列表照旧由 get_devices 刷新。\n'
     "        self._notify_status_listeners(device_sn)\n",
     '        # "1:1 复刻 app_ws_gateway.c"的纪律；小程序 LAN 列表照旧由 get_devices 刷新。\n',
     "test_remove_device_notifies_status_listeners_after_the_cache_drop", "red",
     "通知时机提前到缓存删除之前：0.3s 后重扫到的仍是旧名单，那台已删设备被原样再推一次"
     "（钉住的是顺序，不是『调用过就行』）", GHOST),
    ("ghost_race_pop_no_notify", DMGR,
     "                self._notify_status_listeners(device_sn)\n                return None\n",
     "                return None\n",
     "test_race_rollback_pop_also_marks_dirty", "red",
     "v1.7.12 DM-F3 竞态复检那次 pop 不标脏：并发添加把已删设备又带上过一次快照，"
     "之后再无事件 ⇒ 云端停在『这台还在』", GHOST),
    ("lan_payload_fabricated_for_missing_device", WSGW,
     "        dev = data[\"device_manager\"].devices.get(device_sn)\n"
     "        if dev is None:\n            return None\n",
     "        dev = data[\"device_manager\"].devices.get(device_sn) or {}\n",
     "test_removal_notify_stays_silent_on_the_lan_channel", "red",
     "给不存在的设备造 device_update：新增的删除通知会凭空造出固件没有的消息类型",
     GHOST),
]


def _patch(fp, old, new, expect=1):
    b = fp.read_bytes()
    if b"\r\n" in b:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    ob, nb = old.encode("utf-8"), new.encode("utf-8")
    if old == "":
        fp.write_bytes(nb + b)
        return "prepend"
    if b.count(ob) != expect:
        return "count=%d" % b.count(ob)
    fp.write_bytes(b.replace(ob, nb))
    return "ok"


def _copy():
    d = pathlib.Path(tempfile.mkdtemp(prefix="mm_")) / PKG_ROOT.name
    shutil.copytree(PKG_ROOT, d, ignore=IGNORE)
    for extra in (".github", "docs"):
        s = PKG_ROOT.parent / extra
        if s.exists():
            shutil.copytree(s, d.parent / extra, ignore=IGNORE, dirs_exist_ok=True)
    for f in ("CLAUDE.md", "CHANGELOG.md", "README.md", "repository.yaml"):
        if (PKG_ROOT.parent / f).exists():
            shutil.copy2(PKG_ROOT.parent / f, d.parent / f)
    return d


def _run(root, k=None, path=None, full=False):
    args = [sys.executable, "-m", "pytest", "tests" if full else (path or AUDIT),
            "-q", "-p", "no:cacheprovider", "--no-header"]
    if k:
        args += ["-k", k]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(args, cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env)
    return p.returncode, (p.stdout or "")[-900:] + (p.stderr or "")[-200:]


def main():
    # 本机控制台是 GBK：矩阵输出含中文与 ⇒，不重设编码会在"打印失败结论"时自己先崩
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=len(ARMS))
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print("\n".join("%-32s %s" % (x[0], x[6]) for x in ARMS))
        return 0
    if len(ARMS) < a.floor:
        print("FAIL 臂数 %d < 下限 %d（有人删了臂？）" % (len(ARMS), a.floor))
        return 1

    f5_old, f8_old, f7_old, scan_blk, wd_blk = _anchors()
    w2i = wd_blk.replace("while true; do", "if true; then", 1)
    w2i = w2i.replace("\n    done\n) &", "\n    fi\n) &", 1)
    assert "if true; then" in w2i and "\n    fi\n) &" in w2i, "while→if 双改未到位"
    commented = "".join(("#" + ln if ln.strip() else ln) + "\n"
                        for ln in scan_blk.rstrip("\n").split("\n"))
    DYN = {"DYN:trap": (RUNSH, f5_old, f5_old.replace("cleanup_mdns", ":")),
           "DYN:wd": (RUNSH, f8_old, f8_old.replace(") &", ")", 1)),
           "DYN:scancomment": (RUNSH, scan_blk, commented),
           "DYN:while2if": (RUNSH, wd_blk, w2i),
           "DYN:call": (INIT, f7_old,
                        "        try:\n"
                        + f7_old.replace("        await _cleanup", "            await _cleanup", 1)
                        + "\n        except asyncio.CancelledError:\n            pass")}
    wanted = set(x.strip() for x in a.only.split(",") if x.strip())
    extra = {"c6_contextlib_suppress": [(INIT, "import asyncio\n", "import asyncio\nimport contextlib\n")],
             # 顺序臂：主补丁摘掉"缓存删完之后"的通知，这里再把它插到缓存删除之前。
             # 两条合起来才是"通知过但时机错"这一形态——单独任何一条都不成立。
             "ghost_notify_before_cache_drop": [(
                 DMGR,
                 '        device_info = self.devices.get(device_sn) or {}\n',
                 '        self._notify_status_listeners(device_sn)\n'
                 '        device_info = self.devices.get(device_sn) or {}\n')]}

    base = _copy()
    brc, btail = _run(base, None, full=True)
    rows = [{"id": "BASELINE-full", "expect": "green", "ok": brc == 0,
             "note": "未变异整树全量必须绿：%s" % btail.replace("\n", " | ")[-160:]}]
    if brc != 0:
        print("      基线尾巴：%s" % btail.replace("\n", " | ")[-600:])
        shutil.rmtree(base, ignore_errors=True)
        print("".join("%-34s %s %s\n" % (r["id"], r["expect"], "OK" if r["ok"] else "FAIL")
                      for r in rows))
        print("变异矩阵: 基线不绿 ⇒ 整个矩阵不可信（先修环境/副本）")
        return 1

    n_red_expected = 0
    for arm in ARMS:
        mid, rel, old, new, k, expect, why = arm[:7]
        tpath = arm[7] if len(arm) > 7 else AUDIT      # 判据文件缺省=审计本文件
        if wanted and mid not in wanted:
            continue
        if rel.startswith("DYN:"):
            rel, old, new = DYN[rel]
        exp_cnt = 2 if mid == "c6_swallow_one_level_up" else 1
        root = _copy()
        st = _patch(root / rel, old, new, exp_cnt)
        for (erel, eo, en) in extra.get(mid, []):
            _patch(root / erel, eo, en)
        if st not in ("ok", "prepend"):
            rows.append({"id": mid, "expect": expect, "ok": False,
                         "note": "变异没打上（%s）⇒ 锚漂移，本臂需同步" % st})
            shutil.rmtree(root, ignore_errors=True)
            continue
        syn = True
        for cand in [root / rel] + [root / e for (e, _o, _n) in extra.get(mid, [])]:
            if cand.suffix == ".sh":
                syn = syn and subprocess.run(["bash", "-n", str(cand)],
                                             capture_output=True).returncode == 0
            elif cand.suffix == ".py":
                syn = syn and subprocess.run([sys.executable, "-m", "py_compile", str(cand)],
                                             capture_output=True).returncode == 0
        if not syn:
            rows.append({"id": mid, "expect": expect, "ok": False,
                         "note": "变异后语法不合法 ⇒ 这条红不算抓到（等价回退要求）"})
            shutil.rmtree(root, ignore_errors=True)
            continue
        rc, tail = _run(root, k, tpath)
        want_red = expect == "red"
        rows.append({"id": mid, "expect": expect, "ok": (rc != 0) == want_red,
                     "note": tail.replace("\n", " | ")[-260:]})
        if want_red:
            n_red_expected += 1
        shutil.rmtree(root, ignore_errors=True)

    shutil.rmtree(base, ignore_errors=True)
    out = pathlib.Path(tempfile.gettempdir()) / "mutation_matrix_last.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        print("%-34s expect=%-6s %s" % (r["id"], r["expect"], "OK" if r["ok"] else "FAIL"))
        if not r["ok"]:
            print("      %s" % r["note"][-300:])
    bad = [r["id"] for r in rows if not r["ok"]]
    ran = len([r for r in rows if r["id"] != "BASELINE-full"])
    print("变异矩阵: %d 臂（应红 %d）失守 %d %s" % (ran, n_red_expected, len(bad), bad))
    if bad:
        return 1
    if ran < a.floor and not wanted:
        print("FAIL 实际跑了 %d 臂 < 下限 %d（选择器/锚漂移导致静默少跑）" % (ran, a.floor))
        return 1
    if wanted:
        print("子集模式（--only）：跳过下限自检，跑了 %d 臂" % ran)
        return 0 if not bad else 1
    print("PASS 计数 %d（下限 %d）已复核" % (ran, a.floor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
