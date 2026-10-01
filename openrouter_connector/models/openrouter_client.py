# -*- coding: utf-8 -*-
"""Thin Odoo-facing factory around the pure-Python OpenRouterClient
(openrouter_http_client.py). Any module can do:

    client = self.env["openrouter.client"].get_client()
    result = client.complete("extract this invoice...", system="...")

Configuration precedence for the API key (never hardcoded):
    1. Environment variable OPENROUTER_API_KEY (recommended; set via .env /
       docker-compose, never stored in the DB).
    2. ir.config_parameter "openrouter_connector.api_key" (fallback, useful
       for per-database configuration from the Settings UI). Avoid this in
       shared/multi-tenant setups since it sits in the DB.

The model is swappable without code changes via OPENROUTER_MODEL env var or
the ir.config_parameter "openrouter_connector.model".
"""
import os

from odoo import api, models

from .openrouter_http_client import DEFAULT_MODEL, OpenRouterClient


class OpenRouterClientService(models.AbstractModel):
    """Kept as a thin AbstractModel so business modules can inject a fake
    client in tests instead of calling get_client()."""

    _name = "openrouter.client"
    _description = "OpenRouter Client Factory"

    @api.model
    def get_client(self):
        icp = self.env["ir.config_parameter"].sudo()
        api_key = os.environ.get("OPENROUTER_API_KEY") or icp.get_param(
            "openrouter_connector.api_key", default=""
        )
        model = (
            os.environ.get("OPENROUTER_MODEL")
            or icp.get_param("openrouter_connector.model")
            or DEFAULT_MODEL
        )
        return OpenRouterClient(api_key=api_key, model=model)
