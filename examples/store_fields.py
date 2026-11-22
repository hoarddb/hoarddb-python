# SPDX-License-Identifier: Apache-2.0
"""Store fields: a user's own queue. `Purchases` is a FIFO that belongs to each user document."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_fields", if_not_exists=True)
    users = client.database("example_fields").create_bucket(
        "users",
        "hash",
        if_not_exists=True,
        stores=[hoarddb.StoreSpec("Purchases", "fifo")],
    )
    users.put("ada", {"name": "Ada"})
    users.put("grace", {"name": "Grace"})

    ada = users.field("ada", "Purchases")
    grace = users.field("grace", "Purchases")
    ada.push({"item": "tea"})
    ada.push({"item": "books"})
    grace.push({"item": "compiler"})

    print(ada.len(), grace.len())  # 2 1: each user has a queue of their own
    first = ada.pop()
    assert first is not None and first.element is not None
    print(first.element["item"])  # tea
    rest = [e.element["item"] for e in ada.peek(limit=5) if e.element is not None]
    print(rest)  # ['books']

    client.drop_database("example_fields")
