#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Boeger IT Sh.p.k
#
# Licensed under the Apache License, Version 2.0; see LICENSE.

# Downloads the test server binary named in testing/server.json for this
# machine, checks its SHA-256, caches it under .cache/server/<commit>/ and
# prints its path (and nothing else) on stdout.
#
#   export HOARDDB_SERVER_BIN=$(scripts/fetch-server.sh)
#
# Needs GITHUB_TOKEN or GH_TOKEN with read access to the repository's releases,
# and curl, jq and sha256sum. GITHUB_REPOSITORY (owner/name) overrides the
# default repository, hoarddb/hoarddb-go, which would hold the test-server-<12 hex>
# pre-releases. No test-server release is published yet, so this fails with
# HTTP 404 until one is; until then set HOARDDB_SERVER_BIN to a hoardDB-server
# binary you built.
#
# The token is never put on a command line (where `ps` would show it): it is
# written to a private temporary file that curl reads with --config.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$root/testing/server.json"
repository="${GITHUB_REPOSITORY:-hoarddb/hoarddb-go}"
token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
api="https://api.github.com/repos/$repository"

fail() { echo "fetch-server.sh: $*" >&2; exit 1; }

for tool in curl jq sha256sum; do
  command -v "$tool" >/dev/null || fail "$tool is required and was not found on PATH: install it with your package manager (for example 'sudo apt-get install $tool')"
done
[ -n "$token" ] || fail "no token: set GITHUB_TOKEN or GH_TOKEN to a token that can read the releases of $repository (for example GITHUB_TOKEN=\$(gh auth token))"
[ -f "$manifest" ] || fail "$manifest does not exist"

case "$(uname -s)" in
  Linux) os=linux ;;
  *) fail "no test server binary is published for $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) fail "no test server binary is published for $(uname -m)" ;;
esac
platform="$os-$arch"

tag="$(jq -er '.tag' "$manifest")"
commit="$(jq -er '.server_commit' "$manifest")"
name="$(jq -er --arg p "$platform" '.assets[$p].name' "$manifest")" || fail "$manifest has no asset for $platform"
want="$(jq -er --arg p "$platform" '.assets[$p].sha256' "$manifest")"

# Keyed by tag as well as commit, so a re-cut release at the same commit is not
# mistaken for the old one (the checksum below would catch it, but only after a
# stale hit).
directory="$root/.cache/server/$tag-$commit"
target="$directory/$name"

sum_of() { sha256sum "$1" | cut -d' ' -f1; }

if [ -x "$target" ] && [ "$(sum_of "$target")" = "$want" ]; then
  echo "$target"
  exit 0
fi

mkdir -p "$directory"
scratch="$(mktemp -d)"
partial="$directory/$name.partial.$$"
trap 'rm -rf "$scratch"; rm -f "$partial"' EXIT
curl_config="$scratch/curl.conf"
(umask 077; printf 'header = "Authorization: Bearer %s"\n' "$token" >"$curl_config")

# fetch <url> <accept> <output file>: prints the HTTP status (000 when no answer came).
fetch() {
  curl -sSL --config "$curl_config" -H "Accept: $2" -o "$3" -w '%{http_code}' "$1" 2>"$scratch/curl.err" || true
}

explain_status() { # <status> <what>
  case "$1" in
    401 | 403) fail "$2: HTTP $1, the token was refused or cannot read $repository. Use a token with read access to that repository's releases (a classic token needs 'repo', a fine-grained one 'Contents: read'), and check it has not expired" ;;
    404) fail "$2: HTTP 404. Either the release does not exist on $repository yet (no test-server release is published yet: build hoardDB-server yourself and set HOARDDB_SERVER_BIN; the tag the pin names is in $manifest), or the token cannot see the repository (GitHub answers 404 for a private repository the token cannot read)" ;;
    000) fail "$2: no answer from api.github.com ($(tr '\n' ' ' <"$scratch/curl.err")). Check the network, then retry" ;;
    *) fail "$2: HTTP $1" ;;
  esac
}

release_file="$scratch/release.json"
status="$(fetch "$api/releases/tags/$tag" "application/vnd.github+json" "$release_file")"
[ "$status" = 200 ] || explain_status "$status" "cannot read release $tag of $repository"

asset_id="$(jq -er --arg n "$name" '.assets[] | select(.name == $n) | .id' "$release_file")" \
  || fail "release $tag of $repository has no asset named $name (it has: $(jq -r '[.assets[].name] | join(", ")' "$release_file")). Fix the name in $manifest or publish that asset"

status="$(fetch "$api/releases/assets/$asset_id" "application/octet-stream" "$partial")"
[ "$status" = 200 ] || explain_status "$status" "cannot download $name from release $tag of $repository"

got="$(sum_of "$partial")"
if [ "$got" != "$want" ]; then
  fail "checksum mismatch for $name: $manifest says $want, the download is $got (download deleted). If the release was re-cut on purpose, update the sha256 in $manifest; otherwise do not run this binary"
fi

chmod 0755 "$partial"
mv "$partial" "$target"
echo "$target"
