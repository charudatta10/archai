# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import argparse

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generates new tokens with a pre-trained model.")

    parser.add_argument("pre_trained_model_path", type=str, help="Path to the pre-trained model file.")

    parser.add_argument("hub_tokenizer_path", type=str, help="Path to the Hugging Face's Hub tokenizer.")

    parser.add_argument("prompt", type=str, help="Prompt to serve as the generation's context.")

    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()

    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"

    model = AutoModelForCausalLM.from_pretrained(args.pre_trained_model_path).to(device)
    model.config.use_cache = True

    tokenizer = AutoTokenizer.from_pretrained(args.hub_tokenizer_path)

    inputs = tokenizer(args.prompt, return_tensors="pt").to(device)
    outputs = model.generate(
        inputs["input_ids"],
        attention_mask=inputs["attention_mask"],
        pad_token_id=model.config.eos_token_id,
        do_sample=True,
        temperature=0.8,
        top_p=0.95,
        max_new_tokens=128,
    )

    print(f"Generated: \n{tokenizer.decode(outputs[0], skip_special_tokens=True)}")
