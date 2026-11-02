"""Hand-off of uploaded CSV files from the API process to the worker.

* Local (docker compose): API and worker share a volume; the file stays on disk.
* S3 (AWS): API and worker run as separate ECS tasks with separate filesystems,
  so the API uploads the file to ``s3://<bucket>/uploads/<batch>.csv`` and the
  worker downloads it to a temporary file. The object is deleted after import.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.errors import UnprocessableData

UPLOAD_PREFIX = "uploads/"


def _s3() -> Any:
    import boto3

    return boto3.client("s3")


def publish(local_path: Path, batch_id: str) -> dict[str, str]:
    """Make an uploaded file available to the worker; returns the job payload fields."""
    bucket = get_settings().artifact_s3_bucket
    if not bucket:
        return {"path": str(local_path)}
    key = f"{UPLOAD_PREFIX}{batch_id}.csv"
    _s3().upload_file(str(local_path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    local_path.unlink(missing_ok=True)
    return {"s3_key": key}


@contextmanager
def fetch(payload: dict[str, Any]) -> Iterator[Path]:
    """Yield a local path for the job's file; removes the file (and S3 object) afterwards."""
    s = get_settings()
    if "s3_key" in payload:
        key = str(payload["s3_key"])
        if not key.startswith(UPLOAD_PREFIX) or ".." in key or not s.artifact_s3_bucket:
            raise ValueError("invalid upload key")
        client = _s3()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.csv"
            client.download_file(s.artifact_s3_bucket, key, str(path))
            yield path
        client.delete_object(Bucket=s.artifact_s3_bucket, Key=key)
        return
    path = Path(payload["path"]).resolve()
    if s.upload_dir.resolve() not in path.parents:
        raise ValueError("upload path outside upload directory")
    yield path
    path.unlink(missing_ok=True)


async def store(file: UploadFile, batch_id: uuid.UUID, max_mb: int) -> dict[str, str]:
    """Stream an upload to disk (never trusting the client's filename), enforcing the
    size limit while reading so an oversized file is never fully buffered."""
    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    path = s.upload_dir / f"{batch_id}.csv"
    limit = max_mb * 1024 * 1024
    size = 0
    with path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                path.unlink(missing_ok=True)
                raise UnprocessableData(f"File exceeds the {max_mb} MB limit")
            out.write(chunk)
    return publish(path, str(batch_id))
