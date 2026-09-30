#!/usr/bin/with-contenv bashio
# =============================================================================
# 慧尖 LoRa 网关一体化插件 — 启动脚本（版本号以 config.yaml 为准）
#
# 架构（host_network 模式）：
#   - 容器直接使用主机网络，mosquitto 监听主机 2022 端口
#   - mDNS 用 avahi-publish 注册 _mqtt._tcp 服务，LoRa 网关自动发现 huijian.local
#   - auto_setup 和 HA Core 都用 127.0.0.1:2022 连接 broker
# =============================================================================

set -e
# v1.7.33（全量审计）：补 pipefail——只有 set -e 时"管道左端失败、右端成功"
# 会被整体判成功（v1.6.4 的 cat|jq / cut|tr 两处死兜底即同族事故）。
set -o pipefail

USERNAME=$(bashio::config 'username')
PASSWORD=$(bashio::config 'password')

# v1.6.3：用户名白名单校验（H4 根治）——用户名会拼进密码文件/ACL/heredoc，
# 含 % \ 换行等字符可破坏 printf 输出与 acl 解析；非法则拒绝启动，明确报错。
# v1.7.18（第 7 轮审计 BUG-21）：整体上移到"凭据自动恢复"之前——旧顺序下
# 非法用户名总是先被恢复块改写成 huijian 才走到校验，这道防线实际不可达；
# 且恢复提示把未校验原值回显进启动日志（ANSI/CR 控制符注入面）。报错不再
# 回显原值，通过白名单后的恢复提示回显即安全。
case "${USERNAME}" in
    ''|*[!A-Za-z0-9_-]*)
        echo "[错误] MQTT 用户名非法（仅允许字母/数字/下划线/连字符），拒绝启动"
        exit 1
        ;;
esac

# v1.7.12 凭据自动恢复默认（用户定案）：LoRa 网关固件内置 huijian/huijian2022
# （端口本就在 :31 写死 2022），用户改配置页凭据只会把网关挡在门外
# （not authorised 风暴且无从自救）——凡偏离固件内置值一律自动恢复默认并提示，
# 配置保持默认时本段零输出、全程无感。config.yaml schema 同步提供默认值。
FIRMWARE_MQTT_USER="huijian"
FIRMWARE_MQTT_PASS="huijian2022"
if [ "${USERNAME}" != "${FIRMWARE_MQTT_USER}" ]; then
    echo "[凭据自动恢复] 配置用户名 '${USERNAME}' ≠ 网关固件内置 '${FIRMWARE_MQTT_USER}'，已恢复默认（固件内置凭据，请勿修改）"
    USERNAME="${FIRMWARE_MQTT_USER}"
fi
if [ "${PASSWORD}" != "${FIRMWARE_MQTT_PASS}" ]; then
    echo "[凭据自动恢复] 配置密码 ≠ 网关固件内置值，已恢复默认（密码不回显；固件内置凭据，请勿修改）"
    PASSWORD="${FIRMWARE_MQTT_PASS}"
fi
AUTO_SETUP=$(bashio::config 'auto_setup_ha_mqtt')
INSTALL_INTEGRATION=$(bashio::config 'install_integration')
# v1.7.11 快速自动发现代理开关（零条目 HA 首台网关全自动配置、第二台起弹
# 发现卡片，详见 gateway_discovery_proxy.py 头注释；关闭后仅剩传统等待路径）
FAST_DISCOVERY=$(bashio::config 'fast_auto_discovery')

# （用户名白名单校验已上移至凭据恢复之前，v1.7.18 BUG-21）

# host_network 模式：mosquitto 直接监听主机 2022 端口，无需 Docker 端口映射
MQTT_PORT=2022
INTERNAL_PORT=2022

echo "============================================"
echo "  慧尖 LoRa 网关一体化插件启动中..."
echo "============================================"
echo "[配置] MQTT 用户名: ${USERNAME}"
echo "[配置] MQTT 端口: ${MQTT_PORT} (host_network 模式)"
echo "[配置] 自动配置 HA MQTT 集成: ${AUTO_SETUP}"
echo "[配置] 自动安装网关集成: ${INSTALL_INTEGRATION}"
echo ""

# ---------- 1. 生成密码文件 ----------
PASSWD_FILE="/etc/mosquitto/passwd"

# 先删除旧密码文件（避免 -c 失败时残留损坏文件）
rm -f "${PASSWD_FILE}" 2>/dev/null || true

# 用 -c 创建新文件，-b 批量模式（不提示输入密码）
if mosquitto_passwd -c -b "${PASSWD_FILE}" "${USERNAME}" "${PASSWORD}"; then
    echo "[OK] 密码文件已生成"
else
    # 兜底：手动用 printf 写入密码哈希（适用于 mosquitto_passwd 不可用的场景）
    # Mosquitto 2.x 密码文件支持格式：
    #   $7$（PBKDF2，mosquitto_passwd 默认）、$6$（SHA-512 crypt）、
    #   {SHA}/{SSHA}/{BCRYPT} 前缀。
    # openssl passwd -6 生成 $6$（SHA-512 crypt）格式，Mosquitto 可直接验证。
    # 注意：不能用 openssl dgst -sha256 手动拼 $6$ 前缀——那是无效格式，
    # 会导致所有客户端认证失败（此前的历史 bug，已修复）。
    echo "[警告] mosquitto_passwd 失败，尝试用 openssl passwd 创建密码文件"
    SALT=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 16)
    HASH=$(printf "%s\n" "${PASSWORD}" | openssl passwd -6 -salt "${SALT}" -stdin 2>/dev/null)
    if [ -n "${HASH}" ]; then
        # v1.6.3：必须用 printf '%s:%s\n'——直接把变量当格式串时，
        # 用户名含 % 或反斜杠转义（如 %s、\c）会被 printf 解释，写出损坏的哈希行
        printf '%s:%s\n' "${USERNAME}" "${HASH}" > "${PASSWD_FILE}" 2>/dev/null || {
            echo "[错误] 无法创建密码文件"
            exit 1
        }
        echo "[OK] 已用 openssl passwd 生成密码文件（$6$ SHA-512 crypt 格式）"
    else
        echo "[错误] openssl passwd 不可用，无法创建密码文件"
        exit 1
    fi
fi

# 验证密码文件非空
if [ ! -s "${PASSWD_FILE}" ]; then
    echo "[错误] 密码文件为空"
    exit 1
fi

# ---------- 1a. 创建 ha_mqtt 用户（HA MQTT 集成专用） ----------
# Bug5 修复：分离用户收紧权限。
# - ${USERNAME}（huijian）：LoRa 网关 / 客户端使用，ACL 仅授权网关协议主题，
#   不能读写 homeassistant/#（防凭据泄露后被用来伪造 HA 发现消息）。
# - ha_mqtt：HA MQTT 集成使用，ACL 授权 homeassistant/#（MQTT discovery 必需）。
# 密码与 ${PASSWORD} 相同（均来自插件配置），权限隔离按用户名由 ACL 保证。
HA_MQTT_USERNAME="ha_mqtt"
HA_MQTT_USER_CREATED=false
if command -v mosquitto_passwd >/dev/null 2>&1; then
    if mosquitto_passwd -b "${PASSWD_FILE}" "${HA_MQTT_USERNAME}" "${PASSWORD}" 2>/dev/null; then
        HA_MQTT_USER_CREATED=true
        echo "[OK] HA MQTT 用户已创建: ${HA_MQTT_USERNAME}"
    else
        echo "[警告] 创建 HA MQTT 用户失败（降级：HA 集成将使用 ${USERNAME} 连接）"
    fi
else
    # 兜底：手动用 openssl 追加哈希（mosquitto_passwd 不可用场景）
    # openssl passwd -6 生成 $6$（SHA-512 crypt），Mosquitto 2.x 可验证
    HA_SALT=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 16)
    HASH=$(printf "%s\n" "${PASSWORD}" | openssl passwd -6 -salt "${HA_SALT}" -stdin 2>/dev/null)
    if [ -n "${HASH}" ]; then
        printf '%s:%s\n' "${HA_MQTT_USERNAME}" "${HASH}" >> "${PASSWD_FILE}" 2>/dev/null && HA_MQTT_USER_CREATED=true
    fi
fi

# v1.6.24：z2m 直连慧尖内置 broker 的专用最小权限账号（官方 7.x 强制认证
# 环境下的推荐共存路径：z2m 加载项配置 mqtt://<本机>:2022 + 本账号）。
# 创建失败仅影响直连路径（桥路径不依赖），不降级不告警刷屏。
Z2M_USERNAME="huijian_z2m"
if command -v mosquitto_passwd >/dev/null 2>&1; then
    mosquitto_passwd -b "${PASSWD_FILE}" "${Z2M_USERNAME}" "${PASSWORD}" 2>/dev/null \
        && echo "[OK] z2m 直连账号已创建: ${Z2M_USERNAME}" \
        || echo "[提示] z2m 直连账号创建失败（不影响桥共存路径）"
fi

# bootstrap 标记使用哪个用户：ha_mqtt 创建成功才用，否则回退 ${USERNAME}
BOOTSTRAP_USERNAME="${USERNAME}"
if [ "${HA_MQTT_USER_CREATED}" = "true" ]; then
    BOOTSTRAP_USERNAME="${HA_MQTT_USERNAME}"
fi

chmod 600 "${PASSWD_FILE}"
chown mosquitto:mosquitto "${PASSWD_FILE}" 2>/dev/null || true
echo "[OK] 密码文件权限已设置 (600, mosquitto:mosquitto)"

# v1.6.3：删除"密码文件内容诊断"打印——即使命令端截断，前 20 字符
# 已足够泄露盐值与算法参数，属凭据外泄面

# ---------- 1a+. 共存桥追加主题白名单（v1.7.19 方案一） ----------
# BEGIN BRIDGE-TOPICS-SANITIZER（test_v1719 逐字抽出执行——移动/改名须同步锚）
# 背景（共存根因）：HA 的 MQTT 集成全局单条目，慧尖为保证网关可达必须把它
# 钉在内置 broker(:2022)；官方「Mosquitto broker」上其他生态（ESPHome/
# Tasmota/自研前缀…）要与 HA 互通，唯一通道是共存桥——但旧桥只接
# zigbee2mqtt/# 与 homeassistant/# 两棵树，其他加载项"能发现、控不了"半残。
# 本净化器把 coexist_bridge_topics 的追加树（逗号分隔 pattern[:in|out|both]，
# 方向缺省 both）渲染成两组逐条对齐的行：
#   BRIDGE_TOPICS_EXTRA → 桥块 topic 腿；BRIDGE_ACL_EXTRA → ha_mqtt ACL
#   （in→read / out→write / both→readwrite，维持 v1.6.24"爆炸半径不超桥腿"）。
# 代码级红线（配置不可解除）：
#   1) gateway/test 保留树拒绝（穿桥未认证开窗链，v1.6.24 安全评审定案）；
#   2) 首层必须具名字面量（禁 #/+ 开头——全匹配会圈进慧尖树）；'#' 仅可
#      末层、'+' 仅可整层（mosquitto conf 拒载形态在写入前拦截）；
#   3) 字符白名单挡 conf/ACL 注入（$、空格、反引号、分号、换行全部不可达）；
#   4) homeassistant 树方向钳制为 in（out 会把内置侧心跳回灌官方 broker，
#      v1.6.24 定案），zigbee2mqtt 树默认桥已双向——两者重复请求仅提示忽略；
#   5) 上限 16 树（防配置误刷导致 conf 膨胀）。
BRIDGE_TOPICS_RAW=$(bashio::config 'coexist_bridge_topics' 2>/dev/null || true)
BRIDGE_TOPICS_EXTRA=""
BRIDGE_ACL_EXTRA=""
BRIDGE_EXTRA_TREES=0
_seen_pats=" "
if [ -n "${BRIDGE_TOPICS_RAW}" ]; then
    IFS=',' read -r -a _BTOK_ARR <<< "${BRIDGE_TOPICS_RAW}"
    for _btok in "${_BTOK_ARR[@]}"; do
        _btok=${_btok//[[:space:]]/}
        [ -z "${_btok}" ] && continue
        _bpat="${_btok}"
        _bdir=both
        case "${_btok}" in
            *:*) _bpat="${_btok%%:*}"; _bdir="${_btok##*:}" ;;
        esac
        _bdir=$(printf '%s' "${_bdir}" | tr 'A-Z' 'a-z')
        case "${_bdir}" in
            in|out|both) ;;
            *) echo "[共存桥] 拒绝 '${_btok}'：方向只允许 in/out/both"; continue ;;
        esac
        if [ "${#_bpat}" -gt 100 ] || [ -z "${_bpat}" ]; then
            echo "[共存桥] 拒绝 '${_btok}'：主题为空或超长(>100)"; continue
        fi
        if [ "${_bpat}" != "$(printf '%s' "${_bpat}" | tr -cd 'A-Za-z0-9_./#+-')" ]; then
            echo "[共存桥] 拒绝 '${_btok}'：含非法字符（仅允许字母/数字/_-./#+）"; continue
        fi
        _bfs="${_bpat%%/*}"
        if [ -z "${_bfs}" ]; then
            echo "[共存桥] 拒绝 '${_btok}'：首层必须为具名字面量"; continue
        fi
        case "${_bfs}" in
            *'+'*|*'#'*)
                echo "[共存桥] 拒绝 '${_btok}'：首层禁通配（全匹配会圈进慧尖保留树）"; continue ;;
        esac
        case "${_bfs}" in
            [Gg][Aa][Tt][Ee][Ww][Aa][Yy])
                echo "[共存桥] 拒绝 '${_btok}'：gateway 树禁跨桥（安全红线，配置不可解除）"; continue ;;
            [Tt][Ee][Ss][Tt])
                echo "[共存桥] 拒绝 '${_btok}'：test 健康检查树保留"; continue ;;
            [Hh][Oo][Mm][Ee][Aa][Ss][Ss][Ii][Ss][Tt][Aa][Nn][Tt])
                echo "[共存桥] homeassistant 树默认桥已含 in 腿（out 永久钳制，防心跳回灌），忽略 '${_btok}'"; continue ;;
            [Zz][Ii][Gg][Bb][Ee][Ee]2[Mm][Qq][Tt][Tt])
                echo "[共存桥] zigbee2mqtt 树默认桥已双向，忽略 '${_btok}'"; continue ;;
        esac
        if printf '%s' "${_bpat}" | awk -F/ '{
                for (i = 1; i <= NF; i++) {
                    l = $i
                    if (l ~ /^[A-Za-z0-9_.-]+$/) continue
                    if (i < NF && l == "+") continue
                    if (i == NF && l == "#") continue
                    print "bad"; exit
                }
            }' | grep -q '^bad$'; then
            echo "[共存桥] 拒绝 '${_btok}'：通配符须整层（+）且 # 只能位于末层"; continue
        fi
        case "${_seen_pats}" in
            *" ${_bpat} "*) echo "[共存桥] 忽略重复主题 '${_bpat}'"; continue ;;
        esac
        _seen_pats="${_seen_pats}${_bpat} "
        if [ "${BRIDGE_EXTRA_TREES}" -ge 16 ]; then
            echo "[共存桥] 已达追加主题上限 16 树，忽略 '${_btok}'"; continue
        fi
        BRIDGE_EXTRA_TREES=$((BRIDGE_EXTRA_TREES + 1))
        case "${_bdir}" in
            both)
                BRIDGE_TOPICS_EXTRA="${BRIDGE_TOPICS_EXTRA}topic ${_bpat} out 1
topic ${_bpat} in 1
"
                BRIDGE_ACL_EXTRA="${BRIDGE_ACL_EXTRA}topic readwrite ${_bpat}
" ;;
            out)
                BRIDGE_TOPICS_EXTRA="${BRIDGE_TOPICS_EXTRA}topic ${_bpat} out 1
"
                BRIDGE_ACL_EXTRA="${BRIDGE_ACL_EXTRA}topic write ${_bpat}
" ;;
            in)
                BRIDGE_TOPICS_EXTRA="${BRIDGE_TOPICS_EXTRA}topic ${_bpat} in 1
"
                BRIDGE_ACL_EXTRA="${BRIDGE_ACL_EXTRA}topic read ${_bpat}
" ;;
        esac
    done
    if [ -n "${BRIDGE_TOPICS_EXTRA}" ]; then
        echo "[共存桥] 追加桥腿共 $(printf '%s' "${BRIDGE_TOPICS_EXTRA}" | grep -c '^topic ' || true) 条（coexist_bridge_topics，改配置后重启慧尖生效）"
    fi
fi
# END BRIDGE-TOPICS-SANITIZER

# ---------- 1b. 动态生成 ACL 文件 ----------
ACL_FILE="/etc/mosquitto/acl"
{
    cat <<EOF
# 动态生成 — 按用户隔离权限
#
# ${USERNAME}（LoRa 网关 / 调试客户端）：
#   仅网关协议主题 + 状态心跳，不能读写 homeassistant/#（防伪造 HA 发现）
user ${USERNAME}

# 慧尖网关协议主题
topic readwrite gateway/+
topic readwrite gateway/+/req
topic readwrite gateway/rpt_rsp

# 网关 birth/will 与 HA 状态心跳
topic readwrite homeassistant/status

# 健康检查主题
topic readwrite test/#

# \$SYS 主题（只读）
topic read \$SYS/#
EOF
    if [ "${HA_MQTT_USER_CREATED}" = "true" ]; then
        cat <<EOF

# ${HA_MQTT_USERNAME}（HA MQTT 集成）：白名单与共存桥主题腿逐条对齐
# （v1.6.24 安全评审定案：不用 readwrite # ——爆炸半径不超桥白名单；
# HA 消费面 = discovery + 桥入向 zigbee2mqtt/# + 慧尖网关协议）。
# 未来新增桥主题腿时必须同步扩本白名单（test_v1624 对齐钉桩）。
user ${HA_MQTT_USERNAME}

topic readwrite homeassistant/#
topic readwrite zigbee2mqtt/#
${BRIDGE_ACL_EXTRA}
# 慧尖网关协议主题
topic readwrite gateway/+
topic readwrite gateway/+/req
topic readwrite gateway/rpt_rsp

# \$SYS 主题（只读）
topic read \$SYS/#
EOF
    else
        cat <<EOF

# 回退：${HA_MQTT_USERNAME} 创建失败，${USERNAME} 保留 HA discovery 权限（旧行为）
topic readwrite homeassistant/#
EOF
    fi
    cat <<EOF

# ${Z2M_USERNAME}（zigbee2mqtt 直连慧尖内置 broker，v1.6.24）：
# z2m 自身主题 + HA discovery（z2m 要发布发现配置）——不含 gateway/#，
# 与桥白名单同边界。
user ${Z2M_USERNAME}
topic readwrite zigbee2mqtt/#
topic readwrite homeassistant/#
topic read \$SYS/#
EOF
} > "${ACL_FILE}"
chmod 600 "${ACL_FILE}"
chown mosquitto:mosquitto "${ACL_FILE}" 2>/dev/null || true
echo "[OK] ACL 文件已生成 (用户: ${USERNAME} + ${HA_MQTT_USERNAME})"

# ---------- 2. 创建持久化目录 ----------
mkdir -p /data/mosquitto
chmod 755 /data/mosquitto
chown mosquitto:mosquitto /data/mosquitto 2>/dev/null || true
echo "[OK] 持久化目录已创建"

# ---------- 3. 配置并启动 nginx（Ingress Web UI） ----------
echo "[Ingress] 配置 nginx Web UI..."
mkdir -p /run/nginx

# SUPERVISOR_TOKEN 由 shebang 的 with-contenv 注入环境（s6-overlay v3 的
# /run/s6/container_environment 是"每变量一文件"的目录，不存在可 source 的
# container-env 单文件——v1.6.4 删除该死回退分支，旧代码 source 恒不执行）。
HA_SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"

if [ -z "${HA_SUPERVISOR_TOKEN}" ]; then
    echo "[Ingress] 警告: SUPERVISOR_TOKEN 为空，HA API 代理将返回 401"
fi

if [ -n "${HA_SUPERVISOR_TOKEN}" ]; then
    echo "[Ingress] SUPERVISOR_TOKEN 已加载"
else
    echo "[Ingress] 错误: SUPERVISOR_TOKEN 仍为空，Web UI 状态检测将不可用"
fi

# HA Supervisor 地址：优先用主机名，兜底用固定 IP（host_network 下 DNS 可能不解析）
# v1.6.4 修复：旧判活命令 getent 在 alpine/musl 体系根本不存在（busybox 未编该
# applet、aports 无此包——netstat 事故同构），错误被吞导致恒走固定 IP 分支
# 且每次启动打印误导日志。改用 base 镜像 bind-tools 提供的 host 命令。
SUPERVISOR_HOST="supervisor"
if ! host supervisor >/dev/null 2>&1; then
    echo "[Ingress] supervisor 主机名无法解析，使用固定 IP 172.30.32.2"
    SUPERVISOR_HOST="172.30.32.2"
fi

# 审计 2026-09-30 F-1：https 反代必须校验上游证书。
# nginx 的 proxy_ssl_verify 默认 **off**：本文件与 ingress.conf 的
# /api/github/、/api/gitee/ 两条只开了 proxy_ssl_server_name（SNI）却没开校验，
# 于是同网段 MITM / DNS 劫持可以伪造一份 release JSON，Web UI 的"最新发布版"
# 徽章与更新说明（changelog 正文直显）被攻击者任意支配。两处 conf 对此无一句
# 理由，而纯 http 上游的 /api/ha/ 反倒显式写了 proxy_ssl_verify off（对 http
# 是无意义指令）——说明这个默认值从未进入考虑。
# CA 束由 Dockerfile 显式列装（v1.7.55 起 `apk add ca-certificates`），不再赌
# base 镜像"恰好有"——文件缺失时 nginx -t 是 [emerg] 级失败，整个 Web UI 起不来，
# 比徽章拿不到 release JSON 严重得多。这里的警告只作运行期兜底核对。
# 这里**只留警告、不改配置**：把校验写成 shell 变量会让"两拷贝机械对账"
# （tests/test_v1712_audit.py::TestIngressMechanicalDiff）失去可比的字面形态，
# 而那份漂移门禁正是 v1.7.12 为"三次漂移全靠人工"立的。真缺 CA 束时 nginx -t
# 会在下面的失败分支里把原因打出来，看门狗(:533) 也会持续报"10998 无监听"。
# verify_depth 2（nginx 默认 1）：gitee 实测链是 *.gitee.com → TrustAsia DV TLS
# RSA CA 2024 → TrustAsia TLS RSA Root CA（再由 Certum 根交叉签名，服务端不发），
# 中间 CA 就有两层，按默认 1 会判 depth 超限而 502。GitHub 真链只一层，取 2 是
# 对侧兼容，不是放宽到"任意深"。
if [ ! -f /etc/ssl/certs/ca-certificates.crt ]; then
    echo "[Ingress] 错误：未找到 /etc/ssl/certs/ca-certificates.crt，" \
         "github/gitee 反代的证书校验会让 nginx 起不来——镜像缺 ca-certificates 包" \
         "（Dockerfile 已显式列装，命中这条说明基础镜像换了）" >&2
fi

cat > /etc/nginx/http.d/ingress.conf <<NGINXEOF
server {
    listen 10998;

    # host_network 模式：允许 Supervisor/HA Core (172.30.32.x) 和本地回环。
    # TODO(设备上验证一次 ingress 实际源 IP 后可收紧为具体 IP；当前 supervisor
    # 的 hassio 网桥源地址随部署形态变化，贸然收窄会整站 403)
    allow 127.0.0.1;
    allow 172.30.32.0/24;
    deny all;

    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files \$uri \$uri/ /index.html;
        # v1.6.6：静态页必须 no-store——插件更新后 ingress 会话 token 路径
        # 不变，浏览器对无 Cache-Control 的 index.html 做启发式缓存，
        # 导致容器已是新版、页面却停留在旧版（1.6.5 更新后 Web 显示
        # 1.6.4 实锤）。UI 只有单个 html+几个 json，无缓存代价。
        add_header Cache-Control "no-store" always;
    }

    # 代理 HA Supervisor API — token 在此注入，前端无需携带
    # /api/ha/ → HA Core REST API（设备、服务、实体等）
    location /api/ha/ {
        proxy_pass http://${SUPERVISOR_HOST}/core/api/;
        proxy_set_header Authorization "Bearer ${HA_SUPERVISOR_TOKEN}";
        proxy_set_header Content-Type "application/json";
        proxy_set_header Accept "application/json";
        proxy_read_timeout 30s;
        proxy_connect_timeout 5s;
        proxy_ssl_verify off;
        # v1.6.9：补 no-store（此前唯一漏网：设备/实体/条目 JSON 无缓存头，
        # 会被浏览器启发式缓存 → UI 显示陈旧数据）
        add_header Cache-Control "no-store" always;
    }

    # v1.6.3：删除 /api/supervisor/ 死代理。Supervisor 安全设计禁止插件经 API
    # 自我更新（/addons/self/update 恒 403，2026-08-27 实测定案），Web UI
    # 自 v1.5.3 起零调用；留着只是把一个带完整 Supervisor token 的通道
    # 暴露给同网段其它加载项容器，纯攻击面。

    # MQTT Broker 状态检测 — 由后台探活循环写 status.json（约 5 秒粒度），
    # 真实反映 mosquitto 存活。v1.6.3 前此处是 nginx return 硬编码
    # "running"，broker 崩溃期间页面仍显示运行中，掩盖故障。
    # 文件不存在（循环未就绪/容器早期）返回 404，前端显示 "HTTP 404" 异常态
    location = /api/status {
        add_header Cache-Control "no-store" always;
        root /usr/share/nginx/html;
        try_files /status.json =404;
    }

    # v1.6.6：version.json / integration.json 是容器启动时生成的事实文件，
    # 同样补 no-store（此前无任何缓存头，会被启发式缓存）
    location /api/version {
        add_header Cache-Control "no-store" always;
        root /usr/share/nginx/html;
        try_files /version.json =404;
    }

    # 集成安装状态 — 插件本地事实（integration.json 由本脚本写入），不依赖 HA API
    location = /api/integration {
        add_header Cache-Control "no-store" always;
        root /usr/share/nginx/html;
        try_files /integration.json =404;
    }

    # Broker 客户端连接数 — broker_status.json 由后台循环每 5 秒刷新
    location = /api/broker {
        add_header Cache-Control "no-store" always;
        root /usr/share/nginx/html;
        try_files /broker_status.json =404;
    }

    # 代理 GitHub API（检查更新用），避免 Ingress iframe 中 CSP 拦截外部请求
    # v1.6.6：GitHub 上游自带 Cache-Control（public, max-age=60）会透传给
    # iframe 浏览器、拖慢「有可用升级」发现——hide 后统一改 no-store
    location /api/github/ {
        proxy_pass https://api.github.com/;
        proxy_set_header Host api.github.com;
        proxy_ssl_server_name on;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 2;
        proxy_read_timeout 15s;
        proxy_connect_timeout 10s;
        proxy_hide_header Cache-Control;
        add_header Cache-Control "no-store" always;
    }

    # 代理 Gitee API（默认更新源，无速率限制）
    location /api/gitee/ {
        proxy_pass https://gitee.com/api/v5/;
        proxy_set_header Host gitee.com;
        proxy_ssl_server_name on;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 2;
        proxy_read_timeout 15s;
        proxy_connect_timeout 10s;
        proxy_hide_header Cache-Control;
        add_header Cache-Control "no-store" always;
    }
}
NGINXEOF

# v1.6.26（第八轮审计 W-1）：本文件含 nginx 注入用的明文 SUPERVISOR_TOKEN
# ——与 passwd/acl 的 600/700 同口径收紧（同族"静默失防面"：passwd/acl 都
# 有 chmod，唯独此文件默认 umask 022 → 644，容器内任何低权进程可读 token
# 并经 /api/ha/ 代理调用 HA Core 全套 REST API）。nginx master 以 root 读
# 配置、worker 不读 conf，600 不影响运行。
chmod 600 /etc/nginx/http.d/ingress.conf 2>/dev/null || true

# v1.6.18 运行期兜底：alpine nginx 包自带 http.d/default.conf（listen 80
# default_server + listen [::]:80）。本加载项 host_network，该默认站会抢绑
# 宿主 80——宿主 80 空闲时是"插件白占 80"，被占时（NAS 常见：DSM 反代/其他
# 容器）bind 失败打死整个 nginx master，8099 侧边栏连坐全挂（2026-09-02 实锤）。
# Dockerfile 已 rm，这里再防基础镜像/apk 升级带回同名文件，顺带清掉其他
# 监听 80 的杂散 conf（本插件只应监听 10998）。
for f in /etc/nginx/http.d/*.conf; do
    [ -e "$f" ] || continue
    [ "$f" = "/etc/nginx/http.d/ingress.conf" ] && continue
    if grep -Eq '^[[:space:]]*listen[[:space:]]+(\[::\]:)?80([[:space:];]|$)' "$f"; then
        echo "[Ingress] 移除杂散默认站 conf: $f"
        rm -f "$f"
    fi
done

# v1.6.4：不再 2>/dev/null 吞启动错误——nginx 起不来最常见是 10998 被占/权限，
# 旧写法把真实报错丢了，只剩 nginx -t"配置语法正常"的假象，Web UI 静默瘫痪
# v1.6.18：bind 失败先试一次兜底重启（宿主 80 服务重启竞态窗口），仍失败则
# 打印占用诊断，不再只留 syntax ok 假象
# v1.7.33（全量审计）残留风险留痕：/api/ha/ 的来源白名单当前是整段
# 172.30.32.0/24（Supervisor/HA Core 所在网桥），而该网桥为**所有加载项
# 容器**共用——同网段其它加载项可借本加载器的 token 访问 HA Core 全量
# REST。收窄到具体 IP 需先在真机测出 ingress 实际源 IP（裸收紧会整站 403）。
# 取证命令（真机执行一次）：
#   docker exec addon_<slug> sh -c 'apk add --no-cache tcpdump >/dev/null 2>&1; #       tcpdump -ni any "tcp port 10998" -c 5'   # 观察 SYN 源地址
echo "[Ingress] 提示: /api/ha/ 白名单=172.30.32.0/24（含同网段其它加载项）——"     "收紧前需真机确认 ingress 源 IP，见 run.sh 注释与 CHANGELOG v1.7.33"
nginx || {
    echo "[Ingress] nginx 首启失败，5 秒后重试一次…"
    sleep 5
    nginx && echo "[Ingress] nginx 重试启动成功" || {
        echo "[Ingress] nginx 启动失败（错误见上），侧边栏可能不可用"
        # 追加配置语法测试辅助定位（语法问题与运行时问题分开看）
        nginx -t 2>&1 || true
        # 端口占用现场取证（v1.6.26 第八轮审计 E-4 根治）：旧版用 netstat，
        # 但 HA alpine base 镜像没有 netstat（本文件 §7 的 v1.6.4 注释早已
        # 实锤"apk 包仅 bash/bind-tools/…"且据此改用 /proc 方案——当时漏改
        # 了这处取证，宿主 80/10998 被占时恰在最需要时无输出）。复用
        # /proc/net/tcp{,6} 扫描：state 0A=LISTEN；80=0050、10998=2AF6
        awk -v p80=':0050$' -v p99=':2AF6$' \
            'FNR>1 && ($2 ~ p80 || $2 ~ p99) && $4=="0A" { print "  [取证] LISTEN " $2 }' \
            /proc/net/tcp /proc/net/tcp6 2>/dev/null
    }
}

# ---------- 3a. nginx 存活看门狗 ----------
# 审计 2026-09-30 F-2：上面那段只管**首启**失败重试一次，此后整个容器生命周期
# 再无一次探活；而 config.yaml 的 `watchdog: tcp://[HOST]:2022` 只探 broker ⇒
# nginx master 中途退出/OOM、或宿主 10998 事后被抢占时：Web UI 永久不可用、
# Supervisor 判健康不重启容器、§7b 巡检还在每 5s 写没人取得到的 status.json。
# 同文件对 mosquitto(:1071)、mDNS(:567)、发现代理(:1033) 各配了看门狗，唯独
# nginx 漏——:515 的"不 abort 整脚本"是刻意的，"终身不再管"没有任何认领。
# 判据 = "10998 有 LISTEN" **且** "本容器 nginx 进程在"（探活走 /proc，
#   与 §7b 同源；base 镜像没有 netstat/pgrep）：
#   宿主网络下 /proc/net/tcp 是宿主的表，只看端口会被"端口被其它进程占用"
#   骗成健康；只看进程又会漏掉"进程在但没监听"。两者都要（F-2 复核补口）。
# 探活拆成纯函数：行为钉"抽出真实现 + 喂假 /proc 真跑"（F-2 复核要求），
# 不在测试里重抄一份判据。路径可用环境变量替换——宿主端口表与 /proc 根
# 是测试杠杆（同 HUIJIAN_HUB_BASE 的黑洞杠杆先例），生产不设置即真路径。
nginx_probe_listen() {
    # 10998（十六进制 2AF6）LISTEN 计数。逐文件容错：/proc/net/tcp6 缺失
    # （内核未编 IPv6）只跳过该文件——两文件一起喂给 awk 时，缺失的那个是
    # 致命错误，会把另一个文件里已经数到的命中一起吞掉（判成 0 ⇒ 误拉起）。
    # 读取异常统一归一成 0 并**不**以非零退出：set -e 下 `X=$(awk …)` 的失败
    # 会原样杀掉整个看门狗子 shell（复核缺口 B：旧注释只防了比较、没防退出码）。
    _n=0
    for _f in "${NGINX_PROC_TCP:-/proc/net/tcp}" "${NGINX_PROC_TCP6:-/proc/net/tcp6}"; do
        [ -r "$_f" ] || continue
        _c=$(awk -v pat=':2AF6$' 'FNR>1 && $2 ~ pat && $4=="0A" {c++} END {print c+0}' "$_f" 2>/dev/null) || _c=""
        [ -n "$_c" ] && _n=$((_n + _c))
    done
    echo "${_n}"
}
nginx_probe_procs() {
    # 本容器内 nginx 进程数（/proc/PID/comm 逐行比对；base 镜像无 pgrep）。
    # host_network 下 /proc/net/tcp 是**宿主**的 socket 表：只看端口，会把
    # "端口被宿主其它进程占用"判成健康（审计 F-2 第二场景），必须配进程侧判据。
    local _root="${NGINX_PROC_ROOT:-/proc}" _n=0 _p
    for _p in $(ls "$_root" 2>/dev/null); do
        case "${_p}" in *[!0-9]*) continue ;; esac
        if [ "$(cat "${_root}/${_p}/comm" 2>/dev/null)" = "nginx" ]; then
            _n=$((_n + 1))
        fi
    done
    echo "${_n}"
}
(
    NGINX_RESTART_COUNT=0
    MAX_NGINX_RESTARTS=5
    while true; do
        sleep 20
        NGINX_LISTEN=$(nginx_probe_listen)
        NGINX_PROCS=$(nginx_probe_procs)
        # 判据必须是"数值比较成功且 > 0"：awk 输出异常时 [ "" -gt 0 ] 会报错，
        # 在 set -e 的后台子 shell 里把整个看门狗静默杀死（同文件 :946 的
        # "必须 if 形"是同一条教训）；探针函数已把读取失败归一成 0。
        if [ "${NGINX_LISTEN}" -gt 0 ] 2>/dev/null && [ "${NGINX_PROCS}" -gt 0 ] 2>/dev/null; then
            # 端口在 + 本容器 nginx 在 = 服务在；"连续"语义清零（与 mosquitto 看门狗同规）
            NGINX_RESTART_COUNT=0
            continue
        fi
        if [ "${NGINX_LISTEN}" -gt 0 ] 2>/dev/null; then
            echo "[Ingress] 10998 有监听但容器内无 nginx 进程：端口疑似被宿主其它进程占用（host_network 下 /proc/net/tcp 是宿主的表），仍尝试拉起 nginx"
        fi
        if [ "${NGINX_RESTART_COUNT}" -ge "${MAX_NGINX_RESTARTS}" ]; then
            echo "[Ingress] nginx 连续 ${MAX_NGINX_RESTARTS} 次拉起后仍无 10998 监听（或端口被他人占用），停止重启（Web UI 需人工介入）"
            exit 0
        fi
        NGINX_RESTART_COUNT=$((NGINX_RESTART_COUNT + 1))
        echo "[Ingress] 10998 无监听，nginx 疑似退出，第 ${NGINX_RESTART_COUNT}/${MAX_NGINX_RESTARTS} 次拉起"
        nginx || echo "[Ingress] nginx 拉起失败（见上方错误）"
    done
) &
echo "[Ingress] nginx 存活看门狗已启动（20s 节拍，连续 5 次无监听即放弃）"

# ---------- 3b. 配置并启动 mDNS 广播 ----------
echo "[mDNS] 配置 mDNS 广播..."

# mDNS 相关命令不能因 set -e 而终止整个脚本
set +e

# 清理函数：mosquitto 退出时同时清理 mDNS 后台进程
# v1.6.3：mDNS 现在是看门狗子 shell（MDNS_PID）+ 其 python 子进程，
# kill 子 shell 不会带走 python ⇒ 必须把 python 那一条也一起杀掉，否则残留进程
# 继续占用 5353 并广播一个已死的 broker。（v1.6.3 当时用 pkill 实现，本镜像里
# 并不存在该命令，2026-09-30 C-7 改为 /proc 扫描——目标不变，手段换成 base
# 镜像必有的东西，见 cleanup_mdns 内注释。）
MDNS_PID=""
MOSQUITTO_PID=""
cleanup_mdns() {
    if [ -n "${MDNS_PID}" ]; then
        kill "${MDNS_PID}" 2>/dev/null || true
    fi
    # 审计 2026-09-30 C-7：本行原本是 `pkill -f mdns_publisher.py`——**镜像里没有
    # pkill**（HA alpine base 的 apk 只有 bash/bind-tools/ca-certificates/curl/jq/
    # libstdc++/tzdata/xz，procps 不在其中；busybox 只给了 killall，没有 pkill。
    # 与本文件 :768-772 记实过的 netstat 是同族第三次事故），而
    # `2>/dev/null || true` 把 "command not found" 一起吃掉 ⇒ :520-521 立的目标
    # （"须一并 pkill，否则残留进程继续占用 5353"）从未达成：mosquitto 退出后
    # python 仍活着，huijian.local 继续广播一个已死/已降权的 broker，网关连上即被拒。
    # 改用 base 镜像必有的 /proc 扫描（与 §7b 判活同源）：按 cmdline 匹配，
    # 跳过自身 PID；找不到就什么也不做，但绝不再依赖不存在的命令。
    for _mdns_pid in $(ls /proc 2>/dev/null); do
        case "${_mdns_pid}" in
            ''|*[!0-9]*) continue ;;
        esac
        [ "${_mdns_pid}" = "$$" ] && continue
        if tr '\0' ' ' < "/proc/${_mdns_pid}/cmdline" 2>/dev/null \
           | grep -q 'mdns_publisher\.py'; then
            kill "${_mdns_pid}" 2>/dev/null || true
        fi
    done
}
# v1.6.4 停机路径根修：旧写法 trap cleanup_mdns EXIT INT TERM 的处理函数
# 只清理不退出——SIGTERM 到达后 bash 跑完 handler 从被中断的 wait 返回
# （rc=143），主循环把它当崩溃继续计数并重启 mosquitto，直到 docker 宽限期
# 结束 SIGKILL：broker 从未收到 TERM 优雅落盘（persistence 最坏丢 30 分钟
# retained 状态）、add-on 每次 stop/restart 都拖满超时。现 INT/TERM 显式
# 转发 TERM 给 broker、清理、exit 143。
shutdown_handler() {
    if [ -n "${MOSQUITTO_PID}" ]; then
        kill -TERM "${MOSQUITTO_PID}" 2>/dev/null || true
        # 给 broker 落盘 persistence（retained 消息）的窗口——脚本 exit 后
        # s6 即拆容器进程树，不留窗口 mosquitto 可能带着未保存状态被杀
        sleep 1 2>/dev/null || true
    fi
    cleanup_mdns
    exit 143
}
trap cleanup_mdns EXIT
trap shutdown_handler INT TERM

# mDNS 实现方案（v1.4.2+）：
#
# 之前使用 avahi-publish + D-Bus 连接宿主 avahi-daemon 的方案存在问题：
# 1. HAOS 容器内 D-Bus socket (/run/dbus/system_bus_socket) 不一定可用
# 2. 回退到容器内启动 avahi-daemon 会与宿主 avahi-daemon 端口冲突（5353）
# 3. BusyBox 的 grep 不支持 -P（Perl 正则），导致 IP 检测失败
#
# 新方案：使用 Python zeroconf 库，纯 Python 实现的 mDNS/DNS-SD 协议，
# 直接通过 UDP multicast 在 5353 端口广播，不需要 D-Bus，
# 不需要 avahi-daemon，不会和宿主 avahi-daemon 冲突
# （multicast 是共享的，多个进程可以同时响应 mDNS 查询）。

echo "[mDNS] 使用 Python zeroconf 广播 mDNS 服务..."

# 启动 mDNS 广播（v1.6.3 看门狗监督）：
# 旧实现单发启动 + 3 秒存活检查，进程因网络抖动/IP 变更/异常退出后
# 广播永久消失（LoRa 网关再也发现不了 huijian.local），且无人重启。
# 现由子 shell 循环监督：异常退出 10 秒后重启；正常退出（主动停止）则结束。
if [ -f /usr/bin/mdns_publisher.py ] && command -v python3 >/dev/null 2>&1; then
    (
        while true; do
            # v1.7.12（第 6 轮审计 F1，v1.6.3 C3 同族补漏）：子 shell 若继承
            # set -e，裸 `python3; RC=$?` 在进程非零退出的瞬间即被 errexit
            # 杀掉，RC=$? 与重启分支永不执行（看门狗形同虚设）。`|| RC=$?`
            # 让失败成为已处理条件，循环才真正活着。
            RC=0
            python3 /usr/bin/mdns_publisher.py "${MQTT_PORT}" || RC=$?
            if [ "${RC}" -eq 0 ]; then
                echo "[mDNS] 广播进程正常退出，不再重启"
                break
            fi
            echo "[mDNS] 广播进程异常退出 (code ${RC})，10 秒后重启..."
            sleep 10
        done
    ) &
    MDNS_PID=$!
    echo "[mDNS] mDNS 广播已启动 (看门狗 PID: ${MDNS_PID})"

    # 等待 mDNS 服务注册（zeroconf 需要几秒初始化）
    sleep 3

    # 检查看门狗是否存活（python 崩溃重启间隔内看门狗仍在循环）
    if ! kill -0 "${MDNS_PID}" 2>/dev/null; then
        echo "[mDNS] mDNS 看门狗已退出，检查 zeroconf 是否安装正确"
        MDNS_PID=""
    else
        echo "[mDNS] mDNS 广播中（异常时自动重启）"
        echo "[mDNS] LoRa 网关可通过 huijian.local:${MQTT_PORT} 连接"
    fi
else
    echo "[mDNS] mdns_publisher.py 或 python3 不可用"
    echo "[mDNS] mDNS 不可用，LoRa 网关需手动填写 HA IP 地址连接"
fi

# 恢复 set -e
set -e

# ---------- 4. 自动安装慧尖网关集成 ----------
if [ "${INSTALL_INTEGRATION}" = "true" ]; then
    echo ""
    echo "============================================"
    echo "  安装慧尖网关集成到 HA custom_components..."
    echo "============================================"

    HA_CONFIG_DIR="/homeassistant"
    if [ ! -d "${HA_CONFIG_DIR}" ]; then
        HA_CONFIG_DIR="/config"
    fi

    if [ -d "${HA_CONFIG_DIR}" ]; then
        INTEGRATION_SRC=""
        for src in /usr/share/custom_components/window_controller_gateway /data/custom_components/window_controller_gateway; do
            if [ -d "${src}" ]; then
                INTEGRATION_SRC="${src}"
                break
            fi
        done

        if [ -n "${INTEGRATION_SRC}" ]; then
            INTEGRATION_DST="${HA_CONFIG_DIR}/custom_components/window_controller_gateway"
            mkdir -p "${HA_CONFIG_DIR}/custom_components"

            NEED_UPDATE=false
            if [ -f "${INTEGRATION_DST}/manifest.json" ]; then
                # v1.6.4：jq 直读文件——旧 cat|jq 管道里 cat 失败被 2>/dev/null 吞、
                # jq 收空输入 rc=0 输出空串，|| echo "0" 兜底永不触发（管道状态掩蔽）
                EXISTING_VERSION=$(jq -r '.version // "0"' "${INTEGRATION_DST}/manifest.json" 2>/dev/null || echo "0")
                NEW_VERSION=$(jq -r '.version // "0"' "${INTEGRATION_SRC}/manifest.json" 2>/dev/null || echo "0")
                if [ "${NEW_VERSION}" != "${EXISTING_VERSION}" ]; then
                    NEED_UPDATE=true
                    echo "[集成] 版本变化: ${EXISTING_VERSION} → ${NEW_VERSION}，更新集成"
                else
                    echo "[集成] 版本相同 (${NEW_VERSION})，跳过更新"
                fi
            else
                NEED_UPDATE=true
                echo "[集成] 首次安装集成"
            fi

            if [ "${NEED_UPDATE}" = "true" ]; then
                PERSIST_FILE="${HA_CONFIG_DIR}/window_controller_gateway_data.json"
                BACKUP_PERSIST=false
                if [ -f "${PERSIST_FILE}" ]; then
                    cp "${PERSIST_FILE}" "/tmp/window_controller_gateway_data.json.bak"
                    BACKUP_PERSIST=true
                fi

                # v1.7.33（全量审计）：原子换防。旧序 rm -rf + cp -r 两步非原子，
                # cp 中途失败（盘满/只读/并发读）被 set -e 直接杀死 run.sh——
                # 结果比 640-644 有备份的 persist 文件更惨：集成目录半截且
                # broker（§8）尚未启动，整个加载项连 Mosquitto 一起起不来。
                # 改为"拷到同目录临时名 → mv 原子换名"，失败回滚并降级告警。
                _STAGE="${INTEGRATION_DST}.new.$$"
                if cp -r "${INTEGRATION_SRC}" "${_STAGE}"; then
                    rm -rf "${INTEGRATION_DST}"
                    if mv "${_STAGE}" "${INTEGRATION_DST}"; then
                        echo "[集成] 集成代码已安装到 ${INTEGRATION_DST}（原子换防）"
                    else
                        echo "[集成] 警告: 换名失败，集成目录可能缺失——本次不阻断启动"
                        rm -rf "${_STAGE}"
                    fi
                else
                    echo "[集成] 警告: 拷贝集成代码失败（磁盘/权限？），保留旧版本继续启动"
                    rm -rf "${_STAGE}"
                fi

                if [ "${BACKUP_PERSIST}" = "true" ]; then
                    cp "/tmp/window_controller_gateway_data.json.bak" "${PERSIST_FILE}"
                    echo "[集成] 已恢复持久化数据文件"
                fi
            fi

            echo "[集成] OK — 网关集成已就绪"
            echo "[集成] 重启 HA 后，设置 → 设备与服务 → 添加集成 → 搜索「慧尖」"
        else
            echo "[集成] 警告: 集成源码目录不存在"
        fi
    else
        echo "[集成] 警告: HA 配置目录未找到"
    fi
fi

# ---------- 4b. 写入集成安装状态（Web UI 本地读取，不依赖 HA Core API） ----------
# 背景：HA Core 会拒绝插件 SUPERVISOR_TOKEN 访问 Core REST API（401），
# 因此 Web UI 的状态检查全部改用插件本地生成的状态文件。
STATUS_CONFIG_DIR="/homeassistant"
if [ ! -d "${STATUS_CONFIG_DIR}" ]; then
    STATUS_CONFIG_DIR="/config"
fi
INTEGRATION_MANIFEST="${STATUS_CONFIG_DIR}/custom_components/window_controller_gateway/manifest.json"
if [ -f "${INTEGRATION_MANIFEST}" ]; then
    INTG_VER=$(jq -r '.version // "unknown"' "${INTEGRATION_MANIFEST}" 2>/dev/null || echo "unknown")
    if jq -n --arg v "${INTG_VER}" '{installed:true, version:$v}' > /usr/share/nginx/html/integration.json 2>/dev/null; then
        echo "[状态] 集成安装状态已写入 (v${INTG_VER})"
    fi
else
    echo '{"installed":false}' > /usr/share/nginx/html/integration.json 2>/dev/null || true
fi

# ---------- 5. 确保 mosquitto 用户权限 ----------
echo ""
echo "[启动] 正在配置 mosquitto broker..."
chown -R mosquitto:mosquitto /etc/mosquitto/ 2>/dev/null || true
chown -R mosquitto:mosquitto /data/mosquitto/ 2>/dev/null || true
chmod 700 /etc/mosquitto/passwd 2>/dev/null || true
chmod 700 /etc/mosquitto/acl 2>/dev/null || true

# ---------- 6. 启动信息 ----------
echo "[启动] 启动 mosquitto broker（前台模式）..."
echo "[启动] 监听: 0.0.0.0:${INTERNAL_PORT} (host_network 模式，直接暴露在主机)"

echo ""
echo "============================================"
echo "  慧尖 LoRa 网关一体化插件已就绪"
echo "============================================"
echo ""
echo "MQTT Broker: 0.0.0.0:${INTERNAL_PORT} (host_network)"
echo "Ingress Web UI: 10998 (侧边栏)"
echo "MQTT 用户名: ${USERNAME}"
echo ""

# ---------- 7. 自动写入 MQTT 引导标记（集成侧消费） ----------
# 旧方案（v1.0-v1.1.9）通过 REST API 自动创建 HA MQTT 配置条目，但 HA Core
# REST API 从未提供"创建配置条目"端点，导致所有版本的自动配置静默失败。
# 新方案（v1.2.0+）：将连接信息写入标记文件，集成侧在 async_setup_entry 时
# 读取并通过程序化 config flow 自动创建 MQTT 配置条目（详见 mqtt_bootstrap.py）。
HA_CONFIG_DIR="/homeassistant"
if [ ! -d "${HA_CONFIG_DIR}" ]; then
    HA_CONFIG_DIR="/config"
fi

if [ "${AUTO_SETUP}" = "true" ]; then
    if [ -d "${HA_CONFIG_DIR}" ]; then
        MARKER_PATH="${HA_CONFIG_DIR}/window_controller_gateway_mqtt_bootstrap.json"
        # 使用 jq 构造 JSON，正确转义密码中的特殊字符
        # Bug5 修复：写入 ha_mqtt 用户（ACL 全权限），HA 集成用其连接
        if jq -n \
            --arg broker "127.0.0.1" \
            --argjson port "${MQTT_PORT}" \
            --arg username "${BOOTSTRAP_USERNAME}" \
            --arg password "${PASSWORD}" \
            '{broker:$broker, port:$port, username:$username, password:$password}' \
            > "${MARKER_PATH}" 2>/dev/null; then
            chmod 600 "${MARKER_PATH}" 2>/dev/null || true
            echo "[自动配置] MQTT 引导标记已写入: ${MARKER_PATH}"
        else
            echo "[自动配置] 警告: 无法写入 MQTT 引导标记（jq 不可用或写入失败）"
        fi
    else
        echo "[自动配置] 警告: HA 配置目录未找到，跳过 MQTT 引导标记"
    fi
else
    # 自动配置已关闭：清理历史标记（含凭据），避免集成侧读到过期数据无限重试
    rm -f "${HA_CONFIG_DIR}/window_controller_gateway_mqtt_bootstrap.json" 2>/dev/null || true
fi

# ---------- 7b. Broker 连接数与存活状态后台采集（Web UI 本地读取，不依赖 HA API） ----------
# 每 5 秒：统计 :2022 上的 ESTABLISHED 连接数写 broker_status.json；
# 并写 status.json（nginx /api/status 的真实数据源，v1.6.3 取代硬编码 return 200）。
# v1.6.4 根修：HA alpine base 镜像【没有 netstat】（apk 包仅 bash/bind-tools/
# ca-certificates/curl/jq/libstdc++/tzdata/xz），旧 netstat 判活恒空输出——
# status.json 永远 stopped、clients 永远 0（双双假状态）。改用 base 必有的
# /proc/net/tcp{,6} + busybox awk：state 0A=LISTEN、01=ESTABLISHED，
# 端口比对用 printf '%04X' 动态十六进制（kernel 输出为大写，无硬编码）。
# host_network 下容器网络命名空间=主机，看到的即真实监听。
# 本循环是后台子 shell；v1.6.3 起主脚本无 exec 语义（wait 自愈循环常驻），
# 主脚本退出后由 init 接管继续运行。
# ---------- 7b. 第三方共存自动桥（v1.6.24） ----------
# 场景：用户后装官方「Mosquitto broker」加载项 + zigbee2mqtt（z2m 默认连
# core-mosquitto:1883；host_network 下即主机 127.0.0.1:1883）。HA MQTT 集成
# 全局唯一、已由慧尖指向内置 :2022——为让两个插件"零配置改动"共存，内置
# broker 自动向 1883 搭**方向分离桥**（仅 z2m 生态主题，见 _bridge_on 内
# 红线注释；全量 both 桥实测 retained 乒乓风暴已禁用，gateway/# 跨桥因
# 匿名注入攻击链实锤被安全评审摘除）：z2m 的发现/状态经 in 注入内置
# broker → HA 可见；HA 的 z2m 控制命令经 out 送达官方 broker。
# 状态机：每 30s 对账一次——1883 在且无桥→写 conf 并由主自愈循环重启
# mosquitto（计划内重启：RUN_SECS>=60 时崩溃计数器归零，不触发
# MAX_MOSQUITTO_RESTARTS 防护）；1883 消失且桥在→删段重启。冷却 120s 防抖。
BRIDGE_MARKER="AUTO-COEXIST-BRIDGE"
MOSQ_CONF="/etc/mosquitto/mosquitto.conf"
# v1.6.26（第八轮审计 D-3）：共存桥总开关（v1.7.13 起默认关，用户定案：
# 纯慧尖用户被匿名桥 30s 被拒噪音困扰，零配置共存语义在官方 7.x 强制认证
# 下名不副实；需要 z2m 共存时用户在配置页重开+填官方凭据）。判据只认本机
# :1883 LISTEN，宿主上任何第三方进程占该口都会被当官方 broker 搭桥（out
# 腿送 z2m 控制命令、in 腿注入 discovery——桥消息不受本地 ACL 约束），故
# 开闸须谨慎。BRIDGE_ENABLED=false 时不建新桥，已存在的桥由对账循环自动拆除。
BRIDGE_ENABLED=$(bashio::config 'coexist_bridge_enabled')
# 官方 broker 侧桥凭据（可选，见桥块注释）。净化防换行注入——加载项配置
# 值直通 heredoc 会允许在 mosquitto.conf 里伪造任意配置行
BRIDGE_PEER_USER=$(bashio::config 'coexist_official_user')
BRIDGE_PEER_PASSWORD=$(bashio::config 'coexist_official_password')
BRIDGE_PEER_USER=$(printf '%s' "${BRIDGE_PEER_USER}" | tr -cd 'A-Za-z0-9_-')
BRIDGE_PEER_PASSWORD=$(printf '%s' "${BRIDGE_PEER_PASSWORD}" | tr -d '\n\r')
# v1.6.26（第八轮审计 D-2）：凭据只在**双非空**时成对输出——旧实现 password
# 行按 USER 非空门控，"只填用户名"会展开 `password `（空值行），mosquitto
# 2.x conf__parse_string 对空值直接判 Error 拒载**整份 conf**（上游 v2.0.22
# conf.c 实锤）→ 内置 broker 拒启、加载项全挂。半填形态降级为匿名桥并大声
# 警告（对端 7.x 强制认证时桥不通，但慧尖自身服务无恙）。
BRIDGE_CREDS=""
if [ -n "${BRIDGE_PEER_USER}" ] && [ -n "${BRIDGE_PEER_PASSWORD}" ]; then
    BRIDGE_CREDS="username ${BRIDGE_PEER_USER}
password ${BRIDGE_PEER_PASSWORD}"
elif [ -n "${BRIDGE_PEER_USER}" ] || [ -n "${BRIDGE_PEER_PASSWORD}" ]; then
    echo "[共存] 警告: coexist_official_user/password 须成对填写（当前只填了一边）→ 桥按匿名方式连接"
fi
OFFICIAL_PORT_HEX=$(printf '%04X' 1883)
_bridge_peer_up() {
    # 复用本脚本的 /proc/net/tcp 扫描法（busybox 环境零进程开销）：
    # 本地任意地址 LISTEN(0A) 端口 :1883
    awk -v pat=":${OFFICIAL_PORT_HEX}$" 'FNR>1 && $2 ~ pat && $4=="0A" {found=1} END{exit !found}' \
        /proc/net/tcp /proc/net/tcp6 2>/dev/null
}
_bridge_present() { grep -q "BEGIN ${BRIDGE_MARKER}" "${MOSQ_CONF}" 2>/dev/null; }
_bridge_on() {
    _bridge_present && return 0
    cat >> "${MOSQ_CONF}" <<EOF

# BEGIN ${BRIDGE_MARKER}（自动管理——1883 官方 broker 出现/消失时对账同步）
connection core_mosquitto
address 127.0.0.1:1883
# 方向分离桥（真栈风暴实证禁 both：mosquitto 2.0.22 桥对 both 模式无
# origin 防环，retained 消息两 broker 间乒乓自激复制）——
# out：HA 侧经内置 broker 发的 z2m 控制命令送出去；
# in：z2m 状态 + MQTT discovery 配置注入内置 broker 供 HA 消费。
# homeassistant/# 只进不出（HA 不发 discovery 主题，且防慧尖网关侧
# 心跳主题回灌官方 broker 干扰其用户）。
notifications false
# 官方加载项 7.x 起 go-auth 强制认证（源码实锤 addons/mosquitto gtpl，
# allow_anonymous 时代终结）——匿名桥会被对端拒绝（实测：桥不通但慧尖
# 自身服务无恙）。在插件配置填官方 broker 有效凭据即带认证建桥（实测
# 端到端穿透）；留空=匿名，兼容老版官方/customize 关认证场景。
# v1.6.26（D-2）：改由 BRIDGE_CREDS 预构造（双非空才成对输出，杜绝
# "password" 空值行拒载整份 conf，见 §7b 头部注释）。
# ⚠ 本 heredoc 体内注释禁用反引号——未引号 heredoc 会把反引号对当作
# 命令替换执行（v1.7.9 现场实锤：旧注释里的反引号 password 打出
# "line 664: password: command not found"，无害但污染日志；v1.7.10
# 改普通引号并加回归钉桩 tests/test_runsh_heredoc_v1710.py）。
${BRIDGE_CREDS}
topic zigbee2mqtt/# out 1
topic zigbee2mqtt/# in 1
topic homeassistant/# in 1
# v1.7.19（方案一）：coexist_bridge_topics 追加树（1a+ 净化器渲染，逐条对齐
# ha_mqtt ACL；gateway/test/\$SYS/全匹配形态已在写入门上代码级拒绝）。
${BRIDGE_TOPICS_EXTRA}
# 禁 gateway/# 跨桥（v1.6.24 安全评审定案，实测取证）：in 腿等于把对端
# 信任域直连慧尖执行器——匿名@1883 publish gateway/{sn}/req 可穿桥达固件
# 实现未认证物理开窗。慧尖流量隔离在本 broker；若 HA 的 MQTT 条目被其他
# broker 抢走，慧尖需人工把条目改回 127.0.0.1:2022（README 已知边界）。
# END ${BRIDGE_MARKER}
EOF
    # v1.6.26（第八轮审计 D-5）：桥段刚写入即含对端凭据（若配置了），与
    # passwd/acl 同口径收紧 600——mosquitto 以 root 读 conf（进程内降权后
    # 不再回读），容器内 nginx worker/mosquitto 用户等低权进程不再可读。
    chmod 600 "${MOSQ_CONF}" 2>/dev/null || true
    echo "[共存] 检测到官方 Mosquitto(:1883) → 自动写入桥接（broker 重启/首次启动时加载生效）"
    kill -TERM "$(cat /run/mosquitto.pid 2>/dev/null)" 2>/dev/null || true
}
_bridge_off() {
    _bridge_present || return 0
    sed -i "/# BEGIN ${BRIDGE_MARKER}/,/# END ${BRIDGE_MARKER}/d" "${MOSQ_CONF}"
    echo "[共存] 官方 Mosquitto(:1883) 已消失 → 移除桥接，重启 broker 生效"
    kill -TERM "$(cat /run/mosquitto.pid 2>/dev/null)" 2>/dev/null || true
}

PORT_HEX=$(printf '%04X' "${MQTT_PORT}")
BRIDGE_TICK=0
(
    while true; do
        # v1.6.24 共存桥对账：每 30s（6×5s tick）一次；动作后冷却 120s，
        # 防官方 Mosquitto 安装中/重启期端口闪现闪无导致 broker 连环重启
        BRIDGE_TICK=$((BRIDGE_TICK + 1))
        if [ $((BRIDGE_TICK % 6)) -eq 0 ]; then
            LAST_TS=$(cat /run/bridge_last_ts 2>/dev/null || echo 0)
            # 净化（审计定案）：含非数字的垃圾会让 $((...)) 语法错误杀死
            # 整个巡检子 shell（v1.6.3 静默死同族故障）；空/非数字按 0 处理
            case "${LAST_TS}" in
                ''|*[!0-9]*) LAST_TS=0 ;;
            esac
            # v1.7.18（第 7 轮审计 BUG-20）：旧注释宣称"0 开头八进制坑一律
            # 归 0"但实现从未落地——"078"这类全数字值过 case 后，$(( )) 按
            # 八进制解析报 "value too great for base"，冷却判定失真且 set -e
            # 下有机会杀死本巡检+桥对账子 shell（status.json 冻结）。显式
            # 十进制解析：date +%s 永不出前导零，带前导零即异常值，按十进制
            # 取数值最稳（不会误重置冷却）。
            LAST_TS=$((10#$LAST_TS))
            NOW_TS=$(date +%s)
            if [ $((NOW_TS - LAST_TS)) -ge 120 ] 2>/dev/null; then
                # 仅"真实状态迁移"（peer 在而无桥 / 桥在而无 peer）才动作并
                # 记录冷却时间戳——noop tick 不续期，否则冷却永不过期、
                # 桥拆不掉（本行缺陷为自查实证发现，冷却语义=两次"重启动作"
                # 的最小间隔）
                # v1.6.26（D-3）：coexist_bridge_enabled=false 时短路 peer 判定
                # → 走 else 分支拆已有桥，且不再建新桥
                if [ "${BRIDGE_ENABLED}" = "true" ] && _bridge_peer_up; then
                    if ! _bridge_present; then
                        _bridge_on || true
                        echo "${NOW_TS}" > /run/bridge_last_ts
                    fi
                else
                    if _bridge_present; then
                        _bridge_off || true
                        echo "${NOW_TS}" > /run/bridge_last_ts
                    fi
                fi
            fi
        fi
        # 单次扫描同时得到 ESTABLISHED 连接数与 LISTEN 套接字数
        CONN_STATS=$(awk -v pat=":${PORT_HEX}$" 'FNR>1 && $2 ~ pat {
                            if ($4 == "01") c++; else if ($4 == "0A") l++
                        } END { print c+0, l+0 }' \
                        /proc/net/tcp /proc/net/tcp6 2>/dev/null || echo "0 0")
        CLIENTS=${CONN_STATS% *}
        LISTENERS=${CONN_STATS#* }
        # tmp+mv 原子替换：jq>file 先截断，nginx 高轮询下可能读到半截/空文件。
        # v1.6.4：写失败必须发声——否则 status.json 冻结在最后成功值，
        # broker 死后页面永远"运行中"（v1.6.2 硬编码 200 的隐蔽复刻）
        jq -n --argjson c "${CLIENTS:-0}" \
            '{clients:$c, connected:$c, updated:(now|todate)}' \
            > /usr/share/nginx/html/broker_status.json.tmp 2>/dev/null \
            && mv /usr/share/nginx/html/broker_status.json.tmp /usr/share/nginx/html/broker_status.json 2>/dev/null \
            || echo "[警告] broker_status.json 写入失败（检查 /usr/share/nginx/html 可写性/磁盘）"
        # 存活判据：MQTT 端口有 LISTEN 套接字（比 PID 检查更真实——僵而不死时端口已丢）
        if [ "${LISTENERS:-0}" -gt 0 ] 2>/dev/null; then
            RUNNING=true
        else
            RUNNING=false
        fi
        PID_NOW=$(cat /run/mosquitto.pid 2>/dev/null || echo 0)
        # v1.6.21: 默认密码提示位——与 config.yaml schema default 同串（交叉
        # 引用见 const.py DEFAULT_MQTT_PASSWORD 注释）；Web UI 概览据此提示改密
        DP_IS_DEFAULT=false
        if [ "${PASSWORD}" = "huijian2022" ]; then DP_IS_DEFAULT=true; fi
        # v1.6.24 共存桥状态位（诊断/支持用，无展示面——产品定案同凭据提示）
        # 必须 if 形：`x && y` 短路 false 的行尾返回值会触发本 subshell 继承
        # 的 set -e（v1.6.3 :343 起生效），巡检循环会被静默杀死（v1.6.4 的
        # || echo 防御与 v1.6.2 假状态是同族教训）
        BRIDGE_ON_F=false
        if _bridge_present; then BRIDGE_ON_F=true; fi
        PEER_UP_F=false
        if _bridge_peer_up; then PEER_UP_F=true; fi
        jq -n --argjson r "${RUNNING}" --argjson p "${PID_NOW:-0}" --argjson l "${LISTENERS:-0}" --argjson pt "${MQTT_PORT}" --argjson dp "${DP_IS_DEFAULT}" --argjson bc "${BRIDGE_ON_F}" --argjson pu "${PEER_UP_F}" \
            '{status:(if $r then "running" else "stopped" end), running:$r, broker:"mosquitto", port:$pt, pid:$p, listeners:$l, mqtt_password_is_default:$dp, coexist_bridge:$bc, official_peer_up:$pu, updated:(now|todate)}' \
            > /usr/share/nginx/html/status.json.tmp 2>/dev/null \
            && mv /usr/share/nginx/html/status.json.tmp /usr/share/nginx/html/status.json 2>/dev/null \
            || echo "[警告] status.json 写入失败——页面状态将冻结，重点排查"
        sleep 5
    done
) &

# ---------- 8. 后台启动 mosquitto，验证 MQTT 协议可用后再继续 ----------
echo "[运行] Broker 以前台模式运行..."

# v1.6.24：官方 Mosquitto 若已在运行，先写桥再接管启动（免一次计划内重启）
# v1.6.26（D-3）：同样受 coexist_bridge_enabled 总开关约束
if [ "${BRIDGE_ENABLED}" = "true" ] && _bridge_peer_up; then
    # || true：初启路径在 set -e 主 shell、mosquitto 首启之前——写失败
    # （盘满等）不能杀死整个 run.sh（比循环死重得多），留给 tick 对账重试
    _bridge_on || true
    date +%s > /run/bridge_last_ts 2>/dev/null || true
fi

# 先后台启动 mosquitto
mosquitto -c /etc/mosquitto/mosquitto.conf &
MOSQUITTO_PID=$!
echo "${MOSQUITTO_PID}" > /run/mosquitto.pid
echo "[运行] Mosquitto PID: ${MOSQUITTO_PID}"

# 等待 Mosquitto 进程启动（最多5秒）
MOSQUITTO_READY=false
for i in 1 2 3 4 5; do
    if ! kill -0 "${MOSQUITTO_PID}" 2>/dev/null; then
        echo "[错误] Mosquitto 进程已退出（PID=${MOSQUITTO_PID}）"
        echo "[诊断] 检查密码文件与 ACL 文件是否存在/非空（不打印内容，防哈希入日志）"
        [ -s /etc/mosquitto/passwd ] && echo "  passwd: 存在且非空" || echo "  passwd: 缺失或为空 ← 重点排查"
        [ -s /etc/mosquitto/acl ] && echo "  acl: 存在且非空" || echo "  acl: 缺失或为空 ← 重点排查"
        exit 1
    fi
    # 尝试 TCP 连接到 localhost:${MQTT_PORT}
    # v1.6.3：/dev/tcp 是 bashism，base 镜像的 sh（BusyBox ash）不支持，
    # 旧写法恒失败、就绪检查形同虚设——改用 bash -c
    # v1.6.9：端口改用 $MQTT_PORT（原硬编码 2022 与配置变量不一致）
    if timeout 1 bash -c "echo >/dev/tcp/127.0.0.1/${MQTT_PORT}" 2>/dev/null; then
        MOSQUITTO_READY=true
        break
    fi
    sleep 1
done

if [ "${MOSQUITTO_READY}" = "true" ]; then
    echo "[OK] Mosquitto TCP 端口 ${MQTT_PORT} 已就绪"
else
    echo "[警告] Mosquitto TCP 端口5秒内未就绪，继续启动..."
fi

# 等待 Mosquitto MQTT 协议响应（用 mosquitto_pub 测试）
if command -v mosquitto_pub >/dev/null 2>&1; then
    if mosquitto_pub -h 127.0.0.1 -p "${MQTT_PORT}" -u "${USERNAME}" -P "${PASSWORD}" -t "test/ping" -m "ok" 2>/dev/null; then
        echo "[OK] Mosquitto MQTT 协议验证通过（mosquitto_pub 成功）"
    else
        echo "[警告] mosquitto_pub 测试失败（broker 可能仍在初始化或密码配置有误）"
        # v1.6.3：只打印用户名，不 head 完整哈希行
        # v1.6.4：cut|tr||echo 的兜底是死代码（管道 rc 取 tr，恒 0）——改显式存在性判断
        if [ -f /etc/mosquitto/passwd ]; then
            echo "[诊断] /etc/mosquitto/passwd 用户: $(cut -d: -f1 /etc/mosquitto/passwd | tr '\n' ' ')"
        else
            echo "[诊断] /etc/mosquitto/passwd 文件不存在 ← 重点排查"
        fi
        echo "[诊断] mosquitto.conf 内容（凭据已脱敏，v1.6.26 第八轮审计 D-4——与 v1.6.3 'passwd 只 cut 用户名防哈希入日志'定案同口径；桥块含对端 username/password，此前全文入 Supervisor 日志）:"
        sed -E 's/^([[:space:]]*)(password|remote_password|username|remote_username)([[:space:]]+).*/\1\2 ***REDACTED***/' \
            /etc/mosquitto/mosquitto.conf 2>/dev/null | sed 's/^/  /'
    fi
fi

# =============================================================================
# v1.7.11 快速自动发现代理（fast_auto_discovery，默认开）
# =============================================================================
# 补零条目缺口：全新 HA（未添加过慧尖集成）没有任何 gateway/rpt_rsp 订阅者，
# 网关上报无人接收、发现卡片永不出现。本代理在容器内订阅上报并经 Supervisor
# core API 代发起 discovery flow（用户确认权仍在 HA 卡片上，代理不建条目）。
# 凭据：sub 用配置页账号（与上方 mosquitto_pub 自检同通道同 argv 威胁模型）；
# HA API 用 with-contenv 注入的 SUPERVISOR_TOKEN（环境变量传递，不进 argv）。
# 看门狗模式与 mdns_publisher 同构：异常退出 5 秒重启，正常退出结束。
if [ "${FAST_DISCOVERY}" = "true" ] && [ -f /usr/bin/gateway_discovery_proxy.py ]; then
    if command -v python3 >/dev/null 2>&1; then
        (
            while true; do
                # v1.7.12（审计 F1）：本 fork 在 set -e(:463) 恢复之后，子 shell
                # 带 errexit——必须 `|| RC=$?`。否则 broker 每次重启（共存桥对账
                # kill/崩溃自愈）导致代理非零退出时，看门狗随之猝死，
                # fast_auto_discovery 静默永久下线直到容器重启。
                RC=0
                python3 /usr/bin/gateway_discovery_proxy.py \
                    "${MQTT_PORT}" "${USERNAME}" "${PASSWORD}" || RC=$?
                if [ "${RC}" -eq 0 ]; then
                    echo "[发现代理] 正常退出，不再重启"
                    break
                fi
                echo "[发现代理] 异常退出 (code ${RC})，5 秒后重启..."
                sleep 5
            done
        ) &
        echo "[发现代理] 已启动 (PID: $!)——网关上报即弹 HA 发现卡片"
    else
        echo "[发现代理] python3 不可用，跳过（传统等待条目发现路径不受影响）"
    fi
else
    echo "[发现代理] fast_auto_discovery 关闭或脚本缺失——仅传统发现（需先有待配置条目）"
fi

# 等待 Mosquitto 退出并自愈：
# mosquitto 退出后 trap cleanup_mdns 自动清理 mDNS 后台进程相关资源。
# Bug2 修复：限制连续重启次数。mosquitto 因配置损坏（passwd/acl 格式错误等）
# 反复崩溃时，无限重启会刷屏且无助于恢复；连续 MAX_MOSQUITTO_RESTARTS 次
# 崩溃后退出并保留现场（日志/配置），便于排查。
# C3 修复（v1.6.3）：主循环区 set -e 是生效的（343 行恢复），而崩溃退出时
# `wait` 返回非零——旧写法 `wait ...; EXIT_CODE=$?` 的第二条语句永远执行不到，
# 脚本在到达重启逻辑前就被 set -e 杀死，"自愈"实为死代码。改为 `|| EXIT_CODE=$?`。
# 计数重置（v1.6.3）：旧实现计数只增不减，进程生命周期内累计 5 次崩溃
# （哪怕间隔数月）即永久放弃；现按"连续"语义——上次运行满 60 秒即视为
# 已恢复稳定，计数清零，符合 MAX_MOSQUITTO_RESTARTS 的设计初衷。
MAX_MOSQUITTO_RESTARTS=5
MOSQUITTO_RESTART_COUNT=0
while true; do
    WAIT_START=$(date +%s)
    EXIT_CODE=0
    wait "${MOSQUITTO_PID}" 2>/dev/null || EXIT_CODE=$?
    RUN_SECS=$(( $(date +%s) - WAIT_START ))
    if [ "${RUN_SECS}" -ge 60 ]; then
        MOSQUITTO_RESTART_COUNT=0
    fi
    MOSQUITTO_RESTART_COUNT=$((MOSQUITTO_RESTART_COUNT + 1))
    if [ "${MOSQUITTO_RESTART_COUNT}" -gt "${MAX_MOSQUITTO_RESTARTS}" ]; then
        echo "[错误] Mosquitto 连续退出 ${MAX_MOSQUITTO_RESTARTS} 次（最近 exit code: ${EXIT_CODE}），停止重启"
        echo "[诊断] 请检查 /etc/mosquitto/mosquitto.conf、passwd、acl 配置后手动重启插件"
        exit 1
    fi
    echo "[运行] Mosquitto 已退出 (exit code: ${EXIT_CODE}，本次运行 ${RUN_SECS}s)，5 秒后重启 (${MOSQUITTO_RESTART_COUNT}/${MAX_MOSQUITTO_RESTARTS})..."
    sleep 5
    mosquitto -c /etc/mosquitto/mosquitto.conf &
    MOSQUITTO_PID=$!
    echo "${MOSQUITTO_PID}" > /run/mosquitto.pid
    echo "[运行] Mosquitto 已重启 (PID: ${MOSQUITTO_PID})"
done
