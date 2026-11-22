"""Read-only connectivity check for MediGuide's configured persistent database."""
import os
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    options = {"tlsCAFile": certifi.where()} if uri.startswith("mongodb+srv://") else {}
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, **options) as client:
            client.admin.command("ping")
            client[os.getenv("MONGODB_DB_NAME", "mediguide")].reports.find_one({}, {"_id": 1})
    except (PyMongoError, ValueError) as exc:
        print(f"MongoDB check failed ({type(exc).__name__}). Check cluster hostname, DNS, network access and database credentials.")
        return 1
    print("MongoDB ping and report collection read passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
