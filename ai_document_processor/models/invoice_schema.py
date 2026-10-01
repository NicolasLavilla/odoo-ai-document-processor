# -*- coding: utf-8 -*-
"""Invoice extraction JSON schema + validator.

Pure-Python module (no Odoo imports) so it can be unit-tested without a
running Odoo instance. Uses `jsonschema` if available (installed via
docker/odoo/requirements.txt in the image), and falls back to a manual
key/type check otherwise so the module still degrades gracefully outside
that image (e.g. plain `pytest` on a dev machine without the extra dep).
"""
from copy import deepcopy

try:
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - exercised only without the dep
    _HAS_JSONSCHEMA = False


INVOICE_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "product_code": {"type": "string"},
        "quantity": {"type": "number"},
        "unit_price": {"type": "number"},
        "discount_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "tax": {"type": "number"},
        "tax_components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["vat", "equivalence_surcharge", "other"],
                    },
                    "rate": {"type": "number", "minimum": 0, "maximum": 100},
                    "scope": {"type": "string", "enum": ["goods", "service"]},
                    "label": {"type": "string"},
                },
                "required": ["kind", "rate"],
            },
        },
        "subtotal": {"type": "number"},
        "delivery_note_number": {"type": "string"},
    },
    "required": [
        "description",
        "product_code",
        "quantity",
        "unit_price",
        "discount_percent",
        "tax",
        "subtotal",
    ],
}

DELIVERY_NOTE_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "product_code": {"type": "string"},
        "quantity": {"type": "number"},
        "unit_of_measure": {"type": "string"},
        # Many delivery notes are not valued. These fields are optional and
        # are used only as a secondary check when the supplier prints them.
        "unit_price": {"type": ["number", "null"]},
        "discount_percent": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        "subtotal": {"type": ["number", "null"]},
    },
    "required": ["description", "product_code", "quantity"],
}

DELIVERY_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "const": "delivery_note"},
        "supplier": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "vat": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "website": {"type": "string"},
            },
            "required": ["name", "vat", "address"],
        },
        "delivery_note_number": {"type": "string"},
        "delivery_note_date": {"type": "string"},
        "purchase_order": {"type": "string"},
        "lines": {"type": "array", "items": DELIVERY_NOTE_LINE_SCHEMA},
        "confidence": {"type": "number"},
    },
    "required": [
        "document_type",
        "supplier",
        "delivery_note_number",
        "delivery_note_date",
        "purchase_order",
        "lines",
        "confidence",
    ],
}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "const": "invoice"},
        "supplier": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "vat": {"type": "string"},
                "address": {"type": "string"},
                # Optional additions on top of the spec's verbatim schema:
                # useful for populating res.partner contact fields, but not
                # required (many invoices simply don't show them, and older
                # extractions/tests without these keys must stay valid).
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "website": {"type": "string"},
            },
            "required": ["name", "vat", "address"],
        },
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "due_date": {"type": "string"},
        "currency": {"type": "string"},
        "purchase_order": {"type": "string"},
        "delivery_notes": {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "number": {"type": "string"},
                            "date": {"type": "string"},
                        },
                        "required": ["number"],
                    },
                ]
            },
        },
        "lines": {"type": "array", "items": INVOICE_LINE_SCHEMA},
        "untaxed_total": {"type": "number"},
        "tax_total": {"type": "number"},
        "total": {"type": "number"},
        "confidence": {"type": "number"},
    },
    "required": [
        "document_type",
        "supplier",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "purchase_order",
        "lines",
        "untaxed_total",
        "tax_total",
        "total",
        "confidence",
    ],
}

# Verbatim template as given in the spec, useful as a default/empty payload.
EMPTY_INVOICE_PAYLOAD = {
    "document_type": "invoice",
    "supplier": {
        "name": "",
        "vat": "",
        "address": "",
        "phone": "",
        "email": "",
        "website": "",
    },
    "invoice_number": "",
    "invoice_date": "",
    "due_date": "",
    "currency": "",
    "purchase_order": "",
    "delivery_notes": [],
    "lines": [
        {
            "description": "",
            "product_code": "",
            "quantity": 0,
            "unit_price": 0,
            "discount_percent": 0,
            "tax": 0,
            "tax_components": [],
            "subtotal": 0,
            "delivery_note_number": "",
        }
    ],
    "untaxed_total": 0,
    "tax_total": 0,
    "total": 0,
    "confidence": 0,
}


class InvoiceSchemaError(ValueError):
    """Raised when a payload does not match the invoice extraction schema."""


def empty_invoice_payload():
    return deepcopy(EMPTY_INVOICE_PAYLOAD)


def validate_invoice_payload(payload):
    """Validate `payload` against INVOICE_SCHEMA.

    :raises InvoiceSchemaError: with a human-readable message on failure.
    :return: True if valid.
    """
    if not isinstance(payload, dict):
        raise InvoiceSchemaError("Payload must be a JSON object")

    if _HAS_JSONSCHEMA:
        try:
            jsonschema.validate(instance=payload, schema=INVOICE_SCHEMA)
        except jsonschema.exceptions.ValidationError as exc:
            raise InvoiceSchemaError(str(exc.message)) from exc
        return True

    return _manual_validate(payload)


def validate_delivery_note_payload(payload):
    """Validate an OCR payload for a supplier delivery note."""
    if not isinstance(payload, dict):
        raise InvoiceSchemaError("El resultado no es un objeto JSON")
    if _HAS_JSONSCHEMA:
        try:
            jsonschema.validate(instance=payload, schema=DELIVERY_NOTE_SCHEMA)
        except jsonschema.exceptions.ValidationError as exc:
            raise InvoiceSchemaError(exc.message) from exc
        return True
    for key in DELIVERY_NOTE_SCHEMA["required"]:
        if key not in payload:
            raise InvoiceSchemaError("Falta el campo obligatorio: %s" % key)
    if payload.get("document_type") != "delivery_note":
        raise InvoiceSchemaError("El tipo de documento debe ser delivery_note")
    if not isinstance(payload.get("supplier"), dict):
        raise InvoiceSchemaError("El proveedor debe ser un objeto")
    if not isinstance(payload.get("lines"), list):
        raise InvoiceSchemaError("Las líneas del albarán deben ser una lista")
    for index, line in enumerate(payload["lines"], 1):
        if not isinstance(line, dict):
            raise InvoiceSchemaError("La línea %s no es un objeto" % index)
        if not isinstance(line.get("quantity"), (int, float)) or isinstance(
            line.get("quantity"), bool
        ):
            raise InvoiceSchemaError(
                "La cantidad de la línea %s no es numérica" % index
            )
    return True


def _manual_validate(payload):
    top_required = INVOICE_SCHEMA["required"]
    for key in top_required:
        if key not in payload:
            raise InvoiceSchemaError("Missing required key: %s" % key)

    if payload.get("document_type") != "invoice":
        raise InvoiceSchemaError("document_type must be 'invoice'")

    supplier = payload.get("supplier")
    if not isinstance(supplier, dict):
        raise InvoiceSchemaError("supplier must be an object")
    for key in ("name", "vat", "address"):
        if key not in supplier or not isinstance(supplier[key], str):
            raise InvoiceSchemaError("supplier.%s must be a string" % key)

    for key in (
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "purchase_order",
    ):
        if not isinstance(payload.get(key), str):
            raise InvoiceSchemaError("%s must be a string" % key)

    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise InvoiceSchemaError("lines must be an array")
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            raise InvoiceSchemaError("lines[%d] must be an object" % idx)
        for key in ("description", "product_code"):
            if not isinstance(line.get(key), str):
                raise InvoiceSchemaError(
                    "lines[%d].%s must be a string" % (idx, key)
                )
        if "delivery_note_number" in line and not isinstance(
            line["delivery_note_number"], str
        ):
            raise InvoiceSchemaError(
                "lines[%d].delivery_note_number must be a string" % idx
            )
        for key in (
            "quantity",
            "unit_price",
            "discount_percent",
            "tax",
            "subtotal",
        ):
            if not isinstance(line.get(key), (int, float)) or isinstance(
                line.get(key), bool
            ):
                raise InvoiceSchemaError(
                    "lines[%d].%s must be a number" % (idx, key)
                )
        tax_components = line.get("tax_components")
        if tax_components is not None:
            if not isinstance(tax_components, list):
                raise InvoiceSchemaError(
                    "lines[%d].tax_components must be an array" % idx
                )
            for component_idx, component in enumerate(tax_components, 1):
                if not isinstance(component, dict):
                    raise InvoiceSchemaError(
                        "lines[%d].tax_components[%d] must be an object"
                        % (idx, component_idx)
                    )
                if component.get("kind") not in (
                    "vat",
                    "equivalence_surcharge",
                    "other",
                ):
                    raise InvoiceSchemaError(
                        "lines[%d].tax_components[%d].kind is invalid"
                        % (idx, component_idx)
                    )
                rate = component.get("rate")
                if (
                    not isinstance(rate, (int, float))
                    or isinstance(rate, bool)
                    or not 0 <= rate <= 100
                ):
                    raise InvoiceSchemaError(
                        "lines[%d].tax_components[%d].rate must be between 0 and 100"
                        % (idx, component_idx)
                    )
                if component.get("scope", "goods") not in ("goods", "service"):
                    raise InvoiceSchemaError(
                        "lines[%d].tax_components[%d].scope is invalid"
                        % (idx, component_idx)
                    )
        if not 0 <= line["discount_percent"] <= 100:
            raise InvoiceSchemaError(
                "lines[%d].discount_percent must be between 0 and 100" % idx
            )

    for key in ("untaxed_total", "tax_total", "total", "confidence"):
        if not isinstance(payload.get(key), (int, float)) or isinstance(
            payload.get(key), bool
        ):
            raise InvoiceSchemaError("%s must be a number" % key)

    delivery_notes = payload.get("delivery_notes", [])
    if not isinstance(delivery_notes, list):
        raise InvoiceSchemaError("delivery_notes must be an array")
    for idx, entry in enumerate(delivery_notes, 1):
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("number"), str):
            raise InvoiceSchemaError(
                "delivery_notes[%d].number must be a string" % idx
            )
        if "date" in entry and not isinstance(entry["date"], str):
            raise InvoiceSchemaError(
                "delivery_notes[%d].date must be a string" % idx
            )

    return True
