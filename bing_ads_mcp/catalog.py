"""Discover routes and model information from the installed Microsoft SDK.

The public SDK methods are invoked with an offline recording client, never a
network client. Native JSON goes through ApiClient unchanged: generated model
conversion drops unknown fields, null list entries, and some derived types.
"""

import importlib
import inspect
import pkgutil
import types
from dataclasses import dataclass
from enum import Enum, Flag
from functools import cache
from typing import Annotated, Union, get_args, get_origin
from urllib.parse import urlsplit

import openapi_client.api as apis
from fastmcp.exceptions import ToolError
from openapi_client.api_client import ApiClient
from openapi_client.configuration import Configuration
from pydantic import BaseModel

from .policy import JOBS, READS

SERVICES = {
    "campaign": (apis.CampaignManagementServiceApi, "campaign", "campaign"),
    "ad_insight": (apis.AdInsightServiceApi, "adinsight", "adinsight"),
    "reporting": (apis.ReportingServiceApi, "reporting", "reporting"),
    "bulk": (apis.BulkServiceApi, "bulk", "bulk"),
    "customer": (apis.CustomerManagementServiceApi, "clientcenter", "customer"),
    "billing": (apis.CustomerBillingServiceApi, "clientcenter", "billing"),
}


@dataclass(frozen=True)
class Operation:
    service: str
    name: str
    method: str
    path: str
    model: type

    @property
    def effect(self) -> str:
        key = f"{self.service}.{self.name}"
        # Unknown future operations fail closed as writes until reviewed.
        return "read" if key in READS else "job" if key in JOBS else "write"

    def describe(self) -> dict:
        family = SERVICES[self.service][0].__name__.removesuffix("Api")
        slug = "".join("-" + c.lower() if c.isupper() else c for c in family).lstrip("-")
        return {
            "service": self.service,
            "operation": self.name,
            "effect": self.effect,
            "method": self.method,
            "path": self.path,
            "request_model": self.model.__name__,
            "documentation": f"https://learn.microsoft.com/en-us/advertising/{slug}/"
            f"{self.name.replace('_', '')}?view=bingads-13",
        }


class _Recorder(ApiClient):
    def call_api(self, *args, **kwargs):
        return types.SimpleNamespace(response=args)


@cache
def operations() -> dict[tuple[str, str], Operation]:
    result = {}
    client = _Recorder(Configuration(host="https://catalog.invalid", retries=0))
    for service, (api, _, _) in SERVICES.items():
        for name, method in inspect.getmembers(api, inspect.isfunction):
            if not name.endswith("_without_preload_content"):
                continue
            parameters = [
                p
                for p in inspect.signature(method).parameters.values()
                if p.name != "self" and not p.name.startswith("_")
            ]
            if len(parameters) != 1:
                raise RuntimeError(f"SDK contract changed: {service}.{name}")
            parameter = parameters[0]
            model = dict if get_origin(parameter.annotation) is dict else parameter.annotation
            empty = {} if model is dict else model.model_construct()
            verb, url, *_ = getattr(api(client), name)(**{parameter.name: empty})
            name = name.removesuffix("_without_preload_content")
            result[service, name] = Operation(service, name, verb, urlsplit(url).path, model)
    return result


def operation(service: str, name: str) -> Operation:
    try:
        return operations()[service, name]
    except KeyError:
        raise ToolError("Unknown operation. Use list_operations to discover exact names.") from None


@cache
def models(service: str) -> dict[str, type]:
    package = importlib.import_module(f"openapi_client.models.{SERVICES[service][2]}")
    result = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, (BaseModel, Enum)):
                result[name] = cls
    return result


def type_schema(annotation) -> dict:
    """Compact wire schema; SDK Flag validators cannot generate JSON Schema."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return type_schema(args[0])
    if origin in (Union, types.UnionType):
        return {"anyOf": [type_schema(a) for a in args]}
    if origin is list:
        return {"type": "array", "items": type_schema(args[0])}
    if inspect.isclass(annotation) and issubclass(annotation, Flag):
        return {
            "type": "string",
            "values": [m.to_json() for m in annotation],
            "encoding": "Combine flag names with comma and space, e.g. Search, Shopping",
        }
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return {"enum": [m.value for m in annotation]}
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return {"model": annotation.__name__}
    return {
        "type": {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
            dict: "object",
        }.get(annotation, "any")
    }


def describe_model(service: str, name: str) -> dict:
    if name == "dict":
        return {"type": "object", "properties": {}}
    cls = models(service).get(name)
    if cls is None:
        raise ToolError("Unknown model. Use list_models with this service.")
    if issubclass(cls, Enum):
        return type_schema(cls)
    return {
        "model": name,
        "properties": {
            f.alias or key: {**type_schema(f.annotation), "required": f.is_required()}
            for key, f in cls.model_fields.items()
        },
        "derived_models": sorted(
            n for n, c in models(service).items() if c is not cls and issubclass(c, cls)
        ),
        "note": "Native API field names. Inspect nested/derived models separately. "
        "Microsoft enforces operation-specific required fields and business rules.",
    }
