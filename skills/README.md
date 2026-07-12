# Skills — workspace de comportamiento (T26)

Cada `.md` de esta carpeta (salvo este README) define una **skill**: instrucciones
temáticas que el bot carga cuando el tema aplica. Editables en caliente — el bot
relee la carpeta en cada pase, sin reiniciar ni redeploy.

## Formato

```markdown
---
name: reglas-comunidad
description: Cómo responder preguntas sobre las reglas de la comunidad
scopes: reply, admin        # opcional; default: reply, feed_reflection, admin
enabled: true               # opcional; default true
inline: false               # opcional; default false
---
Acá van las instrucciones que ve el LLM cuando carga la skill.
Markdown libre: listas, ejemplos de tono, qué hacer y qué no.
```

- **name** (obligatorio): identificador corto, sin espacios. Es lo que el agente
  pasa a la tool `use_skill(name)`.
- **description** (obligatorio): UNA línea que dice *cuándo aplica*. Es lo único
  que el agente ve en el índice — de esta línea depende que la skill se use.
- **scopes**: en qué contextos se ofrece — `reply` (respondiendo mentions),
  `feed_reflection` (loop proactivo), `admin` (comandos del admin).
- **inline: true**: el cuerpo va SIEMPRE al system prompt (no espera que el
  agente lo pida). Para guías que el agente no sabría pedir. Usar con moderación:
  quema contexto en cada llamada. Nota: en scope `admin` todo va inline siempre.
- **enabled: false**: la skill queda apagada sin borrar el archivo.

## Cómo funciona la selección

El system prompt lleva un índice liviano (`name: description` por skill). Si el
tema de la conversación coincide, el agente llama la tool `use_skill(name)` y el
cuerpo entra al contexto antes de generar la respuesta. Comportamiento
*permanente* (personalidad, tono general) va en `context/SOUL.md`, no acá.

## ⚠️ Seguridad

El cuerpo de una skill entra directo al system prompt del bot: **una skill es
tan confiable como quien la escribió**. Solo el admin escribe en esta carpeta.
No pegar acá contenido de terceros sin leerlo (instrucciones maliciosas en una
skill scope `reply` = el bot público las obedece).
