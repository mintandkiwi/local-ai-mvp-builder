# 项目开发大纲

## 项目目标

提供跨 Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 的受监督本地 AI MVP 工作流。在不降低测试、安全、数据保护和独立终审门槛的前提下，以 OpenCode Agent 为主要本地编码后端、Codex 直连本地推理为显式备用后端，并在环境失败或严重 finding 出现时由云端 Codex 接管。

## 技术栈

- Python 3.11+：编排器、Token 解析、风险/Review 决策、上下文 capsule、summary 和安全安装器。
- POSIX shell、TOML：统一监督入口与默认策略配置。
- OpenCode CLI：通过 Ollama 兼容接口驱动运行级受限的本地 coder/fixer。
- Codex CLI：作为显式本地备用路径，并驱动云端只读 reviewer 与可写 supervisor。
- Git、macOS Seatbelt：干净基线、diff 导航和无网络/无 Git 元数据写入的验证隔离。

## 架构与关键路径

1. 四种前端发现仓库内唯一 `local-ai-mvp-builder` Skill，并通过 PATH 调用 `mvp-loop-supervised`。
2. 启动器只解析参数并规范化项目/计划路径，然后直接进入编排器唯一 `run` 入口；不在锁外读取 Git clean 状态、项目配置或执行 doctor。
3. 第一次 clean/配置检查前，编排器在 workspace 外获取跨前端项目锁；随后检查 `sandbox-exec`、reviewer 版本与结构化输出能力、schema/仓库/diff 可读性和本地模型（仅 local-first），并把成功或失败的结构化宿主探针写入 summary。它创建排除 `.envrc`、任意层级 `.direnv`、任意层级 `.docker/config.json` 及其他已知敏感路径的私有可丢弃副本；普通 `.docker` 目录和 Dockerfile 会保留并参与验证。验证专用 HOME/cache/tmp/bin 位于另一随机临时目录，Seatbelt 对同一类敏感路径拒绝读写，并拒绝网络和证据目录，但允许在副本中生成与正式验证等价的构建产物。写探针和干净基线结束后以两阶段清理恢复嵌套目录权限/flags 并确认副本不存在，再在首个模型调用前重新核对 HEAD、完整 porcelain、配置哈希及稳定加载的验证命令。环境、基线、并发变化或清理失败均以零 local/cloud 调用停止，真实目标和用户文件保持不变。
4. `low`/`medium` 默认进入 OpenCode local-first。编排器创建第二个去敏实现副本，排除仓库内 `opencode.json`/`opencode.jsonc` 与 `.opencode`，并阻止本地 Agent 修改或回写这些可合并 MCP/插件/Agent 控制面文件；计划若涉及它们会在副本创建前直接进入 cloud supervisor。随后生成运行级临时 Agent，默认拒绝非编辑能力、禁用插件/MCP/子 Agent/网络工具、隔离 HOME/XDG，并用 Seatbelt 限制 OpenCode 进程只能读写该副本和私有运行目录、只能连接配置的精确回环 Ollama `host:port`。受支持 OpenCode 版本的 Agent Markdown 不会实际应用颗粒编辑规则，因此仅在候选副本中使用经集成测试验证的标量编辑授权；计划范围由候选 Git 可见差异门禁和事务回写层强制，绝不会扩大到真实目标。批准计划、capsule 与去敏验证证据仅以只读方式提供给 fixer；完整提示词经运行目录内的私有 `--file` 附件传入，命令行只有固定引导语，超时看门狗记录均不含提示词正文。整次运行固定启动日，跨午夜不会改变开发日志回写范围。所有 fixer 续接同一 Session；`codex-ollama` 仅在用户明确选择时作为备用，不做静默降级。
5. 本地实现、验证与独立 review 均发生在可丢弃副本中。Review 通过后，会在该候选副本重跑验证；任何未评审的 Git 可见验证副作用均阻止回写。随后只允许计划明确声明的路径与三份中文文档事务式回写真实目标；越界、符号链接、并发漂移或候选验证失败均回滚。已评审文件事务性写入完成后，私有备份清理异常只记录为待处理告警，绝不执行可能造成半回滚的恢复；不会自动 Git commit。`high` 或未声明风险仍直接进入 cloud supervisor。
6. 风险只从非代码块正文中恰好一条完整声明读取，且不得并存其他可见 risk-like 控制行；占位、重复、冲突、有效与无效混合、fenced-only 均按 high。Review finding 具有 schema 强制的类别；P0/P1 以及任何 severity 的 security/data_loss/reliability/other 立即接管，只有明确的 correctness/documentation/performance/testing P2 才允许一次 fixer，同类别 P3-only 才非阻塞。supervisor 前预留其自身与 final review 两次云端容量。
7. 接管后重新运行项目验证和中文文档门禁；验证通过才由独立 reviewer 终审，失败则停止并省去无效终审调用。只有验证与终审全部通过才写 `ready_for_user_review`。
8. 每次 agent 调用都在成功、失败和超时路径生成唯一 `usage.stages` 记录。Codex JSON 与 OpenCode `step_finish` JSONL 分别精确解析；文本总数为 partial，缺失为 unavailable/null。summary 同时记录本地后端、OpenCode Session、实现工作区与事务回写状态。
9. 运行记录位于 workspace 外的私有状态目录，宿主证据拒绝符号链接并原子写入；批准计划的源路径、初始 SHA-256 和外部副本在 agent/capsule/summary 边界反复核对。capsule 从 `git status --porcelain=v1 -z` 获取 changed files，并绑定 staged/unstaged diff 与全部 untracked 内容的加密快照；验证后源码漂移、reviewer 前后快照变化或任一证据不一致立即阻塞。

## 重要文件

- `src/mvp_orchestrator.py`：前置门禁、agent 调用、Token 解析、风险/自适应路由、capsule、验证和 summary 契约。
- `config/defaults.toml`：OpenCode 主后端、Codex/Ollama 备用后端、Agent 步数/温度、adaptive/legacy、云端预算和最大调用数。
- `integrations/opencode/agents/local-mvp-coder.md`：运行时动态权限 Agent 的正文模板。
- `prompts/`：本地文件职责边界与按需读取 capsule 的 coder/fixer/reviewer/supervisor 提示词。
- `integrations/skills/local-ai-mvp-builder/`：唯一 Skill 维护源；`references/evidence-contract.md` 解释用量和效率证据。
- `bin/mvp-loop-supervised`：无脏工作区绕过的统一入口和用户帮助。
- `tests/test_orchestrator.py`：Token、summary、预检、风险、自适应路由、watchdog、capsule 和安全回归。
- `tests/test_cross_agent_integration.py`：Skill 包、启动器和四平台安装回归。
- `README.md`、`README_EN.md`：面向用户的中英文安装、运行、安全和恢复说明，并提供双向语言切换；公开内容使用部署无关表述，不披露维护者机器参数或具体模型配置。
- `experiments/token-efficiency/run_ab.py`：准备和执行等价 local-first/direct-cloud A/B，原始证据写入仓库外，仅输出脱敏聚合结果。
- `docs/reports/token-efficiency-ab-2026-07-15.md`、英文对应报告：公开阶段性实测方法、结果、限制和复验要求。

## 约束

- 默认不自动 commit、push、切换分支、改写历史、发布或创建 PR；本次 GitHub 发布和真实云端 A/B 仅在用户明确授权的当前分支、实验范围和 Token 边界内执行，不合并、不创建 tag 或 release。
- 现有 P0–P2 阻塞标准、项目验证、中文三文档和接管后的独立终审不得降低。
- Token 未知不能记为 0；partial/unavailable 不能称为完整，计量不完整时不能宣称在预算内。
- 单次运行默认 `efficiency.claim = no_baseline`；没有另行批准且完整计量的等价 direct-Codex 对照，不得宣称节省或换算费用。
- summary、capsule、live 输出、命令/验证日志、review JSON 和 last-message 不保存未脱敏的凭据、私钥、环境文件内容或私有 remote URL；结构化 JSON key 同样执行递归脱敏。live 只呈现工具/文件事件、模型提供的推理摘要、Token 和顶层失败摘要，不呈现原始私有思维链。
- 仓库 integration 是 Skill 唯一维护源，个人目录只保留安装链接。

## 当前状态

OpenCode 主后端已完成第一阶段实现：能力探测、运行级私有配置、默认拒绝 Agent、同 Session 修复、JSONL/Token 解析、看门狗接管、去敏实现副本、Seatbelt 进程隔离、计划范围门禁和失败回滚均已有自动化覆盖；最终未提交差异独立 Code Review 未发现 P0–P2 finding。Codex 直连本地推理仍保留为显式备用。TE-010 的旧探索性效率结果仍为 `inconclusive`，不能据此宣称 Token 节省；需要在本次固定版本完整验收后重新执行公平 A/B。
