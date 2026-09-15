# `questions.json` 题库编写指南

本文面向生成、审核和维护题库的开发者。目标是生成一份可以被当前应用直接加载的 `questions.json`，无需为不同课程修改 Python、HTML 或 JavaScript 代码。

本文描述的是当前项目实际支持的题库契约。最终校验逻辑以 [`app/repositories/question_loader.py`](../app/repositories/question_loader.py) 为准。专业术语、别名、翻译、定义和分类不写在题库中，请使用独立的 [`glossary.json` 专业术语库编写指南](GLOSSARY_JSON_GUIDE.md)。

## 1. 适用范围

当前题库格式适合文本型选择题：

- 单选题；
- 多选题；
- 任意数量的文本选项；
- 可选的中英文双语内容；
- 按课程资料和章节筛选；
- 一道题同时属于同一课程资料下的多个章节；
- 通过章节、页码和小节名称回溯原始资料。

当前代码不会解释或渲染图片、音频、视频、公式对象、填空题、排序题、主观题等额外题型。即使把 `image`、`difficulty`、`tags` 等未知字段写入 JSON，Loader 也会忽略它们，界面不会自动获得对应功能。

应用启动时会同时校验 `questions.json` 和 `glossary.json`，两个文件都必须有效。只有 `questions.json` 的原始字节参与全局题库指纹：它的任意字节变化都会清空所有账号的课程学习数据；单独修改 `glossary.json` 不会触发清空。

## 2. 新题库的推荐完整结构

新题库统一使用 `schema_version: 2`，并显式提供 `sources`、`chapters` 和每道题的课程元数据。

下面是一份可以直接通过当前 Loader 校验的示例。第二道题演示了一题属于两个章节的写法。

```json
{
  "schema_version": 2,
  "title": "Mathematics Review",
  "title_zh": "数学复习题库",
  "sources": [
    {
      "id": "math-volume-1",
      "title": "Mathematics Volume 1",
      "filename": "mathematics-volume-1.pdf",
      "lecture": "Semester 1"
    }
  ],
  "chapters": [
    {
      "id": "linear-equations",
      "source_id": "math-volume-1",
      "title": "Linear Equations",
      "order": 1
    },
    {
      "id": "coordinate-geometry",
      "source_id": "math-volume-1",
      "title": "Coordinate Geometry",
      "order": 2
    }
  ],
  "questions": [
    {
      "id": "math-v1-q001",
      "source_id": "math-volume-1",
      "chapter_ids": ["linear-equations"],
      "section": "Solving one-variable equations",
      "pages": [12],
      "text": "What is the solution of 2x + 3 = 7?",
      "text_zh": "方程 2x + 3 = 7 的解是什么？",
      "type": "single",
      "options": [
        {
          "id": "x-1",
          "text": "x = 1",
          "text_zh": "x = 1"
        },
        {
          "id": "x-2",
          "text": "x = 2",
          "text_zh": "x = 2"
        },
        {
          "id": "x-5",
          "text": "x = 5",
          "text_zh": "x = 5"
        }
      ],
      "correct_answers": ["x-2"],
      "explanation": "Subtracting 3 and dividing by 2 gives x = 2.",
      "explanation_zh": "等式两边先减 3，再除以 2，得到 x = 2。"
    },
    {
      "id": "math-v1-q002",
      "source_id": "math-volume-1",
      "chapter_ids": ["linear-equations", "coordinate-geometry"],
      "section": "Lines in the coordinate plane",
      "pages": [35, 36],
      "text": "Which statements about the line y = 2x + 1 are correct?",
      "text_zh": "关于直线 y = 2x + 1，哪些陈述正确？",
      "type": "multiple",
      "options": [
        {
          "id": "slope-2",
          "text": "Its slope is 2.",
          "text_zh": "它的斜率是 2。"
        },
        {
          "id": "intercept-1",
          "text": "Its y-intercept is 1.",
          "text_zh": "它在 y 轴上的截距是 1。"
        },
        {
          "id": "through-origin",
          "text": "It passes through the origin.",
          "text_zh": "它经过原点。"
        }
      ],
      "correct_answers": ["slope-2", "intercept-1"],
      "explanation": "In slope-intercept form y = mx + b, m = 2 and b = 1, so the line does not pass through the origin.",
      "explanation_zh": "在斜截式 y = mx + b 中，m = 2、b = 1，因此该直线不经过原点。"
    }
  ]
}
```

## 3. 根对象字段

| 字段 | 新题库要求 | 类型 | 规则 |
|---|---:|---|---|
| `schema_version` | 必填 | 正整数 | 新题库使用 `2`。Loader 仍兼容旧版本，但不要据此生成新的旧格式题库。 |
| `title` | 必填 | 非空字符串 | 题库英文或主要标题，会显示在页面中。 |
| `title_zh` | 可选 | 非空字符串 | 提供时不能为空或纯空白；不需要中文标题时应省略。 |
| `sources` | 必填 | 非空数组 | 课程资料目录。必须与 `chapters` 一起提供。 |
| `chapters` | 必填 | 非空数组 | 可筛选的知识点或章节目录。必须与 `sources` 一起提供。 |
| `questions` | 必填 | 数组 | Loader 允许空数组，但一份可用题库应至少包含一道题。 |

JSON 文件必须使用 UTF-8 编码。标准 JSON 不允许注释、尾随逗号、单引号字符串或未转义的换行符。

## 4. `sources`：课程资料目录

一个 source 表示一本教材、一份讲义、一组课件或其他可以独立引用的资料。

| 字段 | 要求 | 类型 | 规则 |
|---|---:|---|---|
| `id` | 必填 | 非空字符串 | 在整份题库的 `sources` 中唯一，建议使用稳定的 kebab-case ID。 |
| `title` | 必填 | 非空字符串 | 显示给学习者的资料名称。 |
| `filename` | 可选 | 字符串 | 原始资料文件名，仅作为说明信息。 |
| `lecture` | 可选 | 字符串 | 讲次、学期或资料补充说明。 |

推荐：

```json
{
  "id": "world-history-volume-1",
  "title": "World History Volume 1",
  "filename": "world-history-volume-1.pdf",
  "lecture": "Ancient and Medieval History"
}
```

不要把 source 当作题型或难度分类。`source_id` 表示题目内容所依据的资料来源。

## 5. `chapters`：章节与知识点目录

一个 chapter 表示学习者可以选择练习的主题。它可以对应教材章节，也可以对应稳定、粒度适中的知识点。

| 字段 | 要求 | 类型 | 规则 |
|---|---:|---|---|
| `id` | 必填 | 非空字符串 | 在整份题库的 `chapters` 中全局唯一。 |
| `source_id` | 必填 | 非空字符串 | 必须引用一个已经存在的 source。 |
| `title` | 必填 | 非空字符串 | 显示给学习者的章节名称。 |
| `order` | 可选 | 非负整数 | 控制显示顺序；省略时使用该 chapter 在数组中的位置。布尔值不是合法整数。 |

章节设计建议：

- 使用课程本身的知识结构，不要把章节固定成某个现有课程的命名方式。
- 一个章节应能够形成有意义的独立复习范围。
- 避免章节过宽，例如“全部内容”；也避免细到几乎每道题一个章节。
- ID 表示稳定身份，标题负责显示。以后可以修改标题，但不要因为措辞调整就更换 ID。
- 不同 source 下即使有同名章节，也必须使用不同的 chapter ID。

## 6. `questions`：题目对象

| 字段 | 要求 | 类型 | 规则 |
|---|---:|---|---|
| `id` | 必填 | 非空字符串 | 在整份题库中唯一且长期稳定。 |
| `text` | 必填 | 非空字符串 | 主要题干。 |
| `text_zh` | 可选 | 字符串 | 中文辅助题干；省略后不显示翻译。 |
| `type` | 必填 | 字符串 | 只能是 `single` 或 `multiple`。 |
| `options` | 必填 | 数组 | 至少两个 option，没有代码层面的数量上限。 |
| `correct_answers` | 必填 | 非空字符串数组 | 内容必须是本题已有的 option ID，不能重复。 |
| `explanation` | 可选 | 字符串 | 主要解析。强烈建议提供。 |
| `explanation_zh` | 可选 | 字符串 | 中文辅助解析。 |
| `source_id` | 必填 | 非空字符串 | 必须引用一个已有 source。 |
| `chapter_ids` | 必填 | 非空字符串数组 | 至少一个已有 chapter，不可重复，并且所有 chapter 都必须属于本题的 `source_id`。 |
| `section` | 可选 | 字符串 | 原始资料中的小节名或内容位置。 |
| `pages` | 可选 | 正整数数组 | 页码必须大于等于 1 且不能重复；没有页码时可以省略或写 `[]`。 |

### 6.1 题目 ID 必须稳定

用户作答记录和错题记录通过 `question.id` 关联题目。因此：

- 修正题干、选项措辞或解析时，保留原 question ID。
- 删除题目后，不要把它的 ID 分配给一道无关的新题。
- 替换为另一门课程并继续使用原数据库时，不要重新从通用的 `q001` 开始复用旧 ID。推荐加入课程命名空间，例如 `calculus-q001`、`history-q001`。
- 如果确实要复用所有 ID，必须明确清空或迁移旧的学习记录；这属于数据库操作，不是更换 JSON 本身能够解决的问题。

### 6.2 一题属于多个章节

`chapter_ids` 可以包含一个或多个章节：

```json
"chapter_ids": ["linear-equations", "coordinate-geometry"]
```

筛选采用“有任意交集即匹配”的规则。上例中的题目在选择 `linear-equations` 或 `coordinate-geometry` 时都会出现；同时选择两个章节也只会出现一次。

使用原则：

- 默认只填写一个最主要的学习目标。
- 只有当正确作答确实需要综合多个知识点时，才填写多个 chapter ID。
- 题干顺带提到另一个概念，不代表它属于另一个章节。
- 不要为了增加曝光率把每道题归入大量章节。
- 同一道题的全部 chapter 必须属于同一个 `source_id`。当前 Loader 会拒绝跨 source 的章节组合。

### 6.3 `single` 与 `multiple`

单选题必须恰好有一个正确答案：

```json
{
  "type": "single",
  "correct_answers": ["option-id"]
}
```

多选题可以有一个或多个正确答案：

```json
{
  "type": "multiple",
  "correct_answers": ["first-correct-id", "second-correct-id"]
}
```

多选题采用完全匹配判分：学习者选中的 option ID 集合必须与 `correct_answers` 完全一致。少选、多选或选错任意一项都判错。

题干必须明确告诉学习者是单选还是多选，例如：

- 单选：`Which statement best explains ...?`
- 多选：`Which statements are correct? Select all that apply.`

### 6.4 多行文本与换行

`text`、`text_zh`、`options[].text`、`options[].text_zh`、`explanation` 和 `explanation_zh` 都支持用 `\n` 表示换行。界面通过 CSS `white-space: pre-line` 渲染这些字段：`\n` 会显示为实际换行，而连续空格和 Tab 仍会被折叠，不影响正常排版。

适合换行的典型场景是罗马数字分点题干：

```json
"text": "Consider statements I–III about yield:\nI. Yield is the proportion of functional devices.\nII. Reliability is the ability to perform over time.\nIII. Which option is correct?"
```

注意：

- JSON 字符串内不能直接书写真实换行，必须写成转义序列 `\n`（见第 3 节的 JSON 格式要求）。
- 换行只用于确实有分点、分行结构的文本，不要用它制造段间距或做排版微调。
- 同一道题的英文与中文文本应保持一致的换行结构，避免双语对照错位。

## 7. `options`：选项对象

| 字段 | 要求 | 类型 | 规则 |
|---|---:|---|---|
| `id` | 必填 | 非空字符串 | 在同一道题内唯一。 |
| `text` | 必填 | 非空字符串 | 主要选项内容。 |
| `text_zh` | 可选 | 字符串 | 中文辅助内容。 |

判题只使用 option ID，不使用数组位置或界面上的 A、B、C 标签。选项会被打乱显示，因此禁止这样写：

```json
"correct_answers": ["A"]
```

除非某个 option 的真实 `id` 本身就是 `A`，否则这不是对“第一个选项”的引用。推荐使用表达语义且稳定的 ID：

```json
{
  "options": [
    {"id": "lower-latency", "text": "Lower latency"},
    {"id": "higher-latency", "text": "Higher latency"}
  ],
  "correct_answers": ["lower-latency"]
}
```

每题至少需要两个选项，当前界面没有选项数量上限。显示标签会按 `A` 到 `Z`、`AA`、`AB` 的方式继续生成。不过，选项太多通常会降低题目质量，应由课程内容而不是界面能力决定数量。

## 8. 内容质量要求

通过格式校验只表示数据可加载，不表示题目在事实、语义或教学上合格。本节中的 **MUST** 是不满足就必须重写的规则，**SHOULD** 是无明确理由时应遵循的规则；这些要求属于生成和审核流程，不是新的 JSON 字段。

### 8.1 题干

- **MUST：目标明确。** 一般只测试一个主要学习目标；综合题必须明确给出需要组合的条件。
- **MUST：解释唯一。** 掌握知识的学习者应能明确判断题目究竟要求什么。避免未定义的“通常”“最好”“主要”、不明确的时间范围、隐含场景和只能猜测出题意图的措辞。使用 `best`、`most appropriate`、`primary` 或 `most likely` 时，题干必须给出足够条件，使最佳答案唯一。
- **MUST：信息完备。** 题目不得依赖未展示的图片、表格、上下文或隐藏前提。生成后执行 **Necessary Information Test**：是否必须补充题干中没有写出的假设，才能证明答案？如果是，应把必要条件加入题干，而不是在 explanation 中补救。
- **MUST：题型明确。** 存在多个正确项时必须明确要求多选，例如 `Select all that apply.`。
- **MUST：不依赖顺序。** 避免“以上都对”“前两项”等表达；选项会被随机排序。
- **SHOULD：正向提问。** `NOT`、`EXCEPT` 或“哪个不正确”默认应改成正向问题。只有负向判断本身具有学习价值时才能保留，并应显式突出否定条件。难度不能来自漏看一个 `NOT`。
- **SHOULD：语言直接。** 避免双重否定、文字游戏、无关背景和不必要的生僻措辞。
- **MUST：限定语境。** 如果答案依赖特定版本、年份、作者观点或课程定义，题干必须写出必要限定；source 元数据只能帮助溯源，不能代替作答所需条件。

### 8.2 正确答案与干扰项

- **MUST：single 只有一个可合理辩护的最佳答案。** `correct_answers` 只写一个 ID，不等于语义上只有一个答案。审核者必须主动尝试为每个错误项寻找正常、合理且不牵强的成立解释；只要另一个选项也能成立，就应 **REJECT AND REWRITE THE QUESTION**，不能靠 explanation 强行指定答案。
- **MUST：multiple 的每项可独立判定。** 每个 option 都应能单独判断 true/false，不得语义包含、范围重叠、互相依赖或换一种说法重复同一事实。当前应用采用完全匹配判分，模糊的选项边界会直接制造无意义难度。
- **MUST：干扰项真实可信但明确错误。** 优先取材于常见概念混淆、计算错误、因果倒置、条件遗漏、范围扩大或缩小、相似概念、错误推理路径，以及“部分正确但不满足题干完整要求”的陈述。对每个 distractor 都要能回答：“哪一种真实误解会让学习者选择它？”答不出来时，它很可能只是 filler，必须重写。
- **MUST NOT：无效填充。** 禁止明显荒谬、与主题无关、玩笑式、语义类型完全不同或只为凑数量而生成的选项。
- **SHOULD：选项同质。** 检查语法结构、语义类别、抽象层级、单位、数值精度、术语、范围和大致长度。正确答案不能因为最长、最完整、最专业、最谨慎或最像教材原话而显得特殊。
- **MUST：选项可区分。** 两个不同 option ID 不得表达实质相同的答案，也不得严重重叠；答案不能依赖选项原始排列位置。
- **SHOULD：避免措辞启发式。** 不要让错误项系统性使用 `always`、`never`、`all`、`none`、`only`、`completely`、`must`，也不要让正确项系统性使用 `usually`、`generally`、`often`、`may`、`typically`，除非知识本身确实要求这些限定词。答案应由知识决定，而不是由语言习惯决定。

### 8.3 Answer Leakage and Test-Wiseness

逐题检查以下泄漏信号：

- 正确答案明显更长、更具体、更完整或更像教材原话；
- 只有正确答案复用题干关键词，或只有它能与题干形成正确语法；
- `a/an`、单复数、数值精度或单位格式暴露答案；
- 错误项大量使用绝对措辞，而正确项使用保守措辞；
- 一个 option 包含另一个 option，或多个选项的范围不互斥；
- source 原句只被复制到正确答案，而所有 distractor 都是改写文本。

强制执行 **Answer-Without-Knowledge Test**：如果完全不知道该知识点，只看选项长度、格式、语法、措辞和结构，是否仍能明显提高猜中概率？如果答案是 yes，必须修改题干或选项。随机显示选项只能打散位置，不能消除其他泄漏。

### 8.4 解析

高质量 explanation 必须回答：为什么正确答案成立、题干中的哪个关键事实决定答案，以及最具迷惑性的错误项为什么错。

- **MUST：证明而非复述。** 禁止仅重复答案，或写成 `A is correct because A is correct`。
- **MUST：不补造前提。** explanation 不得加入题干没有的关键条件来修复歧义；出现这种情况应修改题干。
- **MUST：与答案一致。** explanation 不得与 `correct_answers` 冲突。
- 多选题应覆盖全部正确项，必要时解释最容易误选的错误项。
- 计算题应给出关键计算或推导步骤；场景题应指出 decisive evidence；概念辨析题应说明正确概念与易混概念的关键区别。
- 优先把 `section` 和 `pages` 作为结构化溯源信息，不要只把来源埋在解析文本中。页码应对应 source 所指文件的实际页码，并统一采用同一种页码口径。

### 8.5 Source-grounded question quality

如果题目声称依据课程资料：

- **MUST：** 正确答案能够由指定 `source_id` 所指资料支持；提供 `section` 和 `pages` 时，必须逐项核对其引用位置。
- **SHOULD：** 原始资料有明确位置时，填写准确的 `section` 和 `pages`，使审核者能复核依据。
- **MUST NOT：** 把模型自己的外部常识加入一个本应只依据课程资料作答的问题。
- **MUST：** 依赖特定版本、年份、作者观点或课程定义时，在题干中提供必要限定。
- **MUST NOT：** 直接复制 source 中一段非常独特的长句作为正确答案，同时把所有 distractor 改写成不同风格。这只是在测试文本匹配。

`source_id`、`section` 和 `pages` 是来源定位信息，不自动证明事实正确。审核者仍需打开相应资料确认“引用位置确实支持题干和答案”，而不只是确认页码存在。

### 8.6 认知难度与题型多样性

不要新增 Loader 不支持的 `difficulty` 或 `cognitive_level` 字段；认知难度通过命题与题集审核控制。

- 合理难度来自概念辨析、推理步骤、条件组合、常见误解、知识迁移，以及重要但细微的区别。
- 人为难度来自冗长文字、生僻措辞、无关背景、隐藏条件、双重否定、文字游戏和无价值细节记忆。
- **MUST：** 难题可以要求更深的知识和推理，但不能依赖不清晰的语言制造难度。
- **SHOULD：** 当课程内容允许时，在整个题库中平衡 recall、understanding、application 和 analysis/reasoning，避免全部写成“X 的定义是什么？”。可加入适量场景判断、原因分析、概念比较、应用题、条件推导和最佳解释，但不要为了提高认知层级而人为加入冗长场景。

### 8.7 双语内容

- `text_zh`、option 的 `text_zh` 和 `explanation_zh` 都是可选字段。
- 如果提供翻译，中文和主要语言的正确答案必须完全一致；含义、否定词、数量限定词、单位，以及 `only`、`all`、`may`、`must` 等程度必须一致。
- 一个语言版本不得比另一个版本提供额外答案线索。正确 option 不能只在一种语言中明显比 distractor 更详细、更自然或更具体。
- 不需要翻译时应省略字段；根级 `title_zh` 如果存在则不能是空字符串。

双语审核不仅检查“翻译是否正确”，还必须检查两种语言版本的难度和答案线索是否等价。

### 8.8 题库整体质量

每一道题单独合格，不代表整个题库整体合格。批量题库还应检查：

- 是否大量复用同一种题干模板、句式或 distractor 结构；
- 是否反复考查同一事实，或存在仅替换少量词语的近似重复题；
- 是否全部停留在 recall，缺少理解、应用和分析；
- 任意两道题是否直接泄露彼此答案；题目会随机抽取和排序，不能假定提供线索的题一定后出现；
- 正确项是否系统性更长，错误项是否系统性使用绝对措辞；
- 各 source、chapter 和重要知识点的覆盖是否严重失衡。

发现系统性模式时，应在题集层面改写模板、去重、消除题间答案泄漏并补足覆盖，不能只逐题润色。

### 8.9 Hard reject criteria

出现任意一项，不进行软评分或“勉强通过”，直接重写：

- 存在事实错误；
- `single` 题存在第二个可合理辩护的答案；
- `multiple` 题有 option 无法明确判定；
- 缺少作答所需的关键条件；
- `correct_answers` 与 explanation 冲突；
- distractor 与正确答案实质等价，或 options 严重重叠；
- 指定 source 不能支持答案，或 source-only 题依赖 source 外知识；
- 存在明显 answer leakage；
- 双语版本改变正确答案或关键限定；
- explanation 只能通过新增前提证明答案。

### 8.10 三个简短示例

以下均是聚焦命题质量的题目字段片段；完整题目仍需补齐第 6 节要求的 ID 和课程元数据。

#### Example A — Weak distractors

Bad：错误项与主题无关，不需要理解知识即可排除。

```json
{
  "text": "Which factor can improve cache performance?",
  "type": "single",
  "options": [
    {"id": "hit-rate", "text": "A higher cache hit rate"},
    {"id": "moon", "text": "The color of the Moon"},
    {"id": "renaissance", "text": "The date of the Renaissance"}
  ],
  "correct_answers": ["hit-rate"],
  "explanation": "A higher cache hit rate improves cache performance."
}
```

Improved：各项对应命中率、未命中率、miss penalty 和 hit time 的真实方向混淆。

```json
{
  "text": "Which isolated change most directly reduces average memory access time?",
  "type": "single",
  "options": [
    {"id": "increase-hit-rate", "text": "Increase the cache hit rate"},
    {"id": "increase-miss-rate", "text": "Increase the cache miss rate"},
    {"id": "increase-miss-penalty", "text": "Increase the cache miss penalty"},
    {"id": "increase-hit-time", "text": "Increase the cache hit time"}
  ],
  "correct_answers": ["increase-hit-rate"],
  "explanation": "Increasing the hit rate reduces the fraction of accesses that pay the miss penalty; each other change increases a time or the frequency/cost of misses."
}
```

#### Example B — Two defensible answers

Bad：把 `Python` 标成唯一答案，但 `R` 在正常语境下同样可合理辩护。

```json
{
  "text": "Which language is best for data analysis?",
  "type": "single",
  "options": [
    {"id": "python", "text": "Python"},
    {"id": "r", "text": "R"},
    {"id": "html", "text": "HTML"}
  ],
  "correct_answers": ["python"],
  "explanation": "Python is widely used for data analysis."
}
```

Adversarial review：`R` 也广泛用于数据分析，因此题目必须拒绝。Improved：补充决定答案的场景条件。

```json
{
  "text": "A team must extend an existing pandas-based analysis pipeline without rewriting it. Which language is the most appropriate choice?",
  "type": "single",
  "options": [
    {"id": "python", "text": "Python"},
    {"id": "r", "text": "R"},
    {"id": "sql", "text": "SQL"},
    {"id": "julia", "text": "Julia"}
  ],
  "correct_answers": ["python"],
  "explanation": "pandas exposes a Python API, so Python extends the existing pipeline directly; R, SQL, or Julia would require porting the transformations or adding an interoperability layer."
}
```

#### Example C — Answer leakage

Bad：正确项远长于其他选项，也只有它完整复用题干概念。

```json
{
  "text": "What does instruction pipelining improve?",
  "type": "single",
  "options": [
    {"id": "throughput", "text": "It improves instruction throughput by overlapping multiple stages of instruction execution"},
    {"id": "power", "text": "Power"},
    {"id": "capacity", "text": "Capacity"}
  ],
  "correct_answers": ["throughput"],
  "explanation": "Pipelining overlaps stages and therefore improves throughput."
}
```

Improved：各项采用相同语义类别和相近形式，答案必须依靠知识判断。

```json
{
  "text": "Under steady-state operation, which processor metric is most directly improved by instruction pipelining?",
  "type": "single",
  "options": [
    {"id": "throughput", "text": "Instructions completed per unit time"},
    {"id": "latency", "text": "Time required by one instruction"},
    {"id": "memory-capacity", "text": "Bytes available in data memory"},
    {"id": "isa-size", "text": "Operations defined by the instruction set"}
  ],
  "correct_answers": ["throughput"],
  "explanation": "Overlapping pipeline stages allows more instructions to complete per unit time after the pipeline fills; it does not inherently increase memory capacity or ISA size, and it need not reduce one instruction's latency."
}
```

## 9. 不同课程如何映射到同一格式

schema 不要求代码理解某个学科的语义。课程差异应由目录和文本表达：

| 课程 | source 示例 | chapter 示例 | 题目仍使用的结构 |
|---|---|---|---|
| 数学 | 教材、习题讲义 | 极限、导数、概率 | `text`、`options`、`correct_answers` |
| 历史 | 教材、史料选编 | 朝代、事件、制度 | 同上，可用 `pages` 回溯史料 |
| 计算机 | 教材、课程课件 | 数据结构、网络、操作系统 | 同上，代码片段作为普通字符串并正确转义 |
| 语言 | 课本、词汇表 | 语法、词汇、阅读理解 | 同上；当前不支持音频题 |

章节 ID 和 option ID 是程序使用的稳定标识，但程序不会推断 `calculus`、`dynasty` 或 `networking` 的特殊含义。

## 10. 生成流程

推荐使用 **Generate → Independent Review → Validate**，并保留以下十步：

1. 确定题库标题和主要语言。
2. 列出全部 source，并分配稳定、唯一的 source ID。
3. 为每个 source 设计粒度一致的 chapters，并分配全局唯一的 chapter ID。
4. 生成题目及选项，先确定学习目标，再填写 `chapter_ids`。
5. 根据 option ID 填写 `correct_answers`，不要根据显示字母填写。
6. 添加解析和结构化来源信息。
7. 审核双语内容的一致性。
8. 由独立 reviewer 执行本节的语义审核，不接受“生成器已经标注的答案”作为证据。
9. 执行第 11 节中的 JSON、Loader 和 pytest 校验；再按 [`GLOSSARY_JSON_GUIDE.md`](GLOSSARY_JSON_GUIDE.md) 准备术语库并运行覆盖审计。
10. 在浏览器中抽查章节筛选、选项显示、判题、错题复习和术语高亮。

### 10.1 Generation pass

生成阶段负责确定学习目标，并写出当前 schema 已支持的 `text`、`options`、`correct_answers`、`explanation`、双语内容和 source/chapter/section/pages 元数据。质量概念不要写成 Loader 不支持的 `learning_objective`、`cognitive_level`、`difficulty`、`quality_score` 或 `review_status` 字段。

### 10.2 Independent review pass

批量生成必须先生成、再独立审核。Reviewer 不应默认 generator 的事实、答案或引用正确；其任务不是润色，而是尝试证明题目不合格。必须重新检查：

1. source 是否支持题干和答案；
2. factual correctness；
3. answer uniqueness；
4. distractor plausibility；
5. answer leakage；
6. explanation 是否真正证明答案；
7. chapter assignment；
8. `section` / `pages` grounding。

### 10.3 Mandatory semantic review pipeline

每道题按固定顺序完成 Pass 1～8，并记录“通过或重写”的结论；整批题目完成后再执行 Pass 9：

1. **Objective** — 题目到底在测试什么？是否只有一个明确主要目标？
2. **Correctness** — 正确答案是否事实正确，并与 explanation 一致？
3. **Uniqueness** — 主动尝试为每个错误 option 辩护；`single` 是否仍只有一个最佳答案？
4. **Distractors** — 每个错误项是否对应真实误解，而不是 filler？
5. **Leakage** — 不知道知识时，能否通过结构、长度或措辞提高猜中概率？
6. **Difficulty** — 难度来自知识和推理，还是隐藏条件与文字陷阱？
7. **Explanation** — 解析是否指出决定性事实并真正证明答案？
8. **Source grounding** — source、section 和 pages 是否真实支持内容？
9. **Schema** — 最后才执行 JSON、Loader 和测试校验。

任何一步命中第 8.9 节的 hard reject 条件，都应回到生成阶段重写。Loader validation 证明的是题库契约合法，不证明题目在教育意义上合格。

## 11. 必须执行的校验

校验分成两类，缺一不可：

- **Machine validation** 能检查 JSON syntax、schema、字段类型、ID、引用关系和 `correct_answers` 是否指向已有 option。
- **Semantic review** 由独立 LLM reviewer 或人工完成，检查答案唯一性、事实、干扰项、隐藏前提、answer leakage、认知难度和 source 的语义支持。

当前项目没有自动化“题目语义质量检测脚本”。以下命令均为仓库中真实存在的校验路径，并不能替代第 10.3 节的语义审核。

### 11.1 检查标准 JSON 语法

Linux 或 WSL：

```bash
python -m json.tool questions.json > /dev/null
```

PowerShell：

```powershell
python -m json.tool questions.json | Out-Null
```

### 11.2 使用项目 Loader 校验完整契约

```bash
python -c "from pathlib import Path; from app.repositories.question_loader import QuestionLoader; loader = QuestionLoader(Path('questions.json')); questions = loader.load(); print(f'OK: {len(questions)} questions, {len(loader.sources)} sources, {len(loader.chapters)} chapters')"
```

只有看到 `OK` 才表示题库已通过当前应用的结构、类型、ID 唯一性和引用关系校验；这不表示自然语言答案语义唯一，也不表示 source 真正支持答案。

### 11.3 运行题库测试和完整测试集

```bash
pytest -q tests/test_bundled_question_bank.py
pytest -q
```

### 11.4 语义审核

在运行机器校验之前，必须对每题执行第 10.3 节的九步流程，并对整份题库执行第 8.8 节检查。Reviewer 至少要查看实际 source、重新求解题目、逐项判断 options、运行 Answer-Without-Knowledge Test，并将 hard reject 题退回重写。不要用 Loader 或 pytest 通过代替这一步。

### 11.5 手工浏览器验收

- 首页显示新的题库标题，而不是旧课程名称。
- 练习设置页显示正确的 source 数量和 chapter 列表。
- 选择每个 chapter 都能启动练习。
- 一道多章节题能从其任意所属 chapter 被筛选出来，且页面显示全部所属章节。
- 单选题使用单选控件，多选题使用多选控件。
- 正确、少选、多选和错选的判定符合预期。
- 中文字段存在时正确显示，不存在时页面布局仍正常。
- 错题列表和复习模式可以正常找到新题目；错题页选择课件或章节后应立即筛选，不再出现“筛选错题”按钮。
- 题目、选项、反馈和解析中的专业术语能按 `glossary.json` 正确高亮。

## 12. 常见错误

### 12.1 Loader / schema 错误

| 错误 | 原因 | 修正方式 |
|---|---|---|
| `Question bank root must be a JSON object` | 根节点写成了数组 | 使用包含 `questions` 的根对象。 |
| `sources must be a non-empty array` | 只提供了 `chapters`，或 sources 为空 | `sources` 与 `chapters` 一起提供且均非空。 |
| `Duplicate source/chapter/question id` | 相应 ID 重复 | 为对象分配全局唯一且稳定的 ID。 |
| `type must be "single" or "multiple"` | 使用了其他题型名称 | 只使用当前支持的两种类型。 |
| `options must contain at least two items` | 选项不足 | 至少提供两个 option。 |
| `correct answer ... does not exist in options` | 答案写了显示字母、文本或错误 ID | 改为对应 option 的 `id`。 |
| `a single-choice question must have exactly one correct answer` | 单选题答案数量不是 1 | 修正答案，或确认题目本应为 `multiple`。 |
| `chapter_ids must be a non-empty array` | 使用空数组、字符串或遗漏字段 | 至少填写一个 chapter ID。 |
| `chapter_ids contains duplicates` | 同一题重复填写章节 | 去重，并重新审核是否真的需要多章节。 |
| `chapter ... belongs to a different source` | 题目的 source 与章节 source 不一致 | 选择同一 source 下的章节，或修正题目 source。 |
| `pages must be an array of positive integers` | 使用了字符串、0、负数或小数 | 使用如 `[1, 2]` 的正整数数组。 |
| JSON 能加载但新字段没有效果 | 字段不在当前 Loader/domain/template 契约中 | 不要依赖未知字段；如确需功能，必须先修改代码和测试。 |

### 12.2 Semantic question failures

这些问题通常不会触发 Loader 报错，但仍然必须修复：

| Failure | 问题 / 检查 | 修复方式 |
|---|---|---|
| Multiple Defensible Answers | `single` 题有两个或更多答案在合理解释下成立。主动为所有错误项辩护。 | 增加决定答案的必要限定，或重新设计题干与选项。 |
| Weak Distractors | 错误项荒谬、无关或没有对应的真实误解。 | 用常见混淆、错误推理或条件遗漏构造 plausible distractor。 |
| Hidden Assumption | 答案只有在题干未声明的前提下成立。 | 把作答所需条件加入题干；若无法简洁说明则重写。 |
| Longest Answer Wins | 正确项明显更长、更完整或更专业。 | 统一 options 的类别、粒度、结构和信息量，再做 Answer-Without-Knowledge Test。 |
| Source Wording Leakage | 只有正确项直接复制 source 的独特原句。 | 在保持事实准确的前提下，以一致风格重新表述全部 options。 |
| Overlapping Options | 两项范围重叠、相互包含或表达同一事实。 | 使每项 mutually distinguishable；多选题还要确保每项可独立判定。 |
| Explanation Repairs a Broken Question | 解析加入题干没有的条件，才能证明指定答案。 | 修改题干或重写整题，不要扩写解析来掩盖歧义。 |
| Unsupported by Source | 引用位置存在，但内容不能支持答案，或混入 source 外知识。 | 回到指定 section/pages 核对；修正内容、引用或题干的资料范围。 |
| Bilingual Answer Shift | 翻译遗漏否定或限定词，导致两种语言下答案不同。 | 对照逐句修正，并分别用两种语言重新作答。 |
| Dataset Pattern | 单题均可用，但整批存在模板重复、答案线索或覆盖失衡。 | 执行第 8.8 节的题集级去重、改写、题间泄漏与覆盖检查。 |

## 13. 旧格式兼容说明

旧题库可以省略 `sources` 和 `chapters`，应用会把所有题目放入 `legacy / Uncategorized`。旧的单值字段也仍然可以读取：

```json
"chapter_id": "floorplanning"
```

但新题库不要继续生成这种格式，应使用：

```json
"chapter_ids": ["floorplanning"]
```

同一道题不能同时提供 `chapter_id` 和 `chapter_ids`。

如果需要迁移旧题库，可以使用：

```bash
python scripts/migrate_question_metadata.py old-questions.json --output questions.json
```

迁移脚本只能帮助整理已有结构化元数据和部分引用信息，不能代替人工审核答案、章节语义、source 归属或页码准确性。

## 14. 发布前检查清单

### 14.1 技术与发布检查

- [ ] 文件名为项目实际加载的 `questions.json`，编码为 UTF-8。
- [ ] `schema_version` 为 `2`。
- [ ] 根标题与当前课程一致。
- [ ] `sources` 和 `chapters` 均非空，所有 ID 唯一。
- [ ] 所有 question ID 唯一、稳定，并且没有复用其他课程的历史 ID。
- [ ] 每题至少两个 option，且本题内 option ID 唯一。
- [ ] 每个 `correct_answers` 值都能在本题 options 中找到。
- [ ] 每道单选题恰好一个正确答案。
- [ ] 每道多选题的题干明确提示多选，答案集合已经复核。
- [ ] 每题至少一个 chapter，多个 chapter 只用于真正的跨知识点题目。
- [ ] 每题的 source 和全部 chapters 属于同一资料来源。
- [ ] `pages` 只含不重复的正整数，并已核对页码口径。
- [ ] 标准 JSON、项目 Loader、题库测试和完整测试集全部通过。
- [ ] 配套 `glossary.json` 已按专业术语库指南完成校验和覆盖审计。
- [ ] 已在浏览器中抽查章节筛选、判题、解析、错题即时筛选、错题复习和术语高亮。

### 14.2 Question quality checklist

- [ ] 每题有一个明确的主要学习目标。
- [ ] 题干包含作答所需的全部关键条件，不依赖隐藏假设。
- [ ] `single` 题只有一个可以合理辩护的最佳答案，而不只是 JSON 中只标了一个 ID。
- [ ] 已主动尝试为每个错误 option 辩护；出现第二个合理答案时已退回重写。
- [ ] `multiple` 题的每个 option 均可独立、明确地判定 true/false。
- [ ] 每个 distractor 对应合理误解，而不是明显错误或无关 filler。
- [ ] Options 在语义类别、语法、抽象层级、单位、精度、范围和信息量上基本一致。
- [ ] 已执行 Answer-Without-Knowledge Test；不知道知识时，无法仅凭措辞、长度、语法或格式明显提高猜中概率。
- [ ] 难度来自知识、辨析或推理，而不是冗长文字、隐含条件、否定陷阱或文字游戏。
- [ ] Explanation 能说明为什么答案成立、哪个事实决定答案，并处理关键 distractor，而不是简单复述。
- [ ] Explanation 没有偷偷加入题干缺失的必要前提，且与 `correct_answers` 完全一致。
- [ ] 指定的 source / section / pages 与实际题目依据一致，且 source 确实支持答案。
- [ ] 中英文内容在题意、否定词、限定程度、单位和正确答案上保持一致，不提供不同的答案线索。
- [ ] 所有命中第 8.9 节 hard reject criteria 的题目都已重写，而非软评分后放行。

### 14.3 Dataset-level checklist

- [ ] 没有大量重复相同题干模板、句式或 distractor 结构。
- [ ] 没有反复考查同一事实或仅替换少量词语的近似重复题。
- [ ] 在课程允许范围内覆盖 recall、understanding、application 和 analysis/reasoning，而非全部停留在定义记忆。
- [ ] 任意题目都不会直接泄露另一题答案；检查时没有假定固定题序。
- [ ] 正确项没有系统性更长或更保守，错误项没有系统性使用绝对措辞。
- [ ] 各 source、chapter 和重要知识点的覆盖没有无依据的严重失衡。
