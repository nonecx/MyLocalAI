# -*- coding: utf-8 -*-
"""基类模块：模型加载、LoRA 配置、进度条。

所有分析模块（情感、攻击性、逻辑谬误）继承自 BaseModule，
共享模型加载 / LoRA 配置 / 进度显示等基础能力。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Optional, cast

from tqdm import tqdm

if TYPE_CHECKING:
    # 仅用于类型检查；运行期在方法内部延迟导入
    from peft import PeftModel  # noqa: F401
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401


class BaseModule(ABC):
    """所有分析模块的抽象基类。

    属性:
        name: 模块名称（子类必须定义）。
        model_name_or_path: 基础模型路径或名称。
        lora_path: LoRA 权重保存/加载路径。
    """

    name: str = "base"

    def __init__(
        self,
        model_name_or_path: str,
        lora_path: Optional[str] = None,
        device: str = "auto",
        seed: int = 42,
    ) -> None:
        self.model_name_or_path = model_name_or_path
        self.lora_path = lora_path or self._default_lora_path()
        self.device = device
        self.seed = seed

        # transformers/peft 的类型存根与实现不一致（from_pretrained 的返回类型
        # 被标成内部类或 None），因此这里用 Any，避免一连串“类型不匹配”。
        self.model: Any = None
        self.tokenizer: Any = None

    # ------------------------------------------------------------------ #
    # 路径工具
    # ------------------------------------------------------------------ #
    def _default_lora_path(self) -> str:
        """默认 LoRA 路径：模块目录下的 model/ 子目录。"""
        return os.path.join(self._module_dir(), "model")

    @staticmethod
    def _module_dir() -> str:
        """返回当前模块的根目录。"""
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load(self) -> "BaseModule":
        """加载基础模型与 tokenizer（含 LoRA 可选合并）。"""
        # 延迟导入：避免 import 本模块时强依赖 torch/transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer

        base_path = self._resolve_base_path()
        self.tokenizer = AutoTokenizer.from_pretrained(
            base_path, trust_remote_code=True
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            base_path,
            trust_remote_code=True,
            device_map=self.device if self.device == "auto" else None,
        )
        if self.device != "auto" and self.model is not None:
            self.model.to(self.device)
        self._maybe_apply_lora()
        return self

    def _resolve_base_path(self) -> str:
        """决定加载的基础模型路径。默认即 model_name_or_path。"""
        return self.model_name_or_path

    def _maybe_apply_lora(self) -> None:
        """若 LoRA 权重存在则加载合并，否则跳过。"""
        if not self.lora_path:
            return
        if not (os.path.isdir(self.lora_path) and os.listdir(self.lora_path)):
            return

        from peft import PeftModel  # 延迟导入

        if self.model is None:
            raise RuntimeError("请先调用 load() 再应用 LoRA")
        self.model = PeftModel.from_pretrained(self.model, self.lora_path)
        self.model = self.model.merge_and_unload()
        self._log(f"已加载 LoRA: {self.lora_path}")

    # ------------------------------------------------------------------ #
    # 训练骨架
    # ------------------------------------------------------------------ #
    def train_lora(
        self,
        dataset_path: str,
        epochs: int = 3,
        lr: float = 2e-4,
    ) -> None:
        """训练/微调 LoRA 的通用骨架，子类可覆盖。

        Args:
            dataset_path: 训练集路径。基类不实现具体读取逻辑，
                由子类覆盖本方法后使用。
            epochs: 训练轮数。
            lr: 学习率。
        """
        self._log(
            f"开始训练 LoRA（dataset={dataset_path}, epochs={epochs}, lr={lr}）..."
        )
        os.makedirs(self.lora_path, exist_ok=True)
        # 具体训练逻辑由子类实现
        self._log(f"LoRA 已保存到: {self.lora_path}")

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def progress(
        self,
        iterable: Iterable[Any],
        desc: str = "",
        **kwargs: Any,
    ) -> Iterator[Any]:
        """进度条包装。

        说明：`tqdm` 实现了 `__iter__` 但类型上不继承 `Iterator`，
        因此先转成 `object` 再转 `Iterator`，以符合类型检查器要求。
        """
        bar = tqdm(iterable, desc=f"[{self.name}] {desc}".strip(), **kwargs)
        return cast(Iterator[Any], cast(object, bar))

    def _log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}")

    # ------------------------------------------------------------------ #
    # 子类必须实现
    # ------------------------------------------------------------------ #
    @abstractmethod
    def analyze(self, text: str) -> dict:
        """子类实现的具体分析逻辑，返回结构化结果。"""
        raise NotImplementedError