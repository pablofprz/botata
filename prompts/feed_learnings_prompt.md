Te paso posts recientes de un feed de Bluesky, cada uno con el handle de su autor.
Tu tarea es extraer **aprendizajes durables** para la memoria del bot. Devolvés JSON.

Extraé dos cosas:

1. **Hechos de usuarios** (`facts`): algo que un usuario **reveló de sí mismo** y que vale
   la pena recordar a futuro (dónde vive, a qué se dedica, gustos fuertes, un proyecto,
   una mascota, etc.). El `handle` es el del autor del post. El `fact` es una frase corta
   en tercera persona (ej. "Vive en Rosario", "Tiene un gato llamado Mishi").

2. **Eventos** (`events`): algo con **fecha** concreta (cumpleaños, un evento, una juntada,
   un estreno). `event_at` en ISO 8601 (`YYYY-MM-DD` o `YYYY-MM-DDTHH:MM`). Resolvé fechas
   relativas ("hoy", "mañana", "el viernes") usando la fecha actual del contexto.
   `handle` = el usuario dueño del evento si aplica (ej. su cumple), o null si es de la
   comunidad. `kind`: `birthday` | `reminder` | `community` | `other`.

# REGLAS
- Solo hechos **autorrevelados** por el autor del post, no inferencias ni chismes de terceros.
- Nada de temas sensibles (salud grave, política delicada, cosas íntimas o pesadas): salteá.
- Nada efímero ("tengo sueño", "qué calor") — solo lo que sirva recordar.
- Si no hay nada que valga la pena, devolvé listas vacías: `{"facts": [], "events": []}`.
- No inventes. Ante la duda, no lo agregues.

Formato de salida (JSON):
{"facts": [{"handle": "...", "fact": "..."}],
 "events": [{"title": "...", "event_at": "YYYY-MM-DD", "handle": null, "kind": "other", "description": null}]}
