from pymongo import MongoClient
from pymongo.database import Database
from config import settings

class MongoDB:
    """MongoDB connection manager"""

    client: MongoClient = None
    db: Database = None

    @classmethod
    def connect(cls):
        """Connect to MongoDB"""
        cls.client = MongoClient(settings.mongodb_url)
        cls.db = cls.client[settings.database_name]
        print(f"Connected to MongoDB at {settings.mongodb_url}")

    @classmethod
    def close(cls):
        """Close MongoDB connection"""
        if cls.client:
            cls.client.close()
            print("MongoDB connection closed")

    @classmethod
    def get_database(cls) -> Database:
        """Get database instance"""
        return cls.db

# Database collections
def get_manifests_collection():
    """Get manifests collection"""
    return MongoDB.get_database()["manifests"]

def get_soft_bindings_collection():
    """Get soft bindings collection"""
    return MongoDB.get_database()["soft_bindings"]

def get_supported_algorithms_collection():
    """Get supported algorithms collection"""
    return MongoDB.get_database()["supported_algorithms"]
