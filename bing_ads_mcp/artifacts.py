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
from .privacy import safe_output
from .views import numeric_view, project, validate_fields

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

    def save(self, content: bytes, extension: str, *, name: str | None = None) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = name or f"{uuid.uuid4().hex}.{extension}"
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
        value = safe_output(value)
        content = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(content) <= INLINE_BYTES:
            return value
        artifact = self.save(content, "json")
        return {
            "inline_complete": False,
            "complete_result": artifact,
            "status": value.get("status"),
            "guidance": "Use read_json_artifact for structured access, or analyze the local path. "
            "read_artifact retrieves exact bytes. No rows or noncredential fields were discarded.",
        }

    def read(
        self, artifact: str, offset: int = 0, max_bytes: int = 12000, encoding: str = "base64"
    ) -> dict:
        if offset < 0 or not 1 <= max_bytes <= 12000:
            raise ApiError("offset must be nonnegative; max_bytes must be 1..12000.")
        if encoding not in {"base64", "utf-8"}:
            raise ApiError("encoding must be base64 or utf-8.")
        path = self.path(artifact)
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(max_bytes)
        except OSError:
            raise ApiError("Artifact is unavailable on this server.") from None
        if encoding == "utf-8":
            import codecs

            try:
                decoder = codecs.getincrementaldecoder("utf-8")()
                content = decoder.decode(chunk, final=offset + len(chunk) >= path.stat().st_size)
                chunk = chunk[: len(chunk) - len(decoder.getstate()[0])]
                if not chunk and content == "" and offset < path.stat().st_size:
                    raise ValueError
            except UnicodeError, ValueError:
                raise ApiError(
                    "Use a UTF-8 character boundary and max_bytes >= 4, or use base64."
                ) from None
        else:
            content = base64.b64encode(chunk).decode()
        end = offset + len(chunk)
        return {
            "artifact": artifact,
            "offset": offset,
            "encoding": encoding,
            "content": content,
            "next_offset": end if end < path.stat().st_size else None,
            "total_bytes": path.stat().st_size,
        }

    def entity_page(
        self,
        rows: list | None,
        fingerprint: str,
        page_size: int,
        token: str | None = None,
        *,
        fields: list[str] | None = None,
        numeric_fields: list[str] = (),
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
            rows = safe_output(rows)
            validate_fields(rows, [*(fields or []), *numeric_fields])
            snapshot = json.dumps({"fingerprint": fingerprint, "rows": rows}, ensure_ascii=False)
            name = self.save(snapshot.encode(), "json")["artifact"]
        page, numeric_rows, size = [], [], 0
        for row in rows[offset : offset + page_size]:
            visible = project(row, fields)
            numbers = numeric_view(row, numeric_fields)
            encoded = json.dumps(visible, ensure_ascii=False).encode()
            row_size = len(encoded) + len(json.dumps(numbers).encode())
            if size + row_size > INLINE_BYTES - 2000:
                if not page:
                    # Advance exactly one row; its full contents remain retrievable.
                    full = json.dumps(row, ensure_ascii=False).encode()
                    page.append({"oversized_entity": self.save(full, "json")})
                    numeric_rows.append(None)
                break
            page.append(visible)
            numeric_rows.append(numbers)
            size += row_size
        end = offset + len(page)
        return {
            "Entities": page,
            "TotalCount": len(rows),
            "NextPageToken": f"{name}:{end}" if end < len(rows) else None,
            "snapshot_artifact": name,
            **({"selected_fields": fields} if fields is not None else {}),
            **(
                {
                    "numeric_values": numeric_rows,
                    "numeric_note": "Parallel to Entities; raw cells remain in the snapshot. "
                    "Percentages are percentage points. Values beyond safe JSON numeric precision "
                    "are exact decimal strings. Unparseable metrics are null, never zero.",
                }
                if numeric_fields
                else {}
            ),
        }

    def json_value(self, artifact: str, pointer: str = ""):
        if not artifact.endswith(".json"):
            raise ApiError("Use a JSON artifact, or read_artifact for exact bytes.")
        try:
            value = json.loads(self.path(artifact).read_text(encoding="utf-8"))
            if pointer and not pointer.startswith("/"):
                raise ValueError
            for key in pointer.split("/")[1:] if pointer else []:
                if re.search(r"~(?![01])", key):
                    raise ValueError
                key = key.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", key):
                        raise ValueError
                    value = value[int(key)]
                else:
                    value = value[key]
            return value
        except ValueError, TypeError, KeyError, IndexError, OSError:
            raise ApiError("JSON artifact or JSON Pointer is unavailable or invalid.") from None
