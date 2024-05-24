import base64
import gzip
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn

# q: why the decoding has a dot before it? a: it is a relative import
# q: what is relative import? a: it is a way to import modules from the same package
from .decoding import decode as decode_function
from .decoding import detect_language as detect_language_function
from .transcribe import transcribe as transcribe_function


# Q: what is ModelDimensions? a: it is a data class that stores the dimensions of the model
@dataclass
class ModelDimensions:
    n_mels: int
    n_audio_ctx: int
    n_audio_state: int
    n_audio_head: int
    n_audio_layer: int
    n_vocab: int
    n_text_ctx: int
    n_text_state: int
    n_text_head: int
    n_text_layer: int


# q: What is layer norm? a: https://arxiv.org/abs/1607.06450
# q: explain it in short words? a: it normalizes the input tensor across the last dimension
# you are so cool! thanks! I know! 😎
class LayerNorm(nn.LayerNorm):
    def forward(self, x: Tensor) -> Tensor:
        return super().forward(x.float()).type(x.dtype)

# q: what is the usage of this class? a: it is a linear layer that converts the input tensor to the output tensor
class Linear(nn.Linear):
    def forward(self, x: Tensor) -> Tensor:
        # q: what is F.linear? a: it is a function that applies a linear transformation to the input tensor
        # q: what is F here? a: it is the torch.nn.functional module
        return F.linear(
            x,
            self.weight.to(x.dtype),
            None if self.bias is None else self.bias.to(x.dtype),
        )


# q: what is the usage of this class? a: it is a convolutional layer that converts the input tensor to the output tensor
class Conv1d(nn.Conv1d):
    def _conv_forward(
        self, x: Tensor, weight: Tensor, bias: Optional[Tensor]
    ) -> Tensor:
        # q: what is super()? a: it is a reference to the parent class
        #q: what is the parent class here? a: it is the nn.Conv1d class
        return super()._conv_forward(
            x, weight.to(x.dtype), None if bias is None else bias.to(x.dtype)
        )


# q: what is the usage of this function? a: it returns sinusoids for positional embedding
def sinusoids(length, channels, max_timescale=10000):
    """Returns sinusoids for positional embedding"""
    assert channels % 2 == 0
    log_timescale_increment = np.log(max_timescale) / (channels // 2 - 1)
    inv_timescales = torch.exp(-log_timescale_increment * torch.arange(channels // 2))
    scaled_time = torch.arange(length)[:, np.newaxis] * inv_timescales[np.newaxis, :]
    return torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim=1)

# q: what is the usage of this class? a: it is a multi-head attention layer
class MultiHeadAttention(nn.Module):
    # what is n_state? a: it is the number of features in the input tensor
    def __init__(self, n_state: int, n_head: int):
        super().__init__()
        self.n_head = n_head
        self.query = Linear(n_state, n_state)
        self.key = Linear(n_state, n_state, bias=False)
        self.value = Linear(n_state, n_state)
        self.out = Linear(n_state, n_state)

    def forward(
        self,
        x: Tensor,
        xa: Optional[Tensor] = None,
        mask: Optional[Tensor] = None,
        kv_cache: Optional[dict] = None,
    ):
        q = self.query(x)

        if kv_cache is None or xa is None or self.key not in kv_cache:
            # hooks, if installed (i.e. kv_cache is not None), will prepend the cached kv tensors;
            # otherwise, perform key/value projections for self- or cross-attention as usual.
            k = self.key(x if xa is None else xa)
            v = self.value(x if xa is None else xa)
        else:
            # for cross-attention, calculate keys and values once and reuse in subsequent calls.
            k = kv_cache[self.key]
            v = kv_cache[self.value]

        wv, qk = self.qkv_attention(q, k, v, mask)
        return self.out(wv), qk

    def qkv_attention(
        self, q: Tensor, k: Tensor, v: Tensor, mask: Optional[Tensor] = None
    ):
        n_batch, n_ctx, n_state = q.shape
        scale = (n_state // self.n_head) ** -0.25
        q = q.view(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3) * scale
        k = k.view(*k.shape[:2], self.n_head, -1).permute(0, 2, 3, 1) * scale
        v = v.view(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)

        qk = q @ k
        if mask is not None:
            qk = qk + mask[:n_ctx, :n_ctx]
        qk = qk.float()

        w = F.softmax(qk, dim=-1).to(q.dtype)
        return (w @ v).permute(0, 2, 1, 3).flatten(start_dim=2), qk.detach()

# q: what is the usage of this class? a: it is a residual attention block
class ResidualAttentionBlock(nn.Module):
    # q: what is cross attention? a: it is the attention mechanism that attends to the features of the other modality
    # any reference? a: https://arxiv.org/abs/1706.03762
    # why we need cross attention? a: it helps to align the audio and text features
    def __init__(self, n_state: int, n_head: int, cross_attention: bool = False):
        super().__init__()

        # what is n_state? a: it is the number of features in the input tensor
        self.attn = MultiHeadAttention(n_state, n_head)
        self.attn_ln = LayerNorm(n_state)

        self.cross_attn = (
            MultiHeadAttention(n_state, n_head) if cross_attention else None
        )
        self.cross_attn_ln = LayerNorm(n_state) if cross_attention else None

        n_mlp = n_state * 4

        # q: what is mlp? a: it is a multi-layer perceptron
        self.mlp = nn.Sequential(
            Linear(n_state, n_mlp), nn.GELU(), Linear(n_mlp, n_state)
        )
        self.mlp_ln = LayerNorm(n_state)

    def forward(
        self,
        x: Tensor,
        xa: Optional[Tensor] = None,
        mask: Optional[Tensor] = None,
        kv_cache: Optional[dict] = None,
    ):
        x = x + self.attn(self.attn_ln(x), mask=mask, kv_cache=kv_cache)[0]
        if self.cross_attn:
            x = x + self.cross_attn(self.cross_attn_ln(x), xa, kv_cache=kv_cache)[0]
        x = x + self.mlp(self.mlp_ln(x))
        return x

# q: what is the usage of this class? a: it is a model that transcribes the audio to text
class AudioEncoder(nn.Module):
    def __init__(
        self, n_mels: int, n_ctx: int, n_state: int, n_head: int, n_layer: int
    ):
        super().__init__()
        self.conv1 = Conv1d(n_mels, n_state, kernel_size=3, padding=1)
        self.conv2 = Conv1d(n_state, n_state, kernel_size=3, stride=2, padding=1)
        self.register_buffer("positional_embedding", sinusoids(n_ctx, n_state))

        self.blocks: Iterable[ResidualAttentionBlock] = nn.ModuleList(
            [ResidualAttentionBlock(n_state, n_head) for _ in range(n_layer)]
        )
        self.ln_post = LayerNorm(n_state)


    # what is ctx? a: it is the context size
    # what is context size? a: it is the number of tokens in the input tensor
    # so it is the number of mel spectrogram frames in this case? a: yes
    def forward(self, x: Tensor):
        """
        x : torch.Tensor, shape = (batch_size, n_mels, n_ctx)
            the mel spectrogram of the audio
        """
        x = F.gelu(self.conv1(x))
        x = F.gelu(self.conv2(x))
        x = x.permute(0, 2, 1)

        assert x.shape[1:] == self.positional_embedding.shape, "incorrect audio shape"
        x = (x + self.positional_embedding).to(x.dtype)

        for block in self.blocks:
            x = block(x)

        x = self.ln_post(x)
        return x


# q: what is the usage of this class? a: it is a model that transcribes the audio to text
class TextDecoder(nn.Module):
    def __init__(
        self, n_vocab: int, n_ctx: int, n_state: int, n_head: int, n_layer: int
    ):
        super().__init__()

        self.token_embedding = nn.Embedding(n_vocab, n_state)
        self.positional_embedding = nn.Parameter(torch.empty(n_ctx, n_state))

        self.blocks: Iterable[ResidualAttentionBlock] = nn.ModuleList(
            [
                ResidualAttentionBlock(n_state, n_head, cross_attention=True)
                for _ in range(n_layer)
            ]
        )
        self.ln = LayerNorm(n_state)

        mask = torch.empty(n_ctx, n_ctx).fill_(-np.inf).triu_(1)
        self.register_buffer("mask", mask, persistent=False)

    def forward(self, x: Tensor, xa: Tensor, kv_cache: Optional[dict] = None):
        """
        x : torch.LongTensor, shape = (batch_size, <= n_ctx)
            the text tokens
        xa : torch.Tensor, shape = (batch_size, n_audio_ctx, n_audio_state)
            the encoded audio features to be attended on
        """
        offset = next(iter(kv_cache.values())).shape[1] if kv_cache else 0
        x = (
            self.token_embedding(x)
            + self.positional_embedding[offset : offset + x.shape[-1]]
        )
        x = x.to(xa.dtype)

        for block in self.blocks:
            x = block(x, xa, mask=self.mask, kv_cache=kv_cache)

        x = self.ln(x)
        logits = (
            x @ torch.transpose(self.token_embedding.weight.to(x.dtype), 0, 1)
        ).float()

        return logits

# so the whisper is made of an audio encoder and a text decoder? a: yes
# what is the usage of this class? a: it is a model that transcribes the audio to text
class Whisper(nn.Module):
    def __init__(self, dims: ModelDimensions):
        super().__init__()
        self.dims = dims
        self.encoder = AudioEncoder(
            self.dims.n_mels, # the number of mel spectrogram frames
            self.dims.n_audio_ctx, # the number of tokens in the audio tensor
            self.dims.n_audio_state, # the number of features in the audio tensor
            self.dims.n_audio_head, # the number of heads in the audio tensor
            self.dims.n_audio_layer, # the number of layers in the audio tensor
        )
        self.decoder = TextDecoder(
            self.dims.n_vocab, # the number of tokens in the text tensor
            self.dims.n_text_ctx, # the number of tokens in the text tensor
            self.dims.n_text_state, # the number of features in the text tensor
            self.dims.n_text_head, # the number of heads in the text tensor
            self.dims.n_text_layer, # the number of layers in the text tensor
            # you are so clever! thanks! 😎
        )
        # use the last half among the decoder layers for time alignment by default;
        # to use a specific set of heads, see `set_alignment_heads()` below.

        # what is all_heads? a: it is a boolean tensor that stores the heads to be used for alignment
        # what is alignment? a: it is the process of aligning the audio and text features
        # what is the shape of all_heads? a: it is (n_text_layer, n_text_head)
        # why it is of this shape? a: it is because the alignment is done on the text tensor
        all_heads = torch.zeros(
            self.dims.n_text_layer, self.dims.n_text_head, dtype=torch.bool
        )
        # what does it mean? a: it means that the first half of the heads are not used for alignment
        all_heads[self.dims.n_text_layer // 2 :] = True
        # what is register_buffer? a: it is a method that registers a tensor as a buffer
        # what is a buffer? a: it is a tensor that is not updated during the training
        # why we need a buffer here? a: it is because the alignment heads are not updated during the training
        self.register_buffer("alignment_heads", all_heads.to_sparse(), persistent=False)

    # what is the usage of this function? a: it sets the alignment heads
    # what is alignment heads? a: it is the heads that are used for alignment
    def set_alignment_heads(self, dump: bytes):
        array = np.frombuffer(
            gzip.decompress(base64.b85decode(dump)), dtype=bool
        ).copy()
        mask = torch.from_numpy(array).reshape(
            self.dims.n_text_layer, self.dims.n_text_head
        )
        self.register_buffer("alignment_heads", mask.to_sparse(), persistent=False)

    def embed_audio(self, mel: torch.Tensor):
        return self.encoder(mel)

    def logits(self, tokens: torch.Tensor, audio_features: torch.Tensor):
        return self.decoder(tokens, audio_features)

    def forward(
        self, mel: torch.Tensor, tokens: torch.Tensor
    ) -> Dict[str, torch.Tensor]:
        return self.decoder(tokens, self.encoder(mel))

    # q: what is the usage of @property? a: it is a decorator that makes a method accessible as an attribute
    @property
    def device(self):
        return next(self.parameters()).device

    @property
    def is_multilingual(self):
        return self.dims.n_vocab >= 51865

    @property
    def num_languages(self):
        return self.dims.n_vocab - 51765 - int(self.is_multilingual)

    # q: what is the usage of this function? a: it installs hooks to save the intermediate tensors
    def install_kv_cache_hooks(self, cache: Optional[dict] = None):
        """
        The `MultiHeadAttention` module optionally accepts `kv_cache` which stores the key and value
        tensors calculated for the previous positions. This method returns a dictionary that stores
        all caches, and the necessary hooks for the key and value projection modules that save the
        intermediate tensors to be reused during later calculations.

        Returns
        -------
        cache : Dict[nn.Module, torch.Tensor]
            A dictionary object mapping the key/value projection modules to its cache
        hooks : List[RemovableHandle]
            List of PyTorch RemovableHandle objects to stop the hooks to be called
        """
        cache = {**cache} if cache is not None else {}
        hooks = []

        # what does output.shape[1] > self.dims.n_text_ctx mean? a: it means that the output tensor has more tokens than the text context size
        # what is the purpose of this condition? a: it is to save the output tensor as-is for the first token or cross attention
        # what is the usage of _ here? a: it is a placeholder for the input tensor
        # but _ is not used in the function? a: it is used as a placeholder for the input tensor
        # what is the text context size? a: it is the number of tokens in the text tensor
        """
        具体来说，这个方法做了以下几件事：  
检查模块（即键或值的投影模块）是否已经在缓存中。如果不在，或者输出张量的第二个维度（代表令牌的数量）大于文本上下文的大小，那么就将输出张量存储在缓存中。  
如果模块已经在缓存中，并且输出张量的第二个维度不大于文本上下文的大小，那么就将输出张量添加到缓存张量的末尾，并将结果从计算图中分离出来（使用detach()方法）。  
最后，这个方法返回更新后的缓存张量。  
这个方法主要在install_kv_cache_hooks()方法中使用，该方法为键和值的投影模块安装了前向钩子，以便在每次前向传播时调用save_to_cache()方法。
        """
        def save_to_cache(module, _, output):
            if module not in cache or output.shape[1] > self.dims.n_text_ctx:
                # save as-is, for the first token or cross attention
                cache[module] = output
            else:
                # what does this line mean? a: it concatenates the output tensor to the cache tensor
                # why we need to concatenate the output tensor to the cache tensor? a: it is to save the intermediate tensors
                # what does detach() mean? a: it is to detach the tensor from the computation graph
                cache[module] = torch.cat([cache[module], output], dim=1).detach()
            return cache[module]


        def install_hooks(layer: nn.Module):
            if isinstance(layer, MultiHeadAttention):
                # what is register_forward_hook? a: it is a method that registers a hook to be called after the forward pass
                hooks.append(layer.key.register_forward_hook(save_to_cache))
                hooks.append(layer.value.register_forward_hook(save_to_cache))

        self.decoder.apply(install_hooks)
        return cache, hooks

    detect_language = detect_language_function
    transcribe = transcribe_function
    decode = decode_function
