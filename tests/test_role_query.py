"""Tests de /check-role: el usuario consulta su rol y permisos.

Cubre el nodo determinístico (HandleRoleQueryNode) para los tres roles y el ruteo
(route_after_classify lo deriva antes del gate admin, abierto a cualquiera).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("BSKY_PASSWORD", "dummy")

import botata as b  # noqa: E402
from tools import Scope, ToolRegistry  # noqa: E402


def _registry():
    reg = ToolRegistry()
    reg.register("search_music", "buscar música", {}, lambda a, c: None, {Scope.REPLY})
    reg.register("solo_power", "exclusiva", {}, lambda a, c: None, {Scope.REPLY},
                 groups={"power_users"})
    # grupo estático (sin depender del feed real): juan es power_user
    reg.set_groups({"power_users": ["juan.bsky.social"]}, admin_handle=b.ADMIN_HANDLE)
    return reg


# ─── nodo determinístico ─────────────────────────────────────────────────────
def test_role_admin():
    out = b.HandleRoleQueryNode(_registry(), None).run(
        {"author_handle": b.ADMIN_HANDLE, "is_admin": True})
    assert "admin" in out["reply_text"].lower()
    assert "acceso total" in out["reply_text"].lower()
    assert len(out["reply_text"]) <= 300


def test_role_group_member_ve_su_grupo_y_tool_gateada():
    out = b.HandleRoleQueryNode(_registry(), None).run(
        {"author_handle": "juan.bsky.social", "is_admin": False})
    txt = out["reply_text"]
    assert "power_users" in txt
    assert "solo_power" in txt          # la tool gateada al grupo aparece
    assert "search_music" in txt        # las ungated también
    assert len(txt) <= 300


def test_role_community_no_ve_tool_gateada():
    out = b.HandleRoleQueryNode(_registry(), None).run(
        {"author_handle": "random.bsky.social", "is_admin": False})
    txt = out["reply_text"]
    assert "comunidad" in txt.lower()
    assert "solo_power" not in txt      # gateada → NO visible para la comunidad
    assert "search_music" in txt        # ungated → sí
    assert len(txt) <= 300


# ─── ruteo ───────────────────────────────────────────────────────────────────
class _Cls:
    def __init__(self, **kw):
        self.is_admin_command = kw.get("is_admin_command", False)
        self.command = kw.get("command")
        self.skip = kw.get("skip", False)
        self.is_block_query = kw.get("is_block_query", False)
        self.is_role_query = kw.get("is_role_query", False)


def test_route_role_query_antes_del_gate_admin():
    # role_query se dirige a handle_role_query aunque el autor NO sea admin
    state = {"classification": _Cls(is_role_query=True), "is_admin": False}
    assert b.route_after_classify(state) == "handle_role_query"


def test_route_admin_command_sigue_gateado():
    # un comando de admin de un no-admin NO va a admin (control: el gate sigue vivo)
    state = {"classification": _Cls(is_admin_command=True, command="config"),
             "is_admin": False}
    assert b.route_after_classify(state) == "load_context"


def test_groups_for():
    reg = _registry()
    assert reg.groups_for("juan.bsky.social") == ["power_users"]
    assert reg.groups_for("random.bsky.social") == []
    assert reg.groups_for(None) == []


# ─── multi-admin ─────────────────────────────────────────────────────────────
def test_is_admin_handle_owner_y_extra(monkeypatch):
    monkeypatch.setattr(b, "ADMIN_HANDLES",
                        frozenset([b.ADMIN_HANDLE, "segundo.bsky.social"]))
    assert b.is_admin_handle(b.ADMIN_HANDLE)
    assert b.is_admin_handle("segundo.bsky.social")
    assert not b.is_admin_handle("random.bsky.social")
    assert not b.is_admin_handle(None)


def test_set_groups_acepta_multiples_admins():
    from tools import Scope, ToolRegistry
    reg = ToolRegistry()
    reg.register("solo_power", "x", {}, lambda a, c: None, {Scope.REPLY},
                 groups={"power_users"})
    reg.set_groups({"power_users": ["juan.bsky.social"]},
                   admin_handle=["ppolci.com", "segundo.bsky.social"])
    t = reg.get("solo_power")
    # ambos admins bypassean el gate de grupo; un ajeno no
    assert reg.allowed_for(t, "ppolci.com")
    assert reg.allowed_for(t, "segundo.bsky.social")
    assert not reg.allowed_for(t, "random.bsky.social")
