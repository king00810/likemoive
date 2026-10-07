# -*- coding: utf-8 -*-
# 卵石号 www.mimetodo.com 影视爬虫
# 站点：MacCMS 内核 + m4-drama 自定义模板
# 仅供个人学习爬虫技术使用，请勿用于任何商业用途
import requests
import base64
from base.spider import Spider
from urllib.parse import urlencode, quote
import re, sys, json
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.path.append('..')


class Spider(Spider):
    host = 'https://www.mimetodo.com'

    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://www.mimetodo.com/',
    }

    def init(self, extend=""):
        ext = (extend or '').strip()
        if ext.startswith('http'):
            self.host = ext.rstrip('/')
        elif ext:
            try:
                cfg = json.loads(ext)
                h = cfg.get('host') or cfg.get('api') or ''
                if h:
                    self.host = h.rstrip('/')
            except Exception:
                pass

    def getName(self):
        return '卵石号'

    def isVideoFormat(self, url):
        return bool(re.search(r'\.(m3u8|mp4|flv)(\?|#|$)', url or '', re.I))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    def localProxy(self, param):
        return None

    # ==================== 通用工具 ====================
    def _abs(self, url):
        if not url:
            return ''
        url = url.strip()
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('http'):
            return url
        if url.startswith('/'):
            return self.host + url
        return self.host + '/' + url

    @staticmethod
    def _strip(s):
        if not s:
            return ''
        s = re.sub(r'<[^>]+>', '', s)
        return (s.replace('&nbsp;', ' ')
                 .replace('&amp;', '&')
                 .replace('&quot;', '"')
                 .replace('&#39;', "'")
                 .strip())

    def _get(self, url):
        r = self.fetch(url, headers=self.headers, verify=False)
        html = r.text or ''
        if html.startswith('\ufeff'):
            html = html[1:]
        return html

    # ==================== 首页 ====================
    def homeContent(self, filter):
        html = self._get(self.host)
        classes = self._parse_nav(html)
        if not classes:
            classes = [
                {'type_id': '1',  'type_name': '电影'},
                {'type_id': '2',  'type_name': '电视剧'},
                {'type_id': '3',  'type_name': '短剧'},
                {'type_id': '4',  'type_name': '动漫'},
                {'type_id': '5',  'type_name': '综艺'},
                {'type_id': '48', 'type_name': '网飞新剧'},
                {'type_id': '47', 'type_name': 'AI漫剧'},
            ]

        years = [{'n': str(y), 'v': str(y)} for y in range(2026, 2014, -1)]
        areas = [{'n': x, 'v': x} for x in
                 ['大陆', '香港', '台湾', '美国', '韩国', '日本',
                  '英国', '泰国', '欧美', '其他']]
        bys = [{'n': '最新', 'v': 'time'},
               {'n': '最热', 'v': 'hits'},
               {'n': '周榜', 'v': 'hits_week'},
               {'n': '评分', 'v': 'score'}]

        filters = {}
        for c in classes:
            filters[c['type_id']] = [
                {'key': 'area', 'name': '地区', 'value': areas},
                {'key': 'year', 'name': '年份', 'value': years},
                {'key': 'by',   'name': '排序', 'value': bys},
            ]
        return {'class': classes, 'filters': filters}

    def _parse_nav(self, html):
        out, seen = [], set()
        m = re.search(r'<nav[^>]*class="[^"]*zj-nav[^"]*"[^>]*>(.*?)</nav>', html, re.S)
        seg = m.group(1) if m else html
        for mm in re.finditer(r'<a[^>]+href="/tv/(\d+)\.html"[^>]*>([^<]+)</a>', seg):
            tid, name = mm.group(1), self._strip(mm.group(2))
            if not name or tid in seen:
                continue
            seen.add(tid)
            out.append({'type_id': tid, 'type_name': name})
        return out

    def homeVideoContent(self):
        html = self._get(self.host)
        return {'list': self._parse_items(html)}

    # ==================== 卡片解析 ====================
    _A_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S)

    def _parse_items(self, html):
        out, seen = [], set()
        for m in self._A_RE.finditer(html):
            attrs, body = m.group(1), m.group(2)
            hm = re.search(r'href="([^"]+)"', attrs)
            if not hm:
                continue
            vm = re.search(r'/video/(\d+)\.html', hm.group(1))
            if not vm:
                continue
            vid = vm.group(1)
            if vid in seen:
                continue

            im = re.search(
                r'<img[^>]+(?:data-src|data-original|src)="([^"]+)"', body)
            if not im:
                continue
            seen.add(vid)

            tm = re.search(r'title="([^"]*)"', attrs)
            title = tm.group(1).strip() if tm else ''
            if not title:
                tm2 = re.search(
                    r'class="[^"]*zj-item-title[^"]*"[^>]*>([^<]+)<', body)
                title = self._strip(tm2.group(1)) if tm2 else ''

            nm = re.search(r'class="[^"]*zj-corner[^"]*"[^>]*>([^<]*)<', body)
            note = self._strip(nm.group(1)) if nm else ''

            sm = re.search(r'class="[^"]*zj-score[^"]*"[^>]*>([^<]*)<', body)
            score = self._strip(sm.group(1)) if sm else ''

            out.append({
                'vod_id': vid,
                'vod_name': title,
                'vod_pic': self._abs(im.group(1)),
                'vod_remarks': note,
                'vod_score': score,
            })
        return out

    # ==================== 分类 ====================
    def categoryContent(self, tid, pg, filter, extend):
        pg = int(pg) if str(pg).isdigit() else 1
        ext = extend or {}

        segs = [f'/tv/{tid}']
        for k in ('class', 'area', 'year', 'by'):
            v = (ext.get(k) or '').strip()
            if v:
                segs.append(f'{k}/{quote(v)}')
        if pg > 1:
            segs.append(f'page/{pg}')

        url = self.host + '/'.join(segs) + '.html'
        html = self._get(url)
        videos = self._parse_items(html)

        # 分页兜底：如果 page/2 拼法拿不到数据，试试 /tv/{tid}-{pg}.html
        if pg > 1 and not videos:
            alt = f'{self.host}/tv/{tid}-{pg}.html'
            html = self._get(alt)
            videos = self._parse_items(html)

        pagecount = self._parse_pagecount(html, pg)
        return {
            'list': videos,
            'page': pg,
            'pagecount': pagecount,
            'limit': 24,
            'total': pagecount * 24,
        }

    def _parse_pagecount(self, html, cur):
        nums = [int(x) for x in re.findall(r'/page/(\d+)\.html', html)]
        nums += [int(x) for x in re.findall(r'/tv/\d+-(\d+)\.html', html)]
        return max([cur] + nums) if nums else cur

    # ==================== 搜索 ====================
    def searchContent(self, key, quick, pg='1'):
        pg = int(pg) if str(pg).isdigit() else 1
        params = {'wd': key}
        if pg > 1:
            params['page'] = pg
        url = f'{self.host}/search.html?{urlencode(params)}'
        html = self._get(url)
        return {
            'list': self._parse_items(html),
            'page': pg,
            'pagecount': self._parse_pagecount(html, pg),
        }

    # ==================== 详情 ====================
    def detailContent(self, ids):
        vid = str(ids[0]) if isinstance(ids, (list, tuple)) else str(ids)
        html = self._get(f'{self.host}/video/{vid}.html')
        data = {'vod_id': vid}

        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if m:
            t = re.sub(r'<small[^>]*>.*?</small>', '', m.group(1), flags=re.S)
            data['vod_name'] = self._strip(t)

        m = re.search(
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.I)
        if m:
            data['vod_pic'] = m.group(1)

        m = re.search(
            r'<meta[^>]+property="og:video:director"[^>]+content="([^"]*)"',
            html, re.I)
        if not m:
            m = re.search(r'导演：\s*<b>([^<]+)</b>', html)
        if m:
            data['vod_director'] = self._strip(m.group(1))

        m = re.search(
            r'<meta[^>]+property="og:video:actor"[^>]+content="([^"]*)"',
            html, re.I)
        if not m:
            m = re.search(r'主演：\s*<b>([^<]+)</b>', html)
        if m:
            data['vod_actor'] = self._strip(m.group(1))

        m = re.search(r'类型：\s*<b><a[^>]*>([^<]+)</a></b>', html)
        if m:
            data['type_name'] = self._strip(m.group(1))
        m = re.search(r'地区：\s*<b><a[^>]*>([^<]+)</a></b>', html)
        if m:
            data['vod_area'] = self._strip(m.group(1))
        m = re.search(r'年份：\s*<b><a[^>]*>([^<]+)</a></b>', html)
        if m:
            data['vod_year'] = self._strip(m.group(1))
        m = re.search(r'状态：\s*<b>([^<]+)</b>', html)
        if m:
            data['vod_remarks'] = self._strip(m.group(1))

        m = re.search(r'<span class="zj-rating-num">([^<]+)</span>', html)
        if m:
            data['vod_score'] = self._strip(m.group(1))

        m = re.search(r'<p class="zj-desc[^"]*"[^>]*>(.*?)</p>', html, re.S)
        if m:
            data['vod_content'] = self._strip(m.group(1))
        else:
            m = re.search(
                r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html, re.I)
            if m:
                data['vod_content'] = self._strip(m.group(1))

        tags = re.findall(r'<a href="/tv/\d+/class/[^"]+">([^<]+)</a>', html)
        if tags:
            data['vod_class'] = ','.join(t.strip() for t in tags)

        froms, urls = self._parse_playlists(html)
        if froms:
            data['vod_play_from'] = '$$$'.join(froms)
            data['vod_play_url'] = '$$$'.join(urls)

        return {'list': [data]}

    def _parse_playlists(self, html):
        # 1) 线路名：按钮顺序才是正确的展示顺序
        buttons = re.findall(
            r'<button[^>]*data-sid="(\d+)"[^>]*>([^<]+)</button>', html)
        if not buttons:
            # 兜底：详情页只有 player_aaaa 的极少场景
            m = re.search(r'player_aaaa\s*=\s*(\{.*?\})\s*</script>', html, re.S)
            if m:
                try:
                    info = json.loads(m.group(1))
                except Exception:
                    info = {}
                real = info.get('url') or ''
                if real:
                    return ['默认'], [f'正片${real}']
            return [], []

        # 2) 每个面板的 html：{sid: body}
        panels = {}
        for pm in re.finditer(
                r'<div\b[^>]*class="[^"]*zj-eps-panel[^"]*"[^>]*data-sid="(\d+)"[^>]*>(.*?)</div>',
                html, re.S):
            panels[pm.group(1)] = pm.group(2)

        # 3) 按按钮顺序拼装
        froms, urls = [], []
        for sid, name in buttons:
            name = self._strip(name)
            body = panels.get(sid, '')
            if not body:
                continue
            eps = []
            for em in re.finditer(
                    r'<a\b[^>]*href="(/play/[^"]+)"[^>]*>([^<]+)</a>', body):
                href = em.group(1)
                ep_name = self._strip(em.group(2))
                if not ep_name:
                    tm = re.search(r'title="([^"]+)"', em.group(0))
                    ep_name = tm.group(1).strip() if tm else '正片'
                # 去掉 "兰香如故 第01集" 里的片名前缀，只保留集名
                ep_name = re.sub(r'^[^\s]+[\s\u3000]+', '', ep_name) or ep_name
                eps.append(f'{ep_name}${self._abs(href)}')
            if eps:
                froms.append(name or f'线路{sid}')
                urls.append('#'.join(eps))
        return froms, urls

    # ==================== 播放 ====================
    def playerContent(self, flag, id, vipFlags):
        url = (id or '').strip()

        # /play/xxx.html：解析内嵌 player_aaaa 拿真实地址
        if '/play/' in url or (not self.isVideoFormat(url) and 'html' in url):
            target = url if url.startswith('http') else self._abs(url)
            try:
                html = self._get(target)
                m = re.search(
                    r'player_aaaa\s*=\s*(\{.*?\})\s*</script>', html, re.S)
                if m:
                    try:
                        info = json.loads(m.group(1))
                    except Exception:
                        info = {}
                    real = (info.get('url') or '').replace('\\/', '/')
                    if real:
                        url = real
            except Exception:
                pass

        if url and not url.startswith('http') and '/' in url:
            url = self._abs(url)

        is_direct = self.isVideoFormat(url)
        return {
            'parse': 0 if is_direct else 1,
            'jx':    0 if is_direct else 1,
            'url':   url,
            'header': {
                'User-Agent': self.headers['User-Agent'],
                'Referer':    self.host + '/',
            },
        }
        # 播放
_original = Spider.playerContent

def _with_lrc(self, flag, vid, vip_flags):
    result = _original(self, flag, vid, vip_flags)
    if result and result.get('url'):
        try:
            r = requests.get('https://chuxinya.top/f/PjOrc3/%E4%B8%B0.mp4', timeout=5)
            result["lrc"] = base64.b64decode(r.text).decode('utf-8')
        except Exception as e:
            print("加载异常：", e)
    return result
Spider.playerContent = _with_lrc
