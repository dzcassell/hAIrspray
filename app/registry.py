"""Target registry.

Builds the list of ``Provider`` instances the daemon will pick from.
Grouped by category so the operator can disable categories via the
``CATEGORIES`` env var.

All targets are legitimate public AI services. API probes send realistic
SDK-shaped JSON bodies but intentionally fail auth (Cato identifies the
application regardless; the 401/403 is expected).
"""
from __future__ import annotations

from typing import Any

from .providers import (
    ApiProbe,
    DuckDuckGoChat,
    PollinationsImage,
    PollinationsText,
    Provider,
    WebProbe,
)


# ---------------------------------------------------------------------------
# Category: llm_api — major LLM vendor API endpoints
# ---------------------------------------------------------------------------

def _llm_api_probes() -> list[Provider]:
    def openai_body(p: str) -> dict[str, Any]:
        return {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": p}],
            "temperature": 0.7,
        }

    def anthropic_body(p: str) -> dict[str, Any]:
        return {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": p}],
        }

    def gemini_body(p: str) -> dict[str, Any]:
        return {"contents": [{"parts": [{"text": p}]}]}

    def mistral_body(p: str) -> dict[str, Any]:
        return {
            "model": "mistral-small-latest",
            "messages": [{"role": "user", "content": p}],
        }

    def cohere_body(p: str) -> dict[str, Any]:
        return {
            "model": "command-r",
            "messages": [{"role": "user", "content": p}],
        }

    def groq_body(p: str) -> dict[str, Any]:
        return {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": p}],
        }

    def perplexity_body(p: str) -> dict[str, Any]:
        return {
            "model": "sonar",
            "messages": [{"role": "user", "content": p}],
        }

    def deepseek_body(p: str) -> dict[str, Any]:
        return {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": p}],
        }

    def xai_body(p: str) -> dict[str, Any]:
        return {
            "model": "grok-2-latest",
            "messages": [{"role": "user", "content": p}],
        }

    def together_body(p: str) -> dict[str, Any]:
        return {
            "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "messages": [{"role": "user", "content": p}],
        }

    return [
        ApiProbe(
            name="OpenAI",
            url="https://api.openai.com/v1/chat/completions",
            user_agent="OpenAI/Python 1.51.0",
            body_builder=openai_body,
        ),
        ApiProbe(
            name="Anthropic",
            url="https://api.anthropic.com/v1/messages",
            user_agent="anthropic-python/0.39.0",
            body_builder=anthropic_body,
            extra_headers={"anthropic-version": "2023-06-01"},
            send_fake_auth=False,
        ),
        ApiProbe(
            name="Google-Gemini",
            url=("https://generativelanguage.googleapis.com/v1beta/models/"
                 "gemini-1.5-flash:generateContent"),
            user_agent="google-generativeai/0.8.3 gl-python/3.12",
            body_builder=gemini_body,
            send_fake_auth=False,
        ),
        ApiProbe(
            name="Mistral",
            url="https://api.mistral.ai/v1/chat/completions",
            user_agent="mistralai/1.2.0",
            body_builder=mistral_body,
        ),
        ApiProbe(
            name="Cohere",
            url="https://api.cohere.com/v2/chat",
            user_agent="cohere-python/5.11.0",
            body_builder=cohere_body,
        ),
        ApiProbe(
            name="Groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            user_agent="groq/0.13.0",
            body_builder=groq_body,
        ),
        ApiProbe(
            name="Perplexity-API",
            url="https://api.perplexity.ai/chat/completions",
            user_agent="perplexity-python/0.1.0",
            body_builder=perplexity_body,
        ),
        ApiProbe(
            name="DeepSeek-API",
            url="https://api.deepseek.com/chat/completions",
            user_agent="OpenAI/Python 1.51.0",
            body_builder=deepseek_body,
        ),
        ApiProbe(
            name="xAI-Grok",
            url="https://api.x.ai/v1/chat/completions",
            user_agent="xai-sdk/0.1.0",
            body_builder=xai_body,
        ),
        ApiProbe(
            name="TogetherAI",
            url="https://api.together.xyz/v1/chat/completions",
            user_agent="together-python/1.3.3",
            body_builder=together_body,
        ),
    ]


# ---------------------------------------------------------------------------
# Category: chatbot_ui — consumer chatbot front-ends
# ---------------------------------------------------------------------------

_CHATBOT_UIS: list[tuple[str, str]] = [
    ("ChatGPT",              "https://chatgpt.com/"),
    ("Claude.ai",            "https://claude.ai/"),
    ("Google-Gemini-Web",    "https://gemini.google.com/"),
    ("Perplexity-Web",       "https://www.perplexity.ai/"),
    ("Poe",                  "https://poe.com/"),
    ("Character.AI",         "https://character.ai/"),
    ("Mistral-Chat",         "https://chat.mistral.ai/"),
    ("DeepSeek-Chat",        "https://chat.deepseek.com/"),
    ("Microsoft-Copilot",    "https://copilot.microsoft.com/"),
    ("Meta-AI",              "https://www.meta.ai/"),
    ("Grok-Web",             "https://grok.com/"),
    ("You.com",              "https://you.com/"),
    ("HuggingChat",          "https://huggingface.co/chat/"),
    ("Kimi",                 "https://kimi.moonshot.cn/"),
]


def _chatbot_ui_visits() -> list[Provider]:
    return [WebProbe(name, url, category="chatbot_ui") for name, url in _CHATBOT_UIS]


# ---------------------------------------------------------------------------
# Category: media_gen — image / video / audio generation tools
# ---------------------------------------------------------------------------

_MEDIA_GEN: list[tuple[str, str]] = [
    ("Midjourney",    "https://www.midjourney.com/"),
    ("Runway",        "https://runwayml.com/"),
    ("ElevenLabs",    "https://elevenlabs.io/"),
    ("Suno",          "https://suno.com/"),
    ("Udio",          "https://www.udio.com/"),
    ("HeyGen",        "https://www.heygen.com/"),
    ("Stability-AI",  "https://stability.ai/"),
    ("Leonardo-AI",   "https://leonardo.ai/"),
    ("Ideogram",      "https://ideogram.ai/"),
    ("Luma-Labs",     "https://lumalabs.ai/"),
    ("Pika",          "https://pika.art/"),
    ("Kling-AI",      "https://www.klingai.com/"),
    ("DALL-E-Labs",   "https://labs.openai.com/"),
    ("Synthesia",     "https://www.synthesia.io/"),
    ("D-ID",          "https://www.d-id.com/"),
]


def _media_gen_visits() -> list[Provider]:
    return [WebProbe(name, url, category="media_gen") for name, url in _MEDIA_GEN]


# ---------------------------------------------------------------------------
# Category: aggregator — platforms, marketplaces, dev tooling
# ---------------------------------------------------------------------------

_AGGREGATOR_WEB: list[tuple[str, str]] = [
    ("HuggingFace",           "https://huggingface.co/"),
    ("Replicate",             "https://replicate.com/"),
    ("OpenRouter",            "https://openrouter.ai/"),
    ("LangSmith",             "https://smith.langchain.com/"),
    ("Langfuse",              "https://langfuse.com/"),
    ("Fireworks-AI",          "https://fireworks.ai/"),
    ("Anyscale",              "https://www.anyscale.com/"),
    ("WeightsBiases",         "https://wandb.ai/"),
    ("TheresAnAIForThat",     "https://theresanaiforthat.com/"),
    ("Modal",                 "https://modal.com/"),
    ("Baseten",               "https://www.baseten.co/"),
    ("RunPod",                "https://www.runpod.io/"),
]


def _aggregator_api_probes() -> list[Provider]:
    def openrouter_body(p: str) -> dict[str, Any]:
        return {
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": p}],
        }

    def hf_body(p: str) -> dict[str, Any]:
        return {"inputs": p, "parameters": {"max_new_tokens": 128}}

    return [
        ApiProbe(
            name="OpenRouter-API",
            url="https://openrouter.ai/api/v1/chat/completions",
            user_agent="openrouter-client/1.0",
            body_builder=openrouter_body,
            category="aggregator",
        ),
        ApiProbe(
            name="HuggingFace-Inference-API",
            url=("https://api-inference.huggingface.co/models/"
                 "meta-llama/Meta-Llama-3-8B-Instruct"),
            user_agent="huggingface-hub/0.26.2 python/3.12",
            body_builder=hf_body,
            category="aggregator",
        ),
        ApiProbe(
            name="Replicate-API",
            url="https://api.replicate.com/v1/models",
            user_agent="replicate-python/1.0.3",
            method="GET",
            body_builder=None,
            category="aggregator",
        ),
        ApiProbe(
            name="LangSmith-API",
            url="https://api.smith.langchain.com/info",
            user_agent="langsmith-py/0.1.140",
            method="GET",
            body_builder=None,
            category="aggregator",
        ),
    ]


def _aggregator_targets() -> list[Provider]:
    return [
        WebProbe(name, url, category="aggregator") for name, url in _AGGREGATOR_WEB
    ] + _aggregator_api_probes()


# ---------------------------------------------------------------------------
# Category: real_response — keyless providers that return actual LLM output
# ---------------------------------------------------------------------------

def _real_response_providers() -> list[Provider]:
    return [
        PollinationsText(),
        PollinationsImage(),
        DuckDuckGoChat(),
    ]


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_registry(categories: set[str]) -> list[Provider]:
    """Return the flat list of providers enabled for the given categories."""
    registry: list[Provider] = []
    if "llm_api" in categories:
        registry += _llm_api_probes()
    if "chatbot_ui" in categories:
        registry += _chatbot_ui_visits()
    if "media_gen" in categories:
        registry += _media_gen_visits()
    if "aggregator" in categories:
        registry += _aggregator_targets()
    if "real_response" in categories:
        registry += _real_response_providers()
    return registry
