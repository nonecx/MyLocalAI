# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""攻击性分析模块的独立训练脚本（含"进度条-验证-重启"流程，快速演示版）。

本版本把训练规模调小以在内存受限的 CPU 上快速跑通完整流程：
  - MAX_STEPS = 5        （只跑 5 步就结束）
  - MAX_LENGTH = 32      （输入截断到 32 token）
  - MAX_SAMPLES = 10     （数据裁剪到 10 条）
  - load_samples 增加 try-except，自动跳过损坏/非法行，保证数据加载成功。
  - 训练循环加入 tqdm 进度条 + 定期验证 + 容错重启机制（最大 5 次）。

用法：
    python module_attack/train.py
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time

import torch

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(CURRENT_DIR))  # 允许导入 shared_core

from shared_core.base_module import BaseModule  # noqa: E402
from tqdm import tqdm  # noqa: E402

# ---- 路径常量（全部基于 CURRENT_DIR，禁止相对路径漂移） ----
MODEL_DIR = os.path.join(CURRENT_DIR, "model")
DATA_PATH = os.path.join(CURRENT_DIR, "data", "train.jsonl")

# 基座模型与 LoRA 超参数（全局工程约束）
BASE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
LORA_R = 8
LORA_ALPHA = 32
LORA_TARGET_MODULES = "all-linear"

# ---- 快速演示用超参数 ----
BATCH_SIZE = 4            # CPU 友好的小批量
MAX_LENGTH = 32           # 输入截断 / padding 上限（调小，加快训练）
MAX_STEPS = 5             # 总训练步数（调小，几秒到几十秒跑完）
EVAL_STEPS = 2            # 每多少步评估一次（配合重启机制）
MAX_SAMPLES = 10          # 只取前 10 条样本，快速验证
LEARNING_RATE = 2e-4
RESTART_LIMIT = 5         # 最大重启次数
ACC_THRESHOLD = 0.5       # 验证准确率阈值

PROMPT_PREFIX = "判断以下文本是否包含攻击性或辱骂内容。只回答“是”或“否”。\n文本：“"
PROMPT_SUFFIX = "”\n答案："


def log(msg: str) -> None:
    """统一格式打印日志。"""
    print(f"[attack.train] {msg}", flush=True)


class AttackModule(BaseModule):
    """攻击性分析模块（判断文本是否含攻击/辱骂，及攻击强度分级）。"""

    name = "attack"

    def analyze(self, text: str) -> dict:
        """返回攻击性判断与强度。"""
        return {"is_attack": False, "intensity": 0.0, "categories": []}


def make_prompt(text: str) -> str:
    return PROMPT_PREFIX + text + PROMPT_SUFFIX


def load_samples(data_path: str = DATA_PATH, max_samples: int = MAX_SAMPLES) -> list[dict]:
    """加载 JSONL 训练样本。

    改进点：对每一行用 try-except 包裹解析，损坏/缺字段的行只打印警告并跳过，
    绝不让单行坏数据中断整个加载流程。返回有效样本，最多 max_samples 条。
    """
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"找不到训练数据文件：{data_path}")

    samples = []
    skipped = 0
    with open(data_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if len(samples) >= max_samples:
                break  # 已取够 max_samples 条
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "text" not in rec or "label" not in rec:
                    raise ValueError("缺少 text 或 label 字段")
                samples.append(rec)
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                log(f"跳过第 {line_no} 行（解析失败）: {exc}")

    log(f"加载完成：有效 {len(samples)} 条，跳过坏行 {skipped} 条")
    return samples


def build_dataset(samples: list[dict], tokenizer) -> list[dict]:
    """
    把样本转成训练记录。

    记录包含：input_ids / attention_mask / labels（-100 掩盖 prompt 部分）/
    expected（期望答案）/ _text（原始文本，供 evaluate 使用）。
    """
    pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id
    records = []
    for s in samples:
        text = s["text"]
        expected = "是" if s.get("label") == "attack" else "否"
        prompt = make_prompt(text)
        full = prompt + expected

        full_ids = tokenizer(
            full, add_special_tokens=True, truncation=True, max_length=MAX_LENGTH
        )["input_ids"]
        prompt_ids = tokenizer(
            prompt, add_special_tokens=True, truncation=True, max_length=MAX_LENGTH
        )["input_ids"]
        prompt_len = len(prompt_ids)

        labels = [-100] * prompt_len + full_ids[prompt_len:]
        if len(labels) < len(full_ids):  # 极端截断兜底
            labels = labels + full_ids[len(labels):]

        records.append({
            "input_ids": full_ids,
            "attention_mask": [1] * len(full_ids),
            "labels": labels,
            "expected": expected,
            "_text": text,
        })
    return records


def collate(batch_recs: list[dict], tokenizer, device) -> dict:
    """将一个 batch 的记录 padding 成同长度张量（有 max_length 上限，CPU 友好）。"""
    pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id
    max_len = max(len(r["input_ids"]) for r in batch_recs)
    max_len = min(max_len, MAX_LENGTH)
    bsz = len(batch_recs)

    input_ids = torch.full((bsz, max_len), pad_id, dtype=torch.long)
    attention_mask = torch.zeros((bsz, max_len), dtype=torch.long)
    labels = torch.full((bsz, max_len), -100, dtype=torch.long)

    for i, r in enumerate(batch_recs):
        ids = r["input_ids"][:max_len]
        L = len(ids)
        input_ids[i, :L] = torch.tensor(ids, dtype=torch.long)
        attention_mask[i, :L] = 1
        labels[i, :L] = torch.tensor(r["labels"][:max_len], dtype=torch.long)

    return {
        "input_ids": input_ids.to(device),
        "attention_mask": attention_mask.to(device),
        "labels": labels.to(device),
    }


def evaluate(model, tokenizer, records: list[dict]) -> float:
    """对验证样本评估“生成答案是否等于期望答案”的准确率。"""
    model.eval()
    if not records:
        model.train()
        return 0.0
    correct = 0
    try:
        with torch.no_grad():
            for rec in records:
                prompt = make_prompt(rec["_text"])
                enc = tokenizer(
                    prompt, return_tensors="pt", truncation=True, max_length=MAX_LENGTH
                ).to(model.device)
                out = model.generate(
                    **enc,
                    max_new_tokens=8,
                    do_sample=False,
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                )
                gen_txt = tokenizer.decode(
                    out[0, enc["input_ids"].shape[1]:], skip_special_tokens=True
                ).strip()
                if gen_txt == rec["expected"] or gen_txt.startswith(rec["expected"]):
                    correct += 1
    except Exception as exc:  # noqa: BLE001
        log(f"评估发生异常: {exc}，本次准确率按 0.0 处理。")
        model.train()
        return 0.0
    model.train()
    return correct / len(records)


def _train_one_step(model, optimizer, records, tokenizer, device, step, n_batches) -> float:
    """执行一个 step 的 forward/backward，返回该步 loss 值。"""
    bi = step % n_batches
    batch_recs = records[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
    batch = collate(batch_recs, tokenizer, device)
    out = model(**batch)
    loss = out.loss
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    return float(loss.item())


def _progress_to(progress_bar, global_step, max_steps) -> None:
    """按 global_step/max_steps 把进度条推进到对应的百分比（0-100）。"""
    target = int(100 * global_step / max(1, max_steps))
    progress_bar.n = min(100, target)
    progress_bar.refresh()


def run_training(
    base_model: str = BASE_MODEL,
    model_dir: str = MODEL_DIR,
    data_path: str = DATA_PATH,
    max_steps: int = MAX_STEPS,
    eval_steps: int = EVAL_STEPS,
    lr: float = LEARNING_RATE,
) -> None:
    """攻击性模块的核心训练流程（含"进度条-验证-重启"）。"""
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    restart_count = 0

    os.makedirs(model_dir, exist_ok=True)
    module = AttackModule(model_name_or_path=base_model, lora_path=model_dir)

    adapter_config = os.path.join(model_dir, "adapter_config.json")
    resumed = os.path.exists(adapter_config)

    log(f"加载基础模型 {base_model} ...")
    if os.environ.get("HF_ENDPOINT") is None:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    tokenizer = None
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            base_model, trust_remote_code=True, local_files_only=True
        )
    except Exception as exc:  # noqa: BLE001
        log(f"本地离线加载 tokenizer 失败({exc})，尝试在线镜像加载...")
        tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"使用设备: {device}")

    base = None
    try:
        base = AutoModelForCausalLM.from_pretrained(
            base_model, trust_remote_code=True, torch_dtype=torch.float16,
            low_cpu_mem_usage=True, local_files_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"本地离线加载模型失败({exc})，尝试在线镜像加载...")
        base = AutoModelForCausalLM.from_pretrained(
            base_model, trust_remote_code=True, torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
        )
    base = base.to(device)

    if resumed:
        log(f"检测到已有 LoRA 权重，从 {model_dir} 续训。")
        model = PeftModel.from_pretrained(base, model_dir, is_trainable=True)
    else:
        log("未检测到已有权重，初始化新的 LoRA 模型。")
        lora_config = LoraConfig(
            task_type="CAUSAL_LM",
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            lora_dropout=0.05,
            target_modules=LORA_TARGET_MODULES,
        )
        model = get_peft_model(base, lora_config)
        model.print_trainable_parameters()
    model = model.to(device)

    # 数据（load_samples 已做 try-except 跳过坏行 + 裁剪到 MAX_SAMPLES）
    samples = load_samples(data_path, max_samples=MAX_SAMPLES)
    records = build_dataset(samples, tokenizer)
    log(f"构造训练记录 {len(records)} 条（用前 {MAX_SAMPLES} 条）")
    n_batches = max(1, (len(records) + BATCH_SIZE - 1) // BATCH_SIZE)

    # ================= 进度条 - 验证 - 重启 流程 =================
    while True:
        restart_pending = False
        force_finish = False

        model.train()
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)

        progress_bar = tqdm(total=100, desc="Attack Training Progress", unit="%")
        global_step = 0
        running_loss = 0.0
        last_acc = 0.0
        best_acc = 0.0
        final_loss = 0.0
        start = time.time()

        try:
            # 内层训练：每步推进进度条，并按 eval_steps 定期验证 + 容错重启
            for step in range(max_steps):
                loss_val = _train_one_step(model, optimizer, records, tokenizer, device, step, n_batches)
                running_loss += loss_val
                global_step += 1
                _progress_to(progress_bar, global_step, max_steps)
                progress_bar.set_postfix({"loss": f"{loss_val:.4f}", "step": global_step})

                # 每 eval_steps 步（或最后一步）做一次验证
                if global_step % eval_steps == 0 or global_step == max_steps:
                    acc = evaluate(model, tokenizer, records)
                    last_acc = acc
                    best_acc = max(best_acc, acc)
                    final_loss = running_loss / global_step
                    log(f"验证 step={global_step}/{max_steps} loss={final_loss:.4f} acc={acc:.4f}")

                    if acc < ACC_THRESHOLD and not force_finish:
                        if restart_count <= RESTART_LIMIT:
                            ckpt_dir = os.path.join(model_dir, "restart_checkpoint")
                            os.makedirs(model_dir, exist_ok=True)
                            model.save_pretrained(ckpt_dir)
                            log("=" * 60)
                            log(f"!!! 警告：acc={acc:.4f} < {ACC_THRESHOLD}，保存断点并重启（第 {restart_count + 1} 次）!!!")
                            log("=" * 60)
                            restart_pending = True
                            progress_bar.reset()
                            restart_count += 1
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                            break  # 跳出内层 for，回外层 while True 重启
                        else:
                            log(f"达到最大重启限制({RESTART_LIMIT})，跳过本次测试，强制推进到结束并保存模型。")
                            restart_count = 0
                            force_finish = True
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()

            if restart_pending:
                log("本轮因准确率过低而重启，开始新的训练轮次。")
                continue  # 回到外层 while True

            # 进度条补满 100%
            _progress_to(progress_bar, max_steps, max_steps)

            log(f"训练完成，进度达 100%（step={global_step}），保存最终模型。")
            os.makedirs(model_dir, exist_ok=True)
            model.save_pretrained(model_dir)
            tokenizer.save_pretrained(model_dir)
            elapsed = time.time() - start
            log(
                f"训练完成，LoRA 保存至 {model_dir}，"
                f"final_loss={final_loss:.4f} last_accuracy={last_acc:.4f} "
                f"best_accuracy={best_acc:.4f} 耗时 {elapsed:.1f}s"
            )
            return

        except KeyboardInterrupt:
            log("训练被用户中断。")
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"训练过程发生异常: {exc}")
            raise


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="攻击性分析模块训练")
    parser.add_argument("--data", default=DATA_PATH)
    parser.add_argument("--base_model", default=BASE_MODEL)
    parser.add_argument("--model_dir", default=MODEL_DIR)
    parser.add_argument("--lr", type=float, default=LEARNING_RATE)
    parser.add_argument("--max_steps", type=int, default=MAX_STEPS)
    parser.add_argument("--eval_steps", type=int, default=EVAL_STEPS)
    args = parser.parse_args()

    run_training(
        base_model=args.base_model,
        model_dir=args.model_dir,
        data_path=args.data,
        max_steps=args.max_steps,
        eval_steps=args.eval_steps,
        lr=args.lr,
    )


if __name__ == "__main__":
    main()