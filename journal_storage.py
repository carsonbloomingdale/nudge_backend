"""S3-compatible presigned URLs for journal photo attachments (optional — disabled when bucket unset)."""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional

_PRESIGN_PUT_SECONDS = int(os.getenv("JOURNAL_ATTACHMENT_PUT_EXPIRES", "900"))
_PRESIGN_GET_SECONDS = int(os.getenv("JOURNAL_ATTACHMENT_GET_EXPIRES", "3600"))
_MAX_BYTES = int(os.getenv("JOURNAL_ATTACHMENT_MAX_BYTES", str(15 * 1024 * 1024)))


def attachments_configured() -> bool:
    return bool((os.getenv("S3_ATTACHMENTS_BUCKET") or "").strip())


def max_upload_bytes() -> int:
    return _MAX_BYTES


def _client():
    import boto3

    region = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1").strip()
    return boto3.client("s3", region_name=region)


def _bucket() -> str:
    return (os.getenv("S3_ATTACHMENTS_BUCKET") or "").strip()


def build_storage_key(user_id: str, journal_id: int, attachment_id: int, content_type: str) -> str:
    ext = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
        "image/heif": "heif",
    }.get(content_type.lower().split(";")[0].strip(), "bin")
    return f"journals/{user_id}/{journal_id}/{attachment_id}-{uuid.uuid4().hex[:12]}.{ext}"


def generate_presigned_put(storage_key: str, content_type: str) -> str:
    url: str = _client().generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": _bucket(), "Key": storage_key, "ContentType": content_type},
        ExpiresIn=_PRESIGN_PUT_SECONDS,
        HttpMethod="PUT",
    )
    return url


def generate_presigned_get(storage_key: str) -> str:
    url: str = _client().generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": _bucket(), "Key": storage_key},
        ExpiresIn=_PRESIGN_GET_SECONDS,
        HttpMethod="GET",
    )
    return url


def delete_object(storage_key: str) -> None:
    _client().delete_object(Bucket=_bucket(), Key=storage_key)


def presign_put_meta() -> dict[str, Any]:
    return {"expires_in": _PRESIGN_PUT_SECONDS}


def presign_get_meta() -> dict[str, Any]:
    return {"expires_in": _PRESIGN_GET_SECONDS}
