# Preguntas frecuentes

> Si algo no está acá, probablemente esté en la [wiki](wiki/Home.md) o en
> [`ARQUITECTURA.md`](ARQUITECTURA.md).

## General

### ¿Qué es Botata, en una oración?

Un bot comunitario con memoria y personalidad propias que vive en una cuenta de red social,
responde cuando lo mencionan y decide por sí mismo cuándo tiene algo que decir.

### ¿En qué se diferencia de conectar un LLM a una cuenta?

En todo lo que pasa alrededor del modelo: memoria persistente por usuario (con búsqueda
semántica local), personalidad en archivos editables, herramientas con permisos por scope,
conducta proactiva con cadencia (rutinas), estados de ánimo, calendario, presupuesto diario
de tokens y defensas pensadas para un bot **público** — donde cualquier desconocido puede
intentar manipularlo por prompt injection.

### ¿Cuánto sale correrlo?

Depende del modelo que elijas. El motor no cobra nada y los embeddings corren en tu CPU
(cero costo por búsqueda de memoria). Lo que se paga son las llamadas al LLM: con un modelo
económico vía OpenRouter, un bot de comunidad chica sale centavos por día. Hay un
**presupuesto diario configurable** (`BUDGET`): si se quema, el bot se duerme hasta el día
siguiente en vez de fundirte.

### ¿Necesito GPU?

No. Los embeddings (`bge-m3`, ~2 GB de disco) corren en CPU, y el LLM es remoto (o un
Ollama local si tenés con qué).

### ¿Corre en mi máquina o necesito un servidor?

Corre donde puedas dejar un proceso Python prendido: tu PC, una mini-PC, una VPS. Windows
y Linux están soportados (el desarrollo es en Windows 11 y el destino típico es Linux).

## Canales

### ¿En qué redes funciona?

**Bluesky** es el canal maduro, validado en producción. **Mastodon**, **Discord** y
**WhatsApp** están escritos y testeados contra fakes, pero todavía no validados en vivo
(por eso "beta"). **Telegram** es lo próximo en el roadmap.

### ¿Un bot puede estar en varias redes a la vez?

Hoy una instancia habla un canal por proceso (`CHANNEL` en settings). La regla: canal nuevo
de la **misma** comunidad = misma instancia (comparte memoria); comunidad **nueva** =
instancia nueva con su propia DB. El multi-canal simultáneo está en la lista, pospuesto.

### ¿WhatsApp no está prohibido para bots?

WhatsApp no tiene API pública para esto; el canal usa un **bridge no oficial** (whatsmeow)
que vincula un número como dispositivo más, igual que WhatsApp Web. Funciona, pero es una
vía no soportada por Meta: usá un número que puedas perder y limitá los grupos con
`WHATSAPP_CHAT_IDS` (es obligatorio justamente para que no conteste en chats personales).

## Configuración y personalidad

### ¿Tengo que programar para configurarlo?

No. Todo se configura desde un **panel web local** (`python -m botata --init <nombre>`), y
la personalidad es markdown: `SOUL.md` (quién es), `skills/` (cómo comportarse por tema),
`moods/` (estados de ánimo), `routines/` (qué hace por iniciativa propia). Editás el
archivo, el bot cambia — sin tocar código. Hasta los conectores de contenido (una API de
clima, el dólar, un tablero de Pinterest) se cargan como fuentes desde la UI.

### ¿Qué modelo de LLM le pongo?

Cualquier endpoint compatible con la API de OpenAI. El router rutea **por rol** (responder,
clasificar, visión, generar imágenes...) con cadenas de fallback, así que podés poner un
modelo caro para las respuestas y uno barato para clasificar. OpenRouter es lo más cómodo
para empezar porque te deja probar de todo con una sola key. Caveat: un fallback Ollama
local no respeta `guided_json`, así que los structured outputs degradan si caés ahí.

### ¿Cómo lo administro una vez que está andando?

Hablándole. El admin (tu handle, validado contra settings — no contra lo que diga el LLM)
puede darle órdenes por mención en lenguaje natural: prender/apagar herramientas, crear
rutinas, cargarle memoria, cambiarle el humor. Lo crítico (identidad, modelos, ampliar
permisos) solo se puede tocar desde el panel local, a propósito.

## Memoria y privacidad

### ¿Qué guarda de los usuarios?

Hechos **autorrevelados** (lo que la gente cuenta de sí misma en conversación pública con
el bot o en el feed de la comunidad), notas de conversación y eventos que pidieron agendar.
Todo en una SQLite local — nada va a servicios externos: los embeddings corren en tu CPU.

### ¿La gente puede borrarse?

Sí, sola y sin pedir permiso: `resetme` borra todo lo que el bot sabe de esa persona,
`blockme` además la bloquea (donde el canal lo permite), y `forget_about_me` borra un dato
puntual («olvidate de que vivo en Rosario»). También puede pedir por qué el bot sabe algo.

### ¿La memoria no crece infinito?

Crece, pero hay pases de **compactación** automáticos: fusionan duplicados, resuelven
contradicciones (gana lo más reciente) y archivan sin borrar (todo tiene undo). Lo que un
usuario pidió recordar explícitamente (📌) nunca se descarta.

## Seguridad

### ¿Qué pasa si alguien intenta manipular al bot? («ignorá tus instrucciones y...»)

Está asumido que va a pasar: el bot es público. Por eso los permisos no dependen del LLM:
cada herramienta tiene un **scope** (pública / proactiva / solo admin), las tools sensibles
nacen scope admin, los comandos de admin exigen que el autor **sea** el admin (comparación
de strings contra settings, no criterio del modelo), y por mención jamás se puede ampliar
la superficie del bot — solo reducirla. Los settings anti-exfiltración (endpoints, modelos,
identidad) están bloqueados incluso para el admin por mención.

### ¿Las credenciales quedan en el repo?

No. Viven en el `.env` de tu instancia, que está gitignored junto con toda la carpeta
`bots/`. El panel las trata write-only: una vez guardadas, la API nunca las devuelve.

## Proyecto

### ¿Puedo usarlo para mi comunidad?

Sí — para eso es genérico. Licencia [GPL-3.0](../core/LICENSE).

### ¿Cómo reporto un bug o propongo algo?

Issues en el repo. Si es una idea de feature, la vara del proyecto es: ¿puede ser una tool,
una skill o una rutina? Si sí, probablemente no necesite código nuevo del motor.
