#!/usr/bin/env python3
"""把背单词弹窗的钩子写进 ~/.claude/settings.json（免插件的安装方式）。

    python install.py               安装（重复运行是幂等的，会先移除旧条目再写入）
    python install.py --uninstall   卸载
    python install.py --settings X  指定 settings.json 路径（主要用于测试）

安装时用当前 Python 解释器的绝对路径写入钩子，所以请用带 tkinter 的
Python 3.8+ 来运行本脚本（Windows 官方安装包默认自带）。
"""

import argparse
import json
import os
import shutil
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MARK = "vocab_popup"  # 识别本项目钩子条目的特征串


def load_settings(path):
    try:
        # utf-8-sig：兼容记事本/PowerShell 写出的带 BOM 文件
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        sys.exit(f"错误：{path} 不是合法 JSON，为避免破坏现有配置已中止，请手动检查。")


def hook_entry(script, timeout):
    return {
        "hooks": [
            {
                "type": "command",
                "command": sys.executable,
                "args": [os.path.join(APP_DIR, script)],
                "async": True,
                "timeout": timeout,
                "statusMessage": "打开背单词窗口" if "start" in script else "关闭背单词窗口",
            }
        ]
    }


def is_ours(group):
    blob = json.dumps(group, ensure_ascii=False)
    return MARK in blob


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true", help="移除本项目的钩子")
    ap.add_argument("--settings", default=DEFAULT_SETTINGS, help="settings.json 路径")
    args = ap.parse_args()

    if not args.uninstall:
        try:
            import tkinter  # noqa: F401  确认当前解释器带 GUI 支持
        except ImportError:
            sys.exit("错误：当前 Python 没有 tkinter，请换用官方安装包的 Python 再运行。")

    data = load_settings(args.settings)
    hooks = data.setdefault("hooks", {})
    for event, script, timeout in (
        ("UserPromptSubmit", "hook_start.py", 20),
        ("Stop", "hook_stop.py", 15),
    ):
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        if not args.uninstall:
            groups.append(hook_entry(script, timeout))
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)

    os.makedirs(os.path.dirname(args.settings), exist_ok=True)
    if os.path.exists(args.settings):
        shutil.copy2(args.settings, args.settings + ".bak")
    with open(args.settings, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    action = "已卸载" if args.uninstall else "已安装"
    print(f"{action}背单词弹窗钩子 -> {args.settings}")
    if os.path.exists(args.settings + ".bak"):
        print(f"原配置备份在 {args.settings}.bak")
    if not args.uninstall:
        print("重启 Claude Code（或开新会话）后，发送任意消息即可看到弹窗。")


if __name__ == "__main__":
    main()
