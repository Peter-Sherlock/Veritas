# Veritas 项目结构与设计文档

> 文档职责：记录项目边界、设计思路、目录演进、阶段状态和关键决策。  
> 当前阶段：P0-2B Scenario and Suite Implementation  
> 当前状态：Implementation verified v0.4; P0-2C pending  
> 更新日期：2026-08-27  
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

截至 2026-08-27，工作区实际存在：

```text
Veritas/
├── .git/                       # main 分支，连接 private GitHub origin
├── .gitattributes              # 跨平台文本统一为 LF
├── .gitignore
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
│   └── evaluation/
│       ├── scenario.py
│       ├── metrics.py
│       ├── runner.py
│       └── suite_runner.py
├── datasets/
│   ├── scenarios/
│   │   ├── GS-001/scenario.json
│   │   ├── GS-002/scenario.json
│   │   └── GS-003/scenario.json
│   └── suites/p0-evolution-suite.json
├── tests/
│   ├── unit/
│   │   ├── test_domain_and_graph.py
│   │   └── test_snapshot_registry.py
│   └── scenarios/
│       ├── test_gs001.py
│       ├── test_gs002.py
│       ├── test_gs003.py
│       └── test_p0_suite.py
├── artifacts/
│   ├── GS-001/run-f39dacf198a857ae/<five JSON artifacts>
│   ├── GS-002/run-65365880276d316f/<five JSON artifacts>
│   ├── GS-003/run-046dcc6b4ed54440/<five JSON artifacts>
│   └── suites/p0-evolution-suite-1.0.0/
│       ├── runs/GS-001/<run-id>/<five JSON artifacts>
│       ├── runs/GS-002/<run-id>/<five JSON artifacts>
│       ├── runs/GS-003/<run-id>/<five JSON artifacts>
│       └── summary.json
└── docs/
    ├── PROJECT_STRUCTURE.md
    └── TECHNICAL_IMPLEMENTATION.md
```

当前 Git 状态：本地 `main` 已连接 private 远程仓库 [Peter-Sherlock/Veritas](https://github.com/Peter-Sherlock/Veritas)，项目文件由首次基线提交跟踪；SQLite 运行数据库与 Python 缓存由项目 `.gitignore` 排除，源码、文档与 JSON 由 `.gitattributes` 统一为 LF。

当前仍没有：

- Web Search、LLM 或真实来源抓取；
- 产品化 CLI 或服务接口；
- 并发 Runtime；
- `expire` 与多来源 `conflict` 场景；
- 生产规模 benchmark 结果。

当前已有的是三个受控场景上的确定性 evidence-evolution runtime 与 evaluation suite，不是会自主搜索、规划、调用工具和生成研究报告的完整 Deep Research Agent。

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
| `evaluation` | 加载 scenario/manifest、计算逐场景与 suite 指标、输出并校验 artifacts | 不修改运行时结果以适配评分 |
| `datasets/scenarios` | 输入 fixture 与 ground truth | 不混放程序生成的运行结果 |
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

## 10. 阶段与门槛

| 阶段 | 目标 | 进入条件 | 退出条件 | 状态 |
| --- | --- | --- | --- | --- |
| P0-0 | 冻结核心语义与 GS-001 | 初始设计完成 | 双文档、黄金样例、指标和断言完整 | 已完成 |
| P0-1 | 实现确定性最小垂直链路 | P0-0 规格通过复核 | GS-001 自动测试与 artifacts 全部通过 | 已完成：11/11 tests |
| P0-2A | 冻结扩展场景与失败口径 | P0-1 通过 | GS-002/003、failure taxonomy、suite gate 完整 | 已完成：仅文档 |
| P0-2B | 实现场景与 suite runner | P0-2A 通过 | GS-002/003 自动测试和逐场景 artifacts 通过 | 已完成：24/24 tests，31/31 JSON hashes |
| P0-2C | 聚合评估与 Failure Analysis | P0-2B 通过 | Suite 指标、full baseline、failure records 完整 | 未开始 |
| Gate P0 | 判断机制是否有价值 | P0-2 结果完整 | 正确性不低于全量重算且重算范围更小 | 未开始 |
| M1 | 初始研究与搜索 | Gate P0 通过 | 另行定义 | 未开始 |

如果 Gate P0 不通过，不进入 Web Search 集成；先分析图粒度、规则语义和 benchmark 是否支持项目假设。

## 11. P0-2B 完成情况与下一阶段边界

P0-2B 已完成：

- [x] 实现 GS-002 retract fixture、空 semantic-change、evidence rebase 和零结论重算；
- [x] 实现 GS-003 Python 3.11 → 3.12 的 compatibility 变化，并保持 retry 子图 untouched；
- [x] 实现 `p0-rules-2` 的 `compatibility_support`，不改变 GS-001 的 `p0-rules-1`；
- [x] 实现 retract append-only current-view 与 nullable change package；
- [x] 实现 Scenario Snapshot Registry、同身份 hash 漂移与部分初始化拒绝；
- [x] 实现 F01～F06 failures、provenance、replay 与 artifact hash 校验；
- [x] 实现显式 suite manifest、独立数据库运行、macro/micro 与 recompute totals；
- [x] 生成 GS-002/003 和 versioned suite artifacts，保留 GS-001 历史 artifacts；
- [x] `24/24` 自动测试、`31/31` JSON content hash 验证通过。

下一阶段是 P0-2C：复核 suite summary，形成正式 Failure Analysis（包括“零失败”时的覆盖充分性分析），审查 full-recompute 对照和受控场景偏差，然后再决定 Gate P0。P0-2C 不引入 Web Search、LLM、产品化 CLI、Trace Viewer 或 storage protocol 全面重构。

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

- GS-001～003 都是人工设计的模拟场景，可能过于规整；
- Claim 评估规则很简单，暂不代表真实来源冲突处理能力；
- 暂不建立 Fact 层可能需要在真实抽取阶段复审；
- Recompute Ratio 在小图上只用于机制验证，不能外推为生产成本收益；
- Snapshot Registry 尚未经过多进程并发初始化或数据库迁移压力验证；
- storage protocol 尚未覆盖读取和图查询，当前并非真正可替换存储；
- 当前验证了 `revise` 与 `retract`，没有验证 `expire` 或多来源 `conflict`；
- suite 的 `p0_2b_acceptance_candidate=true` 不是正式 Gate P0 结论；P0-2C 尚需检查覆盖充分性和受控场景偏差；
- Suite 预期 `2 / 6` 重算比例来自受控图结构，不能外推到真实研究任务；
- 空 semantic-change 场景需要严格遵守空集合指标约定，否则容易产生误导性的 precision；
- 当前只有首次 `main` 基线，尚未配置远程 CI、分支保护或 release 策略。

## 14. 变更记录

| 日期 | 阶段 | 变更 |
| --- | --- | --- |
| 2026-08-27 | Repository setup | 初始化本地 Git `main`，确认忽略规则，并把首次基线提交推送到 private `Peter-Sherlock/Veritas` |
| 2026-08-27 | P0-2B | 落地 GS-002/003、suite manifest/runner、retract current-view、Snapshot Registry、failure records 与 D-014；24/24 tests 和 31/31 JSON hash 通过；P0-2C/Gate 未执行 |
| 2026-08-27 | P0-2A | 冻结 GS-002/003、F01～F06、suite/Gate 口径、P0-2B 计划结构与 D-009～D-013；未修改代码 |
| 2026-08-27 | P0-1 | 落地最小 Python/SQLite 结构、GS-001 runner、11 项测试、五类 artifacts、阶段状态与边界 |
| 2026-08-27 | P0-0 | 建立双文档规则、实际/计划结构、设计原则、决策记录与阶段门槛 |
