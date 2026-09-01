# Magent Console — 多 Agent 编排控制台

把「同时开 N 个 AI Agent 干活」这件事，从命令行黑箱变成一块看得见的仪表盘。

它基于 tmux 做真实的进程编排：评估你这台机器能扛几个 Agent、按三种范式把它们拉起来、
给每个 Agent 单独写 requirement、实时看每个终端在输出什么、收工后一键清理。

---

## 它解决什么

用 Agent CLI（claude-code / codex / opencode…）并发干活时，你会遇到三个真问题：

1. **开几个合适？** 拍脑袋开 8 个，结果内存爆了 / 屏幕挤成一条缝根本看不清。
2. **怎么给任务？** 一个窗口一个窗口敲，敲到第 5 个时已经忘了前面说了什么。
3. **怎么知道谁在摸鱼？** 切来切去看输出，切完就忘了刚才看的是哪个。

Magent Console 把这三件事变成界面上的三个面板。

---

## 三种编排范式

| 模式 | 适用场景 | 做法 |
|------|---------|------|
| **并行同质**（Best-of-N） | 想让 N 个 Agent 各自做一遍，挑最好的 | 同一份 requirement 广播给所有 pane，开启 `synchronize-panes`，一条指令全员收到 |
| **异质分工** | 一个任务拆成几个角色（写码 / 测试 / review / 文档） | 每个 pane 各领一份不同的 requirement，定向投喂 |
| **Worktree 隔离** | 并发改同一个仓库，不能互相踩文件 | 每个 Agent 分一个独立 `git worktree` + 分支，物理杜绝写冲突 |

> Worktree 隔离是生产环境并发改代码的**必要条件**。多个 Agent 同时写同一个工作目录，
> 后写的会覆盖先写的，而且没有冲突提示——静默丢代码。

---

## 快速开始

```bash
# 1) 装依赖（会自动识别系统装 tmux，可选顺带装 agent CLI）
./install.sh
./install.sh --with-agent=claude        # 顺带装 claude-code
./install.sh --with-agent=opencode --with-ollama

# 2) 启动
python3 server.py --port 8899 --open
```

浏览器打开 `http://localhost:8899`。

**依赖**：Python 3.9+（只用标准库，零 pip 安装）、tmux。就这两个。

---

## 界面三个面板

### 左：本机性能评估
- 实时采集 CPU / 内存 / GPU / 磁盘，给出 **S/A/B/C 等级**与综合评分
- 推荐并行数，并明确告诉你**瓶颈是什么**

推荐逻辑不是拍脑袋，取以下几项的**最小值**：
- 内存：每个 Agent CLI 是个常驻进程，实测 150~600MB，保守按 420MB/个算，并给系统留 2GB
- CPU 核心数：按 0.25 核/个
- 屏幕面积：每个 pane 至少要 80×20 字符才看得清，超出就是挤成一团
- 人类注意力：硬上限 8 个（超过你根本看不过来）

跑**本地模型**（Ollama）时是另一套算法：按显存 / 共享内存算能塞几个 4B-Q4 实例。

### 中：编排配置
- 三张卡片选模式，每张带示意图
- 选 Agent CLI（自动检测已装哪些、哪些没认证）
- 数量滑块——超出推荐值会**变黄警告**，超出硬件上限会**变红**
- 给每个 Agent 单独写 requirement，内置 8 种角色模板（实现 / 测试 / Review / 文档 / 重构 / 优化 / 检索 / 验证）

### 右：实时监控
- 每 2 秒轮询，展示每个 pane 的**实时输出**（可关自动刷新）
- pane 存活状态呼吸点，死了立刻看出来
- 指令框：选「广播全体」或指定某个 pane 定向发送
- Diff 按钮：汇总各 Agent 改了几个文件

---

## 换一台机器跑

`install.sh` 覆盖 macOS / Debian-Ubuntu / Fedora-RHEL / Arch / Alpine / openSUSE / WSL2，
幂等可重复运行：

```bash
curl -fsSL https://raw.githubusercontent.com/dearhans/magent-console/master/install.sh | bash
```

脚本已默认指向本项目仓库（`dearhans/magent-console`，master 分支）。想装自己 fork 的版本，先 `export REPO_URL=https://github.com/<你的账号>/magent-console.git` 再执行上面那条命令。

---

## API

后端是纯标准库 HTTP 服务，前端只是个壳，你可以直接用 API 驱动：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查、tmux 版本 |
| GET | `/api/profile?cols=&rows=&bench=` | 性能评估与推荐（cols/rows 由前端上报，用于屏幕约束） |
| GET | `/api/agents` | Agent CLI 注册表与安装状态 |
| GET | `/api/sessions` | tmux 会话列表 |
| GET | `/api/status?session=&lines=` | 各 pane 实时状态与输出 |
| GET | `/api/diff?session=` | 各 pane 的 git 改动汇总 |
| POST | `/api/launch` | 启动编排 |
| POST | `/api/send` | 向某个 pane 或全体发指令 |
| POST | `/api/stop` | 停止会话并清理 worktree |
| POST | `/api/grab` | 把某个 pane 的输出落盘 |

启动编排示例：

```bash
curl -X POST http://localhost:8899/api/launch \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "heterogeneous",
    "agent": "claude",
    "count": 3,
    "repo": "/path/to/git/repo",
    "tasks": [
      {"name": "实现", "text": "实现 foo 函数，附测试"},
      {"name": "审查", "text": "审查改动，只报问题不改代码"},
      {"name": "文档", "text": "补 README 用法示例"}
    ],
    "broadcast": false
  }'
```

---

## 项目结构

```
magent-console/
├── server.py               # HTTP 服务与路由（纯标准库）
├── install.sh              # 跨机器一键安装
├── core/
│   ├── benchmark.py        # 硬件采集 + 并行数推荐引擎
│   ├── agents.py           # Agent CLI 注册表与可用性探测
│   ├── tmuxctl.py          # tmux 命令封装
│   └── orchestrator.py     # 三种范式编排 / 状态 / 清理
└── static/
    ├── index.html
    ├── style.css           # 设计系统：Apple HIG × Material 3
    └── app.js
```

---

## 设计说明

界面不是凭手感画的，是 **Apple HIG** 与 **Material Design 3** 的交叉融合：

| 维度 | 取自 HIG | 取自 M3 |
|------|---------|--------|
| 圆角 | 连续圆角（squircle 观感） | 形状比例分级 |
| 色彩 | 克制的中性色、语义化层级 | primary / surface / outline 语义角色 |
| 交互 | 毛玻璃材质、景深层次 | State Layer 状态叠层（hover/press 叠加当前色） |
| 阴影 | — | Elevation 四级阴影层级 |
| 组件 | 系统字体栈、8pt 留白网格 | Filled / Tonal / Outlined / Text 四类按钮、Chips、Snackbar |
| 动效 | 0.2~0.5s 时长 | Emphasized 缓动曲线 |

亮/暗双主题，选择记在 localStorage。

---

## 已知边界

- **tmux 是硬依赖**。没有 tmux 时控制台能打开（可看性能评估），但编排功能不可用。
- **Windows 原生环境不支持**（tmux 需要 Unix）。请走 WSL2——控制台监听 127.0.0.1，
  Windows 浏览器可直接访问 `localhost:8899`。
- **Worktree 模式要求目标目录是 git 仓库**，且磁盘要够（每个 Agent 一份工作副本）。
- Agent CLI 的**认证要你自己完成**（OAuth / API Key）。控制台只负责检测和提示，不代管凭据。
- 轮询间隔 2 秒，Agent 数量多时若觉得卡可关掉自动刷新。
