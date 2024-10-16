#!/bin/bash
NUM_GPUS=4
BATCH_SIZE_PER_GPU=4
TOTAL_BATCH_SIZE=16
GRADIENT_ACC_STEPS=$(($TOTAL_BATCH_SIZE / $NUM_GPUS / $BATCH_SIZE_PER_GPU))
echo "Training llama model using $NUM_GPUS GPUs, $BATCH_SIZE_PER_GPU batch size per GPU, $GRADIENT_ACC_STEPS gradient accumulation steps"
# You can also set --gradient_checkpointing or use `stage3_offloading_accelerate.conf` to save memory,
# but it will trade off speed.
# sweep learning rate from 2e-5 to 1e-6

name=internlm_v21_12k_anli_short_21k_decompose_and_verify
accelerate launch \
  --mixed_precision bf16 \
  --num_machines 1 \
  --num_processes $NUM_GPUS \
  --use_deepspeed \
  --deepspeed_config_file configs/ds_configs/stage3_no_offloading_accelerate.conf \
  open_instruct/finetune.py \
  --wandb_entity seungjuhan3 \
  --wandb_project fact_verifier \
  --wandb_name $name \
  --model_name_or_path internlm/internlm2_5-7b-chat \
  --tokenizer_name internlm/internlm2_5-7b-chat \
  --trust_remote_code \
  --use_slow_tokenizer \
  --train_file /home/ubuntu/v21_12k_anli_short_21k.jsonl \
  --max_seq_length 2048 \
  --per_device_train_batch_size $BATCH_SIZE_PER_GPU \
  --gradient_accumulation_steps $GRADIENT_ACC_STEPS \
  --learning_rate 1e-6 \
  --lr_scheduler_type linear \
  --warmup_ratio 0.03 \
  --weight_decay 0. \
  --num_train_epochs 1 \
  --output_dir $name \
  --report_to wandb \
  --eval_file /home/ubuntu/open-instruct-general/fact_verification_dev.jsonl \
  --eval_steps 800 \
  --gradient_checkpointing \
  --logging_steps 10 \
  --with_tracking