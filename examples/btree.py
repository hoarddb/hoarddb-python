# SPDX-License-Identifier: Apache-2.0
"""A btree bucket: documents kept in key order, read back as a range."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_btree", if_not_exists=True)
    log = client.database("example_btree").create_bucket("log", "btree", if_not_exists=True)

    for key in ("2026-01-03", "2026-01-01", "2026-01-02", "2026-02-01"):
        log.put(key, {"day": key})

    print([entry.key for entry in log.range()])  # in key order
    print([entry.key for entry in log.range(gte="2026-01-02", lte="2026-01-31")])
    first, last = log.first(), log.last()
    assert first is not None and last is not None
    print(first.key, last.key)  # 2026-01-01 2026-02-01

    client.drop_database("example_btree")
