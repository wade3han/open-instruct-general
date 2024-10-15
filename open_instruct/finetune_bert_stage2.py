import json
import os
import random
import fire
import torch
import wandb
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from torch.utils.data import DataLoader
from transformers import get_cosine_schedule_with_warmup
from tqdm import tqdm
from accelerate.utils import set_seed
from datasets import Dataset


# Load the IMDb dataset
def load_data(dataset_path: str) -> Dataset:
    # dataset_path is jsonl file.
    with open(dataset_path, "r") as f:
        data = [json.loads(line) for line in f]

    # split the data into train and test
    train_data = data

    formatted_train_data = []
    for item in train_data:
        # each item have statement, document, label.
        formatted_train_data.append(
            {
                "statement": item["statement"],
                "document": item["document"],
                "label": item["label"],
            }
        )

    return Dataset.from_list(formatted_train_data)


def train(
    dataset_path: str,
    model_name: str,
    model_path: str,
    lr: float = 1e-5,
    batch_size: int = 8,
    use_lora: bool = False,
):
    set_seed(42)
    # Load the tokenizer and model
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if use_lora:
        from peft import LoraConfig, get_peft_model

        lora_config = LoraConfig(
            r=128,
            task_type="SEQ_CLS",
            lora_alpha=512,
            lora_dropout=0.1,
            target_modules=[
                "query_proj",
                "key_proj",
                "value_proj",
                "attention.output.dense",
            ],  # for deberta-v3-large
            modules_to_save=["classifier", "pooler"],
        )
        model = AutoModelForSequenceClassification.from_pretrained(
            "microsoft/deberta-v3-large", num_labels=2
        )
        model = get_peft_model(model, lora_config)
        model.print_trainable_parameters()
    else:
        model = AutoModelForSequenceClassification.from_pretrained(
            model_path, num_labels=2
        )

    train_dataset = load_data(dataset_path)

    # Tokenization function
    def tokenize_function(example):
        statement = example["statement"]
        document = example["document"]
        text = tokenizer.eos_token.join([document, statement])
        return tokenizer(text, padding="max_length", truncation=True, max_length=512)

    # Tokenize the datasets
    tokenized_train_dataset = train_dataset.map(tokenize_function)

    # Set the format for PyTorch
    tokenized_train_dataset.set_format(
        "torch", columns=["input_ids", "attention_mask", "label"]
    )  # label 1 is SUPPORTED, label 0 is NOT_SUPPORTED

    # Create DataLoaders
    train_loader = DataLoader(
        tokenized_train_dataset, batch_size=batch_size, shuffle=True
    )

    # Set up the optimizer and scheduler
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=lr,
    )  # 1e-5 for the RoBERTa-large
    # load optimizer state
    if os.path.exists(f"{model_path}/optimizer.pt"):
        state_dict = torch.load(
            f"{model_path}/optimizer.pt", map_location="cpu", weights_only=True
        )
        optimizer.load_state_dict(state_dict)
        print(f"Optimizer state loaded from {model_path}/optimizer.pt")

    epochs = 1
    total_steps = len(train_loader) * epochs
    num_warmup_steps = int(0.03 * total_steps)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=total_steps
    )

    # Move model to device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)

    if os.path.exists(f"{model_path}/optimizer.pt"):
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)

    wandb.init(project="fact_verifier_small", entity="seungjuhan3", name=model_name)

    # Training loop
    training_step = 0
    accumulated_loss = 0
    loss_count = 0
    for epoch in range(epochs):
        loop = tqdm(train_loader, leave=True)
        for batch in loop:
            model.train()
            optimizer.zero_grad()
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["label"].to(device)

            outputs = model(
                input_ids=input_ids, attention_mask=attention_mask, labels=labels
            )
            loss = outputs.loss
            loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            training_step += 1
            loss_count += 1
            accumulated_loss += loss.item()
            optimizer.step()
            scheduler.step()

            loop.set_description(f"Epoch {epoch + 1}")
            loop.set_postfix(loss=accumulated_loss / loss_count)

            # log the loss to wandb
            if training_step % 100 == 0:
                lr = optimizer.param_groups[0]["lr"]
                wandb.log(
                    {
                        "loss": accumulated_loss / loss_count,
                        "lr": lr,
                        "grad_norm": grad_norm,
                    },
                    step=training_step,
                )
                accumulated_loss = 0
                loss_count = 0

    # Save the fine-tuned model and tokenizer
    output_dir = f"./finetuned_deberta_{model_name}"

    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)

    print(f"Model and tokenizer saved to {output_dir}")


if __name__ == "__main__":
    fire.Fire(train)
