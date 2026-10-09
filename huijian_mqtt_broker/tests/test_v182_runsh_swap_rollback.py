"""v1.8.2 A1：run.sh 集成换防的 set -e 裸命令与"承诺的回滚不存在"。

改前的块（`cp -r` 成功后）是：
    rm -rf "${INTEGRATION_DST}"          ← set -e 生效区里的裸命令
    if mv "${_STAGE}" "${INTEGRATION_DST}"; then ... else rm -rf "${_STAGE}"; fi
两条病灶：① 那条 `rm -rf` 删不干净（/config 只读重挂、EIO、`.nfsXXXX` 忙）就当场
杀死 run.sh，而 mosquitto 要到 §8 才启动 ⇒ broker+mDNS+Web UI+集成一起下线，正是本块
注释声称要根治的形态；② `mv` 失败分支删的是唯一好副本 `_STAGE`，注释里的"失败回滚"
根本没有回滚源。修法是旧目录**改名让位**（`_OLD`）而不是删除，块内每条 rm/cp/mv 带兜底。

v1.8.5 G-1（第二半）：本文件此前只盯 `_STAGE=` 到"沿用当前文件继续启动"这一小段
（起点锚在被保护块内部 25 行处），**结构性看不见同一段换防区上方的 `mkdir -p
custom_components` 与持久化备份 `cp`** —— 而那两条恰恰是同一族裸写。审计实测该
errexit 区共 15 条裸写、15/15 全在 §8 broker 启动之前（任一失败 = 整套加载项下线）。
现在 `_scan()` 覆盖**整个 errexit 生效区**，并如实建模四种合法的"已被屏蔽"形态
（heredoc 体、`( … ) &` 子壳、函数体、带兜底重定向的 `{ … }` 组、以及 `if !`/`if`
条件表达式），所以它既能抓住新裸写，也不会把已加固的写法误判成病灶。
"""

import pathlib
import re

RUNSH = pathlib.Path(__file__).resolve().parents[1] / "run.sh"

# 行首即"写"的命令：任一失败在 set -e 下都会终止 run.sh。
_WRITE_VERB = re.compile(
    r'^\s*(rm|cp|mv|mkdir|chmod|chown|tee|truncate|dd|ln|touch|install|cat)\b'
)
_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_FUNC_DEF = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{')


def _errexit_code_lines():
    """返回 errexit 生效区里**可执行**的写入行（含行号）。

    生效区：`set -e` 之后、`set +e` 之前，以及其后的 `set -e` 恢复区（run.sh 里
    mDNS 段是唯一刻意关掉 errexit 的地方）。

    被排除的行（都是"失败已在别处被接住"的形态，纳入就是拿假红喂门禁）：
      * 整行注释；
      * heredoc 体（`cat <<EOF` 的内容不是命令）；
      * `( … ) &` 后台子壳体（子壳里失败只杀子壳）；
      * 顶层函数体（`_bridge_on`/`_bridge_off` 等，调用点全部带 `|| true`）；
      * `{ … } > 文件 || 告警` 这种**带兜底重定向的组**。
    """
    lines = RUNSH.read_text(encoding="utf-8").split("\n")
    n = len(lines)

    # ---- 1. errexit 生效区 ----
    active = [False] * n
    on = False
    for i, ln in enumerate(lines):
        st = ln.strip()
        if st == "set -e":
            on = True
        elif st == "set +e":
            on = False
        active[i] = on

    # ---- 2. 行分类：哪些行整体不可执行 ----
    skip = [False] * n
    i = 0
    while i < n:
        st = lines[i].strip()

        # 2a. 注释行
        if st.startswith("#"):
            skip[i] = True
            i += 1
            continue

        # 2b. heredoc：本行是命令（保留），其后的体全部跳过
        m = _HEREDOC_OPEN.search(lines[i])
        if m:
            delim = m.group(2)
            j = i + 1
            while j < n and lines[j].strip() != delim:
                skip[j] = True
                j += 1
            if j < n:
                skip[j] = True  # 定界符行本身也不是命令
            i = j + 1
            continue

        # 2c. 后台子壳 `( … ) &`
        if st == "(":
            depth = 1
            j = i + 1
            while j < n and depth:
                sj = lines[j].strip()
                if sj == "(":
                    depth += 1
                elif sj in (")", ") &", ");", ")&"):
                    depth -= 1
                    if depth == 0:
                        skip[j] = True
                        break
                skip[j] = True
                j += 1
            skip[i] = True
            i = j + 1
            continue

        # 2d. 顶层函数体（单行 `f() { …; }` 也吃得住）
        if _FUNC_DEF.match(lines[i]):
            if "}" in lines[i].split("{", 1)[1]:
                skip[i] = True
                i += 1
                continue
            skip[i] = True
            j = i + 1
            while j < n and not lines[j].startswith("}"):
                skip[j] = True
                j += 1
            if j < n:
                skip[j] = True
            i = j + 1
            continue

        i += 1

    # ---- 3. 带兜底重定向的 `{ … }` 组：组内失败已被组尾 `||` 接住 ----
    i = 0
    while i < n:
        if lines[i].strip() == "{":
            depth = 1
            j = i + 1
            while j < n and depth:
                sj = lines[j].strip()
                if sj.endswith("{"):
                    depth += 1
                elif sj.startswith("}"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n and "||" in lines[j]:
                for k in range(i, j + 1):
                    skip[k] = True
            i = j + 1
            continue
        i += 1

    # ---- 4. 剩下的可执行写入行 ----
    out = []
    for i, ln in enumerate(lines):
        if not active[i] or skip[i]:
            continue
        if _WRITE_VERB.match(ln):
            out.append((i + 1, ln))
    return out


def _is_guarded(idx, ln, lines):
    """该写入行是否已被兜底接住。

    合法形态（v1.8.5 G-1 加固后统一采用，均在真 bash 上验证过 errexit 语义）：
      1. 行内 `||`（如 `cmd 2>/dev/null || echo 告警 >&2`）；
      2. 以 `&&`/`\\` 续行的条件链一环；
      3. `if`/`elif`/`while`/`until` 的条件表达式（如 `if ! mkdir -p x; then`）——
         条件为假只走分支，不触发 errexit。
    """
    if "||" in ln:
        return True
    st = ln.strip()
    if st.endswith(("&&", "\\")):
        return True
    # 条件表达式：命令本身在 `if`/`elif`/`while`/`until` 与它的 `then`/`do` 之间
    if re.match(r'^\s*(if|elif|while|until)\b', ln):
        return True
    for k in range(idx - 1, max(idx - 6, -1), -1):
        prev = lines[k].strip()
        if not prev or prev.startswith("#"):
            continue
        if re.match(r'^(if|elif|while|until)\b', prev):
            # 上一行开了条件，且还没走到 then/do
            return not re.search(r';\s*(then|do)\s*$', prev) and prev not in ("then", "do")
        if re.search(r';\s*(then|do)\s*$', prev) or prev in ("then", "do"):
            return False
    return False


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
    """目标目录只能改名让位，不能先删——删了就没有回滚源。

    v1.8.5 G-1：判据扩到**整个 errexit 生效区**（不只换防块），这样把
    `rm -rf "${INTEGRATION_DST}"` 挪到块外也照样抓得住。
    """
    blk = "\n".join(ln for _, ln in _errexit_code_lines())
    assert 'rm -rf "${INTEGRATION_DST}"' not in blk, \
        "回潮：换防前删目标目录＝失败时无副本可回滚"


def test_rollback_source_is_renamed_aside_and_restored():
    blk = "\n".join(_block())
    assert '_OLD="${INTEGRATION_DST}.old' in blk, "缺改名让位的 _OLD（回滚源）"
    assert re.search(r'mv "\$\{INTEGRATION_DST\}" "\$\{_OLD\}"', blk), "旧目录未改名让位"
    assert re.search(r'mv "\$\{_OLD\}" "\$\{INTEGRATION_DST\}"', blk), "失败分支缺回滚放回"


def test_no_bare_destructive_command_in_errexit_region():
    """errexit 生效区内每条写入都必须带兜底，否则 set -e 会杀死 run.sh。

    v1.8.5 G-1：先前本用例的窗口起点锚在 `_STAGE=`（换防块内部），结构上看不到
    同一段换防区上方的 `mkdir -p custom_components`(:796) 与持久化备份 `cp`(:819)。
    现在覆盖整个 errexit 区，并在 §8 broker 启动之前逐条点名。
    """
    lines = RUNSH.read_text(encoding="utf-8").split("\n")
    offenders = []
    for idx, ln in _errexit_code_lines():
        if _is_guarded(idx - 1, ln, lines):
            continue
        offenders.append("%d: %s" % (idx, ln.strip()[:90]))
    assert not offenders, (
        "以下裸写在 set -e 下会直接终止 run.sh（mosquitto 尚未启动，"
        "broker/mDNS/Web UI/集成会一起下线）。给它们加 `2>/dev/null || echo 告警 >&2` "
        "或改为 `if ! …; then 告警; fi`：\n" + "\n".join(offenders)
    )


def test_persist_backup_flag_is_set_only_when_backup_succeeded():
    """备份标志只能在 cp 真成功时置位（v1.8.5 G-1）。

    旧写法 `cp … ` 后**无条件** `BACKUP_PERSIST=true`：cp 失败（盘满/权限）时下方
    "恢复"支会拿不存在或半截的 /tmp 备份去覆盖用户的真数据文件。
    """
    src = RUNSH.read_text(encoding="utf-8")
    guarded = re.search(
        r'if\s+cp\s+"\$\{PERSIST_FILE\}"\s+"/tmp/window_controller_gateway_data\.json\.bak"'
        r'[^\n]*;\s*then\s*\n\s*BACKUP_PERSIST=true',
        src,
    )
    assert guarded, "备份 cp 未包在 if 成功支里，或未在其内置 BACKUP_PERSIST=true"
    # 反向臂：无条件置真的旧形态不许回潮
    assert not re.search(
        r'^\s*cp\s+"\$\{PERSIST_FILE\}"[^\n]*\n\s*BACKUP_PERSIST=true', src, re.M
    ), "回潮：cp 后无条件置 BACKUP_PERSIST=true（备份失败也会走恢复支覆盖真数据）"


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
