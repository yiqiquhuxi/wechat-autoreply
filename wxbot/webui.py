"""本地设置页：http://127.0.0.1:<port>/?t=<token>

只监听 127.0.0.1；接口需要启动时随机生成的 token，并校验 Host 头，
防止你浏览的其他网站偷偷改配置（比如把接口地址改成别人的服务器）。
"""
import json
import pathlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import llm
from .config import PROVIDER_PRESETS

HTML = (pathlib.Path(__file__).parent / "settings.html").read_text(encoding="utf-8")
SAMPLE = [{"me": False, "text": "在吗？明天下午的会改到几点了？", "sender": None}]


def _mask(cfg):
    key = cfg["llm"].get("api_key", "")
    cfg["llm"]["api_key"] = ""
    cfg["llm"]["api_key_hint"] = ("…" + key[-4:]) if key else ""
    return cfg


def _clean(patch, current):
    """把界面提交的数据整理成合法配置。"""
    def num(v, d, cast=int):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return d

    def names(v):
        if isinstance(v, str):
            v = v.splitlines()
        return [s.strip() for s in (v or []) if s and s.strip()]

    c = current
    out = {
        "mode": patch.get("mode") if patch.get("mode") in ("confirm", "auto") else c["mode"],
        "my_nickname": str(patch.get("my_nickname", c["my_nickname"])).strip(),
        "whitelist": {
            "private": names(patch.get("whitelist", {}).get("private", c["whitelist"]["private"])),
            "groups": names(patch.get("whitelist", {}).get("groups", c["whitelist"]["groups"])),
        },
        "group_reply_only_when_mentioned": bool(patch.get("group_reply_only_when_mentioned", True)),
        "reply_mention_back": bool(patch.get("reply_mention_back", True)),
        "quote_reply": bool(patch.get("quote_reply", True)),
        "always_reply": names(patch.get("always_reply", c.get("always_reply", []))),
        "poll_seconds": max(5, num(patch.get("poll_seconds"), c["poll_seconds"])),
        "idle_seconds_required": max(0, num(patch.get("idle_seconds_required"), c["idle_seconds_required"])),
        "quiet_hours": patch.get("quiet_hours") if isinstance(patch.get("quiet_hours"), list) else c["quiet_hours"],
        "confirm_timeout_seconds": max(10, num(patch.get("confirm_timeout_seconds"), c["confirm_timeout_seconds"])),
    }
    lim = patch.get("limits", {})
    dmin = max(0, num((lim.get("reply_delay_seconds") or [None, None])[0], c["limits"]["reply_delay_seconds"][0], float))
    dmax = max(dmin, num((lim.get("reply_delay_seconds") or [None, None])[1], c["limits"]["reply_delay_seconds"][1], float))
    out["limits"] = {
        "max_replies_per_hour": max(1, num(lim.get("max_replies_per_hour"), c["limits"]["max_replies_per_hour"])),
        "per_chat_cooldown_seconds": max(0, num(lim.get("per_chat_cooldown_seconds"), c["limits"]["per_chat_cooldown_seconds"])),
        "reply_delay_seconds": [dmin, dmax],
    }
    out["llm"] = _clean_llm(patch.get("llm", {}), c["llm"])
    return out


def _clean_llm(p, cur):
    preset = p.get("preset") if p.get("preset") in PROVIDER_PRESETS else cur.get("preset", "custom")
    key = p.get("api_key", "")
    if p.get("clear_api_key"):
        key = ""
    elif not key:
        key = cur.get("api_key", "")  # 留空表示不修改
    return {
        "preset": preset,
        "provider": PROVIDER_PRESETS[preset]["provider"],
        "base_url": str(p.get("base_url", cur.get("base_url", ""))).strip(),
        "model": str(p.get("model", cur.get("model", ""))).strip(),
        "api_key": key.strip(),
        "effort": p.get("effort") if p.get("effort") in ("low", "medium", "high") else cur.get("effort", "low"),
        "persona": str(p.get("persona", cur.get("persona", ""))),
    }


def make_handler(store, status, token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ok_host(self):
            return self.headers.get("Host") in allowed_hosts

        def _authed(self):
            return self._ok_host() and secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                t = parse_qs(u.query).get("t", [""])[0]
                if not self._ok_host() or not secrets.compare_digest(t, token):
                    return self._send(403, "请从菜单栏图标里点「打开设置」进入".encode(), "text/plain; charset=utf-8")
                page = HTML.replace("__TOKEN__", token).replace("__PRESETS__", json.dumps(PROVIDER_PRESETS))
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            if u.path == "/api/config":
                return self._send(200, _mask(store.get()))
            if u.path == "/api/status":
                cfg = store.get()
                return self._send(200, {**status.snapshot(), "enabled": cfg["enabled"], "mode": cfg["mode"]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if path == "/api/config":
                store.update(_clean(body, store.get()))
                return self._send(200, _mask(store.get()))
            if path == "/api/enabled":
                store.update({"enabled": bool(body.get("enabled"))})
                return self._send(200, {"enabled": store.get()["enabled"]})
            if path == "/api/models":
                c = _clean_llm(body.get("llm", {}), store.get()["llm"])
                try:
                    return self._send(200, {"ok": True, "models": llm.list_models(c)})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            if path == "/api/test-llm":
                c = _clean_llm(body.get("llm", {}), store.get()["llm"])
                try:
                    reply = llm.generate(c, "测试联系人", False, SAMPLE, "我")
                    return self._send(200, {"ok": True, "reply": reply or "（模型判断不需要回复）"})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": "not found"})

    return H


def start(store, status):
    port = store.get().get("web_port", 8765)
    token = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, status, token, port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/?t={token}", server
