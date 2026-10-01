# -*- coding: utf-8 -*-
import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import fields

from ..models.invoice_schema import empty_invoice_payload, validate_invoice_payload


class FakeAiClient:
    """Mocked openrouter client: no real HTTP call, no API key needed."""

    def __init__(self, content):
        self._content = content

    def complete(self, prompt, system=None, model=None, timeout=None, **kwargs):
        return {"content": self._content, "raw": {}}


@tagged("post_install", "-at_install")
class TestAiDocument(TransactionCase):
    def test_invoice_schema_accepts_separate_vat_and_equivalence_surcharge(self):
        payload = empty_invoice_payload()
        payload.update(
            {
                "invoice_number": "REC-TEST",
                "invoice_date": "2026-09-26",
                "due_date": "",
                "currency": "EUR",
                "confidence": 0.99,
            }
        )
        payload["lines"][0].update(
            {
                "description": "Material",
                "product_code": "MAT-1",
                "quantity": 1,
                "unit_price": 100,
                "discount_percent": 0,
                "tax": 21,
                "tax_components": [
                    {"kind": "vat", "rate": 21, "scope": "goods"},
                    {
                        "kind": "equivalence_surcharge",
                        "rate": 5.2,
                        "scope": "goods",
                    },
                ],
                "subtotal": 100,
            }
        )
        payload.update({"untaxed_total": 100, "tax_total": 26.2, "total": 126.2})
        self.assertTrue(validate_invoice_payload(payload))

    def test_spain_vat_and_equivalence_surcharge_are_distinct(self):
        doc = self.env["ai.document"].new({})
        partner = self.env["res.partner"].create(
            {"name": "Proveedor español", "country_id": self.env.ref("base.es").id}
        )
        vat = doc._find_invoice_tax_component(
            {"kind": "vat", "rate": 21, "scope": "goods"}, partner
        )
        surcharge = doc._find_invoice_tax_component(
            {"kind": "equivalence_surcharge", "rate": 5.2}, partner
        )
        self.assertEqual(vat.l10n_es_type, "sujeto")
        self.assertEqual(vat.name, "21% B")
        self.assertEqual(surcharge.l10n_es_type, "recargo")
        self.assertEqual(surcharge.amount, 5.2)
        result = (vat | surcharge).compute_all(
            100, currency=self.env.ref("base.EUR")
        )
        self.assertAlmostEqual(result["total_included"], 126.2, places=2)

    def test_equivalence_surcharge_is_not_assigned_to_foreign_supplier(self):
        doc = self.env["ai.document"].new({})
        partner = self.env["res.partner"].create(
            {"name": "Proveedor extranjero", "country_id": self.env.ref("base.fr").id}
        )
        tax = doc._find_invoice_tax_component(
            {"kind": "equivalence_surcharge", "rate": 5.2}, partner
        )
        self.assertFalse(tax)

    def test_unsupported_invoice_tax_component_requires_manual_review(self):
        doc = self.env["ai.document"].new({})
        with self.assertRaises(UserError):
            doc._find_invoice_tax_component(
                {"kind": "other", "rate": 15, "label": "Retención"}
            )

    def _create_test_purchase_with_stock_product(self, suffix):
        partner = self.env["res.partner"].create(
            {"name": "Proveedor OCR %s" % suffix, "supplier_rank": 1}
        )
        product = self.env["product.product"].create(
            {
                "name": "Producto OCR %s" % suffix,
                "default_code": "OCR-%s" % suffix,
                "is_storable": True,
            }
        )
        order = self.env["purchase.order"].create(
            {
                "partner_id": partner.id,
                "company_id": self.env.company.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "name": product.display_name,
                            "product_qty": 5,
                            "product_uom_id": product.uom_id.id,
                            "price_unit": 10,
                            "date_planned": fields.Datetime.now(),
                        },
                    )
                ],
            }
        )
        order.button_confirm()
        picking = order.picking_ids.filtered(
            lambda item: item.picking_type_id.code == "incoming"
            and item.state not in ("done", "cancel")
        )
        self.assertEqual(len(picking), 1)
        return partner, product, order, picking

    def _create_test_delivery_note(self, partner, order, number, code, quantity, description):
        source = self.env["ai.document"].create(
            {
                "name": "Albarán OCR %s" % number,
                "document_type": "delivery_note",
                "state": "done",
                "original_file": base64.b64encode(b"test").decode(),
                "original_filename": "%s.pdf" % number,
            }
        )
        return self.env["ai.delivery.note.reference"].create(
            {
                "name": number,
                "partner_id": partner.id,
                "company_id": self.env.company.id,
                "delivery_date": fields.Date.today(),
                "purchase_order_ref": order.name,
                "source_document_id": source.id,
                "state": "scanned",
                "lines_json": json.dumps(
                    [
                        {
                            "description": description,
                            "product_code": code,
                            "quantity": quantity,
                        }
                    ]
                ),
            }
        )

    def test_delivery_notes_prefill_one_native_receipt_without_validating_stock(self):
        partner, product, order, picking = self._create_test_purchase_with_stock_product(
            "MULTI"
        )
        first_note = self._create_test_delivery_note(
            partner, order, "OCR-DN-1", product.default_code, 2, product.display_name
        )
        first_note.action_prepare_inventory_receipt()
        move = picking.move_ids.filtered(lambda item: item.product_id == product)
        self.assertEqual(first_note.stock_picking_id, picking)
        self.assertEqual(move.quantity, 2)
        self.assertEqual(move.ai_ocr_quantity, 2)
        self.assertNotEqual(picking.state, "done")
        self.assertAlmostEqual(product.qty_available, 0)

        second_note = self._create_test_delivery_note(
            partner, order, "OCR-DN-2", product.default_code, 3, product.display_name
        )
        second_note.action_prepare_inventory_receipt()
        self.assertEqual(second_note.stock_picking_id, picking)
        self.assertEqual(move.quantity, 5)
        self.assertEqual(move.ai_ocr_quantity, 5)
        self.assertAlmostEqual(product.qty_available, 0)

    def test_unknown_supplier_product_does_not_prepare_inventory_quantity(self):
        partner, product, order, picking = self._create_test_purchase_with_stock_product(
            "UNKNOWN"
        )
        note = self._create_test_delivery_note(
            partner, order, "OCR-DN-UNKNOWN", "NOT-IN-ORDER", 1, "Otro producto"
        )
        move = picking.move_ids.filtered(lambda item: item.product_id == product)
        initial_quantity = move.quantity
        with self.assertRaises(UserError):
            note.action_prepare_inventory_receipt()
        self.assertFalse(note.stock_picking_id)
        self.assertEqual(move.quantity, initial_quantity)
        self.assertAlmostEqual(product.qty_available, 0)

    def test_basic_model_creation(self):
        doc = self.env["ai.document"].create(
            {
                "name": "Test Invoice",
                "document_type": "invoice",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        self.assertEqual(doc.state, "draft")

    def test_process_high_confidence_goes_to_reviewed(self):
        import json

        payload = empty_invoice_payload()
        payload.update(
            {
                "invoice_number": "INV-1",
                "invoice_date": "2026-01-01",
                "due_date": "2026-02-01",
                "currency": "EUR",
                "confidence": 0.95,
            }
        )
        doc = self.env["ai.document"].create(
            {
                "name": "Test Invoice",
                "document_type": "invoice",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        doc._process_with_client(FakeAiClient(json.dumps(payload)))
        self.assertEqual(doc.state, "reviewed")
        self.assertAlmostEqual(doc.confidence, 0.95)

    def test_process_low_confidence_needs_review(self):
        import json

        payload = empty_invoice_payload()
        payload.update(
            {
                "invoice_number": "INV-2",
                "invoice_date": "2026-01-01",
                "due_date": "2026-02-01",
                "currency": "EUR",
                "confidence": 0.30,
            }
        )
        doc = self.env["ai.document"].create(
            {
                "name": "Test Invoice",
                "document_type": "invoice",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        doc._process_with_client(FakeAiClient(json.dumps(payload)))
        self.assertEqual(doc.state, "needs_review")

    def test_invalid_schema_sets_error_state(self):
        doc = self.env["ai.document"].create(
            {
                "name": "Bad Invoice",
                "document_type": "invoice",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        doc._process_with_client(FakeAiClient('{"document_type": "invoice"}'))
        self.assertEqual(doc.state, "error")
        self.assertTrue(doc.error_message)

    def test_link_records_without_auto_create_requires_existing_partner(self):
        payload = empty_invoice_payload()
        payload["supplier"]["vat"] = "ESZZZ00000"
        payload.update(
            {
                "invoice_number": "INV-3",
                "invoice_date": "2026-01-01",
                "due_date": "2026-02-01",
                "currency": "EUR",
                "confidence": 0.99,
            }
        )
        doc = self.env["ai.document"].create(
            {
                "name": "Test Invoice",
                "document_type": "invoice",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        doc._process_with_client(FakeAiClient(json.dumps(payload)))
        doc.action_mark_reviewed()
        with self.assertRaises(Exception):
            doc.action_link_records()

    def test_reprocess_is_allowed_for_documents_needing_review(self):
        payload = empty_invoice_payload()
        payload.update(
            {
                "invoice_number": "INV-REPROCESS",
                "invoice_date": "2026-01-01",
                "due_date": "",
                "currency": "EUR",
                "confidence": 0.95,
            }
        )
        doc = self.env["ai.document"].create(
            {
                "name": "Needs Review",
                "state": "needs_review",
                "original_file": base64.b64encode(b"fake image").decode(),
                "original_filename": "invoice.png",
            }
        )
        with patch.object(
            type(doc), "_get_ai_client", return_value=FakeAiClient(json.dumps(payload))
        ):
            doc.action_process()
        self.assertEqual(doc.state, "reviewed")

    def test_reprocessing_a_linked_document_is_blocked(self):
        doc = self.env["ai.document"].create(
            {
                "name": "Already Linked",
                "state": "done",
                "linked_model": "account.move",
                "linked_record_id": 123,
            }
        )
        with self.assertRaises(UserError):
            doc.action_process()

    def test_delivery_note_comparison_uses_unambiguous_description_fallback(self):
        doc = self.env["ai.document"].new({})
        note = SimpleNamespace(
            name="2.471.952",
            normalized_number="2471952",
            lines_json=json.dumps(
                [
                    {
                        "description": "KIT MECANISMO DESCARGA D2-D NR",
                        "product_code": "302000200",
                        "quantity": 6,
                        "unit_of_measure": "unit",
                        "unit_price": 31.5,
                        "discount_percent": 35,
                    }
                ]
            ),
        )
        payload = {
            "currency": "EUR",
            "lines": [
                {
                    "description": "KIT MECANISMO DESCARGA D2-D NR",
                    "product_code": "3020020020",
                    "quantity": 6,
                    "unit_price": 31.5,
                    "discount_percent": 35,
                    "subtotal": 122.85,
                    "delivery_note_number": "2.471.952",
                }
            ],
        }
        warnings = doc._compare_invoice_lines_to_delivery_note(payload, note)
        self.assertFalse(any("diferencias de cantidad" in warning for warning in warnings))
        self.assertTrue(any("códigos extraídos no coinciden" in warning for warning in warnings))

    def test_delivery_note_comparison_still_warns_on_real_quantity_difference(self):
        doc = self.env["ai.document"].new({})
        note = SimpleNamespace(
            name="2.471.952",
            normalized_number="2471952",
            lines_json=json.dumps(
                [
                    {
                        "description": "KIT MECANISMO DESCARGA D2-D NR",
                        "product_code": "302000200",
                        "quantity": 1,
                        "unit_of_measure": "unit",
                    }
                ]
            ),
        )
        payload = {
            "currency": "EUR",
            "lines": [
                {
                    "description": "KIT MECANISMO DESCARGA D2-D NR",
                    "product_code": "3020020020",
                    "quantity": 6,
                    "unit_price": 31.5,
                    "discount_percent": 35,
                    "subtotal": 122.85,
                    "delivery_note_number": "2.471.952",
                }
            ],
        }
        warnings = doc._compare_invoice_lines_to_delivery_note(payload, note)
        self.assertTrue(any("diferencias de cantidad" in warning for warning in warnings))
