# Bing Ads MCP Server 🚀

[![Python 3.15+](https://img.shields.io/badge/python-3.15+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP 4.1](https://img.shields.io/badge/FastMCP-4.1-green.svg)](https://gofastmcp.com/)
[![Microsoft Advertising API v13](https://img.shields.io/badge/Microsoft%20Advertising%20API-v13-blue.svg)](https://learn.microsoft.com/en-us/advertising/guides/?view=bingads-13)

**An MCP server for reading reports and managing Microsoft Advertising accounts —
25 tools over 290 SDK operations, plus Shopping Content and UET conversion events.**

> Ask Claude to "show my highest-spend campaigns last week" or "preview pausing
> these campaigns."

**This is not an officially supported Microsoft product.**

[Quick start](#-quick-start) · [Available tools](#-available-tools-25) ·
[Agent guide](#-agent-guide) · [API coverage](#-api-coverage-and-current-limits)

## ✨ Features

- **Campaign management** — Native reads and changes for campaigns, ad groups,
  ads, keywords, budgets, bidding, targeting, audiences, assets, and conversions.
- **Reports and exports** — Submit reports, resume existing jobs, download
  complete files, and retrieve rows or exact file bytes.
- **Seven entity tools** — Account, campaign, ad group, ad, keyword, asset group,
  and audience-association reads with filters, sorting, pagination, and optional
  performance metrics.
- **API discovery** — Find operations and inspect native request fields, enums,
  and derived models directly from Microsoft's installed REST SDK.
- **Efficient results** — Compact operation discovery, structured JSON artifact
  reads, and optional field selection. Full source files and snapshots remain available.
- **Write verification** — Advisory SDK field/type checks, read-back for supported
  updates, and explicit removal consent for negative-site replacements.
- **Research and recommendations** — Access Ad Insight keyword ideas, estimates,
  auction insights, recommendations, and recommendation changes.
- **Bulk and Shopping** — Bulk CSV upload/download and Shopping Content products,
  inventory, and catalogs.
- **Conversion events** — Native UET Conversions API submissions with indexed
  errors and partial results preserved.
- **Write lock and previews** — Writes start locked and default to a local preview.
  Explicitly unlock a mutation session to execute changes, then lock it afterward.
  Mutations are never automatically retried; the read-only setting blocks unlocking.
- **Individual sign-in** — Each analyst signs in with their own Microsoft identity;
  refresh tokens stay in a private local file.
- **Install from GitHub** — Claude launches the published package with `uvx`.
  Analysts do not need a source checkout.

Browser login and offline checks have passed. A user-provided October 13, 2026
usage report verifies live entity reads, campaign/keyword changes, report downloads,
bulk transfers and a Shopping product read through Claude on an earlier commit.
The improvements below are covered by offline tests; they have not been rerun
against a live advertising account. Broader API coverage still needs live verification.
See [coverage and current limits](#-api-coverage-and-current-limits).

## 📋 Available Tools (25)

Tool arguments below use the exact registered names. `scope` contains
`customer_id` and `account_id`; `query` controls entity filters, performance, and
pagination. Both are explained in the [agent guide](#-agent-guide).

### 🔍 Accounts and Entities

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `get_accounts` | Discover accessible accounts and their customer IDs; optionally add performance | Optional `query`, `scope` |
| `get_campaigns` | Campaign configuration and optional performance across campaign types | Required `scope`; optional `query`, `campaign_id` |
| `get_ad_groups` | Ad groups with campaign context and optional performance | Required `scope`; optional `query`, `campaign_id`, `ad_group_id` |
| `get_ads` | Ads, creative fields, parent context, and optional performance | Required `scope`; optional `query`, `campaign_id`, `ad_group_id` |
| `get_keywords` | Keywords, match types, bids, status, and optional performance | Required `scope`; optional `query`, `campaign_id`, `ad_group_id` |
| `get_asset_groups` | Performance Max asset groups, assets, and search themes | Required `scope`, `campaign_id`; optional `query` |
| `get_audience_associations` | Campaign/ad-group audience associations, names, bids, and optional performance | Required `scope`; optional `query`, `campaign_id`, `ad_group_id` |

### 📖 API Discovery

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `list_operations` | Compact operation discovery; case/separator-insensitive search | Optional `service`, `search`, `offset`, `limit`, `details=false` |
| `get_operation_schema` | Get a request schema, route, classification, and Microsoft documentation link | Required `service`, `operation_name` |
| `list_models` | Find request objects, report types, column enums, and other models | Required `service`; optional `search`, `offset`, `limit` |
| `get_model_schema` | Inspect native fields, enum values, and derived types | Required `service`, `model` |

### ✏️ Native API Access

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `read_api` | Execute a native read or report/download job; advertising writes are rejected | Required `service`, `operation_name`, `request`; optional `scope`, `download_files=false` |
| `write_api` | Preview or execute a native SDK operation, with supported read-back checks | Required `service`, `operation_name`, `request`; optional `scope`, `dry_run=true`, `session_id` (required for execution), `verify=true`, `preflight=false`, `allow_remove=false` |

Valid service names are `campaign`, `ad_insight`, `reporting`, `bulk`,
`customer`, and `billing`. Discover operation names such as `update_campaigns`;
do not pass SOAP-style names such as `UpdateCampaigns` as `operation_name`.

### 🔒 Mutation Sessions

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `unlock_mutation_tools` | After user authorization, create an unlocked session and return its ID | None |
| `lock_mutation_tools` | Revoke that session's permission to send further writes | Required `session_id` |

Reads and local previews need no unlock. Every real write requires the
`session_id` returned by `unlock_mutation_tools`, even when `BING_ADS_READ_ONLY`
is `false`. See [campaign changes](#campaign-changes) for the complete workflow.

### 📊 Reporting and Bulk Transfers

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `run_report` | Submit a native report once and return its request ID | Required `scope`, `request` |
| `get_report` | Poll an existing report and download its complete result | Required `scope`, `request_id`; optional `wait_seconds=0` (0–30) |
| `get_bulk_result` | Poll a bulk download or upload and retrieve complete result/error files | Required `scope`, `request_id`; optional `upload=false` |
| `upload_bulk_csv` | Preview or upload native bulk CSV, including its Format Version record | Required `scope`, `csv_content`; optional `dry_run=true`, `session_id` (required for execution), `verify=true` |

Bulk downloads start through `read_api` with a discovered `bulk` download
operation. Retrieve upload results with `get_bulk_result(upload=true)`.

### 🛒 Shopping and Conversion Events

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `list_content_operations` | List the 12 Shopping Content operations | None |
| `content_api` | Read or change products, inventory, and catalogs | Required `operation_name`, `merchant_id`; optional `scope`, `item_id`, `request`, `query`, `dry_run=true`, `session_id` (required to execute writes) |
| `submit_uet_events` | Preview or submit 1–1,000 native UET events; obtain the tag key internally | Required `scope`, `tag_id`, `request`; optional `dry_run=true`, `session_id` (required for execution) |

Content operation names: `list_products`, `get_product`, `insert_product`,
`delete_product`, `batch_products`, `batch_inventory`, `update_inventory`,
`list_catalogs`, `get_catalog`, `insert_catalog`, `update_catalog`, and
`delete_catalog`. Item-specific operations require `item_id`.
Shopping Content and UET event submission use production endpoints.

### 📄 Complete Result Retrieval

| Tool | Purpose | Arguments |
| --- | --- | --- |
| `read_csv_rows` | Filter, sort, project, and page a saved CSV/TSV artifact | Required `artifact`; optional `query` |
| `read_json_artifact` | Read JSON values or page arrays using a JSON Pointer | Required `artifact`; optional `pointer=""`, `query` |
| `read_artifact` | Retrieve exact bytes in base64 or readable UTF-8 chunks | Required `artifact`; optional `offset=0`, `max_bytes=12000` (1–12,000), `encoding="base64"` or `"utf-8"` |

## 🚀 Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which includes
  `uvx`; Git; and Python 3.15 (uv can install the requested Python version).
- Claude Code, Claude Desktop, or another client supporting local stdio MCP.
- A Microsoft identity with access to the advertising accounts you want to use.
- A Microsoft Advertising developer token and an Azure application client ID.

### 1. Get Your Credentials

1. Obtain a developer token from
   [Microsoft Advertising Developer settings](https://ads.microsoft.com/cc/Settings/DevSettings).
2. Register an application in [Azure App registrations](https://portal.azure.com/)
   supporting accounts in **any organizational directory and personal Microsoft accounts**.
   Copy its Application (client) ID.
3. Under **Authentication → Mobile and desktop applications**, register
   **`http://localhost`**. The local desktop login needs no client secret.
   Microsoft permits [varying localhost ports](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url#localhost-exceptions),
   so the randomly selected callback port does not need its own registration.

A team can use its shared app registration and developer token. Each analyst
signs in separately; Microsoft grants account access according to that user's
permissions. Do not share refresh-token files.

### 2. Configure Claude to Install from GitHub

In the project where you use Claude Code, add this entry to your **private**
`.mcp.json`. If the file already contains servers, merge the `BingAds` entry
into its existing `mcpServers` object.

```json
{
  "mcpServers": {
    "BingAds": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--python",
        "3.15",
        "--from",
        "git+https://github.com/jeffreywlai/bing_ads_mcp.git@main",
        "bing-ads-mcp"
      ],
      "env": {
        "BING_ADS_CLIENT_ID": "",
        "BING_ADS_DEVELOPER_TOKEN": "",
        "BING_ADS_TOKEN_FILE": "~/.bing-ads/token.json",
        "BING_ADS_EXPORT_DIR": "~/.bing-ads/exports"
      }
    }
  }
}
```

Fill the two empty credential values locally. This example is also available as
[.mcp.json.example](.mcp.json.example).

`uvx` installs and runs the package from GitHub in its managed environment.
`--refresh` refreshes cached data and `@main` selects the published branch.
Restart the MCP server to pick up newly published code; local source edits have
no effect on this configuration. An already-running process does not update
itself. No local clone or `--directory` argument is required.
See [uv's tool installation guide](https://docs.astral.sh/uv/guides/tools/).

The token and export paths are separate from the downloaded package.
The server expands `~` to the current user's home directory. Use distinct paths
for different Microsoft identities or production/sandbox profiles.

This repository ignores `.mcp.json`, `.env`, and `.bing-ads/`. If you put the
config in another project, add those private files/directories to that project's
ignore rules too. Commit only placeholder examples. Keep credentials, real
account identifiers, report data, and local machine paths out of public issues,
logs, screenshots, and documentation.

**Claude Desktop / other MCP clients:** put the same `BingAds` entry in the
client's MCP configuration. For login below, point `--mcp-config` at that JSON
file; it must contain `mcpServers.BingAds.env`.

### 3. Sign In with Your Own Microsoft Account

From the folder containing your private `.mcp.json`, run:

```bash
uvx --refresh --python 3.15 \
  --from git+https://github.com/jeffreywlai/bing_ads_mcp.git@main \
  bing-ads-login --mcp-config .mcp.json
```

For a config elsewhere, replace `.mcp.json` with its full path.
The login helper reads the literal `BING_ADS_*` values under
`mcpServers.BingAds.env`. It expands `~` in token/export paths, but does not
expand environment-variable expressions in the JSON.

Complete sign-in in the browser. The callback is received automatically; no URL
needs to be pasted. Wait for **`Signed in.`** in the terminal. The refresh token
is saved privately to the configured token file and refreshed automatically.
Login and the MCP server must use the same client ID, environment, and token path.

**Existing installations:** an explicit `BING_ADS_TOKEN_FILE` continues to work.
When switching from a checkout-based launch, keep that path to reuse your login,
or choose a new private path and sign in again. Do not paste a refresh token into
chat or into a public config.

### 4. Connect and Verify

Start or restart Claude Code in the project containing `.mcp.json`, approve the
project's MCP server when Claude prompts, and check `/mcp` for `BingAds`.
Claude manages the stdio process; you do not need a separate server terminal.
See [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp).

Ask:

> Use BingAds get_accounts to list the accounts I can access.

A successful account response verifies advertising API access. A successful
browser login alone only verifies authentication. If `uvx` is not on your
client's PATH, use its absolute executable path as `command`.

### Optional Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `BING_ADS_CLIENT_ID` | Azure application's client ID | Required for sign-in |
| `BING_ADS_DEVELOPER_TOKEN` | Microsoft Advertising developer token | Required for advertising API calls |
| `BING_ADS_TOKEN_FILE` | Private refresh-token file | `~/.bing-ads/token.json` |
| `BING_ADS_EXPORT_DIR` | Private reports, overflow results, and snapshots | `~/.bing-ads/exports` |
| `BING_ADS_READ_ONLY` | Block advertising writes and unlocking; allow reads, report jobs, and local previews | `false` (writes still require an unlocked session) |
| `BING_ADS_ENVIRONMENT` | `production` or `sandbox` for the six v13 services | `production` |
| `BING_ADS_CLIENT_SECRET` | Optional confidential/web-app secret; unnecessary for the desktop flow above | Empty |
| `BING_ADS_REFRESH_TOKEN` | Optional seed token instead of browser login | Empty |

A local `.env` is an alternative; environment variables take precedence.
For the GitHub setup, the private MCP config above keeps login and server
settings together.

## 🧭 Agent Guide

### Choose the Right Tool

1. Start with `get_accounts`. Its `Entities` contain `Id` and
   `ParentCustomerId`; use those as `scope.account_id` and `scope.customer_id`.
2. Use the seven entity tools for configuration and optional summary performance.
   Use `run_report` for segmented or specialized reports.
3. For any other API feature, use `list_operations`, then
   `get_operation_schema`. Inspect nested/derived objects with
   `get_model_schema`. Never invent operation or field names.
4. Use `read_api` for reads/jobs and `write_api` for advertising changes.
   Shopping operations use `content_api`; UET events use `submit_uet_events`.
5. After user authorization, call `unlock_mutation_tools` and pass its returned
   `session_id` with each real write. Call `lock_mutation_tools` with that ID
   when finished. Never reuse an unlock from another conversation.
6. Follow all continuation tokens and job IDs. A first page is not a full dataset.
7. Request only the report columns and displayed fields needed for the question.
   Analyze large files locally when possible; avoid passing an entire CSV or base64
   export through conversation context merely to calculate an aggregate.

Discovery search ignores case and separators: `NegativeKeyword`, `negative_keyword`
and `negative keyword` find the same operations. `details=true` includes routes and
documentation; normally inspect only the selected `get_operation_schema`.
Claude Code's native [MCP Tool Search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search)
can defer tool schemas; no separate search framework is required here.

MCP arguments use snake_case. Native v13 request bodies use Microsoft's
PascalCase fields. Content and UET use their own documented JSON shapes.
Pass identifiers as decimal strings. Every account-scoped request needs
both the customer and account IDs; an account number is not an account ID.

### Entity Performance, Filters, and Pagination

Example arguments for `get_campaigns` (all IDs here are synthetic):

```json
{
  "scope": {
    "customer_id": "111111111",
    "account_id": "222222222"
  },
  "query": {
    "performance_time": {
      "PredefinedTime": "LastSevenDays",
      "ReportTimeZone": "EasternTimeUSCanada"
    },
    "filters": {
      "Status": {"IsEqualTo": "Active"}
    },
    "sort_by": "Spend",
    "descending": true,
    "page_size": 20
  }
}
```

Use the account's intended report time zone, discovered from the
`reporting` `ReportTimeZone` model. `performance_time` accepts native
`ReportTime`, including predefined or custom dates. Metric filters and sorting
require `performance_time`; `return_only_complete_data` defaults to `true`.

Filters are ANDed across fields and operators. Supported operators:
`IsEqualTo`, `IsNotEqualTo`, `IsGreaterThan`, `IsGreaterThanOrEqualTo`,
`IsLessThan`, `IsLessThanOrEqualTo`, `IsEqualToAny`, `Contains`, and `StartsWith`.
Filter using returned configuration field names or the metric names below.

Metrics, when supported by that entity's report: `Spend`, `Impressions`,
`Clicks`, `ClickThroughRate`, `AverageCostPerClick`,
`AverageCostPerThousandImpressions`, `Conversions`, `ClickConversionRate`,
`Revenue`, `ReturnOnAdSpend`, `TopImpressionRate`, `AbsoluteTopImpressionRate`,
`CPA`, and `CostPerInstall`. Metrics are under `Performance`; unavailable
fields are listed in `UnavailableMetrics`. Missing performance is not zero.
Money is in account currency; percentages are percentage points.

Optional `query.fields` selects exact top-level names or dotted paths, for example
`["Id", "Name", "Performance.Spend"]`. The default returns every field. Unknown
selections fail instead of silently producing empty columns. A field absent from
one row stays absent; it is not filled with zero. Full matching rows remain in
`snapshot_artifact`, even when the displayed rows are projected.

Optional `query.numeric_fields`, such as `["Performance.Spend"]` for entities or
`["Spend", "Ctr"]` for CSV rows, adds `numeric_values` parallel to `Entities`.
`"4,716.21"` becomes `4716.21`, `"5.47%"` becomes `5.47` percentage points, and
`"--"` / `"<10%"` become null rather than invented exact values. Raw cells stay intact.
Values beyond safe JSON numeric precision are returned as exact decimal strings.
Nothing is inferred as numeric unless requested; identifier strings retain their zeros.

- If `status` is `Pending`, repeat the same entity call with its returned
  `query` and unchanged scope/entity IDs. Its report IDs resume existing jobs.
- When `NextPageToken` is present, pass it as `query.next_page_token` with
  the same filters, sort, field selections, scope, and performance settings.
- `page_size` (1–500) limits the returned page, not source data retrieval.
  `TotalCount` is the number of matching entities.
- If an entity has `oversized_entity`, retrieve that artifact to see its fields.
- `read_csv_rows` uses the same filtering/paging inputs, but no
  `performance_time`: the report already defines the period.

### Complete Reports

Discover a report model with `list_models(service="reporting", search="ReportRequest")`,
then inspect it and its column enum. Pass the native report object directly as
`run_report.request`; do not wrap it in another `ReportRequest` field.

For CSV row access, set `Format="Csv"`, `FormatVersion="2.0"`,
`ExcludeReportHeader=true`, `ExcludeReportFooter=true`, and
`ExcludeColumnHeaders=false`. Choose valid `Columns`, `Scope`, `Time`, and
`Aggregation` for the report.

Use the returned `data.ReportRequestId` with `get_report`.
If pending, poll that ID instead of submitting another report.
Read the returned file `artifact` with `read_csv_rows`, or retrieve exact bytes
with `read_artifact`. A successful report can contain no data.

`run_report` returns `report_context` and stores it privately for subsequent
`get_report` calls, including after a server restart. It preserves the requested
time range, warns when explicit start dates precede known retention windows, and
lists observed `TimePeriod` extents when the CSV has that column. Retention is
report/aggregation-specific; unsupported mappings are marked unknown. Cutoffs are
approximate UTC dates, not guaranteed effective boundaries. The first monthly
bucket can be partial. `ReturnOnlyCompleteData=true` does not recover expired
history. An observed first/last bucket cannot prove that every requested day was
returned, so `effective_range_verified` remains false. See
[Microsoft's retention table](https://learn.microsoft.com/en-us/advertising/guides/report-data-retention-time-periods?view=bingads-13).

Large JSON responses return `inline_complete=false` and a `complete_result`
artifact. Use `read_json_artifact` with a pointer such as `/data/Campaigns` to
page an array using the same query options. Large objects list their member
pointers; entity snapshots keep complete rows at `/rows`. Escape `/` as `~1` and
`~` as `~0` inside a pointer member name. Metadata, null slots and errors can be
retrieved separately without reloading every entity. Large scalar values return
an artifact for byte/text retrieval. Local file tools can also read the returned
absolute path directly.

For text files, `read_artifact(encoding="utf-8")` avoids base64. Offsets always
count bytes; follow `next_offset` until null to respect multibyte character
boundaries. Base64 remains the default for exact binary retrieval. Credential query
parameters on Microsoft transfer/sign-in URLs, and URL usernames/passwords, are
redacted from MCP JSON results and errors after downloads use the original URL
internally; downloaded source files retain their exact bytes. Ordinary advertising
URL query parameters, including `code`, `key` and `token`, are preserved in both
displayed results and saved snapshots.
For other native reads returning signed file URLs, such as offline-conversion
reports, set `read_api(download_files=true)` to save files before redaction.
Downloads enforce the same Microsoft-host allowlist and preserve the API result
and job ID if a download fails. Poll existing Reporting/Bulk jobs with their helpers.
Report files and snapshots remain in the private export directory until removed.

### Campaign Changes

Example arguments for `write_api` to **preview** pausing a campaign:

```json
{
  "service": "campaign",
  "operation_name": "update_campaigns",
  "scope": {
    "customer_id": "111111111",
    "account_id": "222222222"
  },
  "request": {
    "AccountId": "222222222",
    "Campaigns": [{"Id": "333333333", "Status": "Paused"}]
  },
  "dry_run": true
}
```

For an authorized change, use the actual discovered IDs, inspect the schema and
current state, and review the preview. Then:

1. Call `unlock_mutation_tools` with `{}`. Keep its returned `session_id` private.
2. Send the intended request with `dry_run=false` and that `session_id`.
3. Read the resulting state and inspect partial errors.
4. Call `lock_mutation_tools` with the same `session_id`, including after a failure.

The lock applies to `write_api`, `upload_bulk_csv`, Shopping Content writes,
and UET event submission (before any tag key is retrieved or created). Missing,
invented, or revoked session IDs cannot execute writes. Previews, reads, and
report/download jobs remain available while locked.

Tools remain visible so Claude can discover them and preview changes. Execution
requires an explicit session ID because modern MCP is stateless; FastMCP's
[session storage](https://gofastmcp.com/servers/sessions) preserves the unlock
across requests on both modern and legacy clients. This uses FastMCP's built-in
session creation and revocation, with no separate session database.

Each unlock is independent. Locking one ID leaves other explicitly unlocked
sessions alone and does not cancel already-started writes. IDs remain valid
until locked or the server process exits; a transport reconnect alone does not
revoke them. This is a local workflow guard: anyone holding an ID can use it on
this unauthenticated, single-identity server. It is not multi-user authentication
or proof of human approval; client approval settings determine when Claude asks.

A default preview is local. It checks SDK field names/types and suggests likely
typos, but Microsoft has not validated business rules. These checks are advisory:
unknown future fields, derived types, and explicit null slots still pass through
unchanged. Fix warnings before executing unless the field is intentionally newer
than the SDK. No schema validator silently edits a request.

`verify=true` reads back `update_campaigns`, `update_ad_groups`, `update_keywords`,
and campaign/ad-group negative-site sets. `verification` reports each requested
field as `matched`, `mismatch`, `not_returned`, or `not_checked` (null update rules
are field-specific). Complete read-back data is saved privately. A mismatch after
a provider-success response sets `status="verification_mismatch"` and preserves
`provider_status` and the original `data`. Read failures produce `unverified`
without hiding an already completed write. Other operations report
`not_supported`; read those entities manually. Set `verify=false` to opt out,
for example when independently verifying a large batch. Read-back observes
current state, not causation; propagation delays and normalization can cause
mismatches. The server never retries a mutation to make a comparison pass.

For negative-site replacements, preview with `preflight=true` to read the current
directly assigned list and inspect `replacement_diff`. This is an explicit
read-only network preflight; normal previews remain local. Execution always reads
again and requires `allow_remove=true` if any existing sites would be removed or
the requested list is empty. Send the complete desired list. An incomplete
preflight blocks execution. Microsoft provides no atomic compare-and-set here;
another concurrent editor can still change a list between reading and writing.

Bulk uploads retain their source artifact and job context. On completed upload
polls, `get_bulk_result(upload=true)` verifies existing Campaign rows for
`Campaign` (name), `Status`, `Budget`, `Bid Adjustment`, `Budget Type`,
`Tracking Template`, and `Final Url Suffix`. Other records, creates/deletes,
unmapped columns and `delete_value` semantics remain explicitly unverified.
Inspect every result row for provider errors even when read-back matches.

For ad-group `AudienceAdsBidAdjustment`, set the request-level
`UpdateAudienceAdsBidAdjustment=true`; otherwise Microsoft ignores it.
Campaign-level accepted-but-unchanged audience adjustments were observed in a
user session, but a universal restriction is unconfirmed. Read back this field
and do not infer a delivery change from HTTP 200. See
[UpdateAdGroups](https://learn.microsoft.com/en-us/advertising/campaign-management-service/updateadgroups?view=bingads-13).

`BING_ADS_READ_ONLY=true` is an additional write block that unlocking cannot override.
Existing clients must restart the server after updating and unlock before sending writes.

Inspect partial errors and null result slots even after HTTP 200. A timeout can
leave `mutation_outcome_unknown=true`: read the current state or poll the
existing job before deciding what to do next. Never blindly replay a mutation.

For Content API products/inventory, provider validation is available through
`query={"dry-run": ""}` with `dry_run=false`; catalogs do not support that
provider validation mode. Product inserts replace the offer, so include all
desired fields. For UET, preserve `eventId` and inspect `eventsReceived` and
indexed errors even when the response is HTTP 200.

## 💬 Usage Examples

After connecting, ask naturally:

- "List my accessible Bing Ads accounts."
- "Show active campaigns ranked by spend over the last seven complete days."
- "Export search-query performance for last month, including every row."
- "Find keyword ideas and traffic estimates for these seed keywords."
- "Read Microsoft's recommendations for this account."
- "Preview changing this campaign's budget to the amount I specified."
- "Preview these bulk keyword changes and show the proposed CSV."
- "List products in my Merchant Center catalog."
- "Compare these two periods using the same account, time zone, and metrics."

Period comparisons and other analysis currently require the agent to compose
reports. There are no dedicated budget-pacing, creative-fatigue, or
period-comparison tools.

## 📚 API Coverage and Current Limits

The catalog exposes all **279 operations** in Microsoft's six public service
operation indexes inventoried on October 13, 2026. The installed SDK exposes
**290 operations** in total; the extra entries do not imply public availability.
The independently maintained inventory and source links are in
[tests/public_operations.json](tests/public_operations.json).

| Surface | Implemented access |
| --- | --- |
| Campaign Management | 194 SDK operations for campaign/entity lifecycle, budgets, bidding, targeting, extensions, audiences, assets, conversions, experiments, and imports |
| Ad Insight | 33 SDK operations for research, estimates, auction insights, opportunities, and recommendation actions |
| Customer Management | 39 SDK operations for accounts, customers, users, and links |
| Customer Billing | 16 SDK operations for spend, billing documents, insertion orders, and related actions |
| Reporting | 2 SDK operations plus report polling/download helpers; discover report types and columns through the model tools |
| Bulk | 6 SDK operations plus CSV upload and complete result-file retrieval |
| Shopping Content v9.1 | 12 product, inventory, and catalog operations through a separate HTTP adapter |
| UET Conversions API | Native event submission with the tag key retrieved internally |

**Operation coverage is not proof that every documented feature works end to end.**
The shared adapter preserves native fields and provider responses, while Microsoft
checks business rules, permissions, eligibility, and unsupported combinations.
A reserved or beta SDK operation remains subject to Microsoft's access restrictions.

Current limits:

- A user-provided session report verified selected campaign/keyword writes,
  report downloads, bulk transfers and a Shopping read on an earlier version.
  Full API coverage and the new behavior are not live-verified. Offline tests use mocks.
- Generic API and Content calls do not automatically exhaust provider pagination.
  Entity tools implement their own complete configuration/report workflows.
- Reporting/Bulk helpers download complete results. Other native reads can opt
  into signed Microsoft file delivery with `read_api(download_files=true)`;
  arbitrary external hosts and unsigned unrelated URLs are not fetched.
- The local server does not host Hotel Center XML feeds or
  [live pricing callbacks](https://learn.microsoft.com/en-us/advertising/query-message/live-query),
  install website UET scripts, or transfer catalog feeds over FTP/SFTP.
- Specialized analysis workflows are composed by the agent from native reports.
  Explicit mutation sessions, default previews, and the global read-only setting are implemented.
- SDK schemas do not encode every service rule. The server explicitly catches
  the one-ID limit on `get_negative_keywords_by_entity_ids` and missing
  `StartDateUtc`/`EndDateUtc` on `get_offline_conversion_reports`. Other business
  rules remain Microsoft's responsibility. Goal flags use comma-and-space
  separated names; `InStoreTransaction` requires pilot eligibility and is never
  silently removed from an explicit request. A null import-job result does not
  establish that no UI-scheduled imports exist.
- UET key retrieval follows the SDK and the operation reference:
  `UetTagId` / `UetTagAuthKey`. The CAPI guide currently shows different names;
  the adapter does not guess by retrying a write.
- HTTP mode binds to localhost and uses one configured Microsoft identity.
  Hosted multi-user authentication is not implemented.

Full public API support remains the project goal. Features outside the SDK and
multi-step workflows need their own coverage checks; they are not considered
complete merely because the catalog contains their setup operations.

See [the usage-report follow-up](docs/usage-report-follow-up.md) for the complete
finding-by-finding disposition and verification limits.

References: [Microsoft Advertising overview](https://learn.microsoft.com/en-us/advertising/guides/?view=bingads-13),
[REST SDK migration](https://learn.microsoft.com/en-us/advertising/guides/python-sdk-migration-soap-to-rest?view=bingads-13),
[Shopping Content](https://learn.microsoft.com/en-us/advertising/shopping-content/),
[UET Conversions API](https://learn.microsoft.com/en-us/advertising/guides/uet-conversion-api-integration?view=bingads-13),
[UET key operation](https://learn.microsoft.com/en-us/advertising/campaign-management-service/getuettagauthkey?view=bingads-13),
[Microsoft's MCP tools](https://learn.microsoft.com/en-us/advertising/guides/mcp-tools?view=bingads-13).

## 🏗️ Project Structure

```text
bing_ads_mcp/
├── server.py        # FastMCP tools and stdio/HTTP entry points
├── auth.py          # Browser login, private token storage, automatic refresh
├── catalog.py       # SDK operation/model discovery
├── policy.py        # Reviewed read/job allowlists; other operations are writes
├── client.py        # Shared native API calls and provider errors
├── validation.py    # Advisory SDK checks and reviewed operation hints
├── mutations.py     # Read-back comparisons and negative-site replacement guards
├── bulk_verification.py # Reviewed bulk Campaign field mappings
├── entities.py      # Seven entity workflows and performance joins
├── transfers.py     # Report and bulk jobs, downloads, CSV handling
├── reporting.py     # Retention hints and persisted report coverage context
├── artifacts.py     # Complete private artifacts and snapshot pagination
├── views.py         # Explicit projections and precision-aware numeric views
├── privacy.py       # URL credential redaction at the output boundary
├── content.py       # Shopping Content API
├── conversions.py   # UET Conversions API
└── models.py        # Account scope and shared query inputs
tests/               # Offline regression tests and public operation inventory
.mcp.json.example    # GitHub installation config with credential placeholders
.env.example         # Optional local environment template
pyproject.toml       # Package, entry points, and pinned direct dependencies
uv.lock              # Resolved dependencies for development
```

FastMCP handles MCP transport and schemas. Microsoft's REST SDK supplies service
routes and model discovery. Native JSON passes through the SDK's public
`ApiClient` without generated-model conversion that could drop fields.
There is one shared executor; new convenience workflows should reuse it.

## 🛠️ Development

A local checkout is only needed to work on the server's source code:

```bash
git clone https://github.com/jeffreywlai/bing_ads_mcp.git
cd bing_ads_mcp
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

For local development, launch `uv run bing-ads-mcp` or configure your MCP client
with `uv run --directory /absolute/path/to/bing_ads_mcp bing-ads-mcp`.
Optional HTTP: `uv run bing-ads-mcp --transport http --port 8000` (localhost only).
Run `uv run bing-ads-login --mcp-config /absolute/path/to/private/.mcp.json`
when testing local changes to login.

The dependency baseline is Python 3.15, FastMCP 4.1.0, and `msads` 13.0.30.
Direct dependencies are pinned in [pyproject.toml](pyproject.toml); development
uses [uv.lock](uv.lock). GitHub `uvx` installation resolves package dependencies;
it does not apply the repository's development lockfile.

Offline tests cover API behavior, MCP discovery of all 25 tools, and mutation
locking on legacy and modern clients, including separate stdio requests.
These checks do not establish live provider behavior.

When updating the SDK, review the public operation inventory and side-effect
allowlists, preserve native fields and partial results, and rerun the checks.
Keep mutation retries disabled. Add focused tests for changed behavior.

## 🔧 Troubleshooting

| Symptom | Check |
| --- | --- |
| `AADSTS50011` | The configured client ID must match the Azure app with `http://localhost` under Mobile and desktop applications. Allow saved Azure changes time to propagate, then start a fresh login. |
| Browser callback arrived but login failed | Wait for the terminal's `Signed in.` message; callback receipt alone does not establish token exchange success. |
| Cannot find token / sign-in required | Login and server must use the same client ID, environment, and token file. Keep an existing explicit path when changing launch methods. |
| `uvx` cannot be found | Install uv and make its executable available to the MCP client's PATH, or use an absolute executable path. |
| GitHub installation fails | Check GitHub/package-registry access, Git availability, and Python 3.15 installation. |
| Tools are connected but API calls fail | Confirm developer token, user account permissions, environment, and network access to Microsoft. |
| Report is pending | Poll the existing request ID or resume the entity tool's returned query. |
| Advertising writes are disabled | Check the locally configured `BING_ADS_READ_ONLY` setting. |
| Advertising writes are locked | After user authorization, call `unlock_mutation_tools` and pass its `session_id` with the write. A revoked ID or one from a previous server process cannot be reused. |
| A preview made no change | `dry_run=true` is local preview; an authorized execution requires `dry_run=false`. |
| Output is paginated or references an artifact | Follow the returned continuation token or retrieve the complete artifact. |

When reporting an issue, include the tool name, a redacted request shape, and the
sanitized error. Exclude credentials, token files, real account data, and exports.
