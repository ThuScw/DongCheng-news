# -*- coding: utf-8 -*-
"""SQLite 存储层：新闻条目、运行状态（心跳）、去重。"""
import os
import re
import sqlite3
import hashlib
import threading
from datetime import datetime

from . import config

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """每个线程一个连接。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS news (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash    TEXT UNIQUE NOT NULL,
            url         TEXT,
            title       TEXT NOT NULL,
            media       TEXT,
            source_key  TEXT,
            pub_date    TEXT,
            pub_time    TEXT,
            summary     TEXT,
            keyword     TEXT,
            match_level INTEGER,
            units       TEXT,
            category    TEXT,
            fetched_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pub_date ON news(pub_date);
        CREATE INDEX IF NOT EXISTS idx_source  ON news(source_key);
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()


def url_key(url: str) -> str:
    """URL 去重键：去掉 query/fragment 的规整化哈希。"""
    u = re.sub(r"[?#].*$", "", (url or "").strip())
    return hashlib.md5(u.encode("utf-8")).hexdigest()


def insert_news(item: dict) -> bool:
    """插入一条新闻；已存在返回 False。

    item 字段：url/title/media/source_key/pub_date/pub_time/summary/
               keyword/match_level/units(list)/category
    """
    conn = get_conn()
    h = url_key(item.get("url", ""))
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO news
        (url_hash, url, title, media, source_key, pub_date, pub_time,
         summary, keyword, match_level, units, category, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            h, item.get("url", ""), item.get("title", ""),
            item.get("media", ""), item.get("source_key", ""),
            item.get("pub_date", ""), item.get("pub_time", ""),
            item.get("summary", ""), item.get("keyword", ""),
            int(item.get("match_level", 0)),
            "、".join(item.get("units", []) or []),
            item.get("category", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def exists_news(url: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM news WHERE url_hash=?", (url_key(url),)
    ).fetchone()
    return row is not None


def count_news() -> int:
    return get_conn().execute("SELECT COUNT(*) c FROM news").fetchone()["c"]


# ---------------- 运行状态 ----------------
def set_state(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_state(key: str, default=None):
    row = get_conn().execute(
        "SELECT value FROM state WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else default


# ---------------- 查询 ----------------
def latest_news(n=10):
    return get_conn().execute(
        """
        SELECT * FROM news
        ORDER BY COALESCE(NULLIF(pub_time,''), pub_date, fetched_at) DESC,
                 id DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()


def news_between(d_from: str, d_to: str):
    """按发布日期闭区间查询（含无日期条目的替代口径见调用方）。"""
    return get_conn().execute(
        """
        SELECT * FROM news
        WHERE pub_date >= ? AND pub_date <= ?
        ORDER BY pub_date ASC, media ASC, id ASC
        """,
        (d_from, d_to),
    ).fetchall()


def update_enrichment(url: str, summary: str, units: list, category: str,
                      pub_date: str = "", pub_time: str = ""):
    """文章详情富化后回写。"""
    conn = get_conn()
    conn.execute(
        """
        UPDATE news SET summary=COALESCE(NULLIF(?,''),summary),
                        units=COALESCE(NULLIF(?,''),units),
                        category=COALESCE(NULLIF(?,''),category),
                        pub_date=COALESCE(NULLIF(?,''),pub_date),
                        pub_time=COALESCE(NULLIF(?,''),pub_time)
        WHERE url_hash=?
        """,
        (summary or "", "、".join(units or []) or "", category or "",
         pub_date or "", pub_time or "", url_key(url)),
    )
    conn.commit()


# ---------------- 别名与富化查询 ----------------
get_latest = latest_news          # reporter 使用
query_between = news_between      # exporter 使用


def get_need_enrich(limit=40):
    """取需要详情富化的条目：缺摘要或精确发布日期的最近记录。"""
    return get_conn().execute(
        """
        SELECT url, title, summary, pub_date, pub_time FROM news
        WHERE (summary='' OR summary IS NULL OR pub_date='' OR pub_date IS NULL)
          AND url IS NOT NULL AND url != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def query_by_date_range(d_from, d_to):
    """exporter 兼容名：按发布日期闭区间查询。"""
    return news_between(d_from, d_to)


def query_no_date():
    """无发布日期的条目（导出时用抓取日期兜底）。"""
    return get_conn().execute(
        "SELECT * FROM news WHERE pub_date='' OR pub_date IS NULL "
        "ORDER BY id DESC"
    ).fetchall()


def reset_conn():
    """fork 后调用：关闭继承的连接并置空，强制线程重建新连接。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
