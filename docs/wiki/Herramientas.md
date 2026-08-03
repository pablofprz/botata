# Herramientas

Las **tools** son las capacidades del bot: traer datos, actuar sobre el mundo. Cada una se
declara una vez con su schema, su handler y sus **scopes**, y se administra desde
`settings.TOOLS` (enable + scopes por nombre) sin tocar código.

## Scopes: quién puede hacer que el bot use qué

| Scope | Contexto | Sensibilidad |
|---|---|---|
| `reply` | respondiendo menciones de **cualquiera** | máxima — superficie pública |
| `feed_reflection` | pases proactivos (rutinas) | media — la dispara el propio bot |
| `admin` | comandos del administrador | controlada — solo el admin |

La regla de oro: una tool en scope `reply` es invocable por cualquier desconocido vía
prompt injection, así que las tools sensibles **nacen** scope `admin` y promoverlas es una
decisión explícita — que además solo puede tomarse desde el panel local, nunca por mención.

## Las tools del motor

**Buscar y leer** — `web_search` (Brave) busca; `read_url` abre una página y la devuelve
como texto (con guarda de SSRF: jamás lee IPs privadas, ni siquiera vía redirect). Buscar ≠
leer: los snippets muy seguido no traen el dato.

**Contenido** — `get_news` (RSS por tema), `search_music` (Spotify), `similar_artists`
(MusicBrainz + ListenBrainz, sin API key), `get_playlist_track`, `add_music_recommendation`,
`share_video` (YouTube), `get_latest_media` (fuentes en vivo: Pinterest, Tumblr, APIs),
`get_data` (APIs de datos: el dólar, el clima), `search_images` (catálogo indexado con
visión), `generate_image` (nace scope admin: cada imagen se paga).

**Comunidad** — `summarize_feed` (qué se está hablando), `get_my_recent_posts`,
`like_post`, `get_upcoming_events` / `create_event` (calendario compartido).

**Memoria y derechos** — `reset_my_memory`, `forget_about_me`, `block_me`
(ver [Memoria](Memoria.md)).

**Comportamiento** — `use_skill` (carga una skill al contexto).

**Admin** — `save_to_memory` / `save_to_user_profile` (cargarle memoria), `update_bio`,
`get_bot_config` + `set_tool_config` / `set_task_config` / `set_feed_config` /
`set_mcp_enabled` / `set_routine` / `delete_routine` (configurar por mención),
`get_debug_info`, `get_help`.

## El loop de tools

El bot puede **encadenar** herramientas: ve el resultado de cada llamada antes de decidir
la siguiente (buscar → elegir un link → leerlo → contestar), hasta `TOOL_ROUNDS` rondas
(1–5, config de la instancia). Anti-loop: no repite la misma tool con los mismos
argumentos.

## Conectores: sumar fuentes sin tocar el motor

Tres puertas, en orden de preferencia:

1. **Sin código**: una entrada `type: "api"` en el registro de fuentes, con `url`,
   `items_path` y `map`. Cubre la mayoría de los casos (APIs JSON públicas).
2. **Plugin de la instancia**: `bots/<nombre>/connectors/*.py` con un dict `CONNECTOR` +
   `fetch(source, limit)`. Para lo que (1) no cubre: OAuth, paginado, dos llamadas por item.
3. **Server MCP**: cualquier server MCP (oficial o propio) declarado en `settings.MCP` —
   sus tools se registran solas como `{server}_{tool}`. Corre aislado en su proceso.

Lo que **no** existe a propósito: una tool genérica de HTTP request en scope público — con
un bot que obedece texto de terceros sería un agujero de exfiltración.

## La regla que manda

> **Todo lo que pueda ser tool, que sea tool. Todo lo que pueda ser skill, que sea skill.**

Antes de pedir (o escribir) una feature: ¿es una **capacidad**? → tool. ¿Es **saber cómo
comportarse**? → skill. ¿Es algo que hace **cada tanto por iniciativa propia**? → rutina.
Las tres son componibles, configurables, testeables de a una y editables en caliente; el
código nuevo del motor, no.
