# `glossary.json` 专业术语库编写指南（GLOSSARY_GUIDE）

本文面向创建、审核和维护课程术语库的用户。目标是写出一份能被当前应用直接加载的 `glossary.json`，让术语学习页、题目与错题中的术语高亮，以及中文释义弹层自动适配任意课程。

本文描述当前项目实际支持的契约。最终校验逻辑以 [`app/repositories/glossary_loader.py`](../app/repositories/glossary_loader.py) 为准；题库本身的写法见 [`questions.json` 题库编写指南](QUESTION_GUIDE.md)。

## 1. 文件用途与放置位置

`glossary.json` 属于**某门课程**。它可能是下面五种情形之一，注意区分：

| 路径 / 取值 | 角色 | 谁读它 |
| --- | --- | --- |
| `courses/<course_id>/glossary_candidate.json` | **candidate**：维护者编辑、`check_glossary.py` 与 `publish_course.py --glossary` 的默认输入 | 只读校验脚本与发布命令（不被 worker 加载） |
| `courses/<course_id>/course.json` 的 `glossary` 字段 | **published content pointer**：唯一已发布指针，指向下面两种之一；写 `null` 表示本课程明确没有术语表 | 应用启动时的 `CourseLoader` |
| `courses/<course_id>/versions/<sha256>/glossary.json` | **immutable version**：`--add` 与每次发布写入的不可变副本（推荐布局） | 通过 manifest 指针间接读取 |
| `courses/<course_id>/glossary.json` | **plain-file 兼容布局**：老课程或手工创建的课程，manifest 直接指向它 | 通过 manifest 指针间接读取 |
| 根目录 `glossary.json` | **legacy root adapter**：没有 manifest 的兼容入口，作为 `course_id = legacy` 的虚拟课程术语表加载 | 应用启动时的 legacy adapter |

```text
courses/<course_id>/
├── course.json      # "glossary": "versions/<sha256>/glossary.json"，或 "glossary": null
├── glossary_candidate.json
└── versions/<sha256>/glossary.json
```

应用启动时读取并完整校验它，修改后需要重启开发服务器或生产 worker 才会生效。manifest 声明了 glossary 但文件缺失/损坏时，该课程会被标记为 `unavailable`（不会假装“没有术语表”）；`glossary: null` 才是明确的“本课程没有术语表”。

两个 JSON 文件职责不同：

- `questions.json` 保存题目、选项、答案、解析和课程目录，**包括中文字段** `text_zh`、`options[].text_zh`、`explanation_zh`；
- `glossary.json` 保存专业词汇：规范词条 `term`、别名 `aliases`、中文译名 `term_zh`、中文释义 `definition_zh` 和分类 `category`；
- 术语表**只存在于当前课程的内存中**（`CourseServices.glossary_repository`），**不写入 SQLite**，也**不参与任何题库指纹或 generation**（`bank_version` 与 grading / content / placement / catalogue 指纹都不包含它）；
- 术语 ID 是**课程内本地身份**（`(course_id, term_id)`），不是任何数据库注册表的主键：题面高亮只用当前课程的术语，课程之间不会互相注入、也不会共用别名；
- 只修改/发布 `glossary.json` 不会清空账号、答题记录、错题状态或练习进度，也**不会**推进该课程的 generation、**不会**让 worker 变 stale：`/ready` 与 `/ready/<course_id>` 仍可能是 200，学习页面不会因此返回 503。

### 1.1 发布术语表之后仍然要重启 worker

术语表只在**进程启动时**读入内存，所以上面那条“不影响 generation”不等于“无需重启”：

* 已经运行的 worker 会继续使用**旧的内存术语表**，直到它被重启；页面上的高亮、释义新内容都不会出现；
* 因为 generation 没有变化，`/ready` 与 `/ready/<course_id>` 很可能**继续返回 200**——readiness 只证明课程可以服务，**不能**用来证明术语表已经重新加载；
* 因此术语表发布的收尾动作依然是“统一重启全部 worker”，再用浏览器确认新术语生效（见第 10 节）。判断“是否需要重启”请以内容是否已发布为准，而不是以 `/ready` 的状态为准。

## 2. 可直接使用的完整示例

当前术语库使用 `schema_version: 1`。下面的统计学示例不依赖部署中的任意一门课程，可以直接通过 Loader 校验（把它保存为某门课的 candidate 文件，或直接传给 Loader 的路径参数）。

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
      "definition_zh": "衡量数据相对于均值离散程度的统计量。",
      "category": "Descriptive Statistics"
    },
    {
      "id": "null-hypothesis",
      "term": "Null Hypothesis",
      "term_zh": "原假设",
      "aliases": ["H0"],
      "definition_zh": "统计检验中等待数据证据判断是否拒绝的假设。",
      "category": "Hypothesis Testing"
    }
  ]
}
```

标准 JSON 不允许注释、尾随逗号、单引号字符串或未转义的换行符。

## 3. 根对象字段

| 字段 | 要求 | 类型 | 当前规则与显示位置 |
|---|---:|---|---|
| `schema_version` | 必填 | 整数 | 使用 JSON 整数 `1`；布尔值和字符串 `"1"` 不合法。 |
| `title` | 必填 | 非空字符串 | 英文或主要标题，显示在专业词汇页。 |
| `title_zh` | 必填 | 非空字符串 | 中文标题，显示在词汇页标题和首页入口。 |
| `description` | 可选 | 非空字符串 | 英文说明；只有没有中文说明时才作为词汇页简介显示。 |
| `description_zh` | 可选 | 非空字符串 | 中文说明，优先显示在词汇页。 |
| `terms` | 必填 | 数组 | 术语对象列表。Loader 允许空数组，但可用课程应至少包含一个术语。 |

可选字符串一旦出现就不能是空字符串或纯空白；不需要时应直接省略字段。

分类不在根对象中维护。页面会从 `terms[].category` 动态提取，并按分类在数组中第一次出现的顺序展示。

## 4. `terms` 中的术语对象

| 字段 | 要求 | 类型 | 当前规则与用途 |
|---|---:|---|---|
| `id` | 必填 | 非空字符串 | 在整份术语库中唯一，供程序稳定定位词条。 |
| `term` | 必填 | 非空字符串 | 规范英文术语，也是词汇卡片主标题。 |
| `term_zh` | 必填 | 非空字符串 | 中文名称，供主动回忆和释义弹层使用。 |
| `aliases` | 可选 | 字符串数组 | 缩写、全称变体或常见写法；省略时等同于 `[]`。每项必须非空。 |
| `definition_zh` | 可选 | 非空字符串 | 中文释义；术语页“显示中文”与高亮弹层显示的就是它。 |
| `category` | 可选 | 非空字符串 | 任意课程自己的分类名称；省略后卡片显示 `General`。 |

术语只需要**中文释义**：英文定义字段 `definition` 已经退役，词条不再提供英文解释。Loader 会**忽略**遗留的 `definition` 键（这样一份更早发布的术语表仍然可以回滚加载），但 `check_glossary.py` 会在报告里列出它（见 9.3、11 节），请从文件里删除。

建议为 ID 使用稳定的 kebab-case，例如 `standard-deviation`、`setup-time`。当前 Loader 只要求它是唯一的非空字符串，并不强制某种字符格式。修改翻译或释义时应保留原 ID；删除词条后也不要把其 ID 分配给含义无关的新词条。

## 5. `term` 与 `aliases` 应该怎么写

### 5.1 一个概念只建立一个规范词条

把最完整、最适合展示的名称写入 `term`，把其他写法放入 `aliases`：

```json
{
  "id": "electronic-design-automation",
  "term": "Electronic Design Automation",
  "term_zh": "电子设计自动化",
  "aliases": ["EDA"],
  "category": "Design Methodology"
}
```

不要再为 `EDA` 建立第二个词条。点击别名时，界面会打开同一个规范词条的释义。

### 5.2 明确列出文本中真实存在的变体

匹配是大小写不敏感的字面匹配，不会自动做词干提取、复数变化、连字符转换或同义词推断。例如：

- `standard cell` 可以匹配 `Standard Cell`，无需重复大小写变体；
- `via` 不会自动匹配 `vias`，需要时应把 `vias` 加入 aliases；
- `low-k` 不会自动匹配 `low k`；
- `clock tree synthesis` 不会自动识别为 `CTS`；
- `H0`、`Cu/low-k`、`AR(1)` 等带数字或标点的写法可以直接作为 alias。

只收录课程内容中确实使用、且不会产生歧义的写法。不要为了“可能有人这样说”加入过短或语义宽泛的 alias，例如单个普通字母。

### 5.3 边界和最长匹配规则

浏览器先尝试较长的术语，因此同时存在 `cell` 和 `standard cell` 时，文本中的 `standard cell` 会优先作为完整短语匹配。

如果术语首尾是字母、数字、组合标记或下划线，匹配器会检查 Unicode 单词边界。因此 `die` 不会误匹配 `dielectric`，`net` 不会误匹配 `network`。标点会按 JSON 中的原样进行字面匹配。

## 6. 唯一性与冲突规则

Loader 会对每个 canonical term 和 alias 执行 Unicode NFKC 规范化、大小写折叠、首尾空白清理和连续空白合并，然后检查冲突。以下内容会被拒绝：

- 重复的 `id`；
- 两个词条使用标准化后相同的 `term`；
- 同一词条的 `term` 和 alias 标准化后相同；
- 同一词条包含重复 aliases；
- 一个词条的 alias 与另一个词条的 term 或 alias 冲突。

例如 `"EDA"`、`"eda"` 和 `"  EDA  "` 会被视为同一个标签。出现冲突时，应先判断它们是否属于同一概念：属于同一概念就合并为一个词条；含义不同则不要使用这个歧义 alias，改用课程文本中更明确的写法。

## 7. 翻译、释义与分类建议

- `term_zh` 应优先使用课程教材或行业通行译法，必要时保留英文缩写。
- `definition_zh` 用一到三句话解释“它是什么、在本课程中做什么”，不要只重复中文名称。
- 只写中文释义：英文定义字段 `definition` 已退役，词汇卡片与释义弹层都不再展示英文解释。
- 同一概念在不同语境有不同含义时，释义应限定当前课程语境。
- 分类应使用稳定、粒度一致的名称，例如 `Descriptive Statistics`、`Hypothesis Testing`，不要把近义类别拆成大小写或单复数不同的多组。
- 术语数组的顺序就是词汇卡片的显示顺序；建议按课程顺序或分类后概念顺序排列。

## 8. 哪些课程文本会参与覆盖校验

术语高亮只发生在模板明确标记的学习内容中。目前包括题干、选项、答案反馈、解析和错题内容。

`scripts/check_glossary.py` 为了检查术语覆盖面，会读取**同一门课程**的 `questions.json` 中面向学习者的英文文本：

- `sources[].title`；
- `chapters[].title`；
- `questions[].section`；
- `questions[].text`；
- `questions[].options[].text`；
- `questions[].explanation`，并忽略末尾旧式 `Source: ...` 引用。

ID、文件名、页码、中文翻译和答案 ID 不属于覆盖语料。校验范围和页面高亮范围用途不同，因此某个出现在 source/chapter 标题里的词条可以通过覆盖校验，但不一定会在普通题目页出现高亮。

## 9. 校验命令

以下命令均在项目根目录执行，`<course_id>` 用课程 manifest 里的身份（例如 `physical_design`）；只声明了一门启用课程时可以省略 `--course`。

### 9.1 检查 JSON 语法

先检查候选文件（`courses/<course_id>/glossary_candidate.json`）；如果你要检查别的文件（manifest 指向的已发布文件或某个 `versions/<sha256>/glossary.json`），把路径换成它即可。

Linux、macOS 或 WSL：

```bash
python -m json.tool courses/<course_id>/glossary_candidate.json > /dev/null
```

PowerShell：

```powershell
python -m json.tool courses/<course_id>/glossary_candidate.json | Out-Null
```

### 9.2 使用当前 Loader 校验完整契约

```bash
python -c "from pathlib import Path; from app.repositories import GlossaryLoader; g = GlossaryLoader(Path('courses/<course_id>/glossary_candidate.json')).load(); print(f'OK: {len(g.terms)} terms, {sum(len(t.aliases) for t in g.terms)} aliases')"
```

也可以直接让课程目录校验把每门课的题目数与术语数一次报出来：

```bash
python scripts/check_courses.py
```

只有看到 `OK`，才表示字段类型、必填值、ID 和标签冲突都通过了当前代码校验。

### 9.3 术语表覆盖校验

校验单门课程（用该课程自己的题库做语料）：

```bash
python scripts/check_glossary.py --course <course_id>
```

课程目录里存在 `questions_candidate.json` / `glossary_candidate.json` 时，它们就是默认校验对象（报告会打印实
际读取的两个文件与来源）；只想复核已发布内容时加 `--published`：

```bash
python scripts/check_glossary.py --course <course_id> --published
```

校验全部启用课程：

```bash
python scripts/check_glossary.py --all
```

显式离线检查任意两个文件（不经过课程目录）：

```bash
python scripts/check_glossary.py \
  --questions path/to/questions.json \
  --glossary path/to/glossary.json
```

新增课程时，`--course <course_id>` 在 `courses/<course_id>/course.json` 存在之前无法解析（脚本会报 `Unknown course`，
因为 loader 只认有 manifest 的课程目录）：先用 `python scripts/publish_course.py --course <course_id> --add ...` 创建
课程（它会先校验候选并归档到 `versions/<sha256>/`），再运行上面的按课程校验；尚未创建课程时就用上面这段显式离线模式先校验候选文件。

校验只读取内容，不会修改 `question_registry`、generation 或任何学习数据。

输出含义：

- `Validated ...`：术语 JSON 已通过 Loader；
- `Retired term fields`：词条里还留着已退役的 `definition`（英文定义）。Loader 会忽略它，命令也仍然成功，但请删除这些键；
- `Orphan entries`：该词条的 term 和 aliases 都没有在校验语料中出现，可能是多余词条、缺少 alias，或题库还未覆盖该概念；
- `Possible uncovered glossary candidates`：脚本从大写缩写、括号缩写和连字符 token 中找出的人工复核候选；它不是“必须加入”的错误清单。

校验不会修改任何 JSON，也不会自动生成翻译。只要文件能读取且 schema 合法，即使报告 orphan 或候选，命令也会以成功状态结束；是否增删词条必须由课程内容审核决定。校验通过后再发布（见第 12 节）。

### 9.4 运行测试

```bash
pytest -q tests/test_bundled_glossary.py
pytest -q
```

## 10. 浏览器验收

重启应用并登录后，至少检查：

1. 首页“专业词汇”入口显示新术语库的中文标题。
2. `/course/<course_id>/glossary` 显示正确标题、简介、术语数量和动态分类数量。
3. 搜索可以匹配英文 term、alias、中文释义和分类。
4. 选择分类后卡片立即筛选，无需提交按钮；选择“全部分类”可恢复全部词条。
5. “显示中文 / 隐藏中文”按钮能显示中文术语与中文释义；卡片与弹层里都不再出现英文定义。
6. `/course/<course_id>/`、`/course/<course_id>/quiz`、反馈和 `/course/<course_id>/mistakes` 中的 term/alias 被正确高亮，点击、Enter 和 Space 均可打开释义。
7. 短词没有误命中较长单词，重叠短语由更长术语优先匹配。
8. 手机宽度下词汇卡片、分类下拉框和弹层可正常阅读。
9. 切到另一门课程后只看到那门课程的术语（课程没有术语表时无高亮、空状态明确）。

## 11. 常见错误

| 报错或现象 | 原因 | 修正方式 |
|---|---|---|
| `Glossary root must be a JSON object` | 根节点写成了数组 | 使用包含标题和 `terms` 的根对象。 |
| `schema_version must be ... 1` | 版本缺失、类型错误或不是 `1` | 写入整数 `"schema_version": 1`。 |
| `field ... must be a non-empty string` | 必填字段缺失、不是字符串或只有空白 | 补充有效字符串。 |
| `optional field ... must be a non-empty string when provided` | 可选字段被写成 `null` 或空字符串 | 填写内容，或直接删除该字段。 |
| `terms must be an array` | `terms` 缺失或写成对象 | 改为 JSON 数组。 |
| `aliases must be an array` | alias 被写成单个字符串 | 即使只有一个 alias，也写成 `["SD"]`。 |
| `duplicate field "id"` | 两个词条 ID 相同 | 为每个概念分配唯一稳定 ID。 |
| `duplicates another term or alias after normalization` | 同一词条内 term/alias 重复 | 删除重复标签。 |
| `collides with glossary term ... after normalization` | 标签已由另一词条占用 | 合并同义词条，或删除/改写歧义 alias。 |
| 报告出现 `Retired term fields ... "definition"` | 词条还带着已退役的英文定义字段；Loader 会忽略它，所以不会报错 | 删除每个词条的 `definition` 键，再按第 12 节重新校验并发布。 |
| 术语页有词条但正文没有高亮 | 正文拼写不是 term/alias 的字面形式，或正文不属于高亮区域 | 添加准确 alias，并确认目标是题干、选项、反馈、解析或错题内容。 |
| 修改后页面仍是旧内容 | 应用只在启动时加载文件 | 重启开发进程或生产 worker。 |

## 12. 新增/更换课程的推荐流程

1. 按 [`QUESTION_GUIDE.md`](QUESTION_GUIDE.md) 准备并校验新题库。
2. 从题干、选项、解析和课程目录收集需要学习的专业概念。
3. 为每个概念确定 canonical term、中文译名、稳定 ID 和分类。
4. 根据题库真实写法补充无歧义 aliases。
5. 编写简洁、课程语境明确的中文释义 `definition_zh`（英文定义 `definition` 已退役，不要再写）。
6. 运行 `python -m json.tool`、`python scripts/check_glossary.py --course <course_id>` 覆盖校验；课程目录或 manifest 有改动时再跑 `python scripts/check_courses.py`。
7. 人工处理 retired field、orphan 与候选报告，不要把候选结果直接批量写入术语库。
8. 校验通过后再发布（见第 12 节），默认会重跑 `check_glossary.py`：`python scripts/publish_course.py --course <course_id> --glossary glossary_candidate.json`（不传 `--glossary` 时，若该课程 manifest 已声明术语表，默认发布的正是这个候选文件；manifest 写 `glossary: null` 时默认候选**不会**自动发布，必须显式传 `--glossary`）。

术语表发布与题库发布是**两次独立的文件系统切换**，不是跨文件事务：同时传 `--questions` 与 `--glossary` 时它们按顺序分别执行，题库可能已经发布成功而术语表步骤失败。推荐先完成两次只读预检，必要时按内容类型分成两条命令发布，以获得清晰的失败边界。发布后必须**统一重启全部 worker**才能让新术语生效（术语表不参与 generation，`/ready` 很可能仍是 200，因此 readiness 不能证明术语表已重新加载；见第 1.1 节）。发布结束会打印版本保留结果：每个内容类型只保留**当前版本 + 上一版**（见 [`COURSE_GUIDE.md`](COURSE_GUIDE.md) 第 7.11 节），因此上一版随时可以回退。

更换 `questions.json` 时按 [`QUESTION_GUIDE.md`](QUESTION_GUIDE.md) 第 11.6 节发布：先预检（`--course <course_id>`）、再原子发布（`publish_course.py --questions`）、最后更新 worker。题库按 `question.id` **在该课程内**逐题增量同步，**不会重置该课程的学习数据**；只有结构性变化（增删题目、判题规则变化、题目归属 `chapter_ids`/`source_id` 变化、课件/章节目录结构变化）会推进**该课程**的 generation，让仍在运行旧内容的 worker 只在该课程的学习页面返回 503，因此这类发布要更新 worker，但**不影响其他课程**。`glossary.json` 不属于任何题库指纹，单独修改它既不触发同步、也不影响 worker 围栏。发布新课程时建议两个文件一起审核，避免旧术语出现在新题库中。

## 13. 发布前检查清单

- [ ] manifest 的 `glossary` 指向该课程目录内正确的文件名（推荐 `versions/<sha256>/glossary.json`；plain-file 布局可写 `glossary.json`），或明确写 `null`，编码为 UTF-8。
- [ ] 编辑的是 candidate 文件（`courses/<course_id>/glossary_candidate.json`），且已用 `python scripts/check_glossary.py --course <course_id>` 校验过它（`--published` 用于复核已发布内容）。
- [ ] `schema_version` 是整数 `1`。
- [ ] 根级 `title`、`title_zh` 和 `terms` 已提供。
- [ ] 每个词条都有唯一稳定的 `id`、`term` 和 `term_zh`。
- [ ] 每个 alias 都是课程中实际使用、含义明确的非空字符串。
- [ ] 没有 canonical term/alias 的标准化冲突。
- [ ] 需要识别的复数、缩写、空格和连字符变体已显式加入 aliases。
- [ ] 中文译名和中文释义（`definition_zh`）已经过课程内容审核。
- [ ] 没有任何词条还带着已退役的 `definition` 字段（`check_glossary.py` 报告 `Retired term fields: none`）。
- [ ] 分类名称与粒度一致，词条顺序符合学习需要。
- [ ] 标准 JSON、GlossaryLoader、`check_glossary.py` 覆盖校验、`check_courses.py` 和完整测试集已运行且通过。
- [ ] orphan 和候选报告已经人工复核。
- [ ] 校验全部通过后才执行 publish course（`publish_course.py --course <course_id> --glossary …`，默认会重跑 `check_glossary.py`）；任一校验失败时没有发布任何内容。
- [ ] 已了解联合发布（同时传 `--questions` 与 `--glossary`）是**顺序执行的两个步骤**，并按需要拆成两条命令。
- [ ] 发布输出里的版本保留结果符合预期（默认每个内容类型保留当前版本 + 上一版，见 `COURSE_GUIDE.md` 第 7.11 节）。
- [ ] 已**统一重启全部 worker**（术语表不推进 generation，`/ready` 可能一直为 200，不能用它判断术语表是否已重新加载），并完成桌面端、手机端、鼠标和键盘验收。
