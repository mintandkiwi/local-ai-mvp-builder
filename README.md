# Local AI MVP Builder

[中文](README.md) | [English](README_EN.md)

Local AI MVP Builder 用同一套仓库内 Agent Skill，把 Codex、Claude Code、Antigravity IDE 和 Antigravity CLI 接入受监督的 MVP 开发闭环。四种前端都只是工作流入口：Codex CLI 通过可配置的本地推理后端驱动 coding agent，云端 Codex 仍负责结构化 Code Review、看门狗接管和最终确认。

流程不会自动 commit、push、切换分支、创建仓库或发布版本。最终改动保留在目标仓库中等待人工评审。

## 架构

```text
Codex / Claude Code / Antigravity IDE / Antigravity CLI
                         │
            shared local-ai-mvp-builder Skill
                         │ PATH
                mvp-loop-supervised
                         │
 doctor + clean Git + risk/plan/config + baseline gates
                         │
              src/mvp_orchestrator.py run
                 ├─ local coding agent
                 ├─ project validations
                 ├─ cloud Codex reviewer
                 └─ cloud Codex takeover supervisor
```

仓库中的 `integrations/skills/local-ai-mvp-builder/` 是唯一维护源。安装器向各平台目录创建指向该目录的软链接，避免复制后的版本漂移；`~/.local/bin/mvp-loop-supervised` 同样指向仓库入口。

## 运行依赖

- Python 3.11 或更高版本（需要标准库 `tomllib`）
- POSIX shell、Git，以及当前版本支持的系统隔离后端
- 已安装并完成配置的本地推理后端与 coding-capable 模型
- 已登录且可使用本地 provider 与云端 Review provider 的 Codex CLI

运行环境检查：

```sh
./bin/mvp-loop doctor
```

具体模型、量化方式、上下文长度、并发和资源限制由部署者根据自己的运行环境配置；本项目 README 不绑定某台设备或某个模型。正式验证依赖系统隔离能力，安装前应以 `doctor` 的结果为准。

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

`--force` 会先对全部目标执行只读冲突/父路径预检，再把原目标移动到 `~/.local/share/local-ai-mvp-builder/backups/<平台>/<时间戳>`，验证备份存在后安装。安装器还会把旧版留在 Skill 发现目录中的 `local-ai-mvp-builder.backup.*` 迁出，避免过时备份继续被识别为第二个 Skill；迁移中途或后续链接失败时，会回滚已移动的旧备份和原目标，并明确报告任何恢复失败。安装器拒绝经过 HOME 内软链接父目录写入，不读取或复制认证文件。

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

- `--model secondary` 使用配置中的备用模型别名；实际模型由部署者决定。
- `--display live` 显示经过脱敏的工具调用、文件变化、模型提供的推理摘要、Token 与顶层失败事件；不会显示或保存原始私有思维链。默认 `summary` 只保留关键状态。
- 入口没有脏工作区绕过参数；tracked、staged 和 untracked 改动都会阻止启动。
- 计划缺失、`.mvp-ai.toml` 缺失、Git 仓库无效或 doctor 失败时，命令以非零状态给出中文错误。
- 计划必须在非代码块正文中恰好包含一条完整声明，例如 `Risk classification: low`；允许 CommonMark 正文的 0–3 个前导空格，Tab 或至少 4 个空格按缩进代码块忽略。把 `low|medium|high` 占位符原样保留、重复声明或互相冲突都会保守按 high 处理，本地模型无权降低风险。

任何模型修改前，编排器会获取 workspace 外的项目级排他锁，检查 reviewer 可执行文件能否启动、版本/结构化输出选项是否兼容以及仓库/diff 是否可读，并创建不含已知 `.env`、私钥、带前后缀的 credential 数据文件和其他认证文件的私有可丢弃副本。它在与正式验证相同的 Seatbelt 环境先探测临时目录与各工具缓存可写性，再以等价项目写权限执行干净基线验证（不运行“本次文档必须更新”门禁）；构建产物只写入副本，随后进行可验证清理。首个模型调用前还会重新核对 HEAD、完整 porcelain 状态和 `.mvp-ai.toml` 快照。缓存不可写、SDK/命令缺失、项目并发变化、清理不完整或基线测试失败时立即输出 `needs_manual_attention`，local/cloud 模型调用数均为 0，真实目标工作区和用户文件保持不变。

验证命令通过临时 Git 包装器按目标路由。控制器把原 HEAD 的允许路径复制成无父提交的合成 HEAD，再把当前 index 的允许路径复制成合成 index；真实工作树保持不变。因此 staged、unstaged、untracked、`git diff --check` 语义仍然成立，同时目标项目只接触无历史、无 remote、无认证信息的私有 `GIT_DIR`。验证前存在的根/嵌套 `.git` 会被枚举并封锁；验证期间在项目内或临时目录中新建的测试子仓库则按自己的工作目录正常执行 `init/add/commit/status` 和 `git -C`。基线复制完全排除原 `.git`，所以验证代码无法读取预存 `FETCH_HEAD`、reflog、hooks、remote、`http.extraHeader` 或历史敏感对象。

默认 `adaptive` 流程按风险和证据路由：low/medium 先由本地 coding agent 实现；high、未声明、有效声明与占位/无效声明并存等歧义风险直接由 Codex supervisor 实现关键代码。编码后的验证失败先给本地模型一次精简验证修复，验证通过后才调用 reviewer。Review finding 使用 schema 强制的结构化 `category`；P0/P1、任何 severity 的 `security`、`data_loss`、`reliability`、无法可靠分类的 `other`，或至少 5 条阻塞 finding 会立即接管。只有至多 4 条定位明确且属于 correctness/documentation/performance/testing 的 P2 才允许一次本地修复和第二轮 review；仅含这些安全类别内 P3 时才按非阻塞通过。编排器会在 supervisor 前预留强制 final review 的云端调用容量；容量不足会在云端修改前分类为配置失败，3-call low/medium 路径会跳过可能耗尽终审容量的第二轮而提前接管。接管后重新验证；验证通过才调用独立终审，验证失败会停止并省去一次无效的 final-review 调用。本地模型启动失败、崩溃、超时或连续 300 秒无进展也会触发接管。`workflow.strategy = "legacy"` 仅用于回滚，不代表 Token 优化。

运行记录保存在目标 workspace 之外的 `~/.local/share/local-ai-mvp-builder/runs/`，每个目录权限为 `0700`，避免 workspace-write agent 篡改批准计划、日志或 summary。命令输出、验证日志、结构化 review 和 last-message 在持久化时都会拒绝符号链接、原子写入并脱敏，同时保留合法数值 Token telemetry；极限压缩的 capsule 仍会保留带哈希的完整 scope、validation manifest 和 review 证据引用。最后一次验证后的 staged、unstaged 与全部 untracked 内容快照会以固定大小分块计算并绑定到 capsule，在 reviewer 前后及写入 ready 前复核；大型 diff 或文件正文不会整体载入内存，任何漂移都会阻塞旧 verdict。只有 `summary.json` 状态为 `ready_for_user_review` 才表示可以交给用户检查。

### 用量与效率证据

`summary.json.usage.stages` 为 coder、fixer、reviewer、supervisor 的每次调用保存唯一阶段记录。`turn.completed.usage` 记为 `exact`；旧版 `tokens used` 总数只能记为 `partial`；缺失、损坏、超时或格式变化记为 `unavailable`/`null`，绝不按 0 计算。`cloud_lower_bound` 是当前可确定的云端 Token 下限，`measurement_complete` 只有所有已调用阶段均为 exact 时才为 true。

`config/defaults.toml` 可配置 `cloud.soft_token_budget`（0 表示未配置）和 `cloud.max_calls_per_run`。软预算只告警，不会在安全或数据修复中途强制退出；计量不完整时预算状态为 `unknown`，不能宣称“仍在预算内”。普通单次运行的 `efficiency.claim` 必须是 `no_baseline`。只有另获 Token 预算批准、使用同等任务 direct-Codex 对照且两组计量完整时，才允许标记 `measured_saving`；否则只能是 `no_baseline`、`inconclusive` 或 `regression`，本地 Token 不能换算成云端节省。

公开 A/B 证据见 [2026-07-15 Token 效率阶段性报告](docs/reports/token-efficiency-ab-2026-07-15.md)。一组探索性观测中 local-first 的云端 Token 高 11.79%，但两臂间 Skill 工作版本发生修复漂移，后续样本又因外部额度中断；因此有效完整配对为 0/3，结论为 `inconclusive`，不得宣传“Token 节省已验证”。

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

若曾用 `--force` 生成备份，先在 `~/.local/share/local-ai-mvp-builder/backups/<平台>/` 人工核对选定时间戳内容；删除当前链接后，再把该备份移动回平台原路径即可恢复。不要把备份放回 Skills 父目录并长期保留，也不要用通配符覆盖多个备份。

## 安全和公开边界

- 本地 coding 使用受限工作区；验证命令通过受支持的系统隔离后端禁止网络、Git 元数据写入和敏感文件读取。
- Skill 和启动器不授予 commit、push、tag、release 或 PR 权限。
- Claude Code 与 Antigravity 当前不充当 reviewer/supervisor；相关后端仍固定为云端 Codex。
- 不提交本地状态目录中的运行证据、模型权重、凭据、用户 Agent 配置、真实 token、私有远程地址或个人绝对路径。
- `summary.json.version_control` 只记录安全的分支、基线 commit、remote 名称和建议提交信息，不记录 remote URL。

更多背景见[实施计划](docs/implementation-plan.md)和[验证报告](docs/verification-report.md)。本项目使用 [MIT License](LICENSE)。
