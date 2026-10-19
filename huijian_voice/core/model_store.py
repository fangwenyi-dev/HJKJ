"""模型仓库：models.lock.json 驱动的首启下载 / 校验 / 手动导入 / 状态外泄。

纪律来源（网关工程模板 §偏离点4 + v3 定案不变）：
- 大模型不进镜像；解包落 /data/models/（Supervisor 持久卷，升级不丢、卸载即清）；
- 下载三级 URL 回退（gh-proxy 国内→官方→hf-mirror），sha256 校验；
- tmp + os.replace 原子落盘；进度写事实文件 models_status.json（nginx try_files 暴露）；
- model_auto_download=false 或全失败 → 手动导入口 /data/models/import/ 放 tarball 即用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import const

logger = logging.getLogger("huijian.models")

# lock 文件带空格的键名笔误兜底：真实 required_files 以 core 侧硬校验为准
# 下载字节闸（第四轮审计 P2 ⑥）：按声明体积上浮 1.5x 即拒；lock 缺声明时走
# 绝对兜底（不设地板——小包声明 1MB 就该按 1.5MB 卡）。
_DL_ABS_MAX_BYTES = 3 << 30       # 3GB
# v1.6.6 刀Y：启动复核摘坏附属前的"下载源够不够得到"探活上限（TCP 层，一次启动
# 只探一轮）。设短是硬约束——它跑在 ctor 同步路径上，探活本身不得变成启动瓶颈。
_NET_PROBE_TIMEOUT_S = 1.0
# v1.1.39（审查 §3[P2]）：**总时限**。`urlopen(timeout=60)` 只管单次 socket 读、
# `_cap` 只管字节 ⇒ 滴流源（每 60s 内吐一点、永不 EOF）能让 `while True` 永不结束，
# 后续 url 一个都不试；而本函数跑在 `ensure()` 的 `self._key_lock(key)` 里，
# `ensure_async` 见线程 `is_alive()` 就静默 return ⇒ **该模型永远装不上**，
# 只有进程退出能打断。`firmware_store.download` 对同一风险早就显式加了
# deadline（:443-449），两处不对称即缺口。
_DL_WALL_TIMEOUT_S = 1800          # 单源 30 分钟（GB 级模型档）


def _src_host(url: str) -> str:
    """日志/状态里显示的源主机名。**永不抛**。

    v1.1.40（发版前对抗复核 §E3）：旧形两处 `url.split('/')[2]`——`urls` 来自
    `models.lock.json`（人来写，漏写 scheme 完全可能），无 "//" 时 IndexError：
    一处把 `_set_status` 的 detail 打崩、一处更坏——它写在
    `raise TimeoutError(f"…{url.split('/')[2]}…")` 的 f-string 里，异常类型当场
    从 TimeoutError 变 IndexError，"超总时限→换源"这条判据连它的日志一起失效。
    """
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or url
    except Exception:        # noqa: BLE001 —— 装饰性提取，绝不改下载控制流
        return url


_STATUS_LOCK = threading.Lock()


class ModelStore:
    def __init__(self, settings, lock_path: Path = const.MODELS_LOCK_FILE,
                 models_dir: Path = const.MODELS_DIR, status_file: Path = const.MODELS_STATUS_FILE):
        self.settings = settings
        self.lock_path = Path(lock_path)
        self.models_dir = Path(models_dir)
        self.import_dir = self.models_dir / "import"
        self.status_file = Path(status_file)
        self._manifest = self._load_manifest()
        self._status: dict[str, dict] = {k: {"state": "pending", "pct": 0, "detail": ""} for k in self._manifest}
        self._threads: dict[str, threading.Thread] = {}
        # F2 single-flight：同 key 的 ensure 全局只许一个执行体（下载线程与
        # 识别路径惰性加载线程互斥）；F4：abort 位让停机即时打断下载循环。
        self._key_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.abort = threading.Event()
        # M12：构造即清扫上次运行（含 SIGKILL/断电）遗留的 .part.* 孤儿。
        self.sweep_orphans()
        # v1.6.3（刀1）：构造即复核附属字节（matcha vocos 案——文件名对字节错可
        # 在 is_ready 的存在性闸下永久隐身，女声被降八度播成"男声"）。只动
        # "已就绪"的 key（未就绪本就无哈希成本），坏字节摘除后交给既有
        # pend→ensure 链自动重取。异常绝不阻断构造（就绪闸与状态循环优先）。
        try:
            flagged = self.verify_all_extras()
            if flagged:
                logger.warning("[模型] 启动附属复核点名 %s（坏字节已摘除，走重取）",
                               "、".join(flagged))
        except Exception:  # noqa: BLE001
            logger.exception("[模型] 启动附属复核异常（跳过，不影响构造）")


    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._key_locks.setdefault(key, threading.Lock())

    # ── 清单 ────────────────────────────────────────────────────
    def _load_manifest(self) -> dict:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            out = {k: v for k, v in data.items() if isinstance(v, dict) and "tarball" in v}
            for k, v in out.items():
                v.setdefault("_key", k)   # 状态回写用的稳定键（不依赖调用方传参）
            return out
        except Exception as e:
            logger.error("[模型] models.lock.json 读取失败: %s", e)
            return {}

    def keys(self):
        return list(self._manifest.keys())

    def lock_entry(self, key: str) -> dict:
        """manifest 条目只读浅拷贝（v1.0.61 审查批 P3-a：TTS 指纹等需要
        **包身份**的调用方使用——换包不换 sid 时旧盘缓存永不轮换）。缺 key → {}。"""
        return dict(self._manifest.get(key) or {})

    def voices_count_for(self, key: str) -> int:
        """TTS 包官方音色数（lock 的 voices_count 字段；缺省 0=不启用自定义注入）。
        单音尺寸由它推导：官方 voices.bin 字节数 ÷ 音色数（纯张量拼接、无 magic，
        v1_1 fp32 本机实测 53,790,720÷103=522,240B 整除）。"""
        try:
            return int((self._manifest.get(key) or {}).get("voices_count") or 0)
        except (TypeError, ValueError):
            return 0

    def model_dir_for(self, key: str) -> Optional[Path]:
        """返回已就绪模型的解包目录（含 top_dir 探测）。未就绪返回 None。"""
        entry = self._manifest.get(key)
        if not entry:
            return None
        # 完成标记=就绪唯一凭证：extractall 逐件落盘，大文件半写时 exists() 会
        # 误报就绪（v1.0.0 CI e2e 实锤：STT 读到半写 decoder.int8.onnx →
        # "Protobuf parsing failed"，run 间 flaky）。标记在完整解包后才写 →
        # 就绪判定升级为内容级原子。
        if not (self.models_dir / key / ".extracted_ok").is_file():
            return None
        # M11（2026-09-23 深审）：完成章此前写 tar_path.name 却全码零读取——
        # 就绪判定与 lock 当前包身份（tarball+sha256）脱钩，models.lock.json:6
        # 自写处置预案「官方重传同版本包→人工回填 sha256」在存量设备上永不
        # 生效（章不校、永不复下，fresh 与存量跑不同字节）。现在读章比对：
        # 不符=不就绪（触发重下/force），旧格式章（仅包名）宽容为按名比对。
        stamp = self._read_stamp(key)
        if stamp is not None and stamp != "dev-migration":
            exp_tarball = entry.get("tarball", "")
            exp_sha = entry.get("sha256", "")
            name_part, _, sha_part = stamp.partition("|")
            if name_part != exp_tarball or (sha_part and sha_part != exp_sha):
                logger.warning("[模型] %s 完成章与 lock 包身份不符（章=%r），判不就绪",
                               key, stamp)
                return None
        top = entry.get("top_dir", "")
        try:
            cand = self.models_dir / key / top if top else self.models_dir / key
            if cand.is_dir() and self._files_ok(cand, entry.get("required_files", [])):
                return cand
            # 兼容直接解压在 models_dir/<key>/ 无 top_dir 层
            cand2 = self.models_dir / key
            if cand2.is_dir() and self._files_ok(cand2, entry.get("required_files", [])):
                return cand2
        except OSError as e:
            # 文件系统级异常（坏符号链接/reparse/权限）绝不上传——否则 is_ready
            # 会把 /api/health 打成 500、状态循环每 5s 抛栈（Windows 实证 WinError 1920）。
            logger.debug("[模型] %s 目录探测异常: %s", key, e)
        return None

    @staticmethod
    def _files_ok(base: Path, required: list) -> bool:
        for rf in required:
            if not (base / rf).exists():
                return False
        return True

    @staticmethod
    def _tarball_required(entry: dict) -> list:
        """主包自带应检文件 = required_files 剔除 extra_files 附属。

        附属（如 matcha 的 vocos 声码器）由二跳补下，**不参与**解包后即刻校验——
        否则解包闸因附属缺失恒 False、补下逻辑永不被调用（鸡生蛋，2026-09-24
        matcha「解包后校验文件缺失」永久 incomplete 实锤）。"""
        extra = {ef.get("file") for ef in (entry.get("extra_files") or [])
                 if ef.get("file")}
        return [rf for rf in entry.get("required_files", []) if rf not in extra]

    def _main_body_ok(self, key: str, entry: dict) -> bool:
        """主包本体（required_files 剔除附属后的部分）在场且完成章与 lock 身份
        一致。v1.6.3 刀1 收口：is_ready=False 但主包体在 ⇒ 缺的只可能是附属，
        ensure 该直走附属二跳，而不是被主包下载/解包分支短路（离线+import/ 只
        放附属救急包＝用户按启动复核提示自救的形制，旧码在 not-auto 闸直接
        return False，逃生门成摆设；在线态也会白白重下整只主包）。"""
        if not (self.models_dir / key / ".extracted_ok").is_file():
            return False
        stamp = self._read_stamp(key)
        if stamp is not None and stamp != "dev-migration":
            exp_tarball = entry.get("tarball", "")
            exp_sha = entry.get("sha256", "")
            name_part, _, sha_part = stamp.partition("|")
            if name_part != exp_tarball or (sha_part and sha_part != exp_sha):
                return False          # 章与 lock 身份不符＝整包须重取
        top = entry.get("top_dir", "")
        try:
            for cand in (p for p in ((self.models_dir / key / top if top else None),
                                     self.models_dir / key) if p):
                if cand.is_dir() and self._files_ok(
                        cand, self._tarball_required(entry)):
                    return True
        except OSError:
            return False
        return False

    def _all_extras_imported(self, entry: dict) -> bool:
        """该 key 的每个附属在 import/ 都有同名救急包（v1.6.3 刀1 收口的
        离线支路闸——auto_download 关时只放得下救急包才准走附属二跳，
        免得借道触发二跳尾部的 urls 补下＝绕过出门下载的门）。"""
        extras = entry.get("extra_files") or []
        if not extras:
            return False
        try:
            return all((self.import_dir / (ef.get("file") or "\x00")).exists()
                       for ef in extras)
        except OSError:
            return False

    def _read_stamp(self, key: str) -> Optional[str]:
        try:
            raw = (self.models_dir / key / ".extracted_ok").read_text(
                encoding="utf-8").strip()
            return raw or None
        except OSError:
            return None

    def is_ready(self, key: str) -> bool:
        return self.model_dir_for(key) is not None

    # ── v1.6.3（刀1·附属启动复核）──────────────────────────────────
    # 病灶（现网 Matcha"男声"事故根因链，2026-10-17 频谱+时域双实锤）：附属文件
    # （matcha 的 vocos 声码器）的 sha256 校验只活在 _ensure_extra_files 里，而它
    # 只在 ensure() 真走到时才被调——stamp 在 ⇒ _ensure_locked 首行 is_ready 即真
    # 直接 return。**文件名正确、字节陈旧的 vocos（早期手动投放/换包窗口遗留）
    # 可永久隐身**：引擎加载照常"成功"，吐出的音频却是另一份声码器的（实测 32k
    # 产物被按 16k 标称＝整整低一个八度，女声 Matcha 变"男声"）。
    # 修法＝启动期显式复核一次（verify_extras），坏字节**摘除文件**——此后
    # is_ready 因缺失转 False，main 既有 pend→ensure 链自动走 import/→多源重取，
    # 全链零新状态机。纪律：绝不塞进 is_ready（5s 状态循环+healthz+每次加载判
    # 都吃它，54MB 哈希会烧穿数据盘——tests/test_v163 热路径钉把这条钉死）。
    def _auto_download_on(self) -> bool:
        """自动下载门的单一事实源（v1.6.3 正向臂抓出的第二洞）：settings 三态
        （None=未设→True）AND env 门 `HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD`。
        判"敢不敢摘/走不走下载"必须与 `_ensure_locked` 的 auto 算式逐字同源——
        分头读会漂移：旧 `_refetch_possible` 只读 settings 漏了 env 门，env 关 +
        settings 未落盘的现网形制下会把唯一副本摘了却无人重取（违 M11）。"""
        auto = self.settings.get("power.auto_download", None)
        if auto is None:
            auto = True
        return bool(auto) and str(os.environ.get(
            "HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD", "true")).lower() != "false"

    def _refetch_possible(self, ef: dict, name: str) -> bool:
        """坏附属敢不敢摘：只有**确有重取来源**才摘（M11 家规——判据动作不得把
        现场弄得更糟：离线+关自动下载时摘掉唯一副本＝从"音高不对"恶化成"彻底哑"）。"""
        try:
            if (self.import_dir / name).exists():
                return True
            # urls 只有配了才够——auto_download 关时下载链被门（ensure 走 manual
            # 支），摘了也没人取回来；import/ 是唯一被允许绕过该门的通道。
            return self._auto_download_on() and bool(ef.get("urls"))
        except Exception:  # noqa: BLE001
            return False

    def _probe_host(self, host: str, port: int) -> bool:
        import socket
        try:
            with socket.create_connection((host, port), timeout=_NET_PROBE_TIMEOUT_S):
                return True
        except OSError:
            return False

    def _net_reachable(self, ef: dict) -> bool:
        """摘在场唯一副本前的「真取得到」探活（v1.6.6 刀Y）。

        `_refetch_possible` 答的是**配置**上有没有来源；配置有源 ≠ 现场够得到：
        自动下载开着而此刻恰好断网/无 DNS 时，旧形照样摘掉在场唯一副本，随后
        重取全失败 ⇒ 从"音高不对"恶化成"彻底哑"，正是 M11 家规禁止的那种自毁
        （10-19 复核定格的在册洞）。够不到就保留 + 如实点名，让用户自己决定切档
        或放 import/ 救急包，而不是替他砸掉唯一逃生通道。

        一次启动只探一轮（结果缓存在实例上，附属复核只在 ctor 跑）；只到 TCP 层
        为止——要分辨的是"这台机器现在出不出得去"，不是证书与内容。任何异常＝
        不可达（保守＝不摘）。
        """
        cached = getattr(self, "_net_ok", None)
        if cached is not None:
            return bool(cached)
        import urllib.parse
        ok = False
        for url in (ef.get("urls") or []):
            try:
                u = urllib.parse.urlsplit(str(url))
                host = u.hostname
                if not host:
                    continue
                if self._probe_host(host, u.port or (443 if u.scheme == "https" else 80)):
                    ok = True
                    break
            except Exception:  # noqa: BLE001 判据件不得成为新故障面
                continue
        self._net_ok = ok
        return ok

    def verify_extras(self, key: str) -> bool:
        """附属文件逐一复核（尺寸快判→sha256）。

        坏字节：有重取来源 ⇒ 摘除（此后 is_ready 因缺失转 False，main 既有
        pend→ensure 链自动走 import/→多源重取）；无重取来源 ⇒ **保留文件只点名**
        （坏副本可能是在场唯一副本，离线现场删它＝自毁唯一逃生通道）。
        缺失 ⇒ False 点名（就绪闸本就兜住）。返回 True＝无附属或全部字节一致。
        永不抛：读盘异常一律按"不可信"处理并走同一分支。"""
        entry = self._manifest.get(key) or {}
        extras = entry.get("extra_files") or []
        if not extras:
            return True
        base = self._resolved_dir(key, entry)
        ok = True
        for ef in extras:
            name = ef.get("file") or ""
            if not name:
                continue
            dest = base / name
            if not dest.exists():
                ok = False            # 缺失由 _files_ok 兜住不就绪，这里只点名
                logger.warning("[模型] %s 附属 %s 缺失（启动复核点名）", key, name)
                continue
            expected = str(ef.get("sha256") or "")
            good = True
            if not expected:
                good = True           # 无登记哈希＝无从判（纪律同 _ensure_extra_files）
            else:
                size_mb = ef.get("size_mb") or 0
                try:
                    # 单位口径＝十进制 MB（×1e6），与 _download_any 字节上限同源
                    # （正向臂实锤：vocos 登记 54、真身 53,882,848B——按 MiB 折
                    # 56,623,104 差 2.7MB>容差，**好字节会被快判误杀**：在线每次
                    # 重启重下 54MB，离线直接哑。sha 才是权威，快判只做李鬼截断）。
                    if size_mb and abs(dest.stat().st_size
                                        - int(float(size_mb) * 1e6)) > 1024 * 1024:
                        good = False  # 尺寸快判（±1MB 容差），免读 54MB 秒断李鬼
                    elif not self._sha_ok(dest, expected):
                        good = False
                except (OSError, ValueError):
                    good = False
            if good:
                continue
            ok = False
            try:
                local_rescue = (self.import_dir / name).exists()
            except OSError:
                local_rescue = False
            if self._refetch_possible(ef, name) and (local_rescue or self._net_reachable(ef)):
                try:
                    dest.unlink(missing_ok=True)
                    logger.warning("[模型] %s 附属 %s 字节与登记不符 → 已摘除，"
                                   "将走导入/多源重取（启动复核，v1.6.3）", key, name)
                except OSError as e:
                    logger.warning("[模型] %s 附属 %s 判坏但摘除失败: %s（下次启动重试）",
                                   key, name, e)
            elif local_rescue:
                logger.warning("[模型] %s 附属 %s 判坏但下载门未开——import/ 有救急包，"
                               "请放置后重启（v1.6.3 二跳）", key, name)
            else:
                logger.warning("[模型] %s 附属 %s 字节与登记不符，但当场取不到替代件"
                               "（无 import/ 救急包，且到下载源的连接不通）——"
                               "保留在场唯一副本仅点名；恢复网络后重启即自愈，"
                               "或放 import/%s 后重启（v1.6.6 刀Y 前此形会被摘成彻底哑）",
                               key, name, name)
        return ok


    def verify_all_extras(self) -> list[str]:
        """启动钩子：逐 key 复核，返回被点名的 key 列表（main 日志/观测用）。"""
        flagged = [k for k in self._manifest
                   if (self._manifest[k].get("extra_files") and self.is_ready(k))
                   and not self.verify_extras(k)]
        return flagged

    def extras_stamp(self, key: str) -> str:
        """附属文件的**身份串**（v1.6.6 刀11）：`文件名:size:mtime` 逐条拼接。

        只 stat、不读内容——本仓有钉：附属哈希绝不进热路径（5s 状态循环/healthz/
        播报指纹都是高频面，54MB vocos 逐轮哈希会烧穿数据盘）。取 mtime 而非 sha
        的代价＝"同尺寸同分钟的原地替换"分辨不出；换文件（摘除→重取→新文件）必然
        换 mtime，正是播报缓存键要的那件事。永不抛：任何异常回 ""（调用方按无附属处理）。
        """
        try:
            entry = self._manifest.get(key) or {}
            bits = []
            for ef in (entry.get("extra_files") or []):
                name = ef.get("file") or ""
                if not name:
                    continue
                try:
                    dest = self._resolved_dir(key, entry) / name
                    st = dest.stat()
                    bits.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
                except Exception:  # noqa: BLE001 文件不在/权限：记名不记时
                    bits.append(f"{name}:-")
            return "|".join(bits)
        except Exception:  # noqa: BLE001
            return ""

    def extras_diag(self, key: str) -> list[dict]:
        """附属文件诊断（v1.6.4，远程定案面用；由嗓音自测低频触发，非热路径）：
        逐一回 {file, expected, actual, size, match}。actual 只回 sha 前 12 位
        （够判别不泄全串）。缺失→actual=None。永不抛：读盘异常 actual="err"。
        这把「设备那份字节是否＝登记值」的问号，从只有容器内能看，变成一次
        /healthz GET 就能定案——正是现网拿不到日志（hassio 口对 REST 令牌 401）
        时缺的那把尺。"""
        entry = self._manifest.get(key) or {}
        out = []
        for ef in (entry.get("extra_files") or []):
            name = ef.get("file") or ""
            if not name:
                continue
            expected = str(ef.get("sha256") or "")
            rec = {"file": name, "expected": expected[:12] if expected else "",
                   "actual": None, "size": None, "match": False}
            try:
                dest = self._resolved_dir(key, entry) / name
                if dest.exists():
                    rec["size"] = dest.stat().st_size
                    if expected:
                        import hashlib
                        h = hashlib.sha256()
                        with open(dest, "rb") as f:
                            for blk in iter(lambda: f.read(1 << 20), b""):
                                h.update(blk)
                        digest = h.hexdigest()
                        rec["actual"] = digest[:12]
                        rec["match"] = digest == expected
                    else:
                        rec["actual"] = "no-expected"
                        rec["match"] = True      # 无登记哈希＝无从判，视同放行不制造假红
            except Exception:  # noqa: BLE001 —— 诊断件永不抛
                rec["actual"] = "err"
            out.append(rec)
        return out

    # ── 状态外泄 ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with _STATUS_LOCK:
            out = {k: dict(v) for k, v in self._status.items()}
        for k in out:
            out[k]["ready"] = self.is_ready(k)
        return out

    def _set_status(self, key: str, **fields):
        with _STATUS_LOCK:
            self._status.setdefault(key, {})
            self._status[key].update(fields)
            snap = {k: dict(v) for k, v in self._status.items()}
        self._write_status(snap)

    def _write_status(self, snap: dict):
        # v1.0.41 审查 S17-2：旧实现中途异常会留孤儿——fchmod/fdopen 抛错时 fd 泄漏
        # （慢性耗尽 select fd 表），写/换名失败时 .mst-*.tmp 永久堆积在数据盘。
        # 统一 finally 兜底：fd 未移交必关、tmp 未转正必删。
        fd, tmp = None, None
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            # F6：tmp 名必须唯一——与主循环 _atomic_write 共用固定 .tmp 名时，
            # 半写文件可被对方 rename 转正（双写者竞态）。
            fd, tmp = tempfile.mkstemp(dir=str(self.status_file.parent),
                                       prefix=".mst-", suffix=".tmp")
            # mkstemp 默认 0600：rename 转正后 nginx worker 读走 13（v1.0.0 实机
            # 实发：开下载起 models_status.json 被本写者反复刷回 600，与主循环
            # _atomic_write 的 fchmod 同源教训——事实文件必须世界可读）。
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None          # 所有权移交 fdopen，finally 不再关
                f.write(json.dumps({"updated": time.time(), "models": snap}, ensure_ascii=False, indent=2))
            os.replace(tmp, self.status_file)
            tmp = None             # 已转正，不再是孤儿
        except Exception as e:  # 状态文件写失败不该阻断下载
            logger.debug("[模型] 状态文件写入失败: %s", e)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── 下载/解包 ───────────────────────────────────────────────
    def sweep_orphans(self) -> int:
        """M12（2026-09-23 深审）：启动无条件回收下载残留 `.part.*`。
        旧清理只在 except 分支，而本仓把 SIGKILL 写成收尾常态（shutdown
        wait_for 5s < 下载块 60s）——半截 GB 级文件在 /data 永久堆积，
        与 M11 复合最坏态直推盘满。返回删除数。"""
        removed = 0
        try:
            # v1.1.39（审查 §3[P3]）：旧形只扫**顶层** `glob("*.part.*")`，而
            # `extra_files` 的分片写在嵌套层（`models_dir/<key>/<top_dir>/<name>.part.<uuid>`，
            # 见 _ensure_extra_files）⇒ 那些残块永远清不掉。SIGKILL 是本仓自述的收尾常态
            # （异常路径的清理根本来不及走），GB 级残块在 /data 永久堆积。改递归扫。
            for stale in self.models_dir.rglob("*.part.*"):
                try:
                    if stale.is_file():
                        stale.unlink()
                        removed += 1
                except OSError as e:
                    logger.warning("[模型] 孤儿残留删除失败 %s: %s", stale, e)
        except OSError as e:
            logger.debug("[模型] 孤儿清扫异常（不阻断）: %s", e)
        if removed:
            logger.warning("[模型] 启动清掉 %d 个下载残留 .part.*", removed)
        return removed

    def _invalidate(self, key: str) -> None:
        """force 逃生门：旧解包树+完成章整删（不清则新解包与旧文件混栈，
        _files_ok 依旧放行=「彻底 no-op」根因之二）。import/ 是用户手动放
        包口，绝不触碰。"""
        import shutil
        target = self.models_dir / key
        try:
            if target.is_dir():
                shutil.rmtree(target)
        except OSError as e:
            logger.warning("[模型] %s 旧树删除失败: %s", key, e)

    def ensure(self, key: str, force: bool = False) -> bool:
        """幂等确保模型就绪。同步执行（调用方放线程）。F2：per-key 锁 single-flight。
        force=True（M11 接线）：面板「重新下载」——清旧树旧章后重走校验下载。"""
        entry = self._manifest.get(key)
        if not entry:
            return False
        with self._key_lock(key):
            return self._ensure_locked(key, entry, force)

    def _ensure_locked(self, key: str, entry: dict, force: bool) -> bool:
        if not force and self.is_ready(key):
            self._set_status(key, state="ready", pct=100, detail="已就绪")
            return True
        self._set_status(key, state="checking", pct=0, detail="检查导入/下载")
        # 1) 导入口
        imported = self.import_dir / entry["tarball"]
        auto = self._auto_download_on()
        if force:
            # M11：先删旧树再重取（旧树不清，新解包与旧文件混栈、_files_ok
            # 照过=「下载」按钮对就绪错版包彻底 no-op 的第二根因）。但清树
            # 只在**确有重取来源**时执行——force 不是摧毁现有就绪态的许可证。
            if imported.exists() or auto:
                self._invalidate(key)
            else:
                self._set_status(key, state="manual", pct=0,
                                 detail="自动下载关闭且无导入包，拒绝清空重取")
                return self.is_ready(key)
        if imported.exists():
            if self._extract(key, imported, entry):
                return self._ensure_extra_files(key, entry)
        # 1.5) v1.6.3 刀1 收口：主包体完好、缺的只可能是附属（启动复核摘除
        # 坏字节后的形制）⇒ 直走附属二跳，不被主包下载分支短路。旧码在
        # 离线+import/ 只放附属救急包时于 not-auto 闸直接 return False＝用户
        # 按复核提示自救的逃生门成摆设。守门：auto_download 关时只有
        # **每个附属都有 import/ 救急包**才放行——二跳尾部的 urls 补下不吃
        # 该门（既有解包链行为），此支不得给它开后门。
        if not imported.exists() and self._main_body_ok(key, entry) \
                and (auto or self._all_extras_imported(entry)):
            return self._ensure_extra_files(key, entry)
        # 2) 三级 URL 下载
        if not auto:
            self._set_status(key, state="manual", pct=0, detail="自动下载关闭；放包到 import/ 或开开关")
            return self.is_ready(key)
        dest_tar = self.models_dir / entry["tarball"]
        dest_tar.parent.mkdir(parents=True, exist_ok=True)
        ok = self._download_any(entry, dest_tar)
        if ok and self._extract(key, dest_tar, entry):
            return self._ensure_extra_files(key, entry)
        return self.is_ready(key)

    # ── extra_files（v1.1.5：主 tarball 之外的附属单文件）──────────
    # 动机：matcha-icefall-zh-en 官方包**不含声码器**（README 明示另取
    # vocos-16khz-univ.onnx），而缺 vocoder 时 sherpa C++ 构造直接终止进程
    # （台架实锤，非可捕获异常）——附属文件必须与主包同级、且就绪判定硬闸。
    # 纪律与主包一致：sha256 校验、tmp+replace 原子落盘、多源回退、
    # import/ 同名文件优先（手动逃生门）。落盘目录=解包实际目录（top_dir 探测）。
    def _resolved_dir(self, key: str, entry: dict) -> Path:
        top = entry.get("top_dir", "")
        cand = self.models_dir / key / top
        if top and cand.is_dir():
            return cand
        return self.models_dir / key

    def _ensure_extra_files(self, key: str, entry: dict) -> bool:
        extras = entry.get("extra_files") or []
        if not extras:
            return True
        base = self._resolved_dir(key, entry)
        base.mkdir(parents=True, exist_ok=True)
        for ef in extras:
            name = ef.get("file") or ""
            if not name:
                continue
            dest = base / name
            imp = self.import_dir / name
            if dest.exists():
                # v1.1.27 项4：清导入口必须**以 dest 校验通过为前置**。旧序先
                # unlink(imp) 再校验 dest——dest 是坏字节时救急包已被销毁、坏 dest
                # 也删了，只剩跨境网络一条路（离线/断网现场只能干等）。
                if not ef.get("sha256") or self._sha_ok(dest, ef["sha256"]):
                    if imp.exists():
                        imp.unlink(missing_ok=True)   # 导入口残留让位已就位文件（防反复重下）
                    continue
                dest.unlink(missing_ok=True)  # 坏字节：重取（导入口→urls 同序）
            if imp.exists():
                try:
                    tmp = dest.with_suffix(dest.suffix + ".tmp")
                    tmp.write_bytes(imp.read_bytes())
                    os.replace(tmp, dest)
                    if not ef.get("sha256") or self._sha_ok(dest, ef["sha256"]):
                        continue
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            pseudo = {"_key": key, "tarball": name, "sha256": ef.get("sha256", ""),
                      "urls": ef.get("urls", []), "size_mb": ef.get("size_mb", 0)}
            if not self._download_any(pseudo, dest):
                self._set_status(key, state="incomplete",
                                 detail=f"附属文件 {name} 获取失败（可放 import/{name}）")
                return False
        if self.is_ready(key):
            self._set_status(key, state="ready", pct=100, detail="已就绪")
        return self.is_ready(key)

    @staticmethod
    def _sha_ok(path: Path, expected: str) -> bool:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            return h.hexdigest() == expected
        except OSError:
            return False

    def ensure_async(self, key: str, force: bool = False) -> None:
        if (t := self._threads.get(key)) and t.is_alive():
            return
        t = threading.Thread(target=self.ensure, args=(key, force),
                             name=f"model-{key}", daemon=True)
        self._threads[key] = t
        t.start()

    def ensure_all_async(self) -> None:
        for k in self.keys():
            if not self.is_ready(k):
                self.ensure_async(k)

    def _download_any(self, entry, dest: Path) -> bool:
        import urllib.request
        expected = entry.get("sha256", "")
        for url in entry.get("urls", []):
            if not url:
                continue
            try:
                size = entry.get("size_mb", 0) * 1e6
                self._set_status(entry["_key"], state="downloading", pct=0,
                                 detail=f"源 {_src_host(url)}")
                logger.info("[模型] 下载 %s ← %s", entry["tarball"], url)
                if self.abort.is_set():
                    return False
                part = str(dest) + ".part." + uuid.uuid4().hex[:8]   # F2：并发唯一 .part
                req = urllib.request.Request(url, headers={"User-Agent": "huijian-voice/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                    got = 0
                    last_pct = -1
                    # v1.1.41（ACR 下载腿复验的同类漏网）：`HTTPResponse.read(n)` 是
                    # BufferedReader 语义——**攒满 n 才返回**，滴流极慢时下面两道总时限
                    # 复检（abort/deadline）永不可达，v1.1.39 加的 `_DL_WALL_TIMEOUT_S`
                    # 形同虚设。`read1(n)` 一次底层读即返回 ⇒ 复检跟随数据到达。
                    # file:/data: 这类响应没有 read1（lock 里手写镜像路径可能用），退回整块读。
                    read1 = getattr(r, "read1", r.read)
                    # 第四轮审计 P2（历史遗留⑥）：**字节上限**——旧形在核 sha 前
                    # 无上限落盘，错源/中间盒顶替可把 /data 写满再被拒；按 lock 声
                    # 明体积上浮 50%（缺声明时给绝对兜底）即拒。
                    _cap = int(size * 1.5) if size else 0
                    _deadline = time.monotonic() + _DL_WALL_TIMEOUT_S
                    while True:
                        if self.abort.is_set():
                            raise TimeoutError("停机/中止：放弃当前下载")
                        if time.monotonic() > _deadline:
                            raise TimeoutError(
                                f"下载超总时限 {_DL_WALL_TIMEOUT_S}s（源 {_src_host(url)} "
                                f"已收 {got} 字节）→ 换源")
                        chunk = read1(1 << 20)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > (_cap or _DL_ABS_MAX_BYTES):
                            raise RuntimeError(
                                f"下载超上限：已 {got} 字节，上限 {_cap or _DL_ABS_MAX_BYTES}")
                        f.write(chunk)
                        if size:
                            pct = min(100, int(got / size * 100))
                            if pct != last_pct and pct % 5 == 0:
                                last_pct = pct
                                self._set_status(entry["_key"], state="downloading", pct=pct, detail=f"{got//1024//1024}MB / {int(size//1e6)}MB")
                if expected:
                    h = hashlib.sha256()
                    with open(part, "rb") as f:
                        for blk in iter(lambda: f.read(1 << 20), b""):
                            h.update(blk)
                    if h.hexdigest() != expected:
                        logger.warning("[模型] %s sha256 不符（源 %s），换源", entry["tarball"], url)
                        Path(part).unlink(missing_ok=True)
                        continue
                os.replace(part, dest)
                return True
            except Exception as e:
                logger.warning("[模型] 源 %s 失败: %s", url, e)
                for stale in dest.parent.glob(dest.name + ".part.*"):
                    stale.unlink(missing_ok=True)
                if self.abort.is_set():
                    return False
                continue
        self._set_status(entry["_key"], state="failed", detail="全部源失败；可手动导入")
        return False

    def _extract(self, key: str, tar_path: Path, entry) -> bool:
        target = self.models_dir / key
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._set_status(key, state="extracting", pct=100, detail="解包中")
            with tarfile.open(tar_path, "r:*") as tf:
                # F2 收口：越界路径之外，symlink/hardlink/设备成员一并拒绝
                # （link 指向目录外 + 同名后续成员可穿透）；py≥3.12 再叠
                # filter="data" 双保险（bookworm 3.11.2 无此参，TypeError 回落手筛）。
                def _safe(m):
                    if m.name.startswith(("/", "..")) or "/../" in m.name:
                        return False
                    return not (m.issym() or m.islnk() or m.isdev())
                members = [m for m in tf.getmembers() if _safe(m)]
                try:
                    tf.extractall(target, members=members, filter="data")
                except TypeError:
                    tf.extractall(target, members=members)
            # 全部成员解包成功后才盖章（见 model_dir_for 的原子性注释）。
            # M11：章内容升级为包身份 `tarball|sha256`（lock 当时期望值）——
            # lock 回填新 sha 后章比对失配 → 自动判不就绪重验，处置预案生效。
            with open(target / ".extracted_ok", "w", encoding="utf-8") as mf:
                mf.write(f"{entry.get('tarball', tar_path.name)}|"
                         f"{entry.get('sha256', '')}")
            # 鸡生蛋根修（2026-09-24 matcha 实锤）：此处只判**主包自带**文件
            # （required_files 剔除 extra_files 附属），齐即返回 True 交
            # _ensure_extra_files 补附属（vocos 声码器等）。此前直接 is_ready 全量
            # 判（含附属）⇒ 附属未下时恒 False ⇒ 补下永不被调用 ⇒ 永久 incomplete。
            base = self._resolved_dir(key, entry)
            if not self._files_ok(base, self._tarball_required(entry)):
                self._set_status(key, state="incomplete", detail="解包后校验文件缺失")
                return False
            if self.is_ready(key):
                self._set_status(key, state="ready", pct=100, detail="已就绪")
                logger.info("[模型] %s 就绪 @ %s", key, target)
                # M12（2026-09-23 深审）：解包成功盖章后即删下载归档——
                # SenseVoice+kokoro ≈1.4GB 死重再无删除点（本仓 _write_status
                # S17-2「必关必删」同纪律）。import/ 目录是用户手动导入口
                # （lock:5「放包即用」），**不动**；校验失败路径也不动（留待重试）。
                # 附属未就绪时暂留归档：补下失败重试不必重拉主包。
                if tar_path.parent == self.models_dir:
                    try:
                        tar_path.unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("[模型] 归档删除失败（不阻断）: %s", e)
            return True
        except Exception as e:
            logger.error("[模型] 解包 %s 失败: %s", key, e)
            self._set_status(key, state="failed", detail=str(e))
            return False


