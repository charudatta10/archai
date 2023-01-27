# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import argparse
import json

from transformers import (
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    GPT2Config,
    GPT2LMHeadModel,
    TrainingArguments,
)

from archai.datasets.nlp.hf_dataset_provider import HfHubDatasetProvider
from archai.datasets.nlp.hf_dataset_provider_utils import tokenize_contiguous_dataset
from archai.trainers.nlp.hf_trainer import HfTrainer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trains a Pareto architecture from Transformer-Flex.")

    parser.add_argument("pareto_config_path", type=str, help="Path to the Pareto architecture configuration file.")

    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()

    tokenizer = AutoTokenizer.from_pretrained("gpt2", model_max_length=192)
    tokenizer.pad_token = tokenizer.eos_token

    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    dataset_provider = HfHubDatasetProvider(dataset="wikitext", subset="wikitext-103-raw-v1")
    train_dataset = dataset_provider.get_train_dataset()
    eval_dataset = dataset_provider.get_val_dataset()

    encoded_train_dataset = train_dataset.map(
        tokenize_contiguous_dataset,
        batched=True,
        fn_kwargs={"tokenizer": tokenizer, "model_max_length": 192},
        remove_columns=train_dataset.column_names,
    )
    encoded_eval_dataset = eval_dataset.map(
        tokenize_contiguous_dataset,
        batched=True,
        fn_kwargs={"tokenizer": tokenizer, "model_max_length": 192},
        remove_columns=eval_dataset.column_names,
    )

    pareto_config = {}
    with open(args.pareto_config_path, "r") as f:
        pareto_config = json.load(f)

    config = GPT2Config(n_positions=192, bos_token_id=0, eos_token_id=0, **pareto_config)
    model = GPT2LMHeadModel(config=config)

    print(f"Total parameters: {sum(p.numel() for p in model.parameters())}")

    training_args = TrainingArguments(
        "hf-gpt2",
        evaluation_strategy="steps",
        logging_steps=10,
        eval_steps=125,
        per_device_train_batch_size=32,
        learning_rate=0.01,
        weight_decay=0.0,
        max_steps=250,
    )
    trainer = HfTrainer(
        model=model,
        args=training_args,
        data_collator=collator,
        train_dataset=encoded_train_dataset,
        eval_dataset=encoded_eval_dataset,
    )

    trainer.train()
