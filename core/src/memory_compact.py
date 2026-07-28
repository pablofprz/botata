"""memory_compact.py — compactación de la memoria del bot (T48).

**El problema.** `bot_memory` se inyecta COMPLETA en cada llamada (menciones,
rutinas, pase de mood): su tamaño es, literalmente, contexto gastado. Crece ~4,5
filas/día, así que a seis meses son ~20k tokens en cada prompt. Y no crece
limpia: medido sobre producción a los 8 días, con 36 filas ya había el jugador
favorito repetido tres veces, cuatro pares de duplicados, un log efímero de una
rutina, y una contradicción viva ("es un usurpador hijo de puta" conviviendo con
"ya no lo odio, eso quedó atrás").

**Por qué un LLM y no código.** Deduplicar textualmente no sirve: los duplicados
están escritos distinto ("Juan Román Riquelme, el diez eterno" vs "hincha de
Boca, su jugador favorito es Juan Román Riquelme"). Resolver una contradicción
exige entender cuál venció a cuál. Descartar lo efímero exige saber qué es un
hecho duradero. Es análisis, no procesamiento.

**Por qué el código igual desconfía.** El mismo día que se diseñó esto se
encontró que una salida de LLM sin validar había escrito 96.789 caracteres de
monólogo interno dentro de la memoria de un usuario. Acá el modelo tiene permiso
para REESCRIBIR la memoria del bot: si divaga, le reescribe la identidad. Por eso
el plan que devuelve se verifica entero contra reglas duras ANTES de tocar nada,
y se aplica en una transacción que se revierte completa si algo no cierra. El
prompt pide; el código comprueba.

**Nada se borra.** Las filas viejas quedan apuntando con `superseded_by` a la que
las reemplazó: dejan de entrar al contexto pero se pueden auditar y revertir.
Apuntar un reasoning model a la memoria del bot sin undo es una puerta de una
sola dirección.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger("botata.memory_compact")

# Una memoria general es una oración, no un párrafo. Tope generoso pero que
# ataja a un modelo que se va de tema.
MAX_CHARS_FILA = 400
# La nota de un día de charla resume decenas de intercambios, así que tiene otro
# tamaño natural: 48 notas comprimidas en una legítimamente no entran en 400.
# Calibrado contra producción en dos pasadas: con 700 todavía se rechazaban
# resúmenes de 894 chars que reemplazaban ~3.100 (3,5x de compresión real). Este
# número solo ataja el desborde; la proporción la cuida el invariante de abajo.
MAX_CHARS_INTERACCION = 1100


class Operacion(BaseModel):
    accion: Literal["fusionar", "descartar"] = Field(
        description="'fusionar' = varias filas dicen lo mismo o se contradicen y quedan en "
                    "una sola (requiere texto). 'descartar' = no aporta y no deja sucesora.")
    ids: list[int] = Field(
        description="Ids de las filas afectadas. Para fusionar, TODAS las que la nueva reemplaza.")
    texto: str = Field(
        default="",
        description="Solo para 'fusionar': el texto único que reemplaza a las de arriba, "
                    "escrito en la voz del bot y en el idioma de las originales.")
    motivo: str = Field(
        default="",
        description="Una frase de por qué. Queda en el log para que el admin pueda auditarlo.")


# Los modelos livianos devuelven la LISTA pelada en vez del objeto que la
# envuelve — medido en producción: 7 de 20 llamadas, cada una tirada y
# reintentada, o sea un tercio del tiempo del pase gastado en nada. El schema lo
# absorbe en vez de pelearlo (mismo criterio que `BotReply`, que acepta 'text' o
# 'message'): es una forma inequívoca de la misma respuesta.
def _envolver_lista(clave: str):
    def _pre(cls, data):
        return {clave: data} if isinstance(data, list) else data
    return model_validator(mode="before")(classmethod(_pre))


class PlanCompactacion(BaseModel):
    operaciones: list[Operacion] = Field(
        default_factory=list,
        description="Vacío si no hay nada que compactar. No inventes trabajo.")

    _lista_pelada = _envolver_lista("operaciones")


@dataclass
class Resultado:
    """Qué pasó (o qué pasaría, en dry-run)."""
    plan: list[Operacion] = field(default_factory=list)
    aplicado: bool = False
    filas_antes: int = 0
    filas_despues: int = 0
    chars_antes: int = 0
    chars_despues: int = 0
    rechazos: list[str] = field(default_factory=list)
    # Solo `bot_memory` se inyecta completa: ahí los chars ahorrados SON tokens
    # menos en cada prompt. En interacciones y facts se gana precisión, no
    # contexto, y decir "tokens por llamada" en el log sería mentirle al admin.
    ahorra_contexto: bool = False

    @property
    def ok(self) -> bool:
        return not self.rechazos

    def resumen(self) -> str:
        if self.rechazos:
            return "plan rechazado: " + "; ".join(self.rechazos)
        if not self.plan:
            return "no hay nada que compactar"
        verbo = "compactadas" if self.aplicado else "se compactarían"
        ahorro = self.chars_antes - self.chars_despues
        detalle = f"{ahorro} chars menos"
        if self.ahorra_contexto:
            detalle += f", ~{ahorro // 4} tokens por llamada"
        return (f"{verbo} {self.filas_antes} → {self.filas_despues} filas "
                f"({detalle})")


def candidatas(memorias: list[dict]) -> list[dict]:
    """Las que el pase puede tocar: las vigentes, fijadas incluidas.

    Las 📌 entran, pero solo para FUSIONARSE (ver `verificar`). Excluirlas del
    todo parecía lo prudente y resultó ser una trampa: medido en producción, el
    59% de los hechos terminó fijado y la compactación se quedó sin material —
    de 35 usuarios compactables a 4. Peor, los duplicados con una punta fijada
    quedaban congelados para siempre: tres filas diciendo que alguien es de
    Chacarita, dos de ellas 📌, que ningún pase podía tocar.

    Fusionar no pierde nada —la sucesora nace 📌 y conserva lo que decían— y
    sigue habiendo undo por `superseded_by`. Descartar sí perdería, y por eso
    sigue prohibido.
    """
    return [m for m in memorias if m.get("superseded_by") is None]


def bloque_para_el_modelo(memorias: list[dict]) -> str:
    """Las filas numeradas y fechadas.

    La FECHA es imprescindible y no decorativa: sin ella el modelo no puede
    resolver una contradicción, que se dirime por cuál es más reciente."""
    return "\n".join(
        f"[{m['id']}] ({str(m.get('created_at') or '')[:10]}) {m['text']}"
        for m in memorias)


def _bloque_con_pin(filas: list[dict]) -> str:
    """Como `bloque_para_el_modelo`, marcando cuáles están fijadas. El modelo
    necesita verlas para poder fusionarlas y para saber que no puede tirarlas."""
    return "\n".join(
        f"[{m['id']}] ({str(m.get('created_at') or '')[:10]}) "
        f"{'📌 ' if m.get('pinned') else ''}{m['text']}"
        for m in filas)


def verificar(plan: PlanCompactacion, memorias: list[dict]) -> list[str]:
    """Reglas duras. Devuelve los motivos de rechazo (vacío = el plan sirve).

    Se verifica contra la BASE, no contra lo que el prompt pidió: que el modelo
    haya prometido no tocar las fijadas no es evidencia de que no las tocó.
    """
    por_id = {m["id"]: m for m in memorias}
    fijadas = {m["id"] for m in memorias if m.get("pinned")}
    archivadas = {m["id"] for m in memorias if m.get("superseded_by") is not None}
    errores: list[str] = []
    vistos: set[int] = set()

    for op in plan.operaciones:
        if not op.ids:
            errores.append("una operación sin ids")
            continue
        for i in op.ids:
            if i not in por_id:
                errores.append(f"id {i} inexistente (¿inventado?)")
            elif i in fijadas and op.accion == "descartar":
                # Fusionarla sí (la sucesora hereda el 📌 y el contenido);
                # tirarla no. Lo que alguien pidió recordar no se pierde.
                errores.append(f"id {i} está fijada 📌 y el plan la descarta")
            elif i in archivadas:
                errores.append(f"id {i} ya estaba archivada")
            if i in vistos:
                errores.append(f"id {i} aparece en dos operaciones")
            vistos.add(i)
        if op.accion == "fusionar":
            if not op.texto.strip():
                errores.append(f"fusión de {op.ids} sin texto")
            elif len(op.texto) > MAX_CHARS_FILA:
                errores.append(f"la fusión de {op.ids} mide {len(op.texto)} chars "
                               f"(máximo {MAX_CHARS_FILA})")
            elif len(op.ids) < 2:
                errores.append(f"fusionar {op.ids} no fusiona nada")

    # Una compactación que no reduce no es una compactación: si el plan deja
    # tantas filas como había, algo entendió mal y no vale la pena el riesgo.
    quitadas = sum(len(op.ids) for op in plan.operaciones)
    agregadas = sum(1 for op in plan.operaciones if op.accion == "fusionar")
    if plan.operaciones and agregadas >= quitadas:
        errores.append("el plan no reduce ninguna fila")
    return errores


def compactar(conn, llm, *, prompt: str, dbmod, dry_run: bool = False) -> Resultado:
    """Corre un pase de compactación sobre `bot_memory`.

    `llm` necesita `.complete(system, user, schema) -> PlanCompactacion`.
    `dbmod` se inyecta para no acoplar este módulo a db.py (y poder testear).
    """
    todas = dbmod.list_bot_memory(conn, limit=1000)
    libres = candidatas(todas)
    res = Resultado(filas_antes=len(todas), ahorra_contexto=True,
                    chars_antes=sum(len(m["text"]) for m in todas))
    res.filas_despues, res.chars_despues = res.filas_antes, res.chars_antes
    if len(libres) < 2:
        log.info("compactación: %d filas tocables, no hay nada que hacer", len(libres))
        return res

    usuario = "Memorias que podés compactar (las 📌 se fusionan, nunca se descartan):\n" \
        + _bloque_con_pin(libres)
    try:
        plan = llm.complete(prompt, usuario, PlanCompactacion)
    except Exception as e:
        log.error("compactación: el modelo falló (%s: %s)", type(e).__name__, e)
        res.rechazos.append(f"el modelo falló: {e}")
        return res

    res.plan = plan.operaciones
    res.rechazos = verificar(plan, todas)
    if res.rechazos:
        log.warning("compactación RECHAZADA: %s", "; ".join(res.rechazos))
        return res
    if not plan.operaciones:
        log.info("compactación: el modelo no encontró nada que compactar")
        return res

    for op in plan.operaciones:
        log.info("compactación: %s %s%s — %s", op.accion, op.ids,
                 f" → {op.texto[:60]!r}" if op.texto else "", op.motivo[:80])
    if dry_run:
        res.filas_despues = res.filas_antes - sum(
            len(op.ids) - (1 if op.accion == "fusionar" else 0) for op in plan.operaciones)
        res.chars_despues = res.chars_antes - sum(
            sum(len(por["text"]) for por in todas if por["id"] in op.ids) - len(op.texto)
            for op in plan.operaciones)
        return res

    # Todo o nada: si algo explota a mitad, la memoria no queda a medio reescribir.
    try:
        with conn:
            fijado = {m["id"] for m in todas if m.get("pinned")}
            for op in plan.operaciones:
                nueva = None
                if op.accion == "fusionar":
                    cur = conn.execute(
                        "INSERT INTO bot_memory (text, source, pinned) VALUES (?, ?, ?)",
                        (op.texto.strip(), "compact",
                         1 if fijado & set(op.ids) else 0))
                    nueva = int(cur.lastrowid)
                for vid in op.ids:
                    conn.execute("UPDATE bot_memory SET superseded_by = ? WHERE id = ?",
                                 (nueva if nueva is not None else vid, vid))
    except Exception as e:
        log.error("compactación: falló al aplicar, se revirtió entera (%s)", e)
        res.rechazos.append(f"falló al aplicar: {e}")
        return res

    quedan = dbmod.list_bot_memory(conn, limit=1000)
    res.aplicado = True
    res.filas_despues = len(quedan)
    res.chars_despues = sum(len(m["text"]) for m in quedan)
    log.info("compactación aplicada: %s", res.resumen())
    return res


# ─── Interacciones: recuperar la ventana de recencia ────────────────────────
# Problema distinto al de bot_memory, y la diferencia importa. `interactions`
# NO se inyecta completa: entra por recencia con k fijo (las 5 últimas del
# usuario), así que no satura el contexto por más que crezca. Lo que se rompe es
# la CALIDAD de esa ventana: cada mención respondida deja una nota, así que un
# rato de ida y vuelta genera cinco notas casi iguales y las cinco ocupan la
# ventana entera. Medido en producción: un usuario con 64 notas de un solo día,
# y 27 de 64 usuarios con la ventana tapada. Comprimir por día la libera —
# las últimas 5 pasan a ser cinco DÍAS de relación en vez de cinco ángulos de
# la misma mañana.
#
# El día es la unidad de COMPRESIÓN; el grupo es el USUARIO. El schema devuelve
# una lista de días, así que pedir un día por llamada desperdiciaba llamadas: en
# el backfill, 38 donde alcanzaban ~15.

class ResumenDia(BaseModel):
    ids: list[int] = Field(description="Ids de las notas de ese día que resume esta línea.")
    resumen: str = Field(
        description="UNA nota que cuenta ese día con esa persona: de qué se habló y en qué "
                    "tono. En la voz del bot y en primera persona.")


class PlanInteracciones(BaseModel):
    dias: list[ResumenDia] = Field(default_factory=list)

    _lista_pelada = _envolver_lista("dias")


def _dias_utiles(plan: PlanInteracciones) -> list[ResumenDia]:
    """Descarta las entradas de UN solo id: reescribir una nota sola no comprime
    nada y solo arriesga distorsionarla. No es un plan inválido —el resto del
    grupo puede estar perfecto—, así que se ignoran en vez de rechazar todo."""
    return [d for d in plan.dias if len(d.ids) >= 2]


def verificar_interacciones(plan: PlanInteracciones, filas: list[dict]) -> list[str]:
    """Mismas reglas duras que en bot_memory, sobre las notas de una persona."""
    largo = {f["id"]: len(f["summary"]) for f in filas}
    fecha = {f["id"]: f.get("dia") for f in filas}
    errores: list[str] = []
    vistos: set[int] = set()
    for dia in plan.dias:
        for i in dia.ids:
            if i not in largo:
                errores.append(f"id {i} no está en el grupo (¿inventado?)")
            if i in vistos:
                errores.append(f"id {i} en dos resúmenes")
            vistos.add(i)
        # Un resumen por DÍA: mezclar días distintos en una nota es exactamente
        # lo contrario de lo que se busca. La ventana son las 5 últimas notas —
        # colapsar tres días en uno la vacía en vez de llenarla de días
        # distintos, que es la mejora que justifica todo el pase.
        dias_tocados = {fecha.get(i) for i in dia.ids if i in fecha}
        if len(dias_tocados) > 1:
            errores.append(f"un resumen mezcla {len(dias_tocados)} días distintos "
                           f"({', '.join(sorted(str(x) for x in dias_tocados))})")
        if not dia.resumen.strip():
            errores.append(f"resumen vacío para {dia.ids}")
            continue
        if len(dia.resumen) > MAX_CHARS_INTERACCION:
            errores.append(f"el resumen de {len(dia.ids)} notas mide "
                           f"{len(dia.resumen)} chars (máximo {MAX_CHARS_INTERACCION})")
        # El invariante de verdad: la nota nueva tiene que ser más corta que la
        # suma de las que reemplaza. Un "resumen" más largo que el original no
        # es un resumen — es el modelo escribiendo de más.
        original = sum(largo.get(i, 0) for i in dia.ids)
        if original and len(dia.resumen) >= original:
            errores.append(f"el resumen de {len(dia.ids)} notas no comprime "
                           f"({len(dia.resumen)} vs {original} chars)")
    return errores


def compactar_interacciones(conn, llm, *, prompt: str, dbmod,
                            min_por_dia: int = 3, max_grupos: int = 40,
                            dry_run: bool = False) -> Resultado:
    """Comprime a UNA nota por día los días con varias notas, un USUARIO por llamada.

    El día sigue siendo la unidad de compresión, pero el grupo es la persona: el
    schema devuelve una lista de días, así que pedir uno por vez era desperdiciar
    llamadas (38 donde alcanzaban ~15). `max_grupos` acota el gasto de una
    corrida: lo que queda afuera se toma en la siguiente, y se avisa en el log —
    un tope silencioso se leería como "ya está todo compactado"."""
    grupos = dbmod.interacciones_compactables(conn, min_por_dia=min_por_dia)
    res = Resultado()
    if not grupos:
        log.info("interacciones: no hay días con notas repetidas")
        return res
    if len(grupos) > max_grupos:
        log.info("interacciones: %d usuarios compactables, hago %d en esta corrida "
                 "(el resto queda para la próxima)", len(grupos), max_grupos)
        grupos = grupos[:max_grupos]

    res.filas_antes = sum(len(g["filas"]) for g in grupos)
    res.chars_antes = sum(len(f["summary"]) for g in grupos for f in g["filas"])
    hechos = 0
    for g in grupos:
        # Con los días separados y rotulados: el modelo tiene que devolver UNA
        # nota por día, y sin el corte visible los mezcla.
        bloque = "\n\n".join(
            f"— {d['dia']} ({len(d['filas'])} notas) —\n" + "\n".join(
                f"[{f['id']}] {f['created_at'][11:16]} {f['summary']}" for f in d["filas"])
            for d in g["dias"])
        usuario = (f"Notas de @{g['handle']} en {len(g['dias'])} día(s) "
                   f"({len(g['filas'])} notas en total):\n\n{bloque}")
        try:
            plan = llm.complete(prompt, usuario, PlanInteracciones)
        except Exception as e:
            log.warning("interacciones: falló @%s (%s)", g["handle"], e)
            continue
        utiles = _dias_utiles(plan)
        errs = verificar_interacciones(PlanInteracciones(dias=utiles), g["filas"])
        if errs:
            log.warning("interacciones: plan rechazado para @%s: %s",
                        g["handle"], "; ".join(errs))
            res.rechazos += errs
            continue
        if not utiles:
            continue
        res.plan += [Operacion(accion="fusionar", ids=d.ids, texto=d.resumen)
                     for d in utiles]
        if dry_run:
            continue
        cuando = {f["id"]: f["created_at"] for f in g["filas"]}
        try:
            with conn:
                for d in utiles:
                    # La nota nueva se fecha con la ÚLTIMA de las que reemplaza,
                    # no con la última del usuario: si no, todos los días de una
                    # persona colapsarían al mismo timestamp y la ventana por
                    # recencia quedaría sin orden.
                    cur = conn.execute(
                        "INSERT INTO interactions (handle, summary, source_uri, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (g["handle"], d.resumen.strip(), "compact",
                         max(cuando[i] for i in d.ids)))
                    dbmod.supersede_interactions(conn, d.ids, int(cur.lastrowid))
            hechos += 1
        except Exception as e:
            log.error("interacciones: falló al aplicar @%s (%s)", g["handle"], e)
            res.rechazos.append(str(e))

    res.aplicado = hechos > 0
    res.filas_despues = res.filas_antes - sum(
        len(op.ids) - 1 for op in res.plan)
    res.chars_despues = res.chars_antes - sum(
        sum(len(f["summary"]) for g in grupos for f in g["filas"] if f["id"] in op.ids)
        - len(op.texto) for op in res.plan)
    log.info("interacciones: %s (%d grupos)", res.resumen(), len(grupos))
    return res


# ─── user_facts: la memoria de cada persona ─────────────────────────────────
# Tercer caso, con su propia forma. `user_facts` tampoco entra completa —se
# recupera por búsqueda híbrida— así que 400 filas no cuestan contexto. Lo que
# se degrada es la PRECISIÓN: el bot trae 5 hechos por consulta, y si dos de
# esos cinco son el mismo hecho escrito distinto, perdió dos lugares. Peor, un
# fragmento mal guardado ("No puede. Es un admin solo de comandos, el
# programador soy yo…", el mensaje del usuario archivado como si fuera un hecho
# sobre él) compite en igualdad con los hechos buenos y a veces gana.
#
# Por eso el grupo es el USUARIO y no el día: los duplicados acá nacen de
# contar lo mismo dos veces con semanas de diferencia, así que agrupar por día
# no los juntaría nunca. Hay dedup semántico en la escritura, pero corre con
# umbral alto y a un solo vecino; lo que se le escapa se acumula.

def _plan_de_facts(llm, prompt: str, usuario: str, g: dict):
    """Pide el plan y, si no pasa las guardas, da UNA segunda oportunidad.

    El todo-o-nada por usuario no se negocia (es la guarda), pero descartar el
    trabajo entero por un desliz de contabilidad sí se puede evitar. Medido
    contra producción: en el grupo más grande —64 hechos de una sola persona— el
    modelo puso un id en dos operaciones y se perdieron las otras diez, buenas.
    Cuanto más grande el grupo, más probable el desliz y más caro tirarlo.
    """
    for intento in (1, 2):
        try:
            plan = llm.complete(prompt, usuario, PlanCompactacion)
        except Exception as e:
            log.warning("facts: falló @%s (%s: %s)", g["handle"], type(e).__name__, e)
            return None, []
        errs = verificar(plan, g["filas"])
        if not errs or intento == 2:
            return plan, errs
        log.info("facts: reintento para @%s — %s", g["handle"], "; ".join(errs))
        usuario += ("\n\nTu plan anterior fue RECHAZADO por: " + "; ".join(errs)
                    + "\nRehacelo corrigiendo eso. Ante la duda, dejá el hecho como está.")
    return None, []  # inalcanzable; el bucle siempre sale por el return de arriba


def compactar_facts(conn, llm, *, prompt: str, dbmod,
                    min_por_usuario: int = 5, max_usuarios: int = 3,
                    dry_run: bool = False) -> Resultado:
    """Deduplica y limpia `user_facts`, un usuario por llamada.

    `max_usuarios` es chico a propósito: esto corre dentro del loop del bot y
    usa el modelo de razonamiento (fusionar hechos de una persona y resolver
    cuál venció a cuál es análisis, no resumen). Lo que no entra en esta corrida
    lo toma la siguiente, y queda dicho en el log — un tope silencioso se leería
    como "ya está todo limpio".
    """
    grupos = dbmod.facts_compactables(conn, min_por_usuario=min_por_usuario)
    res = Resultado()
    if not grupos:
        log.info("facts: ningún usuario con %d+ hechos", min_por_usuario)
        return res
    if len(grupos) > max_usuarios:
        log.info("facts: %d usuarios compactables, hago %d en esta corrida "
                 "(el resto queda para la próxima)", len(grupos), max_usuarios)
        grupos = grupos[:max_usuarios]

    res.filas_antes = sum(len(g["filas"]) for g in grupos)
    res.chars_antes = sum(len(f["text"]) for g in grupos for f in g["filas"])
    hechos = 0
    for g in grupos:
        # Mismo trato que en bot_memory: los 📌 se muestran para que el plan no
        # los contradiga, pero no se ofrecen para compactar. Si igual aparecen
        # en el plan, `verificar` lo rechaza — se comprueba contra la base, no
        # contra lo que el prompt pidió.
        usuario = (f"Hechos que el bot recuerda de @{g['handle']} "
                   f"({len(g['filas'])}):\n" + _bloque_con_pin(g["filas"]))
        plan, errs = _plan_de_facts(llm, prompt, usuario, g)
        if plan is None:
            continue
        if errs:
            log.warning("facts: plan rechazado para @%s: %s", g["handle"], "; ".join(errs))
            res.rechazos += errs
            continue
        if not plan.operaciones:
            continue
        for op in plan.operaciones:
            log.info("facts @%s: %s %s%s — %s", g["handle"], op.accion, op.ids,
                     f" → {op.texto[:60]!r}" if op.texto else "", op.motivo[:80])
        res.plan += plan.operaciones
        if dry_run:
            continue
        # Todo o nada por usuario: la memoria de uno no queda a medio reescribir
        # porque falló la de otro.
        try:
            with conn:
                fijado = {f["id"] for f in g["filas"] if f.get("pinned")}
                for op in plan.operaciones:
                    nueva = None
                    if op.accion == "fusionar":
                        # Si alguna de las que reemplaza estaba 📌, la sucesora
                        # también: fusionar no puede desfijar por la ventana.
                        nueva = dbmod.insert_user_fact(
                            conn, g["handle"], op.texto.strip(), "compact",
                            pinned=bool(fijado & set(op.ids)))
                    dbmod.supersede_user_facts(conn, op.ids, nueva)
            hechos += 1
        except Exception as e:
            log.error("facts: falló al aplicar @%s, se revirtió (%s)", g["handle"], e)
            res.rechazos.append(f"@{g['handle']}: {e}")

    res.aplicado = hechos > 0
    por_id = {f["id"]: f["text"] for g in grupos for f in g["filas"]}
    res.filas_despues = res.filas_antes - sum(
        len(op.ids) - (1 if op.accion == "fusionar" else 0) for op in res.plan)
    res.chars_despues = res.chars_antes - sum(
        sum(len(por_id.get(i, "")) for i in op.ids) - len(op.texto)
        for op in res.plan)
    log.info("facts: %s (%d usuarios)", res.resumen(), len(grupos))
    return res
