"""Advisory SDK validation plus a few documented service constraints.

Never construct SDK request models: they can drop native fields and null slots.
Unknown future fields remain usable and Microsoft's business validation is authoritative.
"""

import re
from difflib import get_close_matches

from .catalog import describe_model, models, type_schema


def normalized(name: str) -> str:
    return "".join(c for c in name.casefold() if c.isalnum())


def operation_hints(service: str, name: str) -> dict:
    if service != "campaign":
        return {}
    if name == "update_ad_groups":
        return {
            "hints": [
                "AudienceAdsBidAdjustment is ignored unless the request-level "
                "UpdateAudienceAdsBidAdjustment flag is true. See the operation documentation."
            ]
        }
    if name == "get_negative_keywords_by_entity_ids":
        return {"hints": ["EntityIds accepts at most one ID. Read each entity separately."]}
    if name == "get_offline_conversion_reports":
        return {
            "documented_required": ["StartDateUtc", "EndDateUtc"],
            "hints": [
                "Both UTC dates are required by Microsoft despite the SDK's optional fields."
            ],
        }
    if name in {"get_conversion_goals_by_ids", "get_conversion_goals_by_tag_ids"}:
        return {
            "hints": [
                "ConversionGoalTypes uses comma-and-space separated names, e.g. Url, Event, "
                "OfflineConversion. InStoreTransaction requires Microsoft's alpha pilot; use it "
                "only for enabled customers. Explicitly requested types are never silently removed."
            ]
        }
    if name.startswith("set_negative_sites_to_"):
        return {
            "hints": [
                "This replaces the entire directly assigned list; [] clears it. Use "
                "write_api(preflight=true) to preview the current-list diff. Execution reads again "
                "and requires allow_remove=true for any removal or empty replacement."
            ]
        }
    if name == "get_import_jobs_by_ids":
        return {
            "hints": [
                "A null ImportJobs result does not prove no UI-scheduled imports exist. "
                "Check ImportType, known IDs and the Microsoft Advertising import schedule UI."
            ]
        }
    return {}


def request_issues(service: str, model: type, body: dict) -> list[dict]:
    def inspect(schema, value, path):
        if "anyOf" in schema:
            options = schema["anyOf"]
            if value is not None:
                options = [s for s in options if s.get("type") != "null"]
            choices = [inspect(s, value, path) for s in options]
            return min(choices, key=len)
        if "model" in schema:
            if not isinstance(value, dict):
                return [{"path": path, "issue": "Expected an object."}]
            name = schema["model"]
            cls = models(service)[name]
            kind = value.get("Type")
            # Native discriminators may omit the base name (ResponsiveSearch -> Ad).
            if isinstance(kind, str):
                for candidate in (kind, kind + name):
                    derived = models(service).get(candidate)
                    if derived and issubclass(derived, cls):
                        cls = derived
                        break
            properties = describe_model(service, cls.__name__)["properties"]
            issues = []
            for key, item in value.items():
                field_path = f"{path}.{key}" if path else key
                if key not in properties:
                    issues.append(
                        {
                            "path": field_path,
                            "issue": "Unknown SDK field; Microsoft may ignore it.",
                            "suggestions": get_close_matches(key, properties, n=2, cutoff=0.7),
                        }
                    )
                else:
                    issues.extend(inspect(properties[key], item, field_path))
            return issues
        if schema.get("type") == "array":
            if isinstance(value, list):
                return [
                    issue
                    for i, item in enumerate(value)
                    for issue in inspect(schema["items"], item, f"{path}[{i}]")
                ]
            return [{"path": path, "issue": "Expected an array."}]
        if "enum" in schema or "values" in schema:
            values = schema.get("enum", schema.get("values"))
            items = value.split(", ") if "values" in schema and isinstance(value, str) else [value]
            if any(item not in values for item in items):
                return [
                    {
                        "path": path,
                        "issue": "Value is outside the SDK enum. "
                        "For flags use comma-and-space separated names; inspect get_model_schema.",
                    }
                ]
            return []
        kind = schema.get("type", "any")
        valid = {
            "null": value is None,
            "string": isinstance(value, str)
            or (type(value) is int and bool(re.search(r"Ids?(?:\[\d+\])?$", path))),
            "integer": type(value) is int,
            "number": type(value) in (int, float),
            "boolean": type(value) is bool,
            "object": isinstance(value, dict),
            "any": True,
        }.get(kind, True)
        return [] if valid else [{"path": path, "issue": f"Expected {kind}."}]

    return inspect(type_schema(model), body, "")


def request_constraints(service: str, name: str, body: dict) -> str | None:
    if service != "campaign":
        return None
    if name == "get_negative_keywords_by_entity_ids" and isinstance(body.get("EntityIds"), list):
        if len(body["EntityIds"]) > 1:
            return "EntityIds accepts at most one ID. Call once per entity (Microsoft limit 1033)."
    if name == "get_offline_conversion_reports":
        if not all(body.get(field) for field in ("StartDateUtc", "EndDateUtc")):
            return "StartDateUtc and EndDateUtc are required UTC dates for this operation."
    return None
