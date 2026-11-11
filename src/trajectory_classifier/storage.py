"""Strict object locations, lazy S3 clients, and local dataset storage."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .credentials import S3Credentials

_MAX_POOL_CONNECTIONS = 8


def _validate_bucket(bucket: str) -> None:
    if not isinstance(bucket, str):
        raise TypeError("S3 bucket must be a string")
    if not bucket:
        raise ValueError("S3 bucket must not be empty")
    if bucket in {".", ".."}:
        raise ValueError("S3 bucket is invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in bucket
    ):
        raise ValueError("S3 bucket must not contain whitespace or control characters")
    if any(character in bucket for character in "/\\@?#:"):
        raise ValueError("S3 bucket must not contain URI delimiters")


def _validate_path_segments(value: str, *, name: str, directory: bool) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    if value.startswith("/"):
        raise ValueError(f"{name} must be relative")
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{name} must not contain a backslash or control character")
    if directory:
        if not value.endswith("/"):
            raise ValueError(f"{name} must end with '/'")
        segment_value = value[:-1]
    else:
        if value.endswith("/"):
            raise ValueError(f"{name} must identify an object, not a directory")
        segment_value = value
    segments = segment_value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} contains an unsafe path segment")


@dataclass(frozen=True, slots=True)
class S3Location:
    """A bucket and a canonical, non-root directory prefix."""

    bucket: str
    prefix: str

    def __post_init__(self) -> None:
        _validate_bucket(self.bucket)
        _validate_path_segments(self.prefix, name="S3 prefix", directory=True)

    @property
    def uri(self) -> str:
        """Return the location without embedding credentials or query data."""

        return f"s3://{self.bucket}/{self.prefix}"

    def key(self, relative: str) -> str:
        """Join a safe object-relative path beneath this location."""

        _validate_path_segments(relative, name="relative S3 key", directory=False)
        return f"{self.prefix}{relative}"


def parse_s3_uri(uri: str) -> S3Location:
    """Parse a strict ``s3://bucket/non-empty/prefix/`` directory URI.

    Valid key text is preserved byte-for-byte.  Ambiguous path spellings are
    rejected instead of being silently normalized to a different S3 key.
    """

    if not isinstance(uri, str):
        raise TypeError("S3 URI must be a string")
    if any(ord(character) < 32 or ord(character) == 127 for character in uri):
        raise ValueError("S3 URI must not contain control characters")
    if not uri.startswith("s3://"):
        raise ValueError("S3 URI must start with 's3://'")
    if "?" in uri or "#" in uri:
        raise ValueError("S3 URI must not contain a query or fragment")

    parsed = urlsplit(uri)
    if parsed.scheme != "s3":  # Defensive; the exact prefix check is authoritative.
        raise ValueError("S3 URI must use the s3 scheme")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("S3 URI must not contain credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("S3 URI contains an invalid authority") from error
    if port is not None:
        raise ValueError("S3 URI must not contain a port")
    if parsed.netloc != parsed.hostname:
        raise ValueError("S3 URI authority must contain only a bucket")
    if not parsed.path.startswith("/"):
        raise ValueError("S3 URI must contain an absolute URI path")

    # Remove only the separator introduced by URI syntax.  A second leading
    # slash remains visible to validation and is rejected as an empty segment.
    prefix = parsed.path[1:]
    return S3Location(bucket=parsed.netloc, prefix=prefix)


def create_s3_client(
    endpoint_url: str,
    region_name: str,
    *,
    credentials: S3Credentials | None = None,
    max_pool_connections: int = _MAX_POOL_CONNECTIONS,
) -> Any:
    """Create the configured S3-compatible client, importing boto3 lazily."""

    import boto3
    from botocore.config import Config

    if max_pool_connections <= 0:
        raise ValueError("max pool connections must be positive")
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"mode": "standard", "max_attempts": 10},
        max_pool_connections=max_pool_connections,
    )
    client_kwargs: dict[str, Any] = {
        "endpoint_url": endpoint_url,
        "region_name": region_name,
        "config": config,
    }
    if credentials is not None:
        client_kwargs["aws_access_key_id"] = credentials.aws_access_key_id
        client_kwargs["aws_secret_access_key"] = credentials.aws_secret_access_key
        if credentials.aws_session_token is not None:
            client_kwargs["aws_session_token"] = credentials.aws_session_token

    return boto3.client("s3", **client_kwargs)


@dataclass(frozen=True, slots=True)
class DirectoryLocation:
    """Expose a local normalized dataset through the batch job's storage protocol."""

    root: Path
    bucket: str
    prefix: str = "dataset/"

    @property
    def uri(self) -> str:
        return self.root.as_uri().rstrip("/") + "/"

    def key(self, relative: str) -> str:
        _validate_path_segments(relative, name="relative dataset path", directory=False)
        return self.prefix + relative


class DirectoryClient:
    """Read the source directory and atomically replace files in the output directory."""

    def __init__(self, source: DirectoryLocation, output: DirectoryLocation) -> None:
        self._locations = {source.bucket: source, output.bucket: output}
        self._output_bucket = output.bucket

    def _path(self, bucket: str, key: str, *, write: bool = False) -> Path:
        if write and bucket != self._output_bucket:
            raise ValueError("the input dataset is read-only")
        location = self._locations[bucket]
        if not key.startswith(location.prefix):
            raise ValueError("object path is outside the dataset")
        relative = key[len(location.prefix) :]
        _validate_path_segments(relative, name="relative dataset path", directory=False)
        path = location.root / relative
        if not path.resolve().is_relative_to(location.root):
            raise ValueError("dataset path resolves outside its directory")
        return path

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        stream = self._path(Bucket, Key).open("rb")
        try:
            size = os.fstat(stream.fileno()).st_size
        except BaseException:
            stream.close()
            raise
        return {"Body": stream, "ContentLength": size}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:
        path = self._path(Bucket, Key, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(Body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self._path(Bucket, Key, write=True).unlink(missing_ok=True)

    def get_paginator(self, operation: str) -> _DirectoryPaginator:
        if operation != "list_objects_v2":
            raise ValueError("unsupported directory operation")
        return _DirectoryPaginator(self)

    def close(self) -> None:
        pass


class _DirectoryPaginator:
    def __init__(self, client: DirectoryClient) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):
        location = self.client._locations[Bucket]
        contents = []
        for path in sorted(location.root.rglob("*")):
            if not path.is_file():
                continue
            key = location.key(path.relative_to(location.root).as_posix())
            if key.startswith(Prefix):
                checked = self.client._path(Bucket, key)
                contents.append({"Key": key, "Size": checked.stat().st_size})
        yield {"Contents": contents}
