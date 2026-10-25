"""Conversation-history normalization helper (verbatim from the OpenAI Agents SDK template).

This app's UI sends only the NEW user message each turn (prior turns come from the managed
session), so in practice there's nothing to normalize — but MLflow evaluation replays full
histories, and this keeps id-less assistant items from crashing the SDK's input converter.
"""


def normalize_history_items(messages: list[dict]) -> list[dict]:
    """Collapse id-less replayed assistant items to the easy-input {"role","content"} form that
    every openai-agents version accepts. Items that carry an `id` are left untouched."""
    normalized: list[dict] = []
    for m in messages:
        if m.get("type") != "message" or m.get("role") != "assistant":
            normalized.append(m)
            continue
        content = m.get("content")
        if isinstance(content, str):
            normalized.append({"role": "assistant", "content": content})
        elif isinstance(content, list) and "id" not in m:
            text = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
            normalized.append({"role": "assistant", "content": text})
        else:
            normalized.append(m)
    return normalized
