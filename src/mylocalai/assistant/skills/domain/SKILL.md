<!--
技能名：identify-domain（领域识别，取证三技能之一）
职责：只根据用户原文判断"这段内容在谈什么"，给出主/次领域、核心议题标签、地区、时间与分类状态。
输入：用户原文（原文会原样送入，不做改写）。
输出（JSON，键名锁定）：{
  "state": "ok|insufficient|ambiguous|uncovered|multi_topic",
  "primary": "<taxonomy 里的领域 id>",
  "secondary": ["<领域 id>", ...],
  "region": "CN|GLOBAL|US|EU|OTHER|''",
  "time": "<原文里的时间锚点，没有就空串>",
  "labels": ["<白名单标签>", ...],
  "basis": ["<原文里的判断依据片段>", ...]
}
边界：不判真假、不联网、不看立场情绪；不做任何评分；不做检索。
与代码校验的关系：本指令输出会被代码白名单/定位校验，不通过即丢弃（见文末）。
-->

# 领域识别（identify-domain）

## 1. 这个技能在回答什么

只回答一个问题：**这段内容在谈什么**。它是取证三技能的第一阶段，产出供第二阶段
选站使用的领域/地区/议题信息。它不判断内容真伪，不搜索，不评估立场情绪，不给分，
也不对"该去哪里查"提出建议——那是第二阶段的事。

领域识别的产物是后续两个阶段的输入：`primary` / `secondary` 决定信源目录按哪些领域
过滤，`region` 决定按哪些地区过滤，`labels` 是议题层的关键词（也用于构造中性的机构
搜索词）。产物指不回原文，后面两步就都是在猜。

## 2. 分类口径

1. **先读完整原文再分类**，不要被开头一句话带跑；复合内容按主要议题定 primary，
   其余有实质内容的议题进 secondary（最多 2 个）。
2. **primary 只能取 taxonomy 里的领域 id**（public_health / food_safety / technology /
   finance / policy_law / education / social_events / environment / history /
   international / celebrity_entertainment / other）。拿不准归哪一个时用 `other`，
   并在 basis 里说明为什么归不进去，不要硬造一个像领域的词。
3. **labels 是议题层标签**，同样只能取 taxonomy 白名单（领域 id 或各领域的 examples
   主题词）。labels 为空等于没有识别出议题，代码会据此要求用户补充信息。
4. **region 只取代码**：CN（国内）、GLOBAL（跨国/全球）、US、EU、OTHER；原文没有
   地区指向时给空串，不要猜。
5. **time 抄原文里的时间锚点**（"2026年11月""上周三"），没有就给空串；不要根据
   发布渠道或常识补一个时间。
6. **basis 必须是从原文里逐字摘出的片段**（每条 6-60 字），是"我凭什么把它归到
   这个领域"的依据。basis 会被代码拿回原文做逐字定位，定位不到就整条丢弃。

## 3. 分类状态 state（四态必须如实）

| state | 含义 | 代码的后续动作 |
|---|---|---|
| `ok` | 领域可判定 | 进入第二阶段选站 |
| `insufficient` | 原文信息不足（太短/只是情绪/没有议题） | needs_clarification，不检索 |
| `ambiguous` | 领域可判但主要议题不清，检索会发散 | needs_clarification，不检索 |
| `uncovered` | 领域不在 taxonomy 覆盖范围内 | needs_clarification，不检索 |
| `multi_topic` | 多个议题分量相当，无法定主次 | needs_clarification，不检索 |

四态不是失败，是"先问再查"：把缺什么写进 basis/gap，让用户补一句比盲目检索更省。
把 `insufficient` 报成 `ok` 是更坏的选择——那会让后面两个阶段在错误的方向上花钱。

## 4. 字段口径明细

| 字段 | 这样做 | 不这样做 |
|---|---|---|
| `primary` | 只填 taxonomy 里的领域 id | 填"民生""健康养生"这类自造领域名 |
| `secondary` | 只填与 primary 不同、且在表内的领域 id | 把同一领域换个说法再填一遍 |
| `region` | 只填五个地区代码或空串 | 填"江浙沪""北方"这类无法用于过滤的词 |
| `time` | 原文写了什么抄什么 | 补一个"近期"当作时间锚点 |
| `labels` | 议题层的白名单主题词（如"疫苗""水质"） | 填立场词（"有害""安全"）或自造词 |
| `basis` | 逐字片段，能指回原文 | 写"综合判断""根据经验"这类指不回原文的话 |

## 5. 边界案例

| # | 输入 | 这样做 | 不这样做 |
|---|---|---|---|
| 1 | "网传某小区自来水有问题，大家千万别喝" | 领域 social_events 或 environment，labels 取"水质"类白名单词，basis 摘"自来水有问题"；缺具体地点时间 → state=ambiguous | 因为带"网传/千万别"就判成谣言并给情绪标签 |
| 2 | "啊啊啊太离谱了！！" | state=insufficient，labels=[]，basis=[] | 硬给一个领域凑出可检索输出 |
| 3 | "隔壁实验室又说发现了新粒子，还上了Nature" | 领域 technology，secondary 可留空；不确定的机构名不进 labels | 把"某实验室"自造一个标签名写进 labels |
| 4 | 同一段里既讲疫苗又讲股市暴跌 | state=multi_topic，basis 分别摘两处 | 强行选一个领域当 primary，把另一个议题悄悄丢掉 |
| 5 | 一段纯广告/招聘/寻人启事 | state=insufficient 或 primary=other，basis 说明"无可核查议题" | 为了能继续跑而给一个看似相关的领域 |

## 6. 与代码校验的关系（重要）

本指令的输出**不直接进入流水线结果**，代码会逐项校验：

- `primary` / `secondary` / `labels` 必须命中 taxonomy 白名单，否则记 error 并丢弃
  该值；labels 被丢空 → 整个运行以 needs_clarification 结束；
- 每条 `basis` 必须能在原文中逐字定位（归一化后），定位不到 → 记 error 并丢弃；
- `state` 只接受上表五个取值，出现别的值按歧义处理（needs_clarification）；
- 模型没有可用（未配置/调用失败）时，阶段一直接判失败，**不会**用规则兜底猜一个
  领域——没有分类就没有选站依据，盲目检索比不检索更糟。

> 记忆点：本技能只做"谈什么"的判断依据整理，判断依据必须能在原文指出来；
> 指不出来的一切结论，代码都会当成没说过。
