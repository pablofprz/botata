"""Compactación de la memoria del bot (T48).

`bot_memory` se inyecta COMPLETA en cada llamada, así que su tamaño es contexto
gastado. La compactación la hace un LLM porque el trabajo es análisis y no
procesamiento: los duplicados están escritos distinto, las contradicciones se
dirimen por fecha y lo efímero hay que reconocerlo.

Lo que más se testea acá NO es el camino feliz sino las **guardas**. El mismo día
que se diseñó esto se encontró que una salida de LLM sin validar había escrito
96k de monólogo interno en la memoria de un usuario (ver `test_bio_interp.py`).
Este pase tiene permiso para REESCRIBIR la memoria del bot: si el modelo divaga o
alucina ids, le reescribe la identidad. El plan se verifica contra la base antes
de tocar nada, y se aplica todo-o-nada.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import db as d  # noqa: E402
import memory_compact as mc  # noqa: E402
from memory_compact import Operacion, PlanCompactacion  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    return d.init_db(tmp_path / "mem.db")


@pytest.fixture()
def memoria(conn):
    """Reproduce la forma real de la memoria de producción: identidad fijada,
    duplicados de distintos autores, una contradicción y un efímero."""
    d.add_bot_memory(conn, "El 19/7 te mataron. Renaciste el 20/7.",
                     source="migration:MEMORY.md", created_at="2026-07-20", pinned=True)
    d.add_bot_memory(conn, "Mi jugador favorito de Boca es Juan Román Riquelme, el diez eterno.",
                     source="tool:@0800panchitos", created_at="2026-07-22")
    d.add_bot_memory(conn, "Mi jugador favorito de Boca es Román Riquelme.",
                     source="tool:@zane-yo", created_at="2026-07-23")
    d.add_bot_memory(conn, "Mapache anarquista es un usurpador hijo de puta.",
                     source="tool:@ppolci.com", created_at="2026-07-26")
    d.add_bot_memory(conn, "Ya no odio a mapacheanarquista. Lo de usurpador quedó atrás.",
                     source="tool:@ppolci.com", created_at="2026-07-28")
    d.add_bot_memory(conn, "[2026-07-27] Posteé bardeando a mapacheanarquista en la rutina.",
                     source="tool:@?", created_at="2026-07-27")
    return d.list_bot_memory(conn)


class _LLM:
    def __init__(self, plan): self.plan = plan
    def complete(self, system, user, schema):
        self.system, self.user = system, user
        return self.plan


# ─── Selección de candidatas ────────────────────────────────────────────────
def test_las_fijadas_no_son_candidatas(memoria):
    ids = {m["id"] for m in mc.candidatas(memoria)}
    fijada = next(m["id"] for m in memoria if m["pinned"])
    assert fijada not in ids and len(ids) == 5


def test_el_bloque_lleva_la_fecha(memoria):
    """Sin fecha el modelo no puede resolver una contradicción."""
    bloque = mc.bloque_para_el_modelo(memoria)
    assert "2026-07-26" in bloque and "2026-07-28" in bloque
    assert all(f"[{m['id']}]" in bloque for m in memoria)


# ─── Las guardas: lo que NO se deja pasar ───────────────────────────────────
def _plan(*ops): return PlanCompactacion(operaciones=list(ops))


def test_rechaza_tocar_una_fijada(memoria):
    fijada = next(m["id"] for m in memoria if m["pinned"])
    errs = mc.verificar(_plan(Operacion(accion="descartar", ids=[fijada])), memoria)
    assert any("fijada" in e for e in errs)


def test_rechaza_ids_inventados(memoria):
    errs = mc.verificar(_plan(Operacion(accion="descartar", ids=[9999])), memoria)
    assert any("inexistente" in e for e in errs)


def test_rechaza_el_mismo_id_en_dos_operaciones(memoria):
    i = mc.candidatas(memoria)[0]["id"]
    j = mc.candidatas(memoria)[1]["id"]
    errs = mc.verificar(_plan(
        Operacion(accion="fusionar", ids=[i, j], texto="x"),
        Operacion(accion="descartar", ids=[i])), memoria)
    assert any("dos operaciones" in e for e in errs)


def test_rechaza_una_fusion_sin_texto(memoria):
    ids = [m["id"] for m in mc.candidatas(memoria)[:2]]
    assert any("sin texto" in e for e in
               mc.verificar(_plan(Operacion(accion="fusionar", ids=ids)), memoria))


def test_rechaza_una_fila_desmesurada(memoria):
    """La guarda que faltaba en el intérprete de bios: un tope duro de largo."""
    ids = [m["id"] for m in mc.candidatas(memoria)[:2]]
    errs = mc.verificar(_plan(Operacion(accion="fusionar", ids=ids, texto="x" * 5000)), memoria)
    assert any("chars" in e for e in errs)


def test_rechaza_un_plan_que_no_reduce(memoria):
    ids = [m["id"] for m in mc.candidatas(memoria)[:1]]
    errs = mc.verificar(_plan(Operacion(accion="fusionar", ids=ids, texto="igual")), memoria)
    assert errs        # fusionar una sola fila no fusiona nada


def test_un_plan_valido_no_tiene_reparos(memoria):
    ids = [m["id"] for m in mc.candidatas(memoria)[:2]]
    assert mc.verificar(_plan(
        Operacion(accion="fusionar", ids=ids, texto="Es hincha de Boca.")), memoria) == []


# ─── Aplicar: nada se borra, todo se archiva ────────────────────────────────
def test_fusionar_archiva_las_viejas_y_crea_la_nueva(conn, memoria):
    ids = [m["id"] for m in memoria if "Riquelme" in m["text"]]
    assert len(ids) == 2
    llm = _LLM(_plan(Operacion(accion="fusionar", ids=ids,
                               texto="Es hincha de Boca; su ídolo es Juan Román Riquelme.")))
    res = mc.compactar(conn, llm, prompt="P", dbmod=d)

    assert res.ok and res.aplicado
    vivas = d.list_bot_memory(conn)
    textos = [m["text"] for m in vivas]
    assert "Es hincha de Boca; su ídolo es Juan Román Riquelme." in textos
    assert not any("Riquelme, el diez eterno" in t for t in textos)   # salió del contexto
    # pero NO se borró: sigue estando, apuntando a su sucesora
    todas = d.list_bot_memory(conn, incluir_archivadas=True)
    viejas = [m for m in todas if m["id"] in ids]
    assert len(viejas) == 2 and all(m["superseded_by"] for m in viejas)


def test_descartar_archiva_sin_sucesora(conn, memoria):
    efimero = next(m["id"] for m in memoria if m["text"].startswith("[2026-07-27]"))
    llm = _LLM(_plan(Operacion(accion="fusionar",
                               ids=[m["id"] for m in memoria if "Riquelme" in m["text"]],
                               texto="Es hincha de Boca."),
                     Operacion(accion="descartar", ids=[efimero], motivo="efímero")))
    mc.compactar(conn, llm, prompt="P", dbmod=d)
    assert efimero not in {m["id"] for m in d.list_bot_memory(conn)}
    fila = [m for m in d.list_bot_memory(conn, incluir_archivadas=True) if m["id"] == efimero][0]
    assert fila["superseded_by"] == efimero          # se apunta a sí misma = descartada


def test_la_fijada_sobrevive_al_pase(conn, memoria):
    llm = _LLM(_plan(Operacion(accion="fusionar",
                               ids=[m["id"] for m in memoria if "Riquelme" in m["text"]],
                               texto="Es hincha de Boca.")))
    mc.compactar(conn, llm, prompt="P", dbmod=d)
    assert any("El 19/7 te mataron" in m["text"] for m in d.list_bot_memory(conn))


def test_un_plan_rechazado_no_toca_la_base(conn, memoria):
    antes = d.list_bot_memory(conn, incluir_archivadas=True)
    llm = _LLM(_plan(Operacion(accion="descartar", ids=[9999])))
    res = mc.compactar(conn, llm, prompt="P", dbmod=d)
    assert not res.ok and not res.aplicado
    assert d.list_bot_memory(conn, incluir_archivadas=True) == antes


def test_si_el_modelo_explota_no_pasa_nada(conn, memoria):
    class _Roto:
        def complete(self, *a): raise RuntimeError("timeout")
    antes = d.list_bot_memory(conn)
    res = mc.compactar(conn, _Roto(), prompt="P", dbmod=d)
    assert not res.ok and d.list_bot_memory(conn) == antes


def test_dry_run_no_escribe_pero_estima(conn, memoria):
    ids = [m["id"] for m in memoria if "Riquelme" in m["text"]]
    llm = _LLM(_plan(Operacion(accion="fusionar", ids=ids, texto="Es hincha de Boca.")))
    antes = d.list_bot_memory(conn, incluir_archivadas=True)
    res = mc.compactar(conn, llm, prompt="P", dbmod=d, dry_run=True)
    assert not res.aplicado and res.plan
    assert res.filas_despues < res.filas_antes and res.chars_despues < res.chars_antes
    assert d.list_bot_memory(conn, incluir_archivadas=True) == antes


def test_no_hace_nada_si_no_hay_material(conn):
    d.add_bot_memory(conn, "única", source="admin")
    res = mc.compactar(conn, _LLM(_plan()), prompt="P", dbmod=d)
    assert res.ok and not res.plan and not res.aplicado


def test_las_fijadas_van_al_prompt_como_contexto(conn, memoria):
    llm = _LLM(_plan())
    mc.compactar(conn, llm, prompt="P", dbmod=d)
    assert "FIJADAS" in llm.user and "El 19/7 te mataron" in llm.user


# ─── Lo archivado deja de entrar al contexto, pero se puede recuperar ───────
def test_restaurar_devuelve_la_fila_al_contexto(conn, memoria):
    ids = [m["id"] for m in memoria if "Riquelme" in m["text"]]
    mc.compactar(conn, _LLM(_plan(Operacion(accion="fusionar", ids=ids, texto="Boca."))),
                 prompt="P", dbmod=d)
    assert d.restore_bot_memory(conn, ids[0])
    assert ids[0] in {m["id"] for m in d.list_bot_memory(conn)}


def test_no_se_rededuplica_contra_lo_archivado(conn, memoria):
    """Si una fila archivada bloqueara el alta de su mismo texto, el admin no
    podría volver a anotar algo que el pase había descartado."""
    ids = [m["id"] for m in memoria if "Riquelme" in m["text"]]
    viejo = next(m["text"] for m in memoria if m["id"] == ids[0])
    mc.compactar(conn, _LLM(_plan(Operacion(accion="fusionar", ids=ids, texto="Boca."))),
                 prompt="P", dbmod=d)
    assert d.add_bot_memory(conn, viejo, source="tool:@ppolci.com") is not None


# ─── Interacciones: el problema es la VENTANA, no el tamaño ────────────────
# `interactions` entra por recencia con k fijo, así que nunca satura el contexto.
# Lo que se rompe es la calidad de esa ventana: cada mención respondida deja una
# nota, así que un rato de ida y vuelta deja cinco notas casi iguales y ocupan
# las cinco. Medido en producción: un usuario con 64 notas de un solo día, y 27
# de 64 usuarios con la ventana tapada por una sola charla.
@pytest.fixture()
def charlas(conn):
    """Un usuario con una charla larga de un día viejo, otra de otro día, y
    actividad de hoy (que no se debe tocar: la conversación puede seguir)."""
    for h in ("panchi.test", "otro.test"):
        conn.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    conn.commit()
    for i in range(6):
        d.log_interaction(conn, "panchi.test", f"saludo afectuoso, angulo {i}",
                          created_at=f"2026-07-21 13:{10 + i:02d}")
    for i in range(4):
        d.log_interaction(conn, "panchi.test", f"charla sobre los redondos {i}",
                          created_at=f"2026-07-23 23:{40 + i:02d}")
    for i in range(3):
        d.log_interaction(conn, "panchi.test", f"lo de hoy {i}",
                          created_at=f"2026-07-28 10:{i:02d}")
    d.log_interaction(conn, "otro.test", "una sola nota", created_at="2026-07-21 10:00")
    return conn


def test_agrupa_por_usuario_y_dia_salvo_el_dia_en_curso(charlas):
    grupos = d.interacciones_compactables(charlas, min_por_dia=3)
    assert {(g["handle"], g["dia"]) for g in grupos} == {
        ("panchi.test", "2026-07-21"), ("panchi.test", "2026-07-23")}
    # el día más reciente del usuario queda afuera (la charla puede seguir)
    assert all(g["dia"] != "2026-07-28" for g in grupos)


def test_un_dia_con_pocas_notas_no_se_toca(charlas):
    assert all(len(g["filas"]) >= 3 for g in d.interacciones_compactables(charlas))
    assert not any(g["handle"] == "otro.test"
                   for g in d.interacciones_compactables(charlas))


class _LLMDias:
    """Devuelve un resumen por grupo, usando los ids que recibió."""
    def __init__(self): self.vistos = []
    def complete(self, system, user, schema):
        import re
        ids = [int(x) for x in re.findall(r"\[(\d+)\]", user)]
        self.vistos.append(user)
        return PlanInteracciones(dias=[ResumenDia(
            ids=ids, resumen="charlamos de fútbol y de los redondos, tono cariñoso")])


from memory_compact import PlanInteracciones, ResumenDia  # noqa: E402


def test_comprime_el_dia_a_una_nota_y_archiva_las_viejas(charlas):
    antes = len(d.recent_interactions(charlas, "panchi.test", limit=99))
    res = mc.compactar_interacciones(charlas, _LLMDias(), prompt="P", dbmod=d)
    assert res.aplicado
    vivas = d.recent_interactions(charlas, "panchi.test", limit=99)
    assert len(vivas) < antes
    # nada se borró: las originales siguen, archivadas
    total = charlas.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    assert total > len(vivas)


def test_la_ventana_de_5_deja_de_estar_tapada(charlas):
    """El objetivo real: que las últimas 5 sean 5 momentos distintos."""
    antes = [r["summary"] for r in d.recent_interactions(charlas, "panchi.test", limit=5)]
    assert sum(1 for s in antes if "lo de hoy" in s) == 3   # hoy ya ocupa 3 de 5
    mc.compactar_interacciones(charlas, _LLMDias(), prompt="P", dbmod=d)
    despues = [r["summary"] for r in d.recent_interactions(charlas, "panchi.test", limit=5)]
    # los dos días viejos ahora aportan UNA nota cada uno en vez de 6 y 4
    assert sum(1 for s in despues if "charlamos de fútbol" in s) == 2
    assert len(despues) == 5


def test_el_dia_en_curso_queda_intacto(charlas):
    mc.compactar_interacciones(charlas, _LLMDias(), prompt="P", dbmod=d)
    vivas = [r["summary"] for r in d.recent_interactions(charlas, "panchi.test", limit=99)]
    assert sum(1 for s in vivas if s.startswith("lo de hoy")) == 3


def test_rechaza_ids_que_no_son_del_grupo(charlas):
    class _Mentiroso:
        def complete(self, s, u, schema):
            return PlanInteracciones(dias=[ResumenDia(ids=[9998, 9999], resumen="x")])
    antes = charlas.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    res = mc.compactar_interacciones(charlas, _Mentiroso(), prompt="P", dbmod=d)
    assert res.rechazos and not res.aplicado
    assert charlas.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == antes


def test_un_grupo_que_falla_no_frena_a_los_demas(charlas):
    class _MedioRoto:
        def __init__(self): self.n = 0
        def complete(self, s, u, schema):
            import re
            self.n += 1
            if self.n == 1:
                raise RuntimeError("timeout")
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", u)]
            return PlanInteracciones(dias=[ResumenDia(ids=ids, resumen="ok")])
    res = mc.compactar_interacciones(charlas, _MedioRoto(), prompt="P", dbmod=d)
    assert res.aplicado                       # el segundo grupo sí se aplicó


def test_dry_run_no_escribe(charlas):
    antes = charlas.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    res = mc.compactar_interacciones(charlas, _LLMDias(), prompt="P", dbmod=d, dry_run=True)
    assert res.plan and not res.aplicado
    assert charlas.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == antes


def test_el_clima_de_la_comunidad_ignora_lo_archivado(charlas):
    mc.compactar_interacciones(charlas, _LLMDias(), prompt="P", dbmod=d)
    clima = d.recent_interactions_all(charlas, limit=50)
    assert not any("angulo" in r["summary"] for r in clima)


def test_el_tope_de_un_resumen_de_dia_es_otro_que_el_de_una_memoria(conn):
    """48 notas comprimidas en una legítimamente no entran en 400 chars: el tope
    de `bot_memory` (un hecho de una oración) no aplica acá. Caso real: el pase
    rechazaba resúmenes de 505 chars que reemplazaban ~6.500."""
    conn.execute("INSERT INTO users(handle) VALUES ('pesado.test')"); conn.commit()
    for i in range(48):
        d.log_interaction(conn, "pesado.test", f"nota {i}: " + ("charla larga " * 10),
                          created_at=f"2026-07-21 1{i % 10}:00")
    d.log_interaction(conn, "pesado.test", "de hoy", created_at="2026-07-28 10:00")
    filas = d.interacciones_compactables(conn)[0]["filas"]
    ids = [f["id"] for f in filas]
    # 894 chars para 23 notas es 3,5x de compresión real: tiene que pasar.
    for n in (505, 894):
        plan = mc.PlanInteracciones(dias=[mc.ResumenDia(ids=ids, resumen="x" * n)])
        assert mc.verificar_interacciones(plan, filas) == [], f"{n} chars debía pasar"
    pasado = mc.PlanInteracciones(
        dias=[mc.ResumenDia(ids=ids, resumen="x" * (mc.MAX_CHARS_INTERACCION + 200))])
    assert any("máximo" in e for e in mc.verificar_interacciones(pasado, filas))


def test_un_resumen_que_no_comprime_se_rechaza(charlas):
    """El invariante real: más corto que la suma de lo que reemplaza."""
    filas = d.interacciones_compactables(charlas)[0]["filas"]
    ids = [f["id"] for f in filas]
    total = sum(len(f["summary"]) for f in filas)
    plan = mc.PlanInteracciones(dias=[mc.ResumenDia(ids=ids, resumen="x" * (total + 5))])
    assert any("no comprime" in e for e in mc.verificar_interacciones(plan, filas))


def test_una_entrada_de_un_solo_id_se_ignora_sin_tumbar_el_grupo(charlas):
    """El modelo a veces devuelve una nota suelta junto a resúmenes válidos.
    Rechazar el grupo entero por eso perdía los buenos (visto en el dry-run
    contra producción)."""
    filas = d.interacciones_compactables(charlas)[0]["filas"]
    ids = [f["id"] for f in filas]

    class _Mixto:
        def complete(self, s, u, schema):
            return mc.PlanInteracciones(dias=[
                mc.ResumenDia(ids=ids[:-1], resumen="charla de la mañana, tono cariñoso"),
                mc.ResumenDia(ids=ids[-1:], resumen="una nota sola"),
            ])
    res = mc.compactar_interacciones(charlas, _Mixto(), prompt="P", dbmod=d, max_grupos=1)
    assert res.aplicado and not res.rechazos
    vivas = [r["summary"] for r in d.recent_interactions(charlas, "panchi.test", limit=99)]
    assert "charla de la mañana, tono cariñoso" in vivas
    assert "una nota sola" not in vivas          # la suelta quedó como estaba


def test_el_schema_acepta_la_lista_pelada(charlas):
    """Los modelos livianos devuelven `[{...}]` en vez de `{"dias": [{...}]}`.
    Medido en producción: 7 de 20 llamadas se tiraban y se reintentaban por eso
    — un tercio del tiempo del pase. Se absorbe en el schema."""
    filas = d.interacciones_compactables(charlas)[0]["filas"]
    ids = [f["id"] for f in filas]
    crudo = [{"ids": ids, "resumen": "charla de la mañana"}]
    plan = mc.PlanInteracciones.model_validate(crudo)
    assert len(plan.dias) == 1 and plan.dias[0].ids == ids
    # y la forma correcta sigue funcionando igual
    assert mc.PlanInteracciones.model_validate({"dias": crudo}).dias[0].ids == ids


def test_el_plan_de_memoria_tambien_acepta_la_lista_pelada():
    crudo = [{"accion": "descartar", "ids": [1, 2], "motivo": "x"}]
    assert len(mc.PlanCompactacion.model_validate(crudo).operaciones) == 1
