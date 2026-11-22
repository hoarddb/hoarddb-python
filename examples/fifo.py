# SPDX-License-Identifier: Apache-2.0
"""A FIFO queue: the first document pushed is the first popped."""

import hoarddb

with hoarddb.connect() as client:
    client.create_database("example_fifo", if_not_exists=True)
    jobs = client.database("example_fifo").create_bucket("jobs", "fifo", if_not_exists=True)

    for task in ("welcome-email", "resize-photo", "send-invoice"):
        jobs.push({"task": task})

    print([job["task"] for job in jobs.peek(limit=2)])  # the next two, none removed
    for _ in range(3):
        job = jobs.pop()
        assert job is not None
        print(job["task"])  # welcome-email, resize-photo, send-invoice
    print(jobs.pop())  # None: the queue is empty

    client.drop_database("example_fifo")
