#!/usr/bin/env python3
"""board-daemon のウォッチドッグ (WSL 側・標準ライブラリのみ)。

デーモンがプロセスごと落ちたり応答しなくなったりする事象が続いたため
(2026-11-06)、隣のプロセスとして health を見張り、連続して失敗したら
デーモンを再起動する。daemon.log は追記式なので証拠は消えず、むしろ
「いつ落ちていつ復旧したか」が watchdog.log に残って切り分けが進む。

使い方 (デーモンと同じフォルダで):
    python3 watchdog.py            # これだけでデーモンも面倒を見る
    ./run.sh                       # バックグラウンドで watchdog を起動

- 起動時にデーモンが居なければ自分で起動する。居ればそのまま監視する。
- health (GET /health) が threshold 回連続で失敗したら再起動する。
  flash の最中でも health は別スレッドで応答するので、正常に時間の
  かかっている書き込みを誤って切ることはない。逆にデーモンごと固まった
  書き込みは再起動が正しい復旧になる。
- 再起動しすぎ防止に、1時間あたりの再起動回数に上限を持ち、超えたら
  10分に1回まで落とす。
- watchdog 自体を止めてもデーモンは止まらない (別セッションで起動)。
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "watchdog.log")
CONSOLE_PATH = os.path.join(HERE, "daemon-console.log")


def log(msg):
    line = "%s [watchdog] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def health_ok(port, timeout=5):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:
        return False


def find_daemon_pids():
    """board_daemon.py を実行中の pid 一覧 (/proc 走査、pgrep 不要)。"""
    pids = []
    me = os.getpid()
    try:
        names = os.listdir("/proc")
    except Exception:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().decode("utf-8", errors="replace")
            if "board_daemon.py" in cmd and "watchdog" not in cmd:
                pids.append(pid)
        except Exception:
            pass
    return pids


def kill_daemons():
    pids = find_daemon_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if pids:
        time.sleep(3)
    for pid in find_daemon_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    if pids:
        log("killed daemon pid(s): %s" % ", ".join(str(p) for p in pids))
    return pids


def start_daemon(config_path):
    console = open(CONSOLE_PATH, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "board_daemon.py"),
         "--config", config_path],
        cwd=HERE, stdout=console, stderr=subprocess.STDOUT,
        start_new_session=True)
    log("daemon started pid=%d (console: daemon-console.log)" % proc.pid)
    return proc.pid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--interval", type=float, default=10,
                    help="health 確認の間隔 秒 (既定 10)")
    ap.add_argument("--threshold", type=int, default=3,
                    help="再起動までの連続失敗回数 (既定 3、約30秒)")
    ap.add_argument("--max-restarts-per-hour", type=int, default=12)
    args = ap.parse_args()

    http_port = 8766
    try:
        with open(args.config, encoding="utf-8") as f:
            http_port = int(json.load(f).get("http_port", 8766))
    except Exception:
        pass

    log("watchdog start (port=%d, interval=%gs, threshold=%d)"
        % (http_port, args.interval, args.threshold))
    restarts = []       # 再起動した時刻 (time.time) のリスト
    fails = 0
    grace_until = 0.0   # 起動直後は失敗を数えない

    while True:
        now = time.time()
        if health_ok(http_port):
            if fails:
                log("health recovered (fails reset from %d)" % fails)
            fails = 0
            time.sleep(args.interval)
            continue
        if now < grace_until:
            time.sleep(args.interval)
            continue
        fails += 1
        log("health failed (%d/%d)" % (fails, args.threshold))
        if fails < args.threshold:
            time.sleep(args.interval)
            continue

        # 再起動予算: 直近1時間の回数が上限を超えていたら間引く
        restarts = [t for t in restarts if now - t < 3600]
        if len(restarts) >= args.max_restarts_per_hour:
            log("restart budget exhausted (%d/hour); retry in 10 min"
                % args.max_restarts_per_hour)
            fails = 0
            time.sleep(600)
            continue

        log("restarting daemon")
        kill_daemons()
        start_daemon(args.config)
        restarts.append(time.time())
        fails = 0
        grace_until = time.time() + 20
        # 起動を待つ (最大20秒)
        for _ in range(10):
            time.sleep(2)
            if health_ok(http_port):
                log("daemon is back (health ok)")
                break
        else:
            log("daemon still not healthy after restart; keep watching")


if __name__ == "__main__":
    main()
