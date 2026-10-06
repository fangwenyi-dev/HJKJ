#!/usr/bin/env bash
# 跨仓契约钉（v1.7.47）：三端各写一份、谁都不会替谁报错的东西，第一次有测试对账。
#
# 为什么必须有：二维码载荷版本位、错误码表、kind/mid 字段名、成员上限、端点名——
# 这些在 hub / 加载项 / 小程序里各存一份，**没有任何编译期或单测能发现漂移**。
# 实测（2026-09-23 审查）：一侧把载荷版本位从 1 升到 2，另一侧不会红；hub 新增
# members_full，小程序没有中文文案，用户看到的就是兜底句。
#
# 缺对端仓时以 **rc=3 响亮跳过**（照 hub_lifecycle_driver.py:36-38 的纪律）：
# skip 变 exit 0 就等于门禁不存在——tests/test_v1747_cross_repo_contract.py 里有一条
# 元钉真跑"无对端仓"路径断言 rc==3。
set -u
PLUGIN_REPO="${PLUGIN_REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}"
HUB_REPO="${HUB_REPO:-}"
MINIPROGRAM_REPO="${MINIPROGRAM_REPO:-}"

for v in HUB_REPO MINIPROGRAM_REPO; do
  eval "val=\${$v}"
  if [ -z "$val" ]; then echo "SKIP 需要 $v 指向对端仓（私有仓，CI 侧不可见）"; exit 3; fi
done
[ -f "$HUB_REPO/src/server.js" ] || { echo "SKIP HUB_REPO 不像 hub 仓: $HUB_REPO"; exit 3; }
[ -f "$MINIPROGRAM_REPO/miniprogram/utils/cloud-gw.js" ] || { echo "SKIP MINIPROGRAM_REPO 不像小程序仓: $MINIPROGRAM_REPO"; exit 3; }

JS="$PLUGIN_REPO/huijian_mqtt_broker/www/js/huijian.js"
HTML="$PLUGIN_REPO/huijian_mqtt_broker/www/index.html"
PYHUB="$PLUGIN_REPO/huijian_mqtt_broker/custom_components/window_controller_gateway/hub_client.py"
WSGW="$PLUGIN_REPO/huijian_mqtt_broker/custom_components/window_controller_gateway/ws_gateway.py"
CONST="$PLUGIN_REPO/huijian_mqtt_broker/custom_components/window_controller_gateway/const.py"
HUBSRV="$HUB_REPO/src/server.js"
HUBSTORE="$HUB_REPO/src/store.js"
HUBREADME="$HUB_REPO/README.md"
CGW="$MINIPROGRAM_REPO/miniprogram/utils/cloud-gw.js"
GWR="$MINIPROGRAM_REPO/miniprogram/utils/gw-router.js"
WSJS="$MINIPROGRAM_REPO/miniprogram/utils/ws-gateway.js"
PAGE="$MINIPROGRAM_REPO/miniprogram/pages/broker-gateways/broker-gateways.js"
DEVPAGE="$MINIPROGRAM_REPO/miniprogram/pages/broker-device/broker-device.js"
for f in "$JS" "$HTML" "$PYHUB" "$WSGW" "$HUBREADME"; do [ -f "$f" ] || { echo "SKIP 对账所需文件缺失: $f"; exit 3; }; done

pass=0; fail=0
check() {  # check <名称> <shell 命令串>
  local name="$1"; shift
  if bash -c "$1" >/dev/null 2>&1; then pass=$((pass+1)); echo "PASS $name";
  else fail=$((fail+1)); echo "FAIL $name"; fi
}

echo "==== 跨仓契约（hub=$HUB_REPO 小程序=$MINIPROGRAM_REPO） ===="
# ① 二维码载荷：插件写什么，小程序就得认什么（版本位不认即拒，所以漂移＝扫码全废）
check "载荷前缀：插件写的版本位 = 小程序认的版本位" \
  "grep -qF \"BIND_PAYLOAD_PREFIX = 'HUJIAN-BIND:1:'\" '$JS' && grep -qF \"m[1] === '1'\" '$CGW'"
# ② 成员上限：hub 常量 = 加载项常量 = 小程序文案里的数字（文案写错＝用户以为还能加）
check "成员上限：hub 常量 = 加载项常量" \
  "grep -qE 'HUB_MEMBERS_MAX = 8' '$HUBSTORE' && grep -qE 'HUB_MEMBERS_MAX = 8' '$PYHUB'"
check "成员上限：小程序文案里的数字 = 8" "grep -qF '8 人' '$PAGE'"
# ③ 字段名：两侧各写一份的 JSON 键（拼错＝静默失效，hub 只会当成缺省值）
check "kind 字段名：加载项发 = hub 读" \
  "grep -qF '\"kind\": kind' '$PYHUB' && grep -qF 'body.kind' '$HUBSRV'"
check "mid 字段名：加载项发 = hub 从 body 解构并按 mid 定位" \
  "grep -qF '\"mid\": str(mid)' '$PYHUB' && grep -qF 'secret, mid } = body' '$HUBSRV' && grep -qF 'removeMemberByMid(' '$HUBSRV'"
check "ownerMasked 字段名：hub 回 = 加载项读" \
  "grep -qF 'ownerMasked' '$HUBSTORE' && grep -qF 'ownerMasked' '$PYHUB'"
# ④ 端点存在性：客户端调的每个路径，对端必须真的实现（否则线上必然 404）
for ep in /agent/register /agent/bindcode /agent/members /agent/unbind; do
  check "加载项调的 $ep 在 hub 侧存在" \
    "grep -qF \"$ep\" '$PYHUB' && grep -qF \"p === '$ep'\" '$HUBSRV'"
done
for ep in /bind /state /cmd /unbind; do
  check "小程序调的 $ep 在 hub 侧存在" \
    "grep -qF \"'$ep'\" '$CGW' && grep -qF \"p === '$ep'\" '$HUBSRV'"
done
# ⑤ 错误码：hub 会回的，用户侧必须有中文文案（否则用户看到裸英文或兜底句）
for code in members_full no_owner owner_cannot_leave unknown_member already_bound code_invalid; do
  check "绑定错误码 $code：hub 会回 且 小程序有中文文案" \
    "grep -qF \"$code\" '$HUBSTORE' && grep -qF \"$code:\" '$PAGE'"
done
# 控制面错误码：**两侧**都要对账。此前只 grep 小程序有文案，hub 把 send_failed 改个名
# 也不会红（文案还在，只是永远命中不了 ⇒ 用户看到兜底句）。判据用 `err: '<code>'` 这个
# 返回点字面量，而不是"这个字符串在某处出现过"。
for code in offline timeout forbidden send_failed superseded; do
  check "控制错误码 $code：hub 真会回（err: 字面量）且 小程序有中文文案" \
    "grep -qF \"err: '$code'\" '$HUBSRV' && grep -qF \"$code:\" '$GWR'"
done
# 身份类：云托管没注入 openid 时 hub 回 401 no_openid，小程序必须说清"用正式版/体验版打开"
check "身份错误码 no_openid：hub 真会回 且 小程序有中文文案" \
  "grep -qF \"err: 'no_openid'\" '$HUBSRV' && grep -qF 'no_openid:' '$PAGE'"
# 加载项侧（/agent/* 用实例凭据）：这两个码决定面板说"云端不认识本机凭据"还是"读取失败"。
# hub 侧两个文件都要找：bad_secret 是 server.js 直接回的，unknown_instance 是 store.js 回的、
# 由 handler 原样透传（写死只查 server.js 会假红——这条钉第一次跑就自己咬出来了）。
for code in bad_secret unknown_instance; do
  check "加载项侧错误码 $code：hub 真会回 且 面板有中文文案" \
    "{ grep -qF \"err: '$code'\" '$HUBSRV' || grep -qF \"err: '$code'\" '$HUBSTORE'; } && grep -qF \"case '$code':\" '$JS'"
done
# ⑥ role：hub 回、小程序存（老 hub 不回时小程序兜 owner，这条钉的是"两侧都知道有 role"）
#    role 的字面量在 hub 的 store.js（bindByCode 的返回体），server.js 只是透传+记日志
check "role 字段：hub 回字面量 owner/member = 小程序读 res.role" \
  "grep -qF \"role: 'owner'\" '$HUBSTORE' && grep -qF \"role: 'member'\" '$HUBSTORE' && grep -qF 'res.role' '$CGW'"
# ⑦ 速度/力度**回显**字段：加载项发 = 小程序读（v1.7.47 之前加载项两条通道都不回传，
#    小程序滑块只能显示本地记忆值 ⇒ 换手机/在 HA 里调过就对不上，用户报"调了没反应"）
for f in winactSpeed winactStrength; do
  check "回显字段 $f：加载项发 且 小程序 LAN+云两侧都读" \
    "grep -qF \"$f\" '$WSGW' && grep -qF \"$f\" '$WSJS' && grep -qF \"$f\" '$CGW'"
done
check "回显字段 windLockMode：加载项列表视图也带（此前只有推送与云端带）" \
  "grep -qF 'windLockMode' '$WSGW' && grep -qF 'windLockMode' '$WSJS'"
# ⑧ 百分比入参口径：加载项 int()+裁剪+不可解析即拒；小程序必须同口径（取整 + NaN 不发）
check "速度/力度入参：小程序取整且拒绝不可解析值（与加载项 int() 同口径）" \
  "grep -qF 'Math.round' '$GWR' && grep -qF 'isFinite' '$GWR' && grep -qF 'Math.round' '$DEVPAGE'"

# ⑨ WS 凭据走请求头（secret 不再进 URL query ⇒ 不落任何记 request line 的中间层日志）。
#    hub 侧的 query 兜底是**发版顺序安全网**：必须 hub 先上，老加载项（仍发 query）才不断；
#    删掉兜底＝发版顺序一反就全员掉线，所以这条钉的是"兜底不许被顺手清理掉"。
check "WS 凭据头 x-hub-instance-id：加载项发 = hub 读" \
  "grep -qF 'x-hub-instance-id' '$PYHUB' && grep -qF \"h['x-hub-instance-id']\" '$HUBSRV'"
check "WS 凭据头 x-hub-secret：加载项发 = hub 读" \
  "grep -qF 'x-hub-secret' '$PYHUB' && grep -qF \"h['x-hub-secret']\" '$HUBSRV'"
check "hub 保留 query 兜底（发版顺序安全网，不许清理）" \
  "grep -qF \"searchParams.get('instanceId')\" '$HUBSRV' && grep -qF \"searchParams.get('secret')\" '$HUBSRV'"
check "加载项不再把凭据拼进 WS URL query" \
  "! grep -qF 'agent/ws?instanceId' '$PYHUB'"

# ⑩ WS 消息词汇：加载项与 hub 各写一份，任一侧改字面量＝命令或状态整条静默失效
check "WS 下行命令 t='cmd'：hub 发 = 加载项认" \
  "grep -qF \"t: 'cmd'\" '$HUBSRV' && grep -qF '\"t\") != \"cmd\"' '$PYHUB'"
check "WS 回执 t='cmd_result'：加载项发 = hub 认" \
  "grep -qF '\"t\": \"cmd_result\"' '$PYHUB' && grep -qF \"msg.t === 'cmd_result'\" '$HUBSRV'"
check "WS 状态 t='state'：加载项发 = hub 认" \
  "grep -qF '\"t\": \"state\"' '$PYHUB' && grep -qF \"msg.t === 'state'\" '$HUBSRV'"
check "WS 命令号 cmdsn：加载项回 = hub 用它配对回执" \
  "grep -qF 'cmdsn' '$PYHUB' && grep -qF 'msg.cmdsn' '$HUBSRV'"
check "WS 状态批量键 items：加载项发 = hub 读" \
  "grep -qF '\"items\": items' '$PYHUB' && grep -qF 'msg.items' '$HUBSRV'"
check "命令参数键 attribute：加载项校验 = 小程序下发" \
  "grep -qF 'params.get(\"attribute\")' '$PYHUB' && grep -qF 'attribute' '$CGW'"

# ⑪ 操作判别键在两条通道**故意不同名**：LAN 是 cmd、云是 action。今天两端各自对得上，
#    谁"顺手统一"一下就静默断一边（加载项回 unknown_action，用户只看到"不支持的操作"）。
check "LAN 判别键 cmd:'control'：加载项认 = 小程序发" \
  "grep -qF 'cmd == \"control\"' '$WSGW' && grep -qF \"cmd: 'control'\" '$WSJS'"
check "云 判别键 action:'control'：加载项认 = 小程序发（与 LAN 故意不同名，别去统一）" \
  "grep -qF 'msg.get(\"action\") != \"control\"' '$PYHUB' && grep -qF \"action: 'control'\" '$CGW'"

# ⑫ 绑定码 TTL 三处各写一份：hub 的毫秒常量、加载项的兜底秒常量、面板给用户看的文案。
#    漂移的形态是"面板对着一张云端已作废的码继续倒计时"，用户扫到 code_invalid 且无人解释。
check "绑定码 TTL 三处对账：hub 600000ms / 加载项 600s 兜底 / 面板 10 分钟文案" \
  "grep -qF '10 * 60 * 1000' '$HUBSTORE' && grep -qE 'BIND_CODE_TTL_S = 600' '$PYHUB' && grep -qF '10 分钟' '$JS'"

# ⑬ 权威值在云端：hub 回 expiresInSec / membersMax，加载项必须优先采用、本地常量只当兜底。
#    用自己的常量盖掉云端值＝hub 一改上限或有效期，面板就开始对用户说谎。
check "加载项优先采用 hub 回的 expiresInSec（本地 TTL 只兜底）" \
  "grep -qF 'data.get(\"expiresInSec\")' '$PYHUB' && grep -qF 'expiresInSec' '$HUBSTORE'"
check "加载项优先采用 hub 回的 membersMax（本地常量只兜底）" \
  "grep -qF 'data.get(\"membersMax\")' '$PYHUB' && grep -qF 'membersMax' '$HUBSTORE'"

# ⑭ hub v0.2.6 新增错误码：会回就必须两端都有中文文案（否则用户看到裸英文或笼统兜底句）。
#    hub 侧的判据用 `err: '<code>'` 返回点字面量；此前写 `grep -qF '$code' 文件A 文件B`，
#    而 grep 多文件只要**任一**命中就 exit 0 ⇒ 那条钉的语义是 OR 而不是注释里说的"hub 会回"。
for code in registry_full rate_limited superseded; do
  check "新错误码 $code：hub 真会回（err: 字面量）且 加载项面板有中文文案" \
    "{ grep -qF \"err: '$code'\" '$HUBSRV' || grep -qF \"err: '$code'\" '$HUBSTORE'; } && grep -qF \"case '$code':\" '$JS'"
done
check "新错误码 rate_limited：小程序绑定失败也有中文文案" \
  "grep -qF 'rate_limited:' '$PAGE'"
# superseded 的小程序文案已由上面「控制错误码」那组两侧对账（更强的判据），这里不重复钉

# ⑮ 加载项自己产的回执错误码（经 hub 原样透传给小程序）：此前无人对账，改名不会红
for code in control_failed control_unavailable invalid_params unknown_action; do
  check "加载项回执码 $code：加载项会回 且 小程序有中文文案" \
    "grep -qF '\"$code\"' '$PYHUB' && grep -qF '$code:' '$GWR'"
done

# ⑯ 数据流贯通（治假绿）：字段"在某文件里出现过"证明不了它走到了页面。
#    v1.4.28 的速度/力度回显就是这么漏的——normalizeStates 产出了 winact*，但 gw-router
#    的云缓存构造把它们丢了，而钉只 grep 了 cloud-gw.js ⇒ 字符串在、值不在，28 条全绿。
#    **但标识符级的 grep 本身也是假绿**：v1.7.50 复审实测过——把 gw-router 首建分支那两行
#    真赋值整行删掉（即把 v1.4.28 的回归原样放回去），解释这个 bug 的注释里就有
#    winactSpeed，55 条照样全绿。所以这里钉**赋值语法**，注释满足不了。
#    行为级的最终证明在小程序仓 tests/winact-echo.test.js（真跑 _pollCloud 断言 _cloudCache）。
for f in winactSpeed winactStrength; do
  check "回显字段 $f 首建分支必须真赋值进云缓存（钉语法；标识符级 grep 会被注释满足＝假绿）" \
    "grep -qE '$f: *inRange\(item\.$f, *0, *100\) *\? *item\.$f *: *-1' '$GWR'"
  check "回显字段 $f 更新分支必须用合法值覆盖已知值（漏了＝云模式冻结在首次轮询的值）" \
    "grep -qE 'if \(inRange\(item\.$f, *0, *100\)\) *dev\.$f *= *item\.$f' '$GWR'"
done
# position 走同一个更新分支、同一类漏测（复审时一并验出：删掉它两仓的测试全绿）
check "回显字段 position 更新分支必须用合法值覆盖（与 winact* 同一分支、同一类漏测）" \
  "grep -qE 'if \(inRange\(item\.position, *0, *100\)\) *dev\.position *= *item\.position' '$GWR'"

# ⑰ LAN 回执文案：小程序 LAN_ACK_TEXT / LAN_TOKEN_ACK_TEXT 的 key 是加载项 ws_gateway.py
#    里英文字面量的**精确副本**（改一个字母就静默退化成「操作失败（原文）」）。这是三端里
#    唯一一处"整句字符串逐字对账"，此前无人钉。用 grep -F 精确匹配，不做任何模糊化。
#    **必须连着引号一起匹配**：裸串会被注释满足——实测把加载项的 "send failed" 改成
#    "send faild"，裸串钉照样全绿，因为同文件有一行注释提到了 send failed。
#    （14 个 key 在加载项里一律是双引号字面量，无单引号形态。）
ack_keys=$(sed -n "/^const LAN_ACK_TEXT = {/,/^}/p;/^const LAN_TOKEN_ACK_TEXT = {/,/^}/p" "$WSJS" \
           | grep -oE "^  '[^']+':" | sed "s/^  '//; s/':$//")
n_ack=0
while IFS= read -r k; do
  [ -z "$k" ] && continue
  n_ack=$((n_ack + 1))
  check "LAN 回执文案「$k」：小程序映射的原文必须在加载项里以字符串字面量逐字存在" \
    "grep -qF '\"$k\"' '$WSGW'"
done <<< "$ack_keys"
# 元钉：抽取量为 0 时上面一条都不会跑 ⇒ 全绿。没有这条，锚点漂移就等于门禁不存在。
check "LAN 回执文案抽取量 ≥14（防空扫假绿：sed 锚点漂移会抽到 0 条然后全绿）" \
  "[ $n_ack -ge 14 ]"

# ⑱ 面板的有效期文案必须**真消费**服务端回的 TTL。只钉"面板里有 10 分钟字样"是反过来的：
#    加载项哪天停发 bindCodeTtlS，面板会静默退回硬编兜底，而这条钉照过。
check "面板 TTL 文案真消费服务端的 bindCodeTtlS（不是硬编「10 分钟」）" \
  "grep -qF '\"bindCodeTtlS\"' '$PYHUB' && grep -qF 'ttlMinutes(info.bindCodeTtlS' '$JS'"
check "面板 TTL 的落点元素 #hubCodeTtl 两侧都在（缺了 JS 就静默 no-op）" \
  "grep -qF 'id=\"hubCodeTtl\"' '$HTML' && grep -qF \"getElementById('hubCodeTtl')\" '$JS'"
check "面板成员码提示的落点元素 #hubMemberTip 两侧都在" \
  "grep -qF 'id=\"hubMemberTip\"' '$HTML' && grep -qF \"getElementById('hubMemberTip')\" '$JS'"

# ⑲ 云回执的关联字段：小程序的在途命令精确配对靠 hub /cmd 响应体里的 cmdsn
#    （v1.4.29 才接上；hub 一直在回，是 router 层把它丢了）。
check "云回执关联字段 cmdsn：hub 的 /cmd 响应体带它 且 小程序 router 真读" \
  "grep -qF 'cmdsn: cmd.cmdsn' '$HUBSRV' && grep -qF 'res.cmdsn' '$GWR'"
check "云回执关联字段 attribute：router 合成 control_ack 时带上（页面按它配对在途命令）" \
  "grep -qF 'res.attribute' '$DEVPAGE' && grep -qE 'attribute,$' '$GWR'"

# ⑳ _bindErrText 不许有死条目。它只有一个调用点（绑定结果 toast），所以判据是
#    "小程序调的那四条路由（/bind /state /cmd /unbind）会不会回这个码"，而不是"hub 有没有
#    这个码"。registry_full 只从 /agent/register 回（加载项的注册通道）⇒ 小程序永远收不到，
#    v1.4.29 把它加进来时正是漏了这一步——而同一批刚因为同样的理由删掉了 not_owner。
#    下面的可达清单是逐个返回点核出来的；hub 加新码时改这里＝强制过一次"这条真的可达吗"。
MP_REACHABLE=" code_invalid already_bound members_full no_owner no_openid rate_limited"
MP_REACHABLE="$MP_REACHABLE forbidden owner_cannot_leave unknown_member offline timeout send_failed superseded"
MP_REACHABLE="$MP_REACHABLE bad_json internal not_found not_bound cloud_unavailable call_failed bad_response "
bind_keys=$(sed -n '/_bindErrText(err) {/,/^  },/p' "$PAGE" | grep -oE "^      [a-z_]+:" | sed 's/^      //; s/:$//')
n_keys=0
for k in $bind_keys; do
  n_keys=$((n_keys + 1))
  check "_bindErrText 的 $k 不是死条目（小程序调的路由真能收到它）" \
    "printf '%s' '$MP_REACHABLE' | grep -qF ' $k '"
done
check "_bindErrText 键抽取量 ≥10（防空扫假绿）" "[ $n_keys -ge 10 ]"
# 反向：hub 会从 /bind 回的码，小程序必须都有文案（漏一个＝用户看到「绑定失败（原码）」兜底）
for code in code_invalid already_bound members_full no_owner rate_limited; do
  check "/bind 会回的 $code：hub 真会回 且 小程序有专门文案（不许只靠兜底）" \
    "{ grep -qF \"err: '$code'\" '$HUBSRV' || grep -qF \"err: '$code'\" '$HUBSTORE'; } && grep -qF '$code:' '$PAGE'"
done
# no_openid 已由上面「身份错误码」那条两侧对账，这里不重复钉

# ㉑ 运维指路必须与 hub 同口径。v0.2.6 把"必须配存储挂载"翻成了"**不要**配"（云开发桶上
#    实测必失败：cosfs endpoint 缺 scheme ⇒ 挂载钩子 exit 1 ⇒ Pod 起不来），而加载项熔断时
#    的 ERROR 话术还在叫运维去配 ⇒ 照着做会把 hub 直接搞成起不来，比原故障更糟。
#    这类"两仓各写一份的运维结论"没有任何编译期/单测能发现漂移。判据刻意写死字面量：
#    任一侧改措辞就该有人重新对一次账，而不是让它静默漂回去。
check "运维口径：hub README 说不要配存储挂载时，加载项不得叫运维去配（旧话术会让 Pod 起不来）" \
  "grep -qF '不要配「存储挂载」' '$HUBREADME' && grep -qF '改去配「存储挂载」' '$PYHUB' && ! grep -qF '确认云托管「存储挂载」已挂到' '$PYHUB'"
check "运维口径：加载项熔断指路必须指向 hub 的注册表镜像（真正的跨重建存活条件）" \
  "grep -qF 'mirror.enabled' '$PYHUB' && grep -qF 'mirror.enabled' '$HUBREADME'"

# ㉒ LAN 回执的关联字段（v1.7.52 新增的跨仓契约）：加载项回带 attribute+cmdsn，
#    小程序发 cmdsn 并把两个字段透传给页面做精确配对。任一侧单独改＝配对静默退回
#    弱 FIFO（表现为"点打开却把速度滑块回退了"这类查不到根因的错乱）。
check "LAN 回执关联字段：加载项 control_ack 回带 attribute 与 cmdsn" \
  "grep -qF 'out[\"attribute\"] = attribute' '$WSGW' && grep -qF 'out[\"cmdsn\"] = cmdsn' '$WSGW'"
check "LAN 回执关联字段：小程序发 cmdsn 且把两个字段透传给页面" \
  "grep -qF 'cmdsn: newLanCmdsn()' '$WSJS' && grep -qF 'attribute: msg.attribute' '$WSJS' && grep -qF 'cmdsn: msg.cmdsn' '$WSJS'"
# 反向（防"单边加了没人用"）：加载项回带了，页面必须真读
check "页面真读关联字段做配对（加载项回带但没人读＝白加一个字段）" \
  "grep -qE 'res\.attribute|res && res\.attribute' '$DEVPAGE'"

# ㉓ 004 属性名与命令值：三端各写一份的字面量（此前无人对账，我这次是手工逐个核的）。
#    加载项 const.py 是权威源，小程序抄一份——抄错就是"命令发了设备不动"，
#    而且两侧单测都不会红（各自都自洽）。
for lit in w_travel rwp_wind_lock_mode rwp_winact_speed rwp_winact_strength; do
  check "004 属性名 $lit：加载项 const 里有这个字面量 且 小程序也写着同一串" \
    "grep -qF \"\\\"$lit\\\"\" '$CONST' && grep -qF \"'$lit'\" '$WSJS'"
done

# ㉔ 取消 str 豁免的**前提**必须两侧同时成立：
#    ① 两条通道对同一个 value 的判据必须**同一实现**（v1.7.65 起云侧直接委托 ws_gateway；
#       此前是两份拷贝靠注释"模式串必须同串"维系，而各自漏检的东西不同——云侧从不校验
#       属性名与值域，"同串"根本挡不住 attribute 打错字回 ok:true）；
#    ② 小程序实际下发的值全是十进制串，否则格式闸会静默挡掉真命令。
check "两条通道判据同一实现：云侧委托 ws_gateway.validate_control_command" \
  "grep -qF 'from .ws_gateway import validate_control_command' '$PYHUB' && grep -qE '^def validate_control_command' '$WSGW'"
# 反向钉：不许再出现"云侧自己抄一份格式闸"（抄回去＝分裂行为重新开门）
check "反钉：hub_client 不得再自带 _VALUE_RE 副本" \
  "! grep -qE '^_VALUE_RE = re.compile' '$PYHUB'"

non_decimal=""
for v in $(grep -oE "^const VALUE_[A-Z_]+ = '[^']*'" "$WSJS" | sed "s/^const //; s/ = '/=/; s/'$//"); do
  case "${v##*=}" in
    ''|*[!0-9]*) non_decimal="$non_decimal $v" ;;
  esac
done
check "小程序下发的 VALUE_* 全是十进制串（取消 str 豁免的合法性前提）" \
  "test -z '$non_decimal'"
# 元钉：上面那条抽取若锚点漂移会抽到 0 条、然后"全是十进制"空判通过
n_val=$(grep -cE "^const VALUE_[A-Z_]+ = " "$WSJS")
check "VALUE_* 抽取量 ≥4（防空扫假绿：锚点漂移会让上一条钉空判通过）" \
  "[ $n_val -ge 4 ]"

# ㉕ 属性全集**双向**相等（v1.7.65 域表）：加载项 CONTROL_ATTR_DOMAINS 的键必须恰好等于
#    小程序实际会下发的那组属性，清单从两侧**真源码**抽取（不手写——手写的清单只证明我自己）。
#    单向"小程序的都在表里"挡不住两类事故：表里少一个 → 真命令被新闸挡掉（用户点不动）；
#    表里多一个 → 那个属性没人会发，却仍留在"发得出去"的面上。
mp_attrs=$(grep -oE "^const ATTR_[A-Z_]+ = '[^']*'" "$WSJS" | sed "s/.*= '//; s/'$//" | sort -u)
dom_attrs=""
for nm in $(sed -n '/^CONTROL_ATTR_DOMAINS/,/^}/p' "$CONST" | grep -oE 'ATTRIBUTE_[A-Z_]+' | sort -u); do
  lit=$(grep -oE "^${nm}: Final = \"[^\"]+\"" "$CONST" | sed 's/.*"\(.*\)".*/\1/')
  [ -n "$lit" ] && dom_attrs="$dom_attrs
$lit"
done
dom_attrs=$(printf '%s\n' "$dom_attrs" | sed '/^$/d' | sort -u)
n_mp=$(printf '%s\n' "$mp_attrs" | sed '/^$/d' | wc -l)
n_dom=$(printf '%s\n' "$dom_attrs" | sed '/^$/d' | wc -l)
if [ "$mp_attrs" = "$dom_attrs" ] && [ "$n_mp" -ge 4 ] && [ "$n_dom" -ge 4 ]; then
  attr_verdict=ok
else
  attr_verdict="不等或抽取为空(mp=$n_mp,dom=$n_dom)：小程序=$(echo $mp_attrs | tr '\n' ' ') 域表=$(echo $dom_attrs | tr '\n' ' ')"
fi
check "属性全集双向相等：小程序下发 == 加载项域表键，且两侧各抽到 ≥4 条（$attr_verdict）" \
  "test ok = '$attr_verdict'"

echo
echo "跨仓契约: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
exit 0
