# Bridge de WhatsApp (T41)

WhatsApp no se puede hablar desde Python. El número entra como **dispositivo
vinculado** —el mismo mecanismo que WhatsApp Web— y eso lo resuelve
[whatsmeow](https://github.com/tulir/whatsmeow), que es Go. Este proceso es la
única pieza que habla ese protocolo; el motor le pega por HTTP en localhost.

El contrato está fijado en `core/src/channels.py` (`WhatsAppChannel`) y testeado
contra un bridge falso, así que el motor no sabe ni le importa qué hay de este
lado.

## Compilar

```bash
go build -o whatsapp-bridge.exe .
```

Usa un driver de SQLite en **Go puro**, así que no hace falta gcc ni cgo.
Compila a un binario único: quien instale Botata no necesita instalar Go.

## Vincular el número (una sola vez)

```bash
./whatsapp-bridge.exe -data ../../../bots/<instancia>/whatsapp
```

Escucha en `127.0.0.1:8899` (fuera del rango que usa la UI de config, que
arranca en 8787 y se corre al siguiente libre: los dos procesos conviven
mientras se configura el canal).

Muestra un link por consola, lo publica en `/status` y lo dibuja en `/qr.png`,
que es lo que muestra la **UI de Botata → sección Canal** cuando el canal es
`whatsapp` (ahí mismo se refresca solo mientras el QR rota). Se escanea desde el
teléfono en **WhatsApp → Ajustes → Dispositivos vinculados**. La sesión queda en
`session.db` y se reusa: no hay que volver a escanear en cada arranque.

## Configurar el canal

Con la sesión viva, la UI lista los grupos con su JID y los deja marcar con un
checkbox — el JID no se ve por ningún lado desde el teléfono. A mano es
`GET /groups`:

```bash
curl -s http://127.0.0.1:8899/groups
```

Queda en el `settings.json` de la instancia:

```json
"CHANNEL": "whatsapp",
"WHATSAPP_CHAT_IDS": ["12036300000000@g.us"],
"WHATSAPP_BRIDGE_URL": "http://127.0.0.1:8899"
```

Y **la misma lista va al bridge** con `-chats`, que es lo que hace que ni
siquiera reciba lo demás:

```bash
./whatsapp-bridge.exe -data ../../../bots/<instancia>/whatsapp \
  -chats "12036300000000@g.us"
```

Sin `-chats` el bridge procesa todos los chats del número y **baja su media a
disco** —incluidas las fotos de las conversaciones personales del dueño— para
que después el motor las descarte. La UI arma el comando con los chats ya
puestos (⚙️ → Canal); si cambiás la lista, hay que reiniciar el bridge con el
comando nuevo.

**`WHATSAPP_CHAT_IDS` no es opcional** y el motor se niega a arrancar sin él.
Todos los dispositivos vinculados de un número reciben **todos** los mensajes,
así que esa lista es lo único que evita que el bot conteste en las
conversaciones personales — y es también lo que permite compartir el número con
otro bot ya vinculado (cada uno actúa en sus chats).

## Contrato

| Endpoint | Qué hace |
|---|---|
| `GET /status` | `{connected, qr, me:{id,name}}` |
| `GET /qr.png` | el QR vigente como imagen (404 si ya está vinculado) |
| `GET /messages?after=<cursor>` | mensajes nuevos + cursor |
| `GET /messages/<id>` | releer uno (para reconstruir citas) |
| `POST /send` | `{chat_id, text, reply_to?, media_path?}` |
| `POST /profile` | `{status}` |
| `GET /groups` | grupos con su JID |

La cola de mensajes vive **en memoria**: si el bridge se reinicia, el motor
relee desde lo vivo y el dedup lo hace la DB de Botata (`has_replied`).
Persistirla sería duplicar una fuente de verdad que ya existe.

La media entrante se baja acá porque viene **cifrada** —la clave la tiene esta
sesión— y el motor solo recibe un path local.

## Seguridad

Escucha **solo en loopback** y se niega a arrancar con cualquier otra dirección:
quien llegue a este puerto puede escribir en el WhatsApp del número.
