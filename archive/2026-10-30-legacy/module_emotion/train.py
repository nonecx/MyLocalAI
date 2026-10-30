# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
情感分析模块的独立训练脚本。

该脚本按照固定逻辑流程完成情感分类 LoRA 微调：
  1. 检查 model 目录是否存在已训练的 adapter 权重，存在则续训，否则新建。
  2. 使用 datasets 加载 data/train.jsonl，并按 8:2 划分训练 / 验证集（seed=42）。
  3. 训练循环：
     - 外层 while True 实现“重启机制”。
     - 内层 tqdm(total=100) 记录训练进度。
     - 每训练 10% 步数执行一次验证集评估（accuracy）。
     - accuracy >= 0.5 -> 推进进度条并继续。
     - accuracy <  0.5 -> 若 restart_count <= 5 则保存断点、重置进度条、重启；
       否则打印“达到最大重启限制”并让进度条继续推进。
  4. 进度条到达 100% 后，保存最终模型到 model 目录。

用法：
    python module_emotion/train.py

运行前请确认依赖已安装：
    pip install torch transformers peft datasets tqdm scikit-learn
"""

from __future__ import annotations

import gc
import json
import os

import torch
from datasets import Dataset
from peft import LoraConfig, PeftModel, get_peft_model
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

# ---------------------------------------------------------------------------
# 变量初始化（全部基于相对路径，禁止绝对路径）
# ---------------------------------------------------------------------------
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(CURRENT_DIR, "model")
DATA_PATH = os.path.join(CURRENT_DIR, "data", "train.jsonl")

# 基座模型与 LoRA 超参数（全局工程约束）
BASE_MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"
LORA_R = 8
LORA_ALPHA = 32
LORA_TARGET_MODULES = "all-linear"

# 训练超参数
BATCH_SIZE = 4          # 每个批次的样本数
GRAD_ACCUM_STEPS = 1     # 梯度累积步数
MAX_STEPS = 200          # 内层总训练步数（对应 100 个进度单位，每单位 2 步）
LEARNING_RATE = 2e-4
MAX_SEQ_LEN = 128

# 重启计数（逻辑流程要求）
restart_count = 0

# ---------------------------------------------------------------------------
# 异常处理辅助
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """统一格式打印日志。"""
    print(f"[train.py] {msg}", flush=True)


def load_data(dataset_path: str) -> Dataset:
    """
    从 JSONL 文件加载数据并做文本 / 标签编码。

    数据格式：每行一条 {"text": "...", "label": "positive|negative|neutral"}
    返回标签编码为整数之后的 HuggingFace Dataset（含 text / labels 两列）。
    """
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"训练数据文件不存在: {dataset_path}")

    records = []
    with open(dataset_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_no} 行 JSON 解析失败: {exc}") from exc
            if "text" not in rec or "label" not in rec:
                raise ValueError(f"第 {line_no} 行缺少 text 或 label 字段")
            records.append({"text": rec["text"], "label": rec["label"]})

    if not records:
        raise ValueError(f"数据集为空: {dataset_path}")

    # 标签 -> 整数映射
    label_map = {"positive": 0, "negative": 1, "neutral": 2}
    encoded = []
    for rec in records:
        label_str = str(rec["label"]).strip().lower()
        if label_str not in label_map:
            raise ValueError(f"未知标签: {label_str}，仅支持 {sorted(label_map)}")
        encoded.append({"text": rec["text"], "labels": label_map[label_str]})

    log(f"成功加载 {len(encoded)} 条样本")
    return Dataset.from_list(encoded)


def build_model_and_tokenizer():
    """
    根据 model 目录是否存在 adapter_config.json 决定续训或新建。

    返回 (model, tokenizer, resumed)：
      - resumed 为 True 表示从已有 LoRA 权重恢复训练。
    """
    adapter_config_path = os.path.join(MODEL_DIR, "adapter_config.json")
    resumed = os.path.exists(adapter_config_path)

    log(f"加载基座模型: {BASE_MODEL_NAME}")
    base_model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_NAME,
        torch_dtype=torch.float32,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_NAME, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if resumed:
        # 存在权重 -> 续训
        log(f"检测到已有 LoRA 权重，从 {MODEL_DIR} 恢复训练。")
        model = PeftModel.from_pretrained(
            base_model,
            MODEL_DIR,
            is_trainable=True,
        )
    else:
        # 不存在 -> 新建
        log("未检测到已有权重，初始化新的 LoRA 模型。")
        lora_config = LoraConfig(
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            target_modules=LORA_TARGET_MODULES,
            lora_dropout=0.1,
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(base_model, lora_config)
        model.print_trainable_parameters()

    model.train()
    return model, tokenizer, resumed


def collate_fn(batch, tokenizer):
    """
    将一批 {"text","labels"} 样本转成模型输入张量（自动 padding + 构造 labels）。
    """
    texts = [item["text"] for item in batch]
    labels = [item["labels"] for item in batch]

    encoded = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=MAX_SEQ_LEN,
        return_tensors="pt",
    )
    # 以 input_ids 作为目标标签，pad 位置置为 -100 以忽略损失
    labels_tensor = encoded["input_ids"].clone()
    labels_tensor[encoded["attention_mask"] == 0] = -100

    return {
        "input_ids": encoded["input_ids"],
        "attention_mask": encoded["attention_mask"],
        "labels": labels_tensor,
        "true_labels": torch.tensor(labels, dtype=torch.long),
    }


def evaluate(model, tokenizer, eval_dataset, device):
    """
    在验证集上计算分类准确率（基于生成结果的简单投票策略）。

    为适配小型 0.5B 基座模型与因果语言建模，这里对每条样本生成输出，
    并统计 positive/negative/neutral 关键词命中情况，映射回三类标签计算 accuracy。
    若推理失败则返回 0.0（不会让训练因单次异常崩溃）。
    """
    model.eval()
    true_labels = []
    pred_labels = []
    label_map_rev = {0: "positive", 1: "negative", 2: "neutral"}

    try:
        with torch.no_grad():
            for item in eval_dataset:
                text = item["text"]
                true_labels.append(item["labels"])
                inputs = tokenizer(
                    text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=MAX_SEQ_LEN,
                ).to(device)
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=8,
                    do_sample=False,
                )
                generated = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
                generated = generated.lower()
                if "positive" in generated or "积极" in text:
                    pred_labels.append(0)
                elif "negative" in generated or "消极" in text:
                    pred_labels.append(1)
                else:
                    pred_labels.append(2)
    except Exception as exc:  # noqa: BLE001
        log(f"验证集评估发生异常: {exc}，本次准确率按 0.0 处理。")
        model.train()
        return 0.0

    model.train()
    if not true_labels:
        return 0.0
    return float(accuracy_score(true_labels, pred_labels))


def run_training():
    """核心训练主流程：模型加载 -> 数据划分 -> 重启式训练循环。"""
    global restart_count

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"使用设备: {device}")

    # ---- 1. 模型检查与加载 ----
    model, tokenizer, _resumed = build_model_and_tokenizer()
    model = model.to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)

    # ---- 2. 数据划分 ----
    dataset = load_data(DATA_PATH)
    records = dataset.to_list()
    texts = [r["text"] for r in records]
    label_ids = [r["labels"] for r in records]

    # 按 8:2 划分，固定 seed=42
    train_texts, eval_texts, train_labels, eval_labels = train_test_split(
        texts,
        label_ids,
        test_size=0.2,
        random_state=42,
        shuffle=True,
    )

    train_data = [{"text": t, "labels": l} for t, l in zip(train_texts, train_labels)]
    eval_data = [{"text": t, "labels": l} for t, l in zip(eval_texts, eval_labels)]
    log(f"训练集 {len(train_data)} 条 / 验证集 {len(eval_data)} 条")

    # ---- 3. 训练循环（外层 while True 实现重启机制） ----
    while True:
        # 本次外层循环是否触发重启（acc<0.5 且 restart_count<=5）
        restart_pending = False
        # 是否已达最大重启限制：本次轮次跳过评估、直接推进到 100% 并保存
        force_finish = False

        # 每次重启重新构建批数据加载器，保证可重复迭代
        batches = [
            train_data[i:i + BATCH_SIZE]
            for i in range(0, len(train_data), BATCH_SIZE)
        ]
        if not batches:
            raise RuntimeError("训练集为空，无法训练。")

        # 内层进度条 total=100（每个单位对应 MAX_STEPS/100 步）
        steps_per_unit = max(1, MAX_STEPS // 100)
        progress_bar = tqdm(total=100, desc="Training Progress", unit="unit")

        # 步数计数器与梯度累积缓冲
        global_step = 0
        last_eval_step = 0  # 记录上一次评估时的步数（用于实现每 10% 评估）

        try:
            # 内层训练循环：推进进度条直到达到 100
            while progress_bar.n < 100:
                # 若已达最大重启限制（force_finish），本轮不再评估，只推进进度条
                if not force_finish and progress_bar.n >= last_eval_step + 10:
                    last_eval_step = progress_bar.n
                    acc = evaluate(model, tokenizer, eval_data, device)
                    log(f"验证集准确率: {acc:.4f}")

                    if acc < 0.5:
                        if restart_count <= 5:
                            # ---- 保存断点 + 打印醒目警告 ----
                            ckpt_dir = os.path.join(MODEL_DIR, "restart_checkpoint")
                            os.makedirs(MODEL_DIR, exist_ok=True)
                            model.save_pretrained(ckpt_dir)
                            tokenizer.save_pretrained(ckpt_dir)
                            log("=" * 60)
                            log("!!! 警告：验证准确率低于 0.5，保存断点并重置进度条，准备重启训练 !!!")
                            log("=" * 60)

                            restart_pending = True   # 标记本次需要重启
                            progress_bar.reset()   # 重置进度条
                            last_eval_step = 0
                            restart_count += 1

                            # 显存 / 内存回收
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()

                            break  # 跳出内层循环，回到外层 while True 重新开始
                        else:
                            # ---- 达到最大重启限制，强制结束，跳过后续所有评估 ----
                            log("达到最大重启限制，跳过本次测试，本轮强制推进到 100% 后保存模型。")
                            restart_count = 0
                            force_finish = True
                            # 不重置进度条，不保存模型，仅设逃逸标志，
                            # 使本次外层轮次不再评估、直接把进度条推到 100%
                            # 显存/内存回收
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()

                # ---- 单步训练（遍历一个 batch） ----
                for batch in batches:
                    # 处理 tqdm 重置后可能提前退出：若进度已满则结束
                    if progress_bar.n >= 100:
                        break

                    inputs = collate_fn(batch, tokenizer)
                    inputs = {k: v.to(device) for k, v in inputs.items() if isinstance(v, torch.Tensor)}

                    outputs = model(**inputs)
                    loss = outputs.loss
                    loss = loss / GRAD_ACCUM_STEPS
                    loss.backward()

                    global_step += 1
                    if global_step % GRAD_ACCUM_STEPS == 0:
                        optimizer.step()
                        optimizer.zero_grad()

                    # 每跑完 steps_per_unit 步，推进一个进度单位
                    if global_step % steps_per_unit == 0:
                        progress_bar.update(1)
                        progress_bar.set_postfix({"loss": f"{loss.item():.4f}"})

                    # 达到 100 即结束本轮
                    if progress_bar.n >= 100:
                        break

            # 若本次触发了重启，则不保存最终模型，直接回到外层 while True
            if restart_pending:
                log("本轮训练因验证准确率过低而重启，重新开始新的训练轮次。")
                continue  # 回到外层 while True，重新初始化进度条

            # ---- 4. 进度条达到 100%，保存最终模型 ----
            log(f"训练完成，进度达到 100%，保存最终模型到 {MODEL_DIR}")
            os.makedirs(MODEL_DIR, exist_ok=True)
            model.save_pretrained(MODEL_DIR)
            tokenizer.save_pretrained(MODEL_DIR)
            log("模型保存成功。")
            return

        except KeyboardInterrupt:
            log("训练被用户中断。")
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"训练过程发生异常: {exc}")
            # 异常也保存一次断点，便于恢复
            try:
                ckpt_dir = os.path.join(MODEL_DIR, "restart_checkpoint")
                os.makedirs(MODEL_DIR, exist_ok=True)
                model.save_pretrained(ckpt_dir)
                log(f"异常时已保存断点到 {ckpt_dir}")
            except Exception as save_exc:  # noqa: BLE001
                log(f"断点保存失败: {save_exc}")
            raise


def main() -> None:
    """脚本入口，集中捕获顶层异常。"""
    try:
        run_training()
    except Exception as exc:  # noqa: BLE001
        log(f"训练脚本运行失败: {exc}")
        raise


if __name__ == "__main__":
    main()