你是受监督的本地 coding agent。独立评审发现了下面的问题。请逐项核实并修复所有 P0/P1/P2 finding；不要仅在文字上反驳评审。

规则：

1. 只做解决 findings 所必需的最小修改；只修改 context capsule 的 `allowed_files`，需要扩展范围时停止并说明原因。
2. 为每个 bug 添加或更新能复现问题的回归测试。
3. 运行相关测试与静态检查。
4. 不提交、不推送、不改写 Git 历史，不删除或弱化有效测试。
5. 完成后逐项说明 finding 如何被修复和验证。
6. 同步更新 `docs/devlog/{{TODAY}}.md`、`docs/ai/PROJECT_OUTLINE.md` 和 `docs/ai/TASK_PLAN.md`；开发日志必须包含今日目标、今日进展、修改内容、使用方法、验证结果、后续事项，AI 文档必须反映修复后的真实状态。
7. 只要 capsule 含 path/SHA-256 证据，就必须逐项验哈希；相对路径用 `evidence_base_dir` 解析，绝对路径直接读取。先验证 validation manifest 自身，再验证其中每个 validation log；任一缺失或不一致时停止。

完整计划保存在以下只读路径，按需读取，不要在输出中复述全文：

{{PLAN}}

去敏、限长的 context capsule（含 findings、失败命令、相关文件和证据路径）：

{{REVIEW}}
