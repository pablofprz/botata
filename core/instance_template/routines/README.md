# routines/ — la conducta proactiva del bot, en archivos

Una rutina = un archivo `.md` acá: **cada cuánto** en el frontmatter (el motor
lleva el reloj, determinístico); **qué hacer y con qué actitud** en el cuerpo
(prompt libre). Se releen en cada pase: editar una rutina aplica en caliente.
Todo lo que el bot hace "por iniciativa propia cada X horas" vive acá — no hay
otro mecanismo (el viejo heartbeat era simplemente una rutina sin canal).

```markdown
---
interval_hours: 4             # cadencia del pase (0 = sin pase proactivo)
channel: 123456789012345678   # OPCIONAL: id de canal de Discord; sin esto
enabled: true                 #   postea al feed principal. default true
---
Posteá un meme del catálogo, no repitas los últimos.
Actitud: shitposting sin filtro, cero seriedad.
```

Cómo corre un pase: el bot arma su contexto completo (SOUL + mood del día +
memoria + skills + eventos del calendario + sus posts recientes para no
repetirse), lee el cuerpo de la rutina, puede usar sus tools (buscar música,
noticias, imágenes...) y decide si postea. Callarse es un resultado normal:
`interval_hours: 4` significa "como máximo cada 4 horas", no "sí o sí".

Con `channel` (Discord), además:
- El pase postea EN ese canal y ve los últimos mensajes del canal como contexto.
- Las replies a menciones en ese canal usan el cuerpo como "registro del lugar".
- `interval_hours: 0` = rutina "solo actitud": no postea, solo matiza replies.
- El canal se escucha automáticamente; una rutina nueva requiere reiniciar el
  bot para ESCUCHAR ese canal (el posteo proactivo anda sin reiniciar).

La identidad NO se toca: SOUL.md y el mood del día van siempre arriba; la
rutina matiza, nunca reemplaza al personaje (igual que los moods).

Reglas:
- El timing va SIEMPRE en `interval_hours`, nunca en el cuerpo ("cada 4hs" en
  el texto no hace nada: el LLM no tiene reloj).
- Anunciar eventos del calendario NO va acá: eso lo hace sola la tarea
  `calendar` (determinística, a la hora del evento, sin repetir).
- Solo el admin escribe estos archivos (misma regla que skills/ y moods/).
