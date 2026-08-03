# Personalidad

Toda la conducta del bot vive en archivos markdown de la instancia, en cuatro capas que se
componen. Ninguna está hardcodeada.

## SOUL.md — quién es

`context/SOUL.md` es la identidad: quién es el bot, cómo habla, qué valora, en qué idioma
responde. Entra en **todas** las llamadas que producen texto hacia afuera. La plantilla trae
la estructura con instrucciones; reemplazá los huecos y tenés un bot.

## Skills — cómo comportarse ante un tema

`skills/*.md`: comportamiento **temático**. Cada skill tiene frontmatter (`name`,
`description`, `scopes`, `enabled`, `inline`) y un cuerpo en prosa. La selección es
agéntica y barata: el bot ve solo un índice (`name: description`); si el tema de la
conversación coincide, pide el cuerpo con la tool `use_skill` y recién ahí entra al
contexto. `inline: true` fuerza el cuerpo siempre al prompt.

La distinción con el SOUL: **SOUL = quién es, siempre. Skill = qué hacer cuando se habla
de X.** Se editan en caliente — guardás el archivo y el próximo pase ya lo usa.

⚠️ Solo el admin escribe en `skills/`: el cuerpo entra al system prompt de un bot público.

## Moods — el humor del día

`moods/*.md`: estados de ánimo que tiñen el tono sin reemplazar la identidad. Tres modos en
`settings.MOODS`:

- **Apagado** — sin humor.
- **Fijo** (`manual.fixed`) — siempre el mismo.
- **Automático** — el bot elige su humor una vez al día leyendo el clima de la comunidad
  (sale del mismo pase de lectura del feed), con **susceptibilidad** (cuánto lo afecta el
  clima) e **histéresis** (no cambia de humor por cualquier brisa) configurables.

Un mood puede además disparar otras conductas: la rutina de bio típica actualiza la
biografía del perfil cuando el humor cambió.

## Rutinas — qué hace por iniciativa propia

`routines/*.md`: TODA la conducta proactiva con cadencia. Una rutina = un archivo con
frontmatter (`interval_hours`, `enabled`, `channel` opcional) y un cuerpo en prosa libre:
qué hacer y con qué actitud. Ejemplos reales: compartir un tema musical cada tantas horas,
opinar de las noticias a la mañana, actualizar la bio cuando cambia el humor.

Claves de diseño:

- La **cadencia es determinística** (cursor en DB): el timing jamás se delega al LLM.
  `interval_hours` es "como máximo cada X horas" — la rutina puede decidir **callarse**,
  y callar es digno.
- El cuerpo se relee en cada pase → edición en caliente.
- Con `channel`, la rutina postea en un canal específico (p. ej. un canal de Discord) y su
  cuerpo además matiza las respuestas del bot en ese canal.
- El admin las crea y modifica **por mención** («armate una rutina que...») vía las tools
  `set_routine` / `delete_routine`, o desde el panel (sección ⏰ Rutinas).

## Cómo se compone el prompt final

Para una respuesta, el bot arma: SOUL + fecha/hora local + memoria general + hechos de la
persona (por búsqueda) + lecciones de conducta + mood del día + skills relevantes + el hilo
de la conversación. Para un pase proactivo: SOUL + mood + el cuerpo de la rutina + contexto
del feed/canal + invariantes del motor (anti-spam, anti-alucinación de eventos, formato).
