# -*- coding: utf-8 -*-
"""文章详情富化：抓取文章页 → 提取发布时间/正文 → 单位与分类。

正文提取使用 GNE（GeneralNewsExtractor，
https://github.com/GeneralNewsExtractor/GeneralNewsExtractor）。
"""
import re
from datetime import datetime

from . import config, db, net
from .keywords import match_units, match_category

try:
    from gne import GeneralNewsExtractor
    _gne = GeneralNewsExtractor()
except Exception:  # GNE 未安装时降级为纯文本提取
    _gne = None


DATE_PATTERNS = [
    r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})[日]?[ Tt]{0,2}(\d{1,2})?[:点时]?(\d{1,2})?",
]


def extract_datetime(text: str):
    """从文本中提取首个时间：返回 (date 'YYYY-MM-DD', time 'YYYY-MM-DD HH:MM') 或 (None, None)。"""
    if not text:
        return None, None
    for pat in DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh = int(m.group(4)) if m.group(4) else None
            mi = int(m.group(5)) if m.group(5) else 0
            if not (2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            try:
                dt = datetime(y, mo, d)
            except ValueError:
                continue
            date_s = dt.strftime("%Y-%m-%d")
            if hh is not None and 0 <= hh <= 23:
                time_s = f"{date_s} {hh:02d}:{mi:02d}"
            else:
                time_s = date_s
            return date_s, time_s
    return None, None


def parse_date_from_url(url: str):
    """从 URL 路径提取日期（各门户常见模式）。"""
    if not url:
        return None, None
    m = re.search(r"20(\d{2})[/-]?(\d{2})[/-]?(\d{2})", url)
    if m:
        y, mo, d = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return (datetime(y, mo, d).strftime("%Y-%m-%d"),) * 2
            except ValueError:
                pass
    return None, None


# 署名区标记：出现在页面文本或正文尾部时，其后 60 字视作署名区
_BYLINE_MARKERS = (
    "通讯员", "记者 ", "记者：", "记者:", "文/", "摄影/", "供图：",
    "供图:", "供稿：", "供稿:", "来源：", "来源:", "署名：",
)


def extract_byline(author: str, page_text: str, content: str) -> str:
    """汇总署名区文本：GNE 作者字段 + 各署名标记后的片段。

    只供 match_units 做词典命中（人名不会被词典收录，天然过滤噪音）。
    """
    parts = []
    if author:
        parts.append(author)
    for src_text in (page_text, content):
        if not src_text:
            continue
        for m in _BYLINE_MARKERS:
            start = 0
            while True:
                i = src_text.find(m, start)
                if i < 0:
                    break
                parts.append(src_text[i:i + 60])
                start = i + len(m)
    return " ".join(parts)[:400]


def enrich_article(url: str, title: str, summary: str = ""):
    """抓取文章页，提取发布时间、正文摘要。

    返回 dict：{summary, units, category, pub_date, pub_time, content}
    失败时返回空 dict（调用方保留原值）。
    """
    out = {"summary": "", "units": [], "category": "",
           "pub_date": "", "pub_time": "", "content": ""}
    if net.referer_for(url):
        # 电子报站点（需 Referer，普通 fetch_page 会 403）
        text, page = net.fetch_page_with_referer(url, retries=1)
        if page is None:
            return out
        html = getattr(page, "html_content", "") or text or ""
        text_all = page.get_all_text() or ""
    else:
        page = net.fetch_page(url, retries=1)
        if page is None:
            return out
        html = page.html_content or ""
        text_all = page.get_all_text() or ""

    # 1) 时间：meta 标签优先，其次全文正则，再次 URL
    d_t = ("", "")
    meta_date = ""
    for meta in page.css("meta"):
        name = (meta.attrib.get("name", "") or meta.attrib.get("property", "")).lower()
        if name in ("publishdate", "publish-date", "article:published_time",
                    "pubdate", "date", "weibo:article:create_at"):
            meta_date = meta.attrib.get("content", "")
            break
    if meta_date:
        d_t = extract_datetime(meta_date) or ("", "")
    if d_t[0] is None:
        # 发布时间通常出现在正文前部
        head = text_all[:3000]
        m = re.search(r"(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}[日]?\s*\d{0,2}[:点时]?\d{0,2})", head)
        d_t = extract_datetime(m.group(1) if m else head) or (None, None)
    if d_t[0]:
        out["pub_date"], out["pub_time"] = d_t[0], d_t[1]
    else:
        d_t = parse_date_from_url(url)
        if d_t[0]:
            out["pub_date"], out["pub_time"] = d_t[0], d_t[1]

    # 2) 正文：GNE 优先
    content = ""
    author = ""
    if _gne is not None:
        try:
            res = _gne.extract(html, noise_node_list=["//div[@class='comment']"])
            content = (res.get("content") or "")[:3000]
            author = (res.get("author") or "")[:60]
        except Exception:
            content = ""
    if not content:
        content = text_all[:3000]
    out["content"] = content

    # 3) 摘要：优先 meta description，其次正文前 120 字
    desc = ""
    for meta in page.css("meta"):
        if (meta.attrib.get("name", "") or "").lower() in ("description", "og:description"):
            desc = net.clean_text(meta.attrib.get("content", ""))
            break
    out["summary"] = summary or desc or content[:120]

    # 4) 单位与分类（结合正文 + 署名区）
    byline = extract_byline(author, text_all, content)
    out["units"] = match_units(title, content, byline=byline)
    out["category"] = match_category(title, content[:600] or summary)
    return out


def enrich_pending(items, limit=None):
    """对已入库但缺摘要/精确时间的条目做详情页富化（带限额）。"""
    limit = limit or config.ENRICH_LIMIT_PER_SOURCE
    done = 0
    for it in items:
        if done >= limit:
            break
        url, title = it["url"], it["title"]
        r = enrich_article(url, title, it["summary"] or "")
        if r["summary"] or r["pub_date"]:
            db.update_enrichment(
                url, r["summary"], r["units"], r["category"],
                r["pub_date"], r["pub_time"],
            )
            done += 1
    return done
