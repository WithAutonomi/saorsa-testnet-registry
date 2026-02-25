import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    DATABASE_URL = os.environ.get(
        "DATABASE_URL", "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry"
    )
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
    WORKFLOW_RUNNER_PAT = os.environ.get("WORKFLOW_RUNNER_PAT", "")
