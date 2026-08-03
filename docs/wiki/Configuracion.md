# Configuración

## El panel web

```bash
python -m botata --init <nombre>   # abre el panel de una instancia (existente o nueva)
```

Es una UI local (solo escucha en 127.0.0.1) que edita todo lo configurable de la instancia:
settings, credenciales, fuentes de contenido, skills, moods, rutinas y grupos de usuarios.
Las credenciales son **write-only**: una vez guardadas, la API nunca las devuelve. Las
escrituras son atómicas y con backup `.bak`.

La mayoría de los cambios de settings requieren reiniciar el bot; el toggle de skills y la
edición de archivos markdown (skills, moods, rutinas) aplican **en caliente**.

## Las secciones de `settings.json`

| Sección | Qué controla |
|---|---|
| `BOT_HANDLE` / `ADMIN_HANDLE` | identidad del bot y quién lo administra |
| `CHANNEL` | en qué red habla la instancia (`bluesky` / `mastodon` / `discord` / `whatsapp`) |
| `MODELS` | endpoints, aliases y roles del router de LLMs, con cadenas de fallback |
| `FEEDS[]` | fuentes de **lectura** (listas, feeds, following) + intervalo por fuente |
| `TOOLS` | enable y scopes por herramienta |
| `TASKS` | enable e intervalo por tarea periódica |
| `MOODS` | estados de ánimo: apagado / fijo / automático, con susceptibilidad e histéresis |
| `MCP` | servers MCP externos (conectores como tools) |
| `BUDGET` | presupuesto diario de tokens |
| `RETRIEVAL` | cuántos hechos/notas/lecciones trae la memoria por respuesta |
| `TOOL_ROUNDS` | cuántas rondas de herramientas puede encadenar el bot (1–5) |

## El registro de fuentes (`config/sources.json`)

Todas las fuentes de contenido viven en un registro único: un **tema** con sus fuentes y un
`type` que dice por dónde entra cada una:

| `type` | Qué es | Quién lo consume |
|---|---|---|
| `rss` | feeds RSS de noticias | tool `get_news` |
| `spotify` | playlists | `get_playlist_track`, `add_music_recommendation` |
| `youtube` | canales o listas | `share_video` |
| `pinterest` | tableros (RSS oficial, sin credenciales) | `get_latest_media` |
| `tumblr` | blogs | `get_latest_media` |
| `api` | **cualquier API JSON**, descripta en la propia entrada | `get_latest_media` (media) / `get_data` (datos) |
| `membrilla` | media scrapeada e indexada con visión | `search_images`, `search_videos` |

El tipo `api` merece mención: con `url`, `items_path` y un `map` de campos podés sumar el
dólar, el clima o los feriados **sin escribir código** — es cargar una fuente en la UI y
probarla con el botón *probar ahora*. Todo se resuelve en query, así que editar el registro
aplica en caliente.

## El presupuesto (`BUDGET`)

Tope diario de gasto en LLM. Si se quema, el bot **se duerme** hasta el día siguiente (a
los admins les sigue contestando lo mínimo). El cambio de día siempre lo despierta. Los
cambios de esta sección requieren reiniciar el bot — el guard se arma al arrancar.

## Variables de entorno (`.env` de la instancia)

| Variable | Para qué |
|---|---|
| `LLM_API_KEY` | la API key del LLM (obligatoria) |
| `BSKY_PASSWORD` / `MASTODON_ACCESS_TOKEN` / `DISCORD_BOT_TOKEN` | credencial del canal activo |
| `BRAVE_API_KEY` | búsqueda web (opcional) |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | música (opcional) |
| `YOUTUBE_API_KEY` | videos (opcional) |
| `TUMBLR_API_KEY` | fuentes Tumblr (opcional) |

Nada de esto entra jamás al repo: la carpeta `bots/` entera está gitignored.
