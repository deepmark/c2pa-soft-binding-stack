"""
Script to initialize MongoDB with sample data for testing
Run this script to populate the database with sample manifests and soft bindings
"""
import asyncio
import base64

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from pymongo import ASCENDING

from soft_binding_api.core.config import settings

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

SAMPLE_ALGORITHMS = [
    {"type": "watermark",   "alg": "example.watermark.v1"},
    {"type": "watermark",   "alg": "truepic.c2pa.watermark"},
    {"type": "watermark",   "alg": "me.deepmark.audio.vigil.128"},
    {"type": "fingerprint", "alg": "example.fingerprint.v1"},
    {"type": "fingerprint", "alg": "youtube.videoid"},
]


async def init_database() -> None:
    client = AsyncIOMotorClient(settings.mongodb_url, uuidRepresentation="standard")
    db = client[settings.database_name]
    fs = AsyncIOMotorGridFSBucket(db, bucket_name="manifest_blobs")

    print("Clearing existing collections...")
    await db.manifests.delete_many({})
    await db.soft_bindings.delete_many({})
    await db.supported_algorithms.delete_many({})
    await db.ingestions.delete_many({})
    await db["manifest_blobs.files"].delete_many({})
    await db["manifest_blobs.chunks"].delete_many({})
    await db["ingest_assets.files"].delete_many({})
    await db["ingest_assets.chunks"].delete_many({})

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
    await db.supported_algorithms.create_index(
        [("type", ASCENDING), ("alg", ASCENDING)],
        name="type_alg_unique",
        unique=True,
    )
    await db.ingestions.create_index(
        [("ingestionId", ASCENDING)], name="ingestionId_unique", unique=True
    )
    await db.ingestions.create_index(
        [("alg", ASCENDING), ("bindingValue", ASCENDING)], name="alg_value_idx"
    )

    print("Inserting supported algorithms...")
    await db.supported_algorithms.insert_many(SAMPLE_ALGORITHMS)

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
            "alg": "example.watermark.v1",
            "value": base64.b64encode(b"watermark_value_123").decode(),
            "manifestId": SAMPLE_MANIFESTS[0][0],
            "similarityScore": 95,
        },
        {
            "alg": "example.fingerprint.v1",
            "value": base64.b64encode(b"fingerprint_abc").decode(),
            "manifestId": SAMPLE_MANIFESTS[1][0],
            "similarityScore": 88,
        },
        {
            "alg": "truepic.c2pa.watermark",
            "value": base64.b64encode(b"truepic_watermark_xyz").decode(),
            "manifestId": SAMPLE_MANIFESTS[0][0],
            "similarityScore": 100,
        },
    ]
    await db.soft_bindings.insert_many(sample_bindings)

    print("\nDatabase initialized successfully.")
    print(f"  - {len(SAMPLE_MANIFESTS)} manifests")
    print(f"  - {len(sample_bindings)} soft bindings")
    print(f"  - {len(SAMPLE_ALGORITHMS)} supported algorithms")
    print("\nSample query examples:")
    print("  - Algorithm:   example.watermark.v1")
    print(f"  - Value:       {base64.b64encode(b'watermark_value_123').decode()}")
    print(f"  - Manifest ID: {SAMPLE_MANIFESTS[0][0]}")

    client.close()


def cli() -> None:
    """Console-script entry point."""
    asyncio.run(init_database())


if __name__ == "__main__":
    cli()
