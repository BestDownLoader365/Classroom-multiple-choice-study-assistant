# 课堂选择题学习助手

一个本地运行、由 `questions.json` 题库和 `glossary.json` 专业术语库驱动的 MCQ 学习工具。支持个人账号、公平随机练习、个人错题纠正、同章节迁移验证、基于固定间隔的错题间隔重复复习（SRS）、中英双语辅助、全文术语释义和独立词汇学习页。

## Development

将自己的 `questions.json` 和 `glossary.json` 放在 `run.py` 旁边，然后运行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

macOS / Linux：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

访问 <http://127.0.0.1:5000>，首次使用时创建本地账号。账号只需要用户名和密码，密码以哈希形式保存在本地 SQLite 数据库中。

`run.py` 使用 Flask 开发服务器，只用于开发，不适合长期运行。

程序在启动时分别读取并校验题库和术语库。修改任一 JSON 后都需要重启。启动时检测到 `questions.json` 原始字节变化后，会在一个事务中清空所有账号的答题历史、错题纠正状态、薄弱知识点状态和正常/巩固练习进度，只保留账号信息，视为开始另一门课程。即使仅修改空格、翻译或复用题目 ID，也会触发清空；切回旧题库不会恢复旧记录。无效题库或术语库会阻止应用启动。`glossary.json` 不参与该指纹，单独修改它不会清空学习记录。

数据库保存全局题库指纹，多个工作进程不会重复清空。首次升级时优先从已有练习进度识别旧题库；若有学习记录但没有可用指纹，则清空学习记录以避免混入新课程。题库从未切换时仍支持旧浏览器进度迁入；首次切换后禁用该迁入。替换文件后请统一重启服务，已过期的工作进程会返回 503，防止写入旧课程记录。

## 主要功能

- 每个账号只能查看和巩固自己的错题。
- 正常练习先按课件章节选择范围，可选择一个或多个章节、全选某份课件内的章节，也可使用 All Chapters；再选择 10、20、50 或全部题目。
- 正常练习先在服务器端按筛选范围建立随机 coverage cycle：优先抽取本轮覆盖周期中尚未出现的题，全部覆盖后重新洗牌；题目仍然随机，且单轮绝不重复。
- 每轮同时随机题目和选项，刷新后的题目队列和当前选项顺序保持不变。
- 单选和多选都必须至少选择一项才能提交。
- 多选反馈区分“正确选择”“漏选”和“误选”。
- 错题巩固分为“原错题纠正”和“同知识点强化”：原错题在 Review 中答对一次即完成题目纠正；以 `chapter_ids` 作为知识点，同一知识点必须在 Review 中答对 2 道不同题目才完成强化。
- 已纠正的错题不会永久消失：纠正完成后进入间隔重复（SRS）排期，按 1 / 3 / 7 / 15 / 30 天的间隔在 Review 中再次出现；连续答对逐级拉长间隔（30 天封顶），任何一次答错都会让该题重新回到纠错流程，重新纠正后从 1 天周期重新开始。
- Review 选题优先级为：待纠正原错题 → 已到期的 SRS 复习题 → 同知识点强化题；同一道题在一次巩固中只会以一种角色出现。
- 首页显示“今日待复习 X 题”入口，点击进入现有错题巩固流程；错题页也展示今日到期数量。只统计当前账号已到期的题目。
- 原错题纠正后若仍不足 2 道不同题，Review 会从同章节选择另一题进行迁移验证。迁移题不会自动成为错题，只有实际答错时才会加入错题并重置相关知识点进度。
- 正常练习和错题巩固的进度按账号分别保存在服务器；Normal coverage bag 与 Review 队列/知识点进度完全独立。不同设备登录同一账号，打开或刷新练习页即可接续相同的题目、角色、答题反馈和巩固进度。
- 题目默认显示英文；有中文辅助字段时，可用页面顶部按钮切换“仅英文 / 中英双语”。
- Review 继续复用正常练习的题卡、选项反馈和解析结构，只以现有次级 metadata/feedback 样式提示“错题纠正”或“同知识点强化”。错题页在原有布局中增加薄弱知识点摘要；手机端仍使用现有卡片式布局，答题按钮保持在容易操作的位置。
- 练习页显示题目所属课件、章节、section 和页码。
- 错题页始终显示最近一次错误答案和课程来源；正确答案与解析仅在完成巩固后显示。课件或章节下拉项选中后立即筛选，无需“筛选错题”按钮；两个条件可联合使用，并可按当前范围巩固。
- 可在错题页确认后将当前账号的全部错题重置为 0；答题历史会保留，其他账号不受影响。
- 题目、选项、答案反馈、解析和错题中的英文专业术语可点击或用键盘打开中文释义；独立的 `/glossary` 页面支持搜索、动态分类筛选和中英文主动回忆。
- 学习数据页（`/dashboard`）按当前账号汇总累计答题、总正确率、最近 7/30 天答题量、待纠正/已纠正错题、逐章节掌握度（含覆盖率与状态分级）和最近 7 天每日答题趋势；没有答题记录时显示空状态。页面时间与趋势日期按显示时区呈现（默认跟随服务器本地时区，可用环境变量 `MCQ_DISPLAY_TIMEZONE` 指定如 `Asia/Shanghai` 的 IANA 时区）。
- 模拟考试（`/exam`）从题库随机抽取固定不重复的一套题，可选不限时或 10/20/30/60 分钟限时；考试过程不提示对错，可上一题/下一题，刷新或关闭页面后可继续；服务端记录开考时间与时限，到时自动交卷，打开首页、考试中心或学习数据页时也会自动结算所有已到期考试。
- 交卷（或到时自动交卷）后生成成绩报告：总分、正确率、用时、章节表现拆分，以及每道错题的你的答案、正确答案与解析；已作答的错题自动进入现有错题本与薄弱知识点流程，不重复创建错题记录。

## 两套独立选题策略

正常练习和 Review 不共享候选状态：

```text
Normal:
source/chapter filter
→ eligible question IDs
→ random coverage bag
→ round queue

Review:
uncorrected wrong questions
+ due SRS reviews (corrected questions whose scheduled time has arrived)
+ active weak chapters
→ original correction / srs review / transfer verification
→ review queue
```

Normal 只读取实时题库筛选结果和自己在 `quiz_progress` 中的 coverage 状态，不读取错题、薄弱知识点或 Review 进度。Review 选择迁移题时也不会消费或修改 Normal coverage bag。选择“全部题目”时，每个 eligible question 恰好出现一次。

Normal coverage、当前 Normal/Review 队列、题目角色、答案 token、反馈和选项随机种子都保存在服务器端 SQLite progress 中，不放入 `localStorage`。浏览器刷新、反馈重定向和另一设备继续不会重新抽题；只有真正开始新的 Normal round 才消费 coverage bag，只有推进 Review queue 才生成后续迁移题。

模拟考试使用完全独立的状态：创建时用 `random.sample` 一次性抽出固定题集，与 Normal coverage bag 和 Review 队列互不影响，也不会改写它们。

## JSON 格式

两个数据文件都有与当前 Loader 同步的独立指南：

- [`questions.json` 题库编写指南](docs/QUESTION_JSON_GUIDE.md)：题目、目录、多章节归属、内容质量、校验与迁移；
- [`glossary.json` 专业术语库编写指南](docs/GLOSSARY_JSON_GUIDE.md)：术语、别名、翻译、定义、分类、匹配规则、覆盖审计与验收。

```json
{
  "schema_version": 2,
  "title": "Physical Design Practice",
  "title_zh": "物理设计选择题练习",
  "sources": [
    {
      "id": "physical-design-floorplanning",
      "title": "Physical Design 2 — Floorplanning",
      "filename": "Physical Design2_v2026.pdf",
      "lecture": "Lecture 3 · Floorplanning"
    }
  ],
  "chapters": [
    {
      "id": "floorplanning-overview",
      "source_id": "physical-design-floorplanning",
      "title": "Floorplanning Overview",
      "order": 1
    },
    {
      "id": "design-planning-partitioning",
      "source_id": "physical-design-floorplanning",
      "title": "Design Planning & Partitioning",
      "order": 2
    }
  ],
  "questions": [
    {
      "id": "q001",
      "source_id": "physical-design-floorplanning",
      "chapter_ids": ["floorplanning-overview", "design-planning-partitioning"],
      "section": "Floorplanning inputs and objectives",
      "pages": [2],
      "text": "Which are programming languages?",
      "text_zh": "哪些是编程语言？",
      "type": "multiple",
      "options": [
        {
          "id": "python",
          "text": "Python",
          "text_zh": "Python"
        },
        {
          "id": "html",
          "text": "HTML",
          "text_zh": "HTML"
        },
        {
          "id": "java",
          "text": "Java",
          "text_zh": "Java"
        }
      ],
      "correct_answers": ["python", "java"],
      "explanation": "Python and Java are programming languages.",
      "explanation_zh": "Python 和 Java 是编程语言。"
    }
  ]
}
```

中文字段 `title_zh`、`text_zh` 和 `explanation_zh` 用于双语辅助显示。题目仍以英文原文为准；这些中文字段可以省略，省略后页面不会显示相应翻译。

## 将专业词汇系统用于其他课程

专业词汇是独立、通用的数据子系统。`questions.json` 定义要练习的题目，根目录的 `glossary.json` 定义当前课程需要识别和学习的词汇；浏览器不会直接请求该文件，应用会在启动时完成校验并通过模板安全下发。完整字段和匹配规则见 [`docs/GLOSSARY_JSON_GUIDE.md`](docs/GLOSSARY_JSON_GUIDE.md)。

更换课程只需要：

1. 提供符合题库 schema 的新 `questions.json`。
2. 提供符合 schema version 1 的新 `glossary.json`。
3. 重启应用。

无需修改 Python、Jinja template、JavaScript、CSS 或数据库 schema。`glossary.json` 不参与 question-bank fingerprint；单独修改术语、翻译或定义不会清空答题记录、错题、纠正/强化状态或练习进度。

最小的 Statistics 示例：

```json
{
  "schema_version": 1,
  "title": "Statistics Glossary",
  "title_zh": "统计学专业词汇",
  "description": "Technical vocabulary used in this course.",
  "description_zh": "本课程涉及的核心专业术语。",
  "terms": [
    {
      "id": "standard-deviation",
      "term": "Standard Deviation",
      "term_zh": "标准差",
      "aliases": ["SD"],
      "definition": "A measure of dispersion around the mean.",
      "definition_zh": "衡量数据相对于均值离散程度的统计量。",
      "category": "Descriptive Statistics"
    }
  ]
}
```

Root 必填字段为 `schema_version`、`title`、`title_zh`、`terms`；`description` 和 `description_zh` 可选。每个 term 必须包含 `id`、`term`、`term_zh`，可选字段为 `aliases`、`definition`、`definition_zh`、`category`。分类直接按 `terms[].category` 首次出现顺序生成，不需要维护第二份 categories 数组。详细的字段表、alias 设计、Unicode 边界与冲突规则见专业术语库指南。

Loader 会拒绝重复 ID、标准化后重复的 canonical term、空 alias、term/alias 冲突和同一 alias 指向多个词条。发布前可运行通用审计：

```bash
python scripts/audit_glossary.py
python scripts/audit_glossary.py --questions path/to/questions.json --glossary path/to/glossary.json
```

审计会验证 schema、报告未在学习语料中出现的 orphan entries，并给出大写缩写、括号缩写和连字符 token 等“可能遗漏候选”；候选只供人工复核，不会自动写入 glossary 或生成翻译。

`sources` 和 `chapters` 是题库内唯一的课程目录；题目通过 `chapter_ids` 可以同时属于同一份课程资料下的一个或多个章节，筛选任一所属章节都能找到该题。当前随附题库包含 225 道题，按 5 份原始课件划分为 38 个可练习主题；术语库包含 220 个规范词条、150 个 aliases 和 13 个动态分类。`section` 和 `pages` 提供更精确的回溯位置。旧题库可以继续加载：旧的单值 `chapter_id` 会自动转换成单元素章节集合；未提供课程目录时，题目自动归入 `Uncategorized`。

基础校验规则：

- `type` 只能是 `single` 或 `multiple`。
- 每题至少两个选项。
- `single` 必须恰好有一个正确答案。
- 多选题只有所选 ID 集合与正确答案集合完全一致时才算正确。
- 题目 ID 在整份文件中唯一；选项 ID 在同一道题内唯一。
- 判题使用 `option.id`，不受随机显示顺序影响。
- 提供 `sources` / `chapters` 目录时，每题必须包含有效的 `source_id` 和非空、无重复的 `chapter_ids`；所有章节必须属于该 `source_id`。旧的单值 `chapter_id` 仍兼容，但不能与 `chapter_ids` 同时提供。
- `pages` 如存在，必须为不重复的正整数数组。

## 题库元数据迁移

仓库没有发现原始题目生成器，因此新增了可重复执行的迁移脚本，将现有 explanation 中的 `Source: ..., p...` 引用转为结构化字段，并保留全部题号和内容：

```powershell
python scripts\migrate_question_metadata.py
```

也可以指定输入和输出文件：

```powershell
python scripts\migrate_question_metadata.py generated.json --output questions.json
```

新生成题库最好直接写出 `source_id`、`chapter_ids`、`section` 和 `pages`；脚本会把旧的单值 `chapter_id` 规范化为数组，并保留已经有效的结构化元数据。

## 数据说明

`instance/mcq.db` 保存：

- 本地账号的用户名、密码哈希和创建时间；
- 每个账号每道题最近 10 次作答的模式、所选答案、结果和时间；
- 每个账号自己的错题次数和题目纠正状态（旧数据库列 `review_streak` / `mastered` 保留作无损兼容，当前语义为未纠正/已纠正，不再表示知识点掌握）；
- 每个账号以 chapter 为单位的薄弱状态、Review 中已验证的不同 question IDs 和强化进度；
- 每个账号正常练习、错题巩固的题目队列、Review item role、当前位置、选项顺序、反馈和本轮统计，以及 Normal coverage bag；
- 每个账号的模拟考试场次（题量、时限、状态、成绩、用时）和每场考试的固定题目集合与保存的作答。

### 换设备继续做题

1. 在另一台设备打开同一个学习网站，登录同一账号。
2. 在首页选择“继续正常练习”或“继续错题巩固”。
3. 如果页面已经打开，刷新后查看另一台设备保存的最新进度。页面不会自动实时刷新。

在任一设备重新开始某种练习，其他设备也会使用该模式的新进度；重置全部错题会同时清除该账号的错题纠正状态和薄弱知识点状态，并结束各设备上的错题巩固。正常练习（包括 coverage 状态）和答题历史会保留。如果提示答题页面已过期，重新进入练习即可继续。

升级后需重启程序。`weak_knowledge_points` 会通过 `CREATE TABLE IF NOT EXISTS` 自动建立，已有账号、attempts 和 wrong questions 会保留；已有错题按实时 `chapter_ids` 初始化为 0/2 的薄弱知识点。旧 Normal progress 没有 fairness 字段时会在下一次新建 round 时自动初始化。缺少新 role metadata 的旧未完成 Review progress 无法安全转换，会仅清除该 Review round，不删除错题、薄弱状态、attempt history、Normal progress 或账号。

## 测试

```bash
python scripts/audit_glossary.py
pytest
```

## Production（Windows 11 + WSL2 Ubuntu）

生产环境运行在 WSL2 Ubuntu 中：

```text
公网 HTTPS 入口（TLS 终止）
      ↓
Sakura FRP / 安全隧道
      ↓
Nginx 127.0.0.1:8080（仅本机回源）
      ↓
Gunicorn 127.0.0.1:8001
      ↓
Flask
```

公网入口必须使用 HTTPS，并把原始协议可靠地传递为 `X-Forwarded-Proto: https`。应用生产入口强制使用 `Secure` Session Cookie；直接把明文 HTTP 的 8080 端口暴露到公网会泄露登录凭据，且浏览器不会在后续 HTTP 请求中发送登录 Cookie。TLS 可以终止在受信任的公网反向代理、CDN 或隧道服务，但代理到本机的 8080 只能作为受保护的回源链路，不能作为公网入口。

### 首次部署

在 WSL2 Ubuntu 中执行：

```bash
cd /home/fangsihan/CodeSpace/Python/MCQ_Template

sudo apt update
sudo apt install python3 python3-venv python3-pip nginx openssl

/usr/bin/python3 -m venv .venv-prod
.venv-prod/bin/python -m pip install -r requirements.txt
```

创建生产密钥。已有密钥文件时不要覆盖：

```bash
sudo install -d -m 0700 /etc/mcq-template

if ! sudo test -s /etc/mcq-template/mcq-template.env; then
    sudo sh -c 'umask 077; printf "MCQ_SECRET_KEY=" > /etc/mcq-template/mcq-template.env; openssl rand -hex 32 >> /etc/mcq-template/mcq-template.env'
fi
```

页面时间与 Dashboard 趋势日期默认跟随服务器本地时区；如果 WSL 系统时区不是本地时区，可在同一个 env 文件中追加 `MCQ_DISPLAY_TIMEZONE=Asia/Shanghai` 显式指定。

安装 systemd 和 Nginx 配置：

```bash
sudo install -m 0644 deploy/mcq-template.service \
    /etc/systemd/system/mcq-template.service
sudo systemctl daemon-reload

sudo install -m 0644 deploy/nginx-mcq-template.conf \
    /etc/nginx/sites-available/mcq-template
sudo ln -sfn /etc/nginx/sites-available/mcq-template \
    /etc/nginx/sites-enabled/mcq-template

if [ -L /etc/nginx/sites-enabled/default ]; then
    sudo unlink /etc/nginx/sites-enabled/default
fi

sudo nginx -t
sudo systemctl enable mcq-template.service nginx.service
sudo systemctl restart mcq-template.service
sudo systemctl restart nginx.service
```

应用启动时会自动将 `instance` 和 SQLite 数据库权限分别收紧为 `0700` 和 `0600`。也可手工复核：

```bash
chmod 0700 instance
chmod 0600 instance/mcq.db
```

### 配置更新

修改 `deploy/` 中的配置后，重新安装并加载：

```bash
sudo install -m 0644 deploy/mcq-template.service \
    /etc/systemd/system/mcq-template.service
sudo systemctl daemon-reload
sudo systemctl restart mcq-template.service

sudo install -m 0644 deploy/nginx-mcq-template.conf \
    /etc/nginx/sites-available/mcq-template
sudo nginx -t && sudo systemctl reload nginx.service
```

### 日常启停

启动全部服务：

```bash
./scripts/start_production.sh
```

停止全部服务：

```bash
./scripts/stop_production.sh
```

脚本会提示输入 Ubuntu sudo 密码。它们只负责启停已经安装的服务，不会复制 `deploy/` 中的配置。

### 状态和日志

```bash
systemctl status mcq-template --no-pager
systemctl status nginx --no-pager

journalctl -u mcq-template -f
sudo tail -f /var/log/nginx/mcq-template.access.log
sudo tail -f /var/log/nginx/mcq-template.error.log
```

### 分层验证

在 WSL 中：

```bash
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8080/health
```

在 Windows PowerShell 中：

```powershell
curl.exe -i http://localhost:8080/health
```

### Sakura FRP 本地目标

```text
Tunnel type: 支持公网 HTTPS 的受信任隧道/反向代理
Local IP: 127.0.0.1
Local Port: 8080
Local HTTP URL: http://127.0.0.1:8080
```

隧道回源应连接 Nginx 的 8080，不要连接 Gunicorn 的 8001；公网侧必须启用有效 TLS 证书并强制 HTTP 跳转 HTTPS。

### 故障排查

- `502 Bad Gateway`：先执行 `systemctl status mcq-template`，再用 `curl http://127.0.0.1:8001/health` 检查 Gunicorn，最后查看 `journalctl -u mcq-template` 和 Nginx error log。
- `Connection refused`：依次检查 Sakura FRP 的本地目标、Windows 的 `curl.exe http://localhost:8080/health`、`systemctl status nginx`、`systemctl status mcq-template`。
- `500 Internal Server Error`：查看 `journalctl -u mcq-template -n 100 --no-pager`；Flask 异常由 Gunicorn 写入该 journal。随后查看 `/var/log/nginx/mcq-template.error.log` 以关联代理请求。
