# OpenReview Crawler

给一篇论文（OpenReview 链接 / 论文标题 / arXiv 号 / DOI），抓出它在 OpenReview 上的**全部审稿人信息、审稿意见、审稿问题、作者回复和后续讨论**，整理成方便边读论文边查阅的 Markdown 与**单文件离线 HTML** 报告。

**纯 Python 标准库实现，零依赖** —— 不需要 pip install，不需要联网 CDN，生成的报告是一个可以直接发给别人的自包含 HTML 文件。

> **English:** A zero-dependency Python tool that, given a paper (OpenReview link, title, arXiv id or DOI), pulls all reviewers, reviews, questions, author rebuttals and follow-up discussion from OpenReview and renders them into a single offline HTML report plus Markdown. Works with ICLR / NeurIPS / ICML / EMNLP / ACL and more, including their very different review field schemas. Python 3.9+ standard library only.

```
python crawl.py "https://openreview.net/forum?id=xxxxx"
python crawl.py "Chain-of-Thought Prompting Elicits Reasoning" --venue NeurIPS --list-candidates
python crawl.py --input-file papers.example.txt
```

---

## 目录

- [1. 为什么用它](#1-为什么用它)
- [2. 快速开始](#2-快速开始)
- [3. 输出结构](#3-输出结构)
- [4. Markdown 与 LaTeX 公式渲染](#4-markdown-与-latex-公式渲染)
- [5. 关于登录](#5-关于登录)
- [6. 可以喂什么](#6-可以喂什么)
- [7. 报告里有什么](#7-报告里有什么)
- [8. 常用参数](#8-常用参数)
- [9. 目录结构](#9-目录结构)
- [10. 自检](#10-自检)
- [11. 隐私与安全](#11-隐私与安全)
- [12. 已知限制](#12-已知限制)
- [13. 许可与致谢](#13-许可与致谢)

---

## 1. 为什么用它

审稿意见在 OpenReview 上是**几十条互相嵌套的 note**：审稿、作者 rebuttal、审稿人追问、AC 评论散在讨论串里，字段名每个会议还不一样。这个工具把它们收拢成一份能读的东西：

- **问答对照是核心**：把审稿人的问题拆成 Q1…Qn，再和作者的 rebuttal 配对。配对**以内容为准**而不是死认编号 —— 作者编号常和审稿人对不上（真实案例：审稿人问 Q1–Q3，作者按自己的提纲答 Q2–Q5）。配不上就如实标注"未找到对应回复"，**宁可少配，也不给一个看起来很像的错答案**。
- **跨会议适配**：ICLR / NeurIPS / ICML / EMNLP / ACL 的评审字段名、大小写、甚至有没有总评分都不一样。工具按会议逐个映射过，并把纯勾选项（行为准则确认、LLM 政策合规等）整类丢弃。
- **公式真正排版出来**，不是显示成 `$\mathcal{S}\_t$` 这样的源码。
- **单文件离线 HTML**：没有 CDN、没有外部字体、没有要加载的 JS 库。可折叠、可全文搜索、深色/浅色主题、可打印。

## 2. 快速开始

**环境要求：Python 3.9+。没有第三方依赖，不用 pip install。**

### 方式 A：图形界面（推荐给不习惯命令行的场景）

```bash
python tools/app_gui.py              # 打开窗口：登录 + 粘贴论文 + 抓取 + 打开报告
python tools/app_gui.py --login-only # 只弹登录框，用来刷新令牌
python tools/cleanup_gui.py          # 清理历史结果（安全白名单删除）
```

Windows 上还可以生成三个双击即用的 exe（用系统自带的 C# 编译器，每个约 7.5 KB，不需要下载任何东西）：

```bash
python tools/build_exe.py            # 或双击 build_exe.bat
# 生成：OpenReview爬虫.exe / OpenReview登录.exe / OpenReview清理.exe
```

`crawl.bat` / `login.bat` / `clean.bat` 是不用编译的等价入口，双击即可。

### 方式 B：命令行

```bash
# 先登录一次（见第 5 节；匿名也能用，但容易被 Cloudflare 人机校验挡住）
python crawl.py --login

# 单篇：直接给 OpenReview 链接
python crawl.py "https://openreview.net/forum?id=c93SBwz1Ma"

# 只有标题也能用
python crawl.py "BadChain: Backdoor Chain-of-Thought Prompting for Large Language Models"

# 有 arXiv 号 / DOI 也行，工具会先查标题再去 OpenReview 匹配
python crawl.py "arXiv:2401.12345"

# 批量：每行一条（# 开头是注释）
python crawl.py --input-file papers.example.txt -o ./results
```

## 3. 输出结构

默认在 `results/` 下，**每篇论文一个以标题命名的子文件夹**：

```
results/
├── BadChain Backdoor Chain-of-Thought Prompting for Large Language Models/
│   ├── BadChain Backdoor Chain-of-Thought Prompting for Large.html   ← 先看这个
│   ├── BadChain Backdoor Chain-of-Thought Prompting for Large.md
│   └── paper.json                    论文元信息（forum id / 评审数 / 决定 / 抓取时间）
├── Direct Preference Optimization Your Language Model is Secretly a/
│   ├── ...
├── report.html                       汇总报告：多篇分 tab（仅多篇时生成）
└── SUMMARY.md                        横向对比表（仅多篇时生成）
```

| 文件 | 说明 |
| --- | --- |
| `<论文标题>.html` | **推荐先看这个**。单文件、离线、可折叠、可全文搜索、Markdown 已渲染 |
| `<论文标题>.md` | Markdown，方便贴进笔记 / 二次加工 |
| `paper.json` | 该篇的元信息；重跑同一篇时会据此复用同一个文件夹，不会产生副本 |
| `report.html` | 批量时的汇总页，多篇论文分 tab（仅多篇时生成） |
| `SUMMARY.md` | 批量时的汇总表：平均分、评分区间、决定、全部审稿人评分一览 |

- 目录名和文件名会被清理掉 Windows 非法字符（`\ / : * ? " < > |`），中文标题原样保留。
- **命名长度按剩余路径预算动态计算**，不是写死的截断值。标题放得下就用完整标题；标题很长时**文件夹保留完整标题**，里面的文件命名为 `report.md` / `report.html` —— 因为文件夹才是你在资源管理器里读到的名字。只有极端长标题才会截断，且始终保证完整路径留在 Windows 的 260 字符限制以内。
- 想回到"所有文件直接堆在输出目录、以 forum id 命名"的老样子：加 `--flat`。
- 换输出目录：`-o 任意目录`。

> 注意：命令行与图形界面的默认输出目录是 `results/`；`crawl.bat` 这个交互式快捷入口输出到 `output/`。清理界面只清理 `results/`，用 `output/` 的话请手动删除。

## 4. Markdown 与 LaTeX 公式渲染

审稿人和作者在 OpenReview 里是**用 Markdown 写**的，所以报告里这些都会正常排版，而不是显示成一堆 `**` 和 `-`：

- 标题、段落、换行
- 有序 / 无序列表，含一层嵌套
- `**粗体**`、`*斜体*`、`~~删除线~~`
- 行内代码、``` 代码块
- 引用块、分隔线、管道表格
- `[链接](url)` 与裸链接
- **LaTeX 公式真正排版出来**：`$\mathcal{S}\_t$` 渲染成花体 *𝒮* 带下标 *t*，`$$obj = \sum_{i} weight_{i} \times cost_{i}$$` 渲染成带 ∑ 的居中公式（求和的 `i` 落在 Σ 下方）。

公式用的是 **MathML —— 浏览器自带的数学排版引擎**，和 OpenReview 用 KaTeX 达到的效果接近，但**不内嵌任何 JS 或字体**：报告仍是单个自包含文件，不联网、不变大。

- 分式有真正的分数线，根号有真正的根号符号，`\left(` 会随内容自动变大，上下标位置由浏览器的数学排版引擎决定。
- 支持：上下标（含 `x_i^2` 生成 `msubsup`）、希腊字母、`\mathcal`/`\mathbb`/`\mathfrak`/`\mathbf`/`\mathrm`/`\text`、运算符与关系符、`\frac`、`\sqrt`、重音符号、间距命令、`\\` 换行与 `&` 对齐（生成多行公式表）。
- `\mathcal{S}` / `\mathbb{R}` 会输出**真正的 Unicode 数学字母**（𝒮、ℝ），而不是依赖 `mathvariant` —— 后者在 MathML Core 里已不被一致支持。Unicode 区段有断档（花体 B/E/F/H/I/L/M/R 在 Letterlike Symbols 里），这些逐个显式映射，不靠区段推算。
- **认得转义下划线**：OpenReview 存的是 `$\mathcal{S}\_t$`（下划线被转义过），当成字面字符会丢掉整个下标 —— 按作者本意当下标处理。
- **拿不准就绝不猜**：遇到 `\begin{}` 环境、矩阵、`\atop` 这类结构，整个表达式**原样显示源码**（紫色等宽底纹，一眼可辨）。
- 原始 LaTeX 同时保存在 MathML 标准的 `<annotation encoding="application/x-tex">` 里，也在外层元素的 `title` / `data-tex` 上 —— 悬停即可核对。

页面上方有 **「显示原文」** 按钮：一键切回 OpenReview 存的原始 Markdown 源码（含未排版的 LaTeX）。这样万一渲染器在某个边界情况下判断错了，原文永远只有一次点击的距离。

> 渲染器是自己写的（标准库限制，不能装 `markdown` 包），目标是覆盖审稿文本实际会用到的子集，不是完整的 CommonMark。**先转义再解析**，所以审稿文本里的 `<script>` 之类的标签永远不会变成真标记（入库时 `clean_text` 还会先剥一层 HTML 标签）。

#### HTML 报告的界面

- **左侧目录**：论文列表 + 当前论文的审稿人快捷跳转；**全文搜索**（高亮命中，自动展开命中的审稿卡）；`全部 / 审稿意见 / 作者回复 / 讨论` 筛选；一键复制某位审稿人的完整意见（复制的是 Markdown 源码，方便贴进笔记）；多篇论文 tab 切换。
- **深色 / 浅色主题**：默认跟随系统，右上角 `◐` 按钮可手动切换，选择会被记住。主题在页面绘制前就已确定（`<head>` 里有一小段内联脚本），所以不会出现"先白闪一下再变黑"。
- **样式是一套完整的设计令牌系统**：颜色、圆角、阴影、间距全部命名复用，不是堆一次性的数值。正文有阅读宽度上限（约 78 字符），超宽屏上不会一行拉到屏幕边缘。
- **可访问性与打印**：键盘焦点有可见描边；锚点跳转留出顶栏高度；`prefers-reduced-motion` 下关闭动画；打印时自动展开全部折叠项、隐藏工具栏与侧边栏、改用黑白。

## 5. 关于登录

OpenReview 对**匿名**的 API 请求会概率性弹出 Cloudflare Turnstile 人机校验：

```
{"name":"ChallengeRequiredError","message":"Challenge verification required"}
```

这不是权限问题，你的网络也没坏。校验页面自己写着 *"Have an OpenReview account? Sign in to skip this check."* —— **登录即可绕过**。

### 令牌只有 24 小时，但不用你操心

令牌是 JWT，`iat` 到 `exp` **正好 86400 秒 = 24 小时**。而 `POST /refreshToken` 会返回：

```
400 No refresh token found
```

刷新令牌走的是**浏览器 cookie**，程序化登录拿不到。所以**没办法静默续期** —— 每天确实需要重新输入一次密码。

工具会在每次抓取前检查有效期，**过期了就直接要密码**：

- 图形界面 → 点「开始抓取」→ 如果令牌过期，会弹出一个密码框（邮箱已自动填好），输完自动继续抓取。
- 命令行 → 令牌过期会就地提示输入密码（输入不回显），输完继续。
- 抓取途中令牌被撤销（比如你在别处登出了）→ 自动重新登录并**重跑本轮抓取**，已抓的部分有缓存，不会白跑。

密码**只用于换取令牌，不保存、不落盘、不写日志**。要换账号或强制重登就用「清除令牌」/ `--logout`。

其它方式：

- `--token <TOKEN>`：直接给令牌（会留在命令历史里，不太推荐）
- 环境变量 `OPENREVIEW_TOKEN`；或 `OPENREVIEW_USER` + `OPENREVIEW_PASSWORD` 用于无人值守脚本
- 浏览器登录后从 DevTools → Application → Local Storage 复制 `openreview.token`，写进 `.openreview_token`
- `--no-prompt`：过期也不问，直接以匿名方式抓取（能抓，但容易被校验挡住）

## 6. 可以喂什么

| 输入 | 例子 |
| --- | --- |
| OpenReview 链接 | `https://openreview.net/forum?id=xxxxx`、`/pdf?id=`、`/notes?forum=` |
| forum / note id | `c93SBwz1Ma` |
| arXiv | `2401.12345`、`arXiv:2401.12345`、`https://arxiv.org/abs/2401.12345v2` |
| DOI | `10.48550/arXiv.2401.12345`、`https://doi.org/10.xxxx/yyyy` |
| 论文标题 | 直接贴标题，工具会搜索并按相似度排序 |

标题检索命中多个时会按相似度排序并列出候选。拿不准就先看看候选：

```bash
python crawl.py "论文标题" --list-candidates     # 只列候选，不抓取
python crawl.py "论文标题" --pick 2              # 直接选第 2 个
python crawl.py "论文标题" --venue ICLR -i       # 限定会议 + 交互选择
```

### 书目镜像会被自动跳过

搜索索引里的 arXiv / Crossref / DBLP 记录标题一样但**没有评审**，而且有些镜像的 venue 字段就写着干净的 "ICLR 2024"，光看标题和会议名根本分不出来。工具改用 `invitations` 和 `venueid` 识别它们，**并顺着记录里的链接自动跳到真正有评审的投稿**：

```
K7dOiGJWrZ   venueid=dblp.org/conf/ICLR/2024   书目记录（1 条 note）
             └─ html → forum?id=c93SBwz1Ma
c93SBwz1Ma   venueid=ICLR.cc/2024/Conference    真投稿（24 条 note，4 篇审稿）✓
```

同一篇论文可能同时投过 workshop 和主会，默认优先主会（`venueid` 含 `/Workshop` 的会轻微降权）；想抓 workshop 版本用 `--pick` 或 `--venue workshop` 指定。

## 7. 报告里有什么

- **论文信息**：标题、venue、作者、关键词、领域、平均评分、最终决定、TL;DR
- **审稿人对照总览**：每个审稿人一行 —— 匿名 ID、评分、置信度、Soundness / Presentation / Contribution、提问数、后续发言数、作者是否已回复、提交时间。评分底色按**本篇内部相对高低**着色（越绿越宽松），因为不同会议量纲不同（ICLR 1–10、NeurIPS 1–6、ICML 1–5）
- **审稿问题 ↔ 作者回复对照**：这是最实用的一节。配对以内容为准，每条配到的答案都会注明**对应作者回复里的哪一段**（如"对应作者 Comment 3"），方便回到讨论串核对。
- **审稿全文**：summary / strengths / weaknesses / questions / limitations / 各维度评分，未知字段也会原样列出，不做截断
- **讨论串**：审稿意见下的作者回复、审稿人追问、AC 评论，按时间嵌套还原，全文保留
- **元审稿意见与最终决定**

> ICLR 2024 等会议把作者 rebuttal 以 `Official_Comment` 形式发布，而不是 `/-/Rebuttal`。工具会**按签名判断**：`Official_Comment` 由 `Authors` 组发出即认定为作者回复，否则整篇 rebuttal 都会被当成普通讨论而漏掉。

### 不同会议的字段结构差异（已实测适配）

各会议的审稿字段名、大小写、甚至字段数量都不统一：

| 会议 | 评审是否公开 | 情况 |
| --- | --- | --- |
| **ICLR** | ✅ 公开 | `summary` / `strengths` / `weaknesses` / `questions`，标准结构，4 篇审稿 |
| **NeurIPS** | ✅ 公开 | 2024 用标准字段；2025 把正文合并在 `strengths_and_weaknesses`，评分改叫 `final_justification` 配合子项分，另带 `Mandatory_Acknowledgement` 确认声明 |
| **ICML** | ✅ 公开 | 2026 换了字段：评分在 `overall_recommendation`（不是 `rating`）、问题在 `key_questions_for_authors`、正文用 `**Strength**`/`**Weakness**` 分段 |
| **EMNLP / ACL 主会** | ✅ 公开 | 字段名**首字母大写**（`Questions_for_the_Authors`、`Reviewer_Confidence`），且**没有总评分** —— 它的分数在 `Excitement` 字段里 |
| **AAAI** | ❌ 无公开评审 | AAAI 的评审不在 OpenReview 上公开，能查到的基本是 `OpenReview.net/Public_Article` 文章记录，没有讨论串 |
| **ACL / ARR 投稿** | ❌ 无公开评审 | 投稿 note 是公开的，但评审不公开 —— 抽查多篇均只有 1 条 note |

针对这些差异做了如下处理：

- **字段匹配改为大小写不敏感**。EMNLP 的 `Questions_for_the_Authors` 之前完全认不出来，所有字段都被当成"未知字段"用拼音化的键名显示。现在按会议逐个映射过了。
- **评分字段按会议探测**（`rating` → `overall_recommendation` → `Excitement` → …），EMNLP 的 `Excitement: 3: Ambivalent…` 现在能正确解析成评分 3。
- **从正文里挖问题**。有审稿人在 `questions` 字段只写了字面量 **"NA"**，真问题写在正文的 "My concerns are as follows:" 之后；有人写的是 **"see above"** / "I have listed the questions in the weakness part"，问题在 `**Weakness**` 段落里。工具会在 `questions` 是空话或"指向别处"时，去正文里找引导句并提取其后的列表。
- **识别作者回复的三种写法**：`Q1/A1`、纯编号、以及 `**Comment 3**. <问题> **Response**: <回答>`（NeurIPS 2025 / ICML 2026 常见）。最后这种其实最可靠，因为作者直接说明了在回答哪一点。
- **噪音字段整类丢弃**：`code_of_conduct`、`responsible_reviewing_acknowledgement`、`mandatory_acknowledgement`、`compliance_with_LLM_reviewing_policy`、`paper_formatting_concerns` 等纯勾选项不再显示；`NA` / `N/A` / `None` / `-` 这类空话值也一律当作"未填写"。
- 中文编号分隔符（`1、` `2：`）和 `问题1：`/`回答1：` 也已支持。

> **一个明确的边界**：如果审稿人没在 `questions` 字段提问、也没写问题列表，而是把关注点写成**散文段落**，工具**不会**把散文硬拆成"Q1/Q2" —— 那种拆分会制造看似合理实则错位的问答。这种情况下报告会显示 0 个问题，而审稿人的完整意见原样保留在「审稿全文」里。

## 8. 常用参数

```
位置参数                 论文链接 / id / arXiv / DOI / 标题（可多个）
-f, --input-file FILE    从文件读列表（可重复，# 开头为注释）
-o, --out DIR            输出目录，默认 results/（每篇论文一个子文件夹）
--flat                   不用子文件夹，文件直接放在输出目录（以 forum id 命名）
--formats md,html        生成哪些格式，默认两者
--stdout                 直接把 Markdown 打到终端
--no-abstract            报告里不含摘要

--venue TEXT             限定会议，消除同名歧义
--pick N                 选第 N 个候选
--min-score F            标题匹配最低相似度，默认 0.62
-i, --interactive        候选接近时在终端询问
--list-candidates        只列候选就退出

--login / --logout       登录 / 退出（见第 5 节）
--email ADDRESS          登录邮箱（默认沿用上次登录的邮箱）
--token-file PATH        令牌保存位置，默认 .openreview_token
--no-prompt              令牌过期时不询问密码，直接匿名抓取
--token TOKEN            直接指定令牌
--cookie COOKIE          浏览器 Cookie，用于过后人机校验
--cache-dir DIR          原始响应缓存目录，默认 .cache/
--refresh                忽略缓存重新抓取
--offline                只用缓存，不联网
--no-cache               完全禁用缓存
--cache-ttl SECONDS      缓存有效期
--interval SECONDS       请求最小间隔，默认 0.35（调大可显著降低触发校验的概率）
--retries N              单请求重试次数，默认 4
--timeout SECONDS        单请求超时，默认 40
--max-notes N            单篇最多抓取 note 数，默认 20000

-q, --quiet              只输出结果路径
--tree                   在终端打印讨论串树状结构
--on-error warn|raise    批量时某篇失败怎么办，默认 warn（继续）
```

**缓存**：所有原始响应按 URL 哈希存进 `.cache/`，重跑同一篇论文秒开。空响应永不入库 —— 否则一次网络抽风会变成永久的"这篇没有结果"。

## 9. 目录结构

```
crawl.py                     入口
crawl.bat / login.bat        双击入口（ASCII 批处理，中文提示由 Python 输出）
clean.bat                    双击入口：清理历史结果
build_exe.bat                重新编译 exe
papers.example.txt           批量输入示例
orcr/
  api.py                     HTTP 客户端：重试、退避、限速、磁盘缓存、错误解码
  auth.py                    登录 / 令牌存取 / 过期判断 / 到期要密码
  cleanup.py                 清理：白名单删除 + 安全防护
  console.py                 控制台 UTF-8 与行缓冲修正（CLI 与工具共用）
  markdown.py                自带的 Markdown → HTML 渲染器（安全、保护 LaTeX）
  mathrender.py              自带的 LaTeX → MathML 转换器（离线、无 KaTeX/MathJax）
  output.py                  输出目录规划：每篇一个文件夹、文件名清理、paper.json
  resolve.py                 输入解析：URL、id、arXiv、DOI、标题 → forum id
  extract.py                 note 分类、签名解析、线程重建、书目记录识别
  qa.py                      审稿问题 ↔ 作者回复配对
  pipeline.py                端到端：查询 → 线程 → Paper
  render_md.py               Markdown 渲染
  render_html.py             单文件 HTML 渲染
  cli.py                     命令行
  gui.py                     Tkinter 界面共用的辅助函数
tools/
  app_gui.py                 Tkinter 界面（含 --self-test 自检）
  cleanup_gui.py             清理界面（含 --self-test 安全断言）
  launcher.cs                exe 启动器源码（C#，用系统自带 csc.exe 编译）
  build_exe.py               编译 exe
  quick_crawl.py             crawl.bat 用的交互式命令行包装
```

### 关于 exe

仓库里**不存放编译产物**（`.gitignore` 已排除 `*.exe`）。需要双击入口时在 Windows 上自行生成：

- 用 **Windows 自带的 C# 编译器**（`csc.exe`）编译，不是 PyInstaller 打包：
  - 每个只有约 **7.5 KB**，编译耗时不到一秒，不需要下载任何东西
  - 依赖你机器上已有的 Python 3（启动器会自动找 `pythonw.exe`，找不到就弹框提示）
  - 用 `pythonw.exe` 启动，所以不会闪黑框
  - 找不到 Python 时，可以在项目根目录建一个 `python-path.txt`，写一行 `pythonw.exe` 的完整路径
- 想重新编译：双击 `build_exe.bat`，或 `python tools/build_exe.py`。
- 想要完全不依赖 Python 的单文件版本：先 `pip install pyinstaller`，再 `python tools/build_exe.py --standalone`。
- 不编译也完全可用 —— `python tools/app_gui.py` 就是同一个界面。

启动器支持 `--self-test`：会构建窗口但不显示，检查控件是否齐全，然后返回退出码 —— 用来验证 exe 到 Python 的整条链路，不会在屏幕上闪窗。

## 10. 自检

三个入口都内置了 `--self-test`：会构建界面但不显示、检查控件与安全规则，然后返回退出码。不联网、不弹窗，适合确认环境没问题：

```bash
python tools/app_gui.py --self-test        # 主窗口 + 登录框
python tools/cleanup_gui.py --self-test    # 清理窗口 + 删除安全断言
OpenReview爬虫.exe --self-test             # 验证 exe → Python → 界面 整条链路
```

清理逻辑的安全性由代码结构保证，不是靠"小心"：

- 只允许删除**白名单**里的东西 —— `results/`、`.cache/`、`__pycache__/`，以及显式勾选时的令牌文件。其余路径一律拒绝，包括 `crawl.py`、`orcr/`、`tools/`、所有 `.exe`、所有 `.bat`、`README.md`
- 删除前先确认目录里**真的同时存在 `crawl.py` 和 `orcr/`**；不像爬虫根目录就直接中止，避免被从别的目录误启动后清掉不相干的 `results/`
- 自检里有一条专门往清理列表里塞一个"项目外的重要目录"，断言它绝不会被删

## 11. 隐私与安全

这个仓库**不含任何账号信息**：

- `.openreview_token`、`.openreview_cookie` 已被 `.gitignore` 排除，**不要提交它们** —— 令牌文件里既有可用的 bearer token，也有账号邮箱和 OpenReview profile id。
- 密码不会被写入磁盘、不会出现在命令行（因此不会进 shell 历史）、不会写进日志。`--login` 用 `getpass` 隐藏输入。
- 令牌只保存在本地 `.openreview_token`（权限尽力设为 0600）。要清除就用 `--logout` 或界面的「清除令牌」。

**如果你是 fork 或二次发布这个项目：** 提交前先跑一遍

```bash
git grep -nE "openreview_token|OPENREVIEW_PASSWORD|eyJhbGciOi" -- . ':!orcr/auth.py' ':!orcr/cli.py'
```

确认没有把 `.openreview_token` 之类的文件带进版本库。默认的 `.gitignore` 已经覆盖了这些路径。

## 12. 已知限制

- **审稿人身份是匿名的**。报告按 OpenReview 的匿名规则原样呈现（`Reviewer_abc` 这样的组名）。工具**不会也不能**绕过匿名机制。若某个 venue 本身没有匿名，签名是 `~Real_Name1`，报告会显示出来。
- **有些评审尚未公开**。ICLR 投稿即公开，大部分情况能直接抓到；部分会议在评审期结束前不公开，工具会明确提示而不是给你一份空报告。
- **标题检索依赖 OpenReview 自己的索引**。索引里没有的论文（例如一些较早的 NeurIPS 投稿）搜不到，这种情况请直接给 forum 链接。命名相似的论文可能排在前列，用 `--list-candidates` 确认后用 `--pick` 指定。
- **问答配对是启发式的**。匹配不上的会明确标注"未找到对应回复"，请结合下方完整讨论串阅读。
- **令牌每 24 小时要重新输一次密码**。OpenReview 的刷新令牌只发给浏览器 cookie，程序化登录拿不到，所以没法静默续期。密码本身不会被保存 —— 这是有意为之。
- **exe 需要你机器上已装 Python 3**（仓库里也没有预编译的 exe，见第 9 节）。
- **Markdown 渲染器是子集实现**，不是完整 CommonMark。遇到拿不准的边界情况时，报告里的「显示原文」按钮可以立刻切回 OpenReview 存的源码。
- **公式排版用 MathML**，即浏览器自带的数学排版引擎。好处是零依赖、报告不变大；代价是需要一个支持 MathML 的浏览器（Chrome 109+ / Edge / Firefox / Safari 均可）。老浏览器里公式会退化成可读的纯文本，而「显示原文」始终可用。
- **请遵守 OpenReview 的使用条款。** 默认限速已调得比较克制（请求间隔 0.35 秒 + 失败退避），不要进一步压榨接口。

## 13. 许可与致谢

- 代码以 [MIT License](LICENSE) 发布。
- 报告内容版权归论文作者与审稿人所有，仅供研究与学习参考。本工具只读取 OpenReview 上公开可访问的数据。
- 与 OpenReview 官方无关，非官方项目。
