# SPDX-License-Identifier: Apache-2.0
"""A LIFO stack: the last document pushed is the first popped."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_lifo", if_not_exists=True)
    undo = client.database("example_lifo").create_bucket("undo", "lifo", if_not_exists=True)

    for step in ("type a", "type b", "delete line"):
        undo.push({"step": step})

    while popped := undo.pop():  # a list of one; [] once the stack is empty
        print(popped[0]["step"])  # delete line, type b, type a
    print(undo.pop())  # []

    client.drop_database("example_lifo")
