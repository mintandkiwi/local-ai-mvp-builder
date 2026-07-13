# 项目开发大纲

## 项目目标

提供可版本管理、可公开发布且与前端 Agent 无关的受监督本地 AI 开发工作流。Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 共用仓库内唯一 Skill；本地 Qwen 承担编码，云端 Codex 承担结构化评审、异常接管和最终确认。

## 技术栈

- Python 3.11+：核心编排、安全安装、Ollama 检查、结构化 Review 和运行摘要。
- POSIX shell：`mvp-loop`、`mvp-loop-supervised`、安装器与模型下载入口。
- Ollama：运行 `qwen3-coder:30b` 和备选 `qwen3.6:35b`。
- Codex CLI：通过 Ollama provider 驱动本地 coder，通过 OpenAI provider 驱动 reviewer/supervisor。
- Git：干净基线、完整工作区状态、diff 和版本快照。
- macOS Seatbelt：隔离项目验证命令的网络、Git 元数据和敏感文件访问。
- Agent Skills 公共子集：首行 YAML `name`/`description` 与 Markdown 工作流说明。

## 架构与关键路径

1. 四种前端从各自用户级 Skills 目录发现 `local-ai-mvp-builder`；这些目录都软链接到仓库的唯一 Skill 源。
2. Skill 只调用 PATH 中的 `mvp-loop-supervised`，计划默认位于 `${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}`。
3. 监督入口解析受控参数，针对 `--project` 验证 Git、tracked/staged/untracked 干净状态、计划和 `.mvp-ai.toml`，再运行 doctor。
4. 入口调用 `mvp_orchestrator.py run`；本地 Qwen 修改目标项目、测试和三份中文文档。
5. Seatbelt 中执行项目验证与文档契约，随后云端 Codex Review 全部未提交 diff。
6. 首次失败交回 Qwen 修复；第二次失败或本地执行异常由云端 Codex 接管，再执行验证和独立最终 Review。
7. 通过后 `summary.json` 标记 `ready_for_user_review`；Git commit、push、tag、release 或 PR 必须另获用户授权。

## 重要文件

- `integrations/skills/local-ai-mvp-builder/SKILL.md`：唯一权威 Skill 工作流与触发描述。
- `integrations/skills/local-ai-mvp-builder/references/`：批准计划格式和中文项目文档契约。
- `integrations/skills/local-ai-mvp-builder/agents/openai.yaml`：可忽略的 Codex UI 元数据。
- `bin/mvp-loop-supervised`：Agent 无关的受监督 PATH 入口，不提供脏工作区绕过。
- `bin/install-agent-skills`：安全安装器 Shell 入口。
- `src/agent_skill_installer.py`：平台映射、冲突保护、备份、回滚和软链接部署逻辑。
- `src/mvp_orchestrator.py`：两轮闭环、看门狗、验证、Review、接管与版本快照。
- `config/`、`prompts/`、`schemas/`、`templates/`：模型、运行、提示词、结构化输出和项目初始化契约。
- `tests/test_cross_agent_integration.py`：真实安装器、启动器和 Skill 包回归测试。
- `tests/test_orchestrator.py`：原有安全、接管、文档和编排回归测试。
- `README.md`、`LICENSE`：公开安装/回滚说明与 MIT 许可。

## 约束

- 仓库内 Skill 是唯一维护源；不得复制个人 Agent 配置或形成第二份仓库 Skill。
- 目标项目必须是干净 Git 工作区，未跟踪文件同样阻止启动；监督入口拒绝 `--allow-dirty`。
- `.mvp-ai.toml` 至少包含一条真实验证命令，两轮本地 Review 失败阈值固定。
- Claude Code 和 Antigravity 目前只是入口，不能被描述为 reviewer/supervisor 后端。
- 安装器只写当前用户已知目录；普通冲突默认拒绝，显式替换必须先生成可恢复备份。
- 不读取或提交凭据、认证配置、模型权重、`runs/`、真实 token、私有远程地址或个人绝对路径。
- 不自动 commit、push、切分支、改写历史、发布 release 或创建 PR。

## 当前状态

跨 Agent Skill 唯一源、安全监督启动器、四平台幂等安装器、真实回归测试、公开 README 和 MIT License 均已实现。自动运行最终状态 `needs_manual_attention` 作为历史证据保留；其遗留 finding 已由人工接管修复，原生环境 44 项测试全部通过，受监督验证沙箱 44 项通过、2 项因父级 Seatbelt 限制精确跳过，Python 编译、Shell 语法、中文文档门禁和新的独立 Review 均已通过。

方案 B 已实际安装：Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 的用户级 Skill 目录均软链接到仓库唯一源，`~/.local/bin/mvp-loop-supervised` 可正常显示帮助；旧 Codex Skill 已保留时间戳备份。公开 GitHub 仓库 `https://github.com/mintandkiwi/local-ai-mvp-builder` 已创建，`main` 基线与 `agent/cross-agent-skill` 功能分支均已推送，Draft PR #1 位于 `https://github.com/mintandkiwi/local-ai-mvp-builder/pull/1`，等待用户最终评审。
