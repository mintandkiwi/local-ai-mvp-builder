# 项目开发大纲

## 项目目标

提供跨 Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 的受监督本地 AI MVP 工作流。在不降低测试、安全、数据保护和独立终审门槛的前提下，按风险分配本地 Qwen 与云端 Codex 的职责，记录每次 agent 调用的 Token 证据，并在环境失败或严重 finding 出现时提前止损。

## 技术栈

- Python 3.11+：编排器、Token 解析、风险/Review 决策、上下文 capsule、summary 和安全安装器。
- POSIX shell、TOML：统一监督入口与默认策略配置。
- Ollama：本地 `qwen3-coder:30b` 与备选 `qwen3.6:35b`。
- Codex CLI：Ollama provider 驱动本地 coder/fixer，OpenAI provider 驱动只读 reviewer 和可写 supervisor。
- Git、macOS Seatbelt：干净基线、diff 导航和无网络/无 Git 元数据写入的验证隔离。

## 架构与关键路径

1. 四种前端发现仓库内唯一 `local-ai-mvp-builder` Skill，并通过 PATH 调用 `mvp-loop-supervised`。
2. 启动器只解析参数并规范化项目/计划路径，然后直接进入编排器唯一 `run` 入口；不在锁外读取 Git clean 状态、项目配置或执行 doctor。
3. 第一次 clean/配置检查前，编排器在 workspace 外获取跨前端项目锁；随后检查 `sandbox-exec`、reviewer 版本与结构化输出能力、schema/仓库/diff 可读性和本地模型（仅 local-first），并把成功或失败的结构化宿主探针写入 summary。它创建排除 `.envrc`、任意层级 `.direnv`、任意层级 `.docker/config.json` 及其他已知敏感路径的私有可丢弃副本；普通 `.docker` 目录和 Dockerfile 会保留并参与验证。验证专用 HOME/cache/tmp/bin 位于另一随机临时目录，Seatbelt 对同一类敏感路径拒绝读写，并拒绝网络和证据目录，但允许在副本中生成与正式验证等价的构建产物。写探针和干净基线结束后以两阶段清理恢复嵌套目录权限/flags 并确认副本不存在，再在首个模型调用前重新核对 HEAD、完整 porcelain、配置哈希及稳定加载的验证命令。环境、基线、并发变化或清理失败均以零 local/cloud 调用停止，真实目标和用户文件保持不变。
4. `low`/`medium` 进入 local-first；`high` 或未声明风险直接进入 cloud supervisor。编码后验证失败只允许一次精简的本地验证修复，验证通过后才进入 review。
5. 风险只从非代码块正文中恰好一条完整声明读取，且不得并存其他可见 risk-like 控制行；占位、重复、冲突、有效与无效混合、fenced-only 均按 high。Review finding 具有 schema 强制的类别；P0/P1 以及任何 severity 的 security/data_loss/reliability/other 立即接管，只有明确的 correctness/documentation/performance/testing P2 才允许一次 fixer，同类别 P3-only 才非阻塞。supervisor 前预留其自身与 final review 两次云端容量。
6. 接管后重新运行项目验证和中文文档门禁；验证通过才由独立 reviewer 终审，失败则停止并省去无效终审调用。只有验证与终审全部通过才写 `ready_for_user_review`。
7. 每次 agent 调用都在成功、失败和超时路径生成唯一 `usage.stages` 记录。持久化先解析完整 JSON/逐行 JSONL 并递归清除敏感子树，再做纯文本兜底，因此数值 JSON usage 保持 exact；文本总数为 partial，缺失为 unavailable/null。summary 同时汇总 cloud 下限、调用数、接管原因、验证/finding 统计、风险、capsule 和预算状态。
8. 运行记录位于 workspace 外的私有状态目录，宿主证据拒绝符号链接并原子写入；批准计划的源路径、初始 SHA-256 和外部副本在 agent/capsule/summary 边界反复核对。capsule 从 `git status --porcelain=v1 -z` 获取 changed files，并绑定 staged/unstaged diff 与全部 untracked 内容的加密快照；normal、compact 和最终降级均携带内容快照及 plan/scope/validation/review 哈希。验证后源码漂移、reviewer 前后快照变化或任一证据不一致立即阻塞。

## 重要文件

- `src/mvp_orchestrator.py`：前置门禁、agent 调用、Token 解析、风险/自适应路由、capsule、验证和 summary 契约。
- `config/defaults.toml`：adaptive/legacy、严重 finding 阈值、capsule 大小、云端软预算和最大调用数。
- `prompts/`：本地文件职责边界与按需读取 capsule 的 coder/fixer/reviewer/supervisor 提示词。
- `integrations/skills/local-ai-mvp-builder/`：唯一 Skill 维护源；`references/evidence-contract.md` 解释用量和效率证据。
- `bin/mvp-loop-supervised`：无脏工作区绕过的统一入口和用户帮助。
- `tests/test_orchestrator.py`：Token、summary、预检、风险、自适应路由、watchdog、capsule 和安全回归。
- `tests/test_cross_agent_integration.py`：Skill 包、启动器和四平台安装回归。
- `README.md`、`README_EN.md`：面向用户的中英文安装、运行、安全和恢复说明，并提供双向语言切换。

## 约束

- 默认不自动 commit、push、切换分支、改写历史、发布或创建 PR；本次 GitHub 发布只在用户明确授权的当前分支和变更范围内执行，本轮不执行真实云端 A/B。
- 现有 P0–P2 阻塞标准、项目验证、中文三文档和接管后的独立终审不得降低。
- Token 未知不能记为 0；partial/unavailable 不能称为完整，计量不完整时不能宣称在预算内。
- 单次运行默认 `efficiency.claim = no_baseline`；没有另行批准且完整计量的等价 direct-Codex 对照，不得宣称节省或换算费用。
- summary、capsule、live 输出、命令/验证日志、review JSON 和 last-message 不保存未脱敏的凭据、私钥、环境文件内容或私有 remote URL；结构化 JSON key 同样执行递归脱敏。live 只呈现工具/文件事件、模型提供的推理摘要、Token 和顶层失败摘要，不呈现原始私有思维链。
- 仓库 integration 是 Skill 唯一维护源，个人目录只保留安装链接。

## 当前状态

TE-001 至 TE-009 模拟验收已落盘。独立 `review-4` 至 `review-35` 共报告 25 个 P1、77 个 P2；`review-15`、`review-26` 因 usage limit 无 verdict，其余 finding 均已针对性修复，原生回归由 68 项增至 143 项并全部通过。`review-36` 核验 12 项门禁、完整证据哈希和冻结快照后返回 `pass`、0 finding，外部 summary 状态为 `ready_for_user_review`。当前 discoverable Skill backup 已迁出。最终实现完全排除基线原 `.git`；允许的原 HEAD blob 形成无父合成 HEAD，当前 index blob 形成合成 index，真实工作树保留 staged/unstaged/untracked 与 diff-check 语义。验证前预存根/嵌套 `.git` 全部封锁，普通根项目路径走私有 `GIT_DIR`，严格子路径 `init/clone` 和验证中新建子仓库按自身目录发现。安装器在任何迁移前预检全部目标，并对迁移中途或后续安装失败执行事务回滚。中英文 README 已完成并互相链接；提交 `b295221` 已推送到 `origin/agent/cross-agent-skill`，Draft PR #1 等待用户评审。TE-010 未获真实 A/B Token 预算授权，不能称“Token 节省已验证”。
