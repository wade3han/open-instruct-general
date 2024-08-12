#!/bin/bash
NUM_GPUS=4
BATCH_SIZE_PER_GPU=1
TOTAL_BATCH_SIZE=128
GRADIENT_ACC_STEPS=$(($TOTAL_BATCH_SIZE / $NUM_GPUS / $BATCH_SIZE_PER_GPU))
echo "Training llama model using $NUM_GPUS GPUs, $BATCH_SIZE_PER_GPU batch size per GPU, $GRADIENT_ACC_STEPS gradient accumulation steps"
# You can also set --gradient_checkpointing or use `stage3_offloading_accelerate.conf` to save memory,
# but it will trade off speed.
# sweep learning rate from 2e-5 to 1e-6
RANK=256
BETA0=0.99
NAME=olmo1b_ladamw_rank${RANK}_beta${BETA0}_lr5e-5_seq2048

gantry run --beaker-image seungjuh/open-instruct-public-240806-preview --venv base --name $NAME --cluster ai2/pluto-cirrascale --workspace ai2/safety --pip requirements.txt --gpus 4 --priority high --preemptible --env-secret WANDB_API_KEY=WANDB_API_KEY --env-secret HF_TOKEN=HUGGING_FACE_HUB_TOKEN --env WANDB_PROJECT=llama2-finetuning --env WANDB_ENTITY=seungjuhan3 --env WANDB_NAME=$NAME --env-secret OPENAI_API_KEY=openai_api_key --budget ai2/oe-adapt -- \
  accelerate launch --mixed_precision bf16 \
  --num_machines 1 \
  --num_processes $NUM_GPUS \
  --main_process_port 2950 \
  open_instruct/exploration/finetune_accelerate_ladamw.py \
  --use_multipack \
  --use_compile \
  --mask_users \
  --add_bos \
  --use_slow_tokenizer False \
  --eval_per_steps 20 \
  --model_name_or_path allenai/OLMo-1B-hf \
  --use_flash_attn \
  --tokenizer_name allenai/OLMo-1B-hf \
  --train_file /net/nfs.cirrascale/mosaic/seungjuh/open-instruct/datasets/tulu-v2-sft-mixture_train.jsonl \
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
  --gradient_checkpointing \
  --reduce_loss "sum" \
  --lr_scheduler_type "wsd" \
  --cooldown_ratio 0.2 \
  --beta0 $BETA0 \
  --ladamw_rank $RANK \
  --clip_grad_norm 1.0 \
  --per_device_eval_batch_size 1 \
  --logging_steps 1
