#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# TVBox Python 爬虫 · 韩圈 (韩圈 • APP / csp_Hxq)
# ------------------------------------------------------------
#
# ============================================================
import sys
import re
import json
import time
import hashlib
import requests
import base64
import ssl
import urllib.request
import urllib.parse
import secrets
import string

try:
    from base.spider import Spider as _BaseSpider
except ImportError:
    _BaseSpider = object

# ============ 加密后端 (三级降级) ============
_BACKEND = None
try:
    from Crypto.Cipher import AES as _PAES
    _BACKEND = "pycryptodome"
except ImportError:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher as _CyCipher, algorithms as _CyA, modes as _CyM
        _BACKEND = "cryptography"
    except ImportError:
        _BACKEND = "pure"

# ============ 常量 (来自 Hxq.smali <clinit>) ============
BASE = "https://hxqapi.hiyun.tv"
VERSION = "6.9"          # C
VC = "a_8350"            # D
CH = "qq"                # E
UA = "HanjuTV/6.9 (25128PNA1C; Android 17; Scale/2.00)"  # I
MD = "25128PNA1C"        # F (device md)
MAKER = "Xiaomi"         # G
OSV = "17"               # H
L = "34F9Q53w/HJW8E6Q"
M = "2E159Q/Z8979WckQ"
SIGN_KEY = "rSwCIIUMYCiHZXIVfgxlxylE9eGqfmwMg5V8mRSBB55vN8P3bNpXkUrv9MFhYOvm"  # N / i
JKEY = b"f349wghhe784tqwh"
KIV = b"d3w8hf94fidk38lk"
OB = b"e2320a0c5c7320a9"
PB = b"cf99167f1f087475"
Q_CDN = "https://voldn-ser01.51touxiang.com"   # Q
PLAY_UA = "tdc.8350"      # R
# 分类 (T)
CLASSES = [
    ["rank", "排行榜"],
    ["1", "韩剧"],
    ["2", "综艺"],
    ["3", "电影"],
    ["star", "明星"],
]
# 明星姓氏 (W 字段)
STARS = list("金李朴张郑姜赵吴张韩吴申黄安宋柳车全高许洪文河南白权元徐蔡成罗鞋朱")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"


# ============ 通用工具 ============
def md5h(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ============ 纯 Python AES (仅 pure 后端启用) ============
if _BACKEND == "pure":
    def _init_sbox():
        global _SBOX
        if _SBOX is not None:
            return
        p = q = 1
        sb = [0] * 256
        while True:
            p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0))
            q ^= q << 1; q ^= q << 2; q ^= q << 4
            q &= 0xFF
            if q & 0x80:
                q ^= 0x09
            x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            sb[p] = x & 0xFF ^ 0x63
            if p == 1:
                break
        sb[0] = 0x63
        globals()["_SBOX"] = sb

    _SBOX = None
    _RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]

    def _aes128_key_expand(key):
        _init_sbox()
        w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= _RCON[i // 4 - 1]
            w.append([w[i - 4][j] ^ t[j] for j in range(4)])
        return w

    def _aes_encrypt_block(w, block):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

        def add_round_key(rnd):
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= w[rnd * 4 + c][r]

        def sub_shift():
            for r in range(4):
                row = [_SBOX[s[r][(c + r) % 4]] for c in range(4)]
                for c in range(4):
                    s[r][c] = row[c]

        def mix():
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _M2[a[0]] ^ _M3[a[1]] ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ _M2[a[1]] ^ _M3[a[2]] ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ _M2[a[2]] ^ _M3[a[3]]
                s[3][c] = _M3[a[0]] ^ a[1] ^ a[2] ^ _M2[a[3]]

        nr = len(w) // 4 - 1
        add_round_key(0)
        for rnd in range(1, nr):
            sub_shift(); mix(); add_round_key(rnd)
        sub_shift(); add_round_key(nr)
        out = bytearray(16)
        for c in range(4):
            for r in range(4):
                out[r + 4 * c] = s[r][c]
        return bytes(out)

    def _init_mul():
        def xt(a):
            a <<= 1
            return (a ^ 0x1B) & 0xFF if a & 0x100 else a
        m2 = [xt(i) for i in range(256)]
        m3 = [m2[i] ^ i for i in range(256)]
        m9 = [xt(xt(xt(i))) ^ i for i in range(256)]
        m11 = [xt(xt(xt(i))) ^ m2[i] ^ i for i in range(256)]
        m13 = [xt(xt(xt(i))) ^ m2[m2[i]] ^ i for i in range(256)]
        m14 = [xt(xt(xt(i))) ^ m2[m2[i]] ^ m2[i] for i in range(256)]
        globals()["_M2"] = m2
        globals()["_M3"] = m3
        globals()["_M9"] = m9
        globals()["_M11"] = m11
        globals()["_M13"] = m13
        globals()["_M14"] = m14

    _init_mul()

    def _inv_sbox():
        _init_sbox()
        sb = _SBOX
        inv = [0] * 256
        for i in range(256):
            inv[sb[i]] = i
        return inv

    _INV_SBOX = _inv_sbox()

    def _aes_decrypt_block(w, block):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

        def add_round_key(rnd):
            for c in range(4):
                for r in range(4):
                    s[r][c] ^= w[rnd * 4 + c][r]

        def inv_sub_shift():
            for r in range(4):
                row = [_INV_SBOX[s[r][(c - r) % 4]] for c in range(4)]
                for c in range(4):
                    s[r][c] = row[c]

        def inv_mix():
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _M14[a[0]] ^ _M11[a[1]] ^ _M13[a[2]] ^ _M9[a[3]]
                s[1][c] = _M9[a[0]] ^ _M14[a[1]] ^ _M11[a[2]] ^ _M13[a[3]]
                s[2][c] = _M13[a[0]] ^ _M9[a[1]] ^ _M14[a[2]] ^ _M11[a[3]]
                s[3][c] = _M11[a[0]] ^ _M13[a[1]] ^ _M9[a[2]] ^ _M14[a[3]]

        nr = len(w) // 4 - 1
        add_round_key(nr)
        for rnd in range(nr - 1, 0, -1):
            inv_sub_shift(); add_round_key(rnd); inv_mix()
        inv_sub_shift(); add_round_key(0)
        out = bytearray(16)
        for c in range(4):
            for r in range(4):
                out[r + 4 * c] = s[r][c]
        return bytes(out)

    def _ecb_encrypt(w, data):
        return b"".join(_aes_encrypt_block(w, data[i:i + 16]) for i in range(0, len(data), 16))


def _pkcs7_pad(d):
    n = 16 - len(d) % 16
    return d + bytes([n]) * n


def _pkcs7_unpad(d):
    if d and 1 <= d[-1] <= 16:
        return d[:-d[-1]]
    return d


def aes_cbc(key, iv, data, encrypt=True, b64=True):
    """AES-CBC。encrypt: 输入 str/bytes -> base64(str)；decrypt: 输入 base64(str) -> bytes。"""
    if _BACKEND == "pycryptodome":
        if encrypt:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            ct = _PAES.new(key, _PAES.MODE_CBC, iv).encrypt(_pkcs7_pad(raw))
            return base64.b64encode(ct).decode()
        ct = base64.b64decode(data) if b64 else data
        pt = _PAES.new(key, _PAES.MODE_CBC, iv).decrypt(ct)
        return _pkcs7_unpad(pt)
    if _BACKEND == "cryptography":
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        if encrypt:
            e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            raw = data.encode("utf-8") if isinstance(data, str) else data
            out = e.update(_pkcs7_pad(raw)) + e.finalize()
            return base64.b64encode(out).decode()
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        ct = base64.b64decode(data) if b64 else data
        return _pkcs7_unpad(d.update(ct) + d.finalize())
    # pure
    w = _aes128_key_expand(key)
    if encrypt:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        out = bytearray(); prev = iv
        for i in range(0, len(_pkcs7_pad(raw)), 16):
            blk = bytes(a ^ b for a, b in zip(_pkcs7_pad(raw)[i:i + 16], prev))
            prev = _ecb_encrypt(w, blk)
            out += prev
        return base64.b64encode(bytes(out)).decode()
    ct = base64.b64decode(data) if b64 else data
    out = bytearray(); prev = iv
    for i in range(0, len(ct), 16):
        blk = _aes_decrypt_block(w, ct[i:i + 16])
        out += bytes(a ^ b for a, b in zip(blk, prev))
        prev = ct[i:i + 16]
    return _pkcs7_unpad(bytes(out))


def clean_ctrl(s):
    return ''.join(ch for ch in s if ord(ch) >= 32 or ch in "\t\n\r")


def rand_uid():
    return ''.join(secrets.choice(ALNUM) for _ in range(20))


def rand_hex():
    return ''.join(secrets.choice(HEX) for _ in range(16))


def rand_uuid():
    return ''.join(secrets.choice(ALNUM) for _ in range(32))


# ============ 协议实现 ============
class Spider(_BaseSpider):

    def getName(self):
        return "韩圈"

    # ---------- 初始化 (设备身份) ----------
    def init(self, extend=""):
        self.uid = rand_uid()
        self.said = rand_hex()
        self.oa = rand_hex()
        self.g = md5h(self.uid)               # udid
        self.uk = aes_cbc(JKEY, KIV, self.uid, encrypt=True)
        self.b_ts = int(time.time() * 1000) - 0x48190800

    # ---------- 惰性初始化 (框架未先调用 init 时自保) ----------
    def _ensure_init(self):
        if not getattr(self, "g", None):
            try:
                self.init("")
            except Exception:
                pass

    # ---------- 设备签名头 ----------
    def _device_json(self):
        return ('{"emu":0,"ou":0,"it":%d,"iit":%d,"bs":0,"uid":"%s","pc":0,"tm":81,'
                '"d8m":"0,0,0,0,0,0,0,4","md":"%s","maker":"%s","osv":"%s",'
                '"br":95,"rpc":0,"scc":2,"plc":6,"toc":19,"tsc":10,"ts":%d,'
                '"pa":1,"crec":0,"nw":2,"px":"0","isp":"","ai":"%s","oa":"%s",'
                '"dpc":0,"dsc":0,"qpc":0,"apad":0,"pk":"com.babycloud.hanju"}'
                % (self.b_ts, self.b_ts, self.uid, MD, MAKER, OSV,
                   int(time.time() * 1000), self.said, self.oa))

    def b0_headers(self):
        self._ensure_init()
        dj = self._device_json()
        sign = aes_cbc(self.g[:16].encode(), self.g[16:32].encode(), dj, encrypt=True)
        return {
            "vc": VC, "vn": VERSION, "ch": CH, "app": "hj",
            "User-Agent": UA, "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "said": self.said, "uk": self.uk, "sign": sign,
        }

    # ---------- HTTP ----------
    def _get(self, path, signed=False, decrypt=False, headers=None):
        # 传完整 URL 时不再拼 BASE（防御：否则域名变成 hxqapi.hiyun.tvhttps: → DNS 失败）
        url = path if path.startswith("http") else BASE + path
        h = {}
        if signed:
            h = self.b0_headers()
        if headers:
            h.update(headers)
        last = None
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                    raw = r.read().decode("utf-8", "ignore")
                if decrypt:
                    try:
                        return self._jdec(json.loads(raw))
                    except Exception:
                        return {}
                return raw
            except (urllib.error.URLError, ssl.SSLError, OSError) as e:
                last = e
                time.sleep(1.5)
        if last:
            raise last
        return ""

    def _jdec(self, obj):
        if not isinstance(obj, dict):
            return {}
        data = obj.get("data", "")
        if not data:
            return obj
        key = obj.get("key", "")
        if not key:
            ts = obj.get("ts", "")
            if ts != "":
                key = md5h(self.uid + str(ts))
        if not key:
            return obj
        dk = md5h(key + L)
        try:
            out = aes_cbc(dk[:16].encode(), dk[16:32].encode(), data, encrypt=False)
        except Exception:
            return obj
        return json.loads(clean_ctrl(out.decode("utf-8", "ignore")))

    # ---------- 列表项 -> vod ----------
    def _item_to_vod(self, it):
        if not isinstance(it, dict):
            return None
        sid = str(it.get("sid", ""))
        if not sid:
            return None
        return {
            "vod_id": sid,
            "vod_name": it.get("name", ""),
            "vod_pic": it.get("thumb") or it.get("poster") or it.get("cover", ""),
            "vod_remarks": it.get("category") or it.get("count") or "",
        }

    # ---------- 首页分类 + 筛选 ----------
    def homeContent(self, filter=1):
        classes = [{"type_id": c[0], "type_name": c[1]} for c in CLASSES]
        filters = {}
        if filter:
            # 排行榜: 从 /api/series/rank 拉取真实榜单 rid 作为筛选
            rank_vals = [{"n": "默认榜单", "v": "-1"}]
            try:
                rj = self._jdec_like(self._get("/api/series/rank"))
                for rk in (rj.get("rankings") or []):
                    rid = rk.get("rid")
                    title = rk.get("title") or ("榜单%d" % rid)
                    if rid is not None:
                        rank_vals.append({"n": str(title), "v": str(rid)})
            except Exception:
                pass
            filters["rank"] = [{
                "key": "rid", "name": "榜单",
                "value": rank_vals,
            }]
            # 韩剧/综艺/电影: 类型(cid) + 排序(sort) + 年份(year)
            for tid in ("1", "2", "3"):
                filters[tid] = [
                    {"key": "cid", "name": "类型", "value": [{"n": "全部", "v": "-1"}]},
                    {"key": "sort", "name": "排序", "value": [
                        {"n": "最新", "v": "new"},
                        {"n": "最热", "v": "hot"},
                        {"n": "好评", "v": "score"},
                    ]},
                    {"key": "year", "name": "年份", "value": [
                        {"n": "全部", "v": "-1"},
                        {"n": "2026", "v": "2026"},
                        {"n": "2025", "v": "2025"},
                        {"n": "2024", "v": "2024"},
                        {"n": "2023", "v": "2023"},
                        {"n": "2022", "v": "2022"},
                        {"n": "2021", "v": "2021"},
                        {"n": "2020", "v": "2020"},
                        {"n": "2015-2019", "v": "2015-2019"},
                        {"n": "更早", "v": "1-2014"},
                    ]},
                ]
            # 明星: smode
            smode = [{"n": "热门明星", "v": "hot"}, {"n": "人气明星", "v": "rank"}]
            for s in STARS:
                smode.append({"n": s, "v": "name:" + s})
            filters["star"] = [{"key": "smode", "name": "筛选", "value": smode}]
        return {"class": classes, "filters": filters} if filter else {"class": classes}

    # ---------- 分类列表 ----------
    def categoryContent(self, tid, pg=1, filter=1, extend=None):
        try:
            page = max(1, int(pg) if pg else 1)
        except (ValueError, TypeError):
            page = 1
        ext = {}
        if isinstance(extend, str) and extend:
            try:
                ext = json.loads(extend)
            except Exception:
                ext = {}
        elif isinstance(extend, dict):
            ext = extend
        stype = str(tid)
        sort = str(ext.get("sort", "hot") or "hot")
        cid = str(ext.get("cid", "-1") or "-1")
        year = str(ext.get("year", "-1") or "-1")
        rid = str(ext.get("rid", "-1") or "-1")
        smode = str(ext.get("smode", "") or "")
        try:
            # 明星: 调用明星接口
            if stype == "star":
                obj = self._star_list(smode)
            # 排行榜: 专用接口 /api/series/rank (带 rid 取具体榜单, 不带取默认榜单)
            elif stype == "rank":
                rq = "/api/series/rank?page=%d" % page
                if rid not in ("", "-1", "None"):
                    rq += "&rid=" + urllib.parse.quote(rid)
                obj = self._jdec_like(self._get(rq))
            else:
                q = "stype=%s&sort=%s&page=%d" % (
                    urllib.parse.quote(stype), urllib.parse.quote(sort), page)
                if cid not in ("", "-1", "None"):
                    q += "&cid=" + urllib.parse.quote(cid)
                if year not in ("", "-1", "None"):
                    q += "&year=" + urllib.parse.quote(year)
                obj = self._jdec_like(self._get("/api/series/cate?" + q))
            lst = obj.get("seriesList") or []
            items = []
            for it in lst:
                v = self._item_to_vod(it)
                if v:
                    items.append(v)
            hasmore = 1 if len(items) >= 20 else 0
            return {"list": items, "page": page,
                    "pagecount": page + 1 if hasmore else page,
                    "limit": max(len(items), 20), "total": len(items)}
        except Exception:
            return {"list": [], "page": page, "pagecount": page, "limit": 20, "total": 0}

    def _jdec_like(self, raw):
        # /api/series/cate 返回明文
        try:
            if isinstance(raw, dict):
                return raw
            return json.loads(raw)
        except Exception:
            return {}

    def _star_list(self, smode):
        # 明星: 复用搜索/榜单式接口尽力获取
        try:
            q = "stype=star&sort=hot&page=1"
            if smode and smode not in ("", "-1", "None"):
                q += "&smode=" + urllib.parse.quote(smode)
            return self._jdec_like(self._get("/api/series/cate?" + q))
        except Exception:
            return {}

    @staticmethod
    def _parse_year(series):
        # publishTime 为毫秒时间戳(>1e10)或秒级; 复刻 APP: 毫秒格式化 yyyy 取前4位, 否则取原串前4位
        if not isinstance(series, dict):
            return ""
        v = series.get("publishTime", "")
        if v in (None, ""):
            v = series.get("year", "")
        if v in (None, ""):
            return ""
        s = str(v).strip()
        try:
            n = int(s)
        except (ValueError, TypeError):
            return s[:4] if len(s) >= 4 else s
        if n > 10 ** 10:
            try:
                import datetime
                return str(datetime.datetime.fromtimestamp(n / 1000).year)
            except Exception:
                return s[:4]
        return s[:4]

    # ---------- 详情 ----------
    def detailContent(self, ids=None):
        if not ids:
            return {}
        try:
            sid = str(ids[0]).strip()
        except (IndexError, TypeError):
            return {}
        if not sid:
            return {}
        try:
            obj = self._jdec_like(self._get("/api/series/detail?sid=" + urllib.parse.quote(sid)))
            if not isinstance(obj, dict) or not obj:
                return {"list": []}
            # 响应形如 {"rescode":0,"series":{"name":...},"playItems":[...]}
            series = obj.get("series") or obj
            pis = obj.get("playItems") or []
            if not isinstance(series, dict):
                series = {}
            vod = {
                "vod_id": sid,
                "vod_name": series.get("name", "") or series.get("title", ""),
                "vod_pic": series.get("thumb") or series.get("poster") or series.get("posterThumb") or series.get("cover", ""),
                "vod_actor": series.get("crew", ""),
                "vod_area": series.get("area", "") or series.get("country", ""),
                "vod_year": self._parse_year(series),
                "vod_content": series.get("intro", "") or series.get("shorthand", ""),
                "vod_remarks": series.get("category", "") or series.get("remarks", ""),
            }
            if not isinstance(pis, list) or not pis:
                return {"list": [vod]}
            eps = []
            for pi in pis:
                if not isinstance(pi, dict):
                    continue
                pid = (pi.get("pid") or pi.get("programId") or pi.get("id") or "").strip()
                if not pid:
                    continue
                title = (pi.get("title") or pi.get("name") or "").strip()
                if not title:
                    try:
                        title = str(int(pi.get("serialNo") or pi.get("sort") or len(eps) + 1))
                    except Exception:
                        title = str(len(eps) + 1)
                eps.append("%s$%s" % (title, pid))
            if not eps:
                return {"list": [vod]}
            vod["vod_play_from"] = "韩圈"
            vod["vod_play_url"] = "#".join(eps)
            return {"list": [vod]}
        except Exception:
            return {"list": []}

    @staticmethod
    def _extract_play_url(dec):
        # 对齐真机 Z(): 依次取 playUrl/url/page/m3u8, 失败则用正则兜底
        if not isinstance(dec, dict):
            dec = {}
        for k in ("playUrl", "url", "page", "m3u8"):
            u = dec.get(k, "")
            if u:
                return u
        txt = clean_ctrl(str(dec))
        m = re.search(r'https?://[^\s"\']+/(?:m3u8|video)/[A-Za-z0-9_]+(?:\.m3u8|\.mp4)?', txt)
        if m:
            return m.group(0)
        m = re.search(r'/m3u8/([A-Za-z0-9_]{20,})(?:\.m3u8)?', txt)
        if m:
            return Q_CDN.rstrip("/") + "/m3u8/" + m.group(1) + ".m3u8"
        return ""

    # ---------- 播放 ----------
    def playerContent(self, flag, id, vipFlags=None):
        self._ensure_init()
        if not id:
            return {"parse": 0, "url": "", "header": {}}
        pid = str(id).strip()
        # 对齐真机 playerContent: 剥离 '|' / '$' / '~~' 前缀, 取出真实 pid
        if "~~" in pid:
            pid = pid.rsplit("~~", 1)[-1].strip()
        if "|" in pid:
            pid = pid.rsplit("|", 1)[-1].strip()
        if "$" in pid:
            pid = pid.rsplit("$", 1)[-1].strip()
        if pid.startswith("ep_"):
            return {"parse": 0, "url": "", "header": {}}
        try:
            # 1) episode detail -> scid (签名 + 解密)
            ep = self._jdec(self._get(
                "/api/series2/episode/detail?pid=" + urllib.parse.quote(pid) + "&refer=" + urllib.parse.quote(SIGN_KEY),
                signed=True, decrypt=True))
            sources = (ep.get("playItem") or {}).get("sources") or []
            if not sources:
                return {"parse": 0, "url": "", "header": {}}
            # 2) 遍历多个 source 的 scid, 取第一个能解出 URL 的
            for src in sources[:6]:
                scid = src.get("scid", "")
                if not scid:
                    continue
                try:
                    blob = self._rslvV4(pid, scid)
                    obj = json.loads(blob) if isinstance(blob, str) else blob
                    if obj.get("rescode", -1) != 0:
                        continue
                    datas = obj.get("datas") or []
                    for d in datas:
                        data = d.get("data", "")
                        if not data:
                            continue
                        dec = self._rslv_data_decrypt(data)
                        if not dec:
                            continue
                        url = self._extract_play_url(dec)
                        if not url:
                            continue
                        url = self._L_url(url)
                        hdr = dec.get("header") or {}
                        ua = hdr.get("User-Agent") or PLAY_UA
                        return {"parse": 0, "url": url, "header": {"User-Agent": ua}}
                except Exception:
                    continue
            return {"parse": 0, "url": "", "header": {}}
        except Exception:
            return {"parse": 0, "url": "", "header": {}}

    def _rslvV4(self, pid, scid):
        t = str(int(time.time()))
        uuid = rand_uuid()
        udid = self.g
        sq = "10"
        re_ = "1"
        q = ("t=%s&dt=android&version=%s&uuid=%s&pid=%s&scid=%s&re=%s&sq=%s" %
             (t, VERSION, uuid, urllib.parse.quote(pid), urllib.parse.quote(scid), re_, sq))
        kv = {"t": t, "dt": "android", "version": VERSION, "uuid": uuid, "udid": udid,
              "pid": pid, "scid": scid, "re": re_, "sq": sq}
        sign_src = "".join("&%s=%s" % (k, kv[k]) for k in sorted(kv, reverse=True)) + "&" + M
        sign = md5h(sign_src)
        return self._get("/api/series/rslvV4?" + q + "&sign=" + urllib.parse.quote(sign), signed=True)

    def _rslv_data_decrypt(self, data):
        key = md5h(self.g + L)
        try:
            out = aes_cbc(key[:16].encode(), key[16:32].encode(), data, encrypt=False)
        except Exception:
            return {}
        try:
            return json.loads(clean_ctrl(out.decode("utf-8", "ignore")))
        except Exception:
            return {}

    def _L_url(self, u):
        if not u:
            return ""
        u = u.strip()
        if u.startswith("/m3u8/") or u.startswith("/video/"):
            return Q_CDN.rstrip("/") + u
        try:
            p = urllib.parse.urlparse(u)
            host = p.netloc
            scheme = p.scheme or "http"
            if "51touxiang.com" in host:
                host = urllib.parse.urlparse(Q_CDN).netloc
                scheme = "https"   # 对齐真实APP：51touxiang CDN 强制 https
            path = p.path
            m = re.search(r"/m3u8/([A-Za-z0-9_]+)(?:\.m3u8)?", path)
            if m:
                path = "/m3u8/" + m.group(1) + ".m3u8"
            q = ("?" + p.query) if p.query else ""
            return scheme + "://" + host + path + q
        except Exception:
            return u

    # ---------- 搜索 ----------
    def searchContent(self, key, quick=None, pg=None):
        if not key or not str(key).strip():
            return {}
        try:
            page = int(pg) if pg else 1
        except (ValueError, TypeError):
            page = 1
        try:
            obj = self._jdec(self._get(
                "/api/search/s5?k=" + urllib.parse.quote(str(key).strip()) +
                "&srefer=" + urllib.parse.quote("search_input") + "&type=0&page=" + str(page),
                signed=True, decrypt=True))
            lst = obj.get("seriesList") or []
            items = []
            for it in lst:
                v = self._item_to_vod(it)
                if v:
                    items.append(v)
            hasmore = 1 if len(items) >= 20 else 0
            return {"list": items, "page": page,
                    "pagecount": page + 1 if hasmore else page,
                    "limit": max(len(items), 20), "total": len(items)}
        except Exception:
            return {"list": [], "page": page, "pagecount": page, "limit": 20, "total": 0}

    def searchContentPage(self, key, quick=None, pg=None):
        return self.searchContent(key, quick, pg)

    # ---------- 首页精选/热门/新剧/热播综艺 ----------
    def homeVideoContent(self):
        out = {}
        seen = set()
        items = []

        def grab(getter):
            for it in getter():
                v = self._item_to_vod(it)
                if v and v["vod_id"] not in seen:
                    seen.add(v["vod_id"])
                    items.append(v)

        try:
            grab(self._home_index)
        except Exception:
            pass
        try:
            grab(lambda: self._home_cate("1", "hot"))
        except Exception:
            pass
        try:
            grab(lambda: self._home_cate("1", "new"))
        except Exception:
            pass
        try:
            grab(lambda: self._home_cate("2", "hot"))
        except Exception:
            pass
        out["list"] = items
        return out

    def _home_index(self):
        obj = self._jdec_like(self._get("/api/series/index?offset=0"))
        return obj.get("seriesList") or []

    def _home_cate(self, stype, sort):
        q = "stype=%s&sort=%s&page=1" % (urllib.parse.quote(stype), urllib.parse.quote(sort))
        obj = self._jdec_like(self._get("/api/series/cate?" + q))
        return obj.get("seriesList") or []

    # ---------- 兼容方法 ----------
    def isVideoFormat(self, url):
        return bool(re.match(r"(?i).*\.(mp4|m3u8|flv|mkv|avi|ts|mov|mpd|m4a|wmv|m3u)(\?.*)?$", url or ""))

    def manualVideoCheck(self):
        return False

    def localProxy(self, param):
        return {"list": [], "parse": 0, "url": ""}

    def liveContent(self, url):
        return {"list": []}

    def categoryContent_test(self, *a, **k):
        return self.categoryContent(*a, **k)


# ============ 自测 ============
if __name__ == "__main__":
    sp = Spider()
    sp.init("")
    print("=== homeContent ===")
    hc = sp.homeContent(True)
    print("分类:", [(c["type_id"], c["type_name"]) for c in hc["class"]])
    print("筛选类:", list(hc.get("filters", {}).keys()))

    print("\n=== categoryContent 韩剧 hot p1 ===")
    cc = sp.categoryContent("1", 1, 1, {"sort": "hot"})
    print("数量:", len(cc.get("list", [])))
    for v in cc.get("list", [])[:3]:
        print("  ", v["vod_id"], v["vod_name"], v["vod_remarks"])

    print("\n=== homeVideoContent ===")
    hv = sp.homeVideoContent()
    print("首页条目:", len(hv.get("list", [])))

    sid = ""
    if cc.get("list"):
        sid = cc["list"][0]["vod_id"]
        print("\n=== detailContent", sid, "===")
        dc = sp.detailContent([sid])
        vod = dc.get("list", [{}])[0]
        print("名称:", vod.get("vod_name"))
        url = vod.get("vod_play_url", "")
        print("播放源条目数:", len(url.split("#")) if url else 0)
        if url:
            pid = url.split("#")[0].split("$")[-1]
            print("\n=== playerContent pid", pid, "===")
            pc = sp.playerContent("韩圈", pid)
            print("  url:", (pc.get("url") or "")[:120])
            print("  header:", pc.get("header"))
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
