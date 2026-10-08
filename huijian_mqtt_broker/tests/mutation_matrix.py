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
# v1.7.63 收口批新增臂的被改文件与判据文件
PROXY = "gateway_discovery_proxy.py"
UTILS = "custom_components/window_controller_gateway/utils.py"
MB = "custom_components/window_controller_gateway/mqtt_bootstrap.py"
REPAIRS = "custom_components/window_controller_gateway/repairs.py"
RUN_E2E_F = "tests/e2e/run_e2e.sh"
TDP = "tests/test_discovery_proxy.py"
TUTILS = "tests/test_utils.py"
T1760 = "tests/test_v1760_channel_guard.py"
T1761AF = "tests/test_v1761_adversarial_followups.py"
T1763H = "tests/test_v1763_healer_resident.py"
# v1.8.4 批次（审计 B-2/B-3 + Gitee 发布腿换载体）的被改文件与判据文件
CTYPES = "custom_components/window_controller_gateway/mqtt_handler/_ctypes.py"
CIY = "../.github/workflows/ci.yaml"       # _copy() 把 .github 一起带进副本
T184R = "tests/test_v184_ws_renewal.py"
T184A = "tests/test_v184_ack_failure.py"
T184G = "tests/test_v184_gitee_leg_carrier.py"
T1763R = "tests/test_v1763_repairs_flow.py"
T1763W = "tests/test_v1763_fast_e2e_wiring.py"
T1763M = "tests/test_v1763_mdns_watchdog.py"
# v1.7.64 / v1.7.65 两批复核收口批的被改文件与判据文件（第四轮复核 N3：把这些
# 一次性人工自证的臂固化进来，否则"钉本身是否还是真钉"没人复检）
COVER = "custom_components/window_controller_gateway/cover.py"
NUMBER = "custom_components/window_controller_gateway/number.py"
PANEL_JS = "www/js/huijian.js"
T1764 = "tests/test_audit_2026_10_05_round3_fixes.py"
T1746F = "tests/test_v1746_panel_unique_funcs.py"
T1765 = "tests/test_audit_2026_10_06_round4_fixes.py"
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
    # ── v1.7.63 第二轮审计收口批（C-1..C-11 + N-1）：每条修复的"退回原形"臂 ──
    # 共同形状：这些缺陷此前测试全绿（假件比真实现宽 / 喂的是构造行 /
    # 脚本从未被 CI 引用）——修法与钉一起进仓后，把产品改回原缺陷形态必须当场红。
    ("c1_replay_raw_line_restored", PROXY,
     "                ok1 = self._pub(payload_raw.strip())\n",
     "                ok1 = self._pub(raw.strip())\n",
     "test_verbose_line_replay_is_clean_json", "red",
     "C-1 原形：重放发整行（含 -v 主题前缀）⇒ 集成侧 json.loads 必失败（日志照打假成功）",
     TDP),
    ("c2_selfack_echo_clears_deaf", PROXY,
     "        key = (parts[1], str(msg_id))\n"
     "        if key in self._self_acked:\n"
     '            self._self_acked.pop(key, None)   # 消费"本代理自答回声"这一帧\n'
     "        else:\n"
     "            self._ha_deaf.pop(parts[1], None)\n"
     "            self._req_ids.pop(parts[1], None)\n",
     "        self._ha_deaf.pop(parts[1], None)\n"
     "        self._req_ids.pop(parts[1], None)\n",
     "test_self_ack_echo_does_not_clear_deaf_state", "red",
     "C-2 原形：自答回送被当 HA 应答证据 ⇒ 失聪态隔帧即清（6 帧只代答 3 帧）",
     T1760),
    ("f1_echo_marker_persists", PROXY,
     "        key = (parts[1], str(msg_id))\n"
     "        if key in self._self_acked:\n"
     '            self._self_acked.pop(key, None)   # 消费"本代理自答回声"这一帧\n'
     "        else:\n"
     "            self._ha_deaf.pop(parts[1], None)\n"
     "            self._req_ids.pop(parts[1], None)\n",
     "        key = (parts[1], str(msg_id))\n"
     "        if key in self._self_acked:\n"
     "            pass\n"
     "        else:\n"
     "            self._ha_deaf.pop(parts[1], None)\n"
     "            self._req_ids.pop(parts[1], None)\n",
     "test_ha_true_ack_with_self_acked_id_still_clears_deaf", "red",
     "F1 原形：记账期内一律不解除 ⇒ HA 恢复后真应答全被当自答吞掉、永不停手",
     T1760),
    ("f2a_keyerror_not_caught", UTILS,
     "    except (ImportError, AttributeError, KeyError):\n",
     "    except (ImportError, AttributeError):\n",
     "test_runtime_keyerror_falls_back_not_raises", "red",
     "F2 原形：is_connected 结构缺失抛 KeyError 逃出回退面 ⇒ 消费方（healer）被炸",
     TUTILS),
    ("f2b_healer_verify_unguarded", MB,
     "                    except Exception as err:  # noqa: BLE001 — 按无结论处理\n"
     "                        verdict = \"probe_error\"\n",
     "                    except Exception as err:  # noqa: BLE001 — 按无结论处理\n"
     "                        raise\n",
     "test_healer_survives_verify_exception", "red",
     "F2 原形：核验逃逸不收敛（handler 直接 re-raise）⇒ 常驻 healer 静默死亡（S2 同族）",
     T1763H),
    ("f3_repair_init_forwards_input", REPAIRS,
     "        return await self.async_step_confirm()\n",
     "        return await self.async_step_confirm(user_input)\n",
     "test_init_step_does_not_run_repair_on_card_click", "red",
     "F3 原形：首步转发 init data ⇒ 点修复瞬间直接执行、确认步被绕过",
     T1763R),
    ("f5_probe_exception_folded", MB,
     '        return "probe_error", None\n',
     '        return "no_endpoint", None\n',
     "test_probe_exception_is_probe_error_not_no_endpoint", "red",
     "F5 原形：端点探针逃逸折成 no_endpoint ⇒ healer 当通过清卡（C-7 只修了一半）",
     T1763H),
    ("c3_empty_uuid_cached", PROXY,
     "        if not self._uuid and self._read_uuid is not None:\n",
     "        if self._uuid is None and self._read_uuid is not None:\n",
     "test_ack_guard_picks_up_uuid_when_file_arrives_later", "red",
     "C-3 原形：读空即永久缓存 ⇒ 集成后落盘也不代答（001 永不兜底）",
     T1760),
    ("c4_async_symbol_restored", UTILS,
     "        from homeassistant.components.mqtt import is_connected\n"
     "        return bool(is_connected(hass))\n",
     "        from homeassistant.components.mqtt import async_connected\n"
     "        return bool(async_connected(hass))\n",
     "test_connected_false", "red",
     "C-4 原形：import 真机不存在的 async_connected ⇒ 恒走回退，disconnected 判词与两条 WARNING 全失效",
     TUTILS),
    ("c5_repair_success_removed", REPAIRS,
     "                return self.async_create_entry(data={})\n",
     "                return self.async_show_form(step_id=\"confirm\", "
     "errors={\"base\": \"still_broken\"})\n",
     "test_channel_issue_fix_reenables_and_creates_entry", "red",
     "C-5 原形：修复成功也不产生 create_entry ⇒ HA 不删卡且用户看不到任何动作",
     T1763R),
    ("c6_resident_sleep_dropped", MB,
     "                    if not await _interruptible_sleep(hass, CHANNEL_VERIFY_INTERVAL):\n"
     "                        return\n"
     "                    continue\n",
     "                    return\n",
     "test_healer_stays_resident_when_healthy", "red",
     "C-6 原形：健康即收尾 ⇒ 一次性核验（此后被抢走/判定面坏掉都无人复查）",
     T1763H),
    ("c7_broken_folded_no_endpoint", MB,
     "            return \"endpoint_broken\", None\n",
     "            return \"no_endpoint\", None\n",
     "test_verify_endpoint_broken_is_named", "red",
     "C-7 原形：端点半坏与无依据混同 ⇒ 坏文件被当通过清卡并永久关核验",
     T1763H),
    ("c8_gap_cap_removed", INIT,
     "                            _gap = min(_gap * 2, 3600)\n",
     "                            _gap = _gap * 2\n",
     "test_arm_retry_backoff_caps_at_3600", "red",
     "C-8 原形：注释称封顶 3600s 而实现无界翻倍 ⇒ 几分钟后即近乎停摆",
     T1761AF),
    ("c10_exit_clear_dropped", MB,
     "                    if not getattr(hass, \"is_stopping\", False):\n"
     "                        _clear_channel_issue(hass)\n"
     "                        _clear_mdns_issue(hass)\n",
     "                    if not getattr(hass, \"is_stopping\", False):\n"
     "                        pass\n",
     "test_healer_clears_cards_when_entries_all_disabled", "red",
     "C-10 原形：启用条目清零直接退出不清卡 ⇒ 禁用后僵尸卡永留",
     T1763H),
    ("c11_fast_e2e_wiring_dropped", RUN_E2E_F,
     "    bash \"$DIR/fast_discovery_e2e.sh\" || RC_F=$?\n",
     "    true \"fast_discovery_e2e.sh 未接线\"\n",
     "test_run_e2e_invokes_fast_script_after_driver", "red",
     "C-11 原形：真栈关口不被引用（哑门）——meta 钉必须抓着接线",
     T1763W),
    ("n1_backoff_cap_raised", RUNSH,
     "            if [ \"${MDNS_RETRY}\" -ge 7 ]; then\n                BACKOFF=600\n",
     "            if [ \"${MDNS_RETRY}\" -ge 7 ]; then\n                BACKOFF=100000\n",
     "test_backoff_expression_runs_in_range", "red",
     "N-1 近似原形：封顶被抬高 ⇒ 溢出档位不再落 [10,600]（sleep 负数/0 忙循环家族）",
     T1763M),
    # ── v1.7.64 收口批（第三轮复核 + 对抗复核 A-1~A-4 + 自伤 1 处）──────────
    # 第四轮独立复核（docs/verify-2026-10-06-v1764.md）N3 点名：这 14 臂当时只是
    # 一次性人工自证（写在 CHANGELOG 叙述里），矩阵本身零覆盖 ⇒ "钉是否还是真钉"
    # 没有持续复检。下面把它们固化进来；锚点 count!=1 会直接报 count=N（防锚漂移）。
    ("r3_cover_isfinite_dropped", COVER,
     "                    position = float(r_travel)\n"
     "                    if math.isfinite(position):\n"
     "                        return position <= 0\n"
     "            except (ValueError, TypeError, OverflowError):\n",
     "                    return float(r_travel) <= 0\n"
     "            except (ValueError, TypeError):\n",
     "test_is_closed_does_not_raise_or_claim_open_on_non_finite", "red",
     "#3 原形：inf/nan 转换不抛错而 `inf<=0`/`nan<=0` 皆 False ⇒ 垃圾位置被判成「打开」，"
     "且大整数进 float() 的 OverflowError 没人接",
     T1764),
    ("r3_cover_class_gate_site_unguarded", COVER,
     "                    attributes[\"r_travel\"] = max(0, min(100, int(pos)))\n"
     "        except (ValueError, TypeError, OverflowError):\n",
     "                    attributes[\"r_travel\"] = max(0, min(100, int(pos)))\n"
     "        except (ValueError, TypeError):\n",
     "test_no_numeric_conversion_left_without_overflow_gate", "red",
     "#3 类扫描钉的活体自证：restore 回填处摘掉 OverflowError，全仓类扫描必须当场红",
     T1764),
    ("r3_http_shape_gate_dropped", HUBC,
     "            body = await resp.json()\n"
     "            if not isinstance(body, dict):\n"
     "                raise HubHttpError(path, resp.status, \"bad_body\")\n"
     "            return body\n",
     "            return await resp.json()\n",
     "test_http_rejects_non_object_200_body", "red",
     "#6 原形：200 + null/[] 原样 return ⇒ 调用方在 try 之外 .get() 炸 AttributeError，"
     "面板 500 / 保活 task 死",
     T1764),
    ("r3_bad_body_copy_dropped", PANEL_JS,
     "                case 'bad_body':\n"
     "                    // v1.7.64（#6）：加载项自己产生的码——hub 回了 200 但响应体不是\n"
     "                    // 对象（云函数返回空时云托管照样回 200 + null）。未知码走 default\n"
     "                    // 回空串＝整条不显示，用户点了「添加家人/移除」会看到毫无反应。\n"
     "                    return '云端返回的响应看不懂（不是预期对象），这次操作可能没生效——请稍后重试；反复出现请把本条上报';\n",
     "",
     "test_op_error_covers_every_code_the_plugin_can_emit", "red",
     "#6 下游自伤原形：新码没上面板码表，未知码走 default 回空串＝点了没反应"
     "（守它的是从生产代码派生的码值宇宙，不是手写清单）",
     T1746F),
    ("r3_op_failed_copy_dropped", PANEL_JS,
     "                case 'op_failed':\n"
     "                    // v1.7.65（第四轮复核 N2）：_set_op_error 的**兜底值**——调用方传进\n"
     "                    // 来的 err 是空时才落到这里。它天生\"没原因\"，所以文案必须承认这点，\n"
     "                    // 并把人指到能拿到原因的地方（HA 日志），而不是假装知道是网络问题。\n"
     "                    return '这次操作没成功，云端也没给出原因——请稍后重试；反复出现请看 HA 日志的 hub 相关行';\n",
     "",
     "test_op_error_covers_every_code_the_plugin_can_emit", "red",
     "N2 原形：函数体兜底码 op_failed 不在派生宇宙/不在码表 ⇒ 调用方传空即静默",
     T1746F),
    ("r3_alias_snapshot_outside_lock", HUBC,
     "        async with self._alias_write_lock():\n"
     "            table = dict(self.member_aliases)\n",
     "        table = dict(self.member_aliases)\n"
     "        async with self._alias_write_lock():\n",
     "test_concurrent_renames_keep_both_names", "red",
     "#5 原形：读-改-写快照做在锁外 ⇒ 并发改名各整份覆盖，先完成那次静默丢失",
     T1764),
    ("r3_alias_load_outside_lock", HUBC,
     "        async with self._alias_write_lock():\n"
     "            if self._aliases_loaded:\n"
     "                return\n"
     "            self.member_aliases = await asyncio.to_thread(load_member_aliases, self.config_dir)\n"
     "            self._aliases_loaded = True\n",
     "        self.member_aliases = await asyncio.to_thread(load_member_aliases, self.config_dir)\n"
     "        self._aliases_loaded = True\n",
     "test_concurrent_renames_survive_cold_alias_file_read", "red",
     "A-2 原形：#5 修不到底——冷启动时第二次读盘晚于第一次提交返回，把内存整份覆盖成"
     "「落盘前的旧快照」，先完成那次改名在内存与磁盘一起丢",
     T1764),
    ("r3_positive_int_cap_dropped", HUBC,
     "        n = int(value)\n"
     "        float(n)   # 超出 float 可表示范围（≈1.8e308）＝后续浮点算术必抛，按不可用处理\n",
     "        n = int(value)\n",
     "test_positive_int_rejects_float_unrepresentable", "red",
     "A-1 源头闸原形：JSON 大整数原样放行，产物进浮点算术后炸长连主循环与 status_view",
     T1764),
    ("r3_member_ttl_guard_dropped", HUBC,
     "        try:\n"
     "            return int(self.member_code_ttl_s() - (time.time() - self._member_code_at))\n"
     "        except (OverflowError, ValueError):\n"
     "            # v1.7.64（对抗复核 A-1）：与上方 bind_code_expires_in 的 v1.7.61 S1 出口\n"
     "            # 守卫同形——这条是同族漏网，非有限/过大的 ttl 或签发时刻会让 int() 抛\n"
     "            # OverflowError 直接炸穿 status_view（面板 500）\n"
     "            return -1\n",
     "        return int(self.member_code_ttl_s() - (time.time() - self._member_code_at))\n",
     "test_member_code_expires_in_matches_owner_sibling_guard", "red",
     "A-1 视图层原形：同族 bind_code_expires_in 有出口守卫、成员码这条没有 ⇒ "
     "非有限值炸穿 status_view（面板 500）",
     T1764),
    ("r3_cleanup_swallow_restored", DMGR,
     "        for res in await asyncio.gather(*list(self._background_tasks),\n"
     "                                        return_exceptions=True):\n",
     "        for _t in list(self._background_tasks):\n"
     "            try:\n"
     "                await _t\n"
     "            except asyncio.CancelledError:\n"
     "                pass\n"
     "        for res in await asyncio.gather(*list(self._background_tasks),\n"
     "                                        return_exceptions=True):\n",
     "test_cleanup_cancellation_propagates_outward", "red",
     "#8 原形：逐条 await + 吞 CancelledError ⇒ cleanup 自身被取消时传不出去，"
     "HA 停机/重载只能等超时",
     T1764),
    ("r3_cleanup_swallow_c6_pin", DMGR,
     "        for res in await asyncio.gather(*list(self._background_tasks),\n"
     "                                        return_exceptions=True):\n",
     "        for _t in list(self._background_tasks):\n"
     "            try:\n"
     "                await _t\n"
     "            except asyncio.CancelledError:\n"
     "                pass\n"
     "        for res in await asyncio.gather(*list(self._background_tasks),\n"
     "                                        return_exceptions=True):\n",
     "test_c6_cleanup_callers_do_not_swallow_cancellation", "red",
     "同一条旧码必须让**扩面后的 C-6 判据**也红（判据宇宙含 cleanup 本体；"
     "只红行为钉不红判据钉＝判据有作用面漏洞）",
     AUDIT),
    ("r3_unload_swallow_restored", INIT,
     "    if _bg:\n"
     "        try:\n"
     "            results = await asyncio.gather(*_bg, return_exceptions=True)\n",
     "    if _bg:\n"
     "        for _t in _bg:\n"
     "            try:\n"
     "                await _t\n"
     "            except asyncio.CancelledError:\n"
     "                _LOGGER.debug(\"后台任务已取消\")\n"
     "        try:\n"
     "            results = await asyncio.gather(*_bg, return_exceptions=True)\n",
     "test_unload_entry_cancellation_propagates", "red",
     "A-3 原形：async_unload_entry 里同形吞取消 ⇒ 卸载被硬跑到结尾返回 True，"
     "HA 以为干净卸载",
     T1764),
    ("r3_unload_swallow_c6_pin", INIT,
     "    if _bg:\n"
     "        try:\n"
     "            results = await asyncio.gather(*_bg, return_exceptions=True)\n",
     "    if _bg:\n"
     "        for _t in _bg:\n"
     "            try:\n"
     "                await _t\n"
     "            except asyncio.CancelledError:\n"
     "                _LOGGER.debug(\"后台任务已取消\")\n"
     "        try:\n"
     "            results = await asyncio.gather(*_bg, return_exceptions=True)\n",
     "test_c6_cleanup_callers_do_not_swallow_cancellation", "red",
     "同一条旧码必须让判据也红（teardown 宇宙 = cleanup / async_unload_entry / "
     "async_remove_entry 本体都在扫）",
     AUDIT),
    ("r3_slider_touch_evidence_dropped", PANEL_JS,
     "            if (!el) return false;\n"
     "            if (document.activeElement === el) return true;\n"
     "            const at = USER_INPUT_AT.get(el);\n"
     "            return !!at && (Date.now() - at) < USER_INPUT_HOLD_MS;\n",
     "            return !!el && document.activeElement === el;\n",
     "test_user_interacting_recognises_touch_drag_in_node", "red",
     "#9 原形：只认 activeElement——iOS Safari 触摸拖动不给非文本控件移焦 ⇒ "
     "手机上等于没修，滑块被 30s 无感刷新覆写",
     T1764),
    # ── v1.7.65（第四轮复核 N4：同族第 7、8 处）────────────────────────
    ("r4_number_set_value_unguarded", NUMBER,
     "        try:\n"
     "            value_int = int(value)\n"
     "        except (ValueError, TypeError, OverflowError) as err:\n"
     "            raise HomeAssistantError(\n"
     "                f\"设置{self._entity_label}失败：无效的值 {value!r}\") from err\n"
     "        value_int = max(SPEED_MIN, min(SPEED_MAX, value_int))\n",
     "        value_int = max(SPEED_MIN, min(SPEED_MAX, int(value)))\n",
     "test_set_native_value_rejects_junk_instead_of_crashing", "red",
     "N4a 原形：number.set_value 入参来自 YAML（.inf/.nan 都是合法 float 字面量），"
     "无闸即服务调用当场炸——同文件设定值回显早就接住了",
     T1765),
    ("r4_endpoint_port_unguarded", MB,
     "    try:\n"
     "        exp_broker, exp_port = endpoint[\"broker\"], int(endpoint[\"port\"])\n"
     "    except (KeyError, TypeError, ValueError, OverflowError) as err:\n"
     "        # v1.7.65（第四轮复核 N4，同族第 8 处）：端点文件 port 被写成 `1e999`（JSON\n"
     "        # 合法）→ float('inf')，`int(inf)` 抛 OverflowError。下方 :312 的**条目侧**\n"
     "        # 早在 v1.7.61 S2 就接住了这个，端点侧漏。逃出去会被 healer / repairs 的宽\n"
     "        # 兜底折成 probe_error 或\"仍坏\"——判词不准（真坏的是端点文件），按 C-7 口径\n"
     "        # 归 endpoint_broken：不清卡，且卡片能把人指到\"端点文件读不出\"。\n"
     "        _LOGGER.warning(\"端点文件 port 字段不可解析（按端点坏处理，不折成无结论）: %r\", err)\n"
     "        return \"endpoint_broken\"\n",
     "    exp_broker, exp_port = endpoint[\"broker\"], int(endpoint[\"port\"])\n",
     "test_endpoint_bad_port_is_endpoint_broken_not_escape", "red",
     "N4b 原形：端点文件 port=1e999→inf 时 int() 抛 OverflowError 逃出判定面，"
     "被宽兜底折成 probe_error（判词不准：真坏的是端点文件）",
     T1765),

    # ── v1.8.4（2026-10-08 审计 B 档 + Gitee 发布腿载体）──
    # 这批臂原本是发版前的一次性脚本跑的；上一版复核点名过"本批臂未固化进矩阵"
    # 的流程欠账（v1.7.64 报告 §五 N3）⇒ 这次直接并进仓内，以后每次整跑都重放。
    ("v184_renewal_ignores_business_gate", WSGW,
     "if business:\n                    # 续期点在分派判定",
     "if True:\n                    # 续期点在分派判定",
     "test_one_byte_text_does_not_renew", "red",
     "B-2 原形：续期不看业务帧判据 ⇒ 任意 1 字节 TEXT 与当年 BINARY 等价，"
     "同网段拿公开令牌即可永久占满 4 槽、把真小程序挤成 503",
     T184R),
    ("v184_renewal_outside_business_guard", WSGW,
     "                business = frame_is_business(msg.data)\n",
     "                business = frame_is_business(msg.data)\n"
     "                deadline = loop.time() + WS_RECV_TIMEOUT_SECONDS\n",
     "test_renewal_point_is_after_dispatch", "red",
     "在分派前再补一处裸续期：受保护那处仍在，所以「存在性」式守卫会绿——"
     "判据必须是「循环内所有续期点都受罩」（本臂就是为钉这个软判据而加的）",
     T184R),
    ("v184_business_table_dropped_ping", WSGW,
     '"get_gateways", "get_devices", "control", "pair", "unbind", "ping", "set_token",\n',
     '"get_gateways", "get_devices", "control", "pair", "unbind", "set_token",\n',
     "test_business_cmd_table_matches_the_dispatch_chain", "red",
     "命令表与分派链漂移（表里少一条）⇒ 该命令的客户端每 300s 被静默踢线",
     T184R),
    ("v184_ack_failure_kills_the_frame", CTYPES,
     '        try:\n            await self._send_ack("002", payload)\n'
     "        except Exception as e:  # noqa: BLE001 - ack 失败不得影响本帧处理\n"
     '            _LOGGER.warning("002 ack 发布失败（本帧照常处理，网关会重发）: %s", e)\n',
     '        await self._send_ack("002", payload)\n',
     "test_ack_failure_does_not_skip_the_frame", "red",
     "B-3 原形：ack 排在 try 之外，一次发布失败就把 002 的状态/批处理/属性整帧吃掉，"
     "与紧邻注释承诺的「与处理结果无关」相反",
     T184A),
    ("v184_gitee_retry_bound_cut", CIY,
     "for try_no in (1, 2, 3):", "for try_no in (1,):",
     "test_transient_tls_error_then_success_is_visible", "red",
     "有界重试被砍成一次 ⇒ v1.8.3 实发形态：一次 TLS 抖动打断整条 Gitee 腿",
     T184G),
    ("v184_gitee_token_not_redacted", CIY,
     'last = redact("curl rc=%s %s" % (proc.returncode, proc.stderr))',
     'last = "curl rc=%s %s" % (proc.returncode, proc.stderr)[:160]',
     "test_token_never_reaches_the_log", "red",
     "失败信息不脱敏 ⇒ curl 诊断里的完整 URL（含 access_token）进公开 job 日志",
     T184G),
    ("v184_gitee_existence_guard_gutted", CIY,
     "if code not in (200, 404):", "if False:",
     "test_non_retryable_status_blocks_creation", "red",
     "存在性查询的非 200/404 守卫被阉 ⇒ 静默落到创建分支，两源正文分叉",
     T184G),
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
