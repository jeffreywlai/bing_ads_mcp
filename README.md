# Bing Ads MCP

A local FastMCP server for Microsoft Advertising reports and account changes.
It exposes 22 tools over 290 operations discovered from Microsoft's REST SDK,
plus 12 Shopping Content operations and UET Conversions API event submission.
Native request and response fields pass
through without being dropped by generated model serialization.

Browser sign-in and token saving are verified, and the implementation passes
offline tests. **Live advertising API reads, reports, and writes still require
verification.** The catalog includes all 279 operations listed in Microsoft's
six public service-operation indexes checked on October 13, 2026. The independent
inventory and its source links are in `tests/public_operations.json`; tests compare
it with the exposed catalog. This establishes operation coverage, not successful
live execution of every feature.

## Local setup and team login

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
`uv sync --locked` in this checkout. The project selects Python 3.15.

1. Register an application in [Azure App registrations](https://portal.azure.com/)
   for accounts in any organizational directory **and personal Microsoft accounts**.
2. Under **Authentication → Mobile and desktop applications**, register
   **`http://localhost`**. No client secret is needed for this local desktop flow.
   Microsoft [supports a changing localhost port](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url#localhost-exceptions);
   do not add a redirect entry for each port printed by the CLI.
3. Obtain a Microsoft Advertising developer token from
   [Developer settings](https://ads.microsoft.com/cc/Settings/DevSettings).
4. Copy `.mcp.json.example` to `.mcp.json` if you do not already have one.
   Fill in `BING_ADS_CLIENT_ID` and `BING_ADS_DEVELOPER_TOKEN`, and replace the
   example paths with absolute paths on your computer.
5. Run:

   ```bash
   uv run bing-ads-login --mcp-config .mcp.json
   ```

   Complete Microsoft sign-in in the browser. The CLI receives the callback
   automatically; there is no redirect URL to paste. Wait for **`Signed in.`**
   in the terminal before treating login as complete.
6. Enable the local `BingAds` server in your MCP client. Start with `get_accounts`
   to discover the account/customer IDs accessible to your signed-in user.

Each analyst keeps a local `.mcp.json` and runs login with their own Microsoft
identity. The team can use the same app registration and developer token;
access to advertising accounts comes from each signed-in user's permissions.
Refresh tokens are saved to that analyst's `BING_ADS_TOKEN_FILE`, replaced
atomically with owner-only file permissions, and refreshed automatically.

`.mcp.json`, `.env`, and `.bing-ads/` are git-ignored. A `.env` file is an optional
alternative to MCP environment settings; existing environment variables take
precedence. The login command reads `mcpServers.BingAds.env` when
`--mcp-config` is supplied. Use literal values there for this command.

For `AADSTS50011`, compare the app ID and redirect URI in the sanitized terminal
diagnostic with the same app registration in Azure. Microsoft says saved changes
can take [3–5 minutes to propagate](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/error-code-aadsts50011-redirect-uri-mismatch).
Retry with a fresh CLI login after that interval. Receiving the browser callback
alone does not mean the token exchange succeeded.

## Using the tools

- **Explore:** `list_operations`, `get_operation_schema`, `list_models`, and
  `get_model_schema` expose the installed SDK's operations, native field names,
  enums, and derived types.
- **Read or change accounts:** `read_api` executes reads and report/download
  jobs. `write_api` previews a native request by default; `dry_run=false` sends
  a real change. A preview is local and is not Microsoft-side validation.
- **Report:** `run_report` submits any native report request. Resume with
  `get_report` using the returned request ID. Set an explicit report time zone.
- **Work with entities:** `get_accounts`, `get_campaigns`, `get_ad_groups`,
  `get_ads`, `get_keywords`, `get_asset_groups`, and `get_audience_associations`
  compose configuration, optional performance, filters, sorting, and paging.
- **Transfer:** `upload_bulk_csv` previews or submits native bulk CSV;
  `get_bulk_result` retrieves status and complete result files.
- **Shopping:** `list_content_operations` and `content_api` handle products,
  inventory, and catalogs. Content API is production-only.
- **Conversions:** `submit_uet_events` previews or sends up to 1,000 native UET
  events. Execution retrieves or creates the tag authentication key internally.
  Inspect `eventsReceived` and validation details even after HTTP 200.
- **Retrieve complete output:** `read_csv_rows` pages report rows, and
  `read_artifact` returns exact file bytes in base64 chunks. Large results are
  saved under `BING_ADS_EXPORT_DIR`; they are not silently truncated.

Use decimal strings for account/entity IDs and native PascalCase request fields.
Account-scoped services require both `scope.customer_id` and `scope.account_id`.
Set `BING_ADS_READ_ONLY=true` to block advertising writes while allowing reads,
local previews, and report/download jobs.

Inspect partial errors and null result slots even after HTTP 200. Mutations are
never automatically retried; a timeout can mean the change reached Microsoft.
Check the resulting account state or resume the existing job before submitting
again. Downloaded reports and snapshots remain on disk until you remove them.

The default transport is stdio (`uv run bing-ads-mcp`). Optional HTTP transport
binds only to `127.0.0.1`: `uv run bing-ads-mcp --transport http --port 8000`.
This is a local server using one user's credentials, not a hosted team gateway.

## Required coverage

Full public Microsoft Advertising API coverage is a project requirement. The
following table distinguishes the implemented adapters from live verification.

| API surface | Required coverage | Current status |
| --- | --- | --- |
| Campaign Management | All public operations and supported request fields, including campaigns, ads, keywords, targeting, budgets, bidding, assets, audiences, and conversion configuration | SDK adapter implemented; live unverified |
| Ad Insight | All public operations, including keyword research, traffic estimates, auction insights, recommendations, and recommendation changes | SDK adapter implemented; live unverified |
| Reporting | All public report types and fields; submit, poll, download, and deliver complete results | SDK adapter and transfer helpers implemented; live unverified |
| Bulk | All public operations; complete upload, download, polling, and per-record result handling | SDK adapter and transfer helpers implemented; live unverified |
| Customer Management | All public account/customer operations | SDK adapter implemented; live unverified |
| Customer Billing | All public billing operations | SDK adapter implemented; live unverified |
| Shopping Content | Products, inventory, catalogs, and their public operations through a separate adapter | 12 operations implemented; live unverified |
| UET Conversions API | Event submission using a UET tag authentication key | Adapter implemented; live unverified |

The [REST SDK guide](https://learn.microsoft.com/en-us/advertising/guides/python-sdk-migration-soap-to-rest?view=bingads-13)
identifies the six Bing Ads v13 services. The
[Content API documentation](https://learn.microsoft.com/en-us/advertising/shopping-content/get-started)
explicitly states that the SDK does not provide Content API interfaces.
Additional current public Microsoft Advertising API families must be inventoried
and covered; omission from the SDK is an implementation gap to resolve, not an
implicit scope exclusion. Microsoft's account permissions and feature eligibility
continue to apply.

For UET key retrieval, the adapter follows the installed SDK and the
[operation reference](https://learn.microsoft.com/en-us/advertising/campaign-management-service/getuettagauthkey?view=bingads-13):
request `UetTagId`, response `UetTagAuthKey`. The
[CAPI guide](https://learn.microsoft.com/en-us/advertising/guides/uet-conversion-api-integration?view=bingads-13)
currently shows different key-retrieval field names. Event payloads pass through
unchanged; no alternate write is attempted to guess around that documentation discrepancy.

All seven [documented MCP entity tools](https://learn.microsoft.com/en-us/advertising/guides/mcp-tools?view=bingads-13)
are required: `get_accounts`, `get_campaigns`, `get_ad_groups`, `get_ads`,
`get_keywords`, `get_asset_groups`, and `get_audience_associations`. Their shared
behavior includes documented filters, sorting, pagination, and applicable
date-scoped performance metrics. These workflows must be implemented over the
API layer; merely exposing SDK operations does not provide them.

The [documented use cases](https://learn.microsoft.com/en-us/advertising/guides/mcp-use-cases?view=bingads-13)
are acceptance scenarios: performance summaries and comparisons, entity and
audience analysis, threshold/anomaly investigation, and complete exportable
datasets. The AI agent composes summaries and presentation text from those data.
Cross-platform scenarios use this server alongside the existing Google Ads MCP;
Microsoft explicitly requires access to both platforms.

## API access design

Use FastMCP and a shared adapter over Microsoft's current REST SDK. Agents must
be able to discover operations, inspect request schemas, and execute reads and
writes across the full API. Derive operation/model information from maintained
SDK/API definitions instead of duplicating every endpoint in handwritten MCP
functions. The seven entity tools and report helpers reuse this adapter.

Classify side effects per operation: Ad Insight also has write operations, and
HTTP POST is used for some reads. Caller-provided flags must never be able to
turn a write into a retried read. All mutations use the same preview, account
scope, timeout, partial-error, and uncertain-outcome handling. Preserve API
fields and provider errors without silently dropping unsupported inputs.

Before claiming full coverage, enumerate the current public operations against
the exposed catalog, resolve every gap, exercise the seven entity workflows,
and validate reporting, bulk transfer, and representative reads/writes end to
end. Distinguish SDK/schema checks, mocked tests, and live API verification.

## Version baseline

Use the latest stable releases when establishing and updating the foundation.
Versions were checked against upstream release pages on October 13, 2026.

| Component | Selected release | Source |
| --- | --- | --- |
| Python | 3.15.0 | [Python releases](https://www.python.org/downloads/) |
| FastMCP | 4.1.0 | [PyPI](https://pypi.org/project/fastmcp/) |
| Microsoft Advertising REST SDK (`msads`) | 13.0.30 | [PyPI](https://pypi.org/project/msads/) |
| Pydantic | 2.14.1 | [PyPI](https://pypi.org/project/pydantic/) |
| HTTPX | 0.28.1 | [PyPI](https://pypi.org/project/httpx/) |
| python-dotenv | 1.2.4 | [PyPI](https://pypi.org/project/python-dotenv/) |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | [pytest](https://pypi.org/project/pytest/), [pytest-asyncio](https://pypi.org/project/pytest-asyncio/) |
| Ruff | 0.17.0 | [PyPI](https://pypi.org/project/ruff/) |
| setuptools | 84.0.0 | [PyPI](https://pypi.org/project/setuptools/) |

The current Microsoft Advertising API is **REST v13**. The current REST SDK is
published as `msads`; Microsoft documents its relationship to the legacy SOAP SDK
in the [Python migration guide](https://learn.microsoft.com/en-us/advertising/guides/python-sdk-migration-soap-to-rest?view=bingads-13).

Direct dependencies are pinned in `pyproject.toml`; `uv.lock` contains the resolved
dependency graph. The installed stack is tested on Python 3.15. Updates should
move to the latest stable releases, rerun the
contract tests, and commit the resulting lock. An older version requires a
specific, documented incompatibility.

## Foundation

FastMCP handles MCP schemas, validation, tool registration, and transport.
Microsoft's REST SDK supplies operation routes, models, and API transport. The local
code should own only the behavior agents need: explicit account scope, report
delivery, previews of proposed changes, and clear results for successful,
partially successful, or uncertain writes.

The Meta and Google MCP projects provide behavior conventions: preserve complete
exports, expose actionable API errors, and never automatically replay campaign
mutations. Their older dependency constraints are specific to those projects.

The adapter sends native JSON through the SDK's public `ApiClient`, preserving
polymorphic and future fields rather than round-tripping payloads through generated
models that can drop them. An explicit read allowlist controls side effects;
unknown SDK operations default to writes. The generic API does not automatically
follow provider pagination; the entity and transfer helpers implement their own
complete workflows. No live campaign mutation has been performed.

## Current validation limit

Run `uv run ruff check .` and `uv run pytest -q`.

On Python 3.15, 52 offline tests pass. They exercise all 290 SDK catalog entries,
all 279 independently inventoried public operations,
native JSON serialization, MCP discovery and validation, OAuth redirect/PKCE
exchange, token rotation, mutation retry prevention, partial errors, complete
artifacts, report polling, bulk uploads, Content API, UET events, and the seven entity
workflows. Provider calls use mocked responses. Installed upstream libraries
emit deprecation warnings; these are not live verification.

A separate stdio subprocess check passed initialization, discovery of all 22
tools and 290 SDK operations, and a local write preview without credentials.
Browser login completed successfully and saved a refresh token with owner-only
permissions. Live account discovery, report delivery, and writes remain
unverified. Passing offline tests does not establish complete public API
coverage or production readiness.
