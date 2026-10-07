# coding=utf-8
"""
如意影视聚合爬虫（内置去广告清洗）
- 单源（如意）
- 播放 M3U8 时自动通过本地代理清洗广告
- 检测方法：广告SPS编码模板 + PTS时间线连续性（指纹仅作辅助候选）
- 无需外部清洗服务器
- 无弹幕
- 缓存优化：只缓存最近一次播放的清洗结果，换源自动覆盖，无过期时间
"""
import sys
import time
import concurrent.futures
import re
import requests
from collections import Counter
from urllib.parse import urljoin, quote, urlparse, parse_qs, urlencode, urlunparse

sys.path.append('..')
from base.spider import Spider


class Spider(Spider):
    SOURCES = {
        '如意': {'name': '️如意', 'api': 'https://cj.rycjapi.com/api.php/provide/vod/from/rym3u8/'}
    }

    def init(self, extend=""):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        # ---------- 缓存（单条目，覆盖式） ----------
        self.cached_url = None      # 规范化后的URL
        self.cached_content = None  # 清洗后的m3u8内容

    # ==================== 清洗引擎 ====================
    MAX_REMOVE_RATIO = 0.25
    SHORT_SEG_THRESHOLD = 0.5  # 短于此阈值的片段视为缝合标记，予以去除
    # 广告时长指纹（如意站统一模板，已实测验证）
    AD_FINGERPRINTS = (
        (4.0, 4.0, 4.0, 4.0, 4.0),        # 5×4.0s —— 已验证 6 条链接
        (4.0, 4.0, 4.0, 4.0, 4.0, 2.0),   # 5×4.0s+2.0s —— 已验证 17056
    )
    FP_TOL = 0.05
    # PTS 连续性容差：候选组起始 PTS 与前一正片组结束时间间隔超过该值则视为独立编码（广告）
    PTS_JUMP_TOL = 30 * 90000  # 30 秒（90kHz 时钟）
    # 广告统一编码模板（profile, level, 分辨率）—— 9 条链接 11 个广告全部一致，正片从不匹配
    AD_SPS = (100, 31, (1280, 720))
    # 候选分组并行探测的并发数与截止时间：仅探测首片 4.0s 的候选组（实测全部广告首片均为 4.0s 整，
    # 8 条链接 11 个广告无一例外），探测量降至全量的 3%-45%。
    # 超过截止时间立即返回未完成的组（保守保留，不误删），保证总响应时长可控不超时。
    SCAN_WORKERS = 32
    SCAN_DEADLINE = 9.0
    # 本地时间窗：正片 PTS 与播放列表累计时长之间的偏移会随片长缓慢漂移
    # （编码时长舍入累积，长片可达数十秒），故候选组只与附近时间窗内
    # 探测组的偏移中位数比对；窗口内不足 MIN_REF 个参考时取最近的 MIN_REF 个。
    WINDOW_SEC = 900
    MIN_REF = 5

    def _split_playlist(self, content):
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        header = []
        i = 0
        while i < len(lines) and lines[i].startswith('#EXT') and \
                not lines[i].startswith(('#EXTINF', '#EXT-X-DISCONTINUITY')):
            header.append(lines[i])
            i += 1
        footer, end = [], len(lines)
        for j in range(len(lines) - 1, i - 1, -1):
            if lines[j] == '#EXT-X-ENDLIST':
                footer.append(lines[j])
                end = j
                break
        segs, cur, disc = [], [], False
        for l in lines[i:end]:
            if l == '#EXT-X-DISCONTINUITY':
                if cur:
                    segs.append((disc, cur))
                cur, disc = [], True
            else:
                cur.append(l)
        if cur:
            segs.append((disc, cur))
        out = []
        for d, s in segs:
            items, extras, k = [], [], 0
            while k < len(s):
                if s[k].startswith('#EXTINF') and k + 1 < len(s) and not s[k + 1].startswith('#'):
                    try:
                        dur = float(s[k].split(':', 1)[1].split(',')[0])
                    except (ValueError, IndexError):
                        dur = 0.0
                    items.append((dur, s[k], s[k + 1]))
                    k += 2
                else:
                    if s[k].startswith('#'):
                        extras.append(s[k])
                    k += 1
            if items or extras:
                out.append({'disc': d, 'items': items, 'extras': extras})
        return header, out, footer

    def _detect_fingerprint(self, segs):
        """基于广告时长指纹检测广告候选块（纯本地，0 请求）。
        如意站广告统一为 5×4.0s 或 5×4.0s+2.0s 的整齐短段。
        仅作候选，最终是否删除还需分辨率核验（见 _clean_m3u8）。"""
        ads = set()
        for i, s in enumerate(segs):
            durs = sorted(d for d, _, _ in s['items'])
            for fp in self.AD_FINGERPRINTS:
                if len(durs) == len(fp) and \
                        all(abs(a - b) <= self.FP_TOL for a, b in zip(durs, sorted(fp))):
                    ads.add(i)
                    break
        return ads

    # ---- H.264 SPS 分辨率解析（用于广告核验，仅下载片段头部几 KB） ----
    _HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}
    PROBE_TIMEOUT = (2, 2)  # (连接, 读取) 秒
    PROBE_HEAD_BYTES = 8192

    def _sps_clean(self, sps):
        """去除 H.264 emulation prevention bytes（00 00 03 -> 00 00）。"""
        out = bytearray()
        for b in sps:
            if len(out) >= 2 and out[-2] == 0 and out[-1] == 0 and b == 3:
                continue
            out.append(b)
        return bytes(out)

    def _sps_ue(self, bits, pos):
        """读 Exp-Golomb ue(v)，返回 (值, 新bit位置)。"""
        zero = 0
        while pos < len(bits) and bits[pos] == 0:
            zero += 1
            pos += 1
        if pos >= len(bits):
            return None, pos
        pos += 1
        val = 1
        for _ in range(zero):
            if pos >= len(bits):
                return None, pos
            val = (val << 1) | bits[pos]
            pos += 1
        return val - 1, pos

    def _sps_se(self, bits, pos):
        """读 Exp-Golomb se(v)（有符号）。"""
        ue, pos = self._sps_ue(bits, pos)
        if ue is None:
            return None, pos
        if ue & 1:
            return (ue + 1) // 2, pos
        return -ue // 2, pos

    def _sps_skip_scaling_list(self, bits, pos, size):
        last_scale = 8
        next_scale = 8
        for _ in range(size):
            if next_scale != 0:
                delta, pos = self._sps_se(bits, pos)
                if delta is None:
                    return pos
                next_scale = (last_scale + delta + 256) % 256
            last_scale = 8 if next_scale == 0 else next_scale
        return pos

    def _parse_sps_resolution(self, sps):
        """解析 SPS NAL 载荷（不含 NAL header），返回 (width, height) 或 None。"""
        if len(sps) < 4:
            return None
        try:
            sps = self._sps_clean(sps)
            bits = []
            for b in sps:
                bits.extend((b >> (7 - i)) & 1 for i in range(8))
            pos = 0
            profile_idc = sps[0]
            pos += 24  # profile_idc(8) + constraint flags(8) + level_idc(8)
            _, pos = self._sps_ue(bits, pos)  # seq_parameter_set_id
            if profile_idc in self._HIGH_PROFILES:
                chroma_format_idc, pos = self._sps_ue(bits, pos)
                if chroma_format_idc is None:
                    return None
                if chroma_format_idc == 3:
                    pos += 1  # separate_colour_plane_flag
                _, pos = self._sps_ue(bits, pos)  # bit_depth_luma_minus8
                _, pos = self._sps_ue(bits, pos)  # bit_depth_chroma_minus8
                pos += 1  # qpprime_y_zero_transform_bypass_flag
                if pos >= len(bits):
                    return None
                if bits[pos]:
                    pos += 1
                    n = 12 if chroma_format_idc == 3 else 8
                    for k in range(n):
                        if pos >= len(bits):
                            return None
                        if bits[pos]:
                            pos += 1
                            pos = self._sps_skip_scaling_list(bits, pos, 16 if k < 6 else 64)
                        else:
                            pos += 1
                else:
                    pos += 1
            _, pos = self._sps_ue(bits, pos)  # log2_max_frame_num_minus4
            poc_type, pos = self._sps_ue(bits, pos)
            if poc_type is None:
                return None
            if poc_type == 0:
                _, pos = self._sps_ue(bits, pos)
            elif poc_type == 1:
                pos += 1  # delta_pic_order_always_zero_flag
                _, pos = self._sps_se(bits, pos)
                _, pos = self._sps_se(bits, pos)
                num_ref, pos = self._sps_ue(bits, pos)
                if num_ref is None:
                    return None
                for _ in range(num_ref):
                    _, pos = self._sps_se(bits, pos)
            _, pos = self._sps_ue(bits, pos)  # max_num_ref_frames
            pos += 1  # gaps_in_frame_num_value_allowed_flag
            w_mbs, pos = self._sps_ue(bits, pos)
            h_mbs, pos = self._sps_ue(bits, pos)
            if w_mbs is None or h_mbs is None:
                return None
            return (w_mbs + 1) * 16, (h_mbs + 1) * 16
        except Exception:
            return None

    def _find_sps(self, data):
        """在 TS 字节流中找 SPS NALU（start code + 0x67），返回其载荷。"""
        i, n = 0, len(data)
        while i < n - 5:
            if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
                if (data[i + 3] & 0x1f) == 7:
                    j = i + 4
                    k = j
                    while k < n - 3:
                        if data[k] == 0 and data[k + 1] == 0 and \
                                (data[k + 2] == 1 or (k + 3 < n and data[k + 2] == 0 and data[k + 3] == 1)):
                            return data[j:k]
                        k += 1
                    return data[j:]
                i += 3
            elif data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
                if (data[i + 4] & 0x1f) == 7:
                    j = i + 5
                    k = j
                    while k < n - 3:
                        if data[k] == 0 and data[k + 1] == 0 and \
                                (data[k + 2] == 1 or (k + 3 < n and data[k + 2] == 0 and data[k + 3] == 1)):
                            return data[j:k]
                        k += 1
                    return data[j:]
                i += 4
            else:
                i += 1
        return None

    def _scan_groups(self, segs, base_url):
        """并行探测分组头部，返回 ({组号: (首个视频PTS, SPS信息)}, 样本组列表)。
        - 候选组 = 首片时长 4.0s±0.05 的组：实测如意站全部广告首片均为 4.0s 整
          （8 条链接 11 个广告无一例外），探测量降至全量的 3%-45%；
          正片组首片也可能是 4.0s，无妨——命中后仍用 SPS/PTS 判定，不会误删。
        - 样本组 = 全片均匀抽取约 24 组：仅用于计算全局时间线基线偏移与正片分辨率。
          正片占绝对多数且均匀分布，中位数不受个别广告/探测失败干扰
          （候选组中广告占比高或数量过少时，用候选组自身的中位数会被带偏）。
        提交顺序：样本铺开 → 候选铺开 → 补齐，截止截断时优先保住基线与全片覆盖；
        超时立即返回不阻塞响应。"""
        all_g = [gi for gi, s in enumerate(segs) if s['items']]
        if not all_g:
            return {}, []
        sstep = max(1, len(all_g) // 24)
        sample = all_g[::sstep]
        cand = [gi for gi in all_g
                if abs(segs[gi]['items'][0][0] - 4.0) <= 0.05]

        def spread(lst):
            step = max(1, len(lst) // 32)
            return lst[::step]

        seen, order = set(), []
        for lst in (sample, cand):
            for gi in spread(lst):
                if gi not in seen:
                    seen.add(gi)
                    order.append(gi)
        for gi in sample + cand:
            if gi not in seen:
                seen.add(gi)
                order.append(gi)

        heads = {}
        deadline = time.time() + self.SCAN_DEADLINE
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=self.SCAN_WORKERS)
        futs = {}
        try:
            for gi in order:
                futs[ex.submit(self._probe_head,
                               segs[gi]['items'][0][2], base_url)] = gi
            for f in concurrent.futures.as_completed(futs):
                if time.time() > deadline:
                    break  # 超时立即停止，剩余组保守保留
                heads[futs[f]] = f.result()
        finally:
            for fu in futs:
                fu.cancel()
            # 不等待运行中的探测线程，立即返回（线程数有限且各带 4s 超时，自行结束）
            ex.shutdown(wait=False)
        return heads, sample

    def _probe_head(self, uri, base_url):
        """下载片段头部，返回 (首个视频PTS, (profile, level, 分辨率)) 或 (None, None)。
        广告为独立编码插播，PTS 从接近 0 开始，SPS 匹配广告模板。使用默认请求头（无自定义）。"""
        url = uri if uri.startswith(('http://', 'https://')) else urljoin(base_url, uri)
        try:
            # 使用 requests 直接请求，不传递任何自定义 headers
            resp = requests.get(url, timeout=self.PROBE_TIMEOUT, stream=True)
            data = resp.raw.read(self.PROBE_HEAD_BYTES)
            pts = None
            i, n = 0, len(data)
            while i + 188 <= n:
                if data[i] != 0x47:
                    i += 1
                    continue
                pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
                afc = (data[i + 3] >> 4) & 0x3
                payload_start = i + 4
                if afc & 0x2:  # 有 adaptation field
                    af_len = data[i + 4]
                    payload_start = i + 5 + af_len
                if afc & 0x1 and payload_start + 14 <= i + 188:
                    if data[payload_start] == 0 and data[payload_start + 1] == 0 and \
                            data[payload_start + 2] == 1:
                        stream_id = data[payload_start + 3]
                        if 0xE0 <= stream_id <= 0xEF:  # 视频流
                            flags = data[payload_start + 7]
                            if flags & 0x80:  # 含 PTS
                                b = data[payload_start + 9: payload_start + 14]
                                pts = ((b[0] & 0x0E) << 29) | (b[1] << 22) | \
                                      ((b[2] & 0xFE) << 14) | (b[3] << 7) | (b[4] >> 1)
                                break
                i += 188
            sps = self._find_sps(data)
            sps_info = None
            if sps:
                clean = self._sps_clean(sps)
                if len(clean) >= 3:
                    res = self._parse_sps_resolution(sps)
                    sps_info = (clean[0], clean[2], res)
            return pts, sps_info
        except Exception:
            return None, None

    def _rewrite_extra(self, line, base_url):
        if 'URI="' in line:
            m = re.search(r'URI="([^"]+)"', line)
            if m and not m.group(1).startswith(('http://', 'https://')):
                return line.replace('URI="%s"' % m.group(1),
                                    'URI="%s"' % urljoin(base_url, m.group(1)))
        return line

    def _clean_m3u8(self, content, base_url):
        """返回清洗后的播放列表文本，去除广告片段。"""
        header, segs, footer = self._split_playlist(content)
        if not segs:
            return content

        total = sum(d for s in segs for d, _, _ in s['items'])

        # 广告时长指纹检测（纯本地，0 请求）→ 辅助候选
        fp = self._detect_fingerprint(segs)

        # 并行探测候选分组头部（PTS + SPS），受截止时间限制
        # （样本组已并入 heads，仅用于充当本地时间线参考点）
        heads, _ = self._scan_groups(segs, base_url)

        # 正片基线分辨率 = 探测成功的组中出现最多的分辨率
        res_cnt = Counter(info[2] for _, info in heads.values()
                          if info and info[2])
        baseline = res_cnt.most_common(1)[0][0] if res_cnt else None

        ads = set()
        if baseline:
            # 720p 正片：广告 SPS 与正片相同，只能靠 PTS 时间线区分
            is_720_film = (baseline == self.AD_SPS[2])

            # 本地时间线参考：广告为独立编码，PTS 与正片时间线相差数百秒；
            # 正片组满足 pts ≈ 本地偏移 + 播放列表累计时长。偏移随片长缓慢漂移
            # （长片可达数十秒），故取候选组附近 WINDOW_SEC 时间窗内探测成功组
            # 偏移的中位数作本地参考（正片占绝对多数，中位数抗广告/失败干扰）。
            cum, t = [], 0.0
            for s in segs:
                cum.append(t)
                t += sum(d for d, _, _ in s['items'])
            probed = sorted((cum[gi], heads[gi][0]) for gi in heads
                            if heads[gi][0] is not None)

            def local_jump(gi, pts):
                """候选组首PTS与本地正片时间线的偏离量（90kHz），无参考返回 None."""
                if pts is None or not probed:
                    return None
                c0 = cum[gi]
                idx = [k for k, (c, _p) in enumerate(probed)
                       if abs(c - c0) <= self.WINDOW_SEC]
                if len(idx) < self.MIN_REF:
                    idx = sorted(range(len(probed)),
                                 key=lambda k: abs(probed[k][0] - c0))[:self.MIN_REF]
                offs = sorted(p - int(c * 90000) for c, p in
                              (probed[k] for k in idx))
                med = offs[len(offs) // 2]
                return abs(pts - (med + int(c0 * 90000)))

            for gi, s in enumerate(segs):
                if not s['items']:
                    continue
                pts, info = heads.get(gi, (None, None))
                jump = local_jump(gi, pts)
                # 规则1：SPS 命中广告模板且分辨率与正片不同 → 广告
                # （正片非 720p 时广告必然命中；正片本身 720p 时该规则不触发）
                if info and info == self.AD_SPS and info[2] != baseline:
                    ads.add(gi)
                    continue
                # 规则2：720p 正片，PTS 偏离全局时间线 → 独立编码广告
                if is_720_film and jump is not None and jump > self.PTS_JUMP_TOL:
                    ads.add(gi)
                    continue
                # 规则3：指纹候选 + PTS 偏离全局时间线 → 同分辨率不规则广告兜底
                # （正片组 PTS 贴合时间线则保留，如片尾整齐片段 / 21分、44分正片）
                if (not is_720_film and gi in fp
                        and jump is not None and jump > self.PTS_JUMP_TOL):
                    ads.add(gi)
                    continue
        # 基线探测全失败：保守处理，不删除任何片段（避免误伤正片）

        removed = sum(d for i in ads for d, _, _ in segs[i]['items'])

        # 去除短片段（缝合标记）
        short_removed = 0.0
        for i, s in enumerate(segs):
            if i in ads:
                continue
            s['items'] = [(dur, inf, uri) for dur, inf, uri in s['items']
                          if dur >= self.SHORT_SEG_THRESHOLD]
            for dur, _, _ in s['items']:
                if dur < self.SHORT_SEG_THRESHOLD:
                    short_removed += dur

        # 保护：删除比例过高时放弃
        if total > 0 and (removed + short_removed) / total > self.MAX_REMOVE_RATIO:
            header, segs, footer = self._split_playlist(content)
            if not segs:
                return content
            removed, short_removed = 0.0, 0.0
            ads = set()

        # 重建播放列表
        body, maxdur, pending_disc = [], 0.0, False
        for i, s in enumerate(segs):
            if i in ads:
                pending_disc = True
                continue
            if not s['items']:
                pending_disc = True
                continue
            if (s['disc'] or pending_disc) and body:
                body.append('#EXT-X-DISCONTINUITY')
            pending_disc = False
            for l in s['extras']:
                body.append(self._rewrite_extra(l, base_url))
            for dur, inf, uri in s['items']:
                maxdur = max(maxdur, dur)
                body.append(inf)
                body.append(uri if uri.startswith(('http://', 'https://'))
                            else urljoin(base_url, uri))

        out = [self._rewrite_extra(l, base_url) for l in header
               if not l.startswith('#EXT-X-TARGETDURATION')]
        if not out or not out[0].startswith('#EXTM3U'):
            out.insert(0, '#EXTM3U')
        if not any(l.startswith('#EXT-X-VERSION') for l in out):
            out.append('#EXT-X-VERSION:3')
        if not any(l.startswith('#EXT-X-PLAYLIST-TYPE') for l in out):
            out.append('#EXT-X-PLAYLIST-TYPE:VOD')
        if not any(l.startswith('#EXT-X-MEDIA-SEQUENCE') for l in out):
            out.append('#EXT-X-MEDIA-SEQUENCE:0')
        out.append('#EXT-X-TARGETDURATION:%d' % max(1, int(maxdur + 0.999)))
        out.extend(body)
        out.extend(footer if footer else ['#EXT-X-ENDLIST'])
        return '\n'.join(out) + '\n'

    def _pick_variant(self, content, base_url):
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        best, best_bw = None, -1
        for i, l in enumerate(lines):
            if not l.startswith('#EXT-X-STREAM-INF'):
                continue
            m = re.search(r'BANDWIDTH=(\d+)', l)
            bw = int(m.group(1)) if m else 0
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith('#'):
                    if bw >= best_bw:
                        best, best_bw = urljoin(base_url, lines[j]), bw
                    break
        return best

    # ==================== URL 规范化（提高缓存命中） ====================
    def _normalize_url(self, url):
        """去除常见的随机查询参数，避免同一视频因不同随机参数产生不同缓存"""
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        # 移除常见的随机参数（可根据实际情况扩展）
        for param in ['_', 't', 'timestamp', 'rand', 'v', 'cb', 'callback']:
            query_params.pop(param, None)
        new_query = urlencode(query_params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    # ==================== 核心爬虫方法 ====================
    def build_url(self, base, query):
        if '?' in base:
            if base.endswith('?') or base.endswith('&'):
                return base + query
            else:
                return base + '&' + query
        else:
            return base + '?' + query

    def fetch_json(self, url, timeout=10):
        try:
            resp = self.session.get(url, headers=self.headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def clean_item(self, item, source_key, source_name, is_detail=False):
        if not is_detail:
            item['vod_id'] = f"{source_key}@@{item['vod_id']}"
        old_remark = item.get('vod_remarks', '')
        item['vod_remarks'] = f"{source_name} | {old_remark}" if old_remark else source_name
        item.pop('vod_down_from', None)
        item.pop('vod_down_url', None)
        return item

    def homeContent(self, filter=False):
        classes = []
        filters = {}
        def fetch_class(source_key, source_info):
            url = self.build_url(source_info['api'], "ac=list")
            data = self.fetch_json(url, timeout=4)
            return source_key, source_info, data
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_class, k, v): k for k, v in self.SOURCES.items()}
            for future in concurrent.futures.as_completed(futures):
                source_key, source_info, data = future.result()
                classes.append({'type_id': source_key, 'type_name': source_info['name']})
                filter_values = [{'n': '全部(最新)', 'v': ''}]
                if data and 'class' in data:
                    for c in data['class']:
                        filter_values.append({'n': c['type_name'], 'v': c['type_id']})
                filters[source_key] = [{'key': 'cateId', 'name': '分类', 'value': filter_values}]
        return {'class': classes, 'filters': filters, 'list': []}

    def homeVideoContent(self):
        return {'list': []}

    def categoryContent(self, tid, pg=1, filter=False, extend=None):
        if tid not in self.SOURCES:
            return {'list': [], 'page': pg, 'pagecount': 0, 'limit': 20, 'total': 0}
        source = self.SOURCES[tid]
        if extend is None:
            extend = {}
        real_tid = extend.get('cateId', '')
        query = f"ac=detail&pg={pg}"
        if real_tid:
            query += f"&t={real_tid}"
        url = self.build_url(source['api'], query)
        data = self.fetch_json(url, timeout=10)
        if not data:
            return {'list': [], 'page': pg, 'pagecount': 0, 'limit': 20, 'total': 0}
        video_list = []
        for item in data.get('list', []):
            video_list.append(self.clean_item(item, tid, source['name'], is_detail=False))
        return {
            'list': video_list,
            'page': data.get('page', pg),
            'pagecount': data.get('pagecount', 0),
            'limit': data.get('limit', 20),
            'total': data.get('total', 0)
        }

    def detailContent(self, ids):
        if not ids:
            return {'list': []}
        full_id = ids[0]
        if '@@' not in full_id:
            return {'list': []}
        source_key, real_id = full_id.split('@@', 1)
        if source_key not in self.SOURCES:
            return {'list': []}
        source = self.SOURCES[source_key]
        url = self.build_url(source['api'], f"ac=detail&ids={real_id}")
        data = self.fetch_json(url, timeout=10)
        if not data or 'list' not in data:
            return {'list': []}
        result_list = []
        for item in data['list']:
            cleaned = self.clean_item(item, source_key, source['name'], is_detail=True)
            cleaned['vod_id'] = full_id
            result_list.append(cleaned)
        return {'list': result_list}

    def searchContent(self, key, quick=False, pg=1):
        keyword = key.strip()
        if not keyword:
            return {'list': [], 'page': pg, 'pagecount': 0, 'limit': 40, 'total': 0}
        encoded_keyword = quote(keyword)
        def search_source(source_key, source_info):
            test_urls = [
                self.build_url(source_info['api'], f"ac=detail&wd={encoded_keyword}&pg={pg}"),
                self.build_url(source_info['api'], f"ac=list&wd={encoded_keyword}&pg={pg}")
            ]
            for url in test_urls:
                data = self.fetch_json(url, timeout=8)
                if not data:
                    continue
                items = None
                if isinstance(data, dict):
                    if 'list' in data:
                        items = data['list']
                    elif 'data' in data and isinstance(data['data'], dict) and 'list' in data['data']:
                        items = data['data']['list']
                    elif 'data' in data and isinstance(data['data'], list):
                        items = data['data']
                if items:
                    return source_key, source_info, data, items
            return source_key, source_info, None, []
        all_items = []
        max_pagecount = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(search_source, k, v): k for k, v in self.SOURCES.items()}
            for future in concurrent.futures.as_completed(futures):
                source_key, source_info, data, items = future.result()
                if items:
                    for item in items:
                        cleaned = self.clean_item(item, source_key, source_info['name'], is_detail=False)
                        all_items.append(cleaned)
                    if data and data.get('pagecount', 0) > max_pagecount:
                        max_pagecount = data.get('pagecount', 0)
        seen = set()
        unique = []
        for v in all_items:
            if v['vod_id'] not in seen:
                seen.add(v['vod_id'])
                unique.append(v)
        return {
            'list': unique,
            'page': pg,
            'pagecount': max_pagecount if max_pagecount else 1,
            'limit': 40,
            'total': 9999
        }

    # ==================== 播放与代理（内置清洗 + 单条目缓存） ====================
    def playerContent(self, flag, id, vipFlags=None):
        raw_url = id
        if '.m3u8' in raw_url:
            proxy_url = self.getProxyUrl(local=True) + f'&action=clean&src={quote(raw_url)}'
            return {
                'parse': 0,
                'url': proxy_url,
            }
        else:
            return {
                'parse': 0,
                'url': raw_url,
            }

    def localProxy(self, param):
        if param.get('action') == 'clean':
            src_url = param.get('src')
            if not src_url:
                return [400, 'text/plain', '缺少 src 参数']

            # ---------- 1. URL 规范化 ----------
            norm_url = self._normalize_url(src_url)

            # ---------- 2. 命中缓存（直接返回） ----------
            if self.cached_url == norm_url and self.cached_content is not None:
                return [
                    200,
                    'application/vnd.apple.mpegurl',
                    self.cached_content,
                    {
                        'Access-Control-Allow-Origin': '*',
                        'Cache-Control': 'no-cache, no-store'
                    }
                ]

            # ---------- 3. 未命中：执行抓取、清洗、缓存覆盖 ----------
            try:
                # 使用框架内置 fetch 方法，不传递 headers（使用默认）
                resp = self.fetch(src_url, timeout=8)
                if not resp or not resp.text:
                    return [500, 'text/plain', '获取 m3u8 失败']
                raw_content = resp.text
                base_url = src_url.rsplit('/', 1)[0] + '/'
                # 处理多码率变体
                hops = 0
                while '#EXT-X-STREAM-INF' in raw_content and hops < 2:
                    variant = self._pick_variant(raw_content, base_url)
                    if not variant:
                        break
                    sub_resp = self.fetch(variant, timeout=8)
                    if not sub_resp or not sub_resp.text:
                        break
                    raw_content = sub_resp.text
                    base_url = variant.rsplit('/', 1)[0] + '/'
                    hops += 1
                cleaned = self._clean_m3u8(raw_content, base_url)

                # ---------- 覆盖缓存 ----------
                self.cached_url = norm_url
                self.cached_content = cleaned

                return [
                    200,
                    'application/vnd.apple.mpegurl',
                    cleaned,
                    {
                        'Access-Control-Allow-Origin': '*',
                        'Cache-Control': 'no-cache, no-store'
                    }
                ]
            except Exception as e:
                # 清洗失败即播放失败，不做 302 降级兜底
                return [500, 'text/plain', '清洗失败: %s' % e]
        return []

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        pass

    def destroy(self):
        pass