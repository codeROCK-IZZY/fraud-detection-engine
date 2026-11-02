"""Model artifact storage and the in-process model cache.

Artifacts are stored as one joblib file per model version, either on local disk
(development, Docker volume) or in S3 (AWS). Each artifact's SHA-256 is saved in
``model_versions`` and verified before loading: joblib/pickle can execute code
when loaded, so we only ever load files we produced, and refuse files that changed.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ServiceUnavailable
from app.core.logging import log_extra
from app.db.models import ModelVersion
from app.ml.training import ModelBundle

logger = logging.getLogger(__name__)


class ArtifactStore:
    def save(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def load(self, uri: str) -> bytes:
        raise NotImplementedError


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save(self, key: str, data: bytes) -> str:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact key escapes the artifact directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}"

    def load(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://")).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path outside the artifact directory")
        return path.read_bytes()


class S3ArtifactStore(ArtifactStore):
    """S3 storage for AWS deployments. Credentials come from the IAM role (never keys in config).

    Not exercised by the automated tests (no AWS in CI) — see docs/known-limitations.md.
    """

    def __init__(self, bucket: str):
        import boto3  # optional dependency, only needed on AWS

        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def save(self, key: str, data: bytes) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
        return f"s3://{self.bucket}/{key}"

    def load(self, uri: str) -> bytes:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        if bucket != self.bucket:
            raise ValueError("artifact bucket mismatch")
        return bytes(self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def get_store() -> ArtifactStore:
    s = get_settings()
    return S3ArtifactStore(s.artifact_s3_bucket) if s.artifact_s3_bucket else LocalArtifactStore(s.artifact_dir)


def serialize(bundle: ModelBundle) -> bytes:
    buf = io.BytesIO()
    joblib.dump(bundle, buf, compress=3)
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def register(db: Session, bundle: ModelBundle, metrics: dict[str, Any], activate: bool) -> ModelVersion:
    """Persist a trained bundle and create its ``model_versions`` row (caller commits)."""
    name = get_settings().model_name
    latest = db.scalar(
        select(ModelVersion.version).where(ModelVersion.name == name).order_by(ModelVersion.version.desc()).limit(1)
    )
    version = (latest or 0) + 1
    data = serialize(bundle)
    uri = get_store().save(f"{name}/v{version}/bundle.joblib", data)
    mv = ModelVersion(
        name=name,
        version=version,
        algorithm=bundle.algorithm,
        feature_names=list(bundle.feature_names),
        metrics=metrics,
        thresholds=dict(bundle.thresholds),
        reference=bundle.reference,
        artifact_uri=uri,
        artifact_sha256=sha256(data),
        data_snapshot={**bundle.trained_on, "library_versions": bundle.library_versions},
        is_active=False,
    )
    db.add(mv)
    db.flush()
    if activate:
        activate_version(db, mv)
    return mv


def activate_version(db: Session, mv: ModelVersion) -> None:
    """Make ``mv`` the only active version of its name (rollback = activating an older one)."""
    db.execute(
        update(ModelVersion).where(ModelVersion.name == mv.name, ModelVersion.id != mv.id).values(is_active=False)
    )
    db.flush()
    mv.is_active = True
    mv.activated_at = datetime.now(UTC)
    db.flush()


@dataclass(frozen=True)
class VersionInfo:
    """Plain snapshot of a ``model_versions`` row. The cache outlives the request that
    loaded it, so it must not hold an ORM object (that would be detached and expired)."""

    id: int
    name: str
    version: int
    algorithm: str


@dataclass(frozen=True)
class LoadedModel:
    version: VersionInfo
    bundle: ModelBundle


_cache: dict[int, LoadedModel] = {}
_lock = threading.Lock()


def load_version(mv: ModelVersion) -> LoadedModel:
    with _lock:
        if mv.id in _cache:
            return _cache[mv.id]
        data = get_store().load(mv.artifact_uri)
        if sha256(data) != mv.artifact_sha256:
            raise ServiceUnavailable("Model artifact failed integrity check; refusing to load it")
        bundle = joblib.load(io.BytesIO(data))
        _cache.clear()  # keep only one model in memory
        _cache[mv.id] = LoadedModel(VersionInfo(mv.id, mv.name, mv.version, mv.algorithm), bundle)
        logger.info("model loaded", extra=log_extra(model=mv.name, version=mv.version))
        return _cache[mv.id]


def get_active(db: Session) -> LoadedModel:
    mv = active_version(db)
    if mv is None:
        raise ServiceUnavailable("No active model. An admin must train and activate a model first.")
    return load_version(mv)


def active_version(db: Session) -> ModelVersion | None:
    return db.scalar(select(ModelVersion).where(ModelVersion.name == get_settings().model_name, ModelVersion.is_active))


def get_active_or_none(db: Session) -> LoadedModel | None:
    mv = active_version(db)
    return load_version(mv) if mv is not None else None


def has_active(db: Session) -> bool:
    return (
        db.scalar(select(ModelVersion.id).where(ModelVersion.name == get_settings().model_name, ModelVersion.is_active))
        is not None
    )
