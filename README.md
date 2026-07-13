# 本地 AI MVP 构建环境

> 推荐入口：在 Codex 中显式调用个人 Skill `$local-ai-mvp-builder`。该 Skill 会固化项目选择、计划、受监督 coding、两轮评审、Codex 接管和最终人工交付，不要绕过 Skill 直接让本地模型修改项目。

这个环境把 Codex 分成两个责任清晰的角色：

- 本地执行者：Codex CLI 通过 Ollama 驱动 Qwen，在受限工作区内实际 coding。
- 监督与评审者：已登录的 Codex 使用独立模型做结构化 Code Review；本地模型两轮仍未修好时，由监督者接管修复。

整个流程不会自动 commit 或 push。最终改动保留在目标仓库，等待人工评审。

详细依据见[硬件与模型评估](docs/hardware-and-model-assessment.md)、[实施计划](docs/implementation-plan.md)和[验证报告](docs/verification-report.md)。

## 当前推荐模型

| 用途 | 模型 | 本地包体 | 说明 |
| --- | --- | ---: | --- |
| 主力 coding | `qwen3-coder:30b` | 约 19GB | 30B MoE、约 3.3B 激活，优先使用 |
| 复杂任务备选 | `qwen3.6:35b` | 约 24GB | 中文、推理和新一代 coding 能力更强 |
| 可选异构复核 | `gemma4:26b` | 约 18GB | 后续按需增加，不在 MVP 默认链路 |

不要在 48GB 机器上让多个大模型同时常驻。默认上下文从 32K 起步；256K 是模型上限，不是这台机器上的合理日常配置。

## 快速开始

在新的 Codex 任务中可以直接说：

```text
使用 $local-ai-mvp-builder 构建 /absolute/path/to/project 中的 MVP。
```

显式调用是确保每次加载固定工作流的首选方式。以下命令保留用于环境维护和故障诊断。

下载主模型；如需同时准备备选模型，加 `--with-secondary`：

```bash
./bin/pull-models
./bin/pull-models --with-secondary
```

下载器从 Ollama 官方 registry 读取实时 manifest，使用并行分段下载大权重，并在注册前校验文件大小和 SHA-256。国内网络下会自动使用本机 `127.0.0.1:7890` 代理（若端口可用）。

检查环境：

```bash
./bin/mvp-loop doctor
```

初始化一个新项目：

```bash
./bin/mvp-loop init --project "/absolute/path/to/project"
```

编辑目标项目中的 `.mvp-ai.toml`，填写真实验证命令，然后将初始化文件提交为干净基线。

执行已批准的项目计划：

```bash
./bin/mvp-loop run \
  --project "/absolute/path/to/project" \
  --plan "/absolute/path/to/approved-plan.md" \
  --model primary \
  --live
```

`--model secondary` 可切换到 Qwen 3.6。每次运行的计划、日志、验证结果、结构化评审和最终状态保存在本仓库的 `runs/` 下。

`--live` 会在终端实时显示经过脱敏的模型消息、命令开始/结束、文件改动、验证结果、Review 轮次和 token 用量。它不会显示隐藏的内部思维链；若模型主动提供推理摘要，则会标明为“模型提供的推理摘要”。完整原始输出仍保存在权限受限的 run 目录中。

Skill 启动器默认使用 `--display summary`，让 Codex 对话只保留启动、Review、看门狗接管和最终状态，减少上下文 token。需要在终端观察详细过程时再显式使用 `--display live`。

## 每次开发的文档产物

本地模型、修复模型和 Codex 接管者都必须同步维护：

- `docs/devlog/YYYY-MM-DD.md`：中文记录今日目标、进展、修改、使用方法、验证结果和后续事项。
- `docs/ai/PROJECT_OUTLINE.md`：面向后续 AI 的项目目标、技术栈、架构、重要文件、约束和当前状态。
- `docs/ai/TASK_PLAN.md`：当前里程碑、已完成、进行中、待办、验收标准和下一步。

编排器会检查文件存在、中文内容、必需章节以及本次 Git diff 中确有更新；不满足时不能进入 `ready_for_user_review`。

## 看门狗与版本管理

- 本地模型启动失败、返回错误、总超时或连续 300 秒无输出时，看门狗写入 `watchdog.json` 并让 Codex 直接接管当前 diff。
- 普通代码问题仍遵守两轮本地修复阈值，第二轮失败后由 Codex 接管。
- `summary.json.version_control` 记录当前分支、基线 commit、remote 名称和建议提交信息，不记录 remote URL。
- 开发过程不自动 commit 或 push。用户验收后，显式要求“上传 GitHub”才会创建提交、推送分支并准备 PR。

## 工作流状态

1. 本地模型读取计划与仓库规则并 coding。
2. 执行 `.mvp-ai.toml` 中的验证命令。
3. Codex 对全部未提交变更执行结构化 review。
4. 第一次失败：把 findings 返回本地模型修复。
5. 第二次仍失败：判定本地模型无法解决，由 Codex 监督者亲自接管修复。
6. 监督者修复后做最终确认 review；通过后标记为 `ready_for_user_review`。两轮是本地模型失败阈值，不是禁止最终复核。

## 安全边界

- 默认拒绝在 dirty worktree 上启动，防止混淆用户已有改动。
- 两轮本地 review/fix 协议是固定边界，CLI 不提供缩短为一轮的选项。
- 本地 coding 进程使用 `workspace-write` 沙箱和 `approval=never`，无法通过交互请求扩大权限。
- 不使用 `--dangerously-bypass-approvals-and-sandbox` 或 Qwen Code 的 YOLO 模式。
- 不自动提交、推送、切分支或改写历史。
- 运行记录位于目标项目外，不进入待评审 diff。
- `.env`、令牌和凭据不应写入计划；仓库规则明确禁止模型输出这些信息。
