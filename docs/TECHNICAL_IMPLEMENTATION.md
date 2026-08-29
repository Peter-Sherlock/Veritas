# Veritas 技术实现文档

> 文档职责：记录可执行技术规格、数据契约、算法、测试与实际验证结果。  
> 当前阶段：P0-2C Aggregate Evaluation and Failure Analysis
> 当前状态：Evaluation complete v0.5; Gate P0 review pending
> 更新日期：2026-08-27  
> 上位设计：[Veritas 初期项目设计文档](<../Veritas-Initial-Design(2).md>)  
> 配套文档：[项目结构与设计文档](PROJECT_STRUCTURE.md)

## 1. 当前阶段目标

P0-2C 对 P0-2B 已实现的三个场景、聚合指标、全量重算基线和 Failure Taxonomy 进行正式评估：

> 当一个来源发布新版本时，系统如何找到需要重新验证的节点，如何确认真正失效的主张，如何只更新必要结论，并保留完整版本谱系。

当前已有 GS-001～003 三个独立场景、显式 suite manifest、逐场景失败记录、Snapshot Registry 和聚合运行器。P0-2C 已完成正式聚合评估、F01～F06 负向校准和覆盖边界分析；结论是证据足以进入 Gate P0 评审，但 Gate 尚未作出通过或不通过决定。

## 2. 本阶段非目标

P0-2C 不包含：

- Web Search；
- LLM 抽取或推理；
- 向量检索；
- 动态规划；
- 多 Agent 或并行 Worker；
- PostgreSQL、图数据库或服务部署；
- 自由文本报告生成；
- 对真实互联网内容的自动变化检测；
- `expire` 与多来源 `conflict` 场景；
- storage protocol 的全面重构；
- Gate P0 的最终通过/不通过结论；
- 现有 GS-001 scenario 与历史逐运行 artifacts 的重写。

这些能力不能作为 P0 核心机制通过验收的前提。

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

## 16. 当前限制

- 证据与 Claim 的关系由 fixture 显式声明，没有测试自动抽取；
- 没有来源质量权重；
- 没有处理复杂逻辑表达式、概率置信度或循环依赖；
- 没有定义真实网页版本检测；
- 没有验证 Fact 层是否需要独立存在；
- 已验证两个 `revise` 和一个 `retract` 场景，尚未验证 `expire` 或多来源 `conflict`；
- Snapshot Registry 已覆盖身份/hash 漂移和未登记部分数据库，但尚未验证多进程并发初始化；
- storage protocol 目前只是最小写入边界，图和规则仍直接依赖 SQLiteRepository；
- 没有并发、多进程、规模或性能结果。
- P0-2C 正式 failure analysis 已完成，但 Gate P0 尚未作出通过或不通过决定；当前 suite summary 仍保留 P0-2B 实现验证字段，正式解释见本节分析；
- `2 / 6` 的聚合重算目标来自受控场景设计，不能当作真实研究负载的成本收益。

因此，目前可以确认的是“三个受控离线场景上的确定性 Evidence Evolution、撤回 current-view、选择性结论重算和可复现 suite 执行已实现并通过测试”；不能外推为真实 Web Research、通用冲突推理、Agent 自主研究或生产规模能力。

## 17. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-27 | P0-2C | 完成三场景聚合评估、full-recompute 对照、F01～F06 负向校准与覆盖分析；30/30 tests、31/31 JSON hash 通过；状态推进为 Ready for Gate P0 review |
| 2026-08-27 | README | 建立 Explorer-first 项目入口；示例、运行命令、导航、链接和结果与 P0-2B 实现对齐 |
| 2026-08-27 | Repository setup | 初始化 Git `main`、验证忽略规则，并将首次基线提交推送至 private `Peter-Sherlock/Veritas` |
| 2026-08-27 | P0-2B | 实现 GS-002/003、retract current-view、compatibility rule、Snapshot Registry、F01～F06、显式 suite runner；24/24 tests 与 31/31 JSON hash 验证通过；阶段结束时 P0-2C/Gate 尚未执行 |
| 2026-08-27 | P0-2A | 冻结 GS-002 retract、GS-003 Python 分支变化、六类 Failure Taxonomy、suite 指标和 Gate 条件；未修改 runtime |
| 2026-08-27 | P0-1 | 实现 GS-001 垂直链路、SQLite 追加式版本、选择性修复、full-recompute 对照、五类 artifacts 与 11 项测试 |
| 2026-08-27 | P0-0 | 建立最小领域模型、传播/验证边界、GS-001、指标和验收断言 |
