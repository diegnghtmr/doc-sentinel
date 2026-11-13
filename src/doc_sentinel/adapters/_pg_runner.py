"""Run the Prompt Guard 2 classifier from an existing local snapshot, never online.

LlamaFirewall's PromptGuard loader looks for HF_HOME/<model-name>, not Hub
snapshots; if missing it invokes login/download and writes a model copy. Loading
the same classifier with transformers and local_files_only avoids that path and
also lets us score every overlapping token window rather than silently truncate.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator


def _chunks(tokenizer, text: str, window: int = 512, overlap: int = 64) -> Iterator[list[int]]:
    size = window - tokenizer.num_special_tokens_to_add(pair=False)
    if size <= overlap or overlap < 0:
        raise ValueError("invalid window or overlap")
    tokens = tokenizer.encode(text, add_special_tokens=False)
    if not tokens:
        yield []
        return
    for start in range(0, len(tokens), size - overlap):
        yield tokens[start:start + size]
        if start + size >= len(tokens):
            break


def main() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    payload = json.load(sys.stdin)
    snapshot = payload["snapshot"]
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(snapshot, local_files_only=True)
    model.eval()
    scores = []
    for index, text in enumerate(payload["channels"]):
        for tokens in _chunks(tokenizer, text):
            inputs = tokenizer.prepare_for_model(tokens, add_special_tokens=True,
                                                 return_tensors="pt")
            with torch.no_grad():
                score = torch.softmax(model(**inputs).logits, dim=-1)[0, -1].item()
            scores.append({"index": index, "score": score,
                           "decision": "BLOCK" if score >= 0.9 else "ALLOW"})
    json.dump(scores, sys.stdout)


if __name__ == "__main__":
    main()
