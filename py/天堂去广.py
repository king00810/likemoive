# -*- coding: utf-8 -*-
import sys
import json
import re
import time
import requests
from urllib.parse import urljoin, urlparse, quote, parse_qs, urlencode, urlunparse
sys.path.append('..')
from base.spider import Spider

class Spider(Spider):
    GROUP_MAX_ITEMS = 12  # 保留: 兼容外部对 _expand_to_groups 的潜在调用

    def init(self, extend=""):
        self.name = "dyttm3u8"
        self.base_url = "http://caiji.dyttzyapi.com/api.php/provide/vod/from/dyttm3u8"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'http://caiji.dyttzyapi.com'
        }
        self.classes = [
            {"type_id": "6", "type_name": "动作片"},
            {"type_id": "7", "type_name": "喜剧片"},
            {"type_id": "8", "type_name": "爱情片"},
            {"type_id": "9", "type_name": "科幻片"},
            {"type_id": "10", "type_name": "恐怖片"},
            {"type_id": "11", "type_name": "剧情片"},
            {"type_id": "12", "type_name": "战争片"},
            {"type_id": "13", "type_name": "国产剧"},
            {"type_id": "14", "type_name": "香港剧"},
            {"type_id": "15", "type_name": "韩国剧"},
            {"type_id": "16", "type_name": "欧美剧"},
            {"type_id": "20", "type_name": "记录片"},
            {"type_id": "21", "type_name": "台湾剧"},
            {"type_id": "22", "type_name": "日本剧"},
            {"type_id": "23", "type_name": "海外剧"},
            {"type_id": "24", "type_name": "泰国剧"},
            {"type_id": "25", "type_name": "大陆综艺"},
            {"type_id": "26", "type_name": "港台综艺"},
            {"type_id": "27", "type_name": "日韩综艺"},
            {"type_id": "28", "type_name": "欧美综艺"},
            {"type_id": "29", "type_name": "国产动漫"},
            {"type_id": "30", "type_name": "日韩动漫"},
            {"type_id": "31", "type_name": "欧美动漫"},
            {"type_id": "32", "type_name": "港台动漫"},
            {"type_id": "33", "type_name": "海外动漫"},
            {"type_id": "34", "type_name": "伦理片"},
            {"type_id": "36", "type_name": "短剧"},
            {"type_id": "37", "type_name": "动画片"}
        ]
        self.filters = {cat['type_id']: [] for cat in self.classes}
        try:
            self.session = requests.Session()
            from requests.adapters import HTTPAdapter
            _ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
            self.session.mount('http://', _ad)
            self.session.mount('https://', _ad)
        except Exception:
            self.session = requests

        self.pic_cache = {}          # 海报缓存(vod_id -> 图片URL)
        self.PIC_CACHE_MAX = 4000
        self._clean_store = {}       # 清洗结果 LRU 缓存
        self._clean_order = []

    def getName(self):
        return self.name

    # ==================== 清洗引擎（统一广告指纹 + 帧网格加速） ====================
    AD_DUR_FP = (
        (5.567, 2.933, 5.7, 3.333, 1.533),       # FP1（主广告段，5 ts / 19s）
        (6.667, 2.133, 3.233, 3.733),             # FP2（副广告段，4 ts / 15.8s）
        (4.6, 5.533, 0.233),                      # FP3（备用）
    )
    FP_TOL = 0.20
    FRAME_GRID = 0.04
    GRID_TOL = 0.02
    GRID_DISABLE_RATIO = 0.30
    MAX_REMOVE_RATIO = 0.15    # 保护线：超 15% 只放弃可疑判据，不放弃已确认的 FP

    def _normalize_lines(self, lines):
        out, i, n = [], 0, len(lines)
        while i < n:
            if (lines[i].startswith('#EXTINF') and i + 2 < n
                    and lines[i + 1] == '#EXT-X-DISCONTINUITY'
                    and not lines[i + 2].startswith('#')):
                out.extend((lines[i + 1], lines[i], lines[i + 2]))
                i += 3
            else:
                out.append(lines[i])
                i += 1
        return out

    def _split_playlist(self, content):
        """★Bug1修复: 原实现对 EXTINF 后紧跟 #EXT-X-KEY 等标签的场景，
        会把 EXTINF 塞进 extras 并静默丢弃其 ts 片段（黑屏根因）。
        现改为: 遇到 #EXTINF 后向后扫描, 跳过/收集中间 # 行, 直到找到 URI。"""
        lines = self._normalize_lines([l.strip() for l in content.splitlines() if l.strip()])
        header, i = [], 0
        while i < len(lines) and lines[i].startswith('#EXT') and \
                not lines[i].startswith(('#EXTINF', '#EXT-X-DISCONTINUITY')):
            header.append(lines[i])
            i += 1
        footer, end = [], len(lines)
        for j in range(len(lines) - 1, i - 1, -1):
            if lines[j] == '#EXT-X-ENDLIST':
                footer, end = [lines[j]], j
                break
        items, extras, disc, k = [], [], False, i
        while k < end:
            line = lines[k]
            if line == '#EXT-X-DISCONTINUITY':
                disc = True
                k += 1
                continue
            if line.startswith('#EXTINF'):
                try:
                    dur = float(line.split(':', 1)[1].split(',')[0])
                except (ValueError, IndexError):
                    dur = 0.0
                j, uri = k + 1, None
                while j < end:
                    l2 = lines[j]
                    if l2 == '#EXT-X-DISCONTINUITY':
                        disc = True
                    elif l2.startswith('#'):
                        extras.append(l2)
                    else:
                        uri = l2
                        j += 1
                        break
                    j += 1
                if uri is not None:
                    items.append({'dur': dur, 'inf': line, 'uri': uri,
                                  'disc': disc, 'extras': extras})
                    extras, disc = [], False
                    k = j
                    continue
                k += 1
                continue
            elif line.startswith('#'):
                extras.append(line)
            k += 1
        return header, items, footer

    def _disc_groups(self, items):
        out, start = [], 0
        for i, it in enumerate(items):
            if it['disc'] and i > start:
                out.append((start, i))
                start = i
        if len(items) > start:
            out.append((start, len(items)))
        return out

    def _expand_to_groups(self, ads, groups):
        out = set(ads)
        for s, e in groups:
            span = set(range(s, e))
            if ads & span and e - s <= self.GROUP_MAX_ITEMS:
                out |= span
        return out

    def _detect_duration_fp(self, items):
        durs = [it['dur'] for it in items]
        ads = set()
        for fp in self.AD_DUR_FP:
            n = len(fp)
            for i in range(len(durs) - n + 1):
                if all(abs(durs[i + j] - fp[j]) <= self.FP_TOL for j in range(n)):
                    ads.update(range(i, i + n))
        return ads

    def _rewrite_extra(self, line, base_url):
        m = re.search(r'URI="([^"]+)"', line) if 'URI="' in line else None
        if m and not m.group(1).startswith(('http://', 'https://')):
            return line.replace('URI="%s"' % m.group(1),
                                'URI="%s"' % urljoin(base_url, m.group(1)))
        return line

    def _abs_uri(self, uri, base_url):
        return uri if uri.startswith(('http://', 'https://')) else urljoin(base_url, uri)

    def _clean_m3u8(self, content, base_url, debug=False):
        header, items, footer = self._split_playlist(content)
        if not items:
            return content
        total = sum(it['dur'] for it in items)

        # ---- 主判据: FP 指纹, 独立校验比例 (★Bug3修复) ----
        fp_ads = self._detect_duration_fp(items)
        fp_removed = sum(items[i]['dur'] for i in fp_ads)
        fp_ok = total <= 0 or fp_removed / total <= self.MAX_REMOVE_RATIO
        if not fp_ok:
            fp_ads, fp_removed = set(), 0.0

        # ---- 辅助判据: 帧网格 (排除 FP 已命中片段后再做健康自检, ★Bug4修复) ----
        candidates = [it for idx, it in enumerate(items) if idx not in fp_ads]
        if self._grid_is_valid(candidates):
            grid_ads = self._detect_frame_grid(items) - fp_ads
            grid_removed = sum(items[i]['dur'] for i in grid_ads)
            grid_disabled = False
            if total > 0 and (fp_removed + grid_removed) / total > self.MAX_REMOVE_RATIO:
                grid_ads = set()  # 只放弃网格判据, 保留已确认的 FP
        else:
            grid_ads = set()
            grid_disabled = True

        ads = fp_ads | grid_ads
        removed = sum(items[i]['dur'] for i in ads)
        over_limit = bool(grid_ads) and ads == fp_ads and fp_ads != set()

        body, maxdur, pending = [], 0.0, False
        for i, it in enumerate(items):
            if i in ads:
                pending = True
                continue
            if (it['disc'] or pending) and body:
                body.append('#EXT-X-DISCONTINUITY')
            pending = False
            for line in it['extras']:
                body.append(self._rewrite_extra(line, base_url))
            maxdur = max(maxdur, it['dur'])
            body.append(it['inf'])
            body.append(self._abs_uri(it['uri'], base_url))
        out = [self._rewrite_extra(l, base_url) for l in header
               if not l.startswith('#EXT-X-TARGETDURATION')]
        if not out or not out[0].startswith('#EXTM3U'):
            out.insert(0, '#EXTM3U')
        for tag, val in (('#EXT-X-VERSION', '#EXT-X-VERSION:3'),
                         ('#EXT-X-PLAYLIST-TYPE', '#EXT-X-PLAYLIST-TYPE:VOD'),
                         ('#EXT-X-MEDIA-SEQUENCE', '#EXT-X-MEDIA-SEQUENCE:0')):
            if not any(l.startswith(tag) for l in out):
                out.append(val)
        out.append('#EXT-X-TARGETDURATION:%d' % max(1, int(maxdur + 0.999)))
        out.extend(body)
        out.extend(footer if footer else ['#EXT-X-ENDLIST'])
        if debug:
            head = ['# ==== dytt 去广告调试 ====',
                    '# 总片数 %d  总时长 %.1fs  删除 %d 片 / %.1fs  (%.2f%%)' % (
                        len(items), total, len(ads), removed,
                        (removed / total * 100) if total > 0 else 0),
                    '# FP命中 %d(%.1fs)  FP有效 %s  网格命中 %d  网格禁用 %s' % (
                        len(fp_ads), fp_removed, fp_ok, len(grid_ads), grid_disabled)]
            head.append('# ==== end ====')
            out = head + out
        return '\n'.join(out) + '\n'

    def _grid_is_valid(self, items):
        """网格健康自检: 非 25fps 源(正片不在 0.04s 网格)时禁用网格判据。
        ★Bug4修复: 传入的 items 已排除 FP 命中片段, 广告不再拉高非网格占比。"""
        if not items:
            return False
        non_grid = 0
        for it in items:
            d = it['dur']
            if d > 0:
                q = d / self.FRAME_GRID
                if abs(q - round(q)) > self.GRID_TOL:
                    non_grid += 1
        return non_grid / len(items) <= self.GRID_DISABLE_RATIO

    def _detect_frame_grid(self, items):
        durs = [it['dur'] for it in items]
        ads = set()
        for i, d in enumerate(durs):
            if d <= 0:
                continue
            q = d / self.FRAME_GRID
            if abs(q - round(q)) > self.GRID_TOL:
                ads.add(i)
        return ads

    def _pick_variant(self, content, base_url):
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        best, best_bw = None, -1
        for i, line in enumerate(lines):
            if not line.startswith('#EXT-X-STREAM-INF'):
                continue
            m = re.search(r'BANDWIDTH=(\d+)', line)
            bw = int(m.group(1)) if m else 0
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith('#'):
                    if bw >= best_bw:
                        best, best_bw = urljoin(base_url, lines[j]), bw
                    break
        return best
    # ==================== 清洗引擎结束 ====================

    # ==================== URL 规范化（提高缓存命中） ====================

    CLEAN_CACHE_MAX = 8

    def _clean_get(self, key):
        try:
            v = self._clean_store.get(key)
            if v is None:
                return None
            if key in self._clean_order:
                self._clean_order.remove(key)
            self._clean_order.append(key)
            return v
        except Exception:
            return None

    def _clean_put(self, key, val):
        try:
            if key in self._clean_store and key in self._clean_order:
                self._clean_order.remove(key)
            self._clean_store[key] = val
            self._clean_order.append(key)
            while len(self._clean_order) > self.CLEAN_CACHE_MAX:
                old = self._clean_order.pop(0)
                self._clean_store.pop(old, None)
        except Exception:
            pass

    def _normalize_url(self, url):
        """★Bug2修复: 原实现盲删 v/t 参数, 不同集数/清晰度若仅凭 v= 区分会缓存串台;
        且 parse_qs 默认丢弃空值参数导致归一化不稳定。
        现只剥离纯缓存破坏参数, 保留 v/t, 并 keep_blank_values=True。"""
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        for param in ['_', 'rand', 'cb', 'callback', 'timestamp']:
            query_params.pop(param, None)
        new_query = urlencode(query_params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _request(self, params=None):
        try:
            resp = self.session.get(self.base_url, params=params, headers=self.headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') != 1:
                return None
            return data
        except Exception:
            return None

    def _fetch_pics_batch(self, vod_ids):
        if not vod_ids:
            return {}
        pic_map = {}
        need = []
        for vid in vod_ids:
            cached = self.pic_cache.get(vid)
            if cached:
                pic_map[vid] = cached
            else:
                need.append(vid)
        if not need:
            return pic_map
        STEP = 40
        for i in range(0, len(need), STEP):
            chunk = need[i:i + STEP]
            resp = self._request({'ac': 'detail', 'ids': ','.join(chunk)})
            if not resp:
                continue
            for item in resp.get('list', []):
                vid = str(item.get('vod_id', ''))
                pic = item.get('vod_pic', '') or item.get('vod_pic_thumb', '') or ''
                if vid and pic:
                    pic_map[vid] = pic
                    if len(self.pic_cache) > self.PIC_CACHE_MAX:
                        self.pic_cache.clear()
                    self.pic_cache[vid] = pic
        return pic_map

    def _build_vod_id(self, name, vid, pic):
        return f"{name}###{vid}###{pic}"

    def _extract_vid_from_composite(self, composite_id):
        if not composite_id:
            return None
        if '###' in composite_id:
            parts = composite_id.split('###')
            if len(parts) >= 2:
                return parts[1].strip()
        match = re.search(r'(\d+)', composite_id)
        return match.group(1) if match else None

    def homeContent(self, filter=False):
        return {'class': self.classes, 'filters': self.filters}

    def homeVideoContent(self):
        resp = self._request({'ac': 'list'})
        if not resp:
            return {'list': []}
        raw_list = resp.get('list', [])
        ids_needed = [str(item.get('vod_id', '')) for item in raw_list if item.get('vod_id')]
        pic_map = self._fetch_pics_batch(ids_needed) if ids_needed else {}
        result = []
        for item in raw_list:
            vod_id = str(item.get('vod_id', ''))
            vod_name = item.get('vod_name', '')
            if not vod_id or not vod_name:
                continue
            vod_pic = pic_map.get(vod_id, '') or item.get('vod_pic', '') or item.get('vod_pic_thumb', '')
            result.append({
                'vod_id': self._build_vod_id(vod_name, vod_id, vod_pic),
                'vod_name': vod_name,
                'vod_pic': vod_pic,
                'vod_remarks': item.get('vod_remarks', '')
            })
        return {'list': result}

    def categoryContent(self, tid, pg, filter=False, extend=None):
        p = int(pg) if pg else 1
        params = {'ac': 'list', 't': tid, 'pg': p}
        if extend and isinstance(extend, dict):
            params.update(extend)
        resp = self._request(params)
        if not resp:
            return {'list': [], 'page': p, 'pagecount': 1, 'total': 0}
        raw_list = resp.get('list', [])
        ids_needed = [str(item.get('vod_id', '')) for item in raw_list if item.get('vod_id')]
        pic_map = self._fetch_pics_batch(ids_needed) if ids_needed else {}
        result = []
        for item in raw_list:
            vod_id = str(item.get('vod_id', ''))
            vod_name = item.get('vod_name', '')
            if not vod_id or not vod_name:
                continue
            vod_pic = pic_map.get(vod_id, '') or item.get('vod_pic', '') or item.get('vod_pic_thumb', '')
            result.append({
                'vod_id': self._build_vod_id(vod_name, vod_id, vod_pic),
                'vod_name': vod_name,
                'vod_pic': vod_pic,
                'vod_remarks': item.get('vod_remarks', '')
            })
        return {
            'page': p,
            'pagecount': resp.get('pagecount', p),
            'list': result,
            'total': resp.get('total', 0)
        }

    def detailContent(self, ids):
        if not ids:
            return {'list': []}
        raw_id = ids[0] if isinstance(ids, list) else ids
        real_id = self._extract_vid_from_composite(raw_id)
        if not real_id:
            return {'list': []}
        resp = self._request({'ac': 'detail', 'ids': real_id})
        if not resp:
            return {'list': []}
        detail_list = resp.get('list', [])
        if not detail_list:
            return {'list': []}
        detail = detail_list[0]
        vod_item = {
            'vod_id': real_id,
            'vod_name': detail.get('vod_name', ''),
            'vod_pic': detail.get('vod_pic', '') or detail.get('vod_pic_thumb', ''),
            'vod_content': detail.get('vod_content', '').strip(),
            'vod_play_from': detail.get('vod_play_from', '线路1'),
            'vod_play_url': detail.get('vod_play_url', ''),
            'vod_actor': detail.get('vod_actor', ''),
            'vod_director': detail.get('vod_director', ''),
            'vod_year': detail.get('vod_year', ''),
            'vod_area': detail.get('vod_area', ''),
            'vod_class': detail.get('vod_class', ''),
            'type_name': detail.get('type_name', ''),
            'vod_remarks': detail.get('vod_remarks', ''),
            'vod_score': detail.get('vod_score', ''),
            'vod_lang': detail.get('vod_lang', ''),
            'vod_pubdate': detail.get('vod_pubdate', ''),
        }
        if not vod_item['vod_play_url']:
            vod_url = detail.get('vod_url', '')
            if vod_url:
                vod_item['vod_play_url'] = f"第1集${vod_url}"
        return {'list': [vod_item]}

    def searchContent(self, key, quick=False, pg=1):
        p = int(pg) if pg else 1
        resp = self._request({'ac': 'search', 'wd': key, 'pg': p})
        if not resp:
            return {'list': [], 'page': p, 'pagecount': 1, 'total': 0}
        raw_list = resp.get('list', [])
        ids_needed = [str(item.get('vod_id', '')) for item in raw_list if item.get('vod_id')]
        pic_map = self._fetch_pics_batch(ids_needed) if ids_needed else {}
        result = []
        for item in raw_list:
            vod_id = str(item.get('vod_id', ''))
            vod_name = item.get('vod_name', '')
            if not vod_id or not vod_name:
                continue
            vod_pic = pic_map.get(vod_id, '') or item.get('vod_pic', '') or item.get('vod_pic_thumb', '')
            result.append({
                'vod_id': self._build_vod_id(vod_name, vod_id, vod_pic),
                'vod_name': vod_name,
                'vod_pic': vod_pic,
                'vod_remarks': item.get('vod_remarks', '')
            })
        return {
            'page': p,
            'pagecount': resp.get('pagecount', p),
            'list': result,
            'total': resp.get('total', 0)
        }

    # ==================== 播放与代理（内置清洗 + 单条目缓存） ====================
    def playerContent(self, flag, id, vipFlags=None):
        if '.m3u8' in id:
            proxy_url = self.getProxyUrl(local=True) + f'&action=clean&src={quote(id)}'
            return {
                'parse': 0,
                'url': proxy_url,
            }
        else:
            # ★Bug7修复: 直链补 header, 部分源需要 UA/Referer
            return {
                'parse': 0,
                'url': id,
                'header': self.headers,
            }

    def localProxy(self, param):
        if param.get('action') == 'clean':
            src_url = param.get('src')
            if not src_url:
                return [400, 'text/plain', '缺少 src 参数']

            norm_url = self._normalize_url(src_url)
            _hit = self._clean_get(norm_url)
            if _hit is not None:
                return [200, 'application/vnd.apple.mpegurl', _hit,
                        {'Access-Control-Allow-Origin': '*',
                         'Cache-Control': 'no-cache, no-store'}]
            try:
                raw_content, base_url = self._fetch_m3u8_chain(src_url)
                if not raw_content:
                    return [500, 'text/plain', '获取 m3u8 失败']
                if not raw_content.lstrip().startswith('#EXTM3U'):
                    return [500, 'text/plain',
                            '上游返回非 m3u8（可能是 WAF 403/502）: %s' % raw_content[:120]]
                cleaned = self._clean_m3u8(raw_content, base_url, debug=False)
                self._clean_put(norm_url, cleaned)
                return [200, 'application/vnd.apple.mpegurl', cleaned,
                        {'Access-Control-Allow-Origin': '*',
                         'Cache-Control': 'no-cache, no-store'}]
            except Exception as e:
                return [500, 'text/plain', '清洗失败: %s' % e]
        # ★Bug5修复: 未知 action 返回明确的 404, 不再返回空列表
        return [404, 'text/plain', 'unknown action: %s' % param.get('action')]

    def _fetch_m3u8_chain(self, src_url, max_hops=3):
        """抓取 m3u8 (含多码率跳转)。★Bug6修复: 失败时用 m3u8 自身域名做 Referer 重试一次。"""
        p = urlparse(src_url)
        alt_headers = dict(self.headers)
        alt_headers['Referer'] = '%s://%s/' % (p.scheme, p.netloc)
        for hdrs in (self.headers, alt_headers):
            try:
                resp = self.fetch(src_url, headers=hdrs, timeout=8)
                if resp and resp.text and resp.text.lstrip().startswith('#EXTM3U'):
                    break
                resp = None
            except Exception:
                resp = None
        if not resp:
            return None, None
        raw_content = resp.text
        base_url = src_url.rsplit('/', 1)[0] + '/'
        hops = 0
        while '#EXT-X-STREAM-INF' in raw_content and hops < max_hops:
            variant = self._pick_variant(raw_content, base_url)
            if not variant:
                break
            try:
                sub_resp = self.fetch(variant, headers=hdrs, timeout=8)
            except Exception:
                break
            if not sub_resp or not sub_resp.text or \
                    not sub_resp.text.lstrip().startswith('#EXTM3U'):
                break
            raw_content = sub_resp.text
            base_url = variant.rsplit('/', 1)[0] + '/'
            hops += 1
        return raw_content, base_url

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        pass

    def destroy(self):
        pass
