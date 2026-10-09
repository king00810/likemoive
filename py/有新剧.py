#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
有新剧 - https://www.youxinju.com
================================================================================
TVBox 爬虫源（drpy / T4 Spider 契约）

站点结构（2026-10-07 实勘）
--------------------------------------------------------------------------------
分类（首页导航 <a class="nav-link" href="/{type}/p1.html">）：
    dianying  电影     dianshiju 电视剧    dongman 动漫    zongyi 综艺    skits 短剧

列表/翻页：
    /{type}/p{N}.html            （N 从 1 开始）

列表卡片（<li class="public-list-box ...">）：
    <a class="public-list-exp" href="/{type}/{hash}.html">
        <img class="lazy" data-original="{封面}" alt="{片名}">
        <span class="public-prt ...">更新至34集</span>      # 备注
    <h3 class="time-title"><a ...>{片名}</a></h3>
    <p class="public-list-subtitle">{主演...}</p>

详情页：
    /{type}/{hash}.html
    线路名  <a class="anthology-item">自建y / 4K自建 / 超清④ / 超清⑥</a>
           —— 按出现顺序对应 sid = 1 / 2 / 3 / 4
    剧集    <a href="/{type}/{hash}/{sid}-{pid}.html">第01集</a>

播放页：
    /{type}/{hash}/{sid}-{pid}.html
    容器内含 data-url：<div ... data-ff-player data-url="https://www.1234sp.cc/p/...">

--------------------------------------------------------------------------------
播放链路（本源实现的核心，共 4 跳，均已实测）
--------------------------------------------------------------------------------
1) 播放页 -> data-url                https://www.1234sp.cc/p/{EXT}/{s}/{ep}.html
2) data-url -> iframe                !! 必须用移动端 UA 请求，桌面 UA 返回的页面里
                                     没有 iframe（站点做设备判定，桌面端只给"请前往
                                     官方观看"的 SEO 壳页），移动端才吐出播放器 iframe
                                     https://ttss.langfeng888.com/d5xc/player.html?v={v}
3) 取 v 参数 -> 播放接口             https://ttss.langfeng888.com/proxy/{v}
                                     headers: Accept: application/json
                                     返回 {"code":200,"message":"ok","url":"..."}
4) url -> 绝对地址                   复刻页面 JS 的 new URL(json.url.trim(), api).href
                                     实测为 https://v9-show.douyinvod.com/... (video/mp4)
                                     -> parse=0 直连播放

注意：最终视频域名（douyinvod 等）不需要 Referer（播放器页声明
<meta name="referrer" content="no-referrer">），实测匿名直取即 200。

封面：卡片 data-original 指向 cms.meilinvps.com / pic.kuyu.eu.org 的 img.php?url=
      图片代理，实测 HTTP 200 image/jpeg，直接返回即可（无需本地代理代取）。

--------------------------------------------------------------------------------
id 约定（重要）
--------------------------------------------------------------------------------
因站点的详情页 / 播放页 URL 都带 {type} 段，而 hash 里不含类型信息，故把 type 一并
编入 id，保证任意环节都能还原完整 URL：
    视频 id（vod_id）  :  {type}/{hash}                 例 dianshiju/dd371199...
    剧集 id（播放入口）:  {type}/{hash}/{sid}-{pid}     例 dianshiju/dd371199.../3-1
"""
import sys
import re
import json
import time
import html as html_mod
from urllib.parse import quote, urljoin

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
    HAS_REQUESTS = True
except Exception:
    requests = None
    HAS_REQUESTS = False

try:
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except Exception:
    _BaseSpider = None


HOST = "https://www.youxinju.com"

# 桌面 UA：列表 / 详情 / 播放页
UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 移动端 UA：请求 data-url（1234sp.cc）时必需，否则拿不到 iframe
UA_MOB = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
          "Mobile/15E148 Safari/604.1")

PROXY_API = "https://ttss.langfeng888.com/proxy/"     # 播放接口

TIMEOUT = 15

# 分类兜底表（首页实勘）
CATS_FALLBACK = [
    ("dianying", "电影"),
    ("dianshiju", "电视剧"),
    ("dongman", "动漫"),
    ("zongyi", "综艺"),
    ("skits", "短剧"),
]

_TYPES = r"(?:dianying|dianshiju|dongman|zongyi|skits)"

# ---------- 列表页正则 ----------
_CARD_RE = re.compile(r'<li class="public-list-box[^"]*">(?:(?!</li>).)*?</li>', re.S)
_CARD_HREF_RE = re.compile(r'<a class="public-list-exp"\s+href="/(%(T)s/[0-9a-zA-Z]{16,})\.html"' % {"T": _TYPES})
_ANY_HREF_RE = re.compile(r'href="/(%(T)s/([0-9a-zA-Z]{16,}))\.html"' % {"T": _TYPES})
_ALT_RE = re.compile(r'alt="([^"]*)"')
_IMG_RE = re.compile(r'data-original="([^"]+)"')
_NOTE_RE = re.compile(r'<span class="public-prt[^"]*">([^<]*)</span>')
_TITLE_RE = re.compile(r'<h3 class="time-title[^"]*"><a[^>]*>([^<]+)</a></h3>')
_SUB_RE = re.compile(r'<p class="public-list-subtitle[^"]*">(.*?)</p>', re.S)
_PAGE_RE = re.compile(r'/%(T)s-p(\d+)\.html|/%(T)s/p(\d+)\.html' % {"T": _TYPES})

# ---------- 详情页正则 ----------
_LINE_RE = re.compile(r'<a class="anthology-item[^"]*"[^>]*>([^<]+)</a>')
_EP_RE = re.compile(
    r'<a[^>]*href="/%(T)s/[0-9a-zA-Z]{16,}/(\d+)-(\d+)\.html"[^>]*>\s*([^<]{1,30}?)\s*</a>' % {"T": _TYPES}
)
# 每个「选集分组」的 <ul>（分组出现顺序 == 线路标签出现顺序）
_UL_RE = re.compile(r'<ul class="anthology-list-play[^"]*"[^>]*>(.*?)</ul>', re.S)

# ---------- 播放页 / 播放器正则 ----------
_DATA_URL_RE = re.compile(r'data-ff-player[^>]*?data-url="([^"]+)"')
_DATA_URL2_RE = re.compile(r'data-url="(https?://[^"]+)"')
_IFRAME_RE = re.compile(r'<iframe[^>]*\ssrc="([^"]+)"', re.I)
_V_RE = re.compile(r'[?&]v=([^&#]+)')
_PLAYER_IFRAME_RE = re.compile(r'^https?://[^/]+/[^/]*/player\.html\?')

_VIDEO_EXT = (".m3u8", ".mp4", ".m4v", ".mov", ".mkv", ".flv", ".webm", ".ts")


def _clean(text):
    """去标签 + 反转义 + 压空白。"""
    if not text:
        return ""
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def _abs(url):
    """站内相对链接补成绝对地址。"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return HOST + url
    return HOST + "/" + url


def _page(pg):
    try:
        v = int(str(pg or "").strip())
        return v if v > 0 else 1
    except Exception:
        return 1


def _play_sources(html, vid):
    """把详情/播放页解析成「线路名 -> 剧集列表」。

    关键坑：线路标签(tab)的**出现顺序**才等于选集分组的出现顺序，
    而分组的真实 sid 是后台自增 ID，顺序是乱的。

    本站《余红旧事》实测：
        tab 顺序     自建y / 4K自建 / 超清④ / 超清⑥
        分组 sid      3    /  4    /  2    /  1
    若按 sid 升序去对应标签名，点「4K自建」实际拿到的是 sid=2（超清④）的源，
    表现出来就是「选了 4K 线路，播放只有 1920×1080」。

    返回 [(线路名, "第x集$vid/sid-pid#..."), ...]，顺序与页面展示一致。
    """
    # 线路标签名（按出现顺序）
    lines = [_clean(x) for x in _LINE_RE.findall(html or "")]
    # 各分组的真实 sid（按出现顺序，与 tab 一一对应）
    group_sids, seen = [], set()
    for blk in _UL_RE.finditer(html or ""):
        m = _EP_RE.search(blk.group(1))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            group_sids.append(m.group(1))
    # 各分组的剧集：{sid: {pid: 名称}}
    groups = {}
    for sid, pid, epname in _EP_RE.findall(html or ""):
        groups.setdefault(sid, {})[pid] = _clean(epname) or ("第%s集" % pid)
    # tab 顺序 -> sid 顺序 的配对
    name_by_sid = {}
    for i, sid in enumerate(group_sids):
        name = lines[i].strip() if i < len(lines) else ""
        name_by_sid[sid] = name or ("线路%s" % sid)

    order = [s for s in group_sids if s in groups]
    for s in sorted(groups.keys(), key=lambda x: int(x)):
        if s not in order:
            order.append(s)

    out = []
    for sid in order:
        eps = groups.get(sid) or {}
        parts = []
        for pid in sorted(eps.keys(), key=lambda x: int(x)):
            parts.append("%s$%s/%s-%s" % (eps[pid], vid, sid, pid))
        if parts:
            out.append((name_by_sid.get(sid) or ("线路%s" % sid), "#".join(parts)))
    return out


class Spider(_BaseSpider if _BaseSpider else object):
    """有新剧采集源"""

    name = "有新剧"
    base_url = HOST

    searchable = 1
    quickSearch = 1
    filterable = 1
    changeable = 1

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self.host = HOST
        self.timeout = TIMEOUT
        self._session = None
        self._last_req = 0.0
        self._min_interval = 0.12        # 轻微限速，避免被 WAF 掐连接
        self._cats_cache = None
        self._play_cache = {}            # 播放解析缓存: play_id -> 真实地址

    # ---------------- 基础设施 ----------------
    def getName(self):
        return self.name

    def init(self, extend=""):
        """支持 extend 覆盖 host / UA / cookie（换域名时无需改代码）。"""
        if not extend:
            return
        cfg = {}
        if isinstance(extend, dict):
            cfg = dict(extend)
        else:
            try:
                cfg = json.loads(str(extend))
            except Exception:
                cfg = {}
        if not isinstance(cfg, dict):
            return
        host = (cfg.get("host") or cfg.get("HOST") or "").strip().rstrip("/")
        if host:
            self.host = host
            self.base_url = host

    def isVideoFormat(self, url):
        u = (url or "").lower()
        return any(x in u for x in _VIDEO_EXT)

    def manualVideoCheck(self):
        return False

    def destroy(self):
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass

    def _session_get(self):
        if not HAS_REQUESTS:
            return None
        if self._session is None:
            self._session = requests.Session()
            self._session.verify = False
        return self._session

    def _throttle(self):
        el = time.time() - self._last_req
        if 0 < el < self._min_interval:
            time.sleep(self._min_interval - el)
        self._last_req = time.time()

    def _fetch(self, url, ua=UA_PC, referer="", json_mode=False):
        """统一取页面；json_mode=True 时返回 dict。"""
        if not url or not HAS_REQUESTS:
            return None if json_mode else ""
        s = self._session_get()
        if s is None:
            return None if json_mode else ""
        headers = {
            "User-Agent": ua,
            "Accept": "application/json" if json_mode
                      else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer or (self.host + "/"),
        }
        for attempt in range(2):                       # 站点偶发断连，重试一次
            try:
                self._throttle()
                r = s.get(url, headers=headers, timeout=self.timeout)
                if r.status_code == 200:
                    if json_mode:
                        try:
                            return json.loads(r.content.decode("utf-8", "replace"))
                        except Exception:
                            return None
                    r.encoding = r.apparent_encoding or "utf-8"
                    return r.text
                if r.status_code in (403, 404, 410):
                    break
            except Exception:
                pass
            time.sleep(0.4)
        return None if json_mode else ""

    # ---------------- 分类 ----------------
    def _categorise(self, html):
        cats, seen = [], set()
        for m in re.finditer(r'<a[^>]*href="/(%s)/p1\.html"[^>]*>([^<]+)</a>' % _TYPES, html or ""):
            tid, name = m.group(1), _clean(m.group(2))
            if not tid or not name or tid in seen or name == "首页":
                continue
            seen.add(tid)
            cats.append({"type_id": tid, "type_name": name})
        return cats

    def _categories(self):
        if self._cats_cache is not None:
            return self._cats_cache
        cats = self._categorise(self._fetch(self.host + "/"))
        if not cats:
            cats = [{"type_id": t, "type_name": n} for t, n in CATS_FALLBACK]
        self._cats_cache = cats
        return cats

    # ---------------- 列表卡片解析 ----------------
    def _parse_cards(self, html, limit=60):
        items, seen = [], set()
        if not html:
            return items
        for blk in _CARD_RE.finditer(html):
            b = blk.group(0)
            hm = _CARD_HREF_RE.search(b) or _ANY_HREF_RE.search(b)
            if not hm:
                continue
            vid = hm.group(1)                      # {type}/{hash}
            if vid in seen:
                continue
            pm = _IMG_RE.search(b)
            am = _ALT_RE.search(b)
            tm = _TITLE_RE.search(b)
            name = _clean(tm.group(1)) if tm else ""
            if not name:
                name = _clean(am.group(1)) if am else ""
            if not name:
                continue
            nm = _NOTE_RE.search(b)
            seen.add(vid)
            items.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": _abs(pm.group(1)) if pm else "",
                "vod_remarks": _clean(nm.group(1)) if nm else "",
            })
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _pagecount(html, default=1):
        nums = []
        for m in _PAGE_RE.finditer(html or ""):
            g = m.group(2) or m.group(4)
            if g and g.isdigit():
                nums.append(int(g))
        return max(nums) if nums else default

    # ---------------- 首页 ----------------
    def homeContent(self, filter=False):
        html = self._fetch(self.host + "/")
        return {"class": self._categories(), "filters": {}, "list": self._parse_cards(html)}

    def homeVideoContent(self):
        return {"list": self._parse_cards(self._fetch(self.host + "/"))}

    # ---------------- 分类 ----------------
    def categoryContent(self, tid, pg, filter, extend):
        pg = _page(pg)
        tid = str(tid or "").strip().strip("/") or "dianshiju"
        url = "%s/%s/p%d.html" % (self.host, tid, pg)
        html = self._fetch(url)
        items = self._parse_cards(html)
        pagecount = self._pagecount(html, default=pg)
        if not items:
            pagecount = pg
        return {
            "list": items,
            "page": pg,
            "pagecount": max(pagecount, pg),
            "limit": len(items),
            "total": len(items) * max(pagecount, 1),
        }

    # ---------------- 详情 ----------------
    def detailContent(self, ids):
        if isinstance(ids, (list, tuple)):
            vid = str(ids[0]) if ids else ""
        else:
            vid = str(ids or "")
        vid = vid.strip().strip("/")
        if not vid:
            return {"list": []}

        url = "%s/%s.html" % (self.host, vid)
        html = self._fetch(url)
        if not html and self.isVideoFormat(vid):
            html = ""
        if not html:
            return {"list": []}

        # 片名：h1 -> title -> og:title
        name = ""
        hm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if hm:
            name = _clean(hm.group(1))
        if not name:
            tm = re.search(r"<title>([^<]+)</title>", html)
            if tm:
                name = _clean(tm.group(1)).split("_")[0].strip()
        if not name:
            mt = re.search(r'<meta[^>]+og:title"[^>]+content="([^"]+)"', html)
            if mt:
                name = _clean(mt.group(1))

        # 封面：详情大图 data-pic / lazyload img / meta image
        pic = ""
        for pat in (r'data-pic="([^"]+)"',
                    r'<img[^>]*class="[^"]*lazyload[^"]*"[^>]*data-original="([^"]+)"',
                    r'<img[^>]*data-original="([^"]+)"'):
            pm = re.search(pat, html)
            if pm:
                pic = _abs(pm.group(1))
                break

        # 简介
        content = ""
        cm = re.search(r'<meta name="description" content="([^"]*)"', html)
        if cm:
            content = _clean(cm.group(1))

        # 线路 + 剧集（tab 顺序 ↔ 分组 sid 顺序，详见 _play_sources 注释）
        sources = _play_sources(html, vid)
        play_from = [n for n, _ in sources]
        play_url = [u for _, u in sources]

        vod = {
            "vod_id": vid,
            "vod_name": name,
            "vod_pic": pic,
            "vod_year": "",
            "vod_area": "",
            "vod_actor": "",
            "vod_director": "",
            "vod_remarks": "",
            "vod_content": content,
            "vod_play_from": "$$$".join(play_from),
            "vod_play_url": "$$$".join(play_url),
        }
        return {"list": [vod]}

    # ---------------- 播放：4 跳解析 ----------------
    def _page_data_url(self, page_url):
        """第 1 跳：播放页里的 data-url。"""
        html = self._fetch(page_url)
        if not html:
            return ""
        m = _DATA_URL_RE.search(html)
        if m and m.group(1).startswith("http"):
            return m.group(1)
        for cand in _DATA_URL2_RE.findall(html):
            if "1234sp.cc" in cand or "/p/" in cand:
                return cand
        return m.group(1) if m else (_DATA_URL2_RE.search(html).group(1)
                                     if _DATA_URL2_RE.search(html) else "")

    def _data_url_to_player(self, data_url):
        """第 2 跳：data-url -> 播放器 iframe（必须移动端 UA）。"""
        html = self._fetch(data_url, ua=UA_MOB, referer=self.host + "/")
        if not html:
            return ""
        for src in _IFRAME_RE.findall(html):
            if _PLAYER_IFRAME_RE.match(src or ""):
                return src
        return _IFRAME_RE.search(html).group(1) if _IFRAME_RE.search(html) else ""

    def _player_to_real(self, player_url):
        """第 3/4 跳：取 v -> 调播放接口 -> 真实视频地址。"""
        vm = _V_RE.search(player_url or "")
        if not vm:
            return ""
        v = vm.group(1)
        api = PROXY_API + quote(v, safe="")
        data = self._fetch(api, ua=UA_MOB, json_mode=True)
        if not isinstance(data, dict):
            return ""
        if data.get("code") != 200:
            return ""
        u = data.get("url")
        if not u or not isinstance(u, str):
            return ""
        # 复刻 JS: new URL(json.url.trim(), api).href
        return urljoin(api, u.strip())

    def _resolve_play(self, play_id):
        """串起完整链路；带缓存，避免同一集反复走 4 跳。"""
        play_id = str(play_id or "").strip().strip("/")
        if not play_id:
            return ""
        if play_id in self._play_cache:
            return self._play_cache[play_id]
        page_url = "%s/%s.html" % (self.host, play_id)
        real = ""
        try:
            data_url = self._page_data_url(page_url)
            if data_url:
                player_url = self._data_url_to_player(data_url)
                if player_url:
                    real = self._player_to_real(player_url)
        except Exception:
            real = ""
        if len(self._play_cache) > 200:
            self._play_cache.clear()
        self._play_cache[play_id] = real
        return real

    def playerContent(self, flag, id, vipFlags=None):
        play_id = str(id or "").strip().strip("/")
        page_url = "%s/%s.html" % (self.host, play_id)

        # 若已经是直链（外部传入），直接播
        if self.isVideoFormat(play_id):
            return {
                "parse": 0, "playUrl": "", "url": play_id if play_id.startswith("http") else page_url,
                "header": {"User-Agent": UA_MOB},
            }

        real = self._resolve_play(play_id)
        if real:
            # 最终视频域不需要 Referer（播放器声明 no-referrer，实测匿名直取 200）
            out = {"parse": 0, "playUrl": "", "url": real, "header": {"User-Agent": UA_MOB}}
            if ".m3u8" in real.lower():
                out["format"] = "application/x-mpegURL"
            return out

        # 兜底：交给 TVBox 解析播放页
        return {"parse": 1, "playUrl": "", "url": page_url, "header": {"User-Agent": UA_PC}}

    # ---------------- 搜索 ----------------
    def searchContent(self, key, quick=False, pg="1"):
        kw = (key or "").strip()
        if not kw:
            return {"list": [], "page": 1, "pagecount": 1, "limit": 24, "total": 0}
        pg = _page(pg)
        cands = ["%s/search/%s.html" % (self.host, quote(kw))]
        if pg > 1:
            cands.insert(0, "%s/search/%s-p%d.html" % (self.host, quote(kw), pg))
        cands.append("%s/search.html?wd=%s" % (self.host, quote(kw)))
        items = []
        for u in cands:
            html = self._fetch(u)
            if not html:
                continue
            items = self._parse_cards(html)
            if items:
                break
        return {
            "list": items,
            "page": pg,
            "pagecount": pg if items else 1,
            "limit": len(items),
            "total": len(items),
        }

    # ---------------- 本地代理（契约） ----------------
    def localProxy(self, param):
        return [404, "text/plain", b""]

    def proxy(self, param):
        return self.localProxy(param)


# ============================================================
# 模块级转发（drpy 运行时调用这些函数）
# ============================================================
_spider = Spider()


def init(extend=""):
    return _spider.init(extend)


def getName():
    return _spider.getName()


def getDependence():
    return []


def isVideoFormat(url):
    return _spider.isVideoFormat(url)


def homeContent(filter=False):
    return _spider.homeContent(filter)


def homeVideoContent():
    return _spider.homeVideoContent()


def categoryContent(tid, pg, filter, extend):
    return _spider.categoryContent(tid, pg, filter, extend)


def detailContent(ids):
    return _spider.detailContent(ids)


def playerContent(flag, id, vipFlags=None):
    return _spider.playerContent(flag, id, vipFlags)


def searchContent(key, quick=False, pg="1"):
    return _spider.searchContent(key, quick, pg)


def localProxy(param):
    return _spider.localProxy(param)


def proxy(param):
    return _spider.proxy(param)


def destroy():
    return _spider.destroy()
