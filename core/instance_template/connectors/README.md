# connectors/ — conectores propios de esta instancia

Cada `.py` de esta carpeta suma un **conector de contenido**: una plataforma más
de donde el bot puede sacar cosas, que después aparece en la UI (🧩 Plugins y
🏷️ Fuentes) igual que Pinterest o Tumblr, con su logo y su switch.

> ### 👀 Antes de escribir código: probá el tipo **API (JSON)**
>
> Si lo que querés conectar es una API que devuelve JSON con imágenes, **no hace
> falta este archivo**. En 🏷️ Fuentes elegí el tipo **API (JSON)**, pegá la URL y
> decí de qué campo sale la imagen. Hay un botón **probar ahora** que te dice qué
> trajo — o qué está mal, si algo lo está.
>
> Ejemplo completo (PokéAPI, sin credenciales):
>
> | campo | valor |
> |---|---|
> | fuentes | `pikachu, snorlax, gengar` |
> | URL | `https://pokeapi.co/api/v2/pokemon/{source}` |
> | dónde está la lista | *(vacío: la respuesta ES un solo item)* |
> | campo de la imagen | `sprites.other.official-artwork.front_default` |
> | campo del título | `name` |
>
> En la URL podés usar `{source}` (cada fuente, una por una), `{limit}` y
> `{env:MI_API_KEY}` para una clave guardada en 🔑 Credenciales. Si la API pide un
> token por header, va en *headers*: `Authorization: Bearer {env:MI_TOKEN}`.
>
> Escribí un `.py` solo si eso no alcanza: OAuth, paginado, o cuando hace falta
> una segunda llamada por item.

> ⚠️ **Esto ejecuta código.** El archivo se importa dentro del proceso del bot,
> con acceso a las credenciales y a la base. Poné acá solo código tuyo. Para
> plugins de terceros el camino es otro: un server **MCP** (corre en su propio
> proceso) declarado como conector — ver más abajo.

## Formato

```python
# connectors/lemmy.py
CONNECTOR = {
    "id": "lemmy",                       # id del tipo de fuente (único)
    "label": "Lemmy",                    # cómo se llama en la UI
    "color": "#00bc8c",                  # color del logo
    "initial": "L",                      # símbolo del logo (1-2 caracteres)
    "placeholder": "comunidad@instancia",  # ejemplo de cómo se escribe una fuente
    "tool": "get_latest_media",          # qué tool lo consume
    "help": "Comunidades de Lemmy. Trae lo último.",
    "needs_env": ("LEMMY_TOKEN",),       # opcional: credenciales que avisa si faltan
    "live": True,                        # True = trae lo último, sin indexar
}


def fetch(source: str, limit: int = 15) -> list[dict]:
    """Devolvé una lista de items. El contrato es el mismo que el de los
    conectores en vivo del motor:

        {"image_url": "https://…",   # obligatorio para postear media
         "url":       "https://…",   # link al post original (sirve para dedup)
         "title":     "…",           # texto corto, opcional
         "source":    source}        # de dónde salió
    """
    ...
```

Nada más: el motor lo carga al arrancar y la UI arma sola el botón, el selector,
el toggle y el aviso de credenciales.

## La otra vía: un server MCP como conector

Si el conector ya existe como **server MCP** (propio o de terceros), no hace falta
escribir Python: se mapea en `config/settings.json`, y el server corre en su
**propio proceso** (si se cuelga o falla, no arrastra al bot).

```json
"MCP": {
  "lemmy": {
    "transport": "stdio", "command": "python", "args": ["…/lemmy_server.py"],
    "enabled": true,
    "connector": {
      "id": "lemmy", "label": "Lemmy", "color": "#00bc8c", "initial": "L",
      "placeholder": "comunidad@instancia",
      "fetch_tool": "get_community_posts",   // tool del server que trae contenido
      "arg": "community"                      // cómo se le pasa la fuente
    }
  }
}
```

La tool tiene que devolver JSON: una lista de items, o un objeto con `items` /
`results` / `posts` / `data`. **El autor del server no tiene que hacer nada
especial** — el mapeo es config tuya, así que cualquier MCP existente que
devuelva contenido sirve.
