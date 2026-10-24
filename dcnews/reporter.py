# -*- coding: utf-8 -*-
"""终端简洁列表：最近 N 条新闻。"""
from . import db


def show_latest(n=10):
    db.init_db()
    rows = db.get_latest(n)
    if not rows:
        print("暂无数据。可先执行 `python dcnews.py start` 启动监听，"
              "或 `python dcnews.py crawl` 立即抓取一轮。")
        return 0
    print(f"最近 {len(rows)} 条东城区新闻：")
    print("-" * 100)
    print(f"{'#':>3}  {'日期':<10}  {'媒体':<12}  {'标题':<44}  链接")
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        date = (r["pub_time"] or r["fetched_at"] or "")[:10]
        media = (r["media"] or r["source_key"] or "")[:12]
        title = (r["title"] or "").strip()
        if len(title) > 42:
            title = title[:42] + "…"
        url = r["url"] or ""
        print(f"{i:>3}  {date:<10}  {media:<12}  {title:<44}  {url}")
    print("-" * 100)
    total = db.count_news()
    print(f"（库内共 {total} 条，执行 `dcnew download` 可拉取并导出周报 Excel）")
    return 0
