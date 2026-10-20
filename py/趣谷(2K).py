# coding = utf-8
#!/usr/bin/python
# Q群：807916734 @Easy
# ============================================================
# 趣谷视频 (com.quguvideo.app v2.8.0) TVBox / OK影视 / EasyTV py 源
# ------------------------------------------------------------
# 接口链路（真机抓包 + Frida 运行时 hook 实测还原）:
#   首页/频道   GET  /cache/channel/{频道}.json                   明文
#   筛选项定义  GET  /cache/filterOptions/filter.json             明文
#   分类列表    GET  /cache/zhaopian/{类型}/{类}/{地区}/{年份}/{排序}/{页}.json  明文
#   搜索        GET  /vc/api/search/{关键词}/{页}.json?version=2.8.0   明文
#   详情        GET  /cache/videos/{sid//100}/{sid}.json?baoming=&version=&channel=
#                   body = base64( [12B nonce][AES-256-GCM 密文 + 16B tag] )
#   设备注册    POST /vc/api/device/isnew  (JSON) -> newDeviceCode / b / uid
#   播放地址    GET  /vc/api/video/playurl?...&b=
#                   header: vuk = MD5(str(sid) + API_KEY)
#
# 关键常量（libkeys.so SecureConfig.getApiKey，实测参与 AES-GCM 与 vuk）:
#   API_KEY = "qvn1u7polu8uaolplopoiuVH9oqDxih7"
#
# 依赖: 无。AES-256-GCM 三级降级（pycryptodome -> cryptography -> 纯 Python）
#       网络层只用标准库 urllib（requests 存在时作为兜底）。
# ============================================================

import base64
import gzip
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request

try:
    from base.spider import Spider as _BaseSpider
except Exception:
    class _BaseSpider(object):
        pass

sys.path.append('..')

# ---------------------------------------------------------------- 常量
HOST = "https://js.trgfd.cn"
API_KEY = "qvn1u7polu8uaolplopoiuVH9oqDxih7"          # libkeys.so
UA = "okhttp/4.12.0"
PKG = "com.quguvideo.app"
VERSION = "2.8.0"
CHANNEL = "baidutuiguang"
MODEL = "Xiaomi 22081212C"

_q = urllib.parse.quote

URL_CHANNEL = HOST + "/cache/channel/" + _q("首页") + ".json"
URL_FILTER = HOST + "/cache/filterOptions/filter.json"
URL_CATEGORY = HOST + "/cache/zhaopian/{t}/{c}/{r}/{y}/{s}/{p}.json"
URL_DETAIL = HOST + "/cache/videos/{d}/{sid}.json"
URL_SEARCH = HOST + "/vc/api/search/{kw}/{p}.json?version=" + VERSION
URL_DEVICE = HOST + "/vc/api/device/isnew"
URL_PLAYURL = HOST + "/vc/api/video/playurl"

# 分类（"少儿" 在 /cache/zhaopian/ 下无对应静态目录，实测 404，故不列出）
CATS = ["剧集", "电影", "综艺", "动漫", "纪录片"]


# ================================================================ AES-256-GCM（三级降级）
_BACKEND = "pure"
try:
    from Crypto.Cipher import AES as _PAES          # pycryptodome
    _BACKEND = "pycryptodome"
except Exception:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
        _BACKEND = "cryptography"
    except Exception:
        _BACKEND = "pure"


# ---------- 纯 Python AES-256-GCM（Q群：807916734 @Easy） ----------
_SBOX = None


def _init_sbox():
    global _SBOX, _INV_SBOX, _MUL2, _MUL3, _MUL9, _MUL11, _MUL13, _MUL14
    if _SBOX is not None:
        return
    p = q = 1
    sb = [0] * 256
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0))
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6))
             ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4)))
        sb[p] = x & 0xFF ^ 0x63
        if p == 1:
            break
    sb[0] = 0x63
    _SBOX = sb
    _INV_SBOX = [0] * 256
    for i in range(256):
        _INV_SBOX[sb[i]] = i
    _MUL2 = [((i << 1) ^ 0x1B) & 0xFF if i & 0x80 else (i << 1) for i in range(256)]
    _MUL3 = [_MUL2[i] ^ i for i in range(256)]


_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _aes256_key_expand(key):
    _init_sbox()
    w = [list(key[i * 4:i * 4 + 4]) for i in range(8)]
    for i in range(8, 60):
        t = list(w[i - 1])
        if i % 8 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 8 - 1]
        elif i % 8 == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - 8][j] ^ t[j] for j in range(4)])
    return w


def _aes_encrypt_block(w, block):
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_rk(rnd):
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
            s[0][c] = _MUL2[a[0]] ^ _MUL3[a[1]] ^ a[2] ^ a[3]
            s[1][c] = a[0] ^ _MUL2[a[1]] ^ _MUL3[a[2]] ^ a[3]
            s[2][c] = a[0] ^ a[1] ^ _MUL2[a[2]] ^ _MUL3[a[3]]
            s[3][c] = _MUL3[a[0]] ^ a[1] ^ a[2] ^ _MUL2[a[3]]

    nr = len(w) // 4 - 1
    add_rk(0)
    for rnd in range(1, nr):
        sub_shift()
        mix()
        add_rk(rnd)
    sub_shift()
    add_rk(nr)
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4 * c] = s[r][c]
    return bytes(out)


def _aes_ecb_encrypt(w, data):
    return b"".join(_aes_encrypt_block(w, data[i:i + 16]) for i in range(0, len(data), 16))


def _ghash(h, data):
    def gmul(a, b):
        r = 0
        v = b
        for i in range(127, -1, -1):
            if (a >> i) & 1:
                r ^= v
            v = (v >> 1) ^ (0xE1 << 120 if v & 1 else 0)
        return r

    y = 0
    hb = int.from_bytes(h, "big")
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        if len(blk) < 16:
            blk = blk + b"\x00" * (16 - len(blk))
        y = gmul(y ^ int.from_bytes(blk, "big"), hb)
    return y.to_bytes(16, "big")


def _gcm_decrypt_pure(key, nonce, ct_tag):
    """ct_tag = 密文 + 16B tag（忽略 tag 校验）"""
    w = _aes256_key_expand(key)
    ct = ct_tag[:-16] if len(ct_tag) > 16 else ct_tag
    pt = bytearray()
    counter = 2
    for i in range(0, len(ct), 16):
        ks = _aes_ecb_encrypt(w, nonce + counter.to_bytes(4, "big"))
        blk = ct[i:i + 16]
        pt += bytes(a ^ b for a, b in zip(blk, ks[:len(blk)]))
        counter += 1
    return bytes(pt)


def aes_gcm_decrypt(key, nonce, ct):
    """ct 含尾部 16B tag"""
    if _BACKEND == "pycryptodome":
        try:
            c = _PAES.new(key, _PAES.MODE_GCM, nonce=nonce)
            return c.decrypt_and_verify(ct[:-16], ct[-16:])
        except Exception:
            c = _PAES.new(key, _PAES.MODE_GCM, nonce=nonce)
            return c.decrypt(ct[:-16])
    if _BACKEND == "cryptography":
        return _AESGCM(key).decrypt(nonce, ct, None)
    return _gcm_decrypt_pure(key, nonce, ct)


def decrypt_detail(body):
    """详情响应体: base64( [12B nonce][密文 + 16B tag] ) -> 明文 JSON 字符串"""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    raw = base64.b64decode(body.strip())
    return aes_gcm_decrypt(API_KEY.encode(), raw[:12], raw[12:]).decode("utf-8")


# ================================================================ 网络层（Q群：807916734 @Easy）
def _http_raw(url, headers=None, data=None, timeout=15):
    try:
        url.encode("ascii")
    except UnicodeEncodeError:                      # urllib 不接受非 ASCII URL
        url = urllib.parse.quote(url, safe=":/?&=%,#+")
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw


def _get_json(url, timeout=15):
    try:
        return json.loads(_http_raw(url, None, None, timeout).decode("utf-8", "replace"))
    except Exception:
        pass
    try:                                    # requests 兜底（部分运行时已内置）
        import requests
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _post_json(url, obj, timeout=15):
    body = json.dumps(obj).encode("utf-8")
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    try:
        return json.loads(_http_raw(url, h, body, timeout).decode("utf-8", "replace"))
    except Exception:
        pass
    try:
        import requests
        r = requests.post(url, data=body, headers=h, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ================================================================ 业务接口
_DEVICE = None


def _register_device():
    """POST /vc/api/device/isnew —— 无签名，服务端直接下发 b / newDeviceCode / uid"""
    device_code = hashlib.md5(("%s-%s" % (time.time(), id(object()))).encode()).hexdigest()[:16]
    payload = {
        "packageName": PKG, "baoming": PKG, "version": VERSION, "channel": CHANNEL,
        "uid": 0, "modelName": MODEL, "isOld000": 0,
        "deviceCode": device_code, "newDeviceCode": "",
    }
    j = _post_json(URL_DEVICE, payload)
    if not isinstance(j, dict):
        return None
    d = j.get("data") or {}
    return {
        "deviceCode": device_code,
        "newDeviceCode": str(d.get("newDeviceCode", "")),
        "b": d.get("b", ""),
        "uid": d.get("uid", 0),
        "modelName": MODEL,
    }


def device():
    global _DEVICE
    if _DEVICE is None:
        try:
            _DEVICE = _register_device()
        except Exception:
            _DEVICE = None
    return _DEVICE


def vuk(sid):
    """播放接口 vuk 头 = MD5(str(sid) + API_KEY)"""
    return hashlib.md5((str(sid) + API_KEY).encode()).hexdigest()


def api_channel():
    return _get_json(URL_CHANNEL)


def api_filter():
    return _get_json(URL_FILTER)


def api_category(tid, cls="全部", region="全部", year="全部", sort="最热", page=1):
    url = URL_CATEGORY.format(t=_q(str(tid)), c=_q(str(cls)), r=_q(str(region)),
                              y=_q(str(year)), s=_q(str(sort)), p=int(page or 1))
    j = _get_json(url)
    return j if isinstance(j, list) else []


def api_search(kw, page=1):
    url = URL_SEARCH.format(kw=_q(str(kw)), p=int(page or 1))
    j = _get_json(url)
    if isinstance(j, dict):
        return ((j.get("data") or {}).get("videos")) or []
    return []


def api_detail(sid):
    url = URL_DETAIL.format(d=int(sid) // 100, sid=sid)
    body = _http_raw(url + "?baoming=%s&version=%s&channel=%s" % (PKG, VERSION, CHANNEL))
    return json.loads(decrypt_detail(body))


def api_playurl(sid, ji, ji_index=0):
    d = device()
    if not d:
        return {}
    params = {
        "sid": sid, "ji": ji, "jiIndex": ji_index, "t": 0, "y": 0, "isjiid": 1,
        "androidId": d["deviceCode"], "modelName": d["modelName"],
        "newDeviceCode": d["newDeviceCode"], "b": d["b"],
        "version": VERSION, "baoming": PKG, "channel": CHANNEL,
    }
    url = URL_PLAYURL + "?" + urllib.parse.urlencode(params)
    try:
        raw = _http_raw(url, {"vuk": vuk(sid)})
        j = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}
    if not isinstance(j, dict) or j.get("code") != 0:
        return {}
    data = j.get("data") or {}
    urls = [x for x in ([data.get("url")] + list(data.get("urlarr") or [])) if x]
    return {"url": data.get("url"), "playType": data.get("playType"), "urls": urls}


# ================================================================ 字段映射
def _map_video(item):
    if not isinstance(item, dict):
        return {}
    return {
        "vod_id": str(item.get("videoId", "") or ""),
        "vod_name": item.get("videoName") or item.get("name") or "",
        "vod_pic": item.get("fengmiantu") or item.get("vod_pic") or "",
        "vod_remarks": (item.get("serialDesc") or item.get("newchapter")
                        or (str(item.get("score", "")) if item.get("score") else "")),
        "vod_year": str(item.get("year", "") or ""),
        "vod_area": item.get("region", "") or "",
        "vod_score": str(item.get("score", "") or ""),
    }


# ================================================================ Spider
class Spider(_BaseSpider):

    def getName(self):
        return "趣谷_Easy"

    def init(self, extend=""):
        return ""

    def isVideoFormat(self, url):
        url = (url or "").lower().split("?")[0]
        return any(url.endswith(x) for x in (".m3u8", ".mp4", ".flv", ".mkv", ".ts"))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    def localProxy(self, param):
        return None

    # ---------- 首页 ----------
    def homeContent(self, filter=False):
        result = {"class": [{"type_id": c, "type_name": c} for c in CATS],
                  "filters": {}}
        # 筛选定义: 用 filter.json 的 children / sort / region / year
        try:
            opts = api_filter() or []
            filters = {}
            for o in opts:
                if not isinstance(o, dict):
                    continue
                name = o.get("name")
                if name not in CATS:
                    continue
                vals = []
                ch = o.get("children") or []
                if ch:
                    vals.append({"key": "cls", "name": "分类", "value":
                                 [{"n": x.get("name"), "v": x.get("name")}
                                  for x in ch if isinstance(x, dict) and x.get("name")]})
                srt = o.get("sort") or []
                if srt:
                    vals.append({"key": "sort", "name": "排序",
                                 "value": [{"n": s, "v": s} for s in srt]})
                reg = o.get("region") or []
                if reg:
                    vals.append({"key": "region", "name": "地区",
                                 "value": [{"n": s, "v": s} for s in reg]})
                yer = o.get("year") or []
                if yer:
                    vals.append({"key": "year", "name": "年份",
                                 "value": [{"n": s, "v": s} for s in yer]})
                if vals:
                    filters[name] = vals
            result["filters"] = filters
        except Exception:
            pass
        # 首页推荐（部分引擎只读 homeContent 的 list）
        try:
            mods = api_channel() or []
            for m in mods:
                if isinstance(m, dict) and m.get("title") == "热门推荐" and m.get("data"):
                    result["list"] = [_map_video(v) for v in m["data"]]
                    break
        except Exception:
            pass
        return result

    def homeVideoContent(self):
        try:
            mods = api_channel() or []
            for m in mods:
                if isinstance(m, dict) and m.get("title") == "热门推荐" and m.get("data"):
                    return {"list": [_map_video(v) for v in m["data"]]}
        except Exception:
            pass
        return {"list": []}

    # ---------- 分类 ----------
    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg or 1)
        except Exception:
            pg = 1
        ext = extend or {}
        try:
            rows = api_category(
                tid,
                cls=ext.get("cls") or "全部",
                region=ext.get("region") or "全部",
                year=ext.get("year") or "全部",
                sort=ext.get("sort") or "最热",
                page=pg,
            )
        except Exception:
            rows = []
        videos = [_map_video(v) for v in rows if isinstance(v, dict)]
        return {"list": videos, "page": pg, "pagecount": 9999, "limit": 30,
                "total": 999999}

    # ---------- 详情 ----------
    def detailContent(self, ids):
        try:
            sid = str(ids[0]).split("/")[0]
        except Exception:
            return {"list": []}
        try:
            d = api_detail(sid)
        except Exception:
            return {"list": []}
        if not isinstance(d, dict):
            return {"list": []}

        eps = d.get("playUrlList") or []
        pairs = []
        for i, e in enumerate(eps):
            if not isinstance(e, dict):
                continue
            ji = e.get("ji")
            if ji in (None, ""):
                continue
            nm = e.get("name") or ("第%d集" % (i + 1))
            pairs.append("%s$%s-%s" % (nm, sid, ji))

        vod = _map_video(d)
        vod.update({
            "vod_id": sid,
            "vod_name": d.get("videoName") or vod.get("vod_name", ""),
            "vod_actor": d.get("actor", "") or "",
            "vod_director": "",
            "vod_content": d.get("blurb", "") or d.get("shortBlurb", "") or "",
            "vod_play_from": "趣谷",
            "vod_play_url": "#".join(pairs),
        })
        return {"list": [vod]}

    # ---------- 搜索 ----------
    def searchContent(self, key, quick, pg="1"):
        try:
            page = int(pg or 1)
        except Exception:
            page = 1
        if not key:
            return {"list": [], "page": page}
        try:
            vids = api_search(key, page)
        except Exception:
            vids = []
        return {"list": [_map_video(v) for v in vids if isinstance(v, dict)],
                "page": page}

    # ---------- 播放 ----------
    def playerContent(self, flag, pid, vipFlags):
        header = json.dumps({"User-Agent": UA})
        try:
            sid, ji = str(pid).split("-", 1)
        except Exception:
            return {"parse": 0, "playUrl": "", "url": "", "header": header}
        try:
            p = api_playurl(sid, ji)
        except Exception:
            p = {}
        url = (p or {}).get("url") or ""
        return {"parse": 0, "playUrl": "", "url": url, "header": header}


# ================================================================ CLI 自测
if __name__ == "__main__":
    def show(title, obj, n=700):
        s = json.dumps(obj, ensure_ascii=False, default=str)
        print("\n===== %s (%d chars) =====" % (title, len(s)))
        print(s[:n] + ("..." if len(s) > n else ""))

    sp = Spider()
    print("backend =", _BACKEND)
    show("设备注册", device())
    hc = sp.homeContent(False)
    print("\nclass:", hc.get("class"))
    print("filters:", list((hc.get("filters") or {}).keys()))
    show("homeContent.list 前2条", (hc.get("list") or [])[:2])
    show("category 剧集/最热/1 前2条", sp.categoryContent("剧集", 1, False, {}).get("list")[:2])
    show("search 玉簟秋 前1条", sp.searchContent("玉簟秋", False, 1).get("list")[:1])
    dt = sp.detailContent(["46163"])
    show("detail 46163", {k: dt["list"][0].get(k) for k in
                          ("vod_name", "vod_play_from", "vod_play_url")} if dt.get("list") else dt)
    if dt.get("list") and dt["list"][0].get("vod_play_url"):
        pid = dt["list"][0]["vod_play_url"].split("#")[0].split("$")[-1]
        show("playerContent " + pid, sp.playerContent("趣谷", pid, ""))
    print("\n>>> 全链路自测完成")
