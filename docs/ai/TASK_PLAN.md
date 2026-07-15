# AI 任务规划

## 当前里程碑

M4：Local AI MVP Builder Token 效率整改。目标是完成 TE-001 至 TE-009 的计量、前置止损、风险路由、自适应 review、上下文压缩和模拟验收；TE-010 真实 A/B 必须等待用户单独批准云端 Token 预算。

## 已完成

- `TE-001`：实现 JSON `turn.completed.usage` 与 `tokens used` 文本解析；重复完成事件不重复求和，大整数保真，缺失/损坏为 unavailable/null；成功、失败、超时入口统一生成阶段记录。
- `TE-002`：在兼容旧字段基础上扩展 summary 的 `usage`、`workflow`、`risk`、`preflight`、`context_capsules` 和 `efficiency`；增加凭据脱敏与 no_baseline 安全回归。
- `TE-003`：官方启动器在任何 Git/config/doctor 项目检查前进入编排器唯一加锁入口；第一次 clean 检查前获取 workspace 外的跨前端项目锁。在排除 `.envrc`、任意层级 `.direnv` 和 `.docker/config.json` 等已知敏感路径的私有可丢弃副本中，以正式 Seatbelt 和等价项目写权限运行干净基线；普通 `.docker` 目录/Dockerfile 保留。结束后两阶段清理嵌套权限/flags 并确认副本不存在，再在模型前复核 HEAD、完整 porcelain 和稳定配置快照；任一失败时真实目标与用户文件不变，agent 调用为 0。
- `TE-004`：默认改为 adaptive，最多两轮 pre-takeover review；schema 强制 finding category，首次 P0/P1、安全/数据丢失/可靠性/未知类别或大量 finding 立即接管，只有明确的普通局部 P2 才允许一次 fixer；保留 legacy 回滚值。
- `TE-005`：计划正文强制恰好一条完整 `Risk classification: low`/`medium`/`high` 声明；占位符、代码块示例、重复或冲突均按 high，high 关键实现 direct-cloud，local coder 仅处理 low/medium。
- `TE-006`：生成可审计、去敏、UTF-8 安全、大小受限的 context capsule，提示词改为完整计划路径加 capsule/按需仓库读取。
- `TE-007`：增加云端软 Token 预算、最大调用数、cloud lower bound 和保守预算状态；计量不完整不判定为预算内。
- `TE-008`：更新唯一 Skill、plan/evidence reference、UI 元数据、README、CLI 帮助和中文 AI 文档。
- `TE-009A`：新增模拟回归覆盖 Token JSON/文本/缺失/损坏/重复/大整数、summary 安全、基线零调用、low/medium/high 路由、首轮 P1、局部 P2、P3-only、watchdog、quoted/多行凭据、各类私钥、reviewer 能力预检和大 capsule 脱敏/截断。
- `TE-009B`：独立 `review-4` 至 `review-35` 共发现 25 个 P1、77 个 P2（`review-15`、`review-26` 无 verdict）；全部有效 finding 已修复，原生回归由 68 项增至 143 项，并覆盖外置证据、内容快照、风险/类别策略、凭据脱敏、去敏 Git 视图、子仓库路由和事务安装。
- `TE-009C`：`review-36` 核验 12 项门禁、完整证据哈希和冻结快照后返回 `pass`、0 finding；summary 已达到 `ready_for_user_review`。
- `RELEASE-001`：完成中英文 README、143 项发布前回归和辅助门禁；提交 `b295221` 已推送到 `origin/agent/cross-agent-skill`，现有 Draft PR #1 自动纳入升级。
- `RELEASE-002`：将公开中英文 README 改为部署无关说明，移除维护者机器容量、具体模型、个人代理和硬件评估入口，保留可配置后端与 `doctor` 兼容性门禁。

## 进行中

- 等待用户评审 Draft PR #1；未经新的明确授权不合并、不创建 tag 或 release。

## 待办

- `TE-010`：待用户单独批准真实云端 Token 预算后，设计至少 3 组等价低/中风险 direct-Codex 与 local-first A/B；未批准前不得执行或宣称节省。
- `M4-FOLLOWUP`：如 A/B 中位数云端 Token 未下降 25% 或成功/安全标准不等价，将 claim 记为 `regression`/`inconclusive` 并调整路由，不关闭优化验证。

## 验收标准

- 每个已启动 agent 阶段有唯一记录；未知 Token 为 null/unavailable，partial 不称完整。
- 基线/环境预检失败时目标工作区保持干净，local/cloud agent 调用均为 0。
- 首轮 P1 不发生第二次 pre-takeover review；局部 P2 通过路径最多两次 reviewer；接管后重新验证并独立终审。
- high/未知风险不由本地 coder 独立实现关键代码。
- capsule 有字节数、字段和截断元数据，不泄露凭据、私钥、原始提示或私有 remote。
- `ready_for_user_review` 仍要求项目验证、中文三文档和独立 review 全部通过。
- 全部项目测试、Python 编译、Shell 语法、Skill quick validation、CLI help 和 `git diff --check` 通过。

## 下一步

用户评审 `https://github.com/mintandkiwi/local-ai-mvp-builder/pull/1`；通过后再决定是否合并到 `main`。TE-010 继续保持“待预算批准”，只有用户明确批准真实云端 Token 消耗后才创建等价 A/B 计划并执行。

证据冻结说明：独立终审 capsule 必须在 tracked 文档停止修改后生成，因此仓库文档只记录上一轮已完成 verdict 与下一步；新一轮外部 capsule/终审结果将在冻结快照之后产生并写入权限受限的运行目录。这是内容快照不可变性约束，不应视为 tracked 状态遗漏。
