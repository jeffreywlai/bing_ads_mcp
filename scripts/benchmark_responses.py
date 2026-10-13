"""Offline response bytes, not tokens. Run: uv run python -m scripts.benchmark_responses."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastmcp import Client

from bing_ads_mcp.artifacts import Artifacts
from bing_ads_mcp.auth import Settings
from bing_ads_mcp.server import create_server


async def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = Artifacts(root)
        rows = [
            {
                "Id": str(9007199254740993 + i),
                "Name": f"Example campaign {i}",
                "Status": "Active",
                "Description": "Synthetic configuration. " * 8,
                "Performance": {"Spend": "4,716.21", "Clicks": "1,205", "Ctr": "5.47%"},
                "Optional": None,
                "Future": {"list": [None, "retained"]},
            }
            for i in range(150)
        ]
        artifact = store.save(json.dumps(rows, ensure_ascii=False).encode(), "json")["artifact"]
        results = {}
        async with Client(create_server(Settings(export_dir=root))) as client:

            async def measure(label, tool, arguments, token_key=None):
                size = calls = 0
                while True:
                    result = await client.call_tool(tool, arguments)
                    size += len(result.content[0].text.encode())
                    calls += 1
                    token = result.data.get(token_key) if token_key else None
                    if token is None:
                        break
                    if token_key == "next_offset":
                        arguments["offset"] = token
                    else:
                        arguments["query"] = {
                            **arguments.get("query", {}),
                            "next_page_token": token,
                        }
                results[label] = {"response_bytes": size, "calls": calls}

            await measure(
                "discovery_detailed_previous_default", "list_operations", {"details": True}
            )
            await measure("discovery_compact", "list_operations", {})
            await measure("artifact_base64", "read_artifact", {"artifact": artifact}, "next_offset")
            await measure(
                "artifact_json_all_fields",
                "read_json_artifact",
                {"artifact": artifact},
                "NextPageToken",
            )
            await measure(
                "artifact_json_selected_fields",
                "read_json_artifact",
                {
                    "artifact": artifact,
                    "query": {"fields": ["Id", "Name", "Status", "Performance"]},
                },
                "NextPageToken",
            )
        assert store.json_value(artifact) == rows
        print(
            json.dumps(
                {
                    "synthetic_rows": len(rows),
                    "measure": "MCP text UTF-8 bytes",
                    "results": results,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
