# AI Document Processor

Addon de Odoo para extraer y revisar datos de facturas, albaranes y otros documentos mediante un servicio de IA, antes de crear o relacionar registros contables o de inventario.

## Requisitos

- Odoo 19.0.
- Addons Odoo: `account` y `purchase_stock`.
- Addon independiente `openrouter_connector`, que debe instalarse antes.
- Dependencias Python: `pymupdf` y `jsonschema`.

## Instalación

1. Instala las dependencias de OpenRouter y este addon en el directorio de addons.
2. Instala los paquetes Python `pymupdf` y `jsonschema` en el entorno que ejecuta Odoo.
3. Reinicia Odoo, actualiza la lista de aplicaciones e instala `AI Document Processor`.
4. Configura el proveedor y el modelo en `OpenRouter` y revisa cada documento antes de crear la factura o registrar el albarán.

## Dependencias externas

La extracción requiere acceso al servicio OpenRouter y una clave configurada en `openrouter_connector`. El conector y la clave no se incluyen en este repositorio. No guardes claves en el código ni en archivos versionados.

El manifiesto declara licencia LGPL-3. Este addon requiere validación funcional en la base de datos destino antes de usarlo en producción.
