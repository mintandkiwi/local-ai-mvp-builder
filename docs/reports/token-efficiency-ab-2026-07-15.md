# Local AI MVP Builder Token 效率 A/B 阶段性报告

[中文](token-efficiency-ab-2026-07-15.md) | [English](token-efficiency-ab-2026-07-15-en.md) | [机器可读结果](../../experiments/token-efficiency/results/2026-07-15-interim.json)

## 验证结论

### 总体评估：可公开为阶段性证据，但不能宣称 Token 节省已验证

截至 2026-07-15，预注册的三组配对 A/B 只产生一组运行完整的探索性观测；第二组 direct-Codex 臂在云端 fixer 阶段因外部 usage limit 中断，其余三臂为避免产生不可比数据而停止。

探索性观测中，两臂都通过项目验证、中文文档契约、仓库外隐藏 evaluator 和独立最终 Review，但 local-first 的云端 Token 比 direct-Codex **高 11.79%**，wall time **高 42.60%**。不过两臂之间 Skill 工作版本发生了验证驱动修复，违反“固定同一工作流版本”的严格 A/B 条件，因此有效完整配对数为 **0/3**。当前数据只能用于定位问题，不能用于证明总体回归，更不能支持节省结论；`efficiency_claim` 必须保持 `inconclusive`。

## 要回答的问题

在不降低成功率、P0–P2 标准、项目验证和独立终审的条件下，当前 `adaptive` local-first 工作流能否让三组低/中风险任务的云端 Token 中位数相比 direct-Codex 至少下降 25%。

## 方法审查

- 三个任务在运行前固定：`slug-normalizer`（low）、`retry-schedule`（medium）、`record-batcher`（medium）。
- 每个任务从同一基础提交复制 local-first 与 direct-Codex 两个 worktree。
- 配对内使用相同计划、项目验证、中文文档契约、仓库外隐藏 evaluator 和独立云端 Review 标准。
- 基线仓库只含可通过的 smoke test；隐藏 evaluator 不出现在模型提示中，避免把验收答案直接提供给 coding agent。
- 运行顺序在实验 manifest 中预先交错，实验串行执行，避免并行资源竞争。
- local-first 使用当前 Skill 的实际自适应路由；direct-Codex 使用云端 coding agent，并执行相同验证和独立 Review。若首次 Review 失败，两臂都必须修复并重新终审。
- 原计划要求同一 Skill 工作版本贯穿全部两臂；实际运行时，另一个功能验证暴露了 supervisor capsule 范围/证据问题并触发源码修复。local-first 在修复前启动，direct-Codex 在修复后启动，因此本次观测不进入正式验收统计。
- 主指标是每臂全部云端 coding/reviewer/supervisor/fixer 阶段的 `total_tokens`。本地 Token 单独记录，不换算为云端节省或费用。
- 成功必须同时满足：最终 Review pass、项目验证通过、隐藏 evaluator 通过、基线测试哈希不变。
- 验收阈值预先设为：三组计量完整、两臂质量等价、local-first 云端 Token 中位数至少下降 25%。

## 探索性完整运行结果

| 任务 | 臂 | 成功 | 云端 Token | 本地 Token | 云端调用 | Wall time | 首轮阻塞 finding |
|---|---|---:|---:|---:|---:|---:|---:|
| slug-normalizer | local-first | 是 | 1,025,809 | 681,342 | 3 | 1,112.14 秒 | 7 P2 |
| slug-normalizer | direct-Codex | 是 | 917,581 | 不适用 | 4 | 779.88 秒 | 2 P2 |

计算复核：

- 云端 Token 节省率：`1 - 1,025,809 / 917,581 = -11.79%`。
- 负节省率表示回归：local-first 多使用 108,228 个云端 Token。
- Wall time 变化：`1,112.14 / 779.88 - 1 = +42.60%`。
- local-first 虽少一次云端调用，但单次接管/终审上下文更大，云端总 Token 仍更高。
- local-first 首轮出现 7 个 P2，超过局部修复阈值后由 supervisor 接管；direct-Codex 首轮出现 2 个 P2，经云端 fixer 修复。两臂最终均为 pass，隐藏 evaluator 均通过。
- 以上数字是真实运行观测，但因 Skill 工作版本漂移，`eligible_for_acceptance_statistics = false`。

## 不完整运行

`retry-schedule` 的 direct-Codex 臂完成了 coder 与第一次 Review：

- direct coder：282,610 Token，exact。
- review 1：269,995 Token，exact。
- 已知云端下限：552,605 Token。
- direct fixer：外部 cloud usage limit，Token unavailable，exit 1。
- 未取得最终 Review，状态为 `needs_manual_attention`，不得计入配对统计。

`retry-schedule` 的 local-first 臂及 `record-batcher` 两臂没有启动。额度中断后继续运行会导致两臂处于不同外部条件，因此主动停止比补齐不等价样本更可靠。

## 发现的问题

1. **高影响：工作流版本漂移。** 两臂不是在完全固定的 Skill 工作版本上执行，因此正式有效配对数为 0/3。
2. **高影响：样本不足。** 只有一组运行完整的探索性观测，不能计算预注册的三组中位数，也不能把单一任务推广到所有 MVP。
3. **高影响：首个 local-first 样本触发接管。** 本地编码没有形成 happy path，额外 supervisor 和 final review 重复读取上下文，抵消了本地编码对云端 coding 的替代。
4. **中影响：固定 Review 成本很高。** 两臂都使用相同高标准 Review，这保证任务质量标准一致，但小任务的固定审查成本占比明显。
5. **中影响：外部额度中断。** 第二组计量不完整；未知 Token 没有按 0 处理，也没有纳入统计。
6. **低影响：Wall time 受服务状态影响。** 当前数据能描述本次运行，但不能单独证明稳定的延迟差异。

## 计算与证据校验

- 完整配对两臂的 usage telemetry 均为 `exact`。
- 两臂任务基础提交、批准计划、验证标准和隐藏 evaluator 一致，但 Skill 工作版本不一致；这是排除该样本的依据。
- 两臂原有 `tests/test_baseline.py` SHA-256 保持不变。
- 两臂最终项目验证、中文文档契约、隐藏 evaluator 和独立 Review 均通过。
- 第二组失败阶段 telemetry 为 `unavailable`，报告只给已知下限，不给虚假总数。
- 公开 JSON 与本报告的整数和公式可以独立复算。

## 建议改进

1. 先冻结并提交唯一 Skill 版本；云端额度恢复后，从全新实验根目录重新执行完整三组，不续接本轮半成品。
2. 在不降低 Review 标准的前提下，减少 supervisor/final-review capsule 中与 finding 无关的重复上下文。
3. 对明显小型低风险任务增加“本地实现质量门禁”：隐藏或扩展边界测试先通过，再调用云端 reviewer，避免 reviewer 发现大量基础 P2 后直接接管。
4. 把中文文档模板的“保留同日历史、稳定任务 ID、可执行下一步”要求提供为确定性脚手架，减少 reviewer 在每个样本中重复发现文档问题。
5. 完整重跑后再判断 `measured_saving`、`regression` 或继续 `inconclusive`；25% 门槛不变。

## 必须公开的限制

- 本报告是阶段性报告，不是 Token 节省证明。
- 唯一运行完整的探索性观测显示回退，但因工作流版本漂移，不能据此证明当前固定版本必然回归。
- 实验使用三个小型、无依赖 Python 任务，不能代表大型前端、移动端、数据库或硬件项目。
- 报告不公开机器参数、具体本地模型、凭据、原始提示或私有日志。
- Token 是当前配置下的相对用量，不等于费用，也不应跨不同模型直接换算。

## 复现入口

- Harness：[run_ab.py](../../experiments/token-efficiency/run_ab.py)
- 公开结果：[2026-07-15-interim.json](../../experiments/token-efficiency/results/2026-07-15-interim.json)
- 基本命令：`python3 experiments/token-efficiency/run_ab.py --help`

重跑前必须保证 Skill 仓库是干净提交；`prepare` 会把 HEAD 写入 manifest，各臂执行前后都会复核：

```sh
python3 experiments/token-efficiency/run_ab.py prepare --root <external-run-root>
python3 experiments/token-efficiency/run_ab.py run-local --root <external-run-root> --task slug-normalizer
python3 experiments/token-efficiency/run_ab.py run-direct --root <external-run-root> --task slug-normalizer
# 按 manifest 顺序完成另外两组后：
python3 experiments/token-efficiency/run_ab.py aggregate --root <external-run-root>
```

完整运行证据保存在仓库外的权限受限目录，不提交原始模型日志、机器信息或具体模型配置。
