# -*- coding: utf-8 -*-
"""dcnew 命令行入口（北京市东城区新闻监测工具）。

命令体系：
  dcnew start [YYYYMMDD]   启动后台监听（可选起点日期，默认从现在开始）
  dcnew stop               停止后台监听
  dcnew status             状态查询（PID/心跳/中断提示/数据量）
  dcnew crawl              前台立即执行一轮抓取（调试用）
  dcnew download [d]       拉取最近 d 天数据并生成 Excel（默认 7 天=一周）
  dcnew latest [d]         终端简洁列举最近 d 条新闻（默认 10 条）
  dcnew doctor [d]         并发体检全部 57 源（不写库；d=回看天数，默认 3）
"""
import argparse
import logging
import re
import sys

from . import __version__, daemon


def build_parser():
    p = argparse.ArgumentParser(
        prog="dcnew",
        description="北京市东城区新闻监测爬取 CLI 工具"
                    "（核心引擎：Scrapling + GNE，57 个数据源）")
    p.add_argument("--version", action="version",
                   version=f"dcnew {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser(
        "start", help="启动后台监听爬取（不加日期默认从现在开始；"
                      "加日期如 20251010 则从该日起补爬后再监听）")
    sp.add_argument("date", nargs="?", default=None,
                    help="起始日期 YYYYMMDD（可选）")
    sp.add_argument("--interval", type=int, default=None,
                    help="轮询间隔（分钟，默认 20）")

    sub.add_parser("stop", help="停止后台监听")
    sub.add_parser("status", help="状态查询")
    sub.add_parser("crawl", help="前台立即执行一轮抓取")

    sp = sub.add_parser(
        "download", help="从数据库导出最近 d 天数据并生成 Excel 周报"
                         "（默认 d=7；秒级出结果，不联网）")
    sp.add_argument("days", nargs="?", type=int, default=7,
                    help="天数 d（默认 7）")
    sp.add_argument("--out", default=None, help="输出 Excel 路径（可选）")
    sp.add_argument("--refresh", action="store_true",
                    help="导出前先联网抓取一轮入库（默认不抓取，"
                         "直接用数据库现有数据）")

    sp = sub.add_parser(
        "latest", help="终端简洁列举最近 d 条新闻（默认 10 条）")
    sp.add_argument("num", nargs="?", type=int, default=10,
                    help="条数 d（默认 10）")

    sp = sub.add_parser(
        "doctor", help="并发体检全部数据源（不写库，约 30 秒）")
    sp.add_argument("days", nargs="?", type=int, default=3,
                    help="回看窗口天数（默认 3）")

    return p


_DATE_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "start":
        date_arg = args.date
        if date_arg:
            m = _DATE_RE.match(date_arg)
            if not m:
                print("日期格式应为 YYYYMMDD，例如 20251010。")
                return 2
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                import datetime as _dt
                _dt.date(y, mo, d)
            except ValueError:
                print(f"日期无效：{date_arg}")
                return 2
            date_arg = f"{y:04d}-{mo:02d}-{d:02d}"
        return daemon.start(date_arg, args.interval)

    if args.cmd == "stop":
        return daemon.stop()
    if args.cmd == "status":
        return daemon.status()

    if args.cmd == "crawl":
        from . import db, pipeline
        db.init_db()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S")
        r = pipeline.run_cycle()
        print(f"本轮完成：候选甄别后新增 {r['new']} 条"
              f"（重复 {r['dup']}），详情富化 {r['enriched']} 条，"
              f"库内累计 {r['total']} 条。")
        return 0

    if args.cmd == "download":
        from . import db
        from .exporter import export_weekly
        days = max(1, min(args.days, 400))
        db.init_db()
        if args.refresh:
            from . import pipeline
            print(f"--refresh：先抓取最近 {days} 天数据入库……")
            added = pipeline.run_window(days)
            print(f"本轮抓取入库 {added} 条新数据。")
        else:
            print(f"从数据库导出最近 {days} 天数据"
                  f"（如需先联网抓取一轮，可加 --refresh）……")
        path = export_weekly(days=days, out_path=args.out)
        return 0 if path else 1

    if args.cmd == "latest":
        from .reporter import show_latest
        return show_latest(max(1, min(args.num, 100)))

    if args.cmd == "doctor":
        from .health import check_all, format_report
        results, summary = check_all(
            window_days=max(1, min(args.days, 30)))
        print(format_report(results, summary))
        return 1 if summary["failed"] else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
