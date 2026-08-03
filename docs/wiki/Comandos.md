# Comandos

La filosofía primero: **lo que falta casi nunca es un comando, es una capacidad.** Casi
todo se le pide al bot hablando («¿qué sabés de mí?», «buscame un tema», «olvidate de que
vivo en Rosario») y el modelo resuelve con sus tools. Las barras existen solo para lo
**preciso e irreversible** o para desambiguar.

## Para cualquier usuario

| Comando | Qué hace |
|---|---|
| `/stop` · `/start` | suelta **este hilo**: silencio total ahí (incluso para el admin) hasta que alguien mande `/start`. Literal a propósito — el bot no interpreta «ya está, basta» como un stop. |
| `/schedule` · `/agendar` | atajo al calendario. Interpretado: «agendá el cumple de Ana el 15/8» funciona igual sin barra. |
| `/agenda` · `/eventos` | qué se viene en el calendario |
| `/bloques` | quiénes bloquean al bot (Bluesky, vía ClearSky) |
| `/check-role` | qué permisos tenés vos ante el bot |
| `resetme` | borra **todo** lo que el bot sabe de vos |
| `blockme` | resetme + bloqueo mutuo (donde el canal lo permite) |

Y hablando: pedirle que recuerde algo tuyo, que olvide un dato puntual, que busque, que
resuma el feed, que recomiende música, que opine.

## Para el admin

Los comandos deterministas de operación:

| Comando | Qué hace |
|---|---|
| `/sleep` · `/wake` | duerme/despierta al bot **entero**: no responde a no-admins ni corre tareas proactivas. Funciona incluso con el LLM caído o el presupuesto quemado. |
| `/remember ...` | guarda un hecho fijado (📌) sobre un usuario o el bot |

Y la administración por **lenguaje natural** (el clasificador la reconoce y la resuelve una
tool scope admin):

> «¿qué tenés apagado?» · «apagá la búsqueda web» · «armate una rutina para compartir
> música cada 6 horas» · «borrá la rutina de noticias» · «acordate de que a Ana le gusta el
> jazz» · «poné modo snarky» · «actualizá tu bio» · «dame el debug»

Dos gates independientes protegen esto: el clasificador tiene que reconocer un comando de
admin **y** el autor tiene que **ser** el admin — comparado por string contra settings,
jamás decidido por el LLM. Un usuario común pidiendo «apagá las menciones» cae al flujo de
conversación normal.

## Lo que ni el admin puede hacer por mención

Identidad (handles), modelos y endpoints, estructura MCP, ampliar scopes de tools, apagar
las menciones. Por mención **solo se reduce exposición**; ampliar = solo desde el panel
local. Ver [Seguridad](Seguridad.md).
