"""Tests del framework de tools (tools.py). Sin deps pesadas — solo stdlib."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tools import Scope, ToolContext, ToolRegistry, ToolResult  # noqa: E402


def _noop(args: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(text=f"ok:{args.get('x', '')}")


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("admin_only", "d", {"type": "object", "properties": {}}, _noop, {Scope.ADMIN})
    reg.register("reply_only", "d", {"type": "object", "properties": {}}, _noop, {Scope.REPLY})
    reg.register("multi", "d", {"type": "object", "properties": {}}, _noop,
                 {Scope.REPLY, Scope.FEED_REFLECTION})
    return reg


def test_available_filters_by_scope():
    reg = _make_registry()
    assert {t.name for t in reg.available(Scope.ADMIN)} == {"admin_only"}
    assert {t.name for t in reg.available(Scope.REPLY)} == {"reply_only", "multi"}
    assert {t.name for t in reg.available(Scope.FEED_REFLECTION)} == {"multi"}


def test_openai_schemas_shape():
    reg = _make_registry()
    schemas = reg.openai_schemas(Scope.ADMIN)
    assert schemas == [{
        "type": "function",
        "function": {"name": "admin_only", "description": "d",
                     "parameters": {"type": "object", "properties": {}}},
    }]


def test_execute_routes_and_returns_result():
    reg = _make_registry()
    out = reg.execute("admin_only", {"x": "42"}, ToolContext(state={}, conn=None))
    assert isinstance(out, ToolResult)
    assert out.text == "ok:42"


def test_execute_unknown_tool_is_graceful():
    reg = _make_registry()
    out = reg.execute("nope", {}, ToolContext(state={}, conn=None))
    assert "desconocida" in out.text


def test_disabled_tool_hidden_and_not_executed():
    reg = _make_registry()
    reg.apply_config({"admin_only": {"enabled": False}})
    assert reg.available(Scope.ADMIN) == []
    out = reg.execute("admin_only", {}, ToolContext(state={}, conn=None))
    assert "deshabilitada" in out.text


def test_config_can_override_scopes():
    reg = _make_registry()
    reg.apply_config({"admin_only": {"scopes": ["reply"]}})
    assert {t.name for t in reg.available(Scope.REPLY)} == {"admin_only", "reply_only", "multi"}
    assert reg.available(Scope.ADMIN) == []


def test_config_ignores_unknown_tool_and_bad_scope():
    reg = _make_registry()
    # tool inexistente + scope inválido no deben romper el arranque
    reg.apply_config({"ghost": {"enabled": True}, "multi": {"scopes": ["reply", "bogus"]}})
    assert {t.name for t in reg.available(Scope.REPLY)} == {"reply_only", "multi"}


def test_duplicate_registration_raises():
    reg = ToolRegistry()
    reg.register("dup", "d", {"type": "object"}, _noop, {Scope.ADMIN})
    try:
        reg.register("dup", "d", {"type": "object"}, _noop, {Scope.ADMIN})
    except ValueError:
        return
    raise AssertionError("registrar duplicado debía lanzar ValueError")


def test_invalid_scope_at_registration_raises():
    reg = ToolRegistry()
    try:
        reg.register("bad", "d", {"type": "object"}, _noop, {"nope"})
    except ValueError:
        return
    raise AssertionError("scope inválido debía lanzar ValueError")


def _make_group_registry() -> ToolRegistry:
    reg = _make_registry()
    reg.register("music", "d", {"type": "object", "properties": {}}, _noop,
                 {Scope.REPLY}, groups={"music_users"})
    reg.set_groups({"music_users": ["@Fulano.bsky.social", "mengano.com"]},
                   admin_handle="ppolci.com")
    return reg


def test_groups_filter_available_by_handle():
    reg = _make_group_registry()
    # miembro (con @ y mayúsculas en la config → normalizado)
    assert "music" in {t.name for t in reg.available(Scope.REPLY, handle="fulano.bsky.social")}
    # no-miembro: la tool restringida desaparece, las libres quedan
    names = {t.name for t in reg.available(Scope.REPLY, handle="otro.bsky.social")}
    assert names == {"reply_only", "multi"}
    # sin handle (contexto sin solicitante): no hay filtro de grupo
    assert "music" in {t.name for t in reg.available(Scope.REPLY)}


def test_groups_admin_bypasses():
    reg = _make_group_registry()
    assert "music" in {t.name for t in reg.available(Scope.REPLY, handle="ppolci.com")}


def test_groups_execute_enforces():
    reg = _make_group_registry()
    ctx = ToolContext(state={}, conn=None)
    assert reg.execute("music", {"x": "1"}, ctx, handle="mengano.com").text == "ok:1"
    out = reg.execute("music", {}, ctx, handle="otro.bsky.social")
    assert "permiso" in out.text
    # sin handle no se chequea (contextos internos)
    assert reg.execute("music", {"x": "2"}, ctx).text == "ok:2"


def test_groups_unknown_group_is_closed():
    reg = _make_group_registry()
    reg.apply_config({"reply_only": {"groups": ["typo_grupo"]}})
    # grupo inexistente en USER_GROUPS → nadie entra (salvo admin)
    assert "reply_only" not in {t.name for t in reg.available(Scope.REPLY, handle="fulano.bsky.social")}
    assert "reply_only" in {t.name for t in reg.available(Scope.REPLY, handle="ppolci.com")}


def test_groups_via_apply_config_and_clear():
    reg = _make_group_registry()
    reg.apply_config({"multi": {"groups": ["music_users"]}})
    assert "multi" not in {t.name for t in reg.available(Scope.REPLY, handle="otro.bsky.social")}
    # groups vacío/null = sin restricción de nuevo
    reg.apply_config({"multi": {"groups": []}})
    assert "multi" in {t.name for t in reg.available(Scope.REPLY, handle="otro.bsky.social")}


def test_groups_feed_ref_resolves_members():
    reg = _make_registry()
    reg.register("music", "d", {"type": "object", "properties": {}}, _noop,
                 {Scope.REPLY}, groups={"music_users"})
    calls = []
    def resolver(name):
        calls.append(name)
        return ["@Alguien.bsky.social", "otra.com"] if name == "polcifeed" else []
    reg.set_groups({"music_users": ["estatico.com", "feed:polcifeed"]},
                   admin_handle="ppolci.com", feed_resolver=resolver)
    # miembro estático no consulta el feed; miembro por feed sí (normalizado)
    assert "music" in {t.name for t in reg.available(Scope.REPLY, handle="estatico.com")}
    assert "music" in {t.name for t in reg.available(Scope.REPLY, handle="alguien.bsky.social")}
    assert "music" not in {t.name for t in reg.available(Scope.REPLY, handle="intruso.com")}
    assert calls == ["polcifeed", "polcifeed"]  # solo los dos checks que no matchearon estático


def test_groups_feed_ref_resolver_failure_is_closed():
    reg = _make_registry()
    reg.register("music", "d", {"type": "object", "properties": {}}, _noop,
                 {Scope.REPLY}, groups={"music_users"})
    def bomb(name):
        raise RuntimeError("red caída")
    reg.set_groups({"music_users": ["feed:polcifeed"]},
                   admin_handle="ppolci.com", feed_resolver=bomb)
    # resolver roto → cerrado para usuarios, pero no explota y el admin sigue entrando
    assert "music" not in {t.name for t in reg.available(Scope.REPLY, handle="alguien.com")}
    assert "music" in {t.name for t in reg.available(Scope.REPLY, handle="ppolci.com")}


def test_groups_feed_ref_without_resolver_is_closed():
    reg = _make_registry()
    reg.register("music", "d", {"type": "object", "properties": {}}, _noop,
                 {Scope.REPLY}, groups={"music_users"})
    reg.set_groups({"music_users": ["feed:polcifeed"]}, admin_handle="ppolci.com")
    assert "music" not in {t.name for t in reg.available(Scope.REPLY, handle="alguien.com")}


def test_no_groups_configured_keeps_everything_open():
    reg = _make_registry()  # sin set_groups: back-compat total
    assert {t.name for t in reg.available(Scope.REPLY, handle="cualquiera.bsky.social")} \
        == {"reply_only", "multi"}


if __name__ == "__main__":
    # Runner sin dependencias (corre con `python tests/test_tools.py`). Sigue siendo
    # compatible con pytest si más adelante se agrega al entorno.
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
