# Memoria

Todo vive en una **SQLite local** de la instancia (`posted/botata.db`) — la única fuente de
verdad. Nada sale a servicios externos: los embeddings (`bge-m3`, multilingüe, bueno en
rioplatense) corren en tu CPU.

## Qué recuerda

- **Hechos por usuario** — cosas **autorreveladas**: lo que la gente cuenta de sí misma
  hablando con el bot o en el feed de la comunidad. Solo de usuarios que ya tienen perfil
  (no acumula datos de desconocidos).
- **Notas de conversación** — un resumen por charla, para retomar el hilo con cada persona.
- **Lecciones de conducta** — lo que el bot aprende de su propia actividad («cuando hago X,
  la comunidad reacciona Y»).
- **Memoria general** — la historia y el lore del bot, que entra entera en cada respuesta.
- **Eventos** — el calendario compartido (cumpleaños, juntadas).

## Cómo busca: híbrida, local, particionada

Cada búsqueda corre dos rankings en paralelo y los fusiona (RRF): **semántico** (KNN por
embeddings, sqlite-vec) y **keyword** (BM25, FTS5) — el keyword rescata lo que el semántico
pierde: handles, nombres propios, términos exactos. Los hechos están **particionados por
usuario**: la búsqueda de una persona jamás se contamina con hechos de otra.

La escritura tiene **dedup semántico**: antes de insertar un hecho se busca el vecino más
cercano; si es casi idéntico, no se duplica. Eso hace idempotente todo el aprendizaje —
releer el mismo feed no duplica memoria.

## Los 📌 (hechos fijados)

Lo que alguien **pidió** que el bot recuerde («acordate de que soy de Racing») se fija:
entra en todas las respuestas a esa persona sin competir con la búsqueda, y **jamás se
descarta** (a lo sumo se fusiona con un duplicado, heredando el pin). Fijar es una decisión
— el acto de pedir — no un efecto de cargar datos.

## Compactación: crecer sin degradarse

Tres pases automáticos, cada uno con su forma:

- **Memoria general** — se compacta por **tamaño** (es la que gasta contexto en cada
  llamada): fusiona duplicados, resuelve contradicciones (gana lo más reciente), descarta
  lo efímero.
- **Notas de conversación** — se comprimen a una nota por (usuario, día): la ventana de
  contexto pasa de cinco ángulos de la misma mañana a cinco *días* de relación.
- **Hechos por usuario** — fusiona lo que dice lo mismo con semanas de diferencia, para que
  la búsqueda no gaste sus lugares en duplicados.

Invariantes que no se negocian: el **código verifica** cada plan del LLM contra la base
(todo-o-nada), **nada se borra** (archivado con undo), y lo 📌 es intocable.

## Los derechos de los usuarios

Cualquier persona, sin permiso de nadie:

- **`resetme`** — borra todo lo que el bot sabe de ella (hechos, embeddings, eventos).
- **`blockme`** — además la bloquea, en los canales que soportan bloqueo.
- **«olvidate de que...»** — borra un dato puntual (`forget_about_me`).
- Puede preguntar qué sabe el bot de ella y de dónde lo sacó.
