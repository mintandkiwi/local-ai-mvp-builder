# 项目开发大纲

## 项目目标

在本机使用 Ollama 大模型执行主要编码工作，由 Codex 负责计划约束、独立 Code Review、问题反馈、异常接管和最终交付，从而降低云端编码 token 消耗，同时保留可审计的质量闭环。

## 技术栈

- Python 3：工作流编排、Ollama 检查、结构化 Review 和日志生成。
- Shell：提供 `mvp-loop` 与模型下载入口。
- Ollama：运行 `qwen3-coder:30b` 和 `qwen3.6:35b`。
- Codex CLI：通过 Ollama 执行本地 coding，通过 OpenAI provider 执行 Review 与监督者接管。
- Git：提供干净基线、diff、分支和版本快照。
- macOS Seatbelt：隔离项目验证命令的网络、Git 和敏感文件访问。

## 架构与关键路径

1. 用户选择项目并批准简洁的实现计划。
2. Skill 调用安全启动器，确认目标是干净 Git 仓库。
3. `mvp_orchestrator.py` 将计划交给本地 Qwen 修改代码、测试和中文文档。
4. 验证沙箱执行项目命令，并检查中文日志、AI 大纲和任务规划。
5. 云端 Codex 对未提交 diff 进行结构化 Review。
6. 首轮失败返回本地模型修复；第二轮失败或本地进程异常时由 Codex 接管。
7. 最终验证与 Review 通过后生成 `summary.json`，等待用户评审。
8. 用户明确授权后才进行 Git commit、push 或 GitHub PR。

## 重要文件

- `src/mvp_orchestrator.py`：核心编排器、看门狗、验证和版本快照。
- `config/defaults.toml`：Review 轮数、超时、模型运行和云端监督配置。
- `config/models.toml`：主模型与备选模型映射。
- `prompts/`：本地编码、修复、Review 和监督者提示词。
- `schemas/review.schema.json`：结构化 Code Review 输出契约。
- `templates/`：新项目的 `AGENTS.md` 与 `.mvp-ai.toml` 模板。
- `tests/test_orchestrator.py`：安全、接管、文档和流程回归测试。
- `~/.codex/skills/local-ai-mvp-builder/SKILL.md`：Codex 自动触发和执行规范。

## 约束

- 目标项目默认必须是干净 Git 工作区。
- 本地编码不得自动 commit、push、切换分支或改写历史。
- 两轮是代码问题的本地模型失败阈值；执行异常由看门狗立即接管。
- 默认只在 Codex 对话中汇报关键状态，详细事件保存在终端与权限受限的 run 目录。
- 每次开发必须更新中文日志、AI 项目大纲和 AI 任务规划。
- GitHub 发布必须经过用户明确授权。

## 当前状态

主模型、备选模型、受监督编排器、结构化 Review、验证沙箱、实时/简洁输出、文档契约、看门狗和版本快照均已实现。当前等待用户评审本次升级，尚未自动提交或上传 GitHub。
