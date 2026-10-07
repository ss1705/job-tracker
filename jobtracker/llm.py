"""Shared Groq JSON-chat helper: model fallback, daily-quota and per-minute rate-limit handling.

Used by the email parser (temperature 0, low reasoning effort) and the outreach drafter (a little more
creative). Raises LLMExhausted when every configured model has used its daily token quota.
"""
import json
import re
import time

from . import config

_client = None
last_model: str = ""            # model that answered the most recent call
_exhausted: set[str] = set()      # models whose daily token quota is gone (for this process)


class LLMExhausted(Exception):
    """Every configured Groq model has hit its daily token limit."""


def available() -> bool:
    return bool(config.GROQ_API_KEY)


def _retry_after_seconds(msg: str) -> float:
    m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)s", msg)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 30.0


def chat_json(prompt: str, *, temperature: float = 0.0, effort: str = "low", label: str = "llm", max_tokens: int | None = None) -> dict | None:
    """Returns the parsed JSON object, None on a non-quota failure, and raises LLMExhausted when no model has
    daily quota left."""
    global _client, last_model
    if not available():
        return None
    from groq import Groq, RateLimitError
    _client = _client or Groq(api_key=config.GROQ_API_KEY)
    everyone = [config.GROQ_MODEL, *config.GROQ_FALLBACK_MODELS]
    models = [m for m in everyone if m not in _exhausted]
    if not models:
        raise LLMExhausted("Groq daily token limit reached on all configured models; progress is saved, resume later.")
    for model in models:
        kwargs = {"reasoning_effort": effort} if model.startswith("openai/gpt-oss") else {}
        if max_tokens:
            kwargs["max_completion_tokens"] = max_tokens        # reasoning models can otherwise spend the default budget and return nothing
        for attempt in range(2):
            try:
                resp = _client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}, temperature=temperature, **kwargs)
                last_model = model
                return json.loads(resp.choices[0].message.content)
            except RateLimitError as e:
                msg = str(e)
                if "per day" in msg or "TPD" in msg:
                    print(f"[{label}] {model}: daily token limit reached, trying next model")
                    _exhausted.add(model)
                    break
                wait = _retry_after_seconds(msg)           # per-minute limit: wait it out once
                if attempt == 0 and wait <= 90:
                    time.sleep(wait + 1)
                    continue
                break
            except Exception as e:                           # bad JSON, network, model unavailable
                print(f"[{label}] LLM call failed on {model}: {e}")
                break
    if all(m in _exhausted for m in everyone):
        raise LLMExhausted("Groq daily token limit reached on all configured models; progress is saved, resume later.")
    return None
