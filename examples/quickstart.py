# SPDX-License-Identifier: Apache-2.0
import hoarddb

# connect() with no argument reads HOARDDB_URI; the user and password come from
# HOARDDB_ROOT_USER and HOARDDB_ROOT_PASSWORD (the user defaults to "admin").
with hoarddb.connect() as client:
    client.create_database("app", if_not_exists=True)
    users = client.database("app").create_bucket("users", "hash", if_not_exists=True)

    users.upsert("ada", {"name": "Ada", "age": 36})     # put() would refuse a second run
    print(users.get("ada")["name"])                        # Ada
    for user in users.find({"age": {"$gte": 30}}):
        print(user["name"])                                 # Ada
