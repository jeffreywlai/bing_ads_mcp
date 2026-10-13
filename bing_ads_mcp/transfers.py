"""Report and bulk transfers: submit once, resume by ID, keep complete files."""

import asyncio
import csv
import io
import os
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .artifacts import Artifacts
from .client import AdsClient, ApiError
from .models import Scope

MAX_TRANSFER_BYTES = 1024 * 1024 * 1024


def transfer_url(url: str, *, upload: bool = False) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    allowed = host.endswith(".bingads.microsoft.com")
    if not upload:
        allowed |= host.endswith(".blob.core.windows.net")
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ApiError("Microsoft returned an unsupported transfer URL; no transfer was attempted.")
    return url


async def download(http: httpx.AsyncClient, artifacts: Artifacts, url: str) -> dict:
    """No credential headers and no redirects to a different endpoint."""
    url = transfer_url(url)
    artifacts.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=artifacts.root, prefix=".download-")
    try:
        size = 0
        with os.fdopen(fd, "wb") as stream:
            async with http.stream("GET", url, timeout=60, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ApiError(
                        "Download failed. Poll the job again for a fresh URL.",
                        http_status=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_TRANSFER_BYTES:
                        raise ApiError(
                            "Download exceeds the 1 GiB limit. Request smaller partitions."
                        )
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        zipped = zipfile.is_zipfile(temporary)
        name = f"{uuid.uuid4().hex}.{'zip' if zipped else 'csv'}"
        os.replace(temporary, artifacts.path(name))
        files = []
        if zipped:
            with zipfile.ZipFile(artifacts.path(name)) as archive:
                entries = [info for info in archive.infolist() if not info.is_dir()]
                if sum(e.file_size for e in entries) > MAX_TRANSFER_BYTES:
                    raise ApiError(
                        "Expanded download exceeds 1 GiB; archive retained.", artifact=name
                    )
                for entry in entries:
                    # Read members, never extract provider-controlled paths.
                    suffix = Path(entry.filename).suffix.lower()
                    extension = suffix[1:] if suffix in {".csv", ".tsv", ".xml"} else "bin"
                    with archive.open(entry) as stream:
                        content = stream.read(MAX_TRANSFER_BYTES + 1)
                    if len(content) > MAX_TRANSFER_BYTES:
                        raise ApiError(
                            "Expanded file exceeds 1 GiB; archive retained.", artifact=name
                        )
                    files.append(artifacts.save(content, extension))
        else:
            files.append(artifacts.describe(name))
        return {"download": artifacts.describe(name), "files": files, "complete": True}
    except httpx.HTTPError:
        raise ApiError(
            "Download transport failed. Poll again to resume; do not resubmit the job."
        ) from None
    except zipfile.BadZipFile, OSError:
        raise ApiError(
            "Could not finish saving the download; retry retrieval, not submission."
        ) from None
    finally:
        await asyncio.to_thread(Path(temporary).unlink, missing_ok=True)


def csv_rows(artifacts: Artifacts, artifact: str) -> list[dict]:
    path = artifacts.path(artifact)
    if path.suffix not in {".csv", ".tsv"}:
        raise ApiError("Use a CSV/TSV artifact.")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t" if path.suffix == ".tsv" else ",")
        if reader.fieldnames and len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ApiError(
                "Duplicate CSV columns; use the exact artifact instead of row projection."
            )
        rows = list(reader)
    if any(None in row for row in rows):
        raise ApiError("CSV contains rows with extra cells; use the exact artifact.")
    return rows


async def submit_report(client: AdsClient, scope: Scope, request: dict) -> dict:
    if not request.get("Type"):
        raise ApiError(
            "ReportRequest.Type is required. Discover all types with list_models(reporting)."
        )
    return await client.execute(
        "reporting", "submit_generate_report", {"ReportRequest": request}, scope
    )


async def poll_report(
    client: AdsClient, artifacts: Artifacts, scope: Scope, request_id: str, wait_seconds: int = 0
) -> dict:
    deadline = time.monotonic() + wait_seconds
    while True:
        output = await client.execute(
            "reporting", "poll_generate_report", {"ReportRequestId": request_id}, scope
        )
        status = output["data"].get("ReportRequestStatus") or {}
        state = status.get("Status")
        if state == "Success":
            url = status.get("ReportDownloadUrl")
            return {
                "request_id": request_id,
                "status": state,
                "no_data": not bool(url),
                **(await download(client.http, artifacts, url) if url else {"files": []}),
            }
        if state != "Pending":
            raise ApiError(
                "Report job did not succeed.", request_id=request_id, provider_response=output
            )
        if time.monotonic() >= deadline:
            return {
                "request_id": request_id,
                "status": "Pending",
                "guidance": "Call get_report with this ID. Do not resubmit.",
            }
        await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))


async def poll_bulk(
    client: AdsClient, artifacts: Artifacts, scope: Scope, request_id: str, upload: bool = False
) -> dict:
    method = "get_bulk_upload_status" if upload else "get_bulk_download_status"
    output = await client.execute("bulk", method, {"RequestId": request_id}, scope)
    body = output["data"]
    state = body.get("RequestStatus")
    url = body.get("ResultFileUrl")
    files = await download(client.http, artifacts, url) if url else {}
    return {
        "request_id": request_id,
        "status": state,
        "provider_response": body,
        **files,
        "guidance": "Inspect every result row for errors; completed uploads can have failures. "
        "For pending jobs call get_bulk_result again with this ID.",
    }


async def upload_bulk(
    client: AdsClient, artifacts: Artifacts, scope: Scope, csv_content: str, dry_run: bool = True
) -> dict:
    """Accept content, never a local path to an arbitrary file that might hold secrets."""
    data = csv_content.lstrip("\ufeff").encode("utf-8-sig")
    try:
        rows = [
            row for row in csv.reader(io.StringIO(csv_content.lstrip("\ufeff")), strict=True) if row
        ]
    except csv.Error:
        raise ApiError("Bulk CSV has invalid quoting.") from None
    if len(rows) < 3 or not {"Type", "Name"} <= set(rows[0]):
        raise ApiError("Bulk CSV needs a Type column, Format Version record, and entity records.")
    if len(rows[0]) != len(set(rows[0])):
        raise ApiError("Bulk CSV column names must be unique.")
    type_column, name_column = rows[0].index("Type"), rows[0].index("Name")
    versions = [
        row for row in rows[1:] if len(row) > type_column and row[type_column] == "Format Version"
    ]
    if len(versions) != 1 or len(versions[0]) <= name_column or not versions[0][name_column]:
        raise ApiError("Include one Format Version record with its version in the Name column.")
    if len(data) > 100_000_000:
        raise ApiError("Bulk upload exceeds the 100 MB limit; split the file.")
    preview = {
        "dry_run": dry_run,
        "sent": False,
        "scope": scope.model_dump(),
        "bytes": len(data),
        "records": len(rows) - 1,
        "file": artifacts.save(data, "csv"),
        "validation": "Local preview only. Microsoft has not validated the CSV.",
    }
    if dry_run:
        return preview
    if client.settings.read_only:
        raise ApiError("Advertising writes are disabled by BING_ADS_READ_ONLY.")
    created = await client.execute(
        "bulk",
        "get_bulk_upload_url",
        {"AccountId": scope.account_id, "ResponseMode": "ErrorsAndResults"},
        scope,
    )
    job = created["data"]
    url = transfer_url(job["UploadUrl"], upload=True)
    headers = await client.headers(scope, legacy=True)
    headers.pop("Content-Type")
    try:
        response = await client.http.post(
            url,
            headers=headers,
            files={"file": ("upload.csv", data, "text/csv")},
            timeout=60,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise ApiError(
            "Bulk upload outcome is unknown. Poll this ID; do not upload again.",
            unknown=True,
            request_id=job["RequestId"],
        ) from None
    if response.status_code != 200:
        raise ApiError(
            "Bulk file was not acknowledged. Poll this ID before retrying.",
            unknown=True,
            request_id=job["RequestId"],
            http_status=response.status_code,
        )
    return {
        "status": "Submitted",
        "request_id": job["RequestId"],
        "sent": True,
        "file": preview["file"],
        "guidance": "Call get_bulk_result(upload=true) for per-record results.",
    }
