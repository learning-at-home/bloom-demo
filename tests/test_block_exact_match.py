import random
from typing import Union

import hivemind
import pytest
import torch
from transformers.models.bloom.configuration_bloom import BloomConfig

from petals.bloom.block import WrappedBloomBlock
from petals.bloom.from_pretrained import DTYPE_MAP, _load_state_dict, load_pretrained_block
from petals.client import DistributedBloomConfig
from petals.client.remote_sequential import RemoteTransformerBlock
from petals.data_structures import UID_DELIMITER
from petals.dht_utils import get_remote_module
from test_utils import *


@pytest.mark.forked
def test_remote_block_exact_match(atol_forward=1e-4, atol_inference=1e-3):
    dht = hivemind.DHT(initial_peers=INITIAL_PEERS, client_mode=True, start=True)
    config = DistributedBloomConfig.from_pretrained(MODEL_NAME)

    for block_index in random.sample(range(config.n_layer), 3):
        remote_block = get_remote_module(dht, f"{MODEL_NAME}{UID_DELIMITER}{block_index}", config)
        assert isinstance(remote_block, RemoteTransformerBlock)

        inputs = torch.randn(1, 8, config.hidden_size)
        outputs_forward = remote_block(inputs)

        outputs_inference = []
        with remote_block.inference_session(max_length=inputs.shape[1]) as sess:
            for i in range(inputs.shape[1]):
                outputs_inference.append(sess.step(inputs[:, i : i + 1, :]))

            # test that max length is respected
            with pytest.raises(ValueError, match=r"Maximum length exceeded") as exc_info:
                sess.step(inputs[:, -1:, :])
            assert "Maximum length exceeded" in repr(exc_info.value)

        outputs_inference = torch.cat(outputs_inference, dim=1)

        ref_block = load_pretrained_block(MODEL_NAME, block_index, torch_dtype=torch.float32)
        (outputs_local,) = ref_block(inputs)

        assert torch.allclose(outputs_local, outputs_forward, rtol=0, atol=atol_forward)
        assert torch.allclose(outputs_local, outputs_inference, rtol=0, atol=atol_inference)


def _old_load_pretrained_block(
    converted_model_name_or_path: str,
    block_index: int,
    torch_dtype: Union[torch.dtype, str] = "auto",
) -> WrappedBloomBlock:
    """Load the BLOOM block by directly initializing the weights."""

    config = BloomConfig.from_pretrained(converted_model_name_or_path)

    block = WrappedBloomBlock(config)
    state_dict = _load_state_dict(
        converted_model_name_or_path,
        block_index,
        config,
        cache_dir=None,
    )

    if torch_dtype == "auto":
        with torch.no_grad():
            for name, param in block.named_parameters():
                assert name in state_dict, f"{name} not in state dict"
                param.data = param.data.to(state_dict[name].dtype)
    else:
        assert torch_dtype in DTYPE_MAP.values(), f"torch_dtype must be one of {list(DTYPE_MAP.values())}"
        block = block.to(dtype=torch_dtype)

    return block


def test_init_pretrained_block(torch_dtype=torch.float32, atol_forward=1e-8):
    config = DistributedBloomConfig.from_pretrained(MODEL_NAME)
    inputs = torch.randn(1, 16, config.hidden_size, requires_grad=True, dtype=torch_dtype)

    block = load_pretrained_block(MODEL_NAME, 3, torch_dtype=torch_dtype)
    ref_block = load_pretrained_block(MODEL_NAME, 3, torch_dtype=torch_dtype)

    outputs = block.forward(inputs)[0]
    outputs_ref = ref_block.forward(inputs)[0]
    assert torch.allclose(outputs, outputs_ref, rtol=0, atol=atol_forward)
