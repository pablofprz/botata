# Wiki de Botata

Botata es un bot comunitario con alma de agente: vive en una cuenta de red social, responde
cuando lo mencionan, recuerda a la gente con la que habla y tiene iniciativa propia. Es
**genérico y multi-comunidad** — la personalidad, el idioma, las fuentes y las herramientas
son configuración, no código.

## Guías

| Página | Qué cubre |
|---|---|
| [Instalación](Instalacion.md) | Requisitos, primer bot en 10 minutos, problemas comunes |
| [Configuración](Configuracion.md) | El panel web, settings.json, fuentes de contenido, presupuesto |
| [Personalidad](Personalidad.md) | SOUL.md, skills, moods y rutinas |
| [Herramientas](Herramientas.md) | Las tools del bot, scopes, conectores y servers MCP |
| [Memoria](Memoria.md) | Qué recuerda, cómo busca, compactación y derechos de los usuarios |
| [Canales](Canales.md) | Bluesky, Mastodon, Discord, WhatsApp: estado y particularidades |
| [Comandos](Comandos.md) | Lo que cualquiera puede pedirle y lo que solo puede el admin |
| [Seguridad](Seguridad.md) | El modelo de amenaza de un bot público y sus defensas |

## Otros documentos

- [FAQ](../FAQ.md) — preguntas frecuentes.
- [ARQUITECTURA.md](../ARQUITECTURA.md) — cómo funciona el código, módulo por módulo.
- [ROADMAP.md](../../ROADMAP.md) — qué está hecho y qué viene.

## Los tres conceptos que ordenan todo

**Motor vs. instancia.** El repo es el motor (`core/`); tu bot es una instancia
(`bots/<nombre>/`): su config, sus credenciales, su personalidad y su base de datos. El
mismo motor corre N bots, cada uno con su memoria separada.

**Todo comportamiento es un archivo.** Prompts, personalidad, skills, moods y rutinas son
markdown dentro de tu instancia. Editás el archivo, el bot cambia.

**El LLM decide, la config limita.** El modelo elige qué decir y qué herramienta usar, pero
los permisos (qué tool puede usar quién) son configuración verificada en código — nunca
criterio del modelo.
