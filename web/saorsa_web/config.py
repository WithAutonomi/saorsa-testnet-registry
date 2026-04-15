import os

from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry",
    )
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
