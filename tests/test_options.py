# SPDX-License-Identifier: Apache-2.0
"""P3: option resolution (spec 4.2): precedence, refused combinations, secrets."""

from __future__ import annotations

import dataclasses
import logging
import math
import pickle
import re
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from hoarddb import errors
from hoarddb._core import diagnostics, failure
from hoarddb._core.config import ResolvedOptions, resolve_options
from hoarddb._secret import Secret
from hoarddb.options import PoolOptions, TlsOptions

FP = "SHA256:3b7c1f0a9d2e4c6b8a0f5d3e7c1b9a4f6d2e8c0b5a7f9d1e3c5b7a9f0d2e4c68"
FP2 = "SHA256:0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
ENV = {"HOARDDB_ROOT_PASSWORD": "env-pw"}


def resolve(
    uri: str | None = None, env: Mapping[str, str] | None = None, **options: Any
) -> ResolvedOptions:
    return resolve_options(uri, options, ENV if env is None else env)


def test_defaults() -> None:
    r = resolve()
    assert [str(h) for h in r.hosts] == ["localhost:7433"]
    assert r.username == "admin"
    assert r.password is not None and r.password.reveal() == "env-pw"
    assert (r.database, r.bucket, r.client_name) == (None, None, None)
    assert (r.dial_timeout, r.request_timeout) == (10.0, 30.0)
    assert r.write_concern is None
    assert r.logger is logging.getLogger("hoarddb")
    assert r.tls == TlsOptions()
    assert r.pool == PoolOptions(size=4, bulk_size=2, max_waiters=1000, idle_timeout=120.0)
    assert r.pins == ()


def test_precedence_is_option_then_uri_then_environment() -> None:
    env = {"HOARDDB_URI": "hoarddb://fromenv/envdb", "HOARDDB_ROOT_USER": "envuser",
           "HOARDDB_ROOT_PASSWORD": "envpw"}  # fmt: skip
    assert [str(h) for h in resolve(None, env).hosts] == ["fromenv:7433"]
    assert resolve(None, env).database == "envdb"
    from_uri = resolve("hoarddb://uu:up@h1:1/udb/ubucket?clientName=cn&dialTimeoutMs=2500", env)
    assert (from_uri.username, from_uri.database, from_uri.bucket) == ("uu", "udb", "ubucket")
    assert from_uri.password is not None and from_uri.password.reveal() == "up"
    assert (from_uri.client_name, from_uri.dial_timeout) == ("cn", 2.5)
    over = resolve(
        "hoarddb://uu:up@h1:1/udb/ubucket?clientName=cn&dialTimeoutMs=2500",
        env,
        username="ou", password="op", database="odb", bucket="ob",
        client_name="ocn", dial_timeout=1.5, hosts=["h2", ("h3", 9)],
    )  # fmt: skip
    assert (over.username, over.database, over.bucket) == ("ou", "odb", "ob")
    assert over.password is not None and over.password.reveal() == "op"
    assert (over.client_name, over.dial_timeout) == ("ocn", 1.5)
    assert [str(h) for h in over.hosts] == ["h2:7433", "h3:9"]
    assert resolve("hoarddb://h", env).username == "envuser"


def test_an_empty_uri_environment_variable_is_unset() -> None:
    assert [str(h) for h in resolve(None, {"HOARDDB_URI": "", **ENV}).hosts] == ["localhost:7433"]


def test_the_client_string_appends_the_program_name() -> None:
    from hoarddb._version import __version__

    assert resolve().client_string == f"hoarddb-python/{__version__}"
    assert resolve(client_name="etl").client_string == f"hoarddb-python/{__version__} (etl)"


def test_no_password_is_refused_before_any_dial_with_the_fix() -> None:
    with pytest.raises(errors.ConfigError) as info:
        resolve("hoarddb://localhost", {})
    assert info.value.code == "NO_PASSWORD"
    assert info.value.message == (
        'No password was given for user "admin" on localhost:7433. Set HOARDDB_ROOT_PASSWORD '
        "(the server prints it once on first start), or pass password=..."
    )
    with pytest.raises(errors.ConfigError, match="No password"):
        resolve("hoarddb://u:@localhost", {})  # an empty password is no password


def test_a_token_alone_is_enough() -> None:
    r = resolve("hoarddb://localhost?authToken=tok", {})
    assert r.password is None
    assert r.auth_token is not None and r.auth_token.reveal() == "tok"
    assert resolve(env={}, auth_token="t2").auth_token is not None


def test_unknown_option_keys_are_refused_naming_the_accepted_set() -> None:
    with pytest.raises(errors.ConfigError) as info:
        resolve(pasword="typo")
    assert info.value.code == "INVALID_OPTIONS"
    assert 'Unknown option "pasword" in connect()' in info.value.message
    assert "the accepted options are" in info.value.message
    for name in ("hosts", "password", "tls", "pool", "dial_timeout", "write_concern", "logger"):
        assert name in info.value.message


def test_tls_and_pool_accept_a_plain_dict_and_refuse_unknown_keys() -> None:
    r = resolve(tls={"fingerprints": [FP]}, pool={"size": 8})
    assert r.tls.fingerprints == (FP,)
    assert r.pool.size == 8
    with pytest.raises(errors.ConfigError, match='Unknown option "szie" in pool'):
        resolve(pool={"szie": 1})
    with pytest.raises(errors.ConfigError, match='Unknown option "fingerprint" in tls'):
        resolve(tls={"fingerprint": FP})
    assert resolve(tls=TlsOptions(use_known_servers=False)).tls.use_known_servers is False


@pytest.mark.parametrize(
    "bad",
    [
        {"dial_timeout": 0}, {"dial_timeout": -1.0}, {"request_timeout": 0},
        {"pool": {"size": 0}}, {"pool": {"bulk_size": 0}}, {"pool": {"max_waiters": -1}},
        {"pool": {"idle_timeout": 0}}, {"hosts": []}, {"hosts": "db"}, {"username": ""},
        {"database": 3}, {"tls": 3}, {"hosts": [("db", 99999)]}, {"hosts": [("db", True)]},
    ],
)  # fmt: skip
def test_bad_option_values_are_refused_locally(bad: dict[str, Any]) -> None:
    with pytest.raises(errors.ConfigError):
        resolve(**bad)


@pytest.mark.parametrize("bad", [math.inf, math.nan, -1.0, 0, 1e300, 10**400, 3601, True, "30"])
def test_request_timeout_is_a_positive_bounded_number(bad: Any) -> None:
    with pytest.raises(errors.ConfigError, match="request_timeout="):
        resolve(request_timeout=bad)
    assert resolve(request_timeout=3600).request_timeout == 3600.0


def test_write_concern_is_checked_locally() -> None:
    assert resolve(write_concern="majority").write_concern == "majority"
    with pytest.raises(errors.InvalidArgumentError, match="one, majority, all"):
        resolve(write_concern="quorum")


# --- pins: one canonicaliser, positional pairing, the refused combinations --------------------


def test_pins_from_the_uri_and_from_tls_options_are_canonicalised_by_the_same_rules() -> None:
    assert resolve(f"hoarddb://h?fingerprint={FP.upper()}").pins == (FP,)
    assert resolve(tls={"fingerprints": [FP.upper()]}).pins == (FP,)
    with pytest.raises(errors.ConfigError, match="64") as from_uri:
        resolve("hoarddb://h?fingerprint=SHA256:abcd")
    with pytest.raises(errors.ConfigError, match="64") as from_tls:
        resolve(tls={"fingerprints": ["SHA256:abcd"]})
    assert (from_uri.value.code, from_tls.value.code) == ("INVALID_URI", "INVALID_OPTIONS")


def test_one_pin_applies_to_every_host_and_pins_pair_by_position() -> None:
    r = resolve(f"hoarddb://a,b,c?fingerprint={FP}")
    assert [r.pin_for(i) for i in range(3)] == [FP, FP, FP]
    r = resolve(f"hoarddb://a,b?fingerprint={FP}&fingerprint={FP2}")
    assert [r.pin_for(i) for i in range(2)] == [FP, FP2]
    assert resolve().pin_for(0) is None


def test_tls_fingerprints_override_the_uri_and_must_match_the_host_count() -> None:
    r = resolve(f"hoarddb://a,b?fingerprint={FP}", tls={"fingerprints": [FP2, FP]})
    assert r.pins == (FP2, FP)
    with pytest.raises(errors.ConfigError) as info:
        resolve("hoarddb://a,b,c", tls={"fingerprints": [FP, FP2]})
    assert "2 fingerprints" in info.value.message
    assert "3 hosts" in info.value.message


def test_insecure_with_any_pin_is_refused_naming_both() -> None:
    insecure = TlsOptions(insecure_reason="local test rig")
    builds: tuple[Callable[[], ResolvedOptions], ...] = (
        lambda: resolve(f"hoarddb://h?fingerprint={FP}", tls=insecure),
        lambda: resolve(tls=TlsOptions(insecure_reason="rig", fingerprints=(FP,))),
        lambda: resolve(f"hoarddb://h?fingerprint={FP}", tls={"insecure_reason": "rig"}),
    )
    for build in builds:
        with pytest.raises(errors.ConfigError) as info:
            build()
        assert info.value.message == (
            "this client was given both a pin and insecure_reason; the pin would be ignored. "
            "Remove one."
        )
    assert resolve(tls=insecure).tls.insecure_reason == "local test rig"


def test_insecure_needs_a_reason() -> None:
    with pytest.raises(errors.ConfigError, match="reason"):
        resolve(tls=TlsOptions(insecure_reason=""))


def test_hosts_with_several_uri_fingerprints_is_refused() -> None:
    uri = f"hoarddb://a,b?fingerprint={FP}&fingerprint={FP2}"
    with pytest.raises(errors.ConfigError) as info:
        resolve(uri, hosts=["c", "d"])
    assert "hosts=" in info.value.message
    assert "TlsOptions.fingerprints" in info.value.message
    ok = resolve(uri, hosts=["c", "d"], tls={"fingerprints": [FP, FP2]})
    assert ok.pins == (FP, FP2)
    assert resolve(f"hoarddb://a?fingerprint={FP}", hosts=["c", "d"]).pins == (FP,)


# --- secrets and immutability ----------------------------------------------------------------


def test_the_resolved_options_never_keep_the_callers_dict_or_a_plain_secret() -> None:
    caller: dict[str, Any] = {"password": "p4ss-sentinel", "auth_token": "t0k-sentinel",
                              "hosts": ["a"]}  # fmt: skip
    r = resolve_options(None, caller, {})
    caller["password"] = "changed"
    caller["hosts"].append("b")
    assert r.password is not None and r.password.reveal() == "p4ss-sentinel"
    assert [str(h) for h in r.hosts] == ["a:7433"]
    assert isinstance(r.password, Secret)
    assert isinstance(r.auth_token, Secret)


def test_the_resolved_options_are_immutable() -> None:
    r = resolve()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.username = "x"  # type: ignore[misc]
    assert not hasattr(r, "__dict__")


def test_the_password_appears_nowhere_in_the_resolved_options() -> None:
    r = resolve(
        "hoarddb://uu:uri-sentinel@h?authToken=uri-token-sentinel",
        password="opt-sentinel",
        auth_token="opt-token-sentinel",
    )
    renderings = [repr(r), str(r), repr(r.password), repr(r.auth_token), r.display_uri]
    renderings.append(repr(pickle.loads(pickle.dumps(r))))
    renderings.append(pickle.dumps(r).decode("latin-1"))
    for text in renderings:
        for secret in ("uri-sentinel", "opt-sentinel", "uri-token-sentinel", "opt-token-sentinel"):
            assert secret not in text
    assert "***" in r.display_uri
    assert "uu:***@h" in r.display_uri
    assert "authToken=***" in r.display_uri


def test_no_error_the_resolver_raises_carries_a_secret() -> None:
    builds: tuple[Callable[[], ResolvedOptions], ...] = (
        lambda: resolve("hoarddb://u:pw-sentinel@h/a/b/c"),
        lambda: resolve("hoarddb://u:pw-sentinel@@h"),
        lambda: resolve("hoarddb://u:pw-sentinel@h?nonsense=1"),
        lambda: resolve("hoarddb://u:pw-sentinel@h", pool={"zzz": 1}),
    )
    for build in builds:
        with pytest.raises(errors.ConfigError) as info:
            build()
        blob = " ".join([repr(info.value), str(info.value), repr(vars(info.value))])
        assert "pw-sentinel" not in blob


def test_display_uri_is_the_redacted_input() -> None:
    assert resolve("hoarddb://u:pw@h/app", {}).display_uri == "hoarddb://u:***@h/app"
    assert resolve().display_uri == "hoarddb://localhost"


# --- every code snippet in every message template (spec 8.5, 11.3) ----------------------------

_SPAN = re.compile(r"`([^`]+)`")


class _Recorder:
    """Stands in for ``hoarddb.connect`` so evaluating a snippet cannot dial."""

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Namespace:
    def __init__(self) -> None:
        import hoarddb

        self._real = hoarddb

    def __getattr__(self, name: str) -> Any:
        if name == "connect":
            return _Recorder()
        return getattr(self._real, name)


def _templates() -> list[str]:
    texts = [v for k, v in vars(failure).items() if k.startswith("HINT_") and isinstance(v, str)]
    for reason in (
        "CREDENTIALS", "TOKEN_EXPIRED", "TOKEN_INVALID", "TOKEN_UNSUPPORTED",
        "PROTOCOL_MISMATCH", "RATE_LIMITED", "ALREADY_AUTHED", "INTERNAL",
    ):  # fmt: skip
        hint = failure._auth_hint(reason, "admin", "localhost:7433")  # pyright: ignore[reportPrivateUsage]
        assert hint is not None
        texts.append(hint)
    texts.append(str(diagnostics.pool_timeout(30.0, 4, 37, "point")))
    texts.append(str(diagnostics.pool_timeout(30.0, 2, 5, "bulk")))
    texts.append(diagnostics.UNKNOWN_DDL_OUTCOME)
    from hoarddb._core.uri import HostPort
    from hoarddb._trust import policy

    host = HostPort("localhost", 7433)
    fp = "SHA256:" + "ab" * 32
    texts.append(str(policy.untrusted(host, fp)))
    texts.append(str(policy.pin_mismatch(host, fp, fp)))
    texts.append(str(policy.key_changed(host, fp, fp, "~/.hoarddb/known_servers")))
    return texts


def test_every_code_snippet_in_every_message_template_is_valid_python_and_names_resolve() -> None:
    import ast

    namespace = {"hoarddb": _Namespace()}
    seen = 0
    for text in _templates():
        for span in _SPAN.findall(text):
            if span.startswith(("python ", "pip ", "chmod ")):
                continue  # a shell command, not Python
            tree = ast.parse(span, mode="eval")  # every marked span is at least valid Python
            seen += 1
            if span.startswith("hoarddb."):
                eval(compile(tree, "<snippet>", "eval"), namespace)
    assert seen >= 8
