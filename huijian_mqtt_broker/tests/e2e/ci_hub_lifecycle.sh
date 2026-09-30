#!/usr/bin/env bash
# CI 侧 hub 生命周期真栈入口（审计 2026-09-30 H-4 复核）。
#
# 为什么要单独一层：hub_lifecycle_e2e.sh 在对端仓不可见时 `exit 3`——直接塞进
# CI 会把"没配 key"变成发布阻断；而静默 exit 0 又把"没跑"说成"跑过并通过"。
# 本入口把两条分支分开（与 ci_contract_pins.sh 同一形态、同一理由）：
#   可见 ⇒ 真跑 + 自己复核 PASS 计数 ≥ 下限（驱程被改瘪也要红）；
#   不可见 ⇒ 退出 0（跳过不该阻断发版）但打 ::warning:: + 写 step summary，
#            且**绝不**打印真栈汇总行——那在日志里就是"跑过了"的形态。
# 两条分支的行为由 tests/test_v1742_hub_identity.py 真跑钉住。
set -u

FLOOR="${HUB_LIFECYCLE_FLOOR:-35}"          # 与驱程自证条数同源；砍臂必须两边一起改
HUB="${HUB_REPO:-}"

summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- $*" >> "$GITHUB_STEP_SUMMARY"
  fi
}

if [ -z "$HUB" ] || [ ! -f "$HUB/src/server.js" ]; then
  echo "::warning title=hub 生命周期真栈未执行::${FLOOR} 条真栈断言本轮**未执行**：HUB_REPO 未指向 huijian-cloud-hub（当前='${HUB:-<未设置>}'）。v1.7.41 抹盘自愈那条线上事故链本轮未被核对。"
  echo "   要让它真跑：给本仓配 hub 私有仓的只读 deploy key（secret HUB_REPO_DEPLOY_KEY），"
  echo "   workflow 的 peer-sync step 会 clone 到 runner 并注入 HUB_REPO。"
  summary "hub 生命周期真栈 ${FLOOR} 条**未执行**（对端私有仓在本 runner 不可见）"
  exit 0
fi

out="$(bash "$(dirname "$0")/hub_lifecycle_e2e.sh" 2>&1)"
rc=$?
printf '%s\n' "$out"
if [ "$rc" -ne 0 ]; then
  echo "::error title=hub 生命周期真栈失败::hub_lifecycle_e2e.sh rc=${rc}"
  summary "hub 生命周期真栈**失败**（rc=${rc}），见本步日志"
  exit "$rc"
fi

# 子脚本 rc=0 不等于"断言充分"：砍臂会让条数掉下来然后全绿。计数在这里复核一次。
n="$(printf '%s' "$out" | sed -n 's/.*hub 生命周期真栈: \([0-9][0-9]*\) passed.*/\1/p' | tail -1)"
if [ -z "$n" ]; then
  echo "::error title=真栈条数不可读::没在输出里抓到「hub 生命周期真栈: N passed」——汇总行格式变了就得同步本入口"
  exit 1
fi
if [ "$n" -lt "$FLOOR" ]; then
  echo "::error title=真栈条数不足::PASS 计数 ${n} < 下限 ${FLOOR}：驱程被砍臂了，不是「没问题」"
  summary "hub 生命周期真栈 PASS 计数 ${n} 低于下限 ${FLOOR}，判失败"
  exit 1
fi

echo "PASS 计数 ${n}（下限 ${FLOOR}）已复核"
summary "hub 生命周期真栈 ${n} 条全部通过（对端 hub 已在本 runner 上 clone）"
