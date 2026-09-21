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

* `LEGACY_COURSE_ID`（默认 `legacy`）：旧数据库第一次执行 schema migration 时确定并**持久化**在
  `schema_meta.legacy_course_id`，永久表示"旧数据属于哪门课"。普通运行时的 `CourseRepository` API
  **只提供读取**（`legacy_course_id()`），没有任何接口可以重新配置它——否则会静默把历史重新归属。
  唯一支持的变更方式是显式运维操作 `scripts/rename_course.py`：它在改 namespace 时**会一起更新这个键**
  （见 7.6），这样重启后不会再冒出一个空的 `legacy` 课程。启动时 `Database.register_courses()` 写入的是
  **持久化后的值**而不是模块默认值，所以也不会把它重建回来。
* `DEFAULT_COURSE_ID`（导航偏好）：只决定"没有显式课程 URL 的浏览器跳到哪里"，可以来自环境变量
  `MCQ_DEFAULT_COURSE` 或 `schema_meta.default_course_id`。修改它**绝不会**重新归属历史数据。

User ID 仍为全站身份；Exam ID 仍全站唯一；SQLite 仍为共享数据库，不按课程拆分。

---

## 2. 目录布局

每门课一个 manifest（`courses/<course_id>/course.json`）：

```text
courses/
├── digital_ic/
│   ├── course.json                 # manifest（课程身份与元数据，唯一权威指针）
│   ├── questions_candidate.json    # 题库候选：日常编辑的工作副本
│   ├── glossary_candidate.json     # 术语表候选（可选）
│   ├── versions/<sha256>/…         # 已发布内容：新增课程/每次发布保存的不可变副本
│   │                               # （每个内容类型只保留当前版本 + 上一版，见 7.11）
│   ├── .publish.lock               # 发布锁（由 publish_course.py 维护）
│   ├── questions.json              # 仅 plain-file 布局（旧课程/手工创建）的已发布题库
│   └── glossary.json               # 同上（可选）
└── physical_design/
    ├── course.json
    ├── questions_candidate.json
    ├── glossary_candidate.json
    └── versions/…
```

`questions_candidate.json` / `glossary_candidate.json` 是**默认输入**：`check_*.py` 与 `publish_course.py` 在不传
文件路径时读它们（见 7 节）；`versions/…`（以及 plain-file 布局下 manifest 直接指向的 `questions.json` /
`glossary.json`）是**已发布内容**，由 worker 读取，不要手工原地编辑。`--add` 新增课程和后续发布一样把内容归档到
`versions/<sha256>/`，所以新课程的目录根部**不会**留下无人引用的已发布副本；`versions/` 与 `.publish.lock` 由发布
命令维护，删除课程时随课程目录一起删除。每次发布结束都会自动清理 `versions/`：每个内容类型只保留**当前版本 + 上一版**
（`--keep-versions N` 可改，`--prune` 可只清理，`--no-prune` 可跳过），既不无限占用空间，又保留一步回退能力
（见 7.11 节）。

⚠️ manifest 一旦指向 `versions/…`，根部的 `questions.json` / `glossary.json` 就**不再被任何进程读取**（loader 只解析
manifest 声明的路径，`check_*.py` 的 `--published` 也只看 manifest 指向的文件）。它们只可能是旧课程的历史/回滚副本；
`check_courses.py` 会把这类文件作为提示列出（见 7.1），编辑它不会生效。

manifest（`schema_version: 1`）——推荐让 `questions`/`glossary` 指向不可变发布副本：

```json
{
  "schema_version": 1,
  "course_id": "physical_design",
  "title": "Physical Design",
  "title_zh": "物理设计",
  "enabled": true,
  "questions": "versions/a8634a86…/questions.json",
  "glossary": "versions/c3c68b98…/glossary.json",
  "order": 20
}
```

`--add` 与每次发布都会把内容归档到 `versions/<sha256>/`，并把 manifest 切换到那里，所以**新课程不会**在目录根部留下已发布副本。下面的 plain-file 形式只用于老课程或手工创建的课程（manifest 直接指向目录内的 `questions.json` / `glossary.json`）：

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
  其中 `operation` 是**表单提交目标**的操作（即 action URL 指向的路由），而不是渲染该表单的页面：
  守卫用它与请求真正到达的端点比对，签成页面操作会让每次提交都被判为 stale form（409）。
  模板统一写成 `form_context(<form action 的 endpoint>)`。
* Quiz / Review 继续保留 `answer_token`；`answer_token` 与 `csrf_token` 都由
  `app/web/auth.py::tokens_match()` 按 UTF-8 字节做常量时间比较：任何缺失、被篡改或非 ASCII 的提交
  都返回 400（invalid submission），不会把拒绝变成未捕获异常（500）。
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
| 伪造 / 缺失 / 非 ASCII 的 `answer_token` 或 `csrf_token` | 400，零写入（不产生 500） |

旧 URL：

* 旧 `GET`（如 `/quiz`、`/exam/<id>/report`）会重定向到显式课程 URL；`exam_id` 会先校验 learner 归属，
  再用数据库里该 session 的真实 `course_id` 决定目标课程，所以书签在切课后仍然可用。
* 旧 `POST`（无课程）**不会**根据 session 猜课程再转发，而是直接返回 409（要求刷新）。

---

## 5. 术语表

* 每门课可选：`glossary: null` 表示没有。`/course/<id>/glossary` 在没有术语表时渲染明确的空状态，
  只下发空 payload，**绝不会**注入其他课程的术语。
* 术语/别名只在本课程内可见（`(course_id, term_id)`）；它只存在于当前课程的内存 `CourseServices` 中，
  **不写入 SQLite**，术语 ID 也不是任何数据库注册表的主键。
* `glossary.json` **不参与**题库 generation 或任何指纹：单独修改/发布术语表不影响学习记录，不会围栏 worker，
  也不会让 worker 变 stale；`/ready` 与 `/ready/<course_id>` 很可能继续返回 200。
* 但术语表只在**进程启动时**读入内存：已运行的 worker 仍在使用旧术语，**必须统一重启才能看到新内容**。
  也就是说 readiness 不能证明术语表已重新加载，判断是否生效要用浏览器验收。
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
├── versions/<sha256>/…         # 已发布内容（`--add` 与每次发布的不可变副本）
└── questions.json / glossary.json   # 仅 plain-file 布局的旧课程才有
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

报告还会以**提示**（不影响退出码）列出课程目录里没有被 manifest 引用的 `questions.json` / `glossary.json`
（`--json` 输出中的 `unreferenced_copies` 字段）：这类文件来自 plain-file 布局的旧课程，任何进程都不会读取它，
编辑它不会生效，通常只作历史/回滚副本保留或直接删除。

### 7.2 新增课程（从零开始）

**顺序要点：`--course` 只能解析「已声明的课程」。** 声明一门课的只有 `courses/<course_id>/course.json`，而这个
manifest 由 `publish_course.py --add` 写入。只有候选文件、还没有 manifest 的目录会被 loader 忽略（日志与校验脚本
都会打印 `Ignoring course directory …: no course.json found`），此时把新 ID 传给 `--course` 只会得到
`Unknown course "<course_id>"`（`check_question_bank.py` 退出码 4，`check_glossary.py` 退出码 1）。因此新课程是
「先创建、再按课程校验」，而不是像已有课程那样「先校验候选、再发布」。

完整流程（`<course_id>` 用小写 slug，例如 `physical_design`）：

```bash
# 1) 创建课程目录和候选文件（候选文件是唯一的内容输入）
mkdir -p courses/physical_design
cp my_questions.json  courses/physical_design/questions_candidate.json
cp my_glossary.json   courses/physical_design/glossary_candidate.json   # 可选；不需要术语表就跳过

# 2) 创建课程：读取候选 → schema/术语表校验 → 归档到 versions/<sha256>/ → 写入 course.json
#    （任何一项校验失败都不会创建目录、也不会写入任何文件）
python scripts/publish_course.py --course physical_design --add \
  --title "Physical Design" --title-zh "物理设计" --order 20

# 3) 课程已被声明，现在按课程校验（默认读候选文件，见 7.1 节）
python scripts/check_question_bank.py --course physical_design --db instance/mcq.db
python scripts/check_glossary.py --course physical_design

# 4) 修复检查结果（见下表），重复 3) 直到退出码为 0

# 5) 统一重启全部 worker，并验证
curl -i http://127.0.0.1:8001/ready/physical_design
python scripts/check_courses.py
```

第 2 步与第 3 步的分工：

* `--add` 对**新课程没有历史可比对**，所以它只做 schema 门禁（题库 `validate_bytes`、术语表离线覆盖校验）；任一校验失败时它**不会**写入 `course.json`，也**不会**写入任何 `versions/<sha256>/` 发布副本，但**不会**撤销你自己创建的目录或候选文件——`courses/<course_id>/` 与 `questions_candidate.json` / `glossary_candidate.json` 是维护者的工作副本，`--add` 只负责校验它们并创建课程声明。真正「与数据库比对」的报告只能在第 3 步做。
* 第 3 步在 worker 首次启动之前运行也没问题：数据库里还没有该课程的 `question_registry` / `question_bank_state`
  行，检查会明确报告「该课程在数据库中没有 question_registry 记录：首次启动将建立基线」，退出码为 `0`；真实的历史
  差异要等该课程已激活后再用同一命令复查（那时它会读到已建立的基线）。
* 术语表在课程创建**之前**也可以校验：显式离线模式不要求课程存在：
  `python scripts/check_glossary.py --questions courses/physical_design/questions_candidate.json --glossary courses/physical_design/glossary_candidate.json`。
* **不要**在课程创建前用位置参数校验新题库（`check_question_bank.py courses/physical_design/questions_candidate.json`）：
  当候选不属于任何已声明课程且没有 `--course` 时，它会把该候选当作 legacy 部署的根内容位置，与持久化的 legacy
  命名空间比对，报告出的 `deleted` / `learner state will be cleared` 与你真正要新增的课程无关。

执行 `--add` 后创建/更新的文件：

| 文件 | 内容 |
| --- | --- |
| `courses/<course_id>/course.json` | manifest：`course_id`、标题、`enabled`、`order`、指向 `versions/…` 的内容路径 |
| `courses/<course_id>/versions/<sha256>/questions.json` | 题库的已发布副本（候选文件保留不动） |
| `courses/<course_id>/versions/<sha256>/glossary.json` | 术语表的已发布副本（提供候选时才有） |
| `courses/<course_id>/.publish.lock` | 发布锁（创建课程时即建立，供后续发布串行化） |
| `courses` 表 | 该课程的永久身份与已接受元数据（worker 启动时写入） |

课程目录**根部不会再写入** `questions.json` / `glossary.json`（那是 plain-file 布局，仅保留给旧课程与手工/legacy
场景），所以新课程从第一天起就不存在「看起来一样、实际无人读取」的副本。

`--add` 的执行顺序与提交点：读取候选 → 校验（题库 schema / 术语表覆盖）→ 创建 `courses/<course_id>/` → 取得 `.publish.lock` → 写入 `versions/<sha256>/` 归档副本 → **写入 `course.json`（唯一提交点）** → 版本清理。因此校验失败时不会有目录、manifest 或 `versions/` 副本；而 manifest 一旦写入，课程就已经被声明，后续（版本清理等）失败会在输出里说明。命令提示 `created, pending worker activation`；重启 worker 后 `/ready/<course_id>` 应返回 `ready`，`check_courses.py` 应列出该课程且状态为 `ok`。已有课程只能用 `--questions`/`--glossary` 发布新内容，`--add` 会拒绝覆盖。

`courses` 表的那一行由 worker 启动时写入，`--add` 本身只改文件系统，所以新课程在第一次成功启动前，任何
`--course <course_id>` 校验都还没有历史可比对；这不是错误，见第 3 步的说明。

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
# 题库：只编辑候选文件，不要原地覆盖正在使用的已发布文件
python scripts/check_question_bank.py --course physical_design --db instance/mcq.db
python scripts/publish_course.py --course physical_design --questions questions_candidate.json
# （也可以省略 --questions：默认就发布 courses/physical_design/questions_candidate.json）

# 术语表：同样先改候选文件
python scripts/check_glossary.py --course physical_design
python scripts/publish_course.py --course physical_design --glossary glossary_candidate.json
```

两个候选都改好时，可以一次命令发布两者（不传内容参数 = 使用该课程适用的默认候选）：

```bash
python scripts/publish_course.py --course physical_design
```

但要理解这一次命令内部的语义：

* `--questions` 与 `--glossary` 是**按顺序分别执行**的两个发布步骤，**不是两个文件的一次事务**。题库可能已经
  切换成功而术语表步骤失败（此时命令以非 0 退出，但题库已经发布）；
* 因此更稳妥的做法是**先分别完成两次只读预检**（`check_question_bank.py` 与 `check_glossary.py`），确认都通过；
  需要清晰失败边界时按内容类型拆成两条命令发布；
* 单个内容类型的 manifest 切换本身是原子的：候选字节被冻结一次、写入不可变副本、最后用一次 `os.replace` 切换 manifest；
* 术语表不参与 generation：单独发布术语表不会推进 generation、不会让 worker stale，但**已运行的 worker 仍在使用旧的内存术语表，必须重启才能看到新内容**，而且 `/ready` 很可能一直是 200，不能用它证明术语表已重新加载。

要点：

* **先改候选文件**：`versions/…`（以及 plain-file 布局下 manifest 直接指向的 `questions.json` / `glossary.json`）是已发布
  内容，被 worker 直接读取；原地覆盖可能让启动中的 worker 读到半截 JSON。
* `publish_course.py` 默认会重跑对应的 `check_*.py` 并拒绝未通过的内容（`--skip-preflight` 可跳过，不推荐）。
* 校验通过、发布成功后输出 `published, pending worker activation`；题库的结构性变化会让**该课程** generation +1，
  因此需要统一重启全部 worker，再用 `/ready/<course_id>` 确认；术语表不参与 generation。
* bare `--course <course_id>` 只选取**存在且适用于当前 manifest** 的默认候选：题库候选存在就发布题库；术语表候选只有在
  该课程 manifest 已声明 glossary（即 `glossary` 不是 `null`）时才自动发布。manifest 写 `glossary: null` 时，候选文件
  仍然会被识别，但会被跳过并打印提示——启用术语表必须**显式**传 `--glossary`。
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

**执行顺序与失败语义（文件系统和数据库不是同一个事务）：**

```text
--dry-run 报告（不写任何内容）
  → 复制带时间戳的数据库备份
  → 删除课程目录（如果该课程有目录）
  → 在一个事务里清理该课程的全部数据库行（提交前重新统计每张表的总行数）
```

* 数据库清理失败时（例如被占用或约束冲突），**目录已经被删除**，而数据库保持原样：命令会打印
  「数据库清理失败（目录已删除，数据库保持原样）」并以退出码 1 结束。
* 这种情况下课程会变成 `undeployed`（数据库有身份、内容已不在）。修复报错原因后**重新执行同一条命令**即可
  只清理数据库记录（此时没有目录可删，命令会把它当作未部署身份处理），不需要手工写 SQL。
* 目录删除失败时不会进入数据库清理阶段，数据库完全没有被改动。

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
python scripts/rename_course.py --db instance/mcq.db --from legacy --to <new_course_id> --dry-run

# 真正执行（自动生成带时间戳的备份）
python scripts/rename_course.py --db instance/mcq.db --from legacy --to <new_course_id>

# 同时也移动课程目录并改写 manifest 的 course_id
python scripts/rename_course.py --db instance/mcq.db --from legacy --to <new_course_id> \
  --courses-dir courses --rename-directory
```

工具保证：先备份；在单个 `BEGIN IMMEDIATE` 事务内改写 `courses` / `quiz_progress` / `attempts` / `wrong_questions` / `weak_knowledge_points` / `exam_sessions` / `question_bank_state` / `question_registry`，并在前后重新计数校验（不一致就整体回滚）；目标 namespace 已有元数据或任何 learner 行时拒绝执行，避免把两个身份合并；`exam_questions` 不参与改写（它不含 `course_id`，始终跟随父 session）；如果改的正是持久化的 `legacy_course_id`，该键也会一起更新，所以重启后不会再出现一个空的 `legacy` 课程。

**数据库改名与目录改名不是同一个事务。** 加 `--rename-directory` 时，

```text
备份数据库
  → 一个事务内完成数据库 namespace rename（提交后即生效）
  → 之后才作为独立步骤把 courses/<from> 改名为 courses/<to>
      并改写新目录里 manifest 的 course_id（title / title_zh 属于内容决定，不自动改）
```

因此目录改名失败（权限、目标目录已存在、文件被占用）**不会**回滚数据库：命令以退出码 1 结束，数据库里历史已经挂在新的 `course_id` 下，但目录仍是旧名字。此时课程会表现为 `undeployed`（数据库有身份、本 worker 未声明）。修复方式二选一：

* 手工完成目录改名（`mv courses/<from> courses/<to>`），并把新目录 `course.json` 的 `course_id` 改成目标值 —— 结果与工具一致；
* 或者用备份回退数据库（`cp instance/mcq.db.bak-<timestamp> instance/mcq.db`），再重新执行一次完整的 rename。

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
manifest → fsync 目录 → 版本清理（见 7.11）。它输出 `published, pending worker activation`，**不会**自行 bump
generation，也**不**声称文件系统发布与数据库激活是同一个事务。

`--course` 对本命令是必填；多课程部署请始终显式指定。

退出码：本命令**原样转发**输入预检的退出码，不重新编号。`0` 表示指定的内容都已发布（也包含 `--enable`/`--disable` 与无需清理的 `--prune`）；`1` 表示被拒绝（候选校验失败、术语表预检返回 1、写入失败、`--add` 发现课程已存在、课程无法解析、版本清理失败），且没有切换任何内容；`2` 既可能是用法错误，也可能是题库预检返回 2（复用退役 ID，或 `--published` 与显式路径冲突）；`4` 表示题库预检无法安全比对（未知课程、数据库未迁移）。`3` 只会由题库预检在 `--strict` 下返回，而本命令不传 `--strict`，因此正常流程不会出现（保留在契约里是因为转发的码不会被改写）。`python scripts/publish_course.py --help` 会打印同一张表。

回滚走正常流程：把旧内容当作新候选再发布一次（`check_question_bank.py` + `publish_course.py --questions`），
**不允许**手工 `generation--`。如果只是回退上一版，可以直接发布保留的上一版副本，无需另存候选文件：

```bash
python scripts/publish_course.py --course physical_design \
    --questions courses/physical_design/versions/<previous-sha256>/questions.json
```

### 7.8 术语表校验

```bash
python scripts/check_glossary.py --course physical_design
python scripts/check_glossary.py --all
python scripts/check_glossary.py --course physical_design --published          # 只校验已发布文件
python scripts/check_glossary.py --questions path/questions.json --glossary path/glossary.json  # 显式离线
```

单课程模式下，语料与术语表同样优先使用该课程的 `questions_candidate.json` / `glossary_candidate.json`，报告里会
打印实际读取的两个文件与来源（`candidate` / `published`）。校验只读取内容，不修改 registry、generation 或任何学
习数据。报告里出现 `Retired term fields` 表示词条还留着已退役的 `definition`（英文定义）：Loader 会忽略它，
命令仍然成功，但请先删掉这些键再发布。校验通过后再发布：

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
4. **重新执行 publish course**：只有校验退出码为 `0` 才发布（`publish_course.py` 会在写入前内部重跑对应的 `check_*.py` 并拒绝未通过的内容；只有明确加 `--skip-preflight` 才跳过，且不推荐）。发布是纯文件系统切换：单个内容类型用一次 `os.replace` 切换 manifest；一条命令同时传 `--questions` 与 `--glossary` 时两者**顺序执行、各自独立**，不是跨文件事务（见 7.3 节）。末尾会再做一次版本清理（见 7.11），最后统一重启全部 worker，并用 `/ready/<course_id>` 确认（术语表不推进 generation，`/ready` 可能仍是 200，因此术语表是否生效必须靠浏览器验收确认）。

校验未通过时不得发布：schema 校验失败或校验脚本返回非零时，发布命令不会替换、不会创建任何文件，也不会推进 generation。

> **新增一门课是这张表的例外顺序。** 上表默认课程已经存在；而 `check_courses.py` 只报告已声明（已有
> `course.json`）的课程，`check_question_bank.py --course <course_id>` / `check_glossary.py --course <course_id>` 在
> 课程创建前都会报 `Unknown course`（没有 manifest 的目录被 loader 忽略）。新课程的顺序是
> `publish_course.py --add`（自带题库 schema 与术语表门禁）→ 再用 `--course` 补跑上表的只读校验，完整步骤见 7.2 节。

### 7.11 版本保留与回退（自动维护最新两版）

`versions/<sha256>/` 是内容寻址的不可变副本，所以每次发布都会留下上一份。`publish_course.py` 在**每次**成功发布
（含 `--add`）之后都会自动做一次版本清理，规则固定且保守：

* **按内容类型分别计数**：`questions.json` 与 `glossary.json` 各保留“当前版本（manifest 指向的那份）+ 上一版”。
  因此只发布术语表不会挤掉上一版题库，反之亦然；一门课最多留 4 个 digest 目录（常见情况是 2–3 个）。
* **manifest 指向的版本永不删除**：即使 `--keep-versions 1` 也只清理历史副本。
* **只删已知文件**：仅删除 `versions/<sha256>/questions.json` 或 `versions/<sha256>/glossary.json`；目录里还有
  别的文件时整目录保留（内容删空后的空目录才会移除），`versions/` 之外的文件一律不动。
* **排序依据是文件修改时间**：内容寻址目录本身不记历史，而回退只是把 manifest 指回一个已存在的目录（不重写文件），
  所以“manifest 引用”优先于 mtime。

```bash
python scripts/publish_course.py --course physical_design --prune              # 只清理，不发布内容
python scripts/publish_course.py --course physical_design --prune --keep-versions 3
python scripts/publish_course.py --course physical_design --glossary g.json --keep-versions 1  # 只留当前版本
python scripts/publish_course.py --course physical_design --questions q.json --no-prune        # 本次跳过清理
# --add 之后会自动清理，因此 --add 与 --prune 不能同时使用；--keep-versions 0 是用法错误（退出码 2）
```

回退一步（不需要手工改 manifest，也不需要手工删文件；归档字节相同，所以不会产生新目录）：

```bash
# 1) 看有哪些版本（上一版 = 除当前 manifest 指向之外最新的一版）
ls courses/physical_design/versions
# 2) 把上一版当作候选重新发布
python scripts/publish_course.py --course physical_design \
    --glossary courses/physical_design/versions/<previous-sha256>/glossary.json
# 3) 统一重启 worker，确认 /ready/physical_design 与页面内容
```

清理只发生在**发布之后**，所以它失败不会让一次已经生效的发布变成失败：命令会打印告警（退出码仍为 `0`），随时可以
用 `--prune` 重试。plain-file 布局（没有 manifest）根本没有 `versions/`，这类课程运行清理只会打印“无需清理”。


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
| `glossary.json` | 是 | 不变（不推进 generation、不围栏 worker；但已运行的 worker 仍用旧术语，需要重启才生效） |
| 一次成功发布（任意内容） | `versions/` 每个内容类型保留当前 + 上一版，更早的副本被清理（见 7.11） | 不变 |

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
* legacy migration 保留 namespace 与历史，但无法推断不可知的旧数据课程归属；持久化的 `legacy_course_id`
  在普通运行时只读，只能通过 `scripts/rename_course.py` 显式迁移（见 7.6）。
* 旧版应用无法在迁移后的 Schema 上安全运行（见
  [`MULTI_COURSE_MIGRATION.md`](MULTI_COURSE_MIGRATION.md)）。
* 内容发布（文件系统）与数据库激活（worker 启动时的 reconciliation）**不是同一个事务**：`publish_course.py`
  只报告 `published, pending worker activation`，从不会自行推进 generation。
* 没有“跨课程的联合事务”：题库与术语表、多个课程之间都是各自独立的提交；遇到部分失败请按上面的
  失败语义逐项收尾。

