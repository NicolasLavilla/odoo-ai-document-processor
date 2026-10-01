# -*- coding: utf-8 -*-
import re
import json
import math
import unicodedata

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


def normalize_delivery_note_number(value):
    """Normalize external refs such as '2.477.159' and '2477159'."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^A-Z0-9]", "", value.upper())


class AiDeliveryNoteReference(models.Model):
    _name = "ai.delivery.note.reference"
    _description = "Supplier Delivery Note Reference"
    _order = "delivery_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(string="N.º de albarán del proveedor", required=True, index=True)
    normalized_number = fields.Char(
        string="Número normalizado", required=True, index=True, copy=False
    )
    partner_id = fields.Many2one(
        "res.partner", string="Proveedor", required=True, index=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    delivery_date = fields.Date(string="Fecha del albarán", index=True)
    purchase_order_ref = fields.Char(string="Referencia del pedido")
    purchase_order_id = fields.Many2one(
        "purchase.order",
        string="Pedido de compra de Odoo",
        check_company=True,
        domain="[('company_id', '=', company_id), ('state', 'in', ('purchase', 'done'))]",
        help="Selecciona el pedido correcto si el proveedor no lo imprimió o el OCR no pudo leerlo.",
    )
    stock_picking_id = fields.Many2one(
        "stock.picking",
        string="Recepción de inventario",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    picking_type_id = fields.Many2one(
        "stock.picking.type",
        string="Tipo de operación de entrada",
        domain="[('code', '=', 'incoming'), ('company_id', '=', company_id)]",
        check_company=True,
        help="Necesario solo si la empresa tiene varios almacenes de entrada.",
    )
    state = fields.Selection(
        [("referenced", "Citado en factura"), ("scanned", "Albarán digitalizado")],
        string="Estado",
        required=True,
        default="referenced",
        index=True,
    )
    source_document_id = fields.Many2one(
        "ai.document", string="Documento OCR del albarán", ondelete="set null"
    )
    invoice_ids = fields.Many2many(
        "account.move",
        "ai_delivery_note_account_move_rel",
        "delivery_note_id",
        "move_id",
        string="Facturas de proveedor",
        copy=False,
    )
    lines_json = fields.Text(string="Líneas extraídas del albarán")
    review_note = fields.Text(string="Nota de revisión")

    _delivery_note_supplier_ref_unique = models.Constraint(
        "unique(company_id, partner_id, normalized_number)",
        "Ya existe un albarán con este número para este proveedor y empresa.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["normalized_number"] = normalize_delivery_note_number(
                vals.get("name")
            )
            if not vals["normalized_number"]:
                raise ValidationError(_("El número del albarán está vacío o no es válido."))
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if "name" in vals:
            vals["normalized_number"] = normalize_delivery_note_number(vals["name"])
            if not vals["normalized_number"]:
                raise ValidationError(_("El número del albarán está vacío o no es válido."))
        return super().write(vals)

    @api.constrains("name", "normalized_number")
    def _check_normalized_number(self):
        for record in self:
            expected = normalize_delivery_note_number(record.name)
            if not expected or record.normalized_number != expected:
                raise ValidationError(
                    _("El número normalizado del albarán no coincide con su referencia.")
                )

    @staticmethod
    def _normalize_label(value):
        value = unicodedata.normalize("NFKD", value or "")
        value = "".join(char for char in value if not unicodedata.combining(char))
        return re.sub(r"[^A-Z0-9]", "", value.upper())

    def _resolve_stock_product(self, line, order):
        """Resolve a delivery-note line using its supplier code or exact PO text.

        The PO is the authority for which Odoo product was ordered. Never create
        products from OCR data or pick a product from a fuzzy description.
        """
        self.ensure_one()
        Product = self.env["product.product"]
        code = (line.get("product_code") or "").strip()
        products = Product.browse()
        if code:
            products |= Product.search(
                [("default_code", "=", code), ("active", "=", True)]
            )
            supplier_infos = self.env["product.supplierinfo"].search(
                [
                    ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
                    ("product_code", "=", code),
                    ("product_tmpl_id.active", "=", True),
                ]
            )
            for info in supplier_infos:
                products |= info.product_id or info.product_tmpl_id.product_variant_id
        products = products.filtered(lambda product: product in order.order_line.product_id)

        # A PO may be the only reliable bridge when the supplier's printed
        # code has not yet been configured in Odoo. Exact text matches only.
        if not products:
            description = self._normalize_label(line.get("description"))
            if description:
                matching_lines = order.order_line.filtered(
                    lambda po_line: not po_line.display_type
                    and description
                    in {
                        self._normalize_label(po_line.name),
                        self._normalize_label(po_line.product_id.display_name),
                        self._normalize_label(po_line.product_id.name),
                    }
                )
                products = matching_lines.product_id

        if len(products) != 1:
            return self.env["product.product"].browse(), (
                "el código «%s» (%s) no identifica un único producto del pedido «%s»"
                % (code or "sin código", line.get("description") or "sin descripción", order.name)
            )
        product = products
        if not product.is_storable:
            return product, (
                "el producto «%s» no está configurado como almacenable en Odoo"
                % product.display_name
            )
        return product, None

    def _resolve_purchase_line(self, order, product, line):
        candidates = order.order_line.filtered(
            lambda po_line: not po_line.display_type and po_line.product_id == product
        )
        if len(candidates) != 1:
            return self.env["purchase.order.line"].browse(), (
                "el producto «%s» aparece en %s líneas del pedido «%s»; hay que aclarar cuál corresponde"
                % (product.display_name, len(candidates), order.name)
            )
        po_line = candidates
        ocr_uom = (line.get("unit_of_measure") or "").strip()
        if ocr_uom and self._normalize_label(ocr_uom) != self._normalize_label(po_line.product_uom_id.name):
            return po_line, (
                "la unidad «%s» de «%s» no coincide con la unidad «%s» del pedido; revisa la conversión"
                % (ocr_uom, product.display_name, po_line.product_uom_id.name)
            )
        return po_line, None

    def action_prepare_inventory_receipt(self):
        """Link notes to the PO's native receipt and stage their OCR quantities.

        The stock move's done quantity is prefilled for review, but stock is not
        changed until a stock user validates Odoo's standard receipt. Multiple
        notes for the same PO and open receipt are accumulated idempotently.
        """
        self.ensure_one()
        if self.stock_picking_id and self.stock_picking_id.state == "done":
            return self._open_stock_picking(self.stock_picking_id)
        if self.stock_picking_id and self.stock_picking_id.state == "cancel":
            raise UserError(_("La recepción vinculada está cancelada. Crea una nueva recepción desde el pedido antes de volver a enlazar este albarán."))
        if self.state != "scanned" or not self.source_document_id:
            raise UserError(_("Solo se pueden preparar recepciones desde albaranes digitalizados correctamente."))
        if not self.purchase_order_ref and not self.purchase_order_id:
            raise UserError(_("Este albarán no contiene una referencia de pedido. No se puede asociar con seguridad a una recepción."))

        if self.purchase_order_id:
            orders = self.purchase_order_id
        else:
            orders = self.env["purchase.order"].search(
                [
                    ("name", "=", self.purchase_order_ref.strip()),
                    ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
                    ("company_id", "=", self.company_id.id),
                ]
            )
        if len(orders) != 1:
            raise UserError(
                _("No se encontró un único pedido confirmado «%(ref)s» para %(supplier)s. Comprueba el número de pedido y el proveedor.")
                % {"ref": self.purchase_order_ref or self.purchase_order_id.display_name, "supplier": self.partner_id.display_name}
            )
        order = orders
        if order.partner_id.commercial_partner_id != self.partner_id.commercial_partner_id:
            raise UserError(_("El pedido seleccionado pertenece a otro proveedor. No se ha enlazado el albarán."))
        if order.company_id != self.company_id:
            raise UserError(_("El pedido seleccionado pertenece a otra empresa. No se ha enlazado el albarán."))
        if order.state not in ("purchase", "done"):
            raise UserError(_("El pedido «%s» aún no está confirmado. Confírmalo en Compras antes de preparar la recepción.") % order.name)

        open_pickings = order.picking_ids.filtered(
            lambda picking: picking.picking_type_id.code == "incoming"
            and picking.state not in ("done", "cancel")
        )
        if self.stock_picking_id:
            open_pickings = open_pickings.filtered(
                lambda picking: picking == self.stock_picking_id
            )
        elif self.picking_type_id:
            open_pickings = open_pickings.filtered(
                lambda picking: picking.picking_type_id == self.picking_type_id
            )
        if len(open_pickings) != 1:
            raise UserError(
                _("El pedido «%s» tiene %s recepciones de entrada abiertas. No se ha enlazado el albarán para evitar elegir una recepción incorrecta; abre la recepción adecuada desde el pedido de compra.")
                % (order.name, len(open_pickings))
            )
        picking = open_pickings

        delivery_notes = self.search([("stock_picking_id", "=", picking.id)]) | self
        quantities = {}
        errors = []
        assumptions = []
        for delivery_note in delivery_notes:
            if delivery_note.partner_id.commercial_partner_id != order.partner_id.commercial_partner_id:
                errors.append("albarán %s: el proveedor no coincide con el pedido %s" % (delivery_note.name, order.name))
                continue
            if delivery_note.purchase_order_id and delivery_note.purchase_order_id != order:
                errors.append("albarán %s: está asociado a otro pedido de compra" % delivery_note.name)
                continue
            try:
                lines = json.loads(delivery_note.lines_json or "[]")
            except (TypeError, ValueError):
                errors.append("albarán %s: sus líneas OCR no son JSON válido" % delivery_note.name)
                continue
            if not isinstance(lines, list) or not lines:
                errors.append("albarán %s: no contiene líneas de producto" % delivery_note.name)
                continue
            for index, line in enumerate(lines, 1):
                if not isinstance(line, dict):
                    errors.append("albarán %s, línea %s: formato no válido" % (delivery_note.name, index))
                    continue
                try:
                    quantity = float(line.get("quantity"))
                except (TypeError, ValueError, AttributeError):
                    errors.append("albarán %s, línea %s: cantidad no válida" % (delivery_note.name, index))
                    continue
                if quantity <= 0 or not math.isfinite(quantity):
                    errors.append("albarán %s, línea %s: la cantidad debe ser mayor que cero" % (delivery_note.name, index))
                    continue
                product, error = delivery_note._resolve_stock_product(line, order)
                if error:
                    errors.append("albarán %s, línea %s: %s" % (delivery_note.name, index, error))
                    continue
                po_line, error = delivery_note._resolve_purchase_line(order, product, line)
                if error:
                    errors.append("albarán %s, línea %s: %s" % (delivery_note.name, index, error))
                    continue
                quantities[po_line] = quantities.get(po_line, 0.0) + quantity
                if not (line.get("unit_of_measure") or "").strip():
                    assumptions.append(
                        "%s, línea %s: cantidad interpretada en «%s», unidad del pedido"
                        % (delivery_note.name, index, po_line.product_uom_id.name)
                    )
                if product.tracking != "none":
                    assumptions.append(
                        "%s: completa el lote o número de serie de «%s» antes de validar"
                        % (delivery_note.name, product.display_name)
                    )

        if errors:
            raise UserError(
                _("No se ha preparado la recepción. Corrige estos puntos en los albaranes o en Compras:\n• ")
                + "\n• ".join(errors)
            )

        for po_line, quantity in quantities.items():
            if quantity - po_line.product_qty > po_line.product_uom_id.rounding / 2:
                errors.append(
                    "«%s»: el albarán indica %.4g %s y el pedido solo contempla %.4g %s"
                    % (
                        po_line.product_id.display_name,
                        quantity,
                        po_line.product_uom_id.name,
                        po_line.product_qty,
                        po_line.product_uom_id.name,
                    )
                )
        if errors:
            raise UserError(
                _("No se ha preparado la recepción porque las cantidades superan lo pendiente del pedido:\n• ")
                + "\n• ".join(errors)
            )

        move_assignments = []
        for po_line, quantity in quantities.items():
            remaining = max(po_line.product_qty - po_line.qty_received, 0.0)
            if quantity - remaining > po_line.product_uom_id.rounding / 2:
                errors.append(
                    "«%s»: el albarán indica %.4g %s y solo quedan %.4g %s pendientes"
                    % (po_line.product_id.display_name, quantity, po_line.product_uom_id.name, remaining, po_line.product_uom_id.name)
                )
                continue
            moves = picking.move_ids.filtered(lambda move: move.purchase_line_id == po_line)
            if len(moves) != 1:
                errors.append(
                    "«%s»: el pedido tiene %s movimientos en esta recepción; no se puede prellenar de forma segura"
                    % (po_line.product_id.display_name, len(moves))
                )
                continue
            move = moves
            if move.state in ("done", "cancel"):
                errors.append("«%s»: el movimiento del pedido ya está cerrado" % po_line.product_id.display_name)
                continue
            if move.ai_ocr_quantity:
                if po_line.product_uom_id.compare(move.quantity, move.ai_ocr_quantity) != 0:
                    errors.append(
                        "«%s»: la cantidad de recepción se modificó manualmente desde el último prellenado; revísala antes de añadir albaranes"
                        % po_line.product_id.display_name
                    )
                    continue
            elif not po_line.product_uom_id.is_zero(move.quantity):
                default_demand = po_line.product_uom_id.compare(
                    move.quantity, move.product_uom_qty
                ) == 0
                if move.picked or not default_demand:
                    errors.append(
                        "«%s»: ya tiene una cantidad recibida modificada o marcada manualmente; no se ha sobrescrito"
                        % po_line.product_id.display_name
                    )
                    continue
                assumptions.append(
                    "«%s»: el albarán ha sustituido la cantidad inicial del pedido; confirma la cantidad recibida antes de validar"
                    % po_line.product_id.display_name
                )
            move_assignments.append((move, quantity))
        if errors:
            raise UserError(
                _("No se ha preparado la recepción. Revisa las líneas indicadas:\n• ")
                + "\n• ".join(errors)
            )

        try:
            for move, quantity in move_assignments:
                move.quantity = quantity
                move.ai_ocr_quantity = quantity
            self.write(
                {
                    "stock_picking_id": picking.id,
                    "picking_type_id": picking.picking_type_id.id,
                    "purchase_order_id": order.id,
                    "review_note": "Albarán enlazado con la recepción estándar %s del pedido %s; sus cantidades OCR se han preparado para revisión. Confirma que lo recibido coincide y valida en Inventario. Las existencias no cambian hasta validar.%s"
                    % (
                        picking.name,
                        order.name,
                        ("\n" + "\n".join(assumptions)) if assumptions else "",
                    ),
                }
            )
        except AccessError as exc:
            raise UserError(_("Tu usuario no tiene permisos de Inventario para enlazar recepciones. Pide acceso de usuario de Inventario y vuelve a intentarlo.")) from exc
        return self._open_stock_picking(picking)

    @staticmethod
    def _open_stock_picking(picking):
        return {
            "type": "ir.actions.act_window",
            "name": _("Recepción de inventario"),
            "res_model": "stock.picking",
            "res_id": picking.id,
            "view_mode": "form",
            "target": "current",
        }


class AccountMove(models.Model):
    _inherit = "account.move"

    ai_delivery_note_ref_ids = fields.Many2many(
        "ai.delivery.note.reference",
        "ai_delivery_note_account_move_rel",
        "move_id",
        "delivery_note_id",
        string="Albaranes del proveedor",
        copy=False,
        readonly=True,
    )


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    ai_delivery_note_ref_id = fields.Many2one(
        "ai.delivery.note.reference",
        string="Albarán del proveedor",
        copy=False,
        readonly=True,
        ondelete="restrict",
    )


class StockPicking(models.Model):
    _inherit = "stock.picking"

    ai_delivery_note_reference_ids = fields.One2many(
        "ai.delivery.note.reference",
        "stock_picking_id",
        string="Albaranes del proveedor",
        readonly=True,
    )


class StockMove(models.Model):
    _inherit = "stock.move"

    ai_ocr_quantity = fields.Float(
        string="Cantidad preparada por OCR",
        readonly=True,
        copy=False,
        help="Última cantidad recibida prellenada desde albaranes OCR; permite detectar cambios manuales.",
    )
