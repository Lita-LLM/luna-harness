"""Скан сети на API моделей — на локальных фальшивых серверах, без сети."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from luna.net import scan


def _server(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, body = routes.get(self.path, (404, "nope"))
            raw = body if isinstance(body, str) else json.dumps(body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw.encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _scan_port(port):
    return scan.scan(plan=[("127.0.0.1", (port,))], workers=4)


def test_openai_sovmestimyy_naiden():
    srv = _server({"/v1/models": (200, {"data": [{"id": "qwen"}, {"id": "llama"}]})})
    try:
        found = _scan_port(srv.server_address[1])
    finally:
        srv.shutdown()
    assert len(found) == 1
    f = found[0]
    assert f.base_url == "http://localhost:%d/v1" % srv.server_address[1]
    assert f.models == ["qwen", "llama"] and not f.needs_key


def test_ollama_i_klyuch():
    srv = _server({"/v1/models": (401, {"error": "no key"})})
    srv2 = _server({"/api/tags": (200, {"models": [{"name": "gemma3"}]})})
    try:
        a = _scan_port(srv.server_address[1])
        b = _scan_port(srv2.server_address[1])
    finally:
        srv.shutdown()
        srv2.shutdown()
    assert a[0].needs_key
    assert b[0].hint == "ollama" and b[0].models == ["gemma3"]
    assert b[0].base_url.endswith("/v1")


def test_veb_stranica_ne_api():
    srv = _server({"/v1/models": (200, "<html>привет</html>"),
                   "/models": (200, {"hello": 1})})
    try:
        assert _scan_port(srv.server_address[1]) == []
    finally:
        srv.shutdown()


def test_zakrytyy_port_i_otmena():
    ev = threading.Event()
    ev.set()
    assert scan.scan(plan=[("127.0.0.1", (1,))], cancel=ev) == []
