# -*- coding: utf-8 -*-
"""dcnew doctor 源健康检查模块。

对全部数据源做一次并发探测（不写库），返回每源状态，
用于运维自查：哪些源存活、哪些无产出、哪些异常。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from . import sources

log = logging.getLogger("dcnews")


def check_all(window_days=3, workers=16):
    """并发探测全部数据源。

    返回 (results, summary)：
      results: [{key, count, seconds, error, kind}] 按候选数降序；
      summary: dict(alive=, degraded=, failed=, total=, window=...)
    count == -1 表示异常；count == 0 表示可达但本窗口无产出（degraded）。
    """
    date_from = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    def probe(src):
        t0 = time.time()
        kind = "search" if hasattr(src, "search") else "list"
        try:
            if kind == "search":
                items = src.search(date_from, date_to)
            else:
                items = src.fetch(date_from, date_to)
            # 命中数：经过东城区关键词甄别后的条数，反映该源对本项目的
            # 实际产出能力；候选数多但命中为 0 的源不能算“有效”。
            hits = len(sources.filter_dongcheng(items)[0])
            return {"key": src.key, "kind": kind, "count": len(items),
                    "hits": hits,
                    "seconds": round(time.time() - t0, 1), "error": None}
        except Exception as e:
            return {"key": src.key, "kind": kind, "count": -1, "hits": 0,
                    "seconds": round(time.time() - t0, 1),
                    "error": f"{type(e).__name__}: {e}"}

    raw = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe, s) for s in sources.ALL_SOURCES]
        for f in as_completed(futs):
            try:
                raw.append(f.result())
            except Exception as e:  # 理论上 probe 已兜底
                raw.append({"key": "?", "kind": "?", "count": -1,
                            "seconds": 0.0, "error": str(e)})

    results = sorted(raw, key=lambda r: (-r["hits"], -r["count"], r["key"]))
    failed = [r for r in results if r["error"]]
    degraded = [r for r in results if not r["error"] and r["count"] == 0]
    with_hits = [r for r in results if r.get("hits", 0) > 0]
    summary = {
        "total": len(results),
        "alive": len(results) - len(failed),
        "degraded": len(degraded),
        "failed": len(failed),
        "with_hits": len(with_hits),
        "window": f"{date_from} ~ {date_to}",
    }
    log.info("DOCTOR window=%s alive=%d degraded=%d failed=%d with_hits=%d",
             summary["window"], summary["alive"],
             summary["degraded"], summary["failed"], summary["with_hits"])
    return results, summary


def format_report(results, summary):
    """渲染终端报告（含返回码语义说明）。"""
    lines = []
    lines.append(f"源健康检查 · 窗口 {summary['window']}"
                 f"（探测 {summary['total']} 源，不写库）")
    lines.append(f"{'源':<22} {'类型':<7} {'候选数':>6} {'命中数':>6}"
                 f" {'耗时s':>6}  状态")
    lines.append("-" * 70)
    for r in results:
        if r["error"]:
            mark = f"× {r['error'][:40]}"
        elif r["count"] == 0:
            mark = "△ 可达但本窗口无候选"
        elif r.get("hits", 0) == 0:
            mark = "△ 可达，但关键词零命中"
        else:
            mark = "√"
        lines.append(f"{r['key']:<22} {r['kind']:<7} "
                     f"{max(r['count'], 0):>6} {r.get('hits', 0):>6}"
                     f" {r['seconds']:>6}  {mark}")
    lines.append("-" * 70)
    lines.append(f"存活 {summary['alive']} / {summary['total']}"
                 f"（有命中 {summary['with_hits']}，无候选 {summary['degraded']}，"
                 f"异常 {summary['failed']}）")
    lines.append("说明：'候选数'是解析出的链接数，'命中数'是经东城区关键词"
                 "甄别后的条数；后者为 0 说明该源对本项目暂无有效产出。")
    return "\n".join(lines)
