"""Tests del router de modelos (router.py). Los OpenAI clients se construyen pero
nunca se llaman a la red — la lógica de ruteo/fallback se ejercita con un fn inyectado."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from router import ModelRouter, build_router, llm_api_key  # noqa: E402


def test_llm_api_key_generica_y_alias():
    assert llm_api_key({"LLM_API_KEY": "gen"}) == "gen"
    assert llm_api_key({"OPENROUTER_API_KEY": "or"}) == "or"
    # la genérica gana si están las dos
    assert llm_api_key({"LLM_API_KEY": "gen", "OPENROUTER_API_KEY": "or"}) == "gen"
    assert llm_api_key({}) == ""


def test_build_router_legacy_sin_key_corta_claro():
    legacy = {"base_url": "http://x/v1", "api_key": "", "reasoning": "m",
              "lite": "m", "vision": "m"}
    with pytest.raises(SystemExit, match="LLM_API_KEY"):
        build_router(None, legacy=legacy, env={})


def _make_router() -> ModelRouter:
    endpoints = {
        "a": {"base_url": "http://a/v1", "api_key": "x"},
        "b": {"base_url": "http://b/v1", "api_key": "y"},
    }
    aliases = {
        "dual":     [{"endpoint": "a", "model": "m1"}, {"endpoint": "b", "model": "m2"}],
        "solo":     [{"endpoint": "a", "model": "m1"}],
        "withghost": [{"endpoint": "ghost", "model": "g"}, {"endpoint": "a", "model": "m1"}],
    }
    roles = {"r_dual": "dual", "r_solo": "solo", "r_ghost": "withghost", "r_badalias": "missing"}
    return ModelRouter(endpoints, aliases, roles, max_retries=1, backoff_base=0)


def test_clients_con_timeout_y_sin_retries_del_sdk():
    """Sin timeout explícito el SDK default es 600s x 3 intentos internos =
    ~30 min de cuelgue silencioso por request (bug real, 2026-07-23)."""
    r = _make_router()
    for client in r._clients.values():
        assert float(client.timeout) == 120.0  # default sano
        assert client.max_retries == 0         # los reintentos son del router

    endpoints = {"lento": {"base_url": "http://a/v1", "api_key": "x", "timeout_s": 300}}
    r2 = ModelRouter(endpoints, {"al": [{"endpoint": "lento", "model": "m"}]}, {"r": "al"})
    assert float(r2._clients["lento"].timeout) == 300.0


def test_build_router_propaga_timeout_s():
    cfg = {
        "endpoints": {"e": {"base_url": "http://x/v1", "api_key": "k", "timeout_s": 45}},
        "aliases": {"a": [{"endpoint": "e", "model": "m"}]},
        "roles": {"reply": "a"},
    }
    r = build_router(cfg, legacy={}, env={})
    assert float(r._clients["e"].timeout) == 45.0


def test_chain_resolves_role_to_targets():
    r = _make_router()
    chain = r._chain("r_dual")
    assert [(t.endpoint, t.model) for t in chain] == [("a", "m1"), ("b", "m2")]


def test_chain_skips_unknown_endpoint():
    r = _make_router()
    chain = r._chain("r_ghost")
    assert [(t.endpoint, t.model) for t in chain] == [("a", "m1")]


def test_chain_unmapped_role_raises():
    r = _make_router()
    for role in ("nope", "r_badalias"):
        try:
            r._chain(role)
        except KeyError:
            continue
        raise AssertionError(f"rol {role} debía lanzar KeyError")


def test_run_uses_first_target_on_success():
    r = _make_router()
    seen = []
    def fn(client, model):
        seen.append(model)
        return f"ok:{model}"
    assert r._run("r_dual", fn) == "ok:m1"
    assert seen == ["m1"]  # no tocó el fallback


def test_run_falls_back_on_failure():
    r = _make_router()
    seen = []
    def fn(client, model):
        seen.append(model)
        if model == "m1":
            raise RuntimeError("boom")
        return f"ok:{model}"
    assert r._run("r_dual", fn) == "ok:m2"
    assert seen == ["m1", "m2"]  # probó el primero, cayó al segundo


def test_run_raises_when_all_fail():
    r = _make_router()
    def fn(client, model):
        raise RuntimeError("siempre falla")
    try:
        r._run("r_dual", fn)
    except RuntimeError as e:
        assert "agotaron" in str(e)
        return
    raise AssertionError("debía lanzar RuntimeError al agotar endpoints")


def test_build_router_backcompat():
    legacy = {"base_url": "http://x/v1", "api_key": "k",
              "reasoning": "R", "lite": "L", "vision": "V"}
    r = build_router(None, legacy=legacy, env={})
    assert [(t.endpoint, t.model) for t in r._chain("reply")] == [("default", "R")]
    assert [(t.endpoint, t.model) for t in r._chain("classify")] == [("default", "L")]
    assert [(t.endpoint, t.model) for t in r._chain("image_describe")] == [("default", "V")]


def test_build_router_from_config_with_env_and_literal_key():
    cfg = {
        "endpoints": {
            "e":     {"base_url": "http://e/v1", "api_key_env": "MY_KEY"},
            "local": {"base_url": "http://l/v1", "api_key": "ollama"},
        },
        "aliases": {"a": [{"endpoint": "e", "model": "m"}, {"endpoint": "local", "model": "lm"}]},
        "roles":   {"reply": "a"},
    }
    r = build_router(cfg, legacy={}, env={"MY_KEY": "secret"})
    assert [(t.endpoint, t.model) for t in r._chain("reply")] == [("e", "m"), ("local", "lm")]


def test_describe_is_readable():
    r = _make_router()
    d = r.describe()
    assert "r_dual=" in d and "a:m1" in d and "b:m2" in d


if __name__ == "__main__":
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
