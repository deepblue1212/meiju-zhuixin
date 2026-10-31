"""Credentialed subtitle APIs with opaque references and bounded, pinned HTTPS fetches.

Only individual SRT/ASS/SSA/VTT files are supported. A match is a version
candidate, never a promise that its timing has been verified against the video.
"""
import collections
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from subtitles import identity, search_url

USER_AGENT = "MeijuZhuixin v1.9.2"
SUPPORTED = {"srt", "ass", "ssa", "vtt"}
API_HOSTS = {"assrt": "api.assrt.net", "opensubtitles": "api.opensubtitles.com"}
SECRET_KEYS = ("assrt_token", "opensubtitles_api_key")
LANG_LABELS = {"zh-CN": "简体", "zh-TW": "繁体", "zh": "中文（简繁未标明）"}
MESSAGES = {
    "unconfigured": "尚未配置字幕服务密钥。",
    "auth_error": "字幕服务密钥无效或没有访问权限，请检查配置。",
    "rate_limited": "字幕服务请求过于频繁，请稍后重试。",
    "quota": "字幕服务下载配额已用完，请稍后重试或前往网站。",
    "unavailable": "字幕服务暂时不可用，请稍后重试或前往网站。",
    "no_matches": "没有找到符合片名、年份和剧集条件的中文字幕。",
    "stale_id": "字幕选择已过期，请重新搜索并选择。",
    "unsupported": "此条目没有可直接下载的 SRT、ASS、SSA 或 VTT 文件，请到字幕网站下载压缩包或其他格式。",
    "unsafe_url": "字幕服务返回的下载地址未通过安全检查，请前往字幕网站。",
    "invalid_subtitle": "下载内容不是有效字幕，已停止保存，请换一个文件或前往字幕网站。",
    "too_large": "字幕文件超过 8 MB 限制，请前往字幕网站下载。",
    "invalid_settings": "字幕配置无效，请检查输入。",
    "settings_error": "字幕配置无法保存，请检查应用数据目录权限。",
}


class SubtitleError(ValueError):
    """All public failures have a fixed, safe message; upstream text is discarded."""
    def __init__(self, code, http_status=400):
        self.code = code
        self.http_status = http_status
        super().__init__(MESSAGES.get(code, MESSAGES["unavailable"]))


def _integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if 0 <= number <= 10**12 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _text(value, limit=700):
    return value[:limit] if isinstance(value, str) else ""


def _format(name):
    return Path(_text(name)).suffix[1:].lower()


def _assrt_languages(row):
    lang = row.get("lang")
    lang = lang if isinstance(lang, dict) else {}
    values = lang.get("langlist")
    values = values if isinstance(values, dict) else {}
    # "双语" alone can mean Japanese/English. It does not establish Chinese.
    return [code for key, code in (("langchs", "zh-CN"), ("langcht", "zh-TW"))
            if values.get(key) is True or values.get(key) == 1]


def _file_language(name, available):
    # A file's explicit label wins over package labels and user preference.
    simplified = bool(re.search(r"(?i)(?:简体|簡體|简中|簡中|简英|chs|zh[-_.]?cn|zh[-_.]?hans|(?:^|[._ -])sc(?:[._ -]|$))", name))
    traditional = bool(re.search(r"(?i)(?:繁体|繁體|繁中|繁英|cht|zh[-_.]?tw|zh[-_.]?hant|(?:^|[._ -])tc(?:[._ -]|$))", name))
    if simplified != traditional:
        return "zh-CN" if simplified else "zh-TW"
    if len(available) == 1:
        return available[0]
    return "zh"


def _safe_name(name, extension, language):
    name = re.split(r"[/\\]", _text(name, 1000))[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")
    # Remove only real media/subtitle extensions; release group suffixes remain.
    name = re.sub(r"(?i)\.(?:mkv|mp4|avi|mov|wmv|m4v|ts|m2ts|webm|srt|ass|ssa|vtt)$", "", name)
    name = re.sub(r"(?i)\.(?:zh[-_](?:cn|tw|hans|hant)|chs|cht)$", "", name).strip(" .")
    if not name or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", name):
        name = "subtitle"
    # Stay below common 255-byte filename limits, including the suffix.
    while len(name.encode("utf-8")) > 210:
        name = name[:-1]
    suffix = "." + language if language in ("zh-CN", "zh-TW", "zh") else ""
    return name + suffix + "." + extension


def _original_name(name, extension):
    name = re.split(r"[/\\]", _text(name, 1000))[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")
    stem = re.sub(r"(?i)\.(?:srt|ass|ssa|vtt)$", "", name).strip(" .")
    while len(stem.encode("utf-8")) > 230:
        stem = stem[:-1]
    if not stem or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", stem):
        stem = "subtitle"
    return stem + "." + extension


def _validate_subtitle(data, extension, content_type=""):
    if not data or len(data) > 8 * 1024 * 1024:
        raise SubtitleError("too_large" if data else "invalid_subtitle")
    if "html" in content_type.lower() or "json" in content_type.lower():
        raise SubtitleError("invalid_subtitle")
    decoded = None
    encodings = ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030", "big5"]
    for encoding in encodings:
        try:
            decoded = data.decode(encoding)
            break
        except UnicodeError:
            continue
    if decoded is None or "\x00" in decoded or re.search(r"(?is)^\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b|\{\s*\")", decoded):
        raise SubtitleError("invalid_subtitle")
    valid = False
    if extension in {"srt", "vtt"}:
        # Require a real cue and some text following the timestamp line.
        clock = r"(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3}"
        valid = bool(re.search(clock + r"\s*-->\s*" + clock + r"[^\r\n]*[\r\n]+[^\r\n]+", decoded))
        if extension == "vtt":
            valid = valid and decoded.lstrip().startswith("WEBVTT")
    elif extension in {"ass", "ssa"}:
        valid = bool(re.search(r"(?im)^\s*\[Script Info\]\s*$", decoded) and
                     re.search(r"(?im)^\s*\[Events\]\s*$", decoded) and
                     re.search(r"(?im)^\s*Dialogue:\s*[^\r\n]*\d+:\d{2}:\d{2}\.\d{2}[^\r\n]*,\S[^\r\n]*", decoded))
    if not valid:
        raise SubtitleError("invalid_subtitle")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout)
        self._address = address

    def connect(self):
        # Pin the validated address while keeping certificate validation/SNI for
        # the original hostname. No second hostname lookup or proxy is involved.
        self.sock = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.sock.close()
            raise


class SubtitleService:
    CACHE_TTL = 15 * 60
    MAX_CANDIDATES = 256
    MAX_FILES = 1024
    TIMEOUT = 8

    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self._lock = threading.RLock()
        self._settings = {"assrt_token": "", "opensubtitles_api_key": "", "language": "zh-CN"}
        self._candidates = collections.OrderedDict()
        self._files = collections.OrderedDict()
        self._searches = collections.OrderedDict()
        self._cooldowns = {}
        self._requests = {key: collections.deque() for key in API_HOSTS}
        try:
            if self.settings_path.is_file() and self.settings_path.stat().st_size <= 16384:
                saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for key in SECRET_KEYS:
                        value = saved.get(key)
                        if isinstance(value, str) and len(value) <= 4096 and not re.search(r"[\x00-\x20\x7f]", value):
                            self._settings[key] = value
                    if saved.get("language") in ("zh-CN", "zh-TW"):
                        self._settings["language"] = saved["language"]
                os.chmod(self.settings_path, 0o600)
        except (OSError, ValueError, TypeError):
            # A broken settings file must not leak secrets or break app startup.
            pass

    def public_settings(self):
        with self._lock:
            return {"assrt_configured": bool(self._settings["assrt_token"]),
                    "opensubtitles_configured": bool(self._settings["opensubtitles_api_key"]),
                    "language": self._settings["language"]}

    def update_settings(self, payload):
        if not isinstance(payload, dict):
            raise SubtitleError("invalid_settings")
        with self._lock:
            updated = dict(self._settings)
            for key in SECRET_KEYS:
                if payload.get("clear_" + key) is True:
                    updated[key] = ""
                elif key in payload:
                    value = payload[key]
                    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
                        raise SubtitleError("invalid_settings")
                    value = value.strip()
                    if " " in value:
                        raise SubtitleError("invalid_settings")
                    if value:
                        updated[key] = value
            if "language" in payload:
                if payload["language"] not in ("zh-CN", "zh-TW"):
                    raise SubtitleError("invalid_settings")
                updated["language"] = payload["language"]
            tmp = None
            try:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".subtitle-settings-", dir=self.settings_path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(updated, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(tmp, self.settings_path)
                tmp = None
            except OSError:
                raise SubtitleError("settings_error", 500) from None
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            self._settings = updated
            self._candidates.clear()
            self._files.clear()
            self._searches.clear()
            self._cooldowns.clear()
            return self.public_settings()

    @staticmethod
    def _validated_url(provider, url, api=False):
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if api:
                allowed = host == API_HOSTS[provider]
            elif provider == "assrt":
                allowed = host == "assrt.net" or bool(re.fullmatch(r"file\d*\.assrt\.net", host))
            else:
                allowed = any(host == root or host.endswith("." + root)
                              for root in ("opensubtitles.com", "opensubtitles.org"))
            if parsed.username or parsed.password:
                raise ValueError()
            # ASSRT documents HTTP file links; upgrade those official hosts only.
            if parsed.scheme == "http" and not api and provider == "assrt" and allowed and parsed.port in (None, 80):
                parsed = parsed._replace(scheme="https", netloc=host)
            if not allowed or parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
                raise ValueError()
            if re.search(r"[\x00-\x20\x7f]", url):
                # Spaces and non-ASCII path characters are encoded by callers
                # below, but control characters must never reach HTTP headers.
                if re.search(r"[\x00-\x1f\x7f]", url):
                    raise ValueError()
            path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
            query = quote(parsed.query, safe="%=&+;,:/?@!$'()*-._~")
            return urlunsplit(("https", parsed.netloc, path, query, ""))
        except (ValueError, TypeError, KeyError):
            raise SubtitleError("unsafe_url") from None

    def _http(self, provider, url, headers=None, body=None, api=False):
        """Return (status, headers, bytes); never expose raw errors or URLs."""
        original_host = API_HOSTS[provider] if api else None
        deadline = time.monotonic() + self.TIMEOUT
        limit = 2 * 1024 * 1024 if api else 8 * 1024 * 1024
        for hop in range(4):
            url = self._validated_url(provider, url, api=api)
            parsed = urlsplit(url)
            if api and parsed.hostname != original_host:
                raise SubtitleError("unsafe_url")
            connection = None
            try:
                addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise SubtitleError("unsafe_url")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SubtitleError("unavailable", 503)
                connection = _PinnedHTTPSConnection(parsed.hostname, addresses[0][4][0], remaining)
                request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json" if api else "text/plain, */*", "Accept-Encoding": "identity"}
                # This branch is the sole credential transport. CDN requests
                # cannot receive Authorization, Api-Key, cookies or proxy auth.
                if api:
                    request_headers.update(headers or {})
                connection.request("POST" if body is not None else "GET", parsed.path + ("?" + parsed.query if parsed.query else ""), body=body, headers=request_headers)
                response = connection.getresponse()
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                if response.status in (301, 302, 303, 307, 308):
                    if hop == 3 or not response_headers.get("location") or (body is not None and response.status not in (307, 308)):
                        raise SubtitleError("unavailable", 503)
                    url = urljoin(url, response_headers["location"])
                    continue
                declared = _integer(response_headers.get("content-length"))
                if declared is not None and declared > limit:
                    raise SubtitleError("unavailable" if api else "too_large")
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SubtitleError("unavailable", 503)
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise SubtitleError("unavailable" if api else "too_large")
                return response.status, response_headers, b"".join(chunks)
            except SubtitleError:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                raise SubtitleError("unavailable", 503) from None
            finally:
                if connection:
                    connection.close()
        raise SubtitleError("unavailable", 503)

    def _api(self, provider, endpoint, params=None, data=None):
        with self._lock:
            token = self._settings["assrt_token" if provider == "assrt" else "opensubtitles_api_key"]
            if not token:
                raise SubtitleError("unconfigured")
            now = time.monotonic()
            cooldown = self._cooldowns.get(provider)
            if cooldown and now < cooldown[0]:
                raise SubtitleError(cooldown[1], 429 if cooldown[1] in ("rate_limited", "quota") else 503)
            window, maximum = (60, 18) if provider == "assrt" else (1, 4)
            calls = self._requests[provider]
            while calls and calls[0] <= now - window:
                calls.popleft()
            if len(calls) >= maximum:
                raise SubtitleError("rate_limited", 429)
            calls.append(now)
            base = "https://" + API_HOSTS[provider] + ("/v1/" if provider == "assrt" else "/api/v1/")
            url = base + endpoint
            if params:
                url += "?" + urlencode(sorted(params.items()))
            headers = {"Authorization": "Bearer " + token} if provider == "assrt" else {"Api-Key": token}
            body = None
            if data is not None:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            try:
                status, response_headers, raw = self._http(provider, url, headers, body, api=True)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                code = None
                if status == 429:
                    code = "rate_limited"
                elif status in (402, 406):
                    code = "quota"
                elif status in (401, 403):
                    code = "auth_error"
                elif not 200 <= status < 300:
                    code = "unavailable"
                elif provider == "assrt" and payload.get("status") != 0:
                    value = _integer(payload.get("status"))
                    code = {1: "auth_error", 20001: "auth_error", 30900: "rate_limited", 20900: "stale_id"}.get(value, "unavailable")
                elif not payload:
                    code = "unavailable"
                if code:
                    retry = _integer(response_headers.get("retry-after")) or (60 if code == "rate_limited" else 120)
                    self._cooldowns[provider] = (now + min(max(retry, 2), 3600), code)
                    raise SubtitleError(code, 429 if code in ("rate_limited", "quota") else 503)
                return payload
            except SubtitleError as exc:
                if exc.code == "unavailable":
                    self._cooldowns[provider] = (now + 15, "unavailable")
                raise

    def _put(self, cache, entry, maximum):
        ref = secrets.token_urlsafe(24)
        entry["expires"] = time.monotonic() + self.CACHE_TTL
        cache[ref] = entry
        while len(cache) > maximum:
            cache.popitem(last=False)
        return ref

    def _get(self, cache, ref):
        if not isinstance(ref, str) or len(ref) > 100:
            raise SubtitleError("stale_id", 410)
        entry = cache.get(ref)
        if not entry or entry["expires"] <= time.monotonic():
            cache.pop(ref, None)
            raise SubtitleError("stale_id", 410)
        return entry

    @staticmethod
    def _match(names, release, base_title, chinese_title, metadata, explicit=None):
        wanted = metadata(release)
        target_names = {identity(value) for value in (base_title, chinese_title) if value}
        candidates = []
        for name in names:
            for part in re.split(r"[/|｜]", name):
                part = re.sub(r"第[零一二三四五六七八九十百\d]+(?:季|集).*", "", part)
                part = re.sub(r"[\[(（【](?:简体|繁体|简中|繁中|中英|双语|字幕)[^\])）】]*[\])）】]", "", part)
                candidates.append(identity(part))
        if not any(value and value in target_names for value in candidates):
            return None
        snapshots = [metadata(name) for name in names]
        if explicit:
            snapshots.append(explicit)
        for candidate in snapshots:
            for key in ("releaseYear", "season"):
                if wanted.get(key) is not None and candidate.get(key) is not None and wanted[key] != candidate[key]:
                    return None
            if wanted.get("season") is None and candidate.get("season") is not None:
                return None
            if wanted.get("episode") is not None and candidate.get("episode") is not None:
                if not candidate["episode"] <= wanted["episode"] <= (candidate.get("episodeEnd") or candidate["episode"]):
                    return None
                if wanted.get("episodeEnd") and wanted["episodeEnd"] > (candidate.get("episodeEnd") or candidate["episode"]):
                    return None
        reasons, score = [], 0
        for key, label in (("releaseYear", "年份"), ("season", "季"), ("episode", "集"), ("sourceFormat", "片源类型"), ("resolution", "分辨率")):
            if wanted.get(key) not in (None, "other") and any(s.get(key) == wanted[key] for s in snapshots):
                reasons.append(label + "一致")
                score += 4 if key in ("season", "episode") else 2
        compact = lambda value: "".join(c for c in re.sub(r"(?i)\.(?:mkv|mp4|avi|m4v|ts|m2ts|webm)$", "", value).casefold() if c.isalnum())
        same = any(compact(release) == compact(name) for name in names)
        if same:
            score += 30
            reasons.insert(0, "发布名一致")
        has_version = any(wanted.get(k) not in (None, "other") for k in ("sourceFormat", "resolution"))
        # A whole season label does not identify one video's subtitle.
        if wanted.get("season") is not None and wanted.get("episode") is None:
            has_version = False
        return {"score": score, "reasons": reasons, "autoMatch": bool(same and has_version),
                "matchLabel": "同发布名 · 仍需确认同步" if same else "候选字幕 · 需确认版本"}

    def _assrt_search(self, query, release, base_title, chinese_title, metadata):
        payload = self._api("assrt", "sub/search", {"q": query, "filelist": 1})
        section = payload.get("sub", {})
        if not isinstance(section, dict) or not isinstance(section.get("subs"), list):
            raise SubtitleError("unavailable", 503)
        results = []
        for row in section["subs"][:30]:
            if not isinstance(row, dict):
                continue
            provider_id = _integer(row.get("id"))
            languages = _assrt_languages(row)
            if provider_id is None or not languages:
                continue
            names = [_text(row.get(key)) for key in ("videoname", "native_name", "title")]
            names = [name for name in names if name]
            match = self._match(names, release, base_title, chinese_title, metadata)
            if not match:
                continue
            entries = row.get("filelist")
            entries = entries if isinstance(entries, list) else []
            formats = sorted({_format(file.get("f", "")) for file in entries if isinstance(file, dict)} & SUPPORTED)
            if not formats:
                formats = [ext for ext in sorted(SUPPORTED) if re.search(r"\b" + ext + r"\b", _text(row.get("subtype")), re.I)]
            if not formats and (entries or re.search(r"(?i)SUP|IDX|VobSub|PGS|bitmap", _text(row.get("subtype")))):
                continue
            results.append({"provider": "assrt", "provider_id": provider_id, "title": names[0], "languages": languages,
                            "formats": [ext.upper() for ext in formats], "release": release, "base_title": base_title,
                            "chinese_title": chinese_title, "metadata": metadata, "websiteUrl": "https://assrt.net/xml/sub/" + str(provider_id) + ".xml", **match})
        return results

    def _opensubtitles_search(self, query, release, base_title, chinese_title, metadata):
        wanted = metadata(release)
        params = {"languages": "zh-cn,zh-tw", "query": query.lower()}
        for key, parameter in (("season", "season_number"), ("episode", "episode_number")):
            if wanted.get(key) is not None:
                params[parameter] = wanted[key]
        payload = self._api("opensubtitles", "subtitles", params)
        if not isinstance(payload.get("data"), list):
            raise SubtitleError("unavailable", 503)
        results = []
        for row in payload["data"][:60]:
            if not isinstance(row, dict) or not isinstance(row.get("attributes"), dict):
                continue
            attrs = row["attributes"]
            code = {"zh-cn": "zh-CN", "zh-tw": "zh-TW"}.get(_text(attrs.get("language")))
            if not code:
                continue
            feature = attrs.get("feature_details")
            feature = feature if isinstance(feature, dict) else {}
            names = [_text(attrs.get("release"))] + [_text(feature.get(key)) for key in ("movie_name", "title", "parent_title")]
            names = [name for name in names if name]
            explicit = {"releaseYear": _integer(feature.get("year")), "season": _integer(feature.get("season_number")), "episode": _integer(feature.get("episode_number"))}
            explicit = {key: value for key, value in explicit.items() if value is not None}
            match = self._match(names, release, base_title, chinese_title, metadata, explicit)
            files = attrs.get("files")
            files = files if isinstance(files, list) else []
            files = [{"provider_file_id": _integer(f.get("file_id")), "name": _text(f.get("file_name")), "language": code}
                     for f in files[:100] if isinstance(f, dict) and _integer(f.get("file_id")) is not None and _format(f.get("file_name", "")) in SUPPORTED]
            if not match or not files:
                continue
            results.append({"provider": "opensubtitles", "provider_id": _text(str(row.get("id", ""))), "title": names[0],
                            "languages": [code], "formats": sorted({_format(f["name"]).upper() for f in files if _format(f["name"]) in SUPPORTED}),
                            "provider_files": files, "release": release, "base_title": base_title, "chinese_title": chinese_title,
                            "metadata": metadata, "websiteUrl": "https://www.opensubtitles.com/", **match})
        return results

    def search(self, release, base_title, chinese_title, metadata, backup_only=False):
        if not all(isinstance(value, str) and len(value) <= 1000 for value in (release, base_title, chinese_title)) or not callable(metadata):
            raise SubtitleError("invalid_settings")
        query = base_title.strip() or chinese_title.strip()
        if not query:
            raise SubtitleError("invalid_settings")
        with self._lock:
            key = (release, base_title, chinese_title, bool(backup_only))
            cached = self._searches.get(key)
            if cached and cached[0] > time.monotonic() and all(item["id"] in self._candidates for item in cached[1]["items"]):
                # Return a copy so callers cannot mutate cached public results.
                return json.loads(json.dumps(cached[1]))
            providers, results = [], []
            for provider, secret in (("assrt", "assrt_token"), ("opensubtitles", "opensubtitles_api_key")):
                if backup_only and provider == "assrt":
                    providers.append({"id": provider, "status": "skipped", "message": "已按你的选择切换至备用来源。"})
                    continue
                if results:
                    providers.append({"id": provider, "status": "skipped", "message": "已找到主服务候选，备用服务未请求。"})
                    continue
                if not self._settings[secret]:
                    providers.append({"id": provider, "status": "unconfigured", "message": MESSAGES["unconfigured"]})
                    continue
                try:
                    method = self._assrt_search if provider == "assrt" else self._opensubtitles_search
                    results = method(query, release, base_title, chinese_title, metadata)
                    status = "ok" if results else "no_matches"
                    providers.append({"id": provider, "status": status, "message": "找到中文字幕候选。" if results else MESSAGES["no_matches"]})
                except SubtitleError as exc:
                    providers.append({"id": provider, "status": exc.code, "message": str(exc)})
            results.sort(key=lambda row: (row["score"] + (1 if self._settings["language"] in row["languages"] else 0)), reverse=True)
            items = []
            for entry in results[:12]:
                candidate_id = self._put(self._candidates, entry, self.MAX_CANDIDATES)
                items.append({"id": candidate_id, **{key: entry[key] for key in ("provider", "title", "formats", "reasons", "matchLabel", "score", "autoMatch")},
                              "languages": [LANG_LABELS[code] for code in entry["languages"]], "downloadable": True})
            if items or any(row["status"] == "no_matches" for row in providers):
                status = "ok"
            elif all(row["status"] in ("unconfigured", "skipped") for row in providers):
                status = "unconfigured"
            else:
                status = "unavailable"
            message = "找到中文字幕候选；下载后仍需核对时间轴。" if items else MESSAGES["no_matches" if status == "ok" else status]
            if not items and status == "ok" and any(p["status"] not in ("no_matches", "unconfigured", "skipped") for p in providers):
                message = "暂未找到匹配字幕，部分来源查询失败；请查看来源状态或稍后重试。"
            response = {"ok": True, "status": status, "message": message, "items": items, "providers": providers, "searchUrl": search_url(query)}
            self._searches[key] = (time.monotonic() + (60 if status == "ok" else 10), response)
            while len(self._searches) > 64:
                self._searches.popitem(last=False)
            return json.loads(json.dumps(response))

    def _assrt_detail(self, candidate):
        payload = self._api("assrt", "sub/detail", {"id": candidate["provider_id"]})
        section = payload.get("sub", {})
        rows = section.get("subs", []) if isinstance(section, dict) else []
        row = next((row for row in rows if isinstance(row, dict) and _integer(row.get("id")) == candidate["provider_id"]), None) if isinstance(rows, list) else None
        if row is None:
            raise SubtitleError("stale_id", 410)
        files = row.get("filelist")
        if not isinstance(files, list):
            files = []
        if not files and _format(row.get("filename", "")) in SUPPORTED:
            files = [{"f": row["filename"], "url": row.get("url", "")}]
        return files[:100]

    def files(self, candidate_id):
        with self._lock:
            candidate = self._get(self._candidates, candidate_id)
            if candidate.get("file_refs"):
                refs = candidate["file_refs"]
                if all(ref in self._files and self._files[ref]["expires"] > time.monotonic() for ref in refs):
                    return {"ok": True, "status": "ok", "files": [self._public_file(ref, self._files[ref]) for ref in refs], "websiteUrl": candidate["websiteUrl"]}
            rows = self._assrt_detail(candidate) if candidate["provider"] == "assrt" else candidate["provider_files"]
            files = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                name = _text(row.get("f") if candidate["provider"] == "assrt" else row.get("name"))
                extension = _format(name)
                if extension not in SUPPORTED:
                    continue
                # A season pack must not offer a different episode as the
                # subtitle for the selected video. Identity was checked above;
                # here only explicit filename year/season/episode conflicts apply.
                wanted, actual = candidate["metadata"](candidate["release"]), candidate["metadata"](name)
                if any(wanted.get(k) is not None and actual.get(k) is not None and wanted[k] != actual[k] for k in ("releaseYear", "season")):
                    continue
                if wanted.get("episode") is not None and actual.get("episode") is not None and not actual["episode"] <= wanted["episode"] <= (actual.get("episodeEnd") or actual["episode"]):
                    continue
                if wanted.get("episodeEnd") and actual.get("episode") is not None and (actual.get("episodeEnd") or actual["episode"]) < wanted["episodeEnd"]:
                    continue
                language = _file_language(name, candidate["languages"])
                explicit_chinese = bool(re.search(r"(?i)简|簡|繁|中文|中英|chs|cht|zh[-_.]?(?:cn|tw|hans|hant)", name))
                if not explicit_chinese and re.search(r"(?i)(?:英语|英語|英文|日语|日語|韩语|韓語|法语|法語|西班牙语|俄语|俄語|德语|德語|(?:^|[._ -])(?:en|eng|ja|jpn|ko|kor|fr|fre|fra|de|ger|deu|es|spa|ru|rus|pt|por|it|ita)(?:[._ -]|$))", name):
                    continue
                entry = {"candidate_id": candidate_id, "provider": candidate["provider"], "name": name, "language": language,
                         "format": extension, "index": index, "provider_file_id": row.get("provider_file_id")}
                ref = self._put(self._files, entry, self.MAX_FILES)
                files.append(self._public_file(ref, entry))
            candidate["file_refs"] = [file["id"] for file in files]
            return {"ok": True, "status": "ok" if files else "unsupported", "files": files,
                    "message": "请选择与视频版本对应的字幕文件。" if files else MESSAGES["unsupported"], "websiteUrl": candidate["websiteUrl"]}

    @staticmethod
    def _public_file(ref, entry):
        return {"id": ref, "name": entry["name"], "language": entry["language"], "format": entry["format"].upper()}

    def download(self, file_id, video_name="", language="zh-CN"):
        if not isinstance(video_name, str) or len(video_name) > 1000 or language not in ("", "zh", "zh-CN", "zh-TW"):
            raise SubtitleError("invalid_settings")
        with self._lock:
            entry = self._get(self._files, file_id)
            candidate = self._get(self._candidates, entry["candidate_id"])
            provider = entry["provider"]
            if provider == "assrt":
                # ASSRT links expire. Resolve a fresh link and bind it to the
                # same file index/name before every download.
                rows = self._assrt_detail(candidate)
                index = entry["index"]
                if index >= len(rows) or not isinstance(rows[index], dict) or rows[index].get("f") != entry["name"]:
                    raise SubtitleError("stale_id", 410)
                url = rows[index].get("url")
            else:
                payload = self._api("opensubtitles", "download", data={"file_id": entry["provider_file_id"]})
                url = payload.get("link")
            url = self._validated_url(provider, url)
            status, headers, data = self._http(provider, url)
            if status in (401, 403, 404, 410):
                raise SubtitleError("stale_id", 410)
            if status == 429:
                raise SubtitleError("rate_limited", 429)
            if not 200 <= status < 300:
                raise SubtitleError("unavailable", 503)
            _validate_subtitle(data, entry["format"], headers.get("content-type", ""))
            # Preference cannot convert Chinese scripts; label the actual file.
            suffix = entry["language"] if language else ""
            filename = _safe_name(video_name, entry["format"], suffix) if video_name.strip() else _original_name(entry["name"], entry["format"])
            return data, filename
