# OpenRouter Connector

Conector reutilizable para que otros addons Odoo soliciten respuestas a modelos de lenguaje a través de OpenRouter.

## Requisitos

- Odoo 19.0.
- Dependencia Python `requests` en el mismo entorno que ejecuta Odoo.

## Instalación y configuración

1. Copia o clona este repositorio dentro de una ruta incluida en `addons_path`.
2. Instala `requests` en el entorno Python de Odoo y reinicia el servicio.
3. Actualiza la lista de aplicaciones e instala `OpenRouter Connector`.
4. Configura la clave y el modelo desde el bloque `OpenRouter` de Ajustes, o proporciona `OPENROUTER_API_KEY` y `OPENROUTER_MODEL` como variables de entorno.

La variable de entorno `OPENROUTER_API_KEY` tiene prioridad sobre el valor guardado en Odoo. Mantén las claves fuera de Git, los logs y los repositorios de clientes.

## Uso por otros addons

`ai_document_processor` declara este addon como dependencia y usa su cliente HTTP. Instala primero este conector cuando despliegues ambos.

El manifiesto declara licencia LGPL-3.
