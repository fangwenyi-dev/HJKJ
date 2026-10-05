#!/usr/bin/env python3
"""v1.7.11 快速自动发现代理 —— 跑在加载项容器内（broker 同侧）。

背景（代码读出的结构性缺口）：HA 侧既有自动发现触发器全部挂在集成条目上——
心跳监听器挂「无 SN 等待条目」，_protocol 的其它网关发现挂「已配置条目」。
全新 HA（零条目）没有任何人订阅 gateway/rpt_rsp，网关主动上报落空，
发现卡片永远不出现（用户只能手动填 SN）。

方案（真栈实锤后定案，绕开两条死路）：
  死路 A：REST POST /config/config_entries/flow 发起 discovery——HA 的
    ConfigManagerFlowIndexView.get_context() 无条件把 source 改写为 user
    （components/config/config_entries.py 源码实锤），discovery 分支进不去；
    且 REST 无主 flow 在请求结束即被回收（实测 1 秒蒸发）。
  死路 B：WebSocket 建流——本地 HA 2026.1.3 全量注册命令里根本没有
    "config_entries/flow" 创建命令（只有 progress/subscribe/ignore 等）。
  正解：代理只做「给 HA 装耳朵」这一最小动作——捕获网关上报（001/002/005）
  后，若集成尚无任何 config entry，经 REST 创建一个 gateway_sn 留空的
  「等待模式」条目（ha_e2e_driver.py 同款 POST+step 提交，持久可靠）；该条目
  setup 时挂载既有心跳监听器（v1.6.26 A-2 已解决 MQTT 晚就绪竞态），代理随即
  把捕获的原报文经 mosquitto_pub 重放一次（仅一次/SN），新耳朵立刻听到 →
  走集成内部 async_discover_gateway → 弹标准"慧尖网关"发现卡片。此后其它
  网关/后续上报全由集成既有链路承接。

边界与语义：
- **网关卡片仍须用户点击确认**——代理创建的是零功能「等待条目」（无 SN 不
  forward 实体、不 ack、只订阅一个 topic 当耳朵），不替用户配对任何网关。
- 重放防回环：每 SN 一生至多重放一次；重放消息若被其它已配置条目处理会产生
  重复 ack，但网关侧对重复 ack 幂等（5s 去重窗），风险可忽略。
- HA 重启窗口（连不上 REST）：不记状态，下一条上报自然重试（上报节奏 ≤10s）。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

SN_RE = re.compile(r"^[a-zA-Z0-9]{10,}$")
TRIGGER_CTYPES = {"001", "002", "005"}
#: 网关主动发起、HA **必 ack** 的三类（方向契约）：兜底应答的适用面。
#: 003/004/006/007 是 HA 主动命令的回复——绝不对答（防回环）。
ACK_CTYPES = frozenset(TRIGGER_CTYPES)
DOMAIN = "window_controller_gateway"

# 默认走 Supervisor core API（加载项容器内 with-contenv 提供 supervisor 主机名
# 与 SUPERVISOR_TOKEN，与 nginx /api/ha/ 同源）；E2E/调试用环境变量覆盖直连。
DEFAULT_API = "http://supervisor/core/api"
RETRY_HTTP_FAIL = 30.0
# v1.7.18（第 7 轮审计 BUG-4）：建耳成功/判定已存在后的再检查冷却——
# 替代旧"_ears_confirmed 永久缓存"（见 has_entries），既防每条上报都打
# create 流程，又保证用户删除/禁用条目后 ≤30s 自动补种。
SEED_RETRY_COOLDOWN = 30.0


def parse_report(raw: str):
    """解析一行 rpt_rsp；返回 (sn, ctype)，非触发报文返回 None。

    与集成内 handle_gateway_response / _heartbeat_listener 同防御口径：
    head 校验、ctype 白名单、SN 类型守卫（int/float 转 str，bool/dict 丢弃）、
    ≥10 位字母数字格式校验。
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("head") != "$SH":
        return None
    ctype = payload.get("ctype")
    if ctype not in TRIGGER_CTYPES:
        return None
    sn = payload.get("sn")
    if isinstance(sn, bool) or not isinstance(sn, (str, int, float)):
        return None
    sn = str(sn) if not isinstance(sn, str) else sn
    if not SN_RE.match(sn):
        return None
    return sn, ctype


def ha_api(path: str, method: str = "GET", body=None):
    """HA Core REST（Supervisor 通道）。返回解析后的 JSON；失败抛异常。"""
    api = os.environ.get("HUIJIAN_HA_API", DEFAULT_API)
    token = os.environ.get("HUIJIAN_HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN") or ""
    if not token:
        raise RuntimeError("缺 HA/SUPERVISOR token")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        api + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


class DiscoveryProxy:
    """耳朵引导 + 重放 + 必 ack 上报（001/002/005）的兜底应答。http/pub 注入，单测无网络。"""

    def __init__(self, list_entries, create_ears, republish,
                 now=time.monotonic, log=print, sleep=time.sleep,
                 publish_ack=None, read_uuid=None):
        self._list = list_entries
        self._create = create_ears
        self._pub = republish
        self._now = now
        self._log = log
        self._sleep = sleep
        self._replayed = set()   # 每 SN 至多重放一次
        self._next_try = 0.0     # 全局退避（HA 重启窗口/建耳冷却）
        # ---- v1.7.60 001 兜底应答（HA MQTT 失聪时的最后防线）----
        # publish_ack(sn, payload_dict) -> bool；read_uuid() -> str|None。
        # 两者缺省不启用（旧测试/旧注入形态零破坏）。
        self._pub_ack = publish_ack
        self._read_uuid = read_uuid
        self._uuid = None
        self._uuid_warned = False
        self._ack_seen = {}      # sn -> {id: 最近一次下行应答的时间}
        self._req_ids = {}       # sn -> [(请求 id, 时间)]（有界 + TTL）
        self._self_acked = {}    # (sn, id) -> 本代理代答时间（TTL 内防重发）
        self._ha_deaf = {}       # sn -> True：连续两轮零应答后判"HA 失聪"
        self._ack_warn_at = {}   # sn -> 上次"HA 零应答"留痕时间

    # ---------- v1.7.60：必 ack 上报的兜底应答 ----------
    #: 应答证据/请求记录的保鲜期（秒）：网关重启后 id 会从 100 重来，旧证据
    #: 不得把新请求误判成"HA 已答"；同时给各表做容量兜底（防无界增长）。
    ACK_EVIDENCE_TTL = 90.0

    def _instance_uuid(self):
        if self._uuid is None and self._read_uuid is not None:
            try:
                self._uuid = self._read_uuid() or ""
            except Exception:  # noqa: BLE001
                self._uuid = ""
        return self._uuid or None

    def _note_downlink(self, topic: str, raw_payload: str) -> None:
        """gateway/<sn>/req 上的任何一帧 = HA（或本代理自己）的应答证据。"""
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "gateway" or parts[2] != "req":
            return
        try:
            payload = json.loads(raw_payload)
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        msg_id = payload.get("id")
        if msg_id is None:
            return
        now = self._now()
        seen = self._ack_seen.setdefault(parts[1], {})
        seen[str(msg_id)] = now
        # HA 有新鲜应答 = 通道活着 → 解除该 SN 的"失聪"态并清空请求积压
        #（代理立即停手；若 HA 再度静默，需重新积累两轮未答才接管）
        self._ha_deaf.pop(parts[1], None)
        self._req_ids.pop(parts[1], None)
        for stale in [k for k, ts in seen.items() if now - ts > self.ACK_EVIDENCE_TTL]:
            seen.pop(stale, None)

    def _maybe_ack_gateway_initiated(self, sn: str, ctype: str, payload: dict) -> None:
        """HA 连续两轮不答同一网关的「必 ack」上报 → 本代理按同形代答。

        契约（CLAUDE.md 方向契约 / mosquitto.conf 主题表）：**001/002/005 是
        网关主动发起，HA 必须回 errcode:0**，否则固件按未确认持续重发。现场
        实锤（2026-10-05，192.168.1.91 只读抓包）：已配置网关 10012250123f 的
        005/002 条条都被 HA 应答；而**未配置**网关 1001215011a3 的 002 零应答
        ——集成两处耳朵只代答 001，未配置网关的 002/005 落进无人应答的洞，
        固件重发不止、发现链也静默。

        判据是**broker 侧真值**（代理直连订阅，不依赖 HA）：同一 SN 的请求 id
        连续出现 ≥2 个而在下行 req 主题上零个对应应答 id ⇒ HA 侧没有订阅者/
        连接不在本 broker（条目被抢走、被禁用、未配置、客户端未连）。HA 恢复
        后其应答会出现在 req 主题 ⇒ 本代理自动停手（防双答）。
        """
        if self._pub_ack is None:
            return
        data = payload.get("data")
        if not isinstance(data, dict) or "errcode" in data:
            return  # 带 errcode = 网关对我方报文的回复，绝不对答（防回环）
        msg_id = payload.get("id")
        if msg_id is None:
            return
        now = self._now()
        sid = str(msg_id)
        ids = self._req_ids.setdefault(sn, [])
        if not ids or ids[-1][0] != sid:   # 同 id 重发只记一次（防单帧凑够两轮）
            ids.append((sid, now))
        del ids[:-4]  # 有界：只看最近 4 个请求 id
        ids[:] = [(i, ts) for i, ts in ids if now - ts <= self.ACK_EVIDENCE_TTL]
        key = (sn, sid)
        if key in self._self_acked:
            return
        seen = self._ack_seen.get(sn) or {}
        unseen = {i: ts for i, ts in ids
                  if not (i in seen and seen[i] >= ts)}  # 应答必须晚于该请求
        if len(unseen) >= 2:
            # 连续两轮无人应答 ⇒ 该网关判"HA 失聪"，此后每帧必 ack 的都当场代答
            #（否则每两帧才答一次，固件仍要等一个完整重发周期）。
            self._ha_deaf[sn] = True
        if not self._ha_deaf.get(sn):
            return  # HA 还有机会应答（5s 窗口内只见一帧）
        uuid = self._instance_uuid()
        if ctype == "001" and not uuid:
            if not self._uuid_warned:
                self._uuid_warned = True
                self._log("[发现代理] 缺实例指纹文件，无法代答 001（等集成 setup "
                          "落盘 window_controller_gateway_instance.json）")
            return
        body = {"errcode": 0}
        if ctype == "001":
            body["uuid"] = uuid  # 001 应答必带指纹（与 _handle_ctype_001 同形）
        ack = {"head": "$SH", "ctype": ctype, "id": msg_id, "sn": sn, "data": body}
        try:
            ok = self._pub_ack(sn, ack) is not False
        except Exception as e:  # noqa: BLE001 — 代答失败绝不反噬主循环
            self._log(f"[发现代理] {ctype} 代答发布异常（忽略该帧）: {e}")
            return
        if not ok:
            return
        self._self_acked[key] = now
        for stale in [k for k, ts in self._self_acked.items()
                      if now - ts > self.ACK_EVIDENCE_TTL]:
            self._self_acked.pop(stale, None)
        last = self._ack_warn_at.get(sn, 0.0)
        if now - last > 600.0:
            self._ack_warn_at[sn] = now
            self._log(
                f"[发现代理] 网关 {sn} 已连续 {len(unseen)} 帧 {ctype} 无人应答"
                f"（gateway/{sn}/req 上零应答）——HA 侧没有订阅者在听本 broker"
                f"（MQTT 条目指向/禁用/未连接，或该网关未配置）。本代理已按同"
                f"形代答止血；请核查 HA「设置→设备与服务→MQTT」条目应指向 "
                f"127.0.0.1:2022，未配置网关请到「设备与服务」完成添加。"
                f"每 SN 10 分钟去重"
            )

    def has_entries(self) -> bool:
        # v1.7.18（第 7 轮审计 BUG-4）：删去 _ears_confirmed 永久缓存——
        # 旧实现确认过一次即永不 list：用户删除/禁用自动建的"等待条目"
        # （卸载残留清理、看不惯占位卡等高概率动作）后，v1.7.11 的秒级
        # 自动发现静默死亡直到容器重启。domain 判定同步补 state 过滤：
        # 禁用/not_loaded 条目没挂心跳监听器，不算耳朵。改为每条上报实时
        # list（一条 GET/≤10s，代价可忽略），上层 _next_try 冷却限频。
        entries = self._list()
        if entries is None:  # 查询失败 → 不改变结论，走重试退避
            raise RuntimeError("entries query failed")
        return any(
            e.get("domain") == DOMAIN
            and (e.get("state") is None or e.get("state") == "loaded")
            for e in entries
        )

    def run_subprocess(self, argv) -> int:
        """长驻订阅循环。mosquitto_sub 退出（broker 重启等）即非零返回，
        交给外层 shell 看门狗重启——与 mdns_publisher 监督模式同构。"""
        # v1.7.33（全量审计）：stderr 并入 stdout——旧实现 DEVNULL 吞掉
        # 认证被拒/broker 拒连的根因，只剩"异常退出 (code N)"，看门狗每 5s
        # 空转静默半瘫（与 run.sh:481 自立的"不再 2>/dev/null 吞启动错误"相悖）。
        # 非触发行由既有的行过滤逻辑丢弃，不污染发现主流程。
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                try:
                    self.handle_line(line)
                except Exception as e:  # 单行毒数据不杀循环
                    self._log(f"[发现代理] 处理上报异常（忽略该行）: {e}")
            return proc.wait() or 1  # EOF（sub 退出）一律视为异常
        finally:
            if proc.poll() is None:
                proc.kill()

    @staticmethod
    def _split_verbose(raw: str):
        """mosquitto_sub -v 的一行 = "topic payload"；兼容纯 payload（旧测试/旧参数）。

        返回 (topic|None, payload)。非 verbose 形态 topic 为 None——调用方据此
        跳过下行记账，行为与 v1.7.59 完全一致。
        """
        raw = raw.rstrip("\r\n")
        if raw.startswith("gateway/"):
            topic, sep, rest = raw.partition(" ")
            if sep:
                return topic, rest.strip()
        return None, raw

    def handle_line(self, raw: str) -> None:
        topic, payload_raw = self._split_verbose(raw)
        if topic is not None and topic.endswith("/req"):
            self._note_downlink(topic, payload_raw)
        parsed = parse_report(payload_raw)
        if parsed is None:
            return
        sn, ctype = parsed
        # v1.7.60：兜底应答与"建耳退避"无关——HA 失聪时退避窗内也必须止血。
        # 契约面 = 001/002/005（网关主动发起、HA 必 ack 的三类）。
        if ctype in ACK_CTYPES:
            try:
                payload = json.loads(payload_raw)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                self._maybe_ack_gateway_initiated(sn, ctype, payload)
        now = self._now()
        if now < self._next_try:
            return
        try:
            if self.has_entries():
                return  # 耳朵早已在（或用户自己加了条目）——纯观察，不重放
            # 1) 建等待条目（无 SN）
            outcome = self._create()
            if outcome not in ("created", "exists"):
                self._next_try = now + RETRY_HTTP_FAIL
                return
            # v1.7.18（BUG-4）：以"短冷却"替代旧"永久确认缓存"——条目 setup
            # 落地前（state 尚未 loaded）与用户删条目后的补种，都由冷却到期
            # 后的下一条上报自然推进
            self._next_try = now + SEED_RETRY_COOLDOWN
            if outcome == "exists":
                self._log("[发现代理] 集成条目已存在，耳朵就位（无需引导）")
                return
            self._log(f"[发现代理] 已创建「等待配置」条目装耳朵（首报网关 {sn}）")
            # 2) 重放原报文让刚挂载的心跳监听器出卡。条目 setup（订阅挂载）
            # 与 create_entry 响应之间存在毫秒级竞态——立即一次 + 3s 兜底一次
            # （监听器幂等：同 SN 已配置时 _protocol/心跳再收也只是 no-op）。
            if sn not in self._replayed:
                # v1.7.12（第 6 轮审计 F7）：发布结果不再丢弃——旧版
                # check=False + stderr 吞掉 + 返回值弃用，mosquitto_pub 缺失
                # /broker 拒连时照样打"已重放上报×2"假日志；且 _replayed 在
                # 发布**前**消费掉该 SN，失败后网关心跳再报也永不重试，
                # 卡片永远不出。显式 False 才算失败（None=旧测试桩视为成功）。
                ok1 = self._pub(raw.strip())
                if ok1 is False:
                    self._log("[发现代理] 重放发布失败（mosquitto_pub 被拒/不可用），"
                              "本 SN 不记账，随下一条上报重试")
                    return
                self._replayed.add(sn)
                try:
                    self._sleep(3.0)
                    self._pub(raw.strip())
                except Exception:
                    pass
                self._log("[发现代理] 已重放上报×2，集成内部发现链应弹出网关卡片")
        except Exception as e:
            self._next_try = self._now() + RETRY_HTTP_FAIL
            self._log(f"[发现代理] 引导失败（{e}），{int(RETRY_HTTP_FAIL)}s 后随下一条上报重试")


def _pub_factory(broker_argv):
    def pub(raw_line: str) -> bool:
        # v1.7.12（F7）：返回发布成败（returncode 判）供调用方记账/重试
        try:
            r = subprocess.run(broker_argv + ["-m", raw_line],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            return r.returncode == 0
        except Exception:
            return False
    return pub


def _read_instance_uuid():
    """读集成落盘的实例指纹（HA 配置目录 = 容器 /homeassistant 或 /config）。

    与 utils.async_write_instance_uuid_file 配对；读不到返回 None（代答降级为
    不动作并在日志说明一次）。
    """
    for base in ("/homeassistant", "/config"):
        try:
            with open(f"{base}/window_controller_gateway_instance.json",
                      encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("uuid"):
            return str(data["uuid"])
    return None


def _ack_factory(port, user, password):
    """001 兜底应答发布器：与 HA 同形同主题（gateway/{sn}/req，QoS1）。"""
    def pub_ack(sn: str, payload: dict) -> bool:
        argv = ["mosquitto_pub", "-h", "127.0.0.1", "-p", str(port),
                "-u", user, "-P", password, "-q", "1",
                "-t", f"gateway/{sn}/req",
                "-m", json.dumps(payload, ensure_ascii=False)]
        try:
            r = subprocess.run(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=False)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    return pub_ack


def list_entries_impl():
    try:
        return ha_api("/config/config_entries/entry")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def create_ears_impl():
    """REST 建流 + 提交空 SN。返回 "created"（真的新建了耳朵）/ "exists"
    （abort：条目已在，无需再建）/ False（可重试的失败）。"""
    try:
        fl = ha_api("/config/config_entries/flow", "POST", {"handler": DOMAIN})
        fid = (fl or {}).get("flow_id")
        if not fid:
            return "exists" if (fl or {}).get("type") == "abort" else False
        # 等待模式：gateway_sn 留空提交（config_flow 空 SN 分支 → 挂心跳监听器）
        step = {"gateway_sn": "", "gateway_name": ""}
        res = ha_api(f"/config/config_entries/flow/{fid}", "POST", step)
        rtype = (res or {}).get("type")
        if rtype == "create_entry":
            return "created"
        if rtype == "abort":
            # already_configured（空条目全局唯一保证/并发）→ 耳朵已在
            return "exists"
        return False  # form（意外形态）等 → 重试
    except urllib.error.HTTPError as e:
        # 400/404：集成代码尚未随 HA 重启加载——可重试失败
        _ = e
        return False
    except (urllib.error.URLError, ValueError, OSError):
        return False


def main(argv) -> int:
    if len(argv) < 4:
        print("用法: gateway_discovery_proxy.py <broker_port> <mqtt_user> <mqtt_password>")
        return 2
    port, user, password = argv[1], argv[2], argv[3]
    # v1.7.60：-v（行首带主题）＋同时订下行 req 主题——001 兜底应答的判据是
    # "请求在响、下行零应答"这条 broker 侧真值，必须看得到 HA 的应答面。
    sub_argv = ["mosquitto_sub", "-h", "127.0.0.1", "-p", port,
                "-u", user, "-P", password, "-v",
                "-t", "gateway/rpt_rsp", "-t", "gateway/+/req"]
    pub_argv = ["mosquitto_pub", "-h", "127.0.0.1", "-p", port,
                "-u", user, "-P", password, "-t", "gateway/rpt_rsp"]
    proxy = DiscoveryProxy(list_entries_impl, create_ears_impl, _pub_factory(pub_argv),
                           publish_ack=_ack_factory(port, user, password),
                           read_uuid=_read_instance_uuid)
    print(f"[发现代理] 启动：订阅 gateway/rpt_rsp + gateway/+/req"
          f"（127.0.0.1:{port}，用户 {user}）——HA 失聪时按同一指纹兜底应答 001")
    return proxy.run_subprocess(sub_argv)


if __name__ == "__main__":
    # 容器内 stdout 重定向到 Supervisor 日志管道：无终端走全缓冲，崩溃/关键
    # 行会滞留——强制行缓冲，日志实时可见
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    sys.exit(main(sys.argv))
