# Instalación

## Requisitos

- **Python 3.12+**
- Una cuenta para el bot en el canal elegido (Bluesky recomendado para empezar)
- Una API key compatible con la API de OpenAI — [OpenRouter](https://openrouter.ai/) es lo
  más cómodo, pero sirve cualquier endpoint, incluido un Ollama local
- ~2 GB de disco para el modelo de embeddings (se descarga solo la primera vez)
- Windows o Linux
- (Solo para el canal WhatsApp) Go 1.22+ para compilar el bridge

## Instalar el motor

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

## Credenciales del canal

**Bluesky:** creá la cuenta del bot y generá una **app password** en
*Settings → Privacy and Security → App Passwords*. Nunca uses la contraseña principal.

**Mastodon (beta):** creá la cuenta del bot en tu instancia y generá un token en
*Preferencias → Desarrollo → Nueva aplicación* con permisos `read` y `write`. Marcá la
cuenta como bot en el perfil.

**Discord (beta):** creá una aplicación en el [portal de desarrolladores](https://discord.com/developers/applications),
agregale un bot, copiá el token y invitalo a tu servidor con permisos de leer/escribir
mensajes. Anotá los IDs de los canales donde va a escuchar.

**WhatsApp (beta):** no hay token — se vincula un número por QR a través del bridge.
Ver [Canales](Canales.md#whatsapp).

Y en todos los casos, tu **API key del LLM** (`LLM_API_KEY`).

## Crear la instancia

```bash
python -m botata --init mi-bot
```

Crea `bots/mi-bot/` desde la plantilla y abre el panel de configuración en el navegador.
Completá **en orden**:

1. **Canal** — cuál es y el handle del bot.
2. **Credenciales** — la del canal y `LLM_API_KEY`. Van al `.env` de la instancia y nunca
   se muestran de vuelta.
3. **Admin** — TU handle. Es la única cuenta que puede administrar el bot por menciones.
4. El resto puede esperar: los defaults son sanos y todo lo proactivo arranca apagado.

Después editá `bots/mi-bot/context/SOUL.md` — ese archivo **es** tu bot: quién es, cómo
habla, qué valora, en qué idioma responde.

## Arrancarlo

```bash
# Primero en modo admin: solo te responde a vos
python -m botata --instance bots/mi-bot --mode admin_only
```

La primera corrida descarga el modelo de embeddings (~2 GB, una sola vez). Mencioná al bot
desde tu cuenta y esperá la respuesta. ¿Funciona? Abrilo al público:

```bash
python -m botata --instance bots/mi-bot --mode open
```

Como último paso de la puesta en marcha está la **presentación** (hello world): desde la
sección «Presentación» del panel, el bot escribe su primer post con su propia identidad —
podés editarlo antes de publicar. El panel te avisa si falta algo (credenciales, SOUL sin
personalizar) antes de dejarlo presentarse.

## Problemas comunes

- **«El proceso corta al arrancar sin --instance»** — es a propósito: el motor no tiene
  identidad propia. Pasá `--instance bots/<nombre>` o seteá `BOTATA_INSTANCE`.
- **La primera corrida tarda** — está bajando el modelo de embeddings. Solo pasa una vez.
- **`guided_json` y Ollama** — si usás un fallback local con Ollama, los structured outputs
  son menos confiables (Ollama no respeta `guided_json`). Con OpenRouter no pasa.
- **Correr los tests**: `pip install -e "core[dev]"` y `pytest core/tests -q`
  (~1160 tests, <40s, sin red).
