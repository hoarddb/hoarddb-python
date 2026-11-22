# SPDX-License-Identifier: Apache-2.0
"""The codec seam: ``serialize(document) -> bytes`` and ``deserialize(bytes) -> dict``.

No codec type appears in a public signature, docstring or example (spec 7.1): callers see ``dict``,
``bytes`` and ``hoarddb.Opaque`` only.
"""

from hoarddb._bson.decode import MAX_DEPTH, deserialize
from hoarddb._bson.encode import serialize
from hoarddb._bson.values import Opaque

__all__ = ["MAX_DEPTH", "Opaque", "deserialize", "serialize"]
