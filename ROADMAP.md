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

---

## M5 · MCP, browser & scrapers  `P2`

**Decisión (admin):** el cliente MCP (T29) va **antes** que los scrapers. Los scrapers
(T17–T20) nacen como **MCP servers** independientes (procesos stdio, deps y crashes
aislados, reutilizables desde otros agentes), no como módulos internos de butterbot.

### T29 · Cliente MCP → ToolRegistry  `infra` `M`  *(primero de M5)*  ✅ **HECHO**
Butterbot consume MCP servers externos como tools; cada conector futuro pasa de "tarea" a "línea de config".
- **Acept.:** módulo `mcp_tools.py` (SDK oficial `mcp`): al arranque lee `settings.json` → sección `MCP` (`{server: {transport: stdio|http, command/url, enabled, scopes, tool_filter}}`), conecta, hace `tools/list` y registra cada tool en el `ToolRegistry` (T1) con handler proxy a `tools/call`.
- Nombres prefijados por server (`reddit_top_posts`) para evitar colisiones. Puente async→sync contenido en el módulo (event loop en thread de fondo).
- **Seguridad (regla dura):** tools MCP nacen con scope `admin`; promover a `reply`/`feed_reflection` es opt-in explícito por tool en config — el bot es público y scope reply = superficie de prompt injection.
- Degradación graceful: server caído al arranque → se loguea y se omite (no tira el bot); error en `tools/call` → `ToolResult` de error, no excepción.
- El calendario interno (`events`, T4/T9) sigue nativo — dominio core acoplado a la DB. Calendarios externos (ej. Google Calendar) = server MCP de terceros vía config, sin código.
- Impl.: `mcp_tools.py` (infra genérica estilo `tools.py`, no conoce butterbot). `MCPBridge` = event loop en thread daemon; **cada server vive en una task dedicada** que entra/sale de sus propios context managers (los cancel scopes de anyio no cruzan tasks — gotcha del SDK). Sesiones quedan abiertas; reuso si el mismo proceso construye dos registries (run() lo hace); `shutdown()` vía `atexit`. Registro **antes** de `apply_config` → la sección `TOOLS` overridea tools MCP por nombre prefijado. Import lazy: sin sección `MCP` el SDK ni se carga. Handler proxy **jamás lanza** (el call site admin ejecuta sin try/except); v1 texto-only (content blocks de imagen = pendiente). `tests/mcp_echo_server.py` = plantilla FastMCP para los servers de T18–T20. Verificado en vivo (registro + execute + server roto omitido). Tests: `tests/test_mcp_tools.py` (13, unit + E2E stdio real).

### T16 · Tool de navegador agéntica  `tools` `M`  ✅ **HECHO**
- **Acept.:** el usuario puede mandar el bot a una página y que acceda. **OFF por default**, con **aviso de riesgo** explícito al activarla.
- Guardrails: allowlist/denylist de dominios, límite de acciones, sin descarga/ejecución arbitraria.
- **Servida como MCP server** (misma fragilidad/deps pesadas que los scrapers); butterbot la consume vía T29.
- **DECISIÓN (investigación 2026-07-11): reusar `@playwright/mcp` de Microsoft — cero código propio.** Verificado en vivo vía el cliente T29 (spawn `npx @playwright/mcp --headless --isolated --allowed-origins ...`): navegación OK y dominio fuera de la allowlist bloqueado (`ERR_BLOCKED_BY_CLIENT`) con error graceful. Los guardrails de la acept. mapean 1:1 a flags oficiales (`--allowed-origins`/`--blocked-origins`, `--headless`, `--isolated`, file access restringido a workspace) + `tool_filter` de T29 para recortar el tool set (25+ tools → navigate/snapshot/close). Requiere **Node 18+** (dev OK v24; verificar en Linux Mint prod). `browser.py`/patchright NO se toca: es la capa *stealth con sesión logueada* para T19/T20 — problema distinto (Playwright MCP no es stealth).
- Impl.: **cero código** — server `browser` declarado en `settings.json` → MCP (`npx @playwright/mcp@latest --headless --isolated`, `tool_filter` a navigate/navigate_back/snapshot/close, **`enabled:false`**). El snapshot inline se resolvió solo: la tool `browser_snapshot` devuelve el árbol de accesibilidad en YAML dentro del ToolResult (el link-a-archivo era solo la respuesta de `navigate`); flujo del LLM = navigate → snapshot. Verificado en vivo por el camino real (`build_tool_registry` + Wikipedia es). Scope `admin` (default T29).
- **⚠️ AVISO DE RIESGO (leer antes de prender `enabled:true`):** el LLM navega cualquier URL que le pidan. Con scope `admin` (default) solo el admin puede mandarlo; **si se promueve a `reply`, cualquier usuario puede dirigir el navegador del bot** → prompt injection vía contenido web + SSRF-lite (URLs internas). Para scope reply: agregar `--allowed-origins "https://sitio1;https://sitio2"` a los args (allowlist dura, verificada: bloquea con `ERR_BLOCKED_BY_CLIENT`) y/o `--blocked-origins`. `--isolated` (perfil en RAM, sin cookies persistentes) ya viene en la config. **Prod:** requiere Node 18+ en el Linux Mint (dev: v24 OK) — la primera corrida de `npx` descarga el paquete.

### T17 · Reddit — spike de API  `scraper` `S`  ✅ **HECHO**
- **Acept.:** investigar viabilidad de la API oficial (PRAW/OAuth read-only) hoy — Reddit devuelve 403 sin auth (visto en T14). Documentar decisión: API / RSS / navegador.
- **DECISIÓN: RSS.** Verificado en vivo (2026-07-11) + fuentes:
  * **API OAuth: descartada.** Desde fines de 2025 las apps nuevas pasan por aprobación manual (Responsible Builder Policy); proyectos personales/hobby son la categoría más rechazada, sin SLA (días a semanas, o silencio). Free tier teórico: 100 QPM no-comercial — irrelevante si no aprueban la app. No hay app preexistente de maripobot (usaba RSS). Reevaluar solo si Reddit afloja el gate.
  * **RSS: funciona hoy sin credenciales.** `https://www.reddit.com/r/{sub}/{sort}/.rss` (Atom, no RSS 2.0) devuelve 200 con UA descriptivo; soporta sorts (`new`, `top/.rss?t=week`) y multireddit (`sub1+sub2`). **Límite duro medido:** 1 request/min por IP, GLOBAL (headers `x-ratelimit-remaining=0.0` tras 1 req; 429 inmediato al segundo hit; confirmado por reportes públicos de junio 2026). Compatible con el patrón butterbot: pases espaciados por `interval_hours`, pocas fuentes, ≥61s entre requests dentro de un pase.
  * **Navegador: innecesario** mientras RSS siga vivo (queda como plan C).
  * JSON público (`hot.json`): 403 sin auth — muerto, confirma T14.

### T18 · Reddit ingestion (MCP server)  `scraper` `M`  ✅ **HECHO**
- **Acept.:** MCP server `reddit` (FastMCP, stdio) **vía RSS** (decisión T17). Solo lectura de subs, sin postear. Reemplaza el stub `RedditSource`; butterbot lo consume vía T29.
- Requisitos del diseño (de las mediciones de T17): **rate limiter interno ≥61s entre requests** (global al server, con cola o cache corto); parser **Atom** (stdlib `xml`, el `fetch_rss` de T15 parsea RSS 2.0 — extender o duplicar); UA descriptivo fijo; degradación graceful ante 429 (respetar `x-ratelimit-reset`).
- Impl.: `mcp_servers/reddit_server.py` (primer server real sobre la plantilla T29; carpeta nueva `mcp_servers/`). Tool única `get_subreddit_posts(subreddit, sort, time_window, limit)` — acepta multireddit `a+b`; sorts hot/new/top/rising; devuelve JSON title/url/author/published/summary. `_fetch` serializa con lock: espera bloqueante hasta el slot de 61s, cache TTL 10 min por URL, retry único ante 429 si `Retry-After` ≤ 90s. Validación estricta de subreddit/sort/window (anti path-traversal). Declarado en `settings.json` → MCP con **`enabled:false`** (`call_timeout_s:90` porque una llamada puede esperar el slot) — el admin lo prende. Stub `RedditSource` borrado de `sources.py`. **Verificado en vivo** (top diario de r/argentina por el pipeline completo T29→T18). Tests: `tests/test_reddit_server.py` (11: parsing/URLs/limiter/cache sin red + spawn E2E).

### T19 · Scraper X (MCP server)  `scraper` `L`
- **Acept.:** X no tiene API libre → vía navegador o investigar otra. MCP server `x` independiente; reemplaza el stub `TwitterSource`.

### T20 · Hardening scraper IG (MCP server)  `scraper` `M`
- **Acept.:** robustecer `scrape_ig.py` / `ig_api.py` (manejo de sesión, reintentos, config de cuentas objetivo) y envolverlo como MCP server `ig`.

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
- Impl.: `scheduler.py` (genérico, no conoce butterbot): `PeriodicTask(name, fn, interval_hours, enabled)` + `run_due()` + `apply_tasks_config()`. **Sin doble gate:** `interval_hours=0` = correr cada iteración (feed/news/mentions se auto-gatean por dentro, sin tocar su lógica); `>0` = gate del scheduler vía cursor `task:{name}` (reusa `get/save_feed_last_run`). **Aislamiento de errores POR TAREA** (mejora sobre el try único por iteración; KeyboardInterrupt atraviesa). `run()` quedó genérico: lista de 4 tareas + `run_due()` + sleep; el poll de menciones se extrajo a `_poll_mentions()` sin cambios. **Heartbeat** = `run_heartbeat_pass()` (función, no grafo): lee `events_today`+`upcoming_events` — **sin eventos no llama al LLM** (costo cero); prompt = SOUL + fecha + `prompts/heartbeat_prompt.md` (nuevo, T23) + skills feed_reflection (T26) + eventos + posts recientes; decide con `FeedDecision` reutilizado (rol `feed_opinion`); dedup normalizado contra `bot_posts`; **nunca postea eventos personales no celebrables** (regla en el prompt). Config `TASKS` en settings (heartbeat **off por default**, 12h). CLI `--heartbeat` (pase único). **Verificado en vivo con LLM real** (bsky falso): saludó un cumpleaños en personaje y en la segunda pasada se negó a repetirse ("ya saludé a Polci hoy"). Tests: `test_scheduler.py` (9) + `test_heartbeat.py` (6). **Cierra M6.**

### T30 · Configuración por comandos de admin  `tools` `M`  ✅ **HECHO** *(agregada post-M6, pedido del admin 2026-07-13)*
El admin ajusta la config del bot **desde Bluesky** ("@bot prendé el heartbeat cada 6 horas"), sin SSH ni editar JSON.
- **Acept.:** tools scope `admin` que mutan el runtime en vivo Y persisten a settings.json; validación previa a todo; claves de identidad intocables por post.
- Impl.: 6 tools registradas en el registry (factories que se cierran sobre `reg`): `get_bot_config` (estado vivo legible) · `set_tool_config` (enabled/scopes) · `set_task_config` (feed/news/mentions/heartbeat: enabled/intervalo) · `set_feed_config` (enabled/intervalo/política) · `set_news_enabled` · `set_mcp_enabled` (solo persiste — spawn requiere reinicio; el bot lo avisa). **Doble efecto con orden seguro:** persistir PRIMERO vía `_persist_settings_delta` (relee disco → delta → validadores de T22 → escritura atómica con `.bak`); si la validación rechaza, el runtime no se toca. **Aplicación en vivo**: registry compartido (tools), `_RUNTIME_TASKS` (lista viva del loop, nueva), `FEEDS_CONFIG`/`NEWS_ENABLED` (globals que los pases releen). **Guard de claves prohibidas** (defensa en profundidad): `BOT_HANDLE`/`ADMIN_HANDLE`/`MODELS`/estructura MCP no se cambian por comando aunque un handler lo intente — lock-out/secuestro queda imposible desde un post. Prompt admin actualizado con ejemplos; regla "un cambio por mensaje" (el flujo admin ejecuta UNA tool). Verificado con LLM real: "prendé el heartbeat cada 6 horas" → `set_task_config {task: heartbeat, enabled: true, interval_hours: 6}`; "qué tenés apagado" → `get_bot_config` con el estado real. Tests: `tests/test_admin_config_tools.py` (13). Suite: 202.

---

## M7 · Multi-canal  `P3`  *(solo con el agente Bluesky completamente listo)*

### T28 · Abstracción Channel (gateway multi-plataforma)  `infra` `L`
- **Acept.:** interfaz `Channel` (mentions/post/reply/perfil de autor, sobre de mensaje normalizado) de la que `BskyChannel` es la primera implementación. El grafo langgraph no sabe en qué plataforma habla.
- **Orden de expansión decidido:** Bluesky → Mastodon → Discord → Telegram → WhatsApp. Un canal nuevo = una implementación de `Channel` + config; cero cambios en el núcleo.
- Incluye revisar el modelo de sesión: en Bluesky la conversación es el thread (se reconstruye por `get_post_thread`); en Discord/Telegram hace falta estado de conversación persistente por canal/chat.

---

## M8 · Después  `P3`

### T21 · Descarga de videos IG  `scraper` `M`
- **Acept.:** extender el scraper IG para bajar videos (hoy se filtran). Baja prioridad.

### T22 · UI de configuración  `ui` `L`  ✅ **HECHO** *(adelantada pre-M7 por decisión del admin)*
- **Acept.:** UI gráfica para: credenciales de Bluesky, modelo(s), tipo de comunidad (feeds/listas/timeline), y toggles de tools.
- ~~Última a propósito~~ **Adelantada (2026-07-12):** el schema de config ya se estabilizó (TOOLS/TASKS/MCP/FEEDS/MODELS) y la pila de toggles justificaba la UI ya. Criterio del admin: **esta es la UI del núcleo**; las futuras UIs por canal (Discord, etc.) serán interfaces separadas — no una UI para todo.
- Impl.: `config_ui.py` (stdlib puro: `http.server`, cero deps) + `ui/config.html` (una página, vanilla JS, dark). **Solo localhost** (bind 127.0.0.1; `python config_ui.py [--port] [--no-browser]`). Secciones: identidad · credenciales (.env **write-only**: la API jamás devuelve valores, solo qué claves están seteadas; input vacío = no tocar) · modelos (endpoints/aliases/roles) · feeds · tools (enabled + scopes con ⚠️ al promover a reply) · tareas · MCP · noticias (master + fuentes) · skills (toggle hot-reload que reescribe el frontmatter). **Validación server-side antes de escribir** (scopes/tipos/políticas/aliases huérfanos → 400 con detalle, no toca el archivo); escrituras atómicas (`tmp` + `os.replace`) con backup `.bak`. Verificado en vivo por browser: toggle de skill → frontmatter en disco → restaurado; settings inválido → 400 visible; round-trip del settings re-serializado → el bot importa igual. Tests: `tests/test_config_ui.py` (18). Suite: 189.
