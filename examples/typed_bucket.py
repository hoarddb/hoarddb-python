# SPDX-License-Identifier: Apache-2.0
"""A typed bucket: the server checks field types, and the driver passes its refusal through."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_typed", if_not_exists=True)
    users = client.database("example_typed").create_bucket(
        "users",
        "hash",
        if_not_exists=True,
        field_types=hoarddb.FieldTypes(
            [hoarddb.FieldSpec("Name", "string", required=True), hoarddb.FieldSpec("Born", "int")]
        ),
    )
    users.put("ada", {"Name": "Ada", "Born": 1815})
    ada = users.get("ada")
    assert ada is not None
    print(ada["Born"])  # 1815

    try:
        users.put("bad", {"Name": "Bad", "Born": "eighteen fifteen"})
    except hoarddb.FieldTypeError as refused:
        print("refused:", "Born" in refused.message)  # refused: True

    client.drop_database("example_typed")
