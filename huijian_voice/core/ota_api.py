"""设备台账与固件仓端点（OTA 方案 Phase 2 加载项侧，2026-09-23）。

独立小模块——避开 admin_api 热区的并行冲突（tts_voices_api 同惯例）。
挂载：admin_api.make_admin_app 内 `ota_api.setup(app, ctx)` 一行。

契约：
  GET  /api/devices            → 卫星台账（数据真源=集成 HuijianSatellitesView：
                                  CMD20 入驻 fw_version + API 在线态 + 区域），
                                  叠加固件仓比对出 updatable；集成不可达回
                                  {"devices": [], "error": …} 不 500。
  GET  /api/firmware           → FirmwareStore.status()：登记版本/在盘/最新/投递口。
  POST /api/firmware/download  → {version} 按 lock urls 拉包（GitHub→Gitee 容灾序，
                                  sha256 必核，运维点触——**无自动公网拉取**，
                                  运行期零 GitHub 依赖定案不破）。
  POST /api/firmware/issue     → {version?, mac?} 生成一次性领取链接（10min）：
                                  现网固件(v2.1.35)无远程接收口；且小程序 v1.4.14
                                  尚无 CMD21 代发入口（2026-09 OTA 深审查证）——
                                  链接为备存形态，面板话术已如实降级；
                                  固件 v2.1.36+ 的 ota_services 非空后面板同键
                                  切真下发（集成侧已探测）。
  POST /api/device/continuous  → {mac, enabled} 连续对话开关（v1.0.80）：中继
                                  集成写设备 esphome switch 实体（真源=设备 NVS
                                  cDialogue，HA/小程序/面板三边同源回显）。
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiohttp import web

from . import const
from .firmware_store import vkey
from .mdns import local_ip

logger = logging.getLogger("huijian.ota")

_SATELLITES_PATH = "/api/huijian-ai/satellites"


def issue_url(host: str, fname: str, token: str, sha256="") -> str:
    """一次性领取链接的**唯一装配点**（v2.3.7 起把期望摘要一起带在 query 上）。

    为什么走 URL、不给设备服务加参数：设备的 `ota_upgrade` user service 现在只声明一个
    参数 `url`，集成侧也只发 `{"url": …}`；参数表一改＝所有未升级的加载项下发当场因
    参数个数不匹配失败——两侧不能版本硬锁死。URL query 是唯一不需要同版本的通道，
    且不认这个参数的老固件照旧能用。

    摘要形态不合（非 64 位 hex，含带空格的"看着像"）就**不带**这个参数：宁可不核，
    也不能把一个被截断/被改写的摘要当真值送去——那会把一次本来正常的升级拒掉。
    设备侧同口径（取不到即"核验关闭"并响亮报 OFF），两边都不许静默降级。
    """
    url = f"http://{host}:{const.WS_PORT}/firmware/{quote(fname)}?t={token}"
    s = sha256 if isinstance(sha256, str) else ""
    if len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
        url += "&sha256=" + s.lower()
    return url


async def _json_body(request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bridge_ok(ha) -> bool:
    """HA 桥可用判据（v1.1.27，单点）：**真连通**（`.reachable`）且凭证已配置（`.ok`）。

    旧写法只看 `.ok`（＝凭证已配置，见 ha_client 注释）⇒ HA 掉线时面板把「网络不通」
    误报成「集成没有台账接口——请把 custom_components/huijian_ai 升级后重启 HA」，
    用户照着错指引升级集成，真因永不露头（同 admin_api:75 用 `.reachable` 的口径）。
    `.ok=False` 时 ha_client 的一切请求原地返回（发不出去）⇒ 同样按"桥不可用"拒：
    否则会白签一枚 10min 令牌、面板显示"已下发"（旧钉 test_dispatch_bridge_down_no_issue
    钉的正是这条）。永不抛（判不了＝不可用，failing closed）。"""
    try:
        return bool(ha and getattr(ha, "reachable", False)
                    and getattr(ha, "ok", True))
    except Exception:  # noqa: BLE001
        return False


async def _live_device_version(ctx, mac: str) -> str:
    """目标设备的**实时**固件读数（判 `min_compatible` 用）；拿不到就回 ""。

    v1.7.5：判据单点在服务端（面板只渲染 `fw_state`，F-OTA-04 同律）——所以签发/下发
    都自己回台账读一次，不要求前端把读数带上来。只认 `fw_source=="实时"` 那一档：
    「入驻时」快照可能是几周前的版本，拿它去拒一次合法下发＝用一个不可信的值做否决，
    正是 1.6.1/1.6.4 两次实锤后立下的"不可判就不断言"纪律。
    """
    try:
        led = await ctx.ha.rest_get(_SATELLITES_PATH) if ctx.ha else None
        for row in ((led or {}).get("devices") or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("mac", "")).strip()[:40].lower() != mac.lower():
                continue
            if str(row.get("fw_source") or "") != "实时":
                return ""
            return str(row.get("fw_version") or "").strip()[:24]
    except Exception:  # noqa: BLE001 读数失败退回"不判"，绝不因新规多一处 500
        return ""
    return ""


def setup(app, ctx):
    async def _devices(request):
        store = getattr(ctx, "firmware", None)
        latest = None
        try:
            latest = await asyncio.to_thread(store.latest) if store else None
        except Exception as e:  # noqa: BLE001 面板数据面永不抛
            logger.warning("[OTA] 固件仓状态读取失败: %s", e)
        # v1.1.27：可达判据单点 `_bridge_ok`（真连通 + 凭证在），旧写法只看 `.ok`
        # ⇒ HA 掉线被误报成「集成没有台账接口，请升级 huijian_ai 并重启 HA」。
        bridge_ok = _bridge_ok(ctx.ha)
        led = await ctx.ha.rest_get(_SATELLITES_PATH) if ctx.ha else None
        devices = (led or {}).get("devices")
        if devices is None:
            # v1.0.65 用户反馈：面板空表必须把原因说透——此前一种文案背两种锅
            if not bridge_ok:
                err = ("HA 桥未连接——请在设置页填好 HA 地址与长期令牌并确认"
                       f"连接绿标（当前 {getattr(const, 'APP_VERSION', '?')} 版加载项要求 HA 可达）")
            else:
                err = ("HA 里的 huijian_ai 集成没有卫星台账接口——集成与加载项是"
                       "两份代码，请把 HA 的 custom_components/huijian_ai 升级到"
                       f"与本加载项同版本（≥{const.APP_VERSION}）后重启 HA，"
                       "卫星入驻后台账自动出现")
            return web.json_response({
                "devices": [],
                "latest": (latest or {}).get("version", "") if latest else "",
                "error": err})
        for d in devices:
            cur = str(d.get("fw_version") or "")
            # v1.6.6 刀A：徽章判据收在本服务端单点（面板不复装，F-OTA-04 同律）。
            # 「实时」＝本轮真连上过设备读到的握手值/本运行期台账值；其余档（未上报、
            # 集成旧版根本没有 fw_source、或落「入驻时」持久快照）都不得断言版本关系
            # ——1.6.1 的 USB 重烧挂旧值、1.6.4 的断连窗口谎标「实时」两次实锤，载体
            # 都是"拿不可信的 fw_version 去比 latest"，而那正是 1.6.4 只修了显示标签、
            # 没修这里的 `updatable` 与面板「已最新」pill 留下的后半截。
            src = str(d.get("fw_source") or "")
            if not latest:
                state = "no-package"
            elif not cur or src != "实时":
                state = "unverified"
            elif vkey(cur) < vkey(latest["version"]):
                # v1.7.5：锁里的 min_compatible 从这里开始**真的被消费**（10-24 审出：
                # 字段登记了但全仓零消费者 ⇒ 2.1.x/2.2.8 会被标"可升级"，而设备侧 OTA
                # 只换 app 段、assets 段留旧代唤醒模型 ⇒ 现场读来是"升级成功但叫不应"）。
                minv = str(latest.get("min_compatible") or "").strip()
                state = ("incompatible" if minv and vkey(cur) < vkey(minv) else "updatable")
            else:
                state = "up-to-date"
            d["fw_state"] = state
            d["updatable"] = state == "updatable"
        return web.json_response({
            "devices": devices,
            "latest": (latest or {}).get("version", "") if latest else ""})

    async def _firmware(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"items": [], "latest": "", "error": "固件仓未挂载"})
        try:
            return web.json_response(await asyncio.to_thread(store.status))
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 固件仓快照失败: %s", e)
            return web.json_response({"items": [], "latest": "", "error": str(e)})

    async def _download(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        if not version:
            return web.json_response({"success": False, "error": "version 必填"}, status=400)
        try:   # v1.0.65 F-OTA-04：面板数据面永不抛——store 侧异常折叠 JSON
            ok, msg = await asyncio.to_thread(store.download, version)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 拉取异常: %s", e)
            return web.json_response({"success": False, "error": f"拉取异常：{e}"[:200]},
                                     status=500)
        logger.info("[OTA] 拉取 v%s → %s（%s）", version, "ok" if ok else "失败", msg)
        return web.json_response({"success": ok, "message": msg})

    async def _issue(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        # mac 仅日志要素，入日志前净化（安全 F4 同族）；空/脏不影响签发
        mac = str(body.get("mac", "")).strip()[:40]
        # v1.7.5：目标设备的**实时**固件读数（面板按设备行传；缺则不判 min_compatible）。
        # 判据本体在 firmware_store.ota_block_reason，本层只负责把读数带下去＋把真因说清。
        dev_ver = str(body.get("device_version", "") or "").strip()[:24]
        if not dev_ver:
            dev_ver = await _live_device_version(ctx, mac)
        res = None
        try:   # v1.0.65 F-OTA-04：含 latest 兜底分支整体折叠——违背「永不抛」契约即 500
            if version:
                res = await asyncio.to_thread(store.issue, version, mac, dev_ver)
            else:
                lat = await asyncio.to_thread(store.latest)
                if lat:
                    version = lat["version"]
                    res = await asyncio.to_thread(store.issue, version, mac, dev_ver)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 签发异常: %s", e)
            res = None
        if not res:
            # 拒签真因单独出精确话术：糊成「不在盘」会让人反复重新投递/重拉，而真因可能
            # 是账上登记的载荷类型错了（合并出厂镜像 ≠ app 镜像），重投多少次都没用。
            # v1.1.27：判据改 `ota_block_reason`（旧 `capacity_error` 只查容量闸，小体积
            # 合并镜像被形态闸拒时拿到 "" ⇒ 真因永不露头）；且**放线程**读——同函数其余
            # store 调用（latest/issue/status/download）一律 to_thread，本处旧写法在
            # :8000 事件循环线程里同步扫盘（import 目录扫描 + 逐包读头）。
            cap = ""
            try:
                cap = (await asyncio.to_thread(store.ota_block_reason, version, dev_ver)
                       if version else "")
            except Exception:  # noqa: BLE001
                cap = ""
            if cap:
                logger.warning("[OTA] 签发被拒（具名真因）：%s", cap)
                return web.json_response({"success": False, "error": cap}, status=400)
            return web.json_response({
                "success": False,
                "error": "该版本不在盘（投递口放包或先「拉取」；或尚无已登记版本）"}, status=400)
        # host 每次签发实时取（v1.0.65 契约 F-09）：ctx.host 是启动时一次性采样，
        # DHCP 重绑后旧值会让链接静默失效；回环地址签发无意义，如实报错。
        host = local_ip()
        if not host or host.startswith("127."):
            return web.json_response({
                "success": False,
                "error": "加载项当前无可路由局域网地址，链接无法供设备领取——"
                         "请确认本机已接入设备所在局域网后重试"}, status=503)
        url = issue_url(host, res["file"], res["token"], res.get("sha256", ""))
        logger.info("[OTA] 生成近场下发链接 v%s mac=%s", res["version"], mac or "-")
        return web.json_response({
            "success": True, "url": url, "sha256": res["sha256"], "size": res["size"],
            "version": res["version"], "expires_in": res["expires_in"],
            "warn_zh": "链接已按 v2.3.7 口径带上期望摘要（&sha256=…）：固件 ≥v2.3.7 会在切槽前逐字节核对，"
                       "不符即拒刷并留在旧版；v2.3.6 及更早、以及 v2.1.35 及更早固件不校验哈希/不防降级——"
                       "请核对版本号后再代发；"
                       "链接一次性、10 分钟过期，过期或下载中断请回本页重新签发；"
                       "仅限设备所在局域网内打开；当前小程序尚无 CMD21 代发入口"
                       "（代发能力未上线，链接先用于备存）"})

    async def _dispatch(request):
        """v1.0.74 真下发：issue + 经集成中继调设备 :6053 用户服务 ota_upgrade。
        设备接收口仅 ≥v2.1.36 有；URL 私网白名单闸在设备侧（单一事实源，此处
        不复装）。令牌签出后若下发失败不回滚——10min TTL 自然作废（防竞态
        简单化），日志点名。全路径折叠 JSON 永不抛（F-OTA-04 同纪律）。"""
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        mac = str(body.get("mac", "")).strip()[:40]
        if not mac:
            return web.json_response(
                {"success": False, "error": "mac 必填——下发按设备寻址，防发错机"}, status=400)
        res = None
        # v1.7.5：先建默认值——签发前任何早退/异常都不能让下面 `if not res:` 的拒因分支
        # 读到未定义名（那条分支同样要拿它判 min_compatible）。
        dev_ver = str(body.get("device_version", "") or "").strip()[:24]
        if not dev_ver:
            dev_ver = await _live_device_version(ctx, mac)
        try:
            if not version:
                lat = await asyncio.to_thread(store.latest)
                if not lat:
                    return web.json_response(
                        {"success": False, "error": "固件仓无已登记版本——投递口放包或先「拉取」"},
                        status=400)
                version = lat["version"]
            # 第四轮审计 P2：桥断**先判**——旧序先签 10min 令牌再回 502，白签一枚
            # （docstring 自述"否则会白签"，实现与文档相反，且令牌驻留内存 10min）。
            if not _bridge_ok(ctx.ha):
                return web.json_response({
                    "success": False, "error": "HA 桥未连接，无法中继到设备",
                    "version": version}, status=502)
            # v1.7.5：`dev_ver`（判 min_compatible 用）已在进 try 之前经 `_live_device_version`
            # 解析过——判据只在服务端口径一处，面板不上送读数（F-OTA-04：一种文案不背两种锅）。
            res = await asyncio.to_thread(store.issue, version, mac, dev_ver)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] dispatch 签发异常: %s", e)
            res = None
        if not res:
            # 同 `_issue`：拒因单一来源（ota_block_reason）且放线程（v1.1.27）。
            cap = ""
            try:
                cap = (await asyncio.to_thread(store.ota_block_reason, version, dev_ver)
                       if version else "")
            except Exception:  # noqa: BLE001
                cap = ""
            if cap:
                logger.warning("[OTA] dispatch 被拒 v%s mac=%s：%s", version, mac, cap)
                return web.json_response({"success": False, "error": cap,
                                          "version": version}, status=400)
            return web.json_response(
                {"success": False, "error": "该版本不在盘（投递口放包或先「拉取」）"}, status=400)
        host = local_ip()
        if not host or host.startswith("127."):
            return web.json_response({
                "success": False,
                "error": "加载项无可路由局域网地址——设备白名单闸只收私网字面 IPv4"}, status=503)
        url = issue_url(host, res["file"], res["token"], res.get("sha256", ""))
        # v1.1.27：可达判据单点 `_bridge_ok`（第四轮审计 P2 起已**前移到签发之前**，
        # 见上方——桥断时不得白签令牌）。
        ack = await ctx.ha.rest_write("POST", "/api/huijian-ai/satellites/ota",
                                      {"mac": mac, "url": url})
        if not ack.get("success"):
            logger.warning("[OTA] dispatch 失败 v%s mac=%s: %s", version, mac,
                           ack.get("error", "?"))
            return web.json_response({
                "success": False, "error": ack.get("error", "集成未受理"), "version": version})
        logger.info("[OTA] 真下发 v%s → %s sha=%s… mac=%s",
                    version, res["file"], str(res["sha256"])[:12], mac)
        return web.json_response({
            "success": True, "version": version, "size": res["size"],
            "sha256": res["sha256"],
            "note": "设备已受理并立即下载（局域网约 1-3 分钟），成功后自动重启；"
                    "5-10 分钟后刷新台账核对版本号"})

    async def _device_continuous(request):
        """v1.0.80：面板「连续对话」开关 → 中继集成写设备 switch 实体。
        真源在设备 NVS（cDialogue，HA/小程序/面板三边同源），本端点纯转发；
        桥断/无实体/失败全折叠 JSON 永不抛（F-OTA-04 同纪律）。"""
        body = await _json_body(request)
        mac = str(body.get("mac", "")).strip()[:40]
        if not mac:
            return web.json_response({"success": False, "error": "mac 必填"}, status=400)
        enabled = bool(body.get("enabled", False))
        # v1.1.27：同 `_dispatch`——可达判据单点 `_bridge_ok`，不用裸 `.ok`。
        bridge_ok = _bridge_ok(ctx.ha)
        if not bridge_ok:
            return web.json_response({"success": False, "error": "HA 桥未连接"}, status=502)
        ack = await ctx.ha.rest_write("POST", "/api/huijian-ai/satellites/continuous",
                                      {"mac": mac, "enabled": enabled})
        if not ack.get("success"):
            logger.info("[连续对话] 中继拒绝 mac=%s: %s", mac, ack.get("error", "?"))
            return web.json_response({"success": False,
                                      "error": ack.get("error", "集成未受理")})
        # v1.0.87：echoed=设备是否已把新态回灌到 HA（集成侧有界复核 ≤1.6s）。
        # 本端纯转发不装判据、原样透传；None=对面集成尚不报此字段（版本不齐）。
        echoed = ack.get("echoed")
        logger.info("[连续对话] mac=%s → %s（设备回显=%s）",
                    mac, "开" if enabled else "关", echoed)
        return web.json_response({"success": True, "enabled": enabled,
                                  "echoed": echoed})

    app.router.add_get("/api/devices", _devices)
    app.router.add_get("/api/firmware", _firmware)
    app.router.add_post("/api/firmware/download", _download)
    app.router.add_post("/api/firmware/issue", _issue)
    app.router.add_post("/api/firmware/dispatch", _dispatch)
    app.router.add_post("/api/device/continuous", _device_continuous)
