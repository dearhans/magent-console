# Magent Console — Multi-Agent Orchestration Console

Turns "run N AI agents at once" from a command-line black box into a dashboard you can actually watch.

It orchestrates real processes on top of tmux: benchmark the machine to see how many agents it can carry, spin them up following one of three paradigms, hand each agent its own requirement, watch every terminal's live output, and clean up when done.

---

## What it solves

Running agent CLIs (claude-code / codex / opencode…) concurrently hits three real problems:

1. **How many do I open?** You guess 8, then memory blows up or the panes shrink to unreadable slivers.
2. **How do I brief them?** You type into windows one by one, and by the fifth you've forgotten what you told the first.
3. **Which one is stuck?** You cycle through panes checking output and lose track of what you just read.

Magent Console turns those three problems into three panels.

---

## Three orchestration paradigms

| Mode | Use when | How it works |
|------|----------|--------------|
| **Parallel homogeneous** (Best-of-N) | You want N agents to each attempt the same task, then pick the best | One requirement broadcast to every pane, `synchronize-panes` on — one keystroke reaches all |
| **Heterogeneous** | One task split into roles (impl / test / review / docs) | Each pane gets a different requirement, delivered point-to-point |
| **Worktree isolated** | Multiple agents editing the same repo without stomping each other | Each agent gets its own `git worktree` + branch — write conflicts made physically impossible |

> Worktree isolation is a **hard requirement** for concurrent code edits in production.
> Multiple agents writing the same working tree means last-write-wins with no conflict warning —
> code disappears silently.

---

## Quick start

```bash
# 1) Install deps (auto-detects your OS, installs tmux; optionally an agent CLI)
./install.sh
./install.sh --with-agent=claude
./install.sh --with-agent=opencode --with-ollama

# 2) Run
python3 server.py --port 8899 --open
```

Open `http://localhost:8899`.

**Dependencies**: Python 3.9+ (stdlib only, zero `pip install`) and tmux. That's it.

---

## Three panels

### Left — Machine benchmark
- Live CPU / memory / GPU / disk sampling, producing an **S/A/B/C grade** and composite score
- Recommends a concurrency count and tells you **what the bottleneck actually is**

The recommendation is the **minimum** of:
- Memory: each agent CLI is a resident process (measured 150–600MB); conservatively 420MB each, with 2GB reserved for the OS
- CPU cores: 0.25 core per agent
- Screen area: each pane needs at least 80×20 characters to stay readable
- Human attention: hard cap of 8 (beyond that you simply cannot follow)

Running **local models** (Ollama) switches to a different model: how many 4B-Q4 instances fit in VRAM / shared memory.

### Middle — Orchestration config
- Three mode cards, each with a diagram
- Pick the agent CLI (auto-detects which are installed and which lack auth)
- Count slider — turns **amber** past the recommended value, **red** past the hardware limit
- Per-agent requirement editor with 8 built-in role templates (impl / test / review / docs / refactor / optimize / research / verify)

### Right — Live monitor
- 2s polling, showing each pane's **live output** (auto-refresh can be turned off)
- Breathing liveness dot per pane — a dead pane is obvious instantly
- Command box: broadcast to all, or target a single pane
- Diff button: aggregate how many files each agent touched

---

## Deploying on another machine

`install.sh` covers macOS / Debian-Ubuntu / Fedora-RHEL / Arch / Alpine / openSUSE / WSL2, and is idempotent:

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/magent-console/main/install.sh | bash
```

Replace `REPO_URL` at the top of the script with your own repo.

---

## API

The backend is a stdlib HTTP server; the frontend is just a shell. Drive it directly if you prefer:

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/health` | Health check, tmux version |
| GET | `/api/profile?cols=&rows=&bench=` | Benchmark + recommendation (frontend reports cols/rows for the screen constraint) |
| GET | `/api/agents` | Agent CLI registry + install status |
| GET | `/api/sessions` | tmux session list |
| GET | `/api/status?session=&lines=` | Per-pane status and output |
| GET | `/api/diff?session=` | Per-pane git change summary |
| POST | `/api/launch` | Start orchestration |
| POST | `/api/send` | Send a command to one pane or all |
| POST | `/api/stop` | Stop session and clean up worktrees |
| POST | `/api/grab` | Dump a pane's output to disk |

Launch example:

```bash
curl -X POST http://localhost:8899/api/launch \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "heterogeneous",
    "agent": "claude",
    "count": 3,
    "repo": "/path/to/git/repo",
    "tasks": [
      {"name": "impl",   "text": "Implement foo() with tests"},
      {"name": "review", "text": "Review the diff, report only, do not edit"},
      {"name": "docs",   "text": "Add README usage examples"}
    ],
    "broadcast": false
  }'
```

---

## Layout

```
magent-console/
├── server.py               # HTTP server + routing (stdlib only)
├── install.sh              # Cross-machine one-shot installer
├── core/
│   ├── benchmark.py        # Hardware sampling + concurrency recommender
│   ├── agents.py           # Agent CLI registry + availability probing
│   ├── tmuxctl.py          # tmux command wrapper
│   └── orchestrator.py     # Three paradigms / status / cleanup
└── static/
    ├── index.html
    ├── style.css           # Design system: Apple HIG × Material 3
    └── app.js
```

---

## Design notes

The UI is not eyeballed — it is a deliberate blend of **Apple HIG** and **Material Design 3**:

| Dimension | From HIG | From M3 |
|-----------|----------|---------|
| Radius | Continuous corner (squircle feel) | Shape-scale tiers |
| Color | Restrained neutrals, semantic hierarchy | primary / surface / outline semantic roles |
| Interaction | Frosted material, depth layering | State Layer (hover/press overlays current color) |
| Shadow | — | Elevation, four levels |
| Components | System font stack, 8pt spacing grid | Filled / Tonal / Outlined / Text buttons, chips, snackbar |
| Motion | 0.2–0.5s durations | Emphasized easing curve |

Light and dark themes, choice persisted in localStorage.

---

## Known limits

- **tmux is a hard dependency.** Without tmux the console still opens (benchmark works) but orchestration is unavailable.
- **Native Windows is unsupported** (tmux needs Unix). Use WSL2 — the console binds 127.0.0.1 and Windows browsers can reach `localhost:8899`.
- **Worktree mode requires the target to be a git repo**, with enough disk for one working copy per agent.
- **Agent CLI authentication is yours to complete** (OAuth / API key). The console detects and hints; it never handles credentials.
- Polling interval is 2s; if many agents feel laggy, turn off auto-refresh.
