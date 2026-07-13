# Project documentation contract

Update all three files during every development run. Write concise Chinese and keep claims consistent with the current diff and validation evidence.

## Daily development log

Use `docs/devlog/YYYY-MM-DD.md` and preserve earlier same-day entries. Include these headings:

```markdown
# YYYY-MM-DD 开发日志

## 今日目标
## 今日进展
## 修改内容
## 使用方法
## 验证结果
## 后续事项
```

Record commands, changed modules, completed and incomplete work, actual results, and safe usage instructions.

## AI project outline

Use `docs/ai/PROJECT_OUTLINE.md` with these headings:

```markdown
# 项目开发大纲

## 项目目标
## 技术栈
## 架构与关键路径
## 重要文件
## 约束
## 当前状态
```

Explain entry points, data flow, configuration boundaries, and non-obvious constraints for an AI agent with no conversation history.

## AI task plan

Use `docs/ai/TASK_PLAN.md` with these headings:

```markdown
# AI 任务规划

## 当前里程碑
## 已完成
## 进行中
## 待办
## 验收标准
## 下一步
```

Keep stable task identifiers and explicit statuses. Make the next task executable without chat history.
