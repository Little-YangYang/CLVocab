# CLVocab — Claude 干活时的背单词弹窗

等 Claude Code 跑长任务的碎片时间，正好背几个单词。

你给 Claude 发消息时，屏幕角落自动弹出一个置顶小窗开始出词；Claude 回答完毕窗口自动关闭。
并行开多个会话也不怕——所有会话都干完活窗口才消失。

- **完整考试词表**：CET4 3849 / CET6 5407 / 考研 4801 / 雅思 5040 / 托福 6974 / GRE 7504 词，
  释义带词性、单词带音标（数据来自 [ECDICT](https://github.com/skywind3000/ECDICT)）
- **三种模式**：背词（可自动播放、打标 认识/模糊/不认识）· 选择题 · 默写（听写式），
  共用「**全部 / 生词 / 已背**」词表范围切换（生词=未打标，已背=打过标）
- **懂复习**：打过标的词按权重回流复背，不认识的出现最勤；测验带**错题记忆曲线**
  （答错 5 分钟后回流，答对逐级拉长到 7 天）；答错停住给复习时间，不倒计时赶人
- **TTS 发音**：出词/出题自动朗读（Windows 自带语音合成，零依赖，可关）
- **不打扰**：深色/浅色主题、透明度可调（半透明时鼠标移入自动变清晰）、
  进度/打标/偏好全部持久化
- 仅支持 Windows；Python 3.8+（官方安装包自带 tkinter 即可），无第三方依赖

## 安装

### 方式一：让 Claude 帮你装（推荐）

把下面这句话发给你的 Claude Code（桌面版或 CLI 均可）：

> 帮我安装这个背单词弹窗：https://github.com/Little-YangYang/CLVocab
> 按仓库 README 里「给 Claude 的安装步骤」操作。

### 方式二：作为 Claude Code 插件

```
/plugin marketplace add Little-YangYang/CLVocab
/plugin install clvocab@clvocab
```

要求 PATH 里的 `python` 是带 tkinter 的 Python 3.8+。

### 方式三：手动安装

```
git clone https://github.com/Little-YangYang/CLVocab.git
cd CLVocab
python install.py
```

重启 Claude Code（或开新会话）后发送任意消息即可看到弹窗。
卸载：`python install.py --uninstall`（插件方式则 `/plugin uninstall`）。

## 给 Claude 的安装步骤

<!-- 如果你是被用户要求安装本项目的 Claude：按以下步骤执行 -->

1. 确认环境：Windows 系统；`python --version` ≥ 3.8 且
   `python -c "import tkinter"` 不报错（否则提示用户安装官方版 Python 并勾选 tcl/tk）。
2. 把仓库克隆到一个**稳定路径**（比如用户主目录下 `~/CLVocab`，
   不要放临时目录——钩子会长期引用该路径）。
3. 在仓库目录运行 `python install.py`，它会把两条钩子（UserPromptSubmit/Stop，
   使用当前解释器绝对路径）幂等地合并进 `~/.claude/settings.json`，并自动备份原配置。
4. 运行 `python -m py_compile clvocab.py clvocab_start.py clvocab_stop.py` 验证无语法错误。
5. 告诉用户：重启 Claude Code 或开新会话后，发任意消息就会弹出背单词窗口；
   Esc 或右上角 × 可手动关闭；学习进度存在 `~/.clvocab/state.json`。

## 使用说明

| 模式 | 玩法 |
|---|---|
| 背词 | 出词 → 释义。`自动:开/关` 控制自动播放（默认关，节奏可调）。新词与复背按 60/20/12/8 的比重乱序混合（新词/不认识/模糊/认识），复背卡片带「复背」角标，最近 8 词不重复。没看释义就打标会先亮 2 秒释义预览。切「生词」范围=纯刷新词（打完标自动退出范围），切「已背」=纯复习 |
| 选择 | 四选一选释义。题库=当前词表范围（切「已背」即只考打过标的），按**错题记忆曲线**抽题：答错的词 5 分钟后回流重考，答对一次升一格（5分钟→30分钟→2小时→8小时→1天→3天→7天），答错打回第一格；标记难的、长期净答错的权重更高，没考过的词视为立即到期，最近 3 题不重复。答对 1.4s 自动下一题；答错停住，按钮/空格继续 |
| 默写 | 看释义+听发音拼单词，有字母数和首字母提示。题库与抽题规则同选择题。答对 1s 自动下一题；答错/不会停住看答案，回车或提交继续 |

只浏览不打标的词记"看过 N 次"，**不算已背**；「今日背」统计当天打标数。

**顶栏**：词书切换 ▾ · 词表范围（全部/生词/已背 循环）· 背词/选择/默写 · 浅色/深色 ·
透明度（100%→90%→75%→60% 循环，半透明时鼠标移入自动恢复清晰）· 音:开/关（自动朗读）·
自动:开/关（自动播放）· 历史

**快捷键**：`空格` 显示释义/下一个 · `←/→` 上/下一个 · `1/2/3` 标记认识/模糊/不认识 · `Esc` 关窗

**发音**：出词、出题自动朗读（受`音:开/关`控制）；背词/默写的「发音」按钮和
选择题点击单词则点了就读。

## 自定义

- **换/加词书**：往 `dicts/` 丢一个 `[{"word","phonetic","meaning"}, ...]` 格式的 JSON
  即可，文件名就是词书名。要重新生成或增加考试标签（中考/高考等），
  跑 `python tools/build_dicts.py`（详见脚本注释）。
- **调节奏/比重**：改 `clvocab.py` 顶部常量——`REVEAL_MS`/`NEXT_MS`（自动播放节奏）、
  `STUDY_WEIGHTS`（复背比重）、`RECENT_BLOCK`（防重复窗口）、`MARK_PREVIEW_MS`（打标预览时长）。
- 更多实现细节（控制协议、状态文件格式、算法）见 [SPEC.md](SPEC.md)。

## 工作原理（一句话版）

两条 Claude Code 钩子：UserPromptSubmit 时 `clvocab_start.py` 向弹窗登记会话（没弹窗先启动），
Stop 时 `clvocab_stop.py` 注销会话；弹窗监听本地端口做单实例锁 + 会话计数，
所有会话注销完才关窗。详见 [SPEC.md](SPEC.md)。

## 许可

代码以 [MIT License](LICENSE) 发布。`dicts/` 下的词书由
[ECDICT](https://github.com/skywind3000/ECDICT)（MIT）生成，见 `tools/build_dicts.py`。
