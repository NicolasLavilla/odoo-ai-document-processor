# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    openrouter_api_key = fields.Char(
        string="OpenRouter API Key",
        config_parameter="openrouter_connector.api_key",
        help="Fallback only. Prefer setting the OPENROUTER_API_KEY environment "
        "variable so the key never lives in the database.",
    )
    openrouter_model = fields.Char(
        string="OpenRouter Model",
        config_parameter="openrouter_connector.model",
        help="e.g. openai/gpt-4o-mini. Overridden by the OPENROUTER_MODEL "
        "environment variable if set.",
    )
