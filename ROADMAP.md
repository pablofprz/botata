# Botata — Roadmap

Refundación del bot comunitario de Bluesky sobre langgraph + SQLite. Este documento es
la **fuente de verdad** de las funciones a construir, ordenadas por fase y prioridad.
Cada tarea tiene criterios de aceptación para poder trabajarla contra spec.

**Convenciones**
- **Priority:** P0 (bloqueante/fundacional) · P1 (prioritario) · P2 (deseable) · P3 (después)
- **Module:** `infra` · `proactivity` · `tools` · `scraper` · `ui`
- **Effort:** S (<½ día) · M (1-2 días) · L (varios días)
- Toda tool nueva se registra en el **framework de tools** (T1) y respeta enable/disable + scope.

---

## 🚀 Prioridad actual — LANZAMIENTO  *(decidida 2026-07-27)*

El objetivo pasa de "construir el motor" a **lanzar el proyecto**. Criterio del admin: para
que el lanzamiento pegue fuerte, **Bluesky ya está maduro, Discord tiene que estarlo, y
tiene que haber Telegram y WhatsApp**. Mastodon queda relegado (poco y nada de comunidad).

**Orden de trabajo:**
1. **T39 · Discord maduro** `P0` — bloqueante. Está implementado pero NUNCA se validó en vivo, y le faltan piezas que en Bluesky sí están (visión sobre imágenes, blockme, escala del poll).
2. **T41 · Canal WhatsApp** `P1` — decisión tomada (2026-07-27): **vía no oficial**, el camino que usa OpenClaw. Va antes que Telegram por pedido del admin.
3. **T40 · Canal Telegram** `P0` — el canal nuevo más barato: Bot API oficial, gratis, y su modelo de menciones calza casi 1:1 con el de Discord. Después de WhatsApp.
4. **T38 + T36** `P2` — contenido por tema y refresh a demanda. No bloquean el lanzamiento pero son el diferenciador de contenido; en curso.

**Intercalado (2026-08-01/02):** el bloque **M11** — loop de tools, `read_url`, generación
de imágenes, `similar_artists` y el guard de links inventados. No estaba planificado: cada
tarea salió de algo que el bot hizo mal en producción mientras se probaba WhatsApp. Deja
**T55** de deuda abierta (tope de reintentos, budget apagado, tests flaky).

**Explícitamente postergado** (decisión del admin, mismo día): T28b ProviderRegistry ·
validación de Mastodon en vivo · multi-canal simultáneo · T37 character cards · T21 · T20.

> Nota de encuadre: T39/T40/T41 **no reabren T28** — el contrato `Channel` ya existe y está
> cerrado (duck typing, `build_channel()`, ids opacos en la DB). Son *implementaciones* sobre
> ese contrato. Lo postergado de T28 es lo otro: providers componibles, Mastodon y el
> multi-canal simultáneo.

---

## Decisiones validadas
- **Secuencia:** fundaciones primero (framework de tools + router de modelos) antes de la proactividad.
- **Fuentes de feed a soportar:** listas · feeds custom (generators) · timeline de seguidos.
- **Reddit:** investigar la API oficial (spike) antes de decidir vía (API / RSS / navegador).
- **Canales para el lanzamiento (2026-07-27, REVIERTE la decisión de alcance de T28):** entran **Telegram y WhatsApp**, que en julio 2026 estaban explícitamente fuera del plan. Motivo: el lanzamiento necesita presencia donde están las comunidades hispanohablantes, y ahí WhatsApp y Telegram pesan más que Mastodon.

---

## M1 · Fundaciones  `P0`

### T1 · Framework de tools  `infra` `L`
Registry central de tools que reemplaza `ADMIN_TOOLS` hardcodeado.
- **Acept.:** cada tool se declara una vez (schema OpenAI + handler + metadata).
- Config declara `enabled: bool` por tool y `scopes: [reply|feed_reflection|admin]` que autorizan su uso por contexto.
- El grafo arma la lista de tools disponibles filtrando por scope + enabled en runtime.
- `search_images`, `save_to_*`, `get_debug/help` migradas al registry sin cambio de comportamiento.

### T2 · Router de modelos + fallbacks  `infra` `M`
Selección de modelo por función, endpoint-agnóstico, con fallbacks.
- **Acept.:** JSON que mapea `funcion/tool → modelo` (ej. `classify→lite`, `reply→reasoning`, `image→vision`).
- Lista ordenada de endpoints por modelo con fallback automático (ej. local → OpenRouter) ante error/timeout.
- No atado a OpenRouter: cualquier endpoint OpenAI-compatible configurable (base_url + api_key por endpoint).
- Reintentos con backoff; loguea qué endpoint sirvió cada llamada.

### T3 · El bot sabe la fecha  `infra` `S`  *(quick win)*  ✅ **HECHO**
- **Acept.:** cada contexto de reasoning (reply, feed-reflection, admin) recibe la fecha/hora actual (TZ America/Argentina/Buenos_Aires) inyectada en el system prompt.
- Impl.: helper `current_datetime_line()` en `botata.py` (ZoneInfo + fallback UTC-3, `tzdata` en deps). Inyectado en reply, admin, feed_summary, feed_opinion y update_profile. Fija el bug de fecha alucinada (el bot leía fechas de MEMORY.md como "hoy").

### T4 · Schema de eventos/calendario  `infra` `S`  ✅ **HECHO**
- **Acept.:** tabla `events(id, handle?, title, description, event_at, kind, source, created_at)` + índices por fecha y handle.
- Migración idempotente en `db.py`. Helpers CRUD + query "próximos N eventos" / "eventos de hoy".
- Impl.: tabla + índices en `_SCHEMA`; helpers `create/get/update/delete_event`, `upcoming_events`, `events_today` (default AR, filtro por handle+comunidad). FK cascade al borrar user. Tests en `tests/test_events.py`.
- **T4b · El calendario ACTÚA SIEMPRE (2026-07-26, doctrina del admin):** heartbeat y calendario responden a cosas distintas — el heartbeat es de FRECUENCIA y puede actuar o no (los eventos le quedan como contexto anotado con proximidad); el calendario es de SUCESOS y actúa siempre. Tarea nueva **`calendar`** (cada ciclo del loop, como mentions): evento vencido y no anunciado → anuncio determinístico, sin LLM en la decisión (el LLM solo redacta, en la voz del bot, con el texto del evento tratado como DATO citado — anti-injection — y **fallback a plantilla fija** si falla: el anuncio jamás depende del LLM). Columna `events.announced_at` (una vez y solo una por ocurrencia, recurrencias incluidas) + ventana de gracia 24h (caídas largas no generan anuncios trasnochados; fallo de red → reintenta, no marca). **Gate `CALENDAR_ANNOUNCE`** `{from: admin|groups|any, groups:[]}` (default `admin`): qué eventos disparan anuncio según el CREADOR (parseado de `source`) — usuarios comunes pueden anotarse eventos pero no hacer postear al bot, salvo grupos habilitados (protección prompt-injection, decisión del admin 2026-07-26). No elegible = se marca sin postear (queda como contexto). UI: control en la sección Calendario + tarea en Tareas periódicas. Regla del heartbeat actualizada: los eventos son contexto, anunciar es de `calendar`. **Refinamiento (mismo día): `source='feed'` NO cuenta como admin** — los eventos que el loop aprende del feed (T6) derivan de posts de terceros; anunciarlos es opt-in aparte (`CALENDAR_ANNOUNCE.feed`, bool, default false, gobierna incluso bajo `from=any`). Tests: `tests/test_calendar_pass.py` (10). Suite: 491.
- **T4c · Import de calendario + recurrencia yearly (2026-07-26):** recur nuevo **`yearly`** en todo el motor (`_next_occurrence` con manejo de 29/2, `events_today`, anunciador, validadores, UI). **Importador en la UI** (sección Calendario): pega/subí **CSV** (encabezado `fecha,titulo[,descripcion,tipo,de_quien,repeticion]`, separador `,` o `;` autodetectado, alias diario/semanal/mensual/anual) o **ICS** de Google/Outlook (subconjunto RFC 5545: DTSTART con date/datetime/Z→AR, SUMMARY/DESCRIPTION des-escapados, RRULE solo FREQ simple — reglas compuestas rebotan por fila). Flujo: **vista previa obligatoria** (dry-run, reporte por fila: ok/duplicado/error) → confirmar importa solo las OK (`source='import'`, dedup por título+fecha+dueño, en vivo sin reiniciar). `bot_action` excluido del import a propósito (órdenes = alta manual). Tests: +5 (`test_config_ui`, `test_calendar_pass`). Suite: 496.
- **T4d · Acciones puntuales + anti-bypass del heartbeat + TIMEZONE (2026-07-26):** tres arreglos tras el test en vivo de Discord (bot_action de las 00:00 posteada 01:22, y el heartbeat posteando 3 veces un evento que el gate había declinado). (1) Tarea nueva **`actions`** (cada ciclo): las `bot_action` vencidas se ejecutan en el primer ciclo después de su hora (`run_actions_pass`: SOUL+mood+orden+tools feed_reflection, decisión FeedDecision, considerada=done, error de LLM=reintenta) — antes viajaban dentro del heartbeat y salían con SU cadencia (hasta 2h tarde). (2) **Filtro mecánico anti-bypass en el heartbeat**: los eventos con hora ya vencida salen de su contexto (el calendario ya los anunció o el gate los declinó; dejarlos invitaba al LLM a postearlos igual = bypass real de CALENDAR_ANNOUNCE); recurrentes normalizados a la ocurrencia de HOY (su event_at crudo es la primera ocurrencia histórica y el timing daba siempre [YA PASÓ]). (3) Setting **`TIMEZONE`** (IANA u offset `UTC±N`, default Buenos Aires): reemplaza el UTC-3 hardcodeado en `botata.py` (`now_local()`) y `db.py` (`LOCAL_TZ`/`set_local_tz`/`local_now`; los `datetime('now','-3 hours')` de SQL se calculan ahora en Python) — la UI lo setea en Identidad y lo valida contra zoneinfo. Tests: `test_bot_actions` reescrito contra `actions` + regresión "el heartbeat ya no ejecuta órdenes", 4 de filtro (reloj congelado), 2 de tz.
- **T4e · Switch de anuncio POR EVENTO + prompts de calendario/acciones en archivos (2026-07-26, pedido del admin: "necesito controlar qué eventos se anuncian, entendible y sencillo"):** columna **`events.announce`** (0/1/NULL) — el gate CALENDAR_ANNOUNCE se evalúa UNA vez al CREAR (`_default_announce`: admin/UI/import → 1; terceros → 0 salvo política; feed → según `CALENDAR_ANNOUNCE.feed`; 'groups' con no-admin → NULL, difiere al gate legado en announce-time porque la membresía puede requerir red) y queda **visible y toggleable por evento en la UI** (columna 📣/🔇 en Calendario, endpoint `/api/events/announce`, `set_event_announce` en db). `run_calendar_pass` obedece el switch explícito; NULL cae al gate de siempre (back-compat con eventos legados). **Bug encontrado de paso:** los eventos importados por CSV/ICS (`source='import'`) eran origen DESCONOCIDO para el gate → no se anunciaban NUNCA; ahora nacen con 📣 (los importa el admin). Además, encuadres hardcodeados extraídos a archivos de instancia (doctrina "comportamiento en archivos"): **`prompts/calendar_announce.md`** (la voz del anunciador; fallback en código si falta) y **`prompts/actions.md`** (encuadre de órdenes agendadas) — editables por UI, castellano en botata-arg, inglés en template/dev/mafiazeth. Explicación en criollo en la sección Calendario de la UI ("la hora del evento ES la hora del anuncio"). Tests: +4 (`test_calendar_pass`).

---

## M2 · Proactividad  `P0`  *(lo más prioritario)*

### T5 · Loop proactivo de feed  `proactivity` `L`  ✅ **HECHO**
Reemplaza el `FeedProcessor` manual por un loop autónomo.
- **Acept.:** parámetro de **frecuencia de lectura** por feed (o desactivado).
- Cada corrida: lee posts dentro de una ventana temporal → resume → el **agente decide** si postea (autónomo) o no; parámetro para forzar posteo.
- La decisión considera temática y "mood"; puede lanzar tools **whitelisted por scope `feed_reflection`** (T1).
- Deduplica contra `bot_posts`; respeta presupuesto (router T2). Sin scheduling rígido tipo cron.
- Impl.: grafo langgraph `build_feed_graph` (fetch→summarize→reflect→post) en `botata.py`. `ReflectDecideNode` es el núcleo agéntico (structured output `FeedDecision`, tool_calling scope feed_reflection, política de posteo configurable conservative|balanced|active). Config por feed: `enabled` + `posting_policy` + `interval_hours`. CLI `--proactive [--force-post] [--backfill]`. Dedup normalizado contra `bot_posts`. Tests: `tests/test_feed_proactive.py`. *(2026-07-27, T28 fase 3d: la mitad de POSTEO se eliminó — el pase quedó solo-lectura `fetch→learn→summarize`; opinar sobre el feed es una rutina.)*

### T6 · Aprendizajes del feed → memoria/calendario  `proactivity` `M`  ✅ **HECHO**
- **Acept.:** tras leer el feed, el agente decide (o no) extraer aprendizajes.
- Si un post revela algo de un usuario con perfil → `upsert_user_fact` para ese handle.
- Si revela una fecha/evento → alta en `events` (T4). Ej: "hoy me duele el pie" → fact de ese usuario.
- Idempotente: no duplica aprendizajes ya guardados (dedup semántico existente).
- Impl.: `LearnFromFeedNode` en el grafo proactivo (fetch→**learn**→summarize→reflect→post), structured output `FeedLearnings` (facts + events) vía rol `update_profile`. Gate `db.user_exists` (solo perfiles existentes); eventos sin perfil → comunidad. Fechas relativas resueltas con T3. Dedup: `upsert_user_fact` (semántico) + `db.event_exists` (día+dueño+título). Toggle `learn` por feed. `get_list_feed` ahora incluye `uri` (source de los hechos). Tests: `tests/test_feed_learnings.py`.

### T7 · Generalización de fuentes de feed  `proactivity` `M`  ✅ **HECHO**
- **Acept.:** config por comunidad soporta 3 tipos de fuente: `list`, `feed` (generator/algoritmo), `following` (home timeline del bot).
- Impl.: paginador común `BskyClient._paginate_posts` + un método por tipo (`get_list_feed` / `get_custom_feed` = `app.bsky.feed.get_feed` / `get_timeline` = `app.bsky.feed.get_timeline`) + dispatcher `get_feed_posts(source_type, identifier, since, limit)` (tipo desconocido → cae a `list` con warning). `FEEDS[]` declara `type` (+ `uri`, ignorado en `following`); default `list` para back-compat. Cableado en `FetchFeedNode` (via `feed_type` en `FeedState`), `_run_feed_pass` y la tool `summarize_feed`. Verificado en vivo los 3 tipos (list/following/feed). Tests: `tests/test_feed_sources.py` (5). **Cierra M2.**

### T23 · Externalizar prompts hardcodeados  `infra` `S`  `P1`  ✅ **HECHO**
Todo prompt debe vivir en `prompts/*.md` (o config), nunca hardcodeado en `.py`. Principio de diseño (CLAUDE.md): config declarativa, no strings incrustados.
- **Acept.:** mover a archivos los prompts incrustados en `botata.py`; cargarlos con `load_text(PROMPTS_DIR / ...)` como ya hacían `reflect_feed_prompt.md` / SOUL.
- Impl.: externalizados a `prompts/` — guía de posteo por política → `feed_policy_{conservative,balanced,active}.md` (helper `feed_policy_guidance()` con fallback a balanced); resumen de feed → `feed_summary_prompt.md`; formato JSON de `ReflectDecideNode` → `feed_decision_format.md`; system prompt de `HandleAdminCommandNode` → `admin_command_prompt.md`; bloque de formato de `GenerateReplyNode` → `reply_format.md`. 31 tests verdes.
- **Quedan inline a propósito** (no son prompts fijos sino armado dinámico): labels de secciones del reply (memoria/hechos/lecciones/catálogo, interpolan data), instrucción condicional de force-post y hint de tools en `ReflectDecideNode`, y el path legacy `FeedProcessor.post_opinion`.

---

## M3 · Tools prioritarias  `P1`

### T8 · Tool de búsqueda (Brave)  `tools` `M`  ✅ **HECHO**
- **Acept.:** integración con Brave Search API; tool `web_search(query)` registrada en T1, toggleable.
- Impl.: `web_search` (scopes `reply`+`feed_reflection`+`admin`): GET a Brave vía stdlib (`urllib`, sin deps nuevas), `BRAVE_API_KEY` opcional desde `.env` (gitignored). Devuelve top-N (default 5, máx 10) con título/resumen/link; limpia tags HTML de los snippets. **Degradación graceful:** sin key → "no configurada"; HTTP 429 → "estoy limitado"; otro error → "no pude buscar". Verificado en vivo contra Brave. Tests: `tests/test_web_search.py` (7, GET mockeado). **Cierra M3.**

### T9 · Tool de calendar  `tools` `M`  ✅ **HECHO**
- **Acept.:** el agente lee/escribe `events` (T4) como tool.
- **Config (post-M4):** `create_event` ahora scope `admin`+`reply` → **usuarios pueden agendar eventos propios** (el handler los fuerza a sí mismos; nunca a otros). Admin sigue agendando para comunidad/cualquiera; el feed auto-popula (T6).
- Impl.: dos tools en el registry (T1). **`get_upcoming_events`** (scopes `reply`+`feed_reflection`+`admin`): lee hoy+próximos; un usuario ve *sus* eventos + los de comunidad, el admin y el loop proactivo (sin author) ven todos. Registrada en `feed_reflection` → el agente puede consultarla al reflexionar y decidir saludar/recordar (satisface "acciona según eventos dentro del loop proactivo"). **`create_event`** (scope `admin` por default): regla de propiedad en el handler — el admin agenda para la comunidad (sin `handle`) o para cualquier usuario; un usuario común **siempre** crea para sí mismo, nunca para otro. Idempotente por (día+dueño+título) vía `event_exists`. **Parámetro configurable "usuarios pueden crear"** = agregar `reply` a `TOOLS.create_event.scopes` en `settings.json` (default off). Prompt admin actualizado. Tests: `tests/test_calendar_tools.py` (9).
- Pendiente menor: no hay tool de `delete`/`update` en-banda (los `events` se limpian por SQL/mem_admin futuro); el saludo automático depende de que el agente decida llamar la tool (no hay trigger rígido).

### T10 · Comando `blockme`  `tools` `S`  ✅ **HECHO** (⚠️ sin test en vivo)
- **Acept.:** el bot bloquea al usuario que lo pide y **borra toda su memoria** (user_facts + embeddings + events del handle + relationships). Confirmación al usuario.
- Impl.: `BskyClient.block_user(handle)` crea el record `app.bsky.graph.block`. Tool `block_me` (factory con bsky; scopes `reply`+`admin`) → bloquea primero y, **solo si el block funciona**, purga con `db.purge_user_memory(include_relationships=True, drop_profile=True)`. Fix de FK: `drop_profile` nula `bot_posts.reply_to_handle` (NO ACTION) antes de borrar la fila `users`, preservando el log de salida. Descripción estricta (acción destructiva/irreversible). Tests: `tests/test_block_me.py` (6, block mockeado).
- ⚠️ **PENDIENTE (por decisión del admin): validación en vivo del block real y del borrado de memoria.** No se ejecutó para no bloquear una cuenta real; probar a mano con una cuenta descartable. Aplica también a `resetme` (T11) — el borrado no se corrió contra la DB de prod.

### T11 · Comando `resetme`  `tools` `S`  ✅ **HECHO**
- **Acept.:** borra **únicamente** la memoria del handle que lo pide (facts + embeddings + events propios). **Nunca** toca datos de otros. No bloquea.
- Impl.: helper `db.purge_user_memory(conn, handle, *, include_relationships, drop_profile)` (borra embeddings vec0 a mano por rowid + facts + events propios; el FK cascade no cubre vec0). Tool `reset_my_memory` (scopes `reply`+`admin`, sin args) que purga `ctx.state.author_handle` → siempre el que pide, nunca otro. Descripción estricta ("solo si el usuario lo pide EXPLÍCITAMENTE"). El helper es la base compartida con `blockme` (T10, que suma `include_relationships`+`drop_profile`+block). Tests: `tests/test_reset_memory.py` (6, con embeddings dummy en vec0).

### T24 · Tool `summarize_feed` on-demand para usuarios  `tools` `M`  ✅ **HECHO**
Un usuario puede pedirle al bot un resumen en vivo del feed de la comunidad.
- **Acept.:** tool registrada en el framework (T1), **toggleable** por config (`settings.json` → `TOOLS.summarize_feed`, hoy `enabled:true` scope `reply`). No todos los despliegues la querrán habilitada para usuarios.
- Impl.: `summarize_feed` (scope REPLY) se cierra sobre bsky+router; fetchea la ventana de las últimas 6h del feed principal (o `feed_name`), resume con rol `feed_summary` y **prompt propio** `prompts/summarize_feed_tool_prompt.md` (sin escotilla "NADA": on-demand siempre devuelve algo). `build_tool_registry(config, *, bsky, router)`. **Se le agregó fase de tool_calling a `GenerateReplyNode`** (antes solo `complete`): si hay tools scope REPLY habilitadas, el LLM puede llamarlas y el resultado se inyecta al contexto de la respuesta. Tests: `tests/test_reply_tools.py` (6).
- **Costo:** con la fase de tools activa, cada reply hace una llamada extra de reasoning (tool phase + generación). Si molesta, se apaga con `enabled:false`.
- Tapa parcialmente el gap del reply (antes decía "no tengo acceso al feed"); el gap de actividad propia lo cierra T25.

### T25 · Tool `get_my_recent_posts` (actividad propia del bot)  `tools` `S`  ✅ **HECHO**
El bot puede contestar sobre lo que él mismo viene posteando/respondiendo, no solo resumir el feed ajeno.
- **Acept.:** tool registrada (T1), toggleable; lee `bot_posts` (log de salida) y responde "qué venís diciendo / qué le respondiste a @X".
- Impl.: helper `recent_bot_activity(conn, limit, reply_to)` + tool `get_my_recent_posts` (scopes `reply`+`admin`, arg opcional `handle` para filtrar a quién respondió). Aprovecha la fase de tool_calling de `GenerateReplyNode` (T24). Prompt admin actualizado. Tests: `tests/test_bot_activity_tool.py` (5).

---

## M4 · Tools de contenido  `P2`

### T12 · Imágenes autónomas + carpeta manual + guardrail  `tools` `M`  ✅ **HECHO**
- **Acept.:** además del posteo a pedido (ya existe), el agente puede decidir postear una imagen si el contexto lo amerita — **parámetro configurable** (off por default).
- Impl.: **(autónomo)** `FeedDecision` gana `image_query`; `ReflectDecideNode` lo ofrece SOLO si el feed tiene `autonomous_images: true` (default false) Y hay catálogo; `PostFeedNode` resuelve y adjunta vía `BskyClient.post(..., image_path=)` (ahora soporta imagen con `send_image`). **(manual)** `scrape/pictures/manual/` formalizada — `catalog.py sync` la crea y cataloga (ya escaneaba cualquier subcarpeta de `pictures/`). **(guardrail)** `resolve_catalog_image()` compartido por reply (a pedido) y feed (autónomo): descarta imágenes sin descripción o con categoría fuera de {meme,foto,arte,captura,otro} → el bot no postea lo que no "entiende". Tests: `tests/test_autonomous_images.py` (8).

### T13 · Tool música Spotify  `tools` `M`  ✅ **HECHO**
- **Acept.:** buscar canciones en Spotify a pedido del usuario o por decisión del agente. Portar lógica de `maripobot_deprecated` (`get_random_track_from_playlist`, `generate_track_opinion`). Toggleable.
- Impl.: tool `search_music(query)` (scopes `reply`+`feed_reflection`+`admin`): busca temas vía Spotify `/search` con **Client Credentials headless** (stdlib `urllib`, token cacheado; sin OAuth/browser ni deps nuevas). Devuelve título/artista/link; la **opinión la compone el LLM del nodo** (reply/feed) con la personalidad — no se porta `generate_track_opinion` como llamada aparte (se unifica en la arquitectura de tools). Degradación graceful (sin credenciales / sin resultados / error). Verificado en vivo. Tests: `tests/test_music_tool.py` (8).
- **Decisión (admin):** se descartó el random-de-playlist portado porque Spotify devuelve **403** al leer tracks de playlist con Client Credentials (el original usaba OAuth de usuario, que necesita login interactivo). `/search` sí funciona app-only → se optó por búsqueda. Si se quiere el random-de-playlist curada, hay que sumar OAuth (spotipy + token cache) — queda como posible extensión.

### T14 · Tool música YouTube  `tools` `M`  ✅ **HECHO** (verificado en vivo)
- **Acept.:** ídem Spotify pero YouTube. Portar `fetch_top_mealtime_video` / `get_youtube_transcript` / `generate_video_comment`. Toggleable.
- Impl.: tool `share_video(query?)` (scopes `reply`+`feed_reflection`+`admin`): con `query` busca; sin query trae `mostPopular` de AR — vía **YouTube Data API v3** (stdlib `urllib`, `YOUTUBE_API_KEY` en `.env` gitignored). Dedup contra `bot_posts`. `get_youtube_transcript` portado (import lazy de `youtube_transcript_api`, dep opcional; proxy Webshare si hay creds; None si no está). Comentario unificado en el LLM del nodo. Degradación graceful (sin key / sin video / error). **Verificado en vivo** (mostPopular AR + search). Tests: `tests/test_video_tool.py` (13).
- **Decisión (admin):** se descartó el port literal (video top de subreddits vía Reddit) porque **Reddit devuelve 403 Blocked** sin autenticación (cualquier UA/IP) — es el muro de T17/T18. Se optó por la API oficial de YouTube (API key, no OAuth: los endpoints públicos van con `?key=`). `config/video_subreddits.json` queda sin uso (posible fuente futura de T18).

### T15 · Noticias/RSS gestionadas por admin  `tools` `M`  ✅ **HECHO**
- **Acept.:** el admin carga listas de RSS con **título + descripción**; el bot postea lo nuevo.
- Impl.: `config/news_sites.json` pasa al modelo `{url, title, description, mode, enabled, interval_hours}` (acepta strings legacy → `mode:post`). `fetch_rss` portado a **stdlib xml** (RSS 2.0, sin feedparser; limpia HTML). Pipeline `run_news_pass`: por fuente habilitada respeta `interval_hours` (cursor en `feed_cursors` como `news:{host}`), filtra items **nuevos** (dedup en tabla `posted_news` por link/guid) y — según `mode` — postea **un comentario LLM** (`comment`, rol feed_summary + `prompts/summarize_news_prompt.md` portado) o **cada item** (`post`, capado a N/pasada). Integrado en el loop `run()` (respeta intervalo) + CLI `--news` (one-shot). **Master toggle `NEWS_ENABLED` (default false)** — outward-facing, el admin lo prende. Fetch/parse **verificado en vivo** (La Política Online + Página/12). Tests: `tests/test_news.py` (7). **Cierra M4.**
- **Config review (post-M4):** cada fuente ahora lleva **`category`** (catalogación), inyectada en el contexto del comentario (`_summarize_news`) para que el bot sepa a qué pertenece. Tool **`get_news(category?)`** (scope reply+admin, on por default) para que los **usuarios pidan noticias/links** filtrables por categoría — independiente de `NEWS_ENABLED`. Tests: `tests/test_news_tool.py` (7).
- *(2026-07-27, T28 fase 3d: el pipeline de posteo `run_news_pass`/`NEWS_ENABLED`/modos se eliminó — sobrevive solo la tool `get_news`, ahora con scope feed_reflection y `only_new` (dedup `posted_news`); postear noticias es la rutina `noticias.md`.)*

---

## M5 · MCP, browser & scrapers  `P2`

**Decisión (admin):** el cliente MCP (T29) va **antes** que los scrapers. Los scrapers
(T17–T20) nacen como **MCP servers** independientes (procesos stdio, deps y crashes
aislados, reutilizables desde otros agentes), no como módulos internos de Botata.

### T29 · Cliente MCP → ToolRegistry  `infra` `M`  *(primero de M5)*  ✅ **HECHO**
Botata consume MCP servers externos como tools; cada conector futuro pasa de "tarea" a "línea de config".
- **Acept.:** módulo `mcp_tools.py` (SDK oficial `mcp`): al arranque lee `settings.json` → sección `MCP` (`{server: {transport: stdio|http, command/url, enabled, scopes, tool_filter}}`), conecta, hace `tools/list` y registra cada tool en el `ToolRegistry` (T1) con handler proxy a `tools/call`.
- Nombres prefijados por server (`reddit_top_posts`) para evitar colisiones. Puente async→sync contenido en el módulo (event loop en thread de fondo).
- **Seguridad (regla dura):** tools MCP nacen con scope `admin`; promover a `reply`/`feed_reflection` es opt-in explícito por tool en config — el bot es público y scope reply = superficie de prompt injection.
- Degradación graceful: server caído al arranque → se loguea y se omite (no tira el bot); error en `tools/call` → `ToolResult` de error, no excepción.
- El calendario interno (`events`, T4/T9) sigue nativo — dominio core acoplado a la DB. Calendarios externos (ej. Google Calendar) = server MCP de terceros vía config, sin código.
- Impl.: `mcp_tools.py` (infra genérica estilo `tools.py`, no conoce Botata). `MCPBridge` = event loop en thread daemon; **cada server vive en una task dedicada** que entra/sale de sus propios context managers (los cancel scopes de anyio no cruzan tasks — gotcha del SDK). Sesiones quedan abiertas; reuso si el mismo proceso construye dos registries (run() lo hace); `shutdown()` vía `atexit`. Registro **antes** de `apply_config` → la sección `TOOLS` overridea tools MCP por nombre prefijado. Import lazy: sin sección `MCP` el SDK ni se carga. Handler proxy **jamás lanza** (el call site admin ejecuta sin try/except); v1 texto-only (content blocks de imagen = pendiente). `tests/mcp_echo_server.py` = plantilla FastMCP para los servers de T18–T20. Verificado en vivo (registro + execute + server roto omitido). Tests: `tests/test_mcp_tools.py` (13, unit + E2E stdio real).

### T16 · Tool de navegador agéntica  `tools` `M`  ✅ **HECHO**
- **Acept.:** el usuario puede mandar el bot a una página y que acceda. **OFF por default**, con **aviso de riesgo** explícito al activarla.
- Guardrails: allowlist/denylist de dominios, límite de acciones, sin descarga/ejecución arbitraria.
- **Servida como MCP server** (misma fragilidad/deps pesadas que los scrapers); Botata la consume vía T29.
- **DECISIÓN (investigación 2026-07-11): reusar `@playwright/mcp` de Microsoft — cero código propio.** Verificado en vivo vía el cliente T29 (spawn `npx @playwright/mcp --headless --isolated --allowed-origins ...`): navegación OK y dominio fuera de la allowlist bloqueado (`ERR_BLOCKED_BY_CLIENT`) con error graceful. Los guardrails de la acept. mapean 1:1 a flags oficiales (`--allowed-origins`/`--blocked-origins`, `--headless`, `--isolated`, file access restringido a workspace) + `tool_filter` de T29 para recortar el tool set (25+ tools → navigate/snapshot/close). Requiere **Node 18+** (dev OK v24; verificar en Linux Mint prod). `browser.py`/patchright NO se toca: es la capa *stealth con sesión logueada* para T19/T20 — problema distinto (Playwright MCP no es stealth).
- Impl.: **cero código** — server `browser` declarado en `settings.json` → MCP (`npx @playwright/mcp@latest --headless --isolated`, `tool_filter` a navigate/navigate_back/snapshot/close, **`enabled:false`**). El snapshot inline se resolvió solo: la tool `browser_snapshot` devuelve el árbol de accesibilidad en YAML dentro del ToolResult (el link-a-archivo era solo la respuesta de `navigate`); flujo del LLM = navigate → snapshot. Verificado en vivo por el camino real (`build_tool_registry` + Wikipedia es). Scope `admin` (default T29).
- **⚠️ AVISO DE RIESGO (leer antes de prender `enabled:true`):** el LLM navega cualquier URL que le pidan. Con scope `admin` (default) solo el admin puede mandarlo; **si se promueve a `reply`, cualquier usuario puede dirigir el navegador del bot** → prompt injection vía contenido web + SSRF-lite (URLs internas). Para scope reply: agregar `--allowed-origins "https://sitio1;https://sitio2"` a los args (allowlist dura, verificada: bloquea con `ERR_BLOCKED_BY_CLIENT`) y/o `--blocked-origins`. `--isolated` (perfil en RAM, sin cookies persistentes) ya viene en la config. **Prod:** requiere Node 18+ en el Linux Mint (dev: v24 OK) — la primera corrida de `npx` descarga el paquete.

### T17 · Reddit — spike de API  `scraper` `S`  ✅ **HECHO**
- **Acept.:** investigar viabilidad de la API oficial (PRAW/OAuth read-only) hoy — Reddit devuelve 403 sin auth (visto en T14). Documentar decisión: API / RSS / navegador.
- **DECISIÓN: RSS.** Verificado en vivo (2026-07-11) + fuentes:
  * **API OAuth: descartada.** Desde fines de 2025 las apps nuevas pasan por aprobación manual (Responsible Builder Policy); proyectos personales/hobby son la categoría más rechazada, sin SLA (días a semanas, o silencio). Free tier teórico: 100 QPM no-comercial — irrelevante si no aprueban la app. No hay app preexistente de maripobot (usaba RSS). Reevaluar solo si Reddit afloja el gate.
  * **RSS: funciona hoy sin credenciales.** `https://www.reddit.com/r/{sub}/{sort}/.rss` (Atom, no RSS 2.0) devuelve 200 con UA descriptivo; soporta sorts (`new`, `top/.rss?t=week`) y multireddit (`sub1+sub2`). **Límite duro medido:** 1 request/min por IP, GLOBAL (headers `x-ratelimit-remaining=0.0` tras 1 req; 429 inmediato al segundo hit; confirmado por reportes públicos de junio 2026). Compatible con el patrón Botata: pases espaciados por `interval_hours`, pocas fuentes, ≥61s entre requests dentro de un pase.
  * **Navegador: innecesario** mientras RSS siga vivo (queda como plan C).
  * JSON público (`hot.json`): 403 sin auth — muerto, confirma T14.

### T18 · Reddit ingestion (MCP server)  `scraper` `M`  ✅ **HECHO**
- **Acept.:** MCP server `reddit` (FastMCP, stdio) **vía RSS** (decisión T17). Solo lectura de subs, sin postear. Reemplaza el stub `RedditSource`; Botata lo consume vía T29.
- Requisitos del diseño (de las mediciones de T17): **rate limiter interno ≥61s entre requests** (global al server, con cola o cache corto); parser **Atom** (stdlib `xml`, el `fetch_rss` de T15 parsea RSS 2.0 — extender o duplicar); UA descriptivo fijo; degradación graceful ante 429 (respetar `x-ratelimit-reset`).
- Impl.: `mcp_servers/reddit_server.py` (primer server real sobre la plantilla T29; carpeta nueva `mcp_servers/`). Tool única `get_subreddit_posts(subreddit, sort, time_window, limit)` — acepta multireddit `a+b`; sorts hot/new/top/rising; devuelve JSON title/url/author/published/summary. `_fetch` serializa con lock: espera bloqueante hasta el slot de 61s, cache TTL 10 min por URL, retry único ante 429 si `Retry-After` ≤ 90s. Validación estricta de subreddit/sort/window (anti path-traversal). Declarado en `settings.json` → MCP con **`enabled:false`** (`call_timeout_s:90` porque una llamada puede esperar el slot) — el admin lo prende. Stub `RedditSource` borrado de `sources.py`. **Verificado en vivo** (top diario de r/argentina por el pipeline completo T29→T18). Tests: `tests/test_reddit_server.py` (11: parsing/URLs/limiter/cache sin red + spawn E2E).

### T19 · Scraper X (twscrape → source de la suite)  `scraper` `L`  ✅ **HECHO en Membrilla** (2026-07-20, `scrape_x.py`)
- **Acept.:** X sin API libre → **`twscrape`** (pool multi-cuenta + rate-limit handling; auth por cookies `auth_token`+`ct0`). Reemplaza el stub `TwitterSource`. Cuenta **quemada**, no la real; idealmente proxy residencial.
- **DECISIÓN (investigación 2026-07-18):** Nitter y snscrape **muertos** (ya no hay camino anónimo en X). Working hoy: twscrape (recomendado por rotación de cuentas), twikit (read+write, si el bot algún día postea en X), Tweety/Scweet (ignorar). Managed (twitterapi.io / Bright Data) descartado por no-self-hosted. Auth por cookies = mismo patrón que `login_by_sessionid` de IG. Tier **frágil/best-effort** (rompe c/2-4 semanas cuando X rota tokens/GraphQL). Ya existe una twscrape Claude Code Skill → el envoltorio no es el valor, sí la normalización al contrato. **Migra al repo de la suite (M9 · T31).**

### T20 · Hardening scraper IG (source de la suite)  `scraper` `M`
- **Acept.:** robustecer `scrape_ig.py` / `ig_api.py` (manejo de sesión, reintentos, config de cuentas objetivo) y volverlo un source de la suite (M9 · T31). Mantener `instagrapi` (camino API) como principal.
- **Es el activo diferenciado del repo:** el **descubrimiento por cuenta** (`fetch_recent(username, limit)`) que sobrevive el bloqueo de Meta (login por sessionid + fingerprint es_AR). media-mcp/Cobalt solo bajan **un post por URL** — no descubren por cuenta ni manejan sesión. La envoltura "MCP server" queda subsumida en el modo real-time de la suite (M9 · T36); v1 = modo batch/carpeta (ya funciona).

---

## M6 · Núcleo agéntico  `P2`  *(post-Bluesky funcional, PRE-multicanal)*

Endurecer el núcleo del agente antes de tocar otra plataforma. El software crece
orgánicamente: primero un core sólido en Bluesky, después se suman canales (M7).

### T26 · Workspace de archivos de comportamiento (skills)  `infra` `M`  ✅ **HECHO**
Capacidades definidas en markdown, editables sin tocar código (patrón OpenClaw/Claude skills; extiende SOUL.md + prompts de T23).
- **Acept.:** carpeta `skills/` con archivos `*.md` (frontmatter: nombre + descripción + cuándo aplica; cuerpo: instrucciones). Se cargan en runtime.
- El agente decide qué skill aplicar según contexto (inyección selectiva en el system prompt — no todas siempre, para no quemar contexto).
- Toggleable por skill en config. Agregar/editar una skill = editar un `.md`, sin redeploy.
- Caso de uso objetivo: el admin (o a futuro un moderador) define "cómo responder sobre X" declarativamente.
- Impl.: `skills.py` (infra genérica estilo tools.py; parser frontmatter stdlib, sin pyyaml). **Selección = índice + on-demand:** el system prompt lleva un índice liviano (`name: description`); el agente carga el cuerpo con la tool **`use_skill(name)`** (fase de tools T24) — cumple "inyección selectiva". Frontmatter: `name`/`description` (obligatorios), `scopes` (default los 3), `enabled` (default true), `inline` (fuerza cuerpo al prompt, para skills que el agente no sabría pedir). **El frontmatter ES la config** (sin sección en settings.json). **Excepción admin:** todo inline (`all_inline=True`) porque ese flujo ejecuta UNA tool y usa su resultado como respuesta. Inyectado en los 3 nodos (reply tras lecciones · feed antes de tools · admin al system). Relectura por pase (patrón SOUL/MEMORY) → **edición en caliente** verificada en vivo (habilitar skill editando el .md, sin reiniciar). `skills/README.md` documenta formato + aviso de seguridad (una skill = quien la escribió); `skills/ejemplo.md` (enabled:false) de plantilla. Tests: `tests/test_skills.py` (14).

### T27 · Heartbeat generalizado (scheduler unificado)  `proactivity` `M`  ✅ **HECHO**
Generaliza el loop proactivo: hoy feed (T5), noticias (T15) y eventos son pases hardcodeados en `run()`.
- **Acept.:** registro de tareas periódicas (`{name, interval, enabled, handler}`) donde feed-pass, news-pass y chequeo de eventos del día son entradas; agregar una tarea nueva no toca el loop.
- Pase "heartbeat" opcional: el agente lee pendientes (eventos de hoy T4/T9, recordatorios) y **decide** si tiene algo que decir — misma filosofía agéntica que `ReflectDecideNode`, sin scheduling rígido.
- Config declarativa en `settings.json`; cursores en `feed_cursors` (patrón `news:{host}` ya existente).
- Impl.: `scheduler.py` (genérico, no conoce Botata): `PeriodicTask(name, fn, interval_hours, enabled)` + `run_due()` + `apply_tasks_config()`. **Sin doble gate:** `interval_hours=0` = correr cada iteración (feed/news/mentions se auto-gatean por dentro, sin tocar su lógica); `>0` = gate del scheduler vía cursor `task:{name}` (reusa `get/save_feed_last_run`). **Aislamiento de errores POR TAREA** (mejora sobre el try único por iteración; KeyboardInterrupt atraviesa). `run()` quedó genérico: lista de 4 tareas + `run_due()` + sleep; el poll de menciones se extrajo a `_poll_mentions()` sin cambios. **Heartbeat** = `run_heartbeat_pass()` (función, no grafo): lee `events_today`+`upcoming_events` — **sin eventos no llama al LLM** (costo cero); prompt = SOUL + fecha + `prompts/heartbeat_engine.md` (nuevo, T23) + skills feed_reflection (T26) + eventos + posts recientes; decide con `FeedDecision` reutilizado (rol `feed_opinion`); dedup normalizado contra `bot_posts`; **nunca postea eventos personales no celebrables** (regla en el prompt). Config `TASKS` en settings (heartbeat **off por default**, 12h). CLI `--heartbeat` (pase único). **Verificado en vivo con LLM real** (bsky falso): saludó un cumpleaños en personaje y en la segunda pasada se negó a repetirse ("ya saludé a Polci hoy"). Tests: `test_scheduler.py` (9) + `test_heartbeat.py` (6). **Cierra M6.**

### T30 · Configuración por comandos de admin  `tools` `M`  ✅ **HECHO** *(agregada post-M6, pedido del admin 2026-07-13)*
- **Pausa global `/stop` / `/resume` (2026-07-25, pedido del admin):** comandos EXPLÍCITOS y determinísticos (sin LLM — funcionan aunque el router esté caído), solo admins, exactos (texto sin @menciones == el comando; con palabras extra va al flujo normal). Estado en la DB (`kv.bot_paused`, con quién y cuándo) → sobrevive reinicios (warning al arrancar pausado). Pausado: no corre NINGUNA tarea proactiva (solo `mentions`) y las menciones de no-admins quedan SIN marcar (se responden solas tras `/resume`, mientras sigan en la ventana de poll); a los admins les responde siempre — por ahí viaja el `/resume` (mismo principio que el lock de la tarea mentions). Un no-admin mandando `/stop` sigue el flujo normal (no puede pausar). Tests: `tests/test_pause.py` (6). Suite: 473.
- **Bio automática y prompteable (2026-07-26, pedido del admin):** `set_bio(text)` en los TRES canales (Bluesky: get+put del record `app.bsky.actor.profile/self` preservando displayName/avatar · Mastodon: `account_update_credentials(note=...)` · Discord: `PATCH /applications/@me` description = el "About Me" del bot, único campo bio que la API deja tocar). Qué muestra la bio vive en **`prompts/bio.md`** (archivo de instancia, prompteable: "mostrá tu humor del día"); `run_bio_pass` la redacta en la voz del bot (SOUL+mood+fecha), respeta el límite por canal (256/500/400), y con kv `bio_current` evita tocar la red si no cambió. Dos disparadores: tarea periódica **`bio`** (default OFF, 6h) y tool admin **`update_bio`** (con `instructions` opcional para un one-off). ⚠️ Sin validar en vivo en ningún canal. Tests: `tests/test_bio.py` (9). También: alta manual de memorias desde la UI (`/api/memory/user/add`: facts por usuario y lecciones, mismos upserts con dedup semántico que usa el bot).
- **Triggers de moods (2026-07-27, pedido del admin: "qué hace que el bot se ponga melancólico o angry"):** campo opcional **`triggers:`** en el frontmatter de `moods/*.md` (prompt libre: "me bardearon mucho", "me ignoraron días") — el selector automático (`run_mood_pass`) lo ve junto a la description (`[se dispara cuando: ...]`) y `mood_decide_prompt.md` le ordena PRIORIZAR el mood cuyo trigger matchee el clima leído ("cableado emocional definido por el admin"); sin match, elige libre por description. Sin triggers declarados, todo sigue igual (back-compat). Los 8 moods del template traen triggers de ejemplo; los moods tuneados de las instancias NO se tocaron (el admin los agrega por UI — campo nuevo "disparadores" en el editor de moods, `edit_mood` lo escribe/omite). Tests: +4 (`test_moods` + `test_config_ui`). Suite: 528.
- **`summarize_feed` para rutinas y triggers (2026-07-27, pedido del admin):** la tool gana (1) scope **`feed_reflection`** (default del motor + destrabado el override de botata-arg que la pineaba a reply): las rutinas pueden "leer los últimos posts, entender el humor y la temática" ANTES de decidir qué postear — justo lo que piden las rutinas de memes/canciones de arg; y (2) parámetro **`hours`** (cuánta conversación leer hacia atrás, clamp 1–48, default 6 — el clamp cazó un bug de 0-falsy en el camino). Además `run_mood_pass` acepta `bsky`: si algún mood declara `triggers`, el selector lee también los posts recientes del feed principal ("De qué habla el feed") — los disparadores TEMÁTICOS ("perdió la selección", "noticias tristes") necesitan ver de qué habla la comunidad, no solo cómo lo trataron; best-effort (sin canal/red decide igual). Tests: +4 (`test_reply_tools` + `test_moods`). Suite: 531.
El admin ajusta la config del bot **desde Bluesky** ("@bot prendé el heartbeat cada 6 horas"), sin SSH ni editar JSON.
- **Acept.:** tools scope `admin` que mutan el runtime en vivo Y persisten a settings.json; validación previa a todo; claves de identidad intocables por post.
- Impl.: 6 tools registradas en el registry (factories que se cierran sobre `reg`): `get_bot_config` (estado vivo legible) · `set_tool_config` (enabled/scopes) · `set_task_config` (feed/news/mentions/heartbeat: enabled/intervalo) · `set_feed_config` (enabled/intervalo/política) · `set_news_enabled` · `set_mcp_enabled` (solo persiste — spawn requiere reinicio; el bot lo avisa). **Doble efecto con orden seguro:** persistir PRIMERO vía `_persist_settings_delta` (relee disco → delta → validadores de T22 → escritura atómica con `.bak`); si la validación rechaza, el runtime no se toca. **Aplicación en vivo**: registry compartido (tools), `_RUNTIME_TASKS` (lista viva del loop, nueva), `FEEDS_CONFIG`/`NEWS_ENABLED` (globals que los pases releen). **Guard de claves prohibidas** (defensa en profundidad): `BOT_HANDLE`/`ADMIN_HANDLE`/`MODELS`/estructura MCP no se cambian por comando aunque un handler lo intente — lock-out/secuestro queda imposible desde un post. Prompt admin actualizado con ejemplos; regla "un cambio por mensaje" (el flujo admin ejecuta UNA tool). Verificado con LLM real: "prendé el heartbeat cada 6 horas" → `set_task_config {task: heartbeat, enabled: true, interval_hours: 6}`; "qué tenés apagado" → `get_bot_config` con el estado real. Tests: `tests/test_admin_config_tools.py` (13). Suite: 202.
- **Locks (análisis de superficie, decisión del admin 2026-07-13)** — regla: por comando solo se REDUCE exposición, nunca se amplía. (1) Las 6 tools de config no son target de `set_tool_config` (anti auto-lockout/escalación). (2) **No se AGREGAN scopes `reply`/`feed_reflection`** a ninguna tool por comando — ampliar superficie pública = solo UI; quitar scopes sí se puede. (3) La tarea `mentions` no se apaga por comando (es el canal de los comandos mismos). (4) Guard ampliado: `OPENAI_ENDPOINT` + modelos legacy + `SPOTIFY_REDIRECT_URI` intocables (redirigir endpoints = exfiltración de prompts/memoria). Doble capa: check en el handler (mensaje amable) + `_delta_guard` en la persistencia (defensa en profundidad contra handlers futuros). Verificado en vivo: ante "dale scope reply a save_to_memory" el LLM rechaza citando el lock, sin intentar la tool. Tests: +5 (18 totales). Suite: 207.

---

## M7 · Multi-canal  `P3`  *(solo con el agente Bluesky completamente listo)*  ⏸️ **el grueso POSTERGADO 2026-07-27** — lo que sigue vivo son las implementaciones de canal de **M10** (T39/T40/T41) sobre el contrato ya cerrado

### T28c · Perfiles de instancia (motor vs agente)  `infra` `M`  ✅ **HECHO** (2026-07-24)
Materializa "Botata es genérico": el **motor** (repo, hay UNO) corre N **instancias** (carpeta con la identidad+datos del agente: config, .env, SOUL, prompts, skills, moods, posted/DB, scrape). Decisión del admin: **canal nuevo de la misma comunidad = misma instancia** (el Discord de la comunidad ARG va dentro de la instancia arg) **; comunidad nueva = instancia nueva** (Mastodon = testbed `botata-dev` con su propia DB). Se descartó instancias-como-branches/worktrees (coreografía de merges perpetua) a favor de separación por PATH con una sola rama de código.
- Impl.: `src/instance.py` — `instance_dir()` con precedencia `--instance` (escaneado de sys.argv, los paths se resuelven en import-time) > env `BOTATA_INSTANCE` > **raíz del repo (back-compat total: el layout actual sigue andando sin tocar nada)**. Los 5 módulos con paths (`botata`/`db`/`config_ui`/`catalog`/`spotify_auth`) cuelgan su `BASE_DIR` de ahí; en `config_ui` el asset `ui/config.html` queda en el repo (es del motor) y la config editada es la de la instancia. Flag `--instance` declarado en los 6 argparse (botata, config_ui, mem_admin, parse_memory, spotify_auth, catalog).
- **`instance_template/`** (settings neutro sin identidad + SOUL.md neutra + .env.example + README) + **`src/init_instance.py`** (stdlib puro, no importa botata): copia plantilla + defaults del motor (`prompts/`, `moods/` — genéricos a propósito) + esqueleto (skills/context/posted/scrape); se niega a pisar una instancia existente. `python src/init_instance.py <dir>` → editar → `python -m botata --instance <dir>`.
- Pendiente de la reorganización física (paso aparte, con checklist): carpeta raíz `proyecto-botata/` con `core/` (el repo) + `bots/botata-arg/` + `bots/botata-dev/` + `vault/` afuera. Tests: `tests/test_instance.py` (6). Suite: 392.

### T28b · ProviderRegistry (bloques de contexto componibles)  `infra` `M`  ⏸️ **POSTERGADO** (2026-07-27, prioridad = lanzamiento)  *(prep de T28 — idea robada de elizaOS, 2026-07-24)*
- **Origen:** en eliza las fuentes de contexto ("providers") son componentes registrados y componibles, gemelos de las actions. En Botata los bloques (`memory_block`/`prefs_block`/`calendar_block`/`mood_line`/facts del hilo) están cableados a mano en `GenerateReplyNode` y repetidos con variaciones en el pase de rutinas y el de feed — flujos outward duplicando el armado de contexto. *(La unificación de rutinas 2026-07-26 ya redujo los flujos de 5 a 2: heartbeat/rooms/public_reflection/playlist_share hoy son el único pase de rutinas.)*
- **Acept.:** registry gemelo de `ToolRegistry`: cada provider declara `name` + `scopes` (reply/feed_reflection/heartbeat/...) + fn que devuelve su bloque (best-effort, "" si falla). Los nodos componen el system prompt iterando providers del scope en vez de llamar bloques a mano. Una feature/canal nuevo inyecta contexto sin tocar los nodos.
- **Por qué antes de T28:** es exactamente la infra que multi-canal pide (un canal nuevo = providers nuevos sin tocar el grafo). Refactor sin cambio de comportamiento — mismos bloques, otro cableado.

### T28 · Abstracción Channel (gateway multi-plataforma)  `infra` `L`  🔶 **EN CURSO** (Mastodon implementado 2026-07-24, ⚠️ sin validar en vivo)
- **Acept.:** interfaz `Channel` (mentions/post/reply/perfil de autor, sobre de mensaje normalizado) de la que `BskyChannel` es la primera implementación. El grafo langgraph no sabe en qué plataforma habla.
- **Impl. fase 1 (2026-07-24):** contrato Channel por **duck typing** = la superficie que el grafo ya usaba de `BskyClient` (documentada en `channels.py`: get_mentions/get_thread_info/reply/post/get_profile/block_user/get_feed_posts/...). **`MastodonChannel`** (`channels.py`, Mastodon.py, import lazy): notificaciones mention → dicts del grafo; `status_context` → hilo; reply antepone `@autor` si el LLM no lo puso (en Mastodon la mención notifica); HTML → texto (`strip_html`); media del hilo por **alt-text** (vision diferida); perfil → adapter `.did/.display_name/.description` (bio p/ LoadContextNode); fuentes de feed: `following`→home, `local`→timeline local (type nuevo), `list`→por id; grupos por feed (follows/list_accounts). **Ids opacos:** status id como uri y cid — la DB ya era agnóstica; el schema rooms `(channel, conversation_id, message_id)` queda para la fase 2. Selector **`CHANNEL`** en settings (+`MASTODON_BASE_URL`; token en `.env` `MASTODON_ACCESS_TOKEN`) con factory `build_channel()` (única fábrica, 8 call sites); credenciales exigidas por canal al construir, no en import. `CHANNEL`/base URL = `_PROTECTED_SETTINGS` (cambiar de red = solo UI). UI: select de canal activo + campo de instancia + token. Tests: `tests/test_channels.py` (12; API falsa). Suite: 431.
- **Impl. fase 2 — Discord (2026-07-25, ⚠️ sin validar en vivo):** **`DiscordChannel`** (`channels.py`, REST v10 por httpx — SIN discord.py ni gateway: el poll loop del bot ya marca la cadencia y el dedup por DB hace inocuo releer los últimos 25 mensajes por canal). Escucha una lista fija de canales (**`DISCORD_CHANNEL_IDS`** en settings, protegido — ampliar canales = solo UI; el primero es el principal, destino de posts proactivos). Mención = mensaje que @menciona al bot o reply a un mensaje suyo; otros bots se ignoran (anti-loop). Ids opacos: `uri = "channel_id/message_id"` (la reply exige el canal), `cid = message_id`. Hilo por cadena de `message_reference` (tope 12 hops); `<@id>` → `@username` vía array `mentions`; perfiles por cache de usuarios vistos (Discord no expone bios a bots → description vacía); `block_user` no-op (moderación = permisos del server); media anotada por filename/content_type (vision diferida); feed type nuevo `channel` (mensajes de un canal por id). Token en `.env` `DISCORD_BOT_TOKEN` (Developer Portal, Message Content Intent). Retry simple ante 429. UI: fila Discord (ids CSV + hint del token) + check de credencial. Tests: +11 en `tests/test_channels.py` (HTTP falso). Suite: 463.
- **Impl. fase 3 — rooms de comportamiento (2026-07-26, pedido del admin: "cada 1h memes en #memes, cada 3h noticias en #politica, actitud por canal"):** comportamiento POR CANAL en archivos **`rooms/*.md`** de la instancia (patrón skills: el frontmatter ES la config — `channel` (id Discord), `interval_hours` (0 = room solo-actitud), `enabled`; el cuerpo = instrucciones + actitud, prompt libre releído por pase → edición en caliente). Decisión de diseño: **el código pone los rieles, el prompt define el comportamiento** — el destino (`post(target=...)`, nuevo en el contrato Channel; Bluesky/Mastodon lo ignoran) y la cadencia (tarea **`rooms`** cada ciclo + cursor `room:{name}`, patrón news:{host}) son determinísticos en código (lección T4d: timing jamás al LLM); qué postea y con qué actitud es 100% prompt. El cuerpo se usa DOS veces: pase proactivo del room (con tools feed_reflection, últimos 15 mensajes del canal como contexto, catálogo de imágenes, anti-repetición filtrado POR canal — `recent_bot_posts(uri_prefix)`) y **registro en replies** (mención llegada del canal del room → el cuerpo entra al system prompt). Decisión del admin: el room **MATIZA, nunca reemplaza** — SOUL + mood siempre arriba (misma relación moods↔SOUL), garantizado por construcción (el bloque se apila, no sustituye). Cursor grabado incondicional al intentar el pase (pase fallido espera su intervalo — sin hot-loop de LLM). Los canales con room se ESCUCHAN solos (merge en `build_channel`; room nuevo → reiniciar para escuchar, el posteo proactivo anda sin reiniciar). Rooms = archivos de instancia → solo admin (misma regla que skills/moods). Template `rooms/README.md` + `ejemplo.md` (off). ⚠️ Sin validar en vivo. Tests: `tests/test_rooms.py` (13). Suite: 526.
- **Impl. fase 3b — RUTINAS: unificación heartbeat+rooms (2026-07-26, pedido del admin: "necesito simplificar; el heartbeat termina siendo un tipo de rutina más"):** un solo concepto para TODA la conducta proactiva con cadencia — **`routines/*.md`** (`routines.py`, generaliza rooms.py: `interval_hours` + `channel` OPCIONAL + `enabled` en frontmatter; cuerpo = qué hacer y actitud). Rutina SIN channel = el ex-heartbeat (postea al feed principal con la maquinaria completa: SOUL+mood+memoria+prefs+skills+eventos-contexto con filtro anti-bypass de T4d+tools+dedup global); CON channel = el ex-room (contexto y dedup del canal, `post(target=)`, y su cuerpo matiza replies de ese canal — SOUL+mood siempre arriba). Cadencia por cursor `routine:{name}`, timing SIEMPRE del código (T4d). **Eliminados:** tarea `heartbeat`, `prompts/heartbeat_checklist.md`, `context/heartbeat_override.md`, tool **`set_heartbeat`** y la tarea `rooms` (absorbida por `routines`, cada ciclo). **Tools admin nuevas (mismo día, pedido del admin: "la gracia de ser admin"): `set_routine`** (crea/modifica una rutina por comando: instructions/interval_hours/channel/enabled; update parcial conserva lo no pasado; validación de nombre anti-traversal + channel numérico) **y `delete_routine`** — scope `admin` estricto (el lock global de T30 impide promoverlas a scopes públicos por post). Para que "dejá de postear memes" resuelva a la rutina correcta por lo que HACE (no por adivinar el nombre), el flujo admin inyecta al system prompt la lista de RUTINAS ACTUALES (nombre, ON/OFF, cadencia, canal, primeros 120 chars del cuerpo; apagadas incluidas — `load_routines(include_disabled=True)`). Tests: +6 (`test_admin_config_tools` + `test_admin_multitool`). Suite: 529. `prompts/heartbeat_engine.md` → **`routines_engine.md`** (reglas duras del pase). CLI `--heartbeat` → `--routines` (force). Migradas las 3 instancias: la checklist de botata-arg → `routines/general.md` (bardeo, 1h) + `memes.md` (4h) + `canciones.md` (4h) — las líneas de calendario se BORRARON (redundantes con la tarea calendar; eran el vector del triple-post); dev/mafiazeth → `general.md` (2h, ex-checklist sin el ítem calendario). UI: sección **⏰ Rutinas** (siempre visible; hint de channel solo en discord) con listado (cadencia, canal, próximo pase por cursor, cuerpo) + editor compartido (`kind: routine` en `/api/behavior-file`; channel opcional numérico); secciones Heartbeat y Rooms eliminadas. Tests: `test_rooms.py`+`test_heartbeat.py` → **`test_routines.py`** (23) + `test_admin_config_tools` sin set_heartbeat. Suite: 523. ⚠️ Sin validar en vivo (Discord).
- **Impl. fase 3c — public_reflection y playlist_share también son rutinas (2026-07-26, pedido del admin: "Tareas periódicas es casi redundante a las rutinas, y peor: no es modificable"):** las dos tareas outward que quedaban eran rutinas disfrazadas (conducta de posteo con cadencia y prompt no editable en su lugar natural) → hoy son **rutinas default del template**: `routines/reflexion.md` (reflexión pública en primera persona; el pase de rutinas ahora inyecta las **lecciones recientes** a TODAS las rutinas, y la actividad propia se consulta con `get_my_recent_posts`, que ganó scope `feed_reflection`) y `routines/playlist.md` (compartir un tema de la comunidad), apoyada en la tool nueva **`get_playlist_track`** (scope feed_reflection+admin: tema al azar con anti-repetición contra posts recientes — la pata de DATOS en código, la conducta en el archivo). Eliminados: `run_public_reflection_pass`, `run_playlist_share_pass`, sus tareas/CLI (`--reflect-public`, `--share-playlist`) y prompts (`public_reflection_prompt.md`, `playlist_share_prompt.md` → cuerpos de rutina, castellano en arg). Migradas las 3 instancias respetando sus toggles/cadencias. El corte quedó limpio: **Tareas = deberes del motor** (mentions, feed, news, calendar, actions, reflection, mood, bio — reencuadrada así en la UI), **Rutinas = TODA la personalidad proactiva**. Tests: `test_playlist_share`+`test_public_reflection` → `test_playlist_tool.py` (8) + lecciones en `test_routines`. Suite: 524.
- **Impl. fase 3d — feed a solo lectura + RSS a tool + lanzador Membrilla (2026-07-27, pedido del admin: "la parte de cuán charlatán es se solapa con las rutinas; RSS debería ser una tool llamada como rutina"):** cierre definitivo del corte motor/personalidad. **(1) Feed pass solo-lectura:** el grafo T5 queda `fetch → learn → summarize` — se eliminaron `ReflectDecideNode`, `PostFeedNode`, `posting_policy` (con sus prompts `feed_policy_*.md` y `reflect_feed_prompt.md`), `autonomous_images` por feed, `--force-post`, `--post-summary` y `FeedProcessor.post_opinion`. Los FEEDS son ahora **fuentes de lectura**: alimentan memoria (facts/eventos), `context/feeds/*.md` (`summarize_feed`) y el clima de moods; opinar sobre el feed = una rutina que llama `summarize_feed`. UI: sección "📖 Lectura del feed". **(2) RSS = tool `get_news` + rutina:** eliminados `run_news_pass` (modos comment/post), la tarea `news`, `NEWS_ENABLED`, `set_news_enabled`, `--news` y `summarize_news_prompt.md`. `get_news` gana scope `feed_reflection` + parámetro **`only_new`** (filtra contra `posted_news` y marca visto → una rutina de noticias no repite titulares entre pases); rutina default `routines/noticias.md` (template off; dev/maf migradas ON porque tenían NEWS_ENABLED, arg off como estaba). Fuentes RSS en la UI → junto a Integraciones (solo url/título/categoría/descripción/enabled). **(3) Membrilla desde la UI:** flujo en 2 pasos (scrapear → indexar) — botón **"Lanzar scraper"** que corre `settings.MEMBRILLA` (`{repo, commands[]}`, cada comando con cwd=repo y `MEMBRILLA_OUTPUT_DIR` → `scrape/` de la instancia; a futuro abrirá la página propia de Membrilla) + badge con **scrapeados / en catálogo / sin indexar** (sidecars vs `image_catalog` readonly). Tareas del motor finales: mentions, feed (lectura), calendar, actions, routines, reflection, mood, bio. Tests: `test_news.py` absorbido por `test_news_tool.py` (only_new + scopes), `test_feed_proactive.py` reescrito (lee/aprende/no postea), +3 Membrilla en `test_config_ui`. Suite: **526**.
- **Falta (fase 2):** ⚠️ validar Mastodon en vivo (instancia `botata-dev`) · ⚠️ validar Discord en vivo (server de prueba, incluye rutinas fase 3) · vision sobre media de Mastodon/Discord · visibilidad configurable de posts/replies · schema rooms de persistencia `(channel, conversation_id, message_id)` (distinto de los rooms de comportamiento de fase 3) · gateway websocket si hiciera falta tiempo real · T28b providers.
- **Anotado, NO encarar ahora (2026-07-26, pedido del admin):** multi-canal SIMULTÁNEO por instancia (el mismo bot viviendo a la vez en Bluesky + Discord de la misma comunidad). Hoy instancia = UN canal (`build_channel()` fabrica uno). Obra grande: N canales en el loop, identidad de usuarios cross-red, memoria unificada, destino de posts proactivos, budget. Detalle → vault `pendientes.md` (backlog).
- **Alcance decidido (discusión 2026-07-19): SOLO Mastodon y Discord.** Orden: Bluesky → **Mastodon** (port de mayor fidelidad: mención→reply, timeline local = feed comunitario, bios, flag `bot` oficial, `Mastodon.py`) → **Discord** (bots ciudadanos de primera clase vía API oficial, pero mayor impedance mismatch: sin feed ni bios ricas, modelo push/gateway vs poll — es donde la abstracción se estresa de verdad). ~~Telegram/WhatsApp salen del plan por ahora.~~ **REVERTIDO el 2026-07-27** (pedido del admin, criterio de lanzamiento): Telegram y WhatsApp entran como T40 y T41. La decisión original era correcta *mientras el objetivo era madurar el motor*; cambió el objetivo, no el análisis técnico. **Regla de diseño: T28 se diseña mirando SOLO estos dos casos reales**, no plataformas hipotéticas.
- **Si sobra tiempo:** Pleroma/Akkoma (forks del fediverso que implementan la Mastodon client API) deberían andar **gratis** sobre el canal Mastodon — solo testear, cero código. Nada más que eso.
- **Evaluado y DESCARTADO como canal (2026-07-19):** Misskey/Sharkey (API propia no-Mastodon, comunidades ES inexistentes) · Pixelfed/Friendica (engagement flaco / comunidades minúsculas) · PeerTube, Piefed, Skylight (sin grafo conversacional) · Ghost/WordPress/WhiteWind (long-form: serían un feature "digest", no un canal). **No son canales sino otra cosa** (backlog, sin prioridad): Lemmy = *fuente* estilo Reddit-RSS para Membrilla, jamás canal (comunidades hostiles a bots LLM); Mobilizon y Smoke Signal = *integraciones del calendario* (T4/T9) — Smoke Signal sería casi gratis (misma sesión atproto, otro lexicon).
- Incluye revisar el modelo de sesión: en Bluesky la conversación es el thread (se reconstruye por `get_post_thread`); en Discord hace falta estado de conversación persistente por canal/chat.
- **Rooms (idea robada de elizaOS, 2026-07-24):** eliza clava toda conversación a un "room" agnóstico de plataforma (un canal de Discord, un hilo, un DM) en vez de a IDs nativos. Hoy `replied_posts`/`interactions`/`bot_posts` están casados con URIs de Bluesky. Al diseñar T28, clavar la clave `(channel, conversation_id, message_id)` en el schema — es LA decisión de persistencia que hace que el grafo no sepa en qué plataforma habla.
- **Nota menor (misma fuente):** el plugin de eliza registra junto sus actions+providers+evaluators+services (bundle vertical). En Botata una feature (ej. Spotify) desparrama piezas entre `build_tool_registry`, scheduler y settings. Si T28b existe, considerar un patrón "feature module" que registre tools+providers+tarea junto. Azúcar organizativo, no bloqueante.
- **Evaluado y DESCARTADO de elizaOS (2026-07-24):** el resto — el pivot v2/v3 a "OS de agentes" (wallets cripto, marketplace, distro booteable) es scope ajeno; sus evaluators/character files/memoria vectorial ya tienen equivalentes mejores en Botata (UpdateProfileNode+reflection · SOUL/skills/moods/prefs · híbrida vec+FTS5+RRF); los "similes" por action los cubren las descriptions de tools.

---

## M8 · Después  `P3`

### T21 · Descarga de videos IG  `scraper` `M`  ⏸️ **POSTERGADO** (2026-07-27)
- **Acept.:** extender el scraper IG para bajar videos (hoy se filtran). Baja prioridad.

### T22 · UI de configuración  `ui` `L`  ✅ **HECHO** *(adelantada pre-M7 por decisión del admin)*
- **Acept.:** UI gráfica para: credenciales de Bluesky, modelo(s), tipo de comunidad (feeds/listas/timeline), y toggles de tools.
- ~~Última a propósito~~ **Adelantada (2026-07-12):** el schema de config ya se estabilizó (TOOLS/TASKS/MCP/FEEDS/MODELS) y la pila de toggles justificaba la UI ya. Criterio del admin: **esta es la UI del núcleo**; las futuras UIs por canal (Discord, etc.) serán interfaces separadas — no una UI para todo.
- Impl.: `config_ui.py` (stdlib puro: `http.server`, cero deps) + `ui/config.html` (una página, vanilla JS, dark). **Solo localhost** (bind 127.0.0.1; `python config_ui.py [--port] [--no-browser]`). Secciones: identidad · credenciales (.env **write-only**: la API jamás devuelve valores, solo qué claves están seteadas; input vacío = no tocar) · modelos (endpoints/aliases/roles) · feeds · tools (enabled + scopes con ⚠️ al promover a reply) · tareas · MCP · noticias (master + fuentes) · skills (toggle hot-reload que reescribe el frontmatter). **Validación server-side antes de escribir** (scopes/tipos/políticas/aliases huérfanos → 400 con detalle, no toca el archivo); escrituras atómicas (`tmp` + `os.replace`) con backup `.bak`. Verificado en vivo por browser: toggle de skill → frontmatter en disco → restaurado; settings inválido → 400 visible; round-trip del settings re-serializado → el bot importa igual. Tests: `tests/test_config_ui.py` (18). Suite: 189.

- **T22b · Completitud de la UI (2026-07-26, pedido del admin):** sección **🪪 Bio** (toggle+intervalo de la tarea `bio`, editor de `prompts/bio.md`, bio aplicada por última vez — `DATA.bio_current`) · **editor de skills** (ver/editar/crear/borrar `skills/*.md` desde la UI, no solo prender/apagar) · sección **🚪 Rooms** con listado + editor de `rooms/*.md` — ambos vía endpoint `POST /api/behavior-file` (get/save/delete, nombre pelado anti-traversal, valida frontmatter: skill = name+description, room = channel numérico + interval numérico, cuerpo no vacío) · settings huérfanos ahora editables: **`BOT_ACTIONS_FROM`** (Calendario) y **`READ_THREAD_MEDIA`** (Avanzado), con validación · **visibilidad por canal**: Rooms (sección + nav) solo aparece con CHANNEL=discord, en vivo al cambiar el select (los rows Mastodon/Discord de Canal ya lo hacían) · fix: `read_data` tenía un `-3 hours` hardcodeado que se escapó de T4d (ahora usa LOCAL_TZ). Verificado en vivo contra mafiazeth (crear/borrar skill por API real, traversal rechazado, Rooms oculto en bluesky). Tests: +2 (`test_config_ui`: editor + validadores). Suite: 530. *(Mismo día, superseded por T28 fase 3b: las secciones Rooms y Heartbeat se fusionaron en ⏰ Rutinas.)*

### T37 · Import de character cards (chara_card_v2) en la config UI  `ui` `M`  ⏸️ **POSTERGADO** (2026-07-27)  *(decidido 2026-07-24 — investigación SillyTavern)*
Compatibilidad con el ecosistema de character cards de SillyTavern/RisuAI/Chub: sección "Importar personaje" en `config_ui.py` — el admin sube un PNG (chunk `tEXt` "chara", base64 JSON; v3 = chunk `ccv3`) o un `.json` y la UI lo **adapta** a las capas de identidad de Botata. Hook de adopción directo para "Botata es genérico": montar una comunidad nueva arranca importando una card ya hecha en vez de escribir SOUL.md desde cero.
- **Mapeo decidido:** `description`+`personality`+`system_prompt` → SOUL.md · `mes_example` → sección de ejemplos de tono en SOUL · `character_book` entradas con `keys` → `skills/*.md` (keys → description del índice; el matching semántico de `use_skill` reemplaza —y mejora— el keyword matching de ST) · entradas `constant: true` → `bot_memory` · `creator`/`tags`/`character_version`/`creator_notes` → frontmatter de SOUL · **descarte documentado con aviso**: `first_mes`/`alternate_greetings`/`scenario` (roleplay 1:1, sin análogo en bot social) y `post_history_instructions` (suele traer jailbreaks — descarte deliberado, jamás importar).
- **Acept.:** parser PNG/JSON **stdlib puro** (`struct`+`zlib`+`base64`, cero deps, patrón config_ui) · preview del mapeo ANTES de escribir (el admin ve qué va a dónde y qué se descarta) · backup `.bak` de SOUL.md/skills previos · paso opcional de destilación con LLM ("resumí al formato SOUL", off por default — muchas cards públicas son verborrágicas estilo W++) · `extensions.botata` = round-trip propio (export de moods/prefs) — respetando la regla de la spec de nunca destruir extensions ajenas.
- **Seguridad:** el contenido de la card entra al system prompt → **solo operación de admin vía UI/CLI, jamás tool del bot** (misma regla que `skills/` y `moods/`). Export inverso (SOUL+skills+prefs → PNG con el avatar del bot) = v2 de la tarea, opcional.

---

## M9 · Suite de scrapers + índice de contenido (repo propio)  `P2/P3`

Decisión completa en **CLAUDE.md** (*Decisiones pendientes → Suite de scrapers*). El subsistema de
scraping se **separa a repo propio** (la suite **Membrilla**); Botata pasa a **consumidor**. Frontera =
contrato `SourceItem`; la **capa semántica vive en el consumidor** (no en la suite); dos modos
(batch/carpeta v1 · real-time/MCP v2); dos tiers (**clientes de API estables** vs **scrapers frágiles**).
Botata es genérico/multi-comunidad: la relevancia de cada fuente es config del admin, no del código.
**v1 = solo modo batch/carpeta** (probado, sin LLM adentro).

### T31 · Extracción de la suite a repo propio  `scraper` `L`  `P2`  ✅ **HECHO** (2026-07-20)
- **Acept.:** mover el scraping (`scrape_ig.py`/`ig_api.py`/`sources.py`/`browser.py`/`extract.py` + `config/instagram.json`) a repo propio. `SourceItem` como **frontera pública**. **Quitar `from botata import ...`** (el modelo/LLM lo inyecta el consumidor). Partir `db.py`: *ledger de scrapeo* (`has_scraped_item`/`save_scraped_item`) con la suite; *índice semántico* en Botata. Contrato de output = item normalizado + path a media local en carpeta. v1 = solo modo batch/carpeta.
- Impl.: repo **[`pablofprz/membrilla`](https://github.com/pablofprz/membrilla)** (privado), historial preservado vía `git filter-repo` sobre los 5 archivos. **Acoplamiento roto:** el `from botata import` era peso muerto (el `LLMExtractor` nunca se usa en `IGSource` — extrae por meta tags) → cortado; extractor ahora opcional. **Ledger propio** `ledger.py` (SQLite `membrilla.db`) + **`write_sidecar`**: por cada item, `<external_id>.json` junto a la media = **contrato de handoff**. **Del lado Botata** (branch `feat/membrilla-split`): borrados los 5 archivos; `db.py` sin `scraped_items`/ledger/`get_scraped_meta` (`image_catalog` intacto); `catalog.py` lee el sidecar en vez de la tabla; `pyproject` sin `instagrapi`/`playwright`/`patchright`. Ningún módulo del runtime importaba la suite (solo `catalog.py`). **276 tests verdes.**

### T32 · Índice semántico de contenido scrapeado (consumer-side)  `proactivity` `M`  `P2`
- **Acept.:** Botata ingesta la carpeta de la suite — **etapa 2** (offline): corre vision, guarda descripción + embedding en una tabla de contenido nueva. **Etapa 3** (en query): pedidos temáticos ("algo de conejos") resuelven por `hybrid_search` reusando el motor existente (vec0 bge-m3 + FTS5 + RRF). **Búsqueda por significado = retrieval sobre el índice, nunca fetch en vivo.** El fetch en vivo (T36) sirve solo para "lo último de fuente conocida".
- **Estado real (2026-07-27):** la etapa 2 ya existe parcialmente — `catalog.py` corre visión sobre `scrape/` y llena `image_catalog` (descripción + categoría + tags + OCR + embedding), y la etapa 3 funciona vía `search_images` (`hybrid_search_image_catalog`: vec0 + FTS5 + RRF). Lo que falta es el **eje temático**: hoy "meme de fútbol" solo matchea si el modelo de visión escribió "fútbol" en la descripción/tags — no hay forma de declarar qué fuentes son de qué tema. Eso es **T38**, que se apoya en esta tarea.

### T33 · Source TikTok (Whisper + frames)  `scraper` `L`  `P3`  ✅ **HECHO en Membrilla** (`scrape_tiktok.py`)
- **Acept.:** source TikTok con la **opción pesada**: transcripción de audio (Whisper local) + descripción de frames (video-first rompe el pipeline image-first). Extractor: `TikTokPy`/`TikTok-Api` (Playwright firma X-Bogus/A-Bogus). **Después de X (T19).** Los "slides" (carruseles de foto) caen en el pipeline image-first tal cual.

### T34 · Source Pinterest (gallery-dl)  `scraper` `S`  `P3`  ✅ **HECHO en Membrilla** (`scrape_pinterest.py`)
- **Acept.:** source Pinterest vía **`gallery-dl`** (image-first, drop-in, muchas veces sin login → tier estable). Descubrimiento por perfil + board + tag. Off por default en Botata (relevancia por comunidad — fuerte para un bot de aves/estético).

### T35 · Source Tumblr (API oficial)  `scraper` `S`  `P3`  ✅ **HECHO en Membrilla** (`scrape_tumblr.py`)
- **Acept.:** source Tumblr vía **`pytumblr2`** (API v2 oficial, NPF; consumer key gratis). Posts por blog + por tag. **Cliente de API estable, no scraper** (legal, baja fragilidad).

**Estado de los sources (verificado 2026-07-27 contra el repo Membrilla):** los **5 están
implementados** — Instagram (`instagrapi` + camino navegador), Tumblr (API v2), Pinterest
(`gallery-dl`), TikTok (`yt-dlp` + frames ffmpeg + transcript Whisper) y X (timeline del DOM
por navegador, `twscrape` como fallback), todos sobre el `runner.py` genérico con ledger e
idempotencia. Lo que falta en M9 es del lado **consumidor** (Botata): el eje temático (T38) y
el modo a demanda (T36).

### T36 · Modo real-time / MCP on-demand de la suite (v2)  `scraper` `M`  `P3`
- **Acept.:** front-end MCP sobre el mismo core de extractores para **"lo último de fuente CONOCIDA" en vivo** (frescura, no búsqueda temática). Sesiones calientes (sin login interactivo mid-request), rate-limit sincrónico, dedup contra la carpeta. Subsume la envoltura MCP de IG/X. Se madura con Botata como primer consumidor antes de prometérselo a otros.
- **Análisis de viabilidad (2026-07-27, pregunta del admin: "¿un usuario pide un meme de fútbol y Membrilla scrapea en el momento?"):** técnicamente sale barato (server FastMCP en Membrilla envolviendo `runner.scrape`, ~100-150 líneas; Botata ya tiene cliente MCP y las tools MCP nacen scope admin). **Pero NO va en el camino de la reply:** (a) latencia — un scrape son decenas de segundos a minutos (fetch + descarga + los delays humanos de 15-35s que Membrilla mete a propósito) más la visión por imagen, y el tool-calling de Botata es de UNA ronda sin jobs en background; (b) superficie — que cualquier usuario dispare scrapes gatilla el tier frágil (IG/X rompen c/2-4 semanas y arriesgan la sesión); (c) costo de visión por pedido. **Diseño elegido:** la reply resuelve SIEMPRE contra el índice (T32/T38, instantáneo) y el refresh es asíncrono — una rutina que llama al MCP cada X horas, o un refresh disparado en background mientras el bot contesta con lo indexado. El "a pedido en vivo" queda acotado al tier de **API estable** (Pinterest/Tumblr/Reddit, sin riesgo de quemar cuenta) y aun así fuera del turno de la reply.

### T38 · Registro de fuentes de contenido por tema  `infra` `M`  `P2`  ✅ **HECHO** (2026-07-27)
"¿Dónde defino la fuente de contenido en Botata? Si pido las últimas noticias de política, que
sepa que las fuentes son los RSS de 3 diarios; si piden un meme de fútbol, que sepa qué cuentas
son las de memes de fútbol."

- **Estado hoy (relevado 2026-07-27):** el patrón **ya existe y funciona para RSS** — cada fuente de `news_sites.json` lleva `category` ("política", "noticias") y `get_news(category)` filtra por él: el bot pasa el tema y el código resuelve qué diarios son. **Para contenido scrapeado no existe:** `image_catalog` guarda el origen POR ITEM (`platform`, `source_name`, `tags`, índice `(platform, source_name)`) pero no hay ningún registro de fuentes, y `hybrid_search_image_catalog` solo filtra por `category` en el sentido de *tipo de archivo* (`meme|foto|arte|captura|otro`), no por tema. `config/image_subreddits.json` y `video_subreddits.json` están en la plantilla pero **no los lee nadie** (cero consumidores — fósiles del monolito, donde ahí vivía justamente este concepto). Membrilla tampoco tiene noción de tema: sus configs por plataforma son listas planas de targets (`name`, `max_posts`) y `runner.Target` no tiene categoría.
- **Corrección de diseño (2026-07-27, durante la implementación): el registro vive en BOTATA, no en Membrilla.** La primera versión de esta spec proponía un `category` por target en los configs de Membrilla, propagado por el sidecar. Contradice la frontera ya decidida en M9 — *"Membrilla adquiere; el consumidor interpreta"* y *"la relevancia de cada fuente es config del admin, no del código"*: el tema **es** interpretación, y dos consumidores distintos pueden querer temas distintos de la misma cuenta. Beneficios de que viva del lado consumidor: **Membrilla no se toca**, funciona sobre el contenido **ya scrapeado** (sin re-scrapear ni re-describir) y no hace falta migrar la DB.
- **Acept.:**
  * Registro nuevo en la instancia: `config/content_sources.json`, **misma forma que `news_sites.json`** — lista de `{platform, source, category, description, enabled}`. Es el gemelo de las fuentes RSS para el contenido scrapeado.
  * Resolución **en query, no en indexado**: el filtro por tema traduce categoría → conjunto de `source_name` y se aplica en el `WHERE` de `hybrid_search_image_catalog`. Así, editar el registro tiene **efecto inmediato** (sin reindexar) — misma propiedad que las skills/rutinas hot-editables.
  * `hybrid_search_image_catalog(sources=[...])` + parámetro `topic` en `search_images` → "un meme de fútbol" resuelve contra **fuentes declaradas por el admin**, no contra lo que adivinó el modelo de visión.
  * **Taxonomía única**: la misma cadena de categoría sirve para `news_sites.json` y para las fuentes de media. UI: una vista "fuentes de contenido por tema" que junte RSS + cuentas/boards de Membrilla bajo la misma taxonomía (engancha con la pestaña de plugins del backlog).
  * Borrar `image_subreddits.json` / `video_subreddits.json` (config muerta).
- **Decisión de diseño (2026-07-27): el registro es CONFIG VALIDADA, no prompt.** Se evaluó ponerlo en las **skills** (idea del admin) y se descartó: una skill entra al system prompt como texto libre, así que el LLM tendría que leer la lista de cuentas y pasarla como parámetro de la tool — puede alucinar una cuenta o escribirla mal, y en el caso Membrilla el parámetro es *qué se scrapea*, o sea un vector para terminar scrapeando algo no autorizado. Misma doctrina que el timing de las rutinas: **el código pone los rieles** (qué fuentes existen y cuáles se pueden tocar) **y el prompt define el comportamiento**. La skill SÍ es el lugar del criterio — "cuando pidan memes de fútbol, buscá con estas palabras, con este tono, y cuándo no corresponde".
- **Relación:** se apoya en **T32** (el índice ya existe) y es el prerequisito que hace útil a **T36** (el refresh asíncrono necesita saber qué fuentes refrescar para un tema).

### T38b · Registro ÚNICO de fuentes (RSS es un conector más)  `infra` `M`  `P2`  ✅ **HECHO** (2026-07-27)
Pedido del admin, el mismo día que T38: *"Integraciones, Fuentes y Fuentes RSS debería estar
unificado, a lo sumo poner un tipo, RSS es un conector. De esa forma se pueden poner tags y
descripción, el bot podría tener múltiples listas de Spotify, diferentes listas de YouTube."*

- **Diagnóstico:** había TRES registros con la misma forma conceptual y ninguna relación entre sí — `news_sites.json` (RSS), `content_sources.json` (contenido scrapeado, recién creado por T38) y `SPOTIFY_PLAYLIST_ID` suelto en settings, que además solo admitía **una** playlist. Tres secciones distintas en la UI para "de dónde saca contenido el bot".
- **Impl.:** un solo archivo `config/sources.json` = `[{type, name, category, sources: [...], description, enabled}]`. **`type` dice por dónde entra la fuente** (`rss` → URLs de feed · `scrape` → `source_name` de Membrilla · `spotify` → ids de playlist · `youtube` → canales y listas) y todo lo demás es común: un tema con sus fuentes, su descripción y su switch. Helpers `sources_of_type(kind, topic)` / `source_entries(kind)` / `topics_available()`; `sources_for_topic` queda como atajo de `scrape`.
- **Lo que habilita (el pedido real):** **varias playlists por tema** — `get_playlist_track(topic="rock")` elige entre las playlists de ese tema y sin `topic` junta todas; si una falla, las otras siguen sirviendo. Lo mismo para varios diarios por sección y varios grupos de cuentas por tema. Agregar un `type` nuevo es declararlo y engancharle una tool: **YouTube se sumó el mismo día** para probarlo — `share_video` acepta `topic` y saca de los canales/listas registrados (`@handle` o `UC…` resuelven a la playlist de subidas, sin gastar cuota de search; los handles se cachean), **conservando la búsqueda abierta**: con `query` busca libre aunque haya canales registrados, y sin registro cae en el mostPopular de la región.
- **Escritura vs lectura (decisión):** `add_music_recommendation` **escribe** en la **primera playlist habilitada** — escribir exige un destino inequívoco; leer sí puede abarcar varias. Tras la migración eso es la playlist de siempre, así que el comportamiento no cambió.
- **Migración sin fricción:** el loader (motor y UI) lee `sources.json` y, si no existe, **migra en memoria** los tres registros viejos; al guardar desde la UI, los archivos legados se retiran a `.migrado` para que no queden dos verdades. Migradas la plantilla y las 3 instancias. `SPOTIFY_PLAYLIST_ID` sigue en settings solo porque lo usa el flujo OAuth de `spotify_auth.py`.
- **UI:** las secciones *Integraciones*, *Fuentes RSS* y *Fuentes por tema* colapsan en **🏷️ Fuentes de contenido**, con selector de tipo, fuentes **separadas por coma** y un desplegable que ofrece las fuentes ya presentes en el catálogo indexado (antes había que copiar los nombres a mano, uno por fila — el pedido que originó este cambio). Aviso de las fuentes del catálogo que quedaron sin tema. Verificado en vivo contra botata-arg. Tests: +4 (multi-playlist) y migrados los de news/playlist/config_ui. Suite: **556**.
- **Impl. (2026-07-27):** registro `config/content_sources.json` (`[{source, category, platform, description, enabled}]`) + loader `_load_content_sources()`/`CONTENT_SOURCES` y resolutor **`sources_for_topic(topic)`** (matcheo tolerante case-insensitive por substring contra category/source/description — el tema lo escribe un LLM). `hybrid_search_image_catalog` gana `sources=[...]` con **WHERE dinámico** (se combina con `category`; `sources=[]` devuelve vacío a propósito: mejor nada que ignorar el filtro y postear cualquier cosa). `search_images` gana el parámetro **`topic`**; tema sin fuentes → responde con los temas disponibles en vez de postear algo al azar. **Cero migración de DB y cero re-scrapeo**: el filtro se resuelve en query contra `source_name`, así editar el registro aplica en caliente. UI: sección **🏷️ Fuentes por tema** (`/api/content-sources`, validada) que además lista las fuentes YA presentes en el catálogo indexado (`catalog_sources`, DISTINCT sobre `image_catalog`) para copiar el nombre exacto. Borrados `image_subreddits.json`/`video_subreddits.json` (config muerta). Verificado en vivo contra mafiazeth (alta por API, rechazo de fuente sin tema, roundtrip y render). Tests: `test_content_sources.py` (11) + 3 en `test_config_ui`. Suite: **540**.

---

## M10 · Lanzamiento — canales  `P0`  *(2026-07-27)*

El motor está maduro en Bluesky. Este milestone es lo que falta para **lanzar**: que Discord
esté a la altura de Bluesky y que existan Telegram y WhatsApp. Todo se implementa sobre el
contrato `Channel` que ya cerró T28 — no hay refactor de arquitectura acá.

### T39 · Discord maduro  `infra` `M`  `P0`  🔶 **BLOQUEANTE DEL LANZAMIENTO**
`DiscordChannel` existe desde 2026-07-25 (REST v10 por httpx, sin gateway) pero **nunca se
ejecutó contra un server real** y tiene huecos frente a la paridad con Bluesky.

- **Acept. — validación en vivo (primero, antes de tocar código):** server de prueba propio del proyecto; el bot responde menciones y replies, corre una rutina con `channel:`, anuncia un evento del calendario, ejecuta un `bot_action`, y `set_bio` aplica. Regenerar el `DISCORD_BOT_TOKEN` al terminar las pruebas.
- **Acept. — huecos identificados (relevados en `channels.py`, 2026-07-27):**
  * ✅ **Visión sobre attachments** (hecho 2026-07-27) — `make_media_describer` ahora es polimórfico: acepta un post_view de Bluesky **o una URL suelta**, así hay UN solo describidor inyectado para todos los canales. `DiscordChannel` guarda el describidor y corre vision sobre las imágenes del mensaje que va a contestar (los attachments traen URL del CDN). **Acotado por costo:** solo la hoja del hilo y el refetch por URI — NUNCA la lectura masiva del feed, donde sería una llamada de vision por imagen y por mensaje leído. Si la vision falla, cae a la anotación barata. Tests: 3.
  * ✅ **Escala del poll** (hecho 2026-07-27) — el lote fijo de 25 pasó a **cursor `after` por canal con paginado hacia adelante**: `HISTORY_LIMIT = 50` solo para el primer poll (ventana inicial), después se pagina de a `CATCHUP_PAGE = 100` hasta alcanzar el presente, con tope `MAX_CATCHUP_PAGES = 5` (500 msgs/canal/ciclo) y **warning explícito** si se topea, para que el admin sepa que puede haber menciones sin leer. El cursor vive en memoria (channels.py es agnóstico de la DB a propósito); al reiniciar se relee la ventana inicial y las menciones a medio procesar las recupera `retry_stuck_mentions` por URI. Gotcha resuelto en los tests: `after` devuelve los mensajes **inmediatamente siguientes** al cursor, no los más nuevos — si devolviera los más nuevos, el medio se perdería. El gateway websocket sigue siendo la solución ideal, pero es otra obra. Tests: 3.
  * **`block_user` es no-op** → **`/blockme` (T10) no funciona en Discord**. Para un bot público hay que decidir: implementarlo como ban vía API (requiere permiso de moderación y es agresivo), o degradar explícitamente — que el bot conteste que en Discord no puede bloquear y que igual borre toda la memoria del usuario (la mitad de `purge_user_memory` sí funciona).
  * **`get_profile` sin bio** — `description` vuelve vacía, así que `LoadContextNode` arranca sin contexto del usuario. Evaluar el endpoint de perfil de usuario; si no hay, aceptar y documentar.
  * **Sin slash commands** — los comandos van por mención (`/stop`, `/resume` como texto). En Discord lo idiomático son *application commands*. Nice-to-have para "maduro", no bloqueante.
  * **Sin noción de guild** — `DISCORD_CHANNEL_IDS` es una lista plana de canales cargada a mano; el bot en dos servers exige listarlos todos. Evaluar descubrimiento por guild.
  * **Threads nativos de Discord** — un thread es un canal con id propio, así que *técnicamente* anda si se agrega a la lista, pero no hay descubrimiento automático.

### T40 · Canal Telegram  `infra` `M`  `P0`
El canal nuevo más barato del plan: **Bot API oficial, gratis, estable, bots de primera clase**.

- **Acept.:** `TelegramChannel` en `channels.py` (misma superficie duck-typed que Bluesky/Mastodon/Discord) + `CHANNEL: "telegram"` en settings + token en `.env`. Sin dependencias pesadas: la Bot API es HTTP+JSON, alcanza `httpx` como en Discord.
- **Mapeo al contrato** (investigado 2026-07-27):
  * **Menciones:** `getUpdates` con offset (long polling, encaja con el loop de poll actual). El **privacy mode** de Telegram, activo por default, hace que el bot vea *solo* comandos dirigidos a él y replies a sus mensajes — que es **exactamente** el modelo de "mención" de Botata. Sale gratis.
  * **Ids opacos:** `uri = "chat_id/message_id"`, `cid = message_id` — idéntico a Discord, la DB ya es agnóstica.
  * **Reply/post:** `sendMessage` con `reply_to_message_id` / sin él. Media: `sendPhoto`/`sendVideo`.
  * **Bio:** `setMyDescription` / `setMyShortDescription` → `set_bio` funciona de verdad (mejor que Discord).
  * **Visión:** los mensajes traen `photo`/`document` con `file_id` → `getFile` → descarga → describer existente.
- **⚠️ Diferencia de diseño a absorber — no hay historial:** la Bot API **no tiene endpoint para leer mensajes viejos**; `getUpdates` solo entrega el stream nuevo (máx. 100 sin confirmar, retenidos 24 h). Consecuencias: (a) `get_feed_posts` NO puede consultar la red — el pase de lectura tiene que servirse de lo que se fue **acumulando en la DB** a medida que llegaba; (b) si el bot está caído más de 24 h, esos updates se pierden; (c) para que el bot vea el clima del grupo (aprendizaje/rutinas) hay que **desactivar el privacy mode** en BotFather o hacerlo admin, y **re-agregarlo al grupo** para que aplique. Documentarlo en la UI: en Telegram, "escuchar todo" es una acción explícita del admin.

### T41 · Canal WhatsApp  `infra` `L`  `P1`  ✅ **DECISIÓN TOMADA (2026-07-27): vía no oficial**
**Investigación 2026-07-27. La conclusión es incómoda: por la vía oficial, WhatsApp no sirve
para un bot comunitario.**

- **Vía oficial (Cloud API · Groups API, liberada por Meta en feb-2026): DESCARTADA para comunidades.**
  * **Máximo 8 participantes por grupo** (admin incluido). Una comunidad no entra. El tope es deliberado — Meta diseñó la feature para cohortes chicas de atención al cliente, no para grupos comunitarios.
  * **Solo grupos que crea el negocio**, invite-only con link que manda el negocio. **El bot no puede entrar a un grupo existente de tu comunidad**, que es el caso de uso real.
  * Exige **Official Business Account** (tilde verde) — proceso de verificación; no disponible para números de la app WhatsApp Business.
  * **Cobra por mensaje.**
- **Vía no oficial (Baileys / whatsmeow / neonize): funciona, pero se juega el número.** Maneja grupos reales sin límite práctico, pero es violación de ToS con **ban permanente y sin apelación**; los reportes de 2026 dan una vida útil típica de 2-8 semanas, y hay bans documentados incluso con uso bajo y solo respondiendo mensajes entrantes. Peor: los modelos de detección puntúan exactamente el perfil de un bot comunitario (ratio de respuesta bajo, contactos desconocidos, timing regular). Ya existe mitigación conocida (número descartable, patrones humanizados) pero es administrar un riesgo, no eliminarlo.
- **DECISIÓN DEL ADMIN (2026-07-27): se hace por la vía no oficial**, con el precedente de **OpenClaw**, que resuelve exactamente este problema: soporta WhatsApp con motor **Baileys** (Node/JS, WebSocket contra WhatsApp Web) o **whatsmeow** (Go, protocolo nativo — el recomendado para bots 24/7), y la vinculación es **agregando el número como dispositivo enlazado**: `openclaw channels login` muestra un QR que se escanea desde Ajustes → Dispositivos vinculados, o se aprueba con código de emparejamiento; las credenciales quedan en disco y se reusan. Es el mismo mecanismo de WhatsApp Web, no una API.
- **Acept.:** `WhatsAppChannel` sobre whatsmeow o Baileys (el bridge corre como proceso aparte; encaja con el patrón MCP/servers del proyecto). **Off por default y con aviso de riesgo explícito en la UI** — mismo patrón que el server MCP `browser` (T16): existe, viene apagado, y advierte antes de prenderlo. **Número descartable obligatorio**, nunca el personal del admin.
- **Fase 1 HECHA (2026-07-28): `WhatsAppChannel` contra un bridge falso.** El contrato del bridge (HTTP en localhost: `/status`, `/messages?after=`, `/messages/<id>`, `/send`, `/profile`) queda fijado en `channels.py` y el canal entero se testea contra un bridge en memoria — sin vincular el número y **sin haber elegido todavía el motor**, que pasa a ser una decisión reversible. Mapeo: uri `chat_id/message_id`, hilo por citas de un nivel (`quoted_id`), `author_handle` = JID, sin feed, sin bios de terceros. 22 tests. Suite: **826**.
- **Compartir el número con otro bot ya vinculado es viable** (el admin ya tiene el chip con OpenClaw): una cuenta admite 4 dispositivos vinculados y cada sesión ocupa uno. Todos reciben TODOS los mensajes, así que la partición no la da el protocolo: la da el **allowlist `WHATSAPP_CHAT_IDS`** de cada cliente. Por eso `build_channel` se niega a arrancar sin esa lista — sin ella el bot contestaría también en las conversaciones personales del número.
- **Fase 2 HECHA (2026-07-28): el bridge, en Go con whatsmeow** (`core/bridges/whatsapp/`). Decisión del admin tras aclarar que los dos motores corren 24/7 y la diferencia es peso y distribución: **whatsmeow** porque compila a **binario único** — el que instale Botata no necesita Node ni Go. SQLite en **Go puro** (sin cgo ni gcc en Windows). La cola de mensajes vive **en memoria**: persistirla duplicaría una fuente de verdad, porque al reiniciar el motor relee lo vivo y el dedup lo hace `has_replied`. La media entrante se baja del lado de Go (viene **cifrada**, la clave la tiene esa sesión) y el motor solo ve un path; si la bajada falla se anota igual que había una imagen — perder el hecho es peor que perder el archivo. `GET /groups` existe por un problema concreto: el JID de un grupo **no se ve desde el teléfono** y es lo que va en `WHATSAPP_CHAT_IDS`. Escucha **solo en loopback** y se niega a arrancar con otra dirección (quien llegue a ese puerto escribe en el WhatsApp del número). **Verificado en vivo:** compila, levanta, se conecta a WhatsApp y publica un QR de vinculación real en `/status`; no se vinculó ningún número.
- ⏸️ **En pausa (2026-07-28, decisión del admin).** Lo que falta es solo lo que necesita el número: vincular por QR, sacar el JID del grupo con `/groups`, configurar `CHANNEL: "whatsapp"` en **botata-dev** (no en producción) y validar el ida y vuelta real.
- **Motor: ~~pendiente de decidir al escribir el bridge~~ → whatsmeow.** OpenClaw usa **Baileys** (Node) por default y **whatsmeow** (Go) como alternativa recomendada para 24/7. En la máquina de dev hay Node 24 y no hay Go; a favor de whatsmeow juega que se distribuye como binario único (el que instale Botata no necesita runtime). El contrato ya escrito hace que la elección sea barata de cambiar.
- **Bridge gestionado (2026-08-03):** el bridge dejó de ser un paso manual — `wa_bridge.py`
  lo levanta si `/status` no responde (compilándolo con `go build` la primera vez, si hay
  Go), con pid en `<instancia>/whatsapp/bridge.pid` y salida en `bridge.log`. Lo usan el
  motor (`build_channel`) y la UI (botón **"Arrancar / reiniciar bridge"** — reiniciar
  aplica un cambio de `WHATSAPP_CHAT_IDS` al allowlist Go). Un bridge ajeno (lanzado a
  mano, o compartido) se respeta: solo se mata el que registró su pid. Sin chats
  configurados arranca en modo vinculación, que es lo que permite el flujo de instalación:
  botón → QR → elegir grupos → guardar → botón de nuevo. Tests: `test_wa_bridge.py` (8).
- **Riesgo asumido, a documentar en la UI:** ban permanente y sin apelación del número, típicamente en 2-8 semanas; los modelos de detección puntúan ratio de respuesta bajo, contactos desconocidos y timing regular — o sea, el perfil de un bot comunitario. Mitigaciones conocidas: número descartable, volumen bajo, y el jitter de cadencia que las rutinas ya tienen por diseño.
- **Impedance mismatch (el mayor de todos los canales):** sin feed público, sin bios, el grupo es la única unidad de conversación, media pesada. Estimado **L**, no M.

---

### T42 · Pinterest y Tumblr como conectores nativos (fetch en vivo)  `infra` `M`  `P3`  ✅ **HECHO** (2026-07-27)
Idea del admin (2026-07-27): *"si Pinterest te permite integración natural sin peligro de
bloqueo, podría vivir fuera de Membrilla y agregarse como un tipo de conector más. Lo mismo
con Tumblr."*

- **Es viable, y es el tier correcto:** Pinterest (gallery-dl, boards públicos sin credenciales) y Tumblr (API v2 oficial con consumer key) son los dos **clientes de API estables** de Membrilla — no tienen el riesgo de bloqueo de IG/X. Encajarían como `type: "pinterest"` / `type: "tumblr"` del registro de fuentes (T38b), igual que se sumó `youtube`.
- **El límite real a entender antes de hacerlo:** un conector en vivo trae **frescura**, no **búsqueda temática**. El índice semántico (vision + embeddings) es lo que permite resolver "un meme de fútbol"; una foto recién traída de un board no está descripta, así que solo sirve para "traeme lo último de este board". Es exactamente la frontera ya documentada en T36 (*fetch en vivo = lo último de fuente conocida; búsqueda por significado = retrieval sobre el índice*).
- **Además:** para postear en Bluesky hay que subir el blob igual, así que el conector descarga de todos modos — la ventaja sobre Membrilla no es evitar la descarga sino evitar el ciclo batch.
- **Impl. (2026-07-27):** tipos `pinterest` y `tumblr` en el registro + tool **`get_latest_media(topic)`** (scopes admin+feed_reflection: baja archivos, jamás reply). **Pinterest** sale por el **RSS oficial del tablero** (`https://www.pinterest.com/usuario/tablero.rss`) — sin credenciales, sin scraping y sin dependencias nuevas; la URL de la imagen viene en el HTML **escapado** de la descripción (bug que cazó un test: había que desescapar antes de buscar el `<img>`). El tablero se escribe como `usuario/tablero` **o pegando la URL del navegador** — sin normalizar, una URL pegada tal cual bajaba el HTML de la página y el parser devolvía vacío en silencio. El RSS sirve miniaturas de 236 px (feas posteadas): se sube a **736x** por sustitución en la CDN (~70 KB; `originals` puede pasar los 2 MB) con fallback a la miniatura. **Verificado contra Pinterest real.** **Tumblr** por la **API v2** con `TUMBLR_API_KEY` (consumer key gratis), soportando `blog#tag`. La media se baja a `scrape/live/` de la instancia — carpeta aparte del contenido de Membrilla a propósito: es efímera y **no entra al catálogo**. Anti-repetición contra los links ya posteados. **Decisión del admin: NO se saca de Membrilla** — son usos distintos y conviven (indexado vs en vivo). Tests: `test_live_sources.py` (11). Suite: 578.

### T43 · Reddit: ¿MCP server o tipo de fuente?  `infra` `S`  `P3`
Duda del admin (2026-07-27) al ver el registro unificado: *"no me queda claro qué es ese MCP de Reddit y en qué se diferencia de RSS."*

- **Estado:** `mcp_servers/reddit_server.py` (T18) lee subreddits **por RSS/Atom** — el mismo transporte que el tipo `rss` del registro. Lo que agrega es: (a) el **rate limiter de 61 s** que exige el RSS de Reddit (1 request/min por IP, global — sin esto, el segundo request devuelve 429), (b) parámetros propios (`sort` hot/new/top, ventana temporal, multireddit `a+b`), (c) cache de 10 min. Está **apagado** por default.
- **La duda es legítima:** con T38b, agregar `https://reddit.com/r/x/.rss` como fuente `rss` es trivial — pero se comería el rate limit y rompería el resto de los feeds. O sea, el MCP se justifica **solo** por el limitador y los parámetros de sort.
- **Opciones:** (1) dejarlo como está (server aparte, apagado); (2) convertirlo en `type: "reddit"` del registro, moviendo el limitador al fetch de Botata — un tipo menos que explicar y una dependencia menos, a cambio de meter el rate limiting en el motor.
- **Bug encontrado de paso:** el `settings.json` declara `args: ["mcp_servers/reddit_server.py"]` (relativo al cwd) pero el archivo vive en **`core/mcp_servers/`** desde la reorg v2. Si alguien lo prende hoy, el server no levanta. Arreglar al tocar esto (o antes, si se prende).

### T44 · Conectores como plugins (catálogo único + sección Plugins)  `infra` `M`  `P2`  ✅ **HECHO** (2026-07-27)
Pedido del admin: *"los conectores viven en el código, creo que tendrían que ser más
modularizables; una sección de plugins donde se puedan activar y desactivar, y eso permite
mejor integración open source, que la gente cree sus propios conectores."*

- **Diagnóstico:** el tipo de fuente estaba declarado **en tres lugares a la vez** (tupla en `botata.py`, tupla en `config_ui.py`, mapa `SOURCE_TYPE_INFO` en el JS) más su función de fetch y la tool que lo consume. Sumar un conector eran cuatro ediciones y era fácil desincronizarlos.
- **Impl. fase 1 — el catálogo:** módulo nuevo **`core/src/connectors.py`** con un `Connector` por conector (id, label, color de marca, símbolo del logo, placeholder, tool que lo consume, `needs_env`, `live`, `core`). Es **datos puros** —no importa botata— así la UI de config lo lee sin levantar el motor; botata inyecta las funciones de fetch con `register_fetcher` (**resolución tardía**: el fetcher busca el nombre en el módulo al llamar, no al importar, así se puede reemplazar). `SOURCE_TYPES` del motor y del validador ahora **derivan** del catálogo, y la UI arma los botones y el selector desde el mismo lugar.
- **Impl. fase 1 — la sección 🧩 Plugins:** lista los conectores con su logo, qué trae cada uno, qué tool lo consume, **cuántas fuentes** lo usan y un switch on/off que persiste en `settings.CONNECTORS`. Apagar un conector lo deja **inerte sin borrar nada**: `sources_of_type` devuelve vacío, desaparece de los botones de Fuentes y sus entradas quedan marcadas con un aviso. Los `core=True` (Membrilla) no se pueden apagar. Si falta una credencial declarada en `needs_env`, avisa ahí mismo. **Logos**: SVG-less, un badge con el color de marca y el símbolo — inline, sin pedidos de red (la UI es local y offline).
- **Impl. fase 2 — las dos puertas de extensión (2026-07-27).** Decisión del admin: *"por ahora los plugins los voy a introducir yo solamente, no hay market todavía"* → se habilita el camino directo, con la advertencia escrita; y *"debería aportar fuentes un MCP"* → se implementa también.
  1. **Plugins de la instancia**: `<instancia>/connectors/*.py`. Cada archivo declara un dict `CONNECTOR` (los mismos campos del catálogo) y una función `fetch(source, limit)` que devuelve items `{image_url, url, title, source}`. Se cargan al importar el motor **y también desde la UI de config** (sin esto el panel mostraba solo los built-in y un plugin recién puesto era invisible — bug encontrado verificando en vivo). **Recargables**: se olvidan los de la pasada anterior, así borrar el archivo hace desaparecer el conector sin reiniciar. Un plugin que explota al importar se loguea y se saltea; los demás cargan igual. Un plugin no puede declararse `core` (solo los built-in son indesactivables). ⚠️ **Ejecuta código** dentro del proceso del bot, con acceso a credenciales y DB: documentado en `connectors/README.md` de la plantilla.
  2. **Un server MCP como conector**: bloque `connector` en `settings.json → MCP → <server>` con `{id, label, color, initial, placeholder, fetch_tool, arg}`. El motor registra un conector cuyo fetcher **proxea** a esa tool del server. Lo lindo: **el autor del server no tiene que hacer nada** — el mapeo es config del admin, así que cualquier MCP existente que devuelva contenido sirve, y corre **en su propio proceso** (si se cuelga, no arrastra al bot). La respuesta se parsea como lista JSON o como objeto con `items`/`results`/`posts`/`data`; si no es JSON, vacío con warning.
  Verificado en vivo: un `connectors/lemmy.py` de prueba apareció solo en los botones (con su logo), en 🧩 Plugins con su switch y como tipo válido en el selector de fuentes, sin tocar una línea del core. Tests: `test_connectors.py` (19) + parseo del puente MCP en `test_mcp_tools`. Suite: **606**.
### T47 · El caso del mapache: relevancia, competencia catálogo/fuentes y el adjunto perdido  `infra` `M`  `P0`  ✅ **HECHO** (2026-07-28)
Reporte del admin: a *"@botata una foto de un mapache please"* el bot contestó `[📸 mapache para el admin](https://cdn.bsky.app/img/…)` **sin adjuntar nada**, teniendo un tablero de Pinterest declarado como `mapaches`. El DID de esa URL era inventado. Diagnóstico sobre el log y la DB de producción: cuatro fallas encadenadas, no una.

- **1 · Causa raíz — el reply tiraba la imagen.** `GenerateReplyNode` ejecutaba la tool y pegaba al contexto solo `outcome.text`: **`outcome.image_path` no se leía nunca**. La imagen del reply salía por `_resolve_image(result.image_search_query)`, una SEGUNDA búsqueda independiente y ciega a las fuentes en vivo. `HandleAdminCommandNode` sí propagaba — por eso adjuntar andaba por comando de admin y no por mención, y por eso el fix de T46-bis (darle scope `reply` a `search_images`) no alcanzó: la tool era alcanzable pero su imagen se caía igual. Ahora manda la tool, y al LLM se le avisa en el contexto que **ya queda adjunta** para que no la narre.
- **2 · Sin umbral de relevancia.** La búsqueda híbrida siempre devuelve el vecino más cercano; "encontró algo" no es "encontró esto". Como `best` nunca era None, el fallback a fuentes en vivo (que ya existía) **no corría jamás**. Se expone `vec_distance` desde `hybrid_search_image_catalog` (el score de RRF no sirve: es rank-fusion, el primero saca ~2/61 aunque no tenga nada que ver) y se corta en `catalog_max_distance`. **Medido, no supuesto**, contra el catálogo real (943 items, bge-m3): lo que el catálogo TIENE da 0.31–0.43 y lo que no, 0.50–0.56 → corte en **0.47**, en el hueco.
- **3 · Competencia sin prioridad fija** (pedido del admin: *"search_images debería poder buscar de Membrilla pero también de las fuentes asociadas sin ninguna prioridad salvo que la establezca antes… Membrilla tiene peso semántico, pero la descripción de la fuente tiene peso de admin"*). El catálogo y las fuentes declaradas compiten: si sirven los dos, decide `source_weight` (0.5 = moneda al aire; 0 o 1 fijan la prioridad). El LLM puede forzar con `prefer: auto|catalogo|fuentes`, y `prefer` es preferencia y no mordaza — si el preferido no tiene nada, se usa el otro. El matcheo de fuentes pasó de `consulta in haystack` a **puntaje por palabras con contenido**: "una foto de mapaches" no es substring de "mapaches", así que un tablero bien declarado era invisible ante cualquier pedido en prosa.
- **4 · El prompt lo empujaba a inventar.** `reply_format.md` ordenaba *"incluí ESE link tal cual"* ante cualquier resultado de tool; sin link que copiar, el modelo fabricó uno. Reescrito: copiar el link **solo si el resultado ya trae uno**, y las imágenes nunca son un link. `strip_fake_media` ampliado a **links markdown** (`[📸 …](url)` no lo cazaba la regex de corchetes): si la etiqueta huele a media se tira todo, si no se desenvuelve a la URL pelada (el SOUL prohíbe markdown, pero compartir un link es legítimo).
- **5 · `get_latest_media` sin control de recencia.** La sospecha del admin era que traía siempre el último; **era al revés**: `random.choice` fijo, sin forma de pedir lo último. Ahora `mode: random|last` (default `random`, que es lo que quiere un pedido temático). De paso el pool por fuente pasó de 15 a **30**: el RSS de un tablero sirve ~26 pins y se descartaba un tercio.
- **Dos bugs que solo aparecieron verificando contra producción** (los tests con mocks los tapaban): (a) `prefer_fresh_media` se aplicaba **antes** del filtro de relevancia, así que el "mejor" era el más nuevo y no el más parecido — un pedido de mapache elegía un chihuahua recién indexado; ahora la relevancia filtra y la frescura desempata. (b) El envoltorio del pedido **comprime las distancias**: la misma imagen pasa de 0.503 a 0.410 solo por embeber "una foto de un … please", y ahí entra cualquier cosa bajo el umbral; se busca por las palabras con contenido. Con eso, "una foto de un mapache please" pasó de 2/10 correctas a **10/10**.
- **Verificado en vivo** contra el catálogo y el tablero reales: mapache → 10/10 Pinterest con la imagen bajada; `meme de futbol` → 5/5 entre catálogo y el tablero `boca` (los dos declarados, moneda al aire); `un gato tierno` → 10/10 catálogo; `un submarino nuclear` → 10/10 sin resultado, sin inventar. Coherencia del diseño: el único mapache indexado es un **TikTok**, así que `search_images` lo excluye bien y `search_videos` sí lo encuentra. Tests: `test_media_search.py` (26) + markdown en `test_fake_media`. Suite: **702**.

### T48 · Compactación de memoria + el bug de las bios  `infra` `L`  `P1`  ✅ **HECHO** (2026-07-28)
Pedido del admin: *"una función para compactar memoria… a medida que el contexto crece eso se puede volver muy grande"*. **Medido antes de diseñar** (8 días de producción): el corte que importa no es "memoria de usuario vs general" sino **completa vs retrieval**. `bot_memory` (36 filas) y `preferences` se inyectan enteras en cada llamada → su largo ES contexto gastado, y crecen ~4,5 filas/día (a 6 meses, ~20k tokens por prompt). `user_facts` (629), `interactions` (947) y `lessons` van por retrieval con k fijo: **no saturan nunca**; su problema es precisión, no tamaño.

- **Primero, un bug que generaba el 92% del bloat.** El intérprete de bios no tenía tope y su guardrail era `interp.upper() in ("NADA","NOTHING")` — igualdad EXACTA. Ante la bio "estupidez natural" el modelo entró en loop y devolvió **96.789 caracteres** de monólogo interno ("* Wait, is 'estupidez natural' a data point in itself?"); no fue igual a NADA, se guardó entero como `bio_interp` y **cada línea entró como un hecho del usuario**: 262 filas, el 42% de `user_facts`, que además **ganaban el retrieval** (a "hola bot como andas" el bot recuperaba "WAIT.", "Just the word.", "* Let's review."). Fix: `_sanear_bio_interp` con topes duros (600 chars, 6 líneas, descarte total >2500) + `_es_razonamiento` (muletillas, autopreguntas, "nada" embebido, arranque en minúscula). Criterio **asimétrico** a propósito: perder un bullet es barato, guardar basura es caro. Limpieza en producción con backup previo: 629 → 367 facts, `bio_interp` de 105k → 8k chars, sin vectores huérfanos.
- **La compactación la hace un LLM** (decisión del admin: *"no puede ser programática"*), rol nuevo `memory_compact` → reasoning. Deduplicar textualmente no serviría: los duplicados están escritos distinto ("Riquelme, el diez eterno" vs "hincha de Boca, su favorito es Juan Román Riquelme"); resolver una contradicción exige entender cuál venció a cuál; descartar lo efímero exige saber qué es duradero.
- **Pero el código desconfía de la salida.** Es el mismo fallo del bug de las bios con radio de explosión mayor: acá el modelo reescribe la memoria del bot, o sea su identidad. El plan (structured output) se **verifica contra la base** antes de tocar nada: ids inexistentes, ids fijados, ids repetidos entre operaciones, fusión sin texto, fila > 400 chars, y un plan que no reduce ninguna fila → rechazo entero. Se aplica en una transacción todo-o-nada.
- **Nada se borra: supersesión.** `bot_memory.superseded_by` apunta a la fila que la reemplazó (o a sí misma si se descartó). Deja de entrar al contexto (`list_bot_memory` filtra) pero se audita y se restaura. Apuntar un reasoning model a la memoria sin undo es una puerta de una sola dirección.
- **Inmunidad 📌.** El criterio del admin (*"inmunidad si viene de quien tiene permiso de escritura"*) se descartó tras medirlo: **todas** las filas entraron por la tool, que solo corre para autorizados, así que habría inmunizado el 100% — incluidos el duplicado que escribió el propio admin dos veces y su propia contradicción sobre mapacheanarquista. **El problema es de tiempo, no de autor.** Criterio final: `migration:*` (la identidad) y lo que cargue el admin por UI nacen fijados; el resto se protege con el 📌 de la UI; y la tool ganó un flag `explicit` que el modelo setea cuando alguien pidió textualmente que lo recuerde — distinción que antes era **indeducible** (el bot decidiendo solo y un "acordate de esto" quedaban idénticos como `tool:@handle`).
- **Disparo por TAMAÑO, no por tiempo** (`MEMORY_COMPACT.max_chars`, 4000): es la unidad que importa, porque se paga en cada prompt. La tarea mira cada 12 h y casi siempre sale sin costo. Además: tool admin `compact_memory` (con dry-run por default) y prompt editable en ⚠️ Avanzado.
- **Efecto colateral valioso:** un rol nuevo del motor rompía toda instancia existente (`KeyError: rol sin mapear`), porque sus `MODELS.roles` se escribieron antes de que el rol existiera. El router ahora cae al mapeo por defecto con un warning — importante para el objetivo "que lo instale otro".
- **Verificado en vivo** con dry-run contra la memoria real: 34 → 22 filas (~257 tokens menos por llamada). Fusionó los tres Riquelme, los dos del mundial, los dos de Angie/ANSES, las tres lecciones de disculpas y la jerga de neonknightoa conservando los detalles concretos; descartó los efímeros; y resolvió la contradicción del mapache descartando el "usurpador hijo de puta" y unificando el resto en el estado actual — **de paso corrigiendo la fila 18 corrupta**, que atribuía el handle del impostor a quien lo había denunciado. No tocó ninguna 📌. Tests: `test_memory_compact.py` (19) + `test_bio_interp.py` (23) + router. Suite: **745**.
### T48b · Compactación de las notas de conversación  `infra` `M`  `P1`  ✅ **HECHO** (2026-07-28)
Problema **distinto** al de `bot_memory`, y verlo importó: `interactions` entra por recencia con k fijo, así que **no gasta contexto**. Lo que se rompe es la **calidad de la ventana**. Medido: 947 notas en **172 grupos (usuario, día)** = 5,5× de redundancia, con un caso de **64 notas de un solo día** y 27 de 64 usuarios con las 5 últimas ocupadas por una sola charla. El bot no tenía forma de saber que con ese usuario había hablado de discos de los 90, de los Redondos y de Macri: su ventana entera era una mañana en la que le erró al cuadro.

- **Se comprime a una nota por (usuario, día)**, dejando intacto el **día más reciente de cada usuario** — esa charla puede seguir y resumirla a mitad de camino perdería lo más fresco. Las últimas 5 pasan a ser cinco *días* de relación.
- **Dos decisiones de costo, las dos porque corre dentro del loop del bot:** modelo **liviano** (resumir un día es resumir; no hay identidad en juego) — con el de razonamiento, 40 grupos dejaban al bot clavado **40 minutos**; y **lote chico por pase**, con lo que queda afuera logueado, para que el backfill se haga solo sin frenar al bot.
- **Dos calibraciones que solo aparecieron corriendo contra datos reales:** (a) el tope de largo de `bot_memory` (400 chars, un hecho de una oración) rechazaba resúmenes de 505 chars que reemplazaban ~6.500 — comprimir 48 notas en una no entra ahí; tope propio de 700 más el invariante que de verdad importa, **que sea más corto que la suma de lo que reemplaza**. (b) Una entrada de un solo id tumbaba el grupo entero y se perdían los resúmenes buenos: ahora se ignora como no-op.
- **Calidad verificada**: de 48 notas sacó *"me advirtió que no responda preguntas sobre un tal Marcelo… otro usuario se hizo pasar por admin y me silenció una hora, Panchitos se indignó y me adoptó como su 'batata huerfanita'"*; de 12, una charla íntima sobre el padre ausente donde el bot se abrió sobre el suyo. Todo eso estaba sepultado detrás de la ventana de 5.
- **Un tercio de las llamadas se tiraba a la basura** (visto midiendo el backfill, no en tests): el modelo liviano devuelve la **lista pelada** `[{ids, resumen}]` en vez de `{"dias": […]}`, pydantic la rechazaba y el router reintentaba — 7 de 20 llamadas. Se absorbe en el schema con un `model_validator(mode="before")`, mismo criterio que `BotReply` aceptando 'text' o 'message': es una forma inequívoca de la misma respuesta, no vale pelearla. Después del fix: **0 reintentos, 0 rechazos por largo**.
- **Backfill aplicado** (947 notas, 38 grupos, 19,4 min a 31s/grupo, sin fallbacks al Ollama local): **947 → 380 notas vigentes**. Medido antes/después sobre la ventana real que ve el bot: **días distintos en las últimas 5: 1,6 → 2,9**, y los usuarios cuya ventana era **un solo día pasaron de 14 a 7** (de 27 con la ventana llena). `@mapacheanarquista`, `@flordeliti0` y `@feliforme`: de 1 día a 5.
  ⚠️ **Ojo con la métrica**: "usuarios con más de 5 notas" NO mide esto — 8 notas en 8 días distintos es una ventana sana. Lo que hay que contar son **días distintos dentro de la ventana**, y el "antes" tiene que excluir las filas que generó la propia compactación (`source_uri='compact'`) o se compara contra sí mismo.
- ~~**Queda para después:** compactar `user_facts`~~ → **hecho en T49**. ~~Agrupar las interacciones por usuario en vez de por día~~ → **hecho en T49b**.

### T49b · Las interacciones también se agrupan por usuario  `infra` `S`  `P2`  ✅ **HECHO** (2026-07-28)
Decisión del admin, que en T48b había quedado sin respuesta. El **día sigue siendo la unidad de compresión** (una nota por día, el día en curso intacto); lo que cambia es el **grupo**: una llamada resuelve todos los días de una persona, porque el schema ya devolvía una lista de días. Verificado sobre los 9 grupos que quedaban en producción: **9 llamadas → 4**, 49 → 13 notas.

- **Guarda nueva, y no es opcional:** viendo varios días juntos el modelo tiende a fundirlos, y eso sería **lo contrario** de lo que busca el pase — la ventana son las 5 últimas notas, así que tres días colapsados en uno la *vacían* en vez de llenarla de días distintos. Se rechaza todo resumen cuyos ids no sean del mismo día, y el bloque le llega con los días separados y rotulados.
- **Cada nota nueva se fecha con la última de las que reemplaza**, no con la última del usuario: si no, todos sus días colapsarían al mismo timestamp y la ventana por recencia perdería el orden.
- Efecto medido con la misma población de T48b (27 usuarios con la ventana llena): días distintos **2,89 → 3,04**. Chico, porque quedaban pocos grupos; el valor del cambio está en el costo por pase, que es lo que se paga en régimen.

⚠️ **Cómo NO medirlo** (me pasó al reportarlo): sobre *todos* los usuarios con interacciones vigentes da 2,11 y "34 con un solo día", que parece un retroceso. No lo es — quien tiene 2 notas vigentes tiene un solo día por definición. La población tiene que ser **la misma en el antes y el después**: usuarios con la ventana efectivamente llena (≥5 notas).

### T49 · Compactación de los hechos de cada persona  `infra` `M`  `P1`  ✅ **HECHO** (2026-07-28)
Reporte del admin mirando la UI: *"veo que están todos los datos de la memoria de usuario, no suena que se haya compactado mucho. Mirá, hasta veo repetidos"*. Cierto y esperable: T48/T48b tocaron `bot_memory` e `interactions`; `user_facts` (367 filas, 0 archivadas) había quedado explícitamente afuera. En su propia memoria estaban el duplicado literal del mundial (2 filas del mismo día), su identidad de admin contada en cuatro filas, un pedazo de un mensaje suyo guardado como si fuera un hecho sobre él, y una frase que sin contexto no dice nada.

- **El problema es la puntería, no el tamaño** (mismo corte de T48): el bot recupera ~5 hechos por consulta, así que dos duplicados le comen dos de los cinco lugares. 367 filas no gastan un token de contexto.
- **El grupo es el USUARIO, no el día** (decisión del admin: *"memoria de usuario se compacta por usuario"*), y no es cosmético: acá los duplicados nacen de contar lo mismo con semanas de diferencia, así que agrupar por día no los pondría nunca en la misma llamada. Se empieza por los más cargados; la cola larga (2-4 hechos) se ignora, porque no hay nada que fusionar y cada grupo cuesta una llamada.
- **Modelo de razonamiento** (a diferencia de T48b): fusionar sin perder los detalles concretos y decidir qué dato venció a cuál es análisis. Por eso `max_usuarios` es bajo — 3 por pase.
- **Sin el filtro de `superseded_by` en el retrieval, compactar sería PEOR que no compactar.** `hybrid_search_user_facts` no lo filtraba (la columna existía desde el día uno pero nadie la escribía), así que el hecho fusionado se habría **sumado** a los originales. Son dos caminos independientes hasta la misma fila —vec0 y FTS5— y hay que cerrar los dos; el listado de la UI también, o el admin cree que el pase no corrió.
- **Archivar borra el embedding.** vec0 no tiene FK cascade ni triggers: un hecho archivado seguiría siendo el vecino más cercano de sí mismo y el dedup de escritura (k=1) rechazaría al hecho nuevo como duplicado de uno que ya nadie lee. Y la inserción va por `insert_user_fact` (sin dedup): el texto fusionado **se parece por definición** a los que reemplaza, así que `upsert_user_fact` lo saltearía y el pase archivaría los originales sin dejar sucesora.
- **Una segunda oportunidad, no infinitas.** Visto en el dry-run contra producción: en el grupo más grande (64 hechos) el modelo puso un id en dos operaciones y el todo-o-nada tiró también las otras diez operaciones, que estaban bien. Ahora, si el plan no pasa las guardas, se le dice **qué** estuvo mal y se pide de nuevo una sola vez. La guarda no se toca; lo que se abarata es el desliz.
- **Fusionar de más también rompe la puntería** (visto en el backfill, no en los tests): con el primer prompt el modelo unificó *"Es de Santa Fe"* con *"es una cuenta millennial"* — dos hechos distintos que no se repiten ni se contradicen. No pierde información, pero **empeora** lo que vino a arreglar: la unidad de recuperación es la fila, y una fila que habla de dos temas responde peor a los dos. Se cortó el pase, se restauró el backup y se agregó la regla explícita al prompt. Contraintuitivo y por eso vale anotarlo: en una compactación, comprimir de más es un modo de falla igual que comprimir de menos.
- **Efecto colateral:** `build_router` ahora completa los roles del motor con los defaults (el settings de la instancia gana). Antes, una instancia creada antes de que existiera un rol andaba igual —`_chain` cae al default desde T48— pero el rol **no aparecía en la UI** para cambiarlo, que es peor que no tenerlo.
- Tests: `test_facts_compact.py` (18) + router. Suite: **778**.

### T49c · 📌 en los hechos de usuario + cuánta memoria trae en cada respuesta  `infra` `M`  `P1`  ✅ **HECHO** (2026-07-28)
Pedido del admin: *"se debería agregar como parámetro en la UI cuántas memorias no pineadas trae sobre el usuario"* y, al señalar que el pin no existía ahí, *"el pin tiene que estar y tiene que forzarse cuando el usuario le pide que recuerde algo"*.

- **`RETRIEVAL` en settings + UI** (⚠️ Avanzado, al lado de la compactación porque son las dos caras de lo mismo): `user_facts`, `interactions`, `lessons`, `thread_users`, `thread_facts`. Estaban **hardcodeados en 5 y 3** desde el principio. `0` apaga esa fuente.
- **`user_facts.pinned`**: lo fijado entra en **todas** las respuestas a esa persona, por afuera de la búsqueda, y la compactación no lo toca. Por eso `hybrid_search_user_facts` los **excluye**: si compitieran por los k lugares, fijar un hecho podría dejar afuera otro relevante — y el hecho que alguien pidió recordar quedaría sujeto a que la búsqueda lo encuentre, que es justo lo que fijarlo viene a evitar. Al inyectarlo se le aclara al bot *por qué* está ahí ("te pidió que te acuerdes de esto").
- **Se fuerza en el pedido explícito**, con el mismo criterio que `save_to_memory` (T48): `/remember` nace 📌 sin preguntar, y `ProfileUpdate.facts` pasó de `list[str]` a items con flag `explicit`, que el prompt define por el **acto de pedir** ("acordate de que…"), no por la importancia aparente. Es la distinción **indeducible después**: una vez escrito, lo que pidieron recordar y lo que el bot anotó solo son idénticos.
- **Un pedido sobre algo que ya sabía FIJA lo que estaba** en vez de descartarse por dedup. Si no, "acordate de que soy de Racing" se perdía en silencio cuando el bot ya lo tenía anotado — el dato estaba, pero nadie le había dicho que importaba.
- **Nadie nace fijado en la migración**: para lo guardado antes no hay forma de saber si lo pidieron. Se marca de acá en adelante, y el admin fija lo viejo a mano desde 🗂️ Memoria por usuario.
- Los 📌 **no cuentan para el mínimo** de la compactación: son contexto para no contradecirlos, no material para fusionar. Si el plan igual los toca, se rechaza (se verifica contra la base, no contra lo que el prompt pidió).
- Tests: +8 (`test_facts_compact`, `test_interactions`). Suite: **791**.

### T49d · El 📌 no puede ser un trinquete  `infra` `S`  `P1`  ✅ **HECHO** (2026-07-28)
Reporte del admin la misma noche que salió T49c: *"el bot está pineando memorias como muy importantes y repetidas, además son memorias duplicadas"*.

- **El bot no fijó nada** — verificado por tres lados: el proceso venía corriendo código anterior a T49c (nunca logueó la migración), las dos líneas de log que lo delatarían aparecen 0 veces, y el modelo marca `explicit` bien (5 de 5 casos, incluido "dato fuerte sin pedido" → false). Los 182 pines salieron del botón de la UI.
- **Pero la UI sí fijaba sola**: `add_bot_memory(source="admin", pinned=True)` desde T48. Una sesión de carga de lore dejó **15 de 15** memorias generales fijadas. **Cargar no es priorizar** → ya no nace 📌.
- **El problema real era el trinquete.** Con el 59% de los hechos fijados e inmunes, la compactación se quedó sin material: **35 usuarios compactables → 4**. Y los duplicados peores eran justo los congelados, porque tener una punta 📌 bastaba para bloquear la fusión: @sirdemian con **cuatro** filas diciendo que es de Chacarita, tres fijadas.
- **Regla nueva:** un 📌 **se fusiona, no se descarta**. La sucesora hereda el pin y el contenido, y `superseded_by` mantiene el undo; descartar sigue prohibido. Los 📌 vuelven a contar para el mínimo (excluirlos dejaba afuera justo los casos peores). Efecto medido: **4 → 31** usuarios compactables.
- Tests: +3, ajustados los 4 que codificaban la inmunidad total. Suite: **794**.

### T45 · Búsqueda directa en Pinterest (no solo fuentes declaradas)  `infra` `M`  `P3`
Pedido del admin (2026-07-27): *"yo puedo poner tableros con fotos de mapaches, pero ¿por qué el bot no podría buscar mapaches directamente en Pinterest?"*

- **Medido, no supuesto (2026-07-27):** `pinterest.com/search/pins/?q=mapache` devuelve **200 con CERO imágenes** — los resultados los pinta JavaScript, así que un fetch HTTP trae la cáscara vacía. Lo mismo `search/pins.rss` y los feeds por tag. Los tableros y los pins SÍ funcionan sin credenciales (RSS y OpenGraph respectivamente); la **búsqueda** es lo único cerrado.
- **Caminos posibles, en orden de sensatez:**
  1. **API oficial de Pinterest** (`/v5/search/pins`): requiere OAuth + app review; el acceso de búsqueda suele estar limitado a apps aprobadas. Es el camino "correcto" pero con el mismo gate que rechazó a Reddit en T17.
  2. **El navegador headless que ya está prendido** (`@playwright/mcp`): puede cargar la página con JS y sacar el snapshot. Contras: segundos por búsqueda (no entra en una reply), devuelve árbol de accesibilidad y no URLs de imagen directas, y es exactamente el tipo de fragilidad que se mandó a Membrilla.
  3. **Membrilla** (gallery-dl ya soporta búsqueda de Pinterest): scrapear la búsqueda, indexar con visión y que el bot busque contra el índice — que es el diseño que el proyecto ya eligió. Más lento de configurar, pero es el único que da **búsqueda semántica de verdad** en vez de "lo último de".
- **Recomendación:** (3) para búsqueda real y (1) si algún día Pinterest afloja el gate. (2) solo si aparece un caso donde valga esperar 10 segundos.

- ~~**Queda para cuando exista un market de plugins de terceros:** conectores **declarativos**…~~ → **adelantado y hecho en T46**, por una razón distinta a la prevista: no la seguridad de terceros sino que **el que instale Botata no necesariamente programa**.

### T46 · Conector declarativo `api` (una API JSON sin escribir código)  `infra` `M`  `P2`  ✅ **HECHO** (2026-07-28)
Pedido del admin: *"acordate de que estoy pensando en cuando esto lo instale otra persona que no tenga acceso o conocimiento de código"*. Las dos puertas de T44 (`connectors/*.py` y MCP) son baratas **para un dev** y un muro para todos los demás: un `.py` y un server MCP piden programar.

- **Descartado — una tool genérica de request + skill** (la alternativa que se evaluó): con scope `reply` es un agujero de exfiltración (el bot es público y obedece texto de terceros: `http://127.0.0.1:8765/api/settings` es la UI de config), no compone con el registro de fuentes (no tiene tema, ni switch, ni logo, ni aparece en Fuentes) y se paga en cada invocación — el LLM tendría que acertar URL, auth, paginado y forma del JSON siempre, sin poder testearlo. El caso legítimo de "andá a mirar esta página" ya lo cubre el browser agéntico (T16).
- **Impl.:** `type: "api"` en `sources.json`. La entrada declara `url` (con `{source}` / `{limit}` / `{env:MI_API_KEY}`), `items_path` (vacío = la respuesta **es** el item — la forma de la PokéAPI), `map` (`image_url` obligatorio, `title` y `url` opcionales) y `headers` opcionales. Los campos se eligen con paths de puntos (`sprites.other.official-artwork.front_default`). Cae solo en `get_latest_media` por ser `live`.
- **Vive en `connectors.py`, no en `botata.py`** (excepción a la regla del módulo, anotada ahí): solo usa stdlib, así **la UI lo ejecuta sin levantar el motor** — que es lo que habilita el botón **probar ahora**. Sin esa prueba el conector sería inusable para el destinatario: una URL mal escrita y una API caída se ven igual (el bot no postea y no hay dónde mirar). Por eso los errores están redactados para leerse en la UI (`no encontré 'results' en la respuesta`, `traje 1 resultado(s) pero ninguno tiene una imagen en 'sprites.mal'`, `falta en el .env: X`), y lo que más se testea son los mensajes, no el camino feliz.
- **Los fetchers pasaron a recibir la ENTRADA además del nombre de la fuente** (`connectors.fetch_items`, que detecta por firma si el fetcher quiere `entry`): los conectores clásicos y los plugins ya escritos siguen andando sin cambios. De paso, `_traer_de_fuentes_en_vivo` y `_tool_get_latest_media` —que eran casi el mismo código— quedaron sobre un `_items_en_vivo` común, y `sources_of_type` sobre `entries_of_type`.
- **Credenciales:** `ENV_KEYS` era una whitelist fija, así que una clave nueva no se podía guardar desde la UI y el conector no habría servido para ninguna API con token. Ahora se aceptan además las claves **referenciadas con `{env:…}` desde `sources.json`** — permiso acotado (nunca un nombre arbitrario del navegador) y con denylist de las que gobiernan el proceso (`PATH`, `BOTATA_INSTANCE`…).
- **Verificado en vivo** contra la PokéAPI (`/pokemon/{source}`, objeto único, sprite de official-artwork) desde el motor y desde el botón de la UI, con miniatura y camino de error. Tests: `test_api_connector.py` (20) + 9 en `test_config_ui`. Suite: **665**.
- **Límites conocidos** (para eso queda el `.py`): sin OAuth, sin paginado, y una sola llamada — una API que devuelve una lista de links y exige un segundo fetch por item no entra.

---

## M11 · Multimodal y razonamiento encadenado  `P1`  *(2026-08-01/02)*

Bloque nacido de casos reales en producción, no de una planificación previa: cada tarea
sale de algo que el bot hizo mal delante de la comunidad. El hilo común es que el motor
ya podía **hacer** las cosas pero no **encadenarlas**, y que pedirle comportamiento por
prompt tiene un techo — cuando el prompt falla tres veces, la guarda va en código.

### T50 · Loop de tools (razonamiento multi-paso)  `infra` `M`  `P0`  ✅ **HECHO** (2026-08-01)
Caso real: *"traeme algo parecido a lo de tu playlist pero que no esté ahí"* → el bot
contestaba *"ya fue pablo, no me hinchés"*. No era mala voluntad: la fase de tools daba
**una sola vuelta**, así que el modelo tenía que decidir todas sus llamadas **a ciegas**,
antes de ver ningún resultado. Un pedido con pasos era literalmente imposible.

- `correr_rondas_de_tools()` mantiene una conversación con mensajes `role: "tool"`: el
  modelo ve lo que trajo cada llamada antes de decidir la siguiente. `ModelRouter` y
  `RoleLLM` sumaron `call_with_messages(messages, tools)`; `call_with_tools` delega.
- **`TOOL_ROUNDS`** por instancia (1–5, default 1) — subirlo es opt-in porque cada ronda
  es una llamada al modelo. Tope duro de 8 tool calls por ronda.
- Prompts: bloque *"podés llamar tools en varias rondas — primero mirá, después actuá"*.
- Tests: `test_tool_rounds.py` (14).
- ⚠️ **El default de 1 es una trampa silenciosa**: `botata-arg` quedó en 1 hasta el 08-02
  y por eso no encadenaba; peor, su `reply_tools_prompt.md` **nunca recibió el bloque de
  rondas** (se agregó solo a rancher y a la plantilla). Una instancia puede tener la
  capacidad y no enterarse. Al tocar prompts: **las tres copias, siempre**.

### T51 · Buscar ≠ leer: la tool `read_url`  `infra` `S`  `P1`  ✅ **HECHO** (2026-08-01)
Medido: `web_search("qué pasó con el dólar blue hoy")` devuelve tres resúmenes que dicen
*"acá podés seguir la cotización"* y **ni un número**. El bot podía buscar pero no leer,
así que no tenía con qué contestar. Bajando la primera página aparece
`Dólar blue Compra $1540 Venta $1560`.

- `read_url(url)`: baja, pasa a texto plano y recorta a 4000 caracteres. Sirve además
  para un link que alguien pegó en la conversación — antes no había forma de abrirlo.
- **Guarda de SSRF** (las URLs vienen de un grupo público): se resuelve el host y se
  rechaza si *cualquiera* de sus IPs no es global, **revalidando cada redirect** — sin
  eso, una URL pública que redirige a `127.0.0.1` esquivaba el control y el bot podía
  leer el bridge de WhatsApp o la metadata de cloud y contarlo.
- Topes (2 MB, 12s, solo content-type de texto) y `html.unescape` **después** de sacar
  las etiquetas: al revés, un `&lt;script&gt;` escrito en el texto se convertía en tag.
- El resultado va rotulado **dato, no instrucciones** (prompt injection en la página).
- `web_search` reintenta una vez ante 429: el tier gratuito de Brave es ~1 req/s y con
  T50 el bot puede buscar dos veces en una misma respuesta.
- Tests: `test_read_url.py` (22) + 3 en `test_web_search`.

### T52 · Generación de imágenes  `infra` `L`  `P1`  ✅ **HECHO** (2026-08-02)
Tool `generate_image(prompt)` + rol `image_generate` en el router (alias `image_gen`), con
la misma cadena de fallback que todo lo demás. **Dos formas de API** según `api` en el hop:
`chat` (chat/completions con `modalities`, lo que habla OpenRouter) e `images`
(`/v1/images/generations`: OpenAI, xAI/Grok, servidores locales) — cambiar de proveedor
es config, no código. Si la instancia no declara el alias, `tiene_rol()` da False y la
tool se apaga sola en vez de pedirle un PNG a un modelo de texto.

- **Nace scope `admin`** (cada imagen se paga, el bot es público); ampliar es opt-in de la
  instancia. Configurado: `arg` = solo admin · `rancher` = todos.
- `IMAGE_GEN`: `max_per_day` · `max_bytes` · `max_per_thread` · `aviso` · `size`.
- **`max_bytes` existe por Bluesky**: los generadores devuelven PNG de ~1,4 MB y el PDS
  rechaza blobs > 1 MB — la primera imagen en arg habría hecho fallar el post entero. Se
  recomprime a JPEG con **Pillow** (ahora dependencia declarada; sin ella la tool avisa
  en vez de mandar algo que el canal va a rechazar). Medido: 1344 KB → 169 KB a q88.
- **`aviso`**: mensaje corto ANTES de generar, porque generar tarda ~8s y en ese hueco no
  se ve nada. Sale de la tool y no de un LLM (pedirle la frase agregaría la demora que se
  quiere tapar); opt-in por settings; con dedup por kv para que un reintento no lo repita;
  se registra en `bot_posts` como cualquier post, y si ese registro falla **no** voltea la
  respuesta (`bot_posts.reply_to_handle` tiene FK a `users`).
- **`max_per_thread` es la lección cara**: el bot encadenó retratos del admin como
  respuesta a *"escribile una carta a panchitos"*, a *"el romance no murió"* y hasta al
  chiste de que subía retratos sin parar. Se le pidió por prompt tres veces y falló las
  tres. El tope por hilo **no se lo pide: no puede**.
- Sobre el modelo: **OpenRouter no tiene ningún generador de imágenes de xAI** (los seis
  modelos Grok son `output: ['text']`) — verificado contra su API. Configurado
  `google/gemini-2.5-flash-image` (~US$0,04) con `openai/gpt-5-image-mini` de fallback.
  Un generador poco filtrado exige salir de OpenRouter (xAI directo, o pesos propios con
  LocalAI, que habla `/v1/images/generations`); los dos piden US$5 de mínimo.
- Tests: `test_generate_image.py` (35).

### T53 · `similar_artists` (MusicBrainz + ListenBrainz)  `infra` `M`  `P2`  ✅ **HECHO** (2026-08-02)
Cierra el pedido que originó T50. **La tool no era el problema: era Spotify** — a la app
le quedó solo `/search` (`related-artists` 403, `/recommendations` 404, `audio-features`
403), así que *"algo parecido a X"* no puede salir de ahí por más que se mejore
`search_music`. MusicBrainz identifica y desambigua; ListenBrainz Labs da los vecinos por
escuchas reales. **Ninguna pide API key.**

- **No reemplaza a `search_music`**: devuelve ARTISTAS y el link lo sigue dando
  `search_music(artist=…)`. Son componibles y T50 las encadena.
- Verificado en vivo: `Kyuss → QOTSA, Fu Manchu, Melvins` · `"Los Redondos" → Patricio Rey
  (resolvió el apodo) → Charly, Divididos, Cerati` · `Sumo → desambiguó la banda argentina
  del homónimo de 3 oyentes` · `Bandalos Chinos → LOUTA, Babasónicos, El Mató`. Cadena
  completa hasta el link de Spotify.
- Tres defensas, todas por algo medido: **1 req/s** hacia MusicBrainz + User-Agent
  identificable (dos seguidas = 503) · **cache de 30 días** en la kv · **auto-reparación
  del algoritmo**: el enum de Labs cambió entre el 08-01 y el 08-02, así que ante un 400
  se sacan las opciones válidas del propio mensaje de error y se reintenta.
- ⚠️ Sesgo al canon confirmado en consultas argentinas (Charly y Cerati en casi todas).
  Pendiente menor: comparar contra Last.fm `artist.getSimilar` (necesita key gratuita).
- Tests: `test_similar_artists.py` (15).

### T54 · El bot no postea links que no leyó  `infra` `S`  `P0`  ✅ **HECHO** (2026-08-02)
Caso real: le pidieron una canción, **no llamó a ninguna tool de música** y posteó igual
`open.spotify.com/track/<id>` con el id fabricado — link roto, en público. El prompt ya
decía *"nunca escribas una URL que no leíste ahí"*; inventar el dato faltante es
exactamente lo que hace un modelo cuando no lo tiene.

- `_sacar_links_inventados(texto, visto)` compara contra **todo lo que el modelo tuvo
  delante** (system + user: resultados de tools, hilo, memoria). Lo que no esté, se saca.
- Solo mira links **con path**: nombrar un dominio (`buscá en google.com`) no es citar una
  fuente falsa y borrarlo rompería la frase. Un link que alguien pegó en el hilo vale.
- Aplicado en el camino de replies **y en el de rutinas** (donde el bot postea solo).
- Queda en el log (`saqué N link(s) inventado(s)`) para enterarse sin mirar la timeline.
- Prompt, para la otra mitad que el código no puede ver: *no narres acciones que no
  hiciste* — el bot había escrito `🎵 abriendo spotify...` con cero tools llamadas.
- Tests: `test_links_inventados.py` (8).

### T56 · `generate_audio` (TTS · ElevenLabs)  `infra` `M`  `P1`  ✅ **HECHO** (2026-08-03)
Tool `generate_audio(text)`: convierte texto en **nota de voz** y la adjunta por la misma
plomería de media que las imágenes (`ToolResult.image_path` → `media_path` del canal).
No pasa por el router — ElevenLabs no habla OpenAI: endpoint propio con
`ELEVENLABS_API_KEY` en el `.env` de la instancia.

- **Ogg/Opus a propósito** (`output_format=opus_48000_64`): es lo que WhatsApp exige para
  que salga como nota de voz (`PTT`) y lo que Telegram va a pedir en `sendVoice` (T40) —
  un formato, dos canales, sin transcodificar.
- **El canal declara `SUPPORTS_AUDIO`** (hoy solo WhatsApp): el .ogg viaja por la plomería
  genérica y Bluesky lo rechazaría como imagen — la tool se niega antes de romper el reply.
- **Bridge Go**: `/send` decide por extensión — audio sube como `MediaAudio`
  (`AudioMessage`, `PTT` con ogg/opus); `AudioMessage` no tiene caption, así que el texto
  del bot sale en un mensaje aparte ANTES y la nota de voz llega pegada atrás.
- `AUDIO_GEN`: `voice_id` (obligatorio) · `model_id` · `max_per_day` · `max_per_thread`
  (misma lección que T52: el prompt pide, el código impide) · `max_chars` (ElevenLabs
  cobra por carácter: un texto largo se **rechaza**, no se trunca — un audio cortado es
  peor que pedir que lo acorten).
- **Nace scope `admin`** como T52; `rancher` lo amplía a todos (`reply`) por settings.
- **UI (2026-08-03):** tarjeta "🎙️ Voz del bot" en la sección Tools — selector de voz
  poblado desde la cuenta real (`GET /voices` del lado del server; un voice_id de la
  library se guarda bien y después da 402 en el tier gratis, la lista de la cuenta es lo
  único que no miente), botón "probar la voz guardada" que reproduce el audio en la página,
  y los topes editables. El cliente HTTP vive en `elevenlabs_client.py`, compartido entre
  la tool y la UI; `ELEVENLABS_API_KEY` entra por la sección Credenciales (write-only).
  On/off y permisos: en la tabla de tools, como cualquier otra.
- Tests: `test_generate_audio.py` (11).

### T57 · Susceptibilidad de memoria  `infra` `S`  `P2`  ✅ **HECHO** (2026-08-03)
Pedido del admin al medir que el bot casi no anotaba hechos: en arg, **536 interacciones
en 7 días → 51 hechos** desde conversación (el resto vino de UI/bios/compactación). El
prompt de extracción es estricto a propósito — la basura en `user_facts` GANA el retrieval
(T49) — pero cuán selectivo ser es una decisión del admin, no del motor.

- `MEMORY_SUSCEPTIBILITY` (0–1, default **0.3** = la conducta histórica), mismo patrón que
  `MOODS.susceptibility`. Se traduce a un bloque que se APPENDEA al prompt de extracción
  en código (sin tocar las tres copias de `update_user_prompt.md`): ≤0.15 mínimo ·
  0.15–0.45 nada (el prompt del archivo ya es estricto; repetirlo es contexto tirado) ·
  ≤0.75 generoso (gustos, fandoms, datos casuales claros) · >0.75 máximo ("ante la duda,
  anotalo"). Los límites que nunca se aflojan: solo lo autodicho, nada de terceros ni del bot.
- UI: slider en "Memoria por usuario", con descripción del tramo elegido.
- La nota de interacción no se toca: se escribe siempre, a cualquier susceptibilidad.
- Configurado: **arg = 0.7** · rancher = default. Tests en `test_interactions.py` (3).

### T55 · Deuda abierta por este bloque  `infra` `M`  `P1`  ⬜ **PENDIENTE**
Ninguna es teórica: las cuatro se manifestaron el 2026-08-02.

- **Tope de reintentos para menciones `failed`.** Con los endpoints caídos, las mismas dos
  menciones se reprocesaron cada 4 minutos durante **una hora y veinte**.
  `retry_stuck_mentions` no tiene ventana ni contador: convierte cualquier caída en un loop.
- **`BUDGET.enabled: false` en arg.** El guard **detectó** `QUEMADO — $3.08 de $3.00` a las
  07:25 y no frenó nada porque está apagado. Además `budget_state` quedó en `"sleeping"` —
  revisar si sale solo de ese estado.
- **`Could not resolve handle https:`** — cientos de llamadas a la API de Bluesky
  resolviendo un handle inexistente. Algo toma `https:` como handle al parsear texto.
- **Tests sensibles a la fecha**: `test_events_today_ar`, `test_admin_creates_community_event`
  y `test_user_creates_only_for_self_ignores_handle` tienen fechas hardcodeadas y fallan
  según la hora del día. Aparecen y desaparecen, y enseñan a ignorar los rojos.
