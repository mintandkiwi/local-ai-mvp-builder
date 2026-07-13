# AI 任务规划

## 当前里程碑

里程碑 M3 已完成：Local AI MVP Builder 已实现跨 Agent 开源部署，关闭自动运行遗留 finding，取得独立 Review 通过结论并发布 Draft PR。

## 已完成

- `M2-01` 至 `M2-08`：完成本地模型、两轮受监督编排、结构化 Review、看门狗、中文文档契约、安全版本快照和原有 29 项回归基线。
- `M3-01`：建立 `integrations/skills/local-ai-mvp-builder/` 唯一 Skill 源，删除备份副本和个人目录维护依赖。
- `M3-02`：加入公共 Agent Skills frontmatter、Agent 无关计划目录、固定门禁、参考文档和 Codex UI 元数据。
- `M3-03`：实现可从任意 cwd 和软链接启动的 `mvp-loop-supervised`，正确映射 summary/live 并拒绝脏工作区绕过。
- `M3-04`：实现 Codex、Claude Code、Antigravity IDE、Antigravity CLI 四平台路径映射和 `~/.local/bin` 入口。
- `M3-05`：实现安装 dry-run、幂等升级、普通冲突拒绝、`--force` 原子备份、失败回滚、软链接父目录保护和部分失败汇总。
- `M3-06`：用临时 HOME 与临时 Git 项目直接测试真实入口，覆盖四平台、冲突、备份、软链接、参数映射、缺失文件和 untracked 门禁。
- `M3-07`：更新 README 的依赖、安装、调用、升级、卸载、恢复、平台差异与 reviewer/supervisor 边界。
- `M3-08`：增加署名为 Yancy Chan 的 MIT `LICENSE`，确认 `.gitignore` 继续排除 `runs/`、缓存和系统文件。
- `M3-09`：更新当日中文日志、AI 项目大纲和任务规划。
- `M3-10`：人工接管修复共享 Skill 非 Codex 接管边界，并在统一配置解析中拒绝空白验证命令。
- `M3-11`：原生环境 44 项测试全部通过，Python 编译与四个 Shell 入口语法通过；故障注入覆盖安装失败后的原 Skill 恢复路径。
- `M3-12`：受监督验证沙箱通过（44 项通过、2 项因父级 Seatbelt 精确跳过），人工修复后的独立 Review verdict 为 `pass`，没有新的 P0/P1/P2。
- `M3-13`：将唯一 Skill 源安装到 Codex、Claude Code、Antigravity IDE 和 Antigravity CLI，核对四个平台文件与统一 PATH 入口；旧 Codex Skill 已生成可恢复备份。
- `M3-14`：创建公开仓库 `mintandkiwi/local-ai-mvp-builder`，推送 `main` 与 `agent/cross-agent-skill`，创建 Draft PR #1。

## 进行中

- 等待用户评审 Draft PR #1；未经新的明确授权不转为 Ready、不合并、不创建 tag/release。

## 待办

- `M4-01`：用首个真实 MVP 验证长任务接管、同日多次日志追加和发布闭环。

## 验收标准

- `SKILL.md` 首行是有效 YAML frontmatter，不含个人路径或前端专属计划目录，仓库中不存在第二 Skill 副本。
- `mvp-loop-supervised --help` 可从任意 cwd 和软链接调用；正式执行验证目标项目、完整脏状态、计划、配置与 doctor。
- 临时 HOME 中四平台安装、dry-run、幂等、冲突拒绝、force 备份和恢复证据可靠，测试不写真实用户配置。
- 原有编排、安全、看门狗和文档回归继续有效，新增真实跨 Agent 测试通过。
- README、MIT License、中文日志和 AI 文档与当前 diff 及验证证据一致。
- 版本管理操作只按用户本轮明确授权执行；不创建 tag/release，不改写 Git 历史，功能变更先通过 Draft PR 交付。

## 下一步

用户评审 `https://github.com/mintandkiwi/local-ai-mvp-builder/pull/1`；通过后再决定合并和版本发布。首个真实 MVP 继续使用 `$local-ai-mvp-builder` 验证完整闭环。
