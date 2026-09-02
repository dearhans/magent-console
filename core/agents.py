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
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


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
    # --- 以下字段服务于「自定义 agent / 自定义模型」 ---
    model: str = ""                 # 模型名/标识，用于展示与连通性探测
    probe_url: str = ""             # 自定义探测端点（留空则用内置映射）
    custom: bool = False            # True = 用户自定义，非内置
    # --- 本地模型 / 自建服务的地址配置（如 ollama） ---
    api_url: str = ""               # 服务地址，例 http://127.0.0.1:11434
    api_url_env: str = ""           # 注入服务地址所用的环境变量，例 OLLAMA_HOST
    probe_path: str = ""            # 健康检查路径，与 api_url 拼接，例 /api/tags
    service_start: str = ""         # 服务未起时的后台启动命令，例 ollama serve

    def resolved_cmd(self) -> str:
        args = " ".join(self.default_args)
        base = f"{self.cmd} {args}".strip()
        if self.api_url and self.api_url_env:
            # 环境变量前缀形式：bash 会把它传给本次启动的 CLI 进程
            return f"{self.api_url_env}={self.api_url} {base}"
        return base

    def service_url(self) -> str:
        """健康检查端点：优先用显式 probe_url，否则 api_url + probe_path。"""
        if self.probe_url:
            return self.probe_url
        if self.api_url and self.probe_path:
            return self.api_url.rstrip("/") + self.probe_path
        return ""


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
        model="qwen3.5:4b",
        api_url="http://127.0.0.1:11434", api_url_env="OLLAMA_HOST",
        probe_path="/api/tags", service_start="ollama serve",
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


def detect(spec: AgentSpec, key: Optional[str] = None) -> Dict:
    """检测单个 agent 的安装与鉴权状态。

    key 显式传入密钥时以它为准；为 None 时才回落到读取 auth_env 环境变量。
    """
    binary = spec.cmd.split()[0]
    path = shutil.which(binary)
    installed = path is not None

    version = ""
    if installed:
        code, out = _run([binary, "--version"])
        if code == 0 and out:
            version = out.splitlines()[0][:60]

    authed: Optional[bool] = None
    if key is not None:
        # 显式密钥优先（来自 ~/.magent-console/config.json 或 env:XXX 引用）
        authed = bool(key)
    elif spec.auth_env:
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
        "model": spec.model,
        "custom": spec.custom,
        "probe_url": spec.probe_url,
        "api_url": spec.api_url,
        "api_url_env": spec.api_url_env,
        "service_start": spec.service_start,
        "service_url": spec.service_url(),
    }


def list_agents() -> List[Dict]:
    return [detect(AGENTS[k]) for k in AGENTS]


# ---------------------------------------------------------------- 内置快照与用户覆盖
_BUILTIN_SNAPSHOT: Dict[str, AgentSpec] = {}


def _snapshot_builtin() -> None:
    """保存内置定义快照，供每次应用覆盖前复位，避免旧覆盖残留。"""
    if _BUILTIN_SNAPSHOT:
        return
    for k, v in AGENTS.items():
        _BUILTIN_SNAPSHOT[k] = AgentSpec(
            **{f: getattr(v, f) for f in v.__dataclass_fields__})


_snapshot_builtin()


def apply_overrides(overrides: Optional[Dict[str, Any]]) -> None:
    """把用户在设置里填的服务地址 / 模型名覆盖到内置 agent 上。

    每次都先从内置快照复位再覆盖，因此删掉某条覆盖后能恢复默认值。
    """
    from copy import deepcopy
    for k, snap in _BUILTIN_SNAPSHOT.items():
        AGENTS[k] = deepcopy(snap)
    for aid, ov in (overrides or {}).items():
        spec = AGENTS.get(str(aid))
        if spec is None or not isinstance(ov, dict):
            continue
        url = str(ov.get("api_url") or "").strip()
        if url:
            spec.api_url = url
        model = str(ov.get("model") or "").strip()
        if model:
            spec.model = model
            if spec.default_args:
                spec.default_args = [model] + list(spec.default_args[1:])
            else:
                spec.default_args = [model]
        args = ov.get("args")
        if args:
            spec.default_args = [
                str(a) for a in (args if isinstance(args, list) else str(args).split())
            ]


# ---------------------------------------------------------------- 依赖服务健康检查
def probe_service(spec: AgentSpec, timeout: float = 3.0) -> Tuple[bool, str]:
    """探测 agent 依赖的服务是否可达。无依赖服务时视为通过。"""
    url = spec.service_url()
    if not url:
        return True, "该 agent 无依赖服务"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"服务可达（HTTP {resp.status}）"
    except Exception as e:                                     # noqa: BLE001
        return False, f"{url} 不可达（{type(e).__name__}）"


def _is_local_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))


def ensure_service(spec: AgentSpec, auto_start: bool = True,
                   wait: float = 15.0) -> Tuple[bool, str]:
    """确保 agent 依赖的服务已就绪：先探测，不通则后台拉起并等待就绪。

    只对本机地址尝试自动拉起；指向他机（如 Windows 宿主）的地址只给明确提示。
    """
    url = spec.service_url()
    if not url:
        return True, "该 agent 无依赖服务"
    ok, detail = probe_service(spec)
    if ok:
        return True, detail

    target = spec.api_url or url
    if auto_start and spec.service_start and _is_local_url(target):
        binary = spec.service_start.split()[0]
        if shutil.which(binary):
            try:
                subprocess.Popen(spec.service_start, shell=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception as e:                             # noqa: BLE001
                return False, f"{detail}；后台拉起 {spec.service_start} 失败：{e}"
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(0.6)
                ok, detail = probe_service(spec)
                if ok:
                    return True, f"已自动拉起 {spec.service_start}，{detail}"
            return False, (f"已执行 {spec.service_start}，但 {wait:.0f}s 内服务仍未就绪"
                           f"（{detail}）")
        return False, (f"{detail}；本机找不到 {binary}，无法自动拉起，请手动启动服务后重试")

    if not _is_local_url(target):
        return False, (f"{detail}；该地址不在本机，无法自动拉起。若服务跑在 Windows 上，"
                       f"请先启动它，并确认其监听地址对 WSL 可达"
                       f"（Windows 版 ollama 需设 OLLAMA_HOST=0.0.0.0）")
    return False, detail


# ---------------------------------------------------------------- 自定义注册
_CUSTOM: Dict[str, AgentSpec] = {}


def reset_custom() -> None:
    """清空已注册的自定义 agent（保持内置 AGENTS 不变）。"""
    _CUSTOM.clear()


def register_custom(spec: AgentSpec) -> None:
    """注册一个自定义 agent/模型，使其在 AGENTS 视图中可见。"""
    if not spec or not spec.id:
        return
    spec.custom = True
    _CUSTOM[spec.id] = spec


def registered_agents() -> Dict[str, AgentSpec]:
    """内置 + 自定义 的合并视图（自定义可覆盖同名内置项）。"""
    merged = dict(AGENTS)
    merged.update(_CUSTOM)
    return merged


def get(agent_id: str) -> Optional[AgentSpec]:
    return registered_agents().get(agent_id)


def resolve_command(agent_id: str) -> str:
    """返回可在 tmux pane 里执行的启动命令。未知 id 则原样返回（允许自定义命令）。"""
    spec = registered_agents().get(agent_id)
    return spec.resolved_cmd() if spec else agent_id


if __name__ == "__main__":
    import json
    print(json.dumps(list_agents(), ensure_ascii=False, indent=2))
