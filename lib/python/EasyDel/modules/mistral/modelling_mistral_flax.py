import functools
import typing

import flax.core
from jax import jit, random, grad, numpy as jnp, Array
from jax.sharding import PartitionSpec as PS
import jax
from flax import linen as nn
from flax.traverse_util import unflatten_dict, flatten_dict
from flax.core import freeze, unfreeze
from typing import Union, Optional, Tuple, Dict, List
from transformers import PretrainedConfig, FlaxPreTrainedModel
from flax.linen import partitioning as nn_partitioning
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput

from ..flax_modelling_utils import ACT2FN, with_sharding_constraint, get_gradient_checkpoint_policy


class MistralConfig(PretrainedConfig):
    def __init__(
            self,
            vocab_size=32000,
            hidden_size=4096,
            intermediate_size=14336,
            num_hidden_layers=32,
            num_attention_heads=32,
            num_key_value_heads=8,
            hidden_act="silu",
            max_position_embeddings=4096 * 32,
            initializer_range=0.02,
            rms_norm_eps=1e-6,
            use_cache=True,
            pad_token_id=None,
            bos_token_id=1,
            eos_token_id=2,
            tie_word_embeddings=False,
            rope_theta=10000.0,
            sliding_window=4096,
            gradient_checkpointing: str = 'nothing_saveable',
            use_pjit_attention_force: bool = True,
            use_flash_attention: bool = False,
            use_sacn_mlp: bool = False,
            flash_attn_query_chunk_size: int = 1024,
            flash_attn_key_chunk_size: int = 1024,
            scan_mlp_chunk_size: int = 1024,
            number_rep_kv: int = 1,
            attn_pdrop: float = 0.0,
            c_max_position_embeddings: int = 4096,
            **kwargs,
    ):
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.sliding_window = sliding_window

        # for backward compatibility
        if num_key_value_heads is None:
            num_key_value_heads = num_attention_heads

        self.num_key_value_heads = num_key_value_heads
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.rms_norm_eps = rms_norm_eps
        self.use_cache = use_cache
        self.rope_theta = rope_theta
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings
        super().__init__(
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

    @staticmethod
    def get_partition_rules(fully_fsdp: bool = True):
        return (

            ("model/embed_tokens/embedding", PS("dp", "fsdp")),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS("fsdp", "dp")),
            ("self_attn/o_proj/kernel", PS("dp", "fsdp")),

            ("mlp/gate_proj/kernel", PS("fsdp", "dp")),
            ("mlp/down_proj/kernel", PS("dp", "fsdp")),
            ("mlp/up_proj/kernel", PS("fsdp", "dp")),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS("fsdp", "dp")),
            ('.*', PS(None)),
        ) if not fully_fsdp else (

            ("model/embed_tokens/embedding", PS("fsdp")),

            ("self_attn/(q_proj|k_proj|v_proj)/kernel", PS("fsdp")),
            ("self_attn/o_proj/kernel", PS("fsdp")),

            ("mlp/gate_proj/kernel", PS("fsdp")),
            ("mlp/down_proj/kernel", PS("fsdp")),
            ("mlp/up_proj/kernel", PS("fsdp")),

            ("input_layernorm/kernel", PS(None)),
            ("post_attention_layernorm/kernel", PS(None)),

            ("model/norm/kernel", PS(None)),
            ("lm_head/kernel", PS("fsdp")),
            ('.*', PS('fsdp')),
        )

    def add_jax_args(self,
                     gradient_checkpointing: str = 'nothing_saveable',
                     use_pjit_attention_force: bool = True,
                     use_flash_attention: bool = False,
                     use_sacn_mlp: bool = False,
                     flash_attn_query_chunk_size: int = 1024,
                     flash_attn_key_chunk_size: int = 1024,
                     scan_mlp_chunk_size: int = 1024,
                     number_rep_kv: int = 1,
                     attn_pdrop: float = 0.0,
                     c_max_position_embeddings: int = 4096
                     ):
        self.use_flash_attention = use_flash_attention
        self.number_rep_kv = number_rep_kv
        self.gradient_checkpointing = gradient_checkpointing
        self.use_pjit_attention_force = use_pjit_attention_force
        self.use_sacn_mlp = use_sacn_mlp
        self.flash_attn_query_chunk_size = flash_attn_query_chunk_size
        self.flash_attn_key_chunk_size = flash_attn_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.attn_pdrop = attn_pdrop
        self.c_max_position_embeddings = c_max_position_embeddings

    @staticmethod
    def get_weight_decay_exclusions():
        return tuple()

    @staticmethod
    def rng_keys():
        return ('params', 'dropout', 'fcm')


remat = nn_partitioning.remat


def repeat_kv(x: jax.Array, n_rep: int) -> jax.Array:
    bs, s, n_kv_heads, head_dim = x.shape
    if n_rep == 1:
        return x
    x = x[:, :, jnp.newaxis, :, :]
    x = jnp.repeat(x, n_rep, axis=2)

    return x.reshape(bs, s,
                     n_kv_heads * n_rep,
                     head_dim)


class MistralRMSNorm(nn.Module):
    dim: int
    eps: float = 1e-6
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16

    def setup(self) -> None:
        self.weight = self.param(
            'kernel',
            nn.initializers.ones,
            (self.dim,),
            self.param_dtype,
        )

    def _norm(self, x: jnp.ndarray) -> jnp.ndarray:
        return x * jax.lax.rsqrt(jnp.square(x).mean(-1, keepdims=True) + self.eps)

    def __call__(self, x: jnp.ndarray) -> jnp.ndarray:
        x = x.astype(jnp.promote_types(self.dtype, jnp.float32))
        output = self._norm(x).astype(self.dtype)
        weight = jnp.asarray(self.weight, self.dtype)
        return output * weight


def precompute_freq_cis(
        method: Union[str, None],
        dim: int, end: int, theta: float = 10000.0,
        scaling_factor: float = 8., **kwargs) -> jnp.ndarray:
    freq = 1.0 / (theta ** (jnp.arange(0, dim, 2)[: (dim // 2)].astype(jnp.float32) / dim))
    t = jnp.arange(end)

    if method is not None:
        if method == 'linear':
            t = t / scaling_factor
        elif method == 'dynamic':
            base = theta * (
                    (scaling_factor * end / end) - (scaling_factor - 1)
            ) ** (dim / (dim - 2))
            freq = 1.0 / (base ** (jnp.arange(0, dim, 2) / dim))
        else:
            raise ValueError(f'unknown {method} method for precompute_freq_cis')

    freq = jnp.outer(t, freq).astype(jnp.float32)
    sin, cos = jnp.sin(freq).astype(jnp.float32), jnp.cos(freq).astype(jnp.float32)
    freq_cis = jnp.complex64(cos + 1j * sin)
    return jnp.asarray(freq_cis)


def apply_rotary_emb(
        xq: jnp.ndarray,
        xk: jnp.ndarray,
        freq_cis: jnp.ndarray,
        dtype: jnp.dtype = jnp.bfloat16,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    reshape_xq = xq.astype(jnp.float32).reshape(*xq.shape[:-1], -1, 2)
    reshape_xk = xk.astype(jnp.float32).reshape(*xk.shape[:-1], -1, 2)

    xq_ = jax.lax.complex(reshape_xq[..., 0], reshape_xq[..., 1])
    xk_ = jax.lax.complex(reshape_xk[..., 0], reshape_xk[..., 1])

    # add head dim
    freq_cis = jnp.reshape(freq_cis, (*freq_cis.shape[:2], 1, *freq_cis.shape[2:]))

    xq_out = xq_ * freq_cis
    xq_out = jnp.stack((jnp.real(xq_out), jnp.imag(xq_out)), axis=-1).reshape(*xq_out.shape[:-1], -1)

    xk_out = xk_ * freq_cis
    xk_out = jnp.stack((jnp.real(xk_out), jnp.imag(xk_out)), axis=-1).reshape(*xk_out.shape[:-1], -1)

    return xq_out.astype(dtype), xk_out.astype(dtype)


class FlaxMistralMLP(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal()
        )
        self.gate_proj = dense(self.config.intermediate_size)
        self.up_proj = dense(self.config.intermediate_size)
        self.down_proj = dense(self.config.hidden_size)
        self.act_fn = ACT2FN[self.config.hidden_act]

    def __call__(self, x: jax.Array):
        return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))


class FlaxMistralAttention(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        config = self.config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings

        dense = functools.partial(
            nn.Dense,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
            kernel_init=nn.initializers.normal()
        )

        self.q_proj = dense(self.num_heads * self.head_dim)
        self.k_proj = dense(self.num_key_value_heads * self.head_dim)
        self.v_proj = dense(self.num_key_value_heads * self.head_dim)
        self.o_proj = dense(self.hidden_size)

    @nn.compact
    def concatenate_to_cache_(self, q: jax.Array, k: jax.Array, v: jax.Array, attention_mask: jax.Array):
        is_cache_available = self.has_variable('cache', 'key')
        key_cache = self.variable('cache', 'key', jnp.zeros, k.shape, k.dtype)
        value_cache = self.variable('cache', 'value', jnp.zeros, k.shape, v.dtype)
        index_cache = self.variable('cache', 'index', lambda: jnp.array(0, dtype=jnp.int32))
        if is_cache_available:
            *bd, ml, nh, dph = key_cache.value.shape
            indices = (0,) * len(bd) + (index_cache.value, 0, 0)
            k = jax.lax.dynamic_update_slice(key_cache.value, k, indices)
            v = jax.lax.dynamic_update_slice(value_cache.value, v, indices)
            key_cache.value = k
            value_cache.value = v
            num_updated_cache_vector = q.shape[1]
            index_cache.value = index_cache.value + num_updated_cache_vector
            pad_mask = jnp.broadcast_to(
                jnp.arange(ml) < index_cache.value,
                tuple(bd) + (1, num_updated_cache_vector, ml)
            )
            attention_mask = nn.combine_masks(pad_mask, attention_mask)
        return q, k, v, attention_mask

    def __call__(
            self,
            hidden_state: jax.Array,
            freq_cis: jax.Array,
            attention_mask: jax.Array,
            causal_mask: jax.Array,
            position_ids: jax.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True
    ):
        batch_size, max_sequence_length = hidden_state.shape[:2]
        q, k, v = self.q_proj(hidden_state), self.k_proj(hidden_state), self.v_proj(hidden_state)

        if self.config.use_pjit_attention_force:
            q = with_sharding_constraint(q, PS('fsdp', 'mp', None))
            k = with_sharding_constraint(k, PS('fsdp', 'mp', None))
            v = with_sharding_constraint(v, PS('fsdp', 'mp', None))

        q = q.reshape(batch_size, max_sequence_length, -1, self.head_dim)
        k = k.reshape(batch_size, max_sequence_length, -1, self.head_dim)
        v = v.reshape(batch_size, max_sequence_length, -1, self.head_dim)

        freq_cis = jnp.take(freq_cis, position_ids, axis=0)
        q, k = apply_rotary_emb(q, k, freq_cis=freq_cis, dtype=self.dtype)
        k = repeat_kv(k, self.num_key_value_groups)
        v = repeat_kv(v, self.num_key_value_groups)
        if self.has_variable('cache', 'key') or init_cache:
            q, k, v, attention_mask = self.concatenate_to_cache_(q, k, v, attention_mask)

        q_l, k_l = q.shape[1], k.shape[1]

        if self.has_variable('cache', 'key'):
            mask_shift: int = self.variables['cache']['index']
            dl = self.variables['cache']['key'].shape[1]
            causal_mask = jax.lax.dynamic_slice(
                causal_mask, (0, 0, mask_shift, 0), (1, 1, q_l, dl)
            )
        else:
            causal_mask = causal_mask[:, :, :q_l, :k_l]

        causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
        attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
        attention_mask = nn.combine_masks(attention_mask, causal_mask)
        attention_bias = jax.lax.select(
            attention_mask > 0,
            jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
            jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
        )

        attn_weight = nn.dot_product_attention_weights(
            query=q,
            key=k,
            bias=attention_bias,
            dtype=jnp.promote_types(self.dtype, jnp.float32),
            deterministic=deterministic,
            dropout_rate=self.config.attn_pdrop,
            precision=self.precision
        )

        if self.config.use_pjit_attention_force:
            attn_weight = with_sharding_constraint(attn_weight, PS(("dp", "fsdp"), "mp", None, None))
        attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weight, v)
        attn_output = attn_output.reshape(attn_output.shape[:2] + (self.hidden_size,))
        out = self.o_proj(attn_output)
        outputs = (out, attn_output) if output_attentions else (out,)
        return outputs


class FlaxMistralDecoderLayer(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        self.self_attn = FlaxMistralAttention(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.mlp = FlaxMistralMLP(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.input_layernorm = MistralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.post_attention_layernorm = MistralRMSNorm(
            dim=self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )

    def __call__(
            self,
            hidden_state: jax.Array,
            freq_cis: jax.Array,
            attention_mask: jax.Array,
            causal_mask: jax.Array,
            position_ids: jax.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = True
    ):
        residual = hidden_state
        attention_output = self.self_attn(
            hidden_state=self.input_layernorm(hidden_state),
            freq_cis=freq_cis,
            attention_mask=attention_mask,
            causal_mask=causal_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions
        )
        hidden_state = attention_output[0] + residual

        hidden_state = self.mlp(self.post_attention_layernorm(hidden_state)) + hidden_state
        outputs = (hidden_state,)
        if output_attentions:
            outputs += attention_output[1]
        return outputs


class FlaxMistralPretrainedModel(FlaxPreTrainedModel):
    config_class = MistralConfig
    base_model_prefix = 'mistral'
    module_class: nn.Module = None

    def __init__(self,
                 config: MistralConfig,
                 input_shape: Tuple = (1, 1),
                 seed: int = 0,
                 dtype: jnp.dtype = jnp.bfloat16,
                 _do_init: bool = True,
                 **kwargs
                 ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype, _do_init=_do_init)

    def init_weights(
            self,
            rng: jax.random.PRNGKey,
            input_shape: Tuple,
            params: flax.core.FrozenDict = None
    ) -> flax.core.FrozenDict:

        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape)
        params_rng, dropout_rng = jax.random.split(rng)
        rng_s = {"params": params_rng, "dropout": dropout_rng}

        if self.config.add_cross_attention:
            encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
            encoder_attention_mask = attention_mask
            module_init_outputs = self.module.init(
                rng_s,
                input_ids,
                attention_mask,
                position_ids,
                encoder_hidden_states,
                encoder_attention_mask,
                return_dict=False,
            )
        else:
            module_init_outputs = self.module.init(rng_s, input_ids, attention_mask, position_ids, return_dict=False)

        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def init_cache(self, batch_size, max_length):

        input_ids = jnp.ones((batch_size, max_length))
        attention_mask = jnp.ones_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, position_ids, return_dict=False, init_cache=True
        )
        return init_variables["cache"]

    def __call__(
            self,
            input_ids,
            attention_mask=None,
            position_ids=None,
            params: dict = None,
            past_key_values: dict = None,
            dropout_rng: jax.random.PRNGKey = None,
            train: bool = False,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            add_params_field: bool = False
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        if position_ids is None:
            if past_key_values is not None:
                raise ValueError("Make sure to provide `position_ids` when passing `past_key_values`.")

            position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))

        rng_s = {}
        if dropout_rng is not None:
            rng_s["dropout"] = dropout_rng

        inputs = {"params": params or self.params} if add_params_field else params or self.params

        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            jnp.array(input_ids, dtype="i4"),
            jnp.array(attention_mask, dtype="i4"),
            jnp.array(position_ids, dtype="i4"),
            not train,
            None,
            False,
            output_attentions,
            output_hidden_states,
            return_dict,
            rngs=rng_s,
            mutable=mutable,
        )

        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxMistralDecoratorCollection(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[None, jax.lax.Precision]] = jax.lax.Precision('fastest')

    def setup(self) -> None:
        block = FlaxMistralDecoderLayer
        if self.config.gradient_checkpointing != '':
            block = remat(
                block,
                static_argnums=(5, 6, 7),
                policy=get_gradient_checkpoint_policy(
                    self.config.gradient_checkpointing
                )
            )
        self.layers = [
            block(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                name=str(i)
            ) for i in range(self.config.num_hidden_layers)
        ]

    def __call__(
            self,
            hidden_state: jax.Array,
            freq_cis: jax.Array,
            attention_mask: jax.Array,
            causal_mask: jax.Array,
            position_ids: jax.Array,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        for layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_state,)
            # hidden_state: jax.Array,
            # freq_cis: jax.Array,
            # attention_mask: jax.Array,
            # causal_mask: jax.Array,
            # position_ids: jax.Array,
            # deterministic: bool = True,
            # init_cache: bool = False,
            # output_attentions: bool = True
            output = layer(
                hidden_state,
                freq_cis,
                attention_mask,
                causal_mask,
                position_ids,
                deterministic,
                init_cache,
                output_attentions
            )
            hidden_state = output[0]

            if output_attentions:
                output_attentions += (output[1],)

        return hidden_state, all_hidden_states, all_attentions


class FlaxMistralModule(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self):

        self.embed_tokens = nn.Embed(
            self.config.vocab_size,
            self.config.hidden_size,
            embedding_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )

        self.layers = FlaxMistralDecoratorCollection(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.norm = MistralRMSNorm(
            self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.freq_cis = precompute_freq_cis(
            method=None,
            scaling_factor=1.0,
            dim=self.config.hidden_size // self.config.num_attention_heads,
            end=self.config.max_position_embeddings * 2
        )
        self.causal_mask = nn.make_causal_mask(jnp.ones((1, self.config.c_max_position_embeddings), dtype='i4'))

    def __call__(
            self,
            input_ids: jax.Array,
            attention_mask: jax.Array,
            position_ids: jax.Array,
            deterministic: bool = True,
            input_embeds: jax.Array = None,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,

    ) -> typing.Union[Tuple[Array, ...], FlaxBaseModelOutput]:
        if input_embeds is None:
            input_embeds = self.embed_tokens(input_ids.astype("i4"))
        if attention_mask.ndim == 2:
            b, s = attention_mask.shape
            attention_mask = attention_mask.reshape(b, 1, 1, s)
        outputs = self.layers(
            hidden_state=input_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            freq_cis=self.freq_cis,
            init_cache=init_cache,
            output_attentions=output_attentions,
            deterministic=deterministic,
            causal_mask=self.causal_mask
        )

        hidden_states = outputs[0]
        hidden_states = self.norm(hidden_states)

        if output_hidden_states:
            all_hidden_states = outputs[1] + (hidden_states,)
            outputs = (hidden_states, all_hidden_states) + outputs[2:]
        else:
            outputs = (hidden_states,) + outputs[1:]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs[1],
            attentions=outputs[-1],
        )


class FlaxMistralModel(FlaxMistralPretrainedModel):
    module_class = FlaxMistralModule


class FlaxMistralForCausalLMModule(nn.Module):
    config: MistralConfig
    dtype: jnp.dtype = jnp.bfloat16
    param_dtype: jnp.dtype = jnp.bfloat16
    precision: Optional[Union[jax.lax.Precision, str]] = None

    def setup(self):
        self.model: FlaxMistralModule = FlaxMistralModule(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
        )
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            precision=self.precision,
        )

    def __call__(
            self,
            input_ids: jax.Array,
            attention_mask: jax.Array,
            position_ids: jax.Array,
            deterministic: bool = True,
            input_embeds: jax.Array = None,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
    ):
        batch_size, seq_length = input_ids.shape
        if attention_mask is None:
            attention_mask = jnp.ones_like(input_ids)
        if position_ids is None:
            position_ids = jnp.broadcast_to(
                jnp.clip(jnp.cumsum(attention_mask, axis=-1) - 1, a_min=0),
                (batch_size, seq_length)
            )
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            deterministic=deterministic,
            input_embeds=input_embeds,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_kernel = self.transformer.variables["params"]["wte"]["embedding"].T
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_kernel}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        # lm_logits = lm_logits.astype(jnp.float32)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxCausalLMOutput(logits=lm_logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions)


class FlaxMistralForCausalLM(FlaxMistralPretrainedModel):
    module_class = FlaxMistralForCausalLMModule

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[jax.Array] = None):
        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = jax.lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    @staticmethod
    def update_inputs_for_generation(model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs