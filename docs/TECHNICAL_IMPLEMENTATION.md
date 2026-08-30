# Veritas 技术实现文档

> 文档职责：记录可执行技术规格、数据契约、算法、测试与实际验证结果。  
> 当前阶段：M1-2 抽取 pipeline 与校准
> 当前状态：M1-2 in progress；M1-2A/M1-2B/M1-2B2 complete；M1-2C next
> 更新日期：2026-08-30  
> 上位设计：[Veritas 初期项目设计文档](<../Veritas-Initial-Design(2).md>)  
> 配套文档：[项目结构与设计文档](PROJECT_STRUCTURE.md)

## 1. 当前阶段目标

M1-2 把 M1-1 的检索与 LLM 协议连接成可校准的 source-grounded extraction 边界，同时保持 P0 的版本、谱系和幂等性由确定性代码控制。

阶段拆分：

- **M1-2A（已完成）**：严格 JSON schema、逐字引用对齐、确定性 Evidence/Claim/edge 候选、10 题 gold dataset 与 fixture baseline；
- **M1-2B（已完成）**：抽取 Failure Taxonomy、独立负向校准、指标与 CI gate 硬化；
- **M1-2B2（已完成）**：benchmark 扩容至 30 题（多断言、contradicts、as_of 版本视图），fixtures 由确定性脚本生成；
- **M1-2C（未开始）**：在不暴露凭据的前提下录制真实 provider 输出，与同一冻结 fixture/gold truth 对照并作阶段评审。

M1-2 的最终退出仍要求真实 LLM 校准记录；M1-2A 的 fixture 10/10 只证明契约与评测链路可复现。

## 2. 本阶段非目标

M1-2A 不包含：

- Web Search；
- 真实 LLM smoke test 或模型质量结论；
- 把候选 Evidence/Claim 持久化进 P0 SQLite 图；
- 向量检索；
- 动态规划；
- 多 Agent 或并行 Worker；
- PostgreSQL、图数据库或服务部署；
- 自由文本报告生成；
- TF-IDF 排序调优或语义检索；
- 对真实互联网内容的自动变化检测与抓取；
- Research Runtime、checkpoint、预算控制与动态重规划；
- 多进程、规模、性能或成本收益验证。

这些能力属于 M1-2B/M1-2C 或后续阶段，不能从确定性 fixture baseline 中外推。

## 3. 核心不变量

后续实现必须始终保持以下不变量。

### I-01：版本不可变

`SourceVersion`、`EvidenceSpan`、`ClaimAssessment` 和 `ConclusionVersion` 一旦写入，不原地修改。更新通过创建新版本或新评估记录完成。`Claim` 是稳定的命题身份，其 statement 也不能原地改写。

### I-02：传播只产生候选影响

依赖图传播得到的是 `candidate_impact`，含义是“需要重新验证”，不能直接把节点判为错误或失效。

### I-03：语义验证决定确认失效

只有重新计算当前有效证据后，节点状态确实发生语义变化，才能进入 `confirmed_invalidation`。

### I-04：未受影响内容保持不变

没有进入候选集合的节点不得被重算；重新验证后语义状态未变化的结论不得创建无意义的新版本。

### I-05：旧版本永不丢失

新结论只能 supersede 旧结论，不能覆盖或删除旧结论。系统必须能够回答旧结论当时依赖了哪些来源版本。

### I-06：ChangeEvent 幂等

同一 `(project_id, external_event_id)` 只能产生一次状态迁移。重复执行必须返回已有结果，不能重复创建图节点或结论版本。

### I-07：P0 结果确定性

给定相同输入快照、ChangeEvent 和规则版本，输出节点集合、状态、版本号与指标必须一致。

### I-08：场景快照身份不可漂移

`(scenario_id, scenario_version, input_snapshot_id)` 只能登记一个 T0 hash。相同 hash 可幂等加载；同一身份出现不同 hash 必须拒绝。未登记的非空数据库不能被当成新鲜 T0 继续初始化。

## 4. 最小领域模型

P0-0 使用八类持久对象。字段名是后续 JSON 和 Python 模型的基线，只有通过记录设计决策才能变更。

### 4.1 SourceVersion

某个来源内容的不可变快照。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `source_id` | string | 跨版本稳定的来源身份 |
| `version_id` | string | 全局唯一 |
| `version_label` | string | 来源自己的版本标识 |
| `canonical_uri` | string | 规范化来源位置 |
| `content_hash` | string | 内容哈希；同一哈希不得重复入库 |
| `published_at` | datetime | 来源发布时间，可为空 |
| `observed_at` | datetime | Veritas 获取该版本的时间 |
| `valid_from` | datetime | 该版本在现实中开始有效的时间 |
| `valid_to` | datetime | 当前仍有效时为空 |
| `supersedes_version_id` | string | 被替代的旧版本，可为空 |

### 4.2 EvidenceSpan

来源中可精确定位、可验证的证据跨度。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `evidence_id` | string | 全局唯一 |
| `source_version_id` | string | 必须引用现存 SourceVersion |
| `locator` | object | 页码、段落、标题或字符区间 |
| `text` | string | P0 fixture 中保存原文 |
| `text_hash` | string | 用于完整性检查 |
| `normalized_assertion` | string | 确定性 fixture 给出的规范化断言 |
| `valid_from` | datetime | 该证据断言开始有效的时间 |
| `valid_to` | datetime | 被替代或撤回后结束 |

P0 不单独建立 `Fact` 节点。`EvidenceSpan.normalized_assertion` 暂时承担来源断言的作用。只有后续实验能证明 Fact 层带来独立价值时才引入，避免在验证前增加一层重复语义。

### 4.3 Claim

系统可支持、反驳和重新评估的稳定原子命题。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `claim_id` | string | 全局唯一、跨评估稳定的语义身份 |
| `statement` | string | 单一、可判定命题 |
| `created_at` | datetime | 命题首次建立时间 |
| `canonical_key` | string | 用于 fixture 去重的规范化键 |

同一 `claim_id` 的命题内容不能变化。“默认重试次数为 3”和“默认重试次数为 1”必须是两个 Claim。

### 4.4 ClaimAssessment

Claim 在某个证据快照下的不可变评估记录。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `assessment_id` | string | 全局唯一 |
| `claim_id` | string | 必须引用现存 Claim |
| `snapshot_id` | string | 本次评估使用的证据快照 |
| `assessment` | enum | `accepted` / `unsupported` / `contradicted` / `conflict` |
| `rule_version` | string | 产生评估结果的规则版本 |
| `reason_refs` | list[string] | 支持评估的证据或边 ID |
| `reasoned_at` | datetime | 形成该版本评估的时间 |
| `supersedes_assessment_id` | string | 同一 Claim 的上一评估，可为空 |

即使 assessment 枚举值没有变化，只要有效证据集合发生变化，也要创建新的 ClaimAssessment，以保留“为什么现在仍然成立”的当前 provenance。这种情况记录为 `rechecked_unchanged`，不计入 confirmed invalidation。

### 4.5 ConclusionVersion

某个研究结论在特定证据快照下的不可变版本。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `conclusion_key` | string | 跨版本稳定的结论身份 |
| `conclusion_version_id` | string | 全局唯一 |
| `statement` | string | 结构化模板生成的结论文本 |
| `outcome` | enum | `pass` / `fail` / `unknown` / `conflict` |
| `dependency_rule` | object | P0 支持 `all` / `any` |
| `reason_refs` | list[string] | 形成该结论时使用的 ClaimAssessment ID |
| `reasoned_at` | datetime | 形成结论的时间 |
| `supersedes_conclusion_version_id` | string | 上一结论版本，可为空 |

Conclusion 的 dependency rule 引用稳定 `claim_id`。如果 ClaimAssessment 更新但 conclusion outcome 与 statement 均不变化，P0 不创建新的 ConclusionVersion；新的评估依据保存在 EvolutionRun 中。只有语义结果变化时才产生新的 ConclusionVersion。

### 4.6 DependencyEdge

边方向统一为“前置依据 → 依赖结果”，以便变化从来源方向向结论方向传播。

| 类型 | from | to | P0 语义 |
| --- | --- | --- | --- |
| `supports` | EvidenceSpan | Claim | 有效证据支持该命题 |
| `contradicts` | EvidenceSpan | Claim | 有效证据反驳该命题 |
| `depends_on` | Claim | ConclusionVersion | 结论依赖该主张 |
| `supersedes` | 新版本节点 | 旧版本节点 | 版本谱系，不参与正向影响传播 |

每条边还必须包含：`edge_id`、`created_at`、`valid_from`、`valid_to` 和 `rule_version`。

### 4.7 ChangeEvent

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `change_event_id` | string | 内部唯一 ID |
| `external_event_id` | string | 幂等键的一部分 |
| `project_id` | string | 幂等键的一部分 |
| `change_type` | enum | `revise` / `retract` / `expire` / `conflict` / `supersede` |
| `old_source_version_id` | string | 旧来源版本 |
| `new_source_version_id` | string | 新版本；撤回时可为空 |
| `changed_locators` | list[object] | 已知变化位置；未知时为空并退化为整份来源 |
| `observed_at` | datetime | 系统收到事件的时间 |
| `effective_at` | datetime | 变化在现实中生效的时间 |

### 4.8 EvolutionRun

记录一次变化处理的输入、输出和规则版本，用于 replay 与指标计算。

必须包含：`run_id`、`change_event_id`、`input_snapshot_id`、`rule_version`、`candidate_impact`、`reverification_results`、`confirmed_invalidations`、`created_versions`、`untouched_nodes`、`metrics` 和 `trace_refs`。

## 5. P0 评估规则

### 5.1 Claim 评估

只计算当前有效、且连接到当前有效 SourceVersion 的证据边。

| 有效支持证据 | 有效反驳证据 | assessment |
| --- | --- | --- |
| 至少 1 条 | 0 条 | `accepted` |
| 0 条 | 0 条 | `unsupported` |
| 0 条 | 至少 1 条 | `contradicted` |
| 至少 1 条 | 至少 1 条 | `conflict` |

P0 不使用来源权重或 LLM 仲裁。`conflict` 必须保留，不能自动选择一个来源。

### 5.2 Conclusion 评估

- `all`：所有依赖 Claim 都为 `accepted` 时满足；
- `any`：至少一个依赖 Claim 为 `accepted` 时满足；
- 依赖中出现 `conflict`，结论为 `conflict`；
- 不满足规则且没有冲突时，由场景的确定性 conclusion rule 输出 `fail` 或 `unknown`。

自由文本生成不属于 P0。结论文本由 scenario fixture 中的模板产生。

### 5.3 候选与确认集合

必须分别输出：

- `candidate_impact`：沿当前有效 `supports`、`contradicts`、`depends_on` 边可达、需要重新验证的节点；
- `rechecked_unchanged`：被重新验证但语义状态未变化的 Claim 或 Conclusion；
- `confirmed_invalidations`：旧状态与新状态发生语义变化的节点；
- `untouched_nodes`：不在候选影响范围内、因此没有重算的节点。

`supersedes` 仅用于查看版本历史，不用于扩大候选集合。

## 6. 变化处理算法

后续 `propagate_and_repair` 实现必须遵守以下顺序：

1. 使用 `(project_id, external_event_id)` 检查 ChangeEvent 是否已处理；
2. 校验旧版本存在、新版本哈希与版本关系合法；
3. 固定变更前 `input_snapshot`，后续候选传播必须基于这个快照；
4. 根据 `changed_locators` 在 input snapshot 中选择旧 EvidenceSpan；位置未知时选择旧版本的全部 EvidenceSpan；
5. 从选中的 EvidenceSpan 沿 input snapshot 的依赖方向广度优先遍历，形成 `candidate_impact`；
6. 追加新 SourceVersion，并用 `supersedes_version_id` 使旧版本退出 current view；旧 SourceVersion、EvidenceSpan 和边不原地修改；随后载入新 EvidenceSpan 与边；
7. 只重新评估候选 Claim，生成新的 ClaimAssessment；新命题则先创建 Claim；
8. 对 assessment 未变化的 Claim 记录为 `rechecked_unchanged`；
9. 只重新计算依赖状态发生变化的候选 Conclusion；
10. 只有 outcome 或语义内容变化时才创建新 ConclusionVersion；
11. 输出新旧结论 diff、影响集合、指标和 trace；
12. 原子提交 EvolutionRun；重复事件直接返回已有 EvolutionRun。

伪代码：

```python
def propagate_and_repair(event, graph, rules):
    if prior_run := graph.find_run(event.idempotency_key):
        return prior_run

    changed_evidence = graph.resolve_changed_evidence(event)
    candidate_impact = graph.walk_dependents(changed_evidence)
    graph.activate_new_source_version(event)

    claim_results = reverify_candidate_claims(candidate_impact.claims, graph, rules)
    changed_claims = select_semantic_changes(claim_results)
    conclusion_results = recompute_dependents(changed_claims, graph, rules)

    return commit_evolution_run(
        event=event,
        candidate_impact=candidate_impact,
        claim_results=claim_results,
        conclusion_results=conclusion_results,
    )
```

## 7. 黄金样例 GS-001

### 7.1 研究问题

> Atlas SDK 是否满足团队“默认至少自动重试 3 次”的架构策略，并且能否部署到 Python 3.11？

该名称和内容均为本地模拟数据，不对应真实产品。

### 7.2 T0 来源与证据

| 来源版本 | EvidenceSpan | 规范化断言 |
| --- | --- | --- |
| `SRC_API@1.0` | `EV_API_RETRY@1` | Atlas 支持自动重试 |
| `SRC_API@1.0` | `EV_API_DEFAULT@1` | Atlas 默认重试次数为 3 |
| `SRC_RETRY_GUIDE@1.0` | `EV_GUIDE_RETRY@1` | Atlas 支持瞬时故障自动重试 |
| `SRC_RUNTIME_GUIDE@1.0` | `EV_RUNTIME_PY311@1` | Atlas 支持 Python 3.11 |
| `SRC_TEAM_POLICY@1.0` | `EV_POLICY_MIN_RETRY@1` | 团队策略要求默认至少重试 3 次 |

### 7.3 T0 Claim

| claim_key | statement | 支持证据 | assessment |
| --- | --- | --- | --- |
| `retry_supported` | Atlas 支持自动重试 | API + Retry Guide | `accepted` |
| `default_retries_3` | Atlas 默认重试次数为 3 | API | `accepted` |
| `python_311_supported` | Atlas 支持 Python 3.11 | Runtime Guide | `accepted` |
| `policy_min_retries_3` | 团队要求默认至少重试 3 次 | Team Policy | `accepted` |

### 7.4 T0 Conclusion

| conclusion_key | 依赖 | outcome | statement |
| --- | --- | --- | --- |
| `retry_policy_fit` | retry_supported + default_retries_3 + policy_min_retries_3 | `pass` | Atlas 默认重试策略满足团队要求 |
| `python_311_compatible` | python_311_supported | `pass` | Atlas 可部署到 Python 3.11 |

### 7.5 T1 ChangeEvent

`SRC_API@1.1` 替代 `SRC_API@1.0`：

- 自动重试能力仍然存在；
- 默认重试次数从 3 改为 1。

新证据：

| EvidenceSpan | 规范化断言 | 关系 |
| --- | --- | --- |
| `EV_API_RETRY@2` | Atlas 支持自动重试 | supports `retry_supported` |
| `EV_API_DEFAULT@2` | Atlas 默认重试次数为 1 | supports `default_retries_1`；contradicts `default_retries_3` |

### 7.6 预期候选影响

```json
{
  "change_event_id": "CHG_API_1_0_TO_1_1",
  "evidence_spans": [
    "EV_API_RETRY@1",
    "EV_API_DEFAULT@1"
  ],
  "claims": [
    "retry_supported",
    "default_retries_3"
  ],
  "conclusions": [
    "retry_policy_fit"
  ]
}
```

`python_311_supported`、`policy_min_retries_3` 和 `python_311_compatible` 不属于候选影响集合。

### 7.7 预期重新验证结果

| 节点 | T0 | T1 | 处理 |
| --- | --- | --- | --- |
| `retry_supported` | accepted | accepted | 重新验证后不变；不算失效 |
| `default_retries_3` | accepted | contradicted | 确认失效 |
| `default_retries_1` | 不存在 | accepted | 创建新 Claim |
| `retry_policy_fit` | pass | fail | 创建 Conclusion v2，supersede v1 |
| `python_311_compatible` | pass | 未重算 | 保持 v1 |

新的 `retry_policy_fit` 结论文本为：

> Atlas 仍支持自动重试，但默认次数为 1，不满足团队默认至少重试 3 次的策略；需要显式配置。

### 7.8 预期结构化输出

```json
{
  "rechecked_unchanged": ["retry_supported"],
  "confirmed_invalidations": [
    {
      "node_key": "default_retries_3",
      "old_state": "accepted",
      "new_state": "contradicted"
    },
    {
      "node_key": "retry_policy_fit",
      "old_state": "pass",
      "new_state": "fail"
    }
  ],
  "created_claims": ["default_retries_1"],
  "created_claim_assessments": [
    "retry_supported@assessment-2",
    "default_retries_3@assessment-2",
    "default_retries_1@assessment-1"
  ],
  "created_conclusions": ["retry_policy_fit@2"],
  "untouched_nodes": [
    "python_311_supported",
    "policy_min_retries_3",
    "python_311_compatible@1"
  ]
}
```

### 7.9 预期结论差异

`conclusion_diff.json` 至少包含：

```json
{
  "change_event_id": "CHG_API_1_0_TO_1_1",
  "conclusion_key": "retry_policy_fit",
  "old_version": {
    "conclusion_version_id": "retry_policy_fit@1",
    "outcome": "pass",
    "statement": "Atlas 默认重试策略满足团队要求"
  },
  "changed_evidence": [
    "EV_API_DEFAULT@1",
    "EV_API_DEFAULT@2"
  ],
  "affected_claims": [
    "default_retries_3",
    "default_retries_1"
  ],
  "new_version": {
    "conclusion_version_id": "retry_policy_fit@2",
    "outcome": "fail",
    "statement": "Atlas 仍支持自动重试，但默认次数为 1，不满足团队默认至少重试 3 次的策略；需要显式配置。"
  },
  "change_reason": "source_revision",
  "action_required": true
}
```

未发生语义变化的 `python_311_compatible` 不出现在 conclusion diff 中。

## 8. Ground Truth 与指标

GS-001 固定三个真值集合：

```text
GT_REVERIFY = {retry_supported, default_retries_3, retry_policy_fit}
GT_SEMANTIC_CHANGE = {default_retries_3, retry_policy_fit}
GT_UNTOUCHED = {python_311_supported, policy_min_retries_3, python_311_compatible}
```

P0 指标按 Claim 和 Conclusion 节点计算，不把 SourceVersion、EvidenceSpan 自身计入分母。

| 指标 | 定义 | GS-001 目标 |
| --- | --- | --- |
| Candidate Impact Precision | candidate 与 GT_REVERIFY 交集 / candidate | 1.0 |
| Candidate Impact Recall | candidate 与 GT_REVERIFY 交集 / GT_REVERIFY | 1.0 |
| Invalidation Precision | confirmed 与 GT_SEMANTIC_CHANGE 交集 / confirmed | 1.0 |
| Invalidation Recall | confirmed 与 GT_SEMANTIC_CHANGE 交集 / GT_SEMANTIC_CHANGE | 1.0 |
| Unaffected Preservation | GT_UNTOUCHED 中没有产生新语义版本的比例 | 1.0 |
| Repair Success | 新结论 outcome 与期望一致 | 1.0 |
| Conclusion Recompute Ratio | 实际重算结论 / 全部当前结论 | 0.5 |
| Full Recompute Baseline | 全量基线重算结论 / 全部当前结论 | 1.0 |
| Replay Determinism | 相同输入输出哈希一致 | true |
| Event Idempotency | 同一事件重复执行不增加节点数 | true |

`Recompute Ratio` 不能单独代表成功；只有在 `Repair Success = 1.0` 时才用于比较效率，防止系统通过“什么都不做”获得低重算比例。

## 9. 输出文件契约

P0-1 的每个 scenario run 产生：

```text
artifacts/<scenario_id>/<run_id>/
├── candidate_impact.json
├── confirmed_invalidations.json
├── conclusion_diff.json
├── trace.json
└── metrics.json
```

文件必须包含 `scenario_version`、`rule_version`、`input_snapshot_hash` 和 `output_hash`。聚合报告不能代替逐任务 JSON。

## 10. Trace 最小事件

P0-1 实际记录：

1. `change_event_received`
2. `candidate_impact_computed`
3. `source_version_activated`
4. `old_evidence_expired`（表示因 source supersession 从 current view 失活，不修改旧行）
5. `claim_reverified`
6. `claim_state_created`、`claim_state_changed` 或 `claim_state_unchanged`
7. `conclusion_recomputed`
8. `conclusion_version_created`
9. `evolution_run_committed`

P0-2B 起，`retract` 用 `source_version_retracted` 替代第 3 步（追加式 ChangeEvent，不修改旧行）。P0-3 新增两种事件类型（不改变已有事件的语义与顺序）：

- `source_version_expired`：替代第 3 步，用于 expire（追加式 ChangeEvent，与 retract 共用 current-view 排除机制）；
- `conflict_source_recorded`：替代第 3 步，用于 conflict；记录独立来源进入证据池且不 supersede 旧来源。conflict 事件不产生 `old_evidence_expired`，因为没有证据退出 current view。

每条事件必须带 `run_id`、`event_seq`、`timestamp`、`entity_refs`、`rule_version` 和结构化 `reason`。

## 11. P0-0 验收断言

- [x] A-01：明确区分 candidate impact 与 confirmed invalidation；
- [x] A-02：来源更新不会直接把下游结论判错；
- [x] A-03：冗余证据使 `retry_supported` 在重新验证后仍成立；
- [x] A-04：默认重试数变化使 `default_retries_3` 确认失效；
- [x] A-05：只重算 `retry_policy_fit`；
- [x] A-06：`python_311_compatible` 不创建新版本；
- [x] A-07：旧结论通过 supersedes 谱系保留；
- [x] A-08：定义 ChangeEvent 幂等规则；
- [x] A-09：定义逐任务输出与可重放信息；
- [x] A-10：指标同时约束正确性、未影响保护和重算成本。

## 12. P0-1 实现与验证

### 12.1 运行环境

| 项目 | 当前验证值 |
| --- | --- |
| Python | 3.14.5 |
| SQLite | 3.50.4 |
| 外部运行依赖 | 无 |
| 测试框架 | 标准库 `unittest` |

环境中没有安装 pytest。本阶段没有为此下载依赖，测试使用标准库完成。

### 12.2 实际实现位置

| 能力 | 实际文件 |
| --- | --- |
| 八类领域对象与校验 | [`domain/models.py`](../src/veritas/domain/models.py)、[`domain/enums.py`](../src/veritas/domain/enums.py) |
| T0 图与候选影响传播 | [`evidence/graph.py`](../src/veritas/evidence/graph.py)、[`invalidation/impact.py`](../src/veritas/invalidation/impact.py) |
| Claim 与 Conclusion 规则 | [`evidence/rules.py`](../src/veritas/evidence/rules.py) |
| 追加式 SQLite 存储、事务和查询 | [`storage/sqlite.py`](../src/veritas/storage/sqlite.py) |
| ChangeEvent、重新验证与选择性修复 | [`invalidation/repair.py`](../src/veritas/invalidation/repair.py) |
| fixture 装载与 input snapshot hash | [`evaluation/scenario.py`](../src/veritas/evaluation/scenario.py) |
| 指标、全量重算对照与 artifacts | [`evaluation/metrics.py`](../src/veritas/evaluation/metrics.py)、[`evaluation/runner.py`](../src/veritas/evaluation/runner.py) |
| GS-001 数据与 Ground Truth | [`scenario.json`](../datasets/scenarios/GS-001/scenario.json) |
| 单元与端到端测试 | [`test_domain_and_graph.py`](../tests/unit/test_domain_and_graph.py)、[`test_gs001.py`](../tests/scenarios/test_gs001.py) |

### 12.3 运行命令

PowerShell：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONDONTWRITEBYTECODE='1'
python -m veritas.evaluation.runner
python -m unittest discover -s tests -p 'test_*.py' -v
```

默认运行产物：

```text
artifacts/GS-001/run-f39dacf198a857ae/
├── candidate_impact.json
├── confirmed_invalidations.json
├── conclusion_diff.json
├── metrics.json
└── trace.json
```

### 12.4 实际状态演化

一次新鲜运行后 SQLite 中的实体数量：

| 表 | 数量 |
| --- | ---: |
| SourceVersion | 5 |
| EvidenceSpan | 7 |
| Claim | 5 |
| ClaimAssessment | 7 |
| ConclusionVersion | 3 |
| DependencyEdge | 15 |
| ChangeEvent | 1 |
| EvolutionRun | 1 |

关键结果：

- `retry_supported` 生成 assessment-2，仍为 `accepted`，同时引用 Retry Guide 旧证据与 API v1.1 新证据；
- `default_retries_3` 从 `accepted` 变为 `contradicted`；
- 新建 `default_retries_1`，状态为 `accepted`；
- `retry_policy_fit@2` supersede `retry_policy_fit@1`，outcome 从 `pass` 变为 `fail`；
- `python_311_compatible` 只有 v1，没有被重算；
- API v1.0 行仍保留，API v1.1 通过 `supersedes_version_id` 指向 v1.0。

### 12.5 实际指标

指标来自 [`metrics.json`](../artifacts/GS-001/run-f39dacf198a857ae/metrics.json)，不是手工估计：

| 指标 | 结果 |
| --- | ---: |
| Candidate Impact Precision | 1.0 |
| Candidate Impact Recall | 1.0 |
| Invalidation Precision | 1.0 |
| Invalidation Recall | 1.0 |
| Unaffected Preservation | 1.0 |
| Repair Success | true |
| Selective Recompute Ratio | 0.5 |
| Full Recompute Ratio | 1.0 |
| Full Recompute Equivalent | true |
| Replay Determinism | true |
| Event Idempotency | true |

重复执行 CLI 后，五个 artifact 文件的 SHA-256 文件哈希变化数为 0。

### 12.6 测试结果

最终验证：

```text
Ran 11 tests in 0.490s
OK
```

覆盖范围包括：

- 领域时间字段校验与 project-scoped idempotency key；
- T0 candidate impact 精确集合；
- 冗余证据重新验证但不误判失效；
- ClaimAssessment 状态变化；
- 只给受影响 Conclusion 创建 v2；
- 旧 SourceVersion 追加式保留；
- ChangeEvent 重放不增加实体；
- 相同 idempotency key 遇到 scenario drift 时拒绝复用；
- conclusion diff 的证据与 Claim 定位；
- 五类 artifact 完整性与 content hash；
- trace 必需事件及顺序；
- selective 结果与 full recompute 等价。

## 13. P0-2A 场景与失败规格

### 13.1 P0 Evolution Suite 不变量

P0-2 使用 suite ID `p0-evolution-suite-1.0.0`。三个场景遵守：

1. GS-001、GS-002、GS-003 各自从独立 T0 fixture 和空数据库开始，不能串行继承另一场景的 T1；
2. 三个场景继续使用本地模拟 Atlas 文档，不联网、不调用 LLM；
3. runtime 可以读取 scenario input，但不能读取 `ground_truth`；
4. 每个场景分别产生五类 artifacts，suite runner 只聚合逐场景结果；
5. 现有 GS-001 v1.0.0、`p0-rules-1` 和历史 artifacts 保持不变；
6. GS-002/GS-003 使用 `p0-rules-2`，新增语义不能静默写入旧 rule version；
7. benchmark 指标仍只计算 Claim 与 Conclusion，不把 SourceVersion 和 EvidenceSpan 自身计入 semantic-change 分母。

### 13.2 GS-002：来源撤回但冗余证据仍成立

#### 目的

验证：来源被撤回只意味着其证据退出 current view，不能直接把仍有独立支持的 Claim 或 Conclusion 判为失效。

GS-002 从与 GS-001 相同语义的 T0 独立开始，但撤回的是只提供 retry 证据的 `SRC_RETRY_GUIDE@1.0`。不能撤回同时包含默认重试数的 `SRC_API@1.0`，否则场景会同时改变 `default_retries_3`，失去单变量控制。

#### ChangeEvent

```json
{
  "scenario_id": "GS-002",
  "scenario_version": "1.0.0",
  "rule_version": "p0-rules-2",
  "change_event_id": "CHG_RETRY_GUIDE_RETRACT_1_0",
  "external_event_id": "atlas-retry-guide-retraction-1.0",
  "project_id": "atlas-architecture-review",
  "change_type": "retract",
  "old_source_version_id": "SRC_RETRY_GUIDE@1.0",
  "new_source_version_id": null,
  "changed_locators": [
    {"section": "automatic-retry"}
  ],
  "observed_at": "2026-08-20T00:00:00Z",
  "effective_at": "2026-08-19T00:00:00Z"
}
```

撤回采用追加式语义：旧 SourceVersion、EvidenceSpan 和边保持不变；current-source resolver 根据 retract ChangeEvent 将该来源版本排除。禁止通过更新旧行 `valid_to` 表达撤回。

#### T1 重新验证

`EV_GUIDE_RETRY@1` 失活后，`retry_supported` 仍由 `EV_API_RETRY@1` 支持：

| 节点 | T0 | T1 | 预期处理 |
| --- | --- | --- | --- |
| `retry_supported` | accepted | accepted | 创建 assessment-2，记录 evidence rebased；不算失效 |
| `retry_policy_fit` | pass | pass | 是 candidate，但不重算、不创建 v2 |
| `default_retries_3` | accepted | 未重算 | untouched |
| `python_311_compatible` | pass | 未重算 | untouched |

新的 `retry_supported` assessment 只能引用仍有效的 `EDGE_API_RETRY_SUPPORT@1`，不得继续引用已撤回来源的边。

#### Ground Truth

```text
GT_REVERIFY = {retry_supported, retry_policy_fit}
GT_SEMANTIC_CHANGE = {}
GT_RECHECKED_UNCHANGED = {retry_supported}
GT_UNTOUCHED = {
  default_retries_3,
  python_311_supported,
  policy_min_retries_3,
  python_311_compatible
}
```

预期 `candidate_impact`：

```json
{
  "evidence_spans": ["EV_GUIDE_RETRY@1"],
  "claims": ["retry_supported"],
  "conclusions": ["retry_policy_fit"]
}
```

预期输出约束：

- `confirmed_invalidations` 为空；
- `created_claim_assessments` 只新增 `retry_supported@assessment-2`；
- `created_conclusions` 为空；
- `recomputed_conclusions` 为空；
- `conclusion_diff.json` 使用 `{"conclusion_diffs": []}`；
- Selective Recompute Ratio 为 `0 / 2 = 0.0`；
- Full Recompute Ratio 为 `2 / 2 = 1.0`，最终 outcome 与 selective current view 一致。

当 expected 与 actual semantic-change 集合都为空时，Invalidation Precision 与 Recall 定义为 1.0；如果 actual 非空，则 Precision 为 0.0。

### 13.3 GS-003：Python 分支变化与跨子图隔离

#### 目的

验证：Runtime Guide 的变化只能传播到 Python compatibility 子图，不能把 retry Claim 或结论加入 candidate、recompute 或新版本集合。

#### ChangeEvent 与新来源

```json
{
  "scenario_id": "GS-003",
  "scenario_version": "1.0.0",
  "rule_version": "p0-rules-2",
  "change_event_id": "CHG_RUNTIME_1_0_TO_1_1",
  "external_event_id": "atlas-runtime-release-1.1",
  "project_id": "atlas-architecture-review",
  "change_type": "revise",
  "old_source_version_id": "SRC_RUNTIME_GUIDE@1.0",
  "new_source_version_id": "SRC_RUNTIME_GUIDE@1.1",
  "changed_locators": [
    {"section": "python-compatibility"}
  ],
  "observed_at": "2026-08-22T00:00:00Z",
  "effective_at": "2026-08-21T00:00:00Z"
}
```

`SRC_RUNTIME_GUIDE@1.1` 明确声明：

- Python 3.11 不再受支持；
- Python 3.12 受支持。

新增证据关系：

| EvidenceSpan | 断言 | 关系 |
| --- | --- | --- |
| `EV_RUNTIME_PY311@2` | Atlas 不再支持 Python 3.11 | contradicts `python_311_supported` |
| `EV_RUNTIME_PY312@1` | Atlas 支持 Python 3.12 | supports 新 Claim `python_312_supported` |

GS-003 使用新的确定性 rule kind `compatibility_support`：

| ClaimAssessment | Conclusion outcome |
| --- | --- |
| accepted | pass |
| contradicted | fail |
| unsupported | unknown |
| conflict | conflict |

该规则属于 `p0-rules-2`，不修改 GS-001 已记录的 `p0-rules-1` 语义。

#### T1 重新验证

| 节点 | T0 | T1 | 预期处理 |
| --- | --- | --- | --- |
| `python_311_supported` | accepted | contradicted | 确认失效 |
| `python_312_supported` | 不存在 | accepted | 新建 Claim 与 assessment-1 |
| `python_311_compatible` | pass | fail | 创建 v2，supersede v1 |
| `retry_policy_fit` | pass | 未重算 | untouched |

#### Ground Truth

```text
GT_REVERIFY = {python_311_supported, python_311_compatible}
GT_SEMANTIC_CHANGE = {python_311_supported, python_311_compatible}
GT_UNTOUCHED = {
  retry_supported,
  default_retries_3,
  policy_min_retries_3,
  retry_policy_fit
}
```

预期 `candidate_impact`：

```json
{
  "evidence_spans": ["EV_RUNTIME_PY311@1"],
  "claims": ["python_311_supported"],
  "conclusions": ["python_311_compatible"]
}
```

预期输出约束：

- confirmed invalidations 精确为 `python_311_supported`、`python_311_compatible`；
- created Claim 精确为 `python_312_supported`；
- created Conclusion 精确为 `python_311_compatible@2`；
- `retry_supported`、`default_retries_3`、`retry_policy_fit` 不得进入 candidate 或产生新版本；
- conclusion diff 的 changed evidence 为 `EV_RUNTIME_PY311@1`、`EV_RUNTIME_PY311@2`；
- Selective Recompute Ratio 为 `1 / 2 = 0.5`；
- Full Recompute Ratio 为 `2 / 2 = 1.0`，两者最终 outcome 一致。

### 13.4 Failure Taxonomy

P0-2 不增加第六类 artifact。每个 `metrics.json` 增加结构化 `failures` 数组；suite summary 聚合相同记录。

| code | 名称 | 判定 | severity |
| --- | --- | --- | --- |
| `F01_IMPACT_DETECTION` | 影响范围错误 | candidate 漏掉 GT_REVERIFY，或包含真值之外节点 | critical |
| `F02_INVALIDATION_DECISION` | 失效判断错误 | confirmed 与 GT_SEMANTIC_CHANGE 不一致 | critical |
| `F03_REPAIR_CORRECTNESS` | 修复结果错误 | current outcome 不符合真值，或与 full recompute 不一致 | critical |
| `F04_RECOMPUTE_SCOPE` | 重算越界 | untouched 节点被重算、创建新版本，或无语义变化仍重算结论 | critical |
| `F05_PROVENANCE_INTEGRITY` | 谱系断裂 | current assessment 引用失活证据、supersedes 断裂、artifact hash 不匹配 | critical |
| `F06_REPLAY_REPRODUCIBILITY` | 重放不可复现 | 重放增加实体、输出哈希漂移、snapshot/rule/idempotency 碰撞未被拒绝 | critical |

失败记录最小契约：

```json
{
  "failure_code": "F04_RECOMPUTE_SCOPE",
  "scenario_id": "GS-003",
  "severity": "critical",
  "entity_refs": ["retry_policy_fit"],
  "expected": "untouched",
  "actual": "recomputed",
  "trace_refs": ["trace-event-id"]
}
```

成功 run 的 `failures` 必须是空数组。不能只输出聚合数量而丢失失败实体与 trace 定位。

### 13.5 Suite 指标与 Gate P0 门槛

Suite runner 必须读取显式 manifest，不能通过扫描目录自动纳入场景。manifest 固定 scenario ID、scenario version、rule version 和 ground-truth hash。

同时报告：

- per-scenario 指标；
- macro average：三个场景等权；
- micro aggregate：合并 Claim/Conclusion 判定后计算；
- 每个场景和 suite 总计的 recomputed / total conclusions；
- failure taxonomy 计数与逐条记录。

P0-2 通过条件：

| 条件 | 目标 |
| --- | --- |
| 三个场景 Candidate Precision / Recall | 每项 1.0 |
| 三个场景 Invalidation Precision / Recall | 每项 1.0 |
| 三个场景 Unaffected Preservation | 每项 1.0 |
| Repair Success | 三个场景均 true |
| Full Recompute Equivalent | 三个场景均 true |
| Replay Determinism / Event Idempotency | 三个场景均 true |
| Provenance Integrity | 三个场景均通过 |
| Critical Failures | 0 |
| Aggregate Selective Recompute | `2 / 6 = 0.333333` |
| Aggregate Full Recompute | `6 / 6 = 1.0` |

只有正确性、provenance 和 replay 全部通过时，才允许用 0.333333 与 1.0 比较重算范围。

### 13.6 P0-2B 必要实现变化

以下 P0-2A 冻结的实现影响已经在 P0-2B 落地：

1. `ChangePackage.new_source`、new evidence 和 new edges 对 retract 必须可为空；
2. current-source resolver 必须同时处理 supersedes 与 retract ChangeEvent；
3. 新增 `compatibility_support` 规则，不修改 `p0-rules-1`；
4. output writer 必须支持零 conclusion diff；
5. 增加 append-only Scenario Snapshot Registry，使 T0 数据和 snapshot hash 在同一事务登记，并拒绝部分初始化漂移；
6. 增加显式 suite manifest、suite runner、macro/micro 汇总和 failure records；
7. 保留现有 GS-001 v1.0.0 与历史 artifacts；
8. storage protocol 的全面重构继续延后，不与本轮场景扩展混做。

### 13.7 P0-2A 验收断言

- [x] GS-002 只撤回单一冗余来源，不混入默认重试数变化；
- [x] GS-002 明确 candidate、空 semantic-change、rechecked 和 untouched 真值；
- [x] GS-002 明确 retract 的追加式 current-view 语义；
- [x] GS-003 只改变 Python 子图并定义跨子图隔离真值；
- [x] GS-003 的 contradicted → fail 使用新 rule version；
- [x] 两个场景均固定输出、指标与 full-recompute 对照；
- [x] 六类 failure 有机器可读契约；
- [x] suite manifest、macro/micro 指标和 Gate 条件已冻结；
- [x] P0-2B 必要改动与明确不做项已记录；
- [x] 没有修改当前 runtime、fixture、tests 或 artifacts。

## 14. P0-2B 实现与验证

### 14.1 实际实现位置

| 能力 | 实际文件 |
| --- | --- |
| GS-002 retract fixture 与真值 | [`GS-002/scenario.json`](../datasets/scenarios/GS-002/scenario.json) |
| GS-003 Python compatibility fixture 与真值 | [`GS-003/scenario.json`](../datasets/scenarios/GS-003/scenario.json) |
| 显式场景/version/rule/GT hash 锁定 | [`p0-evolution-suite.json`](../datasets/suites/p0-evolution-suite.json) |
| retract package、传播与选择性修复 | [`invalidation/repair.py`](../src/veritas/invalidation/repair.py) |
| retract/supersedes current-view 与 Snapshot Registry | [`storage/sqlite.py`](../src/veritas/storage/sqlite.py)、[`evaluation/scenario.py`](../src/veritas/evaluation/scenario.py) |
| `compatibility_support` 规则 | [`evidence/rules.py`](../src/veritas/evidence/rules.py) |
| F01～F06 逐运行失败记录 | [`evaluation/metrics.py`](../src/veritas/evaluation/metrics.py) |
| 独立场景执行、macro/micro 聚合与 artifact 校验 | [`evaluation/suite_runner.py`](../src/veritas/evaluation/suite_runner.py) |
| 新增回归测试 | [`test_gs002.py`](../tests/scenarios/test_gs002.py)、[`test_gs003.py`](../tests/scenarios/test_gs003.py)、[`test_p0_suite.py`](../tests/scenarios/test_p0_suite.py)、[`test_snapshot_registry.py`](../tests/unit/test_snapshot_registry.py) |

### 14.2 已实现语义

- `ChangePackage.new_source` 对 `retract` 可为空；撤回事件禁止夹带新 source、claim、evidence 或 edge；
- SourceVersion 与 EvidenceSpan 不原地修改，current-view 通过 append-only retract ChangeEvent 排除被撤回证据；
- 被撤回来源仍可用于历史解释，但新的 current ClaimAssessment 只能引用仍活动的 evidence edge；
- `compatibility_support` 将 accepted / contradicted / unsupported / conflict 确定性映射为 pass / fail / unknown / conflict；
- 零结论变化时仍写出结构明确的 `{"conclusion_diffs": []}`；
- Snapshot Registry 与 T0 在同一事务登记：相同身份和 hash 幂等，同身份不同 hash 拒绝，未登记的部分非空数据库拒绝；已完成的 P0-1 legacy run 只有在 scenario/version/snapshot/hash/rule 全部匹配时才允许补登记；
- 每个 `metrics.json` 保存 `failures`、`critical_failure_count`、provenance 与 replay 状态；失败记录携带 entity 与确定性 trace reference；
- suite 只读取 manifest 明列的三个场景，并为每个场景使用独立临时空数据库，防止旧 EvolutionRun 掩盖新代码行为；suite 运行产物写入自己的 versioned 目录，不覆盖 GS-001 历史逐运行 artifacts。

### 14.3 逐场景实际结果

| 场景 | Candidate P/R | Invalidation P/R | Unaffected | Repair / Full Equivalent | Selective | Full | Critical failures |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: |
| GS-001 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 2 = 0.5 | 2 / 2 = 1.0 | 0 |
| GS-002 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 0 / 2 = 0.0 | 2 / 2 = 1.0 | 0 |
| GS-003 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 2 = 0.5 | 2 / 2 = 1.0 | 0 |

三个场景的 Replay Determinism、Event Idempotency 与 Provenance Integrity 均为 true。GS-002 保持两个结论为 pass；GS-003 只把 `python_311_compatible` 从 pass 更新为 fail，retry 结论保持 pass 且没有新版本。

### 14.4 Suite 实现验证结果

正式实现验证产物为 [`summary.json`](../artifacts/suites/p0-evolution-suite-1.0.0/summary.json)：

| 聚合项 | 结果 |
| --- | ---: |
| Macro correctness metrics | 全部 1.0 |
| Micro Candidate / Invalidation / Unaffected | 全部 1.0 |
| Selective Recompute | `2 / 6 = 0.3333333333333333` |
| Full Recompute | `6 / 6 = 1.0` |
| Critical Failures | 0 |
| P0-2B acceptance candidate | true |

`p0_2b_acceptance_candidate=true` 只表示实现输出满足 P0-2A 冻结断言。summary 同时明确记录 `gate_p0_decision=not_evaluated_in_p0_2b`；不能单凭该字段宣称 P0-2C 或 Gate P0 通过，P0-2C 的附加分析见第 15 节。

### 14.5 验证命令与结果

```powershell
$env:PYTHONPATH='src'
python -m compileall -q src tests
python -m unittest discover -s tests -v
python -m veritas.evaluation.suite_runner `
  --manifest datasets/suites/p0-evolution-suite.json `
  --artifacts-root artifacts
```

最终验证：

```text
Ran 24 tests
OK
ARTIFACT_JSON_COUNT=31
ARTIFACT_HASH_MISMATCHES=0
```

新增测试覆盖 retract current-view、冗余证据 rebase、零 diff 输出、compatibility 分支隔离、精确结论版本、Snapshot Registry 幂等与漂移拒绝、部分数据库拒绝、跨 Source supersedes 谱系拒绝、manifest 锁定、suite 聚合、artifact 缺失/篡改检测。原有 P0-1 的 11 项回归仍全部通过。

### 14.6 新增正式产物

```text
artifacts/
├── GS-002/run-65365880276d316f/<five JSON artifacts>
├── GS-003/run-046dcc6b4ed54440/<five JSON artifacts>
└── suites/p0-evolution-suite-1.0.0/
    ├── runs/GS-001/<run-id>/<five JSON artifacts>
    ├── runs/GS-002/<run-id>/<five JSON artifacts>
    ├── runs/GS-003/<run-id>/<five JSON artifacts>
    └── summary.json
```

### 14.7 版本控制状态

项目根目录已经初始化为 Git 仓库，当前分支为 `main`，远程 `origin` 指向 private 仓库 [Peter-Sherlock/Veritas](https://github.com/Peter-Sherlock/Veritas)。项目 `.gitignore` 已验证会排除 `artifacts/**/*.sqlite3`、SQLite WAL/SHM、`__pycache__`、`.pyc`、`.coverage` 与 `.venv`；`.gitattributes` 将 Python、JSON、Markdown 与 TOML 固定为 LF。定向扫描未发现常见 API key、token 或 private-key 格式；首次基线提交已推送。

### 14.8 README 项目入口

根目录 [README](../README.md) 面向第一次探索项目的人：先用 GS-001 的具体变化展示输入、影响、修复与未影响结论，再提供 quick start、按目标组织的代码/fixture/artifact 导航、核心机制图、三个实验、输出契约和扩展场景入口。内部阶段历史、完整 failure 规格与详细指标继续保留在双文档；README 中出现的测试数量和重算比例来自当前测试与 suite summary。

## 15. P0-2C 正式评估与 Failure Analysis

### 15.1 评估输入与方法

P0-2C 没有改写冻结 fixture 或运行时规则。正式评估使用以下证据：

1. 显式 manifest 锁定的 GS-001～003、scenario version、rule version 与 ground-truth hash；
2. suite runner 为每个场景创建独立空 SQLite 数据库，避免历史幂等结果掩盖代码变化；
3. 选择性执行结果与同一 current-view 上的 full-recompute baseline；
4. 每个运行的五类 content-addressed artifacts 及 suite summary；
5. 基于真实 GS-001 EvolutionRun 的 F01～F06 负向注入测试，用于证明失败探测器本身可触发。

负向校准只改变传入 `evaluate_run` 的期望值或验证信号，不修改冻结场景、数据库、正式 artifacts 或运行时决策。

### 15.2 冻结验收目标与实际结果

| 验收项 | 冻结目标 | 实际结果 | 结论 |
| --- | --- | --- | --- |
| Candidate Precision / Recall | 三场景每项 1.0 | 三场景每项 1.0 | 通过 |
| Invalidation Precision / Recall | 三场景每项 1.0 | 三场景每项 1.0 | 通过 |
| Unaffected Preservation | 三场景每项 1.0 | 三场景每项 1.0 | 通过 |
| Repair Success | 三场景均 true | 三场景均 true | 通过 |
| Full Recompute Equivalent | 三场景均 true | 三场景均 true | 通过 |
| Replay / Event Idempotency | 三场景均 true | 三场景均 true | 通过 |
| Provenance Integrity | 三场景均通过 | 三场景均通过 | 通过 |
| Critical Failures | 0 | 0 | 通过 |
| Selective Recompute | `2 / 6` | `2 / 6 = 0.3333333333333333` | 通过 |
| Full Recompute | `6 / 6` | `6 / 6 = 1.0` | 通过 |

因此，在当前冻结图和规则内，选择性执行与全量重算得到相同最终结论，同时少重算 4 个未受语义影响的结论。这个比较只在全部正确性、provenance 与 replay 断言通过后成立。

### 15.3 Failure Analysis

正式 suite 的三场景 failure records 均为空，F01～F06 聚合计数均为 0。为了避免把“没有观察到失败”误当成“失败检测一定有效”，P0-2C 增加了六项负向校准：

| Failure code | 注入的可控偏差 | 期望与结果 |
| --- | --- | --- |
| F01 Impact Detection | ground truth 额外要求一个不存在的 candidate node | 单独触发 F01 |
| F02 Invalidation Decision | ground truth 将实际语义变化集合置空 | 单独触发 F02 |
| F03 Repair Correctness | 将 `retry_policy_fit` 期望结果从 fail 改为 pass | 单独触发 F03 |
| F04 Recompute Scope | 将期望重算结论集合置空 | 单独触发 F04 |
| F05 Provenance Integrity | 注入缺失 dependency edge 的 provenance error | 单独触发 F05 |
| F06 Replay Reproducibility | 将 replay determinism 信号置为 false | 单独触发 F06 |

六类记录均满足 failure code、`critical` severity、scenario、entity refs、expected/actual 与非空 trace refs 契约。F05 另有跨来源 supersedes 拒绝和 artifact 缺失/篡改测试支撑；F06 另有真实 ChangeEvent 重放幂等测试支撑。

结论边界：零正式失败表示三个冻结场景没有违反已编码断言；负向校准表示六类探测路径能够报警。两者都不能证明未建模的 failure mode 不存在。

### 15.4 覆盖充分性与缺口

| 能力或风险 | 当前证据 | 覆盖状态 |
| --- | --- | --- |
| revise 导致语义变化与选择性修复 | GS-001、GS-003 | 已覆盖 |
| retract 但冗余证据保持语义 | GS-002 | 已覆盖 |
| 跨分支 untouched preservation | GS-001、GS-003 | 已覆盖 |
| selective 与 full-recompute 等价 | 三场景 | 已覆盖 |
| replay determinism / event idempotency | 三场景与回归测试 | 已覆盖 |
| snapshot identity/hash drift | Snapshot Registry tests | 已覆盖 |
| artifact 缺失或篡改 | suite negative test | 已覆盖 |
| F01～F06 探测器可触发 | 六项负向校准 | 已覆盖 |
| `expire` 变化类型 | 无冻结场景 | 未覆盖 |
| 多来源 `conflict` 与冲突消解 | 无冻结场景 | 未覆盖 |
| 真实网页检测与自动抽取噪声 | 当前为人工 fixture | 未覆盖 |
| 大图、并发、多进程和性能 | 无负载测试 | 未覆盖 |

当前覆盖足以评审 P0 的核心研究问题——在受控证据图上，是否能正确定位、验证并以更小范围修复结论——但不足以声称已具备通用 Web Research 或生产能力。

### 15.5 验证结果

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -p 'test_*.py' -v
python -m veritas.evaluation.suite_runner `
  --manifest datasets/suites/p0-evolution-suite.json `
  --artifacts-root artifacts
```

最终结果：

```text
Ran 30 tests
OK
ARTIFACT_JSON_COUNT=31
ARTIFACT_HASH_VALID=31
ARTIFACT_HASH_MISMATCHES=0
F01_F06_NEGATIVE_CALIBRATIONS=6/6
```

### 15.6 P0-2C 结论

P0-2C 的退出条件已经满足：suite 指标、full-recompute baseline、逐场景 failure records、Failure Taxonomy 校准与覆盖边界均有可复核证据。项目状态推进为 **Ready for Gate P0 review**。

这不是 Gate P0 已通过。Gate 评审仍需明确回答：当前受控证据是否足以支持继续进入 M1，还是应先扩充 `expire`、`conflict` 或更不规则的图结构场景。无论哪种选择，都不应把当前 `2 / 6` 比例外推为真实负载收益。

## 16. P0-3 expire 与 conflict 场景实现

### 16.1 范围与决策

P0-3 补上 15.4 节登记的两个覆盖缺口：`expire` 与多来源 `conflict`。新增语义不写入旧 rule version，GS-004/GS-005 使用 `p0-rules-3`；冻结的 suite 1.0.0 与 GS-001～003 fixture 保持不变，新聚合通过独立的 `p0-evolution-suite-2.json`（suite_version 2.0.0）表达。相关决策见项目结构文档 D-018～D-020。

### 16.2 实现位置

| 能力 | 实际文件 |
| --- | --- |
| GS-004 expire fixture 与真值 | [`GS-004/scenario.json`](../datasets/scenarios/GS-004/scenario.json) |
| GS-005 conflict fixture 与真值 | [`GS-005/scenario.json`](../datasets/scenarios/GS-005/scenario.json) |
| Suite 2.0.0 manifest 与声明式验收 | [`p0-evolution-suite-2.json`](../datasets/suites/p0-evolution-suite-2.json) |
| expire/retract 共用追加式 current-view | [`storage/sqlite.py`](../src/veritas/storage/sqlite.py) `list_active_evidence_edges_for_claim` |
| conflict 候选传播（新边目标为种子，沿 depends_on 下行） | [`evidence/graph.py`](../src/veritas/evidence/graph.py) `candidate_impact_from_claims`、[`invalidation/impact.py`](../src/veritas/invalidation/impact.py) |
| expire/conflict ChangePackage 校验 | [`invalidation/repair.py`](../src/veritas/invalidation/repair.py) `_validate_package` |
| change_event 谱系三形状校验 | [`storage/sqlite.py`](../src/veritas/storage/sqlite.py) `validate_provenance` |
| manifest 声明式验收 | [`evaluation/suite_runner.py`](../src/veritas/evaluation/suite_runner.py) |
| 新增回归测试 | [`test_gs004.py`](../tests/scenarios/test_gs004.py)、[`test_gs005.py`](../tests/scenarios/test_gs005.py)、[`test_p0_suite_2.py`](../tests/scenarios/test_p0_suite_2.py)、[`test_expire_and_conflict.py`](../tests/unit/test_expire_and_conflict.py) |

### 16.3 已实现语义

- **expire**：与 retract 共用追加式 current-view 机制（ChangeEvent 排除来源，不改写旧行）；区别在于语义层——expire 断言来源在 `effective_at` 前有效、之后失效，retract 断言内容被撤回。GS-004 中唯一支持证据过期使 `migration_assistance_available` 从 `accepted` 变为 `unsupported`（不是 contradicted），结论从 `pass` 变为 `unknown`，首次覆盖规则表的 unsupported→unknown 路径；
- **conflict**：事件引入独立来源（不同 `source_id`、`supersedes_version_id` 必须为 NULL）的反驳证据，旧来源保持 active，系统不仲裁、不选边。候选影响以新增 supports/contradicts 边的目标 Claim 为种子，沿 input snapshot 的 depends_on 边传播。GS-005 中 `python_312_supported` 从 `accepted` 变为 `conflict`（assessment 同时引用两条 active 边），结论从 `pass` 变为 `conflict`，首次覆盖 conflict→conflict 路径；
- **校验**：expire 禁止夹带任何新实体；conflict 要求新来源存在、不得 supersede 旧来源、source_id 必须不同、必须携带新证据与新边；
- **suite 验收**：manifest 可声明 `acceptance` 块（验收字段名、evaluation_status、gate 状态、期望 recompute totals）；无该块的 manifest 保持 P0-2B 行为逐字节不变。

### 16.4 逐场景实际结果（suite 2.0.0）

| 场景 | 变化类型 | Candidate P/R | Invalidation P/R | Unaffected | Repair / Full Equivalent | Selective | Full | Critical failures |
| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: |
| GS-001 | revise | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 2 | 2 / 2 | 0 |
| GS-002 | retract | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 0 / 2 | 2 / 2 | 0 |
| GS-003 | revise（分支隔离） | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 2 | 2 / 2 | 0 |
| GS-004 | expire | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 3 | 3 / 3 | 0 |
| GS-005 | conflict | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 | true / true | 1 / 2 | 2 / 2 | 0 |

聚合：selective **4 / 11 ≈ 0.3636**，full **11 / 11**；五个场景 replay determinism、event idempotency、provenance integrity 均为 true；`p0_3_acceptance_candidate=true`（该字段只表示实现输出满足声明的验收契约，Gate P0 结论见项目结构文档第 11 节）。

### 16.5 验证命令与结果

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests -v
python -m veritas.evaluation.suite_runner --manifest datasets/suites/p0-evolution-suite.json --artifacts-root artifacts
python -m veritas.evaluation.suite_runner --manifest datasets/suites/p0-evolution-suite-2.json --artifacts-root artifacts
```

最终结果：

```text
Ran 51 tests
OK
suite 1.0.0 重跑：summary 与逐场景 artifacts 零 diff
suite 2.0.0：critical_failure_count=0，p0_3_acceptance_candidate=true
ARTIFACT_JSON_COUNT=67
ARTIFACT_HASH_MISMATCHES=0
```

新增 21 项测试覆盖：expire 候选集合精确性、unsupported→unknown 状态迁移、过期证据退出 current-view、conflict 双边同时 active 且不仲裁、conflict 谱系形状被 provenance 校验接受、expire/conflict 非法包拒绝（夹带新来源/新证据、superseding 来源、同 source_id、缺新边）、suite 2.0.0 manifest 声明验收与聚合契约、suite 1.0.0 行为不变回归。原有 30 项测试全部保持通过。

## 17. M1-1 协议与语料

### 17.1 范围

M1 的第一切片只建立两条可替换边界与一份冻结语料，不修改 P0 内核：

- `providers/`：`LLMProvider` 协议、`OpenAICompatibleClient`（stdlib urllib、JSON mode、temperature=0、429/5xx 退避重试、token 计量）、`FixtureLLM`（prompt 哈希重放，未知 prompt 拒绝）、`RecordingLLM`（真实调用录制为 fixture）；
- `search/`：`SearchProvider` 协议（search/fetch）、`LocalCorpusProvider`（manifest + TF-IDF，支持 `as_of` 版本视图，加载时校验全部文件 SHA-256）；
- `datasets/corpus/httpx-docs/`：`scripts/harvest_corpus.py`（一次性工具，不进 runtime）从 httpx 仓库 git tags（0.24.1～0.28.1）抽取的 10 篇文档、48 个版本快照。

### 17.2 验证结果

- 新增 21 项测试：provider 重放/录制/重试/错误语义、语料检索排序、as_of 版本选择、hash 篡改拒绝、manifest 重复拒绝、真实语料形状与检索；
- 全部测试：`Ran 72 tests OK`（含 P0 既有 51 项，零回归）；
- 真实 LLM smoke test 未在本切片运行（需要 `VERITAS_LLM_API_KEY`，留待 M1-2 校准时执行）。

### 17.3 已知边界

- TF-IDF 是词面检索，不做语义匹配；httpx 的 `advanced` 文档在 0.27 后改名路径，只有 3 个版本——语料忠实保留这一真实历史；
- `OpenAICompatibleClient` 未接流式、未做 token 计费持久化（预算跟踪在 M1-3）。

## 18. M1-1R 跨平台语料与 CI 收口

### 18.1 故障根因与修复契约

M1-1 的初次 GitHub Actions 在 Linux 上失败：Windows 工作树中的 48 个 Markdown 快照为 CRLF，旧 manifest 对工作树原始字节计算 SHA-256；`.gitattributes` 又要求 `*.md` 在 Git checkout 时规范为 LF，因此 Linux 读取到的已提交字节与 manifest 不一致。

M1-1R 将语料契约冻结为 **UTF-8 解码后把 CRLF/CR 统一为 LF，再计算 SHA-256**：

- `src/veritas/search/local_corpus.py` 以 canonical 文本校验 hash，`fetch()` 也返回同一 canonical 内容；
- `scripts/harvest_corpus.py` 在写文件和计算 hash 前执行相同规范化，并以显式 LF 字节写出语料与 manifest；
- 48 条 manifest hash 全部重算为 Git/Linux checkout 可复现的 LF 内容 hash；
- `test_hash_verification_is_newline_stable` 分别写入 LF 与 CRLF，验证两者都通过且返回相同内容；真实语料测试同时锁定 10 个文档、48 个快照；
- `OpenAICompatibleClient` 在处理 `HTTPError` 后显式关闭 response，消除严格 `ResourceWarning` 检查下的资源泄漏。

### 18.2 CI 与验证结果

GitHub Actions 现有两个正交矩阵：

- 单元/场景测试：Python 3.11 与 3.14，`fail-fast: false`；
- suite 回归：`p0-evolution-suite.json` 与 `p0-evolution-suite-2.json`。

本地收口验证结果：

- Python 3.11.15：`Ran 73 tests OK`，并把 `ResourceWarning` 视为错误；
- Python 3.14.7：`Ran 73 tests OK`，并把 `ResourceWarning` 视为错误；
- 语料定向测试：12/12 通过；
- suite 1.0.0：16 个 JSON 与已提交 artifacts 逐字节一致，critical failures 为 0；
- suite 2.0.0：26 个 JSON 与已提交 artifacts 逐字节一致，critical failures 为 0；
- artifacts：67/67 content hash 有效；
- corpus：48/48 本地 canonical hash 有效，48/48 manifest hash 与 Git 中 LF 字节一致。

远程 GitHub Actions [run 33247415305](https://github.com/Peter-Sherlock/Veritas/actions/runs/33247415305) 已完成并成功：Python 3.11、Python 3.14、suite 1.0.0 与 suite 2.0.0 四个 job 全部为 `success`。

### 18.3 退出边界

M1-1R 只证明 provider/search 边界、冻结语料和 CI 在目标 Python/操作系统换行差异下可复现。它没有把检索结果接入 Evidence/Claim 抽取，也没有验证真实 LLM、检索质量、研究循环或生产规模。

## 19. M1-2A 确定性抽取基线

### 19.1 严格边界

`src/veritas/extraction/pipeline.py` 将 provider 的职责限制为提出四个字段：

```json
{
  "assertions": [
    {
      "statement": "...",
      "canonical_key": "...",
      "relation": "supports | contradicts",
      "quote": "verbatim source substring"
    }
  ]
}
```

确定性 validator 负责：

- JSON 顶层和 assertion 字段必须精确匹配 schema，未知字段直接拒绝；
- `canonical_key` 必须符合受限格式，`relation` 只能为 `supports` 或 `contradicts`；
- `quote` 必须在当前 `VersionedDocument` 中逐字出现且只出现一次；不存在与歧义引用分别分类为 `citation_not_found` 和 `citation_ambiguous`；
- provider 不得提供 Evidence/Claim/edge ID、时间或 hash；`ResearchExtractionPipeline` 根据 source namespace、版本、引用和 canonical key 生成稳定 ID；
- 输出是候选 `EvidenceSpan`、`Claim` 与 `DependencyEdge`，本切片不直接写入 P0 SQLite。

### 19.2 冻结校准数据

`datasets/extraction/httpx-m1-2a/` 包含 10 个问题、gold assertions 与逐文档 fixture responses：

- 每题固定 query、`top_k=3`、正确来源的最大允许排名和精确 statement/key/relation/quote；
- fixture 同时冻结问题文本和实际检索到的文档版本；问题、版本或 top-k 文档集合漂移会在运行前失败；
- pipeline 对 top-3 全部文档执行抽取，无关文档必须显式返回空 assertions，不能使用 gold source 绕过检索；
- `extraction_runner.py` 分开计算 retrieval rank 与 assertion exact match，statement 也属于评分 identity；
- summary 固化在 `artifacts/extraction/httpx-initial-extraction-1.0.0/summary.json`，并携带排除自身字段后计算的 canonical SHA-256。

### 19.3 本地验证结果

冻结 fixture baseline 的实际结果：

| 指标 | 结果 |
| --- | ---: |
| Cases | 10/10 pass |
| Retrieval Hit@3 | 1.0 |
| Mean Reciprocal Rank | 0.7833333333333333 |
| Assertion micro precision | 1.0 |
| Assertion micro recall | 1.0 |
| Citation exact alignment | 1.0 |
| Critical failures | 0 |

MRR 低于 1 是必须保留的检索事实：EX-004/008/010 的正确来源排第 2，EX-009 排第 3。fixture 抽取满分不能改写为“检索质量满分”。

新增 12 项测试覆盖合法抽取、invalid JSON/schema/relation/key、引用缺失/歧义、supports/contradicts 候选物化、空抽取、稳定重放、10 题校准、statement 负向错配、fixture question 漂移和 prompt canary 漂移。Python 3.11.15 与 3.14.7 均在严格 `ResourceWarning` 模式下通过 `85/85` tests。

GitHub Actions 新增独立 `extraction-calibration` job：重跑 10 题并要求 committed summary 零 diff。[run 33250938915](https://github.com/Peter-Sherlock/Veritas/actions/runs/33250938915) 已完成，双 Python、双 evolution suite 与 extraction calibration 共 5 个 job 全部 `success`。

### 19.4 退出边界

M1-2A 已完成，但 M1-2 尚未完成。当前结果证明严格 contract、引用对齐、候选物化与 fixture calibration 可复现；不证明真实模型能达到同样 precision/recall，也不证明候选已安全持久化进 Evidence Graph。

## 20. M1-2B 抽取失败分类与 Gate 硬化

### 20.1 EX 失败分类

M1-2B 按照与 P0 F01～F06 相同的纪律，为抽取校准建立独立可触发的失败分类 `ex-failures-1`。分类区分两种含义不同的坏结果：

- **critical（完整性失败）**：结果不可信——provider 响应根本没能通过契约，或冻结 fixture 已漂移；
- **major（质量差距）**：校准仍然有效，但 provider 或检索没有达到 gold 期望——这正是 M1-2C 真实模型校准要度量的对象。

| code | 名称 | severity | 判定 |
| --- | --- | --- | --- |
| `EX01_RETRIEVAL_MISS` | 检索未命中 | major | gold 来源未在 `expected_retrieval.max_rank` 内命中 |
| `EX02_CONTRACT_REJECTION` | 契约拒绝 | critical | provider 响应被契约校验拒绝（`invalid_json`/`invalid_schema`/`invalid_canonical_key`/`invalid_relation`/`duplicate_assertion`/`canonical_key_conflict`） |
| `EX03_CITATION_REJECTION` | 引用拒绝 | major | 断言因 `citation_not_found` 或 `citation_ambiguous` 未通过逐字 grounding |
| `EX04_ASSERTION_MISMATCH` | 断言不匹配 | major | 契约通过但抽取断言集与 gold 集合 exact-match 不一致 |
| `EX05_FIXTURE_DRIFT` | fixture 漂移 | critical | 冻结 fixture/canary/检索快照/version/prompt/schema/corpus 身份漂移；运行前守卫直接中止运行，不产出 summary |

失败记录契约与 P0 F-code 对齐：`failure_code`、`severity`、`entity_refs`（case_id）、`expected`、`actual`，可选结构化 `reason`（如 `pipeline_code`、`missing_statements`/`unexpected_statements`）。契约拒绝时只记录 EX02/EX03，不再对同 case 重复记 EX04。

### 20.2 语义与契约变化

- `critical_failure_count` 从"失败 case 数"改为"critical 级失败记录数"；新增 `major_failure_count`。正常集两者均为 0，committed artifact 数值不变；
- per-case `failure`（单对象）改为 `failures`（数组），一个 case 可同时携带 EX01 与 EX04；
- summary 新增 `failure_taxonomy`、`failure_counts`（五码显式零值）、`failures`（全量记录）；`m1_2a_acceptance_candidate` 语义保持"冻结 fixture 基线零失败"，现定义为 critical=0 且 major=0；
- benchmark 与 fixtures 数据逐字节不变；summary 为增量 schema 演化并重新生成 content hash，M1-2A 的度量值（10/10、Hit@3=1.0、MRR=0.7833、P/R/citation=1.0）全部不变；
- 运行前守卫（question/version/快照/canary/case 集合/prompt/schema/corpus 漂移）统一以 `EX05_FIXTURE_DRIFT:` 前缀抛出 `ValueError`，使中止路径机器可读；漂移中止运行，因此 EX05 不出现在 summary 内，其"可触发"由异常断言证明。

### 20.3 实现与负向校准

实现位置：[`evaluation/extraction_runner.py`](../src/veritas/evaluation/extraction_runner.py)（`classify_contract_error`、`_failure_record`、`build_fixture_provider` 守卫、`evaluate_extraction_calibration` 聚合）。runner 拆出 `evaluate_extraction_calibration(benchmark, fixtures, corpus, provider)` 内聚入口，负向校准通过内存扰动副本注入，不需要临时文件。

六项负向校准（`tests/unit/test_extraction_taxonomy.py`）：

| 校准 | 注入的扰动 | 结果 |
| --- | --- | --- |
| EX01 | EX-009 `max_rank` 收紧为 1（实际 rank 3） | 单独触发 EX01 major；critical 保持 0 |
| EX02 | gold 文档响应替换为非 JSON | 单独触发 EX02 critical（`invalid_json`），不重复记 EX04 |
| EX03 | gold 文档响应替换为不可定位引用 | 单独触发 EX03 major（`citation_not_found`），citation alignment 降为 0.9 |
| EX04 | gold statement 改写 | 单独触发 EX04 major，携带 missing/unexpected statements |
| EX05 | version map / canary / prompt version 三类漂移 | 均以 `EX05_FIXTURE_DRIFT` 中止 |
| 正常集 | 无扰动 | `failure_counts` 五码显式零、`failures` 空、content hash 有效 |

### 20.4 验证结果

```powershell
$env:PYTHONPATH='src'
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m veritas.evaluation.extraction_runner --benchmark datasets/extraction/httpx-m1-2a/benchmark.json --fixtures datasets/extraction/httpx-m1-2a/fixtures.json --corpus-root datasets/corpus/httpx-docs --output artifacts/extraction/httpx-initial-extraction-1.0.0/summary.json --assert-pass
```

Python 3.14.7 严格 `ResourceWarning` 模式：`Ran 91 tests OK`（85 + 新增 6）。重新生成的 summary 与 M1-2A 版本相比仅含增量 schema 字段与 `failure` → `failures` 结构化替换，全部度量值不变，`--assert-pass` 通过。GitHub Actions [run 33297042264](https://github.com/Peter-Sherlock/Veritas/actions/runs/33297042264) 五路任务全部成功，其中 extraction-calibration job 确认 committed summary 零 diff。

### 20.5 退出边界

M1-2B 证明五类失败探测器独立可触发、正常集零失败、gate 语义分级清晰。它不观察任何真实模型失败模式——EX 分类是对"已编码失败路径"的校准，M1-2C 的真实 provider 记录才是第一条真实分布证据。

## 21. M1-2B2 Benchmark 扩容至 30 题

### 21.1 动机与范围

10 题对 M1-2C 的真实模型校准来说样本太薄：即使全对也不说明问题，出 2～3 个错也无法在 retrieval、contract、语义之间归因。M1-2B2 将冻结 benchmark 扩容为 `httpx-initial-extraction` **2.0.0**（`datasets/extraction/httpx-m1-2b/`），共 30 题：

- EX-001～010 从 v1.0.0 逐字节携带（测试断言 superset 关系）；
- EX-011～030 为 20 道新题，新增覆盖：
  - **多断言**：EX-014（流式响应，2 条 supports）、EX-024（event hooks，2 条 supports）；
  - **contradicts**：EX-017（默认不跟随重定向）、EX-018（utf-8 而非 latin1）、EX-019（cookies 仅 client 级）、EX-022（不支持 HTTPS proxy）；
  - **as_of 版本视图**：EX-029（`index@0.24.1`，"HTTPX requires Python 3.7+"——0.25.2 起改为 3.8+）、EX-030（`troubleshooting@0.25.2`，legacy `proxies` dict 配置风格——0.26.0 起改为 transport mounts）。两题的引文均为语料真实历史差异，为 M1-5 的演化 benchmark 提供直接先例。

`as_of` 语义澄清：`LocalCorpusProvider.latest_version` 将 `as_of` 与版本 `published_at` 做字典序比较，因此 `as_of` 必须是 ISO 日期边界（如 `2023-06-01T00:00:00Z`），不能传版本号。

### 21.2 生成与验证脚本

fixtures v2 不再手写，由 [`scripts/build_extraction_v2_fixtures.py`](../scripts/build_extraction_v2_fixtures.py) 从 benchmark v2 + 冻结语料确定性生成：gold 文档的 fixture 响应 = gold 断言集，其余 top-3 文档响应为空 assertions。脚本在写出前验证：每条 gold 引文在解析到的文档版本中逐字出现且唯一；gold 文档检索排名 ≤ `max_rank`；gold 断言全部被检索文档覆盖；并计算 prompt canary。fixture v1（`httpx-m1-2a/`）与摘要 artifact 1.0.0 保持冻结不变。

### 21.3 冻结基线结果

| 指标 | v1.0.0（10 题） | v2.0.0（30 题） |
| --- | ---: | ---: |
| Cases passed | 10/10 | 30/30 |
| Retrieval Hit@3 | 1.0 | 1.0 |
| Mean Reciprocal Rank | 0.7833 | 0.7222 |
| Assertion micro precision / recall | 1.0 / 1.0 | 1.0 / 1.0 |
| Citation exact alignment | 1.0 | 1.0 |
| Critical / major failures | 0 / 0 | 0 / 0 |

MRR 下降是扩容揭示的检索事实而非退化：advanced 文档（1296 行、词面覆盖广）在多个新题中排第 1，把 gold 文档压到第 2/3。这正是扩容的目的——把 v1 掩盖的检索弱点显性化，供后续检索升级决策使用。

新增 6 项测试（`tests/scenarios/test_extraction_calibration_v2.py`）：30/30 通过与 hash 有效性、v1 superset 断言、覆盖形状（multi/contradicts/as_of 集合精确断言）、as_of fixture 版本映射、确定性重跑、fixture 漂移以 `EX05_FIXTURE_DRIFT` 拒绝。

### 21.4 验证结果

Python 3.14.7 严格 `ResourceWarning` 模式：`Ran 97 tests OK`（91 + 新增 6）。CI `extraction-calibration` job 扩展为双校准矩阵（M1-2A 10 题 + M1-2B2 30 题），各自要求 committed summary 零 diff。GitHub Actions [run 33299526597](https://github.com/Peter-Sherlock/Veritas/actions/runs/33299526597) 六路任务全部成功。

### 21.5 退出边界

v2.0.0 仍为确定性 fixture 基线：30/30 证明契约、引用对齐、检索口径与评分在扩容后可复现，不证明真实模型达到同样 precision/recall。as_of 案例证明版本视图检索与引文对齐可复现，但不构成演化 benchmark 本身——那需要把 ChangeEvent 与 ground truth 一并冻结（M1-5）。

## 22. M1-2C-pre Live Provider 接入路径

### 22.1 范围与 provider 选型

本切片只交付真实 provider 校准的运行路径（M1-2C-pre），不包含录制本身——录制需要 `VERITAS_LLM_API_KEY`。live provider 选定为 **DeepSeek `deepseek-v4-flash`**（决策 D-029）：截至 2026-08-30 的国产 API 现价中，它是"付费里最便宜且质量有保底"的选项（输入未命中 1 元/1M tokens、缓存命中 0.02 元/1M、输出 2 元/1M，1M 上下文），原生 OpenAI 兼容且支持 JSON Output。旧别名 `deepseek-chat`/`deepseek-reasoner` 已不是文档化的模型名，客户端默认模型随之更新为 `deepseek-v4-flash`。

### 22.2 语义与契约变化

- `OpenAICompatibleClient` 新增 `extra_payload` 参数：在标准字段之后合并进请求体，provider 专属参数（如 DeepSeek 的 thinking 开关）不进入客户端本体；默认模型更新为 `deepseek-v4-flash`；
- live 校准固定发送 `"thinking": {"type": "disabled"}`：V4-Flash 默认开启思考模式，校准需要低延迟、低成本与接近确定性的 JSON 输出；
- DeepSeek JSON Output 要求 prompt 中出现 "json" 一词——`EXTRACTION_SYSTEM_PROMPT` 本身含 "Return one JSON object"，满足；
- `RecordingLLM` 累计 `request_count`/`prompt_tokens`/`completion_tokens`（不入库 fixture 文件，仅用于运行报告成本）；
- 新增 `run_live_extraction_calibration`：benchmark、语料、prompt 与指标与 fixture 路径完全一致，仅替换 provider；每次真实交换经 `RecordingLLM` 录制为 `{model_id, responses}` 键值文件（`fixture_key` 为 prompt SHA-256），可随后用 `FixtureLLM` 确定性重放；summary 的 `fixture_id` 记为 `live-recording:<model>` 以区别冻结 fixture；
- live 运行逐题向 stderr 流式输出进度（`[live] 5/30 EX-005 fail requests=15 prompt_tokens=...`），并在每题完成后重写录制文件——90 次串行调用全程可观察，中途中断保留已完成题目的录制；
- CLI 扩展：`--provider {fixture,live}`（默认 fixture，行为不变）；live 模式需 `--record-out`，模型/端点由 `--model`/`--base-url` 指定，API key 只从 `VERITAS_LLM_API_KEY` 读取（不进命令行参数）；缺 key 时以清晰错误快速失败，不发任何网络请求。

### 22.3 运行命令

```bash
export VERITAS_LLM_API_KEY=...   # DeepSeek 平台创建，勿写入仓库
python -m veritas.evaluation.extraction_runner \
  --provider live \
  --model deepseek-v4-flash \
  --benchmark datasets/extraction/httpx-m1-2b/benchmark.json \
  --corpus-root datasets/corpus/httpx-docs \
  --record-out artifacts/extraction/live/responses-recording.json \
  --output artifacts/extraction/live/summary-live.json
```

录制文件是原始记录而非冻结 fixture：冻结（转成 per-case responses + canary + 漂移校验）是录制完成后的独立步骤。

### 22.4 验证结果

新增 7 项测试（`tests/unit/test_live_calibration.py` 4 项 + `tests/unit/test_providers.py` 3 项）：live 路径用注入 provider 打满 10 题且录制文件可经 `FixtureLLM` 重放出同一 prompt 键；全量契约拒绝（invalid JSON）逐题记 EX02、critical=10、major=0；逐题进度行数与内容（含最终 `live provider recording:` 汇总行）；无注入 provider 且缺环境 key 时快速失败。客户端测试锁定默认模型 `deepseek-v4-flash`、`extra_payload` 合并与 JSON mode 不受污染；`RecordingLLM` token 计量单调累计。Python 3.14.7 严格 `ResourceWarning` 模式 `Ran 104 tests OK`；fixture 路径重跑 M1-2A 与 M1-2B2，两个 committed summary 逐字节不变（`git diff --no-index` 零输出）。

### 22.5 退出边界

本切片证明 live 路径在注入 provider 下可运行、可录制、可重放，且不影响冻结基线；不证明真实 DeepSeek 调用的行为——录制、失败分布对比与 canonical_key/citation 校准仍是 M1-2C 待办，需要 API key。

## 23. M1-2C 真实 provider 校准录制与失败分析

### 23.1 运行与成本

2026-08-30 以 DeepSeek `deepseek-v4-flash`（非思考模式、temperature=0、JSON mode，见 D-029）对冻结 30 题 benchmark 执行真实校准录制。实际发生 **67 次请求**（契约拒绝中止该题剩余文档，故少于 90 次上限）、**409,233 prompt tokens + 4,658 completion tokens**，按缓存未命中上限估算成本 **≈ 0.42 元人民币**（输出仅 4.7K tokens，输入占绝对大头；文档跨题重复，实际命中 prompt cache 后更低）。录制与 summary 冻结在 `artifacts/extraction/httpx-initial-extraction-2.0.0-deepseek-v4-flash/`（`responses-recording.json` 67 条真实响应 + `summary.json`），API key 不进仓库。

### 23.2 结果：fixture 基线 vs 真实模型

| 指标 | fixture v2.0.0（重放） | live deepseek-v4-flash |
| --- | ---: | ---: |
| Cases passed | 30/30 | **0/30** |
| Retrieval Hit@3 | 1.0 | 1.0（逐位一致） |
| Mean Reciprocal Rank | 0.7222 | 0.7222（逐位一致） |
| Assertion micro precision / recall | 1.0 / 1.0 | 0.0 / 0.0 |
| Citation exact alignment | 1.0 | 0.4 |
| Critical / major failures | 0 / 0 | **9 / 21** |

检索层与 fixture 基线**逐位相同**，实证检索是模型无关的确定性层。0/30 是严格 exact-match 口径下的真实结果，恰好是 M1-2C 存在的目的。

### 23.3 失败归因（ex-failures-1）

| 失败类 | 次数 | 真实原因与样本 |
| --- | ---: | --- |
| EX01 检索未命中 | 0 | 检索确定性层与模型无关 |
| EX02 契约拒绝 | 9 | 8× `invalid_canonical_key`：模型自然产出环境变量风格的键（如 `NO_PROXY` 大写），不满足 `^[a-z0-9][a-z0-9._:=/-]*$`；1× `canonical_key_conflict`：EX-004 把"client 关闭 trust_env"与"top-level API 关闭 trust_env"两条不同事实压成同一个 `trust_env` 键 |
| EX03 引用拒绝 | 9 | 8× `citation_not_found`：引文被模型改写或规范化空白，不再是逐字子串；1× `citation_ambiguous`：EX-001 的 `client = httpx.AsyncClient(http2=True)` 在文档中出现 2 次 |
| EX04 断言不匹配 | 12 | 语义正确但措辞不同：EX-015 仅差一个句号；多数为真实改写（"Cookies must be set on the client instance, not passed per request" vs "Cookies cannot be passed per request on a Client instance."） |

归一化分析（大小写、句尾标点、空白、反引号全部归一后比较 statement）显示 32 条 gold 断言也只匹配 4 条——**精确 statement 匹配对真实模型不可达**，瓶颈在评分身份设计而非模型能力。这是 D-030 的直接证据。

关键正面结论：9 个完整性违规（EX02）全部在契约边界被拦截，**没有任何坏候选物化**；fixture 30/30 gate 未被真实输出污染。分类法把"完整性失败"与"质量差距"干净分离，正是 D-027 设计的验证。

### 23.4 重放验证

`tests/scenarios/test_live_recording_replay.py`（2 项）：把 67 条真实响应经 `FixtureLLM` 重放整场校准，重放 summary 与 committed `summary.json` **全等**（含 content_hash）；失败分布（0/30、9/9/12/0、critical=9、major=21、Hit@3=1.0、citation=0.4）钉进测试。真实运行自此可永久确定性重放。Python 3.14.7 严格 `ResourceWarning` 模式 `Ran 106 tests OK`。

### 23.5 退出边界

本次是**单 provider、单次运行**的诚实基线：未测多种子/温度方差，未测其他模型，canonical_key 字符集修复与 prompt 迭代（M1-2C2）、评分身份下沉（canonical-key 级或归一化 statement 比较）、候选持久化（M1-2D）均为后续切片。0/30 不能外推为"真实模型不可用"——30 题中 12 题通过了完整契约并物化出候选（全部仅 EX04 措辞级不匹配），其余 18 题在契约边界被拒（9 键格式 + 9 引文精确性）；但"语义可用"的任何数字同样不能在评分身份变更前作为质量声明。

## 24. M1-2C2 canonical_key 确定性派生与 benchmark v3.0.0

### 24.1 契约 v2：模型只提出内容，key 由确定性层派生

按 D-030 登记的方向，抽取契约升级为 `evidence-assertion-2`（prompt `httpx-extractor-2`）：

- 模型只提出 `statement`/`relation`/`quote` 三个字段；`canonical_key` 从模型契约中移除（旧字段响应按未知字段直接拒绝）；
- 新增 `derive_canonical_key(statement)`：statement 小写化后取 `[a-z0-9]+` token 以 `_` 连接。大小写、标点、空白差异共享同一身份，任何真实改写产生不同 claim；无字母数字内容的 statement 以 `invalid_statement` 拒绝；
- `canonical_key_conflict` 检查随之删除：key 是 statement 的纯规范化，同 key ⇔ 同规范化 statement，"一键两义"在构造上不可能（v2 live 中 EX-004 的 `trust_env` 塌缩在新契约下自然成为两个独立 claim）；
- `duplicate_assertion`（key+relation+quote 重复）保留；
- 评分身份下沉（D-030）：identity 从 `(doc_id, statement, key, relation, quote)` 改为 `(doc_id, derived_key, relation, quote)`，原始 statement 仅用于失败报告可读性——仅差大小写/句尾标点的断言不再误计为 EX04；
- system prompt 同步硬化引文要求："copied character-for-character ... keeping every backtick, punctuation mark, capital letter and space exactly as written"。

### 24.2 benchmark v3.0.0 与旧版本退役

`datasets/extraction/httpx-m1-2c/`（`httpx-initial-extraction` 3.0.0）由 `scripts/build_extraction_v3_fixtures.py` 从 v2.0.0 逐字携带全部 30 题，唯一变化是 gold assertions 与 fixture responses 去掉 `canonical_key`；检索、版本映射与引文验证逻辑不变（生成时重新验证）。运行时 schema 检查意味着 v1/v2 数据集与 v2 运行时不再兼容（旧 fixtures 携带 canonical_key 字段会被 v2 契约按未知字段拒绝），因此：

- v1/v2 数据集与其 summary artifact 保留为冻结历史，不再进入 CI；
- CI `extraction-calibration` 矩阵收敛为单条 v3 条目；
- 退役测试：`test_extraction_calibration.py`（v1）、`test_extraction_calibration_v2.py`、旧 `test_live_recording_replay.py`（v2 live 证据）、`test_extraction_taxonomy.py` 重写为 v3 契约负向校准（含"旧 schema 响应被拒"回归）。v2 live 证据目录保留为历史，其重放以当时提交的测试钉死过。

### 24.3 v3 fixture 基线

30/30 cases，Hit@3=1.0、MRR=0.7222（与 v1/v2 逐位一致——检索层不变）、micro precision/recall=1.0、citation alignment=1.0、critical/major=0/0。summary 固化在 `artifacts/extraction/httpx-initial-extraction-3.0.0/summary.json`。

### 24.4 v3 真实重跑：完整性违规清零

| 指标 | v2 契约 live（M1-2C） | v3 契约 live（M1-2C2） |
| --- | ---: | ---: |
| Requests | 67 | 82 |
| Tokens（prompt/completion） | 409,233 / 4,658 | 486,273 / 4,512 |
| 成本（缓存未命中上限） | ≈0.42 元 | ≈0.50 元 |
| EX02 契约拒绝（critical） | 9 | **0** |
| EX03 引用拒绝（major） | 9 | **4** |
| EX04 断言不匹配（major） | 12 | 26 |
| Cases passed | 0/30 | 0/30 |
| Citation exact alignment | 0.4 | **0.8667** |
| Micro precision / recall | 0.0 / 0.0 | 0.0385 / 0.0625 |

三个结论：

1. **canonical_key 派生把完整性失败清零**：9 个 critical 全部消失，本轮无任何坏候选在契约边界之外产生——"确定性层拥有 provenance"（D-025）的强化版得到实证。
2. **引文硬化有效**：character-for-character 指令使 EX03 从 9 降到 4（3× citation_not_found + 1× citation_ambiguous），citation alignment 从 0.4 升至 0.8667。
3. **EX04 上升是评分入口打开的结果**：此前 18 个被契约拒绝的题现在进入断言评分，26/30 题为纯措辞改写级不匹配；本轮 EX-008 与 EX-017 的 gold 断言在 key 级完全匹配（"0 missing"），micro recall 2/32。注意单次运行方差：模型措辞逐轮不同（v2 轮 EX-015 仅差句号的匹配在本轮未复现），单轮数字不能当作模型能力定值。

### 24.5 退出边界

仍是单 provider 单次运行；主要质量差距是语义改写（26/30），当前评分对改写零容忍且无语义匹配；候选尚未持久化（M1-2D）；key 级评分使 claim 级聚合成为下一步可能，但"改写即新 claim"的聚合噪声需在 M1-2D 设计中处理。

## 25. 当前限制

- 已实现检索到 Evidence/Claim 候选的自动 pipeline；真实 provider 校准完成两轮（M1-2C v2 契约 0/30、M1-2C2 v3 契约 0/30 但完整性违规清零、citation alignment 0.8667）；主要质量差距是语义改写（26/30 题），评分无语义匹配能力；
- EX01～EX05 覆盖的是抽取链路已编码的失败路径；真实模型的失败模式（半正确引用、语义 paraphrase、跨文档断言漂移）尚未被观察；
- 抽取候选尚未写入 SQLite 或接入 initial-research graph transaction；
- 没有来源质量权重；
- 没有处理复杂逻辑表达式、概率置信度或循环依赖；
- 没有定义真实网页版本检测；
- 没有验证 Fact 层是否需要独立存在；
- 已验证 `revise`、`retract`、`expire` 与多来源 `conflict` 四类变化场景；`expire` 与 `retract` 在 P0 共享追加式 current-view 机制，基于 `valid_to` 的自动过期与 as-of 历史查询尚未实现；
- Snapshot Registry 已覆盖身份/hash 漂移和未登记部分数据库，但尚未验证多进程并发初始化；
- storage protocol 目前只是最小写入边界，图和规则仍直接依赖 SQLiteRepository；
- 没有并发、多进程、规模或性能结果。
- Gate P0 评审结论已记录（见项目结构文档第 11 节）；`4 / 11` 的聚合重算比例来自受控场景设计，不能当作真实研究负载的成本收益。

因此，目前可以确认的是“五个受控离线演化场景、M1-1 provider/search 边界、M1-2A 检索→严格抽取→候选 Evidence/Claim 的 fixture 链路、M1-2B 的五类抽取失败分类与 gate 分级、M1-2B2 的 30 题扩容 benchmark、M1-2C-pre 的 live provider 运行路径、M1-2C 的真实 provider 校准录制与失败分析，以及 M1-2C2 的 canonical_key 确定性派生与评分身份下沉已经通过可复现验证”；不能外推为真实 LLM 抽取质量达标（两轮真实基线均为 0/30，语义改写差距未解决，且仅单 provider 单次运行）、已持久化的 initial research、真实 Web Research、Agent 自主研究或生产规模能力。

## 26. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-30 | M1-2C2 | 契约 v2（`evidence-assertion-2`/`httpx-extractor-2`）：模型只提 statement/relation/quote，canonical_key 由 `derive_canonical_key` 从 statement 确定性派生（D-031）；评分身份下沉到 key 级；`canonical_key_conflict` 删除、`invalid_statement` 新增；benchmark v3.0.0（30 题 superset，gold 无 canonical_key）；v1/v2 数据集退役出 CI，v1/v2 时代测试退役或重写；v3 fixture 基线 30/30；真实重跑 EX02 9→0、EX03 9→4、citation alignment 0.4→0.8667、critical=0，成本 ≈0.50 元；v3 live 证据入库 + 重放测试；110/110 tests |
| 2026-08-30 | M1-2C | DeepSeek `deepseek-v4-flash`（非思考、temperature=0、JSON mode）真实录制 30 题校准：67 请求、409K prompt tokens、≈0.42 元；0/30 exact-match（EX02×9、EX03×9、EX04×12、EX01×0），检索与 fixture 基线逐位一致，9 个完整性违规全部被契约拦截；归一化后 32 条 gold 仅 4 条匹配，精确 statement 匹配判定为对真实模型不可达（D-030）；录制可确定性重放并钉进测试；106/106 tests |
| 2026-08-30 | M1-2C-pre | 交付 live provider 校准运行路径：`run_live_extraction_calibration` + CLI `--provider live`（`--model deepseek-v4-flash` 默认、`thinking` 禁用、`RecordingLLM` 录制与 token 计量、逐题进度与中断安全保存）；客户端默认模型更新为 `deepseek-v4-flash`、新增 `extra_payload`；104/104 tests，fixture 双摘要逐字节不变；登记 D-029 |
| 2026-08-30 | M1-2B2 | 新增 20 题（EX-011～030）扩容 benchmark 至 `httpx-initial-extraction` 2.0.0；新增多断言 ×2、contradicts ×4、as_of 版本视图 ×2（index@0.24.1 Python 3.7+、troubleshooting@0.25.2 legacy proxies dict）；fixtures 由 `scripts/build_extraction_v2_fixtures.py` 确定性生成并内置 quote 唯一性与检索排名验证；30/30、Hit@3=1.0、MRR=0.7222、P/R/citation=1.0；CI 扩展为双校准矩阵；97/97 tests |
| 2026-08-30 | M1-2B | 建立 `ex-failures-1` 抽取失败分类（EX01～EX05，critical/major 分级）、runner 拆分 `evaluate_extraction_calibration`、summary 增量 schema 演化与 per-case `failures` 数组；六项负向校准独立可触发，正常集 critical=0；91/91 tests（Python 3.14.7 严格模式），committed summary 重生成且度量值不变 |
| 2026-08-29 | M1-2A | 新增严格 extraction contract、逐字引用对齐、确定性 Evidence/Claim/edge 候选、10 题 HTTPX gold/fixture baseline 与 calibration runner；10/10 cases，Hit@3/precision/recall/citation=1.0，MRR=0.7833；Python 3.11/3.14 均 85/85 tests |
| 2026-08-29 | M1-1R | 统一语料 canonical UTF-8/LF hash 契约，重算 48 条 manifest hash，增加换行回归与严格资源检查；CI 扩展为 Python 3.11/3.14 和 suite 1.0.0/2.0.0 双矩阵；两版本均 73/73 tests、67/67 artifact hashes 与 48/48 corpus hashes 通过 |
| 2026-08-29 | M1-1 | 新增 providers 与 search 模块、httpx 版本化语料（10 文档 48 快照）、21 项新测试；72/72 通过 |
| 2026-08-29 | P0-3 | 实现 GS-004 expire 与 GS-005 conflict 场景、conflict 传播模式、manifest 声明式验收与 suite 2.0.0；51/51 tests、67/67 JSON hash 通过，suite 1.0.0 零 diff；Gate P0 评审结论见项目结构文档 |
| 2026-08-27 | P0-2C | 完成三场景聚合评估、full-recompute 对照、F01～F06 负向校准与覆盖分析；30/30 tests、31/31 JSON hash 通过；状态推进为 Ready for Gate P0 review |
| 2026-08-27 | README | 建立 Explorer-first 项目入口；示例、运行命令、导航、链接和结果与 P0-2B 实现对齐 |
| 2026-08-27 | Repository setup | 初始化 Git `main`、验证忽略规则，并将首次基线提交推送至 private `Peter-Sherlock/Veritas` |
| 2026-08-27 | P0-2B | 实现 GS-002/003、retract current-view、compatibility rule、Snapshot Registry、F01～F06、显式 suite runner；24/24 tests 与 31/31 JSON hash 验证通过；阶段结束时 P0-2C/Gate 尚未执行 |
| 2026-08-27 | P0-2A | 冻结 GS-002 retract、GS-003 Python 分支变化、六类 Failure Taxonomy、suite 指标和 Gate 条件；未修改 runtime |
| 2026-08-27 | P0-1 | 实现 GS-001 垂直链路、SQLite 追加式版本、选择性修复、full-recompute 对照、五类 artifacts 与 11 项测试 |
| 2026-08-27 | P0-0 | 建立最小领域模型、传播/验证边界、GS-001、指标和验收断言 |
