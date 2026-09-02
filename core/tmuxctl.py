#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/tmuxctl.py — tmux 控制封装

把编排逻辑与 tmux 命令行细节隔离：上层只关心「开几个 pane、往哪个 pane 发什么」，
不关心 -t 语法和索引规则。

索引约定（务必与 tmux 默认一致）：
    window 从 0 开始，pane 从 0 开始 → 第 N 个 pane 写作  "{session}:0.{N}"
"""

from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional, Tuple


class TmuxError(RuntimeError):
    pass


class Tmux:
    """tmux 命令封装。所有方法失败时抛 TmuxError，由上层转成 HTTP 错误。"""

    def __init__(self, socket: Optional[str] = None):
        self.socket = socket
        self._base = ["tmux"]
        if socket:
            self._base += ["-L", socket]

    # ------------------------------------------------------------ 底层
    def _run(self, args: List[str], timeout: float = 6.0) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(
                self._base + args,
                capture_output=True, text=True, timeout=timeout,
            )
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except FileNotFoundError:
            raise TmuxError("tmux 未安装。请先运行 install.sh，或执行 sudo apt install tmux")
        except subprocess.TimeoutExpired:
            raise TmuxError(f"tmux 命令超时：{' '.join(args)}")

    # ------------------------------------------------------------ 环境
    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    @classmethod
    def version(cls) -> str:
        if not cls.available():
            return ""
        try:
            p = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=4)
            return (p.stdout or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------ 会话
    def session_exists(self, name: str) -> bool:
        code, _, _ = self._run(["has-session", "-t", name])
        return code == 0

    def new_session(self, name: str, cwd: str, command: str = "") -> None:
        args = ["new-session", "-d", "-s", name, "-c", cwd]
        if command:
            args.append(command)
        code, _, err = self._run(args)
        if code != 0:
            raise TmuxError(f"创建会话 {name} 失败：{err}")

    def kill_session(self, name: str) -> None:
        if self.session_exists(name):
            self._run(["kill-session", "-t", name])

    def list_sessions(self) -> List[Dict[str, str]]:
        fmt = "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_created}"
        code, out, _ = self._run(["list-sessions", "-F", fmt])
        if code != 0:
            return []
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({
                    "name": parts[0],
                    "windows": parts[1],
                    "attached": parts[2] != "0",
                    "created": parts[3] if len(parts) > 3 else "",
                })
        return rows

    # ------------------------------------------------------------ 面板
    def split(self, target: str, cwd: str, vertical: bool = True,
              percent: Optional[int] = None, command: str = "") -> None:
        args = ["split-window", "-t", target]
        args += ["-v"] if vertical else ["-h"]
        args += ["-c", cwd]
        if percent:
            args += ["-p", str(percent)]
        if command:
            args.append(command)
        code, _, err = self._run(args)
        if code != 0:
            raise TmuxError(f"分割面板失败：{err}")

    def select_layout(self, session: str, layout: str = "tiled") -> None:
        self._run(["select-layout", "-t", f"{session}:0", layout])

    def set_sync(self, session: str, on: bool) -> None:
        self._run(["set-window-option", "-t", f"{session}:0",
                   "synchronize-panes", "on" if on else "off"])

    def get_sync(self, session: str) -> bool:
        code, out, _ = self._run(
            ["show-window-options", "-t", f"{session}:0", "synchronize-panes"])
        return code == 0 and out.endswith("on")

    def list_panes(self, session: str) -> List[Dict[str, str]]:
        fmt = ("#{pane_index}\t#{pane_pid}\t#{pane_current_path}\t"
               "#{pane_dead}\t#{pane_width}\t#{pane_height}")
        code, out, _ = self._run(["list-panes", "-t", f"{session}:0", "-F", fmt])
        if code != 0:
            return []
        panes = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 6:
                panes.append({
                    "index": p[0], "pid": p[1], "cwd": p[2],
                    "dead": p[3] == "1", "width": p[4], "height": p[5],
                })
        return panes

    def pane_command(self, session: str, index: int) -> str:
        """返回指定 pane 当前前台进程名，用于判断 agent CLI 是否已接管该 pane。"""
        code, out, _ = self._run(
            ["list-panes", "-t", f"{session}:0", "-F",
             "#{pane_index}\t#{pane_current_command}"])
        if code != 0:
            return ""
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) >= 2 and p[0] == str(index):
                return (p[1] or "").strip()
        return ""

    def send_keys(self, target: str, text: str, enter: bool = True) -> None:
        """-l 表示按字面量发送，不做按键名解析，避免特殊字符被吃掉。"""
        self._run(["send-keys", "-t", target, "-l", text])
        if enter:
            self._run(["send-keys", "-t", target, "Enter"])

    def capture(self, target: str, lines: int = 60) -> str:
        code, out, _ = self._run(
            ["capture-pane", "-t", target, "-p", "-S", f"-{lines}"])
        return out if code == 0 else ""

    def resize_pane(self, target: str, width: Optional[int] = None,
                    height: Optional[int] = None) -> None:
        args = ["resize-pane", "-t", target]
        if width:
            args += ["-x", str(width)]
        if height:
            args += ["-y", str(height)]
        self._run(args)

    # ------------------------------------------------------------ 便捷
    def ensure_session(self, name: str, cwd: str, command: str = "") -> None:
        if self.session_exists(name):
            self.kill_session(name)
        self.new_session(name, cwd, command)

    def pane_target(self, session: str, index: int) -> str:
        return f"{session}:0.{index}"
