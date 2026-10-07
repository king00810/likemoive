# -*- coding: utf-8 -*-
"""
埋堆堆 (m.mddcloud.com.cn) - TVBox 爬虫源
=========================================
接口：homeContent / homeVideoContent / categoryContent(含筛选) / detailContent(懒加载)
     / playerContent(按需解析直链) / searchContent(全库聚合 + 模糊匹配)

搜索实现：
- 站内无搜索接口，searchContent 走 3 排序 × 18 页并发扫全库 (~1800 条 ≈ 全库)
- 首屏即全库（不依赖后台补齐），ThreadPoolExecutor 并发度 8，冷启动 ~2s
- __init__ 预热线程：用户浏览首页时后台扫语料，首次搜索命中缓存零网络
- 匹配算法：完整匹配 / 变体全等 / 变体包含 / token 匹配 / 首字母缩写 / 字符重叠
"""

import re
import json
import time
import threading
import hashlib

import requests
from requests.adapters import HTTPAdapter

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
except ImportError:
    ThreadPoolExecutor = None
    as_completed = None

try:
    import sys
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except ImportError:
    _BaseSpider = None


# ============================================================
# 常量
# ============================================================

HOST_CANDIDATES = [
    "https://m.mddcloud.com.cn",
    "https://www.mddcloud.com.cn",
    "https://mddcloud.com.cn",
]

API_CANDIDATES = [
    "https://activity.mddcloud.com.cn",
]

PRIVATE_KEY = "8a4af424bd714be5b2baacbe3cf5bbbe0131effeb92e99356ef1662884558f28"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

TIMEOUT_API = 5

# 缓存 TTL（秒）
TTL_WORDS = 24 * 3600
TTL_CAT = 300
TTL_DETAIL_OK = 300
TTL_DETAIL_EMPTY = 30
TTL_SEARCH = 180
TTL_SEARCH_CORPUS = 1800     # 搜索语料 30min
TTL_PLAY = 900
TTL_DOMAIN = 1800

# 播放直链优先级
PLAY_FIELD_PRIORITY = ("tryM3u8Url", "tryUrl", "tryMp4Url")

MAX_EPISODES = 500

# 搜索聚合（全库并发扫描）
SEARCH_ROWS = 100            # API 支持 100/页
SEARCH_PAGES = 18            # 18 页 × 3 排序 ≈ 全库
SEARCH_FAST_PAGES = 18       # 首屏 = 全库，无后台补齐
SEARCH_SORTS = ("0", "1", "2")   # 默认/最新/好评
SEARCH_MAX_RESULTS = 100

# 签名序列化（前端 JS 无空格，默认 ", " 会导致签名错）
_SIGN_SEPARATORS = (',', ':')

# 分词/去噪
_NAME_NOISE = re.compile(r'[()（）【】\[\]、，,。.·\-—~&/\\|:：!？!?"\'“”‘’\s]+')
_TOKEN_SPLIT = re.compile(r'[()\[\]{}<>《》【】「」『』\u3001，,。.·\-—/\\|:：!？!?\"\'"\s]+')


# ============================================================
# 签名 API 客户端
# ============================================================
class MddApi:
    def __init__(self, session, extend_domain=""):
        self.session = session
        self._api = API_CANDIDATES[0]
        if extend_domain:
            d = extend_domain.strip()
            if not d.startswith('http'):
                d = 'https://' + d
            self._api = d.rstrip('/')

    def _sign_data(self, data):
        """与前端 JS 一致：dict/list 用紧凑 JSON（separators=(',',':')）"""
        parts = []
        for k in sorted(data.keys()):
            v = data[k]
            if isinstance(v, (dict, list)):
                parts.append("%s=%s&" % (k, json.dumps(
                    v, ensure_ascii=False, separators=_SIGN_SEPARATORS)))
            else:
                parts.append("%s=%s&" % (k, v))
        return "".join(parts)

    def post(self, path, data, timeout=TIMEOUT_API):
        now = int(time.time() * 1000)
        sorted_str = self._sign_data(data)
        msg = "time:%s|privateKey:%s|data:%s" % (now, PRIVATE_KEY, sorted_str)
        sign = hashlib.md5(msg.encode('utf-8')).hexdigest()
        body = {"time": now, "sign": sign, "data": data}
        for attempt in range(2):
            try:
                r = self.session.post(
                    self._api + path, json=body, timeout=timeout,
                    headers={'Connection': 'keep-alive'},
                )
                if r.status_code == 429:
                    time.sleep(1.0)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 0:
                    time.sleep(0.2)
                else:
                    return None
        return None


# ============================================================
# Spider 主类
# ============================================================
_Base = _BaseSpider if _BaseSpider is not None else object


class Spider(_Base):
    siteUrl = HOST_CANDIDATES[0]
    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': HOST_CANDIDATES[0] + '/',
    }

    # ===== 初始化 =====
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.headers['Connection'] = 'keep-alive'
        self.session.verify = False
        adapter = HTTPAdapter(
            pool_connections=20, pool_maxsize=40,
            max_retries=0, pool_block=False,
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        self.api = MddApi(self.session)
        self.extend = ""

        self._host = HOST_CANDIDATES[0]
        self._host_checked = 0.0
        self._host_fail = 0

        # 缓存容器 + RLock（并发扫语料合并需要嵌套加锁）
        self._lock = threading.RLock()
        self._words_cache = None
        self._words_time = 0
        self._cat_cache = {}
        self._detail_cache = {}
        self._search_cache = {}
        self._search_corpus_cache = None
        self._search_corpus_time = 0
        self._play_cache = {}
        self._prefetching = set()

        # 搜索语料预热线程（__init__ 触发，用户浏览首页时后台扫全库）
        self._search_warmup_task = None
        self._search_warmup_ok = True
        self._search_warmup_started = False
        try:
            self._search_warmup_started = True
            self._search_warmup_task = threading.Thread(
                target=self._search_corpus_scan, daemon=True)
            self._search_warmup_task.start()
        except Exception:
            self._search_warmup_ok = False
            self._search_warmup_started = False
            self._search_warmup_task = None

    def init(self, extend=""):
        self.extend = extend or ""
        for kv in self.extend.split(','):
            if '=' in kv:
                k, v = kv.split('=', 1)
                k, v = k.strip().lower(), v.strip()
                if k == 'host' and v:
                    d = v if v.startswith('http') else 'https://' + v
                    self._host = d.rstrip('/')
                    self.headers['Referer'] = self._host + '/'
                    self.session.headers['Referer'] = self._host + '/'
                elif k == 'api' and v:
                    self.api = MddApi(self.session, v)

    # ===== 域名跟踪 =====
    def _check_host(self, host):
        try:
            r = self.session.get(host + '/', timeout=TIMEOUT_API)
            if r.status_code != 200:
                return False
            html = r.text[:200000]
            return ('埋堆堆' in html) and ('/video/' in html)
        except Exception:
            return False

    def host(self):
        now = time.time()
        with self._lock:
            if now - self._host_checked < TTL_DOMAIN and self._host_fail < 3:
                return self._host
        for h in HOST_CANDIDATES:
            if self._check_host(h):
                with self._lock:
                    self._host = h
                    self._host_checked = now
                    self._host_fail = 0
                return h
        with self._lock:
            self._host_checked = now - TTL_DOMAIN + 60
            self._host_fail += 1
        return self._host

    # ===== 缓存 =====
    @staticmethod
    def _cache_get(cache, key, ttl=None):
        item = cache.get(key)
        if not item or len(item) < 3:
            return None
        if time.time() - item[0] >= (ttl if ttl is not None else item[2]):
            return None
        return item[1]

    @staticmethod
    def _cache_set(cache, key, value, ttl=TTL_CAT, max_size=1024):
        if len(cache) >= max_size:
            expired = [k for k, v in list(cache.items())
                       if time.time() - v[0] >= (v[2] if len(v) > 2 else ttl)]
            if expired:
                for k in expired:
                    del cache[k]
            elif len(cache) >= max_size:
                cache.clear()
        cache[key] = (time.time(), value, ttl)

    @staticmethod
    def _pic(u):
        u = (u or '').strip()
        if not u:
            return ''
        if u.startswith('//'):
            return 'https:' + u
        if u.startswith('http://'):
            return 'https://' + u[len('http://'):]
        return u

    # ===== 筛选词表 =====
    def _classify_words(self):
        with self._lock:
            if (self._words_cache is not None
                    and time.time() - self._words_time < TTL_WORDS):
                return self._words_cache
        data = self.api.post("/api/vod/classify/list.action", {})
        words = {}
        if data and data.get("msgType") == 0 and data.get("data"):
            for item in data["data"]:
                fc = item.get("fieldCode") or ''
                ws = []
                for w in (item.get("words") or []):
                    uuid = str(w.get("uuid") or '')
                    name = str(w.get("word") or '').strip()
                    if name and name != "全部" and uuid:
                        ws.append({"word": name, "uuid": uuid})
                if fc and ws:
                    words[fc] = ws
        with self._lock:
            if words:
                self._words_cache = words
                self._words_time = time.time()
                return words
            return self._words_cache or {}

    def _word_uuid(self, field_code, word):
        for w in self._classify_words().get(field_code, []):
            if w["word"] == word:
                return w["uuid"]
        return ''

    def _resolve_uuid(self, field_code, raw):
        """接受词名或 uuid（放弃格式启发式，词表精确匹配）"""
        raw = str(raw or '').strip()
        if not raw:
            return ''
        words = self._classify_words().get(field_code, []) or []
        for w in words:
            if w.get("word") == raw:
                return w.get("uuid") or ''
        for w in words:
            if w.get("uuid") == raw:
                return raw
        return ''

    # ============================================================
    # 首页 / 列表
    # ============================================================
    def _fetch_list(self, uuid_list=None, rows=36, start_row=0,
                    is_vip="-1", order_type="0"):
        data = {
            "uuidList": uuid_list or [],
            "rows": rows,
            "startRow": start_row,
            "isVip": str(is_vip),
            "orderType": str(order_type),
        }
        resp = self.api.post("/api/vod/classify/word/search.action", data)
        if not resp or resp.get("msgType") != 0:
            return []
        out = []
        for v in (resp.get("data") or []):
            vid = str(v.get("uuid") or v.get("vodUuid") or '')
            name = str(v.get("name") or '').strip()
            if not vid or not name:
                continue
            out.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": self._pic(v.get("coverImage") or v.get("downImage")),
                "vod_remarks": self._remarks(v),
                # 内部字段（详情元信息回填）
                "_material": str(v.get("materialName") or ''),
                "_year": str(v.get("yearName") or ''),
            })
        return out

    @staticmethod
    def _remarks(v):
        if str(v.get("isVip")) in ('1', 'True', 'true'):
            return "VIP"
        if str(v.get("isTimeLimit")) in ('1', 'True', 'true'):
            return "限免"
        return ''

    def _all_filters(self):
        words = self._classify_words()

        def _mk(fc, name):
            value = [{"n": "全部", "v": ""}]
            for w in words.get(fc, []):
                value.append({"n": w["word"], "v": w["uuid"]})
            return {"key": fc, "name": name, "value": value}

        return [
            _mk("lang", "语言"),
            _mk("year", "年份"),
            _mk("material", "题材"),
            _mk("region", "地区"),
            {"key": "isvip", "name": "收费", "value": [
                {"n": "全部", "v": "-1"},
                {"n": "限免", "v": "0"},
                {"n": "VIP", "v": "1"},
            ]},
            {"key": "order", "name": "排序", "value": [
                {"n": "最热", "v": "0"},
                {"n": "最新", "v": "1"},
                {"n": "好评", "v": "2"},
            ]},
        ]

    def homeContent(self, filter):
        result = {'class': list(ALL_CLASSES)}
        if filter:
            try:
                fl = self._all_filters()
                result['filters'] = {str(c["type_id"]): fl for c in ALL_CLASSES}
            except Exception:
                pass
        return result

    def homeVideoContent(self):
        cached = self._cache_get(self._cat_cache, "__home__", TTL_CAT)
        if cached is not None:
            return {"list": cached}
        lst = self._fetch_list(order_type="0", rows=60)
        if lst:
            self._cache_set(self._cat_cache, "__home__", lst, TTL_CAT)
        return {"list": lst}

    # ============================================================
    # 分类列表（含二级筛选）
    # ============================================================
    def categoryContent(self, tid, pg, filter, extend):
        page = 1
        try:
            page = max(1, int(pg or 1))
            ext = {}
            if extend:
                if isinstance(extend, dict):
                    ext = extend
                elif isinstance(extend, str):
                    try:
                        ext = json.loads(extend)
                    except Exception:
                        ext = {}

            preset = CLASS_PRESET.get(str(tid), {})

            # 多维度 uuidList（用 _resolve_uuid 统一解析，接受词名或 uuid）
            uuid_list = []
            for fc in ("lang", "material", "year", "region"):
                val = str(ext.get(fc) or '').strip()
                if not val:
                    continue
                v = self._resolve_uuid(fc, val)
                if v:
                    uuid_list.append(v)
            # 无二级时走一级预设
            if not uuid_list:
                for fc in ("lang", "year", "material", "region"):
                    pword = preset.get(fc)
                    if pword:
                        u = self._word_uuid(fc, pword)
                        if u:
                            uuid_list = [u]
                            break
            is_vip = str(ext.get("isvip") or preset.get("isvip") or "-1")
            is_vip = is_vip.strip() or "-1"
            order = str(ext.get("order") or "0").strip() or "0"

            ckey = "%s|%d|%s" % (
                tid, page, json.dumps(ext, ensure_ascii=False, sort_keys=True))
            cached = self._cache_get(self._cat_cache, ckey, TTL_CAT)
            if cached is not None:
                return cached

            lst = self._fetch_list(
                uuid_list=uuid_list, rows=36,
                start_row=(page - 1) * 36,
                is_vip=is_vip, order_type=order,
            )
            for item in lst:
                item.pop("_material", None)
                item.pop("_year", None)
            pagecount = 100 if lst and len(lst) == 36 else 1
            result = {
                "list": lst,
                "page": page,
                "pagecount": pagecount,
                "limit": 36,
                "total": pagecount * 36,
            }
            self._cache_set(self._cat_cache, ckey, result, TTL_CAT)
            return result
        except Exception:
            return {"list": [], "page": page, "pagecount": 1, "limit": 36, "total": 0}

    # ============================================================
    # 详情页（懒加载）
    # ============================================================
    def detailContent(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        vid = str(ids[0]).split(',')[0].strip()
        if not vid:
            return {"list": []}

        cached = self._cache_get(self._detail_cache, vid, None)
        if cached is not None:
            return cached

        result = self._fetch_detail(vid)
        ttl = TTL_DETAIL_OK if result.get("list") else TTL_DETAIL_EMPTY
        self._cache_set(self._detail_cache, vid, result, ttl)

        if result.get("list"):
            self._prefetch_play(result["list"][0])
        return result

    def _fetch_detail(self, vid):
        resp = self.api.post("/api/vod/getVodInfo.action", {"vodUuid": vid, "num": 1})
        if not resp or resp.get("msgType") != 0 or not resp.get("data"):
            return {"list": []}
        v = resp["data"]

        eps = []
        for s in (v.get("vodSactionList") or []):
            num = s.get("num")
            ep_name = str(s.get("name") or '').strip()
            if num is None:
                continue
            if not ep_name:
                ep_name = "第%s集" % num
            eps.append("%s$%s:%s" % (ep_name, vid, num))
        if len(eps) > MAX_EPISODES:
            eps = eps[-MAX_EPISODES:]

        year = ''
        m = re.search(r'(\d{4})', str(v.get("yearName") or ''))
        if m:
            year = m.group(1)
        material = str(v.get("materialName") or '').replace(',', '/')

        detail = {
            "vod_id": vid,
            "vod_name": str(v.get("name") or vid),
            "vod_pic": self._pic(v.get("coverImage") or v.get("sideImage") or v.get("downImage")),
            "type_name": material or '港剧',
            "vod_year": year,
            "vod_area": '中国香港',
            "vod_director": str(v.get("director") or '').replace(',', '/'),
            "vod_actor": str(v.get("starring") or '').replace(',', '/'),
            "vod_remarks": "更新至%s集" % v.get("updateNum") if v.get("updateNum") else '',
            "vod_content": str(v.get("introduction") or '').strip(),
            "vod_play_from": "埋堆堆",
            "vod_play_url": "#".join(eps),
        }
        return {"list": [detail]}

    # ============================================================
    # 播放解析
    # ============================================================
    def _resolve_play(self, play_key):
        cached = self._cache_get(self._play_cache, play_key, TTL_PLAY)
        if cached:
            return cached
        real = ''
        try:
            vid, num = str(play_key).split(':', 1)
            resp = self.api.post(
                "/api/vod/getTrySaction.action",
                {"vodUuid": vid.strip(), "num": int(num)},
            )
            if resp and resp.get("msgType") == 0 and resp.get("data"):
                d = resp["data"]
                for f in PLAY_FIELD_PRIORITY:
                    u = str(d.get(f) or '').strip()
                    if u:
                        real = self._pic(u)
                        break
        except Exception:
            real = ''
        if real:
            self._cache_set(self._play_cache, play_key, real, TTL_PLAY)
        return real

    def _first_play_key(self, vod):
        for item in (vod.get("vod_play_url") or "").split("#"):
            parts = item.split("$", 1)
            if len(parts) == 2 and parts[1]:
                return parts[1]
        return None

    def _next_play_key(self, vod, play_key):
        items = (vod.get("vod_play_url") or "").split("#")
        for i, item in enumerate(items):
            parts = item.split("$", 1)
            if len(parts) == 2 and parts[1] == play_key and i + 1 < len(items):
                nxt = items[i + 1].split("$", 1)
                if len(nxt) == 2 and nxt[1]:
                    return nxt[1]
        return None

    def _prefetch(self, play_key):
        if not play_key:
            return
        with self._lock:
            if (self._cache_get(self._play_cache, play_key, TTL_PLAY)
                    or play_key in self._prefetching):
                return
            self._prefetching.add(play_key)

        def _job():
            try:
                self._resolve_play(play_key)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._prefetching.discard(play_key)

        threading.Thread(target=_job, daemon=True).start()

    def _prefetch_play(self, vod):
        self._prefetch(self._first_play_key(vod))

    def _play_payload(self, playurl, play_key, vod):
        is_m3u8 = '.m3u8' in playurl.lower()
        payload = {
            "parse": 0,
            "playUrl": "",
            "url": playurl,
            "header": {
                "User-Agent": UA,
                "Referer": self.host() + "/",
                "Origin": self.host(),
            },
            "format": "application/x-mpegURL" if is_m3u8 else "",
            "contentType": "application/x-mpegURL" if is_m3u8 else "",
        }
        nxt = self._next_play_key(vod, play_key)
        if nxt:
            self._prefetch(nxt)
        return payload

    def playerContent(self, flag, id, vipFlags):
        if not id:
            return {"parse": 0, "playUrl": "", "url": ""}
        play_key = str(id).strip()
        if ':' not in play_key:
            u = self._pic(play_key)
            return {"parse": 0, "playUrl": "", "url": u,
                    "header": {"User-Agent": UA, "Referer": self.host() + "/"}}

        vod = self._cached_vod(play_key.split(':', 1)[0])

        cached = self._cache_get(self._play_cache, play_key, TTL_PLAY)
        if cached:
            return self._play_payload(cached, play_key, vod or {})

        m3u8 = self._resolve_play(play_key)
        if m3u8:
            return self._play_payload(m3u8, play_key, vod or {})

        return {
            "parse": 1,
            "playUrl": "",
            "url": play_key,
            "header": {"User-Agent": UA, "Referer": self.host() + "/"},
        }

    def _cached_vod(self, vid):
        c = self._cache_get(self._detail_cache, vid, TTL_DETAIL_OK)
        if c and c.get("list"):
            return c["list"][0]
        return None

    # ============================================================
    # 搜索（全库并发扫描 + 多档模糊匹配）
    # ============================================================
    def _search_corpus(self, force=False):
        """全库并发扫描（18 页 × 3 排序 ≈ 1800 条），30min 缓存。
        首屏即全库：FongMi 搜索结果列表是一次性的，后台补齐不会刷新已打开结果。
        __init__ 预热线程已提前扫过，用户通常首次搜索命中缓存零网络。
        """
        now = time.time()
        with self._lock:
            if (not force and self._search_corpus_cache is not None
                    and now - self._search_corpus_time < TTL_SEARCH_CORPUS):
                return self._search_corpus_cache
            if force:
                self._search_corpus_cache = {}

        # 若预热线程仍在跑，等它完成（最多 30s）
        with self._lock:
            active = self._search_warmup_task
            active_ok = self._search_warmup_ok

        if active is not None and active.is_alive() and active_ok:
            try:
                active.join(timeout=30.0)
            except Exception:
                pass
            with self._lock:
                return dict(self._search_corpus_cache or {})

        return self._search_corpus_scan()

    def _search_corpus_scan(self):
        """阻塞式全库并发扫描（最多 30s），返回 dict 快照。

        注意：不在持锁时开新线程 —— ThreadPoolExecutor 内部任务用 RLock 合并
        collected，若主线程持锁不放会死锁。此处 body 内联执行。
        """
        collected = {}
        try:
            tasks = [(sk, g) for sk in SEARCH_SORTS
                     for g in range(1, SEARCH_PAGES + 1)]
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(self._warmup_scan_one, sk, g, collected)
                        for sk, g in tasks]
                for f in as_completed(futs):
                    try:
                        f.result(timeout=8)
                    except Exception:
                        pass
            with self._lock:
                self._search_corpus_cache = collected
                self._search_corpus_time = time.time()
                self._search_warmup_ok = True
                self._search_warmup_task = None
            return dict(collected)
        except Exception:
            with self._lock:
                self._search_warmup_ok = False
                self._search_warmup_task = None
            return dict(collected)

    def _warmup_scan_one(self, sort_key, group, collected):
        """并发扫描单页，merge 到 collected（加锁保证并发安全）"""
        try:
            lst = self._fetch_list(rows=SEARCH_ROWS,
                                   start_row=(group - 1) * SEARCH_ROWS,
                                   order_type=sort_key, is_vip="-1")
            if not lst:
                return
            with self._lock:
                for v in lst:
                    vid = v["vod_id"]
                    if vid not in collected:
                        collected[vid] = dict(v)
        except Exception:
            return

    @staticmethod
    def _normalize_name(name):
        return _NAME_NOISE.sub('', str(name or '')).lower()

    @staticmethod
    def _tokens(name):
        toks = [t for t in _TOKEN_SPLIT.split(str(name or '')) if t]
        return toks if toks else ([_NAME_NOISE.sub('', str(name or '')).lower()]
                                  if str(name or '') else [])

    def _match_score(self, kw, kw_norm, kw_tokens, kw_initials, title,
                     title_norm, title_tokens, title_initials):
        """多档评分：全匹配 > 变体全等 > 变体包含 > token 匹配 > 缩写匹配 > 字符重叠"""
        best = 0
        if not kw_norm or not title_norm:
            return 0

        if kw_norm == title_norm:
            best = 100
        if kw_norm in title_tokens or title_norm in kw_tokens:
            best = max(best, 85)
        if kw_norm in title_norm:
            best = max(best, 65)
        if kw_tokens and title_tokens:
            if all(t in title_tokens for t in kw_tokens):
                best = max(best, 55)
            elif any(t in title_tokens for t in kw_tokens):
                best = max(best, 40)
        if kw_initials and title_initials and len(kw_initials) >= 2:
            if kw_initials in title_initials:
                best = max(best, 30)
        if best == 0 and len(kw_norm) >= 2:
            tset = set(title_norm)
            overlap = sum(1 for c in set(kw_norm) if c in tset)
            if overlap >= max(2, len(set(kw_norm)) * 0.6):
                best = max(best, min(20, overlap * 3))
        return best

    def searchContent(self, key, quick=False, pg="1"):
        """FongMi 规范：必须回 page/pagecount/limit/total；无数据 pagecount=1。"""
        if not key:
            return {"list": [], "page": 1, "pagecount": 1,
                    "limit": SEARCH_MAX_RESULTS, "total": 0}
        kw = str(key).strip()
        if not kw:
            return {"list": [], "page": 1, "pagecount": 1,
                    "limit": SEARCH_MAX_RESULTS, "total": 0}
        try:
            page = max(1, int(pg))
        except (TypeError, ValueError):
            page = 1
        ckey = "__search__|%s" % kw
        cached = self._cache_get(self._search_cache, ckey, TTL_SEARCH)
        if cached is not None:
            total = len(cached)
            limit = SEARCH_MAX_RESULTS
            pagecount = max(1, (total + limit - 1) // limit)
            start = (page - 1) * limit
            return {
                "list": cached[start:start + limit],
                "page": page, "pagecount": pagecount,
                "limit": limit, "total": total,
            }

        kw_norm = self._normalize_name(kw)
        kw_tokens = [t.lower() for t in self._tokens(kw)]
        kw_initials = ''.join(t[0] for t in kw_tokens if t).lower()

        corpus = self._search_corpus()

        matched = []
        for item in corpus.values():
            title = item["vod_name"]
            title_norm = self._normalize_name(title)
            title_tokens = [t.lower() for t in self._tokens(title)]
            title_initials = ''.join(t[0] for t in title_tokens if t).lower()
            score = self._match_score(kw, kw_norm, kw_tokens, kw_initials,
                                      title, title_norm, title_tokens,
                                      title_initials)
            if score == 0:
                continue
            score -= min(5, max(0, len(title) - len(kw)))
            matched.append((score, title, item))

        matched.sort(key=lambda x: (-x[0], x[1]))
        result = [it[2] for it in matched[:SEARCH_MAX_RESULTS]]
        for it in result:
            it.pop("_material", None)
            it.pop("_year", None)

        if result:
            self._cache_set(self._search_cache, ckey, result, TTL_SEARCH)
        total = len(result)
        limit = SEARCH_MAX_RESULTS
        pagecount = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        return {
            "list": result[start:start + limit],
            "page": page, "pagecount": pagecount,
            "limit": limit, "total": total,
        }


# ============================================================
# 一级分类表
# ============================================================
ALL_CLASSES = [
    {"type_id": "all", "type_name": "全部港剧"},
    {"type_id": "yue", "type_name": "粤语剧"},
    {"type_id": "mandarin", "type_name": "普通话剧"},
    {"type_id": "free", "type_name": "限免"},
    {"type_id": "vip", "type_name": "VIP"},
]

CLASS_PRESET = {
    "all": {},
    "yue": {"lang": "粤语"},
    "mandarin": {"lang": "普通话"},
    "free": {"isvip": "0"},
    "vip": {"isvip": "1"},
}


# ============================================================
# 本地调试
# ============================================================
if __name__ == "__main__":
    import sys as _sys

    def p(obj, cut=500):
        s = json.dumps(obj, ensure_ascii=False, indent=1)
        print(s[:cut] + ("..." if len(s) > cut else ""))

    sp = Spider()
    sp.init("")

    print("\n===== 域名跟踪 =====")
    print("当前主站:", sp.host())

    print("\n===== searchContent(星空下的仁医) =====")
    t0 = time.time()
    s = sp.searchContent("星空下的仁医")
    print("耗时 %.2fs" % (time.time() - t0),
          "结果:", [v["vod_name"] for v in s["list"][:8]])

    print("\n===== searchContent(仁医) [变体] =====")
    s = sp.searchContent("仁医")
    print("结果:", [v["vod_name"] for v in s["list"][:8]])

    print("\n===== searchContent(爱回家) [长匹配] =====")
    s = sp.searchContent("爱回家")
    print("结果:", [v["vod_name"] for v in s["list"][:8]])

    print("\n===== searchContent(溏心) =====")
    s = sp.searchContent("溏心")
    print("结果:", [v["vod_name"] for v in s["list"][:8]])

    print("\n===== searchContent(反黑) =====")
    s = sp.searchContent("反黑")
    print("结果:", [v["vod_name"] for v in s["list"][:8]])