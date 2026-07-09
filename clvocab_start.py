"""UserPromptSubmit 钩子：向 CLVocab 弹窗登记本会话；弹窗没开就先启动再登记。

从 stdin 读钩子的 JSON 拿 session_id，向弹窗控制端口发 "start <sid>"。
只有收到 "ok" 应答才算成功（避免误连到恰好占用候选端口的其他程序）。
"""

import json
import os
import socket
import subprocess
import sys
import time

PORTS = (28471, 29873, 31597, 32851)
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clvocab.py")
# 优先用 pythonw（无控制台窗口）；找不到就退回当前解释器并隐藏窗口
_pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
PYTHONW = _pyw if os.path.exists(_pyw) else sys.executable


def read_session_id():
    try:
        # 按字节读再按 utf-8-sig 解码：既剥掉 PowerShell 管道的 BOM，
        # 也绕开中文 Windows 上 stdin 默认按 GBK 解码的问题
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "ignore")
        return str(json.loads(raw).get("session_id") or "unknown")
    except Exception:
        return "unknown"


def send(msg):
    """发一条控制消息，收到 ok 应答才算成功。"""
    for port in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3) as c:
                c.sendall((msg + "\n").encode("utf-8"))
                c.settimeout(1.0)
                return c.recv(8).startswith(b"ok")
        except OSError:
            continue
    return False


def main():
    sid = read_session_id()
    if send(f"start {sid}"):
        return
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen([PYTHONW, APP], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    for _ in range(40):  # 最多等 6 秒弹窗起来
        time.sleep(0.15)
        if send(f"start {sid}"):
            return


if __name__ == "__main__":
    main()
