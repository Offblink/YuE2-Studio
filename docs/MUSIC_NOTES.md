# YuE2 Studio（独立面板）

面板 = 「写风格 + 歌词 + 歌名 → 出歌 / 出 ABC 乐谱 → 试听 + 历史管理」的那层 WebUI，**与模型/ComfyUI 解耦**：
不 import ComfyUI 的任何东西、不用它的 venv、不假设它就在隔壁。全部实测记录（2026-09-26）。

命令行那条路（批量/自动化）是另一套脚本，**两者写同一个产物文件夹**，能力等价
（本面板多出：试听、歌词/属性页、一首歌一张卡的历史、自动生成、权重续传进度、真实步数进度条）。

## 与 ComfyUI 的唯一接缝

两个接触点：**ComfyUI 的根目录**（读 `output\`）和 **HTTP `127.0.0.1:8188`**。
根目录写在同目录 `studio.json`，所以面板可以放任何地方、指向任何一份 ComfyUI（同机）：

```json
{ "comfy_root": "../ComfyUI", "songs_dir": "" }
```

| studio.json | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `comfy_root` | `YUE2_COMFY_ROOT` | `<面板>\ComfyUI` | ComfyUI 根目录；**相对路径按面板目录解析** |
| `output_dir` | `YUE2_OUTPUT_DIR` | `<comfy_root>\output` | ComfyUI 原始落点（`output\audio\` 的 flac） |
| `songs_dir` | `YUE2_SONGS_DIR` | `<output_dir>\music` | **产物目录**：历史读这儿、新歌拷这儿 |
| `comfy_python` | `YUE2_COMFY_PYTHON` | `<comfy_root>\.venv\Scripts\python.exe` | 拉起 ComfyUI 用的解释器 |
| `comfy_log` | `YUE2_COMFY_LOG` | `<comfy_root>\comfy_server.log` | 拉起时它的 stdout；同时读 `.err`（文件名随启动器，见下） |
| `comfy_cmd` | — | `["main.py","--listen","127.0.0.1","--port",<comfy_port>]` | 相对 `comfy_root` 执行 |
| `comfy_autostart` | `YUE2_COMFY_AUTOSTART` | `true` | `false` = 面板只当前端，绝不自己拉服务 |
| `presets` | — | 内置四组中文预设 | 创作页 `曲风 / 人声 / 乐器 / BPM` 的清单 |
| `llm`（可选） | `YUE2_LLM_*` | 无 | **自动生成**用的大模型（见下） |
| — | `YUE2_PANEL_PORT` | `8190` | 面板端口（画布 8189、ComfyUI 8188） |
| — | `YUE2_COMFY_PORT` | `8188` | ComfyUI 端口 |
| — | `YUE2_PYTHON` | 面板 `.venv` → 系统 Python | 起面板用的解释器（`yue2_studio.ps1` 读） |

命令行 `yue2_studio.py --port N` 优先级最高；`--print-config` 打印解析后的全部配置后退出。

**命名**：项目原来叫 `qwen-music`、环境变量 `QWEN_*`，2026-09-26 改掉 —— 出歌的是 YuE2，跟 Qwen 无关。
现在仓库里 `qwen` 只出现在**姊妹项目 Qwen-canvas**（同门那套设计语言）的引用里；
`comfy_log` 默认取 `<comfy_root>\comfy_server.log`，你的启动器若写别的日志名，把那路径写进 `studio.json` 就行。
**WebUI 品牌只留英文 `YuE2 Studio`**，入口文件名 `打开乐坊.cmd` 与目录名保持中文。

## 目录与入口

```
<仓库根>\
├── 打开乐坊.cmd / yue2_studio.ps1    起面板 + 开浏览器（-NoBrowser 只起；-Stop 只停面板，**不碰 ComfyUI**）
├── studio.json(.example)             指向哪份 ComfyUI、产物放哪儿、presets、llm（本机那份已 gitignore）
├── yue2_studio.py                     面板服务（**纯标准库**：http.server + urllib + socket + threading）
├── yue2_studio.html                   面板前端（单文件、无框架、无构建）
├── vendor\gsap.min.js / Flip.min.js  动效库，**随仓内置、不连 CDN**
├── README.md                         面向读者（截图在 docs\panel-*.png）
└── docs\MUSIC_NOTES.md               本文件（面板口径）
```

- **没有 `.venv`、也不需要**：服务端只用标准库（Pillow/aiohttp/websockets 一个都不要）。
- 拉起 ComfyUI 用 `CREATE_NO_WINDOW`（0x08000000），**不要 `DETACHED_PROCESS`**（否则每次冷启弹黑窗，
  验收要枚举窗口，别只看任务栏）。
- **重启面板必须等 8190 释放**：`-Stop` 后轮询 `netstat` 确认没有 LISTENING 再起，否则新进程端口占用直接退出。
- **改 `.html` 不用重启服务**（每次从磁盘读），**改 `.py` 要重启**。
- **顶栏和 `#body` 是同一套两栏栅格**：`#wbMain` / `#wbSide` 的 `flex`、`min-width:330px`、`max-width:660px`
  与左右 padding 必须和 `#main` / `#side` 一模一样，否则右上角的页签会和侧栏错开
  （实测对齐判据：`#tabs` 的 `x`/`width` 与 `#pages` 的**完全相同**，908.67 / 440.33）。

## 产物与命名（一首歌 = 同名几件）

| 文件 | 谁写的 | 作用 |
|---|---|---|
| `<歌名>.flac` | `_run_songs()` 从 ComfyUI 的 `output\audio\` **拷贝**过来（不动原件） | 音频 |
| `<歌名>.abc` | 同一次生成的 `YuE2GenerateABC` 文本（`mode != off` 才有） | 乐谱 |
| `<歌名>.json` | `_write_meta()` | **这一首的全部参数**（歌名 / 风格 / 歌词 / 时长上限 / 步数 / 种子 / 模式 / 时间 / 耗时 / 峰值显存 / 两个产物名 / ComfyUI 原始文件名） |

- **歌名**：创作页那个输入框，留空才回落到 `song_<YYYYmmdd_HHMMSS>`。落盘前过 `safe_label()`
  （保留中文；`<>:"/\|?*` 与控制字符换 `_`；首尾的点/下划线/空格去掉；限 60 字；`CON`/`NUL` 这类前缀 `_`），
  重名按 `_2`/`_3` 加后缀（`_unique()`）。
- **老产物**（改版前生成）：`song_<ts>.flac` + `plan_<ts>.abc` 靠**前缀互换**配对（`_pair_stem()`），
  没有 `.json` → 「歌词」页不出现，「属性」页只列文件本身的信息。
- **列表分组**：`_pair_key()` 把 `song_<ts>` / `plan_<ts>` 归到 `<ts>`，新产物就是主干本身；
  `list_songs()` 按这个 key 合成**一首一项**（`{stem, song, plan, meta, bytes, mtime, time, seconds}`），
  `bytes` 优先报音频的大小。

## 界面

新拟态、零原生控件；设计语言取自姊妹项目 Qwen-canvas。**布局是主区 2/3 + 侧栏 1/3**（实测 66.1% / 33.9%）。

| 区域 | 内容 |
|---|---|
| **顶栏** | **两栏，和下面 `主区 / 侧栏` 同宽同内边距**（`#wbMain` flex:2 / `#wbSide` flex:1 + min/max-width 都照抄 `#main` / `#side`）：左边品牌、中间 ComfyUI 灯 + 版本 + 显存 + `ComfyUI 原生界面`、右边 `创作 / 历史` 页签（**从侧栏顶上来了**，`#tabs{flex:1}` 填满那一栏 → 与侧栏像素级对齐）。原生的「拉起 ComfyUI」按钮已经去掉：出歌会自动拉起，顶栏那个「原生界面」没在跑时也会先拉起再开 |
| **主区 2/3** | **音乐 / 歌词 / 属性 / 乐谱**四页，靠**底部切换滑块**换（顶部没有页签） |
| **侧栏 1/3** | 页签已在顶栏，这里就是两块：**创作**（自动生成 / 歌名 / 风格四组 / 歌词 / 参数）与**历史**（**一首歌一张卡** + ✎ 改名 / × 删除） |
| **状态栏** | **左右两端都咬住内容边距（16px）**：左边 `phase` + 已用秒数（与品牌/主区同一条竖线）+ **定长进度条**（真实步数）+ 速度/ETA + 队列；右边 `停止生成` + 消息（消息是**最后一个**元素，所以右缘正好压在边距上）。顺序是 `[status][elapsed][prog][note][queue][grow][停止生成][msg]` |

**历史 = 一首歌一行**（这是用户口径："不如历史列表放一整首歌，点开展开为音乐、歌词与属性，这样就不乱了"）：
磁盘上是一件件产物，列表里只按 `stem` 出一张卡（**不写「歌曲」徽标了** —— 列表里基本全是歌，
只有"纯乐谱"那条能看出来：副行没有「音乐」），副行写着这一首**能展开成哪几页**
（`音乐 · 歌词 · 属性 · 乐谱`，有的才列）。表头是产物目录 + **「打开目录」**按钮（工具提示里给完整路径）。
点卡片 → `openGroup()`：有音频就载入播放台并播放、主区切「音乐」，同时 `loadInfo()`
把「歌词」「属性」两页一起填好（懒切不花钱）。

**卡片右上角两个按钮**（`.sng .acts`：并排、钉在右边、`✎` 在 `×` 左边；`.sng:hover` 才淡入，
`pointer-events` 一起切 —— **验收时点它必须先 hover**，否则 Playwright 等可点性会超时）：

- `✎` **改名** → `promptBox()`（`modalBox({input:true})`，预填当前歌名、Enter/Esc）→ `POST /api/rename`。
  改的是**文件名主干**，服务端把 `_group_files()` 那几件一起 rename，并回写 `.json` 的
  `label`（`name`/`audio`/`plan` 也同步）→ **「属性」页里歌名立刻是新的**（前端重拉 `/api/meta`）。
  撞名**拒绝**（409，不学生成那样加 `_2`）；空名/全非法字符 400；路径穿越 404。
  当前选中的那一首改完会 `loadSong(新名)` 重新指过去（`abcName` 也换），所以播放位置回到开头 —— 改名本来就会断一下。
- `×` **删除** → `askDelete()` 确认框 → `/api/delete` 传裸名 = 删这一首的全部产物。
  **正在播放的那首也能删**（2026-09-27 用户报的 bug，已确定性复现：`/song` 传输线程攥着文件时
  `unlink` 必 WinError 32）。现在三层：前端确认后**先停播、卸掉 `audio.src`**（浏览器不再读，
  面板那头的句柄随连接断开）→ 服务端 `_unlink_wait()` 对 `PermissionError` 重试 8×0.12s
  （瞬时占用 0.4s 实测接住）→ 真被别的程序占着就回人话「文件正被占用……停一下再删」
  （不甩 WinError 与绝对路径）。删除成功后主区退回「还没有歌」；删失败也退，不留按不动的播放器。

**底部滑块只显示有的那几页**：`grpViews(g)` → `{music: 有音频, lyrics: 有 json, meta: 一直有, score: 有谱}`，
`#mainFoot` 只在可用页 ≥ 2 时出现；只有一页就不摆开关。滑块切页时历史里的选中高亮跟着走（`paintCurCard()` 比 `curStem`）。
「属性」页对老产物也开（退化成文件信息 + 同门产物），所以**一首只有 flac 的歌也是 音乐 / 属性 两页**。
实测：`foot: True`；音乐↔歌词↔属性↔乐谱往返都对，且**选中始终停在这一首那张卡上**。

**属性页的盒子贴着行数长**（`#viewMeta{overflow:auto}`，`#metaBox` 不设 `flex`/`height`）——
不留一大片空的；行多了整页滚，不是盒子内滚。实测：12 行 → 盒高 **389**（行高合计 365 + 内边距），
主区 581，底下就是空白。歌词页相反（`#lyricsBox{flex:1}` 撑满，长歌词才好看）。

**生成完创作页自动清空**（歌名 / 歌词 / 风格句 / 四组选中 / 时长·步数·数量·种子·模式回默认）：
**前提是这次作业真的写出了 `.json`**（`results[].meta === true`）—— 参数已经在那一首自己的页里，
创作页清干净留给下一首；写参数失败（磁盘满之类）就不清，免得把输入弄丢。

**创作页 · 风格四组分开点**：`曲风 / 人声 / 乐器 / BPM` 各一行（`presets` 给，中文），每组单选、再点一下取消。
选中项拼成下面那行中文风格句 —— **那行就是发给模型的原话**；手改那行 = 自定义（四组点亮**不动**），
再点任何一组会按四组**重拼一次**覆盖手打的。BPM 行只显示数字（`88`），完整片段 `88 BPM` 放 `title`。
第一次进来输入框还空着就铺一套默认（流行 / 女声 / 钢琴 / 88 BPM），保证「生成」按钮开箱可用。

**创作页 · 自动生成（可选）**：写一句话 → `/api/autotune` → **歌词 + 四组 + 时长 + 步数 + 数量 + 乐谱规划 + 歌名**
一次填好，**然后停手**（绝不提交，`busy` 仍是 `null`），点不点「生成歌曲」永远由用户决定。
**歌名的取法**（2026-09-27 用户口径写进 `_autotune_system()`）：先定歌词、**从副歌 `[Chorus]` 那一段取意起名** ——
歌名要接得住副歌讲的事/核心意象，**不是把用户提示词缩写**。
**纯音乐清歌词**（2026-09-27 用户报的 bug）：以前手点「无人声（纯音乐）」或自动生成填出纯音乐，歌词框都
**不动** —— 上次留下的歌词会被原样发给模型。现在三层兜底：① 提示词要求纯音乐时 `lyrics` 给 `""`；
② `normalize_autotune()` 只要人声含「纯音乐」就**强制** `ch["lyrics"]=""`（模型写了也覆盖）；
③ 前端点 chip 与应用自动填结果时都调 `clearLyricsForInstrumental()` 清框，且 `ch.lyrics` 为空串也按
「清空」处理（改前 `if(ch.lyrics)` 会把空串跳过 —— 这就是清不掉的直接原因）。
等的时候有**加载动画**（`setAutoBusy(true)`）：按钮里 `::before` 转一个小圈（`@keyframes spin`），
文字变 **「放弃生成」**、**按钮保持可点**（点它就是 `abortAuto()` → `AbortController.abort()` 丢掉这次请求），
只锁输入框。**没有**额外那条横条（早期版本在按钮下面加了一条 `#autoBar` 来回滑，被用户否掉了：
"你都有上面那个转动的圆环了"）。状态栏那条进度条也不动它 —— 它归作业轮询管（2.5 s 一次的 `idleBar()` 会擦掉）。
`prefers-reduced-motion` 下动画被全局关掉，所以给那个圈补了"静止也看得出在等"的样式。
实测：忙态 `label=放弃生成` / `disabled=false` / `::before=spin`；点一下 → `label=自动生成`、
状态栏「已放弃这次自动生成 —— 参数一个都没改」、歌名与歌词**都没被填**（`ERR_ABORTED` 就是那一下）；
紧接着完整跑一次仍正常（33.4 s，歌名 + 四组 + 12 行歌词都到位）。注意**服务端那次上游调用会跑完**（只是结果被丢掉）。
`llm` 段：`base_url / model / api_key_env / proxy / timeout / max_tokens`（**任何 OpenAI 兼容端点**都行）。
**api_key 不写进 studio.json**：先看环境变量 `api_key_env`（默认 `YUE2_LLM_API_KEY`，也认 `YUE2_LLM_API_KEY`），
读不到再去 Windows 注册表里找同名用户变量。三样（端点 + 模型名 + key）缺哪样，`/api/state.llm.why` 就说哪样，
前端把这句话写进那一格的副标题、并把两个控件禁用 —— 出歌链路零影响。

**锁什么**：只锁 `[生成歌曲] / [只出乐谱 ABC] / [下载权重]`（防重复提交，一次请求吃满整张卡）。
其余全放开：歌词风格随便改、页签随便切、历史随便翻随便删、播放台照听 —— 因为这些全是**提交那一刻才读**的状态。
**作业跑完创作页会清空**（歌名/歌词/风格句/四组/数字回默认，见上），前提是参数已经写进那一首自己的 `.json`；
失败或取消就不清，免得把输入弄丢。（早期版本是"歌词不清空、方便再来一遍"，加了 `<歌名>.json` 之后改掉了：
同一首的参数在「属性」页随时能翻，创作页清干净更好写下一首。）

**播放台**：`fetch(/song)` → WebAudio `decodeAudioData` → 每 260 段取峰值画柱，解不出来退化成普通进度条。
**播放模式**（2026-09-27 加，**暂停键右边的圆形按钮** `#btnLoop`（`.pbtn`，和播放键同款）三档轮换：
历史循环（默认）/ 单曲循环 / 随机播放 —— 按用户裁决**去掉了「循环关」档**，播完永远接着放；
图标是**自画 inline SVG**（历史 = 循环箭头、单曲 = 循环里带 1、随机 = 交叉箭头，
`LOOP_ICONS` 里换，档位名放 `title`/`aria-label`/状态栏）：`ended` 之后由
`playAfterEnd()` 接管 —— 单曲 = `currentTime=0` 重播；历史 = 按 `songs` 的**展示序**
（新在前）取下一首、尾接头（怎么接见下「接歌不跳页」）；随机 = 从**有音频**的条目里挑一首非当前的
（列表只有一首就重播）。只有 `it.song` 非空的条目参与接歌（只出过乐谱的卡播不了，跳过）。
**接歌不跳页**（2026-09-27 用户口径：「页面不变，但歌曲切换」—— 歌词页就还是歌词页，内容换新歌的）：
`playAfterEnd` 走 `advanceGroup(it, play)` 而不是点卡片的 `openGroup` —— `openGroup` 会
`showMain('music')` 强制跳页；`advanceGroup` 只做 `curStem → loadSong →（当前页是乐谱才）loadAbc
→ loadInfo → updateSwitch → paintCurCard`，当前页对新歌没内容时由 `updateSwitch` 回落到第一页有内容的。
实测：歌词页接歌（霓虹里的虚拟歌姬 → 你哥）歌词页保留、`lyricsBox` 换成新歌的词；
乐谱页接歌（你哥 → 写给你的情书）`abcName` 跟着换。
**播放条的「下载」pill 已删**（2026-09-27 用户：文件本来就在本机产物目录里，下它没有意义）；
**乐谱页的「下载」pill 同批删掉**（同一理由，`btnDlAbc` 的按钮/事件/禁用联动全清，「复制」保留）。
`<audio>` 隐藏，播放/暂停/进度/时间全自绘；点卡片载入即播放（用户手势，浏览器允许），**生成完成不自动播**。

## 进度口径（不编百分比）

**状态栏那条是画出来的进度条**（`#prog`，固定 132px 的轨道 + 里面的填充），**不是** tqdm 的字符画 ——
日志尾行里的 `▉` 方块与 `[00:24<00:42, 51.9it/s]` 在**服务端**就被 `_fmt_note()` 剥成
`3% · 971 / 2250 · 51.9 it/s · 还剩 00:42`，前端只拿到这句人话。
**状态栏的每格都是定宽的**（`#elapsed` 9ch / `#note` 30ch / `#queue` 20ch，超出省略号），
`#prog` 与 `#btnStopJob` 用 `visibility` 占位而不是 `display:none` ——
所以数字在跳、按钮显隐，右边的 `停止生成` **一动不动**（用户口径）。

1. **真实步数**：面板自带 80 行最小 WebSocket 客户端（`socket + base64 + struct`，处理分片帧与 ping/pong），
   连 `ws://127.0.0.1:<port>/ws?clientId=<client_id>` 读 `{"type":"progress","data":{value,max}}`。
   **ws 与 `/prompt` 必须同一个 `client_id`**（ComfyUI 只把进度发给提交者）。实测：ABC 规划 `83→534 / 8192`、
   音乐采样 `85/2250`、KSampler `1→32 / 32`。**ws 连不上只让进度条来回滑**，绝不拖垮作业。
2. **日志尾行**：tqdm 只落在 `comfy_log`（默认 `<comfy_root>\comfy_server.log`）里，
   读出来当 `job.note`（已剥成上面那句短句）；`comfy_log` 指不到文件时这一格就是空的，进度条照常。
3. **峰值显存**：作业期间每 1.5 s 读一次 `nvidia-smi`，没有就 `null`。
4. 三样都没有就只显示 `phase` + 已用秒数 + 来回滑的条，**不出现任何编出来的百分比**。

### 日志读取的三个坑（都实测踩过，已修）

1. **同一份日志里两种编码混写**：模型/采样行是**本地代码页 cp936**（按 UTF-8 读出一串 `?`），
   带 ANSI 色的 `[INFO]` 行是 UTF-8（按 cp936 读出 `鈻堚枅`）→ **逐行**定编码：先试严格 UTF-8，失败才按 cp936。
2. **tqdm 用 `\r` 在同一行上覆盖**，日志里一个"行"塞着整段历史 → 取 `split("\r")` 后**最后一个**像进度的片段，
   否则状态栏永远停在 `0%| 0/8192`。
3. **ANSI 色码会漏到页面上**（`[32m[INFO][0m`）→ 先正则剥掉，再剥控制字符，最后截 120。
4. **`_fmt_note()` 要连方块一起剥**：`_TQDM` 正则把它拆成 `pct · done/total · rate · 还剩 eta`，
   非 tqdm 的行也要过一遍 `_BLOCKS`（`▉▊▋…░▒▓`）—— 否则页面上就是一条字符画进度条（用户明确不要）。

## 接口（`127.0.0.1:8190`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | `yue2_studio.html`（每次读盘，改 html 不用重启） |
| GET | `/vendor/<name>.js` | `vendor\` 下的裸 `.js`（防穿越） |
| GET | `/api/state` | 在线状态 / 版本 / `autostart` / `ckpt` / `gpu` / **`presets`** / **`llm`** / `songs`（**一首一项**，见上） / `busy` / `songs_dir` |
| GET | `/api/status` | `job`（跑完也留着，前端读结果与错误）+ `queue.pending` + `download` |
| GET | `/api/log?lines=N` | 日志尾部的进度行（已剥 ANSI / `\r` 归一 / **方块条剥成数字**） |
| POST | `/api/generate` | `{name, style, lyrics, seconds, steps, seed, mode, count}` → 202；忙 409；参数错 400；权重没好 503 |
| POST | `/api/plan` | `{name, style, lyrics, seed, mode}` → 只出 ABC（落盘 `<歌名>.abc` + `.json`，`results[0].text` 带文本） |
| POST | `/api/autotune` | `{prompt}` → `{changes, unmatched}`（changes 里含 `name`）；**只回参数，不提交**。没配 key 503、提示词空 400、模型出错 502 |
| POST | `/api/cancel` | 给 ComfyUI 发 `/interrupt`，作业标 `已取消` |
| POST | `/api/start` | 按配置拉起 ComfyUI 并等就绪 |
| POST | `/api/download` | `curl.exe -L --fail -C - -o <ckpt> <url>` 断点续传 + 完了核 sha256 |
| POST | `/api/open_dir` | 在资源管理器里打开产物目录并**把它提到前台**（`_open_path()`；返回 `raised`）。非 Windows 退到 open/xdg-open |
| POST | `/api/rename` | `{name, to}`：改歌名 = 改文件名主干（三件同名产物一起 rename + `.json` 回写 label/name/audio/plan）。撞名 409、空名 400、没这一首/路径穿越 404 |
| POST | `/api/delete` | `{name}`：**裸名 = 删这一首的全部产物**（返回 `deleted`）；带后缀 = 只删那一个（`.flac`/`.abc`/`.json`）。`..`/子路径一律 404 |
| GET | `/api/meta?name=` | 这一首的 `<stem>.json`；老产物没有就 `{meta: null}`（不编内容） |
| GET | `/song?name=` | 音频，支持 `Range`（206）—— 播放与拖进度靠它 |
| GET | `/text?name=` | ABC 文本（`text/plain; charset=utf-8`） |

状态机：同一时刻**只跑一个作业**（`count>1` 时循环，`index` 递增），`phase` 走
`准备中 → 乐谱规划中 → 采样中 → 解码中 → 完成/失败/已取消`（下载任务是 `下载中`）。
作业结束后 `job` 保留到下一次任务开始，`busy` 只在真跑着时非空。

## 出歌图：怎么把同次生成的乐谱一起取出来

`mode != off` 时 `graph_song()` 除了 `YuE2GenerateABC(24)` 还挂一个 **`PreviewAny(14)`**
（`{"source":["24",0]}`）。没有它，`/history` 的 `outputs` 里**就没有 `text`** —— 谱拿不到，配对也无从谈起。
出完歌 `_run_songs()` 把 `outputs` 里的 ABC 写成 **`<同一个 stem>.abc`**（与 `<stem>.flac`、`<stem>.json` 同名），
前端按 stem 分组 → 这一首自然就有「音乐 / 乐谱 / 属性」三页。`mode=off` 不提交 ABC 节点 ⇒ 没有谱那一页。

## 自动调参（可选）的实测口径

| 项 | 值 |
|---|---|
| 实测配置 | 任何 OpenAI 兼容端点（`POST <base_url>/chat/completions`）：`base_url` / `model` 填 studio.json，**key 走环境变量**（默认 `YUE2_LLM_API_KEY`，Windows 上读不到还会去注册表找同名用户变量）；需要的话 `proxy` 单独给（本机服务之间的调用不走代理） |
| 延迟 | **53–56 s**（思考型模型；按钮变「想好了…」） |
| 提交前校验 | 四组值**必须逐字落在 `presets` 清单里**，否则丢掉并进 `unmatched`（前端提示「没对上清单」）；`seconds 10-900`、`steps 1-80`、`count 1-4`、`mode` 白名单全部夹逼 |
| 不碰 GPU | 调完 `/api/state` 的 `busy` 仍是 `null`，ComfyUI 一次请求都没收到 |
| 实测样例 | `给朋友的生日歌，中文流行，女声，轻快，一分半，顺便出个谱` → 歌名「生日歌」+ 流行/女声/钢琴/96 BPM + `seconds 90`（一分半 ✓）+ `mode full`（要谱 ✓）+ 四段中文歌词 + 一句选择理由（**2026-09-26 的行为**；2026-09-27 起歌名改按副歌取意，同一句会起出别的名字，见上「歌名的取法」） |
| provider 的教训 | key 没额度时是 4xx（实测见过 **402 Payment Required**）→ 前端收到 502 + 原文案，不吞 |
| 思考型模型 | `max_tokens` 给 4000（给少了正文被思考吃光、返回空串）；空正文**直接报错**并提示调 `llm.max_tokens`，不拿空串糊弄 |
| 没配好的提示 | `/api/state.llm.why` 会给一句人话（缺 base/model 还是缺 key），前端把它写进那一格的副标题 |

## 实测（5070 Ti Laptop 12 GB / ComfyUI 0.37.0）

| 动作 | 耗时 | 备注 |
|---|---|---|
| 只出乐谱（API / 页面） | **15.2 s / 18.3 s** | 页面那次含 ComfyUI 就绪等待 |
| 出歌 全谱 full（90 秒上限） | **52.4 s** | 产物 34.6 s flac |
| 出歌 全谱 full（150 秒上限、中文歌词） | **73.7 s** | 产物与乐谱**成对落盘**，滑块出现 |
| 出歌 off（60 秒上限，页面点的） | **58.9 s** | 产物 1:01 / 4.3 MB flac |
| 自动填写歌词 + 全部参数 | **53–56 s** | 不碰 GPU |
| 出歌 全谱 full（60 秒上限、20 步、**带歌名**） | **61.1 s**（采样 56.1 s） | 落盘 `<歌名>.flac` / `.abc` / `.json` 三件同名，峰值 **4872 MiB** |
| 峰值显存 | **4872–5040 MiB** | 12 GB 卡余量很大 |

- **中文全链路逐字符核过**：POST 体 → `/prompt` → `/history` 里 `inputs.style` / `inputs.lyrics` 与提交的一字不差。
  Python 侧是 `json.dumps(..., ensure_ascii=False).encode("utf-8")` + `charset=utf-8`，
  没有 PS 5.1 字符串 body 按本地代码页编码那个坑，但**别把它"顺手简化"回去**。
- 悬停/焦点按 canvas 的房规实测核过：`×` 静息 opacity 0 → 悬停 1；`#btnGen` 悬停阴影 `8px → 10px` 且**rect 不变**
  （935×49）；真实 `Tab` → `:focus-visible` → `2px solid rgb(108,122,224)`。
- 删除链路：自绘确认框 → `Esc` 取消留文件 → 确认删盘上文件，历史少一张。
- 守卫：忙 → 409、空歌词 → 400、`../music.json`/`x.txt`/空名 → 404 非法文件名、`Range` → 206。

## 改代码时踩过的坑（改 `yue2_studio.html` / `.py` 前必读）

1. **按 marker 切片重建 DOM 结构时，必须断言"关键 id 都在"**：2026-09-26 把布局从「左播放台 + 右三页签」
   改成「主区 2/3 + 侧栏 1/3」时，切片取了 `workbar` 和 `pages`，**中间的 `#tabs` 整块被落下** ——
   页签一个都不渲染、两个 `.page` 全部 `display:none`，页面看着"右栏是空的"。
   改完扫一遍 id 清单（`main / side / viewPlayer / viewScore / mainSwitch / preGroups / page-gen / page-hist …`）才抓住。
2. **少传一个实参会静默降级**：`paintJob(j, logTail)` 漏了 `/api/status` 的原始返回 →
   `st.download`/`st.queue` 恒 `undefined`（下载进度环与队列失效），而且 `logTail` 落到了 `st` 的位置。
   现在是 `paintJob(j, s, logTail)`。
3. **`makeSeg` 只给 `get()` 没法被"自动生成"驱动** → 加了 `set(v)`（自动调参要切乐谱规划档）。
4. **作者工具的坑**：工具调用参数里出现字面 `\u0000` 会被 JSON 解码成真 NUL 字节，**把整条载荷截断**
   （表现为"改了个寂寞、还没报错"）。要匹配源码里的 `\u0000` 字面量，用**下标切片**或 `"\\u0000"` 拼，别直接写。
5. 含中文的 `.ps1` 必须 **UTF-8 + BOM**、`.cmd` 必须**纯 ASCII + CRLF**；从 cmd 里调这些 `.cmd` 时给 stdin 喂 NUL。
6. **两个面板能同时 bind 8190**（Windows + `allow_reuse_address`）→ 你改完起来看着是新的，
   答话的却可能是那个老进程（实测：`/api/state` 一直给老行为）。`main()` 现在**先探 `/api/state`**，
   有人在答就直接退出 1；停面板用 `yue2_studio.ps1 -Stop`（它按 `CommandLine like '*yue2_studio.py*'` 找进程）。
7. **大模型的 key 不一定在自己的进程环境里**：`setx`/用户变量写的是注册表，比它更早起来的 shell
   （或服务方式起的进程）继承不到 → 面板把「自动生成」禁用了，而用户觉得"我明明配了"。
   `_llm_key()` 现在**环境变量 → 注册表**（`HKCU\Environment`，再 `HKLM\...\Session Manager\Environment`）两级找。
   实测：`env -u YUE2_LLM_API_KEY python -c "import yue2_studio; print(yue2_studio.llm_ready())"` → `True`。
   `llm_ready()` 现在要求**端点 + 模型名 + key 三样齐**（默认全是空 = 不挑厂商），缺哪样由 `llm_why()` 说明。
8. **改名的撞名判据必须按"分组 key"，不能只看文件名**：`song_<ts>.flac` / `plan_<ts>.abc`（老命名）
   与 `<ts>.flac`（新命名）**是同一首**（`_pair_key()` 归一到 `<ts>`）。第一版只查 `<new>.flac/.abc/.json`
   是否存在，于是把「测试歌」改成 `20260926_214022` **没被拦**，结果两首并成一张卡、
   `_group_files()` 一下返回 6 个文件（改名/删除会连坐）。现在先算 `_pair_key(new)` 再跟
   **别的**那一首的 key 比（实测：`{"to":"20260926_214022"}` 与 `{"to":"song_20260926_214022"}` 都是 409）。
9. **删元素时搜一遍"还有谁在引用它"**：卡片徽标那版去掉 `bd`（`歌曲` 徽标）时漏了下面那行
   `hd.append(bd, nm, acts)` → `renderSongs()` 抛 `ReferenceError: bd is not defined`，
   表现是**整个历史列表空白**（渲染在异常处中断），而页面其它部分照常（状态栏照样刷新、ComfyUI 灯照样亮）——
   光看界面容易以为是"没数据"。改 DOM 相关代码后**必看控制台**（`tab.errors()`），别只看截图。
10. **播放连续性：同一首不要重载**。`loadSong()` 会 `pause()` + 换 src，位置归零 ——
   所以 `openGroup()` / 切「音乐」页都必须先比 `cur === g.song`，一样就只切页。
   改名那种**必须**换 src 的场景用 `loadSong(name, {seek: at})`：等 `loadedmetadata` 再把
   `currentTime` 接回去、按原状态决定是否继续播。实测（headless 也测得了）：
   切页往返 t 5→7.3、再点同一张卡 →8.8 都没归零；改名 19.6 s 处 → 22.9 s 仍在放、src 已是新名。
11. **后台进程开的窗口默认只会在任务栏闪**：Windows 的**前台锁**不允许非前台进程
   `SetForegroundWindow`，所以「打开目录」第一版 `os.startfile()` 的结果是"任务栏多一枚图标、窗口不弹"（用户实测）。
   现在 `_open_path()` 开完再 `EnumWindows` 找 class=`CabinetWClass` 且标题匹配目录名的窗口，
   用标准手法**把本线程输入挂到当前前台窗口的线程上**（`AttachThreadInput`）再 `ShowWindow(SW_RESTORE)` +
   `BringWindowToTop` + `SetForegroundWindow`，最后摘掉。实测：调用前前台是 Edge，
   调用后前台 = `CabinetWClass` / `music - 文件资源管理器`，接口回 `raised: true`（最多等 ~2 s）。

## 验收配方（改完怎么快速证一遍）

```bash
curl -s http://127.0.0.1:8190/api/state      # 看指向、ckpt、presets、llm.ready、songs（应是**一首一项**）
curl -s http://127.0.0.1:8190/api/status     # 看 job / queue / download
curl -s "http://127.0.0.1:8190/api/meta?name=<某首.flac>"        # 有 json 就回参数，老产物回 meta:null
curl -s -r 0-1023 -D - "http://127.0.0.1:8190/song?name=<某首.flac>"   # 206 = Range 通
curl -s "http://127.0.0.1:8190/song?name=../music.json"           # 必须 404 非法文件名
curl -s -X POST -d '{"name":"../x"}' http://127.0.0.1:8190/api/delete   # 必须 404（路径不进 delete）
```

浏览器里（真页面，不要只数 DOM）：
- 布局：`#main` / `#side` 的 `getBoundingClientRect()` 宽度比 ≈ **2 : 1**；
- 预设：点 `曲风` 第二个 → `#style` 变成 `流行, …`；再点一下 → 这组熄灭、那一段从句子里消失；
- 自动生成：填一句话点按钮 → 歌名/四组/三个步进器/模式/歌词全变，**`busy` 必须还是 `null`**；
- **历史一卡一首**：产物件数 > 卡片数（三件产物只出一张卡），卡片副行写着「音乐 · 歌词 · 属性 · 乐谱」里有的那几个；
- **四页滑块**：点卡片 → 主区「音乐」+ `#mainFoot.hidden === false` → 依次点 `歌词 / 属性 / 乐谱`
  （没有的那几个按钮必须 `hidden`），每切一次 **`.sng.cur` 都还停在这一首**；
- **生成后清空**：出一首（歌名非空）→ 完成后 `#songName / #lyrics / #style` 全空、四组全灭、
  时长/步数/数量/种子/模式回默认；`/api/state` 里新产物是**一项**（`song/plan/meta` 齐）；
- **状态栏不抖**：作业中连点两次量 `#btnStopJob` 与 `#prog` 的 `getBoundingClientRect()`，**x/width 必须一模一样**；
- 悬停：`×` 显隐、`#btnGen` 阴影整串变化、`rect` 不变；
- **截图 + `getBoundingClientRect()` 一起看**：只数元素个数发现不了"容器塌成 0×0"这类回归。

## 边界

- 只支持**同机** ComfyUI（直接读它的 `output\`）；跨机器要另做走 `/view` 的版本。
- 单用户本地工具：**无鉴权**，`/api/delete` 能删产物目录里的文件 —— **别往公网暴露**。
- **自动生成会把提示词发给 `llm` 配置的服务商**；不想要就别配 `llm`。
- 出歌权重 **CC-BY-NC-4.0（非商用）**，选权重前先确认授权。
- 面板只管**拉起** ComfyUI，不管停：**停用你自己那套停法**（避免打断别的面板的连接）。
- 8188 一次只能吃一个作业；出歌时别同时跑别的吃显存的任务。

