# SPDX-License-Identifier: Apache-2.0
import asyncio
import hoarddb.aio as hoarddb

async def main() -> None:
    async with hoarddb.connect() as client:
        await client.create_database("app", if_not_exists=True)
        users = await client.database("app").create_bucket("users", "hash", if_not_exists=True)

        await users.upsert("ada", {"name": "Ada", "age": 36})
        print((await users.get("ada"))["name"])
        async for user in users.find({"age": {"$gte": 30}}):
            print(user["name"])

asyncio.run(main())
