"""Configuration from the environment — the .env.example contract."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    database_url: str | None
    sqlite_path: str
    evidence_dir: str
    evidence_bucket: str
    model_profile: str
    bedrock_model_arn: str | None
    aws_region: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ.get("DATABASE_URL") or None,
            sqlite_path=os.environ.get("RETURNS_SQLITE_PATH", "./data/returns.db"),
            evidence_dir=os.environ.get("RETURNS_EVIDENCE_DIR", "./data/evidence"),
            evidence_bucket=os.environ.get("RETURNS_EVIDENCE_BUCKET", "returns-evidence"),
            model_profile=os.environ.get("RETURNS_MODEL_PROFILE", "offline"),
            bedrock_model_arn=os.environ.get("RETURNS_BEDROCK_MODEL_ARN") or None,
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        )
