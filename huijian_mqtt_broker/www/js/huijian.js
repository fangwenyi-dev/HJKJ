        const DOMAIN = 'window_controller_gateway';

        // v1.6.3：页头/页脚的初始版本号不再硬编码——脚本在 body 末尾，
        // 启动即与 CURRENT_VERSION 常量同步；之后 /api/version 拉到真实版本再刷新。
        // 旧实现硬编码 v1.5.1，/api/version 失败时页面永远显示过期版本号。
        document.getElementById('addonVersion').textContent = 'v' + CURRENT_VERSION;
        document.getElementById('footerVersion').textContent = 'v' + CURRENT_VERSION;

        // Ingress 下页面路径形如 /api/hassio_ingress/<token>/index.html（或带尾斜杠），
        // 直连插件端口 10998 时为 /index.html。去掉最后一段得到部署基路径。
        const INGRESS_BASE = window.location.pathname.replace(/\/[^/]*$/, '/');
        // 各配置条目的网关 SN（控制操作后刷新设备用，避免空 SN 误匹配首个实体）
        const GATEWAY_SN_BY_ENTRY = {};
        // v1.7.12（第 6 轮审计 L-3）：entryId → 配对窗口截止时间戳。
        // startPairing 点亮"配对中"黄徽后，30s 静默刷新/2s 设备重建会把
        // 徽标覆写回 online/offline——60s 内用户看到"离线"会误判配对失败。
        // 窗口内 updateGatewayStatus 跳过非配对状态覆写。
        const PAIRING_UNTIL = {};
        // 审计 2026-09-30 B-7 配套：本轮渲染里被判"禁用/未 loaded"的条目集合。
        // 设备侧的三条入口（loadGateways 的重建循环、30s silentRefresh、配对/状态
        // 按钮的延时刷新）都会按 entryId 回头覆写 devices-<entryId>，只看 entry 对象
        // 的那份判据覆盖不到它们——卡片是 renderGatewayDisabled 建的，后续刷新并不知道
        // 它是禁用条目，于是照样长出一排点下去恒 4xx 的按钮。渲染与刷新共用这一份。
        const DISABLED_ENTRIES = {};
        // 审计 2026-09-30 B-7 的**失效条件**（本批复核补口）：上面那份集合只在
        // loadGateways 整建时清，而 silentRefresh 的重建判据此前只比 entry_id 集合——
        // "禁用→重新启用"和"启动期 setup_retry→loaded"都不改变 id 集合，于是卡片
        // 连同"这条目不可用"的判定一起陈旧：恒停在「条目未启用，暂不可用」零按钮，
        // 只有手动刷新/F5 能纠正（与 v1.7.55 那条 P0 同族——面板不自愈）。
        // ENTRY_SIG 记下"这张卡是按哪个形态渲染的"，形态一变即升级为整建。
        const ENTRY_SIG = {};
        // 渲染输入只留一份判据：loadGateways 记的和 silentRefresh 比的是同一个函数，
        // 两处各自拼字段迟早漂移。
        function entryRenderSig(entry) {
            return [entry.disabled_by || '', entry.state || '',
                    (entry.data && entry.data.gateway_sn) || ''].join('|');
        }
        // 审计 2026-09-30 G-1：配对收尾定时器的句柄必须存下来。旧写法两处
        // setTimeout 都不接句柄、startPairing 也没有重入闸，于是 30s 后再点一次
        // 「配对」时，**前一次**的 60s 回调会把新窗口（应到 t=95s）的 PAIRING_UNTIL
        // delete 掉并立刻重建列表 ⇒ 配对在服务端仍生效的 30 多秒里徽标显示"在线"，
        // 用户误判失败反复点击——正是 PAIRING_UNTIL 注释要防的场景。
        const PAIRING_TIMERS = {};

        // ========== 初始化 ==========
        async function init() {
            // 静态徽章先落当前内置版本（防 /api/version 失败时页面顶部空白/陈旧）
            document.getElementById('addonVersion').textContent = 'v' + CURRENT_VERSION;
            document.getElementById('footerVersion').textContent = 'v' + CURRENT_VERSION;
            try {
                // v1.6.6：显式禁缓存——插件更新后浏览器启发式缓存仍供旧
                // version.json/index.html 导致页面版本号陈旧（1.6.4→1.6.5 实锤）
                const verResp = await fetchT(INGRESS_BASE + 'api/version', { cache: 'no-store' }, 8000);
                if (verResp.ok) {
                    const verData = await verResp.json();
                    CURRENT_VERSION = verData.addon_version || CURRENT_VERSION;
                    document.getElementById('addonVersion').textContent = 'v' + CURRENT_VERSION;
                    document.getElementById('footerVersion').textContent = 'v' + CURRENT_VERSION;
                }
            } catch (e) { console.log('版本获取失败:', e); }
            await refreshAll();
            // v1.6.4：自动静默检查一次更新（只点亮徽章不打扰）。
            // v1.6.9：间隔 10→30 分钟；函数内含跨标签页去重 + 不可见跳过，
            // 多标签/后台页不再叠加 GitHub 匿名限流（60/h/IP）压力
            silentUpdateCheck();
            setInterval(silentUpdateCheck, 30 * 60 * 1000);
        }

        // ========== 刷新所有 ==========
        // v1.7.12（审计 L-4）：手动刷新/初始化共用入口补防重入——旧版无
        // 守卫，连点「刷新」或慢网络下 init 未完又触发，多轮 checkServiceStatus
        // + loadGateways 交错写同一容器 DOM（与 v1.6.10 N6 在 silentRefresh
        // 修过的同族），忙时直接忽略本次
        let _refreshAllBusy = false;
        async function refreshAll() {
            if (_refreshAllBusy) return;
            _refreshAllBusy = true;
            try {
                await checkServiceStatus();
                await loadGateways();
                await loadRemoteControl();
            } finally {
                _refreshAllBusy = false;
            }
        }

        // ========== 远程控制（慧尖云）绑定信息 ==========
        // v1.7.37：只读集成侧视图 GET /api/window_controller_gateway/hub。取不到
        // 一律降级成"—"而不是留"检测中"——老的集成版本（<1.7.35）没有这条路由，
        // 主流程不能因此显示得像坏了；用户看到的应当是"未启用"。
        // v1.7.38：同一份数据顺带驱动本卡右侧的二维码（自动出现 / 点击刷新）。
        // v1.7.39（用户令）：二维码从页头 logo 旁挪进「远程控制」卡右侧（页头摆白底码太突兀）。
        // 二维码载荷：HUJIAN-BIND:<载荷版本>:<6 位码>——小程序侧解析同一格式（对不认识的
        // 版本只提示不绑定），所以版本位以后换协议时老版本不会误解。
        const BIND_PAYLOAD_PREFIX = 'HUJIAN-BIND:1:';
        // 成员称呼长度上限，与 hub_client.MEMBER_ALIAS_MAX_LEN 同值（两份，pytest 对账）。
        // 面板先截一次、加载项再截一次：两侧口径不一致时用户会看到"存进去的名字比填的短"。
        const MEMBER_ALIAS_MAX = 12;
        let _bindCode = '';
        let _copyTimer = null;

        function refreshBindQr() {
            refreshBindCode();   // 点击＝换一个新码（旧码过期后"只读刷新"刷不出可用的码）
        }

        /** 显式换新码（旧码当场作废，所以只能由用户点击触发，不做自动轮换）。
         *  老版本集成没有这条路由 → 退回只读刷新，别把页面显示成"读取失败"。*/
        async function refreshBindCode() {
            try {
                const resp = await haApi('/window_controller_gateway/hub/bindcode', 'POST');
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                const info = await resp.json();
                if (info && info.refreshOk) { applyHubStatus(info); return }
                await loadRemoteControl();
            } catch (e) {
                await loadRemoteControl();
            }
        }

        function renderBindQr(code) {
            const wrap = document.getElementById('hubQr');
            const box = document.getElementById('hubQrBox');
            const tip = document.getElementById('hubQrTip');
            if (!wrap || !box) return;
            if (!code || !window.HjQr) { wrap.hidden = true; return; }
            try {
                window.HjQr.render(box, BIND_PAYLOAD_PREFIX + code, { ecc: 'M' });
            } catch (e) {
                wrap.hidden = true;          // 生成失败宁可不摆——扫不出来的码比没有码更坏
                console.log('绑定码二维码生成失败:', e);
                return;
            }
            wrap.hidden = false;
            if (tip) tip.textContent = code;
        }

        /** 网关口径文案（纯函数，便于真跑）。
         *  一台时直接给 SN（沿用旧面板"一眼看到本机网关"的好处）；多台时给计数 +
         *  全部 SN——写单个 SN 会让人以为远程控制只管一台。 */
        function hubGatewayText(info) {
            const gws = (info && Array.isArray(info.gateways)) ? info.gateways : null;
            if (!gws || !gws.length) {
                const one = info && info.gatewaySn;
                return one ? String(one) : '—';
            }
            const devs = gws.reduce((n, g) => n + (g && g.deviceCount ? g.deviceCount : 0), 0);
            if (gws.length === 1) {
                return gws[0].sn + ' · ' + devs + ' 个子设备';
            }
            return gws.length + ' 台 · ' + devs + ' 个子设备（' +
                gws.map((g) => g.sn).join('、') + '）';
        }

        /** 云通道**连接类**故障文案（纯函数，便于 node 真跑）。
         *  只映射 hub_client.last_error 的已知取值；未知的一律回 ''（不显示）——
         *  面板是给终端用户看的，把异常类名（RuntimeError 之类）摆上去只是噪声。
         *  换码/成员/踢人这类**操作**故障走 hubOpErrorText（两个槽分开，见下）。*/
        function hubErrorText(lastError) {
            switch (lastError) {
                case 'identity_rejected':
                    return '云端不认识本机身份，已自动重新注册——需要重新扫码绑定';
                case 'identity_rejected_loop':
                    return '云端反复拒绝本机身份，已停止重试（多见于 hub 多副本或注册表未持久化）';
                default:
                    return '';
            }
        }

        /** 云通道**操作类**故障文案（换码 / 成员 / 踢人）。
         *  值优先是 hub 回的真实 err（no_owner/members_full/rate_limited/…），
         *  只有拿不到 err 的网络降级才是本地值（bindcode_failed/members_unavailable）。
         *  为什么必须分开一个槽：混在连接类里时，一次瞬时换码失败会长期盖住
         *  identity_rejected_loop 这条最有诊断价值的信息，而且所有成功路径都不清它
         *  ⇒ 面板会在一张刚签发的有效码旁边一直喊"码可能已过期"。
         *  未知码回 ''（不显示）：终端用户看到裸英文码只是噪声。*/
        function hubOpErrorText(opError) {
            switch (opError) {
                case 'no_owner':
                    return '这处安装还没有主人：请先自己扫右边的二维码成为主人，再点「添加家人」';
                case 'members_full':
                    return '家庭成员已满，先移除一位才能再加';
                case 'rate_limited':
                    return '云端限流，请稍后再试';
                case 'registry_full':
                    return '云端实例表已满，暂时无法注册/换码——请联系慧尖';
                case 'superseded':
                    return '这次请求已被更新的请求取代，请重试';
                case 'bad_secret':
                    return '云端不认识本机凭据（注册表可能已重置），稍后会自动重新注册——需要重新扫码绑定';
                case 'unknown_instance':
                    return '云端已没有本机实例（注册表可能已重置），稍后会自动重新注册——需要重新扫码绑定';
                case 'identity_persist_failed':
                    // 审计 2026-10-05 D-1 补口：这个值的产生点（_ensure_registered 落盘失败）
                    // 早在 v1.7.56 就写了，但当时写的是连接槽、面板又不认这个码 ⇒ 用户侧
                    // 永远看不见。文案必须把"现在还活着、重启才作废绑定码"这个时间差说清。
                    return '本机没能保存云端身份（磁盘写入失败）：远程控制现在仍可用，但 HA 重启后'
                        + '会重新注册并作废所有人手上的绑定码——请先腾出磁盘空间，再让家人重新扫码';
                case 'unknown_member':
                    return '云端没有这位家人（可能已被移除），列表即将刷新';
                case 'owner_cannot_leave':
                    return '主人不能移除自己（否则这处安装就没人能管成员了）';
                case 'bindcode_failed':
                    return '换绑定码失败（云端暂不可达），页面上的码可能已过期';
                case 'bindcode_rejected':
                    return '云端拒绝换绑定码请求，稍后再试或点二维码重试';
                case 'bindcode_persist_failed':
                    return '新绑定码已生效但本机没能保存下来（磁盘写入失败）——HA 重启后这张码会失效，'
                        + '请确认二维码能刷新，并尽快让家人扫码';
                case 'hub_too_old_for_member_code':
                    return '云端 hub 版本过旧，暂不支持成员码';
                case 'members_unavailable':
                    return '家庭成员读取失败（云端暂不可达），请稍后重试';
                case 'members_rejected':
                    return '云端拒绝读取家庭成员，请稍后重试';
                case 'member_remove_failed':
                    return '移除家人失败（云端暂不可达），请稍后重试';
                case 'member_remove_rejected':
                    return '云端拒绝移除这位家人';
                case 'member_rename_rejected':
                    return '这位家人已不在成员列表里（可能刚被移除），改名没保存';
                case 'member_rename_failed':
                    return '改名没保存（本机文件写入失败），详情看 HA 日志';
                default:
                    return '';
            }
        }

        /** 有效期文案的分钟数：优先用服务端回的 TTL 秒数，缺失/非法才回落字面量。
         *  hub 改了有效期之后，硬编的"10 分钟"就是对用户说谎——他会照着去扫一张
         *  云端早已作废的码，然后只看到 code_invalid。*/
        function ttlMinutes(ttlSec, fallbackMin) {
            const n = Number(ttlSec);
            if (!isFinite(n) || n <= 0) return fallbackMin;
            return Math.max(1, Math.round(n / 60));
        }

        /** 成员列表是不是"读取失败"（网络/云端拒绝）——必须与"云端版本过旧"分成两句话，
         *  否则用户会去升级云端来修一个网络问题（或反过来以为云通道坏了）。*/
        function membersReadFailed(info) {
            const e = info && info.lastOpError;
            return e === 'members_unavailable' || e === 'members_rejected';
        }

        /** 成员区口径文案（纯函数，便于 node 真跑）。
         *  六种态必须分开：读不到（—）/ 老 hub 不支持（说清是云端版本旧）/ 读取失败且手上
         *  一条都没有（说清是网络，稍后重试）/ 读取失败但还留着上次成功的列表（照报人数 +
         *  标注可能过期）/ 只有主人 / N 与上限。
         *  最后那条是刻意分的：读取失败时 `self.members` 仍是上次成功的值，用"读取失败"把
         *  人数顶掉＝把已知信息藏起来，而且下面照常渲染出的成员行会与这句话自相矛盾。*/
        function membersText(info) {
            if (!info || info.enabled === false) return '—';
            if (info.membersSupported === false) return '云端版本过旧，暂不支持';
            const list = Array.isArray(info.members) ? info.members : [];
            const max = info.membersMax || 8;
            if (membersReadFailed(info)) {
                return list.length ? (list.length + ' / ' + max + ' 人（列表可能已过期）') : '读取失败，稍后重试';
            }
            if (!list.length) return '只有你一人';
            return list.length + ' / ' + max + ' 人';
        }

        function renderMemberQr(code) {
            const wrap = document.getElementById('hubMemberQr');
            const box = document.getElementById('hubMemberQrBox');
            const codeEl = document.getElementById('hubMemberCode');
            if (!wrap || !box) return;
            if (!code || !window.HjQr) { wrap.hidden = true; return; }
            try {
                // 载荷前缀与 owner 码**完全相同**：角色由 hub 按码查表决定，不写进载荷
                // （载荷无签名，写了也能被改；hub 查表才提不了权）
                window.HjQr.render(box, BIND_PAYLOAD_PREFIX + code, { ecc: 'M' });
            } catch (e) {
                wrap.hidden = true;          // 生成失败宁可不摆——扫不出来的码比没有码更坏
                console.log('成员码二维码生成失败:', e);
                return;
            }
            wrap.hidden = false;
            if (codeEl) codeEl.textContent = code;
        }

        function renderMembers(info) {
            const ul = document.getElementById('hubMembers');
            const empty = document.getElementById('hubMembersEmpty');
            const countEl = document.getElementById('hubMembersCount');
            const btn = document.getElementById('addMemberBtn');
            if (!ul || !countEl) return;
            const usable = !!info && info.enabled !== false;
            const supported = usable && info.membersSupported !== false;
            const readFailed = usable && membersReadFailed(info);
            const list = (usable && Array.isArray(info.members)) ? info.members : [];
            const max = (usable && info.membersMax) || 8;
            countEl.textContent = membersText(info);
            if (empty) {
                empty.textContent = !usable ? '—'
                    : (!supported ? '云端 hub 版本过旧，暂不支持添加家人'
                        : (readFailed ? '成员列表读取失败（云端暂不可达），稍后自动重试'
                            : '只有你一人'));
                // 占位行只在**真的一条都没有**时出现：读取失败但还留着上次成功的列表时，
                // 下面照样会渲染出成员行，再叠一句"读取失败"就是同屏自相矛盾（陈旧这件事
                // 已由 membersText 挂在人数上：「N / 8 人（列表可能已过期）」）。
                empty.hidden = supported && list.length > 0;
            }
            // 满员 / 老 hub / 读不到状态都要禁用按钮：能点但必然失败，比不能点更让人困惑。
            // 读取失败（网络）不禁用——那是瞬时的，禁了用户反而以为功能没了。
            if (btn) {
                btn.disabled = !supported || list.length >= max;
                btn.title = !usable ? '读取不到云端状态'
                    : (!supported ? '云端 hub 版本过旧，暂不支持添加家人'
                        : (list.length >= max ? '家庭成员已满（' + max + ' 人），先移除一位才能再加'
                            : '生成一个 ' + ttlMinutes(usable && info.memberCodeTtlS, 10)
                              + ' 分钟有效的一次性成员码'));
            }
            ul.textContent = '';
            for (const m of list) {
                if (!m || !m.mid) continue;
                const li = document.createElement('li');
                const who = document.createElement('span');
                const masked = m.openidMasked || '—';   // 只有掩码：hub 从不回完整 openid
                const alias = (typeof m.alias === 'string' && m.alias) ? m.alias : '';
                // 一律按文本渲染：称呼是用户在面板上手打的自由文本，绝不能当标记语言解析
                who.textContent = alias || masked;
                who.className = 'hub-member-name';
                who.title = alias ? ('云端掩码 ' + masked) : '点「改名」给这位家人起个称呼';
                const rename = document.createElement('button');
                rename.type = 'button';
                rename.className = 'btn btn-ghost btn-mini';
                rename.textContent = '改名';
                rename.setAttribute('data-mid', String(m.mid));
                rename.onclick = function () { renameMember(String(m.mid), alias); };
                const kick = document.createElement('button');
                kick.type = 'button';
                kick.className = 'btn btn-ghost btn-mini';
                kick.textContent = '移除';
                kick.setAttribute('data-mid', String(m.mid));
                kick.onclick = function () { removeMember(String(m.mid)); };
                li.appendChild(who);
                li.appendChild(rename);
                li.appendChild(kick);
                ul.appendChild(li);
            }
        }

        /** 称呼规范化：控制符换空格、去首尾空白、截到上限。
         *  与 hub_client.clean_member_alias 同口径（跨语言两份实现，
         *  tests/test_v1754_member_alias.py 用同一批用例逐条对账）。*/
        function cleanMemberAlias(raw) {
            if (typeof raw !== 'string') return '';
            return raw.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, MEMBER_ALIAS_MAX);
        }

        /** 改一位家人的本机称呼（v1.7.54）。留空＝清掉称呼、恢复显示云端掩码。
         *  名称只存这台 HA，不上云——hub 的成员记录根本没有名称字段。*/
        async function renameMember(mid, current) {
            if (!mid) return;
            const tip = '给这位家人起个称呼（最多 ' + MEMBER_ALIAS_MAX + ' 个字，留空则显示云端掩码）';
            let raw;
            try {
                raw = window.prompt(tip, current || '');
            } catch (e) {
                console.log('改名输入框打不开:', e);
                return;
            }
            if (raw === null) return;          // 取消＝什么都没发生
            const name = cleanMemberAlias(raw);
            try {
                const resp = await haApi('/window_controller_gateway/hub/members/rename',
                                         'POST', { mid: mid, name: name });
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                applyHubStatus(await resp.json());
                return;
            } catch (e) {
                console.log('成员改名失败:', e);
            }
            await loadRemoteControl();
        }

        /** 显式签发成员码（不受自动轮换与节流影响：这是主人的显式意图，
         *  自动轮换会把已经截图发出去的码作废）。*/
        async function refreshMemberCode() {
            try {
                const resp = await haApi('/window_controller_gateway/hub/bindcode', 'POST', { kind: 'member' });
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                applyHubStatus(await resp.json());
            } catch (e) {
                console.log('成员码签发失败:', e);
                await loadRemoteControl();
            }
        }

        async function removeMember(mid) {
            if (!mid) return;
            if (!confirm('移除后这位家人立刻失去远程控制权，确定？')) return;
            try {
                const resp = await haApi('/window_controller_gateway/hub/members/remove', 'POST', { mid: mid });
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                applyHubStatus(await resp.json());
                return;
            } catch (e) {
                console.log('移除成员失败:', e);
            }
            await loadRemoteControl();
        }

        /** 唯一渲染出口：GET 状态与 POST 换码都走这里，任何一条路径都不会漏渲染。*/
        function applyHubStatus(info, failed) {
            const dot = document.getElementById('hubDot');
            const statusEl = document.getElementById('hubStatus');
            const codeEl = document.getElementById('hubCode');
            const gwEl = document.getElementById('hubGateway');
            const gwDot = document.getElementById('hubGwDot');
            const expEl = document.getElementById('hubCodeExp');
            const errEl = document.getElementById('hubError');
            if (!dot || !statusEl || !codeEl || !gwEl) return;
            if (failed || !info || !info.enabled) {
                // 读取失败与"未启用"必须分开显示：故障伪装成正常态会让排障从第一步就走错
                dot.className = 'dot ' + (failed ? 'dot-err' : 'dot-unknown');
                statusEl.textContent = failed ? '读取失败' : '未启用';
                codeEl.textContent = '------';
                gwEl.textContent = '—';
                if (gwDot) gwDot.className = 'dot dot-unknown';
                if (expEl) { expEl.textContent = ''; expEl.className = 'hub-code-exp'; }
                if (errEl) { errEl.textContent = ''; errEl.hidden = true; }
                _bindCode = '';
                renderBindQr('');
                renderMemberQr('');
                renderMembers(null);
                const memExp0 = document.getElementById('hubMemberExp');
                if (memExp0) memExp0.textContent = '';
                return;
            }
            dot.className = 'dot ' + (info.connected ? 'dot-ok' : 'dot-warn');
            statusEl.textContent = info.connected ? '已连接' : '未连接';
            _bindCode = info.bindCode || '';
            codeEl.textContent = _bindCode || '------';
            gwEl.textContent = hubGatewayText(info);
            const gwCount = (Array.isArray(info.gateways) && info.gateways.length) ||
                (info.gatewaySn ? 1 : 0);
            if (gwDot) gwDot.className = 'dot ' + (gwCount ? 'dot-ok' : 'dot-unknown');
            if (expEl) {
                // 码会过期：把"还剩多久/已经过期"摆出来，用户不用靠扫码失败来发现
                const sec = typeof info.bindCodeExpiresIn === 'number' ? info.bindCodeExpiresIn : null;
                if (!_bindCode) { expEl.textContent = ''; expEl.className = 'hub-code-exp'; }
                else if (info.bindCodeExpired || (sec !== null && sec <= 0)) {
                    expEl.textContent = '已过期，点右边的二维码换新码';
                    expEl.className = 'hub-code-exp expired';
                } else if (sec !== null) {
                    expEl.textContent = '剩余 ' + Math.max(1, Math.round(sec / 60)) + ' 分钟';
                    expEl.className = 'hub-code-exp';
                } else { expEl.textContent = ''; expEl.className = 'hub-code-exp'; }
            }
            if (errEl) {
                // 有原因就摆出来（哪怕长连是通的）：换码失败这类问题不会断开长连，
                // 但用户看到的就是一张扫不出来的死码，此前只能去翻 HA 日志。
                // **操作类优先**：它是用户刚刚那一下的结果，比后台的连接类状态更要紧。
                const why = hubOpErrorText(info.lastOpError) || hubErrorText(info.lastError);
                errEl.textContent = why;
                errEl.hidden = !why;
            }
            // 有效期文案跟服务端回的 TTL 走（硬编"10 分钟"在 hub 改了 TTL 后就是假话）
            const ttlEl = document.getElementById('hubCodeTtl');
            if (ttlEl) ttlEl.textContent = ttlMinutes(info.bindCodeTtlS, 10) + ' 分钟';
            renderBindQr(_bindCode);
            // 成员区与成员码同样只在这里渲染（单一出口纪律：多出口＝总有一条路径漏渲染）
            renderMemberQr((info.memberCode && !info.memberCodeExpired) ? info.memberCode : '');
            renderMembers(info);
            const memExpEl = document.getElementById('hubMemberExp');
            if (memExpEl) {
                const memIn = typeof info.memberCodeExpiresIn === 'number' ? info.memberCodeExpiresIn : null;
                if (!info.memberCode) memExpEl.textContent = '';
                else if (info.memberCodeExpired || (memIn !== null && memIn <= 0)) memExpEl.textContent = '已过期，点「添加家人」换新码';
                else if (memIn === null || memIn < 0) memExpEl.textContent = '';
                else memExpEl.textContent = '剩余 ' + Math.max(1, Math.round(memIn / 60)) + ' 分钟';
            }
            const memTipEl = document.getElementById('hubMemberTip');
            if (memTipEl) {
                memTipEl.textContent = '家人用「小慧语音」扫一扫；'
                    + ttlMinutes(info.memberCodeTtlS, 10) + ' 分钟内有效，扫完即失效';
            }
        }

        async function loadRemoteControl() {
            try {
                const resp = await haApi('/window_controller_gateway/hub');
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                applyHubStatus(await resp.json());
            } catch (e) {
                applyHubStatus(null, true);
                console.log('远程控制状态获取失败:', e);
            }
        }

        async function copyBindCode() {
            const btn = document.getElementById('copyCodeBtn');
            const codeEl = document.getElementById('hubCode');
            if (!_bindCode) return;
            let done = false;
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(_bindCode);
                    done = true;
                }
            } catch (e) { done = false; }
            if (!done && codeEl && window.getSelection) {
                // 退路：剪贴板 API 在非安全上下文/权限策略下会被拒——选中文本让用户自己复制
                const range = document.createRange();
                range.selectNodeContents(codeEl);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
            }
            if (btn) {
                btn.textContent = done ? '已复制' : '按 Ctrl+C';
                if (_copyTimer) clearTimeout(_copyTimer);
                _copyTimer = setTimeout(function () { btn.textContent = '复制'; }, 1800);
            }
        }

        // ========== 无感刷新（只更新状态值，不重建 DOM）==========
        // v1.6.10（审计 N6）：带超时仍可能上一周期未走完被 30s 定时器再入，
        // 并发周期会交错写 DOM/集合比对误判——防重入：忙则跳过本周期
        let _silentRefreshing = false;
        async function silentRefresh() {
            if (_silentRefreshing) return;
            _silentRefreshing = true;
            try {
            await checkServiceStatus();
            // hub 卡也要刷：此前只刷设备 ⇒ 连接状态点、二维码、码倒计时全会陈旧
            // （后台标签的跳过语义在 setInterval 那一侧，这里不再判一次 document.hidden）
            await loadRemoteControl();
            // 遍历页面上已有的网关卡片，只更新设备状态
            const container = document.getElementById('gatewayContainer');
            if (!container) return;
            const gwItems = container.querySelectorAll('.gateway-item');
            // v1.6.9（外部审计确认）：网关级增删也要被检测——此前只在卡片为
            // 0 时重建，HA 里新增第二台网关页面永远不出现（与 v1.6.6 设备级
            // 自动增删不对称）。比对 config_entries 集合，不一致才整建。
            // v1.6.10（审计 B5）：检测拉取失败不得阻断本轮设备状态刷新——
            // 此前 catch 里直接 return，config_entries 一次瞬断让整页窗口状态
            // 少刷一周期。现改为 needRebuild 标志：失败=跳过重建判定、继续更新
            let needRebuild = false;
            try {
                const resp = await haApi('/config/config_entries/entry?domain=' + DOMAIN);
                if (resp.ok) {
                    const entries = await resp.json();
                    const ids = new Set(entries.map(e => e.entry_id));
                    const rendered = new Set(Array.from(gwItems).map(el => el.id.replace('gw-', '')));
                    let same = ids.size === rendered.size;
                    if (same) for (const id of ids) { if (!rendered.has(id)) { same = false; break; } }
                    // 本批复核补口（B-7 缺的失效条件）：id 集合相同但**渲染形态**变了
                    // （禁用↔启用、setup_retry/not_loaded↔loaded、条目换了 SN）同样必须
                    // 整建。旧判据只看 id ⇒ 卡片连同 DISABLED_ENTRIES 的判定一起陈旧，
                    // 用户看到的是"重新启用后网关永远没有按钮"，只能手动刷新/F5。
                    // 签名缺失（这张卡不是 loadGateways 建的）按不一致处理——宁可多重建一次。
                    if (same) for (const e of entries) {
                        if (ENTRY_SIG[e.entry_id] !== entryRenderSig(e)) { same = false; break; }
                    }
                    if (!same) needRebuild = true;
                } else if (gwItems.length === 0) {
                    needRebuild = true; // 无卡片可更新：退回重建（旧行为）
                }
            } catch (e) {
                if (gwItems.length === 0) needRebuild = true;
                // 有卡片：检测失败视为"无增删"，继续走下方状态更新
            }
            if (needRebuild) { await loadGateways(); return; }
            if (gwItems.length === 0) return; // 无网关且无卡片：无事可做
            for (const gwItem of gwItems) {
                const entryId = gwItem.id.replace('gw-', '');
                const gatewaySn = GATEWAY_SN_BY_ENTRY[entryId] || '';
                await updateGatewayDevices(entryId, gatewaySn);
            }
            } finally {
                _silentRefreshing = false;
            }
        }

        // ========== 检查服务状态 ==========
        async function checkServiceStatus() {  /* v1.6.22 定案：不展示 credentials 状态项——密码轮换须与
        LoRa 网关固件同步，终端用户无处置能力，提示只会造成困惑（UI 钉桩负向防复活）*/
            // 1. MQTT Broker 状态 — nginx 直接返回，不依赖 HA API
            try {
                const resp = await fetchT(INGRESS_BASE + 'api/status', { cache: 'no-store' }, 8000);
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.status === 'running') {
                        setStatusDot('mqttStatus', 'ok', '运行中');
                    } else {
                        setStatusDot('mqttStatus', 'err', '已停止');
                    }
                } else {
                    setStatusDot('mqttStatus', 'err', 'HTTP ' + resp.status);
                }
            } catch (e) {
                setStatusDot('mqttStatus', 'err', '无法连接');
            }
            // 2. 网关集成检测 — 插件本地事实（run.sh 安装集成后写入 integration.json）。
            //    v1.7.31（D-3 注释订正）：旧注释"Core 会拒绝插件 token 报 401 故不
            //    经 /api/ha/"是 v1.6.3 时代陈旧叙事——本函数第 4 项与 haApi() 都在
            //    经 /api/ha/ 代理调 Core（nginx 注入 token）。用本地事实的真实理由：
            //    不依赖 HA 运行（HA 未起/重启窗口也能装、查集成落盘状态）。
            try {
                const resp = await fetchT(INGRESS_BASE + 'api/integration', { cache: 'no-store' }, 8000);
                if (!resp.ok) {
                    setStatusDot('integrationStatus', 'err', 'HTTP ' + resp.status);
                } else {
                    const info = await resp.json();
                    setStatusDot('integrationStatus', info.installed ? 'ok' : 'warn',
                        info.installed ? '已安装 v' + (info.version || '?') : '未安装');
                }
            } catch (e) {
                setStatusDot('integrationStatus', 'err', '无法读取');
            }
            // 3. MQTT 客户端连接数 — broker 实际连接数（broker_status.json 由后台循环刷新）
            // v1.7.12（第 6 轮审计 M-2）：更名「MQTT 客户端」并加悬停释义——
            // 该计数是 :2022 上全部 ESTABLISHED（LoRa 网关/桥/z2m/HA 集成
            // 混算），旧文案"HA MQTT 已连接"在 HA 条目凭据坏而网关仍连着的
            // 场景显示假绿灯（mqtt_not_available 类排障第一入口被误导）。
            try {
                const resp = await fetchT(INGRESS_BASE + 'api/broker', { cache: 'no-store' }, 8000);
                if (!resp.ok) {
                    setStatusDot('haMqttStatus', 'err', 'HTTP ' + resp.status);
                } else {
                    const info = await resp.json();
                    const n = Number(info.clients) || 0;
                    setStatusDot('haMqttStatus', n > 0 ? 'ok' : 'warn',
                        n > 0 ? '客户端在线 (' + n + ')' : '无客户端');
                    const tip = document.getElementById('haMqttStatus');
                    if (tip) tip.title = '内置 Broker :2022 上的全部 MQTT 连接数（LoRa 网关 / HA 集成 / 共存桥 / z2m 等混计）。'
                        + '此数只证明有客户端连着，不代表 HA 侧 MQTT 集成条目就绪——'
                        + '慧尖设备状态不更新且此数正常时，请到 HA「设置→设备与服务→MQTT」检查条目状态与凭据。';
                }
            } catch (e) {
                setStatusDot('haMqttStatus', 'err', '无法读取');
            }
            // 4. HA MQTT 通道 — HA Core 内 MQTT 集成条目就绪态（v1.7.29 用户拍板 C）。
            //    前三项都看不见这个断点：broker 在跑、集成已装、客户端计数与 HA 侧
            //    条目是否就绪无关（mqtt_not_ready 类现场事故的盲区）。
            try {
                const resp = await fetchT(INGRESS_BASE + 'api/ha/config/config_entries/entry?domain=mqtt', { cache: 'no-store' }, 8000);
                if (!resp.ok) {
                    setStatusDot('haMqttChannelStatus', 'err', 'HTTP ' + resp.status);
                } else {
                    const entries = await resp.json();
                    const mqtt = (Array.isArray(entries) ? entries : [])
                        .filter(e => e.domain === 'mqtt' && !e.disabled_by);
                    let st, tip;
                    if (!mqtt.length) {
                        st = ['err', '无条目'];
                        tip = 'HA 内没有 MQTT 集成条目——慧尖引导自动重试尚未落地'
                            + '（间隔 5 分钟起指数递增、封顶 1 小时，并在「设置→系统→问题」'
                            + '挂修复条目）。可等自愈，'
                            + '或手动添加 MQTT：127.0.0.1:2022 / ha_mqtt。';
                    } else if (mqtt.some(e => e.state === 'loaded')) {
                        st = ['ok', '就绪'];
                        tip = 'HA Core 的 MQTT 集成已连接内置 Broker（127.0.0.1:2022），网关上报可进入 HA。';
                    } else {
                        st = ['warn', '未就绪'];
                        tip = 'MQTT 条目存在但未加载完成（state=' + (mqtt[0].state || '?')
                            + '）——多为 broker 未起/凭据被拒/Supervisor 托管回写；'
                            + '慧尖自愈重试（间隔 5 分钟起指数递增、封顶 1 小时），'
                            + '也可在「设置→系统→问题」提交修复。';
                    }
                    setStatusDot('haMqttChannelStatus', st[0], st[1]);
                    const chanEl = document.getElementById('haMqttChannelStatus');
                    if (chanEl) chanEl.title = tip;
                }
            } catch (e) {
                setStatusDot('haMqttChannelStatus', 'err', '无法读取');
            }
        }

        function setStatusDot(elementId, status, text) {
            const el = document.getElementById(elementId);
            el.textContent = text;
            el.parentElement.querySelector('.dot').className = 'dot dot-' + status;
        }

        // ========== 带超时的 fetch 封装（v1.6.10 审计 N6；v1.6.12 第五轮审计修正）==========
        // 此前全部请求无 AbortController：HA Core 挂起时一次刷新周期可无限
        // 卡住并与 30s 定时器叠跑竞态。默认 12s：HA REST 慢查询容忍上限；
        // 本地 nginx 静态 JSON 8s；外部更新源（github/gitee 经代理）20s。
        // v1.6.12 两处修正：
        // ① abort reason 必须是 Error 对象——规范规定 fetch 以 reason 原值
        //    reject，字符串 reason 会让所有消费 e.message 的 toast 显示
        //    "undefined"（v1.6.10 用字符串，"显示人话"的目标实际未达成）；
        // ② 超时须覆盖 body 读取——原 .finally 在响应头到达即清定时器，
        //    代理"回头不 Body"时 resp.json() 无限悬挂，silentRefresh 防重入
        //    标志永不自愈。定时器延后到 json()/text() 结算再清理；调用方
        //    不读 body 时定时器自然到期（对已完成响应 abort 无害）。
        function fetchT(url, opts = {}, timeoutMs = 12000) {
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(new Error('请求超时')), timeoutMs);
            const clearTimer = () => clearTimeout(timer);
            return fetch(url, Object.assign({}, opts, { signal: ctrl.signal }))
                .then(resp => {
                    const origJson = resp.json.bind(resp);
                    resp.json = () => origJson().finally(clearTimer);
                    const origText = resp.text.bind(resp);
                    resp.text = () => origText().finally(clearTimer);
                    return resp;
                })
                .catch(e => { clearTimer(); throw e; });
        }

        // ========== HA API 调用 ==========
        // v1.7.18（第 7 轮审计 BUG-18）：用户正在交互的滑块不回写——30s 静默
        // 刷新/控制后 2s 刷新与手指拖动重叠时，旧版无条件覆写 value 导致
        // thumb 跳回、设置静默丢失（toast 却已提示"已发送"）。拖动中滑块即
        // activeElement（鼠标/触摸通用）；松手 change 事件照常发控制，下轮
        // 刷新自然同步真值。
        function userInteracting(el) {
            return !!el && document.activeElement === el;
        }
        async function haApi(path, method = 'GET', body = null) {
            const opts = { method, headers: { 'Content-Type': 'application/json' }, cache: 'no-store' };
            if (body) opts.body = JSON.stringify(body);
            return await fetchT(INGRESS_BASE + 'api/ha/' + path.replace(/^\//, ''), opts);
        }

        // ========== 加载网关列表 ==========
        async function loadGateways() {
            const container = document.getElementById('gatewayContainer');
            try {
                const resp = await haApi('/config/config_entries/entry?domain=' + DOMAIN);
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                const entries = await resp.json();
                // v1.7.12（第 6 轮审计 L-10）：全量重建前清空三张渲染态映射——
                // 条目被删除后旧 key 永不清理，同 entryId 复用（HA reload
                // 偶发复用不回来，但残留脏映射会让静默刷新拿旧 SN 发控制）。
                // 审计 2026-09-30 B-7：禁用集合同一处在重建前清空——必须在这里清
                // 而不是在下面的填充循环之后，否则本轮的判定会被下一轮抹掉。
                // 本批复核：三行提到「空清单」早退**之前**——放在后面时条目被全删
                // 的那一轮三张表都还留着上一轮的键；形态签名同批加上。
                for (const k of Object.keys(GATEWAY_SN_BY_ENTRY)) delete GATEWAY_SN_BY_ENTRY[k];
                for (const k of Object.keys(DISABLED_ENTRIES)) delete DISABLED_ENTRIES[k];
                for (const k of Object.keys(ENTRY_SIG)) delete ENTRY_SIG[k];
                if (!entries || entries.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="icon">📡</div><p>暂无网关</p><p class="hint">LoRa 网关上电后自动发现，或手动添加集成</p></div>';
                    return;
                }
                let html = '';
                for (const entry of entries) {
                    ENTRY_SIG[entry.entry_id] = entryRenderSig(entry);
                    // v1.7.33：与 :176-177 的 MQTT 条目同口径——禁用条目不再当
                    // 正常网关渲染（旧版照常配控制按钮，点下去只会 HA 4xx，
                    // 徽标恒"未知"而用户无从知道是条目被禁用）。
                    if (entry.disabled_by || (entry.state && entry.state !== 'loaded')) {
                        const gwSn0 = (entry.data && entry.data.gateway_sn) || '未知';
                        DISABLED_ENTRIES[entry.entry_id] = true;
                        html += renderGatewayDisabled(entry.title || '慧尖网关', gwSn0,
                                                      entry.entry_id, entry.state,
                                                      entry.disabled_by);
                        continue;
                    }
                    const gwSn = (entry.data && entry.data.gateway_sn) || '未知';
                    const gwName = entry.title || '慧尖网关';
                    html += renderGateway(gwName, gwSn, entry.entry_id);
                }
                container.innerHTML = html;
                for (const entry of entries) {
                    // 审计 2026-09-30 B-7：本循环原先遍历**全部** entries，把
                    // 上面判为禁用的条目也喂进 loadGatewayDevices——它按 id 覆写
                    // devices-<entryId>（renderGatewayDisabled 也建了这个容器），
                    // 于是"条目未启用，暂不可用"整块被换成 8 颗可点按钮（/devices
                    // 读设备注册表，条目禁用不影响注册表 ⇒ 必有数据）。同一张卡于是
                    // 灰徽标"已被用户禁用"配一排活按钮，点下去恒 4xx。
                    GATEWAY_SN_BY_ENTRY[entry.entry_id] = (entry.data && entry.data.gateway_sn) || '';
                    if (DISABLED_ENTRIES[entry.entry_id]) continue;
                    await loadGatewayDevices(entry.entry_id, GATEWAY_SN_BY_ENTRY[entry.entry_id]);
                }
            } catch (e) {
                // v1.7.18（第 7 轮审计 BUG-19）：区分超时/断连/权限——旧版
                // 12s 超时（HA 挂起/负载高）与 5xx 全提示"确保走侧边栏"，
                // 把用户引向错误排障方向。
                const _msg = String((e && e.message) || e);
                if (_msg.includes('HA API 401')) {
                    container.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>面板无权直接读取网关列表</p><p class="hint">请在 HA → 设置 → 设备与服务 → 「慧尖」集成中管理网关</p></div>';
                } else if (_msg.includes('请求超时')) {
                    container.innerHTML = '<div class="empty-state"><div class="icon">⏳</div><p>HA 响应超时</p><p class="hint">HA 可能正在启动或负载较高，请稍候，页面会自动重试</p></div>';
                } else {
                    container.innerHTML = '<div class="empty-state"><div class="icon">📡</div><p>无法连接 HA API</p><p class="hint">' + escapeHtml(_msg) + '（请确认 HA 正在运行、本页面经 HA 侧边栏打开）</p></div>';
                }
                container.innerHTML += '<div class="info-box"><h3>使用说明</h3><p>1. 重启 HA → 添加「慧尖」集成<br>2. 在网关设备上点击「配对」按钮添加子设备<br>3. 在 HA 设备页面控制子设备</p></div>';
            }
        }

        function renderGateway(name, sn, entryId) {
            const safeName = escapeHtml(name);
            const safeSn = escapeHtml(sn);
            const safeEntryId = escapeHtml(entryId);
            // 事件属性统一 jsAttr（先 JS 转义后 HTML 转义，v1.6.3）；
            // SN 不再烘焙进 onclick——按钮触发时实时读 GATEWAY_SN_BY_ENTRY，
            // 避免渲染时 SN 未知/变更导致把「未知」当真实 SN 发给服务（假成功）
            return '<div class="gateway-item" id="gw-' + safeEntryId + '">' +
                '<div class="gateway-header"><div class="gw-id"><span class="gw-avatar">📡</span><div class="gw-meta">' +
                '<span class="gateway-name">' + safeName + '</span>' +
                '<span class="gateway-sn">SN: ' + safeSn + '</span>' +
                '</div></div><div style="display:flex;align-items:center;gap:8px;">' +
                // v1.7.18（第 7 轮审计 BUG-17）：配对窗口内重建直接渲染黄徽——
                // 旧版重建后恒为 badge-info"检测中"，updateGatewayStatus 的
                // L-3 守卫以 DOM 现值为前提（badge-warn 才护），守卫失效被
                // 覆写"离线"，用户误判配对失败而重复点击
                (Date.now() < (PAIRING_UNTIL[entryId] || 0)
                    ? '<span class="badge badge-warn" id="gw-status-' + safeEntryId + '">配对中</span>'
                    : '<span class="badge badge-info" id="gw-status-' + safeEntryId + '">检测中</span>') +
                '<button class="btn btn-success btn-sm" onclick="startPairing(\'' + jsAttr(entryId) + '\')">🔗 配对</button>' +
                '<button class="btn btn-slate btn-sm" onclick="checkGatewayStatus(\'' + jsAttr(entryId) + '\')" title="检查网关连接状态">状态</button>' +
                '</div></div>' +
                '<div class="device-list" id="devices-' + safeEntryId + '"><div class="loading"><div class="spinner" style="width:16px;height:16px;"></div></div></div>' +
                '</div>';
        }

        function renderGatewayDisabled(name, sn, entryId, state, disabledBy) {
            // v1.7.33：被禁用/未 loaded 的条目不当正常网关渲染——旧版照常给
            // 配对/状态按钮，点下去只会 HA 4xx，徽标恒"未知"而用户无从知道
            // 根因是条目被禁用（同页 MQTT 条目早已过滤 !disabled_by）。
            let why = '未知';
            if (disabledBy === 'user') why = '已被用户禁用';
            else if (disabledBy) why = '已被 ' + disabledBy + ' 禁用';
            else if (state && state !== 'loaded') why = '状态: ' + state;
            return '<div class="gateway-item" id="gw-' + escapeHtml(entryId) + '">' +
                '<div class="gateway-header"><div class="gw-id"><span class="gw-avatar">📡</span><div class="gw-meta">' +
                '<span class="gateway-name">' + escapeHtml(name) + '</span>' +
                '<span class="gateway-sn">SN: ' + escapeHtml(sn) + '</span>' +
                '</div></div><div style="display:flex;align-items:center;gap:8px;">' +
                '<span class="badge badge-gray" title="条目未启用：' + escapeHtml(why) +
                '——请到 设置→设备与服务 启用后再操作">' + escapeHtml(why) + '</span>' +
                '</div></div>' +
                '<div class="device-list" id="devices-' + escapeHtml(entryId) +
                '"><p class="empty-hint">条目未启用，暂不可用</p></div></div>';
        }

        async function loadGatewayDevices(entryId, gatewaySn) {
            // 审计 2026-09-30 B-7：真正拦住三条路径的闸必须落在本函数入口——
            // 除 loadGateways 的重建循环外，30s silentRefresh（按已渲染卡片调
            // updateGatewayDevices，设备集合一变即升级为完整重建）与配对/状态的
            // 延时刷新都汇聚到这里，而它们手上只有 entryId、拿不到 entry 对象。
            if (DISABLED_ENTRIES[entryId]) return;
            // v1.7.33：let + await 后重取——手动刷新/静默刷新/配对后重建会在
            // 让出点整体重建容器，旧引用写回落在孤儿节点上（列表停在 spinner
            // 直到下一轮 30s 自愈）。每次 await 后按 id 重取，取不到即放弃本轮。
            let deviceListEl = document.getElementById('devices-' + entryId);
            let statusEl = document.getElementById('gw-status-' + entryId);
            if (!deviceListEl) return;
            try {
                const resp = await haApi('/window_controller_gateway/devices?config_entry_id=' + entryId);
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                const devices = await resp.json();
                deviceListEl = document.getElementById('devices-' + entryId);
                statusEl = document.getElementById('gw-status-' + entryId);
                if (!deviceListEl) return;
                if (!devices || devices.length === 0) {
                    deviceListEl.innerHTML = '<p class="empty-hint">暂无子设备，点击「配对」按钮添加</p>';
                    // v1.7.12（第 6 轮审计 M-1）：'offline' 红标改 'unknown'——
                    // 本分支连网关设备都没有，gateway_online 无从读取，
                    // "无数据"≠"离线"。v1.7.11 awaiting 条目/在线但未配
                    // 子设备的网关此前必现假红「离线」。
                    updateGatewayStatus(statusEl, 'unknown');
                    return;
                }
                let subDevices = [];
                let gwDevice = null;
                for (const dev of devices) {
                    if (dev.via_device_id) subDevices.push(dev);
                    else gwDevice = dev;  // 网关（parent）设备
                }
                // 2026-08-28 修复：从网关设备 identifiers 提取真实 SN，
                // 更新网关卡片 SN 显示（解决 "SN: 未知"，无 SN 等待模式
                // 下 entry.data.gateway_sn 为空，但设备注册表中已有真实 SN）。
                let gwSn = gatewaySn || '';
                // v1.6.3：占位符「未知」不是真实 SN（调用方可能透传），视为空；
                // 回退提取按本集成 DOMAIN 匹配 identifiers，不再依赖 [0] 位置假设
                if (!gwSn || gwSn === '未知') gwSn = deviceSnOf(gwDevice) || '';
                if (gwSn) {
                    GATEWAY_SN_BY_ENTRY[entryId] = gwSn;
                    const snEl = document.querySelector('#gw-' + entryId + ' .gateway-sn');
                    if (snEl) snEl.textContent = 'SN: ' + gwSn;
                }
                try {
                    const stateResp = await haApi('/states');
                    if (!stateResp.ok) throw new Error('HA API ' + stateResp.status);
                    const states = await stateResp.json();
                    deviceListEl = document.getElementById('devices-' + entryId);
                    statusEl = document.getElementById('gw-status-' + entryId);
                    if (!deviceListEl) return;
                    // 网关在线状态：优先用 API 的 gateway_online 字段
                    // （mqtt_handler.connected 实时值 = 收到网关上报即在线），
                    // 不依赖 binary_sensor 实体（实体未创建/匹配失败时不再显示"未知"）。
                    let gwStatus = 'unknown';
                    if (gwDevice && typeof gwDevice.gateway_online === 'boolean') {
                        gwStatus = gwDevice.gateway_online ? 'online' : 'offline';
                    } else {
                        // 兼容旧 API：回退到 binary_sensor online 实体精确查找
                        const gwEntity = findEntityByUniqueId(gwDevice, 'binary_sensor', 'online');
                        const gwSensor = gwEntity ? (states.find(s => s.entity_id === gwEntity.entity_id) || null) : null;
                        gwStatus = gwSensor ? (gwSensor.state === 'on' ? 'online' : 'offline') : 'unknown';
                    }
                    updateGatewayStatus(statusEl, gwStatus);
                    if (subDevices.length === 0) {
                        deviceListEl.innerHTML = '<p class="empty-hint">暂无子设备，点击「配对」按钮添加</p>';
                        return;
                    }
                    let html = '';
                    for (const dev of subDevices) html += renderDevice(dev, entryId, states);
                    deviceListEl.innerHTML = html;
                    for (const dev of subDevices) loadDeviceState(dev, states);
                } catch (e) {
                    // 状态获取失败：仍用 API 的 gateway_online（若有）显示网关在线状态
                    if (gwDevice && typeof gwDevice.gateway_online === 'boolean') {
                        updateGatewayStatus(statusEl, gwDevice.gateway_online ? 'online' : 'offline');
                    } else {
                        updateGatewayStatus(statusEl, 'unknown');
                    }
                    if (subDevices.length === 0) {
                        deviceListEl.innerHTML = '<p class="empty-hint">暂无子设备，点击「配对」按钮添加</p>';
                        return;
                    }
                    let html = '';
                    for (const dev of subDevices) html += renderDevice(dev, entryId, []);
                    deviceListEl.innerHTML = html;
                    // v1.7.31（D-2）：降级重建后同样走单实体异步补拉——旧版
                    // 瓷砖冻结在"状态: 加载中"最长 30s（同 catch 网关徽标却有
                    // 降级处理，设备侧是漏网半边）。loadDeviceState 内部对
                    // 缺失实体有兜底，空 states 安全。
                    for (const dev of subDevices) loadDeviceState(dev, []);
                }
            } catch (e) {
                deviceListEl.innerHTML = '<p class="err-text">加载设备失败: ' + escapeHtml(e.message) + '</p>';
            }
        }

        // 无感刷新：获取最新设备数据，只更新状态值，不重建 DOM
        // 与 loadGatewayDevices 的区别：不 innerHTML 重建，只 loadDeviceState 更新
        async function updateGatewayDevices(entryId, gatewaySn) {
            // 审计 2026-09-30 B-7：silentRefresh 是按**已渲染的卡片**遍历的，
            // 禁用卡片也在其中；不在此拦则每轮都为禁用条目白打 /devices（+ /states），
            // 且设备集合一旦变化就升级为重建——虽被 loadGatewayDevices 的闸挡回，
            // 请求已经发出去了。同一判据在两个入口都要生效。
            if (DISABLED_ENTRIES[entryId]) return;
            // v1.7.55：let + await 后重取——与 loadGatewayDevices（:780-781）同款。
            // 此处原为 const，:926-927 给 const 重赋值抛 TypeError，被 :943 的 catch
            // 静默吞掉 ⇒ 网关徽标与逐设备状态更新永久执行不到（面板值冻结到手动 F5）。
            let deviceListEl = document.getElementById('devices-' + entryId);
            let statusEl = document.getElementById('gw-status-' + entryId);
            if (!deviceListEl) return;
            try {
                const resp = await haApi('/window_controller_gateway/devices?config_entry_id=' + entryId);
                if (!resp.ok) return;
                const devices = await resp.json();
                if (!devices || devices.length === 0) {
                    // v1.6.6：服务端一个设备都没有、页面上却还渲染着设备行
                    // （整体被移除等）→ 升级为完整重建，否则残留行永不消失
                    if (deviceListEl.querySelector('.device-item')) {
                        await loadGatewayDevices(entryId, GATEWAY_SN_BY_ENTRY[entryId] || gatewaySn);
                    }
                    return;
                }
                let subDevices = [];
                let gwDevice = null;
                for (const dev of devices) {
                    if (dev.via_device_id) subDevices.push(dev);
                    else gwDevice = dev;
                }
                let gwSn = gatewaySn || '';
                // v1.6.3：占位符「未知」不是真实 SN（调用方可能透传），视为空；
                // 回退提取按本集成 DOMAIN 匹配 identifiers，不再依赖 [0] 位置假设
                if (!gwSn || gwSn === '未知') gwSn = deviceSnOf(gwDevice) || '';
                if (gwSn) {
                    GATEWAY_SN_BY_ENTRY[entryId] = gwSn;
                    const snEl = document.querySelector('#gw-' + entryId + ' .gateway-sn');
                    if (snEl) snEl.textContent = 'SN: ' + gwSn;
                }
                // v1.6.6：无感刷新兼管设备集合变化检测。旧逻辑只更新页面上
                // 已有的 dev-* 元素（新子设备没有对应 DOM，循环直接跳过），
                // 导致「集成里已出现的新设备，Web 界面永远等不到，只能手动
                // 刷新」。现在每轮比对服务端设备 id 集合与已渲染集合，
                // 有增/删即升级为 loadGatewayDevices 完整重建（该方法会
                // 重建设备列表 DOM 并渲染新行）。
                const serverIds = subDevices.map(d => d.id).sort().join(',');
                // .device-item 行 id 形如 dev-<rawId>；DOM 读取 el.id 返回
                // 属性解析后的原始 id（escapeHtml 编码已在解析时还原），
                // 与服务端 d.id 直接可比
                const renderedIds = Array.from(deviceListEl.querySelectorAll('.device-item'))
                    .map(el => el.id.slice('dev-'.length)).sort().join(',');
                if (serverIds !== renderedIds) {
                    await loadGatewayDevices(entryId, gwSn || gatewaySn);
                    return;
                }
                try {
                    const stateResp = await haApi('/states');
                    if (!stateResp.ok) return;
                    const states = await stateResp.json();
                    deviceListEl = document.getElementById('devices-' + entryId);
                    statusEl = document.getElementById('gw-status-' + entryId);
                    if (!deviceListEl) return;
                    let gwStatus = 'unknown';
                    if (gwDevice && typeof gwDevice.gateway_online === 'boolean') {
                        gwStatus = gwDevice.gateway_online ? 'online' : 'offline';
                    } else {
                        const gwEntity = findEntityByUniqueId(gwDevice, 'binary_sensor', 'online');
                        const gwSensor = gwEntity ? (states.find(s => s.entity_id === gwEntity.entity_id) || null) : null;
                        gwStatus = gwSensor ? (gwSensor.state === 'on' ? 'online' : 'offline') : 'unknown';
                    }
                    updateGatewayStatus(statusEl, gwStatus);
                    // 只更新已有设备元素的状态，不重建 DOM
                    for (const dev of subDevices) {
                        const devEl = document.getElementById('dev-' + dev.id);
                        if (devEl) loadDeviceState(dev, states);
                    }
                } catch (e) {
                    if (gwDevice && typeof gwDevice.gateway_online === 'boolean') {
                        updateGatewayStatus(statusEl, gwDevice.gateway_online ? 'online' : 'offline');
                    }
                }
            } catch (e) { /* 静默失败，不干扰用户 */ }
        }

        function updateGatewayStatus(el, status) {
            // v1.6.3：卡片可能被重渲染移除（await 后 DOM 已换），空引用防护
            if (!el) return;
            // v1.7.12（审计 L-3）：配对窗口内不覆写黄徽（见 PAIRING_UNTIL 定义处）。
            // 需要立即改写（失败回退 unknown 等）的调用方先删窗口。
            if (status !== 'pairing') {
                const _eid = (el.id || '').replace('gw-status-', '');
                if (Date.now() < (PAIRING_UNTIL[_eid] || 0)
                        && el.classList.contains('badge-warn')) return;
            }
            if (status === 'online') { el.className = 'badge badge-ok'; el.textContent = '在线'; }
            else if (status === 'pairing') { el.className = 'badge badge-warn'; el.textContent = '配对中'; }
            else if (status === 'offline') { el.className = 'badge badge-err'; el.textContent = '离线'; }
            else { el.className = 'badge badge-gray'; el.textContent = '未知'; }
        }

        function renderDevice(dev, entryId, states) {
            const devName = dev.name_by_user || dev.name || '未命名设备';
            const devId = dev.id;
            const sn = deviceSnOf(dev) || '未知';
            const supportsWindLock = sn.startsWith('5005');
            const coverEntity = findEntityState(dev, 'cover', 'cover', states);
            // v1.7.33：移除 online 实体判据——子设备永无 binary_sensor online
            // 实体（全集成唯一 online 传感器挂在网关设备上），该分支恒假；
            // 圆点真值源在 loadDeviceState（v1.7.31 D-1：status 传感器）。
            const batteryEntity = findEntityState(dev, 'sensor', 'battery', states);
            const speedEntity = findEntityState(dev, 'number', 'speed', states);
            const strengthEntity = findEntityState(dev, 'number', 'strength', states);
            const rawPos = coverEntity ? coverEntity.attributes.position : undefined;
            const currentPos = (rawPos === undefined || rawPos === null) ? '--' : rawPos;
            const safeName = escapeHtml(devName);
            const safeSn = escapeHtml(sn);
            const safeDevId = escapeHtml(devId);
            const safeEntryId = escapeHtml(entryId);
            let batteryText = '';
            if (batteryEntity && batteryEntity.state && batteryEntity.state !== 'unknown' && batteryEntity.state !== 'unavailable') {
                const bUnit = batteryEntity.attributes.unit_of_measurement || 'V';
                batteryText = escapeHtml(batteryEntity.state + bUnit);
            }
            const speedMin = speedEntity && speedEntity.attributes.min !== undefined ? speedEntity.attributes.min : 0;
            const speedMax = speedEntity && speedEntity.attributes.max !== undefined ? speedEntity.attributes.max : 100;
            const speedValid = speedEntity && speedEntity.state && speedEntity.state !== 'unknown' && speedEntity.state !== 'unavailable';
            const speedUnit = (speedEntity && speedEntity.attributes.unit_of_measurement) || '';
            const strengthMin = strengthEntity && strengthEntity.attributes.min !== undefined ? strengthEntity.attributes.min : 0;
            const strengthMax = strengthEntity && strengthEntity.attributes.max !== undefined ? strengthEntity.attributes.max : 100;
            const strengthValid = strengthEntity && strengthEntity.state && strengthEntity.state !== 'unknown' && strengthEntity.state !== 'unavailable';
            const strengthUnit = (strengthEntity && strengthEntity.attributes.unit_of_measurement) || '';

            // === 顶部行：设备信息 + 管理按钮（重命名/移除） ===
            let html = '<div class="device-item" id="dev-' + safeDevId + '">' +
                '<div class="device-top">' +
                '<div class="device-info"><div class="device-name dev-name">' +
                '<span class="dev-dot dot-unknown" id="dev-dot-' + safeDevId + '"></span>' +
                safeName + '</div>' +
                '<div class="device-sn">SN: ' + safeSn + '</div>' +
                '<div class="device-status" id="dev-state-' + safeDevId + '">状态: 加载中' + (batteryText ? ' | 电压: ' + batteryText : '') + '</div></div>' +
                '<div class="device-actions">' +
                '<button class="btn btn-slate btn-sm" onclick="renameDevice(\'' + jsAttr(devId) + '\',\'' + jsAttr(entryId) + '\',\'' + jsAttr(devName) + '\')" title="重命名设备">改名</button>' +
                '<button class="btn btn-danger btn-sm" title="移除设备" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'remove\',\'' + jsAttr(entryId) + '\')">移除</button>' +
                '</div></div>';

            // === 主控制区：开/关/停（5005 含内倒） ===
            html += '<div class="control-section">' +
                '<div class="control-row">' +
                '<button class="btn btn-success btn-sm" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'open\',\'' + jsAttr(entryId) + '\')">开</button>' +
                '<button class="btn btn-danger btn-sm" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'close\',\'' + jsAttr(entryId) + '\')">关</button>' +
                '<button class="btn btn-warn btn-sm" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'stop\',\'' + jsAttr(entryId) + '\')">停</button>';
            if (supportsWindLock) {
                html += '<button class="btn btn-tilt btn-sm" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'a\',\'' + jsAttr(entryId) + '\')">内倒</button>';
            }
            html += '</div>';

            // === 滑块区：位置（按机型能力渲染）/ 速度 / 力度 ===
            // v1.7.33（全量审计）：位置滑块此前无条件渲染且 min/max 硬编码
            // 0-100，无视 cover.py 自 v1.7.21 起暴露的 attributes.position_capable
            // ——无百分比能力机型（如 5002）上滑块可拖、指令打到 LoRa 空口、
            // 服务回 200 而设备不动（假成功）。服务侧同批已补能力闸，此处
            // 消掉"可拖的假滑块"，能力属性成为唯一判据。
            const posCapable = !!(coverEntity && coverEntity.attributes
                && coverEntity.attributes.position_capable);
            html += '<div class="slider-group">';
            if (posCapable) {
                html += '<div class="slider-row"><span class="slider-label">位置</span>' +
                    '<input type="range" class="position-slider" min="0" max="100" value="' + (currentPos === '--' ? 0 : escapeHtml(currentPos)) + '"' +
                    ' oninput="this.nextElementSibling.textContent=this.value+\'%\'"' +
                    ' onchange="controlDevicePosition(\'' + jsAttr(devId) + '\', this.value, \'' + jsAttr(entryId) + '\')">' +
                    '<span class="slider-value position-value">' + escapeHtml(currentPos) + (currentPos === '--' ? '' : '%') + '</span></div>';
            } else {
                html += '<div class="slider-row"><span class="slider-label">位置</span>' +
                    '<span class="slider-value position-na" title="该机型不支持百分比定位（position_capable=false）">不支持百分比定位</span></div>';
            }
            html +=
                '<div class="slider-row"><span class="slider-label">速度</span>' +
                '<input type="range" class="speed-slider" min="' + escapeHtml(speedMin) + '" max="' + escapeHtml(speedMax) + '" value="' + escapeHtml(speedValid ? speedEntity.state : speedMin) + '"' +
                ' oninput="this.nextElementSibling.textContent=this.value' + (speedUnit ? '+\'' + jsAttr(speedUnit) + '\'' : '') + '"' +
                ' onchange="controlDevice(\'' + jsAttr(devId) + '\',\'set_speed\',\'' + jsAttr(entryId) + '\', this.value)">' +
                '<span class="slider-value speed-value">' + (speedValid ? escapeHtml(speedEntity.state + speedUnit) : '--') + '</span></div>' +
                '<div class="slider-row"><span class="slider-label">力度</span>' +
                '<input type="range" class="strength-slider" min="' + escapeHtml(strengthMin) + '" max="' + escapeHtml(strengthMax) + '" value="' + escapeHtml(strengthValid ? strengthEntity.state : strengthMin) + '"' +
                ' oninput="this.nextElementSibling.textContent=this.value' + (strengthUnit ? '+\'' + jsAttr(strengthUnit) + '\'' : '') + '"' +
                ' onchange="controlDevice(\'' + jsAttr(devId) + '\',\'set_strength\',\'' + jsAttr(entryId) + '\', this.value)">' +
                '<span class="slider-value strength-value">' + (strengthValid ? escapeHtml(strengthEntity.state + strengthUnit) : '--') + '</span></div>' +
                '</div>';

            // === 模式切换区（仅 5005）：内倒模式 / 平开模式 ===
            if (supportsWindLock) {
                html += '<div class="control-row">' +
                    '<button class="btn btn-mode-a btn-sm" style="flex:1;" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'wind_lock_tilt\',\'' + jsAttr(entryId) + '\')">内倒模式</button>' +
                    '<button class="btn btn-mode-b btn-sm" style="flex:1;" onclick="controlDevice(\'' + jsAttr(devId) + '\',\'wind_lock_flat\',\'' + jsAttr(entryId) + '\')">平开模式</button>' +
                    '</div>';
            }

            html += '</div>'; // close control-section
            html += '</div>'; // close device-item
            return html;
        }

        function loadDeviceState(dev, states) {
            const stateEl = document.getElementById('dev-state-' + dev.id);
            if (!stateEl) return;
            try {
                // 2026-08-28 修复：用 API 返回的精确实体列表按 unique_id 锚点查找
                const coverEntity = findEntityState(dev, 'cover', 'cover', states);
                // 在线状态点: binary_sensor.*online*
                const onlineEntity = findEntityState(dev, 'binary_sensor', 'online', states);
                const dot = document.querySelector('#dev-' + dev.id + ' .dev-dot');
                if (dot) {
                    if (onlineEntity) {
                        dot.className = 'dev-dot ' + (onlineEntity.state === 'on' ? 'dot-online' : 'dot-offline');
                    } else {
                        // v1.7.31（D-1）：旧 else-if 用 cover unavailable 判设备离线
                        // ——cover 被 v1.7.20 定案钉死 available=True 且子设备本就没有
                        // online 实体（全集成唯一 online 传感器挂在网关上），该判据恒
                        // 假绿：设备断电失联仍亮绿点+陈旧状态，与 M-2 假绿灯纠偏同族。
                        // 改走业务真值源：status 传感器上报驱动、超时效转 unknown
                        // （sensor.py v1.6.12 陈旧判定）——无新鲜上报一律灰点未知。
                        const statusEntity = findEntityState(dev, 'sensor', 'status', states);
                        const fresh = !!statusEntity && !!statusEntity.state &&
                            statusEntity.state !== 'unknown' &&
                            statusEntity.state !== 'unavailable';
                        dot.className = 'dev-dot ' + (fresh ? 'dot-online' : 'dot-unknown');
                    }
                }
                // 电池电压: sensor.*battery*
                const batteryEntity = findEntityState(dev, 'sensor', 'battery', states);
                // 速度/力度: number.*speed* / number.*strength*
                const speedEntity = findEntityState(dev, 'number', 'speed', states);
                const strengthEntity = findEntityState(dev, 'number', 'strength', states);
                // 更新速度/力度滑块：始终可用（与 HA 集成 number 实体一致），
                // 有上报数据时回显当前值，无数据时保持默认值 0 且可拖动。
                const speedSlider = document.querySelector('#dev-' + dev.id + ' .speed-slider');
                if (speedSlider) {
                    speedSlider.disabled = false;
                    if (speedEntity && speedEntity.state !== 'unknown' && speedEntity.state !== 'unavailable'
                            && !userInteracting(speedSlider)) {
                        const unit = speedEntity.attributes.unit_of_measurement || '';
                        speedSlider.value = speedEntity.state;
                        const valEl = document.querySelector('#dev-' + dev.id + ' .speed-value');
                        if (valEl) valEl.textContent = speedEntity.state + unit;
                    }
                }
                const strengthSlider = document.querySelector('#dev-' + dev.id + ' .strength-slider');
                if (strengthSlider) {
                    strengthSlider.disabled = false;
                    if (strengthEntity && strengthEntity.state !== 'unknown' && strengthEntity.state !== 'unavailable'
                            && !userInteracting(strengthSlider)) {
                        const unit = strengthEntity.attributes.unit_of_measurement || '';
                        strengthSlider.value = strengthEntity.state;
                        const valEl = document.querySelector('#dev-' + dev.id + ' .strength-value');
                        if (valEl) valEl.textContent = strengthEntity.state + unit;
                    }
                }
                // 状态文本
                let statusText;
                if (coverEntity) {
                    const pos = coverEntity.attributes.position;
                    const state = coverEntity.state;
                    const devStatus = coverEntity.attributes.device_status;
                    if (state === 'open') statusText = '状态: 打开';
                    else if (state === 'closed' || state === 'close') statusText = '状态: 关闭';
                    else if (state === 'opening') statusText = '状态: 正在打开';
                    else if (state === 'closing') statusText = '状态: 正在关闭';
                    // v1.6.8：state 为 unknown 时按 device_status 属性兜底，
                    // 避免直接暴露英文 unknown/unavailable 给用户
                    else if (devStatus === 'open') statusText = '状态: 打开';
                    else if (devStatus === 'closed') statusText = '状态: 关闭';
                    // 用户定案：状态与位置同步——status 缺失但上报了 position 时，
                    // 按位置推导（0=关闭，>0=打开），避免「待上报 + 位置 65%」矛盾
                    else if (pos !== undefined && pos !== null && pos !== '') statusText = '状态: ' + (Number(pos) > 0 ? '打开' : '关闭');
                    else if (state === 'unavailable') statusText = '状态: 离线';
                    else if (devStatus === 'connected' || state === 'unknown' || devStatus === 'unknown') statusText = '状态: 待上报';
                    else statusText = '状态: ' + state;
                    // v1.7.12（审计 L-2）：position=null 实体（unavailable 时属性
                    // 可为 null）旧判只挡 undefined，会渲染成"位置: null%"并把
                    // slider.value 置成 "null"（数字输入被浏览器钳成 0）——同族
                    // 判空补齐 null/''（与上方 :565 推导分支同口径）
                    if (pos !== undefined && pos !== null && pos !== '') statusText += ' | 位置: ' + pos + '%';
                    const slider = document.querySelector('#dev-' + dev.id + ' .position-slider');
                    if (slider && pos !== undefined && pos !== null && pos !== ''
                            && !userInteracting(slider)) {
                        slider.value = pos;
                        slider.nextElementSibling.textContent = pos + '%';
                    }
                } else {
                    statusText = '状态: 无数据';
                }
                if (onlineEntity) statusText += ' | ' + (onlineEntity.state === 'on' ? '在线' : '离线');
                if (batteryEntity && batteryEntity.state && batteryEntity.state !== 'unknown' && batteryEntity.state !== 'unavailable') {
                    const bUnit = batteryEntity.attributes.unit_of_measurement || 'V';
                    statusText += ' | 电压: ' + batteryEntity.state + bUnit;
                } else if (coverEntity && coverEntity.attributes.voltage) {
                    statusText += ' | 电压: ' + coverEntity.attributes.voltage + 'V';
                }
                stateEl.textContent = statusText;
            } catch (e) {
                stateEl.textContent = '状态: 加载失败';
            }
        }

        async function startPairing(entryId) {
            if (!confirm('确定要启动网关配对模式吗？\n配对期间请将子设备靠近网关。')) return;
            const statusEl = document.getElementById('gw-status-' + entryId);
            updateGatewayStatus(statusEl, 'pairing');
            // v1.6.3：SN 实时读 map，不再用渲染时烘焙的旧值/占位符
            const gatewaySn = GATEWAY_SN_BY_ENTRY[entryId] || '';
            // 审计 2026-09-30 G-1：进场时是否已有一个**未过期**的配对窗口。
            // 失败分支原本无条件 delete PAIRING_UNTIL：30s 后重复点「配对」而这次
            // HA 调用失败时，它删的是**上一个仍在生效**的窗口 ⇒ 服务端还在配对，
            // 徽标却回到"在线/未知"，与 G-1 要防的误判同源。有活窗口就保留它和它的
            // 定时器，照常显示"配对中"；没有才按失败清理。
            const prevLive = PAIRING_UNTIL[entryId] && Date.now() < PAIRING_UNTIL[entryId];
            // 与上面同一目的：让出点之前先记下，失败时决定"清"还是"留"。
            const abortPairing = () => {
                if (prevLive) { paintStatus('pairing'); return; }
                delete PAIRING_UNTIL[entryId];
                if (PAIRING_TIMERS[entryId]) {
                    clearTimeout(PAIRING_TIMERS[entryId].quick);
                    clearTimeout(PAIRING_TIMERS[entryId].close);
                    delete PAIRING_TIMERS[entryId];
                }
                // v1.6.3：失败不硬标 online（设备可能确实离线），回到未知由下次刷新判定
                paintStatus('unknown');
            };
            // 审计 2026-09-30 G-2：徽标一律**按 id 重取**后再写。上方 :1202 抓的
            // statusEl 是两次 await 之前的引用，容器一旦在往返期间被整体重建
            // （30s 静默刷新/手动刷新/needRebuild），写回就落在孤儿节点上——
            // 而 updateGatewayStatus 的守卫只护 badge-warn，此时页面是 badge-ok，
            // 于是"配对成功了却恒显示在线"，要等下一轮刷新才纠正。
            // 与 loadGatewayDevices/updateGatewayDevices 的 await 后重取同口径。
            const paintStatus = (kind) =>
                updateGatewayStatus(document.getElementById('gw-status-' + entryId), kind);
            try {
                const resp = await haApi('/window_controller_gateway/devices?config_entry_id=' + entryId);
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                const devices = await resp.json();
                const gwDevice = devices.find(d => !d.via_device_id);
                if (!gwDevice) { showToast('未找到网关设备', 'err'); abortPairing(); return; }
                const pairResp = await haApi('/services/window_controller_gateway/start_pairing', 'POST', {
                    device_id: gwDevice.id, duration: 60
                });
                if (!pairResp.ok) throw new Error('HA API ' + pairResp.status);
                // v1.7.12（L-3）：配对服务确认受理后才开窗（60s 配对 + 5s 收尾），
                // 窗口结束时主动刷新一次恢复正常徽标判定
                PAIRING_UNTIL[entryId] = Date.now() + 65000;
                // 受理成功后立刻用**新引用**补写一次：若上方两次 await 期间发生过
                // 重建，此刻把徽标从"在线/检测中"纠正回"配对中"（此后窗口内的重建
                // 由 :746 的 PAIRING_UNTIL 判据自己渲染对）。
                paintStatus('pairing');
                showToast('配对模式已启动（60秒），请操作子设备', 'ok');
                // 审计 2026-09-30 G-1：两个 setTimeout 的句柄必须存下来并在重入时清。
                // 旧写法句柄一丢、startPairing 又没有重入闸：30s 后再点一次「配对」
                // （新窗口应到 t=95s），t=60s 时**上一次的**回调照样执行，把新窗口
                // delete 掉并重建列表 ⇒ 配对服务端仍生效的 30 多秒里徽标显示"在线"，
                // 用户误判失败反复点击——正是 :14-18 注释要防的场景。
                if (PAIRING_TIMERS[entryId]) {
                    clearTimeout(PAIRING_TIMERS[entryId].quick);
                    clearTimeout(PAIRING_TIMERS[entryId].close);
                }
                PAIRING_TIMERS[entryId] = {
                    quick: setTimeout(() => loadGatewayDevices(entryId, gatewaySn), 10000),
                    close: setTimeout(() => {
                        delete PAIRING_UNTIL[entryId];
                        delete PAIRING_TIMERS[entryId];
                        loadGatewayDevices(entryId, gatewaySn);
                    }, 60000)
                };
            } catch (e) {
                showToast('配对启动失败: ' + e.message, 'err');
                abortPairing();
            }
        }

        async function checkGatewayStatus(entryId) {
            // v1.6.3：SN 实时读 map；拿不到真实 SN 时明确报错，
            // 不再把占位符发给服务端静默 no-op 后弹「已发送」假成功
            const gatewaySn = GATEWAY_SN_BY_ENTRY[entryId] || '';
            if (!gatewaySn || gatewaySn === '未知') {
                showToast('该网关 SN 尚未识别，无法检查状态（稍后重试或点配对刷新）', 'err');
                return;
            }
            showToast('正在检查网关状态...', 'warn');
            try {
                const resp = await haApi('/services/window_controller_gateway/check_gateway_status', 'POST', { gateway_sn: gatewaySn });
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                showToast('状态检查已发送', 'ok');
                // v1.7.5：状态检查后走静默更新（不 innerHTML 重建）——重建会
                // 重放 .device-item fadeUp 入场动画 + backdrop-filter 重排，
                // 手机端慢渲染＝控制后"闪一下"根因；桌面端快所以无感
                setTimeout(() => updateGatewayDevices(entryId, gatewaySn), 2000);
            } catch (e) {
                showToast('状态检查失败: ' + e.message, 'err');
                // v1.6.4：失败（含服务端"未找到网关"非 2xx）不保留旧徽标结论，
                // 回到"未知"由下次刷新重新判定
                updateGatewayStatus(document.getElementById('gw-status-' + entryId), 'unknown');
            }
        }

        async function renameDevice(deviceId, entryId, currentName) {
            const newName = prompt('请输入新设备名称:', currentName);
            if (!newName || newName === currentName) return;
            // v1.7.12（第 6 轮审计 L-6）：客户端先行校验——旧版把纯空白/超长/
            // 含控制符的名字直发 HA 服务（服务端 name 只校 str 非空），
            // 设备最终显示空白名。trim 后判空 + 限 64 字 + 剔除控制字符。
            const cleaned = newName.replace(/[\u0000-\u001f\u007f]/g, '').trim();
            if (!cleaned) { showToast('名称不能为空', 'err'); return; }
            if (cleaned.length > 64) { showToast('名称过长（最多 64 字）', 'err'); return; }
            if (cleaned === currentName) return;
            showToast('正在重命名...', 'warn');
            try {
                const resp = await haApi('/services/window_controller_gateway/rename_device', 'POST', {
                    device_id: deviceId, name: cleaned
                });
                if (!resp.ok) throw new Error('HA API ' + resp.status);
                showToast('重命名成功: ' + cleaned, 'ok');
                setTimeout(() => loadGatewayDevices(entryId, GATEWAY_SN_BY_ENTRY[entryId] || ''), 2000);
            } catch (e) {
                showToast('重命名失败: ' + e.message, 'err');
            }
        }

        // v1.6.3：删除 transferDevice/refreshDevices 死代码——
        // 二者无任何调用点（页面无按钮），且字段有误：transfer_device 服务
        // 参数为 new_gateway_sn（services.yaml），旧代码发 target_gateway_sn；
        // refresh_devices 需要必填 device_id，旧代码发空 payload 必然 400。
        // 需要该功能时按正确字段重新接线，勿直接恢复旧实现。

        async function controlDevice(deviceId, command, entryId, value) {
            if (command === 'remove' && !confirm('确定要删除该子设备吗？\n将移除该设备在 HA 中的全部实体。')) return;
            showToast('正在发送命令: ' + command + '...', 'warn');
            try {
                // v1.6.3：删除无用的全量 /states 拉取——本函数只按 devices API
                // 的实体列表定位；旧实现拉回后从未使用，且 !ok 抛错会拦死控制
                const devResp = await haApi('/window_controller_gateway/devices?config_entry_id=' + entryId);
                if (!devResp.ok) throw new Error('HA API ' + devResp.status);
                const devices = await devResp.json();
                const dev = devices.find(d => d.id === deviceId);
                // 2026-08-28 修复：用 API 返回的精确实体列表按 unique_id 锚点查找，
                // 不再用 SN 后 6 位模糊匹配 entity_id（设备名只有后 4 位，模糊匹配必然失败）。
                const coverEntity = findEntityByUniqueId(dev, 'cover', 'cover');
                if (command === 'open' || command === 'close' || command === 'stop') {
                    if (!coverEntity) {
                        showToast('未找到设备 cover 实体', 'warn');
                    } else {
                        let service = '';
                        if (command === 'open') service = 'open_cover';
                        else if (command === 'close') service = 'close_cover';
                        else if (command === 'stop') service = 'stop_cover';
                        const r = await haApi('/services/cover/' + service, 'POST', { entity_id: coverEntity.entity_id });
                        if (!r.ok) throw new Error('HA API ' + r.status);
                        showToast('命令已发送: ' + command, 'ok');
                    }
                } else if (command === 'a') {
                    // 内倒（toggle）：与 wind_lock 相同模式，按 button 实体 unique_id 后缀 _a 查找
                    const btnEntity = findEntityByUniqueId(dev, 'button', 'a');
                    if (btnEntity) {
                        const r = await haApi('/services/button/press', 'POST', { entity_id: btnEntity.entity_id });
                        if (!r.ok) throw new Error('HA API ' + r.status);
                        showToast('内倒命令已发送', 'ok');
                    } else {
                        showToast('未找到内倒按钮实体', 'warn');
                    }
                } else if (command === 'wind_lock_tilt' || command === 'wind_lock_flat') {
                    const btnSuffix = command === 'wind_lock_tilt' ? 'wind_lock_tilt' : 'wind_lock_flat';
                    const btnEntity = findEntityByUniqueId(dev, 'button', btnSuffix);
                    if (btnEntity) {
                        const r = await haApi('/services/button/press', 'POST', { entity_id: btnEntity.entity_id });
                        if (!r.ok) throw new Error('HA API ' + r.status);
                        showToast('模式切换: ' + (command === 'wind_lock_tilt' ? '内倒' : '平开'), 'ok');
                    } else {
                        showToast('未找到模式按钮实体', 'warn');
                    }
                } else if (command === 'set_speed' || command === 'set_strength') {
                    const numSuffix = command === 'set_speed' ? 'speed' : 'strength';
                    const numEntity = findEntityByUniqueId(dev, 'number', numSuffix);
                    if (numEntity) {
                        const numVal = parseFloat(value);
                        if (isNaN(numVal)) throw new Error('无效数值: ' + value);
                        const r = await haApi('/services/number/set_value', 'POST', { entity_id: numEntity.entity_id, value: numVal });
                        if (!r.ok) throw new Error('HA API ' + r.status);
                        showToast((command === 'set_speed' ? '速度' : '力度') + '设置: ' + value, 'ok');
                    } else {
                        showToast('未找到' + (command === 'set_speed' ? '速度' : '力度') + '实体', 'warn');
                    }
                } else if (command === 'remove') {
                    // 删除按钮实体挂在网关设备下（不在子设备实体列表），
                    // 用 findRemoveButtonEntity 在整个设备列表中按 unique_id 锚点查找
                    const btnEntity = findRemoveButtonEntity(dev, devices);
                    if (btnEntity) {
                        const r = await haApi('/services/button/press', 'POST', { entity_id: btnEntity.entity_id });
                        if (!r.ok) throw new Error('HA API ' + r.status);
                        showToast('删除命令已发送', 'ok');
                    } else {
                        showToast('未找到删除按钮实体', 'warn');
                    }
                } else {
                    showToast('未知命令: ' + command, 'warn');
                }
                // v1.7.5：控制后走静默更新，理由同状态检查（手机端闪一下修复）；
                // 设备集合若有增删，updateGatewayDevices 内部自动升级完整重建
                setTimeout(() => updateGatewayDevices(entryId, GATEWAY_SN_BY_ENTRY[entryId] || ''), 2000);
            } catch (e) {
                showToast('控制失败: ' + e.message, 'err');
            }
        }

        async function controlDevicePosition(deviceId, position, entryId) {
            try {
                // v1.6.3：钳制 0-100 并拒绝 NaN（服务 schema 范围 0-100，越界会被 400 拒绝）
                const pos = Math.round(Number(position));
                if (!Number.isFinite(pos) || pos < 0 || pos > 100) {
                    showToast('无效位置: ' + position, 'err');
                    return;
                }
                // v1.6.3：删除无用的全量 /states 拉取（同上，仅用 devices API 实体列表）
                const devResp = await haApi('/window_controller_gateway/devices?config_entry_id=' + entryId);
                if (!devResp.ok) throw new Error('HA API ' + devResp.status);
                const devices = await devResp.json();
                const dev = devices.find(d => d.id === deviceId);
                // 2026-08-28 修复：精确查找 cover 实体（仅作存在性判断，
                // 实际服务为 window_controller_gateway.set_position）
                const coverEntity = findEntityByUniqueId(dev, 'cover', 'cover');
                if (coverEntity) {
                    // v1.7.33（全量审计）：能力前置判——从 HA states 读 cover 的
                    // attributes.position_capable（后端服务侧已同批加闸，这里把
                    // 失败提前到点击时，省一次必然 500 的往返并给出可读原因）。
                    // 读失败按放行处理，最终仍由服务侧闸兜底。
                    try {
                        const stResp = await haApi('/states/' + encodeURIComponent(coverEntity.entity_id));
                        if (stResp.ok) {
                            const st = await stResp.json();
                            const cap = st && st.attributes && st.attributes.position_capable;
                            if (cap === false) {
                                showToast('该机型不支持百分比定位', 'warn');
                                return;
                            }
                        }
                    } catch (capErr) { /* 能力探测失败不阻断：服务侧闸为准 */ }
                    // 实际服务为 window_controller_gateway.set_position：
                    // 字段 device_id（设备注册表ID）+ position
                    const r = await haApi('/services/window_controller_gateway/set_position', 'POST', {
                        device_id: deviceId, position: pos
                    });
                    if (!r.ok) {
                        // v1.7.33：失败时带出服务端原因（如"机型不支持百分比
                        // 定位"），不再只回裸状态码。showToast 走 textContent，无注入面。
                        let detail = 'HA API ' + r.status;
                        try {
                            const body = await r.text();
                            if (body) detail = body.length > 160 ? body.slice(0, 160) : body;
                        } catch (readErr) { /* 读不到体则退回状态码 */ }
                        throw new Error(detail);
                    }
                    showToast('位置设置: ' + pos + '%', 'ok');
                } else {
                    showToast('未找到设备 cover 实体', 'warn');
                }
            } catch (e) {
                showToast('位置设置失败: ' + e.message, 'err');
            }
        }

        // ========== 检查更新 ==========
        // v1.6.4：拆出 fetchLatestRelease() 核心——自动静默检查与手动检查
        // 共用同一数据源逻辑。v1.6.7 起：Gitee+GitHub 双源并集取版本号最大者
        // （不信 /releases/latest 的过期数据；也不单信 Gitee——其 Release 需
        // 手动同步、版本会陈旧），tag 必须匹配 数字.数字[.数字...]，
        // 防 1.6.4-beta 之类后缀让 compareVersions 出 NaN。
        async function fetchLatestRelease() {
            // v1.6.7：双源并集取最大。旧逻辑「Gitee 有数据就只用 Gitee」有洞：
            // gitee Release 并非 CI 自动创建（只有 GitHub 有），gitee 最大版本
            // 会陈旧（2026-08-29 实测停在 1.3.0），把「最新发布版」误判成旧值、
            // 升级徽章永不点亮。现两源都拉、合并取最大版本号，单源失败不互碍。
            // GitHub 先入列：同版本时优先取其条目（html_url 详情页更全）。
            const all = [];
            try {
                const ghResp = await fetchT(INGRESS_BASE + 'api/github/repos/fangwenyi-dev/ha-gateway-plugin/releases?per_page=100', { cache: 'no-store' }, 20000);
                if (ghResp.ok) {
                    const d = await ghResp.json();
                    if (Array.isArray(d)) all.push(...d);
                }
            } catch (e) { /* GitHub 限流/异常，Gitee 仍可独立工作 */ }
            try {
                // v1.7.31（现场批次）：Gitee API 默认按创建**升序**返回——release
                // 数超过 per_page 后，第一页只有最旧 100 条（0918 实锤：首页最大
                // v1.7.15，人工补齐的 v1.7.25~30 全在窗外），徽章 Gitee 面永远
                // 看不到新版。direction=desc 让最新进第一页（实测首页即 v1.7.30）。
                const giteeResp = await fetchT(INGRESS_BASE + 'api/gitee/repos/fangwenyi-dev/ha-gateway-plugin/releases?per_page=100&direction=desc', { cache: 'no-store' }, 20000);
                if (giteeResp.ok) {
                    const d = await giteeResp.json();
                    if (Array.isArray(d)) all.push(...d);
                }
            } catch (e) { /* Gitee 网络异常，GitHub 仍可独立工作 */ }
            if (all.length === 0) throw new Error('更新源不可用（Gitee/GitHub 均无数据）');
            let latestVersion = '0.0.0';
            let latestRelease = null;
            for (const r of all) {
                const ver = (r.tag_name || '').replace(/^v/, '');
                if (/^\d+(\.\d+)+$/.test(ver) && compareVersions(ver, latestVersion) > 0) {
                    latestVersion = ver;
                    latestRelease = r;
                }
            }
            if (!latestRelease) throw new Error('未找到任何 Release');
            return { latestVersion, latestRelease };
        }

        // 头部「检查更新」按钮徽章：发现新版变绿显示「⬆️ 有可用升级 vX.Y.Z」
        function renderUpdateBadge(latestVersion) {
            const btn = document.getElementById('checkUpdateBtn');
            if (!btn) return;
            if (latestVersion && compareVersions(latestVersion, CURRENT_VERSION) > 0) {
                btn.textContent = '⬆️ 有可用升级 v' + latestVersion;
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-success');
                btn.title = '点击查看详情与升级引导';
            } else {
                btn.textContent = '检查更新';
                btn.classList.remove('btn-success');
                btn.classList.add('btn-primary');
                btn.title = '';
            }
        }

        // 静默自动检查（init 时一次 + 每 30 分钟一次）：只更新徽章，
        // 不弹卡片不打扰；任何失败仅 console，不污染页面
        async function silentUpdateCheck() {
            // v1.6.9：三重限流（此前 10min×每标签页×双源都打 GitHub，多标签
            // 页逼近匿名 60/h/IP 限流）。① 跨标签页去重——localStorage 时间戳，
            // 5 分钟内其它标签已查过就跳过；② 页面不可见跳过（同 silentRefresh）；
            // ③ 复查间隔 10→30 分钟（升级徽章无需 10 分钟新鲜度）。
            // localStorage 读写包 try/catch：HA ingress 为三方 iframe，
            // 个别浏览器存储策略会直接抛异常，限流优化不能反杀检查本身
            if (document.hidden) return;
            try {
                const last = Number(localStorage.getItem('huijian_last_update_check') || 0);
                if (Date.now() - last < 5 * 60 * 1000) return;
                localStorage.setItem('huijian_last_update_check', String(Date.now()));
            } catch (e) { /* 存储不可用则退化为不去重 */ }
            try {
                const { latestVersion } = await fetchLatestRelease();
                renderUpdateBadge(latestVersion);
            } catch (e) {
                console.log('自动检查更新失败:', e.message);
                // 失败也保留时间戳，避免限流/断网时其他标签立刻重试放大压力
            }
        }

        async function checkForUpdates() {
            const card = document.getElementById('updateCard');
            const content = document.getElementById('updateContent');
            card.style.display = 'block';
            content.innerHTML = '<div class="loading"><div class="spinner"></div><p>正在检查更新...</p></div>';
            try {
                const { latestVersion, latestRelease } = await fetchLatestRelease();
                renderUpdateBadge(latestVersion);
                const hasUpdate = compareVersions(latestVersion, CURRENT_VERSION) > 0;
                if (hasUpdate) {
                    const body = latestRelease.body || '暂无变更日志';
                    const truncatedBody = body.length > 800 ? body.substring(0, 800) + '...' : body;
                    content.innerHTML = '<div class="update-box"><div class="update-info">' +
                        '<div class="update-title">发现新版本 v' + escapeHtml(latestVersion) + '</div>' +
                        '<div class="update-desc">当前版本 v' + CURRENT_VERSION + '</div></div>' +
                        '<button class="btn btn-success" onclick="doUpgrade()">去加载项页面更新</button>' +
                        '<a class="btn btn-primary" href="' + safeReleaseUrl(latestRelease.html_url) + '" target="_blank" rel="noopener noreferrer">查看详情</a></div>' +
                        '<div style="margin-top:10px;font-size:12px;color:var(--text-secondary);white-space:pre-wrap;">' + escapeHtml(truncatedBody) + '</div>' +
                        '<div style="margin-top:8px;font-size:12px;color:var(--text-secondary);">' +
                        '提示：Supervisor 安全设计禁止插件通过 API 自我更新（一键升级会返回 403/400）。' +
                        '请使用上方按钮跳转到 Supervisor 加载项页面，以管理员身份点击「更新」。' +
                        '<br>若跳转后提示「App huijian_mqtt_broker does not exist in the store」，' +
                        '说明本加载项已与加载项商店失去关联（安装后被删除仓库，或商店刷新失败——国内网络访问 GitHub 不通时常见）。' +
                        '恢复方法：设置 → 加载项 → 加载项商店 → ⋮ → 仓库，重新添加 ' +
                        'https://github.com/fangwenyi-dev/ha-gateway-plugin 并更新商店，再回到本页重试。</div>';
                } else {
                    content.innerHTML = '<div class="update-box update-ok"><div class="update-info">' +
                        '<div class="update-title">✅ 已是最新版本 v' + CURRENT_VERSION + '</div>' +
                        '<div class="update-desc">发布源最新版本: v' + escapeHtml(latestVersion) + '</div></div></div>';
                }
            } catch (e) {
                content.innerHTML = '<div class="update-box update-err"><div class="update-info">' +
                    '<div class="update-title">检查更新失败</div>' +
                    '<div class="update-desc">' + escapeHtml(e.message) + '</div></div></div>';
            }
        }

        // ========== 升级引导（跳转 Supervisor 加载项页面） ==========
        // Supervisor 安全设计（2025 年引入）禁止 add-on 通过 API 更新自己：
        //   - /addons/self/update 与 /store/addons/{slug}/update 均检查 REQUEST_FROM，
        //     插件 token 调自己必然 403（"can't update itself"）；
        //   - hassio.addon_update（HA Core 服务）实测返回 400 空响应（add-on token
        //     调 HA Core 服务 API 权限不足，aiohttp HTTPBadRequest）。
        // 因此"一键升级"在官方设计上不可行，唯一可靠路径是管理员在
        // Supervisor 加载项页面点击「更新」（设置 → 加载项 → 慧尖 LoRa 网关）。
        // 本函数直接跳转到该页面，不再发起注定失败的 API 调用。
        // v1.6.16 补充：新版 Supervisor（App 架构）中，已安装加载项的详情页与
        // 「更新」接口都依赖商店条目（App.app_store = store.get(slug)）；若客户
        // 删除过仓库或商店刷新失败，跳转后会报
        // "App huijian_mqtt_broker does not exist in the store"（StoreAppNotFoundError）。
        // 这不是本插件的 bug，须重新添加仓库并更新商店才能恢复，故在文案中给出指引。
        async function doUpgrade() {
            const openAddon = confirm(
                'Supervisor 安全设计禁止插件通过 API 自我更新（一键升级会返回 403/400），\n' +
                '请以管理员身份在 Supervisor 加载项页面点击「更新」。\n\n' +
                '现在为你打开加载项页面？\n\n' +
                '若打开后提示 "does not exist in the store"：请到 设置→加载项→\n' +
                '加载项商店→⋮→仓库 重新添加插件仓库并更新商店后再试。'
            );
            if (openAddon) {
                // ingress iframe 的 origin 即 HA 前端 origin，可跳转到加载项详情页
                window.open(window.location.origin + '/hassio/addon/huijian_mqtt_broker', '_blank');
            }
        }

        // ========== 工具函数 ==========
        function compareVersions(v1, v2) {
            const parts1 = v1.split('.').map(Number);
            const parts2 = v2.split('.').map(Number);
            for (let i = 0; i < Math.max(parts1.length, parts2.length); i++) {
                const p1 = parts1[i] || 0, p2 = parts2[i] || 0;
                if (p1 > p2) return 1;
                if (p1 < p2) return -1;
            }
            return 0;
        }

        function escapeHtml(text) {
            if (text === null || text === undefined) return '';
            return String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
        }

        // ========== 实体精确定位（2026-08-28 修复） ==========
        // 背景：Web UI 之前用设备 SN 后 6 位模糊匹配 entity_id，但设备显示名
        // 只含 SN 后 4 位（"开窗器 1207-0603 (#01)"），HA 生成的 entity_id
        // 里没有后 6 位 → 永远匹配不上 → "未找到设备 cover 实体"。
        // 现在 API（/window_controller_gateway/devices）为每个设备返回精确实体
        // 列表（entity_id/domain/unique_id），这里按 unique_id 精确查找：
        //   unique_id 格式：{gateway_sn}_{device_sn}_{suffix}
        // 用 device_sn（设备 SN）做唯一锚点匹配，天然精确、无前缀歧义。
        // 从 HA 设备 identifiers 中取本集成的 SN（v1.6.3）：
        // identifiers 是 (config_entry 命名空间, 值) 的集合，顺序不保证且可能
        // 含其它命名空间（如 mqtt 发现条目的 (DOMAIN, topic) 形式），
        // 取 identifiers[0] 是位置假设——必须按本集成 DOMAIN 匹配。
        function deviceSnOf(dev) {
            if (!dev || !Array.isArray(dev.identifiers)) return null;
            for (const ident of dev.identifiers) {
                if (Array.isArray(ident) && ident.length > 1 && ident[0] === DOMAIN) return ident[1];
            }
            return null;
        }

        function findEntityByUniqueId(dev, domain, suffix) {
            if (!dev || !dev.entities || !dev.entities.length) return null;
            const deviceSn = deviceSnOf(dev);
            if (!deviceSn) return null;
            // 常规实体 unique_id 为 {gw}_{device_sn}_{suffix}，锚点 _ {device_sn} _ {suffix}。
            // 删除按钮 special：unique_id 为 {gw}_remove_{device_sn}（remove 在 device_sn 前，
            // gateway.py:228），锚点应为 _remove_ {device_sn}——2026-08-28 修复。
            const anchors = [
                '_' + deviceSn + '_' + suffix,   // 常规布局
                '_' + suffix + '_' + deviceSn,   // 删除按钮布局（{gw}_remove_{sn}）
            ];
            for (const e of dev.entities) {
                if (e.domain !== domain || !e.unique_id) continue;
                if (anchors.some(a => e.unique_id.endsWith(a))) return e;
            }
            return null;
        }


        // 删除按钮实体特殊归属：GatewayDeviceRemoveButton.device_info 用网关 SN
        // （gateway.py identifiers={(DOMAIN, gateway_sn)}），实体挂在网关设备下，
        // 不在子设备 dev.entities 里。v1.5.5 起 remove 分支只在子设备实体列表中
        // 按 unique_id 查找，导致恒报"未找到删除按钮实体"（v1.5.9 双锚点仍未
        // 跳出子设备实体列表，方向不对）。
        // 修复：先按常规路径在子设备实体列表中找（兼容未来实体归属调整），
        // 找不到时用目标子设备 SN 构造 _remove_{sn} 锚点（删除按钮 unique_id
        // 固定为 {gw}_remove_{sn}，gateway.py:228），在 API 返回的整个设备列表
        // （网关 parent + 子设备）中搜索——一定能命中网关设备下的删除按钮。
        function findRemoveButtonEntity(dev, devices) {
            const direct = findEntityByUniqueId(dev, 'button', 'remove');
            if (direct) return direct;
            const deviceSn = deviceSnOf(dev);
            if (!deviceSn || !devices || !devices.length) return null;
            const anchor = '_remove_' + deviceSn;
            for (const d of devices) {
                if (!d || !d.entities) continue;
                const hit = d.entities.find(e =>
                    e.domain === 'button' && e.unique_id && e.unique_id.endsWith(anchor)
                );
                if (hit) return hit;
            }
            return null;
        }

        // 按 unique_id 锚点找到实体，再返回其在 states 中对应的 state 对象
        // （含 entity_id/state/attributes，供渲染与控制使用）。
        // 找不到实体或 states 中无此实体时返回 null。
        function findEntityState(dev, domain, suffix, states) {
            const ent = findEntityByUniqueId(dev, domain, suffix);
            if (!ent || !ent.entity_id) return null;
            return states.find(s => s.entity_id === ent.entity_id) || null;
        }

        // HTML 解析器会在 JS 编译前解码 &#39;，实体转义保护不了单引号包裹的 onclick/onchange 参数，需额外转义
        function jsQuote(v) {
            // v1.7.12（第 6 轮审计 L-9）：补行终止符转义——设备名可含 \n/\r，
            // HTML 属性会把换行折叠成空格导致 onclick 实参与原值静默不一致；
            // \u2028/\u2029 是 JS 行终止符，出现在单引号串内直接 SyntaxError
            // 整条 onclick 失效。转义在反斜杠之后（顺序不可颠倒）。
            return String(v == null ? '' : v)
                .replace(/\\/g, '\\\\')
                .replace(/'/g, "\\'")
                .replace(/\r/g, '\\r')
                .replace(/\n/g, '\\n')
                .replace(/\u2028/g, '\\u2028')
                .replace(/\u2029/g, '\\u2029');
        }

        // onclick/onchange 等事件属性的正确转义顺序（v1.6.3 修复）：
        // 必须先 jsQuote（JS 层转义单引号）再 escapeHtml（HTML 属性层）。
        // 反序（jsQuote(escapeHtml(x))）时 `'` 已变成 `&#39;`，jsQuote 找不到
        // 裸引号可转义，而浏览器解析属性时会把 `&#39;` 还原成 `'` 再交给 JS 引擎
        // ——单引号字符串被闭合，用户可控值（设备昵称可重命名）可注入任意 JS。
        // 仅限事件属性；文本节点仍然只用 escapeHtml。
        function jsAttr(v) {
            return escapeHtml(jsQuote(v));
        }

        // v1.7.33：Release 链接协议白名单——escapeHtml 只挡引号逃逸，挡不住
        // `javascript:`/`data:` 协议（上游被劫持/镜像源被投毒时点击即同源执行）。
        // 非 https 的 github/gitee 地址一律不渲染为可点链接。
        function safeReleaseUrl(url) {
            const s = String(url == null ? '' : url);
            if (/^https:\/\/(github\.com|gitee\.com)\//.test(s)) return escapeHtml(s);
            return '#';
        }

        function showToast(message, type) {
            // v1.7.12（第 6 轮审计 M-3）：toast 是 fixed 定位互叠——3s 窗口内
            // 连续两个 toast（配对失败紧跟 rename 失败等）文字完全重叠不可读。
            // 先摘旧再上新（只可能少见 3s 前的旧提示，语义无损失），
            // 同文案 2s 内去重防连点刷屏。
            const now = Date.now();
            const last = showToast._last;
            if (last && last.msg === message && last.type === (type || 'ok')
                    && now - last.at < 2000) return;
            showToast._last = { msg: message, type: type || 'ok', at: now };
            document.querySelectorAll('.toast').forEach(el => el.remove());
            const toast = document.createElement('div');
            toast.className = 'toast toast-' + (type || 'ok');
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                toast.style.transition = 'opacity 0.3s';
                setTimeout(() => toast.remove(), 300);
            }, 3000);
        }

        // ========== 启动 ==========
        init();
        // 定时无感刷新：只更新状态值，不重建 DOM，避免页面闪烁。
        // v1.6.3：页面不可见（后台标签/手机锁屏内嵌页）时跳过——
        // 每轮每个网关各拉一次全量 /states，隐藏页轮询纯耗 Home Assistant 资源
        setInterval(() => {
            if (document.hidden) return;
            silentRefresh();
        }, 30000);
