import random

import pytest
import torch

import transformers

from petals import AutoDistributedModelForCausalLM
from petals import AutoDistributedConfig, RemoteSequential
from petals.server.block_functions import MAX_SHORT_INFERENCE_TOKENS
from petals.server.from_pretrained import load_pretrained_block
from test_utils import *


@pytest.fixture
def tokenizer():
    # We set use_fast=False since LlamaTokenizerFast is slow on load
    return transformers.AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)


@pytest.fixture
def model():
    return AutoDistributedModelForCausalLM.from_pretrained(
        MODEL_NAME, initial_peers=INITIAL_PEERS, torch_dtype=torch.float32
    )

@pytest.fixture
def model2():
    return transformers.AutoModelForCausalLM.from_pretrained(
        REF_NAME, low_cpu_mem_usage=True, torch_dtype=torch.float32
    )

@pytest.fixture
def ref_model():
    return transformers.AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, low_cpu_mem_usage=True, torch_dtype=torch.float32
    )

# @pytest.mark.forked
# def test_remote_block_with_cache_invalidation_exact_match(atol_forward=1e-4, atol_inference=1e-3):
#     config = AutoDistributedConfig.from_pretrained(MODEL_NAME, initial_peers=INITIAL_PEERS)
#     remote_sequential = RemoteSequential(config)

#     block_index = random.randint(0, config.num_hidden_layers - 1)
#     remote_block = remote_sequential[block_index]

#     inputs = torch.randn(1, MAX_SHORT_INFERENCE_TOKENS - 50, config.hidden_size)
#     short_inputs = torch.randn(1, MAX_SHORT_INFERENCE_TOKENS - 50, config.hidden_size)
#     short_inputs[:, :2, :] = inputs[:, :2, :]

#     initial_outputs_inference = None
#     secondary_outputs_inference = None
#     with torch.inference_mode():
#         with remote_block.inference_session(max_length=inputs.shape[1]) as sess:
#             initial_outputs_inference = sess.step(inputs)
#             secondary_outputs_inference = sess.step(short_inputs[:, 2:, :], start_from_position=2)
#             result = torch.cat([initial_outputs_inference[:, :2, :], secondary_outputs_inference], dim=1)

#     ref_block = load_pretrained_block(MODEL_NAME, block_index, torch_dtype=torch.float32)
#     (outputs_local,) = ref_block(short_inputs)

#     assert torch.allclose(outputs_local, result, rtol=0, atol=atol_inference)

# @pytest.mark.forked
# def test_speculative_greedy_generation(tokenizer, model, ref_model, max_new_tokens=4):
#     inputs = tokenizer("A cat sat on a mat", return_tensors="pt")["input_ids"]

#     options = dict(max_new_tokens=max_new_tokens, do_sample=False)
#     outputs = model.generate(inputs, **options)
#     print("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@", outputs.shape, outputs)
#     ref_outputs = ref_model.generate(inputs, **options)
#     assert torch.allclose(
#         outputs, ref_outputs
#     ), f"Greedy generation is not identical to HF with {multiple_calls=}, {inputs.shape=}"

@pytest.mark.forked
def test_speculative_greedy_generation(tokenizer, model, model2, ref_model, max_new_tokens=50, batch_size=10):    
    inputs = tokenizer("A cat sat on a mat", return_tensors="pt")["input_ids"]
    generated_ids = inputs

    with torch.no_grad():
        while generated_ids.shape[1] < max_new_tokens + inputs.shape[1]:
            outputs2 = model2.generate(generated_ids, max_new_tokens=batch_size, do_sample=False)
            new_tokens = outputs2[:, -batch_size:]

            random_pos = random.randrange(1, batch_size)
            new_tokens[:, random_pos] = random.randrange(1, 100)

            combined_ids = torch.cat((generated_ids, new_tokens), dim=1)
            logits = model(combined_ids, start_from_position=1).logits

            # Найти первую позицию, где токены совпали
            match_length = 0
            for i in range(batch_size):
                top_predicted_id_model2 = new_tokens[:, i]
                top_predicted_id_model = torch.argmax(logits[:, generated_ids.shape[1] + i - 1, :], dim=-1)
                
                if top_predicted_id_model2 == top_predicted_id_model:
                    match_length += 1
                else:
                    break
            print(f"Принято {match_length} из {batch_size}")

            if match_length > 0:
                generated_ids = torch.cat((generated_ids, new_tokens[:, :match_length]), dim=1)
                print(f"Всего {generated_ids.shape[1]}")
            else:
                break
        
        ref_outputs = ref_model.generate(inputs, max_new_tokens=max_new_tokens, do_sample=False)
        
    gen_text = tokenizer.decode(generated_ids[0], skip_special_tokens=True)
    ref_text = tokenizer.decode(ref_outputs[0], skip_special_tokens=True)

    print(f"Generated by speculative decoding: {gen_text}")
    print(f"Reference generation: {ref_text}")

    assert gen_text == ref_text, "The outputs do not match!"