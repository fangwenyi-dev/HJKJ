"""mDNS 服务广播：_huijian-voice._tcp.local（v4 §3：配网发现入口）。

固件不广播 mDNS（源码实证），本服务广播加载项自身；小程序/配网向导据此发现网关
地址与三通道 URL。零conf 发布失败不致命（静态 IP 直连仍可用），只告警。
"""
from __future__ import annotations

import logging
import socket

from . import const

logger = logging.getLogger("huijian.mdns")


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))     # 无发包，仅取路由出口 IP
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def lan_ip() -> str:
    """可路由的局域网地址；拿不到就回 ""（调用方自己决定回落顺序）。

    v1.7.5：`local_ip()` 的 127.0.0.1 兜底对"开机 banner 打个地址"是无害的，对
    "告诉设备/小程序来连我"是有害的——发出去一个回环地址＝对方必然连不上，而且
    从对方视角看不出为什么。端点三出口（/discover、/api/endpoints、endpoints.json）
    与 mDNS 重广播统一走本函数，开机 banner 保留 local_ip()（它要的就是"我看到了什么"）。
    """
    ip = local_ip()
    return "" if not ip or ip.startswith("127.") else ip


class Publisher:
    def __init__(self, port: int = const.WS_PORT, props: dict | None = None, version: str = "1.0.0"):
        self.port = port
        self.props = props or {}
        self.version = version
        self._zco = None
        self._info = None
        self._ip = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
            ip = local_ip()
            self._ip = ip
            self._info = ServiceInfo(
                "_huijian-voice._tcp.local.",
                f"huijian-voice._huijian-voice._tcp.local.",
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties={k: str(v) for k, v in self.props.items()},
                server="huijian-voice.local.",
            )
            self._zco = Zeroconf()
            self._zco.register_service(self._info)
            logger.warning("[mDNS] 广播 %s:%d @ %s", "_huijian-voice._tcp.local.", self.port, ip)
        except Exception as e:
            logger.warning("[mDNS] 广播失败（不影响静态接入）: %r", e)  # %r：v1.0.0 实机异常 str 为空，必须带类型显形
            if self._zco is not None:
                try:   # register 失败也必须拆 Zeroconf（UDP socket/引擎线程），否则泄漏
                    self._zco.close()
                except Exception:
                    pass
            self._zco = None

    # v1.1.27 项7：删除 update_props()。全仓零调用（grep 仅此定义处），且实现
    # 必失效——zeroconf 的 ServiceInfo.properties 是只读 property（赋值不会改写
    # 已注册记录），异常又被裸 except 吞掉，"更新广播属性"从来没生效过。留在
    # 这里只会让下一次改动的人以为有热更新通道（要加真实现须走
    # update_service + 可写属性，并带实测钉）。
    def republish_if_changed(self) -> bool:
        """v1.7.5：出口地址变了就把广播拉回真相（返回 True＝真重广播过）。

        `start()` 在注册那一刻读一次地址，之后 A 记录冻在 ServiceInfo 里，zeroconf
        不会自己改 ⇒ 路由器重启换了 DHCP lease、或加载项比网卡先起（`local_ip()` 当时
        回落 127.0.0.1）之后，mDNS 会一直播一个连不上的地址，而设备侧正靠它找服务器。
        OTA 那条链 v1.0.65 已改成"每次签发实时取 IP"，广播这一路是剩下的半截
        （10-24 审出 P2：`ctx.host` 一次性采样）。
        刻意走"拆掉再注册"而不是 `update_service`：属性只读的坑（见上方 v1.1.27 注）
        说明这个封装里"看起来能热更"的路都不生效，重注册是唯一被实测能走通的一条。
        失败只留痕——广播坏了不该把 HTTP/WS 本身带下水。
        """
        if self._zco is None or self._info is None:
            return False
        now = lan_ip()
        if not now or now == self._ip:
            return False
        old = self._ip
        try:
            self.close()
        except Exception:
            logger.warning("[mDNS] 重广播前拆旧实例失败（继续重建）", exc_info=True)
        self._ip = None
        self.start()
        logger.warning("[mDNS] 出口地址 %s → %s，已重广播", old, now)
        return True

    def close(self) -> None:
        # v1.1.39（审查 §3[P3]）：`unregister_service` 与 `close()` 原来写在**同一个
        # try** 里，前者抛错就被 `except: pass` 连坐吞掉 ⇒ Zeroconf 不拆，
        # UDP socket 与引擎线程泄漏。与本文件 `start()` 的既有纪律对齐
        # （"register 失败也必须拆 Zeroconf"）：两步各自兜住，拆池无条件执行。
        try:
            if self._zco and self._info:
                self._zco.unregister_service(self._info)
        except Exception:
            pass
        finally:
            try:
                if self._zco:
                    self._zco.close()
            except Exception:
                pass
