# SPDX-License-Identifier: Apache-2.0
"""A heap: the document with the highest priority is popped first."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_heap", if_not_exists=True)
    tickets = client.database("example_heap").create_bucket("tickets", "heap", if_not_exists=True)

    tickets.push({"title": "typo in footer"}, priority=1)
    tickets.push({"title": "site is down"}, priority=9)
    tickets.push({"title": "slow report"}, priority=5)

    for _ in range(3):
        ticket = tickets.pop()
        assert ticket is not None
        print(ticket["title"])  # site is down, slow report, typo in footer
    print(tickets.pop())  # None

    client.drop_database("example_heap")
