# CLVocab 技术规范

本文档描述实现细节与对外契约，面向想改代码、排查问题或让 AI 助手代为维护的人。

## 架构总览

```
Claude Code 会话 A ──UserPromptSubmit──> clvocab_start.py ──"start A"──┐
Claude Code 会话 B ──UserPromptSubmit──> clvocab_start.py ──"start B"──┤
                                                                    v
                                                    clvocab.py（tkinter 单实例）
                                                    监听 127.0.0.1:<PORT> 控制通道
                                                                    ^
Claude Code 会话 A ──Stop──> clvocab_stop.py ──"stop A"────────────────┤
Claude Code 会话 B ──Stop──> clvocab_stop.py ──"stop B"（全空才关窗）──┘
```

- 零第三方依赖：Python 3.8+ 标准库（tkinter/socket/json/subprocess/threading）。
- 仅支持 Windows（TTS 用 System.Speech，深色标题栏用 DWM API；核心 GUI 逻辑跨平台，
  但未在 macOS/Linux 测试）。

## 端口与单实例

- 候选端口：`PORTS = (28471, 29873, 31597, 32851)`，按序尝试绑定 127.0.0.1。
- 绑定失败且错误为 `EADDRINUSE`/WinError 10048 → 已有实例，直接退出；
  其他错误（典型：WinError 10013，端口落在 Windows 动态保留范围）→ 试下一个候选。
- 绑定成功的监听 socket 同时充当单实例锁和控制通道。

## 控制协议

一行 UTF-8 文本一条消息，服务端处理后回复 `ok\n`：

| 消息 | 语义 |
|---|---|
| `start <session_id>` | 登记一个正在干活的会话（记录时间戳） |
| `stop <session_id>` | 注销该会话；**注销后登记表为空则关窗** |
| 裸连接（连上即断，无数据） | 无条件关窗（手动关闭手段） |
| 其他/未知命令 | 忽略，仍回复 ok |

- 登记条目 8 小时过期（`SESSION_TTL`），防止会话崩溃后窗口永不关闭。
- 客户端（钩子脚本）必须校验 `ok` 应答——候选端口可能被无关程序占用。
- 探活请用 `netstat`，不要用 TCP 连接试探（裸连接会关窗）。

## 钩子契约

Claude Code 通过 stdin 向钩子命令传 JSON（含 `session_id` 等字段）。

- `clvocab_start.py`：读 session_id → 发 `start`；无应答则启动 `clvocab.py`
  （pythonw 优先，回退 python + CREATE_NO_WINDOW），最长重试 6 秒。
- `clvocab_stop.py`：读 session_id → 发 `stop`；无弹窗运行则静默退出。
- **编码注意**：stdin 必须按字节读、`utf-8-sig` 解码
  （PowerShell 管道带 UTF-8 BOM；中文 Windows 的 Python 默认按 GBK 解码 stdin，
  BOM 字节会连带吞掉 JSON 首字符）。

安装形态二选一：

1. **Claude Code 插件**：`hooks/hooks.json` 用 `${CLAUDE_PLUGIN_ROOT}` 定位脚本，
   `command: "python"` 依赖 PATH 中有带 tkinter 的 Python。
2. **install.py**：把钩子（用运行时 `sys.executable` 的绝对路径）合并进
   `~/.claude/settings.json`，幂等，可 `--uninstall`。

## 数据文件

### 词书 `dicts/<名称>.json`

```json
[{"word": "cancel", "phonetic": "/'kænsl/", "meaning": "n. 取消…；vt. 取消…"}]
```

文件名（去掉 .json）即词书显示名。由 `tools/build_dicts.py` 从 ECDICT 按考试标签
（cet4/cet6/ky/ielts/toefl/gre）生成，释义行以 `；` 连接、行首带词性。

### 学习状态 `state.json`

位置：`~/.clvocab/state.json`（旧位置 `~/.vocab_popup` 或程序目录 `state.json` 存在则原地沿用）。
原子写入（tmp + `os.replace`），每 3 秒自动落盘，关窗强制落盘。

```json
{
  "settings": {"dict": "CET4", "mode": "study", "autoplay": false,
                "tts": true, "theme": "dark", "alpha": 1.0, "scope": "all"},
  "progress": {"CET4": {"order": [/*打乱的下标*/], "pos": 42}},
  "words": {"CET4": {"cancel": {"seen": 3, "mark": "fuzzy", "last": 0.0,
                                  "mc_r": 1, "mc_w": 0, "sp_r": 0, "sp_w": 1}}},
  "history": [{"t": 0.0, "dict": "CET4", "word": "cancel", "event": "mark:fuzzy"}]
}
```

- `mark` ∈ `know | fuzzy | unknown | null`；**打过标才算"已背"**（进题库、计入统计）。
- `seen` 是浏览计数，不进 history（自动播放刷词不刷屏历史）。
- history 事件：`mark:*`、`mc_r/mc_w`（选择对/错）、`sp_r/sp_w`（默写对/错），
  封顶 1000 条（`HISTORY_CAP`）。
- 词书文件词数变化时该词书的 `progress` 自动重建（乱序重排、pos 归零），
  `words` 按单词名索引所以打标/成绩不受影响。

## 算法

### 词表范围（`filter_scope`）

顶栏「全部/生词/已背」循环切换（`scope` ∈ `all | new | studied`，持久化），三种模式共用：
生词 = 未打标的词，已背 = 打过标的词。背词的线性进度指针始终基于全表那一份持久顺序，
范围外的词在推进时跳过（生词模式下打完标的词自然退出轮转）；范围为空时给对应提示。

### 背词出词（`_draw_next`）

每次"下一个"按 `STUDY_WEIGHTS = {"new": 60, "unknown": 20, "fuzzy": 12, "know": 8}`
加权抽类别；空池的类别不参与。`new` 推进带记忆的乱序全表轮转（走完重排），
其余从对应打标池随机抽（最近 `RECENT_BLOCK = 8` 个出过的词除外）。
复背卡片带「复背」角标。没看释义就打标 → 先亮释义 `MARK_PREVIEW_MS = 2000` 再换词。

### 测验抽题（`pick_quiz_word` / `quiz_weight` / `quiz_urgency`）

题库 = 当前词表范围内的词（`pick_quiz_word` 的 `marked_only=False` 配合 `filter_scope`；
「已背」范围即传统的只考打过标的词）。每个词带**错题记忆曲线**状态：
`box`（0..6 格）与 `due`（下次复测时间）。

- 答错 → `box=0`，`due = now + 5分钟`；答对 → `box+1`，
  间隔按 `QUIZ_INTERVALS` 拉长：5分钟 → 30分钟 → 2小时 → 8小时 → 1天 → 3天 → 7天
- 权重 = `(1 + 打标难度 + 慢性净错) × 到期系数`，封顶 10
  - 打标难度：unknown +3 / fuzzy +1.5
  - 慢性净错：max(0, 答错 - 0.75×答对)，封顶 2 —— 长期答不对的词略微常驻
  - 到期系数 0.1 ~ 3.0（以当前格子的间隔为尺度）：未到期压低但不清零
    （刚答错的词 5 分钟内先歇，避免无意义的立刻重问）；过期越久越紧迫，封顶 3 倍
- 从没考过的词 `due=0` → 视为严重过期，立即进入循环；旧数据无字段时同样按此兜底

最近考过的 3 个词不重复抽（题库全被排除时回退全池）。答对自动进下一题
（选择 1.4s / 默写 1s）；答错/不会**不自动跳题**，手动继续。

## TTS

常驻一个隐藏 PowerShell 进程跑 `System.Speech.Synthesis.SpeechSynthesizer`，
按行读 stdin 朗读，新词 `SpeakAsyncCancelAll` 打断旧词；优先选 `en*` 文化的语音。
进程挂掉在下次 speak 时自动重启；关窗时 kill。

## 调试

- 设 `VOCAB_DEBUG=1` 启动弹窗 → 控制通道日志写程序目录 `control.log`。
- 手动关窗（PowerShell）：`(New-Object Net.Sockets.TcpClient('127.0.0.1', 28471)).Close()`
  （按 PORTS 顺序试到成功为止）。
