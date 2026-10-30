# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""
逻辑模块：数据加载与 8:2 对齐划分器。

职责：
  1. 读取当前文件夹下 data/input.txt 与 data/output.txt。
  2. 按行读取，去除每行末尾的换行符。
  3. 严格校验两个文件行数一致，否则抛出 ValueError 并打印行数差异。
  4. 构建 HuggingFace Dataset：{"input_text": "...", "target_text": "..."}。
  5. 使用 dataset.train_test_split(test_size=0.2, seed=42) 按 8:2 随机划分。
  6. 将 input 包装成 Prompt，output 作为标签。

可独立运行测试：
    python module_logic/data_utils.py
"""

from __future__ import annotations

import os

from datasets import Dataset

# ---------------------------------------------------------------------------
# 变量初始化（禁止绝对路径，全部基于当前文件目录拼接）
# ---------------------------------------------------------------------------
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
INPUT_PATH = os.path.join(DATA_DIR, "input.txt")
OUTPUT_PATH = os.path.join(DATA_DIR, "output.txt")

# Prompt 模板
PROMPT_TEMPLATE = "请分析以下文本的逻辑关联性和谬误，提取论据和结论：\n{input_text}"

# 固定划分比例与随机种子
TEST_SIZE = 0.2
SEED = 42


def read_lines(file_path: str) -> list:
    """
    按行读取文本文件，去除每行末尾的换行符，跳过纯空行。

    参数:
        file_path: 文本文件路径。

    返回:
        去除首尾空白后的行列表。

    异常:
        FileNotFoundError: 文件不存在。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"数据文件不存在: {file_path}")

    lines = []
    with open(file_path, "r", encoding="utf-8") as fh:
        for raw in fh.readlines():
            line = raw.strip()
            if line:  # 跳过纯空行
                lines.append(line)
    return lines


def load_aligned_data(
    input_path: str = INPUT_PATH,
    output_path: str = OUTPUT_PATH,
) -> Dataset:
    """
    加载 input.txt 与 output.txt 并构建对齐后的 HuggingFace Dataset。

    参数:
        input_path:  问题集路径。
        output_path: 标准答案（结论与论据）路径。

    返回:
        包含 "input_text" 与 "target_text" 两列的 Dataset。

    异常:
        ValueError: 两文件行数不一致。
    """
    inputs = read_lines(input_path)
    outputs = read_lines(output_path)

    # 严格校验行数一致
    if len(inputs) != len(outputs):
        raise ValueError(
            f"input.txt 与 output.txt 行数不一致："
            f"input 有 {len(inputs)} 行，output 有 {len(outputs)} 行。"
        )

    records = [
        {"input_text": inp, "target_text": out}
        for inp, out in zip(inputs, outputs)
    ]
    return Dataset.from_list(records)


def split_dataset(dataset: Dataset) -> tuple:
    """
    按 8:2 随机划分训练集与验证集（固定 seed=42）。

    参数:
        dataset: 待划分的完整数据集。

    返回:
        (train_dataset, test_dataset)。
    """
    split = dataset.train_test_split(test_size=TEST_SIZE, seed=SEED)
    return split["train"], split["test"]


def format_prompt(input_text: str) -> str:
    """
    将原始输入包装成模型 Prompt。

    参数:
        input_text: 网络言论原文。

    返回:
        拼接后的 Prompt 字符串。
    """
    return PROMPT_TEMPLATE.format(input_text=input_text)


def build_datasets() -> tuple:
    """
    便捷封装：加载 -> 校验 -> 划分 -> 格式化。

    返回:
        (train_formatted, test_formatted)，
        其中每个元素是含 "prompt" 与 "target_text" 两列的 Dataset。
    """
    dataset = load_aligned_data()
    train_ds, test_ds = split_dataset(dataset)

    def _format(item):
        return {
            "prompt": format_prompt(item["input_text"]),
            "target_text": item["target_text"],
        }

    train_formatted = train_ds.map(_format)
    test_formatted = test_ds.map(_format)
    return train_formatted, test_formatted


def main() -> None:
    """命令行测试入口：加载、校验、划分并打印统计信息。"""
    print(f"输入文件: {INPUT_PATH}")
    print(f"输出文件: {OUTPUT_PATH}")

    dataset = load_aligned_data()
    print(f"加载样本总数: {len(dataset)}")
    print(f"字段: {list(dataset.features)}")

    train_ds, test_ds = split_dataset(dataset)
    print(f"训练集: {len(train_ds)} / 验证集: {len(test_ds)}")

    # 打印一条格式化后的 Prompt 示例
    sample = train_ds[0]
    print("\n===== Prompt 示例 =====")
    print(format_prompt(sample["input_text"]))
    print("\n===== 标签示例 =====")
    print(sample["target_text"])


if __name__ == "__main__":
    main()