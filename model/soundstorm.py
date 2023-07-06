import math
import random
import joblib
import numpy as np
import torch
from einops import rearrange, reduce
from einops.layers.torch import Rearrange, EinMix
from torch import nn
import torch.nn.functional as F
from model.conformer.conformer import Conformer
from torchmetrics.classification import MulticlassAccuracy
from encodec import EncodecModel


def uniform(shape, min=0, max=1, device=None):
    return torch.zeros(shape, device=device).float().uniform_(0, 1)

def cosine_schedule(t):
    return torch.cos(t * math.pi * 0.5)


def gamma_func(t):
    return np.cos(t * np.pi / 2)


def top_k(logits, thres=0.9):
    k = math.ceil((1 - thres) * logits.shape[-1])
    val, ind = logits.topk(k, dim=-1)
    probs = torch.full_like(logits, float('-inf'))
    probs.scatter_(2, ind, val)
    return probs


def weights_init(m):
    classname = m.__class__.__name__
    if "Linear" in classname or "Embedding" == classname:
        #print(f"Initializing Module {classname}.")
        nn.init.trunc_normal_(m.weight.data, 0.0, 0.02)


def log(t, eps=1e-10):
    return torch.log(t + eps)


def gumbel_noise(t):
    noise = torch.zeros_like(t).uniform_(0, 1)
    return -log(-log(noise))


def gumbel_sample(t, temperature=1., dim=-1):
    return ((t / max(temperature, 1e-10)) + gumbel_noise(t)).argmax(dim=dim)


class SoundStorm(nn.Module):

    def __init__(self, config, encodec=None, hubert_kmean_path=None):

        super().__init__()
        """
        explanations and examples:
        acoustic_codebook_size: 1025 (1024 token + 1 pad), mask_id not included
        semantic_codebook_size 1025 (1024 token + 1 pad)
        positionwise_conv_kernel_size: can use 1 (original), 3, 5 (from fastspeech paper)
        """

        dim = config['model']['dim']
        heads = config['model']['head']
        linear_units = config['model']['linear_units']
        num_blocks = config['model']['n_layer']
        semantic_codebook_size = config['model']['semantic_codebook_size']
        semantic_num_quantizers = 1
        acoustic_codebook_size = config['model']['acoustic_codebook_size']
        acoustic_num_quantizers = config['model']['acoustic_num_quantizers']
        positionwise_conv_kernel_size = config['model']['positionwise_conv_kernel_size']
        conv_module_kernel_size = config['model']['conv_module_kernel_size']
        num_codes_with_mask = acoustic_codebook_size + 1

        self.steps = config['inference']['inference_steps']
        self.filter_threshold = config['inference']['filter_threshold']
        self.temperature = config['inference']['temperature']
        self.ignore_index = acoustic_codebook_size

        # current only support 1024dim codec + 1 padding + 1 mask_id,  last idx is 1025, change later if needed
        assert self.ignore_index == 1025
        self.n_q = acoustic_num_quantizers

        # embeddings for semantic tokens / acoustic tokens
        self.semantic_embeds = nn.Embedding((semantic_codebook_size + 1) * semantic_num_quantizers, dim)

        self.code_embeds = nn.ModuleList(
            [
                nn.Embedding(num_codes_with_mask + 2, dim)
                for _ in range(acoustic_num_quantizers)
            ]
        )

        # the masked token_id during MLM training, and the masked upper level tokens, represented by the last token
        self.mask_token_id = acoustic_codebook_size
        self.mask_upper_level = acoustic_codebook_size

        # the conformer
        self.lm = Conformer(
            attention_dim=dim,
            attention_heads=heads,
            linear_units=linear_units,
            num_blocks=num_blocks,
            positionwise_conv_kernel_size=positionwise_conv_kernel_size,
            use_cnn_module=True,
            cnn_module_kernel=conv_module_kernel_size
        )

        self.heads = nn.Sequential(
            nn.Linear(dim, dim * acoustic_num_quantizers),
            Rearrange('b n (h d) -> b (n h) d', h=acoustic_num_quantizers),
            nn.GELU(),
            nn.LayerNorm(dim, eps=1e-6),
            Rearrange('b (n q) d -> b n q d', q=acoustic_num_quantizers)
        )


        self.bias = nn.ParameterList([
                nn.Parameter(torch.zeros(num_codes_with_mask + 2))
                for _ in range(acoustic_num_quantizers)
            ]
        )

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            EinMix(
                'b n q d -> b n q l',
                weight_shape='q d l',
                bias_shape='q l',
                q=acoustic_num_quantizers,
                l=acoustic_codebook_size,
                d=dim
            )
        )

        self.loss = nn.CrossEntropyLoss(reduction='mean', ignore_index=self.ignore_index)

        # the top 10 accuracy of acoustic tokens
        self.accuracy_metric = MulticlassAccuracy(
            num_classes=acoustic_codebook_size,
            top_k=10,
            average="micro",
            multidim_average="global",
            ignore_index=positionwise_conv_kernel_size - 1,  # ignore padding
        )

        # project the dimension of semantic tokens to model dimension
        self.sem_cond_proj = nn.Linear(dim, dim)
        self.apply(weights_init)

        # initialize conformer token embedding as encodec/hubert vectors
        if encodec is not None:
            self._read_embedding_from_encodec(encodec)

        if hubert_kmean_path is not None:
            self._read_embedding_from_hubert_kmeans(hubert_kmean_path)

    def _read_embedding_from_encodec(self, encodec: EncodecModel):
        for i, layer in enumerate(encodec.quantizer.vq.layers[:self.n_q]):
            layer_weight = layer.codebook
            layer_dim = layer_weight.size(1)
            code_per_layer = layer_weight.size(0)
            assert code_per_layer == 1024
            self.code_embeds[i].weight.data[:code_per_layer, :layer_dim] = layer_weight.clone().data

    def _read_embedding_from_hubert_kmeans(self, km_path: str):
        km_model = joblib.load(km_path)
        centers = km_model.cluster_centers_.transpose()
        centers = torch.tensor(centers, dtype=torch.float32).transpose(0, 1)
        self.semantic_embeds.weight.data[:centers.size(0), :centers.size(1)] = centers.clone()

    def _level_mask(self, code, seq_len, b, t, device):

        rand_times = torch.empty(b, device=device).uniform_(0, 1)
        batched_randperm = torch.rand((b, seq_len - t), device=device).argsort(dim=-1).float()
        rand_probs = cosine_schedule(rand_times)
        num_tokens_mask = (rand_probs * (seq_len - t)).clamp(min=1.)

        mask = batched_randperm < rearrange(num_tokens_mask, 'b -> b 1')
        prompt_mask = torch.ones((b, t), device=device).eq(0)
        mask = torch.cat([prompt_mask, mask], dim=1)

        labels = torch.where(mask, code, self.ignore_index)
        code = torch.where(mask, self.mask_token_id, code)

        return code, labels

    def _fine_mask(self, code, t):
        code[:, t:] = self.mask_upper_level
        return code

    def _masking(self, codes, q=None, t=None):
        seq_len = codes.shape[1]
        batch = codes.shape[0]
        codes = rearrange(codes, 'b n q -> q b n')

        masked_codes = []

        for i, code in enumerate(codes):
            if q == i:
                c, label = self._level_mask(code, seq_len, batch, t, codes.device)
                masked_codes.append(c)
            elif i > q:
                masked_codes.append(self._fine_mask(code, t))
            else:
                masked_codes.append(code)

        return masked_codes, label

    def forward(self, cond, codes):

        """
        cond: [B, Len]
        codes: [B, Len, n_q]
        """

        q = random.randint(0, self.n_q - 1)
        t = random.randint(0, codes.shape[1] - 1)

        masked_codes, labels = self._masking(codes, q, t)

        masked_codes = torch.stack(masked_codes, dim=0)
        masked_codes = rearrange(masked_codes, 'q b n -> b n q')

        emb = None

        for i, layer in enumerate(self.code_embeds):
            if emb is None:
                emb = layer(masked_codes[:, :, i])
            else:
                emb = emb + layer(masked_codes[:, :, i])

        # upsample the semantic tokens
        acoustic_len = codes.size(1)
        semantic_len = cond.size(1)
        semb = self.semantic_embeds(cond)               # [B, n, d]
        fetch_idx = torch.arange(0, acoustic_len).to(semb.device) * 2 / 3
        fetch_idx_int = fetch_idx.to(torch.int64).clamp(0, semantic_len - 1)
        fetch_idx_res = fetch_idx - fetch_idx_int

        sem_cond_upscale = semb[:, fetch_idx_int] * (1 - fetch_idx_res).unsqueeze(0).unsqueeze(2) \
                           + semb[:, (fetch_idx_int + 1).clamp(0, semantic_len - 1)] * fetch_idx_res.unsqueeze(
            0).unsqueeze(2)
        semb = self.sem_cond_proj(sem_cond_upscale)
        emb = emb + semb

        # conformer prediction
        out, _ = self.lm(emb, None)                   # [B, n, d]
        out = self.heads(out)                         # [B, q*n, d]
        logits = self.to_logits(out)                  # [B, n, q, d]
        logits = logits[:, :, q]                      # [B, n, d]

        loss = F.cross_entropy(
            rearrange(logits, 'b n c -> b c n'),
            labels,
            ignore_index=self.ignore_index
        )

        acc_mask = rearrange(~labels.eq(1025), 'b n -> (b n)')

        # top 10 accuracy of acoustic tokens
        acc = self.accuracy_metric(rearrange(logits, 'b n c -> (b n) c')[acc_mask], rearrange(labels, 'b n -> (b n)')[acc_mask]).item()

        return loss, acc

    def tokens_to_logits(self, semb, input_codes):
        # sum the embedding of all (unmasked / masked quantizer layers)    [B, n, q]
        emb = semb
        for i, layer in enumerate(self.code_embeds):
            emb = emb + layer(input_codes[:, :, i])

        out, _ = self.lm(emb, None)   # [B, n, d]
        out = self.heads(out)         # [B, q*n, d]
        logits = self.to_logits(out)  # [B, n, q, d]

        return logits

    @torch.no_grad()
    def generate(self, conds, codes):

        # clip the first 3 sec of ground truth as prompt, remove rest
        # if sample too short, use first half
        # currently we assume we know the ground-truth length to generate, needs to be replaced in the future

        num_latents_input = int(conds.size(1) * 1.5)  # Scale by 1.5 because HuBERT is 50Hz, Encodec is 75Hz
        num_prompt = min(int(num_latents_input * 0.5), 225)  # Default is 3 seconds (3*75Hz = 225 frames)

        prompt = codes[:, :num_prompt, :]
        device = next(self.lm.parameters()).device
        num_latents_to_generate = num_latents_input - num_prompt
        batch_size = 1

        acoustic_len = num_latents_input
        semantic_len = conds.size(1)

        # upsample sem tokens
        semb = self.semantic_embeds(conds)  # [B, n, d]
        fetch_idx = torch.arange(0, acoustic_len).to(semb.device) * 2 / 3
        fetch_idx_int = fetch_idx.to(torch.int64).clamp(0, semantic_len - 1)
        fetch_idx_res = fetch_idx - fetch_idx_int
        sem_cond_upscale = semb[:, fetch_idx_int] * (1 - fetch_idx_res).unsqueeze(0).unsqueeze(2) \
                           + semb[:, (fetch_idx_int + 1).clamp(0, semantic_len - 1)] * fetch_idx_res.unsqueeze(
            0).unsqueeze(2)
        semb = self.sem_cond_proj(sem_cond_upscale)


        # sequence starts off as all masked
        seq_len = num_latents_to_generate
        shape = (batch_size, seq_len, 8)
        seq = torch.full(shape, self.mask_token_id, device=device)
        mask = torch.full(shape, True, device=device)



        # from lucidrain's inference code
        for rvq_layer in range(8):

            # Calculate number of tokens to have masked at each time step
            iter_steps = self.steps[rvq_layer]
            times = torch.linspace(0., 1., iter_steps + 1)
            all_mask_num_tokens = (cosine_schedule(times[1:]) * seq_len).long()

            for mask_num_tokens, steps_until_x0 in zip(all_mask_num_tokens.tolist(), reversed(range(iter_steps))):

                logits = self.tokens_to_logits(semb, torch.cat([prompt, seq], dim=1))
                logits = logits.view(batch_size, num_latents_to_generate + num_prompt, 8, 1025)
                logits = logits[:, num_prompt:, rvq_layer, :]  # Get the logits we want to consider (post-prompt and on given RVQ layer)

                # Top codebook vector index for each of the timestamps
                logits = top_k(logits, self.filter_threshold)  # Remove logits below a certain threshold (convert to -inf)
                sampled_ids = gumbel_sample(logits, temperature=max(self.temperature, 1e-3))

                # Temporarily replace all tokens where mask is still True with sample tokens, will be undone below after mask is recomputed
                # Only tokens that are unmasked in the update will be kept
                seq[:, :, rvq_layer] = torch.where(mask[:, :, rvq_layer], sampled_ids, seq[:, :, rvq_layer])

                scores = 1 - logits.softmax(dim=-1)
                scores = scores.gather(2, rearrange(sampled_ids, 'b n -> b n 1'))  # gather the logits that it sampled
                scores = rearrange(scores, 'b n 1 -> b n')

                # No more tokens left to unmask, move to next RVQ layer
                if mask_num_tokens == 0:
                    continue

                # Remove scores corresponding to positions that have already been unmasked
                scores = scores.masked_fill(~mask[:, :, rvq_layer], -torch.finfo(scores.dtype).max)

                # High score = low probability logit value so select the highest `mask_num_tokens` to remain masked after this step
                mask_indices = scores.topk(mask_num_tokens, dim=-1).indices
                mask[:, :, rvq_layer] = torch.zeros_like(scores, dtype=torch.bool).scatter(1, mask_indices, True)
                # Update seq with the newly calculated mask
                seq[:, :, rvq_layer] = seq[:, :, rvq_layer].masked_fill(mask[:, :, rvq_layer], self.mask_token_id)

        out = torch.cat([prompt, seq], dim=1)
        return out

