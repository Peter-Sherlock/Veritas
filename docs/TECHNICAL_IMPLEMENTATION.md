# Veritas 技术实现文档

> 文档职责：记录可执行技术规格、数据契约、算法、测试与实际验证结果。  
> 当前阶段：M3-R 可靠性收口
> 当前状态：M3-A/M3-B/M3-R complete；Gate M3 待评审
> 更新日期：2026-08-31
> 上位设计：[Veritas 初期项目设计文档](<../Veritas-Initial-Design(2).md>)  
> 配套文档：[项目结构与设计文档](PROJECT_STRUCTURE.md)

## 1. 当前阶段目标

M3-R 把 M3-B 已跑通的自主闭环从“正常路径可用”提升为“在已定义的进程中断窗口内可恢复”。收口对象是 runtime→evolution 的交付空隙、图写入原子性、刷新身份、不可变实体冲突与恢复配置漂移。

阶段拆分：

- **原子 checkpoint**：item 终态与完整 bundle 在同一 SQLite 事务落盘；
- **可靠交付**：outbox 以 at-least-once 投递到幂等刷新事务，崩溃后可重放；
- **身份与事务**：图桥无提前副作用，bundle 全量事务写入，同 ID 不同 payload 显式冲突；
- **恢复约束**：session 固定语料、模型、聚合/候选库、project、rule 与首次时间语义；
- **验证**：两个关键崩溃窗口、schema 迁移、原子回滚、身份漂移和历史演化回归进入 200 项全量测试。

本阶段退出不等于生产级容灾：它证明的是单机 SQLite、单 writer 编排和数据库 reopen/replay；多进程争用、任意 OS kill 点、网络分区与分布式 exactly-once 不在本阶段声明内。

## 2. 本阶段非目标

M3-R 不包含：

- Web Search；
- 新的真实 LLM 质量结论；
- 新检索、推理或规划策略；
- 多 Agent 或并行 Worker；
- PostgreSQL、图数据库或服务部署；
- 自由文本报告生成；
- TF-IDF 排序调优或语义检索；
- 对真实互联网内容的自动变化检测与抓取；
- 跨进程 worker 协调、分布式事务或 exactly-once 协议；
- 新的规模、性能或成本收益声明。

这些能力需要后续独立阶段与独立验收，不能从 M3-R 的故障注入测试中外推。

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

## 25. M1-2D 抽取候选持久化（CandidateStore）

### 25.1 设计（D-032）

抽取候选获得独立的事务性 SQLite 存储层 `src/veritas/extraction/store.py`（`CandidateStore`，schema id `extraction-candidates-1`），与 P0 冻结的 `SQLiteRepository` 分离：候选是 claim 聚合之前的概率边界产物，而演进运行时 `claims.canonical_key UNIQUE` 约束按设计只接受单一身份——两者生命周期与不变量不同，不共用存储。

核心语义：

- **身份**：`(source_version_id, canonical_key, content_hash)`，content_hash 覆盖完整候选内容（statement/relation/quote 的 canonical JSON sha256）；`candidate_id = "cand:" + sha256(身份)[:20]`，确定性可复现；
- **去重即幂等**：`INSERT OR IGNORE` 使任何 run 的精确重复候选塌缩为一行；同一 run 重放对存储零增量（fixture 重放二次运行 persisted=0、observations_new=0）；
- **冲突只暴露不合并**：同 `(source_version_id, canonical_key)` 下的关系翻转（supports/contradicts）、措辞变体、不同引用跨度全部作为独立候选保留；`list_relation_conflicts()` 暴露关系冲突，canonical_key 分组查询暴露改写噪声——存储层不做任何语义合并，语义去重留待后续聚合阶段；
- **存储权衡**：完整派生 slug（可超 100 字符）以 TEXT 存储并建索引——哈希化会摧毁暴露改写噪声的分组查询，且该长度对 SQLite 无压力；
- **完整性守卫（负面校准）**：写入前重派生 canonical_key 比对（D-031 的 trust-but-verify，`canonical_key_mismatch`）、relation 白名单（`invalid_relation`）、空 statement/quote/空 key 拒绝（`invalid_candidate`）、schema id 漂移守卫（`schema_drift`）；整批单事务（`BEGIN IMMEDIATE`），任何守卫拒绝时整批回滚零残留；
- **观测表**：`(candidate_id, run_id)` 主键的 observation 表记录"哪个 run 观察到哪个候选"；run_id 确定性派生（`fixture:<fixture_id>` / `live:<model>`），观察记录追加、内容永不改写。

### 25.2 接线与崩溃安全

`evaluate_extraction_calibration` 的 `on_case_done` 回调升级为 `(case_result, bundle)`（契约拒绝的 case bundle 为 None）。fixture/live 两条运行路径新增 `--store-out`：每个 case 的契约通过候选在 case 完成时提交独立事务，与录制保存相同的崩溃安全语义——中断保留已完成 case，重放幂等。候选持久化不触碰 summary（CI 的逐字节 diff 保持通过）。契约拒绝（含 EX03 中断 case 的前半部分文档）不产生候选。

### 25.3 冻结证据

- **fixture 重放**：金标准身份并集恰为 32（32 条 gold 断言无跨 case 重复），candidates/observations/distinct_keys = 32/32/32，关系冲突 0，二次重放零增量；
- **v3 live recording 重放**：26 个契约通过 case 产出 52 候选，run 内零重复；distinct_keys=51——`httpx_does_not_follow_redirects_by_default` 在 compatibility@0.28.1 与 quickstart@0.28.1 两个独立来源上各有一条（同 key 跨源正确分立，不合并）；关系冲突 0；
- **改写噪声量化钉住**：`quickstart@0.28.1` 上 15 个候选 15 个不同 key，EX-014 的两个 gold key 无一在列——"改写即新 claim"从 M1-2C2 的运行观察升格为存储层冻结证据；
- **跨 run 合库**：fixture 32 + live 52 = 84 候选，live 对金标准候选零去重命中（与 0/30 exact match 一致），distinct_keys=80（3 个 key 因跨源或同源不同引用各多一行）。

## 26. Gate M1-2 收口评审

### 26.1 出口条件核验

| 出口条件 | 证据 | 结论 |
| --- | --- | --- |
| 校准 CI 绿 | extraction-calibration 任务逐切片绿灯（最新 run 33312539577 五路成功）；评审日 HEAD 复跑：121/121 tests、双 suite exit 0、校准 `--assert-pass` exit 0 + artifacts 零 diff | 满足 |
| 真实 LLM 校准记录 | v2/v3 契约两份 DeepSeek V4-Flash 录制入库且重放测试钉死；EX02/EX03/EX04 真实触发、EX01/EX05 负向校准触发 | 满足 |
| benchmark 基线 | 10 题（M1-2A）→ 30 题 v3.0.0 逐字 superset；fixture 30/30、Hit@3=1.0、MRR=0.7222（rank-2/3 保留为诚实检索事实） | 满足 |

### 26.2 携带项与未交付清单

三条非阻塞携带项（D-033）：**C1** M1-3 预算/重规划设计必须以真实口径（0/30 exact-match、改写噪声已入库）为输入；**C2** 候选聚合保持"只暴露不合并"直至有证据支持的方案；**C3** 模型能力结论需第二次运行对照，规模/成本声明由 M1-5 承接。M1-2 未交付且不声称交付：抽取质量达标、语义匹配、聚合方案、多 provider 对照。

正式结论：**Gate M1-2 通过，允许启动 M1-3**（决策 D-033；评审输入与判断见项目结构文档 11.10）。依据：M1-2 的目标是校准 harness 与测量而非抽取质量——0/30 真实基线是 harness 诚实测量的证据而非阶段失败。

## 27. M1-3A Research Runtime 引擎：会话状态、队列、checkpoint 与预算

### 27.1 设计（D-034）

新增 `src/veritas/runtime/` 包（`store.py` 会话存储 + `engine.py` 引擎，schema id `research-runtime-1`），独立于候选存储（D-032）与 P0 演进库：checkpoint 天然可变，证据天然追加——混用会让任一方破坏另一方的不变量。会话 = 工作队列（`WorkItem`：item_id/query/question/top_k/as_of）+ 请求预算；队列处理复用 M1-2 校准过的 `ResearchExtractionPipeline` 原封不动，预算通过包装 provider 施加——概率边界与被校准的那条完全一致。

核心语义：

- **逐项 checkpoint**：每个 item 的状态迁移（`pending` → `completed`/`rejected`）是独立事务；中断只丢当前 item；
- **恢复语义**：以同一 `session_id` 与**完全相同**的队列规格重开（规格漂移以 `session_spec_drift` 拒绝）；终态 item（completed/rejected）跳过、pending 重做。重做 item 的候选去重由 D-032 的身份幂等保证——崩溃前已落库的候选不会重复；
- **预算 = reserve-then-call**：`try_reserve_request` 以单条原子 `UPDATE ... WHERE requests_spent < budget_requests` 在调用前持久化预留；崩溃宁可少花（预留未响应也计为已花），绝不超支。预算耗尽是干净停止（会话转 `budget_exhausted`、pending 保留、无异常抛出），提高预算恢复执行；resume 时预算单调（`budget_decrease` 拒绝）；
- **拒绝即终态**：契约拒绝（`ExtractionContractError`）的 item 记录 `rejected` + 错误码，不再自动重试（确定性重放会复现拒绝、真实重试只是重复花费）；被拒 item 的部分抽取不落库（与校准 harness 的批量语义一致）；
- **会话终态守卫**：全部 item 终态才能 `completed`（`pending_items_remain` 守卫）；已完成会话重跑以 `session_completed` 拒绝；
- **成本锚点（D-033 C1）**：`budget_requests` 是必填显式参数，无静默默认；真实基线（v3 live 82 请求/30 题 ≈ 2.7 请求/题）是预算定标的输入，fixture 满分不是。

### 27.2 验证

13 项新测试（121 → 134）：

- store 单元 7 项：schema 漂移守卫、规格校验与漂移拒绝、预留原子性与持久性、item 迁移 checkpoint、预算单调与耗尽解除、会话终态守卫；
- engine 单元 5 项：正常路径（3 item/3 请求/3 候选/run 归属 `session:<id>`）、预算耗尽干净停止 + 提额恢复（花费 1→2，无浪费调用）、契约拒绝记录且终态（重跑 `session_completed`）、**崩溃恢复收敛**（`_CrashAfterLLM` 模拟进程崩溃：2 item 完成 + 1 item 中断，花费 3；恢复只重做中断 item，健康 provider 仅见 1 次调用，最终花费 4；与无崩溃参考 run 的终态逐项相等、候选身份集合相等）、规格漂移拒绝；
- 场景 1 项：冻结 fixture provider 驱动真实 httpx 语料的 3 题会话（EX-001～003）——completed、9 请求、候选恰为 3 个 gold 身份；已完成会话重放被拒且存储零变化。

## 28. M1-3B Runtime CLI 与 live 接线

### 28.1 命令与语义（D-035）

```
python -m veritas.runtime \
  --spec session-spec.json --corpus-root datasets/corpus/httpx-docs \
  --runtime-store runtime.db [--candidates-out candidates.db] \
  --provider live --record-out responses-recording.json \
  [--observed-at ISO] [--output session-summary.json]
```

- **spec 驱动**：会话由 JSON 定义（`session_id`/`budget_requests`/`items`），逐项校验（空队列、重复 item_id、预算 <1、字段缺失/超集均干净拒绝，exit 2）；同一命令重跑即续跑（resume 语义与 D-034 一致），规格漂移与预算下降浮出为干净 CLI 错误；
- **双 provider**：`live` 走 `OpenAICompatibleClient`（key 只读环境变量 `VERITAS_LLM_API_KEY`，缺失即干净错误；DeepSeek 固定禁用 thinking）并录制；`replay` 从录制确定性重跑（录制进重放、重放不录制，`--record-out` 与 replay 组合被拒绝）；
- **逐项回调**：引擎新增 `on_item_done`（终态迁移与预算停止各触发一次，携带更新后的 item 行）；CLI 用它流式输出 `[session] N/M <item> <status> requests=<spent>` 并逐项保存录制——中断最多丢当前 item 的交换，与 D-034 的逐项 checkpoint 对齐；
- **重跑安全**：已完成的会话重跑重印摘要（exit 0）而非报错——长会话的命令可以盲目重试；
- **确定性摘要**：状态/计数/item 明细（status/attempts/last_error，无时间戳）+ 候选库计数，canonical JSON 后附 `content_hash`。

### 28.2 真实会话证据（冻结）

`artifacts/runtime/httpx-session-m1-3b/`：spec + 7 条 DeepSeek V4-Flash 录制 + 会话摘要（content_hash `84377057…`）。3 题、7/15 请求：EX-001 `citation_ambiguous`、EX-017 `citation_not_found`（均被契约拦截、记为终态拒绝、不落库）；EX-029（as_of 0.24.1 历史视图）完成，1 条候选入库（run 归属 `session:httpx-session-m1-3b`）。两项重放测试钉死：CLI replay 与已提交摘要逐字段相等；完成会话重跑重印同一摘要。本轮 2/3 引用拒绝高于上轮全量跑的 4/30 比例——再次确认单轮方差（D-033 C3），单轮分布不作能力定值。

## 29. M1-4 动态重规划

### 29.1 设计（D-036）

`ReplanPolicy`（默认全关，M1-3 行为逐位保留）定义两个确定性触发器；重试只沿"收窄检索"一条轴——确定性重放下原样重试只会复现同一拒绝，top_k 是运行时唯一可确定性收紧的自由度：

- **拒绝重试（`retry_rejected`）**：item 契约拒绝后，若 `attempts < max_attempts` 且 `effective_top_k > min_top_k`，以 `top_k - 1` 重排一次。重排是事务（`requeue_item`）：状态回 pending、降级宽度持久化、previous 错误码清除——崩溃后重试仍按降级宽度进行，不会回退到原始宽度重复花费。到达次数上限或 top_k 下限即终态拒绝。
- **预算联动（`degrade_to_fit_budget`）**：run/resume 开始时若 pending 队列最坏情况（∑ effective_top_k）超过剩余预算（budget − spent），`degrade_queue_to_fit` 在单事务内确定性降级：每次取最大 effective_top_k 的 pending item（平局取队列序靠前者）减一，floor 为 min_top_k；触底仍不够则不伪装适配，照常在预算处干净停止。预算联动在运行前而非耗尽后——耗尽时剩余预算为零，降级已无意义。

存储 schema 升为 `research-runtime-2`：`work_items.effective_top_k` 与规格身份 `top_k` 分离（规格漂移校验仍用 `top_k`），降级只写 checkpoint 存储。引擎结果暴露 `degraded_items`，CLI 新增 `--retry-rejected`/`--degrade-to-fit` 并在摘要逐 item 暴露 `effective_top_k`；M1-3B 冻结会话摘要由同一录制确定性重导以纳入新字段（原始录制未动，EX-001/017/029 状态与花费逐位不变）。

### 29.2 触发场景验证

7 项新测试（141 → 148）：

- 默认策略保留 M1-3 终态语义（拒绝即终态、宽度不动、无重排）；
- **降级救援**：第二篇文档违约（top_k 2 失败）→ 重排 top_k 1 只见首篇 → 完成（attempts=2、花费 3、1 候选）；
- 双重终止：max_attempts 耗尽后终态；item 已在 top_k 下限时从不重排；
- 预算降级 [3,3]→[2,2] 恰好适配 budget 4（花费 4、4 候选、两 item 完成）；
- 降级触底仍超预算 → 照常干净停止（budget_exhausted、pending 保留）；
- resume 联动：首跑 budget 4 中断于 item-b，resume 提额到 6 且开降级 → 按剩余 2 降级 item-b 至 top_k 2 后完成（总花费 6）；
- CLI 级：`--retry-rejected` 驱动同一救援故事（录制含违约响应），摘要显示 attempts=2、effective_top_k=1。

## 30. M1-5A 端到端集成桥与真实变更事件

### 30.1 GraphBridge 三层翻译（D-037）

`src/veritas/integration/graph_bridge.py` 连接研究侧（M1-2/M1-3 管线与运行时）与演化侧（P0 引擎）：

1. **语料 → SourceVersion**：id 方案与抽取管线一致（`<corpus_id>:<doc_id>@<version>`），证据 `source_version_id` 直接对上已注册源；canonical_uri、content_hash、published_at 全部来自语料 manifest/快照；
2. **会话 bundle → 演进库**：claims/evidence/edges 事务插入（INSERT OR IGNORE，跨会话幂等）；T0 补齐初始 claim 评估（复用 P0 `evaluate_claim`）与 `all_accepted` 结论 v1 + DEPENDS_ON 边——引擎的结论重算依赖这些边；
3. **manifest 历史 → ChangeEvent**：`revision_event(doc, old, new)` 从真实版本表导出 revise 事件，`external_event_id = <corpus_id>/<doc>@<new>` 作幂等键，observed/effective_at 取新版本真实 published_at，新 SourceVersion 预挂 supersedes；`changed_locators` 为空 = 整版本变更范围（无语义 diff 时的诚实默认，宁多验证不漏验证）。

### 30.2 真实修订闭环（冻结场景测试）

用真实语料内容变化驱动完整演化（`tests/scenarios/test_evolution_integration_v1.py`）：index 文档 0.24.1→0.25.2 的 Python 下限句真实修订（"HTTPX requires Python 3.7+" → "HTTPX requires Python 3.8+"，两句均为真实快照原文、唯一定位）。闭环：T0（as_of 2023-06-01）抽取 → 入库 → 旧 claim（`httpx_requires_python_3_7_or_later`）accepted、结论 `python_floor_claim_supported` pass@1 → 真实 revise 事件 → T1 重抽取（as_of 2023-12-01）作为 new_claims 包 → 引擎 apply：旧 claim accepted→**unsupported**（supersession 派生失活，不写 valid_to）、新 claim（`httpx_requires_python_3_8_or_later`）**accepted**、watching 结论 **pass@1 → unknown@2**（fail_statement："no longer supported; re-research required"）；重复 apply 幂等返回同一 run_id、实体计数不变。

负面校准 3 项：`unknown_corpus_version`、`unregistered_old_source_version`、`unregistered_source_version`（namespace 失配证据拒入库，FK 兜底）。

## 31. M1-5B 规模演化 benchmark 与逐事件等价对照

### 31.1 基准构造（D-038）

`src/veritas/evaluation/evolution_benchmark.py` 把 Gate P0 条件二要求的规模声明落在真实语料历史上：

1. **T0 图**：六个文档（advanced、async、compatibility、environment_variables、index、quickstart）各经一次确定性抽取管线 run（`FixtureLLM` 录制按检索结果生成，目标文档断言 watched 事实、其余文档空断言），`GraphBridge.load_bundle` 入库后共 8 个 source versions、6 claims、6 evidence、14 边；`record_initial_assessments` 评估全部 claim，6 个单 claim 结论 + 1 个跨文档聚合结论 `python_floor_claims_supported`（盯 index Python 下限与 env SSLKEYLOGFILE 两条真实事实）；
2. **时间线**：13 个真实内容修订事件，`published_at` 全部来自语料 manifest，按其排序（0.25.2 六文档同一时刻，doc_id 决平局）；内容哈希相同的版本步（如 async 0.25.2→0.26.0）跳过——同内容不是修订，事件允许跨越 SAME 中间版本直达下一个真实内容转移（如 env 0.24.1→0.27.2、quickstart 0.25.2→0.27.2）；
3. **事件两类**：九个幸存修订（watched 句在新版本仍在，T1 bundle 把新证据重挂到同一 claim——`claim_id` 仅由 canonical key 决定，INSERT OR IGNORE 幂等；claim 状态不变，trace 记 `evidence_rebased`）与四个 watched 事实移除（index Python 3.7+→3.8+、quickstart 无解码句移除、compatibility REQUESTS_CA_BUNDLE 段替换、env SSLKEYLOGFILE 整节移除；旧 claim unsupported、替换 claim accepted、watching 结论重算）。

### 31.2 逐事件等价 oracle 与成本口径

每个事件 apply 后立即运行 P0 全量 oracle（`full_recompute_state`：`evaluate_claim` × 所有 claim + `evaluate_conclusion` × 所有当前结论），与存储态逐节点比对，任何漂移抛 `equivalence_violation` 中止——等价是逐事件断言而非只在终点，漂移不会被后续事件掩盖。成本口径：selective = 引擎实际执行的 claim 重评 + conclusion 重算次数；full = 同一时刻 all claims + all conclusions 的反事实计数（与 P0 `full_recompute_ratio` 同语义推广到多事件真实图）。

冻结结果（`artifacts/evolution/m1-5b-benchmark/summary.json`，canonical JSON + `output_hash`，测试字节级复现 + CI 重生成零 diff）：

- selective **23** 次求值 vs 全量 **185** 次，cost ratio **0.1243**（幸存事件 1 次求值 vs 13–14 次，事实移除事件 3–4 次 vs 14–17 次）；
- 4 次语义 claim 变更、9 次 claim 重评未变（重挂证据）、6 次结论重算、5 个结论新版本（聚合结论在事件 4 已翻 unknown，事件 13 重算但无新版本）；
- 终态：4 个 watched claim unsupported（advanced/async 幸存）、3 个单文档结论 unknown、聚合结论 pass→unknown；最终 10 claims / 19 evidence / 33 边。

### 31.3 负面校准与 CI

四项负面校准独立可触发：`quote_not_in_corpus`（计划句不在钉住版本或出现多次）、`unknown_corpus_version`（计划引用语料外版本）、`equivalence_violation`（oracle 可失败——向 `assert_equivalent` 注入漂移态）、`unplanned_extraction_era`（目标文档在计划外时代被检索到即 fail fast），另有事件重放守卫（同一事件包重复 apply 返回同一 run、实体计数与成本不重复累计）。CI 新增 `evolution-benchmark` 任务：重生成 summary 并对已提交 artifact `git diff --exit-code`。

## 32. Gate M1 收口评审

### 32.1 出口条件与核验（D-039）

| 条件 | 核验证据 | 结果 |
| --- | --- | --- |
| 校准 CI 绿 | extraction-calibration 任务重生成零 diff（run 33321253704） | 通过 |
| 运行时可操作且有真实会话证据可重放 | M1-3B 三题 DeepSeek 会话（7 请求）+ CLI 重放测试逐字段钉死 | 通过 |
| 真实语料历史驱动演化闭环且规模等价/成本冻结 | M1-5A 修订闭环 + M1-5B 13 事件逐事件等价、23/185 求值冻结 artifact（CI 零 diff） | 通过 |
| 预算/重规划以真实口径定标（C1） | budget_requests 必填、降级轴确定性触发 | 通过 |
| 双文档与决策记录完整 | D-029..D-038、完成记录 11.6..11.15、双变更记录无欠账 | 通过 |

评审基线：commit 6ea0dad（分支 `codex/m1-5-evolution-integration`），CI run 33321253704 六任务全部成功。结论：**Gate M1 通过**，M1 关闭。

### 32.2 携带项与 M2 进入条件

- **C2**：候选聚合仍只暴露不合并——真实模型 key 级 recall 2/32、quickstart@0.28.1 十五候选十五 key 的改写碎片是图可信度的主导威胁；M1-5B 的幸存事件在真实模型下会退化为改写 churn。
- **C3-R**：同一契约下的重复运行对照未做，单轮方差不能当作模型能力定值。
- **C4**：检索 MRR 0.7222 是词面基线上限，语义检索未评估。
- M2 主题定为"**从候选到可信图**"（语义质量档）：M2-1 候选语义聚合（确定性相似度 + 硬守卫的簇身份层，候选存储保持只追加不动）、M2-2 簇级结论与冻结校准、M2-3 同契约重复运行。自主研究闭环（查询规划、watch 模式、自动再研究）排为 **M3**——自主性建在碎片化图上是在自动化噪声，先让图可信，自主决定才有可靠反馈。

## 33. M2-1 候选语义聚合

### 33.1 确定性相似度与硬守卫（D-040）

`src/veritas/aggregation/clusterer.py`：语句 → 小写 token（版本号整 token 化，`3.8` 不拆成 `3`/`8`），两类**硬守卫**在相似度计算前直接判"永不合"（返回 `None`）：

1. **数字/版本守卫**：数字 token 集必须完全一致——`HTTPX requires Python 3.7 or later` 与 `3.8 or later` 永远分开（P0 真实 floor 修订的直接教训：词面相似不等于事实相同）；
2. **否定守卫**：否定 token 集必须一致——正/反陈述分开。

守卫之外，相似度 = 内容 token Jaccard（数字 token 排除在内容集外由守卫管理；否定词不进停用词表而是被守卫管理）。停用词表冻结在模块内。

### 33.2 ClaimClusterStore 与身份重映射

`store.py`（schema `claim-clusters-1`）：

- **代表冻结**：簇的 representative key 在创建时定格，成员只挂靠、不改键——下游 claim id 终生稳定；
- **单趟指派**：新语句与所有簇代表（按 key 排序）算相似度，加入得分最高且 ≥ 阈值的簇（平局取字典序最小代表），否则自立新簇；簇创建后不再合并（文档化的召回换稳定性取舍）；
- **审计行**：成员行携带 method（founder/lexical）与 score；
- **打开守卫**：schema 漂移 → `schema_drift`；阈值不一致 → `policy_drift`（不同阈值下的决策不可比）；key 与语句重派生不匹配 → `canonical_key_mismatch`。

`resolve.py` 的 `resolve_bundle` 在运行时抽取后执行：逐 claim 经簇存储解析 → claim id 重算为 `claim_id_for(representative_key)` → 证据边重挂（边 id 重算）→ 同簇塌缩去重。证据 span 与原始 assertion 记录不动，候选存储保持 pre-aggregation 真相（C2 的"只暴露"层不被动）。

### 33.3 冻结校准（真录 52 候选 × 32 gold）

阈值 0.375 从 M1-2C2 真录（DeepSeek V4-Flash）与 gold 断言的成对分布校准并冻结：真改写配对最低 **0.385**（EX-027 CA bundle 句），异事实配对最高 **0.364**（EX-012 JSON 编码句），逐对人工审核零假合并。结果：**簇级覆盖 19/32**（精确 key 基线 3/32，≈6.3 倍）；4 个被契约拒绝的 case 无候选不参与；EX-029（版本号钉死）与 EX-030 由数字守卫正确排除。校准钉进 `tests/scenarios/test_aggregation_m2_1.py`。

## 34. M2-2 簇级结论与冻结校准

### 34.1 校准的正式化（D-041）

`veritas.evaluation.aggregation_calibration` 把 M2-1 的校准从测试内助手提升为正式模块：重放 committed 录制 → 候选配对 → `run_calibration` 返回 canonical JSON（policy、counts、exact/cluster 覆盖 case 列表、19 组 matched_pairs 明细）→ `write_summary` 加 output_hash。冻结 artifact `artifacts/aggregation/m2-1-calibration/summary.json` 由测试字节复现，CI `aggregation-calibration` 任务重生成并 `git diff --exit-code`——阈值、真录、gold 三者任一漂移都会被拦下。

### 34.2 演化侧对照：改写幸存 vs churn

`tests/scenarios/test_cluster_evolution_m2_2.py` 在真实本地语料（retries 文档 1.0→2.0，watched 句幸存、内容真实变更）上驱动同一次修订跑两遍，唯一自由度是 `cluster_store` 的有无：

- **开簇**：T1 模型改写同一事实（同引文、不同表述——真实 C2 噪声模式），`resolve_bundle` 使改写重入 T0 claim 的簇，claim id 不变；引擎侧旧证据经 supersession 失活、新证据重挂同一 claim → claim 保持 ACCEPTED（`rechecked_unchanged`）、结论停在 v1 pass、`recomputed_conclusions` 为空——**零 churn**；
- **关簇**：改写成为新 claim → 旧 claim UNSUPPORTED、结论 pass@1→unknown@2——M1-5B 幸存故事在真实模型行为下的退化形式。

这一对照把 M2-1 的召回数字转译为演化行为差异：聚合消除的是"事实未变而结论翻 unknown"的假警报来源。

## 35. M3-A 自主闭环：再研究规划器与刷新事务

### 35.1 规划器（D-042）

`src/veritas/autonomy/planner.py` 的 `plan_re_research(repository, *, session_id, top_k=3, requests_per_item=3)`：遍历当前结论，outcome ≠ PASS 的 `all_accepted` 结论的依赖 claim（跨结论去重）各生成一个研究 item——question = claim statement，query = statement 的内容/数字 token（与聚合同一词表，数字保留），item_id `RR-NNN` 按 (conclusion_key, claim_id) 序确定性编号，budget = 3×items。`ReSearchPlan.to_spec()` 即 runtime CLI spec 格式、`save()` 直接落盘——运行时零改动。其余 rule kind 抛 `PlanningError("unsupported_rule_kind")`；悬空 claim 引用抛 `unknown_claim`；全 PASS 产出空计划（budget 下限 1）。

### 35.2 刷新事务

`refresh.py` 的 `apply_research_refresh(repository, *, bundle, session_id, rule_version, refreshed_at)`：再研究没有源变更事件，P0 `apply` 不适用。刷新在单事务内：守卫（bundle 非空；每条证据的源必须已注册且**活跃**——存储层新增 `source_is_active`，复用派生 current-view 的 supersedes/retract 两个 NOT EXISTS 子句，违反即 `superseded_source`/`unregistered_source`）→ 插入 claims/evidence/edges → 按引擎同款契约重评受影响 claim（previous 对比、语义变化集合）→ 语义交集内的结论重算并按需出新版本（DEPENDS_ON 边重建）→ 审计写入专用 `research_refreshes` 表。refresh_id 由 (session_id, bundle 内容) 决定性派生，重复 apply 幂等返回已存 payload。刷新绝不冒充变更事件进 change log。

### 35.3 修复弧线（冻结场景）

`tests/scenarios/test_autonomy_loop_m3.py` 在真实本地语料上跑完整弧线：T0（聚类）claim accepted + 结论 pass@1 → 真实修订后无聚类再研究（pre-M2 路径）→ claim unsupported、结论 unknown@2 → `plan_re_research` 产出 spec（question = 原 claim statement、query = "httpx retries connection setup failures"）→ 聚类再研究 bundle 经刷新写回 → claim 回 ACCEPTED、结论 pass@3；重复刷新幂等（实体计数不变）。

## 36. M3-B watch 模式与一条命令闭环

### 36.1 漂移检测与四段编排（D-043）

`autonomy/watch.py`：`detect_drift(repository, corpus)` 用新增的 `list_source_versions()` 枚举演化库源、以 `source_is_active`（派生 current-view）取每源当前版本，对照语料 manifest 最新版；版本标签相同或内容哈希相同都不是漂移（M1-5B SAME 步语义的一致延伸）。`run_watch_loop` 四段：

1. **漂移**：bridge revise 事件**不带新 claims** 应用——claim 失去支持、结论翻 unknown（"源已变、再研究待做"的诚实中间态）；模型调用为零；
2. **规划**：`plan_re_research` 从非 PASS 结论生成 runtime spec；
3. **研究**：真实 runtime 会话（预算/checkpoint/聚类）执行计划；引擎新增 `on_item_bundle` 回调把每个解析后 bundle 交给循环；
4. **刷新**：`apply_research_refresh` 写回；报告含漂移清单、计划、会话账目、刷新明细、最终结论状态。

### 36.2 CLI 与无操作不变式

`python -m veritas.autonomy`（`autonomy/cli.py`）：live/replay 双 provider、录制、`--output` 报告落盘，与 runtime CLI 同一风格。空计划跳过会话（runtime 拒绝空会话是契约，不绕开）。**第二轮无操作不变式**有测试钉死：世界没变、结论全 PASS 时——零漂移、空计划、零请求、零刷新。自主系统的安全声明：没有可做的工作时，agent 一步都不走。

## 37. M3-R 可靠性收口

### 37.1 runtime outbox 与 at-least-once 交付（D-044）

`research-runtime-3` 新增 `item_outputs`：canonical bundle JSON/hash、`pending|applied|ignored` 交付状态、刷新 id 与交付时间。`complete_item_with_output` 在一个事务内同时写 bundle 并把 work item 置为 `completed`，因此不存在“item 已完成但恢复所需输出只在内存”的状态；`ExtractionCandidateBundle.from_dict` 负责数据库 reopen 后的完整重建。v2→v3 是加表式迁移，未知 schema 仍 fail closed。

`run_watch_loop` 每次执行前后都 drain outbox：先提交 `apply_research_refresh`，再确认 output；若两步之间退出，重启会以完整 payload + rule version 派生的 refresh id 幂等重放。空 bundle 明确记为 `ignored`。报告暴露 pending/applied/ignored 数量，不把“runtime completed”误写成“图已更新”。

### 37.2 事务、不可变身份与副作用边界

- `GraphBridge.revision_event` 只构造 `ChangeEvent/SourceVersion`，不在 `EvolutionEngine.apply` 前写库；`load_bundle` 的 source/claim/evidence/edge 是一个事务；检测时间进入 `observed_at`，来源发布时间只作 `effective_at`；
- SQLite 的 immutable insert 在 `INSERT OR IGNORE` 后回读并逐字段比对；同 ID 同 payload 幂等，同 ID 不同 payload 抛 `immutable_entity_conflict`，不再静默吞错；
- cluster representative 冻结 statement、canonical key 与 founder `created_at`；后续改写重入时生成完全相同的 Claim。EvolutionEngine 对同 statement/key 的确定性 claim id 复用首次登记时间，对 statement/key 冲突继续拒绝；
- refresh id 覆盖 session、rule version 与完整 bundle（含 edges），避免“claim/evidence 相同但规则或边不同”被误判为同一刷新。

### 37.3 恢复上下文、录制与故障注入证据

`session_contexts` 将 watch session 绑定到 corpus、provider model、cluster/candidate store 路径、project、rule 与首次 `observed_at`。晚些重开同一 session 会沿用首次时间语义；稳定配置漂移以 `session_context_drift` 拒绝。live CLI 的 `RecordingLLM` 在每个 item 终态回调保存，和数据库 checkpoint 对齐。

故障注入覆盖两个关键窗口：(1) output 已与 item 终态提交、session 尚未完成；(2) graph refresh 已提交、outbox ack 尚未提交。两者均关闭并重开 SQLite 文件，恢复后结论正确、请求数不增加、实体计数不重复；规格不匹配负例还验证 item 终态与 outbox 同时回滚。Python 3.14.7 严格 `ResourceWarning` 模式本地全量 **200/200** 通过；Python 3.11 需由 CI 再确认。

## 38. M2-3 同契约重复运行与方差对照

### 38.1 第二轮 live 运行（D-045）

v3 契约原样重跑 30 题 live 校准（`deepseek-v4-flash`，非思考、temperature=0、JSON mode），证据入库 `artifacts/extraction/httpx-initial-extraction-3.0.0-deepseek-v4-flash-repeat1/`：0/30、critical=0、EX03×3/EX04×27、citation alignment 0.9、key 级 recall 1/32。重放测试把录制钉成确定性证据。

### 38.2 方差对照（C3-R 收口）

`veritas.evaluation.run_variance` 重放两份冻结录制，三层数字：

- **run 级**：run1 52 候选/52 key/4 拒绝 vs run2 66/66/3；
- **key 级**：共享 37 / 并集 81，**Jaccard 0.457**——同一契约、temperature=0，模型两轮只重复不到一半的断言；
- **case 级**：仅 8/30 两轮断言集相同，22 题不同；拒绝 case 大体稳定（EX-001/002/025 共有，EX-026 仅首轮被拒）。

结论登记：EX03 4→3、EX04 26→27、recall 2/32→1/32 的波动证实**单轮失败分布不是能力定值**（C3-R 收口）；两轮 critical 均为 0——契约完整性在构造上稳定，波动全部集中在改写层，这正是 M2-1 簇级身份要吸收的方差（19/32 簇级覆盖）。对照 artifact 冻结 + CI `run-variance` 零 diff。

## 39. M3-C 真实模型驱动的 watch 闭环（live 证据）

### 39.1 T0 引导（--init-spec）

`run_t0_init`（`autonomy/watch.py`）让 `python -m veritas.autonomy --init-spec` 在 watch 循环前完成引导：逐 item 走抽取管线（live/replay），经 `resolve_bundle`（聚类）后由桥装载、全量初始评估、每 item 一个 `all_accepted` 结论（key 由 item_id 消毒派生）。引导只触演化库，不碰会话/上下文机制；契约拒绝的 item 记录 `rejected` 后跳过（不崩溃）；重跑安全（INSERT OR IGNORE + 已评估跳过）。

### 39.2 真实 live 证据（`artifacts/autonomy/live-watch-demo/`，D-046）

两遍 live 调用（DeepSeek `deepseek-v4-flash`，真实语料、真实漂移、共约 15 请求）：

- **第一遍**（init + watch-1）：DEMO-2 被契约正确拒绝（`citation_not_found`）；三个真实漂移（advanced→0.26.0、async→0.28.1、index→0.28.1）；再研究对旧事实（Python 3.7+）在 0.28.1 语料中**找不到证据**——空 bundle 被标记 ignored，结论诚实停在 unknown。这是"事实真变更"分支：agent 不掩饰、不假修复。
- **第二遍**（init DEMO-3 + watch-2）：幸存事实（redirect 行为）引导通过后遭遇真实漂移（compatibility/quickstart→0.28.1）；再研究在新版本重申同一事实，刷新把它重挂回原 claim——结论 **pass@1 → unknown@2 → pass@3**（`t0_demo_3@3`）。这是"事实幸存 + 聚合重挂"分支。

两份报告 + 两份录制 + 两份 spec 全部入库；重放测试以钉住的 `observed_at` 从录制逐字节复现两份报告——live 证据本身可确定性重验。

### 39.3 顺带的身份修复（D-046）

演示暴露了一个真问题：M3-R 的刷新身份哈希了整个 bundle（含 `documents` 的 token 计费元数据），导致同一语义刷新在 live 与 replay 下 id 不同。已修复为只对**写入图的语义载荷**（claims/evidence/edges + session/rule）取身份——计费元数据不是语义，不该参与内容寻址。

## 40. Gate M2/M3 联合评审（D-047）

### 40.1 评审基线与核验方法

- 评审基线：commit b99822f，其 CI 八任务全绿（unit 3.11/3.14、suite 1.0.0/2.0.0、extraction-calibration、evolution-benchmark、aggregation-calibration、run-variance，全部重生成零 diff）；
- 评审日本地复跑（Python 3.14.7，严格 `ResourceWarning`）：`python -m unittest discover -s tests` → **206/206 OK**；两 suite + extraction-calibration + evolution-benchmark + aggregation-calibration + run-variance 重跑后 `git diff --exit-code -- artifacts` 零差异；
- 联合评审的理由：M3 的 live 修复证据（D-046）以 M2 聚合为前提，两阶段出口共享同一批冻结材料，拆分评审只会复述数字。

### 40.2 M2 出口条件核验（"从候选到可信图"）

| 出口条件 | 证据 | 结论 |
| --- | --- | --- |
| 簇级身份落地 | 硬守卫（数字/否定集必须一致）+ 真录校准冻结阈值 0.375（真改写 ≥0.385 / 异事实 ≤0.364）；簇级覆盖 **19/32** vs 精确 key 3/32；19 组配对逐对审核**零假合并**；负向校准四类（canonical_key_mismatch / invalid_statement / policy_drift / schema_drift） | 通过（M2-1，D-040） |
| 聚合价值在演化侧证明 | 开簇：改写重入同一 claim、证据重挂、结论停 v1 pass、`recomputed_conclusions` 空——零 churn；关簇：结论 pass@1→unknown@2；校准 artifact 进 CI 零 diff | 通过（M2-2，D-041） |
| 方差口径钉死 | 同契约第二轮 live 可重放；key 级重合率 Jaccard **0.457**（共享 37 / 并集 81）、case 级 8/30 相同、单轮分布非能力定值；对照 artifact 进 CI 零 diff | 通过（M2-3，D-045） |

Gate M1 携带项 **C2（候选聚合）与 C3-R（同契约重复运行）正式关闭**。

### 40.3 M3 出口条件核验（受控自主闭环）

1. **规划器 + 刷新事务**（M3-A/D-042）：非 PASS `all_accepted` 结论 → 确定性 runtime spec；刷新按引擎同款转换契约写回、活跃源守卫、`research_refreshes` 审计、幂等；修复弧线 pass@1→unknown@2→pass@3 有测试；
2. **watch 一条命令 + 无操作不变式**（M3-B/D-043）：漂移检测→规划→预算会话→刷新四段编排；第二轮对未变世界零请求零刷新；
3. **可靠性收口**（M3-R/D-044）：item 输出与 bundle 原子 checkpoint（schema v3）、at-least-once outbox + 幂等 sink、双崩溃窗口故障注入（关闭/重开 SQLite）恢复后零额外请求零重复实体；
4. **真实模型 live 双分支证据**（M3-C/D-046）：`artifacts/autonomy/live-watch-demo/`——第一遍事实真变更（3.7 floor 消失）→ 空 bundle ignored、结论诚实 unknown；第二遍幸存事实（redirect）→ 聚合重挂、pass@1→unknown@2→pass@3；重放测试以钉住 `observed_at` 逐字节复现两份报告。

### 40.4 携带项与 M4 进入条件

- **C4**：检索 MRR 0.7222 为词面基线上限（自 Gate M1 携带，未处理）；
- **C5**：聚类阈值 0.375、19/32 覆盖与零假合并均来自单模型真录，跨模型/跨领域外推未验证，12/32 改写缺口未归因；
- **C6**：自主规划覆盖面——仅支持 `all_accepted` 规则的非 PASS 结论、query 为词面 token、无 web 发现与开放式工具规划；
- **M4 进入条件**：本评审通过。M4 主题"接入真实世界源"（web search 集成）；M4-1 = 版本化 web 来源适配器：把语料 canonical UTF-8/LF hash 契约推广到可重抓取的 web 源（fetch→内容 hash→manifest 记录→SourceVersion，同 URL 多次抓取构成版本时间线），漂移检测从"对照本地 manifest"扩展为"对照上次抓取"。C4/C5/C6 不阻塞 M4（质量外推、规划覆盖与世界边界三者正交），但真实源会同时改变三者面临的分布，故在 M4 内重估。

## 41. 当前限制

- 自主 planner 只处理本地版本化语料上的非 PASS `all_accepted` 结论；没有真实网页发现、抓取、版本轮询或开放式工具规划（D-047 C6，M4 主题即此）；
- M3-R 证明单机 SQLite reopen/replay 和单 writer 事务边界，不证明任意 OS kill 点、多进程争用、网络分区、分布式 exactly-once 或性能上限；
- live provider 的逐 item 录制会保留已完成 item；若进程在 provider 已返回、数据库终态尚未提交前退出，预留预算不会超支，但该次响应仍可能需要重新调用；
- 同一运行期间 corpus 再次变化会触发 `session_world_drift`，旧 session 不混入新世界；当前没有自动取消/迁移旧 pending session 的策略；
- 聚类冻结阈值只在一份真实录制上达到 19/32 coverage 且观察到零假合并；M2-3 已量化同契约重复运行方差（key 级重合率 0.457），但阈值与覆盖的跨模型、跨领域外推仍未验证（D-047 C5）；
- 没有来源质量权重、复杂逻辑表达式、概率置信度、循环依赖消解或独立 Fact 层；
- `expire`/`retract` 的自动时间推进、as-of 历史查询、多来源 conflict 仲裁仍未实现；storage protocol 仍不是完整可替换读取边界；
- M1-5B 的 23/185 成本结果只适用于冻结的六文档、13 事件基准；不能外推生产规模。

因此，当前可以确认的是：M3 的本地自主闭环已具备可持久恢复的 item 输出、幂等图交付、严格事务/身份冲突守卫和恢复上下文约束，且 Gate M2/M3 已评审通过（D-047）；不能据此声称真实 Web Research、通用模型质量或生产级容灾已经完成。

## 42. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-09-02 | Gate M2/M3 | 联合收口评审（D-047）：基线 b99822f（CI 八任务全绿）+ 评审日复跑 206/206 tests、四工件零 diff；M2 三条出口条件核验通过、Gate M1 携带项 C2/C3-R 关闭；M3 四块证据核验通过（规划器/watch/可靠性/live 双分支可重放）；携带 C4/C5/C6 进 M4；M4 主题"接入真实世界源"、M4-1 = 版本化 web 来源适配器 |
| 2026-09-02 | M3-C | 真实模型 watch 闭环 live 证据（D-046）：`--init-spec` T0 引导；两遍 live（约 15 请求）钉死两条分支——事实真变更诚实停 unknown、幸存事实经聚合重挂修复 pass@1→unknown@2→pass@3；报告+录制+spec 入库，重放测试逐字节复现；顺带修复刷新身份混入计费元数据的缺陷；206/206 tests |
| 2026-09-02 | M2-3 | 同契约重复运行对照（D-045）：第二轮 live 30 题录制入库可重放（0/30、critical=0）；`run_variance` 冻结 artifact——key 级重合率 0.457（37/81）、case 级 8/30 相同；C3-R 收口：单轮分布非能力定值；203/203 tests |
| 2026-08-31 | M3-R | 可靠性收口（D-044）：runtime schema v3（原子 item+bundle outbox、session contexts、v2 加表迁移）；watch at-least-once/幂等 refresh 交付与双崩溃窗口恢复；完整 refresh identity；GraphBridge 纯构造 + bundle 事务；immutable payload 冲突显式拒绝；代表 claim 完整身份冻结；live 录制按 item 保存；200/200 tests |
| 2026-08-30 | M3-B | watch 模式与一条命令闭环（D-043）：`detect_drift` + `run_watch_loop` 四段编排（漂移→规划→预算会话→刷新，漂移事件不带 claims）；CLI `python -m veritas.autonomy`；引擎 `on_item_bundle`、存储 `list_source_versions`；一轮修复 unknown@2→pass@3 + 二轮无操作不变式；190/190 tests |
| 2026-08-30 | M3-A | 自主闭环前两块拼图（D-042）：`src/veritas/autonomy/`——`plan_re_research`（非 PASS 结论 → runtime spec 格式确定性研究计划）+ `apply_research_refresh`（活跃源守卫、引擎同款转换契约、`research_refreshes` 审计表、幂等）；存储层新增 `source_is_active`；修复弧线 pass@1→unknown@2→pass@3 场景钉死；188/188 tests |
| 2026-08-30 | M2-2 | 簇级结论与冻结校准（D-041）：校准提升为正式模块 + 冻结 artifact（exact 3/32、cluster 19/32、19 组配对明细）+ CI 零 diff；演化侧对照场景钉死聚合价值——开簇改写再研究重入同一 claim（零 churn、结论停 v1 pass），关簇改写 churn（结论 pass@1→unknown@2）；179/179 tests |
| 2026-08-30 | M2-1 | 候选语义聚合（D-040）：`src/veritas/aggregation/`——相似度 + 数字/否定硬守卫、`ClaimClusterStore`（代表冻结/单趟指派/审计行/policy_drift）、`resolve_bundle` 身份重映射；运行时可选接入默认全关、CLI `--cluster-store`；阈值从真录校准冻结 0.375（真改写 ≥0.385 / 异事实 ≤0.364，零假合并），簇级覆盖 19/32 vs 精确 key 3/32；178/178 tests |
| 2026-08-30 | Gate M1 | 收口评审（D-039）：出口条件五条核验通过（基线 6ea0dad、CI run 33321253704 六任务成功）；携带 C2/C3-R/C4 进 M2；M2 主题"从候选到可信图"、自主闭环排 M3；M1 关闭 |
| 2026-08-30 | M1-5B | 规模演化 benchmark（D-038）：`evolution_benchmark` 六文档 T0 图 + 13 个真实内容修订事件（9 幸存 + 4 watched 事实移除，manifest `published_at` 排序、SAME 哈希步跳过）；逐事件 full-recompute 等价 oracle（漂移即 `equivalence_violation`）；冻结成本声明 selective 23 vs 全量 185 求值（ratio 0.1243），summary 提交 + 测试字节复现 + CI 零 diff；161/161 tests |
| 2026-08-30 | M1-5A | 端到端集成（D-037）：`GraphBridge`（语料→SourceVersion 对齐管线 id、bundle→演进库+T0 评估/结论、manifest 历史→revise 事件）；真实修订 index 0.24.1→0.25.2（Python 3.7+→3.8+）驱动 P0 引擎在真实抽取图上完成演化闭环，结论 pass@1→unknown@2，apply 幂等；152/152 tests |
| 2026-08-30 | M1-4 | 动态重规划（D-036）：`ReplanPolicy`——拒绝以 top_k-1 重排一次（降级宽度持久化、max_attempts/min_top_k 双终止）、预算压力运行前确定性降级适配（最大优先/平局按队列序/触底不伪装）；schema `research-runtime-2`（effective_top_k 与规格身份分离）；CLI 旗标与摘要暴露；M1-3B 冻结摘要由同一录制重导；148/148 tests |
| 2026-08-30 | M1-3B | Runtime CLI（D-035）：spec 驱动会话 + live/replay 双 provider + 逐项进度流与崩溃安全录制 + 重跑安全 + 确定性摘要；引擎新增 `on_item_done`；真实 DeepSeek 3 题会话证据入库（7 请求、2 引用拒绝被契约拦截、1 完成）并由 CLI 重放测试逐字段钉死；M1-3 阶段收口；141/141 tests |
| 2026-08-30 | M1-3A | Research Runtime 引擎（D-034）：独立会话存储 `research-runtime-1`（sessions/work_items）+ `ResearchRuntime` 引擎；逐项 checkpoint 事务，恢复跳过终态、规格漂移/预算下降/已完成会话重跑均拒绝；预算 reserve-then-call 原子预留（崩溃宁少花不超支）、耗尽干净停止、提额恢复；契约拒绝即终态记录错误码；候选经 D-032 身份幂等落库（run 归属 `session:<id>`）；13 项新测试含崩溃恢复收敛与无崩溃参考 run 等价断言；134/134 tests |
| 2026-08-30 | Gate M1-2 | 收口评审：出口条件三条逐项核验（校准 CI 绿/真实录制可重放/30 题基线），评审日 HEAD 复跑 121/121 + 双 suite + 校准零 diff；结论通过、携带 C1～C3（真实口径输入、聚合只暴露不合并、单轮方差）；M1-2 收口，M1-3 进入条件更新（D-033） |
| 2026-08-30 | M1-2D | 抽取候选事务持久化：新增 `CandidateStore`（schema `extraction-candidates-1`，独立于 P0 冻结存储），身份 `(source_version_id, canonical_key, content_hash)`、`INSERT OR IGNORE` 去重、观测表记录 run 归属（D-032）；完整性守卫（key 重派生比对/relation 白名单/空内容/schema 漂移）整批回滚；`on_case_done` 升级携带 bundle，fixture/live 双路径 `--store-out` 逐 case 事务落库，summary 逐字节不变；冻结证据：fixture 金标准并集 32 幂等重放、live 52 候选（51 key，同 key 跨源分立）、quickstart@0.28.1 十五候选十五 key 且 EX-014 gold key 全部缺席（改写噪声入库）、跨 run 合库 84/84/80 且 live 对金标准零命中；121/121 tests |
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
