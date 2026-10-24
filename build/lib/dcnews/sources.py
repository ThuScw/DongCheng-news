# -*- coding: utf-8 -*-
"""数据源适配器：统一返回候选新闻列表 [{url,title,media,pub_date,pub_time,summary}]。

共 57 个数据源，四类：
  A. 站内搜索型（search()，支持关键词检索与时间过滤，用于补爬）：
     - cctv        央视网（Scrapling 解析服务端渲染搜索页）
     - chinadaily  中国日报网检索 API（JSON）
     - thepaper    澎湃新闻搜索 API（JSON）
  B. 聚合搜索型（全网门户聚合检索）：
     - baidu       百度资讯搜索（Scrapling 解析）
     - toutiao     今日头条搜索（cr-params 卡片解析）
  C. 列表型（频道/首页/按日滚动，常态监听）：
     人民网、新华网、中新网、千龙网、新京报、国际在线、光明网、央广网、
     中工网、中青在线、中国青年网、中国经济网、中国科技网、瞭望、半月谈、
     人民政协网、中国网、海外网、求是网、法治日报、健康报网、北青网、
     首都之窗、凤凰网、界面新闻、大公网、文汇网、网易、搜狐、每日经济新闻、
     人民论坛网、中评网、中国警察网、未来网、中国共产党新闻网、香港商报、
     中国妇女网、证券时报网、东方财富网、民主与法制网、文旅中国、
     中国政府网、京报网（北京日报）、21财经、中国质量新闻网、
     东城区人民政府官网
  D. 电子报型（纸媒版面，按日期可回溯，v1.3.0 新增）：
     - epaper_bjrb / epaper_bjwb / epaper_bjqn  京报集团数字报
       （北京日报、北京晚报、北京青年报；URL 按日期组织，可回溯任意日期）
     - epaper_rmrb  人民日报电子版（人民网数字报）
     - epaper_gmrb  光明日报电子版
     - takefoto     北晚在线（北京晚报官方网站）
     摘要字段写入版面信息（如“【版面】第3版·北京新闻”），
     对应人工统计表的“版面”列。
"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta

from . import config, net
from .enrich import parse_date_from_url
from .keywords import is_dongcheng

# ---------- 统一工具 ----------


def _mk(url, title, media, pub_date="", pub_time="", summary="",
        source_key=""):
    return {
        "url": url.strip(), "title": net.clean_text(title),
        "media": media, "pub_date": pub_date or "",
        "pub_time": pub_time or pub_date or "",
        "summary": net.clean_text(summary), "source_key": source_key,
    }


def _abs(base, href):
    return urllib.parse.urljoin(base, href)


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean_title(raw: str) -> str:
    """列表锚文本规整：去掉时间/互动数字尾巴，剔除过短或无汉字的。"""
    s = net.clean_text(raw or "")
    s = re.sub(r"\s*\d+(?:评论|阅读|浏览|播放|次|人|赞|浏览量)\s*$", "", s)
    s = re.sub(r"\s*(?:\d+天前|\d+小时前|\d+分钟前|刚刚|今天|昨天|前天)\s*$", "", s)
    s = re.sub(r"^\s*(?:视频|图文|直播|专题|海报)\s*[|｜]?\s*", "", s)
    if len(_CJK.findall(s)) < 4:
        return ""
    return s


# =========================================================
# A. 搜索型数据源
# =========================================================

class CctvSearch:
    """央视网（CCTV）站内搜索：服务端渲染，Scrapling 解析。"""
    key = "cctv"
    default_media = "央视网"
    base = "https://search.cctv.com/search.php"
    queries = ["东城区", "北京东城"]

    def search(self, date_from=None, date_to=None, max_pages=3):
        """抓取搜索结果；date_from/date_to 为 'YYYY-MM-DD'，None 表示不限。"""
        results = []
        for kw in self.queries:
            for page_no in range(1, max_pages + 1):
                url = (f"{self.base}?qtext={urllib.parse.quote(kw)}"
                       f"&type=web&sort=time&page={page_no}")
                page = net.fetch_page(url)
                if page is None:
                    break
                items = page.css("li.image")
                if not items:
                    break
                stop = False
                for it in items:
                    h3 = it.css("h3.tit")
                    span = h3[0].find("span") if h3 else None
                    a = (h3[0].find("a") if h3 else None) or \
                        (span.find("a") if span is not None else None)
                    if a is None:
                        continue
                    title = net.clean_text(a.get_all_text() or "")
                    href = ""
                    if span is not None:
                        href = span.attrib.get("lanmu1", "") or ""
                    if not href:
                        href = a.attrib.get("href", "") or ""
                    if href.startswith("link_p.php"):
                        q = urllib.parse.parse_qs(
                            urllib.parse.urlparse(href).query)
                        href = q.get("targetpage", [""])[0]
                    if not (href and title):
                        continue
                    d, t = "", ""
                    tim = it.css("span.tim")
                    if tim:
                        m = re.search(r"(20\d{2}-\d{2}-\d{2})"
                                      r"(?:\s+(\d{2}:\d{2}))?",
                                      tim[0].get_all_text() or "")
                        if m:
                            d = m.group(1)
                            t = (f"{m.group(1)} {m.group(2)}"
                                 if m.group(2) else m.group(1))
                    if not d:
                        d, t = parse_date_from_url(href)
                    if date_from and d and d < date_from:
                        stop = True
                        break
                    if date_to and d and d > date_to:
                        continue
                    src = ""
                    s = it.css("span.src")
                    if s:
                        src = _clean_src(s[0].get_all_text())
                    p = it.css("p.bre")
                    summary = net.clean_text(p[0].get_all_text()) if p else ""
                    results.append(_mk(href, title,
                                       src or self.default_media,
                                       d, t, summary, self.key))
                if stop:
                    break
        return results


_CCTV_CHANNEL_WORDS = {
    "新闻", "体育", "经济", "法治", "教育", "青少", "公益频道", "地方频道",
    "科教", "社会", "军事", "农业", "旅游", "时尚", "综艺", "电影", "电视剧",
    "纪录", "动画", "少儿", "音乐", "戏曲", "文化", "健康", "汽车", "房产",
    "央博", "央视网报", "央视新闻",
}
_MEDIA_HINTS = ("报", "网", "台", "社", "刊", "端", "讯", "视", "广播", "融媒")


def _clean_src(src: str):
    """CCTV 搜索结果的'来源'字段可能是栏目名，归一为媒体名。"""
    src = (src or "").replace("来源：", "").strip()
    if not src:
        return ""
    if src in _CCTV_CHANNEL_WORDS:
        return "央视网"
    if len(src) <= 4 and not any(h in src for h in _MEDIA_HINTS):
        return "央视网"
    return src


class ChinaDailySearch:
    """中国日报网检索 API（JSON）。"""
    key = "chinadaily"
    api = "https://newssearch.chinadaily.com.cn/rest/search"

    def search(self, date_from=None, date_to=None, max_pages=3):
        results = []
        for kw in ("东城区", "北京东城"):
            for page in range(1, max_pages + 1):
                data = net.get_json(
                    self.api,
                    params={"keywords": kw, "page": page, "size": 20,
                            "language": "zh", "type": "article"},
                )
                if not data:
                    break
                items = data.get("content") or []
                if not items:
                    break
                # 注意：该 API 按相关性排序（非时间序），不能用旧日期早停分页，
                # 只做客户端过滤；翻页上限由 max_pages 控制。
                for it in items:
                    d, t = _epoch_ms(it.get("publishTime"))
                    if date_from and d and d < date_from:
                        continue
                    if date_to and d and d > date_to:
                        continue
                    results.append(_mk(
                        it.get("url") or it.get("originUrl") or "",
                        it.get("title") or "",
                        net.clean_text(it.get("source") or "") or "中国日报",
                        d, t, it.get("summary") or it.get("leadinLine") or "",
                        self.key,
                    ))
        return results


def _epoch_ms(v):
    try:
        v = int(v)
        dt = datetime.fromtimestamp(v / 1000.0)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "", ""


class ThepaperSearch:
    """澎湃新闻搜索 API（JSON）。"""
    key = "thepaper"
    api = "https://api.thepaper.cn/search/web/news"

    def search(self, date_from=None, date_to=None, max_pages=3):
        start = end = 0
        if date_from:
            start = int(datetime.strptime(date_from, "%Y-%m-%d")
                        .replace(hour=0, minute=0).timestamp()) * 1000
        if date_to:
            end = int((datetime.strptime(date_to, "%Y-%m-%d")
                       .replace(hour=23, minute=59)).timestamp()) * 1000
        results = []
        for kw in ("北京东城", "东城区"):
            for page in range(1, max_pages + 1):
                data = net.get_json(
                    self.api,
                    post_json={"word": kw, "pageIndex": page, "pageSize": 20,
                               "searchType": 1, "sortType": 1,
                               "startTime": start, "endTime": end},
                )
                if not data:
                    break
                items = (data.get("data") or {}).get("list") or []
                if not items:
                    break
                stop = False
                for it in items:
                    d, t = _parse_pub_time(it.get("pubTime") or "")
                    if date_from and d and d < date_from:
                        continue
                    if date_to and d and d > date_to:
                        continue
                    cont_id = str(it.get("contId") or "")
                    url = (f"https://www.thepaper.cn/newsDetail_forward_"
                           f"{cont_id}" if cont_id else "")
                    summary = net.clean_text(
                        re.sub(r"<[^>]+>", "", str(it.get("summary") or "")))
                    results.append(_mk(url, it.get("name") or "",
                                       "澎湃新闻", d, t, summary, self.key))
                if stop:
                    break
        return results


def _parse_pub_time(v):
    v = str(v or "")
    if not v:
        return "", ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10], (v[:16] if len(v) >= 16 else v[:10])
    m = re.match(r"(\d+)天前", v)
    if m:
        dt = datetime.now() - timedelta(days=int(m.group(1)))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d")
    m = re.match(r"(\d+)小时前", v)
    if m:
        dt = datetime.now() - timedelta(hours=int(m.group(1)))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M")
    if "昨天" in v or "前天" in v:
        dt = datetime.now() - timedelta(days=1 if "昨天" in v else 2)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d")
    if "今天" in v:
        return datetime.now().strftime("%Y-%m-%d"), \
            datetime.now().strftime("%Y-%m-%d %H:%M")
    return "", ""


# =========================================================
# B. 聚合搜索型数据源
# =========================================================

class BaiduSearch:
    """百度资讯搜索：全网新闻聚合（Scrapling 解析服务端渲染页）。

    结果标题真实、链接多为百家号/媒体直链；反爬风险由 fetch_page
    的失败重试与周期容错兜底。
    """
    key = "baidu"
    base = "https://www.baidu.com/s"
    queries = ["北京东城", "东城区"]

    def search(self, date_from=None, date_to=None, max_pages=3):
        results = []
        for kw in self.queries:
            url = (f"{self.base}?wd={urllib.parse.quote(kw)}"
                   f"&tn=news&rtt=4&bsst=1&cl=2")
            page = net.fetch_page(url)
            if page is None:
                continue
            for h3 in page.css("h3"):
                a = h3.find("a")
                if a is None:
                    continue
                title = net.clean_text(a.get_all_text() or "")
                href = a.attrib.get("href", "") or ""
                if not (title and href and len(title) > 8):
                    continue
                if href.startswith("http://www.baidu.com/link"):
                    continue  # 跳过需二次跳转的链接，保留直链
                d, t = "", ""
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", h3.html_content or "")
                if m:
                    d = t = m.group(1)
                if not d:
                    d, t = parse_date_from_url(href)
                if date_from and d and d < date_from:
                    continue
                if date_to and d and d > date_to:
                    continue
                results.append(_mk(href, title, "百度资讯聚合",
                                   d, t, title, self.key))
        return results


class ToutiaoSearch:
    """今日头条搜索（so.toutiao.com）：cr-params 卡片解析 + zlink 解码。"""
    key = "toutiao"
    base = "https://so.toutiao.com/search"
    queries = ["北京东城", "东城区"]

    def search(self, date_from=None, date_to=None, max_pages=3):
        results = []
        seen = set()
        for kw in self.queries:
            url = (f"{self.base}?keyword={urllib.parse.quote(kw)}"
                   f"&pd=information")
            page = net.fetch_page(url)
            if page is None:
                continue
            t = page.html_content or ""
            for m in re.finditer(r"cr-params='(\{\"gid\".{0,600}?)'", t):
                raw = m.group(1)
                tm = re.search(r'"title":"([^"]{8,90})"', raw)
                gm = re.search(r'"gid":"?(\d{10,25})"?', raw)
                um = re.search(r'"url":"([^"]{10,300})"', raw)
                if not tm:
                    continue
                title = net.clean_text(tm.group(1))
                real = ""
                if um and "zlink" in um.group(1):
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlparse(um.group(1)).query)
                    real = urllib.parse.unquote(
                        q.get("h5_url", [""])[0]) or um.group(1)
                elif gm:
                    real = f"https://www.toutiao.com/group/{gm.group(1)}/"
                elif um and um.group(1).startswith("http"):
                    real = um.group(1)
                if not (title and real) or real in seen:
                    continue
                seen.add(real)
                # 时间：卡片内嵌 createTime（epoch 秒）
                d, t_s = "", ""
                cm = re.search(r'"createTime":"?(\d{10})"?', t[m.end():m.end() + 3000])
                if cm:
                    try:
                        dt = datetime.fromtimestamp(int(cm.group(1)))
                        d = dt.strftime("%Y-%m-%d")
                        t_s = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass
                if not d:
                    d, t_s = parse_date_from_url(real)
                if date_from and d and d < date_from:
                    continue
                if date_to and d and d > date_to:
                    continue
                results.append(_mk(real, title, "今日头条聚合",
                                   d, t_s, title, self.key))
        return results


# =========================================================
# C. 列表型数据源（Scrapling CSS 锚点解析）
# =========================================================

GENERIC_ART = re.compile(
    r"\.(?:s?html?|htm)(?:\?|#|$)|/a/\d|/c/\d|/detail/\d|/content_?\d|"
    r"/group/\d|/doc/\d|/article/|/dy/|/news/20")


class ListSource:
    """列表型源基类：Scrapling 解析页面 <a> 锚点，按域名/正则过滤文章链接。

    子类属性：
      key / media / pages    源标识、媒体名、入口页列表
      domain                 URL 必须包含的域名（必填）
      art                    额外的 URL 过滤正则（可选）
    """

    key = ""
    media = ""
    pages = []
    domain = ""
    art = None
    encoding = ""  # 非 UTF-8 站点（如 GBK）填写编码，走 requests 解码
    skip_title_prefix = ("更多", "图片", "视频专题", "点击", "查看", "详情",
                         "[", "专题", "热线", "关于", "登录", "注册")

    def _url_ok(self, url):
        if self.domain and self.domain not in url:
            return False
        if self.art is not None:
            return bool(self.art.search(url))
        return bool(GENERIC_ART.search(url))

    def _get(self, page_url):
        if self.encoding:
            return net.fetch_page_decoded(page_url, self.encoding)
        return net.fetch_page(page_url)

    def fetch(self, date_from=None, date_to=None):
        items, seen = [], set()
        for page_url in self.pages:
            page = self._get(page_url)
            if page is None:
                continue
            for a in page.css("a"):
                href = (a.attrib.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript", "mailto")):
                    continue
                url = _abs(page_url, href)
                if url in seen:
                    continue
                title = _clean_title(a.get_all_text() or "")
                if len(title) < 8 or title.startswith(self.skip_title_prefix):
                    continue
                if not self._url_ok(url):
                    continue
                seen.add(url)
                d, t = parse_date_from_url(url)
                if date_from and d and d < date_from:
                    continue
                if date_to and d and d > date_to:
                    continue
                items.append(_mk(url, title, self.media, d, t, "", self.key))
        return items


class ChinanewsScroll(ListSource):
    """中新网滚动新闻：按日期可回溯，支持补爬。"""
    key = "chinanews"
    media = "中新网"
    domain = "chinanews.com.cn"
    art = re.compile(r"/\w+/20\d{2}/\d{2}-\d{2}/\d+\.shtml")

    def _pages_for(self, date_from, date_to):
        days = []
        if date_from:
            d0 = datetime.strptime(date_from, "%Y-%m-%d")
            d1 = datetime.strptime(date_to, "%Y-%m-%d")
            while d0 <= d1:
                days.append(d0)
                d0 += timedelta(days=1)
        else:
            days = [datetime.now() - timedelta(days=1), datetime.now()]
        return [f"https://www.chinanews.com.cn/scroll-news/"
                f"{d.strftime('%Y')}/{d.strftime('%m%d')}/news.shtml"
                for d in days]

    def fetch(self, date_from=None, date_to=None):
        self.pages = self._pages_for(date_from, date_to or
                                     datetime.now().strftime("%Y-%m-%d"))
        return super().fetch(None, None)

    def search(self, date_from=None, date_to=None):
        return self.fetch(date_from, date_to)


class BjdcGov(ListSource):
    """东城区人民政府官网（政务动态）。境外/受限网络不可达时自动跳过。"""
    key = "bjdc_gov"
    media = "东城区人民政府网"
    pages = [config.BJDC_GOV_URL]
    domain = "bjdc.gov.cn"
    art = re.compile(r"\.(?:s)?html?$|art/\d+")

    def fetch(self, date_from=None, date_to=None):
        # DNS 预检（结果缓存），不可达时静默跳过，避免重试噪音
        import socket
        ok = getattr(BjdcGov, "_dns_ok", None)
        if ok is None:
            try:
                socket.getaddrinfo("www.bjdc.gov.cn", 443,
                                   proto=socket.IPPROTO_TCP)
                ok = True
            except Exception:
                ok = False
                import logging
                logging.getLogger("dcnews").warning(
                    "bjdc_gov: www.bjdc.gov.cn 当前网络不可达，"
                    "本周期跳过（境内网络下自动恢复）")
            BjdcGov._dns_ok = ok
        if not ok:
            return []
        return super().fetch(date_from, date_to)


class MzyfzSource(ListSource):
    """民主与法制网：站点为 GBK 编码，使用 requests 手动解码。"""
    key = "mzyfz"
    media = "民主与法制网"
    pages = ["http://www.mzyfz.com/"]
    domain = "mzyfz.com"

    def fetch(self, date_from=None, date_to=None):
        import requests
        items, seen = [], set()
        for page_url in self.pages:
            try:
                r = net.get_session().get(page_url, timeout=config.HTTP_TIMEOUT)
                r.encoding = "gb18030"
                t = r.text
            except Exception:
                continue
            for m in re.finditer(
                    r'<a[^>]+href="([^"]+)"[^>]*>([^<]{8,80})', t):
                href, raw = m.group(1), m.group(2)
                url = _abs(page_url, href)
                title = _clean_title(raw)
                if url in seen or len(title) < 8:
                    continue
                if not self._url_ok(url):
                    continue
                seen.add(url)
                d, tm = parse_date_from_url(url)
                if date_from and d and d < date_from:
                    continue
                items.append(_mk(url, title, self.media, d, tm, "", self.key))
        return items


# ---------- 列表源定义（46 家） ----------

class PeopleRoot(ListSource):
    """人民网：主站 + 北京频道。"""
    key = "people"
    media = "人民网"
    pages = ["http://www.people.com.cn/", "http://bj.people.com.cn/"]
    domain = "people.com.cn"
    art = re.compile(r"/n\d+/20\d{2}/\d{4}/c\d+-\d+\.html?")


class XinhuaRoot(ListSource):
    """新华网：主站 + 北京频道。"""
    key = "xinhua"
    media = "新华网"
    pages = ["http://www.xinhuanet.com/", "http://www.bj.xinhuanet.com/"]
    domain = "xinhuanet.com"
    art = re.compile(r"xinhuanet\.com/.+\.s?html?$|"
                     r"news\.cn/20\d{6}/[0-9a-f]{16,}/c\.html")


class Qianlong(ListSource):
    """千龙网（北京市官方新闻网站）：首页 + 北京频道。"""
    key = "qianlong"
    media = "千龙网"
    pages = ["https://www.qianlong.com/", "https://beijing.qianlong.com/"]
    domain = "qianlong.com"


class Bjnews(ListSource):
    """新京报网。"""
    key = "bjnews"
    media = "新京报"
    pages = ["https://www.bjnews.com.cn/"]
    domain = "bjnews.com.cn"


class Cri(ListSource):
    """国际在线。"""
    key = "cri"
    media = "国际在线"
    pages = ["https://www.cri.cn/"]
    domain = "cri.cn"
    art = re.compile(r"cri\.cn/20\d{6}/[0-9a-f-]{20,}\.html")


class Gmw(ListSource):
    """光明网（光明日报）。"""
    key = "gmw"
    media = "光明网"
    pages = ["https://www.gmw.cn/"]
    domain = "gmw.cn"
    art = re.compile(r"gmw\.cn/20\d{2}-\d{2}/\d{2}/content_\d+\.htm")


class Cnr(ListSource):
    """央广网（中央人民广播电台，GBK 编码）。"""
    key = "cnr"
    media = "央广网"
    pages = ["https://www.cnr.cn/"]
    domain = "cnr.cn"
    encoding = "gb18030"


class Workercn(ListSource):
    """中工网（工人日报）。"""
    key = "workercn"
    media = "中工网"
    pages = ["https://www.workercn.cn/"]
    domain = "workercn.cn"


class Cyol(ListSource):
    """中青在线（中国青年报）。"""
    key = "cyol"
    media = "中青在线"
    pages = ["https://www.cyol.com/"]
    domain = "cyol.com"


class YouthCn(ListSource):
    """中国青年网（GBK 编码）。"""
    key = "youth"
    media = "中国青年网"
    pages = ["https://www.youth.cn/"]
    domain = "youth.cn"
    encoding = "gb18030"


class CeCn(ListSource):
    """中国经济网（经济日报）。"""
    key = "ce"
    media = "中国经济网"
    pages = ["http://www.ce.cn/"]
    domain = "ce.cn"


class Stdaily(ListSource):
    """中国科技网（科技日报）。"""
    key = "stdaily"
    media = "中国科技网"
    pages = ["https://www.stdaily.com/"]
    domain = "stdaily.com"


class Liaowang(ListSource):
    """瞭望（新华社刊物）。"""
    key = "lw"
    media = "瞭望"
    pages = ["http://lw.xinhuanet.com/"]
    domain = "lw.xinhuanet.com"


class Banyuetan(ListSource):
    """半月谈。"""
    key = "banyuetan"
    media = "半月谈"
    pages = ["http://www.banyuetan.org/"]
    domain = "banyuetan.org"


class Rmzxb(ListSource):
    """人民政协网（人民政协报）。"""
    key = "rmzxb"
    media = "人民政协网"
    pages = ["https://www.rmzxb.com.cn/"]
    domain = "rmzxb.com.cn"


class ChinaCom(ListSource):
    """中国网（国务院新闻办公室对外门户）。"""
    key = "chinacom"
    media = "中国网"
    pages = ["http://www.china.com.cn/"]
    domain = "china.com.cn"


class Haiwainet(ListSource):
    """海外网（人民日报海外版）。"""
    key = "haiwainet"
    media = "海外网"
    pages = ["http://www.haiwainet.cn/"]
    domain = "haiwainet.cn"


class Qstheory(ListSource):
    """求是网（求是杂志）。"""
    key = "qstheory"
    media = "求是网"
    pages = ["http://www.qstheory.cn/"]
    domain = "qstheory.cn"


class LegalDaily(ListSource):
    """法治日报。"""
    key = "legaldaily"
    media = "法治日报"
    pages = ["http://www.legaldaily.com.cn/"]
    domain = "legaldaily.com.cn"


class Jkb(ListSource):
    """健康报网（健康报）。"""
    key = "jkb"
    media = "健康报网"
    pages = ["http://www.jkb.com.cn/"]
    domain = "jkb.com.cn"


class Ynet(ListSource):
    """北青网（北京青年报）。"""
    key = "ynet"
    media = "北青网"
    pages = ["https://www.ynet.com/"]
    domain = "ynet.com"


class BeijingGov(ListSource):
    """首都之窗（北京市人民政府门户网站）。"""
    key = "beijing_gov"
    media = "首都之窗"
    pages = ["https://www.beijing.gov.cn/"]
    domain = "beijing.gov.cn"


class Ifeng(ListSource):
    """凤凰网（凤凰卫视新媒体）。"""
    key = "ifeng"
    media = "凤凰网"
    pages = ["https://www.ifeng.com/"]
    domain = "ifeng.com"
    art = re.compile(r"ifeng\.com/c/\d+|ifeng\.com/a/\d+")


class Jiemian(ListSource):
    """界面新闻。"""
    key = "jiemian"
    media = "界面新闻"
    pages = ["https://www.jiemian.com/"]
    domain = "jiemian.com"


class Takungpao(ListSource):
    """大公网（香港大公报，已迁移至 tkww.hk）。"""
    key = "takungpao"
    media = "大公网"
    pages = ["https://www.tkww.hk/"]
    domain = "tkww.hk"
    art = re.compile(r"tkww\.hk/a/\d{6}/\d{2}/AP[0-9a-f]+\.html")


class Wenweipo(ListSource):
    """文汇网（香港文汇报）。"""
    key = "wenweipo"
    media = "文汇网"
    pages = ["https://www.wenweipo.com/"]
    domain = "wenweipo.com"


class NetEase(ListSource):
    """网易新闻。"""
    key = "163"
    media = "网易"
    pages = ["https://www.163.com/"]
    domain = "163.com"
    art = re.compile(r"163\.com/[a-z]+/(?:dy/)?article/[0-9A-Z]+\.html")


class Sohu(ListSource):
    """搜狐新闻。"""
    key = "sohu"
    media = "搜狐"
    pages = ["https://www.sohu.com/"]
    domain = "sohu.com"
    art = re.compile(r"sohu\.com/a/\d+")


class Nbd(ListSource):
    """每日经济新闻。"""
    key = "nbd"
    media = "每日经济新闻"
    pages = ["https://www.nbd.com.cn/"]
    domain = "nbd.com.cn"


class Rmlt(ListSource):
    """人民论坛网（人民论坛杂志）。"""
    key = "rmlt"
    media = "人民论坛网"
    pages = ["http://www.rmlt.com.cn/"]
    domain = "rmlt.com.cn"


class Crntt(ListSource):
    """中评网（中国评论通讯社）。"""
    key = "crntt"
    media = "中评网"
    pages = ["http://www.crntt.com/"]
    domain = "crntt.com"


class Cpd(ListSource):
    """中国警察网（人民公安报）。"""
    key = "cpd"
    media = "中国警察网"
    pages = ["http://www.cpd.com.cn/"]
    domain = "cpd.com.cn"


class K618(ListSource):
    """未来网（共青团中央网络影视中心）。"""
    key = "k618"
    media = "未来网"
    pages = ["https://www.k618.cn/"]
    domain = "k618.cn"


class CpcPeople(ListSource):
    """中国共产党新闻网（人民网承办）。"""
    key = "cpc_people"
    media = "中国共产党新闻网"
    pages = ["http://cpc.people.com.cn/"]
    domain = "cpc.people.com.cn"


class Hkcd(ListSource):
    """香港商报。"""
    key = "hkcd"
    media = "香港商报"
    pages = ["https://www.hkcd.com/"]
    domain = "hkcd.com"


class Cnwomen(ListSource):
    """中国妇女网（中国妇女报）。"""
    key = "cnwomen"
    media = "中国妇女网"
    pages = ["http://www.cnwomen.com.cn/"]
    domain = "cnwomen.com.cn"


class Stcn(ListSource):
    """证券时报网。"""
    key = "stcn"
    media = "证券时报网"
    pages = ["https://www.stcn.com/"]
    domain = "stcn.com"


class Eastmoney(ListSource):
    """东方财富网。"""
    key = "eastmoney"
    media = "东方财富网"
    pages = ["https://finance.eastmoney.com/"]
    domain = "eastmoney.com"


class Ctnews(ListSource):
    """文旅中国（中国文化报 / 文化和旅游部新闻平台）。"""
    key = "ctnews"
    media = "文旅中国"
    pages = ["https://www.ctnews.com.cn/dongtai/node_1824.html",
             "https://www.ctnews.com.cn/jujiao/node_1823.html",
             "https://www.ctnews.com.cn/"]
    domain = "ctnews.com.cn"
    art = re.compile(r"ctnews\.com\.cn/[a-z]+/content/20\d{2}-\d{2}/"
                     r"\d{2}/content_\d+\.html?|node_\d+\.html")


class GovCn(ListSource):
    """中国政府网（国务院）。"""
    key = "gov_cn"
    media = "中国政府网"
    pages = ["https://www.gov.cn/"]
    domain = "gov.cn"
    art = re.compile(r"gov\.cn/(?:yaowen|zhengce|gongbao|xinwen)/.+"
                     r"content_\d+\.htm")


class Bjd(ListSource):
    """京报网（北京日报）。"""
    key = "bjd"
    media = "京报网"
    pages = ["https://www.bjd.com.cn/"]
    domain = "bjd.com.cn"


class Jj21(ListSource):
    """21财经（21世纪经济报道）。"""
    key = "21jingji"
    media = "21财经"
    pages = ["https://www.21jingji.com/"]
    domain = "21jingji.com"


class Cqn(ListSource):
    """中国质量新闻网（中国质量报）。"""
    key = "cqn"
    media = "中国质量新闻网"
    pages = ["http://www.cqn.com.cn/"]
    domain = "cqn.com.cn"


# =========================================================
# D. 电子报型数据源（纸媒版面，按日期可回溯）
# =========================================================
# 入口均经搜索验证（2026-10）：
#   京报集团数字报  bjrbdzb.bjd.com.cn/bjrb  北京日报
#                   bjrbdzb.bjd.com.cn/bjwb  北京晚报
#                   bqbdzb.bjd.com.cn/bjqn  北京青年报
#     版面页 URL：/mobile/{年}/{YYYYMMDD}/{YYYYMMDD}_m.html（含全部版面与
#     文章列表，静态 HTML；需带 Referer，否则 403）
#   人民日报电子版  paper.people.com.cn/rmrb/pc/layout/{YYYYMM}/{DD}/node_NN.html
#   光明日报电子版  epaper.gmw.cn/gmrb/html/layout/{YYYYMM}/{DD}/node_NN.html
#     （休刊日返回“该日无报纸”提示页，自动跳过）
#   北晚在线        takefoto.cn（北京晚报官网，文章 URL 自带日期）
# 上述电子报均按日期组织 URL，search() 支持任意日期区间的历史补爬。

_ANCHOR_RE = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>',
                        re.S | re.I)
_EDITION_RE = re.compile(r"content_(20\d{6})_\d{3}_\d+\.htm", re.I)

# 京报系数字报结构：
#   <div class="nav-panel-heading" pdf_href="...">第2版 要闻时事</div>
#     <ul><li><a data-href="./20261024_002/content_20261024_002_1.htm">标题</a>
# 版面为 div（非锚点），文章锚点用 data-href 属性。
_BJD_SECTION_RE = re.compile(
    r'<div\s+class="nav-panel-heading"[^>]*>\s*(第\d+版[^<]*)</div>(.*?)(?=<div\s+class="nav-panel-heading"|<div\s+class="elec|$)',
    re.S)

# 版面名含以下关键词时，抓正文页做摘要预览（东城区常出现在正文而非标题）
_BJD_PREVIEW_PAGES = ("北京", "要闻", "头版", "民生", "都市", "社会", "文化",
                      "经济", "时政", "生活")
_BJD_PREVIEW_LIMIT = 12  # 单个版面组最多预览的文章数（控制请求量）


class BjdEpaperSource:
    """京报集团数字报（北京日报/北京晚报/北京青年报）。

    单页即整份报纸：版面块（nav-panel-heading div，"第N版 版名"）内为
    该版文章列表（data-href 锚点）。摘要写入版面信息；北京本地版面
    的文章额外抓正文页取首段作摘要预览（东城区常出现在正文首段，
    仅靠标题会漏报）。同一日期版面进程内缓存（报纸内容不变），
    空结果不缓存（等待当日版上线后自动重试）。
    """

    _cache = {}  # (key, ymd) → items

    def __init__(self, key, host, paper, media):
        self.key = key
        self.media = media
        self._host = host
        self._paper = paper

    def _edition_url(self, d):
        return (f"https://{self._host}/{self._paper}/mobile/"
                f"{d:%Y}/{d:%Y%m%d}/{d:%Y%m%d}_m.html")

    def _body_preview(self, art_url):
        """抓正文页，返回首段文本（约 160 字）；失败返回空。"""
        html, _ = net.fetch_page_with_referer(art_url, retries=1)
        if not html:
            return ""
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = net.clean_text(text)
        # 正文起点：报头署名区“媒体名 | 2026年10月24日”之后
        m = re.search(rf"{self.media}\s*[|｜]\s*20\d{{2}}年", text)
        if m:
            seg = text[m.end():m.end() + 260]
        else:
            seg = text[100:360]
        return seg[:160]

    def _edition(self, d):
        ck = (self.key, d.strftime("%Y%m%d"))
        if ck in BjdEpaperSource._cache:
            return BjdEpaperSource._cache[ck]
        url = self._edition_url(d)
        html, _ = net.fetch_page_with_referer(url)
        if not html or len(html) < 2000:
            return []  # 当日版未上线/休刊：不缓存，下周期重试
        date_s = d.strftime("%Y-%m-%d")
        items, seen = [], set()
        previews = 0
        for sec in _BJD_SECTION_RE.finditer(html):
            page_name = net.clean_text(sec.group(1))
            block = sec.group(2)
            for m in _ANCHOR_RE.finditer(block):
                href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
                title = _clean_title(net.clean_text(inner))
                if not title or not _EDITION_RE.search(href):
                    continue
                art_url = _abs(url, href)
                if art_url in seen:
                    continue
                seen.add(art_url)
                summary = f"【版面】{page_name}"
                if (previews < _BJD_PREVIEW_LIMIT
                        and any(k in page_name for k in _BJD_PREVIEW_PAGES)):
                    body = self._body_preview(art_url)
                    if body:
                        summary = f"【版面】{page_name}｜{body}"
                        previews += 1
                items.append(_mk(art_url, title, self.media, date_s, date_s,
                                 summary, self.key))
        if items:
            BjdEpaperSource._cache[ck] = items
        return items

    def fetch(self, date_from=None, date_to=None):
        """常态监听：取今、昨两天版面（当日版未上线/休刊自动回退）。"""
        out = []
        for off in (0, 1):
            d = datetime.now() - timedelta(days=off)
            for it in self._edition(d):
                if date_from and it["pub_date"] < date_from:
                    continue
                if date_to and it["pub_date"] > date_to:
                    continue
                out.append(it)
        return out

    def search(self, date_from=None, date_to=None, max_days=90):
        """按日期区间回溯版面（历史补爬）。"""
        if not date_from:
            return self.fetch()
        d0 = datetime.strptime(date_from, "%Y-%m-%d")
        d1 = datetime.strptime(date_to or datetime.now().strftime("%Y-%m-%d"),
                               "%Y-%m-%d")
        if (d1 - d0).days > max_days:
            d0 = d1 - timedelta(days=max_days)
        out, seen = [], set()
        while d0 <= d1:
            for it in self._edition(d0):
                if it["url"] not in seen:
                    seen.add(it["url"])
                    out.append(it)
            d0 += timedelta(days=1)
        return out


class _NodeEpaperSource:
    """“版面导航型”电子报基类（人民日报/光明日报）。

    版面页 node_NN.html 含版面导航（第N版：版名）与该版文章列表；
    抓取 node_01 解析出全部版面导航后逐版抓取。
    """

    key = ""
    media = ""
    node_tpl = ""    # 版面页模板，如 "https://.../node_{nn}.html"（nn 零填充位数按站点）
    pad = 2
    art_re = None    # 文章链接正则

    def _node_url(self, d, nn):
        return self.node_tpl.format(
            ymd=d.strftime("%Y%m%d"), y=d.strftime("%Y"),
            m=d.strftime("%m"), dd=d.strftime("%d"), nn=str(nn).zfill(self.pad))

    def _node(self, d, nn):
        """抓取单个版面页，返回 (文章列表, 版面名, 全部版面号列表)。"""
        url = self._node_url(d, nn)
        html, _ = net.fetch_page_with_referer(url)
        if not html:
            return [], "", []
        if "无报纸" in html or "404" in html[:600]:
            return [], "", []
        date_s = d.strftime("%Y-%m-%d")
        items = []
        # 版面导航锚点：href=node_NN.html，文本两种格式——
        # 人民日报“06版：文化遗产”、光明日报“第01版：头版”
        navs = {}
        for m in _ANCHOR_RE.finditer(html):
            href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
            text = net.clean_text(inner)
            nm = re.match(r"第?(\d{1,3})版[:：]?\s*(.{0,20})", text or "")
            if nm and re.search(r"node_?\d*\.html?$", href.strip()):
                navs.setdefault(int(nm.group(1)), net.clean_text(nm.group(2)))
        # 文章锚
        for m in _ANCHOR_RE.finditer(html):
            href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
            text = net.clean_text(inner)
            if not text:
                continue
            if not self.art_re or not self.art_re.search(href):
                continue
            art_url = _abs(url, href)
            title = _clean_title(text)
            if len(title) < 6:
                continue
            items.append(_mk(art_url, title, self.media, date_s, date_s,
                             "", self.key))
        page_name = navs.get(nn, "")
        for it in items:
            it["summary"] = f"【版面】第{nn:02d}版·{page_name}" if page_name \
                else f"【版面】第{nn:02d}版"
        return items, page_name, sorted(navs.keys())

    _cache = {}  # (key, date) → items，同日版面当日不变，进程内缓存

    def _edition(self, d):
        ck = (self.key, d.strftime("%Y%m%d"))
        if ck in _NodeEpaperSource._cache:
            return _NodeEpaperSource._cache[ck]
        items, _, navs = self._node(d, 1)
        all_items = list(items)
        for nn in navs:
            if nn == 1:
                continue
            its, _, _ = self._node(d, nn)
            all_items.extend(its)
        # 去重（导航页可能在多个版面页重复出现）
        seen, uniq = set(), []
        for it in all_items:
            if it["url"] not in seen:
                seen.add(it["url"])
                uniq.append(it)
        # 历史日期（含休刊）直接缓存；当日版未上线不缓存，下周期重试
        if uniq or d.date() < datetime.now().date():
            _NodeEpaperSource._cache[ck] = uniq
        return uniq

    def fetch(self, date_from=None, date_to=None):
        out = []
        for off in (0, 1):
            d = datetime.now() - timedelta(days=off)
            for it in self._edition(d):
                if date_from and it["pub_date"] < date_from:
                    continue
                if date_to and it["pub_date"] > date_to:
                    continue
                out.append(it)
        return out

    def search(self, date_from=None, date_to=None, max_days=90):
        if not date_from:
            return self.fetch()
        d0 = datetime.strptime(date_from, "%Y-%m-%d")
        d1 = datetime.strptime(date_to or datetime.now().strftime("%Y-%m-%d"),
                               "%Y-%m-%d")
        if (d1 - d0).days > max_days:
            d0 = d1 - timedelta(days=max_days)
        out = []
        while d0 <= d1:
            out.extend(self._edition(d0))
            d0 += timedelta(days=1)
        return out


class PeopleEpaper(_NodeEpaperSource):
    """人民日报电子版（人民网数字报）。"""

    key = "epaper_rmrb"
    media = "人民日报"
    pad = 2
    art_re = re.compile(r"content_\d+\.html?")

    def _node_url(self, d, nn):
        return (f"https://paper.people.com.cn/rmrb/pc/layout/"
                f"{d:%Y%m}/{d:%d}/node_{nn:02d}.html")


class GmwEpaper(_NodeEpaperSource):
    """光明日报电子版。"""

    key = "epaper_gmrb"
    media = "光明日报"
    pad = 2
    art_re = re.compile(r"content_\d+\.html?")

    def _node_url(self, d, nn):
        return (f"https://epaper.gmw.cn/gmrb/html/layout/"
                f"{d:%Y%m}/{d:%d}/node_{nn:02d}.html")


class Takefoto(ListSource):
    """北晚在线（北京晚报官方网站）：北京频道文章流。"""

    key = "takefoto"
    media = "北晚在线"
    pages = ["https://www.takefoto.cn/beijing/"]
    domain = "takefoto.cn"
    art = re.compile(r"/news/20\d{2}/\d{2}/\d{2}/\d+\.shtml")


# ---------- 注册表 ----------
SEARCH_SOURCES = [CctvSearch(), ChinaDailySearch(), ThepaperSearch(),
                  BaiduSearch(), ToutiaoSearch()]
LIST_SOURCES = [
    PeopleRoot(), XinhuaRoot(), ChinanewsScroll(), Qianlong(), Bjnews(),
    Cri(), BjdcGov(), Gmw(), Cnr(), Workercn(), Cyol(), YouthCn(), CeCn(),
    Stdaily(), Liaowang(), Banyuetan(), Rmzxb(), ChinaCom(), Haiwainet(),
    Qstheory(), LegalDaily(), Jkb(), Ynet(), BeijingGov(), Ifeng(),
    Jiemian(), Takungpao(), Wenweipo(), NetEase(), Sohu(), Nbd(), Rmlt(),
    Crntt(), Cpd(), K618(), CpcPeople(), Hkcd(), Cnwomen(), Stcn(),
    Eastmoney(), MzyfzSource(), Ctnews(), GovCn(), Bjd(), Jj21(), Cqn(),
]
# 电子报源：常态监听走 fetch()（今昨两天版面），
# 历史补爬由 pipeline.run_backfill 按 search(date_from, date_to) 逐日回溯。
EPAPER_SOURCES = [
    BjdEpaperSource("epaper_bjrb", "bjrbdzb.bjd.com.cn", "bjrb", "北京日报"),
    BjdEpaperSource("epaper_bjwb", "bjrbdzb.bjd.com.cn", "bjwb", "北京晚报"),
    BjdEpaperSource("epaper_bjqn", "bqbdzb.bjd.com.cn", "bjqn", "北京青年报"),
    PeopleEpaper(),
    GmwEpaper(),
    Takefoto(),
]
ALL_SOURCES = SEARCH_SOURCES + LIST_SOURCES + EPAPER_SOURCES

# 搜索型源（查询词本身即"东城区/北京东城"，甄别时视为可信源）
TRUSTED_SEARCH_KEYS = {s.key for s in SEARCH_SOURCES}

# 北京本地媒体（T3 关键词语境自动成立）
BEIJING_MEDIA_KEYS = {"people", "xinhua", "qianlong", "bjnews", "bjdc_gov",
                      "ynet", "beijing_gov", "bjd", "chinanews",
                      "epaper_bjrb", "epaper_bjwb", "epaper_bjqn",
                      "takefoto"}


def run_all(date_from=None, date_to=None):
    """执行一轮抓取：返回 (候选条目列表, 每源统计 dict)。"""
    out, stats = [], {}
    for src in ALL_SOURCES:
        try:
            if hasattr(src, "search"):
                items = src.search(date_from, date_to)
            else:
                items = src.fetch(date_from, date_to)
            stats[src.key] = len(items)
            out.extend(items)
        except Exception as e:
            stats[src.key] = f"ERR:{type(e).__name__}"
    return out, stats


def run_all_list_only(date_from=None, date_to=None):
    """仅执行列表型数据源（补爬时的近端补齐）。"""
    out, stats = [], {}
    for src in LIST_SOURCES:
        if isinstance(src, ChinanewsScroll):
            continue  # 滚动源已在补爬流程单独处理
        try:
            items = src.fetch(date_from, date_to)
            stats[src.key] = len(items)
            out.extend(items)
        except Exception as e:
            stats[src.key] = f"ERR:{type(e).__name__}"
    return out, stats


def filter_dongcheng(items):
    """按东城区关键词甄别。返回 (命中列表, 命中统计)。"""
    hits = []
    per_source = {}
    for it in items:
        ok, kw, level = is_dongcheng(it.get("title", ""),
                                     it.get("summary", ""),
                                     it.get("source_key", ""),
                                     BEIJING_MEDIA_KEYS,
                                     trusted=it.get("source_key", "")
                                     in TRUSTED_SEARCH_KEYS)
        if ok:
            it["keyword"] = kw
            it["match_level"] = level
            hits.append(it)
            per_source[it["source_key"]] = per_source.get(
                it["source_key"], 0) + 1
    return hits, per_source
