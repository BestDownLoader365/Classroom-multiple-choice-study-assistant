# 多课程迁移与回滚手册（Multi-Course Migration）

本文说明如何把旧版单课程数据库迁移到多课程 Schema、如何验证、如何在出错时回滚，以及为什么旧版应用
不能在迁移后的数据库上运行。

所有命令与行为以当前代码为准（`app/repositories/schema_migrations.py`、
`app/repositories/course_repository.py`、`scripts/migrate_courses.py`）。

---

## 1. 迁移做了什么

`Database.initialize()`（以及 `scripts/migrate_courses.py`）会调用 `ensure_schema()`，在**独立连接**上
执行一次事务：

```text
PRAGMA foreign_keys = OFF        （事务之前；不能在事务内修改）
BEGIN IMMEDIATE
  在拿到写锁之后重新读取 schema_version
  建立新表
  复制所有旧行
  字段级校验（行数一致、course_id 非空、父子关系完整）
  用新表替换旧表
  建立索引
  写入 schema_version 与持久化的 legacy_course_id
COMMIT
PRAGMA foreign_key_check          （提交后做完整父子检查）
```

任何失败都会整体回滚；重复执行总能得到同一个结果。

### Schema 变化

| 表 | 变化 |
| --- | --- |
| `courses`（新） | 永久课程身份：`course_id` PK、`title`、`title_zh`、`enabled`、`sort_order`、时间戳 |
| `schema_meta`（新） | `schema_version` 与持久化的 `legacy_course_id`；另可存导航偏好 `default_course_id` |
| `quiz_progress` | PK 由 `(learner_id, mode)` 变为 `(learner_id, course_id, mode)`；state JSON / NULL tombstone / `bank_version` 原样迁移 |
| `attempts` | 新增必填 `course_id`；原自增 ID、时间戳、所选答案 JSON、mode、sequence 全部保留 |
| `wrong_questions` | PK 变为 `(learner_id, course_id, question_id)`；SRS / corrected / count / 时间戳原样复制 |
| `weak_knowledge_points` | PK 变为 `(learner_id, course_id, chapter_id)`；verification JSON / active / 时间戳原样复制 |
| `exam_sessions` | 新增必填 `course_id`；旧 session 全部归入 legacy course |
| `exam_questions` | 保持 PK `(exam_id, position)`，**不重复存** `course_id`；课程 namespace 通过父 `exam_sessions.course_id` 取得；新增 `exam_questions -> exam_sessions` 外键（restrictive delete） |
| `question_registry` | PK 变为 `(course_id, question_id)`；active / retired / 无身份 tombstone 全部原样迁移 |
| `question_bank_state` | 去掉 singleton（原 `id = 1`），改为 `course_id` PRIMARY KEY；旧 singleton 值原样迁入 legacy course |
| `users` / `auth_rate_limits` | **不变**，仍为全站 |

外键策略：`course_id` 指向 `courses`，`exam_questions.exam_id` 指向 `exam_sessions`，均使用 restrictive
delete。**不**给 `attempts -> live question` 或 `weak -> live chapter` 建外键 —— 历史记录必须允许对应内容
已被删除。

索引（`schema_migrations.INDEX_STATEMENTS`）：

```text
attempts                (learner_id, course_id, question_id, answered_at, id)
attempts                (course_id, answered_at, id)
wrong_questions         (learner_id, course_id, mastered, next_review_at)
weak_knowledge_points   (learner_id, course_id, active)
exam_sessions           (learner_id, course_id, created_at)
exam_sessions           (course_id, status, deadline_at)
```

保留窗口变为"每个 `(learner_id, course_id, question_id)` 最近 10 次"，仍跨 Normal / Review / Exam 共享。
retention 是**独立于迁移**的一步（`Database.enforce_attempt_retention()`），命名空间迁移本身不做清理。

### 迁移**不会**做的事

迁移不得：retention 清理、weak backfill、题库 diff、registry reconciliation、内容清理、重算 fingerprint、
重写成绩。命名空间迁移与题库 reconciliation 是两个独立步骤。

---

## 2. 迁移步骤

### 2.1 备份

```bash
# 手动备份（最稳妥：先停服务，再复制）
systemctl stop mcq-template.service
cp -a instance/mcq.db instance/mcq.db.manual-$(date -u +%Y%m%dT%H%M%SZ)
systemctl start mcq-template.service
```

`scripts/migrate_courses.py` 在没有 `--no-backup` 时会自动生成一份带时间戳的备份副本。

### 2.2 只读检查

```bash
python scripts/migrate_courses.py --db instance/mcq.db --dry-run
```

输出表清单、`schema_version`、`legacy_course_id`、是否需要迁移、每表行数，以及孤儿
`exam_questions` 行数。**只读**，不写任何内容。

### 2.3 执行迁移

```bash
python scripts/migrate_courses.py --db instance/mcq.db
```

输出 `schema_version`、持久化的 `legacy_course_id`、本次是否真的执行了迁移，以及已登记的课程。

如果要把旧的根目录文件也变成 manifest 布局：

```bash
python scripts/migrate_courses.py --db instance/mcq.db --layout
```

它把根 `questions.json`/`glossary.json` **复制**到 `courses/legacy/`（不移动，便于回滚），并写入 manifest。
复制完成后必须删除根文件：两者同时存在会被判定为重复 `course_id`，导致应用无法启动。

### 2.4 验证

```bash
python scripts/migrate_courses.py --db instance/mcq.db --dry-run       # 应显示"不需要迁移"
python scripts/check_courses.py                                       # 课程目录校验
python scripts/check_question_bank.py --course legacy --db instance/mcq.db
python scripts/check_glossary.py --course legacy                      # 术语表校验（声明了术语表时）
systemctl start mcq-template.service
curl -i http://127.0.0.1:8001/ready
curl -i http://127.0.0.1:8001/ready/legacy
```

`tests/test_course_migration.py` 用一份手工构造的旧版数据库做**字段级**比对（attempt ID、answers、时间、
mode、sequence、SRS、weak JSON、progress JSON、NULL tombstone、`bank_version`、exam ID/status/score/
deadline/questions/answers/fingerprint、registry 时间戳/retired/tombstone、generation、全部 fingerprint），
并对 `after_create` / `after_copy` / `before_replace` / `before_version` / `before_commit` 五个阶段做故障
注入，验证回滚后重新执行结果一致。

### 2.5 父子约束冲突

迁移**不会**静默删除数据。如果旧库里已经存在违反新父子约束的行（例如 `exam_questions` 指向不存在的
`exam_sessions`），迁移会带着这些行的清单中止：

```text
Cannot migrate: exam_questions rows reference missing exam sessions (...).
No data was changed; repair or remove those rows deliberately, then retry.
```

处理方式：人工确认后删除或修复这些行，再重新执行迁移。绝不使用 `INSERT OR REPLACE` 掩盖冲突。

---

## 3. 回滚

Schema 迁移本身**没有**原地回滚：旧版应用无法在新的 Schema 上安全运行（见第 4 节）。因此回滚 =
恢复备份 + 回滚应用版本：

```bash
systemctl stop mcq-template.service
cp -a instance/mcq.db.bak-<timestamp> instance/mcq.db     # 或你的手动备份
git checkout <旧版本标签>
python -m pip install -r requirements.txt                # 若依赖有变化
systemctl start mcq-template.service
curl -i http://127.0.0.1:8001/ready
```

**内容回滚**（只回退某门课的题库/术语表）走正常发布流程，不要手工改 generation：

```bash
python scripts/check_question_bank.py --course physical_design old_questions.json --db instance/mcq.db
python scripts/publish_course.py --course physical_design --questions old_questions.json --db instance/mcq.db
# 统一重启 worker
```

---

## 4. 兼容性限制

* **旧版应用不能在迁移后的 Schema 上运行。** 旧代码按 `question_registry(question_id)` 与
  `question_bank_state(id = 1)` 读取，迁移后这些列不存在；`attempts` 等表新增的 `course_id` 是 `NOT NULL`
  且带外键。混用会导致 `no such column: id` / `NOT NULL constraint failed` 一类错误。
* 迁移保留 namespace 与历史，但**无法推断**不可知的旧数据课程归属：旧数据全部归入第一次迁移时确定的
  `LEGACY_COURSE_ID`，之后修改它不会重新归属。
* 迁移**不会**重算历史 fingerprint，也**不会**重写成绩。
* 未迁移的数据库只能映射到 legacy 课程：`scripts/check_question_bank.py --course <非 legacy>` 会拒绝执行
  （退出码 4），因为把任意新课程候选与全库历史比较会给出误导性的结论。先迁移，再逐课程检查。
* 历史考试不会获得完整的历史题目内容快照；报告仍基于当前 live 内容。
* 聚合 `/ready` 的失败只表示"至少一门课程在本 worker 上不可服务"，**不**表示健康课程的路由也会失败。

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `no such column: course_id` | 数据库未迁移 | `python scripts/migrate_courses.py --db instance/mcq.db` |
| `Duplicate course_id "legacy"` | 根 `questions.json` 与 `courses/legacy/course.json` 同时存在 | 删除其中一份（`--layout` 迁移会提示） |
| 某课程 `/ready/<id>` 返回 `unavailable` | 该课程内容损坏（题目/术语表校验失败） | 查看日志原因，修复后用 `check_courses.py` 复核 |
| 某课程 `/ready/<id>` 返回 `stale` | 仍有 worker 使用旧内容 | 统一重启 worker |
| `/courses` 显示 `undeployed` 的课程 | 数据库有身份但本 worker 未声明 | 检查部署内容是否齐全；这不影响就绪度 |
| 迁移被拒绝并列出 exam_id | 旧库有孤儿 `exam_questions` | 人工确认后修复这些行，再重试 |
| 想给 legacy 课程换一个真实的课程 ID | `course_id` 就是 namespace | `python scripts/rename_course.py --db instance/mcq.db --from legacy --to <course_id> --courses-dir courses --rename-directory`（自动备份、单事务、前后计数校验）。它会一起更新 `legacy_course_id`，所以重启后不会再出现空的 `legacy` 课程 |
| 迁移后 `/courses` 出现一个空的 `legacy`（undeployed） | 该行是持久化的 `legacy_course_id` 命名空间占位身份，由启动逻辑维护（缺失即重建） | 用 `rename_course.py --rename-directory` 把历史改挂到真实课程 ID；不要手工删除这一行，它会被重建。真正需要删除一门 manifest 课程时用 `python scripts/delete_course.py --course <course_id> --dry-run` 预览后删除 |
| 想彻底删除一门课程（目录 + 数据库引用） | 手工 `rm -rf courses/<id>` 会留下 `courses` 身份、学习数据、`question_bank_state`、`question_registry` 退役记录与 `default_course_id` 偏好 | `python scripts/delete_course.py --course <course_id> --dry-run` 先预览，确认后去掉 `--dry-run`（默认备份数据库；仍有学习数据时需 `--force`） |

