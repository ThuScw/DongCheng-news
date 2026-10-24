# -*- coding: utf-8 -*-
"""后台守护进程：PID 管理、日志、中断检测与补爬。

跨平台守护化：
  - Linux/macOS：经典双 fork，脱离终端；
  - Windows   ：subprocess + DETACHED_PROCESS 派生子进程（python -m dcnews.daemon）。
停止方式：SIGTERM（Unix）；pidfile 消失自检 + os.kill 兜底（Windows）。
"""
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import config, db, pipeline

log = logging.getLogger("dcnews")

IS_WINDOWS = (os.name == "nt")

# Windows 进程分离标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


# ---------------- 日志初始化 ----------------

def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    # 仅文件处理器：守护进程中 stderr 已重定向到同一日志文件，
    # 再加 StreamHandler 会导致每行重复写两次
    fh = logging.handlers.TimedRotatingFileHandler(
        config.LOG_PATH, when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ---------------- PID 管理 ----------------

def read_pid():
    try:
        with open(config.PID_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    """跨平台进程存活探测。"""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Windows: ctypes OpenProcess
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def write_pid():
    with open(config.PID_PATH, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    try:
        os.remove(config.PID_PATH)
    except OSError:
        pass


# ---------------- 守护化 ----------------

def _daemonize_unix():
    """经典双 fork 守护化，脱离终端，输出重定向到日志文件。"""
    log_fd = os.open(config.LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o644)
    pid = os.fork()
    if pid > 0:
        # 父进程：等待子进程写好 PID 文件后退出
        for _ in range(50):
            if read_pid() is not None:
                break
            time.sleep(0.1)
        return pid
    # 第一次 fork 的子进程
    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    # 守护进程本体
    os.chdir(config.HOME)
    os.umask(0o022)
    write_pid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    if log_fd > 2:
        os.close(log_fd)
    return None


def _daemonize_windows(interval_min, backfill_from):
    """Windows：派生分离的子进程运行监听循环。返回子进程 PID。"""
    out = open(config.LOG_PATH, "a", buffering=1, encoding="utf-8",
               errors="replace")
    devnull = open(os.devnull, "r")
    args = [sys.executable, "-m", "dcnews.daemon",
            str(interval_min), backfill_from or "-"]
    proc = subprocess.Popen(
        args, cwd=config.HOME, stdin=devnull, stdout=out, stderr=out,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    out.close()
    devnull.close()
    # 等子进程写 PID 文件
    for _ in range(50):
        if read_pid() is not None:
            break
        time.sleep(0.1)
    return proc.pid


# ---------------- 主循环 ----------------

def _run_loop(interval_min, backfill_from=None, note=""):
    setup_logging()
    db.reset_conn()
    db.init_db()
    write_pid()
    log.info("守护进程启动 pid=%d interval=%dmin note=%s",
             os.getpid(), interval_min, note)

    # 补爬（指定起点 > 中断区间 > 无）
    if backfill_from:
        try:
            added = pipeline.run_backfill(backfill_from)
            log.info("启动补爬完成 from=%s added=%d", backfill_from, added)
        except Exception:
            log.exception("启动补爬失败")

    cycle = 0
    while True:
        cycle += 1
        try:
            r = pipeline.run_cycle()
            log.info("周期 %d 完成：新增 %d（重复 %d），库内累计 %d",
                     cycle, r["new"], r["dup"], r["total"])
        except Exception:
            log.exception("周期 %d 执行异常，将继续重试", cycle)
        # 心跳即使失败也刷新（表示进程存活）
        db.set_state("pid", str(os.getpid()))
        db.set_state("last_heartbeat",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 间隔内分段休眠：响应信号（Unix）与 pidfile 消失（优雅停止）
        stop_requested = False
        slept = 0
        target = interval_min * 60
        while slept < target:
            step = min(10, target - slept)
            time.sleep(step)
            slept += step
            # pid 文件被 stop() 删除（或被改写）→ 自行退出
            current = read_pid()
            if current is None or current != os.getpid():
                stop_requested = True
                break
        if stop_requested:
            log.info("检测到 PID 文件已被删除，退出监听。")
            remove_pid()
            break


def _handle_term(signum, frame):
    log.info("收到停止信号 (%s)，退出。", signum)
    remove_pid()
    db.set_state("pid", "")
    sys.exit(0)


# ---------------- 对外接口 ----------------

def start(date_arg=None, interval_min=None):
    """启动监听。date_arg：YYYYMMDD 或 None（默认从现在开始）。"""
    interval_min = interval_min or config.DEFAULT_INTERVAL_MIN
    db.init_db()

    old = read_pid()
    if old and pid_alive(old):
        print(f"监听进程已在运行（pid={old}）。如需重启请先执行 stop。")
        return 1

    # ---- 中断检测（在守护化前完成，便于前台提示）----
    backfill_from = None
    note = ""
    if date_arg:
        # 兼容 YYYYMMDD 与 YYYY-MM-DD 两种入参（cli 层已做格式转换）
        s = date_arg.replace("-", "")
        if not (len(s) == 8 and s.isdigit()):
            print("日期参数格式应为 YYYYMMDD，例如 20251010")
            return 1
        backfill_from = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        note = f"指定起点 {backfill_from}"
        print(f"将以 {backfill_from} 为起点补爬历史新闻，随后进入常态监听。")
    else:
        interrupted, msg, gap_start = pipeline.detect_interrupt(interval_min)
        print(msg)
        if interrupted:
            backfill_from = gap_start
            note = f"中断补爬 {gap_start}"
            print(f"启动后将自动补爬 {gap_start} 以来的新闻。")
        else:
            print("默认从现在开始监听。")

    # ---- 守护化（跨平台）----
    if IS_WINDOWS:
        pid = _daemonize_windows(interval_min, backfill_from)
        print(f"监听已启动并转入后台运行（pid={pid}）。")
        print(f"日志：{config.LOG_PATH}")
        print(f"数据库：{config.DB_PATH}")
        return 0

    pid = _daemonize_unix()
    if pid is not None:
        child = read_pid() or pid
        print(f"监听已启动并转入后台运行（pid={child}）。")
        print(f"日志：{config.LOG_PATH}")
        print(f"数据库：{config.DB_PATH}")
        return 0

    # Unix 守护进程本体
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    try:
        _run_loop(interval_min, backfill_from, note)
    finally:
        remove_pid()
    return 0


def stop():
    pid = read_pid()
    if not pid or not pid_alive(pid):
        print("监听进程未在运行。")
        remove_pid()
        return 0
    # 先删 pidfile：Windows 子进程休眠片段里会检测到并自行退出
    remove_pid()
    try:
        if IS_WINDOWS:
            os.kill(pid, signal.SIGTERM)  # TerminateProcess 兜底
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        print("进程已不存在，已清理 PID 文件。")
        return 0
    for _ in range(60):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL if not IS_WINDOWS else 9)
        except OSError:
            pass
    print(f"监听进程（pid={pid}）已停止。")
    return 0


def status():
    db.init_db()
    pid = read_pid()
    running = bool(pid and pid_alive(pid))
    print("=" * 56)
    print("dcnew 状态")
    print("=" * 56)
    print(f"运行状态   ：{'运行中 (pid=%s)' % pid if running else '未运行'}")
    print(f"运行平台   ：{'Windows' if IS_WINDOWS else 'Unix/macOS'}")
    hb = db.get_state("last_heartbeat")
    print(f"上次心跳   ：{hb or '—'}")
    print(f"数据目录   ：{config.HOME}"
          f"{'（可用环境变量 DCNEW_HOME 修改）' if os.name == 'nt' else ''}")
    print(f"日志文件   ：{config.LOG_PATH}")
    print(f"数据库     ：{config.DB_PATH}（共 {db.count_news()} 条新闻）")
    if not running and hb:
        try:
            last = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
            gap_h = (datetime.now() - last).total_seconds() / 3600
            if gap_h > config.DEFAULT_INTERVAL_MIN / 60 * 2:
                print(f"提示       ：距上次心跳 {gap_h:.1f} 小时，"
                      f"下次 start 时将自动补爬中断区间新闻。")
        except ValueError:
            pass
    return 0


if __name__ == "__main__":
    # Windows 守护子进程入口：python -m dcnews.daemon <interval> <backfill|->
    _interval = int(sys.argv[1]) if len(sys.argv) > 1 else \
        config.DEFAULT_INTERVAL_MIN
    _bf = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    signal.signal(signal.SIGINT, _handle_term)
    try:
        _run_loop(_interval, _bf, "Windows 守护子进程")
    finally:
        remove_pid()
