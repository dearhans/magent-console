#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/benchmark.py — 本机性能评估与 Agent 并行数推荐

设计原则：
  云端 agent CLI（claude/codex/opencode…）本身几乎不消耗本地算力，
  它们是把请求发到云端 API、本地只渲染终端 UI。因此推荐并行数的
  真实瓶颈不是 CPU/GPU，而是三样东西：
      1) 内存   —— 每个 agent CLI 是一个 Node/原生进程，实测量级 150~600MB
      2) 屏幕   —— 每个 tmux pane 有最小可读尺寸，超过就看不清了
      3) 注意力 —— 人眼同时监控的 agent 上限，经验值 6~8
  只有跑「本地模型」（Ollama / LM Studio）时，CPU/GPU/显存才重新成为瓶颈，
  本模块对这两种模式分别建模。

零第三方依赖，纯标准库，Python 3.9+ 可跑。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 经验常量
# 以下数值来自对 Node 系 agent CLI（claude-code / opencode / codex）的实测量级，
# 保守取值，宁可少推荐也不让机器卡死。
MEM_PER_CLOUD_AGENT_MB = 420     # 单云端 agent CLI 常驻内存（含输出缓冲）
MEM_RESERVED_FOR_OS_MB = 2048    # 系统 + 编辑器 + 浏览器 的保底占用
CPU_PER_CLOUD_AGENT = 0.25       # 单 agent 平均 CPU 占用（核）
MIN_PANE_COLS = 80               # 单个 pane 最小可读宽度（字符）
MIN_PANE_ROWS = 20               # 单个 pane 最小可读高度（行）
HARD_MAX_AGENTS = 8              # 人类注意力硬上限
MEM_MULTIPLIER_LOCAL = 1.6       # 本地模型：权重 + KV cache + 上下文 的放大系数
VRAM_MULTIPLIER_LOCAL = 1.25     # 本地模型：显存占用放大系数


def _run(cmd: List[str], timeout: float = 5.0) -> str:
    """安全执行命令，任何异常都返回空串，绝不抛出。"""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.readline().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 数据采集
def cpu_info() -> Dict[str, Any]:
    """cores = 物理核，threads = 逻辑处理器（含超线程）。

    os.cpu_count() 在 Windows/Linux 上返回的是「逻辑处理器」数，
    直接拿它当物理核会高估并行能力，因此优先借 psutil 区分二者；
    psutil 不可用时退回 os.cpu_count()，并如实标注无法区分。
    """
    logical = os.cpu_count() or 1
    physical = logical
    freq_mhz: Optional[float] = None
    precise = False            # 是否拿到了可信的物理核数

    try:
        import psutil  # type: ignore
        p = psutil.cpu_count(logical=False)
        if p:
            physical = p
            precise = True
        l = psutil.cpu_count(logical=True)
        if l:
            logical = l
        try:
            f = psutil.cpu_freq()
            if f and getattr(f, "max", 0):
                freq_mhz = round(float(f.max), 0)
        except Exception:
            pass
    except Exception:
        pass

    model = ""
    system = platform.system()

    if system == "Linux":
        for line in _run(["lscpu"]).splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[-1].strip()
                break
        if not model:
            model = _read_first_line("/proc/cpuinfo").split(":")[-1].strip()
        if freq_mhz is None:
            raw = _read_first_line("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
            if raw.isdigit():
                freq_mhz = round(int(raw) / 1000.0, 0)

    elif system == "Darwin":
        model = _run(["sysctl", "-n", "machdep.cpu.brand_string"])

    elif system == "Windows":
        # wmic 在 Win11 24H2+ 已被移除，失败时回退 PowerShell CIM
        out = _run(["wmic", "cpu", "get", "name"], timeout=8)
        for line in out.splitlines():
            t = line.strip()
            if t and t.lower() != "name":
                model = t
                break
        if not model:
            out = _run(["powershell", "-NoProfile", "-Command",
                        "(Get-CimInstance Win32_Processor).Name"], timeout=15)
            if out.strip():
                model = out.strip().splitlines()[0].strip()
        if not model:
            model = platform.processor()
        if freq_mhz is None:
            raw = _run(["wmic", "cpu", "get", "MaxClockSpeed"], timeout=8)
            for line in raw.splitlines():
                if line.strip().isdigit():
                    freq_mhz = float(line.strip())
                    break

    return {"cores": physical, "threads": logical,
            "model": model or platform.machine(), "freq_mhz": freq_mhz,
            "cores_precise": precise,
            "hyperthreading": bool(precise and logical > physical)}


def memory_info() -> Dict[str, float]:
    """返回 MB 单位的 total / available / used。

    优先级：psutil → 平台原生（Linux /proc/meminfo、macOS sysctl+vm_stat、
    Windows GlobalMemoryStatusEx）。Windows 分支用 ctypes 直调 Win32 API，
    零依赖且准确；只有当所有途径都失败时才返回 unavailable=True，
    绝不再静默编造一个 8192MB 的假数字。
    """
    # 1) psutil：跨平台且 available 口径最贴近真实可用量
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return {"total_mb": round(vm.total / 1048576),
                "available_mb": round(vm.available / 1048576),
                "used_mb": round(vm.used / 1048576)}
    except Exception:
        pass

    system = platform.system()

    # 2) Linux
    if system == "Linux":
        try:
            info: Dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    val = rest.strip().split()
                    if val and val[0].isdigit():
                        info[key] = int(val[0])       # kB
            total = info.get("MemTotal", 0) / 1024
            avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024
            if total > 0:
                return {"total_mb": round(total), "available_mb": round(avail),
                        "used_mb": round(total - avail)}
        except Exception:
            pass

    # 3) macOS
    if system == "Darwin":
        try:
            total_b = int(_run(["sysctl", "-n", "hw.memsize"]) or 0)
            vm = _run(["vm_stat"])
            page_size = 4096
            # vm_stat 给的是页数：free + inactive + speculative ≈ 可用
            free_ = inactive_ = speculative_ = 0
            m = re.search(r"Pages free:\s+(\d+)", vm)
            if m:
                free_ = int(m.group(1))
            m = re.search(r"Pages inactive:\s+(\d+)", vm)
            if m:
                inactive_ = int(m.group(1))
            m = re.search(r"Pages speculative:\s+(\d+)", vm)
            if m:
                speculative_ = int(m.group(1))
            if total_b > 0:
                total = total_b / 1048576
                avail = (free_ + inactive_ + speculative_) * page_size / 1048576
                return {"total_mb": round(total), "available_mb": round(avail),
                        "used_mb": round(total - avail)}
        except Exception:
            pass

    # 4) Windows：ctypes 直调 GlobalMemoryStatusEx
    if system == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total = stat.ullTotalPhys / 1048576
                avail = stat.ullAvailPhys / 1048576
                return {"total_mb": round(total), "available_mb": round(avail),
                        "used_mb": round(total - avail)}
        except Exception:
            pass

    # 5) 兜底：如实标记不可用，不编造数值
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1048576
        return {"total_mb": round(total), "available_mb": round(total * 0.5),
                "used_mb": round(total * 0.5), "unavailable": False}
    except Exception:
        return {"total_mb": 0, "available_mb": 0, "used_mb": 0, "unavailable": True}


def gpu_info() -> Dict[str, Any]:
    """尽力探测独显 / NPU。探测不到如实返回 detected=False，不猜测。"""
    result: Dict[str, Any] = {"detected": False, "devices": [], "vram_mb": 0}
    system = platform.system()

    if system == "Linux":
        lspci = _run(["lspci"])
        if not lspci:
            lspci = _run(["lspci", "-nn"], timeout=8)
        for line in lspci.splitlines():
            low = line.lower()
            if any(k in low for k in ("vga compatible", "3d controller", "display controller")):
                name = line.split(":", 2)[-1].strip() if ":" in line else line.strip()
                result["devices"].append(name)
                result["detected"] = True
        # NVIDIA 显存
        if shutil.which("nvidia-smi"):
            out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
            if out:
                result["devices"] = [l.split(",")[0].strip() for l in out.splitlines() if l.strip()]
                vram = [int(float(l.split(",")[-1])) for l in out.splitlines()
                        if "," in l and l.split(",")[-1].strip().replace(".", "").isdigit()]
                if vram:
                    result["vram_mb"] = max(vram)
                result["detected"] = True
        # Intel Arc / NPU
        for line in (lspci or "").splitlines():
            if "intel" in line.lower() and any(k in line.lower() for k in ("arc", "xe", "graphics")):
                result.setdefault("note", "Intel 核显/独显（Arc）— Linux 下由 xe/i915 内核驱动原生支持")
    elif system == "Darwin":
        sp = _run(["system_profiler", "SPDisplaysDataType"], timeout=12)
        if sp:
            result["detected"] = True
            for line in sp.splitlines():
                if "Chipset Model" in line:
                    result["devices"].append(line.split(":", 1)[-1].strip())
    elif system == "Windows":
        # wmic 在 Win11 24H2+ 被移除，优先 PowerShell CIM，失败再回退 wmic
        names: List[str] = []
        vram_bytes = 0
        ps = ("Get-CimInstance Win32_VideoController | "
              "ForEach-Object { $_.Name + '||' + [string]$_.AdapterRAM }")
        out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=20)
        for line in out.splitlines():
            if "||" in line:
                name, _, ram = line.partition("||")
                name = name.strip()
                if not name:
                    continue
                names.append(name)
                try:
                    ram_i = int(ram.strip())
                except Exception:
                    ram_i = 0
                if ram_i > vram_bytes:
                    vram_bytes = ram_i

        if not names:
            out = _run(["wmic", "path", "win32_VideoController", "get", "name"], timeout=10)
            for line in out.splitlines()[1:]:
                if line.strip() and line.strip().lower() != "name":
                    names.append(line.strip())

        if names:
            result["detected"] = True
            result["devices"] = names
        if vram_bytes > 0:
            result["vram_mb"] = round(vram_bytes / 1048576)

        joined = " ".join(names).lower()
        if any(k in joined for k in ("intel", "arc", "iris", "uhd", "hd graphics")):
            # 独立 Arc 一定带型号（A310/A380/A580/A750/A770/B580…）；
            # 只写 "Arc(TM) Graphics" 的是 Core Ultra 内置核显，没有自己的显存，
            # Windows 报的 AdapterRAM 其实是共享内存配额，不能当独显显存用。
            discrete_arc = re.search(r"\b[ab]\s?\d{3}\b", joined) is not None
            integrated = not discrete_arc
            result["integrated"] = integrated
            if integrated:
                result["vram_is_shared"] = result.get("vram_mb", 0) > 0
                result["note"] = (
                    "检测到 Intel 核显（无独立显存"
                    + ("，Windows 报告 " + str(result["vram_mb"]) + "MB，实为共享内存配额"
                       if result.get("vram_mb", 0) > 0 else "")
                    + "）。本地模型走共享内存推理，速度远慢于独显；"
                      "Windows 下建议改用 WSL2 以获得更完整的 GPU 计算栈"
                )
            else:
                result["note"] = ("检测到 Intel 独立 Arc 显卡，Windows 下建议用 WSL2 "
                                  "以获得更完整的 GPU 计算栈")

    return result



def _project_root() -> str:
    """本项目根目录（core/ 的上一级），即 server.py 所在目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _decode_mountinfo(field: str) -> str:
    """mountinfo 里路径字段用八进制转义（\\040 空格、\\011 制表、\\134 反斜杠）。"""
    return (field.replace("\\040", " ")
                 .replace("\\011", "\t")
                 .replace("\\012", "\n")
                 .replace("\\134", "\\"))


def _windows_drive_from_wsl(path: str) -> str:
    """WSL 下 /mnt/c/... → C:；非 drvfs 挂载返回空串。"""
    m = re.match(r"^/mnt/([a-zA-Z])(/|$)", path)
    return (m.group(1).upper() + ":") if m else ""


def _mount_of(path: str) -> Dict[str, str]:
    """在 Linux/WSL 上找出 path 所属挂载点（最长前缀匹配）。

    直接读 /proc/self/mountinfo 而不是解析 df 输出，避免依赖列位置与语言环境。
    """
    best_mp = best_fs = best_src = ""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 10:
                    continue
                try:
                    sep = parts.index("-")
                except ValueError:
                    continue
                if sep + 2 >= len(parts):
                    continue
                mp = _decode_mountinfo(parts[4])
                fs = parts[sep + 1]
                src = parts[sep + 2]
                if path == mp or path.startswith(mp.rstrip("/") + "/"):
                    if len(mp) > len(best_mp):
                        best_mp, best_fs, best_src = mp, fs, src
    except Exception:
        pass
    return {"mount_point": best_mp, "fstype": best_fs, "device": best_src}


def disk_info(path: Optional[str] = None) -> Dict[str, Any]:
    """检测 path 所在磁盘的真实容量，并带上「这是哪块盘」的定位信息。

    path 省略时默认检测**项目实际所在目录**，而不是 os.path.expanduser("~")：
    服务跑在 WSL2 里时 home 位于 WSL 虚拟磁盘（/dev/sdc，动辄 1TB 可用），
    而项目文件其实在 Windows C 盘的 /mnt/c 挂载点下，两者的可用空间完全不是一回事。
    量错盘会让「磁盘还剩多少」这项指标彻底失真，因此这里必须量项目目录。
    """
    target = os.path.abspath(os.path.expanduser(path or _project_root()))
    out: Dict[str, Any] = {
        "path": target,
        "total_gb": 0.0, "free_gb": 0.0, "used_gb": 0.0,
        "mount_point": "", "fstype": "", "device": "",
        "drive": "", "label": "", "is_wsl_mount": False,
    }
    try:
        usage = shutil.disk_usage(target)
        out["total_gb"] = round(usage.total / 1024 ** 3, 1)
        out["free_gb"] = round(usage.free / 1024 ** 3, 1)
        out["used_gb"] = round(usage.used / 1024 ** 3, 1)
    except Exception:
        return out

    system = platform.system()
    if system == "Windows":
        drive = os.path.splitdrive(target)[0]
        out.update(drive=drive, mount_point=drive, device=drive, fstype="NTFS")
        out["label"] = f"Windows {drive} 盘" if drive else target
        return out

    m = _mount_of(target)
    drive = _windows_drive_from_wsl(target)
    out.update(mount_point=m["mount_point"], fstype=m["fstype"],
               device=m["device"], drive=drive,
               is_wsl_mount=bool(drive))
    if drive:
        out["label"] = f"Windows {drive} 盘（WSL 挂载于 {m['mount_point'] or target}）"
    elif m["mount_point"] == "/":
        out["label"] = "WSL2 虚拟磁盘（根挂载点 /）"
    elif m["mount_point"]:
        out["label"] = f"挂载点 {m['mount_point']}"
    else:
        out["label"] = target
    return out


def environment_info() -> Dict[str, Any]:
    system = platform.system()
    is_wsl = False
    wsl_ver = None
    try:
        ver = _read_first_line("/proc/version").lower()
        if "microsoft" in ver:
            is_wsl = True
            wsl_ver = "2" if "wsl2" in ver else "1"
    except Exception:
        pass

    return {
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "is_wsl": is_wsl,
        "wsl_version": wsl_ver,
        "has_tmux": shutil.which("tmux") is not None,
        "tmux_version": _run(["tmux", "-V"]) or None,
        "has_git": shutil.which("git") is not None,
        "has_docker": shutil.which("docker") is not None,
        "has_ollama": shutil.which("ollama") is not None,
    }


def cpu_benchmark(rounds: int = 3) -> Dict[str, Any]:
    """
    轻量 CPU 基准：纯 Python 整数/浮点混合运算，不依赖 numpy。
    仅用于横向对比不同机器，绝对值无意义。
    """
    def workload(n: int = 400_000) -> float:
        start = time.perf_counter()
        acc = 0
        for i in range(1, n):
            acc += (i * i) % 1000003
            acc ^= (i << 3) & 0xFFFF
            if i % 7 == 0:
                acc -= int(i ** 0.5)
        return time.perf_counter() - start

    times = [workload() for _ in range(rounds)]
    best = min(times)
    # 归一化：以 1.0 秒为 100 分基准（越快分越高）
    score = round(100.0 / max(best, 0.01), 1)
    return {"seconds_best": round(best, 3),
            "seconds_all": [round(t, 3) for t in times],
            "score": score,
            "grade": _grade(score)}


def _grade(score: float) -> str:
    if score >= 180:
        return "S"
    if score >= 120:
        return "A"
    if score >= 70:
        return "B"
    return "C"


# ---------------------------------------------------------------- 推荐引擎
@dataclass
class Recommendation:
    max_agents_cloud: int = 1
    max_agents_local: int = 0
    recommended: int = 1
    bottleneck: str = ""
    reasons: List[str] = field(default_factory=list)
    grade: str = "C"
    score: float = 0.0
    screen_limit: Optional[int] = None
    local_model_note: str = ""


def recommend(profile: Dict[str, Any],
              terminal_cols: Optional[int] = None,
              terminal_rows: Optional[int] = None) -> Recommendation:
    """
    基于硬件画像推荐 agent 并行数。
    terminal_cols/rows 由前端从浏览器上报；为 None 时跳过屏幕约束。
    """
    rec = Recommendation()
    mem = profile.get("memory", {})
    cpu = profile.get("cpu", {})
    gpu = profile.get("gpu", {})
    disk = profile.get("disk", {})

    mem_avail = float(mem.get("available_mb", 0))
    cores = int(cpu.get("cores", 1) or 1)

    # ---- 云端模式约束 ----
    usable_mem = max(mem_avail - MEM_RESERVED_FOR_OS_MB, 0)
    by_mem = int(usable_mem // MEM_PER_CLOUD_AGENT_MB)
    by_cpu = int(cores / CPU_PER_CLOUD_AGENT)

    limits = [by_mem, by_cpu, HARD_MAX_AGENTS]
    limit_names = {id(by_mem): "内存", id(by_cpu): "CPU 核心数", id(HARD_MAX_AGENTS): "人类注意力上限"}

    if terminal_cols and terminal_rows:
        grid_cols = max(int(terminal_cols // MIN_PANE_COLS), 1)
        grid_rows = max(int(terminal_rows // MIN_PANE_ROWS), 1)
        by_screen = grid_cols * grid_rows
        rec.screen_limit = by_screen
        limits.append(by_screen)

    max_cloud = max(min(limits), 1)
    rec.max_agents_cloud = max_cloud

    # 找出瓶颈
    min_val = min(limits)
    for cand in limits:
        if cand == min_val:
            rec.bottleneck = limit_names.get(id(cand), "屏幕可用面积")
            break

    rec.recommended = max(min(max_cloud, 4), 1)  # 默认推荐 4，保守起步

    # ---- 本地模型模式约束 ----
    # 核显没有自己的显存：Windows 报的 AdapterRAM 是共享内存配额，
    # 不能按独显显存那样直接拿来算并发实例数，否则会严重高估。
    vram = float(gpu.get("vram_mb", 0) or 0)
    has_discrete_gpu = bool(gpu.get("detected")) and vram > 0 and not gpu.get("integrated")

    if has_discrete_gpu:
        # 以 4B 模型 Q4（约 2.5GB）为基准单位
        unit_mb = 2500 * VRAM_MULTIPLIER_LOCAL
        rec.max_agents_local = max(int(vram // unit_mb), 1)
        rec.local_model_note = (
            f"检测到独立 GPU（显存 {int(vram)}MB），本地模型建议最多 "
            f"{rec.max_agents_local} 个实例（以 4B/Q4 为基准）"
        )
    else:
        # 无独显：走共享内存 / CPU 推理
        unit_mb = 2500 * MEM_MULTIPLIER_LOCAL
        by_mem_local = int(usable_mem // unit_mb)
        by_cpu_local = max(int(cores // 2), 1)
        rec.max_agents_local = max(min(by_mem_local, by_cpu_local), 0)
        if gpu.get("integrated"):
            vram_desc = ("Windows 报告 " + str(int(vram)) + "MB，实为共享内存配额"
                         if vram > 0 else "无独立显存")
            rec.local_model_note = (
                f"检测到核显（{vram_desc}）。本地模型只能走共享内存 / CPU 推理，速度极慢；"
                f"按当前可用内存算最多 {rec.max_agents_local} 个实例，但建议 ≤1 个，或改走云端 agent"
            )
        else:
            rec.local_model_note = (
                "未检测到独立 GPU，本地模型只能 CPU 推理，速度极慢；"
                "若必须本地跑，建议 ≤1 个实例，或改走云端 agent"
            )

    # ---- 综合评分 ----
    score = float(profile.get("cpu_benchmark", {}).get("score", 0))
    mem_score = min(mem_avail / 8192.0, 1.0) * 60          # 8GB 可用内存 = 60 分
    cpu_score = min(cores / 8.0, 1.0) * 40                 # 8 核 = 40 分
    total = round(score * 0.3 + mem_score + cpu_score, 1)
    rec.score = total
    rec.grade = _grade(total)

    # ---- 人话理由 ----
    rec.reasons = [
        f"可用内存 {int(mem_avail)}MB，扣除系统保底 {MEM_RESERVED_FOR_OS_MB}MB 后，"
        f"按每 agent {MEM_PER_CLOUD_AGENT_MB}MB 计 → 上限 {by_mem} 个",
        f"CPU {cores} 核，按每 agent {CPU_PER_CLOUD_AGENT} 核计 → 上限 {by_cpu} 个",
        f"人眼同时监控上限 → {HARD_MAX_AGENTS} 个",
    ]
    if rec.screen_limit:
        rec.reasons.append(
            f"当前终端 {terminal_cols}×{terminal_rows}，按每 pane ≥{MIN_PANE_COLS}×{MIN_PANE_ROWS} "
            f"字符计 → 上限 {rec.screen_limit} 个"
        )
    disk_free_gb = float(disk.get("free_gb", 0) or 0)
    if disk_free_gb < 5:
        rec.reasons.append(
            f"⚠ 项目所在磁盘（{disk.get('label') or disk.get('path') or '未知'}）"
            f"仅剩 {disk_free_gb}GB，agent 写代码、装依赖、跑构建都需要落盘空间，"
            f"建议先清理到 10GB 以上再开工"
        )
    rec.reasons.append(f"综合瓶颈：{rec.bottleneck}")

    return rec


# ---------------------------------------------------------------- 主入口
def collect(terminal_cols: Optional[int] = None,
            terminal_rows: Optional[int] = None,
            run_bench: bool = True) -> Dict[str, Any]:
    profile: Dict[str, Any] = {
        "cpu": cpu_info(),
        "memory": memory_info(),
        "gpu": gpu_info(),
        "disk": disk_info(_project_root()),
        "environment": environment_info(),
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if run_bench:
        profile["cpu_benchmark"] = cpu_benchmark()
    profile["recommendation"] = asdict(
        recommend(profile, terminal_cols, terminal_rows)
    )
    return profile


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
