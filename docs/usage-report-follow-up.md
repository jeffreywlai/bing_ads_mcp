# Usage report follow-up

This document records the disposition of the October 13, 2026 user-provided
report. It contains no account data, credentials, exports or transcript excerpts.
The report observed an older server; new behavior is tested with offline provider
fixtures, not newly executed advertising changes.

| Finding | Implemented behavior and remaining limit |
| --- | --- |
| F01 / R1: accepted writes that do not change state | Campaign, ad-group and keyword updates read back by default. Every supplied field has a comparison result; missing fields and failed reads remain unverified. Negative-site sets are also checked. Completed bulk uploads check reviewed fields on existing Campaign rows, including `Bid Adjustment`; unmapped fields/record types remain explicit. No mutation or upload is retried. |
| F02 / R2: previews miss typos | Advisory recursive checks use current SDK fields, types, enums and derived models. Unknown names receive spelling suggestions. Checks never coerce or discard native fields/null slots. A warning is not Microsoft validation. |
| F03 / R3: replacing exclusions removes existing sites | `preflight=true` adds read-only current-list diffs to previews. Execution reads again and requires `allow_remove=true` for removals or empty lists. Null/missing entities and incomplete reads block execution. The guard covers the two documented negative-site set operations; it does not guess replacement semantics for every operation beginning with `set`. |
| F04 / R5: discovery misses names | Operation and model searches ignore case and separators. Compact operation entries retain classification; `details=true` and individual schemas provide full information. |
| F05 / R6: negative-keyword entity limit | Requests containing more than one entity ID fail locally with the documented one-ID limit and instructions to read individually. No automatic splitting hides partial failures. |
| F06 / R6: offline report without dates | Both UTC dates are required before calling Microsoft. Discovery separates documented requirements from the generated SDK's optional fields. |
| F07 / R6: pilot goal type | Discovery/results explain flag encoding and pilot eligibility. Explicitly requested `InStoreTransaction` is preserved; silently dropping it would change the requested coverage. |
| F08 / R7: signed URLs | Credential query parameters are redacted from MCP JSON results/errors after internal downloads. Native read callers can request signed Microsoft file downloads before redaction, preserving access to offline report files. Exact downloaded file bytes remain available. |
| F09 / R4: formatted numbers | Explicit `numeric_fields` adds parsed values next to raw data for CSV/JSON arrays and entities. Percentage points, missing values and decimal precision are preserved. It does not infer identifiers as metrics or duplicate every number by default. |
| F10 / R4: retention clips history | Report context persists requested dates and known retention hints. Completed CSV reports show observed time buckets. The original request is never clipped; observed buckets do not establish exact effective coverage or complete first months. Unknown retention mappings remain unknown. |
| F11: null import jobs | Operation guidance explains that a null result is not proof of absent UI schedules. This provider/UI visibility question is unresolved. |
| F12: client setup | README retains GitHub `uvx` installation, Python 3.15, per-user sign-in and restart/reconnect instructions. The server does not kill unrelated processes or control Claude's tool approval UI. |

The existing mutation session guard and UET first-key-creation disclosure already
addressed the other R7 requests. Write tools remain discoverable for local
previews; execution requires an explicit session handle. Hiding tool descriptions
is not the authorization boundary.

## Qualifications discovered during implementation

The report's ad-group audience-bid test needs an important qualification:
Microsoft requires `UpdateAudienceAdsBidAdjustment=true` at the request level or
ignores `AudienceAdsBidAdjustment`. The server now warns when that flag is absent.
This may explain that particular no-op; it does not explain the campaign-level
observations. The latter remain unconfirmed upstream behavior, not a universal
restriction enforced by this server. See
[UpdateAdGroups](https://learn.microsoft.com/en-us/advertising/campaign-management-service/updateadgroups?view=bingads-13)
and [Campaign](https://learn.microsoft.com/en-us/advertising/campaign-management-service/campaign?view=bingads-13).

Read-back says whether requested values are currently visible, not whether a
write caused a change. Propagation delays, null update semantics, normalization
and concurrent editors remain relevant. Replacement checks are not atomic locks.
Bulk creates/deletes and every unmapped record/column still need explicit
verification using the native APIs and complete result files.

## Manual analysis gaps

G01 is reduced by automatic supported read-back. G02/G06 remain agent-composed
analysis over complete local report files: period comparisons, weighting and
publisher heuristics depend on the question and should not become a second
analytics framework inside this MCP. Filtering, sorting, selected fields and
numeric views reduce the data passed into conversation context. G03 gains clean
numeric views; warehouse loading still belongs to the warehouse workflow. G04
has reviewed Campaign bulk mappings and the native
[Bulk schema reference](https://learn.microsoft.com/en-us/advertising/bulk-service/campaign?view=bingads-13).
G05 has flag and pilot guidance without silently changing requested goal types.

## Verification

Tests cover complete pagination and source recovery, Unicode chunk boundaries,
numeric precision, null slots, derived/future fields, no-op/read-failure handling,
replacement guards, locked execution, signed downloads, and retained report/job
context. `scripts/benchmark_responses.py` measures response UTF-8 bytes on synthetic
data. It does not estimate Claude billing, tokenizer counts or analysis quality.

Run `uv run python -m scripts.benchmark_responses`. On the 150-row synthetic
fixture, the measured MCP text output was:

| Response path | UTF-8 bytes | Calls |
| --- | ---: | ---: |
| Operation discovery, previous detailed default | 16,040 | 1 |
| Compact operation discovery | 3,914 | 1 |
| Complete artifact via base64 | 86,691 | 6 |
| Complete artifact via structured JSON | 61,896 | 3 |
| Same rows with four explicitly selected fields | 21,424 | 2 |

Full JSON retrieval is 28.6% smaller in this fixture; discovery is 75.6% smaller.
Field projection changes the displayed view, while the complete original stays
retrievable. Verification and requested numeric views add useful information and
can increase output; these figures do not claim every call became smaller.

Reference rules were checked against Microsoft's
[negative-keyword read limit](https://learn.microsoft.com/en-us/advertising/campaign-management-service/getnegativekeywordsbyentityids?view=bingads-13),
[offline report dates](https://learn.microsoft.com/en-us/advertising/campaign-management-service/getofflineconversionreports?view=bingads-13),
[campaign exclusions](https://learn.microsoft.com/en-us/advertising/campaign-management-service/setnegativesitestocampaigns?view=bingads-13),
[ad-group exclusions](https://learn.microsoft.com/en-us/advertising/campaign-management-service/setnegativesitestoadgroups?view=bingads-13),
[pilot eligibility](https://learn.microsoft.com/en-us/advertising/campaign-management-service/instoretransactiongoal?view=bingads-13)
and [retention table](https://learn.microsoft.com/en-us/advertising/guides/report-data-retention-time-periods?view=bingads-13).
