"""Complete private files, bounded MCP responses, and stable entity snapshots."""

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from .client import ApiError

INLINE_BYTES = 32_000


class Artifacts:
    def __init__(self, root: Path):
        self.root = root

    def path(self, artifact: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}\.(json|csv|tsv|xml|zip|bin)", artifact):
            raise ApiError("Invalid artifact ID. Use an ID returned by this server.")
        path = self.root / artifact
        if path.is_symlink():
            raise ApiError("Artifact symlinks are not allowed.")
        return path

    def save(self, content: bytes, extension: str) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"{uuid.uuid4().hex}.{extension}"
        path = self.path(name)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.describe(name)

    def describe(self, name: str) -> dict:
        path = self.path(name)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {
            "artifact": name,
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": digest,
        }

    def bounded(self, value: dict) -> dict:
        content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(content) <= INLINE_BYTES:
            return value
        artifact = self.save(content, "json")
        return {
            "inline_complete": False,
            "complete_result": artifact,
            "status": value.get("status"),
            "guidance": "The complete result is saved. Use read_artifact to retrieve it. "
            "No rows or provider fields were discarded.",
        }

    def read(self, artifact: str, offset: int = 0, max_bytes: int = 12000) -> dict:
        if offset < 0 or not 1 <= max_bytes <= 12000:
            raise ApiError("offset must be nonnegative; max_bytes must be 1..12000.")
        path = self.path(artifact)
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(max_bytes)
        except OSError:
            raise ApiError("Artifact is unavailable on this server.") from None
        end = offset + len(chunk)
        return {
            "artifact": artifact,
            "offset": offset,
            "encoding": "base64",
            "content": base64.b64encode(chunk).decode(),
            "next_offset": end if end < path.stat().st_size else None,
            "total_bytes": path.stat().st_size,
        }

    def entity_page(
        self, rows: list | None, fingerprint: str, page_size: int, token: str | None = None
    ) -> dict:
        if token:
            try:
                name, raw_offset = token.split(":")
                offset = int(raw_offset)
                snapshot = json.loads(self.path(name).read_text())
                if snapshot["fingerprint"] != fingerprint or offset < 0:
                    raise ValueError
                rows = snapshot["rows"]
            except ValueError, KeyError, OSError, TypeError:
                raise ApiError("Invalid page token or query changed. Restart the query.") from None
        else:
            offset = 0
            snapshot = json.dumps({"fingerprint": fingerprint, "rows": rows}, ensure_ascii=False)
            name = self.save(snapshot.encode(), "json")["artifact"]
        page, size = [], 0
        for row in rows[offset : offset + page_size]:
            encoded = json.dumps(row, ensure_ascii=False).encode()
            if size + len(encoded) > INLINE_BYTES - 2000:
                if not page:
                    # Advance exactly one row; its full contents remain retrievable.
                    page.append({"oversized_entity": self.save(encoded, "json")})
                break
            page.append(row)
            size += len(encoded)
        end = offset + len(page)
        return {
            "Entities": page,
            "TotalCount": len(rows),
            "NextPageToken": f"{name}:{end}" if end < len(rows) else None,
            "snapshot_artifact": name,
        }
