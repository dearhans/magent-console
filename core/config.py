#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/config.py — 配置中心（凭据与自定义模型 / 角色库）

职责划分（刻意把「秘密」和「可共享配置」分开存）：

    凭据类配置  →  用户家目录 ~/.magent-console/config.json
                   含 API Key、自定义模型的密钥等，文件权限 600，
                   且已在 .gitignore 中排除，永不进 git。
    角色库      →  项目内 config/roles.json
                   角色 = 名称 + 该角色的 requirement 模板，团队可共享，可进 git。

支持环境变量 MAGENT_CONFIG 覆盖凭据文件路径（可指向目录，也可直接指向 json 文件）。

API Key 两种写法：
    1) 直接填值      →  落盘到家目录凭据文件（已 chmod 600）
    2) env:XXX       →  引用环境变量，敏感值不落盘

零第三方依赖，纯标准库，Python 3.9+ 可跑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import agents as agents_mod
from .agents import AgentSpec

# 项目根目录（core/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 角色库：项目内，可共享，可进 git
ROLES_PATH = PROJECT_ROOT / "config" / "roles.json"

# 凭据文件：家目录，绝不进 git
CONFIG_DIR = Path(os.path.expanduser("~")) / ".magent-console"
CRED_PATH = CONFIG_DIR / "config.json"

# 前端回传该占位符表示「保留原值，不修改」
KEEP = "__KEEP__"

CONFIG_ENV_VAR = "MAGENT_CONFIG"

VERSION = 1


# ---------------------------------------------------------------- 路径
def _resolve_cred_path() -> Path:
    """MAGENT_CONFIG 可指向目录，也可直接指向 json 文件。"""
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if not override:
        return CRED_PATH
    p = Path(os.path.expanduser(override))
    if p.suffix.lower() == ".json":
        return p
    return p / "config.json"


def cred_path() -> Path:
    return _resolve_cred_path()


def config_env_active() -> bool:
    return bool(os.environ.get(CONFIG_ENV_VAR, "").strip())


# ---------------------------------------------------------------- 默认角色库
DEFAULT_ROLES: List[Dict[str, str]] = [
    {"id": "impl", "name": "实现",
     "requirement": "实现 <功能>：\n- 按现有代码风格，不引入新依赖\n- 附带最小可运行示例\n- 完成后自测并报告改动文件"},
    {"id": "test", "name": "测试",
     "requirement": "为 <功能> 编写测试：\n- 覆盖正常路径 + 边界 + 异常\n- 不修改被测源码\n- 报告覆盖率与失败用例"},
    {"id": "review", "name": "Review",
     "requirement": "审查本次改动：\n- 指出 bug / 安全隐患 / 性能问题\n- 按严重程度排序\n- 不直接改代码，只给清单"},
    {"id": "docs", "name": "文档",
     "requirement": "为 <功能> 补充文档：\n- 用法示例 + 参数说明\n- 与现有 README 风格一致\n- 只改文档，不改代码"},
    {"id": "refactor", "name": "重构",
     "requirement": "重构 <模块>：\n- 保持外部接口不变\n- 消除重复逻辑\n- 每步提交可独立回滚"},
    {"id": "perf", "name": "优化",
     "requirement": "分析 <模块> 性能瓶颈：\n- 先测量再优化，给出前后数据\n- 只做有数据支撑的改动\n- 报告收益与风险"},
    {"id": "research", "name": "检索",
     "requirement": "调研 <主题>：\n- 给出结论 + 来源链接\n- 标注结论的确定性\n- 不写代码，只出调研报告"},
    {"id": "verify", "name": "验证",
     "requirement": "验证实现是否满足 requirement：\n- 逐条比对验收标准\n- 列出不符合项\n- 给出最小修复建议"},
]


# ---------------------------------------------------------------- 读写底层
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_json_secret(path: Path, data: Dict[str, Any]) -> None:
    """写凭据文件：目录 700、文件 600，尽最大努力收紧权限（Windows 上无 chmod 语义则跳过）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
    except Exception:
        # Windows / 某些文件系统不支持 POSIX 权限位，忽略
        pass


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 凭据配置
def load_credentials() -> Dict[str, Any]:
    data = _read_json(cred_path())
    data.setdefault("version", VERSION)
    data.setdefault("agent_auth", {})
    data.setdefault("custom_agents", [])
    data.setdefault("agent_overrides", {})
    if not isinstance(data.get("agent_auth"), dict):
        data["agent_auth"] = {}
    if not isinstance(data.get("custom_agents"), list):
        data["custom_agents"] = []
    if not isinstance(data.get("agent_overrides"), dict):
        data["agent_overrides"] = {}
    return data


def save_credentials(data: Dict[str, Any]) -> None:
    data["version"] = VERSION
    _write_json_secret(cred_path(), data)


def get_agent_auth() -> Dict[str, str]:
    return dict(load_credentials().get("agent_auth", {}))


def get_custom_agents() -> List[Dict[str, Any]]:
    return list(load_credentials().get("custom_agents", []))


def get_agent_overrides() -> Dict[str, Dict[str, Any]]:
    """内置 agent 的用户覆盖配置：{agent_id: {api_url, model, args}}。

    用于给 ollama 等本地模型填服务地址与模型名，也可覆盖任意内置 agent 的参数。
    """
    raw = load_credentials().get("agent_overrides", {})
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k)] = v
    return out


# ---------------------------------------------------------------- 角色库
def load_roles() -> List[Dict[str, str]]:
    """读取项目内角色库；文件不存在时用默认角色库初始化一份。"""
    data = _read_json(ROLES_PATH)
    roles = data.get("roles")
    if isinstance(roles, list) and roles:
        return [normalize_role(r, i) for i, r in enumerate(roles) if isinstance(r, dict)]
    # 首次使用：落盘默认角色库
    save_roles(DEFAULT_ROLES)
    return [dict(r) for r in DEFAULT_ROLES]


def save_roles(roles: List[Dict[str, Any]]) -> None:
    clean = [normalize_role(r, i) for i, r in enumerate(roles or []) if isinstance(r, dict)]
    _write_json(ROLES_PATH, {"version": VERSION, "roles": clean})


def normalize_role(r: Dict[str, Any], idx: int = 0) -> Dict[str, str]:
    name = str(r.get("name") or "").strip() or ("角色 " + str(idx + 1))
    rid = str(r.get("id") or "").strip() or _slug(name) or ("role-" + str(idx + 1))
    return {
        "id": rid,
        "name": name,
        "requirement": str(r.get("requirement") or r.get("text") or ""),
    }


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(s)).strip("-").lower()


# ---------------------------------------------------------------- 密钥解析
def resolve_key(agent_id: str, auth_env: Optional[str],
                stored: Optional[str]) -> Tuple[str, str]:
    """解析某 agent 的密钥。

    返回 (value, source)，source ∈ {"env", "file", "none"}。
    stored 可以是字面值，也可以是 "env:VAR" 形式的引用。
    未显式配置时，回落到直接读取 auth_env 环境变量。
    """
    if stored:
        s = str(stored).strip()
        if s.lower().startswith("env:"):
            var = s[4:].strip()
            if var:
                val = os.environ.get(var, "")
                return val, ("env" if val else "none")
            return "", "none"
        return s, "file"
    if auth_env:
        val = os.environ.get(auth_env, "")
        return val, ("env" if val else "none")
    return "", "none"


def mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return v[:3] + "…" + v[-4:]


def apply_auth_env() -> Dict[str, str]:
    """把家目录配置里「字面值」形式的密钥注入当前进程环境变量，供 agent CLI 启动继承。

    返回 {agent_id: source} 便于排查。env: 引用形式不做注入（依赖服务启动环境本身已有该变量）。
    """
    applied: Dict[str, str] = {}
    creds = load_credentials()
    auth = creds.get("agent_auth", {})
    for spec in iter_agent_specs():
        stored = auth.get(spec.id)
        if not stored or not spec.auth_env:
            continue
        s = str(stored).strip()
        if s.lower().startswith("env:"):
            applied[spec.id] = "env-ref"
            continue
        os.environ[spec.auth_env] = s
        applied[spec.id] = "file"
    return applied


# ---------------------------------------------------------------- 自定义 agent 注册表
def _spec_from_dict(d: Dict[str, Any]) -> Optional[AgentSpec]:
    if not isinstance(d, dict):
        return None
    aid = str(d.get("id") or "").strip()
    cmd = str(d.get("cmd") or "").strip()
    if not aid or not cmd:
        return None
    args = d.get("args") or []
    if isinstance(args, str):
        args = args.split()
    return AgentSpec(
        id=aid,
        name=str(d.get("name") or aid).strip() or aid,
        cmd=cmd,
        vendor=str(d.get("vendor") or "自定义").strip() or "自定义",
        auth_env=(str(d["auth_env"]).strip() if d.get("auth_env") else None),
        install_hint=str(d.get("install_hint") or ""),
        local=bool(d.get("local")),
        default_args=[str(a) for a in args],
        doc_url=str(d.get("doc_url") or ""),
        model=str(d.get("model") or ""),
        probe_url=str(d.get("probe_url") or ""),
        api_url=str(d.get("api_url") or "").strip(),
        api_url_env=(str(d["api_url_env"]).strip() if d.get("api_url_env") else None),
        probe_path=str(d.get("probe_path") or ""),
        service_start=str(d.get("service_start") or ""),
        custom=True,
    )


def sync_registry() -> List[str]:
    """把自定义 agent 同步进 core.agents 注册表，返回自定义 agent 的 id 列表。

    先把用户在设置里填的覆盖（服务地址 / 模型名）应用到内置 agent，
    再注册自定义 agent。与内置 id 冲突时自动加 -custom 后缀，不覆盖内置定义。
    """
    agents_mod.apply_overrides(get_agent_overrides())
    custom = get_custom_agents()
    specs: List[AgentSpec] = []
    seen = set()
    for d in custom:
        spec = _spec_from_dict(d)
        if spec is None:
            continue
        if spec.id in agents_mod.AGENTS and not agents_mod.AGENTS[spec.id].custom:
            # 内置 agent 只能改鉴权，不能整体覆盖
            continue
        if spec.id in seen:
            continue
        seen.add(spec.id)
        specs.append(spec)
    agents_mod.reset_custom()
    for spec in specs:
        agents_mod.register_custom(spec)
    return [s.id for s in specs]


def iter_agent_specs() -> List[AgentSpec]:
    """内置 + 自定义，按注册表顺序（自定义同名项覆盖内置）。"""
    return list(agents_mod.registered_agents().values())


# ---------------------------------------------------------------- 组装视图
def agent_view(spec: AgentSpec, stored: Optional[str]) -> Dict[str, Any]:
    """单个 agent 的完整视图：安装状态 + 鉴权状态（密钥只回传掩码）。"""
    value, source = resolve_key(spec.id, spec.auth_env, stored)
    info = agents_mod.detect(spec, key=(value or None))
    info.update({
        "custom": bool(spec.custom),
        "model": spec.model,
        "args": list(spec.default_args),
        "probe_url": spec.probe_url,
        "api_url": spec.api_url,
        "api_url_env": spec.api_url_env,
        "service_start": spec.service_start,
        "service_url": spec.service_url(),
        "auth_source": source,
        "auth_value_masked": mask(value) if source == "file" else
                             (mask(value) if source == "env" else ""),
        "auth_ref": (str(stored).strip() if stored and str(stored).strip().lower().startswith("env:")
                     else (spec.auth_env or "")),
        "command": spec.resolved_cmd(),
    })
    # 只有配置了 auth_env 才有意义；无 env 型（opencode / ollama）如实置 None
    if spec.auth_env:
        info["authed"] = bool(value)
    return info


def build_config_view() -> Dict[str, Any]:
    """GET /api/config 的响应体。"""
    sync_registry()
    creds = load_credentials()
    auth = creds.get("agent_auth", {})
    path = cred_path()
    roles_path = ROLES_PATH
    return {
        "paths": {
            "credentials": str(path),
            "credentials_exists": path.is_file(),
            "roles": str(roles_path),
            "roles_exists": roles_path.is_file(),
            "config_env": CONFIG_ENV_VAR if config_env_active() else None,
        },
        "agents": [agent_view(s, auth.get(s.id)) for s in iter_agent_specs()],
        "roles": load_roles(),
        "auth_hint": (
            "API Key 两种写法：① 直接填值 —— 落盘到 "
            + str(path) + "（chmod 600，已在 .gitignore 中排除）；"
            "② env:XXX —— 引用环境变量，敏感值不落盘。"
        ),
    }


# ---------------------------------------------------------------- 保存
def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/config：合并保存 agent 鉴权、自定义 agent、角色库。"""
    creds = load_credentials()
    auth = dict(creds.get("agent_auth", {}))

    incoming_auth = payload.get("agent_auth")
    if isinstance(incoming_auth, dict):
        for k, v in incoming_auth.items():
            if v is None:
                continue
            s = str(v).strip()
            if s == KEEP or s == "":
                # 空串表示清空；__KEEP__ 表示保持不变
                if s == "":
                    auth.pop(str(k), None)
                continue
            auth[str(k)] = s
    creds["agent_auth"] = auth

    # 内置 agent 的覆盖配置（服务地址 / 模型名），传空值即恢复内置默认
    incoming_ov = payload.get("agent_overrides")
    if isinstance(incoming_ov, dict):
        ov = dict(creds.get("agent_overrides", {}) or {})
        for k, v in incoming_ov.items():
            aid = str(k)
            if v is None:
                ov.pop(aid, None)
                continue
            if not isinstance(v, dict):
                continue
            cur = dict(ov.get(aid, {}) or {})
            for f in ("api_url", "model", "args"):
                if f not in v:
                    continue
                val = v[f]
                if val is None or (isinstance(val, str) and not val.strip()):
                    cur.pop(f, None)          # 清空 = 恢复内置默认
                else:
                    cur[f] = val
            if cur:
                ov[aid] = cur
            else:
                ov.pop(aid, None)
        creds["agent_overrides"] = ov

    if isinstance(payload.get("custom_agents"), list):
        cleaned: List[Dict[str, Any]] = []
        for d in payload["custom_agents"]:
            spec = _spec_from_dict(d or {})
            if spec is None:
                continue
            cleaned.append({
                "id": spec.id,
                "name": spec.name,
                "cmd": spec.cmd,
                "vendor": spec.vendor,
                "model": spec.model,
                "args": list(spec.default_args),
                "auth_env": spec.auth_env,
                "local": spec.local,
                "install_hint": spec.install_hint,
                "doc_url": spec.doc_url,
                "probe_url": spec.probe_url,
                "api_url": spec.api_url,
                "api_url_env": spec.api_url_env,
                "probe_path": spec.probe_path,
                "service_start": spec.service_start,
            })
        creds["custom_agents"] = cleaned

    save_credentials(creds)

    if isinstance(payload.get("roles"), list):
        save_roles(payload["roles"])

    # 立即让新的字面值密钥对后续 agent 启动生效
    apply_auth_env()
    return build_config_view()


# ---------------------------------------------------------------- 连通性测试
PROBE_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "claude": {"url": "https://api.anthropic.com/v1/models", "mode": "anthropic"},
    "codex": {"url": "https://api.openai.com/v1/models", "mode": "bearer"},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models", "mode": "query"},
    "copilot": {"url": "https://api.github.com/user", "mode": "bearer"},
    "ollama": {"url": "http://127.0.0.1:11434/api/tags", "mode": "none"},
    "opencode": {"url": "", "mode": "none"},
}


def _http_probe(url: str, mode: str, key: str, timeout: float = 8.0) -> Dict[str, Any]:
    target = url
    headers = {"User-Agent": "magent-console/1.0"}
    if mode == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif mode == "bearer":
        headers["Authorization"] = "Bearer " + key
    elif mode == "query":
        sep = "&" if "?" in target else "?"
        target = target + sep + "key=" + urllib.parse.quote(key)
    try:
        req = urllib.request.Request(target, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"checked": True, "ok": True, "status": resp.status,
                    "detail": "HTTP %s" % resp.status}
    except urllib.error.HTTPError as e:
        return {"checked": True, "ok": False, "status": e.code,
                "detail": "HTTP %s %s" % (e.code, e.reason)}
    except Exception as e:                                     # noqa: BLE001
        return {"checked": False, "ok": None, "status": None,
                "detail": "%s: %s" % (type(e).__name__, e)}


def test_agent(agent_id: str, spec_override: Optional[Dict[str, Any]] = None,
               do_network: bool = True) -> Dict[str, Any]:
    """测试某个 agent / 模型的可用性与鉴权连通性。

    spec_override 允许测试尚未保存的自定义 agent 定义。
    """
    sync_registry()
    creds = load_credentials()
    auth = creds.get("agent_auth", {})

    spec: Optional[AgentSpec] = None
    if spec_override:
        spec = _spec_from_dict(spec_override)
        if spec is None:
            return {"ok": False, "agent": agent_id,
                    "message": "自定义 agent 定义不完整：id 与 cmd 必填"}
    if spec is None:
        spec = agents_mod.AGENTS.get(agent_id)
    if spec is None:
        return {"ok": False, "agent": agent_id,
                "message": "未注册该 agent：%s" % agent_id}

    stored = auth.get(spec.id)
    if spec_override and "auth_env" in (spec_override or {}):
        stored = spec_override.get("auth_value") or stored
    value, source = resolve_key(spec.id, spec.auth_env, stored)

    binary = spec.cmd.split()[0]
    path = shutil.which(binary)
    installed = path is not None
    version = ""
    if installed:
        try:
            p = subprocess.run([binary, "--version"], capture_output=True,
                               text=True, timeout=6)
            if p.returncode == 0:
                version = ((p.stdout or "") or (p.stderr or "")).strip().splitlines()[0][:80]
        except Exception:
            version = ""

    auth_info = {
        "required": bool(spec.auth_env),
        "env": spec.auth_env or "",
        "configured": bool(value),
        "source": source,
        "masked": mask(value) if value else "",
    }

    network: Dict[str, Any] = {"checked": False, "ok": None, "status": None, "detail": ""}
    endpoint = spec.service_url() or (PROBE_ENDPOINTS.get(spec.id, {}) or {}).get("url", "")
    mode = "bearer" if spec.probe_url else (PROBE_ENDPOINTS.get(spec.id, {}) or {}).get("mode", "none")
    if do_network and endpoint and (value or mode == "none"):
        network = _http_probe(endpoint, mode, value)
        network["endpoint"] = endpoint
    elif do_network and endpoint and not value:
        network = {"checked": False, "ok": None, "status": None,
                   "detail": "缺少密钥，跳过联网探测", "endpoint": endpoint}
    else:
        network["detail"] = "该 agent 无通用连通性探测端点（不同供应商鉴权方式各异）"

    # 结论
    if not installed:
        ok = False
        msg = "未安装：在 PATH 中找不到 %s。%s" % (binary, spec.install_hint or "")
    elif spec.auth_env and not value:
        ok = False
        msg = ("已安装，但未配置鉴权（%s 为空）。可直接在设置里填 API Key，"
               "或填 env:变量名 引用环境变量" % spec.auth_env)
    elif network.get("checked") and network.get("ok") is False:
        ok = False
        if spec.local and spec.service_start:
            msg = ("本地服务未就绪：%s。请启动服务（%s）后重试；"
                   "若服务跑在宿主机/另一台机器上，请在设置里把服务地址改成实际可达的地址"
                   % (network.get("detail"), spec.service_start))
        else:
            msg = ("已安装且已配置鉴权，但接口返回 %s —— 请检查密钥是否有效、"
                   "是否有该模型的访问权限" % network.get("detail"))
    elif network.get("ok"):
        ok = True
        msg = "可用：已安装（%s），鉴权连通性探测通过（%s）" % (
            version or binary, network.get("detail"))
    elif network.get("checked") is False and network.get("detail", "").startswith(("URLError", "socket", "timeout", "Timeout")):
        ok = True
        msg = ("已安装且已配置密钥，但联网探测失败（%s）—— 多半是本机网络/代理问题，"
               "不代表密钥无效" % network.get("detail"))
    else:
        ok = True
        msg = "可用：已安装（%s），密钥已配置（来源：%s）%s" % (
            version or binary,
            {"env": "环境变量", "file": "本地配置文件"}.get(source, source),
            "；" + network["detail"] if network.get("detail") else "")

    return {
        "ok": ok,
        "agent": spec.id,
        "name": spec.name,
        "installed": installed,
        "path": path or "",
        "version": version,
        "local": spec.local,
        "command": spec.resolved_cmd(),
        "auth": auth_info,
        "network": network,
        "message": msg,
    }
