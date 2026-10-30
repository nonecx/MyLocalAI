---
base_model: Qwen/Qwen2.5-0.5B-Instruct
library_name: peft
pipeline_tag: text-generation
tags:
- base_model:adapter:Qwen/Qwen2.5-0.5B-Instruct
- lora
- transformers
---
# 目录说明
# 本目录存放逻辑谬误模块训练产出的 LoRA 权重（checkpoint-* 系列）。
# 由 train.py 训练生成，运行时由 shared_core.base_module 自动加载。
### Framework versions

- PEFT 0.21.0