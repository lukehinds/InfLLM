import torch
from ..attention import RotaryEmbeddingESM, ATTN_FORWRAD

def huggingface_forward(forward):
    def hf_forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask = None,
        position_ids = None,
        past_key_value = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        **kwargs,
    ):
        assert not output_attentions
        
        # Get the correct attribute names based on model type
        if hasattr(self, 'num_heads'):
            # LLaMA style
            num_heads = self.num_heads
            num_key_value_heads = self.num_key_value_heads
            head_dim = self.head_dim
        else:
            # Mistral style - get from config
            config = self.q_proj.weight.shape[0] // self.head_dim
            num_heads = config
            num_key_value_heads = config // self.num_key_value_groups
            head_dim = self.head_dim
            
        # Get position bias by traversing up to find model
        position_bias = None
        
        # Try to find position_bias in the module hierarchy
        current_module = self
        while current_module is not None:
            if hasattr(current_module, 'position_bias'):
                position_bias = current_module.position_bias
                break
            if hasattr(current_module, 'model') and hasattr(current_module.model, 'position_bias'):
                position_bias = current_module.model.position_bias
                break
            # Move up to parent module if possible
            if hasattr(current_module, '_modules'):
                # Try to find parent module
                parent = None
                for name, mod in current_module._modules.items():
                    if mod is current_module:
                        parent = current_module._modules[name]
                        break
                current_module = parent
            else:
                current_module = None
            
        if position_bias is None:
            raise ValueError("Could not find position_bias in model hierarchy")
            
        ret = forward(
            self, hidden_states, hidden_states,
            position_bias, use_cache, past_key_value,  # Use position_bias instead of position_ids
            self.q_proj, self.k_proj, self.v_proj, self.o_proj, 
            head_dim, num_heads, num_key_value_heads
        )
        
        if use_cache:
            o, pkv = ret
            return (o, pkv)  # Return tuple for Mistral's unpacking
        else:
            return ret, None  # Return tuple for Mistral's unpacking

    return hf_forward


def patch_hf(
    model,
    attn_type: str = "inf_llm",
    attn_kwargs: dict = {},
    base = None, 
    distance_scale = None,
    **kwargs
):
    attn_kwargs.update(kwargs)
    # This approach lacks scalability and will be refactored.
    from transformers import LlamaForCausalLM, MistralForCausalLM, Qwen2ForCausalLM
    from transformers.models.llama.modeling_llama import LlamaAttention, LlamaModel, BaseModelOutputWithPast
    from transformers.models.mistral.modeling_mistral import MistralAttention, MistralModel
    from transformers.models.qwen2.modeling_qwen2 import Qwen2Attention, Qwen2Model

    def model_forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask = None,
        position_ids = None,
        past_key_values = None,
        inputs_embeds = None,
        use_cache = None,
        output_attentions = None,
        output_hidden_states = None,
        return_dict = None,
        *args,
        **kwargs
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)
            if hasattr(self, "config") and hasattr(self.config, "scale_emb"):
                inputs_embeds = inputs_embeds * self.config.scale_emb

        hidden_states = inputs_embeds  # Set hidden_states from inputs_embeds
        present_key_values = [] if use_cache else None

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None

        for idx, decoder_layer in enumerate(self.layers):
            past_key_value = past_key_values[idx] if past_key_values is not None else None

            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_value,
                output_attentions=output_attentions,
                use_cache=use_cache,
            )

            # Handle layer outputs based on what was returned
            if isinstance(layer_outputs, tuple):
                hidden_states = layer_outputs[0]
                if use_cache:
                    present_key_values.append(layer_outputs[1])
            else:
                hidden_states = layer_outputs

            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            if output_attentions:
                all_self_attentions += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, present_key_values, all_hidden_states, all_self_attentions] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=present_key_values,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )

    forward = huggingface_forward(ATTN_FORWRAD[attn_type](**attn_kwargs))

    if isinstance(model, LlamaForCausalLM):
        Attention = model.model.layers[0].self_attn.__class__
        Model = model.model.__class__
        rope_attr = 'rotary_emb'
        hf_rope = getattr(model.model.layers[0].self_attn, rope_attr)
    elif isinstance(model, MistralForCausalLM):
        Attention = model.model.layers[0].self_attn.__class__
        Model = model.model.__class__
        # Mistral stores rope parameters directly in config
        rope_base = model.config.rope_theta
        rope_dim = model.config.hidden_size // model.config.num_attention_heads
        hf_rope = None
    elif isinstance(model, Qwen2ForCausalLM):
        Attention = model.model.layers[0].self_attn.__class__
        Model = model.model.__class__
        rope_attr = 'rotary_emb'
        hf_rope = getattr(model.model.layers[0].self_attn, rope_attr)
    elif model.__class__.__name__ == "MiniCPMForCausalLM":
        Attention = model.model.layers[0].self_attn.__class__
        Model = model.model.__class__
        rope_attr = 'rotary_emb'
        hf_rope = getattr(model.model.layers[0].self_attn, rope_attr)
    else:
        raise ValueError("Only supports llama, mistral and qwen2 models.")

    if hf_rope is not None:
        if hasattr(hf_rope, 'base'):
            rope_base = hf_rope.base
        elif hasattr(hf_rope, '_rope_scaling_factor'):
            rope_base = 10000 * hf_rope._rope_scaling_factor
        else:
            rope_base = 10000
            
        if hasattr(hf_rope, 'dim'):
            rope_dim = hf_rope.dim
        elif hasattr(hf_rope, 'rotary_dim'):
            rope_dim = hf_rope.rotary_dim
        else:
            rope_dim = model.config.hidden_size // model.config.num_attention_heads

    base = base if base is not None else rope_base
    distance_scale = distance_scale if distance_scale is not None else 1.0
    
    rope = RotaryEmbeddingESM(
        rope_dim,
        base,
        distance_scale
    )
    model.model.position_bias = rope

    def set_forward(m):
        if isinstance(m, Attention):
            m._old_forward = m.forward
            m.forward = forward.__get__(m, Attention)

    model.apply(set_forward)

    model.model._old_forward = model.model.forward
    model.model.forward = model_forward.__get__(model.model, Model)

    return model

