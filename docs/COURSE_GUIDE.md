# 课程模型与运营手册（Course Guide）

本文说明多课程版本的课程模型、目录布局、URL、发布流程与运维操作。所有命令与行为都以当前代码为准
（`app/models/course.py`、`app/repositories/course_loader.py`、`app/course_runtime.py`、
`app/services/course_service.py`、`scripts/*.py`）。

---

## 1. 核心概念

| 概念 | 说明 |
| --- | --- |
| `Course` | 一门课的不可变元数据：稳定的 `course_id`、`title`/`title_zh`、`enabled`、`order` |
| `CourseDefinition` | 内容在哪里、以哪种布局声明（`manifest` 或 `legacy`） |
| `CourseBundle` | `Course` + 不可变 `QuestionRepository` + `GlossaryRepository \| None` + 加载到的题库指纹与 publication identity |
| `CourseServices` | 该课程完整的 scoped 依赖图（persistence repositories + 全部 service + worker 加载到的 generation） |
| `CourseRegistry` | worker 内**只读**的 `course_id -> CourseServices` 查询，集中处理 missing / disabled / unavailable / stale |
| `CourseRepository` | SQLite 中的永久课程身份与已接受元数据（`courses` 表 + `schema_meta`） |
| `AppServices` | 只保存全站资源（共享 SQLite、账号、限流、课程身份表、课程注册表）；**不再暴露全局 `question_repository`** |

身份规则：

```text
Course          = course_id
Question        = (course_id, question_id)
Source          = (course_id, source_id)
Chapter         = (course_id, chapter_id)
Glossary term   = (course_id, term_id)
Option          = 题目内的 option_id
User            = 全站 user_id
Exam            = 全站唯一 exam_id，且父 session 上有显式 course_id
```

不同课程**允许**同时存在 `A/q001` 与 `B/q001`，以及 `A/chapter_1` 与 `B/chapter_1`，且内容与正确答案
可以完全不同。不要给原 ID 加课程前缀，也不要通过拆字符串推导课程，更不需要为跨课程 ID 冲突重命名内容。

两个**不同**的课程 ID 概念：

* `LEGACY_COURSE_ID`（默认 `legacy`）：旧数据库第一次 migration 时确定并**持久化**在
  `schema_meta.legacy_course_id`，永久表示"旧数据属于哪门课"。只能读取，不会被后续操作改写。
* `DEFAULT_COURSE_ID`（导航偏好）：只决定"没有显式课程 URL 的浏览器跳到哪里"，可以来自环境变量
  `MCQ_DEFAULT_COURSE` 或 `schema_meta.default_course_id`。修改它**绝不会**重新归属历史数据。

User ID 仍为全站身份；Exam ID 仍全站唯一；SQLite 仍为共享数据库，不按课程拆分。

---

## 2. 目录布局

每门课一个 manifest（`courses/<course_id>/course.json`）：

```text
courses/
├── digital_ic/
│   ├── course.json                 # manifest（课程身份与元数据）
│   ├── questions_candidate.json    # 题库候选：日常编辑的工作副本
│   ├── glossary_candidate.json     # 术语表候选（可选）
│   ├── questions.json              # 已发布题库（--add 首次写入；发布后 manifest 指向 versions/）
│   ├── glossary.json               # 已发布术语表（可选）
│   ├── versions/<sha256>/…         # 每次发布保存的不可变内容副本
│   └── .publish.lock               # 发布锁（由 publish_course.py 维护）
└── physical_design/
    ├── course.json
    ├── questions_candidate.json
    ├── glossary_candidate.json
    └── versions/…
```

`questions_candidate.json` / `glossary_candidate.json` 是**默认输入**：`check_*.py` 与 `publish_course.py` 在不传
文件路径时读它们（见 7 节）；`questions.json` / `glossary.json` / `versions/…` 是**已发布内容**，由 worker 读取，
不要手工原地编辑。`versions/` 与 `.publish.lock` 由发布命令维护，删除课程时随课程目录一起删除。

manifest（`schema_version: 1`）：

```json
{
  "schema_version": 1,
  "course_id": "physical_design",
  "title": "Physical Design",
  "title_zh": "物理设计",
  "enabled": true,
  "questions": "questions.json",
  "glossary": "glossary.json",
  "order": 20
}
```

规则：

* `title`/`title_zh` 在 manifest 存在时**以 manifest 为准**：这样一门课在 `disabled`（尚未加载）与 `enabled`（已加载）时显示的名字完全一致，不会因为是否加载成功而改名。只有 legacy 适配器（没有 manifest）才用题库自己的 `title`。
* 目录名**不参与**身份判定，`course_id` 只来自 manifest；两者不一致只记录一条 info 日志。
* 路径相对 manifest 目录解析，必须留在该目录内：拒绝绝对路径、`..` 与符号链接逃逸。
* 请求参数绝不会被拼进文件系统路径。
* `questions` 必需。
* `glossary: null` 表示"这门课明确没有术语表"；字段缺失是 manifest 错误（避免"忘记配置"被当成"没有术语表"）。
* manifest 声明了 glossary 但文件缺失/损坏 → 该课程 `unavailable`，**不会**假装没有术语表。
* 重复 `course_id` 是**全局启动错误**（应用无法装配）。
* `enabled=false`（目录级停用）与 runtime `unavailable`（启用了但加载失败）是两个不同状态。

`course_id` 必须是稳定的 URL-safe 小写 ASCII slug，允许 `_` 与 `-`，最长 64 字符
（`^[a-z0-9]+(?:[_-][a-z0-9]+)*$`）。

### 旧版根目录兼容

根目录 `questions.json` / `glossary.json` 仍然被支持，通过 **legacy adapter** 转换成一个虚拟
`CourseDefinition`（`course_id = legacy`，`layout = legacy`），然后进入**完全相同**的注册表、scoped
repository 与 service 装配流程。不存在第二套业务逻辑。

如果根 `questions.json` 不存在，manifest 课程就是全部；此时 `legacy` 只作为数据库身份出现，`/courses`
会把它显示为 `undeployed`（未部署，不影响就绪度）。

---

## 3. 运行时状态与故障隔离

| 状态 | 含义 | 学习页面 | 影响 `/ready` |
| --- | --- | --- | --- |
| `ready` | 已加载，且 worker generation == 数据库 generation | 200 | 是 |
| `stale` | 已加载，但数据库 generation 已推进 | 503（术语表除外） | 是（汇总 503） |
| `unavailable` | 已声明且启用，但内容加载/校验失败 | 503 | 是（汇总 503） |
| `disabled` | manifest `enabled: false`，故意不加载 | 503（停用提示） | 否 |
| `undeployed` | 数据库已知但本 worker 的课程目录未声明 | 503 | 否 |

* **单课程内容损坏**：该课程 `unavailable`，不提供服务、不同步题库、不做 learner reconciliation，
  历史状态原样保留；其他课程继续服务。
* **全局歧义**（重复 `course_id`、manifest 无法解析/读取）：整个应用装配失败，worker 不启动。
* 正常 A → B → A 切课是纯导航：不重新加载全局配置、不覆盖文件、不重启服务，也不会有 process-wide
  "current course"。

---

## 4. URL 与切课

```text
/                                    → 跳转到偏好课程（或 /courses）
/courses                             → 课程列表与状态
/course/<course_id>/                 → 本课程首页
/course/<course_id>/quiz/setup|quiz|quiz/start|quiz/answer|quiz/next
/course/<course_id>/review|review/start|review/answer|review/next
/course/<course_id>/mistakes|mistakes/reset
/course/<course_id>/dashboard
/course/<course_id>/stats
/course/<course_id>/glossary
/course/<course_id>/exam|exam/start|exam/<exam_id>|exam/<exam_id>/answer|submit|report
```

**URL 是唯一权威。** `session["last_course_id"]` 只在没有显式课程 URL 的入口（`/`）以及旧 URL 重定向
中作为偏好使用，**不会**覆盖 URL 中的课程。因此同一浏览器两个标签页（Tab 1 = A，Tab 2 = B）不会互相
改写当前请求的课程上下文。

表单：

* 所有学习表单的 action URL 都包含课程；
* CSRF 保留；
* 表单携带服务端签名的 `form_context`，至少绑定 `course_id`、`operation`、`generation`；
* Quiz / Review 继续保留 `answer_token`；
* Exam 表单额外绑定 `position` 与 `question_id`，事务内重新确认 `position -> 同一 question_id`，
  防止 startup reconciliation 重排槽位后旧页面写到别的题目。

事务守卫（`app/services/course_consistency.py`）在 `BEGIN IMMEDIATE` 之后依次检查：

```text
课程是否仍然 enabled
worker 加载的 generation == 数据库 generation
form_context（课程 / operation / generation）
（业务操作）
（learner 写入）
COMMIT
```

Web 层负责映射：stale worker → 503、stale form → 409、missing course → 404、unavailable → 503。

行为契约：

| 场景 | 结果 |
| --- | --- |
| A 页面打开 → 另一 tab 切到 B → 提交 A | 仍提交 A（A 的 URL 与 `answer_token` 都指向 A） |
| A 的表单提交到 B 的 URL | 拒绝，零写入 |
| worker 的 A generation 过期 | 503，零 learner 写入 |
| 新 worker 收到旧结构的 A 表单 | 409（要求刷新），**不清空**进度 |
| 课程不存在 | 404 |
| 课程已知但不可用/未加载 | 503 |
| B 的 URL + A 的 exam | 409 |

旧 URL：

* 旧 `GET`（如 `/quiz`、`/exam/<id>/report`）会重定向到显式课程 URL；`exam_id` 会先校验 learner 归属，
  再用数据库里该 session 的真实 `course_id` 决定目标课程，所以书签在切课后仍然可用。
* 旧 `POST`（无课程）**不会**根据 session 猜课程再转发，而是直接返回 409（要求刷新）。

---

## 5. 术语表

* 每门课可选：`glossary: null` 表示没有。`/course/<id>/glossary` 在没有术语表时渲染明确的空状态，
  只下发空 payload，**绝不会**注入其他课程的术语。
* 术语/别名只在本课程内可见（`(course_id, term_id)`）。
* `glossary.json` **不参与**题库 generation：单独修改术语表不影响学习记录，也不会围栏 worker。
* `app/static/js/glossary.js` 保持通用（只读取当前页面的 payload），不为不同课程复制算法。

---

## 6. 就绪与诊断

```text
/health             进程活着（stale worker 也返回 200，因为仍要能登录/登出）
/ready              本 worker 是否可服务其"声明且启用"的所有课程
/ready/<course_id>  指定课程在本 worker 上的状态
```

`/ready` 返回示例：

```json
{
  "status": "degraded",
  "courses": {
    "course_a": {"status": "stale", "worker_generation": 0,
                 "database_generation": 1, "reason": "..."},
    "course_b": {"status": "ready", "worker_generation": 3,
                 "database_generation": 3, "reason": ""}
  },
  "enabled_course_count": 2,
  "ready_course_count": 1
}
```

`/ready/<course_id>` 返回：

```json
{"status": "stale", "course_id": "course_a",
 "worker_generation": 0, "database_generation": 1, "reason": "..."}
```

关键语义：**汇总 `/ready` 因 A stale 返回 503 不会导致 B 的路由返回 503**。汇总只是监控信号，
路由永远按 URL 的课程决定。`unknown` 课程返回 404。


---

## 7. 日常操作

所有课程内容命令都遵循同一个约定：**候选文件（working copy）位于课程目录内**，不传路径参数时默认读取它。

```text
courses/<course_id>/
├── course.json                 # manifest（身份与元数据）
├── questions_candidate.json    # 题库候选（默认输入）
├── glossary_candidate.json     # 术语表候选（默认输入）
├── questions.json              # 已发布内容（`--add` 或 legacy 布局的落点）
├── glossary.json
└── versions/<sha256>/…         # 每次发布保存的不可变内容副本
```

默认文件解析规则（`check_*.py` 与 `publish_course.py` 一致）：

1. 显式传入的路径永远优先；
2. 否则使用课程目录里的 `questions_candidate.json` / `glossary_candidate.json`（存在时）；
3. 候选文件不存在时，`check_*.py` 回退到该课程**当前已发布**的文件（用 `--published` 可强制只看已发布文件）；
4. `publish_course.py` 在没有候选文件可发布时不写任何东西（退出码 2），并打印它期望的默认路径。

因此课程目录里存在 `questions_candidate.json` 之后，`python scripts/check_question_bank.py --course <course_id>` 检
验的就是这份候选，而不是线上文件。

### 7.1 校验课程目录

```bash
python scripts/check_courses.py            # 人类可读报告
python scripts/check_courses.py --json     # 机器可读
```

全局歧义（重复 `course_id`、manifest 非法）退出码 2；单门课程加载失败退出码 1，但其他课程仍会报告。

### 7.2 新增课程（从零开始）

完整流程（`<course_id>` 用小写 slug，例如 `physical_design`）：

```bash
# 1) 创建课程目录和候选文件（候选文件是唯一的输入）
mkdir -p courses/physical_design
cp my_questions.json  courses/physical_design/questions_candidate.json
cp my_glossary.json   courses/physical_design/glossary_candidate.json   # 可选；不需要术语表就跳过

# 2) 校验候选：题库 schema + 与数据库的差异；术语表 schema + 覆盖度
python scripts/check_question_bank.py --course physical_design --db instance/mcq.db
python scripts/check_glossary.py --course physical_design

# 3) 修复检查结果（见下表），重复 2) 直到退出码为 0

# 4) 创建课程：发布候选内容并写入 manifest
python scripts/publish_course.py --course physical_design --add \
  --title "Physical Design" --title-zh "物理设计" --order 20

# 5) 统一重启全部 worker，并验证
curl -i http://127.0.0.1:8001/ready/physical_design
python scripts/check_courses.py
```

执行 `--add` 后创建/更新的文件：

| 文件 | 内容 |
| --- | --- |
| `courses/<course_id>/course.json` | manifest：`course_id`、标题、`enabled`、`order`、内容路径 |
| `courses/<course_id>/questions.json` | 题库的已发布副本（候选文件保留不动） |
| `courses/<course_id>/glossary.json` | 术语表的已发布副本（提供候选时才有） |
| `courses` 表 | 该课程的永久身份与已接受元数据（worker 启动时写入） |

`--add` 只做 schema 校验（新课程没有历史可比对），并提示 `created, pending worker activation`；重启 worker 后
`/ready/<course_id>` 应返回 `ready`，`check_courses.py` 应列出该课程且状态为 `ok`。已有课程只能用
`--questions`/`--glossary` 发布新内容，`--add` 会拒绝覆盖。

检查结果的常见修复：

| 检查输出 | 修复方式 |
| --- | --- |
| `题库校验失败` | 修正 JSON 语法、必填字段、重复 ID、`correct_answers` 与 `type` 不匹配等 |
| 退出码 2（复用退役 ID） | 为新题目分配**全新** ID，不要复用历史 ID |
| `grading-changed` / `deleted` 警告 | 确认可以清理这些题的学习状态；`--strict` 可让这种情况直接返回非 0 |
| `catalogue-changed: yes` | 属于结构性变化：发布后必须统一重启 worker |
| 术语表 `Orphan entries` / 候选词 | 人工复核：补 alias、删多余词条或补充题库，不影响退出码 |

### 7.3 修改已有课程

题库和术语表各自独立修改，标准流程都是「改候选 → 校验 → 发布」：

```bash
# 题库：只编辑候选文件，不要原地覆盖正在使用的 questions.json
python scripts/check_question_bank.py --course physical_design --db instance/mcq.db
python scripts/publish_course.py --course physical_design --questions questions_candidate.json
# （也可以省略 --questions：默认就发布 courses/physical_design/questions_candidate.json）

# 术语表：同样先改候选文件
python scripts/check_glossary.py --course physical_design
python scripts/publish_course.py --course physical_design --glossary glossary_candidate.json

# 两个候选都改好了，可以一次发布（不传内容参数 = 使用两个默认候选）
python scripts/publish_course.py --course physical_design
```

要点：

* **先改候选文件**：`questions.json` / `glossary.json` / `versions/…` 是已发布内容，被 worker 直接读取；原地覆盖
  可能让启动中的 worker 读到半截 JSON。
* `publish_course.py` 默认会重跑对应的 `check_*.py` 并拒绝未通过的内容（`--skip-preflight` 可跳过，不推荐）。
* 校验通过、发布成功后输出 `published, pending worker activation`；题库的结构性变化会让**该课程** generation +1，
  因此需要统一重启全部 worker，再用 `/ready/<course_id>` 确认；术语表不参与 generation。
* 确认最终数据：`python scripts/check_courses.py` 查看题目/术语数量，并重新启动应用后在页面上核对内容。

### 7.4 删除课程

**不要直接 `rm -rf courses/<course_id>`，也不要手工删数据库里的行。** 一门课程在项目里不止是一个目录，手工删除会留下：

* `courses` 表里的永久课程身份与已接受元数据（`/courses` 会一直显示它）；
* 该课程的全部学习数据（`quiz_progress`、`attempts`、`wrong_questions`、`weak_knowledge_points`、
  `exam_sessions` 及其 `exam_questions` 槽位）——这些行没有目录引用，不会被 `rm` 影响；
* `question_bank_state`（generation）与 `question_registry`（该课程**永久**的题目 ID 退役记录）；
* `schema_meta.default_course_id` 这类导航偏好仍指向已删除的课程；
* `versions/<sha256>/` 之外的引用（candidate 文件）与已发布内容同时消失，但数据库仍认为课程存在。

结果是仓库进入不一致状态：内容没了、历史还在，以后同名 `course_id` 一旦重新加入就会继承旧的退役 ID；
`check_courses.py` 会把数据库里的身份报告为 `undeployed`。

用正式脚本删除（先看，后删）：

```bash
# 1) 先看会删除什么：目录、每张表的行数、将清除的默认课程偏好；不写任何文件
python scripts/delete_course.py --course physical_design --dry-run

# 2) 确认无误后执行（会自动为该数据库生成带时间戳的备份）
python scripts/delete_course.py --course physical_design

# 课程仍有学习数据时，必须先明确确认（否则拒绝执行）
python scripts/delete_course.py --course physical_design --force

# 3) 统一重启全部 worker，并复核
python scripts/check_courses.py
```

安全机制：

| 机制 | 说明 |
| --- | --- |
| `--dry-run` | 只报告：课程目录、`courses` 行、每张 course-scoped 表的行数、`question_registry` 退役记录、将清除的 `default_course_id` |
| 时间戳备份 | 默认先复制 `instance/mcq.db` 为 `mcq.db.bak-<UTC 时间戳>`（`--no-backup` 可关闭，不推荐） |
| 学习数据确认 | 课程还有学习数据但没有 `--force` 时拒绝执行（退出码 1），且不写任何内容 |
| 单课程范围 | 所有 SQL 都以 `course_id` 为条件；提交前会重新统计每张表的总行数，必须恰好减少被删除的行数，否则回滚 |
| 目录白名单 | 只删除位于 `--courses-dir` 之内、且 manifest 声明的 `course_id` 与目标一致的目录 |
| legacy 保护 | 根目录 `questions.json` 的 legacy adapter 没有课程目录，拒绝执行（先用 `migrate_courses.py --layout` 迁移）；持久化的 `legacy_course_id` 也不能删除（它的行会在每次启动时重建，改归属请用 `rename_course.py`） |

删除后该课程的题目 ID 退役记录一并消失：如果以后重新加入同名课程，它的 ID 从零开始，不会继承旧的退役状态。
数据库里只剩身份、内容已不在的课程（`undeployed`）也可以用同一命令清理：它会报告为「未部署的课程身份」并只删除数据库记录。

### 7.5 停用 / 重新启用

```bash
python scripts/publish_course.py --course physical_design --disable
python scripts/publish_course.py --course physical_design --enable
```

以 manifest 的 `enabled` 为准；学习数据不会被修改。重启 worker 后生效。

### 7.6 重命名一门课程（namespace 迁移）

`course_id` 就是这门课所有学习数据的 namespace，因此“改课程 ID”不是改一个字符串，而是把所有 learner 行的 key 一致地改掉：

```bash
# 先看会移动哪些行，不写任何内容
python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 --dry-run

# 真正执行（自动生成带时间戳的备份）
python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106

# 同时也移动课程目录并改写 manifest 的 course_id
python scripts/rename_course.py --db instance/mcq.db --from legacy --to eek5106 \
  --courses-dir courses --rename-directory
```

工具保证：先备份；在单个 `BEGIN IMMEDIATE` 事务内改写 `courses` / `quiz_progress` / `attempts` / `wrong_questions` / `weak_knowledge_points` / `exam_sessions` / `question_bank_state` / `question_registry`，并在前后重新计数校验（不一致就整体回滚）；目标 namespace 已有元数据或任何 learner 行时拒绝执行，避免把两个身份合并；`exam_questions` 不参与改写（它不含 `course_id`，始终跟随父 session）；如果改的正是持久化的 `legacy_course_id`，该键也会一起更新，所以重启后不会再出现一个空的 `legacy` 课程。

它**不会**清理数据、不会推进 generation、也不会重算 fingerprint：历史只是换了 namespace。完成后记得让 `courses/<course_id>/course.json` 的 `course_id` 与数据库一致，再重启 worker，并用 `/ready/<course_id>` 确认。

### 7.7 发布某门课的题库

```bash
# 1) 只读预检（默认检查 courses/physical_design/questions_candidate.json）
python scripts/check_question_bank.py --course physical_design --db instance/mcq.db

# 2) 原子发布（默认重跑上面的预检；--skip-preflight 才可跳过，不推荐）
python scripts/publish_course.py --course physical_design --questions questions_candidate.json
# 省略 --questions 时同样发布该课程的默认候选文件

# 3) 统一重启 worker，并确认
curl -i http://127.0.0.1:8001/ready/physical_design
```

指定其他候选文件、或只想复核已发布内容：

```bash
python scripts/check_question_bank.py --course physical_design other_questions.json --db instance/mcq.db
python scripts/check_question_bank.py --course physical_design --published --db instance/mcq.db
python scripts/publish_course.py --course physical_design --questions other_questions.json
```

`publish_course.py --questions` 的顺序是：**一次性**读取候选文件字节 → 用同一份字节校验 → 只读预检 → 写入不可变
的 `versions/<sha256>/questions.json` → 在课程发布锁内**重新验证** baseline → 单次 `os.replace` 原子切换
manifest → fsync 目录。它输出 `published, pending worker activation`，**不会**自行 bump generation，也
**不**声称文件系统发布与数据库激活是同一个事务。

`--course` 对本命令是必填；多课程部署请始终显式指定。

回滚走正常流程：把旧内容当作新候选再发布一次（`check_question_bank.py` + `publish_course.py --questions`），
**不允许**手工 `generation--`。

### 7.8 术语表校验

```bash
python scripts/check_glossary.py --course physical_design
python scripts/check_glossary.py --all
python scripts/check_glossary.py --course physical_design --published          # 只校验已发布文件
python scripts/check_glossary.py --questions path/questions.json --glossary path/glossary.json  # 显式离线
```

单课程模式下，语料与术语表同样优先使用该课程的 `questions_candidate.json` / `glossary_candidate.json`，报告里会
打印实际读取的两个文件与来源（`candidate` / `published`）。校验只读取内容，不修改 registry、generation 或任何学
习数据。校验通过后再发布：

```bash
python scripts/publish_course.py --course physical_design --glossary glossary_candidate.json
# 或直接：python scripts/publish_course.py --course physical_design（使用默认候选）
```

### 7.9 故障定位

```bash
curl -s http://127.0.0.1:8001/ready | python -m json.tool
curl -i http://127.0.0.1:8001/ready/physical_design
journalctl -u mcq-template.service -n 100 --no-pager | grep -E 'is stale|is unavailable'
```

* `stale`：还有 worker 使用旧内容 → 统一重启；只有该课程的页面被围栏。
* `unavailable`：该课程内容损坏 → 查看日志中的具体原因，修复后用 `check_courses.py` 复核。
* `undeployed`：数据库有该课程身份，但本 worker 的课程目录未声明它 → 检查部署内容是否齐全。

### 7.10 内容变更统一流程（先校验，后发布）

脚本命名统一为 `check_<校验对象>.py`；每一类课程内容都有一个只读校验脚本，发布命令只切换已通过校验的内容：

| 变更对象 | 校验脚本（只读） | 发布命令 |
| --- | --- | --- |
| 课程目录 / manifest / 整门课能否加载 | `check_courses.py` | `publish_course.py --add` / `--enable` / `--disable` / `delete_course.py` |
| `questions.json` | `check_question_bank.py --course <course_id> --db instance/mcq.db` | `publish_course.py --course <course_id> --questions questions_candidate.json` |
| `glossary.json` | `check_glossary.py --course <course_id>` | `publish_course.py --course <course_id> --glossary glossary_candidate.json` |

固定流程（缺一不可）：

1. **准备校验脚本**：确认该内容种类已有 `check_<校验对象>.py`；若还没有，先补齐脚本与测试。
2. **修改内容**：只编辑候选文件（`courses/<course_id>/questions_candidate.json` /
   `glossary_candidate.json`），不要原地覆盖正在使用的 `questions.json` / `glossary.json`。
3. **运行校验**：`check_courses.py` 必须始终通过；题库再跑 `check_question_bank.py`，术语表再跑 `check_glossary.py`。
   不传文件参数时它们默认校验上面的候选文件（用 `--published` 可改为只校验已发布内容）。
4. **重新执行 publish course**：只有校验退出码为 `0` 才发布（`publish_course.py` 会在写入前内部重跑对应的 `check_*.py` 并拒绝未通过的内容；只有明确加 `--skip-preflight` 才跳过，且不推荐）。发布是纯文件系统切换，最后统一重启全部 worker，并用 `/ready/<course_id>` 确认。

校验未通过时不得发布：schema 校验失败或校验脚本返回非零时，发布命令不会替换、不会创建任何文件，也不会推进 generation。

---

## 8. 内容发布生命周期（per course）

结构化与非结构化变更都**限定在该课程内**，且一次发布最多让该课程 generation **+1**：

| 变更 | 历史保留 | generation |
| --- | --- | --- |
| 题干 / 翻译 / 解析 / 选项文案 / 选项顺序 / `section` / `pages` / JSON 格式 | 是 | 不变 |
| 新增一个错误选项（判题身份兼容） | 是 | 不变 |
| 判题身份不兼容（题型、正确答案集合、删除/重命名选项 ID） | 只清理该课程该题的相关 learner state | +1 |
| 删除题目 | attempts 保留，错题/SRS 等按现有规则处理 | +1 |
| 新增 / 恢复题目 | 按现有规则 | +1 |
| `chapter_ids` / `source_id` 变化 | 是 | +1 |
| 课件/章节增删、顺序或归属（catalogue） | 是 | +1 |
| 课程/课件/章节标题、`lecture`、`filename` | 是 | 不变 |
| `glossary.json` | 是 | 不变 |

attempts 保留窗口是"每个 `(learner_id, course_id, question_id)` 最近 10 次"，仍跨 Normal / Review / Exam
共享该窗口。

fingerprint（grading / content / placement / catalogue）算法**未改变**，且**不包含** `course_id`：相同 hash
可以出现在不同课程；所有 fingerprint 比较都发生在绑定课程的注册表内；namespace migration 不会重算任何
历史 fingerprint。retired question ID 只在本课程内永久保留。

陈旧 worker 的写入会在事务内被拒绝（503），因此"旧 worker 外层预检通过 → 新 worker 提交 A 的
generation → 旧 worker 才开始 learner 事务"这一竞态以**零 learner 写入**结束。

---

## 9. 明确的限制

* 历史考试**不会**神奇地获得完整的历史题目内容快照：报告始终基于当前 live 内容，且 grading fingerprint
  已变化的槽位会被排除在统计之外。
* 被停用或不可用的课程**不会**用其他课程的同名题去补内容。
* legacy migration 保留 namespace 与历史，但无法推断不可知的旧数据课程归属。
* 旧版应用无法在迁移后的 Schema 上安全运行（见
  [`MULTI_COURSE_MIGRATION.md`](MULTI_COURSE_MIGRATION.md)）。

