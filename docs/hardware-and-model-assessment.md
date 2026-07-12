# 硬件与模型评估（2026-07-12）

## 本机结论

| 项目 | 实测配置 | 对本地 AI 的意义 |
| --- | --- | --- |
| 设备 | MacBook Pro / Apple M5 Pro | Apple Silicon 统一内存适合 MLX、Ollama 和 llama.cpp |
| CPU | 18 核 | 足够承担 agent 工具调用、编译与测试 |
| GPU | 20 核，Metal 4 | 适合 4-bit 量化推理，不适合大模型训练 |
| 统一内存 | 48GB | 20B–35B Q4/MXFP4 是日常甜点区；70B+ 不宜作为主力 |
| 可用磁盘 | 约 642GiB | 足够保存两到三个 20GB 级模型和多个项目 |
| 当前工具 | Ollama 0.31.2、Codex CLI、Cursor、VS Code | 已具备最短落地路径，无需 Docker |

体检时系统没有发生 swap in/out，系统报告约 55% 内存可用。实际运行模型时仍需给 macOS、IDE、编译器和 KV cache 留出至少 12–16GB，因此不能只按模型文件大小判断是否“装得下”。

## 推荐顺序

### 1. Qwen3-Coder 30B：主力执行者

Ollama 包约 19GB，30B 总参数、约 3.3B 激活，原生面向 agentic coding 和长周期工具调用。它在 48GB 机器上能留下足够空间给 32K 上下文、Codex 工具层和构建测试，是第一阶段风险最低的选择。

来源：[Qwen3-Coder 官方发布](https://qwenlm.github.io/blog/qwen3-coder/)、[Ollama 模型页](https://ollama.com/library/qwen3-coder)

### 2. Qwen 3.6 35B-A3B：复杂任务备选

Ollama Q4 包约 24GB，适合中文需求理解、规划和主力模型失败后的第二次本地尝试。虽然模型声明支持 256K 上下文，但本机默认限制为 32K；需要 repo 级长上下文时，应优先用检索和分段，而不是盲目扩大 KV cache。

来源：[Qwen 3.6 官方权重页](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)、[Ollama tags](https://ollama.com/library/qwen3.6/tags)

### 3. Gemma 4 26B-A4B：可选异构模型

Ollama 包约 18GB，约 4B 激活，兼具推理、coding 和图像输入。它适合后续做跨模型复核或多模态 MVP，但第一阶段已有独立 Codex Review，暂不默认下载。

来源：[Google Gemma 4 文档](https://ai.google.dev/gemma/docs/core)、[Ollama 模型页](https://ollama.com/library/gemma4)

### 不推荐：Qwen3-Coder-Next 本地 Q4

该模型约 80B 总参数、3B 激活，但 Ollama Q4 权重本身约 52GB。MoE 的“激活参数少”能降低计算量，却不会让所有权重免于装入内存；48GB 机器无法为权重、系统与 KV cache 同时留出合理空间。可将它保留为云端或未来 64/96GB 机器选项。

来源：[Ollama Qwen3-Coder-Next](https://ollama.com/library/qwen3-coder-next)

## 运行原则

- 同一时间只常驻一个 20GB 级模型。
- 默认 32K context；只有实测内存和质量均需要时再提升到 48K/64K。
- coding 用确定性较高的配置和明确验收标准，不以聊天观感替代测试。
- 不在这台笔记本上做全参数训练；如需定制，优先考虑小模型 LoRA，并另立资源评估。
