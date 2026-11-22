# SPDX-License-Identifier: Apache-2.0
"""What an operation needs to know about the bucket it is aimed at: two names, no state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BucketRef:
    database: str
    name: str

    @property
    def target(self) -> str:
        return f"{self.database}/{self.name}"
