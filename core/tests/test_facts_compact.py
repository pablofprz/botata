"""Compactación de `user_facts` — la memoria de cada persona (T49).

Tercer caso de compactación, con su propia forma. `user_facts` NO se inyecta
completa: el bot busca los ~5 hechos más relevantes para lo que se está
hablando. Por eso el problema no es el tamaño (367 filas no cuestan contexto)
sino la **puntería**: si dos de esos cinco son el mismo hecho escrito distinto,
se quedó con tres. Y un fragmento mal guardado —"No puede. Es un admin solo de
comandos, el programador soy yo", el mensaje del usuario archivado como si fuera
un hecho SOBRE él— compite en igualdad con los hechos buenos.

El grupo es el USUARIO entero y no el día (como en `interactions`): acá los
duplicados nacen de contar lo mismo con semanas de diferencia, así que agrupar
por día no los pondría nunca en la misma llamada.

Lo más importante que se testea acá es el filtro de `superseded_by` en el
retrieval. Sin él, compactar sería PEOR que no compactar: el hecho fusionado se
sumaría a los originales en vez de reemplazarlos.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import db as d  # noqa: E402
import memory_compact as mc  # noqa: E402
from memory_compact import Operacion, PlanCompactacion  # noqa: E402


@pytest.fixture()
def sin_modelo(monkeypatch):
    """Embeddings deterministas: acá no se prueba calidad semántica y cargar
    bge-m3 (~2GB) para eso no tiene sentido."""
    def _fake(text: str) -> bytes:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.standard_normal(1024).astype("float32")
        return (v / np.linalg.norm(v)).tobytes()
    monkeypatch.setattr(d, "embed", _fake)
    return _fake


@pytest.fixture()
def hechos(tmp_path, sin_modelo):
    """La forma real de producción: el admin con su identidad contada en tres
    pedazos, un duplicado literal y un fragmento de conversación; un usuario
    cargado; y uno con dos hechos (cola larga, no se toca)."""
    conn = d.init_db(tmp_path / "facts.db")
    for h in ("ppolci.com", "panchi.test", "corto.test"):
        conn.execute("INSERT INTO users(handle) VALUES (?)", (h,))
    for txt, dia in [
        ("@ppolci.com es el creador del bot (admin)", "2026-07-21"),
        ("@ppolci.com tiene acceso a modificar configuración del bot", "2026-07-21"),
        ("@ppolci.com es el programador del bot, corre en su PC.", "2026-07-20"),
        ("No quiere ver memes del mundial, le ponen muy triste.", "2026-07-25"),
        ("No quiere ver memes del mundial porque le entristecen.", "2026-07-25"),
        ("No puede. Es un admin solo de comandos, el programador soy yo.", "2026-07-20"),
    ]:
        conn.execute("INSERT INTO user_facts(handle, fact_text, created_at) "
                     "VALUES ('ppolci.com', ?, ?)", (txt, dia))
    for i in range(5):
        conn.execute("INSERT INTO user_facts(handle, fact_text) "
                     "VALUES ('panchi.test', ?)", (f"le gusta la banda {i}",))
    for i in range(2):
        conn.execute("INSERT INTO user_facts(handle, fact_text) "
                     "VALUES ('corto.test', ?)", (f"hecho suelto {i}",))
    conn.commit()
    yield conn
    conn.close()


def _vivos(conn, handle) -> list[str]:
    return [f["text"] for g in d.facts_compactables(conn, min_por_usuario=1)
            if g["handle"] == handle for f in g["filas"]]


# ─── Agrupación: por usuario, no por día ────────────────────────────────────
def test_agrupa_por_usuario_y_prioriza_al_mas_cargado(hechos):
    grupos = d.facts_compactables(hechos, min_por_usuario=5)
    assert [g["handle"] for g in grupos] == ["ppolci.com", "panchi.test"]
    assert len(grupos[0]["filas"]) == 6


def test_la_cola_larga_se_deja_quieta(hechos):
    """Cada grupo cuesta una llamada al modelo y con dos hechos no hay nada que
    fusionar."""
    grupos = d.facts_compactables(hechos, min_por_usuario=5)
    assert all(g["handle"] != "corto.test" for g in grupos)


def test_el_bloque_lleva_id_y_fecha(hechos):
    """La fecha no es decorativa: una contradicción se dirime por cuál es más
    reciente."""
    filas = d.facts_compactables(hechos)[0]["filas"]
    bloque = mc.bloque_para_el_modelo(filas)
    assert all(f"[{f['id']}]" in bloque for f in filas)
    assert "2026-07-25" in bloque and "2026-07-20" in bloque


def test_los_archivados_no_vuelven_a_entrar_al_grupo(hechos):
    ids = [f["id"] for f in d.facts_compactables(hechos)[0]["filas"]][:2]
    nueva = d.insert_user_fact(hechos, "ppolci.com", "hecho fusionado", "compact")
    d.supersede_user_facts(hechos, ids, nueva)
    hechos.commit()
    vivos = {f["id"] for g in d.facts_compactables(hechos, min_por_usuario=1)
             if g["handle"] == "ppolci.com" for f in g["filas"]}
    assert not (set(ids) & vivos) and nueva in vivos


# ─── Lo que hace que archivar SIRVA ─────────────────────────────────────────
def test_el_archivado_sale_del_retrieval_por_las_dos_ramas(hechos):
    """vec0 y FTS5 son dos caminos independientes hasta la misma fila: filtrar
    en uno solo deja al hecho archivado entrando por el otro."""
    fid = d.insert_user_fact(hechos, "ppolci.com", "es el creador del bot", "test")
    hechos.commit()
    antes = dict(d.hybrid_search_user_facts(hechos, "ppolci.com", "creador del bot", k=20))
    assert fid in antes

    d.supersede_user_facts(hechos, [fid], None)
    hechos.commit()
    despues = dict(d.hybrid_search_user_facts(hechos, "ppolci.com", "creador del bot", k=20))
    assert fid not in despues
    # y sigue en la tabla: archivar no es borrar
    assert hechos.execute("SELECT 1 FROM user_facts WHERE id = ?", (fid,)).fetchone()


def test_supersede_le_saca_el_embedding(hechos):
    """vec0 no tiene FK cascade ni triggers: si el vector queda, el hecho
    archivado sigue siendo el vecino más cercano de sí mismo y el dedup (k=1)
    rechazaría al hecho nuevo como duplicado de uno que ya nadie lee."""
    fid = d.insert_user_fact(hechos, "panchi.test", "toca la guitarra", "test")
    assert hechos.execute("SELECT 1 FROM user_facts_vec WHERE rowid = ?", (fid,)).fetchone()
    d.supersede_user_facts(hechos, [fid], None)
    assert not hechos.execute("SELECT 1 FROM user_facts_vec WHERE rowid = ?", (fid,)).fetchone()


def test_insert_user_fact_no_dedupea(hechos):
    """`upsert_user_fact` no sirve para compactar: el texto fusionado se PARECE
    por definición a los que reemplaza, así que el dedup lo saltearía y el pase
    archivaría los originales sin dejar sucesora."""
    texto = "le gusta la banda 0"
    d.upsert_user_fact(hechos, "panchi.test", texto)  # siembra el vector
    assert d.upsert_user_fact(hechos, "panchi.test", texto) is None   # dedup skip
    assert d.insert_user_fact(hechos, "panchi.test", texto, "compact") > 0


# ─── El pase ────────────────────────────────────────────────────────────────
class _LLMFacts:
    """Fusiona los tres primeros ids del grupo y descarta el último."""
    def __init__(self): self.usuarios = []

    def complete(self, system, user, schema):
        import re
        ids = [int(x) for x in re.findall(r"\[(\d+)\]", user)]
        self.usuarios.append(user)
        return PlanCompactacion(operaciones=[
            Operacion(accion="fusionar", ids=ids[:3], texto="hecho fusionado",
                      motivo="decían lo mismo"),
            Operacion(accion="descartar", ids=ids[-1:], motivo="no es un hecho"),
        ])


def test_compacta_fusiona_archiva_y_deja_sucesora(hechos):
    res = mc.compactar_facts(hechos, _LLMFacts(), prompt="P", dbmod=d, max_usuarios=1)
    assert res.aplicado and not res.rechazos
    vivos = _vivos(hechos, "ppolci.com")
    assert "hecho fusionado" in vivos
    assert len(vivos) == 3          # 6 → 3 archivadas + 1 descartada + 1 nueva
    # nada se borró: las viejas siguen en la tabla, apuntando a su sucesora
    assert hechos.execute("SELECT COUNT(*) FROM user_facts "
                          "WHERE handle = 'ppolci.com'").fetchone()[0] == 7


def test_el_prompt_recibe_los_hechos_de_un_solo_usuario(hechos):
    llm = _LLMFacts()
    mc.compactar_facts(hechos, llm, prompt="P", dbmod=d, max_usuarios=2)
    assert len(llm.usuarios) == 2
    assert "@ppolci.com" in llm.usuarios[0]
    assert "le gusta la banda" not in llm.usuarios[0]


def test_respeta_el_tope_de_usuarios_por_pase(hechos):
    """Corre dentro del loop del bot y usa el modelo de razonamiento: el tope es
    lo que lo hace pagable. Lo que queda afuera lo toma la próxima corrida."""
    llm = _LLMFacts()
    mc.compactar_facts(hechos, llm, prompt="P", dbmod=d, max_usuarios=1)
    assert len(llm.usuarios) == 1


def test_rechaza_un_id_que_no_es_de_ese_usuario(hechos):
    """El plan se verifica contra el GRUPO, no contra la tabla entera: un id
    real de otra persona es tan inválido como uno inventado."""
    ajeno = d.facts_compactables(hechos)[1]["filas"][0]["id"]

    class _Cruzado:
        def complete(self, s, u, schema):
            import re
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", u)]
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=ids[:2] + [ajeno], texto="mezcla")])

    res = mc.compactar_facts(hechos, _Cruzado(), prompt="P", dbmod=d, max_usuarios=1)
    assert not res.aplicado and any("inexistente" in r for r in res.rechazos)
    assert hechos.execute("SELECT COUNT(*) FROM user_facts "
                          "WHERE superseded_by IS NOT NULL").fetchone()[0] == 0


def test_rechaza_una_fusion_desmesurada(hechos):
    """Un hecho es una oración. El tope ataja al modelo que se va de tema — es
    la guarda que faltaba el día que un LLM escribió 96k de monólogo interno
    adentro de la memoria de un usuario."""
    class _Verborragico:
        def complete(self, s, u, schema):
            import re
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", u)]
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=ids[:3], texto="x" * (mc.MAX_CHARS_FILA + 1))])

    res = mc.compactar_facts(hechos, _Verborragico(), prompt="P", dbmod=d, max_usuarios=1)
    assert not res.aplicado and any("chars" in r for r in res.rechazos)
    assert len(_vivos(hechos, "ppolci.com")) == 6


def test_dry_run_no_toca_nada(hechos):
    antes = hechos.execute("SELECT COUNT(*) FROM user_facts").fetchone()[0]
    res = mc.compactar_facts(hechos, _LLMFacts(), prompt="P", dbmod=d,
                             max_usuarios=2, dry_run=True)
    assert res.plan and not res.aplicado
    assert res.filas_despues < res.filas_antes
    assert hechos.execute("SELECT COUNT(*) FROM user_facts").fetchone()[0] == antes


def test_si_falla_un_usuario_el_otro_igual_se_compacta(hechos):
    """Todo-o-nada por usuario: la memoria de uno no queda a medio reescribir
    porque el modelo se equivocó con la de otro."""
    class _MitadRota:
        def __init__(self): self.n = 0

        def complete(self, s, u, schema):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("timeout del modelo")
            import re
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", u)]
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=ids[:3], texto="hecho fusionado")])

    res = mc.compactar_facts(hechos, _MitadRota(), prompt="P", dbmod=d, max_usuarios=2)
    assert res.aplicado
    assert len(_vivos(hechos, "ppolci.com")) == 6     # el que falló, intacto
    assert len(_vivos(hechos, "panchi.test")) == 3    # 5 → 3


def test_sin_usuarios_cargados_no_llama_al_modelo(hechos):
    class _Explota:
        def complete(self, s, u, schema):
            raise AssertionError("no debería llamarse")

    res = mc.compactar_facts(hechos, _Explota(), prompt="P", dbmod=d,
                             min_por_usuario=99)
    assert not res.aplicado and not res.plan


def test_el_resumen_no_promete_tokens_donde_no_los_ahorra(hechos):
    """`user_facts` no se inyecta completa: decir 'tokens por llamada' en el log
    sería mentirle al admin sobre lo que ganó."""
    res = mc.compactar_facts(hechos, _LLMFacts(), prompt="P", dbmod=d, max_usuarios=1)
    assert "chars menos" in res.resumen() and "tokens" not in res.resumen()


# ─── El reintento ───────────────────────────────────────────────────────────
def test_reintenta_una_vez_cuando_el_plan_no_pasa_las_guardas(hechos):
    """Medido contra producción: en el grupo más grande (64 hechos de una sola
    persona) el modelo puso un id en dos operaciones y se perdieron las otras
    diez, que estaban bien. El todo-o-nada no se negocia, pero tirar el trabajo
    entero por un desliz de contabilidad sí se puede evitar."""
    class _SeEquivocaUnaVez:
        def __init__(self): self.n, self.prompts = 0, []

        def complete(self, s, u, schema):
            import re
            self.n += 1
            self.prompts.append(u)
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", u)]
            if self.n == 1:      # el mismo id en dos operaciones
                return PlanCompactacion(operaciones=[
                    Operacion(accion="fusionar", ids=ids[:2], texto="a"),
                    Operacion(accion="fusionar", ids=ids[1:3], texto="b")])
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=ids[:3], texto="hecho fusionado")])

    llm = _SeEquivocaUnaVez()
    res = mc.compactar_facts(hechos, llm, prompt="P", dbmod=d, max_usuarios=1)
    assert llm.n == 2 and res.aplicado and not res.rechazos
    # el segundo pedido le dice QUÉ estuvo mal, si no repetiría el error
    assert "RECHAZADO" in llm.prompts[1] and "dos operaciones" in llm.prompts[1]
    assert "hecho fusionado" in _vivos(hechos, "ppolci.com")


def test_si_se_equivoca_dos_veces_no_se_aplica_nada(hechos):
    """Una segunda oportunidad, no infinitas: el reintento abarata un desliz,
    no cambia la guarda."""
    class _SiempreMal:
        def __init__(self): self.n = 0

        def complete(self, s, u, schema):
            self.n += 1
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=[999_001, 999_002], texto="inventado")])

    llm = _SiempreMal()
    res = mc.compactar_facts(hechos, llm, prompt="P", dbmod=d, max_usuarios=1)
    assert llm.n == 2 and not res.aplicado and res.rechazos
    assert len(_vivos(hechos, "ppolci.com")) == 6


# ─── 📌: lo que la persona pidió que recuerde (T49c) ────────────────────────
# La distinción es INDEDUCIBLE después: una vez escrito, "acordate de que soy de
# Racing" y el bot anotándolo por su cuenta quedan idénticos. Por eso la marca la
# pone quien escribe, en el momento.
def test_lo_fijado_entra_siempre_y_no_pasa_por_la_busqueda(hechos):
    fid = d.upsert_user_fact(hechos, "panchi.test", "es de Racing, no de Boca",
                             source_uri="/remember", pinned=True)
    assert fid is not None
    # sale por el camino directo, sin depender de la consulta
    assert fid in {i for i, _ in d.pinned_user_facts(hechos, "panchi.test")}
    # y NO compite por los k lugares de la búsqueda
    for consulta in ("de que cuadro sos", "hola que tal"):
        assert fid not in {i for i, _ in d.hybrid_search_user_facts(
            hechos, "panchi.test", consulta, k=20)}


def test_pedir_que_recuerde_algo_que_ya_sabia_fija_lo_que_estaba(hechos):
    """Si el dedup lo descartara en silencio, el pedido se perdería: el bot ya
    sabía el dato pero nadie le había dicho que era importante."""
    d.upsert_user_fact(hechos, "panchi.test", "le gusta el asado")
    previo = {i for i, _ in d.pinned_user_facts(hechos, "panchi.test")}
    assert d.upsert_user_fact(hechos, "panchi.test", "le gusta el asado",
                              pinned=True) is None      # dedup: no inserta
    ahora = {i for i, _ in d.pinned_user_facts(hechos, "panchi.test")}
    assert len(ahora) == len(previo) + 1                # pero fija el que estaba


def test_la_compactacion_no_toca_lo_fijado(hechos):
    ids = [f["id"] for f in d.facts_compactables(hechos)[0]["filas"]]
    d.set_user_fact_pinned(hechos, ids[0], True)

    class _Avaro:
        """Intenta fusionar el 📌 con los demás."""
        def __init__(self): self.prompts = []

        def complete(self, s, u, schema):
            self.prompts.append(u)
            return PlanCompactacion(operaciones=[Operacion(
                accion="fusionar", ids=ids[:3], texto="todo junto")])

    llm = _Avaro()
    res = mc.compactar_facts(hechos, llm, prompt="P", dbmod=d, max_usuarios=1)
    assert not res.aplicado and any("fijada" in r for r in res.rechazos)
    # se le mostró como contexto, pero fuera de la lista de compactables
    assert "FIJADOS" in llm.prompts[0]
    assert hechos.execute("SELECT superseded_by FROM user_facts WHERE id = ?",
                          (ids[0],)).fetchone()[0] is None


def test_los_fijados_no_cuentan_para_el_minimo(hechos):
    """Un usuario cuyos hechos son casi todos 📌 no tiene material que fusionar,
    así que no vale gastarle una llamada al modelo."""
    for f in d.facts_compactables(hechos, min_por_usuario=1):
        if f["handle"] != "panchi.test":
            continue
        for fila in f["filas"][:4]:
            d.set_user_fact_pinned(hechos, fila["id"], True)
    assert not any(g["handle"] == "panchi.test"
                   for g in d.facts_compactables(hechos, min_por_usuario=5))
