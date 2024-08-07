# !/usr/bin/env python
# coding=utf-8
# Copyright 2024 AllenAI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import random
import time
from functools import partial

import datasets
import deepspeed
import numpy as np
import torch
import transformers
from datasets import load_dataset
from deepspeed import get_accelerator, DeepSpeedEngine
from deepspeed.utils import safe_get_full_grad
from safetensors.torch import save_file
from torch.utils.data import DataLoader, DistributedSampler
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    GPT2Tokenizer,
    GPTNeoXTokenizerFast,
    LlamaTokenizer,
    LlamaTokenizerFast,
    OPTForCausalLM, LlamaForCausalLM,
)

from open_instruct.multipack import SUPPORTED_MULTIPACK_MODEL_TYPES, MultipackBatchSampler, \
    V2BatchSamplerDataCollatorForSeq2SeqPadding, V2BatchSamplerDataCollatorForSeq2Seq, get_dataset_lengths, \
    patch_for_multipack
from open_instruct.utils import ArgumentParserPlus, FlatArguments

# The flag below controls whether to allow TF32 on matmul. This flag defaults to False
# in PyTorch 1.12 and later.
torch.backends.cuda.matmul.allow_tf32 = True
# The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.backends.cudnn.allow_tf32 = True

# hard-coded for now.
EVAL_MAX_SEQ_LENGTH = 8192
EVAL_BATCH_SIZE = 1
OFFLOAD = False
# zero_stage = 2
ZERO_STAGE = 3


def encode_with_prompt_completion_format(example, tokenizer, max_seq_length, add_bos=False):
    """
    Here we assume each example has 'prompt' and 'completion' fields.
    We concatenate prompt and completion and tokenize them together because otherwise prompt will be padded/trancated
    and it doesn't make sense to follow directly with the completion.
    """
    # if prompt doesn't end with space and completion doesn't start with space, add space
    if not example["prompt"].endswith((" ", "\n", "\t")) and not example["completion"].startswith((" ", "\n", "\t")):
        example_text = example["prompt"] + " " + example["completion"]
    else:
        example_text = example["prompt"] + example["completion"]
    example_text = example_text + tokenizer.eos_token
    if add_bos:
        example_text = tokenizer.bos_token + example_text
    tokenized_example = tokenizer(example_text, return_tensors="pt", max_length=max_seq_length, truncation=True)
    input_ids = tokenized_example.input_ids
    labels = input_ids.clone()
    tokenized_prompt = tokenizer(example["prompt"], return_tensors="pt", max_length=max_seq_length, truncation=True)
    # mask the prompt part for avoiding loss
    labels[:, : tokenized_prompt.input_ids.shape[1]] = -100
    attention_mask = torch.ones_like(input_ids)
    return {
        "input_ids": input_ids.flatten(),
        "labels": labels.flatten(),
        "attention_mask": attention_mask.flatten(),
    }


def encode_with_messages_format(example, tokenizer, max_seq_length, add_bos=False,
                                mask_users=True,
                                mask_padding=False, ):
    """
    Here we assume each example has a 'messages' field Each message is a dict with 'role' and 'content' fields.
    We concatenate all messages with the roles as delimiters and tokenize them together.
    """
    messages = example["messages"]
    if len(messages) == 0:
        raise ValueError("messages field is empty.")

    def _concat_messages(messages):
        message_text = ""
        for message in messages:
            if message["role"] == "system":
                message_text += "<|system|>\n" + message["content"].strip() + "\n"
            elif message["role"] == "user":
                message_text += "<|user|>\n" + message["content"].strip() + "\n"
            elif message["role"] == "assistant":
                message_text += "<|assistant|>\n" + message["content"].strip() + tokenizer.eos_token + "\n"
            else:
                raise ValueError("Invalid role: {}".format(message["role"]))
        return message_text

    example_text = _concat_messages(messages).strip()
    if add_bos:
        example_text = tokenizer.bos_token + example_text

    if not mask_padding:
        tokenized_example = tokenizer(example_text, return_tensors="pt", max_length=max_seq_length, truncation=True)
        input_ids = tokenized_example.input_ids
        labels = input_ids.clone()
    else:
        raise NotImplementedError("This is deprecated.")
        # tokenized_example = tokenizer(example_text, return_tensors="pt", max_length=max_seq_length, truncation=True,
        #                               padding="max_length")
        # input_ids = tokenized_example.input_ids
        # labels = input_ids.clone()
        # labels[labels == tokenizer.pad_token_id] = -100

    # mask the non-assistant part for avoiding loss
    if mask_users:
        for message_idx, message in enumerate(messages):
            if message["role"] != "assistant":
                if message_idx == 0:
                    message_start_idx = 0
                else:
                    message_start_idx = tokenizer(
                        _concat_messages(messages[:message_idx]),
                        return_tensors="pt",
                        max_length=max_seq_length,
                        truncation=True,
                    ).input_ids.shape[1]
                if message_idx < len(messages) - 1 and messages[message_idx + 1]["role"] == "assistant":
                    # here we also ignore the role of the assistant
                    messages_so_far = _concat_messages(messages[: message_idx + 1]) + "<|assistant|>\n"
                else:
                    messages_so_far = _concat_messages(messages[: message_idx + 1])
                message_end_idx = tokenizer(
                    messages_so_far, return_tensors="pt", max_length=max_seq_length, truncation=True
                ).input_ids.shape[1]
                labels[:, message_start_idx:message_end_idx] = -100

                if message_end_idx >= max_seq_length:
                    break

    attention_mask = torch.ones_like(input_ids)
    return {
        "input_ids": input_ids.flatten(),
        "labels": labels.flatten(),
        "attention_mask": attention_mask.flatten(),
    }


def measure_gradient(local_rank: int,
                     model_engine: DeepSpeedEngine,
                     optimizer,
                     test_data_loaders: list[DataLoader],
                     test_data_loaders_names: list[str],
                     device: torch.device,
                     output_dir: str,
                     ):
    for test_data_loader, dataset_name in zip(test_data_loaders, test_data_loaders_names):
        loss_count = 0
        grad_per_params = {}
        for i, eval_batch in enumerate(test_data_loader):
            start_time = time.time()
            eval_batch_device = {k: v.to(device) for k, v in eval_batch.items()}
            # print(
            #     f"[START] Rank {local_rank}: eval batch input_ids: {eval_batch['input_ids'][0, :20]}, {eval_batch['input_ids'].shape}")

            # print(model_engine.model.embed_tokens(eval_batch_device['input_ids']).shape)
            outputs = model_engine(**eval_batch_device, use_cache=False)
            loss = outputs.loss
            model_engine.backward(loss)
            batch_size = eval_batch['input_ids'].shape[0]
            loss_count += batch_size

            for n, p in model_engine.named_parameters():
                grad = safe_get_full_grad(p).detach().cpu()
                if n not in grad_per_params:
                    grad_per_params[n] = grad * batch_size
                else:
                    grad_per_params[n] += grad * batch_size

            # zero the gradients
            optimizer.zero_grad(set_to_none=True)
            if local_rank == 0:
                print(
                    f"Processed {loss_count}/{len(test_data_loader)} samples for {dataset_name}. Loss: {loss}. Spend {time.time() - start_time:.2f}s.")
            # print(
            #     f"[END] Rank {local_rank}: eval batch input_ids: {eval_batch['input_ids'][0, :20]}, {eval_batch['input_ids'].shape}")

        # get the average gradient norm for each parameter group
        for n in grad_per_params:
            grad_per_params[n] = grad_per_params[n] / loss_count

        # save the gradient norm for each parameter group
        if local_rank == 0:
            output_path = f"{output_dir}/{dataset_name}_gradient_norms.safetensors"
            save_file(grad_per_params, output_path)


def set_seed(seed=42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def main():
    parser = ArgumentParserPlus((FlatArguments))
    args = parser.parse()

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    datasets.utils.logging.set_verbosity_warning()
    transformers.utils.logging.set_verbosity_info()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    get_accelerator().set_device(args.local_rank)
    device = torch.device(get_accelerator().device_name(), args.local_rank)
    deepspeed.init_distributed()

    offload_device = "cpu" if OFFLOAD else "none"

    ds_config = {
        "train_micro_batch_size_per_gpu": EVAL_BATCH_SIZE,
        "train_batch_size": EVAL_BATCH_SIZE * int(os.environ["WORLD_SIZE"]),
        "zero_optimization": {
            "stage": ZERO_STAGE,
            "offload_param": {"device": offload_device},
            "offload_optimizer": {"device": offload_device},
            "stage3_param_persistence_threshold": 1e4,
            "stage3_max_live_parameters": 3e7,
            "stage3_prefetch_bucket_size": 3e7,
            "memory_efficient_linear": False,
        },
        "bfloat16": {"enabled": True},
        "gradient_clipping": 1.0,
    }

    torch.distributed.barrier()
    global_rank = torch.distributed.get_rank()

    if args.output_dir is not None and global_rank == 0:
        os.makedirs(args.output_dir, exist_ok=True)

    print(f"Arguments: {args}")

    # Load pretrained model and tokenizer
    if args.config_name:
        config = AutoConfig.from_pretrained(
            args.config_name,
            trust_remote_code=args.trust_remote_code,
            revision=args.model_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=False,
            attn_implementation="flash_attention_2" if args.use_flash_attn else None,
        )
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(
            args.model_name_or_path,
            trust_remote_code=args.trust_remote_code,
            revision=args.model_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=False,
            attn_implementation="flash_attention_2" if args.use_flash_attn else None,
        )
    else:
        raise ValueError(
            "You are instantiating a new config instance from scratch. This is not supported by this script."
        )

    tokenizer_revision = args.model_revision if args.tokenizer_revision is None else args.tokenizer_revision

    if tokenizer_revision != args.model_revision:
        # Warn user if tokenizer and model use different revisions; this is an unusual
        # use case.
        warning = f"""Requested tokenizer revision `{tokenizer_revision}` is different
                   from the model revision `{args.model_revision}`."""
        print(warning)

    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer_name,
            trust_remote_code=args.trust_remote_code,
            use_fast=not args.use_slow_tokenizer,
            revision=tokenizer_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=False,
        )
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path,
            trust_remote_code=args.trust_remote_code,
            use_fast=not args.use_slow_tokenizer,
            revision=tokenizer_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=False,
        )
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    assert args.model_name_or_path is not None, "You need to specify a model name or path"
    model_weights = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
        trust_remote_code=args.trust_remote_code,
        low_cpu_mem_usage=args.low_cpu_mem_usage,
        revision=args.model_revision,
        token=os.getenv("HF_TOKEN", None),
        force_download=False,
    ).state_dict()

    with deepspeed.zero.Init(enabled=(ZERO_STAGE == 3)):
        model = LlamaForCausalLM(config=config).cuda()
        model.load_state_dict(model_weights, strict=False)

    if args.gradient_checkpointing:
        model.gradient_checkpointing = True
        model._gradient_checkpointing_func = torch.utils.checkpoint.checkpoint
        for module in model.modules():
            if hasattr(module, "gradient_checkpointing"):
                module.gradient_checkpointing = True
                module._gradient_checkpointing_func = torch.utils.checkpoint.checkpoint
        # TODO: why deepspeed checkpointing doesn't work?
        # gradient_checkpointing_func = deepspeed.checkpointing.checkpoint
        # deepspeed.checkpointing.configure(mpu_=None)
        # model._gradient_checkpointing_func = gradient_checkpointing_func
        # model.gradient_checkpointing = True
        # for module in model.modules():
        #     if hasattr(module, "gradient_checkpointing"):
        #         module.gradient_checkpointing = True
        #         module._gradient_checkpointing_func = gradient_checkpointing_func

    model.train()

    if args.local_rank == 0:
        print(f"Is gradient checkpointing enabled?: {deepspeed.checkpointing.is_configured()}")

    # no default pad token for llama!
    # here we add all special tokens again, because the default ones are not in the special_tokens_map
    if isinstance(tokenizer, LlamaTokenizer) or isinstance(tokenizer, LlamaTokenizerFast):
        num_added_tokens = tokenizer.add_special_tokens(
            {
                "bos_token": "<s>",
                "eos_token": "</s>",
                "unk_token": "<unk>",
                "pad_token": "<pad>",
            }
        )
        assert num_added_tokens in [
            0,
            1,
        ], "LlamaTokenizer should only add one special token - the pad_token, or no tokens if pad token present."
    elif isinstance(tokenizer, GPTNeoXTokenizerFast):
        # OLMo newer models use this tokenizer
        if tokenizer.bos_token is None:
            tokenizer.bos_token = tokenizer.eos_token
            assert (
                args.add_bos
            ), "For OLMo with GPTNeoX, you must add bos token to the beginning of the input sequence."
        # else, pythia / other models
        else:
            num_added_tokens = tokenizer.add_special_tokens(
                {
                    "pad_token": "<pad>",
                }
            )
            assert num_added_tokens == 1, "GPTNeoXTokenizer should only add one special token - the pad_token."
    elif isinstance(tokenizer, GPT2Tokenizer) and isinstance(model, OPTForCausalLM):
        num_added_tokens = tokenizer.add_special_tokens({"unk_token": "<unk>"})
    elif isinstance(tokenizer, transformers.PreTrainedTokenizerFast) and tokenizer.pad_token is None:
        num_added_tokens = tokenizer.add_special_tokens({"pad_token": "<pad>"})
        assert num_added_tokens == 1, "We detected no padding token but add_special_tokens did not add one."

    # We resize the embeddings only when necessary to avoid index errors.
    embeddings = model.get_input_embeddings()
    embedding_size = embeddings.weight.shape[0]
    # resize does its own gather
    if len(tokenizer) > embedding_size:
        # pad to multiple for tensor cores.
        model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8)

    # set the tokenizer chat template to the tulu format
    # this makes evaluation/etc easier down the line.
    tokenizer.chat_template = "{% for message in messages %}\n{% if message['role'] == 'user' %}\n{{ '<|user|>\n' + message['content'] }}\n{% elif message['role'] == 'assistant' %}\n{{ '<|assistant|>\n'  + message['content'] + eos_token }}\n{% endif %}\n{% if loop.last and add_generation_prompt %}\n{{ '<|assistant|>' }}\n{% endif %}\n{% endfor %}"  # noqa: E501
    if args.add_bos:
        # also add bos in the chat template
        tokenizer.chat_template = "{{ bos_token }}" + tokenizer.chat_template

    TEST_DATASET_DIR = "/net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/"
    selected_validation_dataset_names = [
        "lmsyschat",
        # "tulu2mix-code_alpaca",
        # "tulu2mix-cot",
        # "tulu2mix-flan_v2",
        # "tulu2mix-gpt4_alpaca",
        # "tulu2mix-oasst1",
        # "tulu2mix-open_orca",
        # "tulu2mix-science",
        # "tulu2mix-sharegpt",
        # "tulu2mix-wizardlm",
        # "ultrachat",
        # "ultrainteract",
        # "wildchat-gpt-4-0125-preview",
    ]
    lm_datasets_tests = []
    for dataset_name in selected_validation_dataset_names:
        validation_datapath = f"{TEST_DATASET_DIR}/megamixv2_dedup_{dataset_name}_validation.jsonl"
        data_files = {"test": validation_datapath}
        raw_datasets_test = load_dataset(
            "json",
            data_files=data_files,
        )
        encode_function_mask_non_assistant = partial(
            encode_with_messages_format,
            tokenizer=tokenizer,
            mask_padding=False,
            mask_users=True,
            max_seq_length=EVAL_MAX_SEQ_LENGTH,  # HARD-CODED
            add_bos=args.add_bos,
        )
        lm_datasets_test = raw_datasets_test.map(
            encode_function_mask_non_assistant,
            batched=False,
            num_proc=args.preprocessing_num_workers,
            load_from_cache_file=not args.overwrite_cache,
            remove_columns=[name for name in raw_datasets_test["test"].column_names if
                            name not in ["input_ids", "labels", "attention_mask"]],
            desc="Tokenizing and reformatting instruction data",
        )
        lm_datasets_test.set_format(type="pt")
        lm_datasets_test = lm_datasets_test.filter(lambda example: (example["labels"] != -100).any())
        lm_datasets_tests.append(lm_datasets_test)

    test_datasets = [lm_datasets_test["test"] for lm_datasets_test in lm_datasets_tests]

    # DataLoaders creation:
    assert args.use_compile, "Multipack only works with compile. TODO: fix this."
    assert not args.mask_padding, "Mask padding is not supported with multipack."
    assert config.model_type in SUPPORTED_MULTIPACK_MODEL_TYPES, f"Model type {config.model_type} not supported."

    from torch.utils.data._utils.fetch import _BaseDatasetFetcher
    from torch.utils.data._utils.worker import _worker_loop

    class _MapDatasetFetcher(_BaseDatasetFetcher):
        def fetch(self, possibly_batched_index):
            if isinstance(possibly_batched_index[0], list):
                data = [None for i in possibly_batched_index]
                for i, possibly_batched_index_ in enumerate(possibly_batched_index):
                    if self.auto_collation:
                        if (
                                hasattr(self.dataset, "__getitems__")
                                and self.dataset.__getitems__
                        ):
                            data[i] = self.dataset.__getitems__(possibly_batched_index_)
                        else:
                            data[i] = [self.dataset[idx] for idx in possibly_batched_index_]
                    else:
                        data[i] = self.dataset[possibly_batched_index_]
            else:
                if self.auto_collation:
                    if hasattr(self.dataset, "__getitems__") and self.dataset.__getitems__:
                        data = self.dataset.__getitems__(possibly_batched_index)
                    else:
                        data = [self.dataset[idx] for idx in possibly_batched_index]
                else:
                    data = self.dataset[possibly_batched_index]
            return self.collate_fn(data)

    def patch_fetchers():
        torch.utils.data._utils.fetch._MapDatasetFetcher = _MapDatasetFetcher
        torch.utils.data.dataloader._utils.fetch._MapDatasetFetcher = _MapDatasetFetcher

    def patched_worker_loop(*args, **kwargs):
        patch_fetchers()
        return _worker_loop(*args, **kwargs)

    torch.utils.data._utils.worker._worker_loop = patched_worker_loop
    patch_fetchers()

    batch_max_len = EVAL_BATCH_SIZE * EVAL_MAX_SEQ_LENGTH
    batch_size = 1

    if args.use_compile:
        collate_fn = V2BatchSamplerDataCollatorForSeq2SeqPadding(
            tokenizer=tokenizer,
            model=model,
            padding="longest",
            max_length=batch_max_len,
        )
        model = torch.compile(model)
    else:
        collate_fn = V2BatchSamplerDataCollatorForSeq2Seq(
            tokenizer=tokenizer,
            model=model,
            padding="longest",
        )

    samplers = [MultipackBatchSampler(
        DistributedSampler(test_dataset, num_replicas=int(os.environ["WORLD_SIZE"]), rank=args.local_rank),
        lengths=get_dataset_lengths(test_dataset),
        packing_efficiency_estimate=1.0,
        batch_max_len=batch_max_len,
        batch_size=batch_size,
        drop_last=True,
    ) for test_dataset in test_datasets]

    test_data_loaders = [
        DataLoader(
            test_dataset,
            batch_sampler=sampler,
            collate_fn=collate_fn,
        )
        for test_dataset, sampler in zip(test_datasets, samplers)
    ]

    if args.use_flash_attn:
        patch_for_multipack(config.model_type, model_name=config._name_or_path)

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "layer_norm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = deepspeed.ops.adam.FusedAdam(optimizer_grouped_parameters, lr=args.learning_rate,
                                             weight_decay=0.0, )

    model_engine, optimizer, _, _ = deepspeed.initialize(model=model, optimizer=optimizer, config=ds_config)
    model_engine.train()

    measure_gradient(args.local_rank, model_engine, optimizer,
                     test_data_loaders, selected_validation_dataset_names, device,
                     args.output_dir)


if __name__ == "__main__":
    main()
