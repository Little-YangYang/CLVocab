"""CLVocab —— Claude Code 任务期间的背单词弹窗。

由 UserPromptSubmit 钩子（clvocab_start.py）启动，Stop 钩子（clvocab_stop.py）通知退出。
监听 PORTS 中第一个可用端口 —— 既当单实例锁，也当控制通道：
端口被占用（EADDRINUSE）说明已有窗口在跑（直接退出）；
Windows 的保留端口范围（WinError 10013）会随重启变化，所以准备多个候选端口。

控制协议（一行文本，回复 "ok"）：多会话计数，谁的任务都没结束就不关窗
  "start <session_id>" —— 登记一个正在干活的会话
  "stop <session_id>"  —— 注销该会话；全部注销完才关窗（登记 8 小时过期，防会话崩溃悬挂）
  裸连接（不发数据）    —— 无条件关窗（兼容手动关闭脚本）

词表放在同目录 dicts/ 下，每个 *.json 是一本词书：[{"word","phonetic","meaning"}, ...]
学习状态（进度、打标、测验成绩、历史）持久化在 ~/.clvocab/state.json（旧位置原地兼容）。

三种模式：
  背词 —— 出词 → 释义，可自动播放（默认关，开关和进度都带记忆），可标 认识/模糊/不认识。
          出词按 STUDY_WEIGHTS 比重在新词和复背之间乱序混合：不认识复背最勤，模糊次之，认识偶尔
  选择 —— 四选一（选释义）。只有打过标的词才算"已背"、才会进题库，优先抽标记难的和答错过的。
          答对 1.4 秒自动进下一题；答错停住给复习时间，按钮/空格手动继续
  默写 —— 看释义拼单词，同样只考打过标的词。答对 1 秒自动下一题；答错/不会停住，回车或提交继续

外观：深色/浅色主题可切换（标题栏跟随）；透明度按 100%→90%→75%→60% 循环，
半透明时鼠标移入自动恢复不透明、移出复原。主题和透明度都持久化。

发音：用 Windows 自带的 System.Speech 语音合成（零依赖），常驻一个隐藏的
PowerShell 合成进程避免每次发音的启动延迟，优先选英文语音。顶栏「音:开/关」
控制自动朗读（出词/出题时自动读）；各模式里的「发音」按钮无视开关、点了就读。

快捷键：空格=显示释义/下一个，←/→=上下词，1/2/3=认识/模糊/不认识，Esc=关闭
"""

import errno
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

PORTS = (28471, 29873, 31597, 32851)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DICTS_DIR = os.path.join(APP_DIR, "dicts")
# 学习状态默认放用户主目录（插件目录更新时不会被冲掉）；
# 老版本的位置（程序目录 state.json、~/.vocab_popup）存在就继续用，原地兼容
_LOCAL_STATE = os.path.join(APP_DIR, "state.json")
_LEGACY_HOME = os.path.join(os.path.expanduser("~"), ".vocab_popup", "state.json")
if os.path.exists(_LOCAL_STATE):
    STATE_FILE = _LOCAL_STATE
elif os.path.exists(_LEGACY_HOME):
    STATE_FILE = _LEGACY_HOME
else:
    STATE_FILE = os.path.join(os.path.expanduser("~"), ".clvocab", "state.json")

REVEAL_MS = 5000    # 自动播放：出词多久后显示释义
NEXT_MS = 7000      # 自动播放：显示释义多久后换下一个
MARK_PREVIEW_MS = 2000  # 没看释义就打标时，先亮释义预览这么久再进下一个
HISTORY_CAP = 1000  # 历史记录条数上限
SESSION_TTL = 8 * 3600  # 会话登记的过期时间（防某个会话崩溃后永远不关窗）

# 背词乱序比重：每次"下一个"按权重决定出新词还是复背已打标的词（改数字即可调比重）。
# 全部池子非空时约 60% 新词、20% 不认识、12% 模糊、8% 认识；某类没有词则权重自动让给其余类。
STUDY_WEIGHTS = {"new": 60, "unknown": 20, "fuzzy": 12, "know": 8}
RECENT_BLOCK = 8    # 最近出过的 N 个词不参与复背抽取，避免同一个词打转

ALPHA_STEPS = (1.0, 0.9, 0.75, 0.6)  # 透明度循环档位

THEMES = {
    "dark": {
        "bg": "#1f2430", "card": "#262c3a", "fg": "#eceff4", "dim": "#8a93a8",
        "good": "#a6d189", "warn": "#e5c890", "bad": "#e78284",
        "btn": "#3b4252", "btn_active": "#4c566a", "entry": "#2a3040",
        "right_bg": "#3e5f43", "wrong_bg": "#6b3a3f",
    },
    "light": {
        "bg": "#e9ecf2", "card": "#f7f8fb", "fg": "#2e3440", "dim": "#6b7280",
        "good": "#3d7a3f", "warn": "#9a6b1f", "bad": "#b3454d",
        "btn": "#d8dce6", "btn_active": "#c2c9d8", "entry": "#ffffff",
        "right_bg": "#b9dcb6", "wrong_bg": "#f0bcc0",
    },
}

MARKS = {"know": ("认识", "good"), "fuzzy": ("模糊", "warn"), "unknown": ("不认识", "bad")}
EVENT_LABELS = {
    "seen": "看过", "mark:know": "标记·认识", "mark:fuzzy": "标记·模糊",
    "mark:unknown": "标记·不认识", "mc_r": "选择 ✓", "mc_w": "选择 ✗",
    "sp_r": "默写 ✓", "sp_w": "默写 ✗",
}


# ---------------------------------------------------------------- 网络锁

def acquire_lock():
    """绑定候选端口之一作为单实例锁；EADDRINUSE 表示已有实例在运行。"""
    for port in PORTS:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", port))
            srv.listen(5)
            return srv
        except OSError as e:
            srv.close()
            if e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) == 10048:
                return None  # 已有实例
            continue  # 端口被系统保留等，换下一个
    return None


def watch_control(srv, on_stop):
    """控制通道：多会话计数，全部会话都 stop 了才关窗。

    "start <sid>" 登记 / "stop <sid>" 注销；裸连接（无数据）无条件关窗。
    每条消息回复 "ok"，让钩子脚本确认连到的确实是本弹窗而不是别的程序。
    """
    sessions = {}
    lock = threading.Lock()
    debug = os.environ.get("VOCAB_DEBUG")

    def log(msg):
        if debug:
            with open(os.path.join(APP_DIR, "control.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    def handle(conn):
        try:
            conn.settimeout(1.0)
            try:
                data = conn.recv(256).decode("utf-8", "ignore").strip()
            except Exception:
                data = ""
            if not data:
                log(f"bare connection -> close; sessions={list(sessions)}")
                on_stop()  # 兼容旧的裸连接关窗
                return
            cmd, _, sid = data.partition(" ")
            sid = sid.strip() or "unknown"
            stop = False
            with lock:
                now = time.time()
                for k in [k for k, v in sessions.items() if now - v > SESSION_TTL]:
                    del sessions[k]
                if cmd == "start":
                    sessions[sid] = now
                elif cmd == "stop":
                    sessions.pop(sid, None)
                    stop = not sessions
            log(f"{data!r} -> sessions={list(sessions)} stop={stop}")
            try:
                conn.sendall(b"ok\n")
            except Exception:
                pass
            if stop:
                on_stop()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


# ---------------------------------------------------------------- 词表与状态

def list_dicts():
    """返回 {词书名: 文件路径}，按名称排序。"""
    result = {}
    if os.path.isdir(DICTS_DIR):
        for fn in sorted(os.listdir(DICTS_DIR)):
            if fn.lower().endswith(".json"):
                result[fn[:-5]] = os.path.join(DICTS_DIR, fn)
    return result


def load_dict(path):
    with open(path, encoding="utf-8") as f:
        words = json.load(f)
    return [w for w in words if w.get("word") and w.get("meaning")]


class Store:
    """state.json 的读写：设置、每本词书的进度、每个词的学习记录、历史。"""

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}
        self.data.setdefault("settings", {})
        self.data.setdefault("progress", {})
        self.data.setdefault("words", {})
        self.data.setdefault("history", [])

    # ---- 设置 ----
    def get_setting(self, key, default=None):
        return self.data["settings"].get(key, default)

    def set_setting(self, key, value):
        self.data["settings"][key] = value
        self.dirty = True

    # ---- 进度（背词顺序带记忆）----
    def get_order(self, dict_name, size):
        prog = self.data["progress"].get(dict_name)
        if not prog or len(prog.get("order", [])) != size:
            order = list(range(size))
            random.shuffle(order)
            prog = {"order": order, "pos": 0}
            self.data["progress"][dict_name] = prog
            self.dirty = True
        return prog

    def reshuffle(self, dict_name, size):
        order = list(range(size))
        random.shuffle(order)
        self.data["progress"][dict_name] = {"order": order, "pos": 0}
        self.dirty = True
        return self.data["progress"][dict_name]

    def set_pos(self, dict_name, pos):
        self.data["progress"][dict_name]["pos"] = pos
        self.dirty = True

    # ---- 单词记录 ----
    def dict_words(self, dict_name):
        return self.data["words"].setdefault(dict_name, {})

    def word_stat(self, dict_name, word):
        return self.dict_words(dict_name).setdefault(
            word, {"seen": 0, "mark": None, "mc_r": 0, "mc_w": 0, "sp_r": 0, "sp_w": 0, "last": 0}
        )

    def bump_seen(self, dict_name, word):
        """浏览计数：只记在单词身上，不进历史（自动播放刷过的词不刷屏）。"""
        st = self.word_stat(dict_name, word)
        st["seen"] += 1
        st["last"] = time.time()
        self.dirty = True

    def record(self, dict_name, word, event):
        st = self.word_stat(dict_name, word)
        st["last"] = time.time()
        if event.startswith("mark:"):
            st["mark"] = event.split(":", 1)[1]
        elif event in ("mc_r", "mc_w", "sp_r", "sp_w"):
            st[event] += 1
        self.data["history"].append({"t": time.time(), "dict": dict_name, "word": word, "event": event})
        if len(self.data["history"]) > HISTORY_CAP:
            self.data["history"] = self.data["history"][-HISTORY_CAP:]
        self.dirty = True

    def today_counts(self):
        today = time.strftime("%Y-%m-%d")
        marked = quiz = 0
        for ev in self.data["history"]:
            if time.strftime("%Y-%m-%d", time.localtime(ev["t"])) != today:
                continue
            if ev["event"].startswith("mark:"):
                marked += 1
            elif ev["event"] in ("mc_r", "mc_w", "sp_r", "sp_w"):
                quiz += 1
        return marked, quiz

    def save(self, force=False):
        if not (self.dirty or force):
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.dirty = False


# ---------------------------------------------------------------- 发音

class TTS:
    """常驻一个隐藏的 PowerShell 语音合成进程（System.Speech，Windows 自带）。

    按行从 stdin 读单词朗读；新词打断上一个，避免排队。优先选英文语音，
    没有英文语音就用系统默认。进程挂了会在下次 speak 时自动重启。
    """

    SCRIPT = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try {"
        " $v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'en*' } | Select-Object -First 1;"
        " if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }"
        "} catch {};"
        "while ($true) {"
        " $line = [Console]::In.ReadLine(); if ($null -eq $line) { break };"
        " $s.SpeakAsyncCancelAll(); [void]$s.SpeakAsync($line)"
        "}"
    )

    def __init__(self):
        self.proc = None

    def start(self):
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self.SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except Exception:
            self.proc = None

    def speak(self, text):
        text = (text or "").strip()
        if not text:
            return
        if self.proc is None or self.proc.poll() is not None:
            self.start()
            if self.proc is None:
                return
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            self.proc = None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# ---------------------------------------------------------------- 测验逻辑（纯函数，便于测试）

def quiz_weight(stat):
    """抽题权重：标记难的、净答错多的优先。

    答对会抵消此前的答错（净错误 = 错 - 0.75×对，下限 0），
    所以一个词答顺了权重就回落，不会因早期的错永远霸榜；权重封顶防垄断。
    """
    w = 1.0
    if stat.get("mark") == "unknown":
        w += 3.0
    elif stat.get("mark") == "fuzzy":
        w += 1.5
    wrong = stat.get("mc_w", 0) + stat.get("sp_w", 0)
    right = stat.get("mc_r", 0) + stat.get("sp_r", 0)
    w += max(0.0, wrong - 0.75 * right)
    return min(w, 8.0)


def pick_quiz_word(words, stats, exclude=()):
    """从打过标（=已背）的词里按权重抽一个；一个都没标过时返回 None。

    exclude 里的词（最近刚考过的）不抽，除非题库全被排除。
    """
    pool = [w for w in words if stats.get(w["word"], {}).get("mark")]
    if not pool:
        return None
    if len(pool) > 1 and exclude:
        ex = set(exclude)
        filtered = [w for w in pool if w["word"] not in ex]
        if filtered:
            pool = filtered
    weights = [quiz_weight(stats.get(w["word"], {})) for w in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def build_review_pools(words, stats, exclude=()):
    """按打标分类可复背的词，排除 exclude（最近刚出过的）。"""
    pools = {"unknown": [], "fuzzy": [], "know": []}
    ex = set(exclude)
    for w in words:
        if w["word"] in ex:
            continue
        m = stats.get(w["word"], {}).get("mark")
        if m in pools:
            pools[m].append(w)
    return pools


def draw_study_category(pools, weights=STUDY_WEIGHTS):
    """按比重决定这一张出新词还是复背哪一类；空池子不参与。"""
    cats = ["new"] + [k for k, v in pools.items() if v]
    return random.choices(cats, weights=[weights[c] for c in cats], k=1)[0]


def make_choices(words, correct, n=4):
    """正确释义 + (n-1) 个同词书干扰项，返回打乱后的 [(释义, 是否正确)]。"""
    others = [w["meaning"] for w in words
              if w["meaning"] != correct["meaning"] and w["word"] != correct["word"]]
    distractors = random.sample(others, min(n - 1, len(others)))
    options = [(correct["meaning"], True)] + [(m, False) for m in distractors]
    random.shuffle(options)
    return options


def trim(text, limit=52):
    """截断过长释义：尽量断在分隔符处（优先"；"的词性分组边界），
    清理断口残留的标点后再补省略号，避免出现"蹒跚地走,…"这类断半截的观感。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("；", "，", ",", "、", " "):
        i = cut.rfind(sep)
        if i >= limit // 2:
            cut = cut[:i]
            break
    return cut.rstrip(" ,，;；、.") + " …"


# ---------------------------------------------------------------- 主界面

class App:
    def __init__(self, root, dicts, store):
        self.root = root
        self.dicts = dicts            # {名称: 路径}
        self.store = store
        self.words = []               # 当前词书条目
        self.dict_name = None
        self.timer = None             # 自动播放定时器
        self.quiz_timer = None        # 测验切题定时器
        self.revealed = False
        self.seen_recorded = False
        self.current = None           # 背词模式当前词条
        self.back_stack = []          # 背词模式回看栈（支持"上一个"跨过复背插词）
        self.quiz_recent = []         # 最近考过的词，抽题时避开防连续重复
        self.closed = False
        self.history_win = None

        self.theme_name = store.get_setting("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"
        self.T = THEMES[self.theme_name]
        alpha = store.get_setting("alpha", 1.0)
        self.alpha = alpha if alpha in ALPHA_STEPS else 1.0

        root.title("CLVocab · Claude 干活中")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        w, h = 480, 360
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 90}")
        root.attributes("-alpha", self.alpha)
        self._apply_title_bar()

        self.autoplay = tk.BooleanVar(value=bool(store.get_setting("autoplay", False)))
        self.mode = store.get_setting("mode", "study")
        self.tts = TTS()
        self.tts_on = tk.BooleanVar(value=bool(store.get_setting("tts", True)))
        if self.tts_on.get():
            self.tts.start()  # 预热，首次发音不用等进程启动

        root.bind("<space>", self._on_space)
        root.bind("<Left>", lambda e: self._guard_keys(self.prev_word))
        root.bind("<Right>", lambda e: self._guard_keys(self.next_word))
        root.bind("1", lambda e: self._guard_keys(lambda: self.set_mark("know")))
        root.bind("2", lambda e: self._guard_keys(lambda: self.set_mark("fuzzy")))
        root.bind("3", lambda e: self._guard_keys(lambda: self.set_mark("unknown")))
        root.bind("<Escape>", lambda e: self.on_close())
        root.bind("<Enter>", self._hover_enter, add="+")
        root.bind("<Leave>", self._hover_leave, add="+")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        name = store.get_setting("dict")
        if name not in dicts:
            name = next(iter(dicts), None)
        if name:
            self.switch_dict(name)
        else:
            self._populate_card()

        self._autosave_tick()

    # ---- 界面骨架（换主题时整体重建）----
    def _build_ui(self):
        self._cancel_timers()
        for child in self.root.winfo_children():
            child.destroy()
        self.root.configure(bg=self.T["bg"])
        self._build_topbar()
        # 状态栏先 pack 占住底部，卡片内容再多也挤不掉它
        self.status = tk.Label(self.root, bg=self.T["bg"], fg=self.T["dim"], font=("Microsoft YaHei UI", 9))
        self.status.pack(side="bottom", pady=(2, 6))
        self.card = tk.Frame(self.root, bg=self.T["card"])
        self.card.pack(fill="both", expand=True, padx=10, pady=(6, 0))

    def _populate_card(self):
        if self.dict_name:
            self.set_mode(self.mode)
        else:
            tk.Label(self.card, text="dicts/ 目录下没有词表",
                     bg=self.T["card"], fg=self.T["bad"]).pack(expand=True)

    def _build_topbar(self):
        T = self.T
        bar = tk.Frame(self.root, bg=T["bg"])
        bar.pack(fill="x", padx=10, pady=(8, 0))

        self.dict_box = ttk.Combobox(bar, state="readonly", width=8,
                                     values=list(self.dicts.keys()), font=("Microsoft YaHei UI", 9))
        self.dict_box.pack(side="left")
        self.dict_box.bind("<<ComboboxSelected>>", lambda e: self.switch_dict(self.dict_box.get()))

        self.mode_btns = {}
        for key, label in (("study", "背词"), ("choice", "选择"), ("spell", "默写")):
            b = tk.Button(bar, text=label, relief="flat", bg=T["btn"], fg=T["fg"],
                          activebackground=T["btn_active"], activeforeground=T["fg"],
                          font=("Microsoft YaHei UI", 9), padx=8,
                          command=lambda k=key: self.set_mode(k))
            b.pack(side="left", padx=(7, 0))
            self.mode_btns[key] = b

        def flat_btn(text, cmd):
            b = tk.Button(bar, text=text, relief="flat", bg=T["bg"], fg=T["dim"],
                          activebackground=T["bg"], activeforeground=T["fg"],
                          font=("Microsoft YaHei UI", 9), command=cmd)
            b.pack(side="right")
            return b

        flat_btn("历史", self.open_history)
        self.auto_btn = flat_btn("", self._toggle_autoplay)
        self._render_auto_btn()
        self.tts_btn = flat_btn("", self._toggle_tts)
        self._render_tts_btn()
        self.alpha_btn = flat_btn(f"{int(self.alpha * 100)}%", self.cycle_alpha)
        self.theme_btn = flat_btn("浅色" if self.theme_name == "dark" else "深色", self.toggle_theme)

    def _render_auto_btn(self):
        on = self.autoplay.get()
        color = self.T["good"] if on else self.T["dim"]
        self.auto_btn.config(text="自动:开" if on else "自动:关", fg=color, activeforeground=color)

    def _render_tts_btn(self):
        on = self.tts_on.get()
        color = self.T["good"] if on else self.T["dim"]
        self.tts_btn.config(text="音:开" if on else "音:关", fg=color, activeforeground=color)

    # ---- 发音 ----
    def speak(self, text):
        """自动朗读：受「音:开/关」控制。"""
        if self.tts_on.get():
            self.tts.speak(text)

    def _toggle_tts(self):
        self.tts_on.set(not self.tts_on.get())
        self.store.set_setting("tts", self.tts_on.get())
        self._render_tts_btn()
        if self.tts_on.get() and self.mode == "study" and self.current:
            self.tts.speak(self.current["word"])

    def _speak_current(self):
        if self.mode == "study" and self.current:
            self.tts.speak(self.current["word"])  # 手动点按钮无视开关
        elif self.mode == "choice" and getattr(self, "choice_answer", None):
            self.tts.speak(self.choice_answer["word"])
        elif self.mode == "spell" and getattr(self, "spell_answer", None):
            self.tts.speak(self.spell_answer["word"])

    # ---- 主题与透明 ----
    def _apply_title_bar(self):
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if self.theme_name == "dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
        except Exception:
            pass

    def toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.store.set_setting("theme", self.theme_name)
        self.T = THEMES[self.theme_name]
        if self.history_win is not None and self.history_win.winfo_exists():
            self.history_win.destroy()  # 换主题后重开即是新配色
        self._apply_title_bar()
        self._build_ui()
        self._populate_card()

    def cycle_alpha(self):
        idx = ALPHA_STEPS.index(self.alpha) if self.alpha in ALPHA_STEPS else 0
        self.alpha = ALPHA_STEPS[(idx + 1) % len(ALPHA_STEPS)]
        self.store.set_setting("alpha", self.alpha)
        self.root.attributes("-alpha", self.alpha)
        self.alpha_btn.config(text=f"{int(self.alpha * 100)}%")

    def _hover_enter(self, _event):
        if self.alpha < 1.0 and not self.closed:
            self.root.attributes("-alpha", 1.0)

    def _hover_leave(self, _event):
        if self.alpha >= 1.0 or self.closed:
            return
        x, y = self.root.winfo_pointerxy()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        inside = rx <= x < rx + self.root.winfo_width() and ry <= y < ry + self.root.winfo_height()
        if not inside:
            self.root.attributes("-alpha", self.alpha)

    # ---- 通用 ----
    def switch_dict(self, name):
        self.store.set_setting("dict", name)
        self.dict_name = name
        self.words = load_dict(self.dicts[name])
        self.dict_box.set(name)
        self.current = None
        self.back_stack = []
        self.set_mode(self.mode)

    def set_mode(self, mode):
        self.mode = mode
        self.store.set_setting("mode", mode)
        self._cancel_timers()
        for k, b in self.mode_btns.items():
            b.config(bg=self.T["btn_active"] if k == mode else self.T["btn"])
        for child in self.card.winfo_children():
            child.destroy()
        if mode == "study":
            self._build_study()
        elif mode == "choice":
            self._build_choice()
        else:
            self._build_spell()
        self._update_status()

    def _cancel_timers(self):
        for attr in ("timer", "quiz_timer"):
            t = getattr(self, attr)
            if t is not None:
                self.root.after_cancel(t)
                setattr(self, attr, None)

    def _schedule(self, ms, fn):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
        self.timer = self.root.after(ms, fn)

    def _guard_keys(self, fn):
        if isinstance(self.root.focus_get(), tk.Entry):
            return  # 默写输入时不要触发快捷键
        if self.mode == "study":
            fn()

    def _update_status(self):
        marked_today, quiz = self.store.today_counts()
        studied = sum(1 for s in self.store.dict_words(self.dict_name).values() if s.get("mark"))
        self.status.config(text=f"{self.dict_name} 共 {len(self.words)} 词 · 已背 {studied} · 今日背 {marked_today} 测 {quiz}")

    def _update_history_view(self):
        if self.history_win is not None and self.history_win.winfo_exists():
            self._fill_history()

    # ---- 背词模式 ----
    def _build_study(self):
        c, T = self.card, self.T
        top = tk.Frame(c, bg=T["card"])
        top.pack(fill="x", padx=12, pady=(8, 0))
        self.progress_label = tk.Label(top, bg=T["card"], fg=T["dim"], font=("Segoe UI", 9))
        self.progress_label.pack(side="left")
        self.mark_badge = tk.Label(top, bg=T["card"], fg=T["dim"], font=("Microsoft YaHei UI", 9))
        self.mark_badge.pack(side="right")

        self.word_label = tk.Label(c, bg=T["card"], fg=T["fg"], font=("Segoe UI", 25, "bold"), wraplength=440)
        self.word_label.pack(pady=(8, 0))
        self.phonetic_label = tk.Label(c, bg=T["card"], fg=T["dim"], font=("Segoe UI", 12))
        self.phonetic_label.pack()
        self.meaning_label = tk.Label(c, bg=T["card"], fg=T["good"], font=("Microsoft YaHei UI", 11),
                                      wraplength=440, justify="center")
        self.meaning_label.pack(pady=(6, 0), fill="x")

        marks = tk.Frame(c, bg=T["card"])
        marks.pack(side="bottom", pady=(0, 10))
        for i, (key, (label, ckey)) in enumerate(MARKS.items(), 1):
            tk.Button(marks, text=f"{label}({i})", command=lambda k=key: self.set_mark(k),
                      bg=T["btn"], fg=T[ckey], activebackground=T["btn_active"], activeforeground=T[ckey],
                      relief="flat", padx=10, pady=2, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=5)

        nav = tk.Frame(c, bg=T["card"])
        nav.pack(side="bottom", pady=(4, 2))
        for text, cmd in (("← 上一个", self.prev_word), ("发音", self._speak_current),
                          ("显示释义", self.reveal), ("下一个 →", self.next_word)):
            tk.Button(nav, text=text, command=cmd, bg=T["btn"], fg=T["fg"],
                      activebackground=T["btn_active"], activeforeground=T["fg"],
                      relief="flat", padx=10, pady=2, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=5)

        if self.current is None:
            entry, _ = self._linear_entry()
            self.show_entry(entry)
        else:
            self.show_entry(self.current)  # 换主题/切模式回来时保留当前卡片

    def _linear_entry(self):
        """线性顺序（带记忆的乱序全表轮转）里当前位置的词。"""
        prog = self.store.get_order(self.dict_name, len(self.words))
        return self.words[prog["order"][prog["pos"]]], prog

    def _advance_linear(self):
        _, prog = self._linear_entry()
        pos = prog["pos"] + 1
        if pos >= len(self.words):
            prog = self.store.reshuffle(self.dict_name, len(self.words))
        else:
            self.store.set_pos(self.dict_name, pos)
        return self.words[prog["order"][prog["pos"]]]

    def _draw_next(self):
        """按 STUDY_WEIGHTS 比重决定：出新词（推进线性进度）还是复背已打标的词。"""
        recent = {e["word"] for e in self.back_stack[-RECENT_BLOCK:]}
        if self.current:
            recent.add(self.current["word"])
        pools = build_review_pools(self.words, self.store.dict_words(self.dict_name), recent)
        cat = draw_study_category(pools)
        if cat == "new":
            return self._advance_linear(), False
        return random.choice(pools[cat]), True

    def show_entry(self, entry, review=False):
        self.current = entry
        self.revealed = False
        self.seen_recorded = False
        self.word_label.config(text=entry["word"])
        self.phonetic_label.config(text=entry.get("phonetic", ""))
        self.meaning_label.config(text="· · ·")
        _, prog = self._linear_entry()
        self.progress_label.config(
            text=f"{prog['pos'] + 1} / {len(self.words)}" + (" · 复背" if review else ""))
        stat = self.store.dict_words(self.dict_name).get(entry["word"])
        if stat and stat.get("mark"):
            label, ckey = MARKS[stat["mark"]]
            self.mark_badge.config(text=f"已标·{label}", fg=self.T[ckey])
        elif stat and stat["seen"] > 0:
            self.mark_badge.config(text=f"看过 {stat['seen']} 次", fg=self.T["dim"])
        else:
            self.mark_badge.config(text="新词", fg=self.T["dim"])
        self.speak(entry["word"])
        if self.autoplay.get():
            self._schedule(REVEAL_MS, self.reveal)

    def reveal(self):
        if self.current is None:
            return
        entry = self.current
        self.revealed = True
        self.meaning_label.config(text=entry["meaning"])
        if not self.seen_recorded:
            self.seen_recorded = True
            self.store.bump_seen(self.dict_name, entry["word"])
        if self.autoplay.get():
            self._schedule(NEXT_MS, self.next_word)

    def next_word(self):
        if self.current:
            self.back_stack.append(self.current)
            del self.back_stack[:-50]
        entry, review = self._draw_next()
        self.show_entry(entry, review)

    def prev_word(self):
        if self.back_stack:
            self.show_entry(self.back_stack.pop())

    def set_mark(self, kind):
        if self.current is None:
            return
        entry = self.current
        self.store.record(self.dict_name, entry["word"], f"mark:{kind}")
        if not self.seen_recorded:
            self.seen_recorded = True
            self.store.bump_seen(self.dict_name, entry["word"])
        self._update_status()
        self._update_history_view()
        if not self.revealed:
            # 没看释义就打标：先亮释义预览两秒再进下一个
            self.revealed = True
            self.meaning_label.config(text=entry["meaning"])
            self._schedule(MARK_PREVIEW_MS, self.next_word)
        else:
            self.next_word()

    def _toggle_autoplay(self):
        self.autoplay.set(not self.autoplay.get())
        self._render_auto_btn()
        self.store.set_setting("autoplay", self.autoplay.get())
        if self.mode != "study":
            return
        if self.autoplay.get():
            self._schedule(NEXT_MS if self.revealed else REVEAL_MS,
                           self.next_word if self.revealed else self.reveal)
        else:
            self._cancel_timers()

    def _on_space(self, _event):
        if isinstance(self.root.focus_get(), tk.Entry):
            return
        if self.mode == "study":
            if self.revealed:
                self.next_word()
            else:
                self.reveal()
        elif self.mode == "choice" and getattr(self, "choice_done", False):
            self._new_choice()  # 答完后按空格进下一题

    # ---- 选择模式 ----
    def _build_choice(self):
        c, T = self.card, self.T
        self.choice_prompt = tk.Label(c, bg=T["card"], fg=T["fg"], font=("Segoe UI", 22, "bold"))
        self.choice_prompt.pack(pady=(14, 0))
        self.choice_prompt.bind("<Button-1>", lambda e: self._speak_current())  # 点单词发音
        self.choice_phonetic = tk.Label(c, bg=T["card"], fg=T["dim"], font=("Segoe UI", 11))
        self.choice_phonetic.pack()
        self.choice_frame = tk.Frame(c, bg=T["card"])
        self.choice_frame.pack(fill="both", expand=True, padx=14, pady=(8, 10))
        self._new_choice()

    def _remember_quiz(self, word):
        self.quiz_recent.append(word)
        del self.quiz_recent[:-3]

    def _cancel_quiz_timer(self):
        if self.quiz_timer is not None:
            self.root.after_cancel(self.quiz_timer)
            self.quiz_timer = None

    def _new_choice(self):
        self._cancel_quiz_timer()
        for child in self.choice_frame.winfo_children():
            child.destroy()
        stats = self.store.dict_words(self.dict_name)
        entry = pick_quiz_word(self.words, stats, exclude=self.quiz_recent)
        if entry is None:
            self.choice_prompt.config(text="题库是空的")
            self.choice_phonetic.config(text="在「背词」模式给单词打标（认识/模糊/不认识）后才会出题")
            return
        T = self.T
        self.choice_answer = entry
        self.choice_done = False
        self._remember_quiz(entry["word"])
        self.choice_prompt.config(text=entry["word"])
        self.choice_phonetic.config(text=entry.get("phonetic", ""))
        self.speak(entry["word"])
        self.choice_btns = []
        for meaning, is_correct in make_choices(self.words, entry):
            b = tk.Button(self.choice_frame, text=trim(meaning), anchor="w", justify="left",
                          bg=T["btn"], fg=T["fg"], activebackground=T["btn_active"], activeforeground=T["fg"],
                          relief="flat", font=("Microsoft YaHei UI", 10), wraplength=410, padx=8)
            b.config(command=lambda btn=b, ok=is_correct: self._answer_choice(btn, ok))
            b.pack(fill="x", pady=2)
            self.choice_btns.append((b, is_correct))

    def _answer_choice(self, btn, is_correct):
        if self.choice_done:
            return
        self.choice_done = True
        word = self.choice_answer["word"]
        if is_correct:
            btn.config(bg=self.T["right_bg"])
            self.store.record(self.dict_name, word, "mc_r")
        else:
            btn.config(bg=self.T["wrong_bg"])
            for b, ok in self.choice_btns:
                if ok:
                    b.config(bg=self.T["right_bg"])
            self.store.record(self.dict_name, word, "mc_w")
        self._update_status()
        self._update_history_view()
        if is_correct:
            self.quiz_timer = self.root.after(1400, self._new_choice)
        else:
            # 答错不倒计时：留足时间对照正确答案，手动继续
            tk.Button(self.choice_frame, text="下一题 →（空格）", command=self._new_choice,
                      bg=self.T["btn"], fg=self.T["fg"],
                      activebackground=self.T["btn_active"], activeforeground=self.T["fg"],
                      relief="flat", padx=12, pady=2,
                      font=("Microsoft YaHei UI", 9)).pack(pady=(6, 0))

    # ---- 默写模式 ----
    def _build_spell(self):
        c, T = self.card, self.T
        self.spell_meaning = tk.Label(c, bg=T["card"], fg=T["good"], font=("Microsoft YaHei UI", 11),
                                      wraplength=440, justify="center")
        self.spell_meaning.pack(pady=(16, 2))
        self.spell_hint = tk.Label(c, bg=T["card"], fg=T["dim"], font=("Segoe UI", 11))
        self.spell_hint.pack()
        self.spell_feedback = tk.Label(c, bg=T["card"], fg=T["fg"], font=("Segoe UI", 16, "bold"))
        self.spell_feedback.pack(pady=(4, 0))

        row = tk.Frame(c, bg=T["card"])
        row.pack(side="bottom", pady=(0, 14))
        self.spell_entry = tk.Entry(row, bg=T["entry"], fg=T["fg"], insertbackground=T["fg"],
                                    relief="flat", font=("Segoe UI", 13), width=22, justify="center")
        self.spell_entry.pack(side="left", ipady=4, padx=(0, 8))
        self.spell_entry.bind("<Return>", lambda e: self._check_spell())
        tk.Button(row, text="提交", command=self._check_spell, bg=T["btn"], fg=T["fg"],
                  activebackground=T["btn_active"], activeforeground=T["fg"], relief="flat",
                  padx=10, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=2)
        tk.Button(row, text="不会", command=self._give_up_spell, bg=T["btn"], fg=T["dim"],
                  activebackground=T["btn_active"], activeforeground=T["dim"], relief="flat",
                  padx=10, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=2)
        tk.Button(row, text="发音", command=self._speak_current, bg=T["btn"], fg=T["dim"],
                  activebackground=T["btn_active"], activeforeground=T["dim"], relief="flat",
                  padx=10, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=2)
        self._new_spell()

    def _new_spell(self):
        self._cancel_quiz_timer()
        stats = self.store.dict_words(self.dict_name)
        entry = pick_quiz_word(self.words, stats, exclude=self.quiz_recent)
        if entry is None:
            self.spell_meaning.config(text="题库是空的：在「背词」模式给单词打标后才会出题")
            self.spell_hint.config(text="")
            return
        self.spell_answer = entry
        self.spell_done = False
        self._remember_quiz(entry["word"])
        self.speak(entry["word"])  # 听写提示：结合释义和读音拼写
        self.spell_meaning.config(text=trim(entry["meaning"], 80))
        self.spell_hint.config(text=f"{len(entry['word'])} 个字母，首字母 {entry['word'][0]}")
        self.spell_feedback.config(text="")
        self.spell_entry.delete(0, "end")
        self.spell_entry.focus_set()

    def _check_spell(self):
        if not hasattr(self, "spell_answer"):
            return
        if getattr(self, "spell_done", True):
            self._new_spell()  # 已揭晓答案：回车/提交 = 下一题
            return
        guess = self.spell_entry.get().strip().lower()
        if not guess:
            return
        word = self.spell_answer["word"]
        self.spell_done = True
        if guess == word.lower():
            self.spell_feedback.config(text=f"✓ {word}", fg=self.T["good"])
            self.store.record(self.dict_name, word, "sp_r")
            self.quiz_timer = self.root.after(1000, self._new_spell)  # 答对才自动进下一题
        else:
            # 答错不倒计时：留足时间对照，回车/提交手动继续
            self.spell_feedback.config(text=f"✗ {word} {self.spell_answer.get('phonetic', '')}", fg=self.T["bad"])
            self.spell_hint.config(text="看清楚了？回车 / 提交 → 下一题")
            self.store.record(self.dict_name, word, "sp_w")
            self.speak(word)
        self._update_status()
        self._update_history_view()

    def _give_up_spell(self):
        if getattr(self, "spell_done", True) or not hasattr(self, "spell_answer"):
            return
        word = self.spell_answer["word"]
        self.spell_done = True
        self.spell_feedback.config(text=f"{word} {self.spell_answer.get('phonetic', '')}", fg=self.T["warn"])
        self.spell_hint.config(text="看清楚了？回车 / 提交 → 下一题")
        self.speak(word)
        self.store.record(self.dict_name, word, "sp_w")
        self._update_status()
        self._update_history_view()

    # ---- 历史 ----
    def open_history(self):
        if self.history_win is not None and self.history_win.winfo_exists():
            self.history_win.lift()
            return
        T = self.T
        win = tk.Toplevel(self.root)
        self.history_win = win
        win.title("背诵历史")
        win.configure(bg=T["bg"])
        win.attributes("-topmost", True)
        win.geometry(f"400x320+{self.root.winfo_x() - 410}+{self.root.winfo_y()}")
        self.history_summary = tk.Label(win, bg=T["bg"], fg=T["fg"], font=("Microsoft YaHei UI", 10))
        self.history_summary.pack(pady=(10, 4))
        frame = tk.Frame(win, bg=T["bg"])
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        self.history_list = tk.Listbox(frame, bg=T["card"], fg=T["fg"], font=("Microsoft YaHei UI", 9),
                                       relief="flat", yscrollcommand=sb.set, selectbackground=T["btn_active"])
        self.history_list.pack(fill="both", expand=True)
        sb.config(command=self.history_list.yview)
        self._fill_history()

    def _fill_history(self):
        stats = self.store.dict_words(self.dict_name)
        studied = [s for s in stats.values() if s.get("mark")]
        marks = {k: sum(1 for s in studied if s.get("mark") == k) for k in MARKS}
        self.history_summary.config(
            text=f"{self.dict_name}：已背 {len(studied)} 词 · "
                 f"认识 {marks['know']} · 模糊 {marks['fuzzy']} · 不认识 {marks['unknown']}")
        self.history_list.delete(0, "end")
        for ev in reversed(self.store.data["history"][-200:]):
            t = time.strftime("%m-%d %H:%M", time.localtime(ev["t"]))
            label = EVENT_LABELS.get(ev["event"], ev["event"])
            self.history_list.insert("end", f" {t}  [{ev['dict']}] {ev['word']}  —  {label}")

    # ---- 保存与退出 ----
    def _autosave_tick(self):
        self.store.save()
        self.root.after(3000, self._autosave_tick)

    def on_close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.store.save(force=True)
            self.tts.stop()
        finally:
            self.root.destroy()


# ---------------------------------------------------------------- 入口

def main():
    srv = acquire_lock()
    if srv is None:
        sys.exit(0)  # 已有窗口在运行

    dicts = list_dicts()
    store = Store()
    root = tk.Tk()
    app = App(root, dicts, store)
    threading.Thread(
        target=watch_control,
        args=(srv, lambda: root.after(0, app.on_close)),
        daemon=True,
    ).start()
    try:
        root.mainloop()
    finally:
        try:
            store.save(force=True)
        except Exception:
            pass
        srv.close()


if __name__ == "__main__":
    main()
