"""Клетка в web_fetch (хосты, приватные адреса, редиректы) и jsonschema_lite."""
import http.server
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from luna.core.jsonschema_lite import extract_json, validate
from luna.tools import web
from luna.tools.registry import ToolContext
from luna.tools.web import web_fetch_tool
from luna.worker.cage import Cage

_REAL_IP_REASON = web._ip_reason
_REAL_RESOLVE = web._resolve


def fake_dns(table):
    def resolve(host):
        try:
            return [str(__import__("ipaddress").ip_address(host))]
        except ValueError:
            pass
        if host in table:
            return list(table[host])
        raise socket.gaierror("нет такого хоста: " + host)
    return resolve


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LUNA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def ctx(self, cage=None, fetcher=None):
        ctx = ToolContext(cwd=Path(self.tmp.name), web_fetcher=fetcher)
        ctx.cage = cage
        return ctx

    def fetch(self, ctx, url):
        return web_fetch_tool().run(ctx, {"url": url})


class IpReasonTest(unittest.TestCase):
    def test_blocked(self):
        for ip in ("127.0.0.1", "127.8.8.8", "10.0.0.1", "172.16.5.5", "192.168.1.1",
                   "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                   "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1",
                   "fe80::1%eth0", "fc00::1", "fd12::1", "ff02::1",
                   "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
                   "2002:7f00:1::1", "2002:a00:1::1"):
            with self.subTest(ip=ip):
                self.assertTrue(web._ip_reason(ip), ip)

    def test_public(self):
        for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111",
                   "2a00:1450:4010:c05::8b"):
            with self.subTest(ip=ip):
                self.assertEqual(web._ip_reason(ip), "", ip)

    def test_garbage(self):
        self.assertTrue(web._ip_reason("not-an-ip"))

    def test_resolve_literal_without_dns(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
            self.assertEqual(web._resolve("127.0.0.1"), ["127.0.0.1"])
            self.assertEqual(web._resolve("::1"), ["::1"])


class CagePreCheckTest(_Env):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(web, "_resolve", fake_dns({
            "ria.ru": ["178.248.233.32"], "news.ria.ru": ["178.248.233.33"],
            "t.me": ["149.154.167.99"], "evil.ria.ru": ["10.0.0.7"],
            "mixed.ria.ru": ["178.248.233.40", "127.0.0.1"],
            "v6.ria.ru": ["::ffff:192.168.0.1"],
        }))
        p.start()
        self.addCleanup(p.stop)
        self.seen = []

    def fetcher(self, url):
        self.seen.append(url)
        return "текст страницы"

    def cage(self, **kw):
        kw.setdefault("tools", ["web_fetch"])
        kw.setdefault("fetch_hosts", ["*.ria.ru", "ria.ru", "t.me"])
        return Cage(**kw)

    def test_allowed_host_goes_through(self):
        res = self.fetch(self.ctx(self.cage(), self.fetcher), "https://news.ria.ru/a/b")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(self.seen, ["https://news.ria.ru/a/b"])

    def test_host_not_in_list(self):
        res = self.fetch(self.ctx(self.cage(), self.fetcher), "https://example.com/")
        self.assertFalse(res.ok)
        self.assertTrue(res.text.startswith("клетка: "), res.text)
        self.assertIn("example.com", res.text)
        self.assertEqual(self.seen, [])

    def test_query_refused(self):
        res = self.fetch(self.ctx(self.cage(), self.fetcher), "https://t.me/s/chan?before=5")
        self.assertFalse(res.ok)
        self.assertIn("fetch_query", res.text)
        res = self.fetch(self.ctx(self.cage(fetch_query=True), self.fetcher),
                         "https://t.me/s/chan?before=5")
        self.assertTrue(res.ok, res.text)

    def test_private_dns_refused(self):
        for host in ("evil.ria.ru", "mixed.ria.ru", "v6.ria.ru"):
            with self.subTest(host=host):
                res = self.fetch(self.ctx(self.cage(), self.fetcher), "http://" + host + "/")
                self.assertFalse(res.ok)
                self.assertIn("клетка:", res.text)
                self.assertIn("не публичный", res.text)
        self.assertEqual(self.seen, [])

    def test_unresolvable_refused(self):
        res = self.fetch(self.ctx(self.cage(), self.fetcher), "http://nope.ria.ru/")
        self.assertFalse(res.ok)
        self.assertIn("не разрешается", res.text)

    def test_literal_ips_refused_any_host_cage(self):
        cage = self.cage(fetch_hosts=None)
        for url in ("http://127.0.0.1/", "http://[::1]:8080/", "http://10.1.1.1/",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"):
            with self.subTest(url=url):
                res = self.fetch(self.ctx(cage, self.fetcher), url)
                self.assertFalse(res.ok)
                self.assertIn("клетка:", res.text)
        res = self.fetch(self.ctx(cage, self.fetcher), "http://8.8.8.8/")
        self.assertTrue(res.ok, res.text)

    def test_numeric_host_forms(self):
        # Десятичная запись 127.0.0.1: решает настоящий getaddrinfo без сети.
        try:
            ips = socket.getaddrinfo("2130706433", None)
        except OSError:
            self.skipTest("платформа не понимает десятичный IPv4")
        if not ips:
            self.skipTest("пусто")
        with mock.patch.object(web, "_resolve", _REAL_RESOLVE):
            res = self.fetch(self.ctx(self.cage(fetch_hosts=None), self.fetcher),
                             "http://2130706433/")
        self.assertFalse(res.ok)
        self.assertIn("клетка:", res.text)

    def test_tricky_urls_refused(self):
        cage = self.cage()
        for url in ("http://user:pw@ria.ru/", "http://ria.ru\\@evil.com/",
                    "http://ria.ru /", "http://ria.ru/\x00", "http://ria.ru:99999/"):
            with self.subTest(url=url):
                res = self.fetch(self.ctx(cage, self.fetcher), url)
                self.assertFalse(res.ok)
                self.assertIn("клетка:", res.text)
        self.assertEqual(self.seen, [])

    def test_no_cage_unchanged(self):
        res = self.fetch(self.ctx(None, self.fetcher), "http://127.0.0.1/?q=1")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(self.seen, ["http://127.0.0.1/?q=1"])

    def test_context_without_cage_attribute(self):
        ctx = ToolContext(cwd=Path(self.tmp.name), web_fetcher=self.fetcher)
        if hasattr(ctx, "cage"):
            ctx.cage = None
        res = self.fetch(ctx, "https://example.com/")
        self.assertTrue(res.ok)


class _Handler(http.server.BaseHTTPRequestHandler):
    port = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        routes = {
            "/ok": None,
            "/to-ok": "http://good.test:%d/ok",
            "/to-evil": "http://evil.example:%d/ok",
            "/to-inner": "http://inner.test:%d/ok",
            "/to-query": "http://good.test:%d/ok?x=1",
            "/to-file": "file:///etc/passwd",
            "/to-loop": "http://127.0.0.1:%d/ok",
            "/chain": "http://other.test:%d/to-inner",
        }
        target = routes.get(self.path.split("?")[0], "missing")
        if target == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if target is None:
            body = ("<html><body><p>Привет из " + self.headers.get("Host", "")
                    + "</p></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", target % self.port if "%d" in target else target)
        self.send_header("Content-Length", "0")
        self.end_headers()


class CageRedirectTest(_Env):
    """Настоящий urllib против локального сервера: петлю 127.0.0.1 тесту
    разрешаем точечно, остальное проверяется как в бою."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        _Handler.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        for target, value in (
                ("_resolve", fake_dns({"good.test": ["127.0.0.1"],
                                       "other.test": ["127.0.0.1"],
                                       "inner.test": ["10.9.9.9"],
                                       "evil.example": ["127.0.0.1"]})),
                ("_ip_reason", lambda ip: "" if ip == "127.0.0.1" else _REAL_IP_REASON(ip))):
            p = mock.patch.object(web, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.cage = Cage(tools=["web_fetch"],
                         fetch_hosts=["good.test", "other.test", "inner.test"])
        self.base = "http://good.test:%d" % _Handler.port

    def test_direct_ok(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/ok")
        self.assertTrue(res.ok, res.text)
        self.assertIn("Привет из good.test", res.text)

    def test_redirect_ok(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/to-ok")
        self.assertTrue(res.ok, res.text)
        self.assertIn("Привет", res.text)

    def test_redirect_to_foreign_host(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/to-evil")
        self.assertFalse(res.ok)
        self.assertIn("клетка: редирект", res.text)
        self.assertIn("evil.example", res.text)

    def test_redirect_to_private(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/to-inner")
        self.assertFalse(res.ok)
        self.assertIn("не публичный", res.text)

    def test_redirect_chain_second_hop(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/chain")
        self.assertFalse(res.ok)
        self.assertIn("inner.test", res.text)

    def test_redirect_query_and_scheme(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/to-query")
        self.assertFalse(res.ok)
        self.assertIn("fetch_query", res.text)
        res = self.fetch(self.ctx(self.cage), self.base + "/to-file")
        self.assertFalse(res.ok)
        self.assertIn("клетка:", res.text)

    def test_redirect_to_literal_loopback_not_in_list(self):
        res = self.fetch(self.ctx(self.cage), self.base + "/to-loop")
        self.assertFalse(res.ok)
        self.assertIn("клетка:", res.text)

    def test_env_proxy_ignored(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1",
                                          "http_proxy": "http://127.0.0.1:1"}):
            res = self.fetch(self.ctx(self.cage), self.base + "/ok")
        self.assertTrue(res.ok, res.text)

    def test_connect_time_check_blocks_rebinding(self):
        # Проверка перед запросом увидела публичный адрес, а к connect DNS
        # «передумал» — соединение всё равно не уйдёт на приватный.
        answers = iter([["8.8.8.8"], ["10.0.0.1"]])
        with mock.patch.object(web, "_resolve", lambda host: next(answers)):
            res = self.fetch(self.ctx(self.cage), self.base + "/ok")
        self.assertFalse(res.ok)
        self.assertIn("не публичный", res.text)

    def test_connection_uses_checked_ip(self):
        seen = []
        real = socket.create_connection

        def spy(addr, *a, **kw):
            seen.append(addr)
            return real(addr, *a, **kw)

        with mock.patch.object(socket, "create_connection", spy):
            res = self.fetch(self.ctx(self.cage), self.base + "/ok")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(seen, [("127.0.0.1", _Handler.port)])


class ValidateTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(validate(1, {"type": "integer"}), [])
        self.assertEqual(validate(1.0, {"type": "integer"}), [])
        self.assertTrue(validate(1.5, {"type": "integer"}))
        self.assertEqual(validate(1.5, {"type": "number"}), [])
        self.assertEqual(validate(2, {"type": "number"}), [])
        self.assertTrue(validate(True, {"type": "number"}))
        self.assertTrue(validate(True, {"type": "integer"}))
        self.assertEqual(validate(False, {"type": "boolean"}), [])
        self.assertTrue(validate(0, {"type": "boolean"}))
        self.assertEqual(validate(None, {"type": ["string", "null"]}), [])
        self.assertEqual(validate("x", {"type": ["string", "null"]}), [])
        self.assertTrue(validate(3, {"type": ["string", "null"]}))
        self.assertTrue(validate([], {"type": "object"}))
        self.assertTrue(validate({}, {"type": "array"}))
        errs = validate(5, {"type": "string"})
        self.assertEqual(len(errs), 1)
        self.assertIn("$", errs[0])
        self.assertIn("string", errs[0])
        self.assertIn("integer", errs[0])

    def test_unknown_keywords_and_bool_schemas(self):
        self.assertEqual(validate({"a": 1}, {"type": "object", "x-weird": 5,
                                             "$schema": "whatever"}), [])
        self.assertEqual(validate(5, True), [])
        self.assertEqual(validate(5, {}), [])
        self.assertTrue(validate(5, False))
        self.assertEqual(validate(5, {"type": "nonsense"}), [])

    def test_object(self):
        schema = {"type": "object",
                  "properties": {"summary": {"type": "string", "minLength": 3},
                                 "count": {"type": "integer", "minimum": 0}},
                  "required": ["summary", "count"],
                  "additionalProperties": False}
        self.assertEqual(validate({"summary": "всё ок", "count": 2}, schema), [])
        errs = validate({"summary": "a", "extra": 1}, schema)
        text = "\n".join(errs)
        self.assertIn("$.summary", text)
        self.assertIn("$.count: обязательное", text)
        self.assertIn("$.extra: лишнее", text)
        self.assertEqual(len(errs), 3)

    def test_additional_schema_and_patterns(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}},
                  "patternProperties": {"^n_": {"type": "number"}},
                  "additionalProperties": {"type": "boolean"}}
        self.assertEqual(validate({"a": "x", "n_1": 3, "flag": True}, schema), [])
        errs = validate({"n_1": "x", "flag": 1}, schema)
        self.assertEqual(len(errs), 2)
        self.assertIn("$.n_1", errs[0] + errs[1])
        self.assertIn("$.flag", errs[0] + errs[1])

    def test_weird_key_path(self):
        errs = validate({"с пробелом": 1}, {"properties": {"с пробелом": {"type": "string"}}})
        self.assertIn('$["с пробелом"]', errs[0])

    def test_array(self):
        schema = {"type": "array", "items": {"type": "object",
                                             "properties": {"url": {"type": "string",
                                                                    "pattern": "^https?://"}},
                                             "required": ["url"]},
                  "minItems": 1, "maxItems": 2}
        self.assertEqual(validate([{"url": "https://a"}], schema), [])
        self.assertTrue(validate([], schema))
        errs = validate([{"url": "ftp://x"}, {}, {"url": "http://y"}], schema)
        text = "\n".join(errs)
        self.assertIn("элементов больше 2", text)
        self.assertIn("$[0].url", text)
        self.assertIn("$[1].url: обязательное", text)
        self.assertEqual(validate([1, "a"], {"items": [{"type": "integer"},
                                                       {"type": "string"}]}), [])

    def test_string_number_limits(self):
        self.assertTrue(validate("abcd", {"maxLength": 3}))
        self.assertEqual(validate("абв", {"maxLength": 3, "minLength": 3}), [])
        self.assertTrue(validate("ab", {"minLength": 3}))
        self.assertTrue(validate(-1, {"minimum": 0}))
        self.assertTrue(validate(11, {"maximum": 10}))
        self.assertEqual(validate(10, {"maximum": 10, "minimum": 10}), [])
        self.assertTrue(validate(10, {"exclusiveMaximum": 10}))
        self.assertTrue(validate(0, {"exclusiveMinimum": 0}))
        # Ограничения строки к числу не применяются.
        self.assertEqual(validate(5, {"minLength": 10}), [])
        self.assertEqual(validate("xyz", {"pattern": "y"}), [])
        self.assertTrue(validate("xyz", {"pattern": "^y"}))
        self.assertIn("pattern", validate("x", {"pattern": "("})[0])

    def test_enum_const_strict(self):
        self.assertEqual(validate("ru", {"enum": ["ru", "en"]}), [])
        self.assertTrue(validate("de", {"enum": ["ru", "en"]}))
        self.assertTrue(validate(True, {"enum": [1, 0]}))
        self.assertTrue(validate(1, {"const": True}))
        self.assertEqual(validate(1.0, {"const": 1}), [])
        self.assertEqual(validate({"a": [1, None]}, {"const": {"a": [1, None]}}), [])
        self.assertTrue(validate({"a": [1]}, {"const": {"a": [1, None]}}))
        self.assertEqual(validate(None, {"enum": [None]}), [])

    def test_combinators(self):
        any_s = {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 5}]}
        self.assertEqual(validate("x", any_s), [])
        self.assertEqual(validate(7, any_s), [])
        errs = validate(3, any_s)
        self.assertEqual(len(errs), 1)
        self.assertIn("anyOf", errs[0])
        one_s = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
        self.assertEqual(validate(1.5, one_s), [])
        self.assertIn("несколько", validate(2, one_s)[0])
        self.assertIn("oneOf", validate("x", one_s)[0])
        all_s = {"allOf": [{"type": "string"}, {"minLength": 2}, {"maxLength": 3}]}
        self.assertEqual(validate("ab", all_s), [])
        self.assertTrue(validate("abcd", all_s))
        self.assertTrue(validate(1, {"not": {"type": "integer"}}))
        self.assertEqual(validate("1", {"not": {"type": "integer"}}), [])

    def test_nested_nullable(self):
        schema = {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": ["object", "null"],
                                                 "properties": {"t": {"type": "string"}}}}}}
        self.assertEqual(validate({"items": [None, {"t": "x"}]}, schema), [])
        errs = validate({"items": [None, {"t": 1}]}, schema)
        self.assertEqual(errs and errs[0].split(":")[0], "$.items[1].t")

    def test_deep_does_not_crash(self):
        value = []
        cur = value
        for _ in range(200):
            nxt = []
            cur.append(nxt)
            cur = nxt
        schema = {"type": "array"}
        schema_cur = schema
        for _ in range(200):
            schema_cur["items"] = {"type": "array"}
            schema_cur = schema_cur["items"]
        errs = validate(value, schema)
        self.assertTrue(any("вложенность" in e for e in errs))


class ExtractJsonTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), ({"a": 1}, ""))
        self.assertEqual(extract_json('  \n{"a": 1}\n\n '), ({"a": 1}, ""))

    def test_fence(self):
        for text in ('```json\n{"a": 1}\n```', '```\n{"a": 1}\n```',
                     '\n```JSON\n  {"a": 1}  \n```\n', '```json\r\n{"a": 1}\r\n```'):
            with self.subTest(text=text):
                self.assertEqual(extract_json(text), ({"a": 1}, ""))

    def test_rejects_prose_and_non_objects(self):
        for text in ('Вот ответ: {"a": 1}', '{"a": 1} готово', '```json\n{"a": 1}',
                     '```json\n{"a": 1}\n``` и ещё', '[1, 2]', '"строка"', '42',
                     '', '   ', '{"a": 1}{"b": 2}', '{"a": NaN}', '{"a": Infinity}',
                     'текст\n```json\n{"a":1}\n```'):
            with self.subTest(text=text):
                obj, err = extract_json(text)
                self.assertIsNone(obj)
                self.assertTrue(err)

    def test_not_text(self):
        self.assertEqual(extract_json(None)[0], None)

    def test_roundtrip_with_validate(self):
        obj, err = extract_json('```json\n{"summary": "Новости дня", "sources": ["https://ria.ru/1"]}\n```')
        self.assertEqual(err, "")
        schema = {"type": "object", "required": ["summary", "sources"],
                  "properties": {"summary": {"type": "string", "maxLength": 300},
                                 "sources": {"type": "array", "items": {"type": "string"}}},
                  "additionalProperties": False}
        self.assertEqual(validate(obj, schema), [])


if __name__ == "__main__":
    unittest.main()
