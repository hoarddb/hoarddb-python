# SPDX-License-Identifier: Apache-2.0
"""A blob bucket: bytes with a small metadata document, read back whole."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_blob", if_not_exists=True)
    files = client.database("example_blob").create_bucket("files", "blob", if_not_exists=True)

    written = files.put_blob("hello.txt", b"hello, hoardDB", metadata={"type": "text/plain"})
    print(written.key, written.size)  # hello.txt 14

    blob = files.get_blob("hello.txt")
    assert blob is not None
    print(blob.data.decode(), blob.size, blob.metadata)  # hello, hoardDB 14 {'type': 'text/plain'}
    print(files.get_blob("missing.txt"))  # None

    client.drop_database("example_blob")
