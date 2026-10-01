# AI Document Processor — OCR/IA de Facturas y Albaranes para Odoo 19

![Odoo 19](https://img.shields.io/badge/Odoo-19.0-purple)
![License: LGPL-3](https://img.shields.io/badge/License-LGPL--3-blue)

Digitaliza y contabiliza **facturas de proveedor** y **albaranes** automáticamente con IA. Sube el PDF, la IA extrae los datos, tú revisas y confirmas con un clic.

---

## Funcionalidades

### Facturas de proveedor (PDF → Asiento contable)
- Extracción automática de: proveedor, NIF, fecha, número, líneas de producto, IVA, totales
- Creación automática del asiento contable en Odoo (vendor bill)
- Búsqueda o creación automática del contacto proveedor
- Inferencia de IVA español (21 %, 10 %, 4 %, exento) según tipo de producto
- Detección de duplicados (mismo PDF o mismo número de factura)
- Flujo de revisión humana antes de contabilizar

### Albaranes de proveedor (PDF → Recepción de inventario)
- Extracción de: número de albarán, líneas con cantidad y producto, referencia de pedido
- Cotejo automático contra el pedido de compra de Odoo
- Prellenado de la recepción de inventario con las cantidades del albarán
- Detección de discrepancias entre albarán y pedido
- Enlace automático albarán ↔ factura de proveedor

### Gestión centralizada
- Panel "Documentos OCR" con estados: Borrador → Procesando → Revisión → Contabilizado
- Historial de procesamiento con confianza de extracción
- Compatible con **Hub central** (créditos compartidos) o **OpenRouter** directo

---

## Instalación

### Requisitos

- Odoo 19.0
- `hub_client_base` (incluido en este repo como dependencia)
- `openrouter_connector` (incluido en este repo como fallback)
- Python: `pymupdf`, `jsonschema`

```bash
pip install pymupdf jsonschema
```

### Pasos

1. Copia las carpetas `hub_client_base/`, `openrouter_connector/` y `ai_document_processor/` en tu directorio de addons
2. Actualiza la lista de módulos y activa **AI Document Processor**
3. Configura en **Ajustes → Catalog & OCR Hub** (recomendado) o en **Ajustes → OpenRouter** (local)

---

## Arquitectura

```
ai_document_processor
    └── _get_ai_client()
           ├── Hub Client (hub_client_base) — si está configurado con API Key
           └── OpenRouter Client (openrouter_connector) — fallback local
```

El módulo usa automáticamente el Hub central si está configurado, o el conector OpenRouter si no.

---

## Licencia

LGPL-3 — Libre para uso comercial y modificación, con atribución.
