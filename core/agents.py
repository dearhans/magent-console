#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/agents.py — Agent CLI 注册表与可用性探测

统一抽象各类 agent CLI 的：显示名、启动命令、安装方式、鉴权环境变量。
新增 agent 只需往 AGENTS 里加一条，前端下拉框会自动出现。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class AgentSpec:
    id: str
    name: str                       # 显示名
    cmd: str                        # 启动命令
    vendor: str                     # 厂商
    auth_env: Optional[str] = None  # 鉴权所需环境变量
    install_npm: Optional[str] = None   # npm 全局包名
    install_shell: Optional[str] = None # shell 安装命令（curl 类）
    install_hint: str = ""
    local: bool = False             # True = 本地模型，吃本地算力
    default_args: List[str] = field(default_factory=list)
    doc_url: str = ""

    def resolved_cmd(self) -> str:
        args = " ".join(self.default_args)
        return f"{self.cmd} {args}".strip()


# ---------------------------------------------------------------- 注册表
AGENTS: Dict[str, AgentSpec] = {
    "claude": AgentSpec(
        id="claude", name="Claude Code", cmd="claude", vendor="Anthropic",
        auth_env="ANTHROPIC_API_KEY",
        install_npm="@anthropic-ai/claude-code",
        install_hint="npm i -g @anthropic-ai/claude-code，或订阅 Max/Pro 后 claude login",
        doc_url="https://docs.claude.com/claude-code",
    ),
    "codex": AgentSpec(
        id="codex", name="Codex CLI", cmd="codex", vendor="OpenAI",
        auth_env="OPENAI_API_KEY",
        install_npm="@openai/codex",
        install_hint="npm i -g @openai/codex，然后 export OPENAI_API_KEY=sk-...",
        doc_url="https://github.com/openai/codex",
    ),
    "opencode": AgentSpec(
        id="opencode", name="opencode", cmd="opencode", vendor="SST / 社区",
        auth_env=None,
        install_npm="opencode-ai",
        install_hint="npm i -g opencode-ai；支持 75+ 家模型供应商，首次启动会引导登录",
        doc_url="https://opencode.ai",
    ),
    "copilot": AgentSpec(
        id="copilot", name="GitHub Copilot CLI", cmd="copilot", vendor="GitHub",
        auth_env="GITHUB_TOKEN",
        install_npm="@github/copilot",
        install_hint="npm i -g @github/copilot，需 Copilot 订阅",
        doc_url="https://docs.github.com/copilot",
    ),
    "gemini": AgentSpec(
        id="gemini", name="Gemini CLI", cmd="gemini", vendor="Google",
        auth_env="GEMINI_API_KEY",
        install_npm="@google/gemini-cli",
        install_hint="npm i -g @google/gemini-cli",
        doc_url="https://github.com/google-gemini/gemini-cli",
    ),
    "ollama": AgentSpec(
        id="ollama", name="Ollama（本地模型）", cmd="ollama run", vendor="本地",
        auth_env=None, local=True, default_args=["qwen3.5:4b"],
        install_shell="curl -fsSL https://ollama.com/install.sh | sh",
        install_hint="本地模型无需 API key，但吃 CPU/GPU/内存；无独显时速度很慢，"
                     "且 ollama run 是交互式 REPL，不适合作为 agent 编排目标，"
                     "建议仅用于轻量任务验证",
        doc_url="https://ollama.com",
    ),
}


# ---------------------------------------------------------------- 探测
def _run(cmd: List[str], timeout: float = 4.0):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except Exception:
        return -1, ""


def detect(spec: AgentSpec) -> Dict:
    """检测单个 agent 的安装与鉴权状态。"""
    binary = spec.cmd.split()[0]
    path = shutil.which(binary)
    installed = path is not None

    version = ""
    if installed:
        code, out = _run([binary, "--version"])
        if code == 0 and out:
            version = out.splitlines()[0][:60]

    authed: Optional[bool] = None
    if spec.auth_env:
        val = os.environ.get(spec.auth_env, "")
        authed = bool(val)
    elif installed:
        # 无 env 型（如 opencode、ollama）无法可靠探测登录态，如实置 None
        authed = None

    return {
        "id": spec.id,
        "name": spec.name,
        "vendor": spec.vendor,
        "cmd": spec.resolved_cmd(),
        "installed": installed,
        "path": path or "",
        "version": version,
        "auth_env": spec.auth_env,
        "authed": authed,
        "local": spec.local,
        "install_hint": spec.install_hint,
        "install_npm": spec.install_npm,
        "install_shell": spec.install_shell,
        "doc_url": spec.doc_url,
    }


def list_agents() -> List[Dict]:
    return [detect(AGENTS[k]) for k in AGENTS]


def get(agent_id: str) -> Optional[AgentSpec]:
    return AGENTS.get(agent_id)


def resolve_command(agent_id: str) -> str:
    """返回可在 tmux pane 里执行的启动命令。未知 id 则原样返回（允许自定义命令）。"""
    spec = AGENTS.get(agent_id)
    return spec.resolved_cmd() if spec else agent_id


if __name__ == "__main__":
    import json
    print(json.dumps(list_agents(), ensure_ascii=False, indent=2))
