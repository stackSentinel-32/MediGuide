import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from server/.env
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGODB_DB_NAME", "mediguide")

# Try to connect to MongoDB, fallback to mock
try:
    import certifi
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure

    if "mongodb+srv" in MONGO_URI:
        # Atlas connection
        client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=10000,
            socketTimeoutMS=20000,
        )
    else:
        # Local connection
        client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=30000,
            connectTimeoutMS=10000,
            socketTimeoutMS=20000,
        )

    # Test connection
    client.admin.command("ping")
    print("MongoDB connected successfully.")

    # Select database
    db = client[DB_NAME]

    # Collections
    users_collection = db["users"]
    users_collection.create_index("username", unique=True)
    appointments_collection = db["appointments"]
    doctors_collection = db["doctors"]
    reports_collection = db["reports"]

except (ImportError, ConnectionFailure) as e:
    if os.getenv("MEDIGUIDE_MOCK_DB") != "1":
        raise RuntimeError("MongoDB unavailable. Set MEDIGUIDE_MOCK_DB=1 only for isolated demos.") from e
    print("Using mock database for development...")

    # Mock database
    from mongomock import MongoClient as MockClient
    db = MockClient()[DB_NAME]
    users_collection = db["users"]
    users_collection.create_index("username", unique=True)
    appointments_collection = db["appointments"]
    doctors_collection = db["doctors"]
    reports_collection = db["reports"]

reports_collection.create_index([("status", 1), ("uploaded_at", 1)])
