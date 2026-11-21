# 5. Dify 工作流完整复刻指南

这一章的目标：你手上没有这个 Dify 应用，照着做完能建出一个功能等价的。

**配套文件：** 压缩包里的 `dify_workflow_graph.json`，含每个节点的完整原始配置。

---

## 5.1 工作流总览

```
                     ┌──────────────┐
                     │  用户输入     │  start
                     │  1792291899555│
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐
                     │    LLM       │  需求解析器
                     │ 1792292308073│  deepseek-flash
                     └──┬────┬───┬──┘
                        │    │   │
        ┌───────────────┘    │   └──────────────┐
        │                    │                  │
┌───────▼────────┐  ┌────────▼────────┐  ┌──────▼──────────┐
│  检索词提取     │  │  知识检索 2      │  │  规则合并检查    │
│ sq_extract_1   │  │ 1792316098509   │  │ 1792293113071   │
│  code 522字    │  │ 自制礼物知识库   │  │  code 20451字   │
└──┬──┬──┬───────┘  └────────┬────────┘  └──────┬──────────┘
   │  │  │                   │                  │
   │  │  └──────────┐        │                  │
   │  │             │        │                  │
┌──▼──┴──┐  ┌───────▼──────┐ │                  │
│知识检索1│  │ 知识检索 3    │ │                  │
│商品事实库│  │ 节日与场合    │ │                  │
└──┬─────┘  └──────┬───────┘ │                  │
   │               │         │                  │
   │        ┌──────▼─────────▼──┐               │
   │        │  地域与禁忌检索     │               │
   │        │ 1792833782604     │               │
   │        └──────┬────────────┘               │
   │               │                            │
   └───────────────┴────────────┬───────────────┘
                                │
                         ┌──────▼───────┐
                         │    LLM 2     │  候选排序器
                         │ 1792294044029│  temp 0.3 / max_tokens 16384
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │  代码执行 2   │  校验 + 截断到 8 张
                         │ 1792295487009│  code 8704字
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │ Card Writer  │  贺卡文案
                         │ 1792296553557│  deepseek-flash
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │Output Assembler│ 清洗文案
                         │ 1792296554000│  code 980字
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │    输出       │  end
                         │ 1792296856116│
                         └──────────────┘
```

## 5.2 13 个节点清单

| # | 标题 | 类型 | 节点 ID | 配置要点 |
|---|---|---|---|---|
| 1 | 用户输入 | start | 1792291899555 | 2 个 text-input 变量 |
| 2 | LLM | llm | 1792292308073 | deepseek-flash，需求解析 |
| 3 | 规则合并检查 | code | 1792293113071 | python3，20451 字符 |
| 4 | 知识检索 2 | knowledge-retrieval | 1792316098509 | 自制礼物知识库，top_k 8 |
| 5 | 知识检索 1 | knowledge-retrieval | 1792830184658 | 商品事实库，top_k 8 |
| 6 | 知识检索 3 | knowledge-retrieval | 1792831342745 | 节日与场合，top_k 8 |
| 7 | 地域与禁忌检索 | knowledge-retrieval | 1792833782604 | 地域与禁忌，top_k 8 |
| 8 | 检索词提取 | code | sq_extract_1 | python3，522 字符 |
| 9 | LLM 2 | llm | 1792294044029 | deepseek-flash，temp 0.3，max_tokens 16384 |
| 10 | 代码执行 2 | code | 1792295487009 | python3，8704 字符 |
| 11 | Card Writer | llm | 1792296553557 | deepseek-flash，贺卡文案 |
| 12 | Output Assembler | code | 1792296554000 | python3，980 字符 |
| 13 | 输出 | end | 1792296856116 | 输出 Output Assembler 的 result |

## 5.3 18 条边（完整清单）

```
用户输入          → LLM
LLM               → 规则合并检查
LLM               → 知识检索 2
LLM               → 检索词提取
检索词提取        → 知识检索 1
检索词提取        → 知识检索 3
检索词提取        → 地域与禁忌检索
知识检索 1        → 规则合并检查
知识检索 2        → 规则合并检查
规则合并检查      → LLM 2
知识检索 2        → LLM 2
知识检索 1        → LLM 2
知识检索 3        → LLM 2
地域与禁忌检索    → LLM 2
LLM 2             → 代码执行 2
代码执行 2        → Card Writer
Card Writer       → Output Assembler
Output Assembler  → 输出
```

**注意两个反直觉的地方：**

1. **LLM 2 有 5 条入边。** 规则检查结果 + 四个检索结果全部汇入。
2. **知识检索 1 和 2 同时连到「规则合并检查」和「LLM 2」。** 不是链式，是扇出。

## 5.4 逐节点配置

### 节点 1：用户输入（start）

```
节点 ID: 1792291899555
类型: start
```

两个变量，都是 `text-input`，都必填：

| 变量名 | 显示标签 | 说明 |
|---|---|---|
| `gift_request` | 礼物需求 JSON | 前端传来的结构化需求，字符串 |
| `candidate_products` | 候选商品 JSON | 36 件候选的 JSON 数组字符串 |

**为什么用字符串而不是 object？** 因为 Dify 的 start 节点对复杂类型支持有限，
且后端要把 `gender_hint` 拼到 `gift_request` 末尾，用字符串最省事。

### 节点 2：LLM（需求解析器）

```
节点 ID: 1792292308073
类型: llm
模型: langgenius/deepseek/deepseek · deepseek-flash
mode: chat
context: {enabled: false, variable_selector: []}
vision: {enabled: false}
structured_output_enabled: false
```

**提示词见第 6.1 节（system 1201 字符 + user 106 字符）。**

**这个节点的作用：** 把前端那份半结构化的问卷（可能带自由文本）翻译成严格 JSON。
输出 `occasion` / `recipient_relation` / `budget_min` / `budget_max` /
`hard_preferences` / `soft_preferences` / `unknown_notes` / `input_confidence`，
外加一个 `search_query` 字段（含 `product` 和 `occasion_festival` 两个检索词串）。

**为什么 `context` 是关闭的？** 这个节点不需要知识库上下文，纯靠提示词和输入。

**`structured_output_enabled: false` 但配了 schema** —— 这是历史遗留。
schema 存在但没启用，实际靠提示词约束输出格式，再由下游代码节点容错解析。
想更稳可以打开它。

### 节点 3：规则合并检查（最大的代码节点）

```
节点 ID: 1792293113071
类型: code
语言: python3
代码: 20451 字符 / 579 行
```

**输入变量（5 个）：**

| 变量名 | 来源 | 类型 |
|---|---|---|
| `candidate_products` | start.candidate_products | string |
| `gift_request` | start.gift_request | string |
| `purchased_knowledge` | 知识检索 1.result | array[object] |
| `diy_knowledge` | 知识检索 2.result | array[object] |
| `structured_output` | LLM.text | string |

**输出：** `result`（string，JSON）

**这个节点是整个工作流最大的维护负担。** 逻辑全埋在 579 行代码里，
改一行要看半天，而且**改坏了表现为「静默 0 个合格商品」，不报错**。

它做的事：
1. 解析各方输入（容错 JSON）
2. 按预算区间过滤（闭区间，`price < budget_min` 或 `price > budget_max` 都排除）
3. 按硬约束过滤（过敏、宗教饮食、素食、不饮酒）
4. 按禁忌过滤
5. 按场合冲突过滤
6. 输出 `eligible_products` / `hard_blocks` / `soft_warnings` / `excluded_summary`

**⚠️ 输出结构容易看错（我错过一次）：**

```python
out = json.loads(node_output["result"])       # 先解析成 dict
inner = json.loads(out["result"])             # 里面还有一个 JSON 字符串
eligible = inner["rule_result"]["eligible_products"]   # 这是【商品 ID 的字符串列表】
```

**`eligible_products` 是 ID 字符串列表，不是商品对象列表。**

**离线可读副本：** 压缩包里的 `dify/rule_node_final.py`（614 行）是这个节点的精简版，
逻辑等价但注释更少，适合本地调试。注意它写的是 `ranker-v5`，线上是 `ranker-v6`。

### 节点 4–7：四个知识检索

全部相同配置：

```
类型: knowledge-retrieval
retrieval_mode: multiple
top_k: 8
score_threshold: null
reranking_model: langgenius/tongyi/tongyi · qwen3-rerank
```

| 节点 | 节点 ID | 数据集 ID | 绑定文件 | 查询变量 |
|---|---|---|---|---|
| 知识检索 1 | 1792830184658 | `ef0c2968-367a-453d-a354-e2749348304c` | `06_商品事实库_合并重点名版.md` | `检索词提取.q_product` |
| 知识检索 2 | 1792316098509 | `e87f8114-863c-4ca7-8fc2-a9baca0cfa65` | `02_自制礼物知识库.md` | `start.gift_request` |
| 知识检索 3 | 1792831342745 | `f787b1b7-6fb0-4213-b962-66c5770d3255` | `03_节日与场合知识库.md` | `检索词提取.q_occasion` |
| 地域与禁忌检索 | 1792833782604 | `6576c580-4309-4a15-b71f-edf2bc3e207c` | `04_地域与禁忌提醒库.md` | `检索词提取.q_occasion` |

**每个节点只挂 1 个数据集。** 早期版本有节点挂了多个，检索质量明显下降。
最多返回 4 × 8 = 32 条文档。

**数据集 ID 是实例相关的**，你新建的知识库会有不同的 ID。导入知识库后
要在节点里重新选一次数据集。

**为什么知识检索 2 的查询用 `gift_request` 原文而不是检索词？**
自制礼物知识库条目少（343 行），用原始需求描述召回更准；
商品库太大（14,698 行），必须用提炼过的检索词。

### 节点 8：检索词提取（小代码节点）

```
节点 ID: sq_extract_1
类型: code
语言: python3
代码: 522 字符 / 19 行
输入: text（来自 LLM.text）
输出: q_product, q_occasion
```

完整源码：

```python
def main(text: str) -> dict:
    import json, re
    t = text or ''
    if '</think>' in t:
        t = t.split('</think>', 1)[1]
    m = re.search(r'\{.*\}', t, re.S)
    q = {}
    if m:
        try:
            q = json.loads(m.group(0)).get('search_query', {})
        except Exception:
            q = {}
    p = str(q.get('product', '')).strip()
    o = str(q.get('occasion_festival', '')).strip()
    if not p:
        p = '生日礼物 可爱 小东西'
    if not o:
        o = '生日 礼物'
    return {'q_product': p, 'q_occasion': o}
```

**作用：** 从 LLM 输出的 JSON 里抠出 `search_query` 两个字段，
供三个检索节点当查询词。

**注意两个兜底值：** 解析失败时 `q_product` 用「生日礼物 可爱 小东西」，
`q_occasion` 用「生日 礼物」。这是为了保证检索节点永远有输入，不会空查。

### 节点 9：LLM 2（候选排序器）

```
节点 ID: 1792294044029
类型: llm
模型: langgenius/deepseek/deepseek · deepseek-flash
mode: chat
completion_params: {max_tokens: 16384, temperature: 0.3}
context: {enabled: false, variable_selector: []}
```

**提示词见第 6.2 节（system 1976 字符 + user 241 字符）。**

**这是耗时最大的一环，实测约 28 秒，占整个工作流 47%。**

**三个参数为什么这么设：**

| 参数 | 值 | 原因 |
|---|---|---|
| `max_tokens` | 16384 | 原设 4096，导致约 33% 的 run 因 JSON 截断而零挑选。提到 16384 后解决 |
| `temperature` | 0.3 | 排序任务要稳定，但文案需要一点变化，0.3 是折中 |
| `context.enabled` | false | 检索文本靠 user prompt 里硬编码的变量引用注入，不用 context 机制 |

**⚠️ `context` 关闭但检索文本照样进去** —— 因为 user prompt 里写死了
`{{#1792830184658.result#}}` 这种引用。这是容易看漏的一点。

### 节点 10：代码执行 2（校验与截断）

```
节点 ID: 1792295487009
类型: code
语言: python3
代码: 8704 字符 / 322 行
输入: arg1（LLM 2 输出）, arg2（规则合并检查结果）
输出: result
```

**作用：**
1. 校验 LLM 2 选的商品 ID 是否真在 `eligible_products` 里（防模型幻觉）
2. 补回 `price_estimate` / `delivery_estimate`（模型不输出这两个，从规则节点数据合并）
3. **截断到最多 8 张**

**⚠️ 第 204 行的硬上限：**

```python
if not isinstance(item, dict) or len(candidates) >= 8:
    continue
```

**这是硬上限 8，没有下限。** Dify 挑出几张就是几张 —— 挑 5 张页面就 5 张，不会补。
所以「8 件」是软目标，实测 7 或 8 都正常。

### 节点 11：Card Writer（贺卡文案）

```
节点 ID: 1792296553557
类型: llm
模型: deepseek-flash
mode: chat
```

**提示词见第 6.3 节（system 540 字符 + user 135 字符）。**

只写一句 `card_message`，不超过 80 中文字符。不动候选事实。

### 节点 12：Output Assembler（清洗）

```
节点 ID: 1792296554000
类型: code
语言: python3
代码: 980 字符 / 30 行
```

完整源码：

```python
def main(card_message: str, validated: str) -> dict:
    import json
    import re

    s = str(card_message or "").strip()
    s = re.sub(r"<think[^>]*>.*?</think\s*>", "", s, flags=re.S | re.I).strip()
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s).strip()
    s = re.sub(r"\s*```$", "", s).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            inner = json.loads(s)
            if isinstance(inner, dict) and isinstance(inner.get("card_message"), str):
                s = inner["card_message"].strip()
        except Exception:
            pass
    s = s.strip("\"'“”‘’` ").strip()
    # Card Writer 契约：card_message 不超过 80 个中文字符。
    if len(s) > 80:
        s = s[:80]

    try:
        data = json.loads(validated)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["card_message"] = s

    return {"result": json.dumps(data, ensure_ascii=False)}
```

**作用：** 模型可能输出 `<think>` 块、Markdown 代码块、引号包裹的 JSON。
这个节点全部剥掉，只留纯文案，截断到 80 字，然后塞回完整 JSON。

### 节点 13：输出（end）

```
节点 ID: 1792296856116
类型: end
outputs: [{value_selector: [1792296554000, "result"], variable: "text"}]
```

输出变量名叫 `text`，值是 Output Assembler 的 `result`。

**所以后端拿到的是 `data.outputs.text`，一个 JSON 字符串。**

## 5.5 搭建步骤（从零开始）

### 第 1 步：建知识库

在 Dify 里建 4 个知识库，分别导入：

| 知识库名 | 导入文件 | 分段方式 |
|---|---|---|
| 商品事实库 | `06_商品事实库_合并重点名版.md` | 自动分段 |
| 自制礼物知识库 | `02_自制礼物知识库.md` | 自动分段 |
| 节日与场合知识库 | `03_节日与场合知识库.md` | 自动分段 |
| 地域与禁忌提醒库 | `04_地域与禁忌提醒库.md` | 自动分段 |

**导入 `06_商品事实库_合并重点名版.md` 会比较久**（5.2 MB / 14,698 行），
Dify 要切片和向量化，耐心等。

### 第 2 步：建工作流

Dify → 创建应用 → 工作流（Workflow）。

### 第 3 步：加节点

按 5.1 的图加 13 个节点，按 5.3 连 18 条边。

**顺序建议：** 先连主链（start → LLM → 规则合并检查 → LLM 2 → 代码执行 2 →
Card Writer → Output Assembler → end），跑通再加四个检索和检索词提取。

### 第 4 步：填配置

每个节点照 5.4 填。**代码节点直接粘贴第 5.4 节里给的源码。**
但「规则合并检查」的 20451 字符源码太长，本文档没收录全文 ——
请从 `dify_workflow_graph.json` 里取：

```python
import json
g = json.load(open("dify_workflow_graph.json", encoding="utf-8"))
for n in g["nodes"]:
    if n["data"].get("title") == "规则合并检查":
        open("rule_check.py", "w", encoding="utf-8").write(n["data"]["code"])
```

或者直接用 `dify/rule_node_final.py`（614 行精简版，逻辑等价）。

### 第 5 步：改数据集绑定

新建的知识库 ID 和你看到的不一样。四个检索节点要重新选一次数据集。

### 第 6 步：发布并拿密钥

右上角「发布」→ 左侧「访问 API」→ 复制 `app-` 开头的密钥。

### 第 7 步：接到后端

密钥填进 `.env` 的 `DIFY_API_KEY`，重启服务，`/api/health` 看 `configured: true`。

## 5.6 发布时的坑（重要）

**⚠️ 发布会覆盖服务端草稿修改。**

如果 Dify 画布标签页在服务端草稿改动**之前**就打开了，
点「发布」会用画布里的旧状态覆盖掉服务端的新草稿。

**正确顺序：**
1. 先做完所有草稿修改
2. 刷新 / 重新聚焦画布
3. 立刻发布

**回滚手段：** Dify 有「版本历史」按钮，可以回退到之前的版本。

## 5.7 验收清单

搭完之后逐条验证：

- [ ] 正常场景返回 `candidates` 非空，`ranking_version` 是 `ranker-v6`
- [ ] 选「不饮酒」后，酒类不出现在候选里
- [ ] 预算 100–300 时，低于 100 和高于 300 的都不出现
- [ ] 预算上限 180 时，超过 180 的不出现
- [ ] 缺少发货地/收货地时，时效为 null，候选仍保留并提示查询
- [ ] 不能解析时返回 `unknown`，不静默猜测
- [ ] 关掉 Dify（改错密钥）后，本地规则模式仍能出 30 张卡
- [ ] `/api/health` 的 `configured` 字段正确反映状态

---
