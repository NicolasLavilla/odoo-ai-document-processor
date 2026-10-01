# -*- coding: utf-8 -*-
"""Provider-agnostic LLM abstraction.

Any concrete provider (OpenRouter today, something else tomorrow) implements
this small interface. Consumers (e.g. ai_document_processor) should depend on
this abstraction rather than on OpenRouter specifics, so swapping providers
later does not require touching business logic.
"""
import abc


class LLMProviderError(Exception):
    """Raised for any provider-level failure (timeout, HTTP error, bad response)."""


class LLMProvider(abc.ABC):
    """Minimal interface a chat/completion LLM provider must implement."""

    @abc.abstractmethod
    def complete(self, prompt, system=None, model=None, timeout=None, **kwargs):
        """Run a completion/chat call.

        :param prompt: user prompt / message content (str)
        :param system: optional system prompt (str)
        :param model: optional model override (str); falls back to the
            provider's configured default model
        :param timeout: optional timeout override in seconds
        :param images: optional list of data URLs
            ("data:<mimetype>;base64,<...>") for vision-capable models, sent
            alongside the text prompt as multimodal content
        :return: dict with at least {"content": str, "raw": <provider response>}
        :raises LLMProviderError: on any failure
        """
        raise NotImplementedError
