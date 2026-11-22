# SPDX-License-Identifier: Apache-2.0
"""P3: the connection URI parser (trust.md section 6) and the one pin canonicaliser."""

from __future__ import annotations

import random
from typing import Any

import pytest

import vectors
from hoarddb import errors
from hoarddb._core import config, uri
from hoarddb._trust import fingerprint

URI_VECTORS = vectors.uri_vectors()
FP = "SHA256:3b7c1f0a9d2e4c6b8a0f5d3e7c1b9a4f6d2e8c0b5a7f9d1e3c5b7a9f0d2e4c68"
FP2 = "SHA256:0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _expected(parsed: uri.ParsedUri) -> dict[str, Any]:
    pairing = {
        h.host: parsed.pin_for(i)
        for i, h in enumerate(parsed.hosts)
        if parsed.fingerprints and len(parsed.hosts) > 1
    }
    return {
        "hosts": [{"host": h.host, "port": h.port} for h in parsed.hosts],
        "username": parsed.username,
        "password": parsed.password.reveal() if parsed.password is not None else None,
        "database": parsed.database,
        "bucket": parsed.bucket,
        "params": {
            "fingerprints": list(parsed.fingerprints),
            "authToken": parsed.auth_token.reveal() if parsed.auth_token is not None else None,
            "clientName": parsed.client_name,
            "dialTimeoutMs": parsed.dial_timeout_ms,
        },
        **({"pin_for_host": pairing} if pairing else {}),
    }


@pytest.mark.parametrize("case", URI_VECTORS["cases"], ids=lambda c: c["name"])
def test_every_case_parses_to_exactly_expect(case: dict[str, Any]) -> None:
    assert _expected(uri.parse_uri(case["uri"])) == case["expect"]


@pytest.mark.parametrize("case", URI_VECTORS["cases"], ids=lambda c: c["name"])
def test_a_rendered_uri_never_shows_the_password(case: dict[str, Any]) -> None:
    rendered = uri.redact(case["uri"])
    if "redacted" in case:
        assert rendered == case["redacted"]
    password = case["expect"]["password"]
    if password:
        assert password not in rendered
    token = case["expect"]["params"]["authToken"]
    if token:
        assert token not in rendered
        assert "authToken=***" in rendered


@pytest.mark.parametrize("case", URI_VECTORS["invalid"], ids=lambda c: c["name"])
def test_every_refusal_carries_error_contains(case: dict[str, Any]) -> None:
    with pytest.raises(errors.ConfigError) as info:
        uri.parse_uri(case["uri"])
    assert case["error_contains"] in info.value.message
    assert info.value.code == "INVALID_URI"


def test_the_defaults_are_asserted_independently_of_the_vector_file() -> None:
    parsed = uri.parse_uri("hoarddb://localhost")
    assert uri.DEFAULT_PORT == 7433
    assert parsed.hosts == (uri.HostPort("localhost", 7433),)
    assert parsed.dial_timeout_ms == 10000


def test_the_vector_file_has_its_documented_size() -> None:
    assert (len(URI_VECTORS["cases"]), len(URI_VECTORS["invalid"])) == (19, 20)


def test_the_srv_scheme_is_refused_by_name() -> None:
    with pytest.raises(errors.ConfigError, match="DNS SRV lookups are not supported"):
        uri.parse_uri("hoarddb+srv://cluster.example.com/app")


def test_a_foreign_scheme_message_echoes_what_was_typed_and_names_the_right_one() -> None:
    with pytest.raises(errors.ConfigError) as info:
        uri.parse_uri("http://localhost/app")
    assert info.value.message == 'scheme "http" is not a hoardDB scheme; use hoarddb://'


def test_a_missing_scheme_is_refused_with_the_shape() -> None:
    with pytest.raises(errors.ConfigError, match=r"hoarddb://"):
        uri.parse_uri("localhost:7433")


@pytest.mark.parametrize(
    "name",
    [
        "tls", "ssl", "insecure", "sslVerify", "srv", "replicaSet", "readPreference", "w",
        "journal", "authSource", "authMechanism", "retryWrites", "directConnection",
    ],
)  # fmt: skip
def test_each_refused_by_name_parameter_says_why(name: str) -> None:
    for spelled in (name, name.upper()):
        with pytest.raises(errors.ConfigError) as info:
            uri.parse_uri(f"hoarddb://host/app?{spelled}=1")
        assert f'"{spelled}"' in info.value.message
        assert "accepted parameters" not in info.value.message  # not the unknown-parameter text
        assert len(info.value.message) > 40


def test_an_unknown_parameter_names_the_accepted_set() -> None:
    with pytest.raises(errors.ConfigError) as info:
        uri.parse_uri("hoarddb://host/app?nonsense=1")
    assert info.value.message == (
        'unknown parameter "nonsense"; the accepted parameters are '
        "fingerprint, authToken, clientName, dialTimeoutMs"
    )


def test_two_fingerprints_for_three_hosts_names_both_counts() -> None:
    with pytest.raises(errors.ConfigError) as info:
        uri.parse_uri(f"hoarddb://a,b,c/app?fingerprint={FP}&fingerprint={FP2}")
    assert "2 fingerprints" in info.value.message
    assert "3 hosts" in info.value.message


def test_a_fingerprint_is_canonicalised() -> None:
    parsed = uri.parse_uri(f"hoarddb://h/app?fingerprint={FP.upper().replace('SHA256', 'sha256')}")
    assert parsed.fingerprints == (FP,)


def test_parameter_names_are_case_insensitive_and_only_fingerprint_repeats() -> None:
    parsed = uri.parse_uri("hoarddb://h?CLIENTNAME=etl&DialTimeoutMs=2500")
    assert (parsed.client_name, parsed.dial_timeout_ms) == ("etl", 2500)
    with pytest.raises(errors.ConfigError, match="more than once"):
        uri.parse_uri("hoarddb://h?clientName=a&clientName=b")


@pytest.mark.parametrize("value", ["0", "-5", "1.5", "", "10s"])
def test_a_bad_dial_timeout_is_refused(value: str) -> None:
    with pytest.raises(errors.ConfigError, match="dialTimeoutMs"):
        uri.parse_uri(f"hoarddb://h?dialTimeoutMs={value}")


def test_percent_encoding_is_strict_utf8() -> None:
    assert uri.parse_uri("hoarddb://u:%C3%A9%20x@h").password is not None
    with pytest.raises(errors.ConfigError, match="percent"):
        uri.parse_uri("hoarddb://u:%ZZ@h")
    with pytest.raises(errors.ConfigError, match="UTF-8"):
        uri.parse_uri("hoarddb://u:%FF@h")
    with pytest.raises(errors.ConfigError, match="percent"):
        uri.parse_uri("hoarddb://u:abc%4@h")


def test_control_characters_are_refused_so_a_log_line_cannot_be_forged() -> None:
    with pytest.raises(errors.ConfigError, match="control character"):
        uri.parse_uri("hoarddb://h/app\nINJECTED")


def test_an_unencoded_colon_in_the_password_and_empty_parts_are_refused() -> None:
    with pytest.raises(errors.ConfigError, match="%3A"):
        uri.parse_uri("hoarddb://u:a:b@h")
    with pytest.raises(errors.ConfigError, match="user name"):
        uri.parse_uri("hoarddb://:pw@h")
    assert uri.parse_uri("hoarddb://u:@h").password is not None  # an empty password is legal text


def test_ipv6_literals() -> None:
    assert uri.parse_uri("hoarddb://[::1]:9000").hosts == (uri.HostPort("::1", 9000),)
    assert str(uri.HostPort("::1", 9000)) == "[::1]:9000"
    assert str(uri.HostPort("db", 7433)) == "db:7433"
    with pytest.raises(errors.ConfigError, match=r"\["):
        uri.parse_uri("hoarddb://2001:db8::1/app")
    with pytest.raises(errors.ConfigError, match="IPv6"):
        uri.parse_uri("hoarddb://[not-an-address]/app")
    with pytest.raises(errors.ConfigError, match="closing"):
        uri.parse_uri("hoarddb://[::1/app")


def test_path_shapes() -> None:
    with pytest.raises(errors.ConfigError, match="bucket name"):
        uri.parse_uri("hoarddb://h/app/")
    with pytest.raises(errors.ConfigError, match="database"):
        uri.parse_uri("hoarddb://h//bucket")


def test_parse_host_port_serves_the_hosts_option() -> None:
    assert uri.parse_host_port("db.internal") == uri.HostPort("db.internal", 7433)
    assert uri.parse_host_port("db:7500") == uri.HostPort("db", 7500)
    assert uri.parse_host_port("[::1]:7") == uri.HostPort("::1", 7)
    with pytest.raises(errors.ConfigError):
        uri.parse_host_port("")


def test_redact_never_raises_and_hides_secrets_in_garbage_too() -> None:
    assert uri.redact("hoarddb://u:pw@h/app?authToken=abc&x=1") == (
        "hoarddb://u:***@h/app?authToken=***&x=1"
    )
    assert uri.redact("not a uri at all") == "not a uri at all"
    assert "pw" not in uri.redact("hoarddb://u:pw@@h")


def test_fuzz_the_parser_only_ever_raises_a_config_error() -> None:
    rng = random.Random(0xC0FFEE)
    alphabet = "hoarddb://[]:,@?&=#%/.-_aAfF09 \x00é"
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        if rng.random() < 0.6:
            text = "hoarddb://" + text
        try:
            parsed = uri.parse_uri(text)
        except errors.ConfigError:
            continue
        assert parsed.hosts
        assert all(1 <= h.port <= 65535 for h in parsed.hosts)


# --- the one pin canonicaliser (trust.md section 2) -------------------------------------------


def test_canonicalise_accepts_the_prefix_and_hex_case_insensitively() -> None:
    assert fingerprint.canonicalise(FP) == FP
    assert fingerprint.canonicalise(FP.upper()) == FP
    assert fingerprint.canonicalise("sha256:" + FP[7:].upper()) == FP


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (FP[7:], "SHA256:"),
        ("SHA1:" + FP[7:], "SHA256:"),
        ("SHA256:abcd", "64"),
        ("SHA256:" + "a" * 65, "64"),
        ("SHA256:" + "z" * 64, "hex"),
        (" " + FP, "SHA256:"),
        (FP + "\n", "hex"),
        ("", "SHA256:"),
    ],
)
def test_canonicalise_refuses_anything_that_is_not_the_one_format(bad: str, needle: str) -> None:
    with pytest.raises(fingerprint.FingerprintError) as info:
        fingerprint.canonicalise(bad)
    assert needle in str(info.value)


# --- security review M1: delimiters inside the userinfo ---------------------------------------


@pytest.mark.parametrize(
    ("text", "delimiter", "escape", "secret"),
    [
        ("hoarddb://admin:s3cr/et@db.example/app", "/", "%2F", "s3cr"),
        ("hoarddb://admin:9000/x@db.example", "/", "%2F", "9000/x"),
        ("hoarddb://admin:pa?ss@db.example/app", "?", "%3F", "pa?ss"),
        ("hoarddb://admin:hunter2/x?y@db.example", "/", "%2F", "hunter2"),
    ],
)
def test_a_password_with_an_unencoded_slash_or_question_mark_is_refused_by_name(
    text: str, delimiter: str, escape: str, secret: str
) -> None:
    with pytest.raises(errors.ConfigError) as caught:
        uri.parse_uri(text)
    message = str(caught.value)
    assert f"unencoded '{delimiter}'" in message
    assert escape in message
    assert secret not in message
    assert "db.example" not in message


@pytest.mark.parametrize(
    "text",
    [
        "hoarddb://admin:s3cr/et@db.example/app",
        "hoarddb://admin:9000/x@db.example",
        "hoarddb://admin:pa?ss@db.example/app",
        "hoarddb://admin:hunter2/x?y@db.example",
    ],
)
def test_redact_masks_a_password_that_holds_a_slash_or_question_mark(text: str) -> None:
    rendered = uri.redact(text)
    assert rendered.startswith("hoarddb://admin:***@db.example")
    for secret in ("s3cr", "9000/x", "pa?ss", "hunter2", "et@"):
        assert secret not in rendered


def test_redact_leaves_an_at_sign_without_a_password_alone() -> None:
    assert uri.redact("hoarddb://h/app?clientName=a@b") == "hoarddb://h/app?clientName=a@b"


def test_the_port_text_is_never_quoted_in_the_not_a_number_error() -> None:
    with pytest.raises(errors.ConfigError) as caught:
        uri.parse_uri("hoarddb://n1:hunter2/app")
    assert "hunter2" not in str(caught.value)
    assert "port" in str(caught.value)
    assert "n1" in str(caught.value)


# --- security review L3: a refusal never chains the raw bytes ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hoarddb://u:%ff%fe@h/app",
        "hoarddb://%ff:pw@h/app",
        "hoarddb://u@h/app?authToken=%c3",
        "hoarddb://u@h/%ffx",
    ],
)
def test_a_bad_utf8_refusal_keeps_no_exception_context_holding_the_raw_bytes(text: str) -> None:
    with pytest.raises(errors.ConfigError) as caught:
        uri.parse_uri(text)
    assert "UTF-8" in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


# --- security review L4: a parameter without '=' ------------------------------------------------


@pytest.mark.parametrize("param", ["authToken s3cret", "authToken", "s3cret", "=s3cret"])
def test_a_parameter_without_equals_is_refused_without_quoting_it(param: str) -> None:
    with pytest.raises(errors.ConfigError) as caught:
        uri.parse_uri(f"hoarddb://h?{param}")
    assert "s3cret" not in str(caught.value)
    assert "name=value" in str(caught.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hoarddb://h?authToken s3cret", "hoarddb://h?authToken=***"),
        ("hoarddb://h?authtoken", "hoarddb://h?authtoken=***"),
        ("hoarddb://h?authTokenX=s3cret&clientName=a", "hoarddb://h?authToken=***&clientName=a"),
        ("hoarddb://h?clientName=a&AUTHTOKEN%20s3cret", "hoarddb://h?clientName=a&AUTHTOKEN=***"),
    ],
)
def test_redact_masks_anything_that_starts_with_authtoken(text: str, expected: str) -> None:
    assert uri.redact(text) == expected


# --- security review L5: decoded hosts are validated again ---------------------------------------


@pytest.mark.parametrize("encoded", ["a%00b", "a%0ab", "a%20b", "a%2fb", "a%3ab", "a%40b", "a%7fb"])
def test_a_host_that_decodes_to_a_forbidden_character_is_refused(encoded: str) -> None:
    with pytest.raises(errors.ConfigError, match="host"):
        uri.parse_host_port(encoded)
    with pytest.raises(errors.ConfigError, match="host"):
        uri.parse_uri(f"hoarddb://{encoded}/app")


def test_a_percent_encoded_non_ascii_host_is_still_accepted() -> None:
    assert uri.parse_host_port("b%C3%BCcher.example") == uri.HostPort("bücher.example", 7433)


def test_an_ipv6_zone_is_percent_decoded_and_checked() -> None:
    assert uri.parse_host_port("[fe80::1%25eth0]:9000") == uri.HostPort("fe80::1%eth0", 9000)
    assert uri.parse_host_port("[fe80::1%25en%30]") == uri.HostPort("fe80::1%en0", 7433)
    for bad in ("[fe80::1%25]", "[fe80::1%25a%20b]", "[fe80::1%eth0]", "[fe80::1%25a%2Fb]"):
        with pytest.raises(errors.ConfigError, match="zone"):
            uri.parse_host_port(bad)


@pytest.mark.parametrize("host", ["a b\n", "a/b", "a:b", "", "a\x00b"])
def test_the_hosts_option_checks_a_tuple_host_too(host: str) -> None:
    with pytest.raises(errors.ConfigError, match="host"):
        config.resolve_options("hoarddb://u:p@h", {"hosts": [(host, 7433)]}, {})


def test_the_hosts_option_accepts_an_ordinary_tuple_host() -> None:
    resolved = config.resolve_options("hoarddb://u:p@h", {"hosts": [("db.example", 7433)]}, {})
    assert resolved.hosts == (uri.HostPort("db.example", 7433),)


# --- security review L12: dialTimeoutMs has an upper bound and never leaks a ValueError ----------


@pytest.mark.parametrize("digits", ["9" * 30, "9" * 4301, "9" * 100_000, "3600001"])
def test_an_absurd_dial_timeout_is_a_config_error_not_a_value_error(digits: str) -> None:
    with pytest.raises(errors.ConfigError, match="dialTimeoutMs") as caught:
        uri.parse_uri(f"hoarddb://h?dialTimeoutMs={digits}")
    assert "3600000" in str(caught.value)  # says what the largest accepted value is
    assert digits not in str(caught.value)


def test_the_largest_dial_timeout_and_leading_zeros_are_accepted() -> None:
    assert uri.parse_uri("hoarddb://h?dialTimeoutMs=3600000").dial_timeout_ms == 3_600_000
    assert uri.parse_uri("hoarddb://h?dialTimeoutMs=" + "0" * 5000 + "25").dial_timeout_ms == 25


@pytest.mark.parametrize("value", [1e27, float("inf"), 3601.0, 10**30, 10**400])
def test_the_dial_timeout_option_has_the_same_bound(value: float) -> None:
    with pytest.raises(errors.ConfigError, match="dial_timeout"):
        config.resolve_options("hoarddb://u:p@h", {"dial_timeout": value}, {})


@pytest.mark.parametrize(
    "bad",
    [
        chr(0x17F) + "ha256:" + FP[7:],  # LATIN SMALL LETTER LONG S upper-cases to "S"
        "SHA256" + chr(0xFF1A) + FP[7:],  # fullwidth colon
        "SHA256:" + chr(0x661) * 64,  # Arabic-indic digits
        "SHA256:" + chr(0x212A) * 64,  # KELVIN SIGN lower-cases to "k"
        "S" + chr(0x131) + "A256:" + FP[7:],  # dotless i
    ],
)
def test_the_fingerprint_prefix_and_digits_are_ascii_only(bad: str) -> None:
    with pytest.raises(fingerprint.FingerprintError, match="SHA256:"):
        fingerprint.canonicalise(bad)
