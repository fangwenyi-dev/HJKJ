#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghcr → 阿里云 ACR 镜像转码推送器（v1.0.1 定案，push-acr run1/run2 教训）.

根因（本地实证 2026-09-06）：hassio builder 以 buildx 默认压缩推 ghcr，
层 mediaType=application/vnd.oci.image.layer.v1.tar+zstd；ACR 个人版对
非 gzip 层流在上传阶段即拒（PATCH 403 → 前端表现为 PUT "unknown: blob
type invalid"）。鉴别实验：同一 PUT 通道下 真gzip流=202 / zstd魔数=403 /
纯字节=403 —— ACR 校验的是层内容 gzip 帧本身，不只是 mediaType 字符串。
`imagetools create` 按 digest 复制原 blob 无法过此关，故必须转码：
下载 zstd 层 → 解压 → 重压缩 gzip（mtime=0 确定性）→ 重写 manifest
（层 mediaType 换 tar+gzip，config/diff_ids 不动）→ 推送。

用法（凭据只从环境变量 ACR_USER / ACR_PASS 读，绝不落参数/日志）：
  # 转码单架构子镜像：src 指定 manifest digest，推成 dst 仓的 tag
  python3 acr_transcode.py transcode \
      --src ghcr.io --src-repo OWNER/amd64-huijian-voice \
      --src-ref sha256:... --dst REG --dst-repo NS/repo --dst-tag 1.0.1-amd64
  # 无 docker 环境下合成多架构 index（CI 用 imagetools，二者等价）
  python3 acr_transcode.py index --dst REG --dst-repo NS/repo \
      --tag 1.0.1 --member amd64=sha256:... --member arm64=sha256:...

正确性硬约束：解压后的层 sha256 必须逐条等于 config.diff_ids（镜像合法性
的定义），不等立即中止——保证转码只换压缩容器、不动文件系统内容。
"""
import argparse
import base64
import gzip
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
CHUNK = 2 * 1024 * 1024   # 降级链路实测 8MB 块 300s 写不完（<30KB/s）；2MB 块=同速下 67s 稳过
UA = {"User-Agent": "acr-transcode/1.0 (huijian_voice CI)"}

# ── 墙钟预算（v1.1.17 收口）───────────────────────────────────────
# 实测（v1.1.14 那轮逐块时间戳）：跨境中位 ≈37.9KB/s ⇒ 2MB 块 ≈54s。结构性坑是
# **单个卡死块的最坏代价**＝3 轮 × 6 次 × 300s socket 超时 = 5400s，与 CI 的 90min
# job 预算同量级 ⇒ 一块就能把整次发版耗死（run 36249197066：90.1min 被硬杀，日志
# 停在 layer6 54.5/76.5MB，两头都看不出"是卡在某个块上"）。
# 现加**转码链墙钟预算**：到期即抛具名 RuntimeError 退出，把失败原因留在日志里，
# 也把 job 预算让给后续重试（`gh run rerun --failed` 可只补没传完的层）。
# 默认 35min：push-acr job 里是**串行两次** transcode（ci.yaml 的 DA/DR 两步，两个架构
# 各一次），而 job 上限 90min ⇒ 单链预算必须 ≤ (90−余量)/2。上一版写的 60min 只按
# "一条链 ≤75min"算，两档都慢时第二档照样被 CI 硬杀（1.1.16 的 run 36330078632 就是这么
# 死的：90min 整点 cancel ⇒ Release skip）。
# 判断点在每次尝试/每轮起点，超期最多多走一次在途超时＝`OVERSHOOT_S`（见下）。
# **一条链一个时钟**（ACR 下载腿复验 §1.2/§2.5 改）：旧形 `PUSH_DEADLINE_S` 只在
# `request()`/`push_blob()` 里查，下载腿自己另有 900s ⇒
# ①逐 blob 900s × (19 层 + config) = **5h**，比 push-acr job 的 90min 上限大一个量级，
#   而这道闸存在的理由恰恰是"别被 CI 硬杀"（v1.1.14/1.1.16 的形状）——下载期不查
#   链时钟＝闸护不到 job；
# ②`push_deadline_reset()` 在下载**之前**起算，慢下载把 40min 推送预算吃光后，第一条
#   push 报「ACR 推送墙钟预算耗尽」，归因指错腿。
# 现在下载与推送共用同一个链时钟，报错点名当时在哪个腿。
# 数值从 2400 降到 **2100**：预算必须与 `OVERSHOOT_S` 一起对账（旧钉只算
# `2×40min+10min ≤ 90min`＝刚好卡线，把在途读写超时与退避算进去就漏了；探针
# `_goldtest/adv1141_push_overshoot.py`：预算 2s 实耗 8.03s）。
# `2 × (2100 + 365) / 60 + 5 ≈ 87.2min ≤ job 90min`，由算术钉判死。
CHAIN_DEADLINE_S = float(os.environ.get("ACR_CHAIN_DEADLINE_S", "2100"))
# **单个 blob** 的墙钟子预算（链时钟之外的局部闸）：防某一层滴流独吞整条链，
# 并让日志能指到具体层名。生效值取 `min(本条, 链时钟剩余)`，见 `get_blob` 的 `end`。
# 这条纪律的来由（v1.1.39 本机代推实锤）：`urlopen(timeout=600)` 只管单次 socket 读，
# 遇到"还在滴但极慢"的镜像源就能把一次调用钉死——发版当晚两次各 595s 前台预算内
# **零层完成**，日志停在 "19 层，config 19107B" 之后一行不出，与产品侧
# `core/model_store._download_any` 的"无总时限"完全同型（对照同源：
# `firmware_store.download` 早就有 deadline）。
FETCH_DEADLINE_S = float(os.environ.get("ACR_FETCH_DEADLINE_S", "900"))
# 超冲上界（与 job 对账必须带上它，只比预算＝算术钉假绿，复验 C 段实测）：
# 判断点之间最坏多走＝一次在途**写**超时(300s) ＋ 一次在途**读**超时(sock_timeout 60)
# ＋ 一轮退避(≤5s)。探针 `adv1141_push_overshoot.py`：预算 2s 实耗 8.03s。
OVERSHOOT_S = 300 + 60 + 5
_deadline_at = 0.0        # 0＝未进入转码链（不施加预算；monotonic 基准）
_deadline_budget = None   # 本次实际起算的秒数（报错印这个，别谎报模块默认值）


def chain_deadline_reset(seconds=None):
    """进入转码链（下载+推送）时调用一次：从现在起 N 秒内必须走完（None=模块默认）。"""
    global _deadline_at, _deadline_budget
    _deadline_budget = float(CHAIN_DEADLINE_S if seconds is None else seconds)
    _deadline_at = time.monotonic() + _deadline_budget


# 旧名保留为别名会让"这是推送预算"的错误语义继续活着（复验点名的就是这条），
# 因此不留 shim：调用方一律改 `chain_deadline_reset`。
def chain_deadline_clear():
    """退出转码链/测试收尾：撤销预算（不影响后续 manifest 等调用）。"""
    global _deadline_at, _deadline_budget
    _deadline_at = 0.0
    _deadline_budget = None


def _deadline_hit() -> bool:
    return _deadline_at > 0 and time.monotonic() >= _deadline_at


def _deadline_err(leg: str = "") -> RuntimeError:
    where = f"（当前卡在{leg}腿）" if leg else ""
    budget = CHAIN_DEADLINE_S if _deadline_budget is None else _deadline_budget
    return RuntimeError(
        f"ACR 转码墙钟预算耗尽（{budget:.0f}s）{where}：链路过慢或卡死，"
        f"主动退出交上层重试（不再等 CI 硬杀）")


def _fetch_budget_err(label, budget, got, size) -> RuntimeError:
    """下载超预算的具名错：先判是不是**链时钟**到点（报链、点名下载腿），
    否则报本块子预算——两种都得是"墙钟预算"，不许掉进「零字节/字节数不符」里
    让排障的人以为是源坏了。`budget` 只用于日志（真实到点的是哪一个由时钟判）。"""
    if _deadline_hit():
        return _deadline_err("下载")
    return RuntimeError(
        f"{label} 下载超墙钟预算 {budget:.0f}s（已收 {got / 2 ** 20:.1f}/"
        f"{(size or 0) / 2 ** 20:.1f}MB）→ 主动退出交重试")


def _cred(env_name, file_path):
    """CI：Secrets→env。本地：~/.acr_user / ~/.acr_pass（600 权限凭证文件，
    绝不入库）——WSL→Windows exe 桥不透传 env，本地实证必须走 WSL python3 +
    文件路径（2026-09-06 实发）。"""
    v = os.environ.get(env_name, "")
    if v:
        return v
    try:
        with open(os.path.expanduser(file_path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def emit(digest: str) -> None:
    """机器契约：stdout 只此一行纯 digest（CI $(cmd) 捕获+格式校验）。"""
    print(digest)


def log(msg):
    """人类日志走 stderr——stdout 是 digest 单通道（run4 教训：print 默认
    stdout 把日志前缀混进 $(cmd|tail -1) 捕获，拼 URL 控制字符崩）。"""
    print(f"[acr-transcode] {msg}", file=sys.stderr, flush=True)


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def stream_open(req, timeout=60):
    """流式取体的**唯一**入口（与 `http_get` 同一钩子面，永不包装语义）。

    本机代推 `_acr_local_push.py` 靠钩 `http_get` 把 ghcr 数据面重写到国内代理
    （直连 ghcr 本机实测 ≈37KB/s＝不可用，代理 ≈2MB/s）。v1.1.41 把 blob 下载从
    `get(binary=True)` 换成 `get_blob` 流式读时差点绕过这个钩子——发版前对抗复核
    抓出（探针 `_goldtest/adv1141_chain_spy.py`：`http_get` 被调用 0 次 ⇒ 20 个 blob
    会变回直连，正是这批要治的慢链路）。**凡取流必须走这里**，别在函数里直调 urlopen。
    """
    return urllib.request.urlopen(req, timeout=timeout)


def http_get(url, headers=None, timeout=60, binary=False):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read()
    return (data if binary else data.decode("utf-8", "replace")), dict(resp.headers)


def zstd_decompress(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError as e:
        raise SystemExit("需要 zstandard 库: pip install zstandard（CI step 已自动装）") from e
    dctx = zstandard.ZstdDecompressor()
    return dctx.stream_reader(__import__("io").BytesIO(data)).read()


# ────────────────────────── registry 认证面 ──────────────────────────

class Registry:
    """token 链全动态发现（realm/service 都从 401 头拿，不硬编码——防
    ACR 端点漂移臆造；对 ghcr 同样成立）。"""

    def __init__(self, host, user=None, password=None):
        self.host = host
        self.user = user
        self.password = password
        self._realm = None
        self._service = None
        self._tokens = {}

    def _challenge(self):
        if self._realm:
            return
        try:
            http_get(f"https://{self.host}/v2/", timeout=15)
            realm, service = f"https://{self.host}/v2/token", self.host
        except urllib.error.HTTPError as e:
            auth = e.headers.get("Www-Authenticate") or e.headers.get("WWW-Authenticate") or ""
            m = re.search(r'realm="([^"]+)"', auth)
            if not m:
                raise RuntimeError(f"{self.host}: 401 无 realm（auth头={auth!r}）")
            s = re.search(r'service="([^"]+)"', auth)
            realm, service = m.group(1), (s.group(1) if s else self.host)
        self._realm, self._service = realm, service

    def token(self, repo, actions, fresh=False):
        self._challenge()
        scope = f"repository:{repo}:{','.join(actions)}"
        key = scope
        if not fresh and key in self._tokens:
            return self._tokens[key]
        hdr = {}
        if self.user:
            b = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            hdr["Authorization"] = "Basic " + b
        url = f"{self._realm}?service={urllib.parse.quote(self._service)}&scope={urllib.parse.quote(scope)}"
        body, _ = http_get(url, hdr, timeout=20)
        tok = json.loads(body)
        t = tok.get("token") or tok.get("access_token")
        if not t:
            raise RuntimeError(f"{self.host}: token 交换失败（repo={repo}）")
        self._tokens[key] = t
        return t

    def head(self, path, repo, actions=("pull",), timeout=20) -> bool:
        """零响应体的存在性探测（第四轮审计 P3）。True=已在盘；404=False；
        其余异常原样抛；401 与 get 同规 fresh 重取一次。"""
        h = {}
        for i in range(2):
            t = self.token(repo, list(actions), fresh=(i > 0))
            h["Authorization"] = "Bearer " + t
            try:
                req = urllib.request.Request(f"https://{self.host}{path}", method="HEAD")
                for k, v in h.items():
                    req.add_header(k, v)
                urllib.request.urlopen(req, timeout=timeout)
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return False
                if e.code != 401 or i == 1:
                    raise
        return False

    def get(self, path, repo, actions=("pull",), headers=None, binary=False, timeout=60,
            attempts=3):
        # run 35681170094 实证：request() 会 fresh 重取 token 了，但 get() 还用
        # 缓存——76MB 层传完 token 已过期，下一层秒传探测 401 且非 404 直接炸穿。
        # 401 时 fresh 重取再试（穷尽后原样抛，交 call site 语义处理）。
        h = dict(headers or {})
        for i in range(attempts):
            t = self.token(repo, list(actions), fresh=(i > 0))
            h["Authorization"] = "Bearer " + t
            try:
                return http_get(f"https://{self.host}{path}", h, timeout=timeout, binary=binary)
            except urllib.error.HTTPError as e:
                if e.code != 401 or i + 1 >= attempts:
                    raise
                log(f"  ⚿ GET {path.split('?')[0]} 401，重取 token 重试")
                time.sleep(2)

    def _conn(self, timeout=300):
        return http.client.HTTPSConnection(self.host, timeout=timeout)

    def get_blob(self, path, repo, label="", size=0, expect_digest="",
                 actions=("pull",), sock_timeout=60, chunk=1 << 20,
                 deadline_s=None):
        """流式取一个 blob：**墙钟总时限**＋Range 续传＋每 16MB 刷进度＋逐字节校验。

        与链时钟 `CHAIN_DEADLINE_S` 同一条纪律——缺了它，一次"滴流不断"的读就能把
        整个发版钉死（v1.1.39 本机代推就是这么卡的）。四点硬要求：
        ①超期抛**具名** RuntimeError（日志里要看得出卡在哪层多少 MB）；
        ②续传只在源真回 206 时才有意义：代理类源忽略 Range 回 200 全量，此时
          必须丢弃已收字节重头取（否则 200 的全量体会被拼在半截后面＝静默损坏）；
        ③拼完必须按 manifest 声明的 digest/size 校验——**拼错的层比没传完更坏**
          （它会以"合法镜像"的身份被推到 ACR，用户刷完直接变砖）；
        ④复检必须跟得住"一次读一直阻塞"这个形状：`HTTPResponse.read(n)` 是
          BufferedReader 语义——**攒满 n 才返回**，滴流不断时单次调用可堵
          `chunk/速率` 秒（1MB ÷ 5B/s ≈ 60h），而墙钟复检在循环顶部 ⇒ **永不可达**。
          读流因此用 `read1(n)`：一次底层读即返回，复检频率跟随数据到达事件
          （真 socket 滴流实测：`read` ⇒ 预算 2s 被套到 6s 看门狗、复检 0 次；
          `read1` ⇒ 2.0s 当场抛具名错。`_goldtest/adv_verify_acr_drip.py`）。
        预算层级：`end = min(now + deadline_s(默认 FETCH_DEADLINE_S), 链时钟
        _deadline_at)` ⇒ 单块独吞与整链超 job 都封得住（链时钟由调用方在进链前
        `chain_deadline_reset()` 起算，下载与推送共用）。
        """
        budget = float(FETCH_DEADLINE_S if deadline_s is None else deadline_s)
        end = time.monotonic() + budget
        if _deadline_at:                    # 下载腿也认链时钟（旧形只护推送腿）
            end = min(end, _deadline_at)
        data = bytearray()
        budget_hit = False
        full_via_416 = False
        for attempt in range(1, 7):
            if attempt > 1:
                # 退避挪到"要再打一轮"这一处，恰好在轮与轮之间睡一次。旧形把
                # `time.sleep` 写在**内层尾部**，两个方向都错：401 就地换 token 重打
                # 反而不睡、非 401 每轮睡两遍（还顺手把请求打了两遍），而 OSError 那支
                # `break` 绕过了 sleep＝完全不退避。本批改内层时差点把退避整条删掉
                # （自查抓出），所以这里配一条计数钉。
                time.sleep(min(5.0, 1.5 * (attempt - 1)))
            if time.monotonic() >= end:
                budget_hit = True
                break
            resp = None
            for i in (0, 1):
                # 只有 401 才换 fresh token 就地再打一次（与 `get()` 同规）。旧形所有
                # 错误都走这条内层循环 ⇒ 每次 attempt 打两遍、睡两遍（实测 6 attempt
                # → **12 次 urlopen**，日志同一个"第N次"出两行），而 500/502 这类
                # 语义错重发并不会变好＝错误流量白白翻倍。
                t = self.token(repo, list(actions), fresh=(i > 0))
                req = urllib.request.Request(
                    f"https://{self.host}{path}",
                    headers={**UA, "Authorization": "Bearer " + t})
                if data:
                    req.add_header("Range", f"bytes={len(data)}-")
                try:
                    resp = stream_open(req, timeout=sock_timeout)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 401 and i == 0:
                        continue
                    if e.code == 416 and size and len(data) == size:
                        # 字节已收满、只差一个干净 EOF：源对 `bytes=<size>-` 回 416 是
                        # **正确**答复。旧形裸 `raise` 让 HTTPError 直穿调用方 ⇒ 明明
                        # 手里就有完整数据，却连按 size/digest 校验的机会都没有。
                        log(f"  {label} 416＝已收满 {size}B，按完整体走校验")
                        full_via_416 = True
                        break
                    if e.code == 404:
                        raise
                    log(f"  {label} 取回 HTTP {e.code}（第{attempt}次）→ 重试")
                    break
                except (OSError, http.client.HTTPException) as e:
                    log(f"  {label} 连接异常 {type(e).__name__}（第{attempt}次）"
                        f"@{len(data) / 2 ** 20:.1f}MB → 续传重试")
                    break
            if full_via_416:
                break
            if resp is None:
                continue          # 退避在下一轮 attempt 顶部统一做（见循环起始处）
            complete = False
            try:
                if data and resp.status != 206:
                    log(f"  {label} 源不支持 Range（HTTP {resp.status}）"
                        f"→ 丢弃已收 {len(data) / 2 ** 20:.1f}MB 重头取")
                    data = bytearray()
                step = 16 << 20
                marked = len(data) // step
                while True:
                    if time.monotonic() >= end:
                        raise _fetch_budget_err(label, budget, len(data), size)
                    try:
                        b = resp.read1(chunk)
                    except (OSError, http.client.HTTPException) as e:
                        log(f"  {label} 读流中断（{type(e).__name__}）"
                            f"@{len(data) / 2 ** 20:.1f}MB → 续传重试")
                        break
                    if not b:
                        complete = True
                        break
                    data += b
                    if len(data) // step != marked:
                        marked = len(data) // step
                        log(f"  ↓ {label} {len(data) / 2 ** 20:.1f}/"
                            f"{(size or 0) / 2 ** 20:.1f}MB")
            finally:
                # 下载腿也要关响应：push 腿早有 `finally: c.close()`，这一腿漏了
                # （实测 6 次尝试 → `close()` 调用 0 次，套接字只能等 GC）。
                try:
                    resp.close()
                except Exception:            # noqa: BLE001 —— 收尾关闭不参与裁决
                    pass
            if complete:
                break
        raw = bytes(data)
        if budget_hit:
            # 预算在"本轮起点"就已到点：旧形会往下掉进「下载失败（零字节）」或
            # 「字节数不符」，同一个病三种报错——排障时看不出是被闸拦下的。
            raise _fetch_budget_err(label, budget, len(raw), size)
        if not raw:
            raise RuntimeError(f"{label or path} 下载失败（零字节）")
        if size and len(raw) != size:
            raise RuntimeError(f"{label} 字节数不符：{len(raw)} ≠ 声明 {size}")
        if expect_digest and sha256_hex(raw) != expect_digest:
            raise RuntimeError(f"{label} sha256 不符（{sha256_hex(raw)[:20]}… ≠ "
                               f"{expect_digest[:20]}…）：分块/续传拼接损坏，拒用")
        return raw, {}

    def request(self, method, path, repo, body: bytes, headers=None,
                actions=("pull", "push"), status_ok=(200, 201, 202, 204),
                attempts=6):
        # GitHub runner → 国内 ACR 的跨境链路写超时是常态（v1.1.5 三连 run 实证：
        # 块写超时、upload session 被掐、以及 run 35678680808 的 layer4 推到 97%
        # 后 token 过期→新 session POST 全 401）。纪律：①连接级异常(OSError)指数
        # 退避重试；②**每次重试都 fresh=True 重取 token**（ACR 临时 token 有效期
        # 短，慢链路单 blob 十分钟级上传中途必过期）；③401 也走重试；④其余 HTTP
        # 状态错不重试（语义错误重发不会变好）。PATCH 带 Content-Range=幂等放置。
        h = dict(headers or {})
        last = None
        for i in range(attempts):
            if _deadline_hit():             # 链时钟：推送腿到点即退（见 CHAIN_DEADLINE_S）
                raise _deadline_err("推送")
            c = self._conn()
            try:
                t = self.token(repo, list(actions), fresh=(i > 0))
                h["Authorization"] = "Bearer " + t
                c.request(method, path, body=body, headers=h)
                r = c.getresponse()
                data = r.read()
                loc = r.getheader("Location")
                if r.status == 401 and i + 1 < attempts:
                    log(f"  ⚿ {method} 401（token 过期？），重取 token 重试")
                    time.sleep(2)
                    continue
                if r.status not in status_ok:
                    raise RuntimeError(
                        f"{method} {path} → {r.status} {data[:200].decode('utf-8','replace')}")
                return r.status, loc, data
            except OSError as e:            # TimeoutError/SSLError/连接重置都属此类
                last = e
                log(f"  ↻ {method} {path.split('?')[0]} 网络异常({type(e).__name__})，"
                    f"第{i + 1}/{attempts}次重试")
                if i + 1 < attempts:
                    time.sleep(5 * (2 ** i))
            finally:
                c.close()
        raise RuntimeError(f"{method} {path} 重试 {attempts} 次仍失败：{last}")


# ────────────────────────── blob 上传（分块 PATCH 流，ACR 已实证） ──────────────────────────

def push_blob(dst: Registry, repo: str, digest: str, data: bytes, label: str):
    # 秒传：HEAD 命中直接跳（双 tag 复用同层时省 130MB 上传）。
    # 第四轮审计 P3：旧实现用 GET 探测——http_get 会 resp.read() 把整层（最大
    # 76MB）读回内存只为确认存在，且不经墙钟预算。改真 HEAD（零响应体）。
    if dst.head(f"/v2/{repo}/blobs/{digest}", repo):
        log(f"  blob 已存在（秒传）: {label}")
        return
    # blob 级重启（run 35676700243 当时的判断）：跨境链路掐 session 后复用旧
    # Location 秒败 EOF，于是整块失败即重 POST 开新 session 从 0 重传，最多 3 轮。
    # v1.1.17 审计更正：该前提**未被后续证据支持**——两版发布的日志里"第N/3轮
    # 整体失败"触发 0 次，而 run 35681170094 里同一 session/同一 Location 在三次
    # SSL 异常后照样续传成功（真正救场的是 request() 的 fresh-token 重试）。保留
    # 这一层是兜底（换 session 至少不会更糟），但**不要再把它当根因**。
    for round_no in range(1, 4):
        if _deadline_hit():                 # 轮起点复检：别让 3 轮把预算撑爆
            raise _deadline_err("推送")
        try:
            if _push_blob_once(dst, repo, digest, data, label):
                return
        except (RuntimeError, OSError) as e:
            log(f"  ⟲ {label} 第{round_no}/3轮整体失败({type(e).__name__}: "
                f"{str(e)[:120]})，换新 session 重传")
            if round_no == 3:
                raise
            time.sleep(10 * round_no)


def _push_blob_once(dst: Registry, repo: str, digest: str, data: bytes, label: str) -> bool:
    _, loc, _ = dst.request("POST", f"/v2/{repo}/blobs/uploads/", repo, b"",
                            headers={"Content-Length": "0"})
    if not loc:
        raise RuntimeError("ACR 未返回 upload Location")
    offset = 0
    use = loc
    while True:
        part = data[offset:offset + CHUNK]
        if not part and offset:
            break
        if part:
            sep = "&" if "?" in use else "?"
            _, nloc, _ = dst.request(
                "PATCH", use + sep + "stage=1", repo, part,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Range": f"{offset}-{offset + len(part) - 1}",
                         "Content-Length": str(len(part))},
                status_ok=(202, 200))
            offset += len(part)
            use = nloc or use
            log(f"  ↑ {label}: {offset}/{len(data)}B")
        if offset >= len(data):
            break
    sep = "&" if "?" in use else "?"
    dst.request("PUT", use + sep + f"digest={digest}", repo, b"",
                headers={"Content-Length": "0"})
    log(f"  blob 完成: {label} ({len(data)}B)")
    return True


def push_manifest(dst: Registry, repo: str, ref: str, manifest: bytes, media_type: str):
    dst.request("PUT", f"/v2/{repo}/manifests/{ref}", repo, manifest,
                headers={"Content-Type": media_type, "Content-Length": str(len(manifest))})
    log(f"  manifest 已推: {repo}@{ref[:24]}")
    return sha256_hex(manifest)


# ────────────────────────── transcode ──────────────────────────

def cmd_transcode(a):
    src = Registry(a.src)
    dst_user = _cred("ACR_USER", "~/.acr_user")
    dst_pass = _cred("ACR_PASS", "~/.acr_pass")
    if not dst_user or not dst_pass:
        sys.exit("ACR 凭据缺失：env ACR_USER/ACR_PASS（CI 用）或家目录 ~/.acr_user / ~/.acr_pass（本地用）")
    dst = Registry(a.dst, dst_user, dst_pass)

    body, _ = src.get(f"/v2/{a.src_repo}/manifests/{a.src_ref}", a.src_repo,
                      headers={"Accept": f"{MEDIA_MANIFEST},{MEDIA_CONFIG}"})
    man = json.loads(body)
    assert man.get("schemaVersion") == 2 and "layers" in man, "源不是 OCI image manifest（勿指 index digest）"
    cfg_dgst = man["config"]["digest"]
    # 链时钟在**第一个 blob 之前**起算，覆盖整条链（config 下载 + 逐层下载 + 逐层推送）：
    # 旧形 `push_deadline_reset()` 点在层循环前 ⇒ config 那次下载落在时钟外，而且这个
    # 名字把它谎报成"推送阶段"预算——下载腿根本不查它（复验 §1.2 的 5h 与 §2.5 的
    # 归因指错腿，都是这一处起的头）。
    chain_deadline_reset()
    try:
        cfg_bytes, _ = src.get_blob(f"/v2/{a.src_repo}/blobs/{cfg_dgst}", a.src_repo,
                                    label="config", size=man["config"].get("size") or 0,
                                    expect_digest=cfg_dgst)
        cfg = json.loads(cfg_bytes)
        diff_ids = cfg["rootfs"]["diff_ids"]
        assert len(diff_ids) == len(man["layers"]), "diff_ids 与层数不符"

        log(f"源 {a.src_repo}@{a.src_ref[:19]}: {len(man['layers'])} 层，"
            f"config {len(cfg_bytes)}B")
        new_layers = []
        for i, (layer, want) in enumerate(zip(man["layers"], diff_ids)):
            raw, _ = src.get_blob(f"/v2/{a.src_repo}/blobs/{layer['digest']}",
                                  a.src_repo, label=f"layer{i}",
                                  size=layer.get("size") or 0,
                                  expect_digest=layer["digest"])
            mt = layer.get("mediaType", "")
            if mt.endswith("+zstd") or raw[:4] == ZSTD_MAGIC:
                plain = zstd_decompress(raw)
                data = gzip.compress(plain, mtime=0)   # 确定性重压缩
                got = sha256_hex(plain)
                if got != want:
                    sys.exit(f"层{i} 解压 sha256={got[:20]}… ≠ config.diff_ids {want[:20]}…（层损坏/非标准帧）")
                media = LAYER_GZIP
            elif mt.endswith("+gzip") or raw[:2] == GZIP_MAGIC:
                data, media = raw, layer.get("mediaType") or "application/vnd.oci.image.layer.v1.tar+gzip"
            else:
                sys.exit(f"层{i} 未知压缩形态 mediaType={mt!r}，拒绝盲转")
            ann = layer.get("annotations")
            nl = {"mediaType": media, "digest": sha256_hex(data), "size": len(data)}
            if ann:
                nl["annotations"] = ann
            new_layers.append(nl)
            push_blob(dst, a.dst_repo, nl["digest"], data, f"layer{i} {len(raw)//2**20}MB→{len(data)//2**20}MB")
            del raw, data
        # 链尾也在闸内（复验 ②b：旧形 `chain_deadline_clear()` 在层循环的 finally，
        # config push 与 manifest PUT 全程无预算＝"闸外写"）。宁可不发布，也不要
        # 在预算已尽时被 CI 掐成"半条 manifest 挂在 ACR 上"——blob 有秒传，
        # 重试一次即可补齐，而 index 坏了客户刷完就是装不上。
        push_blob(dst, a.dst_repo, cfg_dgst, cfg_bytes, "config")
        nm = {"schemaVersion": 2, "mediaType": MEDIA_MANIFEST,
              "config": dict(man["config"]), "layers": new_layers}
        if "annotations" in man:
            nm["annotations"] = man["annotations"]
        digest = push_manifest(dst, a.dst_repo, a.dst_tag,
                               json.dumps(nm, separators=(",", ":")).encode(), MEDIA_MANIFEST)
    finally:
        chain_deadline_clear()
    # stdout 最后一行=纯 digest，与 log() 的 "[acr-transcode]" 人类前缀彻底分离
    # （CI 用 $(python3 …|tail -1) 捕获——本地手跑 tail 混前缀侥幸未炸，
    # CI 实发把前缀行当 digest 用，拼 URL 控制字符崩。单通道纪律见双函数注释）
    emit(digest)
    log(f"✅ 转码完成 {a.dst}/{a.dst_repo}:{a.dst_tag} → {digest}")


def cmd_index(a):
    dst = Registry(a.dst, _cred("ACR_USER", "~/.acr_user"), _cred("ACR_PASS", "~/.acr_pass"))
    members = []
    for m in a.member:
        arch, dg = m.split("=", 1)
        plat = {"amd64": "linux/amd64", "arm64": "linux/arm64"}[arch]
        os_, arch_ = plat.split("/")
        body, hdr = dst.get(f"/v2/{a.dst_repo}/manifests/{dg}", a.dst_repo,
                            headers={"Accept": MEDIA_MANIFEST})
        members.append({"mediaType": MEDIA_MANIFEST, "digest": dg,
                        "size": int(hdr.get("Content-Length", len(body))),
                        "platform": {"architecture": arch_, "os": os_}})
    idx = json.dumps({"schemaVersion": 2, "mediaType": MEDIA_INDEX,
                      "manifests": members}, separators=(",", ":")).encode()
    digest = push_manifest(dst, a.dst_repo, a.tag, idx, MEDIA_INDEX)
    emit(digest)
    log(f"✅ index 已合成 {a.dst}/{a.dst_repo}:{a.tag} → {digest}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcode")
    t.add_argument("--src", required=True); t.add_argument("--src-repo", required=True)
    t.add_argument("--src-ref", required=True)
    t.add_argument("--dst", required=True); t.add_argument("--dst-repo", required=True)
    t.add_argument("--dst-tag", required=True)
    t.set_defaults(fn=cmd_transcode)
    x = sub.add_parser("index")
    x.add_argument("--dst", required=True); x.add_argument("--dst-repo", required=True)
    x.add_argument("--tag", required=True); x.add_argument("--member", action="append", required=True)
    x.set_defaults(fn=cmd_index)
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a.fn(a)


if __name__ == "__main__":
    main()
