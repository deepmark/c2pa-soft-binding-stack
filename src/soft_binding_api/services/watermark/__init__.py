"""
Watermark algorithms (embed + detect).

Importing this package registers the current ``me.deepmark.audio.vigil.128``
implementation against the algorithm registry as a side effect, so
``resolve_watermark_embedder(...)`` returns something usable out of the
box.

To swap in a real Vigil-128 embedder/detector later:

1. Replace the function bodies in ``embed.py`` / ``detect.py`` (or import
   them from a new module).
2. Re-run the ``register_watermark`` call below if the function objects
   change.
"""
from soft_binding_api.services.registry import register_watermark
from soft_binding_api.services.watermark.detect import detect
from soft_binding_api.services.watermark.embed import (
    BINDING_ALG,
    BINDING_BITS,
    compute_binding_value,
    embed,
)

register_watermark(BINDING_ALG, embed=embed, detect=detect)

__all__ = [
    "BINDING_ALG",
    "BINDING_BITS",
    "compute_binding_value",
    "embed",
    "detect",
]
