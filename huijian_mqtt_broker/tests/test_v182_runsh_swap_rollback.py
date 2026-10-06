"""v1.8.2 A1：run.sh 集成换防的 set -e 裸命令与"承诺的回滚不存在"。

改前的块（`cp -r` 成功后）是：
    rm -rf "${INTEGRATION_DST}"          ← set -e 生效区里的裸命令
    if mv "${_STAGE}" "${INTEGRATION_DST}"; then ... else rm -rf "${_STAGE}"; fi
两条病灶：① 那条 `rm -rf` 删不干净（/config 只读重挂、EIO、`.nfsXXXX` 忙）就当场
杀死 run.sh，而 mosquitto 要到 §8 才启动 ⇒ broker+mDNS+Web UI+集成一起下线，正是本块
注释声称要根治的形态；② `mv` 失败分支删的是唯一好副本 `_STAGE`，注释里的"失败回滚"
根本没有回滚源。修法是旧目录**改名让位**（`_OLD`）而不是删除，块内每条 rm/cp/mv 带兜底。
"""

import pathlib
import re

RUNSH = pathlib.Path(__file__).resolve().parents[1] / "run.sh"


def _block():
    """返回换防块的**代码行**（剔除整行注释）。

    必须剔注释：本块的解释性注释里逐字引用了被修掉的旧代码
    （`rm -rf "${INTEGRATION_DST}"`），不剔就会把"文档里提到旧写法"判成
    "旧写法回潮"——假红。判据只吃代码，注释不参与任何断言。
    """
    lines = RUNSH.read_text(encoding="utf-8").split("\n")
    start = [i for i, ln in enumerate(lines)
             if '_STAGE="${INTEGRATION_DST}.new' in ln]
    assert len(start) == 1, "换防块起点定位失败"
    s = start[0]
    end = next(i for i in range(s, len(lines)) if "沿用当前文件继续启动" in lines[i])
    raw = lines[s:end + 3]
    return [ln for ln in raw if not ln.strip().startswith("#")]


def test_destination_is_never_deleted_before_swap():
    """目标目录只能改名让位，不能先删——删了就没有回滚源。"""
    blk = "\n".join(_block())
    assert 'rm -rf "${INTEGRATION_DST}"' not in blk, \
        "回潮：换防前删目标目录＝失败时无副本可回滚"


def test_rollback_source_is_renamed_aside_and_restored():
    blk = "\n".join(_block())
    assert '_OLD="${INTEGRATION_DST}.old' in blk, "缺改名让位的 _OLD（回滚源）"
    assert re.search(r'mv "\$\{INTEGRATION_DST\}" "\$\{_OLD\}"', blk), "旧目录未改名让位"
    assert re.search(r'mv "\$\{_OLD\}" "\$\{INTEGRATION_DST\}"', blk), "失败分支缺回滚放回"


def test_no_bare_destructive_command_inside_set_e_region():
    """块内每条 rm/cp/mv 都必须自带兜底，否则 set -e 会杀死 run.sh。"""
    offenders = []
    for ln in _block():
        if re.match(r'^\s*(rm|cp|mv)\b', ln):
            tail = ln.strip()
            if "||" not in ln and not tail.endswith(("&&", "\\")):
                offenders.append(tail[:70])
    assert not offenders, "这些裸命令在 set -e 下会直接终止 run.sh（broker 尚未启动）：\n" + "\n".join(offenders)


def test_persist_restore_is_guarded():
    src = RUNSH.read_text(encoding="utf-8")
    m = re.search(r'cp "/tmp/window_controller_gateway_data\.json\.bak" "\$\{PERSIST_FILE\}"(.*)', src)
    assert m, "找不到持久化数据恢复语句"
    assert "2>/dev/null" in m.group(1) or "||" in m.group(1), \
        "持久化恢复是裸 cp：失败会杀死 run.sh"


def test_old_atomic_swap_pin_still_holds():
    """v1.7.33 那条原子换防钉不许被本次修改削弱（临时名 + mv 换名仍在）。"""
    src = RUNSH.read_text(encoding="utf-8")
    assert "_STAGE=" in src
    assert 'mv "${_STAGE}" "${INTEGRATION_DST}"' in src
    assert not re.search(r'rm -rf "\$\{INTEGRATION_DST\}"\n\s*cp -r "\$\{INTEGRATION_SRC\}"', src)
