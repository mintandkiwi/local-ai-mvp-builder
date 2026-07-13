# 环境验证报告（2026-07-12）

## 主模型运行

`qwen3-coder:30b` 官方模型层经 SHA-256 校验后注册到 Ollama。32K context 下的短生成探针结果：

| 指标 | 结果 |
| --- | ---: |
| 模型加载 | 7.21 秒 |
| 短输出速度 | 111.79 tokens/s |
| Ollama 常驻占用 | 约 21GB |
| 处理器 | 100% GPU |
| 模型常驻后的系统可用内存 | 约 18% |

短输出速度不能等同于长任务的持续吞吐，但证明 M5 Pro 的 Metal 路径、量化权重和 32K KV cache 均可正常工作，没有发生 swap。

`qwen3.6:35b` 备选模型也已通过 32K 探针：首次加载 8.82 秒、thinking 输出约 68.16 tokens/s，常驻约 23GB，系统可用内存约 15%；关闭 thinking 的短输出约 83.54 tokens/s。它可以运行，但内存余量比主模型更小，因此只按需切换。

## 端到端冒烟项目

测试任务要求本地模型实现带无效区间检查的 `clamp` 函数，并覆盖下界、上界、区间内、包含边界和异常范围。

| 阶段 | 结果 |
| --- | --- |
| 本地 Codex + Qwen coding | 54.00 秒，正确修改实现与测试 |
| Seatbelt 验证 | `python3 -m unittest -v`，6/6 通过 |
| 独立云端结构化 review | `pass`，无 findings |
| 最终状态 | `ready_for_user_review` |
| Git 行为 | 未 commit、未 push |

验证记录保存在被 `.gitignore` 排除的 `runs/` 目录中。故障路径也已实测：在 loopback 代理配置错误导致本地 agent 无法启动时，编排器写出了包含错误、Git 状态和 `needs_manual_attention` 的 `summary.json`。

## 沙箱探针

- 允许目标项目文件写入。
- 拒绝 `/tmp` 等项目外路径写入。
- 拒绝读取 `~/.codex/auth.json` 和工作区外普通用户文件。
- 拒绝修改目标项目 `.git` 索引。
- 验证进程网络被禁用，环境变量仅保留构建所需白名单。

## 实时可观测性

使用真实 `qwen3-coder:30b` 只读探针验证了 `--live` 事件流。终端依次显示会话建立、模型开始处理、模型说明和 token 用量；原始 JSONL 仍写入 run 日志。命令正文输出不会进入实时摘要，常见 API key、token、password、secret、Bearer token 和私钥标记会在显示前脱敏。

编排器现有 29 项单元测试全部通过，其中包括实时 JSONL 流、敏感信息脱敏、命令输出隔离、run 目录权限、live 模式参数、中文开发文档契约、版本快照和看门狗接管测试。Skill 格式校验也已通过。

## 中文文档与看门狗

- 缺少当日中文开发日志、AI 项目大纲或 AI 任务规划时，文档契约返回非零状态并强制 Review 失败。
- 文档存在但中文内容不足、缺少规定章节或没有出现在本次 Git diff 中时，同样阻止交付。
- 模拟本地模型命令失败后，看门狗生成 `watchdog.json`，跳过进一步本地重试，由 Codex 监督者接管并执行验证与最终 Review。
- 模拟本地进程无输出时，空闲超时在阈值内终止进程并保留日志；默认阈值为 300 秒。
- 版本快照仅保存 remote 名称，不保存 URL，且明确标记 `commit_created=false`、`pushed=false`。
