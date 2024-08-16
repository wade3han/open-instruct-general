#!/bin/bash
NUM_GPUS=1
BATCH_SIZE_PER_GPU=1
TOTAL_BATCH_SIZE=16
GRADIENT_ACC_STEPS=$(($TOTAL_BATCH_SIZE / $NUM_GPUS / $BATCH_SIZE_PER_GPU))
echo "Training llama model using $NUM_GPUS GPUs, $BATCH_SIZE_PER_GPU batch size per GPU, $GRADIENT_ACC_STEPS gradient accumulation steps"
# You can also set --gradient_checkpointing or use `stage3_offloading_accelerate.conf` to save memory,
# but it will trade off speed.
# sweep learning rate from 2e-5 to 1e-6
NAME=debugs

export WANDB_ENTITY='seungjuhan3'
export WANDB_PROJECT='lora_olmo1b_selections'
export WANDB_NAME='reweighting-50k-warmup-1k'
python open_instruct/gradient/finetune_pure_mixture_faster.py \
  --use_multipack \
  --use_compile \
  --mask_users \
  --model_name_or_path allenai/OLMo-1B-hf \
  --use_flash_attn \
  --tokenizer_name allenai/OLMo-1B-hf \
  --train_file /net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/round0_data.jsonl \
  --max_seq_length 2048 \
  --preprocessing_num_workers 128 \
  --per_device_train_batch_size $BATCH_SIZE_PER_GPU \
  --gradient_accumulation_steps $GRADIENT_ACC_STEPS \
  --learning_rate 5e-5 \
  --add_bos \
  --use_slow_tokenizer False \
  --warmup_ratio 0.03 \
  --weight_decay 0. \
  --eval_per_steps 100 \
  --num_train_epochs 2 \
  --output_dir ./debug_results/$NAME \
  --reduce_loss "sum" \
  --lr_scheduler_type "wsd" \
  --cooldown_ratio 0.2 \
  --logging_steps 1 \
  --clip_grad_norm 1.0 \
  --max_train_samples 50000 \
  --with_tracking \
  --report_to wandb \
  --reweighting \
  --reweight_warmup_steps 1000 \
  --per_device_eval_batch_size 1
