# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
import sys
from typing import Dict, List, Optional

import torch
import torch.nn as nn
from torch import Tensor

from fairseq import search, utils
from fairseq import mc_dropout_config as config
from fairseq.data import data_utils
from fairseq.models import FairseqIncrementalDecoder
from fairseq.ngram_repeat_block import NGramRepeatBlock



class SequenceGenerator(nn.Module):
    def __init__(
        self,
        models,
        tgt_dict,
        beam_size=1,
        max_len_a=0,
        max_len_b=200,
        max_len=0,
        min_len=1,
        normalize_scores=True,
        len_penalty=1.0,
        unk_penalty=0.0,
        temperature=1.0,
        match_source_len=False,
        no_repeat_ngram_size=0,
        search_strategy=None,
        eos=None,
        symbols_to_strip_from_output=None,
        lm_model=None,
        lm_weight=1.0,
        tokens_to_suppress=(),
    ):
        """Generates translations of a given source sentence.

        Args:
            models (List[~fairseq.models.FairseqModel]): ensemble of models,
                currently support fairseq.models.TransformerModel for scripting
            beam_size (int, optional): beam width (default: 1)
            max_len_a/b (int, optional): generate sequences of maximum length
                ax + b, where x is the source length
            max_len (int, optional): the maximum length of the generated output
                (not including end-of-sentence)
            min_len (int, optional): the minimum length of the generated output
                (not including end-of-sentence)
            normalize_scores (bool, optional): normalize scores by the length
                of the output (default: True)
            len_penalty (float, optional): length penalty, where <1.0 favors
                shorter, >1.0 favors longer sentences (default: 1.0)
            unk_penalty (float, optional): unknown word penalty, where <0
                produces more unks, >0 produces fewer (default: 0.0)
            temperature (float, optional): temperature, where values
                >1.0 produce more uniform samples and values <1.0 produce
                sharper samples (default: 1.0)
            match_source_len (bool, optional): outputs should match the source
                length (default: False)
        """
        super().__init__()
        if isinstance(models, EnsembleModel):
            self.model = models
        else:
            self.model = EnsembleModel(models)
        self.tgt_dict = tgt_dict
        self.pad = tgt_dict.pad()
        self.unk = tgt_dict.unk()
        self.eos = tgt_dict.eos() if eos is None else eos
        self.symbols_to_strip_from_output = (
            symbols_to_strip_from_output.union({self.eos})
            if symbols_to_strip_from_output is not None
            else {self.eos}
        )

        self.token_indices_to_suppress: Optional[Tensor] = None
        token_indices_to_suppress = []
        for token_string in tokens_to_suppress:
            token_index = tgt_dict.index(token_string)
            assert token_index != self.unk
            token_indices_to_suppress.append(token_index)
        if len(token_indices_to_suppress) > 0:
            self.token_indices_to_suppress = torch.Tensor(
                token_indices_to_suppress
            ).long()

        self.vocab_size = len(tgt_dict)
        self.beam_size = beam_size
        # the max beam size is the dictionary size - 1, since we never select pad
        self.beam_size = min(beam_size, self.vocab_size - 1)
        self.model.set_decoder_beam_size(self.beam_size)
        self.max_len_a = max_len_a
        self.max_len_b = max_len_b
        self.min_len = min_len
        self.max_len = max_len or self.model.max_decoder_positions()

        self.normalize_scores = normalize_scores
        self.len_penalty = len_penalty
        self.unk_penalty = unk_penalty
        self.temperature = temperature
        self.match_source_len = match_source_len

        if no_repeat_ngram_size > 0:
            self.repeat_ngram_blocker = NGramRepeatBlock(no_repeat_ngram_size)
        else:
            self.repeat_ngram_blocker = None

        assert temperature > 0, "--temperature must be greater than 0"

        self.search = (
            search.BeamSearch(tgt_dict) if search_strategy is None else search_strategy
        )
        # We only need to set src_lengths in LengthConstrainedBeamSearch.
        # As a module attribute, setting it would break in multithread
        # settings when the model is shared.
        self.should_set_src_lengths = (
            hasattr(self.search, "needs_src_lengths") and self.search.needs_src_lengths
        )

        self.model.train()

        self.lm_model = lm_model
        self.lm_weight = lm_weight
        if self.lm_model is not None:
            self.lm_model.train()

    def cuda(self):
        self.model.cuda()
        return self

    @torch.no_grad()
    def forward(
        self,
        sample: Dict[str, Dict[str, Tensor]],
        prefix_tokens: Optional[Tensor] = None,
        bos_token: Optional[int] = None,
    ):
        """Generate a batch of translations.

        Args:
            sample (dict): batch
            prefix_tokens (torch.LongTensor, optional): force decoder to begin
                with these tokens
            bos_token (int, optional): beginning of sentence token
                (default: self.eos)
        """
        return self._generate(sample, prefix_tokens, bos_token=bos_token)

    # TODO(myleott): unused, deprecate after pytorch-translate migration
    def generate_batched_itr(self, data_itr, beam_size=None, cuda=False, timer=None):
        """Iterate over a batched dataset and yield individual translations.
        Args:
            cuda (bool, optional): use GPU for generation
            timer (StopwatchMeter, optional): time generations
        """
        for sample in data_itr:
            s = utils.move_to_cuda(sample) if cuda else sample
            if "net_input" not in s:
                continue
            input = s["net_input"]
            # model.forward normally channels prev_output_tokens into the decoder
            # separately, but SequenceGenerator directly calls model.encoder
            encoder_input = {
                k: v for k, v in input.items() if k != "prev_output_tokens"
            }
            if timer is not None:
                timer.start()
            with torch.no_grad():
                hypos = self.generate(encoder_input)
            if timer is not None:
                timer.stop(sum(len(h[0]["tokens"]) for h in hypos))
            for i, id in enumerate(s["id"].data):
                # remove padding
                src = utils.strip_pad(input["src_tokens"].data[i, :], self.pad)
                ref = (
                    utils.strip_pad(s["target"].data[i, :], self.pad)
                    if s["target"] is not None
                    else None
                )
                yield id, src, ref, hypos[i]

    @torch.no_grad()
    def generate(
        self, models, sample: Dict[str, Dict[str, Tensor]], **kwargs
    ) -> List[List[Dict[str, Tensor]]]:
        """Generate translations. Match the api of other fairseq generators.

        Args:
            models (List[~fairseq.models.FairseqModel]): ensemble of models
            sample (dict): batch
            prefix_tokens (torch.LongTensor, optional): force decoder to begin
                with these tokens
            constraints (torch.LongTensor, optional): force decoder to include
                the list of constraints
            bos_token (int, optional): beginning of sentence token
                (default: self.eos)
        """
        return self._generate(sample, **kwargs)

    def _generate(
        self,
        sample: Dict[str, Dict[str, Tensor]],
        prefix_tokens: Optional[Tensor] = None,
        constraints: Optional[Tensor] = None,
        bos_token: Optional[int] = None,
    ):
        incremental_states = torch.jit.annotate(
            List[Dict[str, Dict[str, Optional[Tensor]]]],
            [
                torch.jit.annotate(Dict[str, Dict[str, Optional[Tensor]]], {})
                for i in range(self.model.models_size)
            ],
        )
        net_input = sample["net_input"]

        if "src_tokens" in net_input:
            src_tokens = net_input["src_tokens"]
            # length of the source text being the character length except EndOfSentence and pad
            # if src_lengths exists in net_input (speech_to_text dataset case), then use it
            if "src_lengths" in net_input:
                src_lengths = net_input["src_lengths"]
            else:
                src_lengths = (
                    (src_tokens.ne(self.eos) & src_tokens.ne(self.pad))
                    .long()
                    .sum(dim=1)
                )
        elif "source" in net_input:
            src_tokens = net_input["source"]
            src_lengths = (
                net_input["padding_mask"].size(-1) - net_input["padding_mask"].sum(-1)
                if net_input["padding_mask"] is not None
                else torch.tensor(src_tokens.size(-1)).to(src_tokens)
            )
        elif "features" in net_input:
            src_tokens = net_input["features"]
            src_lengths = (
                net_input["padding_mask"].size(-1) - net_input["padding_mask"].sum(-1)
                if net_input["padding_mask"] is not None
                else torch.tensor(src_tokens.size(-1)).to(src_tokens)
            )
        else:
            raise Exception(
                "expected src_tokens or source in net input. input keys: "
                + str(net_input.keys())
            )

        # bsz: total number of sentences in beam
        # Note that src_tokens may have more than 2 dimensions (i.e. audio features)
        bsz, src_len = src_tokens.size()[:2]
        beam_size = self.beam_size

        if constraints is not None and not self.search.supports_constraints:
            raise NotImplementedError(
                "Target-side constraints were provided, but search method doesn't support them"
            )

        # Initialize constraints, when active
        self.search.init_constraints(constraints, beam_size)

        max_len: int = -1
        if self.match_source_len:
            max_len = src_lengths.max().item()
        else:
            max_len = min(
                int(self.max_len_a * src_len + self.max_len_b),
                self.max_len - 1,
            )
        assert (
            self.min_len <= max_len
        ), "min_len cannot be larger than max_len, please adjust these!"
        # compute the encoder output for each beam
        with torch.autograd.profiler.record_function("EnsembleModel: forward_encoder"):
            encoder_outs = self.model.forward_encoder(net_input)

        # placeholder of indices for bsz * beam_size to hold tokens and accumulative scores
        new_order = torch.arange(bsz).view(-1, 1).repeat(1, beam_size).view(-1)
        new_order = new_order.to(src_tokens.device).long()
        encoder_outs = self.model.reorder_encoder_out(encoder_outs, new_order)
        # ensure encoder_outs is a List.
        assert encoder_outs is not None

        # initialize buffers
        scores = (
            torch.zeros(bsz * beam_size, max_len + 1).to(src_tokens).float()
        )  # +1 for eos; pad is never chosen for scoring
        tokens = (
            torch.zeros(bsz * beam_size, max_len + 2)
            .to(src_tokens)
            .long()
            .fill_(self.pad)
        )  # +2 for eos and pad
        tokens[:, 0] = self.eos if bos_token is None else bos_token
        attn: Optional[Tensor] = None
        
        eentropies = torch.full_like(tokens, 0).type(torch.FloatTensor).to(tokens.device)
        pentropies = torch.full_like(tokens, 0).type(torch.FloatTensor).to(tokens.device)
        balds = torch.full_like(tokens, 0).type(torch.FloatTensor).to(tokens.device)
        epkls = torch.full_like(tokens, 0).type(torch.FloatTensor).to(tokens.device)
        rmis = torch.full_like(tokens, 0).type(torch.FloatTensor).to(tokens.device)

        # A list that indicates candidates that should be ignored.
        # For example, suppose we're sampling and have already finalized 2/5
        # samples. Then cands_to_ignore would mark 2 positions as being ignored,
        # so that we only finalize the remaining 3 samples.
        cands_to_ignore = (
            torch.zeros(bsz, beam_size).to(src_tokens).eq(-1)
        )  # forward and backward-compatible False mask

        # list of completed sentences
        finalized = torch.jit.annotate(
            List[List[Dict[str, Tensor]]],
            [torch.jit.annotate(List[Dict[str, Tensor]], []) for i in range(bsz)],
        )  # contains lists of dictionaries of infomation about the hypothesis being finalized at each step

        # a boolean array indicating if the sentence at the index is finished or not
        finished = [False for i in range(bsz)]
        num_remaining_sent = bsz  # number of sentences remaining

        # number of candidate hypos per step
        cand_size = 2 * beam_size  # 2 x beam size in case half are EOS

        # offset arrays for converting between different indexing schemes
        bbsz_offsets = (
            (torch.arange(0, bsz) * beam_size)
            .unsqueeze(1)
            .type_as(tokens)
            .to(src_tokens.device)
        )
        cand_offsets = torch.arange(0, cand_size).type_as(tokens).to(src_tokens.device)

        reorder_state: Optional[Tensor] = None
        batch_idxs: Optional[Tensor] = None

        original_batch_idxs: Optional[Tensor] = None
        if "id" in sample and isinstance(sample["id"], Tensor):
            original_batch_idxs = sample["id"]
        else:
            original_batch_idxs = torch.arange(0, bsz).type_as(tokens)

        for step in range(max_len + 1):  # one extra step for EOS marker
            # reorder decoder internal states based on the prev choice of beams
            if reorder_state is not None:
                if batch_idxs is not None:
                    # update beam indices to take into account removed sentences
                    corr = batch_idxs - torch.arange(batch_idxs.numel()).type_as(
                        batch_idxs
                    )
                    reorder_state.view(-1, beam_size).add_(
                        corr.unsqueeze(-1) * beam_size
                    )
                    original_batch_idxs = original_batch_idxs[batch_idxs]
                self.model.reorder_incremental_state(incremental_states, reorder_state)
                encoder_outs = self.model.reorder_encoder_out(
                    encoder_outs, reorder_state
                )
            with torch.autograd.profiler.record_function(
                "EnsembleModel: forward_decoder"
            ):
                # lprobs, avg_attn_scores, eentropy, pentropy, bald, epkl, rmi = self.model.forward_decoder(
                lprobs, avg_attn_scores, models_lprobs = self.model.forward_decoder(
                    tokens[:, : step + 1],
                    encoder_outs,
                    incremental_states,
                    self.temperature,
                )
            if self.lm_model is not None:
                lm_out = self.lm_model(tokens[:, : step + 1])
                probs = self.lm_model.get_normalized_probs(
                    lm_out, log_probs=True, sample=None
                )
                probs = probs[:, -1, :] * self.lm_weight
                lprobs += probs

            # What happens here and further down?
            
            #################
            # This is the part where probability distributions from decoder over vocabulary
            # undergo additional post-processing. In basic implementation of ensembles in fairseq
            # this only happens for averaged probabilities. For uncertainty estimation this must
            # be done for individual model outputs also
            #################
            
            lprobs[lprobs != lprobs] = torch.tensor(-math.inf).to(lprobs)

            lprobs[:, self.pad] = -math.inf  # never select pad
            lprobs[:, self.unk] -= self.unk_penalty  # apply unk penalty

            # handle max length constraint
            if step >= max_len:
                lprobs[:, : self.eos] = -math.inf
                lprobs[:, self.eos + 1 :] = -math.inf

            # handle prefix tokens (possibly with different lengths)
            if (
                prefix_tokens is not None
                and step < prefix_tokens.size(1)
                and step < max_len
            ):
                lprobs, tokens, scores = self._prefix_tokens(
                    step, lprobs, scores, tokens, prefix_tokens, beam_size
                )
            else:
                if step < self.min_len:
                    # minimum length constraint (does not apply if using prefix_tokens)
                    lprobs[:, self.eos] = -math.inf

                if self.token_indices_to_suppress is not None:
                    lprobs[:, self.token_indices_to_suppress] = -math.inf

            # Record attention scores, only support avg_attn_scores is a Tensor
            if avg_attn_scores is not None:
                if attn is None:
                    attn = torch.empty(
                        bsz * beam_size, avg_attn_scores.size(1), max_len + 2
                    ).to(scores)
                attn[:, :, step + 1].copy_(avg_attn_scores)

            scores = scores.type_as(lprobs)
            eos_bbsz_idx = torch.empty(0).to(
                tokens
            )  # indices of hypothesis ending with eos (finished sentences)
            eos_scores = torch.empty(0).to(
                scores
            )  # scores of hypothesis ending with eos (finished sentences)

            if self.should_set_src_lengths:
                self.search.set_src_lengths(src_lengths)

            if self.repeat_ngram_blocker is not None:
                lprobs = self.repeat_ngram_blocker(tokens, lprobs, bsz, beam_size, step)

            #############
            # HERE GOES REPEATING: REFACTOR THIS!
            # This is the same post-processing but for individual outputs.
            #############
            
            postprocessed_models_lprobs = []
            for model_lprobs in models_lprobs:
                model_lprobs[model_lprobs != model_lprobs] = torch.tensor(-math.inf).to(model_lprobs)

                model_lprobs[:, self.pad] = -math.inf  # never select pad
                model_lprobs[:, self.unk] -= self.unk_penalty  # apply unk penalty

                # handle max length constraint
                if step >= max_len:
                    model_lprobs[:, : self.eos] = -math.inf
                    model_lprobs[:, self.eos + 1 :] = -math.inf

                # handle prefix tokens (possibly with different lengths)
                if (
                    prefix_tokens is not None
                    and step < prefix_tokens.size(1)
                    and step < max_len
                ):
                    model_lprobs, tokens, scores = self._prefix_tokens(
                        step, model_lprobs, scores, tokens, prefix_tokens, beam_size
                    )
                else:
                    if step < self.min_len:
                        # minimum length constraint (does not apply if using prefix_tokens)
                        model_lprobs[:, self.eos] = -math.inf

                    if self.token_indices_to_suppress is not None:
                        model_lprobs[:, self.token_indices_to_suppress] = -math.inf

                if self.repeat_ngram_blocker is not None:
                    model_lprobs = self.repeat_ngram_blocker(tokens, model_lprobs, bsz, beam_size, step)
                
                postprocessed_models_lprobs.append(model_lprobs)
                
            # Shape: (batch, cand_size)
            cand_scores, cand_indices, cand_beams, eentropy, pentropy, bald, epkl, rmi = self.search.step(
                step,
                lprobs.view(bsz, -1, self.vocab_size),
                [model_lprobs.view(bsz, -1, self.vocab_size) for model_lprobs in postprocessed_models_lprobs],
                scores.view(bsz, beam_size, -1)[:, :, :step],
                tokens[:, : step + 1],
                original_batch_idxs,
            )
            
            # eentropies.append(eentropy)
            # pentropies.append(pentropy)
            # balds.append(bald)
            # epkls.append(epkl)
            # rmis.append(rmi)

            # cand_bbsz_idx contains beam indices for the top candidate
            # hypotheses, with a range of values: [0, bsz*beam_size),
            # and dimensions: [bsz, cand_size]
            cand_bbsz_idx = cand_beams.add(bbsz_offsets)

            # finalize hypotheses that end in eos
            # Shape of eos_mask: (batch size, beam size)
            eos_mask = cand_indices.eq(self.eos) & cand_scores.ne(-math.inf)
            eos_mask[:, :beam_size][cands_to_ignore] = torch.tensor(0).to(eos_mask)

            # only consider eos when it's among the top beam_size indices
            # Now we know what beam item(s) to finish
            # Shape: 1d list of absolute-numbered
            eos_bbsz_idx = torch.masked_select(
                cand_bbsz_idx[:, :beam_size], mask=eos_mask[:, :beam_size]
            )
                        
            finalized_sents: List[int] = []
            if eos_bbsz_idx.numel() > 0:
                eos_scores = torch.masked_select(
                    cand_scores[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                eos_eentropy = torch.masked_select(
                    eentropy[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                eos_pentropy = torch.masked_select(
                    pentropy[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                eos_bald = torch.masked_select(
                    bald[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                eos_epkl = torch.masked_select(
                    epkl[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                eos_rmi = torch.masked_select(
                    rmi[:, :beam_size], mask=eos_mask[:, :beam_size]
                )
                
                finalized_sents = self.finalize_hypos(
                    step,
                    eos_bbsz_idx,
                    eos_scores,
                    tokens,
                    scores,
                    eentropies,
                    pentropies,
                    balds,
                    epkls,
                    rmis,
                    eos_eentropy,
                    eos_pentropy,
                    eos_bald,
                    eos_epkl,
                    eos_rmi,
                    finalized,
                    finished,
                    beam_size,
                    attn,
                    src_lengths,
                    max_len,
                )
                num_remaining_sent -= len(finalized_sents)

            assert num_remaining_sent >= 0
            if num_remaining_sent == 0:
                break
            if self.search.stop_on_max_len and step >= max_len:
                break
            assert step < max_len, f"{step} < {max_len}"

            # Remove finalized sentences (ones for which {beam_size}
            # finished hypotheses have been generated) from the batch.
            if len(finalized_sents) > 0:
                new_bsz = bsz - len(finalized_sents)

                # construct batch_idxs which holds indices of batches to keep for the next pass
                batch_mask = torch.ones(
                    bsz, dtype=torch.bool, device=cand_indices.device
                )
                batch_mask[finalized_sents] = False
                # TODO replace `nonzero(as_tuple=False)` after TorchScript supports it
                batch_idxs = torch.arange(
                    bsz, device=cand_indices.device
                ).masked_select(batch_mask)

                # Choose the subset of the hypothesized constraints that will continue
                self.search.prune_sentences(batch_idxs)

                eos_mask = eos_mask[batch_idxs]
                cand_beams = cand_beams[batch_idxs]
                bbsz_offsets.resize_(new_bsz, 1)
                cand_bbsz_idx = cand_beams.add(bbsz_offsets)
                cand_scores = cand_scores[batch_idxs]
                cand_indices = cand_indices[batch_idxs]

                if prefix_tokens is not None:
                    prefix_tokens = prefix_tokens[batch_idxs]
                src_lengths = src_lengths[batch_idxs]
                cands_to_ignore = cands_to_ignore[batch_idxs]

                scores = scores.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                tokens = tokens.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                eentropies = eentropies.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                pentropies = pentropies.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                balds = balds.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                epkls = epkls.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                rmis = rmis.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                
                if attn is not None:
                    attn = attn.view(bsz, -1)[batch_idxs].view(
                        new_bsz * beam_size, attn.size(1), -1
                    )
                bsz = new_bsz
            else:
                batch_idxs = None

            # Set active_mask so that values > cand_size indicate eos hypos
            # and values < cand_size indicate candidate active hypos.
            # After, the min values per row are the top candidate active hypos

            # Rewrite the operator since the element wise or is not supported in torchscript.

            eos_mask[:, :beam_size] = ~((~cands_to_ignore) & (~eos_mask[:, :beam_size]))
            active_mask = torch.add(
                eos_mask.type_as(cand_offsets) * cand_size,
                cand_offsets[: eos_mask.size(1)],
            )

            # get the top beam_size active hypotheses, which are just
            # the hypos with the smallest values in active_mask.
            # {active_hypos} indicates which {beam_size} hypotheses
            # from the list of {2 * beam_size} candidates were
            # selected. Shapes: (batch size, beam size)
            new_cands_to_ignore, active_hypos = torch.topk(
                active_mask, k=beam_size, dim=1, largest=False
            )

            # update cands_to_ignore to ignore any finalized hypos.
            cands_to_ignore = new_cands_to_ignore.ge(cand_size)[:, :beam_size]
            # Make sure there is at least one active item for each sentence in the batch.
            assert (~cands_to_ignore).any(dim=1).all()

            # update cands_to_ignore to ignore any finalized hypos

            # {active_bbsz_idx} denotes which beam number is continued for each new hypothesis (a beam
            # can be selected more than once).
            active_bbsz_idx = torch.gather(cand_bbsz_idx, dim=1, index=active_hypos)
            active_scores = torch.gather(cand_scores, dim=1, index=active_hypos)

            active_bbsz_idx = active_bbsz_idx.view(-1)
            active_scores = active_scores.view(-1)

            # copy tokens and scores for active hypotheses
            
            # Set the tokens for each beam (can select the same row more than once)
            tokens[:, : step + 1] = torch.index_select(
                tokens[:, : step + 1], dim=0, index=active_bbsz_idx
            )
            # Select the next token for each of them
            tokens.view(bsz, beam_size, -1)[:, :, step + 1] = torch.gather(
                cand_indices, dim=1, index=active_hypos
            )
            if step > 0:
                scores[:, :step] = torch.index_select(
                    scores[:, :step], dim=0, index=active_bbsz_idx
                )
            scores.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
                cand_scores, dim=1, index=active_hypos
            )
            
            # Do the same for uncertainty estimate containers:
            eentropies[:, : step] = torch.index_select(
                eentropies[:, : step], dim=0, index=active_bbsz_idx
            )
            eentropies.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
                eentropy, dim=1, index=active_hypos
            )
            
            pentropies[:, : step] = torch.index_select(
                pentropies[:, : step], dim=0, index=active_bbsz_idx
            )
            pentropies.view(bsz, beam_size, -1)[:, :, step + 1] = torch.gather(
                pentropy, dim=1, index=active_hypos
            )
            
            balds[:, : step] = torch.index_select(
                balds[:, : step], dim=0, index=active_bbsz_idx
            )
            balds.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
                bald, dim=1, index=active_hypos
            )
            
            epkls[:, : step] = torch.index_select(
                epkls[:, : step], dim=0, index=active_bbsz_idx
            )
            epkls.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
                epkl, dim=1, index=active_hypos
            )
            
            rmis[:, : step] = torch.index_select(
                rmis[:, : step], dim=0, index=active_bbsz_idx
            )
            rmis.view(bsz, beam_size, -1)[:, :, step] = torch.gather(
                rmi, dim=1, index=active_hypos
            )


            # Update constraints based on which candidates were selected for the next beam
            self.search.update_constraints(active_hypos)

            # copy attention for active hypotheses
            if attn is not None:
                attn[:, :, : step + 2] = torch.index_select(
                    attn[:, :, : step + 2], dim=0, index=active_bbsz_idx
                )

            # reorder incremental state in decoder
            reorder_state = active_bbsz_idx
        
        # sort by score descending
        for sent in range(len(finalized)):
            scores = torch.tensor(
                [float(elem["score"].item()) for elem in finalized[sent]]
            )
            _, sorted_scores_indices = torch.sort(scores, descending=True)
            finalized[sent] = [finalized[sent][ssi] for ssi in sorted_scores_indices]
            finalized[sent] = torch.jit.annotate(
                List[Dict[str, Tensor]], finalized[sent]
            )
    
        return finalized

    def _prefix_tokens(
        self, step: int, lprobs, scores, tokens, prefix_tokens, beam_size: int
    ):
        """Handle prefix tokens"""
        prefix_toks = prefix_tokens[:, step].unsqueeze(-1).repeat(1, beam_size).view(-1)
        prefix_lprobs = lprobs.gather(-1, prefix_toks.unsqueeze(-1))
        prefix_mask = prefix_toks.ne(self.pad)
        lprobs[prefix_mask] = torch.tensor(-math.inf).to(lprobs)
        lprobs[prefix_mask] = lprobs[prefix_mask].scatter(
            -1, prefix_toks[prefix_mask].unsqueeze(-1), prefix_lprobs[prefix_mask]
        )
        # if prefix includes eos, then we should make sure tokens and
        # scores are the same across all beams
        eos_mask = prefix_toks.eq(self.eos)
        if eos_mask.any():
            # validate that the first beam matches the prefix
            first_beam = tokens[eos_mask].view(-1, beam_size, tokens.size(-1))[
                :, 0, 1 : step + 1
            ]
            eos_mask_batch_dim = eos_mask.view(-1, beam_size)[:, 0]
            target_prefix = prefix_tokens[eos_mask_batch_dim][:, :step]
            assert (first_beam == target_prefix).all()

            # copy tokens, scores and lprobs from the first beam to all beams
            tokens = self.replicate_first_beam(tokens, eos_mask_batch_dim, beam_size)
            scores = self.replicate_first_beam(scores, eos_mask_batch_dim, beam_size)
            lprobs = self.replicate_first_beam(lprobs, eos_mask_batch_dim, beam_size)
        return lprobs, tokens, scores

    def replicate_first_beam(self, tensor, mask, beam_size: int):
        tensor = tensor.view(-1, beam_size, tensor.size(-1))
        tensor[mask] = tensor[mask][:, :1, :]
        return tensor.view(-1, tensor.size(-1))

    def finalize_hypos(
        self,
        step: int,
        bbsz_idx,
        eos_scores,
        tokens,
        scores,
        eentropies,
        pentropies,
        balds,
        epkls,
        rmis,
        eos_eentropy,
        eos_pentropy,
        eos_bald,
        eos_epkl,
        eos_rmi,
        finalized: List[List[Dict[str, Tensor]]],
        finished: List[bool],
        beam_size: int,
        attn: Optional[Tensor],
        src_lengths,
        max_len: int,
    ):
        """Finalize hypothesis, store finalized information in `finalized`, and change `finished` accordingly.
        A sentence is finalized when {beam_size} finished items have been collected for it.

        Returns number of sentences (not beam items) being finalized.
        These will be removed from the batch and not processed further.
        Args:
            bbsz_idx (Tensor):
        """
        assert bbsz_idx.numel() == eos_scores.numel()

        # clone relevant token and attention tensors.
        # tokens is (batch * beam, max_len). So the index_select
        # gets the newly EOS rows, then selects cols 1..{step + 2}
        tokens_clone = tokens.index_select(0, bbsz_idx)[
            :, 1 : step + 2
        ]  # skip the first index, which is EOS
        tokens_clone[:, step] = self.eos
        
        attn_clone = (
            attn.index_select(0, bbsz_idx)[:, :, 1 : step + 2]
            if attn is not None
            else None
        )
        
        eentropies_clone = eentropies.index_select(0, bbsz_idx)[:, : step + 1]
        pentropies_clone = pentropies.index_select(0, bbsz_idx)[:, : step + 1]
        balds_clone = balds.index_select(0, bbsz_idx)[:, : step + 1]
        epkls_clone = epkls.index_select(0, bbsz_idx)[:, : step + 1]
        rmis_clone = rmis.index_select(0, bbsz_idx)[:, : step + 1]
                
        eentropies_clone[:, step] = eos_eentropy
        pentropies_clone[:, step] = eos_pentropy
        balds_clone[:, step] = eos_bald
        epkls_clone[:, step] = eos_epkl
        rmis_clone[:, step] = eos_rmi
        
        cum_eentropies = (torch.sum(eentropies_clone, dim=-1) / (step + 1)) * torch.exp(eos_scores)
        cum_pentropies = (torch.sum(pentropies_clone, dim=-1) / (step + 1)) * torch.exp(eos_scores)
        cum_balds = (torch.sum(balds_clone, dim=-1) / (step + 1)) * torch.exp(eos_scores)
        cum_epkls = (torch.sum(epkls_clone, dim=-1) / (step + 1)) * torch.exp(eos_scores)
        cum_rmis = (torch.sum(rmis_clone, dim=-1) / (step + 1)) * torch.exp(eos_scores)

        # compute scores per token position
        pos_scores = scores.index_select(0, bbsz_idx)[:, : step + 1]
        pos_scores[:, step] = eos_scores
        # convert from cumulative to per-position scores
        pos_scores[:, 1:] = pos_scores[:, 1:] - pos_scores[:, :-1]

        # normalize sentence-level scores
        if self.normalize_scores:
            eos_scores /= (step + 1) ** self.len_penalty

        # cum_unfin records which sentences in the batch are finished.
        # It helps match indexing between (a) the original sentences
        # in the batch and (b) the current, possibly-reduced set of
        # sentences.
        cum_unfin: List[int] = []
        prev = 0
        for f in finished:
            if f:
                prev += 1
            else:
                cum_unfin.append(prev)
        cum_fin_tensor = torch.tensor(cum_unfin, dtype=torch.int).to(bbsz_idx)

        unfin_idx = torch.div(bbsz_idx, beam_size, rounding_mode="trunc")
        sent = unfin_idx + torch.index_select(cum_fin_tensor, 0, unfin_idx)

        # Create a set of "{sent}{unfin_idx}", where
        # "unfin_idx" is the index in the current (possibly reduced)
        # list of sentences, and "sent" is the index in the original,
        # unreduced batch
        # For every finished beam item
        # sentence index in the current (possibly reduced) batch
        seen = (sent << 32) + unfin_idx
        unique_seen: List[int] = torch.unique(seen).tolist()

        if self.match_source_len:
            condition = step > torch.index_select(src_lengths, 0, unfin_idx)
            eos_scores = torch.where(condition, torch.tensor(-math.inf), eos_scores)
        sent_list: List[int] = sent.tolist()
        for i in range(bbsz_idx.size()[0]):
            # An input sentence (among those in a batch) is finished when
            # beam_size hypotheses have been collected for it
            if len(finalized[sent_list[i]]) < beam_size:
                if attn_clone is not None:
                    # remove padding tokens from attn scores
                    hypo_attn = attn_clone[i]
                else:
                    hypo_attn = torch.empty(0)

                finalized[sent_list[i]].append(
                    {
                        "tokens": tokens_clone[i],
                        "score": eos_scores[i],
                        "attention": hypo_attn,  # src_len x tgt_len
                        "alignment": torch.empty(0),
                        "positional_scores": pos_scores[i],
                        "cumulative_uncertainties": {"eentropy": cum_eentropies[i],
                                                     "pentropy": cum_pentropies[i],
                                                     "bald": cum_balds[i],
                                                     "epkl": cum_epkls[i],
                                                     "rmi": cum_rmis[i],
                                                    }
                    }
                )

        newly_finished: List[int] = []
        for unique_s in unique_seen:
            # check termination conditions for this sentence
            unique_sent: int = unique_s >> 32
            unique_unfin_idx: int = unique_s - (unique_sent << 32)

            if not finished[unique_sent] and self.is_finished(
                step, unique_unfin_idx, max_len, len(finalized[unique_sent]), beam_size
            ):
                finished[unique_sent] = True
                newly_finished.append(unique_unfin_idx)

        return newly_finished

    def is_finished(
        self,
        step: int,
        unfin_idx: int,
        max_len: int,
        finalized_sent_len: int,
        beam_size: int,
    ):
        """
        Check whether decoding for a sentence is finished, which
        occurs when the list of finalized sentences has reached the
        beam size, or when we reach the maximum length.
        """
        assert finalized_sent_len <= beam_size
        if finalized_sent_len == beam_size or step == max_len:
            return True
        return False


class EnsembleModel(nn.Module):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__()
        self.models_size = len(models)
        # method '__len__' is not supported in ModuleList for torch script
        self.single_model = models[0]
        self.models = nn.ModuleList(models)

        self.has_incremental: bool = False
        if all(
            hasattr(m, "decoder") and isinstance(m.decoder, FairseqIncrementalDecoder)
            for m in models
        ):
            self.has_incremental = True

    def forward(self):
        pass

    def has_encoder(self):
        return hasattr(self.single_model, "encoder")

    def has_incremental_states(self):
        return self.has_incremental

    def max_decoder_positions(self):
        return min(
            [
                m.max_decoder_positions()
                for m in self.models
                if hasattr(m, "max_decoder_positions")
            ]
            + [sys.maxsize]
        )

    def set_decoder_beam_size(self, beam_size):
        """Set beam size for efficient beamable enc-dec attention."""
        if beam_size > 1:
            for i, model in enumerate(self.models):
                if len(config.model_seeds) > 0:
                    torch.manual_seed(config.model_seeds[i])

                if hasattr(model, "set_beam_size"):
                    model.set_beam_size(beam_size)

    @torch.jit.export
    def forward_encoder(self, net_input: Dict[str, Tensor]):
        if not self.has_encoder():
            return None
        results = []
        for i, model in enumerate(self.models):
            if len(config.model_seeds) > 0:
                torch.manual_seed(config.model_seeds[i])
            results.append(model.encoder.forward_torchscript(net_input))
        return results

    @torch.jit.export
    def forward_decoder(
        self,
        tokens,
        encoder_outs: List[Dict[str, List[Tensor]]],
        incremental_states: List[Dict[str, Dict[str, Optional[Tensor]]]],
        temperature: float = 1.0,
    ):
        log_probs = []
        avg_attn: Optional[Tensor] = None
        encoder_out: Optional[Dict[str, List[Tensor]]] = None
        for i, model in enumerate(self.models):
            if len(config.model_seeds) > 0:
                torch.manual_seed(config.model_seeds[i])
            if self.has_encoder():
                encoder_out = encoder_outs[i]
            # decode each model
            if self.has_incremental_states():
                decoder_out = model.decoder.forward(
                    tokens,
                    encoder_out=encoder_out,
                    incremental_state=incremental_states[i],
                )
            else:
                if hasattr(model, "decoder"):
                    decoder_out = model.decoder.forward(tokens, encoder_out=encoder_out)
                else:
                    decoder_out = model.forward(tokens)

            attn: Optional[Tensor] = None
            decoder_len = len(decoder_out)
            if decoder_len > 1 and decoder_out[1] is not None:
                if isinstance(decoder_out[1], Tensor):
                    attn = decoder_out[1]
                else:
                    attn_holder = decoder_out[1]["attn"]
                    if isinstance(attn_holder, Tensor):
                        attn = attn_holder
                    elif attn_holder is not None:
                        attn = attn_holder[0]
                if attn is not None:
                    attn = attn[:, -1, :]

            decoder_out_tuple = (
                decoder_out[0][:, -1:, :].div_(temperature),
                None if decoder_len <= 1 else decoder_out[1],
            )
            probs = model.get_normalized_probs(
                decoder_out_tuple, log_probs='log', sample=None
            )
            
            probs = probs[:, -1, :]
            if self.models_size == 1:
                return probs, attn
            
            log_probs.append(probs)
            if attn is not None:
                if avg_attn is None:
                    avg_attn = attn
                else:
                    avg_attn.add_(attn)
                    
        avg_probs = torch.logsumexp(torch.stack(log_probs, dim=0), dim=0) - math.log(
            self.models_size
        )
        
        if avg_attn is not None:
            avg_attn.div_(self.models_size)

        return avg_probs, avg_attn, log_probs


    @torch.jit.export
    def reorder_encoder_out(
        self, encoder_outs: Optional[List[Dict[str, List[Tensor]]]], new_order
    ):
        """
        Reorder encoder output according to *new_order*.

        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order

        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        new_outs: List[Dict[str, List[Tensor]]] = []
        if not self.has_encoder():
            return new_outs
        for i, model in enumerate(self.models):
            if len(config.model_seeds) > 0:
                torch.manual_seed(config.model_seeds[i])

            assert encoder_outs is not None
            new_outs.append(
                model.encoder.reorder_encoder_out(encoder_outs[i], new_order)
            )
        return new_outs

    @torch.jit.export
    def reorder_incremental_state(
        self,
        incremental_states: List[Dict[str, Dict[str, Optional[Tensor]]]],
        new_order,
    ):
        if not self.has_incremental_states():
            return
        for i, model in enumerate(self.models):
            if len(config.model_seeds) > 0:
                torch.manual_seed(config.model_seeds[i])

            model.decoder.reorder_incremental_state_scripting(
                incremental_states[i], new_order
            )


class SequenceGeneratorWithAlignment(SequenceGenerator):
    def __init__(
        self, models, tgt_dict, left_pad_target=False, print_alignment="hard", **kwargs
    ):
        """Generates translations of a given source sentence.

        Produces alignments following "Jointly Learning to Align and
        Translate with Transformer Models" (Garg et al., EMNLP 2019).

        Args:
            left_pad_target (bool, optional): Whether or not the
                hypothesis should be left padded or not when they are
                teacher forced for generating alignments.
        """
        super().__init__(EnsembleModelWithAlignment(models), tgt_dict, **kwargs)
        self.left_pad_target = left_pad_target

        if print_alignment == "hard":
            self.extract_alignment = utils.extract_hard_alignment
        elif print_alignment == "soft":
            self.extract_alignment = utils.extract_soft_alignment

    @torch.no_grad()
    def generate(self, models, sample, **kwargs):
        finalized = super()._generate(sample, **kwargs)

        src_tokens = sample["net_input"]["src_tokens"]
        bsz = src_tokens.shape[0]
        beam_size = self.beam_size
        (
            src_tokens,
            src_lengths,
            prev_output_tokens,
            tgt_tokens,
        ) = self._prepare_batch_for_alignment(sample, finalized)
        if any(getattr(m, "full_context_alignment", False) for m in self.model.models):
            attn = self.model.forward_align(src_tokens, src_lengths, prev_output_tokens)
        else:
            attn = [
                finalized[i // beam_size][i % beam_size]["attention"].transpose(1, 0)
                for i in range(bsz * beam_size)
            ]

        if src_tokens.device != "cpu":
            src_tokens = src_tokens.to("cpu")
            tgt_tokens = tgt_tokens.to("cpu")
            attn = [i.to("cpu") for i in attn]

        # Process the attn matrix to extract hard alignments.
        for i in range(bsz * beam_size):
            alignment = self.extract_alignment(
                attn[i], src_tokens[i], tgt_tokens[i], self.pad, self.eos
            )
            finalized[i // beam_size][i % beam_size]["alignment"] = alignment
        return finalized

    def _prepare_batch_for_alignment(self, sample, hypothesis):
        src_tokens = sample["net_input"]["src_tokens"]
        bsz = src_tokens.shape[0]
        src_tokens = (
            src_tokens[:, None, :]
            .expand(-1, self.beam_size, -1)
            .contiguous()
            .view(bsz * self.beam_size, -1)
        )
        src_lengths = sample["net_input"]["src_lengths"]
        src_lengths = (
            src_lengths[:, None]
            .expand(-1, self.beam_size)
            .contiguous()
            .view(bsz * self.beam_size)
        )
        prev_output_tokens = data_utils.collate_tokens(
            [beam["tokens"] for example in hypothesis for beam in example],
            self.pad,
            self.eos,
            self.left_pad_target,
            move_eos_to_beginning=True,
        )
        tgt_tokens = data_utils.collate_tokens(
            [beam["tokens"] for example in hypothesis for beam in example],
            self.pad,
            self.eos,
            self.left_pad_target,
            move_eos_to_beginning=False,
        )
        return src_tokens, src_lengths, prev_output_tokens, tgt_tokens


class EnsembleModelWithAlignment(EnsembleModel):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__(models)

    def forward_align(self, src_tokens, src_lengths, prev_output_tokens):
        avg_attn = None
        for model in self.models:
            decoder_out = model(src_tokens, src_lengths, prev_output_tokens)
            attn = decoder_out[1]["attn"][0]
            if avg_attn is None:
                avg_attn = attn
            else:
                avg_attn.add_(attn)
        if len(self.models) > 1:
            avg_attn.div_(len(self.models))
        return avg_attn
