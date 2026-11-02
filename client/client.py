#!/usr/bin/env python3
# client.py -- Hatoba (board_daemon.py) 向けの小さな MCP クライアント CLI
#
# ネットワーク越しに Hatoba デーモンを呼ぶためのクライアント。
# 接続先は BOARD_DAEMON_ENDPOINT で指定する
# (例: http://100.x.y.z:8766 -- Tailscale 経由など)。
#
# 使い方:
#   client.py list-tools
#   client.py call-tool --name doctor
#   client.py call-tool --name serial_send_expect \
#       --arguments-json '{"text":"help","pattern":">","timeout_ms":5000}'
#   client.py call-tool --name flash_kflash --arguments-file args.json
#   client.py health
#
# 環境変数:
#   BOARD_DAEMON_ENDPOINT  接続先 (既定 http://127.0.0.1:8766)
#   BOARD_DAEMON_NO_PROXY=1 でプロキシを使わない

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8766"


def build_opener():
    if os.environ.get("BOARD_DAEMON_NO_PROXY"):
        return urllib.request.build_opener()
    https_proxy = os.environ.get("HTTPS_PROXY", "")
    if https_proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": https_proxy,
                                         "https": https_proxy}))
    return urllib.request.build_opener()


class McpClient:
    def __init__(self, endpoint):
        self.url = endpoint.rstrip("/") + "/mcp"
        self.endpoint = endpoint.rstrip("/")
        self.opener = build_opener()
        self._id = 0

    def _post(self, payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with self.opener.open(req, timeout=210) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else None

    def call(self, method, params=None):
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params or {}})
        if resp and "error" in resp:
            raise RuntimeError("JSON-RPC error: %s" % resp["error"])
        return resp["result"] if resp else None

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def handshake(self):
        self.call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "board-daemon-client", "version": "0.1.0"}})
        self.notify("notifications/initialized")

    def health(self):
        req = urllib.request.Request(self.endpoint + "/health")
        with self.opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint",
                    default=os.environ.get("BOARD_DAEMON_ENDPOINT",
                                           DEFAULT_ENDPOINT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("list-tools")
    ct = sub.add_parser("call-tool")
    ct.add_argument("--name", required=True)
    ct.add_argument("--arguments-json", default=None)
    ct.add_argument("--arguments-file", default=None)
    args = ap.parse_args()

    client = McpClient(args.endpoint)
    try:
        if args.cmd == "health":
            print(json.dumps(client.health(), ensure_ascii=False, indent=2))
            return 0
        client.handshake()
        if args.cmd == "list-tools":
            result = client.call("tools/list")
            for t in result["tools"]:
                print("%s\t%s" % (t["name"], t["description"]))
            return 0
        if args.cmd == "call-tool":
            if args.arguments_file:
                with open(args.arguments_file, encoding="utf-8") as f:
                    arguments = json.load(f)
            elif args.arguments_json:
                arguments = json.loads(args.arguments_json)
            else:
                arguments = {}
            result = client.call("tools/call",
                                 {"name": args.name, "arguments": arguments})
            for item in result.get("content", []):
                if item.get("type") == "text":
                    text = item["text"]
                    try:
                        print(json.dumps(json.loads(text),
                                         ensure_ascii=False, indent=2))
                    except Exception:
                        print(text)
            return 1 if result.get("isError") else 0
    except Exception as e:
        print("client error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
