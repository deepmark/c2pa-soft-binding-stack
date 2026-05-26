"""
Plugin catalog loader.

Catalog source of truth is the ``supported_algorithms`` collection in
MongoDB (database configured via ``settings.mongodb_database``).
Each entry maps a soft-binding algorithm identifier (``alg``) to the
plugin that implements it:

- its type (``watermark`` or ``fingerprint``),
- its binding value width in bits,
- supported media types,
- a base URL for the plugin container (``url``).
"""
from __future__ import annotations

from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.core.config import settings
from ingestion_api.core.errors import PluginNotFoundError
from ingestion_api.core.logging import get_logger
from ingestion_api.repositories.database import MongoDB

logger = get_logger(__name__)


async def load_plugin_catalog_from_db() -> list[PluginEntry]:
    """Read the supported_algorithms collection and return PluginEntry list.

    Returns an empty list if the collection is empty or unreachable.
    """
    if MongoDB.client is None:
        logger.warning("MongoDB not connected; cannot load plugin catalog")
        return []

    col = MongoDB.client[settings.mongodb_database][settings.supported_algorithms_collection]
    docs = await col.find({}).to_list(length=100)
    out: list[PluginEntry] = []
    for doc in docs:
        try:
            binding_bits = int(doc["bindingBits"])
            if binding_bits <= 0:
                raise ValueError(
                    f"bindingBits must be positive, got {binding_bits}",
                )
            out.append(
                PluginEntry(
                    alg=str(doc["alg"]),
                    type=doc["type"],
                    binding_bits=binding_bits,
                    media_types=tuple(doc.get("mediaTypes") or ()),
                    url=doc.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed algorithm entry (alg=%s): %s",
                doc.get("alg", "?"),
                exc,
            )
    return out


async def resolve_plugin(alg: str) -> PluginEntry:
    """Find a plugin by alg id; raises ``PluginNotFoundError`` if missing.

    Queries the supported_algorithms collection directly each time,
    so newly-registered algorithms are available immediately without
    a service restart.
    """
    if MongoDB.client is None:
        raise PluginNotFoundError(
            f"alg={alg!r} cannot be resolved: MongoDB not connected"
        )

    col = MongoDB.client[settings.mongodb_database][settings.supported_algorithms_collection]
    doc = await col.find_one({"alg": alg})
    if not doc:
        raise PluginNotFoundError(
            f"alg={alg!r} not found in supported_algorithms collection"
        )

    binding_bits = int(doc["bindingBits"])
    if binding_bits <= 0:
        raise PluginNotFoundError(
            f"alg={alg!r} has invalid bindingBits={binding_bits}"
        )

    return PluginEntry(
        alg=str(doc["alg"]),
        type=doc["type"],
        binding_bits=binding_bits,
        media_types=tuple(doc.get("mediaTypes") or ()),
        url=doc.get("url"),
    )
