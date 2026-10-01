# -*- coding: utf-8 -*-
import base64
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
import mimetypes
import re
import unicodedata
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .invoice_schema import (
    EMPTY_INVOICE_PAYLOAD,
    InvoiceSchemaError,
    validate_delivery_note_payload,
    validate_invoice_payload,
)
from .delivery_note_reference import normalize_delivery_note_number

_logger = logging.getLogger(__name__)

# Below this confidence (0-1 or 0-100 depending on provider; we normalize to
# 0-1 internally), documents require human review before any Odoo record is
# created or linked.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


class AiDocument(models.Model):
    _name = "ai.document"
    _description = "AI-Processed Document"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Nombre del documento", required=True, default="Nuevo documento")
    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        copy=False,
    )
    document_type = fields.Selection(
        [
            ("auto", "Detectar automáticamente"),
            ("invoice", "Factura de proveedor"),
            ("delivery_note", "Albarán de proveedor"),
            ("ticket", "Ticket / justificante"),
            ("other", "Otro documento"),
        ],
        string="Tipo de documento (lo detecta la IA)",
        required=True,
        default="auto",
        help="La IA identifica el tipo real del documento al procesarlo y corrige este campo automáticamente.",
        tracking=True,
    )

    # The original uploaded file is never deleted or overwritten: once set,
    # write() below blocks changes to these two fields.
    original_file = fields.Binary(string="Archivo original", attachment=True)
    original_filename = fields.Char(string="Nombre del archivo")

    result_json = fields.Text(
        string="Resultado OCR (JSON)",
        help="Datos estructurados extraídos del documento.",
    )
    confidence = fields.Float(string="Confianza de extracción")
    processed_at = fields.Datetime(string="Procesado el")
    error_message = fields.Text(string="Error de procesamiento")
    review_warnings = fields.Text(string="Avisos de revisión")
    delivery_note_reference_id = fields.Many2one(
        "ai.delivery.note.reference",
        string="Registro digital del albarán",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    @api.constrains("company_id", "delivery_note_reference_id")
    def _check_delivery_note_company(self):
        for document in self:
            reference = document.delivery_note_reference_id
            if reference and reference.company_id != document.company_id:
                raise ValidationError(
                    "El documento OCR y el albarán deben pertenecer a la misma empresa."
                )

    state = fields.Selection(
        [
            ("draft", "Nuevo"),
            ("processing", "Procesando"),
            ("needs_review", "Pendiente de revisión"),
            ("reviewed", "Revisado"),
            ("done", "Enlazado en Odoo"),
            ("error", "Error"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )

    linked_model = fields.Char(string="Modelo enlazado")
    linked_record_id = fields.Integer(string="ID del registro enlazado")

    confidence_threshold = fields.Float(
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        help="Documents scoring below this threshold are routed to 'Needs Review'.",
    )
    auto_create_records = fields.Boolean(
        string="Permitir crear automáticamente registros relacionados",
        default=True,
        help="Si se desactiva, el proveedor no se creará automáticamente; tendrás que crearlo o enlazarlo manualmente.",
    )

    # ------------------------------------------------------------------
    # Integrity: original file is immutable once set
    # ------------------------------------------------------------------
    def write(self, vals):
        for rec in self:
            if rec.original_file and (
                "original_file" in vals or "original_filename" in vals
            ):
                raise UserError(
                    "El archivo original no se puede sustituir una vez subido. Crea otro documento si necesitas procesar una copia diferente."
                )
        return super().write(vals)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def _get_ai_client(self):
        """Returns an object with a .complete(prompt, system=...) method.

        Prioritizes the centralized Hub client (hub.client) if configured with an API key,
        routing requests through the credit ledger. Falls back to openrouter_connector
        if a local direct OpenRouter key is set instead.
        """
        if "hub.client" in self.env and self.env["hub.client"].is_configured():
            return self.env["hub.client"]
        return self.env["openrouter.client"].get_client()

    def action_process(self):
        """Entry point: run AI extraction and route based on confidence."""
        for rec in self:
            if rec.state not in ("draft", "error", "needs_review"):
                raise UserError(
                    "Solo se pueden reprocesar documentos nuevos, con error o pendientes de revisión."
                )
            if rec.linked_model and rec.linked_record_id:
                raise UserError(
                    "Este documento ya está enlazado a un registro de Odoo. Crea otro documento para evitar duplicados."
                )
            rec._process_with_client(rec._get_ai_client())
        return True

    # OpenRouter/OpenAI-compatible endpoints reject non-image MIME types in
    # image_url content parts ("Invalid MIME type. Only image types are
    # supported."), so a PDF must be rendered to PNG pages first.
    # By default, process the entire document. A safety ceiling can be set via
    # ir.config_parameter 'ai_document_processor.max_pdf_pages' (defaults to 100).
    DEFAULT_MAX_PDF_PAGES = 100

    def _build_image_data_urls(self):
        """Build one or more data: URLs from the uploaded file so it can
        actually be sent to a vision-capable model. Without this, the AI
        never sees the document and will fabricate a plausible-looking but
        fake answer. PDFs are rendered to PNG pages (image_url content parts
        must be an actual image MIME type); other files are assumed to
        already be an image and sent as-is.
        All pages of the document are rendered to images, consuming 1 credit
        per page at the OCR Hub."""
        self.ensure_one()
        if not self.original_file:
            return []
        raw_bytes = base64.b64decode(self.original_file)
        mimetype, _ = mimetypes.guess_type(self.original_filename or "")

        if mimetype == "application/pdf" or raw_bytes[:4] == b"%PDF":
            import pymupdf

            max_pages = int(
                self.env["ir.config_parameter"]
                .sudo()
                .get_param(
                    "ai_document_processor.max_pdf_pages",
                    default=self.DEFAULT_MAX_PDF_PAGES,
                )
            )
            urls = []
            with pymupdf.open(stream=raw_bytes, filetype="pdf") as doc:
                page_slice = doc[:max_pages] if max_pages > 0 else doc
                for page in page_slice:
                    pixmap = page.get_pixmap(dpi=200)
                    png_b64 = base64.b64encode(pixmap.tobytes("png")).decode()
                    urls.append("data:image/png;base64,%s" % png_b64)
            return urls

        mimetype = mimetype or "image/png"
        file_b64 = (
            self.original_file.decode()
            if isinstance(self.original_file, bytes)
            else self.original_file
        )
        return ["data:%s;base64,%s" % (mimetype, file_b64)]

    def _process_with_client(self, client):
        self.ensure_one()
        self.state = "processing"
        self.error_message = False
        try:
            if not self.original_file:
                raise UserError(
                    "No hay ningún archivo adjunto. Sube primero la factura o el albarán."
                )
            prompt = self._build_extraction_prompt()
            image_urls = self._build_image_data_urls()
            result = client.complete(
                prompt,
                system="You are a document data extraction and classification assistant. Look "
                "carefully at the attached document image(s) and extract "
                "its actual content and identify its real document type. "
                "Respond with one JSON object matching exactly one of the "
                "provided schemas, with no additional commentary. "
                "Never invent placeholder/sample data — if a field is "
                "genuinely unreadable, use an empty value for it.",
                images=image_urls or None,
            )
            payload = self._parse_ai_response(result["content"])
            self._apply_result(payload)
        except UserError:
            # UserError ya lleva un mensaje legible en español; lo persistimos
            # en el documento y lo re-lanzamos para que Odoo lo muestre en el popup.
            import sys
            exc = sys.exc_info()[1]
            _logger.warning("AI processing UserError for %s: %s", self.name, exc)
            self.write({"state": "error", "error_message": str(exc)})
            raise
        except Exception as exc:  # noqa: BLE001 - persisting failures is intentional
            _logger.exception("AI document processing failed for %s", self.name)
            detail = str(exc)
            if "insufficient_credits" in detail or "402" in detail:
                msg = (
                    "Saldo de créditos OCR agotado en el Hub central. "
                    "Recarga tu bolsa de documentos en el panel de administración del Hub "
                    "para poder seguir procesando documentos."
                )
            else:
                msg = (
                    "No se pudo procesar el documento. "
                    "Revisa la conexión con el proveedor de IA, el archivo y el formato. "
                    "Detalle técnico: %s" % exc
                )
            self.write({"state": "error", "error_message": msg})

    def _build_extraction_prompt(self):
        self.ensure_one()
        invoice_template = json.dumps(EMPTY_INVOICE_PAYLOAD, ensure_ascii=False, indent=2)
        delivery_template = json.dumps(
            {
                "document_type": "delivery_note",
                "supplier": {
                    "name": "",
                    "vat": "",
                    "address": "",
                    "phone": "",
                    "email": "",
                    "website": "",
                },
                "delivery_note_number": "",
                "delivery_note_date": "",
                "purchase_order": "",
                "lines": [],
                "confidence": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
        return (
            "Inspect the attached document and DETECT its real type from the "
            "printed content. Do not follow, infer, or repeat the type currently "
            "selected in Odoo; that selection may be wrong. Supported types are "
            "a supplier invoice ('invoice') and a supplier delivery note/albarán "
            "('delivery_note'). An invoice has invoice/tax/total information; a "
            "delivery note records goods delivered and may have no prices. If "
            "the document is neither, return only {\"document_type\":\"other\", "
            "\"confidence\":0}. Choose exactly one type and return only its "
            "corresponding JSON structure shown below.\n\n"
            "Extraction rules:\n"
            "- Use plain text only, never Markdown or clickable links. Put email "
            "addresses only in supplier.email. Put a real web address in "
            "supplier.website only when one is printed; otherwise use an empty string.\n"
            "- If a value is absent, use an empty string for text and 0 for required "
            "invoice numbers. Do not invent values. For optional delivery-note "
            "prices/discount/subtotals use null when absent.\n"
            "- For invoices, currency must be an ISO 4217 code such as EUR, never "
            "a symbol such as €.\n"
            "- Normalize dates to YYYY-MM-DD, including Spanish two-digit years, "
            "for example 17/09/26 -> 2026-09-17.\n"
            "- Preserve supplier article codes exactly. Include every printed row, "
            "including free items. Never treat a heading such as 'ALBARÁN' as an "
            "albarán number; only extract an actual reference printed next to it.\n"
            "- For invoices, extract the unit price before discount when shown, "
            "the discount percentage, and the line subtotal after discount before "
            "tax. Never apply a discount twice. Preserve printed base, tax, and total.\n"
            "- For each invoice line, list every printed tax in tax_components. Use "
            "kind='vat' for IVA and kind='equivalence_surcharge' for recargo de "
            "equivalencia; keep their rates separate (for example 21 and 5.2), "
            "and set scope to goods or service. Do not combine IVA and surcharge "
            "into one rate. Keep the legacy tax field equal to the IVA rate only. "
            "If another tax or withholding is printed, use kind='other' and its "
            "printed label; the system will require manual review. tax_total is "
            "the sum of all tax and surcharge amounts on the invoice.\n"
            "- Extract delivery-note references on an invoice only when a specific "
            "number/date is printed; do not copy headings, labels, or purchase-order "
            "numbers into delivery_notes. Assign lines to a delivery note only "
            "when the document makes that association clear.\n"
            "- Estimate confidence between 0 and 1. Return raw JSON only, no "
            "Markdown, fences, or commentary.\n\n"
            "Invoice structure:\n%s\n\nDelivery-note structure:\n%s"
            % (invoice_template, delivery_template)
        )

    @staticmethod
    def _parse_ai_response(content):
        """Models frequently wrap the JSON in a markdown code fence (```json
        ... ```) or add stray text before/after it despite instructions not
        to. Strip that defensively before giving up."""
        candidates = [content]

        fence_match = re.search(r"```(?:json)?\s*(.*?)```", content or "", re.DOTALL)
        if fence_match:
            candidates.append(fence_match.group(1))

        brace_match = re.search(r"\{.*\}", content or "", re.DOTALL)
        if brace_match:
            candidates.append(brace_match.group(0))

        last_exc = None
        for candidate in candidates:
            try:
                return json.loads(candidate.strip())
            except (TypeError, ValueError) as exc:
                last_exc = exc
        preview = (content or "")[:300]
        raise UserError(
            "La IA devolvió una respuesta que no se pudo leer como JSON. Vuelve a procesar o corrige el resultado manualmente. Detalle: %s. Respuesta inicial: %s"
            % (last_exc, preview)
        ) from last_exc

    def _apply_result(self, payload):
        self.ensure_one()
        if not isinstance(payload, dict):
            self.write(
                {
                    "state": "error",
                    "error_message": "La IA no devolvió un objeto JSON de documento. Vuelve a procesar el archivo.",
                }
            )
            return
        detected_type = self._normalize_document_type(payload.get("document_type"))
        if detected_type not in ("invoice", "delivery_note"):
            payload_type = detected_type if detected_type in ("ticket", "other") else "other"
            self.write(
                {
                    "document_type": payload_type,
                    "result_json": json.dumps(payload, ensure_ascii=False, indent=2),
                    "processed_at": fields.Datetime.now(),
                    "state": "error",
                    "error_message": "La IA no ha identificado una factura de proveedor ni un albarán. Este flujo solo procesa esos dos tipos; revisa que el archivo sea uno de ellos y vuelve a procesarlo.",
                }
            )
            return

        payload["document_type"] = detected_type
        previous_type = self.document_type
        type_warning = False
        if previous_type not in ("auto", detected_type):
            type_label = "una factura de proveedor" if detected_type == "invoice" else "un albarán de proveedor"
            type_warning = (
                "La IA detectó %s y corrigió el tipo seleccionado anteriormente. Comprueba que la clasificación sea correcta."
                % type_label
            )
        if detected_type == "invoice":
            try:
                validate_invoice_payload(payload)
                payload["currency"] = self._normalize_currency_code(
                    payload.get("currency")
                )
            except InvoiceSchemaError as exc:
                self.write(
                    {
                        "document_type": detected_type,
                        "result_json": json.dumps(payload, ensure_ascii=False, indent=2),
                        "state": "error",
                        "error_message": self._schema_error_message_es(exc, "factura"),
                        "review_warnings": type_warning or False,
                    }
                )
                return
        else:
            try:
                validate_delivery_note_payload(payload)
            except InvoiceSchemaError as exc:
                self.write(
                    {
                        "document_type": detected_type,
                        "result_json": json.dumps(payload, ensure_ascii=False, indent=2),
                        "state": "error",
                        "error_message": self._schema_error_message_es(exc, "albarán"),
                        "review_warnings": type_warning or False,
                    }
                )
                return

        try:
            confidence = float(payload.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        # Normalize 0-100 scales down to 0-1 if the provider returned that way.
        if confidence > 1:
            confidence = confidence / 100.0

        new_state = (
            "needs_review" if confidence < self.confidence_threshold else "reviewed"
        )

        self.write(
            {
                "document_type": detected_type,
                "result_json": json.dumps(payload, ensure_ascii=False, indent=2),
                "confidence": confidence,
                "processed_at": fields.Datetime.now(),
                "state": new_state,
                "review_warnings": type_warning or False,
            }
        )

    @staticmethod
    def _normalize_document_type(value):
        if not isinstance(value, str):
            return ""
        normalized = unicodedata.normalize("NFKD", value.strip().lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
        if normalized in ("invoice", "supplier_invoice", "vendor_bill", "factura", "factura_de_proveedor"):
            return "invoice"
        if normalized in ("delivery_note", "deliverynote", "albaran", "albaran_de_proveedor", "nota_de_entrega"):
            return "delivery_note"
        if normalized in ("ticket", "receipt", "justificante"):
            return "ticket"
        if normalized in ("other", "otro", "document"):
            return "other"
        return ""

    @staticmethod
    def _schema_error_message_es(exc, document_name):
        message = str(exc)
        match = re.search(r"Missing required key: ([A-Za-z0-9_]+)", message)
        if match:
            field = match.group(1)
            labels = {
                "supplier": "proveedor",
                "vat": "NIF/CIF",
                "invoice_number": "número de factura",
                "invoice_date": "fecha de factura",
                "delivery_note_number": "número de albarán",
                "delivery_note_date": "fecha del albarán",
                "quantity": "cantidad",
                "unit_price": "precio unitario",
                "discount_percent": "descuento",
                "tax": "IVA",
                "subtotal": "subtotal",
                "delivery_notes": "referencias de albarán",
            }
            return "Falta el dato obligatorio «%s» en el resultado OCR del %s. Corrígelo antes de continuar." % (
                labels.get(field, field), document_name
            )
        if "is not of type 'number'" in message or "must be a number" in message:
            return "Hay una cantidad o un importe que no es numérico en el resultado OCR del %s. Revisa las líneas." % document_name
        if "is not of type 'string'" in message:
            return "Hay un campo de texto con formato incorrecto en el resultado OCR del %s. Revisa proveedor, referencias y fechas." % document_name
        if "greater than or equal to 0" in message or "between 0 and 100" in message:
            return "El descuento del %s debe estar entre 0 y 100. Corrige la línea indicada por el OCR." % document_name
        if "document_type" in message:
            return "El tipo indicado en el resultado OCR no coincide con el documento abierto (%s). Corrígelo o vuelve a procesar el archivo." % document_name
        return "Los datos extraídos del %s no tienen el formato esperado. Revisa el resultado OCR. Detalle: %s" % (
            document_name, message
        )

    def _validate_invoice_payload_for_link(self, payload):
        """Fail early with actionable Spanish messages before creating a bill."""
        try:
            validate_invoice_payload(payload)
        except InvoiceSchemaError as exc:
            raise UserError(
                self._schema_error_message_es(exc, "factura")
            ) from exc

        invoice_number = (payload.get("invoice_number") or "").strip()
        if not invoice_number:
            raise UserError(
                "No se ha leído el número de factura. Corrígelo en «Resultado OCR» antes de crearla."
            )
        invoice_date = (payload.get("invoice_date") or "").strip()
        if not invoice_date or not self._parse_ai_date(invoice_date):
            raise UserError(
                "La fecha de factura falta o no se reconoce. Usa DD/MM/AAAA, DD/MM/AA o AAAA-MM-DD y vuelve a guardar."
            )
        due_date = (payload.get("due_date") or "").strip()
        if due_date and not self._parse_ai_date(due_date):
            raise UserError(
                "La fecha de vencimiento «%s» no se reconoce. Corrígela con formato DD/MM/AAAA, DD/MM/AA o AAAA-MM-DD."
                % due_date
            )
        if not (payload.get("supplier") or {}).get("name", "").strip():
            raise UserError(
                "No se ha leído el nombre del proveedor. Corrígelo antes de continuar."
            )

        currency_code = self._normalize_currency_code(payload.get("currency"))
        payload["currency"] = currency_code
        currency = self.env["res.currency"].search(
            [("name", "=", currency_code), ("active", "=", True)], limit=1
        )
        if not currency:
            raise UserError(
                "La moneda «%s» no existe o está desactivada en Odoo. Revisa el campo «currency» de la factura."
                % (currency_code or "(vacío)")
            )

        lines = payload.get("lines")
        if not lines:
            raise UserError(
                "La factura no tiene líneas de producto. Añade o corrige las líneas antes de crearla."
            )

        header_values = (
            payload.get("untaxed_total"),
            payload.get("tax_total"),
            payload.get("total"),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in header_values
        ):
            raise UserError(
                "Falta la base, el IVA o el total de la factura, o el OCR leyó un valor no numérico."
            )
        untaxed, tax, total = header_values

        invoice_type_str = str(payload.get("invoice_type") or "").strip().lower()
        is_refund = (
            total < 0
            or untaxed < 0
            or invoice_type_str in ("refund", "credit_note", "rectificativa", "abono")
            or any(
                isinstance(line.get("subtotal"), (int, float)) and line.get("subtotal") < 0
                for line in lines
            )
        )

        for index, line in enumerate(lines, 1):
            label = line.get("product_code") or line.get("description") or "sin descripción"
            for key in ("quantity", "unit_price", "discount_percent", "tax", "subtotal"):
                value = line.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise UserError(
                        "La línea %s (%s) tiene un valor inválido en «%s». Corrígelo en el resultado OCR."
                        % (index, label, key)
                    )
            if not is_refund:
                if line["quantity"] <= 0:
                    raise UserError(
                        "La cantidad de la línea %s (%s) debe ser mayor que cero. Si es una devolución, procésala como factura rectificativa."
                        % (index, label)
                    )
                if line["unit_price"] < 0 or line["subtotal"] < 0 or line["tax"] < 0:
                    raise UserError(
                        "La línea %s (%s) contiene un precio, subtotal o impuesto negativo. Las devoluciones deben registrarse como factura rectificativa."
                        % (index, label)
                    )
            else:
                if line["quantity"] == 0:
                    raise UserError(
                        "La cantidad de la línea %s (%s) no puede ser cero."
                        % (index, label)
                    )
                if line["tax"] < 0:
                    raise UserError(
                        "El tipo de IVA de la línea %s (%s) no puede ser negativo."
                        % (index, label)
                    )
            if not 0 <= line["discount_percent"] <= 100:
                raise UserError(
                    "El descuento de la línea %s (%s) debe estar entre 0 y 100."
                    % (index, label)
                )
            expected_subtotal = (
                line["quantity"]
                * line["unit_price"]
                * (1 - line["discount_percent"] / 100.0)
            )
            if abs(currency.round(expected_subtotal) - currency.round(line["subtotal"])) > 0.01:
                raise UserError(
                    "El subtotal de la línea %s (%s) no cuadra: cantidad × precio − descuento da %.2f y el OCR leyó %.2f. Revisa precio, descuento y unidad."
                    % (index, label, currency.round(expected_subtotal), line["subtotal"])
                )

        if not is_refund:
            if untaxed < 0 or tax < 0 or total <= 0:
                raise UserError(
                    "La factura tiene importes negativos o un total igual a cero. Si es un abono, debe tratarse como factura rectificativa."
                )
            line_total = sum(line["subtotal"] for line in lines)
            if currency.compare_amounts(line_total, untaxed):
                raise UserError(
                    "La suma de subtotales de las líneas (%.2f) no coincide con la base imponible (%.2f). Revisa líneas omitidas, descuentos y redondeos."
                    % (line_total, untaxed)
                )
            if currency.compare_amounts(untaxed + tax, total):
                raise UserError(
                    "La base (%.2f) más el IVA (%.2f) no coincide con el total impreso (%.2f). Revisa impuestos, recargos y redondeos."
                    % (untaxed, tax, total)
                )
        else:
            # Factura rectificativa / abono (in_refund)
            # El total y base deben ser coherentes en signo o magnitud
            line_total = sum(line["subtotal"] for line in lines)
            if currency.compare_amounts(abs(line_total), abs(untaxed)):
                raise UserError(
                    "En la factura rectificativa/abono, la suma de líneas (%.2f) no coincide con la base imponible (%.2f)."
                    % (line_total, untaxed)
                )
            expected_total_magnitude = abs(untaxed) + abs(tax)
            if currency.compare_amounts(expected_total_magnitude, abs(total)):
                raise UserError(
                    "En la factura rectificativa/abono, la base (|%.2f|) más impuestos (|%.2f|) no coincide con el total (|%.2f|)."
                    % (untaxed, tax, total)
                )

    def _prevent_duplicate_invoice_file(self):
        self.ensure_one()
        if not self.original_file:
            return
        current = self.original_file
        if isinstance(current, str):
            current = current.encode()
        current_hash = hashlib.sha256(base64.b64decode(current)).hexdigest()
        candidates = self.search(
            [
                ("id", "!=", self.id),
                ("document_type", "=", "invoice"),
                ("linked_model", "=", "account.move"),
                ("linked_record_id", ">", 0),
            ]
        )
        for candidate in candidates:
            if not candidate.original_file:
                continue
            raw = candidate.original_file
            if isinstance(raw, str):
                raw = raw.encode()
            if hashlib.sha256(base64.b64decode(raw)).hexdigest() == current_hash:
                move = self.env["account.move"].browse(candidate.linked_record_id).exists()
                raise UserError(
                    "Este mismo archivo ya se procesó en «%s» y está enlazado a la factura %s. No lo vuelvas a importar para evitar duplicados."
                    % (candidate.display_name, move.display_name if move else "(registro no disponible)")
                )

    def _prevent_duplicate_vendor_bill(self, payload, partner):
        reference = (payload.get("invoice_number") or "").strip()
        if not reference:
            return
        company = self.env.company
        candidates = self.env["account.move"].search(
            [
                ("move_type", "in", ("in_invoice", "in_refund")),
                ("partner_id", "child_of", partner.commercial_partner_id.id),
                ("company_id", "=", company.id),
                ("ref", "!=", False),
                ("state", "!=", "cancel"),
            ]
        )
        reference_key = normalize_delivery_note_number(reference)
        duplicate = candidates.filtered(
            lambda move: normalize_delivery_note_number(move.ref) == reference_key
        )[:1]
        if duplicate:
            raise UserError(
                "Ya existe la factura de proveedor «%s» con número %s (estado: %s). Comprueba que no estés intentando contabilizarla dos veces."
                % (partner.display_name, reference, duplicate.state)
            )

    @staticmethod
    def _is_generic_delivery_note_label(value):
        normalized = normalize_delivery_note_number(value)
        return normalized in {
            "ALBARAN",
            "ALBARA",
            "ALBARANES",
            "NOTADEENTREGA",
            "NOTASDEENTREGA",
            "DELIVERYNOTE",
            "DELIVERYNOTES",
            "REFERENCIA",
            "REFERENCIAS",
            "REF",
        }

    def _resolve_invoice_delivery_notes(self, payload, partner, ignore_move_id=None):
        """Find scanned notes or create traceable, unscanned references."""
        self.ensure_one()
        entries = payload.get("delivery_notes") or payload.get("delivery_note_refs") or []
        if not isinstance(entries, list):
            raise UserError(
                "El campo «delivery_notes» debe ser una lista. Corrígelo o elimínalo si la factura no cita albaranes."
            )
        parsed = []
        seen = set()
        warnings = []
        for entry in entries:
            if isinstance(entry, str):
                number, date_value = entry.strip(), ""
            elif isinstance(entry, dict):
                number = str(entry.get("number") or entry.get("delivery_note_number") or "").strip()
                date_value = str(entry.get("date") or entry.get("delivery_note_date") or "").strip()
            else:
                raise UserError(
                    "Hay una referencia de albarán con formato incorrecto. Usa una lista de números o de objetos {number, date}."
                )
            normalized = normalize_delivery_note_number(number)
            if self._is_generic_delivery_note_label(number):
                warnings.append(
                    "Se ignoró «%s» porque parece una etiqueta de albarán, no un número de referencia. Comprueba el documento original."
                    % number
                )
                continue
            if not normalized:
                raise UserError(
                    "La factura contiene una referencia de albarán vacía o ilegible. Corrígela o elimínala del resultado OCR."
                )
            if normalized in seen:
                raise UserError(
                    "La referencia de albarán «%s» aparece duplicada en la factura. Revisa la extracción antes de continuar."
                    % number
                )
            seen.add(normalized)
            parsed.append({"number": number, "normalized": normalized, "date": date_value})

        # Line-level refs are useful when the invoice groups products below
        # each albarán. Add omitted header refs, but make the repair visible.
        for line in payload.get("lines") or []:
            number = str(line.get("delivery_note_number") or "").strip()
            normalized = normalize_delivery_note_number(number)
            if self._is_generic_delivery_note_label(number):
                warnings.append(
                    "Se ignoró «%s» en una línea porque parece una etiqueta, no un número de albarán. Comprueba la asignación de esa línea."
                    % number
                )
                continue
            if normalized and normalized not in seen:
                parsed.append({"number": number, "normalized": normalized, "date": ""})
                seen.add(normalized)
                warnings.append(
                    "Se añadió la referencia de albarán «%s» indicada en una línea pero ausente de la cabecera OCR. Comprueba la factura."
                    % number
                )

        Reference = self.env["ai.delivery.note.reference"]
        records = Reference.browse()
        for entry in parsed:
            note = Reference.search(
                [
                    ("company_id", "=", self.env.company.id),
                    ("partner_id", "=", partner.id),
                    ("normalized_number", "=", entry["normalized"]),
                ],
                limit=1,
            )
            delivery_date = self._parse_ai_date(entry["date"])
            if entry["date"] and not delivery_date:
                warnings.append(
                    "No se reconoció la fecha del albarán «%s»; la referencia se guardará sin fecha."
                    % entry["number"]
                )
            if not note:
                note = Reference.create(
                    {
                        "name": entry["number"],
                        "partner_id": partner.id,
                        "company_id": self.env.company.id,
                        "delivery_date": delivery_date,
                        "state": "referenced",
                    }
                )
            elif note.name != entry["number"] and note.state == "scanned":
                warnings.append(
                    "La referencia «%s» coincide con el albarán digitalizado «%s» al ignorar puntos y separadores."
                    % (entry["number"], note.name)
                )
            if delivery_date and not note.delivery_date:
                note.delivery_date = delivery_date
            if not note.source_document_id:
                warnings.append(
                    "El albarán «%s» está citado en la factura pero aún no tiene su documento digitalizado; solo se guardará la referencia."
                    % entry["number"]
                )
            else:
                warnings.extend(
                    self._compare_invoice_lines_to_delivery_note(payload, note)
                )
            active_bills = note.invoice_ids.filtered(
                lambda move: move.state != "cancel" and move.id != ignore_move_id
            )
            if active_bills:
                raise UserError(
                    "El albarán «%s» ya está asociado a otra factura de proveedor (%s). Revisa si es una factura duplicada o una rectificativa."
                    % (entry["number"], ", ".join(active_bills.mapped("display_name")))
                )
            records |= note

        if parsed:
            scanned_count = sum(bool(note.source_document_id) for note in records)
            if scanned_count and scanned_count < len(parsed):
                warnings.append(
                    "Solo se han encontrado %s de %s albaranes digitalizados; quedan referencias pendientes de cotejo. La factura no se contabilizará ni validará automáticamente."
                    % (scanned_count, len(parsed))
                )
        return records, list(dict.fromkeys(warnings))

    def action_sync_delivery_note_references(self):
        """Attach refs from a linked OCR invoice without changing accounting data."""
        self.ensure_one()
        if (
            self.state != "done"
            or self.document_type != "invoice"
            or self.linked_model != "account.move"
            or not self.linked_record_id
        ):
            raise UserError(
                "Esta acción solo sirve para añadir referencias de albarán a una factura OCR ya enlazada."
            )
        try:
            payload = json.loads(self.result_json or "{}")
        except (TypeError, ValueError) as exc:
            raise UserError("El resultado OCR no es un JSON válido. Corrígelo antes de continuar.") from exc
        move = self.env["account.move"].browse(self.linked_record_id).exists()
        if not move or move.move_type not in ("in_invoice", "in_refund"):
            raise UserError(
                "La factura enlazada ya no existe o no es una factura de proveedor. No se han modificado datos."
            )
        if not (payload.get("delivery_notes") or payload.get("delivery_note_refs")) and not any(
            line.get("delivery_note_number") for line in payload.get("lines") or []
        ):
            raise UserError(
                "El resultado OCR no contiene números de albarán. Añádelos en «Resultado OCR» y guarda antes de sincronizar."
            )
        note_refs, warnings = self._resolve_invoice_delivery_notes(
            payload, move.partner_id, ignore_move_id=move.id
        )
        for note in note_refs:
            comparison_warnings = self._compare_invoice_lines_to_delivery_note(
                payload, note
            )
            note.review_note = "\n".join(comparison_warnings) or False
        move.ai_delivery_note_ref_ids = [(6, 0, note_refs.ids)]
        line_link_warnings = self._sync_invoice_line_delivery_note_links(
            move, payload, note_refs
        )
        warnings.extend(line_link_warnings)
        if warnings:
            move.message_post(body="<br/>".join(warnings))
        else:
            move.message_post(body="Se han actualizado las referencias de albarán desde el documento OCR.")
        self.write(
            {
                "review_warnings": "\n".join(warnings) or False,
                "linked_model": "account.move",
                "linked_record_id": move.id,
            }
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "view_mode": "form",
            "res_id": move.id,
        }

    def _compare_invoice_lines_to_delivery_note(self, invoice_payload, note):
        """Compare quantities by supplier code, with a conservative description
        fallback for OCR code errors. Ambiguous lines are reported for review,
        never treated as certain quantity mismatches.
        """
        invoice_lines = [
            line
            for line in invoice_payload.get("lines") or []
            if normalize_delivery_note_number(line.get("delivery_note_number"))
            == note.normalized_number
        ]
        if not invoice_lines:
            return [
                "El albarán «%s» está digitalizado, pero el OCR no asignó líneas de factura a esa referencia. Comprueba manualmente las cantidades."
                % note.name
            ]
        try:
            note_lines = json.loads(note.lines_json or "[]")
        except (TypeError, ValueError):
            return [
                "No se pudieron leer las líneas guardadas del albarán «%s». Revisa el documento original."
                % note.name
            ]

        def aggregate(lines):
            groups = {}
            missing_codes = 0
            for index, line in enumerate(lines):
                code = self._normalize_partner_name(line.get("product_code") or "")
                description = self._normalize_partner_name(line.get("description") or "")
                if not code:
                    missing_codes += 1
                    key = ("line", index)
                else:
                    key = ("code", code)
                group = groups.setdefault(
                    key,
                    {
                        "code": code,
                        "description": description,
                        "display_description": (line.get("description") or "").strip(),
                        "quantity": 0.0,
                        "lines": [],
                    },
                )
                group["quantity"] += float(line.get("quantity") or 0)
                group["lines"].append(line)
                if not group["description"]:
                    group["description"] = description
            return groups, missing_codes

        invoiced, invoice_missing = aggregate(invoice_lines)
        received, note_missing = aggregate(note_lines)
        warnings = []
        if invoice_missing or note_missing:
            warnings.append(
                "En el albarán «%s» faltan códigos de artículo en alguna línea; no se puede cotejar automáticamente todo el detalle."
                % note.name
            )
        matched = []
        used_invoice = set()
        used_received = set()

        # Prefer exact supplier item-code matches. Invoice schemas do not
        # reliably contain a unit of measure, so comparing it here would
        # create false quantity differences for otherwise identical items.
        for key in sorted(set(invoiced) & set(received)):
            matched.append((invoiced[key], received[key]))
            used_invoice.add(key)
            used_received.add(key)

        # OCR often confuses one or two digits in long supplier codes. Only
        # use descriptions as a fallback when the best match is clear in both
        # directions; otherwise leave the rows unmatched for human review.
        invoice_left = [key for key in invoiced if key not in used_invoice]
        note_left = [key for key in received if key not in used_received]
        candidates = {}
        for invoice_key in invoice_left:
            invoice_desc = invoiced[invoice_key]["description"]
            if not invoice_desc:
                continue
            scores = []
            for note_key in note_left:
                note_desc = received[note_key]["description"]
                if not note_desc:
                    continue
                score = SequenceMatcher(None, invoice_desc, note_desc).ratio()
                if score >= 0.90:
                    scores.append((score, note_key))
            scores.sort(reverse=True)
            if scores and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.08):
                candidates[invoice_key] = scores[0]
        for invoice_key, (score, note_key) in candidates.items():
            reverse = [key for key, candidate in candidates.items() if candidate[1] == note_key]
            if len(reverse) == 1 and note_key not in used_received:
                matched.append((invoiced[invoice_key], received[note_key]))
                used_invoice.add(invoice_key)
                used_received.add(note_key)

        mismatches = []
        code_mismatches = []
        for invoice_group, note_group in matched:
            invoice_qty = invoice_group["quantity"]
            note_qty = note_group["quantity"]
            if abs(invoice_qty - note_qty) > 0.0001:
                label = invoice_group["code"] or note_group["code"] or invoice_group["description"]
                mismatches.append(
                    "%s: recibida %s, facturada %s" % (label, note_qty, invoice_qty)
                )
            if (
                invoice_group["code"]
                and note_group["code"]
                and invoice_group["code"] != note_group["code"]
            ):
                code_mismatches.append(
                    "%s / %s (%s)"
                    % (
                        note_group["code"],
                        invoice_group["code"],
                        invoice_group["display_description"],
                    )
                )
        if mismatches:
            warnings.append(
                "Hay diferencias de cantidad entre factura y albarán «%s» (%s). Revisa sustituciones, unidades y entregas parciales; el módulo no modifica ni valida la factura automáticamente."
                % (note.name, "; ".join(mismatches[:8]))
            )
        if code_mismatches:
            warnings.append(
                "La descripción permitió cotejar líneas de «%s», pero algunos códigos extraídos no coinciden (%s). Confirma los códigos en los PDF originales."
                % (note.name, "; ".join(code_mismatches[:8]))
            )
        unmatched_invoice = [key for key in invoiced if key not in used_invoice]
        unmatched_note = [key for key in received if key not in used_received]
        if unmatched_invoice or unmatched_note:
            warnings.append(
                "Quedan líneas sin correspondencia inequívoca entre factura y albarán «%s». Revisa artículos o descripciones antes de dar el cotejo por válido."
                % note.name
            )

        price_differences = []
        for invoice_group, note_group in matched:
            if len(invoice_group["lines"]) != 1 or len(note_group["lines"]) != 1:
                continue
            line = invoice_group["lines"][0]
            note_line = note_group["lines"][0]
            if line.get("unit_price") is None or note_line.get("unit_price") is None:
                continue
            invoice_net = float(line.get("unit_price") or 0) * (
                1 - float(line.get("discount_percent") or 0) / 100.0
            )
            note_net = float(note_line.get("unit_price") or 0) * (
                1 - float(note_line.get("discount_percent") or 0) / 100.0
            )
            currency = self.env["res.currency"].search(
                [("name", "=", (invoice_payload.get("currency") or "").upper())],
                limit=1,
            ) or self.env.company.currency_id
            if currency.compare_amounts(invoice_net, note_net):
                price_differences.append(
                    "%s: albarán %.2f, factura %.2f"
                    % (
                        invoice_group["code"]
                        or note_group["code"]
                        or invoice_group["display_description"],
                        note_net,
                        invoice_net,
                    )
                )
        if price_differences:
            warnings.append(
                "Los precios netos difieren entre el albarán valorado «%s» y la factura (%s). La factura conserva sus precios; confirma si el cambio es correcto."
                % (note.name, "; ".join(price_differences[:8]))
            )
        return warnings

    def _sync_invoice_line_delivery_note_links(self, move, payload, note_refs):
        """Update only the traceability field on posted/draft invoice lines.

        This does not alter quantities, prices, taxes, or accounting entries.
        Matching must be unambiguous; otherwise no line links are changed.
        """
        note_by_number = {note.normalized_number: note for note in note_refs}
        payload_lines = list(payload.get("lines") or [])
        invoice_lines = move.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        )
        if len(payload_lines) != len(invoice_lines):
            return [
                "Se actualizaron las referencias generales, pero no se cambiaron las referencias por línea: la factura tiene distinto número de líneas que el OCR."
            ]

        remaining = list(payload_lines)
        assignments = []
        for invoice_line in invoice_lines:
            code_match = re.search(r"^\[([^\]]+)\]", invoice_line.name or "")
            invoice_code = self._normalize_partner_name(
                code_match.group(1) if code_match else ""
            )
            candidates = [
                line for line in remaining
                if invoice_code
                and self._normalize_partner_name(line.get("product_code") or "") == invoice_code
                and abs(float(line.get("quantity") or 0) - invoice_line.quantity) < 0.0001
                and abs(float(line.get("subtotal") or 0) - invoice_line.price_subtotal) < 0.011
            ]
            if len(candidates) != 1:
                return [
                    "Se actualizaron las referencias generales, pero no se cambiaron las referencias por línea: una línea no se pudo emparejar de forma única con el OCR."
                ]
            payload_line = candidates[0]
            remaining.remove(payload_line)
            note_number = payload_line.get("delivery_note_number") or ""
            note = note_by_number.get(normalize_delivery_note_number(note_number))
            assignments.append((invoice_line, note))
        if remaining:
            return [
                "Se actualizaron las referencias generales, pero no se cambiaron las referencias por línea: quedaron líneas OCR sin emparejar."
            ]
        for invoice_line, note in assignments:
            if invoice_line.ai_delivery_note_ref_id != note:
                invoice_line.ai_delivery_note_ref_id = note.id if note else False
        return []

    def _link_delivery_note(self, payload):
        self.ensure_one()
        try:
            validate_delivery_note_payload(payload)
        except InvoiceSchemaError as exc:
            raise UserError(
                self._schema_error_message_es(exc, "albarán")
            ) from exc
        number = (payload.get("delivery_note_number") or "").strip()
        normalized = normalize_delivery_note_number(number)
        if self._is_generic_delivery_note_label(number):
            raise UserError(
                "La IA leyó «%s», que parece la etiqueta «albarán» y no su número. Corrige el número en Resultado OCR antes de registrarlo."
                % number
            )
        if not normalized:
            raise UserError(
                "No se ha leído el número del albarán. Corrígelo en «Resultado OCR» antes de registrarlo."
            )
        if not payload.get("lines"):
            raise UserError(
                "El albarán no contiene líneas de producto. Comprueba el archivo y vuelve a procesarlo."
            )
        for index, line in enumerate(payload["lines"], 1):
            quantity = line.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
                raise UserError(
                    "La cantidad de la línea %s debe ser numérica y mayor que cero. Corrige el albarán antes de registrarlo."
                    % index
                )
            if not (line.get("description") or "").strip() and not (
                line.get("product_code") or ""
            ).strip():
                raise UserError(
                    "La línea %s no tiene descripción ni código de artículo. Corrige el albarán antes de registrarlo."
                    % index
                )
            for key in ("unit_price", "discount_percent", "subtotal"):
                value = line.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise UserError(
                        "El dato opcional «%s» de la línea %s no es numérico. Déjalo vacío si el albarán no muestra precios."
                        % (key, index)
                    )
                if value < 0 or (key == "discount_percent" and value > 100):
                    raise UserError(
                        "El %s de la línea %s está fuera de rango. El descuento debe ser 0–100 y los importes no pueden ser negativos."
                        % ("descuento" if key == "discount_percent" else "importe", index)
                    )
        partner = self._find_or_create_supplier(payload.get("supplier") or {})
        Reference = self.env["ai.delivery.note.reference"]
        note = Reference.search(
            [
                ("company_id", "=", self.env.company.id),
                ("partner_id", "=", partner.id),
                ("normalized_number", "=", normalized),
            ],
            limit=1,
        )
        if note and note.source_document_id and note.source_document_id != self:
            raise UserError(
                "El albarán «%s» del proveedor %s ya se digitalizó en el documento «%s». No lo registres dos veces."
                % (number, partner.display_name, note.source_document_id.display_name)
            )
        delivery_date_text = (payload.get("delivery_note_date") or "").strip()
        delivery_date = self._parse_ai_date(delivery_date_text)
        if delivery_date_text and not delivery_date:
            raise UserError(
                "La fecha del albarán «%s» no se reconoce. Corrígela con formato DD/MM/AAAA, DD/MM/AA o AAAA-MM-DD."
                % number
            )
        warnings = []
        if note and note.delivery_date and delivery_date and note.delivery_date != delivery_date:
            warnings.append(
                "La fecha del albarán digitalizado (%s) difiere de la fecha leída en la factura (%s). Se conservará la fecha del albarán; comprueba ambas."
                % (delivery_date, note.delivery_date)
            )
        values = {
            "name": number,
            "partner_id": partner.id,
            "company_id": self.env.company.id,
            "delivery_date": delivery_date,
            "purchase_order_ref": (payload.get("purchase_order") or "").strip(),
            "state": "scanned",
            "source_document_id": self.id,
            "lines_json": json.dumps(payload["lines"], ensure_ascii=False, indent=2),
        }
        if note:
            note.write(values)
        else:
            note = Reference.create(values)
        linked_invoice_docs = self.env["ai.document"].search(
            [
                ("linked_model", "=", "account.move"),
                ("linked_record_id", "in", note.invoice_ids.ids),
            ]
        )
        for invoice_doc in linked_invoice_docs:
            try:
                invoice_payload = json.loads(invoice_doc.result_json or "{}")
            except (TypeError, ValueError):
                continue
            comparison_warnings = invoice_doc._compare_invoice_lines_to_delivery_note(
                invoice_payload, note
            )
            if comparison_warnings:
                warnings.extend(comparison_warnings)
                note.review_note = "\n".join(comparison_warnings)
                invoice_doc.review_warnings = "\n".join(
                    filter(None, [invoice_doc.review_warnings] + comparison_warnings)
                )
        warning = (
            "Albarán digitalizado y relacionado con su referencia de proveedor. "
            "No se ha creado ni validado una recepción de inventario; eso requiere revisar el pedido y las cantidades."
        )
        if warnings:
            warning += "\n" + "\n".join(dict.fromkeys(warnings))
            note.review_note = "\n".join(dict.fromkeys(warnings))
        self.write(
            {
                "delivery_note_reference_id": note.id,
                "linked_model": "ai.delivery.note.reference",
                "linked_record_id": note.id,
                "review_warnings": warning,
                "state": "done",
            }
        )
        return note

    # ------------------------------------------------------------------
    # Human review workflow
    # ------------------------------------------------------------------
    def action_mark_reviewed(self):
        """A human confirms the extracted data is correct. Does NOT create
        Odoo records by itself — that is a separate, explicit step guarded
        by auto_create_records."""
        for rec in self:
            if rec.state not in ("needs_review", "reviewed"):
                raise UserError(
                    "Solo se pueden confirmar documentos en estado «Pendiente de revisión» o «Revisado»."
                )
            rec.state = "reviewed"

    def action_link_records(self):
        """Create/link the underlying Odoo records (e.g. account.move) from
        the reviewed JSON. Only allowed after human review, and only creates
        new supplier/product records if auto_create_records is explicitly
        enabled on the document."""
        for rec in self:
            if rec.state != "reviewed":
                raise UserError(
                    "Antes de crear registros, confirma la revisión del documento."
                )
            rec._link_records()

    def _link_records(self):
        """Route the reviewed OCR payload using its detected type, not the
        stale type selection that may have been present before OCR."""
        self.ensure_one()
        if not self.result_json:
            raise UserError("El documento no contiene datos OCR. Procésalo o completa el resultado antes de continuar.")
        try:
            payload = json.loads(self.result_json)
        except (TypeError, ValueError) as exc:
            raise UserError(
                "El resultado OCR no es un JSON válido. Corrige su formato antes de continuar."
            ) from exc

        detected_type = self._normalize_document_type(
            payload.get("document_type") if isinstance(payload, dict) else None
        )
        if detected_type not in ("invoice", "delivery_note"):
            raise UserError(
                "El tipo del documento no está identificado como factura o albarán en el resultado OCR. Vuelve a procesarlo o corrige el campo document_type."
            )
        if self.document_type != detected_type:
            self.document_type = detected_type
            self.review_warnings = "\n".join(
                filter(
                    None,
                    [
                        self.review_warnings,
                        "Al enlazar, se ha usado el tipo detectado en el resultado OCR (%s) y se ha corregido la selección del formulario."
                        % detected_type,
                    ],
                )
            )

        if detected_type == "delivery_note":
            return self._link_delivery_note(payload)

        self._validate_invoice_payload_for_link(payload)
        self._prevent_duplicate_invoice_file()
        partner = self._find_or_create_supplier(payload.get("supplier") or {})
        self._prevent_duplicate_vendor_bill(payload, partner)
        note_refs, warnings = self._resolve_invoice_delivery_notes(payload, partner)
        inferred_tax, inferred_rate = self._infer_spanish_goods_purchase_tax(
            payload, partner
        )
        if inferred_tax:
            for line in payload.get("lines") or []:
                line["tax"] = inferred_rate
            payload["tax_inference"] = (
                "Se infirió el IVA general español del 21%% a partir del resumen de la factura y se asignó el impuesto «%s»."
                % inferred_tax.name
            )
        move = self._create_vendor_bill(payload, partner, inferred_tax, note_refs)
        if note_refs:
            move.ai_delivery_note_ref_ids = [(6, 0, note_refs.ids)]
        if warnings:
            move.message_post(body="<br/>".join(warnings))
        self.write(
            {
                "result_json": json.dumps(payload, ensure_ascii=False, indent=2),
                "review_warnings": "\n".join(warnings) or False,
                "linked_model": "account.move",
                "linked_record_id": move.id,
                "state": "done",
            }
        )

    def _find_or_create_supplier(self, supplier_data):
        Partner = self.env["res.partner"]
        supplier_data = dict(supplier_data or {})
        supplier_data["email"] = self._plain_contact_value(supplier_data.get("email"))
        supplier_data["website"] = self._plain_contact_value(supplier_data.get("website"))
        # OCR sometimes puts an email address (or a mailto link) in website.
        # Never save that as a supplier website.
        if supplier_data["website"] and (
            supplier_data["website"].lower().startswith("mailto:")
            or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", supplier_data["website"])
        ):
            supplier_data["website"] = ""
        supplier_vat = supplier_data.get("vat") or False
        supplier_name = (supplier_data.get("name") or "").strip()
        if not supplier_name:
            raise UserError("No se ha identificado al proveedor. Revisa el nombre y el NIF/CIF extraídos.")
        if (
            self.env.company.country_id.code == "ES"
            and self._is_spanish_company_vat(supplier_vat)
            and not self._is_valid_spanish_company_vat(supplier_vat)
        ):
            raise UserError(
                "El NIF/CIF extraído «%s» no supera la comprobación del dígito de control español. "
                "Corrígelo en el resultado OCR antes de continuar."
                % supplier_vat
            )
        partner = (
            Partner.search([("vat", "=", supplier_vat)], limit=1)
            if supplier_vat
            else Partner.browse()
        )

        possible_partners = Partner.browse()
        if not partner:
            # Name matching is a fallback only; an ambiguous or conflicting
            # tax ID must never silently select a contact.
            query = Partner.search([("name", "ilike", supplier_name)])
            key = self._normalize_partner_name(supplier_name)
            possible_partners = query.filtered(
                lambda item: self._normalize_partner_name(item.name) == key
            )
            commercial = possible_partners.mapped("commercial_partner_id")
            possible_partners = commercial.sorted("id")
            unique = Partner.browse()
            seen_ids = set()
            for item in possible_partners:
                if item.id not in seen_ids:
                    unique |= item
                    seen_ids.add(item.id)
            possible_partners = unique
            if supplier_vat and possible_partners:
                conflicting = possible_partners.filtered(
                    lambda item: item.vat and item.vat.upper() != supplier_vat.upper()
                )
                if conflicting:
                    raise UserError(
                        "El proveedor «%s» ya existe en Contactos con otro NIF/CIF (%s), distinto del extraído (%s). "
                        "Revisa ambas fichas y corrige el OCR; no se creará un proveedor duplicado."
                        % (
                            supplier_name,
                            ", ".join(conflicting.mapped("vat")),
                            supplier_vat,
                        )
                    )
            elif not supplier_vat and len(possible_partners) > 1:
                raise UserError(
                    "Hay varios proveedores llamados «%s» y el albarán no contiene NIF/CIF. "
                    "Selecciona un proveedor inequívoco añadiendo su NIF/CIF al resultado OCR."
                    % supplier_name
                )
            elif len(possible_partners) == 1:
                partner = possible_partners

        if not partner and not self.auto_create_records:
            raise UserError(
                "No se encontró el proveedor «%s» (NIF/CIF: %s) y la creación automática está desactivada. "
                "Crea o enlaza el contacto y vuelve a intentarlo."
                % (supplier_name, supplier_vat or "no indicado")
            )

        if not partner and self.auto_create_records:
            if not supplier_vat:
                raise UserError(
                    "El documento no incluye NIF/CIF y no se encontró un único proveedor existente llamado «%s». "
                    "Para evitar duplicados, crea o selecciona primero el contacto en Odoo."
                    % supplier_name
                )
            partner = Partner.create(
                {
                    "name": supplier_name,
                    "vat": supplier_vat,
                    "street": supplier_data.get("address") or False,
                    "phone": supplier_data.get("phone") or False,
                    "email": supplier_data.get("email") or False,
                    "website": supplier_data.get("website") or False,
                    "supplier_rank": 1,
                }
            )
        elif partner and self.auto_create_records:
            # Backfill contact details the existing record is missing,
            # never overwrite data that's already there.
            updates = {}
            if supplier_vat and not partner.vat:
                updates["vat"] = supplier_vat
            for field, key in (
                ("street", "address"),
                ("phone", "phone"),
                ("email", "email"),
                ("website", "website"),
            ):
                if not partner[field] and supplier_data.get(key):
                    updates[field] = supplier_data[key]
            if updates:
                partner.write(updates)

        return partner

    @staticmethod
    def _plain_contact_value(value):
        """Turn simple Markdown mail/web links from OCR into plain addresses."""
        if not isinstance(value, str):
            return ""
        value = value.strip()
        match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", value)
        if match:
            value = match.group(2).strip()
        if value.lower().startswith("mailto:"):
            value = value[7:].strip()
        return value

    @staticmethod
    def _normalize_partner_name(value):
        value = unicodedata.normalize("NFKD", value or "")
        value = "".join(char for char in value if not unicodedata.combining(char))
        return re.sub(r"[^A-Z0-9]", "", value.upper())

    def _find_purchase_tax(self, tax_rate):
        """Best-effort match of the AI's numeric 'tax' field (assumed to be
        a percentage, e.g. 21 for 21%) to a configured purchase tax. Returns
        an empty recordset if there's no unambiguous match — lines are then
        left without a tax for a human to set during review, rather than
        guessing wrong."""
        if not tax_rate:
            return self.env["account.tax"].browse()
        taxes = self.env["account.tax"].search(
            [
                ("company_id", "=", self.env.company.id),
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", tax_rate),
                ("active", "=", True),
            ]
        )
        return taxes[:1] if len(taxes) == 1 else self.env["account.tax"].browse()

    def _find_invoice_tax_component(self, component, partner=None):
        """Resolve an explicit IVA or Spanish equivalence-surcharge rate.

        Spanish domestic VAT taxes are selected by their localized standard
        goods/service label. Surcharges are selected by Odoo's l10n_es_type,
        avoiding collisions with ordinary VAT at the same percentage.
        """
        Tax = self.env["account.tax"].with_context(lang="es_ES")
        kind = component.get("kind")
        if kind not in ("vat", "equivalence_surcharge"):
            raise UserError(
                "El documento contiene un impuesto no automatizado (%s). Revísalo y asigna ese impuesto manualmente antes de crear la factura."
                % (component.get("label") or kind or "sin identificar")
            )
        rate = float(component.get("rate") or 0)
        if (
            self.env.company.country_id.code != "ES"
            or not partner
            or partner.country_id.code != "ES"
            or rate == 0
        ):
            return Tax.browse()
        domain = [
            ("company_id", "=", self.env.company.id),
            ("type_tax_use", "=", "purchase"),
            ("amount_type", "=", "percent"),
            ("amount", "=", rate),
            ("active", "=", True),
        ]
        if kind == "equivalence_surcharge":
            domain.append(("l10n_es_type", "=", "recargo"))
            taxes = Tax.search(domain)
            return taxes[:1] if len(taxes) == 1 else Tax.browse()
        scope = component.get("scope") or "goods"
        tax_scope = "service" if scope == "service" else "consu"
        domain.extend(
            [("l10n_es_type", "=", "sujeto"), ("tax_scope", "=", tax_scope)]
        )
        taxes = Tax.search(domain)
        suffix = "S" if scope == "service" else "B"
        expected_name = "%s%% %s" % ("%g" % rate, suffix)
        standard_tax = taxes.filtered(lambda tax: (tax.name or "").strip() == expected_name)
        return standard_tax[:1] if len(standard_tax) == 1 else Tax.browse()

    @staticmethod
    def _is_spanish_company_vat(vat):
        """Recognize the shape of Spanish company CIFs (without asserting
        that the extracted control digit is correct)."""
        vat = re.sub(r"[\s.\-]", "", (vat or "").upper())
        if vat.startswith("ES"):
            vat = vat[2:]
        return bool(re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", vat))

    @staticmethod
    def _is_valid_spanish_company_vat(vat):
        """Validate the check digit/letter of a Spanish company CIF."""
        vat = re.sub(r"[\s.\-]", "", (vat or "").upper())
        if vat.startswith("ES"):
            vat = vat[2:]
        if not re.fullmatch(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]", vat):
            return False

        digits = vat[1:8]
        total = 0
        for index, digit in enumerate(digits):
            value = int(digit)
            if index % 2 == 0:
                doubled = value * 2
                total += doubled // 10 + doubled % 10
            else:
                total += value
        control = (10 - total % 10) % 10
        control_letter = "JABCDEFGHI"[control]
        prefix = vat[0]
        if prefix in "ABEH":
            return vat[-1] == str(control)
        if prefix in "PQRS":
            return vat[-1] == control_letter
        return vat[-1] in (str(control), control_letter)

    def _infer_spanish_goods_purchase_tax(self, payload, partner):
        """Infer only a single-rate, standard Spanish goods VAT when all
        line tax rates are missing, the invoice header reconciles, and an
        unambiguous company purchase tax named e.g. '21% B' exists.

        This deliberately does not infer service, EU, exempt, investment,
        or mixed-rate taxes from a percentage alone.
        """
        self.ensure_one()
        company = self.env.company
        lines = payload.get("lines") or []
        supplier_vat = (payload.get("supplier") or {}).get("vat") or partner.vat
        if (
            company.country_id.code != "ES"
            or not self._is_spanish_company_vat(supplier_vat)
            or not lines
            or any("tax_components" in line for line in lines)
            or any(not (line.get("product_code") or "").strip() for line in lines)
        ):
            return self.env["account.tax"].browse(), None

        currency_code = self._normalize_currency_code(payload.get("currency"))
        # This conservative shortcut is limited to domestic Spanish invoices
        # in EUR with only the standard goods rate (or missing line rates).
        if currency_code != "EUR":
            return self.env["account.tax"].browse(), None
        line_rates = [float(line.get("tax") or 0) for line in lines]
        if any(abs(rate) > 0.001 and abs(rate - 21.0) > 0.001 for rate in line_rates):
            return self.env["account.tax"].browse(), None

        untaxed_total = float(payload.get("untaxed_total") or 0)
        tax_total = float(payload.get("tax_total") or 0)
        invoice_total = float(payload.get("total") or 0)
        if untaxed_total <= 0 or tax_total <= 0:
            return self.env["account.tax"].browse(), None

        currency = self.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            return self.env["account.tax"].browse(), None
        line_subtotal = sum(float(line.get("subtotal") or 0) for line in lines)
        if (
            currency.compare_amounts(line_subtotal, untaxed_total)
            or currency.compare_amounts(untaxed_total + tax_total, invoice_total)
        ):
            return self.env["account.tax"].browse(), None

        rate = round(tax_total / untaxed_total * 100, 2)
        # Restrict this automatic mapping to the common 21% domestic-goods
        # case. Other rates and tax regimes must be reviewed explicitly.
        if abs(rate - 21.0) > 0.02:
            return self.env["account.tax"].browse(), None

        taxes = self.env["account.tax"].with_context(lang="es_ES").search(
            [
                ("company_id", "=", company.id),
                ("country_id.code", "=", "ES"),
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", 21),
                ("l10n_es_type", "=", "sujeto"),
            ]
        )
        standard_goods_tax = taxes.filtered(
            lambda tax: (tax.name or "").strip() == "21% B"
        )
        if len(standard_goods_tax) != 1:
            return self.env["account.tax"].browse(), None
        return standard_goods_tax, 21.0

    def _create_vendor_bill(self, payload, partner, inferred_tax=None, note_refs=None):
        AccountMove = self.env["account.move"]
        Currency = self.env["res.currency"]
        note_refs = note_refs or self.env["ai.delivery.note.reference"].browse()
        note_by_number = {note.normalized_number: note for note in note_refs}

        currency_code = self._normalize_currency_code(payload.get("currency"))
        currency = (
            Currency.search([("name", "=", currency_code)], limit=1)
            if currency_code
            else Currency.browse()
        )

        line_vals = []
        for line in payload.get("lines") or []:
            taxes = self.env["account.tax"].browse()
            components = line.get("tax_components")
            if components is not None:
                for component in components:
                    component_tax = self._find_invoice_tax_component(component, partner)
                    if not component_tax:
                        kind = component.get("kind")
                        label = "recargo de equivalencia" if kind == "equivalence_surcharge" else "IVA"
                        raise UserError(
                            "No se encontró un único impuesto de compra configurado para %s del %.4g%% (%s). Revisa país, régimen y tipo de producto del impuesto en Odoo; no se ha creado la factura."
                            % (label, float(component.get("rate") or 0), line.get("description") or "línea sin descripción")
                        )
                    taxes |= component_tax
            else:
                tax = self._find_purchase_tax(line.get("tax"))
                if not tax and inferred_tax:
                    tax = inferred_tax
                if line.get("tax") and not tax:
                    raise UserError(
                        "No se puede asignar con seguridad el IVA del %.2f%% a la línea «%s». "
                        "Hay varios impuestos de compra con ese porcentaje o ninguno está configurado. "
                        "Revisa el impuesto correcto en Odoo antes de crear la factura."
                        % (float(line["tax"]), line.get("description") or line.get("product_code") or "sin descripción")
                    )
                taxes |= tax
            if inferred_tax and components is None and not taxes:
                taxes |= inferred_tax
            description = (line.get("description") or "/").strip()
            product_code = (line.get("product_code") or "").strip()
            note_number = (line.get("delivery_note_number") or "").strip()
            note = note_by_number.get(normalize_delivery_note_number(note_number))
            # Supplier article codes are not necessarily our internal SKU.
            # Preserve them visibly on the bill instead of guessing a product.
            line_name = (
                "[%s] %s" % (product_code, description)
                if product_code
                else description
            )
            invoice_type_str = str(payload.get("invoice_type") or "").strip().lower()
            is_refund = (
                float(payload.get("total") or 0) < 0
                or float(payload.get("untaxed_total") or 0) < 0
                or invoice_type_str in ("refund", "credit_note", "rectificativa", "abono")
                or any(
                    isinstance(l.get("subtotal"), (int, float)) and l.get("subtotal") < 0
                    for l in (payload.get("lines") or [])
                )
            )

            # In Odoo account.move, 'in_refund' expects positive quantity and price_unit;
            # the move type itself represents a reversal (credit note to vendor).
            raw_qty = line.get("quantity") or 1
            raw_price = line.get("unit_price") or 0
            if is_refund:
                line_qty = abs(raw_qty)
                line_price = abs(raw_price)
            else:
                line_qty = raw_qty
                line_price = raw_price

            line_vals.append(
                (
                    0,
                    0,
                    {
                        "name": line_name,
                        "quantity": line_qty,
                        "price_unit": line_price,
                        "discount": line.get("discount_percent") or 0,
                        "tax_ids": [(6, 0, taxes.ids)] if taxes else False,
                        "ai_delivery_note_ref_id": note.id if note else False,
                    },
                )
            )

        invoice_type_str = str(payload.get("invoice_type") or "").strip().lower()
        is_refund = (
            float(payload.get("total") or 0) < 0
            or float(payload.get("untaxed_total") or 0) < 0
            or invoice_type_str in ("refund", "credit_note", "rectificativa", "abono")
            or any(
                isinstance(l.get("subtotal"), (int, float)) and l.get("subtotal") < 0
                for l in (payload.get("lines") or [])
            )
        )
        move_type = "in_refund" if is_refund else "in_invoice"

        move_vals = {
            "move_type": move_type,
            "partner_id": partner.id,
            "invoice_origin": ", ".join(
                value
                for value in (
                    (payload.get("purchase_order") or "").strip(),
                    self.name,
                )
                if value
            ),
            "ref": payload.get("invoice_number") or False,
            "invoice_line_ids": line_vals,
        }
        if currency:
            move_vals["currency_id"] = currency.id
        invoice_date = self._parse_ai_date(payload.get("invoice_date"))
        if invoice_date:
            move_vals["invoice_date"] = invoice_date
        due_date = self._parse_ai_date(payload.get("due_date"))
        if due_date:
            move_vals["invoice_date_due"] = due_date

        try:
            move = AccountMove.create(move_vals)
        except Exception as exc:  # noqa: BLE001 - surface an actionable Odoo failure
            _logger.exception("Unable to create draft vendor bill from %s", self.name)
            raise UserError(
                "Odoo no pudo crear el borrador de factura. No se ha contabilizado nada. "
                "Revisa la configuración contable del proveedor, el diario y los impuestos. Detalle técnico: %s"
                % exc
            ) from exc
        expected_total = payload.get("total")
        if expected_total is not None and abs(float(expected_total)) > 0:
            currency = move.currency_id or move.company_currency_id
            expected_untaxed = abs(float(payload.get("untaxed_total") or 0))
            expected_tax = abs(float(payload.get("tax_total") or 0))
            expected_total_abs = abs(float(expected_total))
            if (
                currency.compare_amounts(move.amount_total, expected_total_abs)
                or (
                    expected_untaxed > 0
                    and currency.compare_amounts(move.amount_untaxed, expected_untaxed)
                )
                or currency.compare_amounts(move.amount_tax, expected_tax)
            ):
                raise UserError(
                    "La factura no se ha creado porque los importes calculados por Odoo no cuadran con el OCR. "
                    "Odoo: base %.2f, IVA %.2f, total %.2f. Factura: base %.2f, IVA %.2f, total %.2f. "
                    "Revisa líneas, descuentos, impuestos, recargos y redondeos."
                    % (
                        move.amount_untaxed,
                        move.amount_tax,
                        move.amount_total,
                        expected_untaxed,
                        expected_tax,
                        expected_total_abs,
                    )
                )
        return move

    @staticmethod
    def _normalize_currency_code(value):
        """Convert common printed currency names/symbols to ISO 4217 codes.

        Ambiguous symbols such as bare '$' are deliberately left unchanged so
        the system never silently chooses USD/CAD/AUD for an unclear invoice.
        """
        if not isinstance(value, str):
            return ""
        raw = value.strip().upper()
        aliases = {
            "€": "EUR",
            "EUR": "EUR",
            "EURO": "EUR",
            "EUROS": "EUR",
            "US$": "USD",
            "USD": "USD",
            "US DOLLAR": "USD",
            "US DOLLARS": "USD",
            "DÓLAR ESTADOUNIDENSE": "USD",
            "DÓLARES ESTADOUNIDENSES": "USD",
            "£": "GBP",
            "GBP": "GBP",
            "LIBRA ESTERLINA": "GBP",
            "LIBRAS ESTERLINAS": "GBP",
        }
        if raw in aliases:
            return aliases[raw]
        normalized = unicodedata.normalize("NFKD", raw)
        normalized = "".join(
            char for char in normalized if not unicodedata.combining(char)
        )
        compact = re.sub(r"[^A-Z0-9]", "", normalized)
        return {
            "DOLARESTADOUNIDENSE": "USD",
            "DOLARESESTADOUNIDENSES": "USD",
            "LIBRAESTERLINA": "GBP",
            "LIBRASESTERLINAS": "GBP",
        }.get(compact, raw)

    @staticmethod
    def _parse_ai_date(value):
        """AI-extracted dates arrive in whatever format the document used
        (DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD, and Spanish DD/MM/YY...). Try
        unambiguous day-first formats before the US month-first format. For
        two-digit years, Python's standard pivot maps 00-68 to 2000-2068 and
        69-99 to 1969-1999. If none match, leave the field unset rather than
        guess wrong — a human can fill it in during review."""
        if not value:
            return None
        if not isinstance(value, str):
            return None
        value = value.strip()
        for fmt in (
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%d/%m/%y",
            "%d-%m-%y",
            "%d.%m.%Y",
            "%d.%m.%y",
            "%m/%d/%Y",
        ):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None
