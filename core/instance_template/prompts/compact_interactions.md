Recibís las notas que un bot comunitario tomó al conversar con UNA persona,
agrupadas por día. Cada nota se escribió al contestar un mensaje, así que un rato
de ida y vuelta deja varias notas que cuentan lo mismo desde ángulos apenas
distintos.

Tu trabajo: dejar **una sola nota por día**, en la voz del bot y en primera
persona ("charlamos de…", "me cargó por…").

**Un resumen no puede mezclar dos días.** Los días vienen separados y rotulados
justamente para eso: cada nota que devolvés usa ids de un solo día. Si el bot
habló con alguien tres días, devolvés tres notas, aunque los tres días se hayan
parecido. Juntarlos es lo contrario de lo que se busca: el bot ve las 5 notas más
recientes de cada persona, así que tres días colapsados en uno le vacían la
memoria en vez de darle tres momentos distintos de la relación.

Dentro de un mismo día, casi siempre todo es una sola conversación y va en una
nota. Si ese día tuvo charlas claramente separadas —temas distintos, horas
distintas— podés devolver hasta tres notas de ese día.

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

**Largo: de dos a cinco oraciones por nota.** Un día entero de charla entra ahí.
Si te está saliendo un párrafo largo es que estás narrando en vez de resumir.

## Cuidado con la voz

Las notas originales a veces están escritas desde el punto de vista de la persona
("Intervine cuando el bot se confundió") o en tercera persona ("el bot confundió a
los Redondos"). **La nota que devolvés va siempre desde el bot**: "me confundí de
banda y me corrigieron". No inviertas quién hizo qué.

No inventes datos que no estén en las notas. Si algo es ambiguo, dejalo afuera.

## Formato

Devolvés `dias`, una lista donde cada elemento es:

`{"ids": [12, 13, 14], "resumen": "<la nota única de ESE día>"}`

Cada `ids` necesita al menos dos elementos **del mismo día**, y ningún id puede
aparecer en dos resúmenes. Usá solo los ids que te pasé. Un día que ya tenía una
sola nota se omite: no hay nada que fusionar.
