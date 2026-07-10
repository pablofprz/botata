# Butterbot — Roadmap

Refundación del bot comunitario de Bluesky sobre langgraph + SQLite. Este documento es
la **fuente de verdad** de las funciones a construir, ordenadas por fase y prioridad.
Cada tarea tiene criterios de aceptación para poder trabajarla contra spec.

**Convenciones**
- **Priority:** P0 (bloqueante/fundacional) · P1 (prioritario) · P2 (deseable) · P3 (después)
- **Module:** `infra` · `proactivity` · `tools` · `scraper` · `ui`
- **Effort:** S (<½ día) · M (1-2 días) · L (varios días)
- Toda tool nueva se registra en el **framework de tools** (T1) y respeta enable/disable + scope.

---

## Decisiones validadas
- **Secuencia:** fundaciones primero (framework de tools + router de modelos) antes de la proactividad.
- **Fuentes de feed a soportar:** listas · feeds custom (generators) · timeline de seguidos.
- **Reddit:** investigar la API oficial (spike) antes de decidir vía (API / RSS / navegador).

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
- Impl.: helper `current_datetime_line()` en `butterbot.py` (ZoneInfo + fallback UTC-3, `tzdata` en deps). Inyectado en reply, admin, feed_summary, feed_opinion y update_profile. Fija el bug de fecha alucinada (el bot leía fechas de MEMORY.md como "hoy").

### T4 · Schema de eventos/calendario  `infra` `S`  ✅ **HECHO**
- **Acept.:** tabla `events(id, handle?, title, description, event_at, kind, source, created_at)` + índices por fecha y handle.
- Migración idempotente en `db.py`. Helpers CRUD + query "próximos N eventos" / "eventos de hoy".
- Impl.: tabla + índices en `_SCHEMA`; helpers `create/get/update/delete_event`, `upcoming_events`, `events_today` (default AR, filtro por handle+comunidad). FK cascade al borrar user. Tests en `tests/test_events.py`.

---

## M2 · Proactividad  `P0`  *(lo más prioritario)*

### T5 · Loop proactivo de feed  `proactivity` `L`  ✅ **HECHO**
Reemplaza el `FeedProcessor` manual por un loop autónomo.
- **Acept.:** parámetro de **frecuencia de lectura** por feed (o desactivado).
- Cada corrida: lee posts dentro de una ventana temporal → resume → el **agente decide** si postea (autónomo) o no; parámetro para forzar posteo.
- La decisión considera temática y "mood"; puede lanzar tools **whitelisted por scope `feed_reflection`** (T1).
- Deduplica contra `bot_posts`; respeta presupuesto (router T2). Sin scheduling rígido tipo cron.
- Impl.: grafo langgraph `build_feed_graph` (fetch→summarize→reflect→post) en `butterbot.py`. `ReflectDecideNode` es el núcleo agéntico (structured output `FeedDecision`, tool_calling scope feed_reflection, política de posteo configurable conservative|balanced|active). Config por feed: `enabled` + `posting_policy` + `interval_hours`. CLI `--proactive [--force-post] [--backfill]`. Dedup normalizado contra `bot_posts`. Tests: `tests/test_feed_proactive.py`.

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
- **Acept.:** mover a archivos los prompts incrustados en `butterbot.py`; cargarlos con `load_text(PROMPTS_DIR / ...)` como ya hacían `reflect_feed_prompt.md` / SOUL.
- Impl.: externalizados a `prompts/` — guía de posteo por política → `feed_policy_{conservative,balanced,active}.md` (helper `feed_policy_guidance()` con fallback a balanced); resumen de feed → `feed_summary_prompt.md`; formato JSON de `ReflectDecideNode` → `feed_decision_format.md`; system prompt de `HandleAdminCommandNode` → `admin_command_prompt.md`; bloque de formato de `GenerateReplyNode` → `reply_format.md`. 31 tests verdes.
- **Quedan inline a propósito** (no son prompts fijos sino armado dinámico): labels de secciones del reply (memoria/hechos/lecciones/catálogo, interpolan data), instrucción condicional de force-post y hint de tools en `ReflectDecideNode`, y el path legacy `FeedProcessor.post_opinion`.

---

## M3 · Tools prioritarias  `P1`

### T8 · Tool de búsqueda (Brave)  `tools` `M`  ✅ **HECHO**
- **Acept.:** integración con Brave Search API; tool `web_search(query)` registrada en T1, toggleable.
- Impl.: `web_search` (scopes `reply`+`feed_reflection`+`admin`): GET a Brave vía stdlib (`urllib`, sin deps nuevas), `BRAVE_API_KEY` opcional desde `.env` (gitignored). Devuelve top-N (default 5, máx 10) con título/resumen/link; limpia tags HTML de los snippets. **Degradación graceful:** sin key → "no configurada"; HTTP 429 → "estoy limitado"; otro error → "no pude buscar". Verificado en vivo contra Brave. Tests: `tests/test_web_search.py` (7, GET mockeado). **Cierra M3.**

### T9 · Tool de calendar  `tools` `M`  ✅ **HECHO**
- **Acept.:** el agente lee/escribe `events` (T4) como tool.
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

---

## M5 · Browser & scrapers  `P2`

### T16 · Tool de navegador agéntica  `tools` `M`
- **Acept.:** el usuario puede mandar el bot a una página y que acceda. **OFF por default**, con **aviso de riesgo** explícito al activarla.
- Guardrails: allowlist/denylist de dominios, límite de acciones, sin descarga/ejecución arbitraria. Reusa `browser.py`.

### T17 · Reddit — spike de API  `scraper` `S`
- **Acept.:** investigar viabilidad de la API oficial (PRAW/OAuth read-only) hoy. Documentar decisión: API / RSS / navegador.

### T18 · Reddit ingestion  `scraper` `M`
- **Acept.:** implementar `RedditSource` (hoy stub) según el resultado de T17. Solo lectura de subs, sin postear.

### T19 · Scraper X  `scraper` `L`
- **Acept.:** X no tiene API libre → vía navegador (`browser.py`) o investigar otra. `TwitterSource` (hoy stub).

### T20 · Hardening scraper IG  `scraper` `M`
- **Acept.:** robustecer `scrape_ig.py` / `ig_api.py` (manejo de sesión, reintentos, config de cuentas objetivo).

---

## M6 · Después  `P3`

### T21 · Descarga de videos IG  `scraper` `M`
- **Acept.:** extender el scraper IG para bajar videos (hoy se filtran). Baja prioridad.

### T22 · UI de configuración  `ui` `L`
- **Acept.:** UI gráfica para: credenciales de Bluesky, modelo(s), tipo de comunidad (feeds/listas/timeline), y toggles de tools.
- **Última a propósito:** es un front sobre un schema de config que todavía se está estabilizando (T1/T2/T7).
