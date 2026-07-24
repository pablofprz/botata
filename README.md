<p align="center">
  <img src="botata.png" alt="Botata" width="340">
</p>

<h1 align="center">Botata</h1>

<p align="center">
  <b>Un bot comunitario con alma de agente, para Bluesky y próximamente Mastodon, Discord y Telegram.</b><br>
  Integración de memoria general y de usuarios, tools, skills, calendario, herramientas de personalidad (moods, likes, dislikes)   
  Conectá una Key compatible con API Open AI, crea una cuenta y comenzá a setearlo para tu comunidad definiendo admin y grupos de usuarios con permisos personalizados. 

    
</p>

---

## ¿Qué es Botata?

Botata es un bot para comunidades en redes sociales. Vive en una cuenta de **Bluesky** (próximamente nuevas redes), responde cuando lo mencionan, lee el feed de su comunidad, aprende de la gente con la que habla y decide por sí mismo cuándo tiene algo que decir.

Es **genérico y multi-comunidad**: la personalidad, el idioma, las fuentes y las herramientas
son configuración, no código. El mismo motor puede correr un bot de memes, uno
de observación de aves o el asistente de un club de software libre — cada uno es una
**instancia** con su propia identidad y su propia memoria.

### Lo que trae

- 🧠 **Memoria real por usuario** — hechos autorrevelados, historial de conversaciones y
  lecciones de comportamiento, con búsqueda híbrida (embeddings locales + keywords) en SQLite.
  Sin servicios externos: los embeddings (`bge-m3`) corren en tu CPU.
- 🕊️ **Multi-canal** —  con el mismo motor (Mastodon, Discord y Telegram en el roadmap).
- 🛠️ **Herramientas** — búsqueda web, música (Spotify), videos (YouTube), calendario,
  noticias RSS, imágenes, y cualquier server **MCP** externo como tool extra.
- 🎭 **Personalidad en archivos** — `SOUL.md` (quién es), `skills/` (cómo responder sobre
  temas específicos, editable en caliente), `moods/` (estados de ánimo diarios que tiñen el
  tono), gustos y disgustos editables.
- 📅 **Proactividad** — lee el feed y decide si opinar, saluda cumpleaños, comparte música,
  reflexiona en público. Todo con toggles, todo apagado por default.
- 🔒 **Pensado para bots públicos** — scopes por herramienta, grupos de usuarios, config
  protegida contra prompt injection, presupuesto diario de tokens.
- 🖥️ **Panel de configuración local** — todo se configura desde el navegador, sin editar JSON.

---

## Requisitos

- **Python 3.12+**
- Una cuenta para el bot en **Bluesky** 
- Una API key de **[OpenRouter](https://openrouter.ai/)** (o cualquier endpoint compatible
  con la API de OpenAI, incluido un Ollama local)
- ~2 GB de disco para el modelo de embeddings (se descarga solo la primera vez)
- Funciona en Windows y Linux

---

## Tutorial: tu primer bot en 10 minutos

### 1. Instalá el motor

```bash
git clone https://github.com/pablofprz/botata.git
cd botata
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -e core
```

### 2. Conseguí las credenciales del canal

**Bluesky:** creá la cuenta del bot y generá una **app password** en
*Settings → Privacy and Security → App Passwords* (nunca uses la contraseña principal).
También funciona con credenciales de cuenta normal, pero no es recomendado. 

**Mastodon (soon):** creá la cuenta del bot en tu instancia y generá un token en
*Preferencias → Desarrollo → Nueva aplicación*, con permisos `read` y `write`.
Marcá la cuenta como bot en el perfil (buena ciudadanía fediversal).

Y tu API key compatible con OpenAI API, recomiendo usar OpenRouter u otro enrutador para probar distintos modelos, [openrouter.ai/keys](https://openrouter.ai/settings/keys).

> **¿Otro proveedor de LLM?** Botata habla con cualquier API OpenAI-compatible (OpenAI,
> Groq, DeepSeek, un Ollama local...). Cargá la key como `LLM_API_KEY`, apuntá
> `OPENAI_ENDPOINT` (o la sección `MODELS`, que soporta varios endpoints con fallback) a tu
> proveedor, y elegí modelos que existan ahí. Único límite: el presupuesto diario de tokens
> mide gasto contra la API de OpenRouter — con otro proveedor dejalo apagado (su default).

### 3. Creá tu instancia

```bash
python -m botata --init mi-bot
```

Esto crea la carpeta `bots/mi-bot/` con toda la configuración de plantilla y abre el panel
de configuración en el navegador. Completá en orden:

1. **Canal** — `bluesky` o `mastodon` (+ URL de tu instancia si es Mastodon) y el handle
   del bot.
2. **Credenciales** — `BSKY_PASSWORD` (la app password) o `MASTODON_ACCESS_TOKEN` (el
   token), y `LLM_API_KEY`. Se guardan en el `.env` de tu instancia, nunca se
   muestran de vuelta.
3. **Admin** — TU handle. El admin controla el bot por menciones y es la única cuenta que
   puede usar los comandos de administración. En Mastodon: si tu cuenta está en la misma
   instancia que el bot es `usuario` a secas; si está en otra, `usuario@dominio`.
4. El resto (grupos, feeds, herramientas, moods...) puede esperar — los defaults son sanos
   y todo lo proactivo arranca apagado.

Cerrá la UI con Ctrl+C cuando termines.

### 4. Dale una personalidad

Editá `bots/mi-bot/context/SOUL.md`. Ese archivo **es** tu bot: quién es, cómo habla, qué
valora, en qué idioma responde. La plantilla trae la estructura con instrucciones — reemplazá
los huecos. No hace falta tocar nada más para empezar (skills y moods son capas opcionales
que podés sumar después).

### 5. Arrancalo

```bash
# Primero en modo admin: solo te responde a vos
python -m botata --instance bots/mi-bot --mode admin_only
```

La primera corrida descarga el modelo de embeddings (~2 GB, una sola vez). Cuando veas el
loop corriendo, **mencioná al bot desde tu cuenta** y esperá la respuesta.

¿Funciona? Abrilo al público:

```bash
python -m botata --instance bots/mi-bot --mode open
```

### 6. Manejalo desde la red

Como admin podés hablarle al bot para administrarlo, en lenguaje natural:

> «@mi-bot ¿qué tenés apagado?» · «prendé el heartbeat cada 6 horas» ·
> «acordate de que a Ana le gusta el jazz» · «poné modo snarky»

Y cualquier usuario puede pedirle cosas normales: opinar, buscar, resumir el feed,
agendar su cumpleaños, o pedirle `resetme` para que borre todo lo que sabe de él.

### 7. Cuando quieras más

Volvé a abrir el panel (`python -m botata --init mi-bot`) y prendé de a uno:

- **Feeds proactivos** — el bot lee el feed de la comunidad y decide si comentar.
- **Heartbeat** — pase periódico con instrucciones tuyas («compartí un tema de jazz cada
  tanto»).
- **Moods** — estados de ánimo diarios (automáticos o por agenda).
- **Noticias RSS**, **presupuesto diario de tokens**, **servers MCP**, **skills** temáticas.

---

## Conceptos en 30 segundos

**Motor vs. instancia.** El repo es el motor (carpeta `core/`); tu bot es una instancia
(carpeta `bots/<nombre>/`): su config, su `.env`, su SOUL, sus prompts y su base de datos.
Podés correr N bots de N comunidades con el mismo motor — cada uno con su memoria separada.

**Todo es un archivo.** Prompts, personalidad, skills y moods son markdown dentro de tu
instancia. Editás el archivo, el bot cambia. Nada de personalidad hardcodeada.

**El LLM decide, la config limita.** Cada herramienta tiene un scope (¿la puede usar
cualquiera o solo el admin?) y opcionalmente un grupo de usuarios. Los settings críticos no
se pueden cambiar por mención, ni siquiera del admin — solo desde el panel local.

## Estructura del repo

```
core/                   ← el motor: src/ (código), tests/, ui/ (panel), mcp_servers/,
                          instance_template/ (la plantilla de toda instancia nueva)
bots/                   ← tus instancias (nunca se suben: identidad + datos privados)
docs/ARQUITECTURA.md    ← cómo funciona por dentro, módulo por módulo
```

## Tests

```bash
pip install -e "core[dev]"
pytest core/tests -q        # ~430 tests, <15s, sin red
```

## Documentación

- [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) — el código por dentro, módulo por módulo.
- [`ROADMAP.md`](ROADMAP.md) — qué está hecho y qué viene.
- Wiki con guías detalladas: próximamente.

## Licencia

[GPL-3.0](core/LICENSE)
