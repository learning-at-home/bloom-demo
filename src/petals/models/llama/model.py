from typing import List, Optional, Tuple, Union

import hivemind
import torch
import torch.nn as nn
from hivemind.utils.logging import get_logger
from transformers.modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast
from transformers.models.llama import LlamaForCausalLM, LlamaForSequenceClassification, LlamaModel, LlamaPreTrainedModel

from petals.client.from_pretrained import FromPretrainedMixin
from petals.client.inference_session import InferenceSession
from petals.client.lm_head import LMHead
from petals.client.ptune import PTuneMixin
from petals.client.remote_generation import RemoteGenerationMixin, RemotePastKeyValues
from petals.client.remote_sequential import RemoteSequential
from petals.models.llama.config import DistributedLlamaConfig

logger = get_logger(__name__)


class DistributedLlamaModel(FromPretrainedMixin, PTuneMixin, LlamaModel):
    """LlamaModel, but all transformer layers are hosted by the swarm"""

    _keys_to_ignore_on_load_missing = PTuneMixin._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = [r"^model\.layers\."]

    config_class = DistributedLlamaConfig

    def __init__(self, config: DistributedLlamaConfig, *, dht: Optional[hivemind.DHT] = None):
        n_layer, config.num_hidden_layers = config.num_hidden_layers, 0  # Prevent initialization
        super().__init__(config)
        assert len(self.layers) == 0
        config.num_hidden_layers = n_layer

        self.layers = RemoteSequential(config, dht=dht)

        self.requires_grad_(False)  # Forbid accumulate grads for embeddings and layernorm
        self.init_prompts(config)

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[RemotePastKeyValues] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> BaseModelOutputWithPast:
        # FIXME: Assert that the mask is None or triangle and position_ids are valid
        # assert attention_mask is None, f"{self.__class__.__name__} does not support attention masks right now"
        logger.warning(f"forward: {input_ids=} {self.layers.active_session=}")

        assert use_cache is None or use_cache, "use_cache=False is not supported"
        assert not output_attentions, "output_attentions=True is not supported"
        assert not output_hidden_states, "output_hidden_states=True is not supported"
        assert return_dict is None or return_dict, "return_dict=True is not supported"

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        if (
            self.config.tuning_mode
            and "ptune" in self.config.tuning_mode
            and (self.layers.active_session is None or self.layers.active_session.position == 0)
        ):
            batch_size = inputs_embeds.shape[0]
            prompts, intermediate_prompts = self.get_prompt(batch_size)
            inputs_embeds = torch.cat([prompts, inputs_embeds], dim=1)
        else:
            prompts = intermediate_prompts = None

        hidden_states = inputs_embeds
        output_shape = input_shape + (hidden_states.size(-1),)

        hidden_states = self.layers(hidden_states, prompts=intermediate_prompts, hypo_ids=past_key_values.hypo_ids if past_key_values is not None else None)

        # Remove prefix
        if self.config.tuning_mode and "ptune" in self.config.tuning_mode:
            hidden_states = hidden_states[:, self.pre_seq_len :]

        # Add last hidden state
        hidden_states = self.norm(hidden_states)
        hidden_states = hidden_states.view(output_shape)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=RemotePastKeyValues(),
            hidden_states=None,
            attentions=None,
        )

    @property
    def word_embeddings(self) -> nn.Embedding:  # For compatibility with RemoteGenerationMixin
        return self.embed_tokens

    @property
    def word_embeddings_layernorm(self) -> nn.Module:  # For compatibility with RemoteGenerationMixin
        return nn.Identity()

    @property
    def h(self) -> RemoteSequential:  # For compatibility with RemoteGenerationMixin
        return self.layers

    @property
    def ln_f(self) -> nn.Module:  # For compatibility with RemoteGenerationMixin
        return self.norm


class DistributedLlamaForCausalLM(FromPretrainedMixin, RemoteGenerationMixin, LlamaForCausalLM):
    _keys_to_ignore_on_load_missing = DistributedLlamaModel._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = DistributedLlamaModel._keys_to_ignore_on_load_unexpected

    config_class = DistributedLlamaConfig

    def __init__(self, config: DistributedLlamaConfig):
        LlamaPreTrainedModel.__init__(self, config)
        self.model = DistributedLlamaModel(config)
        self.pretraining_tp = config.pretraining_tp
        self.vocab_size = config.vocab_size
        self.lm_head = LMHead(config)

        # Initialize weights and apply final processing
        self.post_init()

    def get_output_embeddings(self):
        return self.lm_head

    @property
    def transformer(self) -> DistributedLlamaModel:  # For compatibility with RemoteGenerationMixin
        return self.model


class DistributedLlamaForSequenceClassification(FromPretrainedMixin, LlamaForSequenceClassification):
    _keys_to_ignore_on_load_missing = DistributedLlamaModel._keys_to_ignore_on_load_missing
    _keys_to_ignore_on_load_unexpected = DistributedLlamaModel._keys_to_ignore_on_load_unexpected

    config_class = DistributedLlamaConfig

    def __init__(self, config):
        LlamaPreTrainedModel.__init__(self, config)
        self.num_labels = config.num_labels

        self.model = DistributedLlamaModel(config)
        self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    @property
    def transformer(self) -> DistributedLlamaModel:  # For compatibility with RemoteGenerationMixin
        return self.model
