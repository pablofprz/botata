# Canales

El grafo de decisión no sabe en qué red habla: habla con un **canal**, un contrato que hoy
implementan cuatro clases. `CHANNEL` en settings elige cuál usa la instancia.

| Canal | Estado | Límite de post | Bloqueo | Credencial |
|---|---|---|---|---|
| **Bluesky** | maduro, validado en producción | 295 | ✅ | `BSKY_PASSWORD` (app password) |
| **Mastodon** | beta (escrito, sin validar en vivo) | 490 | ✅ | `MASTODON_ACCESS_TOKEN` |
| **Discord** | beta (escrito, sin validar en vivo) | 1990 | — | `DISCORD_BOT_TOKEN` |
| **WhatsApp** | beta (bridge verificado, canal sin validar en vivo) | 4000 | — | vinculación por QR |
| **Telegram** | en el roadmap (M10) | — | — | Bot API oficial |

El truncado de posts respeta el límite **del canal activo** — un texto para Discord no se
recorta a la medida de Bluesky. En canales sin bloqueo, la tool `block_me` directamente no
se registra. El «me gusta» se traduce a lo que cada red tiene: like en Bluesky, favourite
en Mastodon, reacción ❤️ en Discord y WhatsApp.

**Instancia vs. canal:** canal nuevo de la **misma** comunidad = misma instancia (comparte
memoria); comunidad **nueva** = instancia nueva con su DB. Multi-canal simultáneo en un
mismo proceso está pospuesto.

## Bluesky

El canal de referencia. Menciones y replies, reconstrucción de hilos, feeds de lectura
(listas, feed generators, following), facets de links (Bluesky no autodetecta URLs),
tarjetas de preview con OpenGraph, bloqueo, y la consulta `/bloques` (quién bloquea al bot,
vía ClearSky).

## Mastodon

Token de aplicación con `read` + `write`. Las respuestas descuentan el `@usuario` del
límite de 500. Marcá la cuenta como bot en el perfil — buena ciudadanía fediversal.

## Discord

Bot oficial por token. Escucha una lista de canales por ID y mantiene una **ventana de
conversación** por canal (`DISCORD_CONTEXT_MESSAGES`, default 20): en un chat grupal el
contexto no es solo el hilo de replies, es la charla de la sala. Entiende attachments
(imágenes) con el modelo de visión. Las rutinas pueden postear a un canal específico
(`channel` en el frontmatter de la rutina).

## WhatsApp

La vía es **no oficial** (no existe API pública para esto): un **bridge en Go**
(`core/bridges/whatsapp/`, sobre whatsmeow, binario único sin cgo) vincula un número como
dispositivo — igual que WhatsApp Web — y le expone al motor un HTTP local en
`127.0.0.1:8899`.

Puntos importantes:

- **`WHATSAPP_CHAT_IDS` es obligatorio** y es la allowlist de grupos, aplicada de los dos
  lados (Python y Go). Sin ella el bot vería *todos* los chats del número, incluidos los
  personales.
- La vinculación es por QR desde el panel; el panel muestra los grupos disponibles con sus
  JIDs.
- Ventana de conversación (`WHATSAPP_CONTEXT_MESSAGES`), menciones por @ o por nombre,
  visión sobre imágenes citadas.
- Usá un número que puedas perder: es una vía no soportada por Meta.

## Telegram (próximo)

Bot API oficial; el *privacy mode* de Telegram calza con el modelo de menciones. Caveat de
diseño ya resuelto: no hay endpoint de historial, así que la lectura de contexto se sirve
de lo que el bot fue acumulando en su DB.
