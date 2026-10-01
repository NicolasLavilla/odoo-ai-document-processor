{
    "name": "AI Document Processor",
    "version": "19.0.1.0.11",
    "summary": "OCR/AI extraction of invoices, delivery notes and tickets with human review",
    "category": "Accounting/Documents",
    "author": "Ataraxial",
    "license": "LGPL-3",
    "depends": ["base", "mail", "account", "purchase_stock", "openrouter_connector", "hub_client_base"],
    "external_dependencies": {"python": ["pymupdf", "jsonschema"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/ai_document_views.xml",
        "views/delivery_note_reference_views.xml",
    ],
    "installable": True,
    "application": True,
}
