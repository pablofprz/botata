# moods/ — estados de ánimo del bot

Cada `*.md` es un **mood**: un registro afectivo que tiñe cómo responde el bot
durante un día (más pila, más bajón, filoso, arisco…). Es transversal a todo lo
que postea ese día (replies + proactivo), a diferencia de:

- **SOUL.md** → personalidad permanente (no cambia).
- **skills/** → conocimiento temático que se carga cuando aplica.

## Formato

```markdown
---
name: gloomy
description: Melancholic and terse — short replies, no mood for jokes
enabled: true          # default true; ponelo false para archivar sin borrar
---
(instrucciones de tono que ve el LLM cuando este mood está activo)
```

Los `name` y las claves de config van en **inglés** a propósito: el core es
portable, no atado al español. El cuerpo (el tono) escribilo en el idioma que
prefieras — el bot igual responde en su idioma (lo dicta SOUL.md, no el mood).

El `body` se inyecta al system prompt como "tu estado de ánimo HOY es …". Escribí
tono, NO personalidad nueva: el mood modula, no reemplaza a SOUL.md.

## Cómo se activa

Se configura en `settings.json` → sección `MOODS`:

```json
"MOODS": {
  "enabled": false,              // master switch (default off = comportamiento normal)
  "mode": "manual",              // "manual" | "auto"
  "manual": {
    "fixed": "",                 // un name de acá → siempre ese mood (pisa el schedule)
    "schedule": {                // si fixed está vacío: día de semana (mon..sun) → mood
      "mon": "upbeat", "fri": "snarky", "sun": "gloomy"
    }
  }
}
```

- **manual + fixed** → siempre ese humor.
- **manual + schedule** → el humor del día (`mon`..`sun`); día sin mapear = sin mood.
- **auto** → el bot elige UNA vez por día leyendo el clima de la comunidad + su
  propia actividad reciente, y guarda el porqué (tabla `kv`, visible en
  `get_bot_config`). Reacciona: si lo tratan mal, puede caer en `gloomy`/`prickly`/`angry`.

## Seguridad

El `body` entra al system prompt del bot → **solo el admin escribe acá** (mismo
criterio que `skills/`). Un mood es una instrucción de comportamiento, no un dato.

## Editar en caliente

Los archivos se releen en cada pase: editar un `.md` cambia el tono sin reiniciar.
Cambiar la sección `MOODS` de `settings.json` sí requiere reiniciar el bot.
