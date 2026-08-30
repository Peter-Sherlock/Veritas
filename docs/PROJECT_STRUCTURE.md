# Veritas 项目结构与设计文档

> 文档职责：记录项目边界、设计思路、目录演进、阶段状态和关键决策。  
> 当前阶段：M1-2 抽取 pipeline 与校准
> 当前状态：M1-2 in progress；M1-2A/M1-2B/M1-2B2 complete；M1-2C next
> 更新日期：2026-08-30  
> 上位设计：[Veritas 初期项目设计文档](<../Veritas-Initial-Design(2).md>)  
> 配套文档：[技术实现文档](TECHNICAL_IMPLEMENTATION.md)

## 1. 双文档维护规则

从 P0-0 开始，每个阶段必须同时维护以下两份长期文档。

| 文档 | 回答的问题 | 必须更新的内容 |
| --- | --- | --- |
| `TECHNICAL_IMPLEMENTATION.md` | 具体怎样实现、怎样验证？ | 数据模型、接口、算法、测试、实际运行结果、已知限制 |
| `PROJECT_STRUCTURE.md` | 为什么这样设计、项目现在处于哪里？ | 模块边界、目录、阶段、决策、取舍、下一道门槛 |

维护原则：

1. 两份文档必须在同一阶段一起更新；
2. 设计中的计划结构必须标注为“计划”，不能写成已经存在；
3. 只有代码和测试实际通过后，技术文档才能把能力标为“已实现”；
4. 阶段结束时，两份文档都要增加变更记录；
5. 设计决策发生改变时保留旧决策及原因，不静默覆盖历史。

## 2. 项目核心设计判断

Veritas 的首要研究对象不是搜索质量，而是证据变化后的研究状态演化。

核心链路为：

```text
不可变来源版本
  → 可定位证据跨度
  → 原子 Claim
  → 版本化 Conclusion
  → 来源变化事件
  → 候选影响传播
  → 语义重新验证
  → 最小范围结论修复
  → 可解释的新旧版本差异
```

项目是否值得继续，应先由这条链路的正确性和成本收益决定，而不是由 UI、搜索 provider 数量或 Agent 数量决定。

## 3. 当前实际结构

截至 2026-08-30，工作区实际存在：

```text
Veritas/
├── .git/                       # codex/m1-2-extraction-calibration 阶段分支
├── .github/workflows/tests.yml # 双 Python、双 suite、extraction calibration
├── .gitattributes              # 跨平台文本统一为 LF
├── .gitignore
├── README.md                    # 项目首页、验证摘要与运行入口
├── pyproject.toml
├── Veritas-Initial-Design(2).md
├── src/veritas/
│   ├── domain/
│   │   ├── enums.py
│   │   └── models.py
│   ├── evidence/
│   │   ├── graph.py
│   │   └── rules.py
│   ├── invalidation/
│   │   ├── impact.py
│   │   └── repair.py
│   ├── storage/
│   │   ├── protocol.py
│   │   └── sqlite.py
│   ├── extraction/
│   │   ├── models.py         # contract error、抽取结果与 candidate bundle
│   │   └── pipeline.py       # strict parser、quote alignment、候选物化
│   ├── evaluation/
│   │   ├── scenario.py
│   │   ├── metrics.py
│   │   ├── runner.py
│   │   ├── suite_runner.py
│   │   └── extraction_runner.py
│   ├── providers/
│   │   └── llm.py            # LLM 协议、OpenAI 兼容客户端、Fixture/Recording
│   └── search/
│       ├── provider.py       # 检索协议
│       └── local_corpus.py   # 本地版本化语料 TF-IDF
├── scripts/
│   └── harvest_corpus.py     # 一次性语料采集工具（非 runtime）
├── datasets/
│   ├── corpus/
│   │   └── httpx-docs/       # 10 篇文档 × 48 版本快照 + manifest.json（hash 钉住）
│   ├── extraction/
│   │   ├── httpx-m1-2a/      # 冻结 10 题 benchmark v1.0.0 + fixture responses
│   │   └── httpx-m1-2b/      # 扩容 30 题 benchmark v2.0.0 + 生成式 fixtures
│   ├── scenarios/
│   │   ├── GS-001/scenario.json
│   │   ├── GS-002/scenario.json
│   │   ├── GS-003/scenario.json
│   │   ├── GS-004/scenario.json
│   │   └── GS-005/scenario.json
│   └── suites/
│       ├── p0-evolution-suite.json        # 冻结 1.0.0
│       └── p0-evolution-suite-2.json      # 2.0.0：五场景 + 声明式验收
├── tests/
│   ├── unit/
│   │   ├── test_domain_and_graph.py
│   │   ├── test_expire_and_conflict.py
│   │   ├── test_extraction.py
│   │   ├── test_extraction_taxonomy.py
│   │   ├── test_failure_taxonomy.py
│   │   ├── test_httpx_corpus.py
│   │   ├── test_local_corpus.py
│   │   ├── test_providers.py
│   │   └── test_snapshot_registry.py
│   └── scenarios/
│       ├── test_gs001.py
│       ├── test_gs002.py
│       ├── test_gs003.py
│       ├── test_gs004.py
│       ├── test_gs005.py
│       ├── test_extraction_calibration.py
│       ├── test_extraction_calibration_v2.py
│       ├── test_p0_suite.py
│       └── test_p0_suite_2.py
├── artifacts/
│   ├── extraction/
│   │   ├── httpx-initial-extraction-1.0.0/summary.json
│   │   └── httpx-initial-extraction-2.0.0/summary.json
│   ├── GS-001/run-f39dacf198a857ae/<five JSON artifacts>
│   ├── GS-002/run-65365880276d316f/<five JSON artifacts>
│   ├── GS-003/run-046dcc6b4ed54440/<five JSON artifacts>
│   ├── GS-004/run-eb6622de84727a41/<five JSON artifacts>
│   ├── GS-005/run-16ba47710b6847fb/<five JSON artifacts>
│   └── suites/
│       ├── p0-evolution-suite-1.0.0/
│       │   ├── runs/GS-001..003/<run-id>/<five JSON artifacts>
│       │   └── summary.json
│       └── p0-evolution-suite-2.0.0/
│           ├── runs/GS-001..005/<run-id>/<five JSON artifacts>
│           └── summary.json
└── docs/
    ├── PROJECT_STRUCTURE.md
    └── TECHNICAL_IMPLEMENTATION.md
```

当前 Git 状态：M1-2A 在 `codex/m1-2-extraction-calibration` 阶段分支开发，基线为已同步的 `main@de24dbd`；private 远程仓库为 [Peter-Sherlock/Veritas](https://github.com/Peter-Sherlock/Veritas)。SQLite 运行数据库与 Python 缓存由 `.gitignore` 排除，源码、文档与 JSON 由 `.gitattributes` 统一为 LF。

当前仍没有：

- Web Search 或真实来源抓取；
- 经验证的真实 LLM 抽取，以及候选写入 Evidence Graph 的 transaction；
- 产品化 CLI 或服务接口；
- Research Runtime、checkpoint、预算控制或并发执行；
- 足以代表通用检索质量的 benchmark 结论；
- 生产规模 benchmark 结果。

当前已有的是五个受控场景上的确定性 evidence-evolution runtime、两套 evolution suite、可替换的 LLM/检索协议、冻结语料、检索→严格抽取→Evidence/Claim 候选的 30 题 fixture benchmark（v2.0.0），以及 EX01～EX05 抽取失败分类与独立负向校准；这些模块尚未组成会自主搜索、持久化研究状态、规划、调用工具和生成研究报告的完整 Deep Research Agent。

## 4. P0-0 阶段边界

### 输入

- 初始项目设计文档；
- “先验证 Evidence Evolution Mechanism”的阶段选择；
- 一个本地模拟技术文档变化场景。

### 产物

- 最小实体及边语义；
- candidate impact 与 confirmed invalidation 的严格边界；
- GS-001 黄金样例；
- Ground Truth 集合；
- 结构化输出契约；
- P0-0 验收断言；
- 后续最小目录规划。

### 不进入本阶段

- 代码脚手架；
- 联网检索；
- LLM；
- 数据库选型实现；
- 多 Agent；
- Trace Viewer；
- 完整 Deep Research Loop。

## 5. P0-1 结构落地结果

P0-1 基本按 P0-0 计划落地，并做了三项窄调整：

1. 新增 `evaluation/scenario.py`，把 fixture 构造、input snapshot hash 与 runner 分离；
2. 只创建 GS-001，严格没有提前创建 GS-002/003；
3. artifacts 目录保存实际逐运行 JSON，SQLite 运行数据库通过 `.gitignore` 排除。

该结构仍只服务于 P0 Evidence Evolution Mechanism。初始设计中的 search、planning、memory、providers、observability 等完整模块要等 Gate P0 通过后再逐步引入。

### 5.1 P0-2B 结构落地

P0-2B 已按冻结边界新增以下结构：

```text
Veritas/
├── datasets/
│   ├── scenarios/
│   │   ├── GS-002/scenario.json
│   │   └── GS-003/scenario.json
│   └── suites/
│       └── p0-evolution-suite.json
├── src/veritas/evaluation/
│   └── suite_runner.py
├── tests/
│   ├── scenarios/
│   │   ├── test_gs002.py
│   │   ├── test_gs003.py
│   │   └── test_p0_suite.py
│   └── unit/test_snapshot_registry.py
└── artifacts/
    ├── GS-002/<run-id>/
    ├── GS-003/<run-id>/
    └── suites/p0-evolution-suite-1.0.0/
        ├── runs/GS-001..003/<run-id>/<five JSON artifacts>
        └── summary.json
```

P0-2B 同时在 SQLite 中增加了 Scenario Snapshot Registry；它属于 evaluation metadata，不增加新的研究领域对象。

## 6. 实际模块职责

| 模块 | 单一职责 | P0 禁止扩张 |
| --- | --- | --- |
| `domain` | 版本化实体、枚举和基础校验 | 不包含数据库或 LLM 调用 |
| `evidence` | 图结构与确定性 Claim 评估规则 | 不做搜索或自由文本推理 |
| `invalidation` | 候选传播、重新验证和结论修复 | 不直接抓取来源 |
| `storage` | 事务、幂等和快照持久化 | 不包含业务判定规则 |
| `providers` | 可替换 structured-completion、fixture replay 与录制 | 不拥有领域 ID、版本或持久化 |
| `search` | 版本化文档检索与抓取边界 | 不生成 Evidence/Claim |
| `extraction` | 校验 provider 输出、对齐逐字引用并物化候选 | 不静默修复模型输出，不直接写数据库 |
| `evaluation` | 加载 scenario/manifest、计算逐场景与 suite 指标、输出并校验 artifacts | 不修改运行时结果以适配评分 |
| `datasets/scenarios` | 输入 fixture 与 ground truth | 不混放程序生成的运行结果 |
| `datasets/extraction` | 冻结问题、检索口径、gold assertions 与 fixture responses | 不把 fixture 分数当作真实模型成绩 |
| `tests` | 单元不变量与端到端黄金样例 | 不依赖外部网络 |
| `artifacts` | 每次 run 的结构化输出 | 不作为人工编辑的数据源 |

## 7. 数据流与控制边界

P0-2B 已实现的控制流：

```text
Scenario Fixture
      ↓
Register / Validate T0 Snapshot
      ↓
Apply ChangeEvent + Optional T1 SourceVersion
      ↓
Impact Propagation
      ↓
Claim Reverification
      ↓
Selective Conclusion Recompute
      ↓
Persist EvolutionRun
      ↓
Compare Ground Truth
      ↓
Write Per-run Artifacts
      ↓
Manifest-locked Suite Aggregate
```

关键边界：

- propagation 只标记需要检查的范围；
- verifier 决定 Claim 状态是否改变；
- repair 只消费已确认的语义变化；
- evaluator 只观察和评分，不能参与运行时决策；
- fixture 数据与运行产物必须分开；
- `retract` 通过 ChangeEvent 改变 current-view，不改写旧 SourceVersion/EvidenceSpan；
- suite 的每个场景使用独立空数据库，不能复用另一场景状态或旧 run 缓存；
- suite artifacts 与 GS-001 历史逐运行目录隔离。

## 8. 设计原则

### 8.1 Deterministic-first

P0 的图传播、状态评估、版本创建和指标计算全部确定性执行。LLM 以后只能作为可替换组件加入抽取或语义判断，不能成为版本谱系与幂等性的基础。

### 8.2 Provenance-native

来源、证据跨度、Claim 和 Conclusion 从创建时就携带版本与依赖关系。Provenance 不是报告生成后的补充字段。

### 8.3 Candidate before invalidation

结构影响和语义失效是两个阶段。这样才能同时控制漏报与误杀，并能分别计算 Candidate Impact 和 Invalidation 指标。

### 8.4 Preserve unaffected state

选择性重研究的价值不仅是少算，还包括明确证明哪些节点没有被重算、没有产生无意义的新版本。

### 8.5 Evaluation before integration

先固定 scenario、ground truth 和 full-recompute baseline，再引入 Web Search。搜索质量不能掩盖状态演化机制本身的问题。

### 8.6 Evidence over narrative

里程碑状态只依据存在的文件、可执行测试和结构化结果更新。设计文档、依赖声明和目录占位不能作为实现证据。

## 9. 决策记录

### D-001：每阶段维护双文档

- 状态：Accepted
- 日期：2026-08-27
- 决策：每一步同时更新技术实现文档与项目结构/设计文档。
- 原因：避免实现细节与设计动机分离，也防止计划能力被误报为实际能力。

### D-002：先做离线确定性 P0

- 状态：Accepted
- 日期：2026-08-27
- 决策：P0 不依赖联网、LLM 或搜索 provider。
- 原因：隔离 Evidence Evolution Mechanism，获得可重复、可判分结果。

### D-003：区分候选影响与确认失效

- 状态：Accepted
- 日期：2026-08-27
- 决策：图传播只输出 candidate，重新验证后才能输出 confirmed invalidation。
- 原因：来源更新不必然意味着旧 Claim 为假；它可能仍有冗余证据支持。

### D-004：实体版本不可变

- 状态：Accepted
- 日期：2026-08-27
- 决策：所有来源和研究产物通过新版本演进，旧版本保留。
- 原因：支持 as-of 解释、replay 和新旧结论 diff。

### D-005：P0 暂不建立独立 Fact 层

- 状态：Accepted for P0
- 日期：2026-08-27
- 决策：用 EvidenceSpan 的规范化断言直接连接 Claim。
- 原因：先验证最短核心链路，避免 Fact 与 Claim 在没有实验前形成重复抽象。
- 复审条件：多个 EvidenceSpan 需要合并为可复用来源事实，或消融证明 Fact 层有独立价值。

### D-006：P0-1 目标存储为 SQLite

- 状态：Accepted and implemented for P0-1
- 日期：2026-08-27
- 决策：首个持久化适配器使用 SQLite，支持事务、幂等键、版本谱系和 current-view 查询。
- 原因：单机、低规模、事务和幂等验证足够；暂不承担 PostgreSQL 运维成本。
- 当前边界：`storage/protocol.py` 只覆盖最小写入接口，图、规则和 runtime 仍直接使用 SQLiteRepository；可替换存储尚未完成。

### D-007：保持单进程、单 Runtime

- 状态：Accepted for P0
- 日期：2026-08-27
- 决策：不引入并行 Worker 或多 Agent。
- 原因：并发不是当前研究假设，只会增加重放和事务复杂度。

### D-008：第一个 baseline 是 Full Recompute

- 状态：Accepted
- 日期：2026-08-27
- 决策：P0 先比较 selective recompute 与全量重算；Temporal RAG 延后。
- 原因：Full Recompute 与离线图变化实验共享相同输入，能直接比较正确性和重算范围。

### D-009：场景独立执行并由显式 Suite Manifest 组成

- 状态：Implemented for P0-2B
- 日期：2026-08-27
- 决策：GS-001～003 各自从独立 T0 和空数据库运行；suite 由显式 manifest 列出 scenario/rule/ground-truth 版本，禁止扫描目录自动纳入。
- 原因：避免场景顺序污染状态，也避免临时 fixture 意外进入 benchmark。

### D-010：Retract 使用追加式 current-view 语义

- 状态：Implemented for P0-2B
- 日期：2026-08-27
- 决策：撤回事件不修改旧 SourceVersion 或 EvidenceSpan；current-source resolver 根据 append-only ChangeEvent 排除被撤回版本。
- 原因：保留历史 as-of 证据链，并与不可变版本原则一致。

### D-011：Failure Taxonomy 写入现有 Metrics Artifact

- 状态：Implemented for P0-2B
- 日期：2026-08-27
- 决策：定义 F01～F06 六类 critical failure，每个 `metrics.json` 保存逐条 failures，suite summary 再聚合；不增加第六类 artifact。
- 原因：保留失败实体和 trace 定位，同时维持五类逐运行产物契约。

### D-012：P0-2 增加 Scenario Snapshot Registry

- 状态：Implemented for P0-2B
- 日期：2026-08-27
- 决策：T0 数据和 `(scenario_id, scenario_version, snapshot_id, snapshot_hash)` 在同一事务登记；同一身份出现不同 hash 时拒绝执行。
- 原因：补上 P0-1 对部分初始化和 fixture drift 的已知缺口。

### D-013：Storage Protocol 重构延后

- 状态：Deferred until Gate P0
- 日期：2026-08-27
- 决策：P0-2B 不同时重构所有读取与图查询接口。
- 原因：当前研究目标是验证变化类型和选择性传播，存储可替换性不是本阶段假设。

### D-014：Suite 使用新鲜数据库并隔离历史产物

- 状态：Implemented for P0-2B
- 日期：2026-08-27
- 决策：suite 中每个场景使用独立临时空 SQLite 数据库；suite 逐运行 artifacts 写入 `artifacts/suites/<suite-version>/runs/`，不复用或覆盖 `artifacts/GS-001` 的历史目录。
- 原因：已持久化的幂等 EvolutionRun 可能掩盖 runtime 代码变化；产物隔离同时满足新鲜执行与 P0-1 历史保留。

### D-015：用 Git 建立本地变更基线

- 状态：Implemented and pushed
- 日期：2026-08-27
- 决策：在项目根目录初始化 `main` 分支，继续由项目 `.gitignore` 排除 SQLite 运行数据库、WAL/SHM 与 Python 缓存；将首次基线提交推送至 private GitHub 仓库 `Peter-Sherlock/Veritas`。
- 原因：让后续阶段具备可审计 diff 和可回滚基线，同时保留用户对首次提交内容与时机的控制。

### D-016：README 采用 Explorer-first 项目入口

- 状态：Implemented
- 日期：2026-08-27
- 决策：README 先用具体变化场景回答“项目解决什么问题”，再提供 quick start 和按探索目标组织的入口；架构、实验结果与边界只保留理解项目所需的最小信息，详细阶段记录和 failure 规格下沉到双文档。
- 原因：README 服务第一次探索项目的人，而不是承担项目内部总结；访客应先能运行、导航和形成心智模型，再决定是否深入阅读规格。

### D-017：零失败结论必须经过负向校准

- 状态：Implemented for P0-2C
- 日期：2026-08-27
- 决策：正式 suite 的 F01～F06 计数为零，只能与六类独立负向校准共同作为 failure detection 证据；校准基于真实 EvolutionRun 改变期望或验证信号，不改写冻结 fixture 与正式 artifacts。
- 原因：正向场景零失败不能证明 detector 可触发；把 detector calibration 纳入回归测试可以区分“系统没有失败”与“系统看不见失败”。

### D-018：expire 与 retract 共享追加式 current-view 机制

- 状态：Implemented for P0-3
- 日期：2026-08-29
- 决策：`expire` 事件与 `retract` 一样通过 append-only ChangeEvent 把来源版本排除出 current view，不改写旧行；两者区别只保留在语义层（expire 断言来源在 `effective_at` 前有效、之后失效；retract 断言内容被撤回）。
- 原因：P0 没有 as-of 历史查询需求，引入独立的 valid_to 驱动过期只会增加机制而不增加可验证行为；change_type、effective_at 与 trace/diff reason 已保留语义区分，未来需要 as-of 语义时可在此基础上扩展。

### D-019：conflict 以新增证据边为传播种子

- 状态：Implemented for P0-3
- 日期：2026-08-29
- 决策：`conflict` 事件引入独立来源（不同 `source_id`、`supersedes_version_id` 必须为 NULL）的证据；候选影响以新增 supports/contradicts 边的目标 Claim 为种子，沿 input snapshot 的 depends_on 边下行到结论；旧来源保持 active，系统不仲裁、不选边。
- 原因：conflict 的触发证据在 input snapshot 中尚不存在，不能沿用"从旧来源证据出发"的传播模式；从新增边出发保持 input-snapshot 语义且不需要把新实体提前写入图。

### D-020：suite 聚合验收由 manifest 显式声明

- 状态：Implemented for P0-3
- 日期：2026-08-29
- 决策：suite manifest 可携带 `acceptance` 块（验收字段名、evaluation_status、gate 状态、期望 recompute totals）；无该块的 manifest 保持 P0-2B 硬编码行为不变。新增场景通过新的 suite version（2.0.0）表达，冻结的 1.0.0 manifest 不修改。
- 原因：验收口径属于评测契约的一部分，应随 suite 版本显式冻结，而不是埋在 runner 常量里随代码漂移。

### D-021：Gate P0 评审结论为附条件通过

- 状态：Accepted
- 日期：2026-08-29
- 决策：Gate P0 **附条件通过**，允许进入 M1。
- 机制判断证据：五个冻结场景（revise/retract/分支隔离/expire/conflict）中选择性执行与 full-recompute 完全等价，全部 correctness 指标 1.0，critical failures 为 0，selective 重算 4/11 对比 full 11/11；F01～F06 探测器经负向校准确认可触发。
- 附加条件：进入 M1 后（1）LLM 抽取与语义判断必须以确定性 fixture 为回归基准校准，图谱系、幂等与版本机制保持确定性；（2）任何成本收益声明必须来自规模化 benchmark，不得外推受控小图的 4/11；（3）真实来源接入前先冻结本地版本化语料 adapter。
- 未覆盖风险（明确接受进入 M1）：真实抽取噪声、大图与并发性能、多进程初始化、storage 可替换性。

### D-022：LLM 通过协议接入，客户端保持零依赖

- 状态：Implemented for M1-1
- 日期：2026-08-29
- 决策：`providers/llm.py` 定义最小 `LLMProvider` 协议（structured completion + token 计量）；`OpenAICompatibleClient` 只用标准库 urllib 实现 OpenAI 兼容 chat completions（temperature=0、JSON mode、429/5xx 指数退避）；`FixtureLLM` 以 prompt 哈希重放预录响应，未知 prompt 直接拒绝；`RecordingLLM` 把真实调用录制成 fixture。
- 原因：LLM 必须可替换且默认不进测试关键路径（D-002/D-021 的延续）；引入 SDK 依赖换来的便利小于可复现性损失。

### D-023：首个检索源为 httpx 文档的本地版本化语料

- 状态：Implemented for M1-1
- 日期：2026-08-29
- 决策：一次性脚本 `scripts/harvest_corpus.py` 从 httpx 仓库 git tags 抽取 10 篇文档共 48 个版本快照，落地为 `datasets/corpus/httpx-docs/`（manifest 钉住每个文件的 SHA-256，加载时校验）；检索用 stdlib TF-IDF，支持 `as_of` 版本视图。
- 原因：真实开源文档的 git 历史提供免费的版本演进数据（后续 evolution benchmark 的 ChangeEvent 来源），同时满足"评测先于集成"——语料冻结后检索结果完全可复现。

### D-024：语料内容哈希采用 canonical UTF-8/LF

- 状态：Implemented for M1-1R
- 日期：2026-08-29
- 决策：语料在 hash 与 `fetch()` 前先把 CRLF/CR 统一为 LF；采集器以 LF 字节写出文件和 manifest；CI 的 Python 3.11/3.14 与 suite 1.0.0/2.0.0 矩阵均关闭 fail-fast，保留完整失败证据。
- 原因：Git 的 EOL 规范化会使同一 Markdown 在 Windows 工作树和 Linux checkout 中具有不同原始字节。以原始工作树字节定义内容身份会制造与语义无关的跨平台 hash 漂移；canonical 文本契约让采集、版本控制和运行时校验一致。

### D-025：模型只提出 assertion，确定性层拥有 provenance

- 状态：Implemented for M1-2A
- 日期：2026-08-29
- 决策：LLM 只能返回 `statement`、`canonical_key`、`relation` 与逐字 `quote`。schema、引用唯一定位、char offsets、hash、Evidence/Claim/edge ID 和时间字段全部由 `extraction` 模块校验或生成；模型输出失败时显式拒绝，不做模糊引用修复。
- 原因：把 lineage ID 或 citation 修复交给概率模型会破坏 P0 已验证的不可变性、幂等与 provenance 约束。模型可以提出语义候选，但不能成为事实身份的权威。

### D-026：抽取校准分开报告 retrieval 与 extraction

- 状态：Implemented for M1-2A
- 日期：2026-08-29
- 决策：10 题 runner 对 top-3 每篇文档实际执行抽取，分别报告 Hit@3/MRR 与 assertion precision/recall/citation alignment；fixture 冻结 question、检索文档集合、version 与 prompt canary，statement 也纳入 exact-match identity；summary 携带 canonical content hash。
- 原因：只在 gold source 上抽取会绕过检索失败；只报告 10/10 又会掩盖正确来源排名第 2/3 的问题。分层指标让 M1-2B/2C 能判断失败来自 retrieval、contract 还是模型语义。

### D-027：抽取失败按 critical/major 分级，gate 只要求正常集零失败

- 状态：Implemented for M1-2B
- 日期：2026-08-30
- 决策：建立 `ex-failures-1` 失败分类。EX02 契约拒绝与 EX05 fixture 漂移为 critical（结果不可信）；EX01 检索未命中、EX03 引用拒绝、EX04 断言不匹配为 major（校准有效但未达 gold，是 M1-2C 要度量的对象）。`critical_failure_count` 从"失败 case 数"改为"critical 级失败记录数"，新增 `major_failure_count` 与全量 `failures` 记录；per-case `failure` 单对象改为 `failures` 数组。`m1_2a_acceptance_candidate` 定义为 critical=0 且 major=0，保持冻结 fixture 基线 gate 不变松。benchmark 与 fixtures 数据逐字节不变，summary 为增量 schema 演化并重新生成 content hash；M1-2A 全部度量值保持不变。
- 原因：真实模型校准（M1-2C）必然产生 major 级偏差；若把质量差距与完整性失败混为一个计数，gate 要么过松（掩盖契约破坏）要么过紧（任何模型偏差都算"critical"）。分级让同一份 runner 既服务冻结基线的零失败 gate，也服务未来真实模型的差距报告。运行前守卫统一以 `EX05_FIXTURE_DRIFT` 前缀抛错，漂移中止运行，因此 EX05 只经异常断言校准、不出现在 summary 内。

### D-028：benchmark v2.0.0 为独立冻结数据集，fixtures 由确定性脚本生成

- 状态：Implemented for M1-2B2
- 日期：2026-08-30
- 决策：扩容 benchmark 落地为新数据集 `datasets/extraction/httpx-m1-2b/`（`httpx-initial-extraction` 2.0.0，30 题），v1.0.0 的 benchmark、fixtures 与 summary artifact 保持冻结不动。v2 的 fixtures.json 由 `scripts/build_extraction_v2_fixtures.py` 从 benchmark + 冻结语料确定性生成：gold 文档响应 = gold 断言集，其余检索文档响应为空；脚本内置引文唯一性、检索排名 ≤ max_rank 与 gold 断言覆盖三类验证，验证失败拒绝写出。新增覆盖包括多断言、contradicts 关系与两道 `as_of` 版本视图题（as_of 为 ISO 日期边界，与版本 `published_at` 字典序比较，不是版本号）。
- 原因：真实模型校准（M1-2C）需要足够的样本量才能在 retrieval、contract、语义之间归因失败。手写 fixtures 既费力又难以审计；从 benchmark + 语料确定性生成让"fixture = perfect model"的语义显式可重建。v1 保持冻结使 M1-2A 的历史基线永远可复现，两套 summary 由 CI 双矩阵分别锁定。

### D-029：M1-2C live provider 选定 DeepSeek `deepseek-v4-flash`（非思考模式录制）

- 状态：Implemented for M1-2C-pre
- 日期：2026-08-30
- 决策：真实 provider 校准的运行路径以 DeepSeek `deepseek-v4-flash` 为默认目标（`https://api.deepseek.com`，OpenAI 兼容）。客户端默认模型由已弃用的 `deepseek-chat` 更新为 `deepseek-v4-flash`，并新增 `extra_payload` 请求体合并参数；live 校准固定发送 `"thinking": {"type": "disabled"}`。CLI 以 `--provider live` 进入录制模式（`--record-out` 保存 `{model_id, responses}` 键值记录，可经 `FixtureLLM` 确定性重放），API key 只从 `VERITAS_LLM_API_KEY` 环境变量读取。benchmark、语料、prompt 与评分与 fixture 路径逐字节一致，仅替换 provider。
- 原因：截至 2026-08-30 的国产 API 现价对比中，`deepseek-v4-flash` 是"付费里最便宜且质量有保底"的选项（输入未命中 1 元/1M、缓存命中 0.02 元/1M、输出 2 元/1M），原生支持 JSON Output 且 1M 上下文足够容纳 top-3 文档；30 题 × top-3 ≈ 90 次调用的校准成本在 1 元人民币量级。禁用思考模式换取低延迟、低成本与接近确定性的 JSON 输出（校准要度量的是抽取契约遵从，不是推理能力）。旧别名 `deepseek-chat` 已不是文档化的模型名，不再作为默认。录制文件先落原始记录，冻结（per-case 重排 + canary + 漂移校验）是录制完成后的独立步骤，避免把未评审的真实输出直接固化为 fixture。

## 10. 阶段与门槛

| 阶段 | 目标 | 进入条件 | 退出条件 | 状态 |
| --- | --- | --- | --- | --- |
| P0-0 | 冻结核心语义与 GS-001 | 初始设计完成 | 双文档、黄金样例、指标和断言完整 | 已完成 |
| P0-1 | 实现确定性最小垂直链路 | P0-0 规格通过复核 | GS-001 自动测试与 artifacts 全部通过 | 已完成：11/11 tests |
| P0-2A | 冻结扩展场景与失败口径 | P0-1 通过 | GS-002/003、failure taxonomy、suite gate 完整 | 已完成：仅文档 |
| P0-2B | 实现场景与 suite runner | P0-2A 通过 | GS-002/003 自动测试和逐场景 artifacts 通过 | 已完成：24/24 tests，31/31 JSON hashes |
| P0-2C | 聚合评估与 Failure Analysis | P0-2B 通过 | Suite 指标、full baseline、failure records 完整 | 已完成：30/30 tests，F01～F06 校准，31/31 JSON hashes |
| P0-3 | expire 与 conflict 场景 | Gate 风险清单 | GS-004/005、suite 2.0.0、声明式验收通过 | 已完成：51/51 tests，67/67 JSON hashes |
| Gate P0 | 判断机制是否有价值 | P0-2 结果完整 | 正确性不低于全量重算且重算范围更小 | 已评审：附条件通过（D-021） |
| M1-1 | LLM/检索协议与本地版本化语料 | Gate P0 通过 | 协议、Fixture/真实客户端、语料与测试落地 | 已完成：72/72 tests |
| M1-1R | 跨平台语料与 CI 收口 | M1-1 完成 | canonical hash、双 Python/双 suite CI、双文档同步 | 已完成：73/73 tests；Actions #33247415305 四路成功 |
| M1-2A | 严格抽取契约与确定性基线 | M1-1R 完成 | 10 题 gold/fixture、candidate pipeline、双 Python 测试 | 已完成：10/10；85/85 tests；Actions #33250938915 五路成功 |
| M1-2B | Failure Taxonomy 与 gate 硬化 | M1-2A 完成 | 每类失败独立可触发，正常集 critical=0 | 已完成：91/91 tests；EX01～EX05 负向校准；Actions #33297042264 五路成功 |
| M1-2B2 | Benchmark 扩容至 30 题 | M1-2B 完成 | 新增 20 题冻结、fixtures 可重建、双校准 CI 绿 | 已完成：97/97 tests；30/30、MRR=0.7222；Actions #33299526597 六路成功 |
| M1-2C-pre | Live provider 接入路径 | M1-2B2 完成 | live CLI/录制路径可运行，fixture 双摘要零 diff | 已完成：103/103 tests；真实录制待 API key |
| M1-2C | 真实 provider 校准与评审 | M1-2C-pre 完成 | 真实录制、fixture 对照、边界结论 | 进行中（路径已就绪，待 `VERITAS_LLM_API_KEY`） |
| M1-2 | 抽取 pipeline 与校准 harness | M1-1R 完成 | 校准 CI 绿、真实 LLM 校准记录、10 题 benchmark 基线 | 进行中（M1-2A/M1-2B/M1-2B2/M1-2C-pre 已完成） |
| M1-3 | Research Runtime（状态/队列/checkpoint/预算） | M1-2 完成 | 中断恢复与预算测试通过 | 未开始 |
| M1-4 | 动态重规划 | M1-3 完成 | 触发场景测试通过 | 未开始 |
| M1-5 | 端到端演化集成 | M1-4 完成 | 真实抽取图上的 evolution benchmark 跑通 | 未开始 |
| M1 | 初始研究与搜索 | Gate P0 通过 | 另行定义 | 进行中（M1-2A 已完成） |

如果 Gate P0 不通过，不进入 Web Search 集成；先分析图粒度、规则语义和 benchmark 是否支持项目假设。

## 11. P0-2C 与 Gate P0 历史记录

P0-2C 已完成：

- [x] 从显式 manifest 在独立空数据库上重跑 GS-001～003；
- [x] 复核逐场景与 macro/micro correctness，所有冻结正确性指标均为 1.0；
- [x] 复核 selective `2 / 6` 与 full-recompute `6 / 6` 的最终结果等价；
- [x] 确认正式 suite 中 F01～F06 计数均为 0，critical failures 为 0；
- [x] 用六项负向注入分别单独触发 F01～F06，并验证完整 failure-record 契约；
- [x] 区分已覆盖的 revise/retract/branch isolation 与未覆盖的 expire/conflict/真实抽取/规模风险；
- [x] `30/30` 自动测试、`31/31` JSON content hash 验证通过；
- [x] 技术实现文档与项目结构文档同步记录正式分析和边界。

P0-2C 结束时的下一步是 Gate P0 评审，而不是直接开始 M1。当时 Gate 需要把两类判断分开：

1. **机制判断**：三个冻结场景中，选择性执行与 full-recompute 等价，并减少 4/6 次结论重算，这一项已具备通过证据；
2. **证据充分性判断**：场景均为小型人工图，尚无 `expire`、多来源 `conflict`、真实抽取噪声或规模数据，是否接受这些风险进入 M1 仍需明确决策。

Gate P0 的正式结果应记录为通过、附条件通过或不通过，并说明进入 M1 前必须补的场景。

### 11.1 Gate P0 评审记录（2026-08-29）

评审输入：suite 1.0.0（GS-001～003）与 suite 2.0.0（GS-001～005）的正式 summary、F01～F06 负向校准、覆盖缺口分析（技术实现文档 15.4 与第 16 节）。

1. **机制判断：通过。** 五个冻结场景中选择性执行与 full-recompute 完全等价，全部 correctness 指标 1.0，critical failures 为 0，selective 重算 4/11 对比 full 11/11；`revise`/`retract`/`expire`/`conflict` 四类变化与规则表四象限（accepted/contradicted/unsupported/conflict）均有场景覆盖。
2. **证据充分性判断：附条件接受。** 场景仍为小型人工图，真实抽取噪声与规模风险未消除；这些风险不作为进入 M1 的阻塞项，但转化为 D-021 的三条附加条件（LLM 组件以确定性 fixture 校准、成本声明必须有规模化 benchmark、真实来源接入前先冻结本地语料 adapter）。

正式结论：**Gate P0 附条件通过，允许启动 M1**（决策全文见 D-021）。

### 11.2 M1-1R 收口记录（2026-08-29）

- [x] 定位首次 Linux CI 失败为 Windows CRLF 原始字节 hash 与 Git LF checkout 不一致；
- [x] 采集器与运行时统一 canonical UTF-8/LF 内容及 hash 契约；
- [x] 重算并验证 48/48 corpus manifest hash；
- [x] 增加 LF/CRLF 等价回归，并锁定真实语料为 10 个文档、48 个快照；
- [x] Python 3.11.15 与 3.14.7 均以严格 `ResourceWarning` 模式通过 73/73 tests；
- [x] suite 1.0.0/2.0.0 均重跑且与 67 个已提交 JSON artifacts 一致；
- [x] README、技术实现文档和项目结构文档同步更新；
- [x] GitHub Actions [run 33247415305](https://github.com/Peter-Sherlock/Veritas/actions/runs/33247415305) 四路任务全部成功。

### 11.3 M1-2A 完成记录（2026-08-29）

- [x] 建立 `codex/m1-2-extraction-calibration` 阶段分支；
- [x] 严格校验 JSON schema、relation、canonical key 与唯一逐字引用；
- [x] 由确定性代码生成 EvidenceSpan、Claim 与 supports/contradicts edge 候选；
- [x] 冻结 10 题 benchmark、gold assertions、question/version fixture snapshot；
- [x] top-3 全文档实际抽取，未使用 gold source 跳过检索；
- [x] 10/10 cases；Hit@3=1.0，MRR=0.7833，precision/recall/citation=1.0；
- [x] statement 错配和 fixture question 漂移负向回归可触发；
- [x] Python 3.11.15 与 3.14.7 严格模式均通过 85/85 tests；
- [x] README、技术实现文档和项目结构文档同步更新；
- [x] GitHub Actions [run 33250938915](https://github.com/Peter-Sherlock/Veritas/actions/runs/33250938915) 五路任务全部成功。

### 11.4 M1-2B 完成记录（2026-08-30）

- [x] 建立 `ex-failures-1` 抽取失败分类：EX01 检索未命中、EX02 契约拒绝、EX03 引用拒绝、EX04 断言不匹配、EX05 fixture 漂移；
- [x] `critical_failure_count` 语义改为 critical 级记录数，新增 `major_failure_count`、`failure_counts` 与全量 `failures`；per-case `failure` 改为 `failures` 数组；
- [x] runner 拆分 `build_fixture_provider` 与 `evaluate_extraction_calibration`，负向校准通过内存扰动副本注入；
- [x] 六项负向校准：EX01/EX02/EX03/EX04/EX05 各自独立可触发，正常集五码显式零且 content hash 有效；
- [x] 运行前守卫统一携带 `EX05_FIXTURE_DRIFT` 前缀，漂移中止路径机器可读；
- [x] benchmark 与 fixtures 逐字节不变；committed summary 重新生成，M1-2A 全部度量值不变；
- [x] Python 3.14.7 严格 `ResourceWarning` 模式 91/91 tests 通过；
- [x] README、技术实现文档和项目结构文档同步更新；
- [x] GitHub Actions [run 33297042264](https://github.com/Peter-Sherlock/Veritas/actions/runs/33297042264) 五路任务（Python 3.11/3.14、suite 1.0.0/2.0.0、extraction calibration 零 diff）全部成功。

### 11.5 M1-2B2 完成记录（2026-08-30）

- [x] 冻结 benchmark v2.0.0：v1 十题逐字节携带 + 20 道新题（EX-011～030）；
- [x] 新增覆盖：多断言（EX-014/024）、contradicts（EX-017/018/019/022）、as_of 版本视图（EX-029 index@0.24.1 Python 3.7+、EX-030 troubleshooting@0.25.2 legacy proxies dict）；
- [x] `scripts/build_extraction_v2_fixtures.py` 确定性生成 fixtures，内置引文唯一性/检索排名/断言覆盖验证与 canary 计算；
- [x] 澄清 `as_of` 语义为 ISO 日期边界（与 `published_at` 字典序比较）；
- [x] v2 冻结基线：30/30、Hit@3=1.0、MRR=0.7222、P/R/citation=1.0、critical=0、major=0；
- [x] 6 项新测试（superset 断言、覆盖形状、as_of 版本映射、确定性、漂移拒绝）；
- [x] CI `extraction-calibration` 扩展为 M1-2A/M1-2B2 双矩阵，各自零 diff；
- [x] Python 3.14.7 严格模式 97/97 tests 通过；
- [x] README、技术实现文档和项目结构文档同步更新；
- [x] GitHub Actions [run 33299526597](https://github.com/Peter-Sherlock/Veritas/actions/runs/33299526597) 六路任务（Python 3.11/3.14、suite 1.0.0/2.0.0、extraction calibration M1-2A/M1-2B2 双矩阵零 diff）全部成功。

### 11.6 M1-2C-pre 完成记录（2026-08-30）

- [x] live provider 选型定为 DeepSeek `deepseek-v4-flash`（D-029），客户端默认模型同步更新；
- [x] `OpenAICompatibleClient` 新增 `extra_payload` 合并；live 校准固定 `thinking: disabled`；
- [x] `RecordingLLM` 累计 request/prompt/completion 计量，live 运行打印成本报告；
- [x] `run_live_extraction_calibration` + CLI `--provider live`（`--record-out` 录制、`--model`/`--base-url` 可配、缺 key 快速失败）；
- [x] 6 项新测试：live 满分录制可重放、全量契约拒绝逐题 EX02、缺 key 拒绝、默认模型/extra_payload/token 计量；
- [x] fixture 路径重跑 M1-2A/M1-2B2，两个 committed summary 逐字节不变；
- [x] Python 3.14.7 严格 `ResourceWarning` 模式 103/103 tests 通过；
- [x] README、技术实现文档和项目结构文档同步更新。

## 12. 文档更新检查表

每个阶段结束前检查：

### 技术实现文档

- [x] 数据模型是否与实际代码一致；
- [x] 接口和算法是否包含实际文件定位；
- [x] 测试命令、数量和结果是否真实记录；
- [x] 指标是否来自结构化 artifacts；
- [x] 失败样例和限制是否保留；
- [x] 计划项与已实现项是否明确区分。

### 项目结构文档

- [x] 实际目录树是否更新；
- [x] 新模块是否有单一职责；
- [x] 新决策是否登记状态与原因；
- [x] 阶段状态和进入/退出条件是否更新；
- [x] 下一阶段是否保持小而可验收；
- [x] 两份文档是否互相链接且没有语义冲突。

## 13. 当前风险

- GS-001～005 都是人工设计的模拟场景，可能过于规整；
- Claim 评估规则很简单，暂不代表真实来源冲突处理能力；
- 暂不建立 Fact 层可能需要在真实抽取阶段复审；
- Recompute Ratio 在小图上只用于机制验证，不能外推为生产成本收益；
- Snapshot Registry 尚未经过多进程并发初始化或数据库迁移压力验证；
- storage protocol 尚未覆盖读取和图查询，当前并非真正可替换存储；
- `expire` 与 `retract` 在 P0 共享追加式 current-view 机制，as-of 历史查询与基于 `valid_to` 的自动过期尚未实现；多来源 `conflict` 只验证保留冲突、不做消解仲裁；
- Gate P0 已附条件通过（D-021）；附加条件要求在 M1 中用确定性 fixture 校准 LLM 组件，并在规模声明前补规模化 benchmark；
- M1-1R 已消除语料 hash 的 CRLF/LF 平台漂移；后续新增语料必须继续遵守 canonical UTF-8/LF 契约；
- 本地 TF-IDF 仍是词面基线；30 题 Hit@3=1.0 但 MRR=0.7222，advanced 文档的词面优势把多个 gold 来源压到第 2/3，不能宣称通用检索质量；
- M1-2A/M1-2B2 的满分来自 `FixtureLLM` 重放，不能宣称模型抽取质量；live provider 运行路径已就绪（M1-2C-pre），但真实录制与对比尚未执行；
- EX01～EX05 校准的是抽取链路已编码的失败路径；真实模型的失败分布（半正确引用、语义 paraphrase、跨文档断言漂移）要等 M1-2C 的真实录制才能观察；
- extraction 当前只产出候选对象，尚未定义写入 Evidence Graph 的事务、去重与跨运行 canonical-key 冲突策略；
- Suite 2.0.0 的 `4 / 11` 重算比例来自受控图结构，不能外推到真实研究任务；
- 空 semantic-change 场景需要严格遵守空集合指标约定，否则容易产生误导性的 precision；
- 已配置 GitHub Actions CI（3.11/3.14 测试、suite 1.0.0/2.0.0、extraction calibration + artifact 零 diff），但尚无分支保护或 release 策略。

## 14. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-30 | M1-2C-pre | 交付 live provider 校准运行路径：CLI `--provider live`、`run_live_extraction_calibration`、`RecordingLLM` token 计量与录制重放闭环；客户端默认模型更新为 `deepseek-v4-flash`、新增 `extra_payload`（live 固定禁用 thinking）；103/103 tests，fixture 双摘要零 diff；真实录制待 `VERITAS_LLM_API_KEY`；登记 D-029 |
| 2026-08-30 | M1-2B2 | 冻结 benchmark v2.0.0（v1 十题 + 20 新题：多断言 ×2、contradicts ×4、as_of 版本视图 ×2）；fixtures 由确定性脚本生成并内置三类验证；30/30、Hit@3=1.0、MRR=0.7222；CI 双校准矩阵；97/97 tests；登记 D-028 |
| 2026-08-30 | M1-2B | 建立 `ex-failures-1` 抽取失败分类（EX01～EX05，critical/major 分级）、runner 拆分与 summary 增量 schema 演化；六项负向校准独立可触发，正常集 critical=0；91/91 tests；committed summary 重生成且 M1-2A 度量值不变；登记 D-027 |
| 2026-08-29 | M1-2A | 新增 extraction contract/pipeline、10 题 HTTPX gold 与 fixture、calibration runner/summary 和 CI job；10/10，Hit@3=1.0、MRR=0.7833、precision/recall/citation=1.0；Python 3.11/3.14 均 85/85 tests；登记 D-025～D-026 |
| 2026-08-29 | M1-1R | 将 corpus hash 冻结为 canonical UTF-8/LF，重算 48 条 manifest hash；新增 LF/CRLF 回归和 HTTPError 资源关闭；CI 扩展为 Python 3.11/3.14、suite 1.0.0/2.0.0 双矩阵；两版本均 73/73 tests、67/67 artifact hashes 与 48/48 corpus hashes 通过；登记 D-024 |
| 2026-08-29 | M1-1 | 新增 providers（LLM 协议/OpenAI 兼容零依赖客户端/FixtureLLM/RecordingLLM）与 search（检索协议/本地语料 TF-IDF）模块；`scripts/harvest_corpus.py` 采集 httpx 文档语料（10 文档、48 版本快照、hash 钉住）；72/72 tests 通过；登记 D-022～D-023 |
| 2026-08-29 | P0-3 + Gate P0 | 实现 GS-004 expire、GS-005 conflict、conflict 传播模式与 manifest 声明式验收；新增 suite 2.0.0；51/51 tests、67/67 JSON hash 通过，suite 1.0.0 重跑零 diff；登记 D-018～D-021，Gate P0 附条件通过，允许启动 M1 |
| 2026-08-29 | CI | 新增 GitHub Actions（Python 3.11/3.14 测试矩阵 + suite 1.0.0 回归）；README 补充 Python 版本要求与 Windows launcher 说明 |
| 2026-08-27 | P0-2C | 完成正式 suite/full-recompute 评估、F01～F06 负向校准和覆盖缺口分析；30/30 tests、31/31 JSON hash 通过；登记 D-017，进入 Gate P0 待评审状态 |
| 2026-08-27 | README | 新增并重构为 Explorer-first 项目入口：具体示例、quick start、探索导航、机制图、实验与产物；登记 D-016 |
| 2026-08-27 | Repository setup | 初始化本地 Git `main`，确认忽略规则，并把首次基线提交推送到 private `Peter-Sherlock/Veritas` |
| 2026-08-27 | P0-2B | 落地 GS-002/003、suite manifest/runner、retract current-view、Snapshot Registry、failure records 与 D-014；24/24 tests 和 31/31 JSON hash 通过；阶段结束时 P0-2C/Gate 尚未执行 |
| 2026-08-27 | P0-2A | 冻结 GS-002/003、F01～F06、suite/Gate 口径、P0-2B 计划结构与 D-009～D-013；未修改代码 |
| 2026-08-27 | P0-1 | 落地最小 Python/SQLite 结构、GS-001 runner、11 项测试、五类 artifacts、阶段状态与边界 |
| 2026-08-27 | P0-0 | 建立双文档规则、实际/计划结构、设计原则、决策记录与阶段门槛 |
