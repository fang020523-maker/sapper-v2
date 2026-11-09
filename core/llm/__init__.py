"""Platform LLM gateway: the only door to a model (CTFY-0001 v3 §1.1).

Every reasoning call goes through one ``complete()``; the flavor router
picks exactly one wire protocol from the platform's injection, and no
adapter ever falls back to another base URL, key or provider.
"""
from .router import (
    GatewayError,
    LLMRequest,
    LLMResponse,
    UsageTracker,
    build_gateway,
)

__all__ = [
    "GatewayError",
    "LLMRequest",
    "LLMResponse",
    "UsageTracker",
    "build_gateway",
]
