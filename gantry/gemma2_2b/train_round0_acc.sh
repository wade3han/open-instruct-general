#!/bin/bash
NUM_GPUS=4
BATCH_SIZE_PER_GPU=1
TOTAL_BATCH_SIZE=128
GRADIENT_ACC_STEPS=$(($TOTAL_BATCH_SIZE / $NUM_GPUS / $BATCH_SIZE_PER_GPU))
echo "Training llama model using $NUM_GPUS GPUs, $BATCH_SIZE_PER_GPU batch size per GPU, $GRADIENT_ACC_STEPS gradient accumulation steps"
# You can also set --gradient_checkpointing or use `stage3_offloading_accelerate.conf` to save memory,
# but it will trade off speed.
# sweep learning rate from 2e-5 to 1e-6
NAME=ds_gemma2_2b_batch1_seq2048_sum_wsd20_user_mask_lr5e-5_round0_no_multipack

gantry run --beaker-image seungjuh/open-instruct-public-240806-preview \
  --venv base \
  --name $NAME \
  --cluster ai2/pluto-cirrascale \
  --workspace ai2/safety \
  --pip requirements-gemma.txt \
  --gpus $NUM_GPUS \
  --priority high \
  --preemptible \
  --env-secret WANDB_API_KEY=WANDB_API_KEY \
  --env-secret HF_TOKEN=HUGGING_FACE_HUB_TOKEN \
  --env WANDB_PROJECT=llama2-finetuning \
  --env WANDB_ENTITY=seungjuhan3 \
  --env WANDB_NAME=$NAME \
  --env-secret OPENAI_API_KEY=openai_api_key \
  --budget ai2/oe-adapt -- accelerate launch \
  --mixed_precision bf16 \
  --num_machines 1 \
  --num_processes $NUM_GPUS \
  --use_deepspeed \
  --main_process_port 2950 \
  --deepspeed_config_file configs/ds_configs/stage2_accelerate.conf \
  open_instruct/finetune_accelerate.py \
  --use_compile \
  --mask_users \
  --eval_per_steps 20 \
  --model_name_or_path google/gemma-2-2b \
  --use_flash_attn \
  --tokenizer_name google/gemma-2-2b \
  --train_file /net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/round0_data.jsonl \
  --max_seq_length 2048 \
  --preprocessing_num_workers 128 \
  --per_device_train_batch_size $BATCH_SIZE_PER_GPU \
  --gradient_accumulation_steps $GRADIENT_ACC_STEPS \
  --learning_rate 5e-5 \
  --warmup_ratio 0.03 \
  --weight_decay 0. \
  --num_train_epochs 2 \
  --output_dir /results/$NAME \
  --with_tracking \
  --report_to wandb \
  --reduce_loss "sum" \
  --lr_scheduler_type "wsd" \
  --cooldown_ratio 0.2 \
  --gradient_checkpointing \
  --logging_steps 1
