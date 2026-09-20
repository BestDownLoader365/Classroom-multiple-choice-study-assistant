# 课堂选择题学习助手

一个本地运行的**多课程** MCQ 学习工具：每门课程由自己的 `courses/<course_id>/questions.json` 题库与可选 `glossary.json` 术语库驱动，课程之间完全隔离（题目/章节/课件 ID 都是课程内本地 ID）。支持个人账号、公平随机练习、个人错题纠正、同章节迁移验证、基于固定间隔的错题间隔重复复习（SRS）、中英双语辅助、全文术语释义和独立词汇学习页。

## Development

推荐的多课程布局是 `courses/<course_id>/course.json` + `questions.json`（可选 `glossary.json`）；日常维护时把候选内容写成同目录的 `questions_candidate.json` / `glossary_candidate.json`，`check_*.py` 与 `publish_course.py` 在不传路径时默认读取它们（发布流程与目录约定见 [`docs/COURSE_GUIDE.md`](docs/COURSE_GUIDE.md) 第 7 节）。课程内容不进入版本控制（`.gitignore` 排除了 `courses/`、`instance/` 与根目录的 `*.json`），所以全新 checkout 需要在 `courses/<course_id>/` 自备内容；旧版的根目录 `questions.json`/`glossary.json` 仍然支持：没有 manifest 时会作为 `legacy` 课程加载。然后运行：

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

程序在启动时加载并校验**每一门启用课程**的题库与术语库；发布内容后需要重启 worker 才能激活（文件系统发布与数据库激活不是同一个事务）。`questions.json` 的维护按 `question.id` 逐题增量生效，不会再清空全站学习数据：修改题干、翻译、解析、选项文案或顺序、`section`/`pages`、JSON 格式等普通维护会完整保留所有账号的答题历史、错题纠正状态、SRS 排期、薄弱知识点状态和练习/考试进度；修改某题的题型、正确答案集合，或删除/重命名已有选项 ID 时，只清理这一道题受影响的记录；删除题目会保留其历史作答，但静默移除它的错题/SRS 状态和相关复习引用。注意“保留历史作答”不等于“统计数字不变”：页面统计只统计仍在题库中的题目，因此删除题目后累计答题数可能下降、正确率可能变化，题目恢复后这些历史作答又会重新计入。单门课程的无效题库不会阻止应用启动：该课程变为 `unavailable`，历史状态原样保留，其他课程继续服务；只有重复 `course_id` 这类全局歧义才会让应用装配失败。`glossary.json` 不参与题库同步，单独修改它不会影响学习记录。

数据库为**每门课程的每个题目 ID** 永久保存注册信息：被删除题目的 ID 在该课程内永久退役，不能再分配给不同的新题（误判复用会让该课程变为 `unavailable`，可先用 `python scripts/check_question_bank.py --course <course_id>` 预检）；误删的题目按原 ID 原判题规则加回即可自动恢复。`chapter_ids` 或 `source_id` 的修改会改变题目归属（影响章节筛选、Review 与章节进度），该课程课件/章节的**增删、顺序或归属变化**会改变 worker 的章节菜单与筛选校验，因此它们与增删题目、判题规则变化一样属于**结构性变化**：**该课程**的 generation 会 +1，运行旧内容的 worker 在该课程的学习页面（含 `/stats`）返回 503，直到 worker 重启；**其他课程完全不受影响**（generation、数据与请求行为都不变）；纯文案修改（题库标题、课件/章节标题、lecture/filename、JSON 格式）不会打断运行中的工作进程，只会让不同 worker 的标签文案在重启前短暂不同。发布题库文件必须使用原子替换（推荐 `python scripts/publish_course.py --course <course_id> --questions questions_candidate.json`），不要直接 `cp` 覆盖正在使用的文件，也不要用编辑器原地保存：写入中断时 worker 可能读到半截 JSON，报出误导性的 `Invalid JSON in question bank at line 1, column N`。预检脚本会分别报告 `catalogue-changed`（需要统一重启）与 `presentation-only`（仅文案，无需为此重启）。这两个标记是从“文件字节是否变化 + 各类指纹是否变化”反推的分类，因此 `presentation-only: no` 并不代表文案没变（例如同一次发布里还改了题干或章节目录），判断是否需要统一重启请以 `catalogue-changed` 与题目级各行为准。

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
- 首页“待处理”区在复习到期时显示“待复习 X 题”入口（巩固进行中时并入“继续错题巩固”行），点击进入现有错题巩固流程；没有待处理事项时该区域整体隐藏，不再显示占位行。错题页也展示今日到期数量。只统计当前账号已到期的题目。
- 原错题纠正后若仍不足 2 道不同题，Review 会从同章节选择另一题进行迁移验证。迁移题不会自动成为错题，只有实际答错时才会加入错题并重置相关知识点进度。
- 正常练习和错题巩固的进度按账号分别保存在服务器；Normal coverage bag 与 Review 队列/知识点进度完全独立。不同设备登录同一账号，打开或刷新练习页即可接续相同的题目、角色、答题反馈和巩固进度。
- 题目默认显示英文；有中文辅助字段时，可用页面顶部按钮切换“仅英文 / 中英双语”。
- Review 继续复用正常练习的题卡、选项反馈和解析结构，只以现有次级 metadata/feedback 样式提示“错题纠正”或“同知识点强化”。错题页在原有布局中增加薄弱知识点摘要；手机端仍使用现有卡片式布局，答题按钮保持在容易操作的位置。
- 练习页显示题目所属课件、章节、section 和页码。
- 错题页始终显示最近一次错误答案和课程来源；正确答案与解析仅在完成巩固后显示。课件或章节下拉项选中后立即筛选，无需“筛选错题”按钮；两个条件可联合使用，并可按当前范围巩固。
- 可在错题页确认后将当前账号的全部错题重置为 0；答题历史会保留，其他账号不受影响。
- 题目、选项、答案反馈、解析和错题中的英文专业术语可点击或用键盘打开中文释义；独立的 `/glossary` 页面支持搜索、动态分类筛选和中英文主动回忆。
- 学习数据页（`/dashboard`）按当前账号汇总累计答题、总正确率、最近 7/30 天答题量、待纠正/已纠正错题、逐章节掌握度（含覆盖率与状态分级）和最近 7 天每日答题趋势；没有答题记录时显示空状态。页面时间与趋势日期按显示时区呈现（默认跟随服务器本地时区，可用环境变量 `MCQ_DISPLAY_TIMEZONE` 指定如 `Asia/Shanghai` 的 IANA 时区）。
- 全员统计页（`/stats`）汇总所有账号的学习数据，仅供参考：注册账号数、有作答的账号数、全员累计答题、全员总正确率、最近 7/30 天全员答题量、最近 7 天全员每日答题趋势，以及按全员正确率从低到高排序的章节难度榜（含作答人数与“较难 / 中等 / 较易”分级）。页面只展示群体整体情况，不展示任何单个账号的明细，也不含待纠正错题、SRS 到期等个人化指标；与个人学习数据页一样，统计基于每道题每个账号保留的最近 10 次作答。首页“学习资源”区提供入口，所有登录账号均可访问。
- 模拟考试（`/exam`）从题库随机抽取固定不重复的一套题，限时通过下拉选择：10–90 分钟（每 10 分钟一档）或不限时（默认）；考试过程不提示对错，可上一题/下一题，刷新或关闭页面后可继续；服务端记录开考时间与时限，到时自动交卷，打开首页、考试中心或学习数据页时也会自动结算所有已到期考试。
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

题库与术语库各有与当前 Loader 同步的独立指南，多课程运营请看课程手册：

- [`questions.json` 题库编写指南](docs/QUESTION_GUIDE.md)：题目、目录、多章节归属、内容质量、校验与迁移；
- [`glossary.json` 专业术语库编写指南](docs/GLOSSARY_GUIDE.md)：术语、别名、翻译、定义、分类、匹配规则、覆盖校验与验收；
- [课程模型与运营手册](docs/COURSE_GUIDE.md)：课程 manifest、URL、切课、就绪检查、按课程发布与故障隔离；
- [多课程迁移与回滚手册](docs/MULTI_COURSE_MIGRATION.md)：命名空间迁移、字段级验证、故障注入与回滚；
- [技术架构说明](docs/ARCHITECTURE.md)：模块职责、请求流程、SQLite schema、测试架构与部署；
- [前端视觉与交互设计规范](docs/FRONTEND_DESIGN_SYSTEM.md)：设计令牌、组件配方、无障碍规则与多课程选择器约定。

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

题干、选项和解析文本支持用 `\n` 换行；罗马数字分点题干（`I.`、`II.`、`III.` 开头的行）中的陈述行会以较小字号显示，详见 [`docs/QUESTION_GUIDE.md`](docs/QUESTION_GUIDE.md) 第 6.4 节。

## 将专业词汇系统用于其他课程

专业词汇是独立、通用的数据子系统，并且**按课程隔离**。`questions.json` 定义该课程要练习的题目，该课程的 `glossary.json` 定义它需要识别和学习的词汇；浏览器不会直接请求该文件，应用会在启动时完成校验并通过模板安全下发**当前课程**的 payload（没有术语表的课程只下发空 payload，绝不会混入其他课程的术语）。完整字段和匹配规则见 [`docs/GLOSSARY_GUIDE.md`](docs/GLOSSARY_GUIDE.md)。

更换课程只需要：

1. **已有课程改内容**：把课程内容放进 `courses/<course_id>/`：题库写成 `questions_candidate.json`（术语库写成 `glossary_candidate.json`），先运行 `python scripts/check_question_bank.py --course <course_id> --db instance/mcq.db` 预检（加 `--strict` 可让“会清理学习状态”的更新直接返回非 0），再用 `python scripts/publish_course.py --course <course_id> --questions questions_candidate.json --db instance/mcq.db` 原子发布（该命令默认会重跑同一份预检；省略 `--questions` 时发布的就是默认候选文件）。
2. **新增一门课必须先创建**：`--course` 只能解析已经有 `courses/<course_id>/course.json` 的课程；只有候选文件、还没有 manifest 的目录会被 loader 忽略，此时传 `--course <course_id>` 只会得到 `Unknown course`。所以先把候选文件放进 `courses/<course_id>/`，运行 `python scripts/publish_course.py --course <course_id> --add --title "..."`（它自己会校验题库 schema 与术语表，失败不写任何文件；通过后先归档到 `courses/<course_id>/versions/<sha256>/` 再写 manifest，所以课程目录根部不会留下无人引用的发布副本）创建课程，之后 `python scripts/check_question_bank.py --course <course_id> --db instance/mcq.db` 与 `python scripts/check_glossary.py --course <course_id>` 才能解析这门课。
3. 如需术语表，提供符合 schema version 1 的 `glossary.json`（或在 manifest 中写 `"glossary": null` 明确表示没有）。
4. 停用/启用用 `python scripts/publish_course.py --course <course_id> --disable` / `--enable`；删除课程用 `python scripts/delete_course.py --course <course_id> --dry-run` 预览后再删除（**不要**手工 `rm -rf courses/<course_id>`）。
5. 统一重启应用 worker，并用 `/ready/<course_id>` 确认该课程已激活。

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

Loader 会拒绝重复 ID、标准化后重复的 canonical term、空 alias、term/alias 冲突和同一 alias 指向多个词条。发布前运行统一命名的术语表校验脚本 `check_glossary.py`：

```bash
python scripts/check_glossary.py --course physical_design   # 校验某门课的术语表
python scripts/check_glossary.py --all                       # 校验全部启用课程
python scripts/check_glossary.py --questions path/to/questions.json --glossary path/to/glossary.json  # 显式离线
```

校验只读取内容，不会修改 `question_registry`、generation 或任何学习数据。它会验证 schema、报告未在学习语料中出现的 orphan entries，并给出大写缩写、括号缩写和连字符 token 等“可能遗漏候选”；候选只供人工复核，不会自动写入 glossary 或生成翻译。

`sources` 和 `chapters` 是**该课程**题库内唯一的课程目录；题目通过 `chapter_ids` 可以同时属于同一份课程资料下的一个或多个章节，筛选任一所属章节都能找到该题。

课程内容不在版本控制内（`courses/` 已被 `.gitignore` 排除），部署时把内容放进 `courses/<course_id>/`。本地示例课程：`eek5101`（EEK5101 集成电路技术与设计方法：50 题，按 3 份课件划分为 12 个章节；术语库 83 个词条、19 个 aliases、22 个分类，manifest 指向 `versions/<sha256>/` 中的不可变发布副本）和 `eek5106`（EEK5106 半导体良率与失效分析：225 题，按 5 份原始课件（`eek5106-week1`…`week5`）划分为 38 个可练习主题；术语库 220 个词条、150 个 aliases、13 个分类）。`section` 和 `pages` 提供更精确的回溯位置。旧题库可以继续加载：旧的单值 `chapter_id` 会自动转换成单元素章节集合；未提供课程目录时，题目自动归入 `Uncategorized`。

基础校验规则：

- `type` 只能是 `single` 或 `multiple`。
- 每题至少两个选项。
- `single` 必须恰好有一个正确答案。
- 多选题只有所选 ID 集合与正确答案集合完全一致时才算正确。
- 题目 ID 在整份文件中唯一；选项 ID 在同一道题内唯一。
- 判题使用 `option.id`，不受随机显示顺序影响。
- 提供 `sources` / `chapters` 目录时，每题必须包含有效的 `source_id` 和非空、无重复的 `chapter_ids`；所有章节必须属于该 `source_id`。旧的单值 `chapter_id` 仍兼容，但不能与 `chapter_ids` 同时提供。
- `pages` 如存在，必须为不重复的正整数数组。

## 内容变更统一流程

脚本命名统一为 `check_<校验对象>.py`，每一类课程内容都有一个只读校验脚本。修改任何课程内容都必须遵循「准备校验脚本 → 修改内容 → 运行校验 → 重新执行 publish course」，**校验未通过时不得发布**：

| 变更对象 | 校验脚本（只读，先运行） | 发布命令（校验通过后） |
| --- | --- | --- |
| 课程目录 / manifest / 整门课能否加载 | `python scripts/check_courses.py` | `python scripts/publish_course.py --add` / `--enable` / `--disable` |
| `questions.json` | `python scripts/check_question_bank.py --course <course_id> --db instance/mcq.db` | `python scripts/publish_course.py --course <course_id> --questions questions_candidate.json` |
| `glossary.json` | `python scripts/check_glossary.py --course <course_id>` | `python scripts/publish_course.py --course <course_id> --glossary glossary_candidate.json` |
| 删除整门课程 | `python scripts/delete_course.py --course <course_id> --dry-run` | `python scripts/delete_course.py --course <course_id>`（有学习数据时先确认再加 `--force`） |

> 新增一门课时上表的顺序要调整：`check_courses.py` 只报告已声明（已有 `course.json`）的课程，而且 `check_question_bank.py --course <course_id>` / `check_glossary.py --course <course_id>` 在课程创建前都会报 `Unknown course`。新课程先运行 `python scripts/publish_course.py --course <course_id> --add ...`（它自带题库 schema 与术语表门禁），再用 `--course` 补跑上表的只读校验；完整步骤见[课程模型与运营手册](docs/COURSE_GUIDE.md)第 7.2 节。

1. **准备校验脚本**：确认该内容种类已有 `check_<校验对象>.py`；没有就先补齐脚本和测试。
2. **修改内容**：只编辑候选文件（`courses/<course_id>/questions_candidate.json` / `glossary_candidate.json`），不要原地覆盖正在使用的 `questions.json` / `glossary.json`。
3. **运行校验**：`check_courses.py` 必须始终通过；题库再用 `check_question_bank.py`，术语表再用 `check_glossary.py`。不传文件参数时它们默认读取该课程的候选文件（用 `--published` 可以强制只校验已发布内容）。
4. **重新执行 publish course**：只有校验退出码为 `0` 才发布（`publish_course.py` 会在写入前内部重跑对应的 `check_*.py` 并拒绝未通过的内容；只有明确加 `--skip-preflight` 才跳过，且不推荐）。发布是纯文件系统切换，最后统一重启全部 worker，并用 `/ready/<course_id>` 确认。
5. **删除课程不要手工 `rm`**：课程目录之外还有 `courses` 身份、学习数据、`question_bank_state`、`question_registry` 退役记录和 `default_course_id` 偏好。`python scripts/delete_course.py --course <course_id> --dry-run` 会先列出全部将删除/清理的内容，确认后再去掉 `--dry-run` 执行（默认自动备份数据库；仍有学习数据时需要 `--force`），它同样支持只清理“数据库里有身份、内容已不在”的课程。

校验失败时，发布命令不会替换或创建任何文件，也不会推进 generation；课程创建 / 修改 / 删除的完整教程见[课程模型与运营手册](docs/COURSE_GUIDE.md)第 7 节。

## 数据说明

`instance/mcq.db` 保存：

- 本地账号的用户名、密码哈希和创建时间；
- 每个账号每道题最近 10 次作答的模式、所选答案、结果和时间；
- 每个账号自己的错题次数和题目纠正状态（旧数据库列 `review_streak` / `mastered` 保留作无损兼容，当前语义为未纠正/已纠正，不再表示知识点掌握）；
- 每个账号以 chapter 为单位的薄弱状态、Review 中已验证的不同 question IDs 和强化进度；
- 每个账号正常练习、错题巩固的题目队列、Review item role、当前位置、选项顺序、反馈和本轮统计，以及 Normal coverage bag；
- 每个账号的模拟考试场次（题量、时限、状态、成绩、用时）和每场考试的固定题目集合与保存的作答；
- 永久课程身份与已接受元数据（`courses`）以及 schema/legacy 归属记录（`schema_meta`）；
- 题库注册信息与题库状态：**每门课程的**每个题目 ID 的判题身份、内容指纹、归属指纹和退役状态（`question_registry`，PK 为 `(course_id, question_id)`），以及该课程当前的 `bank_version`、结构性 generation 和目录指纹（`question_bank_state`，PK 为 `course_id`）；这些表只保存指纹与状态，不保存题目正文；
- 登录/注册失败的限流窗口（`auth_rate_limits`），用于阻止暴力尝试，过期记录会被清理。

### 换设备继续做题

1. 在另一台设备打开同一个学习网站，登录同一账号。
2. 在首页选择“继续正常练习”或“继续错题巩固”。
3. 如果页面已经打开，刷新后查看另一台设备保存的最新进度。页面不会自动实时刷新。

在任一设备重新开始某种练习，其他设备也会使用该模式的新进度；重置全部错题会同时清除该账号的错题纠正状态和薄弱知识点状态，并结束各设备上的错题巩固。正常练习（包括 coverage 状态）和答题历史会保留。如果提示答题页面已过期，重新进入练习即可继续。

升级后需重启程序。`weak_knowledge_points` 会通过 `CREATE TABLE IF NOT EXISTS` 自动建立，已有账号、attempts 和 wrong questions 会保留；已有错题按实时 `chapter_ids` 初始化为 0/2 的薄弱知识点。旧 Normal progress 没有 fairness 字段时会在下一次新建 round 时自动初始化。缺少新 role metadata 的旧未完成 Review progress 无法安全转换，会仅清除该 Review round，不删除错题、薄弱状态、attempt history、Normal progress 或账号。

## 测试

```bash
python scripts/check_glossary.py --all
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
curl -i http://127.0.0.1:8001/health   # 进程存活
curl -i http://127.0.0.1:8001/ready    # 可以承接学习流量
curl -i http://127.0.0.1:8080/health
curl -i http://127.0.0.1:8080/ready
```

`/health` 只表示进程活着（旧内容的 worker 也会返回 200，因为它仍要能登录/登出）；`/ready` 汇总本 worker **声明且启用**的课程，只要有一门不是 `ready` 就返回 503 与每门课程的 `status`/`worker_generation`/`database_generation`/`reason`；`/ready/<course_id>` 报告单门课程。**汇总失败只表示"至少一门课程不可服务"，不会让健康课程的路由失败。** 监控与启动脚本应使用这两个端点。

在 Windows PowerShell 中：

```powershell
curl.exe -i http://localhost:8080/health
curl.exe -i http://localhost:8080/ready
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
- `503 题库正在更新`：当前 worker 仍在使用该课程的旧内容（日志中会出现 `Course "<id>" is stale on this worker (worker=… db=…)`），或数据库被恢复到较旧备份。先 `curl http://127.0.0.1:8001/ready` 找到受影响课程，再统一重启 `mcq-template.service`，最后用 `curl http://127.0.0.1:8001/ready/<course_id>` 确认返回 200；不要手工修改 `question_bank_state.generation` 绕过检查。其余课程在此期间仍可正常学习。
- `503 该课程暂时不可用`：该课程内容加载失败（日志中会出现 `Course "<id>" is unavailable: …`），通常是因为 manifest 声明的文件缺失、题目/术语表校验失败。用 `python scripts/check_courses.py` 复核；修复后重启 worker。
- `Connection refused`：依次检查 Sakura FRP 的本地目标、Windows 的 `curl.exe http://localhost:8080/health`、`systemctl status nginx`、`systemctl status mcq-template`。
- `500 Internal Server Error`：查看 `journalctl -u mcq-template -n 100 --no-pager`；Flask 异常由 Gunicorn 写入该 journal。随后查看 `/var/log/nginx/mcq-template.error.log` 以关联代理请求。
