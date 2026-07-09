"""Stop 钩子：向 CLVocab 弹窗注销本会话；所有会话都注销完弹窗才会关闭。"""

import json
import socket
import sys

PORTS = (28471, 29873, 31597, 32851)


def read_session_id():
    try:
        # 按字节读再按 utf-8-sig 解码：既剥掉 PowerShell 管道的 BOM，
        # 也绕开中文 Windows 上 stdin 默认按 GBK 解码的问题
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "ignore")
        return str(json.loads(raw).get("session_id") or "unknown")
    except Exception:
        return "unknown"


def main():
    sid = read_session_id()
    for port in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3) as c:
                c.sendall(f"stop {sid}\n".encode("utf-8"))
                c.settimeout(1.0)
                if c.recv(8).startswith(b"ok"):
                    return
        except OSError:
            continue  # 弹窗没在跑，或候选端口是别的程序


if __name__ == "__main__":
    main()
