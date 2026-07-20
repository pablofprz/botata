# botata

Bot comunitario de Bluesky construido como **agente**: no reacciona con plantillas, sino que
un LLM decide qué responder, si postear, qué herramienta usar y qué recordar de cada persona.
Refundación del monolito `maripobot.py` sobre langgraph + SQLite. Genérico y multi-comunidad
(no atado a Argentina): la personalidad y las fuentes son config.

> Repo privado. Admin único: **Polci** (`ppolci.com`). Dev en Windows 11, prod en Linux Mint.

## Pilares

- **Grafos de estado (`langgraph`)** para los flujos de decisión (menciones + pases proactivos).
- **SQLite como única fuente de verdad** (`posted/botata.db`, WAL) con búsqueda semántica local:
  `sqlite-vec` (vec0, embeddings **bge-m3** dim 1024, en CPU) + FTS5 (BM25), fusionados por RRF.
- **Herramientas declarativas**: registry propio con scopes (`src/tools.py`) + conectores MCP
  externos (`src/mcp_tools.py`).
- **Comportamiento en archivos, no en código**: prompts (`prompts/`), skills en markdown
  (`skills/`, hot-reload), settings (`config/settings.json`) — nada de prompts hardcodeados.

## Estructura del repo

```
src/          ← todo el código Python (botata.py = el corazón; db, router, tools, skills,
                scheduler, mcp_tools, catalog, config_ui, budget, clearsky, mem_admin, ...)
config/       ← settings.json (config central) + news_sites.json (credenciales gitignored)
context/      ← SOUL.md (personalidad) + MEMORY.md (memoria general del bot)
prompts/      ← todos los prompts del sistema · skills/ ← skills en markdown editables en caliente
mcp_servers/  ← servers MCP propios (reddit_server.py; futuros: x, ig)
ui/           ← panel web local de configuración (config_ui.py)
vault/        ← memoria de desarrollo (Zettelkasten) + tablero de tareas (dentro del repo)
docs/         ← ARQUITECTURA.md (cómo funciona el código) · tests/ ← ~276 tests (pytest)
scripts/      ← sync de credenciales/DB, snapshot WAL, pipeline de chats al vault
```

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/activate      # (Linux: source .venv/bin/activate)
pip install -e ".[dev]"                              # instala botata en editable + dev tools
cp env.example .env                                  # completá BSKY_PASSWORD, OPENROUTER_API_KEY, ...
python config_ui.py                                  # panel web local (127.0.0.1) para configurar
python -m botata --mode open                         # arranca el bot (open | admin_only)
```

La primera corrida descarga el modelo de embeddings (bge-m3, ~2 GB, cacheado por HuggingFace).

## Configuración

- **UI local** (`python config_ui.py`): edita `settings.json`, credenciales (`.env`, write-only),
  noticias y toggles de skills desde el navegador (solo localhost).
- **Desde Bluesky**: el admin ajusta config por mención (T30) — ver `ROADMAP.md`.
- **Modelos**: router por rol con fallbacks entre endpoints OpenAI-compatibles (`config/settings.json`
  → sección `MODELS`).

## Contenido y scraping

El **scraping** vive en una suite aparte, **[Membrilla](https://github.com/pablofprz/membrilla)**
(local-first, reusable por otros agentes). Membrilla deja media + un sidecar `<id>.json` en una
carpeta; botata la ingiere con `src/catalog.py` (la describe con visión → `image_catalog`) sin
compartir código ni DB. Contenido on-demand: Spotify, YouTube, noticias/RSS, búsqueda web (Brave),
Reddit (MCP), navegador agéntico (`@playwright/mcp`).

## Tests

```bash
pytest -q          # ~276 tests, <10s, sin red (salvo E2E de MCP marcados)
```

## Documentación

- **`CLAUDE.md`** — estado del proyecto, decisiones de arquitectura y su porqué.
- **`ROADMAP.md`** — tareas T1–T36 con criterios de aceptación (espejo navegable en `vault/botata/roadmap.md`).
- **`docs/ARQUITECTURA.md`** — cómo funciona el código, módulo por módulo.
- **`vault/`** — notas permanentes (Zettelkasten), chats indexados, tablero de tareas.
  Consulta antes de re-leer código: grafo `graphify-out/` → vault → código.

## Licencia

Ver `LICENSE`.
