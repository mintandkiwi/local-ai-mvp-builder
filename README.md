# Local AI MVP Builder

Local AI MVP Builder 用同一套仓库内 Agent Skill，把 Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 接入受监督的 MVP 开发闭环。四种前端都只是工作流入口：Codex CLI 通过 Ollama provider 驱动本地 Qwen 编码，云端 Codex 仍负责结构化 Code Review、看门狗接管和最终确认。

流程不会自动 commit、push、切换分支、创建仓库或发布版本。最终改动保留在目标仓库中等待人工评审。

## 架构

```text
Codex / Claude Code / Antigravity IDE / Antigravity CLI
                         │
            shared local-ai-mvp-builder Skill
                         │ PATH
                mvp-loop-supervised
                         │
       doctor + clean Git + plan/config gates
                         │
              src/mvp_orchestrator.py run
                 ├─ local Qwen coder
                 ├─ project validations
                 ├─ cloud Codex reviewer
                 └─ cloud Codex takeover supervisor
```

仓库中的 `integrations/skills/local-ai-mvp-builder/` 是唯一维护源。安装器向各平台目录创建指向该目录的软链接，避免复制后的版本漂移；`~/.local/bin/mvp-loop-supervised` 同样指向仓库入口。

## macOS 依赖

- Python 3.11 或更高版本（需要标准库 `tomllib`）
- POSIX shell、Git 和 macOS `sandbox-exec`
- [Ollama](https://ollama.com/) 与本地模型 `qwen3-coder:30b`；可选 `qwen3.6:35b`
- 已登录且可使用 Ollama provider 与 OpenAI provider 的 Codex CLI
- `aria2c`，用于并行拉取并校验模型文件

下载模型并检查依赖：

```sh
./bin/pull-models
./bin/pull-models --with-secondary
./bin/mvp-loop doctor
```

下载器只在检测到本机 `127.0.0.1:7890` 代理时使用它。不要让多个大模型在 48 GB 机器上同时常驻。

## 安装 PATH 入口和四个平台 Skill

先预览；dry-run 不创建任何文件：

```sh
./bin/install-agent-skills --dry-run
```

确认后安装到全部平台：

```sh
./bin/install-agent-skills
```

确保用户级命令目录在 PATH 中，例如在 `~/.zprofile` 中加入：

```sh
export PATH="$HOME/.local/bin:$PATH"
```

重新打开终端后验证：

```sh
mvp-loop-supervised --help
```

默认平台映射如下：

| 平台 | Skill 目标目录 |
| --- | --- |
| Codex | `~/.codex/skills/local-ai-mvp-builder` |
| Claude Code | `~/.claude/skills/local-ai-mvp-builder` |
| Antigravity IDE | `~/.gemini/config/skills/local-ai-mvp-builder` |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills/local-ai-mvp-builder` |

只安装指定平台时使用逗号分隔列表：

```sh
./bin/install-agent-skills --platforms codex,claude
./bin/install-agent-skills --platforms antigravity-ide,antigravity-cli
```

普通文件、目录或指向其他来源的链接发生冲突时，安装器默认拒绝且继续汇总其他目标的完成/失败状态。确认需要替换后才使用：

```sh
./bin/install-agent-skills --platforms codex --force
```

`--force` 会先把原目标移动为同目录下的 `local-ai-mvp-builder.backup.<时间戳>`，验证备份存在后再安装；安装失败会尝试原位恢复。安装器拒绝经过 HOME 内软链接父目录写入，不读取或复制认证文件。

安装后，在各前端的 Skill 列表中确认 `local-ai-mvp-builder` 可见，或在任务中显式写 `$local-ai-mvp-builder`。不同版本的 Antigravity 对目录软链接发现行为可能不同；若未显示，先确认目标 `SKILL.md` 可读，再重启前端并检查其 Skill 列表。不要手工复制一份 Skill 形成第二维护源。

## 初始化目标项目

目标必须是 Git 仓库，并以干净工作区开始。新项目可由兼容入口生成基础文件：

```sh
./bin/mvp-loop init --project "/absolute/path/to/project"
```

编辑目标项目的 `.mvp-ai.toml`，至少填写一条真实的测试、lint、类型检查或构建命令。初始化文件需要先形成用户认可的干净 Git 基线；工具不会代替用户提交。

批准的计划应放在目标工作区之外。默认的 Agent 无关位置是：

```text
${MVP_LOOP_PLAN_DIR:-$HOME/.local/share/local-ai-mvp-builder/plans}
```

## 执行

通常从任一已安装前端显式调用 `$local-ai-mvp-builder`。也可以直接运行统一入口：

```sh
mvp-loop-supervised \
  --project "/absolute/path/to/project" \
  --plan "$HOME/.local/share/local-ai-mvp-builder/plans/project-plan.md" \
  --model primary \
  --display summary
```

- `--model secondary` 使用 `qwen3.6:35b`。
- `--display live` 显示经过脱敏的详细终端事件；默认 `summary` 只保留关键状态。
- 入口没有脏工作区绕过参数；tracked、staged 和 untracked 改动都会阻止启动。
- 计划缺失、`.mvp-ai.toml` 缺失、Git 仓库无效或 doctor 失败时，命令以非零状态给出中文错误。

固定工作流是：本地 Qwen 编码 → 项目验证与中文文档契约 → 云端 Codex Review → 首次失败交回 Qwen 修复 → 第二次失败由云端 Codex 接管 → 再验证和独立最终 Review。本地模型启动失败、崩溃、超时或连续 300 秒无进展会直接触发接管。

运行记录保存在本仓库忽略的 `runs/` 下。只有 `summary.json` 状态为 `ready_for_user_review` 才表示可以交给用户检查。

## 每次开发的文档契约

每次成功运行都必须更新：

- `docs/devlog/YYYY-MM-DD.md`：中文目标、进展、修改、使用方法、验证结果和后续事项。
- `docs/ai/PROJECT_OUTLINE.md`：面向后续 AI 的架构、入口、边界和当前状态。
- `docs/ai/TASK_PLAN.md`：稳定任务 ID、状态、验收标准和下一步。

编排器会检查文件存在、中文内容、必需章节以及本次 Git diff 中确有更新；缺一项都不能进入 `ready_for_user_review`。

## 升级、卸载与恢复

Skill 和入口是软链接，因此仓库更新后会立即使用新版本。更新仓库后可重跑安装器做幂等校验：

```sh
./bin/install-agent-skills
```

卸载前先确认目标确为软链接，然后删除相应平台入口和 PATH 入口：

```sh
test -L "$HOME/.codex/skills/local-ai-mvp-builder" && rm "$HOME/.codex/skills/local-ai-mvp-builder"
test -L "$HOME/.claude/skills/local-ai-mvp-builder" && rm "$HOME/.claude/skills/local-ai-mvp-builder"
test -L "$HOME/.gemini/config/skills/local-ai-mvp-builder" && rm "$HOME/.gemini/config/skills/local-ai-mvp-builder"
test -L "$HOME/.gemini/antigravity-cli/skills/local-ai-mvp-builder" && rm "$HOME/.gemini/antigravity-cli/skills/local-ai-mvp-builder"
test -L "$HOME/.local/bin/mvp-loop-supervised" && rm "$HOME/.local/bin/mvp-loop-supervised"
```

若曾用 `--force` 生成备份，删除当前链接后，将选定的时间戳备份移动回原名即可恢复。先人工核对备份内容；不要用通配符覆盖多个备份。

## 安全和公开边界

- 本地 coding 使用受限工作区，验证命令在 macOS Seatbelt 中禁止网络、Git 元数据写入和敏感文件读取。
- Skill 和启动器不授予 commit、push、tag、release 或 PR 权限。
- Claude Code 与 Antigravity 当前不充当 reviewer/supervisor；相关后端仍固定为云端 Codex。
- 不提交 `runs/`、模型权重、凭据、用户 Agent 配置、真实 token、私有远程地址或个人绝对路径。
- `summary.json.version_control` 只记录安全的分支、基线 commit、remote 名称和建议提交信息，不记录 remote URL。

更多背景见[硬件与模型评估](docs/hardware-and-model-assessment.md)、[实施计划](docs/implementation-plan.md)和[验证报告](docs/verification-report.md)。本项目使用 [MIT License](LICENSE)。
