# Bing Ads MCP

A Microsoft Advertising MCP server in development, targeting full public API
access, performance reporting, and account changes. The MCP tools are not
implemented yet; the current files are an initial prototype undergoing
foundation checks.

## Required coverage

Full public Microsoft Advertising API coverage is a project requirement. The
following table describes required capabilities, not completed implementation.

| API surface | Required coverage | Current status |
| --- | --- | --- |
| Campaign Management | All public operations and supported request fields, including campaigns, ads, keywords, targeting, budgets, bidding, assets, audiences, and conversion configuration | Not implemented as MCP tools |
| Ad Insight | All public operations, including keyword research, traffic estimates, auction insights, recommendations, and recommendation changes | Not implemented |
| Reporting | All public report types and fields; submit, poll, download, and deliver complete results | Not implemented |
| Bulk | All public operations; complete upload, download, polling, and per-record result handling | Not implemented |
| Customer Management | All public account/customer operations | Not implemented |
| Customer Billing | All public billing operations | Not implemented |
| Shopping Content | Products, inventory, catalogs, and their public operations through a separate adapter | Not implemented |

The [REST SDK guide](https://learn.microsoft.com/en-us/advertising/guides/python-sdk-migration-soap-to-rest?view=bingads-13)
identifies the six Bing Ads v13 services. The
[Content API documentation](https://learn.microsoft.com/en-us/advertising/shopping-content/get-started)
explicitly states that the SDK does not provide Content API interfaces.
Additional current public Microsoft Advertising API families must be inventoried
and covered; omission from the SDK is an implementation gap to resolve, not an
implicit scope exclusion. Microsoft's account permissions and feature eligibility
continue to apply.

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

Direct dependencies are pinned in `pyproject.toml`. A complete dependency lock
must be generated and validated on Python 3.15 before release. It has not been
generated yet. Updates should move to the latest stable releases, rerun the
contract tests, and commit the resulting lock. An older version requires a
specific, documented incompatibility.

## Foundation

FastMCP will handle MCP schemas, validation, tool registration, and transport.
Microsoft's REST SDK is the baseline for evaluating the API adapter. The local
code should own only the behavior agents need: explicit account scope, report
delivery, previews of proposed changes, and clear results for successful,
partially successful, or uncertain writes.

The Meta and Google MCP projects provide behavior conventions: preserve complete
exports, expose actionable API errors, and never automatically replay campaign
mutations. Their older dependency constraints are specific to those projects.

The custom REST client in this prototype remains provisional. Before finalizing
the SDK integration, verify request serialization, explicit timeouts, disabled
transport retries for mutations, partial errors, and report download behavior
against the installed current SDK. No Bing account has been contacted or changed.

## Current validation limit

FastMCP 4.1.0, MCP 2.3.0, and Pydantic 2.14.1 passed an in-process MCP
client/server smoke check using cached packages on the available Python 3.12
interpreter: initialization, tool discovery, nested input validation, structured
results, and invalid-ID rejection. This does not establish Python 3.15 or
Microsoft SDK compatibility.

The development environment currently lacks Python 3.15 and `msads`. Dependency
installation is restricted, and its installed `uv` also crashes while reading
macOS system configuration inside the sandbox. These are environment blockers,
not established incompatibilities in the selected releases. The full stack and
live reporting/writes have not been validated.
