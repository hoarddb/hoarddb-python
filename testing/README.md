# Testing against a real server

The conformance tests replay `testdata/driver-vectors/v1.json` against a real
hoardDB test server. The vectors name the server commit they were recorded from
(`server_commit` at the top of the file); the binary must come from that commit
or a later one that keeps the vectors passing. A binary built from v1.1.0 does,
for the single-node vectors of the opcodes this driver sends.

A test-server download is not available yet: no test-server release is published.
Build `hoarddb-server` from the hoardDB server repository
(`github.com/hoarddb/hoarddb-server`) and point the tests at it:

    export HOARDDB_SERVER_BIN=/path/to/hoarddb-server

The vectors also record opcodes this driver does not send (the grouped batch, the
leased queue verbs, the user verbs, the ring family). The single-node leg replays
the vectors of the opcodes the driver sends and names the rest in its `NOT_SENT`
list. The three-node legs (`tests/conformance/test_leg3_cluster.py` and
`test_leg9_cluster.py`) start the earlier engine's replicated mode, which a
v1.1.0 server refuses to start in, so against v1.1.0 leave them out:

    pytest -q -m server --deselect tests/conformance/test_leg3_cluster.py \
        --deselect tests/conformance/test_leg9_cluster.py

`testing/server.json` and `scripts/fetch-server.sh` are for the download, once a
release exists (the script needs `curl`, `jq` and `sha256sum`); the commit
`server.json` names is an earlier one than the vectors'. The tests that need a
running server read the binary from `HOARDDB_SERVER_BIN` and fail loudly when it
is unset.
