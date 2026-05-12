"""
Initialize MongoDB with sample data for local testing.

Seeds:
- two example manifests in the ``manifests`` collection (with GridFS blobs),
- a couple of soft-binding rows pointing at them.

The supported-algorithms list is no longer seeded here — it's read from
``plugins.yaml`` at request time. Add new plugin entries to that file.
"""
import asyncio
import base64

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from pymongo import ASCENDING

from resolution_api.core.config import settings

SAMPLE_MANIFESTS = [
    (
        "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
        b"Sample C2PA active manifest data",
        b"Sample C2PA manifest store data",
    ),
    (
        "urn:c2pa:A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
        b"Another C2PA active manifest",
        b"Another C2PA manifest store",
    ),
]


async def init_database() -> None:
    client = AsyncIOMotorClient(settings.mongodb_url, uuidRepresentation="standard")
    db = client[settings.database_name]
    fs = AsyncIOMotorGridFSBucket(db, bucket_name="manifest_blobs")

    print("Clearing existing collections...")
    await db.manifests.delete_many({})
    await db.soft_bindings.delete_many({})
    await db["manifest_blobs.files"].delete_many({})
    await db["manifest_blobs.chunks"].delete_many({})

    print("Creating indexes...")
    await db.soft_bindings.create_index(
        [("alg", ASCENDING), ("value", ASCENDING)], name="alg_value_idx"
    )
    await db.soft_bindings.create_index(
        [("manifestId", ASCENDING)], name="manifestId_idx"
    )
    await db.soft_bindings.create_index(
        [("alg", ASCENDING), ("value", ASCENDING), ("manifestId", ASCENDING)],
        name="binding_unique",
        unique=True,
    )

    print("Inserting sample manifests...")
    for manifest_id, active, store in SAMPLE_MANIFESTS:
        active_id = await fs.upload_from_stream(f"{manifest_id}.active", active)
        store_id = await fs.upload_from_stream(f"{manifest_id}.store", store)
        await db.manifests.insert_one({
            "_id": manifest_id,
            "activeManifestFileId": active_id,
            "manifestStoreFileId": store_id,
        })

    print("Inserting sample soft bindings...")
    sample_bindings = [
        {
            "alg": "me.deepmark.audio.aware.20",
            "value": base64.b64encode(b"watermark_value_123").decode(),
            "manifestId": SAMPLE_MANIFESTS[0][0],
        },
        {
            "alg": "me.deepmark.audio.aware.20",
            "value": base64.b64encode(b"fingerprint_abc").decode(),
            "manifestId": SAMPLE_MANIFESTS[1][0],
        },
    ]
    await db.soft_bindings.insert_many(sample_bindings)

    print("\nDatabase initialized successfully.")
    print(f"  - {len(SAMPLE_MANIFESTS)} manifests")
    print(f"  - {len(sample_bindings)} soft bindings")
    print("(Supported algorithms come from plugins.yaml — not Mongo.)")

    client.close()


def cli() -> None:
    asyncio.run(init_database())


if __name__ == "__main__":
    cli()
