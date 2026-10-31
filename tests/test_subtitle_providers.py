"""Deterministic provider contracts: no credentials or network access required."""
import json
import os
import pathlib
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))
import server
import subtitle_providers as sp

RELEASE = "Example.2020.S01E02.1080p.WEB-DL-GRP"
SRT = b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:02,000\r\n\xe7\xa4\xba\xe4\xbe\x8b\r\n\r\n"


def assrt_row(title=RELEASE, languages=None, files=None):
    return {"id": 123456, "videoname": title, "native_name": "Example",
            "lang": {"langlist": {"langchs": True} if languages is None else languages},
            "subtype": "SubRip(srt)",
            "filelist": files if files is not None else [{"f": title + ".chs.srt"}]}


def assrt_payload(rows):
    return {"status": 0, "sub": {"subs": rows}}


def os_row(title=RELEASE, language="zh-cn", **feature):
    return {"id": "42", "attributes": {"release": title, "language": language,
            "feature_details": {"movie_name": "Example", **feature},
            "files": [{"file_id": 99, "file_name": title + ".srt"}]}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.directory.name) / "private" / "subtitles.json"
        self.service = sp.SubtitleService(self.path)
        self.responses = []
        self.calls = []
        self.service._http = self.http

    def tearDown(self):
        self.directory.cleanup()

    def configure(self, assrt=True, opensubtitles=True):
        self.service.update_settings({"assrt_token": "secret-assrt" if assrt else "",
                                      "opensubtitles_api_key": "secret-opensubtitles" if opensubtitles else ""})

    def http(self, provider, url, headers=None, body=None, api=False):
        self.calls.append({"provider": provider, "url": url, "headers": headers, "body": body, "api": api})
        self.assertTrue(self.responses, "Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, {}, json.dumps(response).encode()

    def search(self, release=RELEASE):
        return self.service.search(release, "Example", "示例", server.detect_release_metadata)

    def select_assrt_file(self, name=None, languages=None):
        name = name or RELEASE + ".chs.srt"
        self.responses.append(assrt_payload([assrt_row(languages=languages)]))
        candidate = self.search()["items"][0]
        detail = assrt_payload([dict(assrt_row(languages=languages), filelist=[{"f": name, "url": "http://file0.assrt.net/onthefly/123456/1.srt?api=1"}])])
        self.responses.append(detail)
        file = self.service.files(candidate["id"])["files"][0]
        return file, detail

    def test_unconfigured_distinct_from_no_matches(self):
        result = self.search()
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual([p["status"] for p in result["providers"]], ["unconfigured", "unconfigured"])
        self.assertEqual(self.calls, [])
        self.configure()
        self.responses.extend([assrt_payload([]), {"data": []}])
        self.assertEqual(self.search()["status"], "ok")
        self.assertEqual([c["provider"] for c in self.calls], ["assrt", "opensubtitles"])

    def test_primary_candidate_skips_backup_and_returns_opaque_ids(self):
        self.configure()
        self.responses.append(assrt_payload([assrt_row()]))
        result = self.search()
        self.assertEqual(result["items"][0]["provider"], "assrt")
        self.assertTrue(result["items"][0]["autoMatch"])
        self.assertTrue(result["items"][0]["downloadable"])
        self.assertNotIn("123456", result["items"][0]["id"])
        self.assertNotIn("url", json.dumps(result["items"]).lower())
        self.assertEqual(result["providers"][1]["status"], "skipped")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("filelist=1", self.calls[0]["url"])
        self.assertEqual(self.calls[0]["headers"]["Authorization"], "Bearer secret-assrt")
        self.assertEqual(result, self.search())
        self.assertEqual(len(self.calls), 1)

    def test_title_only_or_different_group_not_auto_match(self):
        self.configure()
        for title in ("Example", RELEASE.replace("-GRP", "-OTHER")):
            with self.subTest(title=title):
                self.service._searches.clear()
                self.responses.append(assrt_payload([assrt_row(title)]))
                result = self.search()
                self.assertFalse(result["items"][0]["autoMatch"])

    def test_backup_only_after_invalid_primary_and_language_filter(self):
        self.configure()
        self.responses.extend([assrt_payload([assrt_row(languages={"langeng": True, "langdou": True})]),
                               {"data": [os_row(language="en"), os_row(language="zh-tw")]}])
        result = self.search()
        self.assertEqual(result["items"][0]["provider"], "opensubtitles")
        self.assertEqual(result["items"][0]["languages"], ["繁体"])
        self.assertEqual(result["providers"][0]["status"], "no_matches")
        url = self.calls[1]["url"]
        self.assertIn("languages=zh-cn%2Czh-tw", url)
        self.assertIn("season_number=1", url)
        self.assertIn("episode_number=2", url)
        self.assertEqual(self.calls[1]["headers"], {"Api-Key": "secret-opensubtitles"})

    def test_wrong_identity_year_season_episode_rejected(self):
        self.configure()
        titles = [RELEASE.replace("Example", "Other"), RELEASE.replace("2020", "2021"),
                  RELEASE.replace("S01E02", "S02E02"), RELEASE.replace("S01E02", "S01E03"),
                  RELEASE.replace("Example", "Example.2")]
        rows = [dict(assrt_row(title), native_name=title, id=123456 + i) for i, title in enumerate(titles)]
        self.responses.extend([assrt_payload(rows), {"data": []}])
        self.assertEqual(self.search()["items"], [])

    def test_conflicting_native_name_rejected(self):
        self.configure()
        self.responses.extend([assrt_payload([dict(assrt_row(), native_name="Example.2020.S02E02")]), {"data": []}])
        self.assertEqual(self.search()["items"], [])

    def test_backup_structured_metadata_conflicts_rejected(self):
        self.configure(assrt=False)
        self.responses.append({"data": [os_row(year=2021), os_row(season_number=2), os_row(episode_number=3)]})
        self.assertEqual(self.search()["items"], [])

    def test_episode_range_allows_containing_range(self):
        self.configure()
        self.responses.append(assrt_payload([assrt_row(RELEASE.replace("S01E02", "S01E01-E03"))]))
        self.assertEqual(len(self.search()["items"]), 1)

    def test_upstream_errors_fallback_and_backoff(self):
        self.configure()
        self.responses.extend([(401, {}, b'{"message":"raw-secret-must-not-leak"}'), {"data": [os_row()]}])
        result = self.search()
        self.assertEqual(result["providers"][0]["status"], "auth_error")
        self.assertEqual(result["items"][0]["provider"], "opensubtitles")
        self.assertNotIn("raw-secret", json.dumps(result))
        self.service._searches.clear()
        self.responses.append({"data": [os_row()]})
        result = self.search()
        self.assertEqual(len([c for c in self.calls if c["provider"] == "assrt"]), 1)

    def test_rate_limit_backoff_no_retry_storm(self):
        self.configure(opensubtitles=False)
        self.responses.append((429, {"retry-after": "60"}, b'{}'))
        first = self.search()
        self.service._searches.clear()
        second = self.search()
        self.assertEqual(first["providers"][0]["status"], "rate_limited")
        self.assertEqual(second["providers"][0]["status"], "rate_limited")
        self.assertEqual(first["status"], "unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_assrt_error_payload_not_reported_no_match(self):
        self.configure(opensubtitles=False)
        self.responses.append({"status": 20001})
        self.assertEqual(self.search()["providers"][0]["status"], "auth_error")

    def test_quota_is_distinct(self):
        self.configure(assrt=False)
        self.responses.append((406, {}, b'{"message":"quota"}'))
        self.assertEqual(self.search()["providers"][1]["status"], "quota")

    def test_settings_atomic_private_blank_and_clear(self):
        self.configure()
        result = self.service.update_settings({"assrt_token": "", "opensubtitles_api_key": "  ", "language": "zh-TW"})
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result, {"assrt_configured": True, "opensubtitles_configured": True, "language": "zh-TW"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(sp.SubtitleService(self.path).public_settings(), result)
        self.service.update_settings({"clear_assrt_token": True})
        self.assertEqual(json.loads(self.path.read_text())["assrt_token"], "")
        self.assertFalse(self.service.public_settings()["assrt_configured"])
        previous = self.path.read_bytes()
        with patch.object(sp.os, "replace", side_effect=OSError("secret-path")):
            with self.assertRaisesRegex(sp.SubtitleError, "无法保存"):
                self.service.update_settings({"assrt_token": "new"})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertFalse(self.service.public_settings()["assrt_configured"])
        self.assertEqual(list(self.path.parent.glob(".subtitle-settings-*")), [])

    def test_invalid_settings_do_not_write(self):
        for payload in ({"assrt_token": "secret\nheader"}, {"language": "en"}, {"assrt_token": []}, []):
            with self.assertRaises(sp.SubtitleError):
                self.service.update_settings(payload)
        self.assertFalse(self.path.exists())

    def test_files_opaque_and_individual_only(self):
        self.configure()
        self.responses.append(assrt_payload([assrt_row()]))
        candidate = self.search()["items"][0]
        self.responses.append(assrt_payload([dict(assrt_row(), filelist=[
            {"f": RELEASE + ".zip", "url": "https://file0.assrt.net/archive.zip"},
            {"f": RELEASE + ".en.srt", "url": "https://file0.assrt.net/en.srt"},
            {"f": RELEASE.replace("E02", "E03") + ".chs.srt", "url": "https://file0.assrt.net/wrong.srt"},
            {"f": RELEASE + ".chs.srt", "url": "https://file0.assrt.net/a.srt?secret=private"}])]))
        result = self.service.files(candidate["id"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["language"], "zh-CN")
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(result["websiteUrl"].startswith("https://assrt.net/"))

    def test_archive_only_explicit_unsupported(self):
        self.configure()
        self.responses.append(assrt_payload([assrt_row()]))
        candidate = self.search()["items"][0]
        self.responses.append(assrt_payload([dict(assrt_row(), filelist=[{"f": "archive.rar"}])]))
        result = self.service.files(candidate["id"])
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["files"], [])
        self.assertIn("网站", result["message"])

    def test_download_original_bytes_name_and_no_cdn_credentials(self):
        self.configure()
        file, detail = self.select_assrt_file()
        self.responses.extend([detail, (200, {"content-type": "application/octet-stream"}, SRT)])
        content, filename = self.service.download(file["id"], "/movies/Example.2020.S01E02.mkv")
        self.assertEqual(content, SRT)
        self.assertEqual(filename, "Example.2020.S01E02.zh-CN.srt")
        self.assertTrue(self.calls[-1]["url"].startswith("https://file0.assrt.net/"))
        self.assertIsNone(self.calls[-1]["headers"])
        self.assertFalse(self.calls[-1]["api"])

    def test_blank_video_name_preserves_original_subtitle_name(self):
        self.configure()
        file, detail = self.select_assrt_file()
        self.responses.extend([detail, (200, {}, SRT)])
        content, filename = self.service.download(file["id"])
        self.assertEqual(content, SRT)
        self.assertEqual(filename, RELEASE + ".chs.srt")

    def test_bitmap_only_primary_uses_backup(self):
        self.configure()
        row=assrt_row(files=[{'f': 'Example.sup'}]);row['subtype']='PGS'
        self.responses.extend([assrt_payload([row]), {'data':[os_row()]}])
        self.assertEqual(self.search()['items'][0]['provider'], 'opensubtitles')

    def test_whole_season_not_automatic(self):
        self.configure()
        title=RELEASE.replace('S01E02', 'S01')
        self.responses.append(assrt_payload([assrt_row(title)]))
        self.assertFalse(self.search(title)['items'][0]['autoMatch'])

    def test_partial_source_failure_is_visible(self):
        self.configure()
        self.responses.extend([(503, {}, b'{}'), {'data':[]}])
        self.assertIn('部分来源', self.search()['message'])

    def test_manual_backup_does_not_reuse_primary_cache(self):
        self.configure()
        self.responses.append(assrt_payload([assrt_row()]))
        self.assertEqual(self.search()['items'][0]['provider'],'assrt')
        self.responses.append({'data':[os_row()]})
        value=self.service.search(RELEASE,'Example','示例',server.detect_release_metadata,backup_only=True)
        self.assertEqual(value['items'][0]['provider'],'opensubtitles')
        self.assertEqual(len(self.calls),2)

    def test_manual_backup_unconfigured_is_clear(self):
        self.configure(opensubtitles=False)
        value=self.service.search(RELEASE,'Example','示例',server.detect_release_metadata,backup_only=True)
        self.assertEqual(value['status'],'unconfigured')
        self.assertEqual(self.calls,[])

    def test_traditional_never_renamed_simplified(self):
        self.configure()
        file, detail = self.select_assrt_file(RELEASE + ".cht.srt", {"langchs": True, "langcht": True})
        self.responses.extend([detail, (200, {}, SRT)])
        _, filename = self.service.download(file["id"], "Example.zh-CN.mkv", "zh-CN")
        self.assertEqual(file["language"], "zh-TW")
        self.assertEqual(filename, "Example.zh-TW.srt")

    def test_ambiguous_language_labeled_unknown(self):
        self.configure()
        file, _ = self.select_assrt_file(RELEASE + ".srt", {"langchs": True, "langcht": True})
        self.assertEqual(file["language"], "zh")

    def test_opensubtitles_download_uses_file_id_post(self):
        self.configure(assrt=False)
        self.responses.append({"data": [os_row(language="zh-tw")]})
        candidate = self.search()["items"][0]
        file = self.service.files(candidate["id"])["files"][0]
        self.responses.extend([{"link": "https://dl.opensubtitles.com/download/file.srt?signature=hidden"}, (200, {}, SRT)])
        content, filename = self.service.download(file["id"], "Video.mp4")
        self.assertEqual(content, SRT)
        self.assertEqual(filename, "Video.zh-TW.srt")
        self.assertEqual(json.loads(self.calls[-2]["body"]), {"file_id": 99})
        self.assertEqual(self.calls[-2]["headers"]["Api-Key"], "secret-opensubtitles")
        self.assertIsNone(self.calls[-1]["headers"])

    def test_replaced_detail_file_rejected(self):
        self.configure()
        file, _ = self.select_assrt_file()
        self.responses.append(assrt_payload([dict(assrt_row(), filelist=[{"f": "replaced.srt", "url": "https://file0.assrt.net/other.srt"}])]))
        with self.assertRaises(sp.SubtitleError) as caught:
            self.service.download(file["id"])
        self.assertEqual(caught.exception.code, "stale_id")

    def test_stale_expired_config_change_and_bounded_cache(self):
        self.configure()
        file, _ = self.select_assrt_file()
        self.service._files[file["id"]]["expires"] = time.monotonic() - 1
        with self.assertRaises(sp.SubtitleError) as caught:
            self.service.download(file["id"])
        self.assertEqual(caught.exception.code, "stale_id")
        candidate_id = next(iter(self.service._candidates))
        self.service.update_settings({"language": "zh-TW"})
        with self.assertRaises(sp.SubtitleError):
            self.service.files(candidate_id)
        for _ in range(5):
            self.service._put(self.service._candidates, {}, 3)
        self.assertEqual(len(self.service._candidates), 3)
        for value in ("https://file0.assrt.net/arbitrary", {}, "x" * 1000):
            with self.assertRaises(sp.SubtitleError):
                self.service.files(value)

    def test_html_error_empty_binary_and_invalid_subtitles_rejected(self):
        for data, mime in ((b"", ""), (b"<html>error</html>", ""), (b'{"error":"no"}', ""),
                           (b"PK\x03\x04binaryzip", ""), (b"hello", "text/plain"), (SRT, "text/html"),
                           (b"\x00\x01\xff\xfe", "")):
            with self.subTest(data=data):
                with self.assertRaises(sp.SubtitleError):
                    sp._validate_subtitle(data, "srt", mime)
        with self.assertRaises(sp.SubtitleError) as caught:
            sp._validate_subtitle(b"a" * (8 * 1024 * 1024 + 1), "srt")
        self.assertEqual(caught.exception.code, "too_large")

    def test_supported_encodings_and_formats_preserve_input(self):
        for data, extension in ((SRT, "srt"), ("1\r\n00:00:01,000 --> 00:00:02,000\r\n字幕\r\n".encode("utf-16"), "srt"),
                                ("1\n00:00:01,000 --> 00:00:02,000\n字幕\n".encode("gb18030"), "srt"),
                                (b"WEBVTT\n\n00:01.000 --> 00:02.000\nExample\n", "vtt"),
                                (b"[Script Info]\nTitle: Example\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Example\n", "ass")):
            with self.subTest(extension=extension):
                sp._validate_subtitle(data, extension)

    def test_filename_safety_and_optional_suffix(self):
        self.assertEqual(sp._safe_name("../../x.mkv", "srt", "zh-CN"), "x.zh-CN.srt")
        self.assertEqual(sp._safe_name("C:\\movies\\x.mp4", "ass", ""), "x.ass")
        self.assertEqual(sp._safe_name("..\r\nx.mkv", "srt", "zh-CN"), "__x.zh-CN.srt")
        self.assertLess(len(sp._safe_name("长" * 200, "srt", "zh-CN").encode()), 256)


class NetworkSafetyTests(unittest.TestCase):
    def test_unapproved_http_private_and_userinfo_urls(self):
        urls = ["https://127.0.0.1/a.srt", "http://192.168.1.1/a.srt", "file:///etc/passwd",
                "https://file0.assrt.net.evil.test/a.srt", "https://file0.assrt.net:444/a.srt",
                "https://user@file0.assrt.net/a.srt", "http://user@file0.assrt.net/a.srt",
                "https://file0.assrt.net/a.srt#fragment", "https://file0.assrt.net/a\r\nHeader:x"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(sp.SubtitleError):
                    sp.SubtitleService._validated_url("assrt", url)
        self.assertEqual(sp.SubtitleService._validated_url("assrt", "http://file0.assrt.net/a.srt"), "https://file0.assrt.net/a.srt")
        with self.assertRaises(sp.SubtitleError):
            sp.SubtitleService._validated_url("opensubtitles", "http://dl.opensubtitles.com/a.srt")

    def test_dns_private_and_mixed_answers_blocked_before_connection(self):
        service = sp.SubtitleService("/nonexistent/settings-for-test.json")
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        for addr in ("127.0.0.1", "192.168.1.3", "10.0.0.1", "169.254.169.254", "::1"):
            records = [public, (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]
            with patch.object(sp.socket, "getaddrinfo", return_value=records), patch.object(sp, "_PinnedHTTPSConnection") as connection:
                with self.assertRaises(sp.SubtitleError) as caught:
                    service._http("assrt", "https://file0.assrt.net/file.srt")
                self.assertEqual(caught.exception.code, "unsafe_url")
                connection.assert_not_called()

    def test_cdn_credentials_discarded_and_redirect_blocked(self):
        service = sp.SubtitleService("/nonexistent/settings-for-test.json")
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        response = Mock(status=302)
        response.getheaders.return_value = [("Location", "https://evil.test/collect")]
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(sp.socket, "getaddrinfo", return_value=address), patch.object(sp, "_PinnedHTTPSConnection", return_value=connection) as factory:
            with self.assertRaises(sp.SubtitleError):
                service._http("assrt", "https://file0.assrt.net/file.srt", headers={"Authorization": "secret", "Api-Key": "secret"})
            sent = connection.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", sent)
            self.assertNotIn("Api-Key", sent)
            self.assertEqual(factory.call_args.args[:2], ("file0.assrt.net", "1.1.1.1"))
            connection.close.assert_called()

    def test_api_redirect_cannot_leak_credentials_to_cdn(self):
        service = sp.SubtitleService("/nonexistent/settings-for-test.json")
        response = Mock(status=302)
        response.getheaders.return_value = [("Location", "https://file0.assrt.net/collect")]
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(sp.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]), patch.object(sp, "_PinnedHTTPSConnection", return_value=connection):
            with self.assertRaises(sp.SubtitleError):
                service._http("assrt", "https://api.assrt.net/v1/sub/search", headers={"Authorization": "secret"}, api=True)
            self.assertEqual(connection.request.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
