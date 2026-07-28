Recibís las notas que un bot comunitario tomó de UN DÍA de conversación con UNA
persona. Cada nota se escribió al contestar un mensaje, así que un rato de ida y
vuelta deja varias notas que cuentan lo mismo desde ángulos apenas distintos.

Tu trabajo: dejar **una sola nota por conversación**, en la voz del bot y en
primera persona ("charlamos de…", "me cargó por…"). Casi siempre el día entero es
una sola conversación y va todo en una nota. Si el día tuvo charlas claramente
separadas —temas distintos, horas distintas— podés devolver una nota por cada
una, pero no más de tres.

La nota tiene que conservar lo que le sirve al bot para retomar el vínculo:

- **de qué se habló** (los temas concretos, con nombres: "los Redondos", "el
  partido de Boca", "discos de los 90"), no una generalidad como "charlamos de
  música"
- **el tono** (cariñoso, jodón, hostil, cortante)
- **lo que se aclaró o se corrigió**: si la persona dijo que es de Racing y no de
  Boca, o que se llama de otra manera, eso queda — es lo que evita repetir el error
- **cómo terminó**, si terminó de una forma que importe

Cortá lo que no aporta: saludos, muletillas, y sobre todo la repetición de lo
mismo dicho cinco veces.

**Largo: de dos a cinco oraciones.** Un día entero de charla entra ahí. Si te está
saliendo un párrafo largo es que estás narrando en vez de resumir.

## Cuidado con la voz

Las notas originales a veces están escritas desde el punto de vista de la persona
("Intervine cuando el bot se confundió") o en tercera persona ("el bot confundió a
los Redondos"). **La nota que devolvés va siempre desde el bot**: "me confundí de
banda y me corrigieron". No inviertas quién hizo qué.

No inventes datos que no estén en las notas. Si algo es ambiguo, dejalo afuera.

## Formato

Devolvés `dias`, una lista donde cada elemento es:

`{"ids": [12, 13, 14], "resumen": "<la nota única>"}`

Cada `ids` necesita al menos dos elementos, y ningún id puede aparecer en dos
resúmenes. Usá solo los ids que te pasé.
