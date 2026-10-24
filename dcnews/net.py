# -*- coding: utf-8 -*-
"""网络层：基于 Scrapling 的页面抓取 + requests 的 JSON API。

Scrapling 为核心抓取引擎（https://github.com/D4Vinci/Scrapling），
其自适应选择器可在页面结构微调时保持解析稳定。
"""
import logging
import threading
import time
import random

import requests
from scrapling import Fetcher

from . import config

# 屏蔽 Scrapling 每次请求的 INFO/WARNING 输出，保持日志干净
logging.getLogger("scrapling").setLevel(logging.ERROR)
logging.getLogger("curl_cffi").setLevel(logging.ERROR)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_local = threading.local()


def get_fetcher() -> Fetcher:
    """每线程一个 Fetcher：doctor 的并发探测不共享连接/会话状态。"""
    fetcher = getattr(_local, "fetcher", None)
    if fetcher is None:
        fetcher = Fetcher()
        _local.fetcher = fetcher
    return fetcher


def fetch_page(url: str, retries: int = 2, timeout: int = None):
    """用 Scrapling 抓取页面，返回 AdaptiveResponse 或 None。"""
    timeout = timeout or config.HTTP_TIMEOUT
    for i in range(retries + 1):
        try:
            page = get_fetcher().get(url, timeout=timeout)
            if page.status and page.status < 400:
                return page
        except Exception:
            if i >= retries:
                return None
        time.sleep(1 + random.random() * 2)  # 重试前退避
    return None


def fetch_page_decoded(url, encoding="gb18030", retries=1, timeout=None):
    """抓取 GBK/GB2312 编码站点：requests 拉取后按指定编码解码，
    返回 Scrapling Selector（保持一致的 CSS 解析体验）。"""
    from scrapling import Selector
    timeout = timeout or config.HTTP_TIMEOUT
    for i in range(retries + 1):
        try:
            r = get_session().get(url, timeout=timeout)
            if r.status_code < 400:
                r.encoding = encoding
                return Selector(content=r.text)
        except Exception:
            if i >= retries:
                return None
        time.sleep(1 + random.random())
    return None


def get_session() -> requests.Session:
    """每线程一个 requests.Session（共享 Session 在并发下不安全）。"""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        _local.session = session
    return session


def get_json(url: str, params=None, post_json=None, retries: int = 2,
             timeout: int = None):
    """请求 JSON API，返回 dict/list 或 None。"""
    timeout = timeout or config.HTTP_TIMEOUT
    for i in range(retries + 1):
        try:
            if post_json is not None:
                r = get_session().post(url, json=post_json, timeout=timeout)
            else:
                r = get_session().get(url, params=params, timeout=timeout)
            if r.status_code < 400:
                return r.json()
        except Exception:
            if i >= retries:
                return None
        time.sleep(1 + random.random() * 2)
    return None


def clean_text(s: str) -> str:
    """清理标题/摘要中的高亮标签与多余空白；去掉 CJK 字符间的空格。

    部分检索接口（如中国日报）会在关键词高亮边界插入空格，
    例如“北京市 东城区 首届”，此处统一规整为“北京市东城区首届”。
    """
    import re
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff，。、；：！？“”（）]) +(?=[\u4e00-\u9fff，。、；：！？“”（）])", "", s)
    return s.strip()


# ---------------- 电子报站点支持 ----------------
# 部分电子报站点（京报集团数字报等）要求请求携带 Referer，否则返回 403。
# 域名（URL 子串）→ Referer 值。fetch_page/enrich 均会自动附加。
REFERER_MAP = {
    "bjrbdzb.bjd.com.cn": "https://bjrbdzb.bjd.com.cn/bjrb/paperindex.htm",
    "bqbdzb.bjd.com.cn": "https://bqbdzb.bjd.com.cn/bjqn/paperindex.htm",
    "bjsbdzb.bjd.com.cn": "https://bjsbdzb.bjd.com.cn/bjsb/paperindex.htm",
    "paper.people.com.cn": "https://paper.people.com.cn/",
    "epaper.gmw.cn": "https://epaper.gmw.cn/",
}


def referer_for(url: str) -> str:
    """返回 URL 对应的 Referer；无匹配返回空字符串。"""
    for host, ref in REFERER_MAP.items():
        if host in url:
            return ref
    return ""


def fetch_page_with_referer(url: str, encoding: str = "",
                            retries: int = 2, timeout: int = None):
    """抓取需要 Referer 的电子报页面。

    返回 (html 文本, Scrapling Selector) 元组；encoding 为空时自动探测
    （UTF-8 优先，失败回退 GB18030）。任一步失败返回 (None, None)。
    """
    from scrapling import Selector
    timeout = timeout or config.HTTP_TIMEOUT
    ref = referer_for(url)
    for i in range(retries + 1):
        try:
            r = get_session().get(
                url, timeout=timeout,
                headers={"Referer": ref} if ref else {})
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            if encoding:
                r.encoding = encoding
                text = r.text
            else:
                text = decode_any(r.content)
            return text, Selector(content=text)
        except Exception:
            if i >= retries:
                return None, None
            time.sleep(1 + random.random() * 2)
    return None, None


def decode_any(raw: bytes) -> str:
    """电子报页面编码探测：BOM/UTF-8 优先，失败回退 GB18030。

    同一电子报站点不同日期模板编码可能不一致（京报集团即如此），
    统一走本函数解码。
    """
    if not raw:
        return ""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "ignore")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "ignore")
