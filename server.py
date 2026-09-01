#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Magent Console — 多 Agent 编排控制台

后端采用纯 Python 标准库（http.server + ThreadingHTTPServer），刻意不引入
FastAPI/Flask：这样换一台新机器时，install.sh 只需装 tmux 和 agent CLI，
不必赌 pip 源能连通，也不会因为某个依赖装不上而整盘失败。

启动：  python3 server.py [--port 8899] [--host 127.0.0.1] [--open]

API:
    GET  /api/health                 健康检查
    GET  /api/profile?cols=&rows=    本机性能评估与 agent 数推荐
    GET  /api/agents                 agent CLI 注册表与可用性
    GET  /api/config                 读取配置（角色库 + 自定义 agent + 密钥占位）
    POST /api/config                 保存配置（凭据落盘到 ~/.magent-console/config.json）
    POST /api/config/test            测试某个 agent/模型的可用性与鉴权连通性
    GET  /api/sessions               tmux 会话列表
    GET  /api/status?session=&lines= 各 pane 实时状态与输出
    GET  /api/diff?session=          各 pane 的 git 改动汇总
    POST /api/launch                 启动编排
    POST /api/send                   向某 pane 或全体发指令
    POST /api/stop                   停止会话
    POST /api/grab                   把某 pane 输出落盘
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.benchmark import collect as collect_profile       # noqa: E402
from core.agents import list_agents, resolve_command        # noqa: E402
from core import config as config_mod                       # noqa: E402
from core.tmuxctl import Tmux, TmuxError                    # noqa: E402
from core.orchestrator import Orchestrator, LaunchConfig    # noqa: E402

STATIC_DIR = HERE / "static"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 全局单例
TMUX = Tmux()
ORCH = Orchestrator(TMUX)


# ================================================================= 工具
def _ok(*a):
    """包装成功响应。只取第一个入参作为 data。"""
    return {"ok": True, "data": a[0] if a else None}


def _err(msg: str, code: int = 400) -> Dict[str, Any]:
    return {"ok": False, "error": msg, "code": code}


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "MagentConsole/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------- 基础 IO
    def _write_json(self, obj: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, rel: str) -> None:
        """路径穿越防护：解析后的绝对路径必须仍在 STATIC_DIR 内。"""
        rel = rel.lstrip("/") or "index.html"
        # 静态根目录本身就是 static/，允许两种写法：/app.js 与 /static/app.js
        if rel == "static" or rel.startswith("static/"):
            rel = rel[len("static"):].lstrip("/") or "index.html"
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "Not Found")
            return
        data = target.read_bytes()
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------- GET
    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if not u.path.startswith("/api/"):
            return self._serve_static(u.path)

        route = u.path[len("/api/"):]

        try:
            if route == "health":
                return self._write_json(_ok({
                    "status": "up",
                    "tmux": TMUX.available(),
                    "tmux_version": Tmux.version(),
                    "python": sys.version.split()[0],
                    "cwd": str(HERE),
                }))

            if route == "profile":
                cols = int(q.get("cols", [0])[0] or 0) or None
                rows = int(q.get("rows", [0])[0] or 0) or None
                bench = q.get("bench", ["1"])[0] not in ("0", "false")
                prof = collect_profile(terminal_cols=cols, terminal_rows=rows,
                                       run_bench=bench)
                return self._write_json(_ok(prof))

            if route == "agents":
                config_mod.sync_registry()
                return self._write_json(_ok({"agents": list_agents()}))

            if route == "config":
                config_mod.sync_registry()
                return self._write_json(_ok(config_mod.build_config_view()))

            if route == "sessions":
                if not TMUX.available():
                    return self._write_json(
                        _err("tmux 未安装，无法列出会话", 503), 503)
                return self._write_json(_ok({"sessions": TMUX.list_sessions()}))

            if route == "status":
                session = q.get("session", [""])[0]
                if not session:
                    return self._write_json(_err("缺少 session 参数", 400), 400)
                lines = int(q.get("lines", ["40"])[0] or 40)
                return self._write_json(_ok(ORCH.status(session, lines)))

            if route == "diff":
                session = q.get("session", [""])[0]
                if not session:
                    return self._write_json(_err("缺少 session 参数", 400), 400)
                return self._write_json(_ok(ORCH.diff(session)))

        except TmuxError as e:
            return self._write_json(_err(str(e), 503), 503)
        except Exception as e:                      # noqa: BLE001
            return self._write_json(_err("%s: %s" % (type(e).__name__, e), 500), 500)

        return self._write_json(_err("未知接口 %s" % u.path, 404), 404)

    # ---------------------------------------------------- POST
    def do_POST(self) -> None:
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._write_json(_err("Not Found", 404), 404)

        route = u.path[len("/api/"):]
        body = self._read_json()

        try:
            if route == "config":
                return self._write_json(_ok(config_mod.save_config(body)))

            if route == "config/test":
                agent_id = body.get("agent", "")
                if not agent_id:
                    return self._write_json(_err("缺少 agent 参数", 400), 400)
                return self._write_json(
                    _ok(config_mod.test_agent(agent_id, body.get("config"))))

            if route == "launch":
                if not TMUX.available():
                    return self._write_json(
                        _err("tmux 未安装。请运行 ./install.sh，或 sudo apt install tmux", 503), 503)
                # 前端传的是 {name,text} 结构，后端只取 text 注入 tmux
                raw_tasks = body.get("tasks", []) or []
                tasks = [(t if isinstance(t, str) else str((t or {}).get("text", "") or ""))
                         for t in raw_tasks]
                cfg = LaunchConfig(
                    mode=body.get("mode", "parallel"),
                    agent=body.get("agent", "claude"),
                    count=int(body.get("count", 4)),
                    repo=body.get("repo", ".") or ".",
                    tasks=tasks,
                    roles=[str(r or "") for r in (body.get("roles", []) or [])],
                    broadcast=bool(body.get("broadcast", True)),
                    session=body.get("session") or None,
                )
                info = ORCH.launch(cfg)
                info["command"] = resolve_command(cfg.agent)
                return self._write_json(_ok(info))

            if route == "send":
                session = body.get("session", "")
                text = body.get("text", "")
                pane = body.get("pane", None)
                if not session or not text:
                    return self._write_json(_err("缺少 session 或 text", 400), 400)
                pane_i = int(pane) if pane is not None and str(pane) != "" else None
                res = ORCH.send(session, text, pane_i)
                return self._write_json(_ok(res))

            if route == "stop":
                session = body.get("session", "")
                if not session:
                    return self._write_json(_err("缺少 session", 400), 400)
                return self._write_json(_ok(ORCH.stop(session)))

            if route == "grab":
                session = body.get("session", "")
                pane = int(body.get("pane", 0))
                if not session:
                    return self._write_json(_err("缺少 session", 400), 400)
                out = TMUX.capture("%s:0.%d" % (session, pane),
                                   int(body.get("lines", 500)))
                log_dir = HERE / "logs"
                log_dir.mkdir(exist_ok=True)
                fn = log_dir / ("%s-pane%d-%s.log" % (session, pane, _stamp()))
                fn.write_text(out, encoding="utf-8")
                return self._write_json(_ok({"file": str(fn), "bytes": len(out)}))

        except TmuxError as e:
            return self._write_json(_err(str(e), 503), 503)
        except ValueError as e:
            return self._write_json(_err(str(e), 400), 400)
        except Exception as e:                      # noqa: BLE001
            return self._write_json(_err("%s: %s" % (type(e).__name__, e), 500), 500)

        return self._write_json(_err("未知接口 %s" % u.path, 404), 404)


# ================================================================= 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="Magent Console 多 Agent 编排控制台")
    ap.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--allow-external", action="store_true",
                    help="允许局域网访问（host 设为 0.0.0.0，谨慎使用）")
    args = ap.parse_args()

    host = "0.0.0.0" if args.allow_external else args.host

    if not (STATIC_DIR / "index.html").is_file():
        print("[!] 未找到前端文件：%s" % (STATIC_DIR / "index.html"))
        print("    请确认 static/ 目录完整")

    # 启动时：把「自定义 agent / 模型」注册进注册表，并注入已保存的 API Key
    try:
        config_mod.sync_registry()
    except Exception as e:                          # noqa: BLE001
        print("[!] 载入自定义 agent 配置失败：%s" % e)
    try:
        injected = config_mod.apply_auth_env()
        if injected:
            print("  已注入凭据 : %s" % ", ".join(injected))
    except Exception as e:                          # noqa: BLE001
        print("[!] 注入已保存凭据失败：%s" % e)

    srv = ThreadingHTTPServer((host, args.port), ConsoleHandler)
    url = "http://%s:%d" % ("localhost" if host == "0.0.0.0" else host, args.port)

    print("=" * 58)
    print("  Magent Console — 多 Agent 编排控制台")
    print("=" * 58)
    print("  地址     : %s" % url)
    print("  tmux     : %s" % (Tmux.version() or "未安装（编排功能不可用）"))
    print("  Python   : %s" % sys.version.split()[0])
    print("  工作目录 : %s" % HERE)
    if args.allow_external:
        print("  [!] 已开放局域网访问，请勿在不可信网络中使用")
    print("=" * 58)
    print("  Ctrl+C 退出")
    print("")

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
