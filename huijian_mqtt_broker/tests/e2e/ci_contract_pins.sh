#!/usr/bin/env bash
# CI 侧跨仓契约钉入口（B3）。
#
# 为什么要单独一层：cross_repo_contract.sh 在对端仓不可见时 `exit 3`，pytest 记 skipped，
# job 照绿——于是"CI 9/9 success"把**没跑**说成了**跑过并通过**。跳过本身是对的（hub 与
# 小程序都是私有仓，公共 runner 拿不到），要治的是"跳过与成功不可区分"。
#
# 两条分支都要能被机器核对：
#   可见 ⇒ 真跑对账 + 自己复核 PASS 计数 ≥ 下限（下限掉下来＝脚本被改瘪，判失败）；
#   不可见 ⇒ 退出 0（不阻断发版）但打 ::warning:: 并把条数与缺的变量名说全，
#            且**绝不**打印对账汇总行——那在日志里就是"跑过了"的形态。
# 判据由 tests/test_v1753_ci_contract_pins.py 真跑两条分支钉住（含"把子脚本改瘪到 3 条
# ⇒ 入口必须失败"那条变异）。
set -u

FLOOR="${CONTRACT_PIN_FLOOR:-110}"          # 与 cross_repo_contract.sh 的自证下限同源
HUB="${HUB_REPO:-}"
MP="${MINIPROGRAM_REPO:-}"

summary() {
  # 同步写进 step summary：日志会滚，跳过的事实在 job 页上必须留痕
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- $*" >> "$GITHUB_STEP_SUMMARY"
  fi
}

if [ -z "$HUB" ] || [ -z "$MP" ] \
   || [ ! -f "$HUB/src/server.js" ] \
   || [ ! -f "$MP/miniprogram/utils/cloud-gw.js" ]; then
  echo "::warning title=跨仓契约钉未执行::${FLOOR} 条三仓契约对账本轮**未执行**：对端仓不可见（HUB_REPO 或 MINIPROGRAM_REPO 未指向真仓）。本轮 CI 绿灯不代表三仓契约已核对。"
  echo "   HUB_REPO='${HUB:-<未设置}'  MINIPROGRAM_REPO='${MP:-<未设置}'"
  echo "   要让它真跑：给本仓配 hub 与小程序两个私有仓的**只读** deploy key，"
  echo "   secrets 名 HUB_REPO_DEPLOY_KEY / MINIPROGRAM_REPO_DEPLOY_KEY（workflow 里有对应 step）。"
  summary "跨仓契约钉 ${FLOOR} 条**未执行**（对端私有仓在公共 runner 不可见，HUB_REPO/MINIPROGRAM_REPO 未指向真仓）"
  exit 0
fi

out="$(bash "$(dirname "$0")/cross_repo_contract.sh" 2>&1)"
rc=$?
printf '%s\n' "$out"
if [ "$rc" -ne 0 ]; then
  echo "::error title=跨仓契约漂移::cross_repo_contract.sh rc=${rc}——三仓里「两边各写一份」的契约不一致"
  summary "跨仓契约**漂移**（rc=${rc}），见本步日志"
  exit "$rc"
fi

# 子脚本 rc=0 不等于"对账充分"：锚点漂移会抽到 0 条然后全绿。计数在这里复核一次。
n="$(printf '%s' "$out" | sed -n 's/.*跨仓契约: \([0-9][0-9]*\) passed.*/\1/p' | tail -1)"
if [ -z "$n" ]; then
  echo "::error title=对账条数不可读::没在输出里抓到「跨仓契约: N passed」——汇总行格式变了就得同步本入口"
  exit 1
fi
if [ "$n" -lt "$FLOOR" ]; then
  echo "::error title=对账条数不足::PASS 计数 ${n} < 下限 ${FLOOR}：对账脚本被改瘪了，不是「契约没问题」"
  summary "跨仓契约 PASS 计数 ${n} 低于下限 ${FLOOR}，判失败"
  exit 1
fi

echo "PASS 计数 ${n}（下限 ${FLOOR}）已复核"
summary "跨仓契约钉 ${n} 条全部通过（三仓对端已在本 runner 上 clone）"
