#!/usr/bin/env python3
"""
慧尖 LoRa 网关 mDNS 广播服务

使用 zeroconf 库直接通过 UDP multicast 广播 mDNS 服务和主机名，
不依赖 D-Bus / avahi-daemon，避免与 HAOS 宿主 avahi-daemon 端口冲突。

注册内容：
  1. _mqtt._tcp 服务（LoRa 网关服务发现）
  2. huijian.local 主机名 A 记录（LoRa 网关 hostname 解析）
"""

import json
import os
import socket
import sys
import time

try:
    from zeroconf import ServiceInfo, Zeroconf
    # 尝试导入 IPVersion（不同版本 API 可能不同）
    try:
        from zeroconf import IPVersion
    except ImportError:
        IPVersion = None
    # v1.7.60：撞名专用异常（旧版 zeroconf 无此类型时降级为 None，
    # 由 describe_failure 的"异常无文本"兜底分支覆盖）
    try:
        from zeroconf import NonUniqueNameException
    except ImportError:
        NonUniqueNameException = None
except ImportError:
    print("[mDNS] zeroconf 库未安装，mDNS 不可用", file=sys.stderr)
    sys.exit(1)


#: 状态文件（无凭据，HA 配置目录＝容器可见）：给集成侧出"修复/提示卡"用。
STATUS_FILENAME = "window_controller_gateway_mdns_status.json"


def _status_path():
    for base in ("/homeassistant", "/config"):
        if os.path.isdir(base):
            return os.path.join(base, STATUS_FILENAME)
    return None


def write_status(state, local_ip, port, owner=""):
    """写 mDNS 状态供 HA 侧可见（失败只降级，绝不反噬广播主流程）。"""
    path = _status_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": state, "local_ip": local_ip, "port": port,
                       "owner": owner, "ts": int(time.time())}, f,
                      ensure_ascii=False)
    except OSError as e:
        print(f"[mDNS] 状态文件写入失败（不影响广播）: {e}", file=sys.stderr)


def describe_failure(exc, local_ip, port, owner=""):
    """注册失败 → (状态码, 人话)。

    v1.7.60 根因修复：**撞名（NonUniqueNameException）是无文本异常**——旧实现
    f"服务注册失败: {e}" 打出空白行，现场只见看门狗每 10 秒刷屏、零归因。
    且撞名是**永久失败**（名字被另一台占着，重试永远不会成功），必须响亮点名
    占位者与二选一的处置。非撞名的空文本异常也带上类型名，杜绝空白错误。
    """
    if NonUniqueNameException is not None and isinstance(exc, NonUniqueNameException):
        return ("name_conflict",
                f"NAMECONFLICT mDNS 名字冲突：huijian.local / "
                f"huijian-mqtt._mqtt._tcp.local. 已被局域网内另一台设备占用"
                f"（占位者：{owner or '未解析到'}）——本机 {local_ip} 的 mDNS 自动"
                f"发现不会生效，走 mDNS 的 LoRa 网关只会连上占位者那台 HA。"
                f"二选一：关掉其中一台的慧尖加载项，或把网关的 MQTT 服务器地址"
                f"改成本机 IP {local_ip}:{port}（本机 broker 照常可用）")
    msg = str(exc).strip()
    if not msg:
        msg = (f"{type(exc).__name__}（异常无文本；若局域网内还有第二台慧尖"
               f"加载项，优先怀疑名字冲突）")
    return ("error", f"服务注册失败: {msg}")


def _query_owner(zc, timeout_ms=2000):
    """解析当前占着 huijian-mqtt 名字的对端（拿来点名"被谁占用"）。"""
    try:
        info = zc.get_service_info("_mqtt._tcp.local.",
                                   "huijian-mqtt._mqtt._tcp.local.",
                                   timeout=timeout_ms)
        if info and info.addresses:
            return f"{socket.inet_ntoa(info.addresses[0])}:{info.port}"
    except Exception:  # noqa: BLE001 — 诊断面失败不影响主流程
        pass
    return ""


def get_local_ip():
    """获取本机 IP（host_network 模式下就是 HA 主机 IP）"""
    # 方式 1: 连接外部地址获取本机出口 IP（最可靠）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # 方式 2: hostname -I 等效方式
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # 方式 3: 遍历所有网络接口
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            ip = item[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("::"):
                return ip
    except Exception:
        pass

    return None


def reregister_service(zeroconf, old_info, new_ip, mqtt_port):
    """IP 变化：注销旧广播 → 按新地址重注册 → **回写状态文件**。

    回写是 2026-10-08 审计 B-6 补的：旧实现在此分支只 print，状态文件永远停在
    首次注册的 IP 上，而集成侧提示卡读的正是文件里的 local_ip ⇒ DHCP 续租/换网
    后卡片显示的是失联前的旧地址。注册失败照原样向上抛（由 main 归因撞名/退出码），
    所以新 IP 只在注册成功后才落地。
    """
    try:
        zeroconf.unregister_service(old_info)
    except Exception:  # noqa: BLE001 — 注销失败不能挡住按新地址重注册
        pass
    info = ServiceInfo(
        type_="_mqtt._tcp.local.",
        name="huijian-mqtt._mqtt._tcp.local.",
        addresses=[socket.inet_aton(new_ip)],
        port=mqtt_port,
        properties={},
        server="huijian.local.",
    )
    zeroconf.register_service(info)
    write_status("ok", new_ip, mqtt_port)
    return info


def main():
    mqtt_port = int(sys.argv[1]) if len(sys.argv) > 1 else 2022

    local_ip = get_local_ip()
    if not local_ip:
        # v1.6.3：绝不广播 127.0.0.1——旧实现在 IP 探测失败时把回环地址
        # 宣告为 huijian.local，LoRa 网关解析后对着自己发连接，永远失败
        # 且毫无排障线索。退出非零，交由 run.sh 看门狗 10 秒后重试。
        print("[mDNS] 无法确定本机 IP（网络未就绪？），退出等待看门狗重试", file=sys.stderr)
        sys.exit(1)

    print(f"[mDNS] 本机 IP: {local_ip}")

    # 创建 Zeroconf 实例
    # 不显式指定 ip_version，使用默认值（自动选择），避免不同版本 API 差异
    try:
        zeroconf = Zeroconf()
    except Exception as e:
        print(f"[mDNS] Zeroconf 初始化失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 注册 _mqtt._tcp 服务
    # zeroconf 0.132.0 要求 type_ 和 name 都以 .local. 结尾
    service_info = ServiceInfo(
        type_="_mqtt._tcp.local.",
        name="huijian-mqtt._mqtt._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=mqtt_port,
        properties={},
        server="huijian.local.",
    )

    # 注册 huijian.local 主机名
    # zeroconf 通过 ServiceInfo 的 server 字段自动广播 A 记录
    # 同时也注册一个主机名服务

    try:
        zeroconf.register_service(service_info)
        print(f"[mDNS] _mqtt._tcp 服务已注册: huijian-mqtt._mqtt._tcp.local. @ {local_ip}:{mqtt_port}")
        print(f"[mDNS] huijian.local → {local_ip}")
        print(f"[mDNS] mDNS 广播中，LoRa 网关可通过 huijian.local:{mqtt_port} 连接")
        write_status("ok", local_ip, mqtt_port)

        # 保持运行，mDNS 广播持续在线。
        # v1.6.3：每 30 秒复查本机 IP——DHCP 续租/换网后旧地址仍被广播，
        # 网关会连到失效 IP；IP 变化时注销并按新地址重注册。
        while True:
            time.sleep(30)
            current = get_local_ip()
            if current and current != local_ip:
                print(f"[mDNS] 本机 IP 变化: {local_ip} → {current}，重新注册")
                service_info = reregister_service(zeroconf, service_info,
                                                  current, mqtt_port)
                local_ip = current
                print(f"[mDNS] huijian.local → {local_ip}（已更新）")

    except KeyboardInterrupt:
        pass
    except Exception as e:
        # v1.7.6x：注册/重注册失败退出非零，让看门狗接管重试（旧实现吞异常后
        # 正常退出 0，广播静默消失且无人重启）。
        # v1.7.60：撞名单独识别——它是**永久失败**，重试不可能成功，必须响亮
        # 点名占位者（旧日志 "服务注册失败: " 后空白，见 describe_failure）。
        state, reason = describe_failure(e, local_ip, mqtt_port,
                                         owner=_query_owner(zeroconf))
        print(f"[mDNS] {reason}", file=sys.stderr)
        if state == "name_conflict":
            write_status(state, local_ip, mqtt_port,
                         owner=_query_owner(zeroconf))
        try:
            zeroconf.unregister_service(service_info)
            zeroconf.close()
        except Exception:
            pass
        sys.exit(3 if state == "name_conflict" else 2)
    finally:
        if sys.exc_info()[0] is None or isinstance(sys.exc_info()[1], KeyboardInterrupt):
            try:
                zeroconf.unregister_service(service_info)
                zeroconf.close()
            except Exception:
                pass
        print("[mDNS] mDNS 服务已注销")


if __name__ == "__main__":
    main()
