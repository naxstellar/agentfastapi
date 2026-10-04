"""Chat models, restricted to the deployment's ALLOWED_MODELS allow-list."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.config import ALLOWED_MODELS, ANTHROPIC_API_KEY, DEFAULT_MODEL, OPENAI_API_KEY

# Per request. A retried LLM call has no side effects, unlike a retried sandbox execution.
LLM_TIMEOUT_S = 300
LLM_MAX_RETRIES = 2


class UnknownModelError(ValueError):
    """The model id isn't in ALLOWED_MODELS."""


class ModelNotConfiguredError(RuntimeError):
    """The model is allowed, but its provider's API key isn't set."""


def resolve_model(model_id: str | None) -> str:
    """The allow-listed model id to use; None means DEFAULT_MODEL."""
    if model_id is None:
        return DEFAULT_MODEL
    if model_id not in ALLOWED_MODELS:
        raise UnknownModelError(f"Model {model_id!r} is not allowed")
    return model_id


def make_chat_model(model_id: str) -> BaseChatModel:
    model_id = resolve_model(model_id)
    provider = model_id.partition(":")[0]
    kwargs: dict = {"timeout": LLM_TIMEOUT_S, "max_retries": LLM_MAX_RETRIES}
    if provider == "anthropic":
        api_key = ANTHROPIC_API_KEY
    else:
        api_key = OPENAI_API_KEY
        # OpenAI reports token usage on streamed responses only when asked to; quota metering needs it.
        kwargs["stream_usage"] = True
    if not api_key:
        raise ModelNotConfiguredError(f"No API key is configured for {provider}")
    return init_chat_model(model_id, api_key=api_key, **kwargs)
