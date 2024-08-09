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
import math
import os
import random
import time
from datetime import timedelta
from functools import partial

import datasets
import deepspeed
import torch
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import InitProcessGroupKwargs, set_seed
from datasets import load_dataset
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import DataLoader, RandomSampler
from tqdm.auto import tqdm
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    DataCollatorForSeq2Seq,
    GPT2Tokenizer,
    GPTNeoXTokenizerFast,
    LlamaTokenizer,
    LlamaTokenizerFast,
    OPTForCausalLM,
    get_scheduler,
)

from open_instruct.ladamw import LAdamW
from open_instruct.multipack import V2BatchSamplerDataCollatorForSeq2Seq, MultipackBatchSampler, get_dataset_lengths, \
    patch_for_multipack, SUPPORTED_MULTIPACK_MODEL_TYPES, V2BatchSamplerDataCollatorForSeq2SeqPadding
from open_instruct.utils import ArgumentParserPlus, FlatArguments, MFUEstimator
from open_instruct.wsd_scheduler import get_constant_schedule_with_warmup_and_cooldown

logger = get_logger(__name__)

# The flag below controls whether to allow TF32 on matmul. This flag defaults to False
# in PyTorch 1.12 and later.
torch.backends.cuda.matmul.allow_tf32 = True
# The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.backends.cudnn.allow_tf32 = True

EVAL_MAX_SEQ_LENGTH = 8192
EVAL_BATCH_SIZE = 8


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


def save_with_accelerate(accelerator, model, tokenizer, output_dir, args):
    # set the generation config to an empty setting to be safe.
    # we usually do greedy decoding for generation, so this should be okay.
    # otherwise, we get an error thrown at save time.
    if accelerator.is_main_process:
        tokenizer.save_pretrained(output_dir)

    model.generation_config = transformers.GenerationConfig(
        temperature=None, top_p=None, eos_token_id=tokenizer.eos_token_id, bos_token_id=tokenizer.bos_token_id
    )

    unwrapped_model = accelerator.unwrap_model(model)
    # When doing multi-gpu training, we need to use accelerator.get_state_dict(model) to get the state_dict.
    # Otherwise, sometimes the model will be saved with only part of the parameters.
    # Also, accelerator needs to use the wrapped model to get the state_dict.
    state_dict = accelerator.get_state_dict(getattr(model, '_orig_mod', model))
    if args.use_lora:
        # When using lora, the unwrapped model is a PeftModel, which doesn't support the is_main_process
        # and has its own save_pretrained function for only saving lora modules.
        # We have to manually specify the is_main_process outside the save_pretrained function.
        if accelerator.is_main_process:
            unwrapped_model.save_pretrained(output_dir, state_dict=state_dict)
    else:
        # don't use safetensors for saving for now
        unwrapped_model.save_pretrained(
            output_dir,
            is_main_process=accelerator.is_main_process,
            save_function=accelerator.save,
            state_dict=state_dict,
            safe_serialization=False,
        )


def test_model(args,
               model,
               test_data_loaders: list[DataLoader],
               test_data_loaders_names: list[str],
               accelerator,
               completed_steps: int,
               embedding_size: int,
               ):
    model.eval()
    total_eval_loss = 0
    DIVIDE_CONSTANT = EVAL_MAX_SEQ_LENGTH * EVAL_BATCH_SIZE
    loss_fct = torch.nn.CrossEntropyLoss(reduction="sum")
    with torch.no_grad():
        for test_data_loader, dataset_name in zip(test_data_loaders, test_data_loaders_names):
            eval_loss = 0
            loss_count = 0
            for eval_batch in test_data_loader:
                outputs = model(**eval_batch, use_cache=False)
                loss = outputs.loss
                # logits = outputs.logits
                # labels = eval_batch["labels"]
                # # Shift so that tokens < n predict n
                # shift_logits = logits[..., :-1, :].contiguous()
                # shift_labels = labels[..., 1:].contiguous()
                # shift_logits = shift_logits.view(-1, embedding_size)
                # shift_labels = shift_labels.view(-1)
                # # Enable model parallelism
                # shift_labels = shift_labels.to(shift_logits.device)
                # loss = loss_fct(shift_logits, shift_labels)
                # loss = loss / DIVIDE_CONSTANT
                eval_loss += loss
                loss_count += 1
            eval_loss = accelerator.gather(eval_loss).mean().item() / loss_count
            total_eval_loss += eval_loss
            logger.info(f"Eval loss for {dataset_name}: {eval_loss}")
            if args.with_tracking:
                accelerator.log({f"eval_loss_{dataset_name}": eval_loss}, step=completed_steps)
    total_eval_loss /= len(test_data_loaders)
    logger.info(f"Total eval loss: {total_eval_loss}")
    if args.with_tracking:
        accelerator.log({"eval_loss": total_eval_loss}, step=completed_steps)

    model.train()


def main():
    parser = ArgumentParserPlus((FlatArguments))
    args = parser.parse()

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment
    accelerator_log_kwargs = {}

    if args.with_tracking:
        accelerator_log_kwargs["log_with"] = args.report_to
        accelerator_log_kwargs["project_dir"] = args.output_dir

    # if you get timeouts (e.g. due to long tokenization) increase this.
    timeout_kwargs = InitProcessGroupKwargs(timeout=timedelta(seconds=args.timeout))

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        **accelerator_log_kwargs,
        kwargs_handlers=[timeout_kwargs],
    )
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    accelerator.wait_for_everyone()

    if accelerator.is_main_process:
        print(f"Arguments: {args}")

    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset(
            args.dataset_name,
            args.dataset_config_name,
        )
    else:
        data_files = {}
        dataset_args = {}
        if args.train_file is not None:
            data_files["train"] = args.train_file
        raw_datasets = load_dataset(
            "json",
            data_files=data_files,
            **dataset_args,
        )

    # Load pretrained model and tokenizer
    if args.config_name:
        config = AutoConfig.from_pretrained(
            args.config_name,
            trust_remote_code=args.trust_remote_code,
            revision=args.model_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=True,
        )
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(
            args.model_name_or_path,
            trust_remote_code=args.trust_remote_code,
            revision=args.model_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=True,
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
        logger.warn(warning)

    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer_name,
            trust_remote_code=args.trust_remote_code,
            use_fast=not args.use_slow_tokenizer,
            revision=tokenizer_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=True,
        )
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path,
            trust_remote_code=args.trust_remote_code,
            use_fast=not args.use_slow_tokenizer,
            revision=tokenizer_revision,
            token=os.getenv("HF_TOKEN", None),
            force_download=True,
        )
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    if args.model_name_or_path:
        if args.use_qlora:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
            )
            device_index = accelerator.local_process_index
            device_map = {"": device_index}  # force data-parallel training.
            model = AutoModelForCausalLM.from_pretrained(
                args.model_name_or_path,
                from_tf=bool(".ckpt" in args.model_name_or_path),
                config=config,
                load_in_4bit=True,
                quantization_config=bnb_config,
                device_map=device_map,
                trust_remote_code=args.trust_remote_code,
                torch_dtype=torch.bfloat16,
                use_flash_attention_2=True if args.use_flash_attn else False,
                revision=args.model_revision,
                token=os.getenv("HF_TOKEN", None),
                force_download=True,
            )
        else:
            model = AutoModelForCausalLM.from_pretrained(
                args.model_name_or_path,
                from_tf=bool(".ckpt" in args.model_name_or_path),
                config=config,
                trust_remote_code=args.trust_remote_code,
                low_cpu_mem_usage=args.low_cpu_mem_usage,
                use_flash_attention_2=True if args.use_flash_attn else False,
                revision=args.model_revision,
                token=os.getenv("HF_TOKEN", None),
                force_download=True,
            )
    else:
        logger.info("Training new model from scratch")
        model = AutoModelForCausalLM.from_config(config)

    if args.use_compile:
        model = torch.compile(model)

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

    # We resize the embeddings only when necessary to avoid index errors. If you are creating a model from scratch
    # gather deepspeed to get "real" embedding size
    embeddings = model.get_input_embeddings()
    with deepspeed.zero.GatheredParameters(embeddings.weight, modifier_rank=None):
        embedding_size = embeddings.weight.shape[0]
    # resize does its own gather
    if len(tokenizer) > embedding_size:
        # pad to multiple for tensor cores.
        model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8)
    # update embedding size after resizing for sum loss
    embeddings = model.get_input_embeddings()
    with deepspeed.zero.GatheredParameters(embeddings.weight, modifier_rank=None):
        embedding_size = embeddings.weight.shape[0]

    # set the tokenizer chat template to the tulu format
    # this makes evaluation/etc easier down the line.
    tokenizer.chat_template = "{% for message in messages %}\n{% if message['role'] == 'user' %}\n{{ '<|user|>\n' + message['content'] }}\n{% elif message['role'] == 'assistant' %}\n{{ '<|assistant|>\n'  + message['content'] + eos_token }}\n{% endif %}\n{% if loop.last and add_generation_prompt %}\n{{ '<|assistant|>' }}\n{% endif %}\n{% endfor %}"  # noqa: E501
    if args.add_bos:
        # also add bos in the chat template
        tokenizer.chat_template = "{{ bos_token }}" + tokenizer.chat_template

    if args.use_lora:
        if args.use_qlora:
            model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=args.gradient_checkpointing)

        logger.info("Initializing LORA model...")
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=args.lora_rank,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            target_modules=["q_proj", "o_proj", "v_proj", "k_proj", "gate_proj", "up_proj", "down_proj"],
        )
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
    elif args.gradient_checkpointing:
        model.gradient_checkpointing_enable()

    # Preprocessing the datasets.
    if "prompt" in raw_datasets["train"].column_names and "completion" in raw_datasets["train"].column_names:
        encode_function = partial(
            encode_with_prompt_completion_format,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            add_bos=args.add_bos,
        )
    elif "messages" in raw_datasets["train"].column_names:
        encode_function = partial(
            encode_with_messages_format,
            mask_users=args.mask_users,
            mask_padding=args.mask_padding,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            add_bos=args.add_bos,
        )
    else:
        raise ValueError("You need to have either 'prompt'&'completion' or 'messages' in your column names.")

    with accelerator.main_process_first():
        lm_datasets = raw_datasets.map(
            encode_function,
            batched=False,
            num_proc=args.preprocessing_num_workers,
            load_from_cache_file=not args.overwrite_cache,
            remove_columns=[
                name
                for name in raw_datasets["train"].column_names
                if name not in ["input_ids", "labels", "attention_mask"]
            ],
            desc="Tokenizing and reformatting instruction data",
        )

        if args.use_multipack:
            def add_position_ids(sample):
                sample_len = len(sample["input_ids"])
                sample["position_ids"] = torch.arange(len(sample["input_ids"]))
                sample["length"] = sample_len
                return sample

            lm_datasets = lm_datasets.map(
                add_position_ids,
                desc="Add position_id column (Pretraining Sample Packing)",
            )

        lm_datasets.set_format(type="pt")
        lm_datasets = lm_datasets.filter(lambda example: (example["labels"] != -100).any())

    with accelerator.main_process_first():
        # TEST_DATASET_PATH = "/net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/tulu-v2-sft-mixture-held-out.jsonl"
        TEST_DATASET_DIR = "/net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/"
        selected_validation_dataset_names = [
            "lmsyschat",
            "tulu2mix-code_alpaca",
            "tulu2mix-cot",
            "tulu2mix-flan_v2",
            "tulu2mix-gpt4_alpaca",
            "tulu2mix-oasst1",
            "tulu2mix-open_orca",
            "tulu2mix-science",
            "tulu2mix-sharegpt",
            "tulu2mix-wizardlm",
            "ultrachat",
            "ultrainteract",
            "wildchat-gpt-4-0125-preview",
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
            lm_datasets_tests.append(lm_datasets_test)

    train_dataset = lm_datasets["train"]
    test_datasets = [lm_datasets_test["test"] for lm_datasets_test in lm_datasets_tests]

    # debugging tool for fewer samples
    if args.max_train_samples is not None:
        max_train_samples = min(len(train_dataset), args.max_train_samples)
        logger.info(f"Limiting training samples to {max_train_samples} from {len(train_dataset)}.")
        train_dataset = train_dataset.select(range(max_train_samples))

    # Log a few random samples from the training set:
    for index in random.sample(range(len(train_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # DataLoaders creation:
    if args.use_multipack:
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

        batch_max_len = args.per_device_train_batch_size * args.max_seq_length
        batch_size = 1

        sampler = MultipackBatchSampler(
            RandomSampler(train_dataset),
            lengths=get_dataset_lengths(train_dataset),
            packing_efficiency_estimate=1.0,
            batch_max_len=batch_max_len,
            batch_size=batch_size,
            drop_last=True,
        )

        if args.use_compile:
            collate_fn = V2BatchSamplerDataCollatorForSeq2SeqPadding(
                tokenizer=tokenizer,
                model=model,
                padding="longest",
                max_length=batch_max_len,
            )
        else:
            collate_fn = V2BatchSamplerDataCollatorForSeq2Seq(
                tokenizer=tokenizer,
                model=model,
                padding="longest",
            )

        train_dataloader = DataLoader(
            train_dataset,
            batch_sampler=sampler,
            collate_fn=collate_fn,
        )

        # for data in train_dataloader:
        #     break
        # input_ids, attention_mask, labels = data["input_ids"], data["attention_mask"], data["labels"]
        # from open_instruct.multipack import get_unpad_data
        # indices, cu_len, max_seq_len = get_unpad_data(attention_mask)

        accelerator.state.deepspeed_plugin.deepspeed_config[
            'train_micro_batch_size_per_gpu'] = batch_size
        accelerator.even_batches = False

        # monkeypatch
        if args.use_flash_attn:
            patch_for_multipack(config.model_type, model_name=config._name_or_path)

    else:
        train_dataloader = DataLoader(
            train_dataset,
            shuffle=True,
            collate_fn=DataCollatorForSeq2Seq(tokenizer=tokenizer, model=model, padding="longest"),
            batch_size=args.per_device_train_batch_size,
        )

    test_data_loaders = [
        DataLoader(
            test_dataset,
            shuffle=False,
            collate_fn=DataCollatorForSeq2Seq(tokenizer=tokenizer, model=model, padding="longest"),
            batch_size=EVAL_BATCH_SIZE,
        )
        for test_dataset in test_datasets
    ]

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
    # if args.use_qlora:
    #     from bitsandbytes.optim import AdamW
    #
    #     optimizer = AdamW(
    #         optimizer_grouped_parameters,
    #         lr=args.learning_rate,
    #         optim_bits=8 if args.use_8bit_optimizer else 32,
    #         is_paged=True,
    #     )
    # else:
    #     optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
    optimizer = LAdamW(optimizer_grouped_parameters, lr=args.learning_rate,
                       betas=(args.beta0, args.beta1, args.beta2),
                       rank=args.ladamw_rank, )

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    # Create the learning rate scheduler.
    # Note: the current accelerator.step() calls the .step() of the real scheduler
    # for the `num_processes` times. This is because they assume
    # the user initialize the scheduler with the entire training set.
    # In the case of data parallel training, each process only
    # sees a subset (1/num_processes) of the training set.
    # So each time the process needs to update the lr multiple times so that the total
    # number of updates in the end matches the num_training_steps here.
    # Here we need to set the num_training_steps to either using
    # the entire training set (when epochs is specified) or we need to multiply the
    # num_training_steps by num_processes so that the total number
    # of updates matches the num_training_steps.
    num_training_steps_for_scheduler = (
        args.max_train_steps if overrode_max_train_steps else args.max_train_steps * accelerator.num_processes
    )
    if args.lr_scheduler_type == "wsd":
        num_cooldown_steps = int(num_training_steps_for_scheduler * args.cooldown_ratio)
        lr_scheduler = get_constant_schedule_with_warmup_and_cooldown(
            optimizer,
            num_warmup_steps=int(num_training_steps_for_scheduler * args.warmup_ratio),
            num_training_steps=num_training_steps_for_scheduler,
            num_cooldown_steps=num_cooldown_steps,
        )
    else:
        lr_scheduler = get_scheduler(
            name=args.lr_scheduler_type,
            optimizer=optimizer,
            num_training_steps=num_training_steps_for_scheduler,
            num_warmup_steps=int(num_training_steps_for_scheduler * args.warmup_ratio),
        )

    # Prepare everything with `accelerator`.
    model, optimizer, lr_scheduler, train_dataloader, *test_data_loaders = accelerator.prepare(
        model, optimizer, lr_scheduler, train_dataloader, *test_data_loaders,
    )

    # Load weights and states from a trained model, but not resuming.
    if args.load_from_checkpoint:
        accelerator.print(f"Loading from checkpoint: {args.load_from_checkpoint}")

        # monkey-patch load_state do just load model and optimizer.
        def load_state(accelerator, input_dir: str = None, **load_model_func_kwargs):
            import re
            from accelerate import DistributedType
            from accelerate.utils import load_fsdp_model

            MODEL_NAME = "pytorch_model"

            if input_dir is not None:
                # Check if folder exists
                input_dir = os.path.expanduser(input_dir)
                if not os.path.isdir(input_dir):
                    raise ValueError(f"Tried to find {input_dir} but folder does not exist")
            elif accelerator.project_configuration.automatic_checkpoint_naming:
                # Pick up from automatic checkpoint naming
                input_dir = os.path.join(accelerator.project_dir, "checkpoints")
                folders = [os.path.join(input_dir, folder) for folder in os.listdir(input_dir)]

                def _inner(folder):
                    return list(map(int, re.findall(r"[\/]?([0-9]+)(?=[^\/]*$)", folder)))[0]

                folders.sort(key=_inner)
                input_dir = folders[-1]
            else:
                raise ValueError("No input_dir provided and automatic checkpoint naming is disabled.")
            logger.info(f"Loading states from {input_dir}")

            for i, model in enumerate(accelerator._models):
                if accelerator.distributed_type == DistributedType.FSDP:
                    logger.info("Loading FSDP model")
                    load_fsdp_model(accelerator.state.fsdp_plugin, accelerator, model, input_dir, i)
                    logger.info(f"FSDP Model loaded from input dir {input_dir}")
                elif accelerator.distributed_type == DistributedType.DEEPSPEED:
                    logger.info("Loading DeepSpeed Model and Optimizer")
                    ckpt_id = f"{MODEL_NAME}" if i == 0 else f"{MODEL_NAME}_{i}"
                    model.load_checkpoint(input_dir, ckpt_id, **load_model_func_kwargs)
                    logger.info(
                        f"DeepSpeed Model and Optimizer loaded from input dir {os.path.join(input_dir, ckpt_id)}")
                elif accelerator.distributed_type == DistributedType.MEGATRON_LM:
                    logger.info("Loading Megatron-LM Model, Optimizer and Scheduler")
                    model.load_checkpoint(input_dir)
                    logger.info(f"Megatron-LM Model , Optimizer and Scheduler loaded from input dir {input_dir}")

        load_state(accelerator, args.load_from_checkpoint)

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # Figure out how many steps we should save the Accelerator states
    checkpointing_steps = args.checkpointing_steps
    if checkpointing_steps is not None and checkpointing_steps.isdigit():
        checkpointing_steps = int(checkpointing_steps)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"]
        accelerator.init_trackers(
            os.environ["WANDB_PROJECT"], experiment_config,
            init_kwargs={"wandb": {"entity": os.environ["WANDB_ENTITY"]}}
        )

    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0

    if args.resume_from_checkpoint:
        raise NotImplementedError("Should not use this.")
        # if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
        #     checkpoint_path = args.resume_from_checkpoint
        #     path = os.path.basename(args.resume_from_checkpoint)
        # else:
        #     # Get the most recent checkpoint
        #     dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
        #     dirs.sort(key=os.path.getctime)
        #     path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        #     checkpoint_path = path
        #     path = os.path.basename(checkpoint_path)
        #
        # accelerator.print(f"Resumed from checkpoint: {checkpoint_path}")
        # accelerator.load_state(path)
        # # Extract `epoch_{i}` or `step_{i}`
        # training_difference = os.path.splitext(path)[0]
        #
        # if "epoch" in training_difference:
        #     starting_epoch = int(training_difference.replace("epoch_", "")) + 1
        #     resume_step = None
        #     completed_steps = starting_epoch * num_update_steps_per_epoch
        # else:
        #     # need to multiply `gradient_accumulation_steps` to reflect real steps
        #     resume_step = int(training_difference.replace("step_", "")) * args.gradient_accumulation_steps
        #     starting_epoch = resume_step // len(train_dataloader)
        #     completed_steps = resume_step // args.gradient_accumulation_steps
        #     resume_step -= starting_epoch * len(train_dataloader)

    # update the progress_bar if load from checkpoint
    progress_bar.update(completed_steps)

    mfu_estimator = MFUEstimator(config.num_hidden_layers,
                                 config.num_attention_heads,
                                 config.hidden_size,
                                 model_num_params=12 * config.num_hidden_layers * (config.hidden_size ** 2))

    t0 = time.time()
    running_emfu = -1.0
    running_mfu = -1.0
    ignore_first_few_steps_num = 4
    effective_num_tokens_per_fwdbwd = 0
    seq_length_per_fwdbwd = 0
    _loss_quantiles = None  # only available when using below_median loss masking

    for epoch in range(starting_epoch, args.num_train_epochs):
        model.train()
        total_loss = 0
        # if args.resume_from_checkpoint and epoch == starting_epoch and resume_step is not None:
        #     # We skip the first `n` batches in the dataloader when resuming from a checkpoint
        #     active_dataloader = accelerator.skip_first_batches(train_dataloader, resume_step)
        # else:
        #     active_dataloader = train_dataloader
        active_dataloader = train_dataloader

        for step, batch in enumerate(active_dataloader):
            with accelerator.accumulate(model):
                outputs = model(**batch, use_cache=False)
                if args.reduce_loss == "mean":
                    assert args.loss_masking == "default", "mean loss only works with default loss masking"
                    loss = outputs.loss
                else:
                    # reduce loss is sum
                    # this ensures that we weight all tokens in the dataset equally,
                    # rather than weighting each overall example equally when
                    # using high amounts of gradient accumulation.
                    # this can result in > 5 point improvements in AlpacaEval
                    # see https://github.com/huggingface/transformers/issues/24725 for
                    # more discussion and details.
                    logits = outputs.logits
                    labels = batch["labels"]
                    # Shift so that tokens < n predict n
                    shift_logits = logits[..., :-1, :].contiguous()
                    shift_labels = labels[..., 1:].contiguous()
                    shift_logits = shift_logits.view(-1, embedding_size)
                    shift_labels = shift_labels.view(-1)
                    # Enable model parallelism
                    shift_labels = shift_labels.to(shift_logits.device)
                    if args.loss_masking == "below_median":
                        loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
                        loss = loss_fct(shift_logits, shift_labels)
                        # pick only loss where the value is > 0
                        loss = loss[shift_labels != -100]

                        # for stats, get the 10%, 25%, 50%, 75%, 90% quantiles
                        _loss_quantiles = torch.quantile(loss.cpu(), torch.tensor([0.1, 0.25, 0.5, 0.75, 0.9])).detach()

                        # get the median
                        loss_median = loss.median().detach()
                        # pick the loss that has a value higher than the median
                        loss = loss[loss > loss_median].sum()
                        loss = loss / (args.per_device_train_batch_size * args.max_seq_length)
                    elif args.loss_masking == "below_median_multiple":
                        loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
                        loss = loss_fct(shift_logits, shift_labels)
                        # pick only loss where the value is > 0
                        loss = loss[shift_labels != -100]

                        # for stats, get the 10%, 25%, 50%, 75%, 90% quantiles
                        _loss_quantiles = torch.quantile(loss.cpu(), torch.tensor([0.1, 0.25, 0.5, 0.75, 0.9])).detach()

                        # get the median
                        loss_median = loss.median().detach()
                        # pick the loss that has a value higher than the median
                        loss = loss[loss > loss_median].sum() * 2.0
                        loss = loss / (args.per_device_train_batch_size * args.max_seq_length)
                    elif args.loss_masking == "below_quantile_multiple":
                        loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
                        loss = loss_fct(shift_logits, shift_labels)
                        # pick only loss where the value is > 0
                        loss = loss[shift_labels != -100]

                        # for stats, get the 10%, 25%, 50%, 75%, 90% quantiles
                        _loss_quantiles = torch.quantile(loss.cpu(), torch.tensor([0.1, 0.25, 0.5, 0.75, 0.9])).detach()

                        # get the 25% quantile
                        loss_quantile = _loss_quantiles[3].detach()
                        # pick the loss that has a value higher than the quantile
                        loss = loss[loss > loss_quantile].sum() * 4.0
                        loss = loss / (args.per_device_train_batch_size * args.max_seq_length)
                    # elif args.loss_masking == "none":
                    #     loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
                    #     loss = loss_fct(shift_logits, shift_labels)
                    #     # pick only loss where the value is > 0
                    #     loss = loss[shift_labels != -100]
                    #     # get the mean
                    #     loss = loss.mean()
                    else:
                        # Flatten the tokens
                        loss_fct = torch.nn.CrossEntropyLoss(reduction="sum")
                        loss = loss_fct(shift_logits, shift_labels)
                        # We scale the loss based on the batch size and sequence length
                        loss = loss / (args.per_device_train_batch_size * args.max_seq_length)
                # We keep track of the loss at each logged step
                total_loss += loss.detach().float()
                accelerator.backward(loss)
                # clip gradient norm. don't do this with deepspeed
                if accelerator.sync_gradients and args.clip_grad_norm > 0:
                    accelerator.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
                total_norm = model.get_global_grad_norm()
                if hasattr(total_norm, "item"):
                    total_norm = total_norm.item()
                optimizer.step()
                optimizer.zero_grad()
                lr_scheduler.step()

                seq_length_per_fwdbwd += batch["labels"].shape[-1]
                effective_num_tokens_per_fwdbwd += (batch["labels"] != -100).detach().sum().item()

            if accelerator.sync_gradients:
                if completed_steps % args.eval_per_steps == 0 and completed_steps > 0:
                    test_model(args, model, test_data_loaders, selected_validation_dataset_names,
                               accelerator, completed_steps, embedding_size)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                completed_steps += 1

                t1 = time.time()
                dt = t1 - t0
                t0 = t1

                if ignore_first_few_steps_num > 0:
                    emfu = -1.0
                    mfu = -1.0
                    ignore_first_few_steps_num -= 1
                else:
                    emfu = mfu_estimator.estimate_mfu(effective_num_tokens_per_fwdbwd,
                                                      dt,
                                                      int(seq_length_per_fwdbwd / args.gradient_accumulation_steps))
                    mfu = mfu_estimator.estimate_mfu(seq_length_per_fwdbwd,
                                                     # seq_length_per_fwdbwd * args.per_device_train_batch_size,
                                                     dt,
                                                     int(seq_length_per_fwdbwd / args.gradient_accumulation_steps))
                effective_num_tokens_percentage = effective_num_tokens_per_fwdbwd / \
                                                  seq_length_per_fwdbwd \
                                                  * 100
                # (seq_length_per_fwdbwd * args.per_device_train_batch_size) \
                running_emfu = emfu if running_emfu == -1.0 else 0.9 * running_emfu + 0.1 * emfu
                running_mfu = mfu if running_mfu == -1.0 else 0.9 * running_mfu + 0.1 * mfu

                if args.logging_steps and completed_steps % args.logging_steps == 0:
                    avg_loss = (
                            accelerator.gather(total_loss).mean().item()
                            / args.gradient_accumulation_steps
                            / args.logging_steps
                    )
                    logger.info(f"  Step: {completed_steps}, LR: {lr_scheduler.get_last_lr()[0]}, Loss: {avg_loss},"
                                f" eMFU: {running_emfu * 100:.2f}%, MFU: {running_mfu * 100:.2f},"
                                f" Total Norm: {total_norm:.2f},"
                                f" Effective Num Tokens (%): {effective_num_tokens_percentage:.2f},"
                                # f" Effective Num Tokens Per Instance: {effective_num_tokens_per_fwdbwd / (args.per_device_train_batch_size * args.gradient_accumulation_steps):.2f}"
                                f" Effective Num Tokens Per Instance: {effective_num_tokens_per_fwdbwd / args.gradient_accumulation_steps:.2f}"
                                f" Seq Length: {seq_length_per_fwdbwd / args.gradient_accumulation_steps:.2f}")
                    if args.with_tracking:
                        accelerator.log(
                            {
                                "learning_rate": lr_scheduler.get_last_lr()[0],
                                "train_loss": avg_loss,
                                "total_norm": total_norm,
                                "eMFU (%)": running_emfu * 100,
                                "MFU (%)": running_mfu * 100,
                                "effective_num_tokens (%)": effective_num_tokens_percentage,
                                "effective_num_tokens_per_instance": effective_num_tokens_per_fwdbwd / (
                                        args.per_device_train_batch_size * args.gradient_accumulation_steps),
                                "seq_length": seq_length_per_fwdbwd / args.gradient_accumulation_steps,
                            },
                            step=completed_steps,
                        )
                        if _loss_quantiles is not None:
                            accelerator.log(
                                {
                                    "loss_quantiles (10%)": _loss_quantiles[0].item(),
                                    "loss_quantiles (25%)": _loss_quantiles[1].item(),
                                    "loss_quantiles (50%)": _loss_quantiles[2].item(),
                                    "loss_quantiles (75%)": _loss_quantiles[3].item(),
                                    "loss_quantiles (90%)": _loss_quantiles[4].item(),
                                },
                                step=completed_steps,
                            )
                    total_loss = 0

                seq_length_per_fwdbwd = 0
                effective_num_tokens_per_fwdbwd = 0

                if isinstance(checkpointing_steps, int):
                    if completed_steps % checkpointing_steps == 0:
                        output_dir = f"step_{completed_steps}"
                        if args.output_dir is not None:
                            output_dir = os.path.join(args.output_dir, output_dir)
                        save_with_accelerate(accelerator, model, tokenizer, output_dir, args)

                # if args.lr_scheduler_type == "wsd" and \
                #         completed_steps + int(args.max_train_steps * args.cooldown_ratio) == args.max_train_steps:
                #     # save the model before cooling down
                #     output_dir = f"step_{completed_steps}"
                #     if args.output_dir is not None:
                #         output_dir = os.path.join(args.output_dir, output_dir)
                #     save_with_accelerate(accelerator, model, tokenizer, output_dir, args)

                if completed_steps >= args.max_train_steps:
                    break

        if args.checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            save_with_accelerate(accelerator, model, tokenizer, output_dir, args)

    accelerator.wait_for_everyone()
    # last evaluation
    test_model(args, model, test_data_loaders, selected_validation_dataset_names,
               accelerator, completed_steps, embedding_size)

    if args.output_dir is not None:
        if accelerator.is_main_process:
            tokenizer.save_pretrained(args.output_dir)
        save_with_accelerate(accelerator, model, tokenizer, args.output_dir, args)

        if args.save_state:
            accelerator.save_state(args.output_dir)

    accelerator.wait_for_everyone()
    if args.with_tracking:
        accelerator.end_training()


if __name__ == "__main__":
    main()