from typing import Optional, Union

import torch
from accelerate import init_empty_weights
from transformers import PretrainedConfig


def resolve_block_dtype(config: PretrainedConfig, dtype: Union[str, torch.dtype]) -> torch.dtype:
    """If dtype is "auto", resolves it using BloomConfig. Returns `dtype` intact otherwise."""
    if dtype not in ("auto", None):
        return dtype
    if config.torch_dtype not in ("auto", None):
        return config.torch_dtype
    return torch.bfloat16


def get_block_size(
    config: PretrainedConfig,
    location: str,
    *,
    dtype: Optional[Union[str, torch.dtype]] = None,
    load_in_8bit: Optional[bool] = None,
    eps: float = 0.01,  # eps accounts for ~1% of metainfo for tensor descriptions, quantization tables, etc.
) -> int:
    if location == "memory":
        assert (
            dtype is not None and load_in_8bit is not None
        ), 'get_block_size(..., location="memory") requires to specify dtype and load_in_8bit for calculations'

    with init_empty_weights(include_buffers=True):
        block = config.block_class(config)
        n_params = sum(param.numel() for param in block.parameters())

    if location == "memory" and load_in_8bit:
        # Note: We may need a larger eps here for models of size < 1B
        return n_params * (1 + eps)

    if location == "memory":
        dtype = resolve_block_dtype(config, dtype)
    elif location == "disk":
        dtype = resolve_block_dtype(config, "auto")
    else:
        raise ValueError('get_block_size() expects location to be "memory" or "disk"')

    return round(n_params * torch.finfo(dtype).bits // 8 * (1 + eps))
