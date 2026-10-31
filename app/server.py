#!/usr/bin/env python3
"""fnOS media link helper: authenticated gateway UI plus token-protected API."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import concurrent.futures
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import signal
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subtitles
from subtitle_providers import SubtitleService, SubtitleError


APP_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = APP_DIR / "public"
GATEWAY_PREFIX = "/app/media_link_helper"
LEGACY_GATEWAY_PREFIX = "/app/media-link-helper"
API_URL = "https://apibay.org/q.php?q={}"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
DOUBAN_SUGGEST_API = "https://movie.douban.com/j/subject_suggest?q={}"
DOUBAN_TV_API = "https://movie.douban.com/j/search_subjects"
MIRRORS = (
    "thepiratebay.org",
    "piratebay.live",
    "pirateproxy.live",
    "tpb.party",
)
MAX_RESULTS = 50
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CACHE_TTL_SECONDS = 90
CACHE = {}
CACHE_LOCK = threading.Lock()
TITLE_CACHE_TTL_SECONDS = 6 * 60 * 60
TITLE_CACHE = {}
TITLE_CACHE_LOCK = threading.Lock()
DISCOVER_CACHE_TTL_SECONDS = 15 * 60
DISCOVER_CACHE = {}
DISCOVER_CACHE_LOCK = threading.Lock()
DISCOVER_PAGE_SIZE = 20
DISCOVER_TARGET_ITEMS = 60
LOOKUP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="title-lookup")
PROVIDER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="title-provider")
LOGGER = logging.getLogger("media-link-helper")
MEDIA_DESCRIPTION = re.compile(
    r"电影|電影|电视剧|電視劇|电视节目|電視節目|剧集|劇集|影集|动画|動畫|纪录片|紀錄片|"
    r"\bfilm\b|television|\bTV\b|series|anime|miniseries|documentary|motion picture",
    re.I,
)
CJK_TEXT = re.compile(r"[\u3400-\u9fff]")
LATIN_TEXT = re.compile(r"[A-Za-z]{2,}")

# Common titles remain usable if the NAS cannot reach either online provider.
OFFLINE_TITLE_ALIASES = {
    "超人": ("Superman", "超人", "电影或漫画角色相关作品"),
    "超人大战": ("Batman v Superman: Dawn of Justice", "超人大战蝙蝠侠", "2016 年电影"),
    "超人大战蝙蝠侠": ("Batman v Superman: Dawn of Justice", "超人大战蝙蝠侠", "2016 年电影"),
    "蝙蝠侠大战超人": ("Batman v Superman: Dawn of Justice", "蝙蝠侠大战超人：正义黎明", "2016 年电影"),
    "复仇者联盟": ("The Avengers", "复仇者联盟", "2012 年电影"),
    "复仇者联盟4": ("Avengers: Endgame", "复仇者联盟4：终局之战", "2019 年电影"),
    "阿凡达": ("Avatar", "阿凡达", "2009 年电影"),
    "盗梦空间": ("Inception", "盗梦空间", "2010 年电影"),
    "星际穿越": ("Interstellar", "星际穿越", "2014 年电影"),
    "流浪地球": ("The Wandering Earth", "流浪地球", "2019 年电影"),
    "权力的游戏": ("Game of Thrones", "权力的游戏", "电视剧"),
    "绝命毒师": ("Breaking Bad", "绝命毒师", "电视剧"),
    "鱿鱼游戏": ("Squid Game", "鱿鱼游戏", "电视剧"),
    "我与超人的冒险": ("My Adventures with Superman", "我与超人的冒险", "动画剧集"),
    "超人和露易丝": ("Superman & Lois", "超人和露易丝", "电视剧"),
}

ENGLISH_TITLE_ALIASES = {
    "superman": "超人",
    "my adventures with superman": "我与超人的冒险",
    "superman and lois": "超人和露易丝",
    "batman v superman dawn of justice": "蝙蝠侠大战超人：正义黎明",
    "the avengers": "复仇者联盟",
    "avengers endgame": "复仇者联盟4：终局之战",
    "avatar": "阿凡达",
    "inception": "盗梦空间",
    "interstellar": "星际穿越",
    "the wandering earth": "流浪地球",
    "game of thrones": "权力的游戏",
    "breaking bad": "绝命毒师",
    "squid game": "鱿鱼游戏",
}

RELEASE_STOP = re.compile(
    r"\b(?:S\d{1,2}E\d{1,3}|S\d{1,2}\b|(?:19|20)\d{2}\b|"
    r"2160p|1080p|720p|576p|480p|UHD|4K|Blu[- .]?Ray|BDRip|WEB[- .]?(?:DL|Rip)|HDTV|"
    r"HEVC|x26[45]|H[ .]?26[45]|AV1|HDR|DV)\b",
    re.I,
)


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_size(value):
    size = as_int(value)
    if size <= 0:
        return "未知"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    number = float(size)
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.2f} {units[index]}"


def fetch_url(url, timeout=12):
    request = urllib.request.Request(url, headers={"User-Agent": "MeijuZhuixin/1.9.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        length = as_int(response.headers.get("Content-Length"))
        if length > MAX_RESPONSE_BYTES:
            raise RuntimeError("响应数据过大")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("响应数据过大")
        return body


def fetch_json(params, timeout=12):
    url = WIKIDATA_API + "?" + urllib.parse.urlencode(params)
    return json.loads(fetch_url(url, timeout=timeout).decode("utf-8", "replace"))


def fetch_douban_json(url, timeout=10):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://movie.douban.com/",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Douban HTTP {response.status}")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("豆瓣响应数据过大")
        return json.loads(body.decode("utf-8", "replace"))


def discover_douban(category="us_tv", force=False, with_status=False):
    if category != "us_tv":
        raise ValueError("只支持最新美剧")

    def finish(result_items, partial=False, cached_result=False):
        if with_status:
            return result_items, {"partial": partial, "cached": cached_result}
        return result_items

    now = time.monotonic()
    with DISCOVER_CACHE_LOCK:
        cached = DISCOVER_CACHE.get(category)
        if cached and not force and now - cached[0] < DISCOVER_CACHE_TTL_SECONDS:
            return finish(cached[1], cached_result=True)

    items = []
    seen_ids = set()
    complete = True
    for page_start in range(0, DISCOVER_TARGET_ITEMS, DISCOVER_PAGE_SIZE):
        url = DOUBAN_TV_API + "?" + urllib.parse.urlencode({
            "type": "tv", "tag": "美剧", "sort": "time",
            "page_limit": DISCOVER_PAGE_SIZE, "page_start": page_start,
        })
        try:
            payload = fetch_douban_json(url)
            rows = payload.get("subjects") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError("豆瓣返回了无法识别的数据")
        except Exception:
            # A stale full list is more useful than a blank page when the
            # first request is temporarily rejected by the upstream service.
            if not items and cached:
                LOGGER.warning("豆瓣最新美剧刷新失败，继续使用缓存")
                return finish(cached[1], partial=True, cached_result=True)
            if not items:
                raise
            complete = False
            LOGGER.warning("豆瓣最新美剧第 %s 页读取失败，保留已取得结果", page_start // DISCOVER_PAGE_SIZE + 1)
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            subject_id = str(row.get("id", ""))
            title = str(row.get("title", "")).strip()
            if not subject_id.isdigit() or not title or subject_id in seen_ids:
                continue
            seen_ids.add(subject_id)
            items.append({
                "id": subject_id,
                "title": title[:120],
                "rate": str(row.get("rate", "")).strip() or "暂无评分",
            })
            if len(items) >= DISCOVER_TARGET_ITEMS:
                break
        if len(rows) < DISCOVER_PAGE_SIZE or len(items) >= DISCOVER_TARGET_ITEMS:
            break

    # Do not replace a good cached list with a partial list caused by a later
    # page failure. The partial list is still returned for the current visit.
    if complete:
        with DISCOVER_CACHE_LOCK:
            DISCOVER_CACHE[category] = (time.monotonic(), items)
    return finish(items, partial=not complete)


def first_label(entity, languages):
    labels = entity.get("labels", {})
    for language in languages:
        value = labels.get(language, {}).get("value", "").strip()
        if value:
            return value
    return ""


def first_description(entity, languages):
    descriptions = entity.get("descriptions", {})
    for language in languages:
        value = descriptions.get(language, {}).get("value", "").strip()
        if value:
            return value
    return ""


def lookup_wikidata_title_options(query):
    query = query.strip()
    is_chinese = bool(CJK_TEXT.search(query))
    language = "zh" if is_chinese else "en"
    terms = (query, f"{query} 电影" if is_chinese else f"{query} film")
    search_rows = []
    seen_ids = set()
    for term in terms:
        payload = fetch_json({
            "action": "wbsearchentities",
            "search": term,
            "language": language,
            "uselang": language,
            "type": "item",
            "limit": 10,
            "format": "json",
        })
        for row in payload.get("search", []):
            entity_id = row.get("id", "")
            if re.fullmatch(r"Q\d+", entity_id) and entity_id not in seen_ids:
                seen_ids.add(entity_id)
                search_rows.append(row)
        if len(search_rows) >= 10:
            break
    if not search_rows:
        return []

    ids = [row["id"] for row in search_rows[:12]]
    entity_payload = fetch_json({
        "action": "wbgetentities",
        "ids": "|".join(ids),
        "props": "labels|descriptions",
        "languages": "zh|zh-cn|zh-hans|en",
        "languagefallback": 1,
        "format": "json",
    })
    entities = entity_payload.get("entities", {})
    options = []
    seen_titles = set()
    for index, row in enumerate(search_rows):
        entity = entities.get(row["id"], {})
        english = first_label(entity, ("en",))
        chinese = first_label(entity, ("zh-cn", "zh-hans", "zh"))
        if is_chinese and not CJK_TEXT.search(chinese):
            row_label = row.get("label", "")
            chinese = row_label if CJK_TEXT.search(row_label) else ""
        zh_description = first_description(entity, ("zh-cn", "zh-hans", "zh"))
        en_description = first_description(entity, ("en",))
        description = zh_description or en_description or row.get("description", "")
        if not english or not chinese or not CJK_TEXT.search(chinese):
            continue
        if not MEDIA_DESCRIPTION.search(" ".join((description, row.get("description", "")))):
            continue
        title_key = (english.casefold(), chinese)
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        exact = query.casefold() in {english.casefold(), chinese.casefold(), str(row.get("label", "")).casefold()}
        options.append({
            "id": row["id"],
            "englishTitle": english,
            "chineseTitle": chinese,
            "description": description[:240],
            "score": (100 if exact else 0) + max(0, 20 - index),
        })
    options.sort(key=lambda item: item["score"], reverse=True)
    for item in options:
        item.pop("score", None)
    options = options[:6]
    return options


def lookup_douban_title_options(query):
    """Use Douban's public suggestion feed as an independent fallback."""
    url = DOUBAN_SUGGEST_API.format(urllib.parse.quote(query))
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://movie.douban.com/",
    })
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError(f"Douban HTTP {response.status}")
        rows = json.loads(response.read(512 * 1024).decode("utf-8", "replace"))
    options = []
    for row in rows if isinstance(rows, list) else []:
        chinese = str(row.get("title", "")).strip()
        subtitle = str(row.get("sub_title", "")).strip()
        english = subtitle if LATIN_TEXT.search(subtitle) and not CJK_TEXT.search(subtitle) else ""
        if not chinese or not english:
            continue
        options.append({
            "id": "douban-" + str(row.get("id", "")),
            "englishTitle": english[:240],
            "chineseTitle": chinese[:120],
            "description": (str(row.get("year", "")).strip() + " · 豆瓣影视资料").strip(" ·"),
        })
    return options


def lookup_offline_title_options(query):
    key = re.sub(r"\s+", "", query).casefold()
    match = OFFLINE_TITLE_ALIASES.get(key)
    if match:
        english, chinese, description = match
        return [{"id": "offline-" + key, "englishTitle": english, "chineseTitle": chinese, "description": description}]
    normalized_query = normalize_title_key(query)
    chinese = ENGLISH_TITLE_ALIASES.get(normalized_query)
    if not chinese:
        return []
    return [{
        "id": "offline-en-" + normalized_query,
        "englishTitle": query.strip(),
        "chineseTitle": chinese,
        "description": "内置影视片名资料",
    }]


def lookup_title_options(query):
    query = query.strip()
    cache_key = query.casefold()
    now = time.monotonic()
    with TITLE_CACHE_LOCK:
        cached = TITLE_CACHE.get(cache_key)
        if cached and now - cached[0] < TITLE_CACHE_TTL_SECONDS:
            return cached[1]

    offline = lookup_offline_title_options(query)
    if offline:
        with TITLE_CACHE_LOCK:
            TITLE_CACHE[cache_key] = (now, offline)
        return offline

    providers = (lookup_wikidata_title_options, lookup_douban_title_options)
    futures = [PROVIDER_EXECUTOR.submit(provider, query) for provider in providers]
    options, failures, seen = [], [], set()
    try:
        for future in concurrent.futures.as_completed(futures, timeout=15):
            try:
                rows = future.result()
            except Exception as error:
                failures.append(type(error).__name__)
                continue
            for item in rows:
                title_key = (item["englishTitle"].casefold(), item["chineseTitle"])
                if title_key not in seen:
                    seen.add(title_key)
                    options.append(item)
    except concurrent.futures.TimeoutError:
        failures.append("Timeout")
    if not options and failures:
        LOGGER.warning("片名在线识别不可用（%s），已尝试本地词库", ", ".join(failures))
    options = options[:6]
    # Never cache an empty answer for hours: it may only mean a temporary DNS,
    # TLS or outbound-network failure on the NAS.
    if options:
        with TITLE_CACHE_LOCK:
            TITLE_CACHE[cache_key] = (now, options)
    return options


def normalize_title_key(value):
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def extract_release_title(value):
    """Remove scene-release metadata while preserving the actual work title."""
    cleaned = re.sub(r"[._]+", " ", value).strip()
    marker = RELEASE_STOP.search(cleaned)
    base = cleaned[:marker.start()] if marker else cleaned
    base = re.sub(r"\s+", " ", base).strip(" -[]()")
    return base or cleaned[:120]


def chinese_number(value):
    number = as_int(value)
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 20:
        return "十" + (digits[number % 10] if number % 10 else "")
    if number < 100:
        return digits[number // 10] + "十" + (digits[number % 10] if number % 10 else "")
    return str(number)


def build_release_translation(release_name, chinese_title, base_title=""):
    display_title = chinese_title or base_title or "片名未识别"
    parts = [f"《{display_title}》"]
    episode = re.search(r"\bS(\d{1,2})E(\d{1,3})(?:[- .]?E(\d{1,3}))?\b", release_name, re.I)
    season = re.search(r"\bS(\d{1,2})\b", release_name, re.I)
    if episode:
        episode_text = f"第{chinese_number(episode.group(1))}季第{chinese_number(episode.group(2))}集"
        if episode.group(3):
            episode_text += f"至第{chinese_number(episode.group(3))}集"
        parts[0] += episode_text
    elif season:
        parts[0] += f"第{chinese_number(season.group(1))}季"

    quality = next((item for pattern, item in (
        (r"\b2160p\b|\bUHD\b", "2160p超高清版"),
        (r"\b1080p\b", "1080p高清版"),
        (r"\b720p\b", "720p高清版"),
        (r"\b480p\b", "480p标清版"),
    ) if re.search(pattern, release_name, re.I)), "")
    if quality:
        parts.append(quality)

    if re.search(r"\b(?:HEVC|x265|H[ .]?265)\b", release_name, re.I):
        parts.append("采用H.265/x265编码")
    elif re.search(r"\b(?:AVC|x264|H[ .]?264)\b", release_name, re.I):
        parts.append("采用H.264/x264编码")
    elif re.search(r"\bAV1\b", release_name, re.I):
        parts.append("采用AV1编码")

    group = re.search(r"-([A-Za-z0-9][A-Za-z0-9._-]{0,50})$", release_name.strip())
    if group:
        parts.append(f"由{group.group(1)}发布组制作")
    sentence = "，".join(parts) + "。"
    if not chinese_title:
        sentence += "（中文片名暂未识别）"
    return sentence


def detect_release_metadata(release_name):
    """Extract filterable release attributes without changing the source title."""
    year_matches = [int(value) for value in re.findall(r"\b(18\d{2}|19\d{2}|20\d{2}|2100)\b", release_name)]
    if re.search(r"\b(?:2160p|UHD|4K)\b", release_name, re.I):
        resolution = "2160p"
    else:
        match = re.search(r"\b(1080p|720p|576p|480p)\b", release_name, re.I)
        resolution = match.group(1).lower() if match else "other"

    if re.search(r"\b(?:HEVC|x265|H[ .]?265)\b", release_name, re.I):
        codec = "hevc"
    elif re.search(r"\b(?:AVC|x264|H[ .]?264)\b", release_name, re.I):
        codec = "avc"
    elif re.search(r"\bAV1\b", release_name, re.I):
        codec = "av1"
    else:
        codec = "other"

    if re.search(r"\b(?:BDRemux|REMUX)\b", release_name, re.I):
        source_format = "remux"
    elif re.search(r"\bWEB[- .]?DL\b", release_name, re.I):
        source_format = "webdl"
    elif re.search(r"\bWEB[- .]?Rip\b", release_name, re.I):
        source_format = "webrip"
    elif re.search(r"\bBlu[- .]?Ray\b|\bBDRemux\b", release_name, re.I):
        source_format = "bluray"
    elif re.search(r"\bBDRip\b", release_name, re.I):
        source_format = "bdrip"
    elif re.search(r"\bHDTV\b", release_name, re.I):
        source_format = "hdtv"
    elif re.search(r"\bDVD(?:Rip)?\b", release_name, re.I):
        source_format = "dvd"
    elif re.search(r"\bCAM\b", release_name, re.I):
        source_format = "cam"
    elif re.search(r"\bTS\b", release_name, re.I):
        source_format = "ts"
    else:
        source_format = "other"

    episode_match = re.search(r"\bS(\d{1,2})E(\d{1,3})(?:[- .]?E(\d{1,3}))?\b", release_name, re.I)
    season_match = re.search(r"\bS(\d{1,2})\b", release_name, re.I)
    return {
        # The final valid year is generally the scene-release year; this also
        # avoids treating numeric titles such as "1917.2019" as year 1917.
        "releaseYear": year_matches[-1] if year_matches else None,
        "resolution": resolution,
        "codec": codec,
        "sourceFormat": source_format,
        "season": as_int(episode_match.group(1) if episode_match else season_match.group(1), None) if (episode_match or season_match) else None,
        "episode": as_int(episode_match.group(2), None) if episode_match else None,
        "episodeEnd": as_int(episode_match.group(3), None) if episode_match and episode_match.group(3) else None,
    }


def resolve_release_chinese_title(base_title, query="", query_chinese_title=""):
    key = normalize_title_key(base_title)
    if key in ENGLISH_TITLE_ALIASES:
        return ENGLISH_TITLE_ALIASES[key]
    if query_chinese_title and key == normalize_title_key(query):
        return query_chinese_title
    options = lookup_title_options(base_title)
    for option in options:
        if normalize_title_key(option.get("englishTitle", "")) == key:
            return option.get("chineseTitle", "")
    return ""


def annotate_search_results(results, query="", query_chinese_title=""):
    """Attach an accurate, per-release Chinese description to each result."""
    bases = []
    for item in results:
        base = extract_release_title(item.get("title", ""))
        if base and base not in bases:
            bases.append(base)

    resolved = {}
    unresolved = []
    for base in bases:
        alias = ENGLISH_TITLE_ALIASES.get(normalize_title_key(base), "")
        if alias:
            resolved[base] = alias
        elif query_chinese_title and normalize_title_key(base) == normalize_title_key(query):
            resolved[base] = query_chinese_title
        elif len(unresolved) < 8:
            unresolved.append(base)

    futures = {
        LOOKUP_EXECUTOR.submit(resolve_release_chinese_title, base, query, query_chinese_title): base
        for base in unresolved
    }
    try:
        for future in concurrent.futures.as_completed(futures, timeout=18):
            base = futures[future]
            try:
                resolved[base] = future.result()
            except Exception as error:
                resolved[base] = ""
                LOGGER.info("资源片名识别失败（%s）：%s", base[:80], error)
    except concurrent.futures.TimeoutError:
        LOGGER.info("部分资源片名识别超时")

    annotated = []
    for item in results:
        row = dict(item)
        base = extract_release_title(row.get("title", ""))
        chinese = resolved.get(base, "")
        row["releaseTitle"] = base
        row["chineseTitle"] = chinese
        row["translation"] = build_release_translation(row.get("title", ""), chinese, base)
        row.update(detect_release_metadata(row.get("title", "")))
        annotated.append(row)
    return annotated


def normalize_magnet(value, fallback_name="未知标题"):
    value = html.unescape(value.strip())
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() != "magnet":
        return None
    params = urllib.parse.parse_qs(parsed.query)
    xt = params.get("xt", [""])[0]
    match = re.fullmatch(r"urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", xt, re.I)
    if not match:
        return None
    info_hash = match.group(1).lower()
    name = params.get("dn", [fallback_name])[0].strip()[:500] or fallback_name
    return {
        "title": name,
        "detailUrl": "",
        "magnet": "magnet:?xt=urn:btih:" + info_hash + "&dn=" + urllib.parse.quote(name),
        "seeders": 0,
        "seedersKnown": False,
        "leechers": 0,
        "size": "未知",
        "sizeBytes": None,
        "added": 0,
        "uploader": "镜像来源",
    }


def search_apibay(query):
    payload = json.loads(fetch_url(API_URL.format(urllib.parse.quote(query))).decode("utf-8", "replace"))
    if not isinstance(payload, list):
        raise RuntimeError("上游返回了无法识别的数据")
    if payload and payload[0].get("name") == "No results returned":
        return []

    results = []
    for item in payload[:200]:
        if not isinstance(item, dict):
            continue
        info_hash = str(item.get("info_hash", "")).strip()
        name = str(item.get("name", "未知标题")).strip()[:500]
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", info_hash):
            continue
        item_id = str(item.get("id", ""))
        size_bytes = as_int(item.get("size"))
        results.append({
            "title": name,
            "detailUrl": f"https://thepiratebay.org/description.php?id={item_id}" if item_id.isdigit() else "",
            "magnet": "magnet:?xt=urn:btih:" + info_hash.lower() + "&dn=" + urllib.parse.quote(name),
            "seeders": as_int(item.get("seeders")),
            "seedersKnown": True,
            "leechers": as_int(item.get("leechers")),
            "size": format_size(size_bytes),
            "sizeBytes": size_bytes if size_bytes > 0 else None,
            "added": as_int(item.get("added")),
            "uploader": str(item.get("username", "未知"))[:100],
        })
    results.sort(key=lambda row: row["seeders"], reverse=True)
    return results[:MAX_RESULTS]


def search_mirror(query, mirror):
    url = f"https://{mirror}/search/{urllib.parse.quote(query)}/1/99/0"
    page = fetch_url(url, timeout=15).decode("utf-8", "replace")
    candidates = re.findall(r'''href\s*=\s*["'](magnet:\?[^"']+)["']''', page, re.I)
    results = []
    seen = set()
    for candidate in candidates:
        item = normalize_magnet(candidate)
        if item and item["magnet"] not in seen:
            seen.add(item["magnet"])
            results.append(item)
        if len(results) >= MAX_RESULTS:
            break
    return results


def search_all(query):
    key = hashlib.sha256(query.casefold().encode("utf-8")).hexdigest()
    now = time.monotonic()
    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    errors = []
    try:
        results = search_apibay(query)
        if results:
            payload = {"results": results, "source": "apibay", "warnings": []}
            with CACHE_LOCK:
                CACHE[key] = (now, payload)
            return payload
    except Exception as error:
        errors.append(f"apibay: {type(error).__name__}")

    for mirror in MIRRORS:
        try:
            results = search_mirror(query, mirror)
            if results:
                payload = {"results": results, "source": mirror, "warnings": errors}
                with CACHE_LOCK:
                    CACHE[key] = (now, payload)
                return payload
        except Exception as error:
            errors.append(f"{mirror}: {type(error).__name__}")
    return {"results": [], "source": "none", "warnings": errors}


if hasattr(socketserver, "UnixStreamServer"):
    class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
else:  # Enables local source validation on Windows; fnOS Linux provides UnixStreamServer.
    ThreadingUnixHTTPServer = None


class Handler(SimpleHTTPRequestHandler):
    server_version = "MeijuZhuixin/1.9.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    @property
    def is_gateway(self):
        return bool(getattr(self.server, "is_gateway", False))

    def is_authorized(self):
        if self.is_gateway:
            if getattr(self.server, "local_test", False):
                return True
            return self.headers.get("X-Trim-Isadmin", "").lower() == "true"
        supplied = self.headers.get("Authorization", "")
        token = getattr(self.server, "api_token", "")
        return bool(token) and hmac.compare_digest(supplied, "Bearer " + token)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def subtitle_error(self, error):
        if isinstance(error, SubtitleError):
            self.send_json(getattr(error, "http_status", 400), {"ok": False, "error": str(error), "code": error.code})
        elif isinstance(error, ValueError):
            self.send_json(400, {"ok": False, "error": "字幕参数无效，请检查后重试。"})
        else:
            # Never log upstream URLs, credentials, or request bodies.
            LOGGER.warning("字幕服务请求失败：%s", type(error).__name__)
            self.send_json(502, {"ok": False, "error": "字幕服务暂不可用，请稍后重试。"})

    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path
        if self.is_gateway:
            prefix = next((p for p in (GATEWAY_PREFIX, LEGACY_GATEWAY_PREFIX) if route.startswith(p + "/")), None)
            if prefix is None:
                self.send_json(404, {"ok": False, "error": "路径不存在"})
                return
            route = route[len(prefix):]
        if not self.is_authorized():
            self.send_json(403, {"ok": False, "error": "未授权访问"})
            return
        expected = getattr(self.server, "subtitle_csrf", "")
        supplied = self.headers.get("X-Subtitle-CSRF", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            self.send_json(403, {"ok": False, "error": "页面已过期，请刷新后重试。"})
            return
        if route not in ("/api/subtitles/settings", "/api/subtitles/download"):
            self.send_json(404, {"ok": False, "error": "路径不存在"})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"ok": False, "error": "需要 JSON 请求"})
            return
        length = as_int(self.headers.get("Content-Length"), -1)
        if not 0 < length <= 8192:
            self.send_json(400, {"ok": False, "error": "请求长度无效"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("object required")
            if route.endswith("/settings"):
                result = self.server.subtitle_service.update_settings(payload)
                self.send_json(200, {"ok": True, **result, "csrf": expected})
                return
            values = [payload.get(k, "") for k in ("file_id", "video_name", "language")]
            if not all(isinstance(v, str) for v in values) or len(values[0]) > 200 or len(values[1]) > 500:
                raise ValueError("invalid arguments")
            data, name = self.server.subtitle_service.download(values[0], values[1], values[2] or "zh-CN")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename=subtitle.txt; filename*=UTF-8''" + urllib.parse.quote(name, safe=""))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as error:
            self.subtitle_error(error)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if self.is_gateway:
            prefix = next((item for item in (GATEWAY_PREFIX, LEGACY_GATEWAY_PREFIX)
                           if route == item or route.startswith(item + "/")), None)
            if prefix is None:
                self.send_json(404, {"ok": False, "error": "路径不存在"})
                return
            route = route[len(prefix):] or "/"

        if route == "/api/health":
            self.send_json(200, {"ok": True, "version": "1.9.2", "mode": "gateway" if self.is_gateway else "token-api"})
            return
        if not self.is_authorized():
            self.send_json(403, {"ok": False, "error": "未授权访问"})
            return
        if route == "/api/subtitles/settings":
            self.send_json(200, {"ok": True, **self.server.subtitle_service.public_settings(), "csrf": self.server.subtitle_csrf})
            return
        if route == "/api/subtitles/files":
            candidate_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            try:
                if not candidate_id or len(candidate_id) > 200:
                    raise ValueError("invalid id")
                self.send_json(200, self.server.subtitle_service.files(candidate_id))
            except Exception as error:
                self.subtitle_error(error)
            return
        if route == "/api/config":
            if not self.is_gateway:
                self.send_json(404, {"ok": False, "error": "路径不存在"})
                return
            self.send_json(200, {"ok": True, "token": getattr(self.server, "api_token", ""), "port": getattr(self.server, "api_port", 18088)})
            return
        if route == "/api/title-options":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if not query or len(query) > 120:
                self.send_json(400, {"ok": False, "error": "片名长度必须为 1–120 个字符"})
                return
            try:
                options = lookup_title_options(query)
                self.send_json(200, {"ok": True, "query": query, "options": options})
            except Exception as error:
                LOGGER.warning("片名识别失败：%s", error)
                self.send_json(200, {"ok": True, "query": query, "options": [], "warning": "片名识别服务暂不可用；请稍后重试或直接输入英文片名"})
            return
        if route == "/api/discover":
            category = urllib.parse.parse_qs(parsed.query).get("category", ["us_tv"])[0]
            if category != "us_tv":
                self.send_json(400, {"ok": False, "error": "只支持最新美剧"})
                return
            force = urllib.parse.parse_qs(parsed.query).get("refresh", [""])[0] == "1"
            try:
                result = discover_douban(category, force=force, with_status=True)
                # Keeps the API resilient for integrations that replace the
                # discovery function with an old list-only implementation.
                if isinstance(result, tuple):
                    items, status = result
                else:
                    items, status = result, {"partial": False, "cached": False}
                self.send_json(200, {
                    "ok": True, "category": category, "items": items,
                    "partial": bool(status.get("partial")),
                    "cached": bool(status.get("cached")),
                })
            except Exception as error:
                LOGGER.warning("豆瓣最新美剧读取失败：%s", error)
                self.send_json(502, {"ok": False, "error": "豆瓣最新美剧暂时无法读取，请稍后重试"})
            return
        if route == "/api/search":
            query_params = urllib.parse.parse_qs(parsed.query)
            query = query_params.get("q", [""])[0].strip()
            chinese_title = query_params.get("zh", [""])[0].strip()[:120]
            base_title = query_params.get("base", [""])[0].strip()[:200]
            if not query or len(query) > 200:
                self.send_json(400, {"ok": False, "error": "搜索词长度必须为 1–200 个字符"})
                return
            payload = search_all(query)
            translated_results = annotate_search_results(payload.get("results", []), base_title or query, chinese_title)
            self.send_json(200, {"ok": True, "query": query, **payload, "results": translated_results})
            return
        if route == "/api/subtitles":
            params = urllib.parse.parse_qs(parsed.query)
            release = params.get("title", [""])[0].strip()
            chinese = params.get("zh", [""])[0].strip()
            if not release or len(release) > 500 or len(chinese) > 120:
                self.send_json(400, {"ok": False, "error": "片源标题长度必须为 1–500 个字符"})
                return
            try:
                payload = self.server.subtitle_service.search(release, extract_release_title(release), chinese, detect_release_metadata,
                                                              backup_only=params.get("source", [""])[0] == "backup")
                self.send_json(200, payload)
            except Exception as error:
                self.subtitle_error(error)
            return
        if not self.is_gateway:
            self.send_json(404, {"ok": False, "error": "此端口只提供令牌 API"})
            return

        self.path = "/tv.html" if route == "/" else route + (("?" + parsed.query) if parsed.query else "")
        super().do_GET()

    def end_headers(self):
        if self.command == "GET" and "/api/" not in self.path:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'self' http: https:",
            )
        super().end_headers()

    def log_message(self, fmt, *args):
        client = self.client_address if isinstance(self.client_address, str) else self.client_address[0]
        LOGGER.info("%s - %s", client or "gateway", fmt % args)


def configure_logging(path):
    LOGGER.setLevel(logging.INFO)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def main():
    port = as_int(os.environ.get("PORT") or os.environ.get("TRIM_SERVICE_PORT"), 18088)
    socket_path = os.environ.get("SOCKET_PATH", "")
    token_file = Path(os.environ.get("TOKEN_FILE", APP_DIR / ".dev-token"))
    log_file = os.environ.get("LOG_FILE", str(APP_DIR / "media-link-helper.log"))
    if not 1 <= port <= 65535:
        raise SystemExit("Invalid port")
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else "dev-token"
    if len(token) < 16 and token != "dev-token":
        raise SystemExit("API token is invalid")

    configure_logging(log_file)
    subtitle_service = SubtitleService(Path(os.environ.get("SUBTITLE_CONFIG", token_file.parent / "subtitle-settings.json")))
    subtitle_csrf = secrets.token_urlsafe(32)
    mimetypes.add_type("application/javascript", ".js")
    direct_server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    direct_server.is_gateway = False
    direct_server.api_token = token
    direct_server.subtitle_service = subtitle_service
    direct_server.subtitle_csrf = subtitle_csrf
    threading.Thread(target=direct_server.serve_forever, daemon=True).start()

    if socket_path:
        if ThreadingUnixHTTPServer is None:
            raise SystemExit("Unix domain sockets are unavailable on this platform")
        socket_file = Path(socket_path)
        if socket_file.exists() or socket_file.is_socket():
            socket_file.unlink()
        gateway_server = ThreadingUnixHTTPServer(socket_path, Handler)
        os.chmod(socket_path, 0o666)
        gateway_server.local_test = False
    else:
        gateway_server = ThreadingHTTPServer(("127.0.0.1", port + 1), Handler)
        gateway_server.local_test = True
    gateway_server.is_gateway = True
    gateway_server.api_token = token
    gateway_server.api_port = port
    gateway_server.subtitle_service = subtitle_service
    gateway_server.subtitle_csrf = subtitle_csrf

    def stop_servers(_signum=None, _frame=None):
        threading.Thread(target=gateway_server.shutdown, daemon=True).start()
        direct_server.shutdown()

    signal.signal(signal.SIGTERM, stop_servers)
    signal.signal(signal.SIGINT, stop_servers)
    LOGGER.info("服务已启动：API 端口 %s，网关 %s", port, socket_path or f"127.0.0.1:{port + 1}")
    try:
        gateway_server.serve_forever()
    finally:
        direct_server.server_close()
        gateway_server.server_close()
        if socket_path and Path(socket_path).exists():
            Path(socket_path).unlink()


if __name__ == "__main__":
    main()
