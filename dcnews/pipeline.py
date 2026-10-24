# -*- coding: utf-8 -*-
"""流程编排：单周期监听抓取、按日期区间补爬。"""
import logging
from datetime import datetime, timedelta

from . import config, db, sources
from .enrich import enrich_pending
from .keywords import match_units, match_category, normalize_unit

log = logging.getLogger("dcnews")

# 常态监听时，搜索型源只取最近 N 天（避免把多年前的旧闻灌入库）
MONITOR_WINDOW_DAYS = 3


def _store_items(items):
    """甄别 + 入库。返回 (新入库条数, 命中但重复条数, 命中统计, 新条目列表)。"""
    hits, per_source = sources.filter_dongcheng(items)
    new, dup = 0, 0
    inserted = []
    for it in hits:
        # 入库即基于标题+摘要计算单位与分类（详情富化后再升级）
        it.setdefault("units", match_units(it["title"], it.get("summary", "")))
        it.setdefault("category",
                      match_category(it["title"], it.get("summary", "")))
        ok = db.insert_news(it)
        if ok:
            new += 1
            inserted.append(it)
        else:
            dup += 1
    return new, dup, per_source, inserted


def run_cycle():
    """执行一个监听周期：全部数据源 → 甄别 → 入库 → 富化 → 心跳。"""
    started = datetime.now()
    since = (started - timedelta(days=MONITOR_WINDOW_DAYS)).strftime("%Y-%m-%d")
    items, stats = sources.run_all(date_from=since)
    new, dup, per_source, inserted = _store_items(items)

    # 对新入库且缺摘要/日期的条目做详情页富化；顺带消化历史积压
    enriched = enrich_pending(inserted, limit=config.ENRICH_LIMIT_PER_SOURCE)
    try:
        backlog = db.get_need_enrich(20)
        enriched += enrich_pending(backlog, limit=20)
    except Exception:
        pass

    total = db.count_news()
    db.set_state("last_heartbeat", started.strftime("%Y-%m-%d %H:%M:%S"))
    db.set_state("last_cycle_new", str(new))
    log.info(
        "HEARTBEAT cycle_at=%s new=%d dup=%d enriched=%d total=%d "
        "per_source=%s fetch_stats=%s",
        started.strftime("%Y-%m-%d %H:%M:%S"), new, dup, enriched, total,
        per_source, stats,
    )
    return {"new": new, "dup": dup, "enriched": enriched, "total": total}


def run_backfill(date_from: str, date_to: str = None):
    """按日期区间补爬（断点续爬/指定起点）。

    搜索型源按日期过滤并翻 config.SEARCH_MAX_PAGES 页；中新网按日滚动；
    电子报按版面回溯（上限 config.EPAPER_MAX_BACKFILL_DAYS 天）。
    重复执行是幂等的，中断后重新运行不会产生重复数据。
    """
    date_to = date_to or datetime.now().strftime("%Y-%m-%d")
    log.info("BACKFILL start date_from=%s date_to=%s", date_from, date_to)
    total_new = 0
    # 1) 搜索型源
    for src in sources.SEARCH_SOURCES:
        try:
            items = src.search(date_from, date_to,
                               max_pages=config.SEARCH_MAX_PAGES)
        except Exception as e:
            log.warning("补爬源 %s 失败: %s", src.key, e)
            continue
        new, dup, _, inserted = _store_items(items)
        enrich_pending(inserted, limit=60)
        total_new += new
        log.info("BACKFILL source=%s candidates=%d new=%d dup=%d",
                 src.key, len(items), new, dup)
    # 2) 中新网滚动（按日回溯）
    try:
        cn = sources.ChinanewsScroll()
        items = cn.search(date_from, date_to)
        new, dup, _, inserted = _store_items(items)
        enrich_pending(inserted, limit=60)
        total_new += new
        log.info("BACKFILL source=chinanews candidates=%d new=%d dup=%d",
                 len(items), new, dup)
    except Exception as e:
        log.warning("中新网补爬失败: %s", e)
    # 3) 电子报源（纸媒版面按日回溯：京报集团数字报、人民日报、光明日报等）
    try:
        ep_items = []
        for src in sources.EPAPER_SOURCES:
            try:
                if hasattr(src, "search"):
                    ep_items.extend(src.search(date_from, date_to))
                else:  # 列表型（如北晚在线）走 fetch + 日期过滤
                    ep_items.extend(src.fetch(date_from, date_to))
            except Exception as e:
                log.warning("电子报补爬 %s 失败: %s", src.key, e)
        new, dup, _, inserted = _store_items(ep_items)
        enrich_pending(inserted, limit=60)
        total_new += new
        log.info("BACKFILL source=epapers candidates=%d new=%d dup=%d",
                 len(ep_items), new, dup)
    except Exception as e:
        log.warning("电子报补爬失败: %s", e)
    # 4) 列表型源当前页（尽量补齐近端数据）
    try:
        items, _ = sources.run_all_list_only()
        new, dup, _, inserted = _store_items(items)
        total_new += new
    except Exception as e:
        log.warning("列表源补爬失败: %s", e)

    total = db.count_news()
    log.info("BACKFILL done date_from=%s date_to=%s new_total=%d db_total=%d",
             date_from, date_to, total_new, total)
    return total_new


def detect_interrupt(interval_min=None):
    """通过数据库心跳 + 运行日志判断上次运行是否中断。

    返回 (中断与否, 说明, 建议补爬起点 'YYYY-MM-DD' 或 None)。
    """
    interval_min = interval_min or config.DEFAULT_INTERVAL_MIN
    now = datetime.now()
    last = None

    # 1) 数据库心跳
    hb = db.get_state("last_heartbeat")
    if hb:
        try:
            last = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    # 2) 日志尾部时间戳（以更晚者为准）
    log_last = _log_last_timestamp()
    if log_last and (last is None or log_last > last):
        last = log_last

    if last is None:
        return False, "首次运行，无历史心跳记录。", None

    gap = (now - last).total_seconds()
    threshold = interval_min * 60 * config.INTERRUPT_FACTOR
    if gap > threshold:
        # 补爬起点：上次心跳（并限制最大回溯窗口）
        floor = now.timestamp() - config.MAX_BACKFILL_DAYS * 86400
        start_ts = max(last.timestamp(), floor)
        d = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        hours = gap / 3600.0
        return True, (f"检测到运行中断：上次心跳 {last:%Y-%m-%d %H:%M:%S}，"
                      f"距现在 {hours:.1f} 小时。"), d
    return False, f"心跳正常（上次 {last:%Y-%m-%d %H:%M:%S}）。", None


def _log_last_timestamp():
    import os
    path = config.LOG_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 20000))
            tail = f.read().decode("utf-8", "ignore")
        for line in reversed(tail.splitlines()):
            m = __import__("re").match(
                r"(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line.strip())
            if m:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def run_window(days: int = 7) -> int:
    """按最近 N 天窗口拉取数据（download 命令使用）。

    等价于以 today-(days-1) 为起点的定向补爬：
    搜索型源按日期过滤 + 列表源全量甄别 + 入库去重 + 详情富化。
    返回本轮新入库条数。
    """
    d_to = datetime.now().strftime("%Y-%m-%d")
    d_from = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    log.info("WINDOW fetch days=%d date_from=%s date_to=%s",
             days, d_from, d_to)
    added = run_backfill(d_from, d_to)
    return added
