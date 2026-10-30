"""The provider switch: the only module that knows which LLM we are talking to.

Everything else in the agent is provider-agnostic. Nothing outside this file
should branch on `provider`, because the moment it does, adding a provider means
touching the loop -- and the loop is the part we want students reading, not
editing.

WHY A SWITCH AT ALL. It separates two failures that look identical from the
outside: "the agent reasons badly" and "this model cannot call tools". When a run
goes wrong, re-running it against a known-good hosted model tells you
immediately which one you are looking at. Without that, you can lose a day
debugging a correct loop.

"OpenAI-compatible" is a wire format, not a vendor. vLLM, llama.cpp, Ollama and
several hosted APIs all speak it, so one client library reaches all of them --
which is exactly why we did not need a framework for provider abstraction.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI

from ..config import LLMConfig


@dataclass
class Provider:
    client: AsyncOpenAI
    model: str
    temperature: float
    max_tokens: int


def build(cfg: LLMConfig) -> Provider:
    """Construct the client for the selected provider block."""
    chosen = cfg.selected()

    if not chosen.api_key:
        # Interpolation of ${VAR} yields "" when the variable is unset, so this
        # is where a missing key surfaces -- at startup, naming the provider,
        # rather than as a 401 several seconds into the first call.
        raise RuntimeError(
            f"provider {cfg.provider!r} has an empty api_key. "
            "Set the environment variable referenced in config.yaml. "
            "(Local servers ignore the value, but the SDK requires a non-empty "
            "string -- see the vllm block for the placeholder pattern.)"
        )

    return Provider(
        client=AsyncOpenAI(api_key=chosen.api_key, base_url=chosen.base_url),
        model=chosen.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
