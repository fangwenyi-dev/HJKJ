ARG BUILD_ARCH
# 基础镜像使用 HA 官方 base 镜像（不固定 Alpine 版本号，避免 tag 不存在导致安装失败）
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:latest

# 安装依赖包（v1.6.4 分层重构：此前 apk+pip 挤在同一个 RUN，任何一侧
# 小改动都令 pip 走网络的层全量重建——Supervisor 源码安装表现为
# "一直安装不成功"。拆层后各层独立命中缓存。
# 注意：RUN 续行体内不能插 # 注释行——sh 会把 `\`+注释行的线拼断，
# 后续包名/命令脱链成为独立命令造成构建期事故）
# - mosquitto/mosquitto-clients: MQTT broker + 客户端工具
# - curl/jq: HA API 调用
# - nginx: Ingress Web UI
# - ca-certificates: nginx proxy_ssl_trusted_certificate 指向的就是这份束（F-1）。
#   base 镜像"恰好有"不是保证：proxy_ssl_verify on + 缺失的 CA 文件会让
#   nginx -t 直接 [emerg]，整个 Web UI 起不来（run.sh 那段只警告不改配置的取舍
#   只有在这里成立才有意义）。显式列装比继承便宜。
# - python3/py3-pip: 运行 zeroconf mDNS 广播脚本
# - openssl: base 镜像只有 libssl 库没有 CLI，run.sh 密码哈希兜底需要
RUN apk add --no-cache \
    mosquitto \
    mosquitto-clients \
    curl \
    jq \
    nginx \
    ca-certificates \
    python3 \
    py3-pip \
    openssl

# zeroconf: 纯 Python mDNS 库，不依赖 D-Bus/avahi（HAOS 端口冲突定案）。
# pip 多级回退：pypi 直连 → 清华 → 阿里（--timeout 防挂死，国内链路慢时
# 快速失败立刻切镜像源）。首条不带 --break-system-packages 是 alpine
# EXTERNALLY-MANAGED 下秒败的兼容位，真实下载从第二条开始
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --timeout 30 --retries 2 -r /tmp/requirements.txt \
    || pip3 install --no-cache-dir --break-system-packages --timeout 30 --retries 2 -r /tmp/requirements.txt \
    || pip3 install --no-cache-dir --break-system-packages --timeout 60 -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/requirements.txt \
    || pip3 install --no-cache-dir --break-system-packages --timeout 60 -i https://mirrors.aliyun.com/pypi/simple -r /tmp/requirements.txt

# 复制 mDNS 广播脚本（纯 Python zeroconf，不依赖 avahi/D-Bus）
COPY mdns_publisher.py /usr/bin/mdns_publisher.py
RUN chmod +x /usr/bin/mdns_publisher.py

# v1.7.11 快速自动发现代理（run.sh 尾部看门狗拉起，补 HA 零条目时的发现缺口）
COPY gateway_discovery_proxy.py /usr/bin/gateway_discovery_proxy.py
RUN chmod +x /usr/bin/gateway_discovery_proxy.py

# 复制配置文件
COPY mosquitto.conf /etc/mosquitto/mosquitto.conf
COPY run.sh /run.sh

# 复制慧尖网关集成代码
# /usr/share：run.sh 集成安装/升级链的第一查找路径（生产实际使用）
# /data：运行时会被 HA 挂载的持久化卷覆盖，看似死层，但它是 run.sh
# INTEGRATION_SRC 回退链的第二路径（`for src in /usr/share/... /data/...`）——
# 不带卷挂载直接 docker run 调试镜像时 /usr/share 缺失则靠它兜底。
# v1.6.3 审查定论：保留勿删；构建体积已由 .dockerignore 排除 __pycache__/tests
COPY custom_components /usr/share/custom_components
COPY custom_components /data/custom_components

# 复制 Ingress Web UI
# ingress.conf 在 run.sh 启动时会动态重新生成（注入 SUPERVISOR_TOKEN）
COPY ingress.conf /etc/nginx/http.d/ingress.conf
COPY www/ /usr/share/nginx/html/

# v1.6.18 事故修复：删除 nginx 包自带默认站。alpine nginx 的
# /etc/nginx/http.d/default.conf 内含 `listen 80 default_server; listen [::]:80;`，
# 而下方重写的 nginx.conf 以 `include /etc/nginx/http.d/*.conf` 拉全目录——
# host_network 模式下该默认站会一直抢绑【宿主】80：宿主空闲时表现为插件悄悄
# 占用 80（副作用），宿主 80 被其他服务占用时 bind 失败直接打死整个 nginx
# master，连 8099 侧边栏一起瘫痪（2026-09-02 现场日志实锤）。旧注释"移除默认
# server 块"只删了 nginx.conf 内嵌默认块，从未删过 http.d 这个文件——半截工程。
# run.sh 另有运行期兜底扫描（防基础镜像/apk 升级带回同名文件）。
RUN rm -f /etc/nginx/http.d/default.conf

# 覆盖默认 nginx.conf（nginx.conf 本体默认 server 块亦随整体重写一并移除；
# 端口冲突的真正防线是上方 rm + run.sh 运行期扫描）
RUN echo 'user nginx;' > /etc/nginx/nginx.conf && \
    echo 'worker_processes auto;' >> /etc/nginx/nginx.conf && \
    echo 'error_log /dev/stderr warn;' >> /etc/nginx/nginx.conf && \
    echo 'pid /run/nginx.pid;' >> /etc/nginx/nginx.conf && \
    echo 'events { worker_connections 768; }' >> /etc/nginx/nginx.conf && \
    echo 'http {' >> /etc/nginx/nginx.conf && \
    echo '    include /etc/nginx/mime.types;' >> /etc/nginx/nginx.conf && \
    echo '    default_type application/octet-stream;' >> /etc/nginx/nginx.conf && \
    echo '    access_log /dev/stdout;' >> /etc/nginx/nginx.conf && \
    echo '    sendfile on;' >> /etc/nginx/nginx.conf && \
    echo '    keepalive_timeout 65;' >> /etc/nginx/nginx.conf && \
    echo '    include /etc/nginx/http.d/*.conf;' >> /etc/nginx/nginx.conf && \
    echo '}' >> /etc/nginx/nginx.conf

# 创建持久化目录，设置权限
RUN mkdir -p /data/mosquitto && \
    chmod 755 /data/mosquitto && \
    chmod +x /run.sh && \
    mkdir -p /run/nginx

CMD ["/run.sh"]
