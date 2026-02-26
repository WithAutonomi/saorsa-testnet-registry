import os

from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry",
    )
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
    WORKFLOW_RUNNER_PAT = os.environ.get("WORKFLOW_RUNNER_PAT", "")
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "jacderida/saorsa-testnet-registry")
    WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "launch-testnet.yml")
