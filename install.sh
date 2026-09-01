#!/usr/bin/env bash
# ==========================================================================
# Magent Console — 一键安装脚本
#
# 目标：在一台全新机器上，一条命令把控制台跑起来。
#   自动做的事：检查 Python → 装 tmux → （可选）装 agent CLI → （可选）装 Ollama
#
# 用法：
#   ./install.sh                         # 只装必需依赖（Python + tmux）
#   ./install.sh --with-agent=claude     # 顺带装 claude-code
#   ./install.sh --with-agent=opencode --with-ollama
#   ./install.sh --yes                   # 非交互，全部用默认值
#
# 远程一键装（把 REPO_URL 换成你自己的仓库）：
#   curl -fsSL https://raw.githubusercontent.com/<你>/magent-console/main/install.sh | bash
#
# 支持：macOS / Debian-Ubuntu / Fedora-RHEL / Arch / Alpine / openSUSE / WSL2
# 幂等：已装好的组件会跳过，可重复运行。
# ==========================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/your-name/magent-console.git}"
INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PORT="${PORT:-8899}"

WITH_AGENT=""
WITH_OLLAMA=0
ASSUME_YES=0

# ---------------------------------------------------------------- 输出
if [ -t 1 ]; then
  C_R=$'\033[0m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[34m'; C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_BO=$'\033[1m'
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_RED=""; C_DIM=""; C_BO=""
fi

ok()   { printf '%s  ✓%s %s\n' "$C_G" "$C_R" "$*"; }
warn() { printf '%s  !%s %s\n' "$C_Y" "$C_R" "$*" >&2; }
err()  { printf '%s  ✗%s %s\n' "$C_RED" "$C_R" "$*" >&2; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_G" "$C_R" "$C_BO" "$*" "$C_R"; }
info() { printf '     %s\n' "$*"; }

die() { err "$*"; exit 1; }

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
  exit 0
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

confirm() {
  # $1=问题；非交互模式或 stdin 非终端时默认 yes
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ -t 0 ] || return 0
  printf '%s  ?%s %s [Y/n] ' "$C_B" "$C_R" "$1"
  read -r reply < /dev/tty 2>/dev/null || return 0
  case "$reply" in
    n|N|no|No|NO) return 1 ;;
    *) return 0 ;;
  esac
}

# ---------------------------------------------------------------- 参数
for arg in "$@"; do
  case "$arg" in
    --with-agent=*)   WITH_AGENT="${arg#*=}" ;;
    --with-agent)     warn "--with-agent 需要值，例：--with-agent=claude" ;;
    --with-ollama)    WITH_OLLAMA=1 ;;
    -y|--yes)         ASSUME_YES=1 ;;
    --port=*)         PORT="${arg#*=}" ;;
    -h|--help)        usage ;;
    *)                warn "忽略未知参数：$arg" ;;
  esac
done

printf '%s' "$C_BO"
cat <<'BANNER'
  ┌──────────────────────────────────────────────┐
  │   Magent Console — 多 Agent 编排控制台        │
  │   本机性能评估 · 三种编排模式 · 实时监控      │
  └──────────────────────────────────────────────┘
BANNER
printf '%s' "$C_R"

# ---------------------------------------------------------------- 0) 拿到代码
step "检查项目文件"
if [ ! -f "$INSTALL_DIR/server.py" ]; then
  warn "当前目录没有 server.py，尝试从仓库获取…"
  if need_cmd git; then
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/magent-console" \
      && INSTALL_DIR="$INSTALL_DIR/magent-console" \
      || die "clone 失败，请手动下载后重跑本脚本"
  else
    die "既没有 server.py，也没有 git。请先安装 git 或手动下载项目。"
  fi
fi
cd "$INSTALL_DIR" || die "无法进入目录 $INSTALL_DIR"
ok "项目目录：$INSTALL_DIR"

# ---------------------------------------------------------------- 1) 系统识别
step "识别系统"
OS="unknown"; PKG=""; IS_WSL=0

case "$(uname -s)" in
  Darwin)
    OS="macos"
    if need_cmd brew; then PKG="brew"; else PKG="brew-missing"; fi
    ;;
  Linux)
    grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
    if   need_cmd apt-get; then OS="debian";   PKG="apt"
    elif need_cmd dnf;     then OS="fedora";   PKG="dnf"
    elif need_cmd yum;     then OS="fedora";   PKG="yum"
    elif need_cmd pacman;  then OS="arch";     PKG="pacman"
    elif need_cmd apk;     then OS="alpine";   PKG="apk"
    elif need_cmd zypper;  then OS="suse";     PKG="zypper"
    else OS="linux-unknown"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    die "检测到 Windows 原生环境。请在 WSL2 中运行本脚本（tmux 依赖 Unix 环境）。"
    ;;
esac

info "系统：${OS}${IS_WSL:+ (WSL2)}"
info "包管理器：${PKG:-未识别}"
if [ "$IS_WSL" -eq 1 ]; then
  info "WSL2 提示：控制台监听 127.0.0.1，Windows 浏览器可直接访问 localhost:$PORT"
fi

# ---------------------------------------------------------------- 2) Python
step "检查 Python"
PY=""
for cand in python3 python; do
  if need_cmd "$cand"; then
    ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
    maj="${ver%%.*}"; min="${ver##*.}"
    if [ "$maj" -eq 3 ] && [ "$min" -ge 9 ]; then
      PY="$cand"; PYVER="$ver"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  warn "未找到 Python 3.9+，尝试安装…"
  case "$PKG" in
    apt)     sudo apt-get update -qq && sudo apt-get install -y python3 ;;
    dnf|yum) sudo "$PKG" install -y python3 ;;
    pacman)  sudo pacman -Sy --noconfirm python ;;
    apk)     sudo apk add --no-cache python3 ;;
    zypper)  sudo zypper install -y python3 ;;
    brew)    brew install python3 ;;
    *)       die "请手动安装 Python 3.9+ 后重跑" ;;
  esac
  PY="python3"
  PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
fi
ok "Python $PYVER（$PY）"
info "后端只用标准库，无需 pip install 任何东西"

# ---------------------------------------------------------------- 3) tmux
step "检查 tmux"
if need_cmd tmux; then
  ok "已安装：$(tmux -V 2>/dev/null || echo '未知版本')"
else
  warn "未安装 tmux —— 它是本控制台的编排核心"
  if confirm "现在安装 tmux？"; then
    case "$PKG" in
      apt)     sudo apt-get update -qq && sudo apt-get install -y tmux ;;
      dnf|yum) sudo "$PKG" install -y tmux ;;
      pacman)  sudo pacman -Sy --noconfirm tmux ;;
      apk)     sudo apk add --no-cache tmux ;;
      zypper)  sudo zypper install -y tmux ;;
      brew)    brew install tmux ;;
      brew-missing)
        warn "未找到 Homebrew，先装 Homebrew 再装 tmux"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
          && brew install tmux
        ;;
      *)       die "无法自动安装 tmux，请手动安装：https://github.com/tmux/tmux/wiki/Installing" ;;
    esac
    need_cmd tmux && ok "tmux 安装完成：$(tmux -V)" || die "tmux 安装失败，请手动安装后重跑"
  else
    warn "跳过 tmux。没有 tmux 时控制台能打开，但无法启动/监控 agent 编排。"
  fi
fi

# ---------------------------------------------------------------- 4) Agent CLI
step "Agent CLI"
if [ -n "$WITH_AGENT" ] && [ "$WITH_AGENT" != "none" ]; then
  case "$WITH_AGENT" in
    claude)   NPM_PKG="@anthropic-ai/claude-code" ;;
    codex)    NPM_PKG="@openai/codex" ;;
    opencode) NPM_PKG="opencode-ai" ;;
    copilot)  NPM_PKG="@github/copilot" ;;
    gemini)   NPM_PKG="@google/gemini-cli" ;;
    *)        die "未知 agent：$WITH_AGENT（可选 claude / codex / opencode / copilot / gemini）" ;;
  esac

  if need_cmd npm; then
    info "安装 $WITH_AGENT（$NPM_PKG）…"
    if npm install -g "$NPM_PKG"; then
      ok "$WITH_AGENT 安装完成"
    else
      warn "$WITH_AGENT 安装失败（可能需要 sudo 或 npm 权限配置）。可稍后手动安装：npm i -g $NPM_PKG"
    fi
  else
    warn "未找到 npm，跳过。装好 Node.js 后执行：npm i -g $NPM_PKG"
  fi
else
  info "未指定 --with-agent，跳过（可在网页里看各 CLI 的安装提示）"
  info "常见选择：claude / codex / opencode / copilot / gemini"
fi

# ---------------------------------------------------------------- 5) Ollama
if [ "$WITH_OLLAMA" -eq 1 ]; then
  step "Ollama（本地模型）"
  if need_cmd ollama; then
    ok "已安装：$(ollama --version 2>/dev/null | head -1)"
  elif confirm "安装 Ollama 以运行本地模型？"; then
    curl -fsSL https://ollama.com/install.sh | sh && ok "Ollama 安装完成" \
      || warn "Ollama 安装失败，可手动安装：https://ollama.com"
  fi
fi

# ---------------------------------------------------------------- 6) 自检
step "自检"
if need_cmd tmux; then
  ok "编排功能：可用"
else
  warn "编排功能：不可用（缺 tmux）"
fi
[ -f "$INSTALL_DIR/static/index.html" ] && ok "前端文件：完整" || warn "前端文件：缺失"
[ -f "$INSTALL_DIR/core/orchestrator.py" ] && ok "后端模块：完整" || warn "后端模块：缺失"

# 跑一次性能评估，确认模块能正常工作
info "试跑一次本机性能评估…"
if "$PY" -c "import sys; sys.path.insert(0,'.'); from core.benchmark import collect; \
p=collect(run_bench=False); r=p['recommendation']; \
print('     评估完成：等级 %s，推荐 %d 个 agent，瓶颈：%s' % (r['grade'], r['recommended'], r['bottleneck']))"; then
  ok "性能评估模块：正常"
else
  warn "性能评估模块异常（不影响启动，但评分可能不准）"
fi

# ---------------------------------------------------------------- 7) 完成
step "安装完成"
cat <<EOF

  启动控制台：
      cd $INSTALL_DIR
      python3 server.py --port $PORT --open

  然后浏览器会自动打开；手动访问：
      http://localhost:$PORT

  常用参数：
      --port 9000            换端口
      --allow-external       允许局域网其他机器访问
      --open                 启动后自动开浏览器

  停止：Ctrl+C
EOF

if confirm "现在就启动控制台？"; then
  exec "$PY" server.py --port "$PORT" --open
fi
