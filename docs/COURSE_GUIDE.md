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

每门课一个 manifest：

```text
courses/
├── digital_ic/
│   ├── course.json
│   ├── questions.json
│   └── glossary.json
└── physical_design/
    ├── course.json
    ├── questions.json
    └── glossary.json
```

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

### 7.1 校验课程目录

```bash
python scripts/check_courses.py            # 人类可读报告
python scripts/check_courses.py --json     # 机器可读
```

全局歧义（重复 `course_id`、manifest 非法）退出码 2；单门课程加载失败退出码 1，但其他课程仍会报告。

### 7.2 新增课程

```bash
python scripts/publish_course.py --course physical_design \
  --title "Physical Design" --title-zh "物理设计" --order 20 \
  --questions questions.json --glossary glossary.json --add
```

写入 `courses/physical_design/`（manifest + 内容）后提示 `created, pending worker activation`。
重启 worker 后用 `/ready/physical_design` 验证。

### 7.3 停用 / 重新启用

```bash
python scripts/publish_course.py --course physical_design --disable
python scripts/publish_course.py --course physical_design --enable
```

以 manifest 的 `enabled` 为准；学习数据不会被修改。重启 worker 后生效。

### 7.4 重命名一门课程（namespace 迁移）

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

### 7.5 发布某门课的题库

```bash
# 1) 只读预检（同一份数据库快照，只读打开）
python scripts/check_question_bank.py --course physical_design candidate.json --db instance/mcq.db

# 2) 原子发布
python scripts/swap_question_bank.py --course physical_design candidate.json --db instance/mcq.db

# 3) 统一重启 worker，并确认
curl -i http://127.0.0.1:8001/ready/physical_design
```

`swap_question_bank.py` 的顺序是：**一次性**读取候选文件字节 → 用同一份字节校验 → 只读预检 → 写入不可变
的 `versions/<sha256>/questions.json` → 在课程发布锁内**重新验证** baseline → 单次 `os.replace` 原子切换
manifest → fsync 目录。它输出 `published, pending worker activation`，**不会**自行 bump generation，也
**不**声称文件系统发布与数据库激活是同一个事务。

`--course` 在只声明了一门启用课程时可以省略；多课程部署请始终显式指定。

回滚走正常流程：把旧内容当作新候选再发布一次（`check_question_bank.py` + `swap_question_bank.py`），
**不允许**手工 `generation--`。

### 7.6 术语表校验

```bash
python scripts/check_glossary.py --course physical_design
python scripts/check_glossary.py --all
python scripts/check_glossary.py --questions path/questions.json --glossary path/glossary.json  # 显式离线
```

校验只读取内容，不修改 registry、generation 或任何学习数据。校验通过后再发布：

```bash
python scripts/publish_course.py --course physical_design --glossary new_glossary.json --run-preflight
```

### 7.7 故障定位

```bash
curl -s http://127.0.0.1:8001/ready | python -m json.tool
curl -i http://127.0.0.1:8001/ready/physical_design
journalctl -u mcq-template.service -n 100 --no-pager | grep -E 'is stale|is unavailable'
```

* `stale`：还有 worker 使用旧内容 → 统一重启；只有该课程的页面被围栏。
* `unavailable`：该课程内容损坏 → 查看日志中的具体原因，修复后用 `check_courses.py` 复核。
* `undeployed`：数据库有该课程身份，但本 worker 的课程目录未声明它 → 检查部署内容是否齐全。

### 7.8 内容变更统一流程（先校验，后发布）

脚本命名统一为 `check_<校验对象>.py`；每一类课程内容都有一个只读校验脚本，发布命令只切换已通过校验的内容：

| 变更对象 | 校验脚本（只读） | 发布命令 |
| --- | --- | --- |
| 课程目录 / manifest / 整门课能否加载 | `check_courses.py` | `publish_course.py --add` / `--enable` / `--disable` |
| `questions.json` | `check_question_bank.py --course <course_id> candidate.json --db instance/mcq.db` | `swap_question_bank.py --course <course_id> candidate.json --db instance/mcq.db`（或 `publish_course.py --questions … --run-preflight`） |
| `glossary.json` | `check_glossary.py --course <course_id>` | `publish_course.py --course <course_id> --glossary new_glossary.json --run-preflight` |

固定流程（缺一不可）：

1. **准备校验脚本**：确认该内容种类已有 `check_<校验对象>.py`；若还没有，先补齐脚本与测试。
2. **修改内容**：只编辑候选文件，不要原地覆盖正在使用的 `questions.json` / `glossary.json`。
3. **运行校验**：`check_courses.py` 必须始终通过；题库再跑 `check_question_bank.py`，术语表再跑 `check_glossary.py`。
4. **重新执行 publish course**：只有校验退出码为 `0` 才发布（`swap_question_bank.py` 与 `publish_course.py --run-preflight` 会在写入前内部重跑校验并拒绝未通过的内容）。发布是纯文件系统切换，最后统一重启全部 worker，并用 `/ready/<course_id>` 确认。

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

