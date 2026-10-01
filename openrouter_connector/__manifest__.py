{
    "name": "OpenRouter Connector",
    "version": "19.0.1.0.1",
    "summary": "Reusable, provider-agnostic LLM API client wrapper (OpenRouter by default)",
    "category": "Technical",
    "author": "Ataraxial",
    "license": "LGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}
