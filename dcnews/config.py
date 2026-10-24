# -*- coding: utf-8 -*-
"""全局配置：路径、轮询间隔、超时等。

数据存储目录解析优先级：
  1. 环境变量 DCNEW_HOME（绝对路径，最高优先级）
  2. 源码目录运行（pip install -e 或项目目录内）：使用项目根目录，
     与 dcnew/dcnews.py 同级
  3. pip 安装运行 + Windows 存在 D 盘：使用 D:\\dcnew（自动创建）
  4. 兜底：用户主目录 ~/dcnew（Linux/macOS 或无 D 盘的 Windows）

目录确定后，首次运行会把旧位置（包目录下）已有的 news.db
自动迁移过来，避免 pip 安装/卸载导致数据丢失。
"""
import os
import shutil


def _resolve_home():
    """确定数据主目录。"""
    # 1) 环境变量显式指定
    env = os.environ.get("DCNEW_HOME")
    if env:
        return os.path.abspath(env)
    pkg_dir = os.path.dirname(os.path.abspath(__file__))       # dcnews/ 包
    parent = os.path.dirname(pkg_dir)                           # 上层
    # 2) 源码目录运行：项目根有入口文件（dcnews.py / dcnew 启动器）
    if os.path.exists(os.path.join(parent, "dcnews.py")) or \
            os.path.exists(os.path.join(parent, "dcnew")):
        return parent
    # 3) pip 安装运行：Windows 优先 D:\dcnew
    if os.name == "nt" and os.path.isdir("D:\\"):
        return "D:\\dcnew"
    # 4) 兜底
    return os.path.join(os.path.expanduser("~"), "dcnew")


HOME = _resolve_home()

DATA_DIR = os.path.join(HOME, "data")            # SQLite 数据库
LOG_DIR = os.path.join(HOME, "logs")             # 运行日志
EXPORT_DIR = os.path.join(HOME, "exports")       # 周报 Excel 输出
for _d in (DATA_DIR, LOG_DIR, EXPORT_DIR):
    os.makedirs(_d, exist_ok=True)


def _migrate_legacy_db():
    """pip 安装场景下，把包目录内旧位置的 news.db 迁移到新主目录。"""
    legacy = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "news.db")
    target = os.path.join(DATA_DIR, "news.db")
    try:
        if (os.path.exists(legacy)
                and os.path.abspath(legacy) != os.path.abspath(target)
                and not os.path.exists(target)):
            shutil.copy2(legacy, target)
    except Exception:
        pass  # 迁移失败不影响运行


_migrate_legacy_db()

DB_PATH = os.path.join(DATA_DIR, "news.db")
PID_PATH = os.path.join(DATA_DIR, "dcnews.pid")
LOG_PATH = os.path.join(LOG_DIR, "dcnews.log")

# 轮询间隔（分钟），可通过 start --interval 覆盖
DEFAULT_INTERVAL_MIN = 20
# 单个 HTTP 请求超时（秒）
HTTP_TIMEOUT = 25
# 每个周期内每个源最多补抓文章详情页条数
ENRICH_LIMIT_PER_SOURCE = 40
# 中断补爬的最大回溯天数（防止日志丢失后无限回溯）
MAX_BACKFILL_DAYS = 180
# 断点判定：心跳缺失超过 N 个周期视为中断
INTERRUPT_FACTOR = 2

# 东城区人民政府官网（本环境可能无法直连，境内可用；失败自动跳过）
BJDC_GOV_URL = "https://www.bjdc.gov.cn/"
