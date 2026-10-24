#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dcnews 命令行工具入口。

用法示例：
    python dcnews.py start              # 从现在开始后台监听
    python dcnews.py start 20251010     # 从 2025-10-10 起补爬后再监听
    python dcnews.py weekly             # 导出最近一周 Excel
    python dcnews.py latest             # 终端列出最近 10 条新闻
"""
import sys

from dcnews.cli import main

if __name__ == "__main__":
    sys.exit(main())
