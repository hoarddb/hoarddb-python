# SPDX-License-Identifier: Apache-2.0
"""A LIFO stack: the last document pushed is the first popped."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_lifo", if_not_exists=True)
    undo = client.database("example_lifo").create_bucket("undo", "lifo", if_not_exists=True)

    for step in ("type a", "type b", "delete line"):
        undo.push({"step": step})

    for _ in range(3):
        entry = undo.pop()
        assert entry is not None
        print(entry["step"])  # delete line, type b, type a
    print(undo.pop())  # None

    client.drop_database("example_lifo")
