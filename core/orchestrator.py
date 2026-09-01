#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/orchestrator.py — 三种编排模式的实现

模式一 parallel      并行同质（Best-of-N）
    同一份 requirement 广播给 N 个 agent，各自独立产出，人工挑最优。
    打开 synchronize-panes，一条指令 N 个 pane 同时收到。

模式二 heterogeneous 异质分工
    N 个 agent 各领一份不同的 requirement（写码 / 测试 / review / 查文档），
    关闭广播，逐个定向注入。

模式三 worktree      Git worktree 隔离（生产级并发）
    每个 agent 分配独立 git worktree 与分支，物理上杜绝写冲突。
    这是唯一能安全让多个 agent 同时改同一个仓库的模式。

三种模式共用同一套「启动 → 注入 → 监控 → 收尾」生命周期。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .agents import resolve_command, get
from .tmuxctl import Tmux, TmuxError

MODES = ("parallel", "heterogeneous", "worktree")


@dataclass
class LaunchConfig:
    mode: str = "parallel"
    agent: str = "claude"
    count: int = 4
    repo: str = "."                      # 工作目录（worktree 模式下须是 git 仓库）
    tasks: List[str] = field(default_factory=list)   # 每个 pane 的 requirement
    broadcast: bool = True               # parallel 模式下是否开启输入同步
    session: Optional[str] = None        # 会话名，留空自动生成
    branch_prefix: str = "ai"

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"未知模式 {self.mode}，可选：{', '.join(MODES)}")
        if self.count < 1 or self.count > 16:
            raise ValueError("agent 数量须在 1~16 之间")
        if self.mode == "worktree" and not os.path.isdir(os.path.join(self.repo, ".git")):
            raise ValueError(f"worktree 模式要求 {self.repo} 是 git 仓库（未找到 .git）")
        if self.mode == "heterogeneous" and len(self.tasks) > self.count:
            raise ValueError("任务条数不能多于 agent 数量")


@dataclass
class PaneState:
    index: int
    pid: str = ""
    cwd: str = ""
    dead: bool = False
    output: str = ""
    branch: str = ""
    task: str = ""
    alive: bool = True


class Orchestrator:
    def __init__(self, tmux: Optional[Tmux] = None):
        self.tmux = tmux or Tmux()

    # ------------------------------------------------------------ 启动
    def launch(self, cfg: LaunchConfig) -> Dict:
        cfg.validate()
        repo = os.path.abspath(os.path.expanduser(cfg.repo))
        os.makedirs(repo, exist_ok=True) if not os.path.isdir(repo) else None

        session = cfg.session or self._session_name(cfg)
        cmd = resolve_command(cfg.agent)
        spec = get(cfg.agent)

        if spec and spec.local and cfg.mode == "worktree":
            raise ValueError("本地模型（Ollama）不适合 worktree 并发模式："
                             "每个实例都要独立加载权重，内存会爆")

        worktrees: List[str] = []

        # 第 1 个 pane：主副本
        self.tmux.ensure_session(session, repo)
        self.tmux.send_keys(f"{session}:0.0", cmd, enter=True)

        # 其余 pane
        for i in range(1, cfg.count):
            if cfg.mode == "worktree":
                wt_path = self._make_worktree(repo, cfg.branch_prefix, i)
                worktrees.append(wt_path)
                cwd = wt_path
            else:
                cwd = repo
            self.tmux.split(f"{session}:0", cwd)
            self.tmux.send_keys(f"{session}:0.{i}", cmd, enter=True)
            self.tmux.select_layout(session, "tiled")

        self.tmux.select_layout(session, "tiled")

        # 广播开关
        if cfg.mode == "parallel":
            self.tmux.set_sync(session, cfg.broadcast)
        else:
            self.tmux.set_sync(session, False)

        # 注入任务（等 agent CLI 启动，给一点缓冲）
        time.sleep(1.2)
        self._inject_tasks(session, cfg)

        return {
            "session": session,
            "mode": cfg.mode,
            "agent": cfg.agent,
            "count": cfg.count,
            "repo": repo,
            "worktrees": worktrees,
            "sync": self.tmux.get_sync(session),
            "panes": [
                {"index": i,
                 "cwd": (worktrees[i - 1] if cfg.mode == "worktree" and i > 0 else repo),
                 "task": cfg.tasks[i] if i < len(cfg.tasks) else (
                     cfg.tasks[0] if cfg.mode == "parallel" and cfg.tasks else "")}
                for i in range(cfg.count)
            ],
        }

    def _session_name(self, cfg: LaunchConfig) -> str:
        base = os.path.basename(os.path.abspath(os.path.expanduser(cfg.repo))) or "work"
        base = re.sub(r"[^A-Za-z0-9_-]", "-", base)
        return f"magent-{cfg.mode[:4]}-{base}"[:40]

    def _make_worktree(self, repo: str, prefix: str, idx: int) -> str:
        stamp = time.strftime("%m%d")
        branch = f"{prefix}/wt{idx}-{stamp}"
        path = os.path.join("/tmp", f"{os.path.basename(repo)}-wt{idx}-{stamp}")
        # 已存在则先清理，保证幂等
        subprocess.run(["git", "-C", repo, "worktree", "remove", "-f", path],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", repo, "branch", "-D", branch],
                       capture_output=True, text=True)
        p = subprocess.run(
            ["git", "-C", repo, "worktree", "add", "-f", path, "-b", branch],
            capture_output=True, text=True)
        if p.returncode != 0:
            raise TmuxError(f"创建 git worktree 失败：{p.stderr.strip()}")
        return path

    def _inject_tasks(self, session: str, cfg: LaunchConfig) -> None:
        if not cfg.tasks:
            return
        if cfg.mode == "parallel":
            # 同一份 requirement 广播：只需发给 pane 0，sync 已开则自动同步；
            # 未开 sync 时逐个发同样的内容
            text = cfg.tasks[0]
            if self.tmux.get_sync(session):
                self.tmux.send_keys(f"{session}:0.0", text)
            else:
                for i in range(cfg.count):
                    self.tmux.send_keys(f"{session}:0.{i}", text)
        else:
            # 异质 / worktree：第 i 个 pane 领第 i 份任务
            for i, task in enumerate(cfg.tasks):
                if i >= cfg.count or not task.strip():
                    break
                self.tmux.send_keys(f"{session}:0.{i}", task)

    # ------------------------------------------------------------ 运行时
    def status(self, session: str, lines: int = 40) -> Dict:
        if not self.tmux.session_exists(session):
            raise TmuxError(f"会话 {session} 不存在")

        panes: List[PaneState] = []
        for p in self.tmux.list_panes(session):
            idx = int(p["index"])
            target = f"{session}:0.{idx}"
            st = PaneState(
                index=idx, pid=p["pid"], cwd=p["cwd"], dead=p["dead"],
                output=self.tmux.capture(target, lines),
                branch=self._branch_of(p["cwd"]),
            )
            st.alive = not p["dead"] and self._pid_alive(p["pid"])
            panes.append(st)

        return {
            "session": session,
            "sync": self.tmux.get_sync(session),
            "panes": [
                {"index": s.index, "pid": s.pid, "cwd": s.cwd, "dead": s.dead,
                 "alive": s.alive, "branch": s.branch,
                 "output": s.output, "bytes": len(s.output)}
                for s in panes
            ],
            "count": len(panes),
        }

    def send(self, session: str, text: str, pane: Optional[int] = None) -> Dict:
        if pane is None:
            # 广播：优先用 tmux 自带的 sync 机制
            was_sync = self.tmux.get_sync(session)
            if not was_sync:
                self.tmux.set_sync(session, True)
            self.tmux.send_keys(f"{session}:0.0", text)
            if not was_sync:
                # 立即恢复，避免影响后续定向操作
                self.tmux.set_sync(session, False)
            return {"sent": "broadcast", "pane": None, "sync_restored": not was_sync}

        self.tmux.send_keys(f"{session}:0.{pane}", text)
        return {"sent": "pane", "pane": pane}

    def stop(self, session: str, cleanup_worktree: bool = True) -> Dict:
        panes = self.tmux.list_panes(session)
        dirs = [p["cwd"] for p in panes]
        self.tmux.kill_session(session)

        removed = []
        if cleanup_worktree:
            for d in dirs:
                if "/tmp/" in d and "-wt" in d:
                    repo = self._main_repo_of_worktree(d)
                    if repo:
                        subprocess.run(["git", "-C", repo, "worktree", "remove", "-f", d],
                                       capture_output=True, text=True)
                        removed.append(d)
        return {"session": session, "stopped": True, "worktrees_removed": removed}

    # ------------------------------------------------------------ 差异汇总
    def diff(self, session: str) -> Dict:
        """汇总各 pane 工作目录的 git 改动，用于「谁改了什么」的对比视图。"""
        result = {"session": session, "panes": []}
        if not self.tmux.session_exists(session):
            raise TmuxError(f"会话 {session} 不存在")
        for p in self.tmux.list_panes(session):
            cwd = p["cwd"]
            entry = {"index": int(p["index"]), "cwd": cwd, "branch": "",
                     "stat": "", "files": [], "error": ""}
            if os.path.isdir(cwd):
                entry["branch"] = self._branch_of(cwd)
                r = subprocess.run(["git", "-C", cwd, "--no-pager",
                                    "diff", "--stat", "HEAD"],
                                   capture_output=True, text=True)
                entry["stat"] = (r.stdout or "").strip()
                if r.returncode != 0 and "not a git repo" in (r.stderr or "").lower():
                    entry["error"] = "非 git 仓库"
                r2 = subprocess.run(["git", "-C", cwd, "--no-pager",
                                     "status", "--porcelain"],
                                    capture_output=True, text=True)
                entry["files"] = [l.strip() for l in (r2.stdout or "").splitlines() if l.strip()]
            result["panes"].append(entry)
        return result

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _pid_alive(pid: str) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    @staticmethod
    def _branch_of(cwd: str) -> str:
        try:
            r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                               capture_output=True, text=True, timeout=4)
            return (r.stdout or "").strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    @staticmethod
    def _main_repo_of_worktree(wt_path: str) -> Optional[str]:
        try:
            r = subprocess.run(["git", "-C", wt_path, "rev-parse",
                                "--git-common-dir"],
                               capture_output=True, text=True, timeout=4)
            if r.returncode != 0:
                return None
            common = (r.stdout or "").strip()
            if not os.path.isabs(common):
                common = os.path.join(wt_path, common)
            return os.path.dirname(os.path.abspath(common))
        except Exception:
            return None
