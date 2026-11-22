# Testing against a real server

The conformance tests replay `testdata/driver-vectors/v1.json` against a real
hoardDB test server. The vectors were generated against server commit
`5abce282d5ebda882f49b628d9d8870e1780a656`, so the binary must come from that
commit or a later one that keeps the vectors passing.

A test-server download is not available yet: no test-server release is published.
Build `hoardDB-server` from the hoardDB server repository and point the tests at it:

    export HOARDDB_SERVER_BIN=/path/to/hoardDB-server

`testing/server.json` and `scripts/fetch-server.sh` are for the download, once a
release exists (the script needs `curl`, `jq` and `sha256sum`). The tests that need a running
server read the binary from `HOARDDB_SERVER_BIN` and fail loudly when it is
unset.
