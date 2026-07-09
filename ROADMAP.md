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

### T5 · Loop proactivo de feed  `proactivity` `L`
Reemplaza el `FeedProcessor` manual por un loop autónomo.
- **Acept.:** parámetro de **frecuencia de lectura** por feed (o desactivado).
- Cada corrida: lee posts dentro de una ventana temporal → resume → el **agente decide** si postea (autónomo) o no; parámetro para forzar posteo.
- La decisión considera temática y "mood"; puede lanzar tools **whitelisted por scope `feed_reflection`** (T1).
- Deduplica contra `bot_posts`; respeta presupuesto (router T2). Sin scheduling rígido tipo cron.

### T6 · Aprendizajes del feed → memoria/calendario  `proactivity` `M`
- **Acept.:** tras leer el feed, el agente decide (o no) extraer aprendizajes.
- Si un post revela algo de un usuario con perfil → `upsert_user_fact` para ese handle.
- Si revela una fecha/evento → alta en `events` (T4). Ej: "hoy me duele el pie" → fact de ese usuario.
- Idempotente: no duplica aprendizajes ya guardados (dedup semántico existente).

### T7 · Generalización de fuentes de feed  `proactivity` `M`
- **Acept.:** config por comunidad soporta 3 tipos de fuente: `list` (actual), `feed` (generator/algoritmo), `following` (home timeline del bot).
- Cliente Bluesky con método por tipo; `FEEDS[]` declara `type` + identificador.
- Documentar que hoy solo hay `list`.

---

## M3 · Tools prioritarias  `P1`

### T8 · Tool de búsqueda (Brave)  `tools` `M`
- **Acept.:** integración con Brave Search API; tool `web_search(query)` registrada en T1, toggleable.
- Se dispara cuando un usuario pide buscar algo (o el agente lo decide en feed_reflection si está en scope).
- Devuelve top resultados resumidos; maneja rate limit / falta de API key con degradación graceful.

### T9 · Tool de calendar  `tools` `M`
- **Acept.:** el agente lee/escribe `events` (T4) como tool.
- Acciona según eventos cargados (ej. saludar en cumpleaños, recordar evento del día) dentro del loop proactivo.
- Alta de eventos por admin y — parámetro configurable — por usuario. Un usuario nunca crea eventos de otro.

### T10 · Comando `blockme`  `tools` `S`
- **Acept.:** el bot bloquea al usuario que lo pide y **borra toda su memoria** (user_facts + embeddings + events del handle + relationships). Confirmación al usuario.

### T11 · Comando `resetme`  `tools` `S`
- **Acept.:** borra **únicamente** la memoria del handle que lo pide (facts + embeddings + events propios). **Nunca** toca datos de otros. No bloquea.

---

## M4 · Tools de contenido  `P2`

### T12 · Imágenes autónomas + carpeta manual + guardrail  `tools` `M`
- **Acept.:** además del posteo a pedido (ya existe), el agente puede decidir postear una imagen si el contexto lo amerita — **parámetro configurable** (off por default).
- Carpeta de **input manual** formalizada (`scrape/pictures/manual/`), catalogada por `catalog.py`.
- **Guardrail:** el agente NO postea imágenes que no entiende (sin descripción/categoría válida en el catálogo).

### T13 · Tool música Spotify  `tools` `M`
- **Acept.:** buscar canciones en Spotify a pedido del usuario o por decisión del agente. Portar lógica de `maripobot_deprecated` (`get_random_track_from_playlist`, `generate_track_opinion`). Toggleable.

### T14 · Tool música YouTube  `tools` `M`
- **Acept.:** ídem Spotify pero YouTube. Portar `fetch_top_mealtime_video` / `get_youtube_transcript` / `generate_video_comment`. Toggleable.

### T15 · Noticias/RSS gestionadas por admin  `tools` `M`
- **Acept.:** el admin carga listas de RSS con **título + descripción**; el bot postea lo nuevo.
- Toggle por feed: **comentar (LLM)** o **solo postear**. Portar `fetch_rss` / `summarize_news`. Dedup de items ya posteados.

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
