# -*- coding: utf-8 -*-
"""Pure-Python OpenRouter HTTP client — no Odoo import here on purpose, so it
can be unit-tested with plain pytest + a mocked `requests` session (see
tests/test_openrouter_client.py) without any Odoo runtime.
"""
import logging

import requests

try:
    from .llm_provider import LLMProvider, LLMProviderError
except ImportError:  # pragma: no cover - fallback for standalone unit tests
    # When this module is loaded standalone (outside the Odoo addon package,
    # e.g. by tests/test_openrouter_client.py via sys.path), relative
    # imports don't work. Fall back to a plain top-level import.
    from llm_provider import LLMProvider, LLMProviderError

_logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_TIMEOUT = 60


class OpenRouterClient(LLMProvider):
    """Independent from the ORM, so it is trivially mockable/injectable in
    unit tests (no Odoo registry needed) — pass a fake `session` with a
    `.post()` method."""

    def __init__(self, api_key, model=None, timeout=None, session=None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout or DEFAULT_TIMEOUT
        self._session = session or requests

    def complete(self, prompt, system=None, model=None, timeout=None, images=None, **kwargs):
        """`images`, if given, is a list of data URLs
        ("data:<mimetype>;base64,<...>") sent alongside the text prompt as
        multimodal content, for vision-capable models. Without this, the
        model only ever sees the text instructions and has no way to read
        the actual document — it will happily hallucinate a plausible-looking
        but fabricated answer instead of erroring out."""
        if not self.api_key:
            raise LLMProviderError(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "or the openrouter_connector.api_key system parameter."
            )

        if images:
            user_content = [{"type": "text", "text": prompt}]
            for image_url in images:
                user_content.append(
                    {"type": "image_url", "image_url": {"url": image_url}}
                )
        else:
            user_content = prompt

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": model or self.model,
            "messages": messages,
        }
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }

        try:
            response = self._session.post(
                OPENROUTER_API_URL,
                json=payload,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            _logger.error("OpenRouter request timed out: %s", exc)
            raise LLMProviderError("OpenRouter request timed out") from exc
        except requests.exceptions.RequestException as exc:
            # Never log the API key; only the exception type/message.
            _logger.error("OpenRouter request failed: %s", exc)
            raise LLMProviderError("OpenRouter request failed: %s" % exc) from exc

        if response.status_code >= 400:
            _logger.error(
                "OpenRouter returned HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )
            raise LLMProviderError(
                "OpenRouter returned HTTP %s" % response.status_code
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            _logger.error("Unexpected OpenRouter response shape: %s", exc)
            raise LLMProviderError("Unexpected OpenRouter response shape") from exc

        return {"content": content, "raw": data}
