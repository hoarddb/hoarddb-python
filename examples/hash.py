# SPDX-License-Identifier: Apache-2.0
"""A hash bucket: keyed documents, read back by key or by a filter.

Run it against a server you trust (see the README): HOARDDB_URI names the server and its pin, and
HOARDDB_ROOT_USER / HOARDDB_ROOT_PASSWORD are the credentials the server printed at its first start.
"""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_hash", if_not_exists=True)
    users = client.database("example_hash").create_bucket("users", "hash", if_not_exists=True)

    users.put("ada", {"name": "Ada", "age": 36})
    users.put("grace", {"name": "Grace", "age": 85})
    ada = users.get("ada")
    assert ada is not None
    print(ada["name"])  # Ada
    print(users.get("nobody"))  # None: a miss is a value, not an error
    print(users.exists("grace"))  # True

    after = users.update("ada", {"$inc": {"age": 1}})
    assert after is not None
    print(after["age"])  # 37

    print(sorted(u["name"] for u in users.find({"age": {"$gte": 40}})))  # ['Grace']
    print(users.count())  # 2
    print(users.delete("grace"), users.delete("grace"))  # True False

    client.drop_database("example_hash")
