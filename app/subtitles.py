"""SubHD search metadata only; downloads remain on the publisher's website."""
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError

BASE = "https://subhd.tv"
HOSTS = ("subhd.tv", "subhd.me", "subhd.one")
_cache = {}
_lock = threading.Lock()


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443) or parsed.username:
            raise ValueError('字幕源重定向超出允许范围')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_search(url, timeout=4):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443) or parsed.username:
        raise ValueError('不支持的字幕源')
    with build_opener(SafeRedirect()).open(Request(url, headers={'User-Agent': 'Mozilla/5.0 MediaLinkHelper/1.7.0'}), timeout=timeout) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('字幕搜索页过大')
        return data


def identity(title):
    title = re.split(r'(?i)(?<![a-z0-9])(?:19\d{2}|20\d{2}|s\d{1,2}(?:e\d+)?|\d{3,4}p|bluray|blu-ray|web-dl|webrip)\b', title)[0]
    return ''.join(c for c in title.casefold() if c.isalnum())


class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.card = None
        self.rows = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and "clearfix" in classes and self.card is None:
            self.card = {"texts": [], "links": []}
            self.depth = len(self.stack)
        if tag == "a" and self.card is not None:
            self.anchor = {"href": attrs.get("href", ""), "text": [],
                           "release": any("view-text" in x[1] for x in self.stack)}
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.card["links"].append(self.anchor)
            self.anchor = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if self.card is not None and len(self.stack) <= self.depth:
            for a in self.card["links"]:
                if a["release"] and re.fullmatch(r"/a/[A-Za-z0-9]+", a["href"]):
                    self.rows.append({"id": a["href"].split("/")[-1],
                                      "title": " ".join(a["text"]).strip(),
                                      "text": " ".join(self.card["texts"])})
            self.card = None

    def handle_data(self, data):
        text = data.strip()
        if text and self.card is not None:
            self.card["texts"].append(text)
            if self.anchor is not None:
                self.anchor["text"].append(text)


def search_url(title):
    return BASE + "/search/" + quote(title, safe="")


def words(value):
    return set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold()))


def rank_candidates(rows, release, base_title, chinese_title, metadata):
    wanted = metadata(release)
    names = [words(x) for x in (base_title, chinese_title) if x.strip()]
    ranked, seen = [], set()
    for row in rows:
        if row["id"] in seen or not any(x in row["text"] for x in ("简体", "繁体", "中文")):
            continue
        seen.add(row["id"])
        candidate_name = identity(row["title"])
        exact_names = [identity(x) for x in (base_title, chinese_title) if x.strip()]
        english_name = identity(re.sub(r"[\u3400-\u9fff]+", "", row["title"]))
        if not any(x and x in (candidate_name, english_name) for x in exact_names):
            continue
        candidate = metadata(row["title"])
        # A conflicting year/season/episode is never recommended.
        if any(wanted[k] is not None and candidate[k] is not None and wanted[k] != candidate[k]
               for k in ("releaseYear", "season")):
            continue
        if wanted["episode"] is not None and candidate["episode"] is not None:
            if not candidate["episode"] <= wanted["episode"] <= (candidate["episodeEnd"] or candidate["episode"]):
                continue
        if wanted["episodeEnd"] and candidate["episode"] is not None:
            if (candidate["episodeEnd"] or candidate["episode"]) < wanted["episodeEnd"]:
                continue
        if wanted["season"] is None and candidate["season"] is not None:
            continue
        reasons, score = [], 0
        for key, label in (("releaseYear", "年份"), ("season", "季"), ("episode", "集"), ("sourceFormat", "片源类型"), ("resolution", "分辨率")):
            if wanted[key] not in (None, "other") and wanted[key] == candidate[key]:
                reasons.append(label + "一致")
                score += 4 if key in ("season", "episode") else 2
        same_release = "".join(c for c in release.casefold() if c.isalnum()) == "".join(c for c in row["title"].casefold() if c.isalnum())
        if same_release:
            score += 30
            reasons.insert(0, "发布名一致")
        languages = [x for x in ("简体", "繁体", "双语") if x in row["text"]]
        formats = [x for x in ("SRT", "ASS", "SSA", "SUP", "SUB", "IDX") if re.search(r"\b" + x + r"\b", row["text"], re.I)]
        score += 1 if "简体" in languages else 0
        ranked.append({"title": row["title"], "detailUrl": BASE + "/a/" + row["id"],
                       "downloadPageUrl": BASE + "/a/" + row["id"],
                       "languages": languages, "formats": formats, "reasons": reasons,
                       "matchLabel": "同发布名 · 仍需确认同步" if same_release else "候选字幕 · 需确认版本",
                       "score": score})
    return sorted(ranked, key=lambda x: x["score"], reverse=True)[:3]


def lookup(release, base_title, chinese_title, fetch, metadata):
    query = base_title.strip() or chinese_title.strip()
    if not query:
        raise ValueError("缺少片名")
    url = search_url(query)
    # Serialize upstream fetches and share results across releases of one title.
    with _lock:
        now = time.monotonic()
        cached = _cache.get(query.casefold())
        if cached and now - cached[0] < cached[1]:
            rows, status, origin = cached[2:]
        else:
            rows, status, origin = [], "unavailable", BASE
            for host in HOSTS:
                origin = "https://" + host
                try:
                    page = fetch(origin + "/search/" + quote(query, safe=""), timeout=4).decode("utf-8", "replace")
                    parser = SearchParser()
                    parser.feed(page)
                    rows = parser.rows
                    if not rows and re.search(r'(?i)access denied|captcha|cf-chl-|人机验证|验证码', page):
                        status = "blocked"
                        break
                    status = "ok" if rows or re.search(r"搜索结果|没有找到|未找到", page) else "layout_changed"
                    break
                except HTTPError as exc:
                    if exc.code in (403, 429):
                        status = "blocked"
                        break
                    if exc.code < 500:
                        break
                except (OSError, TimeoutError):
                    continue
                except Exception:
                    status = "layout_changed"
                    break
            _cache[query.casefold()] = (now, 600 if status == "ok" else 60, rows, status, origin)
            while len(_cache) > 128:
                _cache.pop(next(iter(_cache)))
    items = rank_candidates(rows, release, base_title, chinese_title, metadata)
    url = origin + "/search/" + quote(query, safe="")
    for item in items:
        item["detailUrl"] = item["detailUrl"].replace(BASE, origin)
        item["downloadPageUrl"] = item["detailUrl"]
    return {"ok": True, "source": "SubHD", "sourceHost": urlsplit(origin).hostname, "status": status,
            "alternativeUrl": "https://www.opensubtitles.org/",
            "searchUrl": url, "items": items,
            "message": ({"blocked": "字幕站要求验证或限制访问，请到网站手动下载。", "layout_changed": "字幕站页面已变化，请到网站查找。"}.get(status, "SubHD 及备用域名暂不可用，可换站查找或选择本地字幕。")) if status != "ok" else
                       ("字幕尚未验证时间轴；下载在 SubHD 完成。" if items else "本页未找到匹配的中文字幕，可到 SubHD 继续查找。")}
