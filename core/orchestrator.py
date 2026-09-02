#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/orchestrator.py — 两种编排模式的实现

模式一 parallel      并行同质（Best-of-N）
    同一份 requirement 广播给 N 个 agent，各自独立产出，人工挑最优。
    打开 synchronize-panes，一条指令 N 个 pane 同时收到。

模式二 heterogeneous 异质分工
    N 个 agent 各领一份不同的 requirement（写码 / 测试 / review / 查文档），
    关闭广播，逐个定向注入。

两种模式共用同一套「启动 → 注入 → 监控 → 收尾」生命周期。

注：本文件曾实现过第三种「git worktree 隔离」模式，已整体移除。
理由：该模式并非 Omarchy 官方能力，属于本项目自行追加的实验特性，
实际使用中会为每个 agent 复制一份工作副本，带来额外的磁盘与心智负担，
且与「并行同质 / 异质分工」两套模式在生命周期上高度耦合，维护成本高于收益。
移除后不留死代码，模式集合以 MODES 为准。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import agents as agents_mod
from .agents import resolve_command
from .tmuxctl import Tmux, TmuxError

MODES = ("parallel", "heterogeneous")

# pane 前台进程仍是这些名字时，说明 agent CLI 尚未接管该 pane
_SHELL_NAMES = {"bash", "zsh", "sh", "dash", "fish", "ksh", "pwsh"}

# 等 CLI 接管 pane 的最长时间（秒）；超时仍会注入，但会如实标注未就绪
PANE_READY_TIMEOUT = 25.0


@dataclass
class LaunchConfig:
    mode: str = "parallel"
    agent: str = "claude"
    count: int = 4
    repo: str = "."                      # 工作目录
    tasks: List[str] = field(default_factory=list)   # 每个 pane 的 requirement
    roles: List[str] = field(default_factory=list)   # 每个 pane 的角色名（仅作标注，异质分工模式用）
    broadcast: bool = True               # parallel 模式下是否开启输入同步
    session: Optional[str] = None        # 会话名，留空自动生成

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"未知模式 {self.mode}，可选：{', '.join(MODES)}")
        if self.count < 1 or self.count > 16:
            raise ValueError("agent 数量须在 1~16 之间")
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

        # 启动前先确认该 agent 依赖的服务已就绪（如 ollama serve），不通就自动拉起
        service = self._prepare_service(cfg.agent)

        session = cfg.session or self._session_name(cfg)
        cmd = resolve_command(cfg.agent)

        # 第 1 个 pane：主副本
        self.tmux.ensure_session(session, repo)
        self.tmux.send_keys(f"{session}:0.0", cmd, enter=True)

        # 其余 pane：两种模式下都在同一份工作目录里启动
        for i in range(1, cfg.count):
            self.tmux.split(f"{session}:0", repo)
            self.tmux.send_keys(f"{session}:0.{i}", cmd, enter=True)
            self.tmux.select_layout(session, "tiled")

        self.tmux.select_layout(session, "tiled")

        # 广播开关：只有并行同质模式才允许同步输入
        if cfg.mode == "parallel":
            self.tmux.set_sync(session, cfg.broadcast)
        else:
            self.tmux.set_sync(session, False)

        # 等 CLI 真正接管每个 pane，避免任务文本落进 bash 变成 command not found
        ready = self._wait_panes_ready(session, cfg.count)
        if not any(ready):
            raise RuntimeError(
                "没有任何 pane 成功启动 %s（%s）。常见原因：该命令不存在，"
                "或依赖服务不可达导致 CLI 直接退出。可在设置里点「测试」确认。"
                % (cfg.agent, cmd))
        self._inject_tasks(session, cfg, ready)

        return {
            "session": session,
            "mode": cfg.mode,
            "agent": cfg.agent,
            "count": cfg.count,
            "repo": repo,
            "sync": self.tmux.get_sync(session),
            "service": service,
            "ready": ready,
            "panes": [
                {"index": i,
                 "cwd": repo,
                 "task": cfg.tasks[i] if i < len(cfg.tasks) else (cfg.tasks[0] if cfg.mode == "parallel" and cfg.tasks else ""),
                 "role": cfg.roles[i] if i < len(cfg.roles) else "",
                 "ready": ready[i] if i < len(ready) else False}
                for i in range(cfg.count)
            ],
        }

    # ------------------------------------------------------------ 依赖服务
    def _prepare_service(self, agent_id: str) -> Dict:
        """启动前确认 agent 依赖的服务可达，必要时自动后台拉起。"""
        spec = agents_mod.AGENTS.get(agent_id)
        if spec is None:
            return {"required": False, "ok": True, "detail": "未注册该 agent，跳过服务检查"}
        url = spec.service_url()
        if not url:
            return {"required": False, "ok": True, "detail": "该 agent 无依赖服务"}
        ok, detail = agents_mod.ensure_service(spec, auto_start=True)
        if not ok:
            raise RuntimeError(f"{spec.name} 依赖的服务未就绪：{detail}")
        return {"required": True, "ok": True, "detail": detail, "url": url}

    # ------------------------------------------------------------ 就绪等待
    def _wait_panes_ready(self, session: str, count: int,
                          timeout: float = PANE_READY_TIMEOUT) -> List[bool]:
        """等每个 pane 的前台进程从启动 shell 切成 agent CLI，返回每个 pane 是否就绪。

        只按进程名判断，不做输出匹配，避免把 agent 的 banner 误当成就绪信号。
        """
        ready = [False] * count
        deadline = time.time() + timeout
        while time.time() < deadline:
            for i in range(count):
                if ready[i]:
                    continue
                proc = self.tmux.pane_command(session, i)
                if proc and proc.lower() not in _SHELL_NAMES:
                    ready[i] = True
            if all(ready):
                break
            time.sleep(0.5)
        return ready

    def _session_name(self, cfg: LaunchConfig) -> str:
        base = os.path.basename(os.path.abspath(os.path.expanduser(cfg.repo))) or "work"
        base = re.sub(r"[^A-Za-z0-9_-]", "-", base)
        return f"magent-{cfg.mode[:4]}-{base}"[:40]

    def _inject_tasks(self, session: str, cfg: LaunchConfig,
                      ready: Optional[List[bool]] = None) -> None:
        if not cfg.tasks:
            return
        live = ready if ready is not None else [True] * cfg.count

        if cfg.mode == "parallel":
            # 同一份 requirement 广播：sync 已开则只发给一个 pane，tmux 自动同步；
            # 未开 sync 时逐个发同样的内容
            text = cfg.tasks[0] if cfg.tasks else ""
            if not text.strip():
                return
            if self.tmux.get_sync(session):
                src = next((i for i in range(cfg.count)
                            if i < len(live) and live[i]), 0)
                self.tmux.send_keys(f"{session}:0.{src}", text)
            else:
                for i in range(cfg.count):
                    if i < len(live) and not live[i]:
                        continue
                    self.tmux.send_keys(f"{session}:0.{i}", text)
        else:
            # 异质分工：第 i 个 pane 领第 i 份任务，逐条定向注入
            for i, task in enumerate(cfg.tasks):
                if i >= cfg.count:
                    break
                if not task.strip():
                    continue
                if i < len(live) and not live[i]:
                    continue
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

    def stop(self, session: str) -> Dict:
        self.tmux.kill_session(session)
        return {"session": session, "stopped": True}

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

