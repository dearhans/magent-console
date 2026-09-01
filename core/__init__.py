#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core — Magent Console 核心模块

    benchmark    本机性能评估与 agent 并行数推荐
    agents       agent CLI 注册表与可用性探测
    tmuxctl      tmux 命令封装
    orchestrator 三种编排模式（parallel / heterogeneous / worktree）
"""

__version__ = "1.0.0"

__all__ = ["benchmark", "agents", "tmuxctl", "orchestrator"]
