"""
Script to initialize MongoDB with sample data for testing
Run this script to populate the database with sample manifests and soft bindings
"""
from pymongo import MongoClient
from config import settings
import base64

def init_database():
    """Initialize database with sample data"""

    client = MongoClient(settings.mongodb_url)
    db = client[settings.database_name]

    # Clear existing collections
    print("Clearing existing collections...")
    db.manifests.delete_many({})
    db.soft_bindings.delete_many({})
    db.supported_algorithms.delete_many({})

    # Insert sample supported algorithms
    print("Inserting supported algorithms...")
    db.supported_algorithms.insert_one({
        "_id": "supported_algorithms",
        "watermarks": [
            {"alg": "example.watermark.v1"},
            {"alg": "truepic.c2pa.watermark"}
        ],
        "fingerprints": [
            {"alg": "example.fingerprint.v1"},
            {"alg": "youtube.videoid"}
        ]
    })

    # Insert sample manifests
    print("Inserting sample manifests...")
    sample_manifests = [
        {
            "manifestId": "urn:uuid:12345678-1234-1234-1234-123456789abc",
            "activeManifest": b"Sample C2PA active manifest data",
            "manifestStore": b"Sample C2PA manifest store data"
        },
        {
            "manifestId": "urn:uuid:87654321-4321-4321-4321-cba987654321",
            "activeManifest": b"Another C2PA active manifest",
            "manifestStore": b"Another C2PA manifest store"
        }
    ]
    db.manifests.insert_many(sample_manifests)

    # Insert sample soft bindings
    print("Inserting sample soft bindings...")
    sample_bindings = [
        {
            "alg": "example.watermark.v1",
            "value": base64.b64encode(b"watermark_value_123").decode(),
            "manifestId": "urn:uuid:12345678-1234-1234-1234-123456789abc",
            "similarityScore": 95
        },
        {
            "alg": "example.fingerprint.v1",
            "value": base64.b64encode(b"fingerprint_abc").decode(),
            "manifestId": "urn:uuid:87654321-4321-4321-4321-cba987654321",
            "similarityScore": 88
        },
        {
            "alg": "truepic.c2pa.watermark",
            "value": base64.b64encode(b"truepic_watermark_xyz").decode(),
            "manifestId": "urn:uuid:12345678-1234-1234-1234-123456789abc",
            "similarityScore": 100
        }
    ]
    db.soft_bindings.insert_many(sample_bindings)

    print("\n✅ Database initialized successfully!")
    print(f"\nInserted:")
    print(f"  - {len(sample_manifests)} manifests")
    print(f"  - {len(sample_bindings)} soft bindings")
    print(f"  - 4 supported algorithms (2 watermarks, 2 fingerprints)")

    print("\n📝 Sample query examples:")
    print(f"  - Algorithm: example.watermark.v1")
    print(f"  - Value: {base64.b64encode(b'watermark_value_123').decode()}")
    print(f"  - Manifest ID: urn:uuid:12345678-1234-1234-1234-123456789abc")

    client.close()

if __name__ == "__main__":
    init_database()
