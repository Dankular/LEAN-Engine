from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import Field, model_validator

from .models import StrictModel


class KnowledgeRequest(StrictModel):
    provider: Literal["wikidata", "dbnomics", "nist_codata", "geonames", "open_meteo"]
    operation: Literal[
        "search", "entity", "population", "capital", "property", "series", "constant", "weather", "weather_forecast"
    ]
    query: str | None = Field(default=None, min_length=1, max_length=400)
    entity_id: str | None = Field(default=None, pattern=r"^[QP][1-9][0-9]*$")
    property_id: Literal[
        "P17", "P30", "P36", "P37", "P38", "P1082", "P2131", "P4010", "P2132", "P2299"
    ] | None = None
    provider_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    dataset_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    series_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    constant: str | None = Field(default=None, min_length=1, max_length=80)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    units: Literal["metric", "nonmetric"] | None = None
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    max_results: int = Field(default=5, ge=1, le=10)
    timeout_seconds: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_provider_operation(self) -> KnowledgeRequest:
        expected = {
            "wikidata": {"search", "entity", "population", "capital", "property"},
            "dbnomics": {"search", "series"},
            "nist_codata": {"constant"},
            "geonames": {"search"},
            "open_meteo": {"weather", "weather_forecast"},
        }
        if self.operation not in expected[self.provider]:
            raise ValueError(f"{self.operation} is not supported by {self.provider}")
        if self.provider == "wikidata":
            if self.operation in {"search", "population", "capital", "property"} and self.query is None:
                raise ValueError(f"Wikidata {self.operation} lookup requires query")
            if self.operation == "entity" and self.entity_id is None:
                raise ValueError("Wikidata entity lookup requires entity_id")
            if self.operation == "property" and self.property_id is None:
                raise ValueError("Wikidata property lookup requires an allowlisted property_id")
            if self.operation != "property" and self.property_id is not None:
                raise ValueError("property_id is only used with Wikidata property lookups")
        elif self.property_id is not None:
            raise ValueError("property_id is only supported for Wikidata")
        elif self.provider == "dbnomics":
            if self.operation == "search" and self.query is None:
                raise ValueError("DBnomics dataset search requires query")
            if self.operation == "series" and not all((self.provider_code, self.dataset_code, self.series_code)):
                raise ValueError("DBnomics series lookup requires provider_code, dataset_code, and series_code")
        elif self.provider == "nist_codata" and self.constant is None:
            raise ValueError("NIST lookup requires constant")
        elif self.provider == "geonames" and self.query is None:
            raise ValueError("GeoNames search requires query")
        elif self.provider == "open_meteo":
            has_lat = self.latitude is not None
            has_lon = self.longitude is not None
            if has_lat != has_lon:
                raise ValueError("Open-Meteo requires both latitude and longitude")
            if not (has_lat and has_lon) and self.location is None:
                raise ValueError("Open-Meteo requires a location or coordinates")
        if self.provider != "open_meteo" and self.units is not None:
            raise ValueError("units are currently supported only for Open-Meteo weather")
        return self


class KnowledgeProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class KnowledgeDeadlineExceeded(TimeoutError):
    """The caller's absolute query deadline expired during provider retrieval."""


class ProviderResponse(dict[str, object]):
    def __init__(self, payload: dict[str, object], cache_metadata: dict[str, object]) -> None:
        super().__init__(payload)
        self.cache_metadata = cache_metadata


_WIKIDATA_CACHE_TTL_SECONDS = 300
_WIKIDATA_CACHE_MAX_ENTRIES = 128
_WIKIDATA_CACHE_MAX_ENTRY_BYTES = 2_000_000
_WIKIDATA_CACHE_MAX_BYTES = 16_000_000
_WIKIDATA_CACHE_LOCK = Lock()
_WIKIDATA_CACHE: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_WIKIDATA_CACHE_BYTES = 0


def query_knowledge(
    request: KnowledgeRequest, *, deadline: float | None = None
) -> dict[str, object]:
    try:
        _check_deadline(deadline)
        if request.provider == "wikidata":
            data, source = _wikidata(request, deadline)
        elif request.provider == "dbnomics":
            data, source = _dbnomics(request, deadline)
        elif request.provider == "nist_codata":
            data, source = _nist(request)
        elif request.provider == "geonames":
            data, source = _geonames(request, deadline)
        else:
            data, source = _open_meteo(request, deadline)
        _check_deadline(deadline)
    except KnowledgeDeadlineExceeded:
        raise
    except KnowledgeProviderError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        if deadline is not None and perf_counter() >= deadline:
            raise KnowledgeDeadlineExceeded from exc
        if isinstance(exc, HTTPError):
            code = 502 if exc.code < 500 else 503
            raise KnowledgeProviderError(f"{request.provider} returned HTTP {exc.code}", code) from exc
        raise KnowledgeProviderError(f"Could not retrieve data from {request.provider}") from exc

    evidence: dict[str, object] = {"kind": "retrieved_source_data", "verified": False}
    if isinstance(data, dict) and isinstance(data.get("source_cache"), dict):
        evidence["source_cache"] = data["source_cache"]
    return {
        "provider": request.provider,
        "operation": request.operation,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sources": [source],
        "data": data,
        "evidence": evidence,
        "qualification": "Source data is returned with provenance; retrieval does not establish a formal proof.",
    }


def _request_json(
    url: str, timeout: int, deadline: float | None = None
) -> ProviderResponse:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": os.environ.get(
                "DATA_PROVIDER_USER_AGENT", "LeanProofService/0.1 (source-aware computation API)"
            ),
        },
    )
    _check_deadline(deadline)
    effective_timeout = float(timeout)
    if deadline is not None:
        effective_timeout = min(effective_timeout, deadline - perf_counter())
        if effective_timeout <= 0:
            raise KnowledgeDeadlineExceeded
    cacheable = url.startswith("https://www.wikidata.org/w/api.php?")
    if cacheable:
        cached = _wikidata_cache_get(url)
        if cached is not None:
            payload, source_retrieved_at = cached
            value = json.loads(payload)
            if isinstance(value, dict):
                return ProviderResponse(value, {
                    "status": "hit",
                    "source_retrieved_at": source_retrieved_at,
                })
    try:
        with urlopen(request, timeout=effective_timeout) as response:
            payload = response.read(2_000_001)
    except (TimeoutError, OSError) as exc:
        if deadline is not None and perf_counter() >= deadline:
            raise KnowledgeDeadlineExceeded from exc
        raise
    _check_deadline(deadline)
    if len(payload) > 2_000_000:
        raise KnowledgeProviderError("Provider response exceeded the 2 MB limit", 502)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise KnowledgeProviderError("Provider returned an unexpected JSON shape", 502)
    source_retrieved_at = datetime.now(timezone.utc).isoformat()
    cached = cacheable and _wikidata_cache_put(url, payload, source_retrieved_at)
    return ProviderResponse(value, {
        "status": "miss" if cached else ("bypass" if cacheable else "not_applicable"),
        "source_retrieved_at": source_retrieved_at,
    })


def _wikidata_cache_get(url: str) -> tuple[bytes, str] | None:
    global _WIKIDATA_CACHE_BYTES
    now = perf_counter()
    with _WIKIDATA_CACHE_LOCK:
        entry = _WIKIDATA_CACHE.get(url)
        if entry is None:
            return None
        expires_at, payload, source_retrieved_at = entry
        if expires_at <= now:
            _WIKIDATA_CACHE.pop(url, None)
            _WIKIDATA_CACHE_BYTES -= len(payload)
            return None
        _WIKIDATA_CACHE.move_to_end(url)
        return payload, source_retrieved_at


def _wikidata_cache_put(url: str, payload: bytes, source_retrieved_at: str) -> bool:
    global _WIKIDATA_CACHE_BYTES
    if len(payload) > _WIKIDATA_CACHE_MAX_ENTRY_BYTES:
        return False
    with _WIKIDATA_CACHE_LOCK:
        previous = _WIKIDATA_CACHE.pop(url, None)
        if previous is not None:
            _WIKIDATA_CACHE_BYTES -= len(previous[1])
        _WIKIDATA_CACHE[url] = (
            perf_counter() + _WIKIDATA_CACHE_TTL_SECONDS,
            payload,
            source_retrieved_at,
        )
        _WIKIDATA_CACHE_BYTES += len(payload)
        while (
            len(_WIKIDATA_CACHE) > _WIKIDATA_CACHE_MAX_ENTRIES
            or _WIKIDATA_CACHE_BYTES > _WIKIDATA_CACHE_MAX_BYTES
        ):
            _, evicted = _WIKIDATA_CACHE.popitem(last=False)
            _WIKIDATA_CACHE_BYTES -= len(evicted[1])
        return url in _WIKIDATA_CACHE


def _wikidata_cache_summary(responses: list[ProviderResponse]) -> dict[str, object]:
    metadata = [response.cache_metadata for response in responses]
    statuses = [str(item.get("status", "bypass")) for item in metadata]
    hit_count = sum(status == "hit" for status in statuses)
    miss_count = sum(status == "miss" for status in statuses)
    status = "hit" if hit_count == len(statuses) and statuses else (
        "partial" if hit_count else ("miss" if miss_count else "bypass")
    )
    retrieved_times = [
        str(item["source_retrieved_at"])
        for item in metadata
        if isinstance(item.get("source_retrieved_at"), str)
    ]
    summary: dict[str, object] = {
        "status": status,
        "ttl_seconds": _WIKIDATA_CACHE_TTL_SECONDS,
        "cached_responses": hit_count,
        "live_responses": len(statuses) - hit_count,
    }
    if retrieved_times:
        summary["oldest_source_response_at"] = min(retrieved_times)
    return summary


def _wikidata(
    request: KnowledgeRequest, deadline: float | None
) -> tuple[object, dict[str, str]]:
    base = "https://www.wikidata.org/w/api.php"
    if request.operation == "search":
        params = {
            "action": "wbsearchentities", "search": request.query or "", "language": request.language,
            "format": "json", "type": "item", "limit": str(request.max_results),
        }
        data = _request_json(f"{base}?{urlencode(params)}", request.timeout_seconds, deadline)
        results = data.get("search", [])
        return results, {
            "name": "Wikidata entity search", "url": "https://www.wikidata.org/", "license": "CC0"
        }
    if request.operation in {"population", "capital", "property"}:
        return _wikidata_property(request, base, deadline)
    params = {
        "action": "wbgetentities", "ids": request.entity_id or "", "props": "labels|descriptions|claims",
        "languages": request.language, "format": "json",
    }
    data = _request_json(f"{base}?{urlencode(params)}", request.timeout_seconds, deadline)
    entities = data.get("entities", {})
    entity = entities.get(request.entity_id or "") if isinstance(entities, dict) else None
    return entity, {
        "name": "Wikidata entity API", "url": "https://www.wikidata.org/", "license": "CC0"
    }


def _wikidata_property(
    request: KnowledgeRequest,
    base: str,
    deadline: float | None,
) -> tuple[object, dict[str, str]]:
    property_id = {
        "population": "P1082",
        "capital": "P36",
        "property": request.property_id,
    }.get(request.operation)
    property_names = {
        "P17": "country",
        "P30": "continent",
        "P36": "capital",
        "P37": "official language",
        "P38": "currency",
        "P1082": "population",
        "P2131": "nominal GDP",
        "P4010": "GDP at purchasing power parity",
        "P2132": "nominal GDP per capita",
        "P2299": "GDP per capita at purchasing power parity",
    }
    if property_id not in property_names:
        raise KnowledgeProviderError("Wikidata property is not allowlisted", 400)
    property_name = property_names[property_id]
    search_params = {
        "action": "wbsearchentities",
        "search": request.query or "",
        "language": request.language,
        "format": "json",
        "type": "item",
        "limit": str(request.max_results),
    }
    search_data = _request_json(
        f"{base}?{urlencode(search_params)}", request.timeout_seconds, deadline
    )
    cached_responses = [search_data]
    search_results = search_data.get("search", [])
    candidates = [
        result for result in search_results
        if isinstance(search_results, list)
        and isinstance(result, dict)
        and isinstance(result.get("id"), str)
        and re.fullmatch(r"Q[1-9][0-9]*", result["id"])
    ][: request.max_results] if isinstance(search_results, list) else []
    if not candidates:
        return {
            "property_id": property_id,
            "property": property_name,
            "entity_query": request.query,
            "matches": [],
            "selection_rule": "Wikidata entity search order; no entity selected by the service",
            "source_cache": _wikidata_cache_summary(cached_responses),
        }, {
            "name": "Wikidata API",
            "url": base,
            "license": "CC0",
            "attribution": "Wikidata contributors",
        }

    candidate_ids = [candidate["id"] for candidate in candidates]
    entity_params = {
        "action": "wbgetentities",
        "ids": "|".join(candidate_ids),
        "props": "labels|descriptions|claims",
        "languages": request.language,
        "format": "json",
    }
    entity_data = _request_json(
        f"{base}?{urlencode(entity_params)}", request.timeout_seconds, deadline
    )
    cached_responses.append(entity_data)
    entities = entity_data.get("entities", {})
    if not isinstance(entities, dict):
        entities = {}

    matches: list[dict[str, object]] = []
    target_ids: set[str] = set()
    value_key = {
        "P17": "countries",
        "P30": "continents",
        "P36": "capitals",
        "P37": "languages",
        "P38": "currencies",
        "P1082": "population",
        "P2131": "nominal_gdp",
        "P4010": "gdp_ppp",
        "P2132": "nominal_gdp_per_capita",
        "P2299": "gdp_ppp_per_capita",
    }[property_id]
    for search_rank, candidate in enumerate(candidates, start=1):
        entity_id = candidate["id"]
        entity = entities.get(entity_id)
        if not isinstance(entity, dict):
            continue
        claims = entity.get("claims", {})
        if isinstance(claims, dict):
            claims = claims.get(property_id, [])
        if property_id == "P1082":
            values = _wikidata_population_values(claims)
        elif property_id in {"P2131", "P4010", "P2132", "P2299"}:
            values = _wikidata_quantity_values(claims, property_id)
            target_ids.update(
                str(value["unit_id"])
                for value in values
                if isinstance(value.get("unit_id"), str)
            )
        else:
            values = _wikidata_entity_values(claims, property_id)
            target_ids.update(
                str(value["id"]) for value in values if isinstance(value.get("id"), str)
            )
        matches.append({
            "id": entity_id,
            "search_rank": search_rank,
            "label": (
                _wikidata_language_value(entity.get("labels"), request.language)
                or candidate.get("label")
                or entity_id
            ),
            "description": (
                _wikidata_language_value(entity.get("descriptions"), request.language)
                or candidate.get("description")
            ),
            "url": f"https://www.wikidata.org/wiki/{entity_id}",
            value_key: values,
        })

    if property_id != "P1082" and target_ids:
        target_params = {
            "action": "wbgetentities",
            "ids": "|".join(sorted(target_ids)),
            "props": "labels",
            "languages": request.language,
            "format": "json",
        }
        target_data = _request_json(
            f"{base}?{urlencode(target_params)}", request.timeout_seconds, deadline
        )
        cached_responses.append(target_data)
        target_entities = target_data.get("entities", {})
        if not isinstance(target_entities, dict):
            target_entities = {}
        for match in matches:
            relationships = match.get(value_key, [])
            if not isinstance(relationships, list):
                continue
            if property_id in {"P2131", "P4010", "P2132", "P2299"}:
                for quantity in relationships:
                    if not isinstance(quantity, dict):
                        continue
                    unit_id = quantity.get("unit_id")
                    unit_entity = target_entities.get(unit_id)
                    unit_label = (
                        _wikidata_language_value(unit_entity.get("labels"), request.language)
                        if isinstance(unit_entity, dict)
                        else None
                    )
                    if unit_label:
                        quantity["unit_label"] = unit_label
                    if isinstance(unit_id, str):
                        quantity["unit_url"] = f"https://www.wikidata.org/wiki/{unit_id}"
                continue
            for relationship in relationships:
                if not isinstance(relationship, dict):
                    continue
                relationship_entity = target_entities.get(relationship.get("id"))
                if isinstance(relationship_entity, dict):
                    relationship["label"] = (
                        _wikidata_language_value(relationship_entity.get("labels"), request.language)
                        or relationship.get("id")
                    )
                relationship["url"] = (
                    f"https://www.wikidata.org/wiki/{relationship.get('id')}"
                )

    selection_rule = (
        "Wikidata entity search order; all returned candidates are preserved. Preferred "
        "population statements are used when present; otherwise the latest dated statements "
        "are returned with ties preserved."
        if property_id == "P1082"
        else (
            "Wikidata entity search order; preferred GDP statements are used when present, "
            "otherwise the latest dated statements are returned with ties preserved; amount, "
            "unit, rank, references, and time qualifiers are preserved."
            if property_id in {"P2131", "P4010", "P2132", "P2299"}
            else (
                "Wikidata entity search order; all returned candidates and non-deprecated "
                f"{property_name} statements are preserved, including rank, references, "
                "and time qualifiers where available."
            )
        )
    )
    return {
        "property_id": property_id,
        "property": property_name,
        "entity_query": request.query,
        "matches": matches,
        "selection_rule": selection_rule,
        "source_cache": _wikidata_cache_summary(cached_responses),
    }, {
        "name": "Wikidata API",
        "url": base,
        "license": "CC0",
        "attribution": "Wikidata contributors",
    }


def _wikidata_language_value(value: object, language: str) -> str | None:
    if not isinstance(value, dict):
        return None
    localized = value.get(language)
    if not isinstance(localized, dict):
        return None
    text = localized.get("value")
    return text if isinstance(text, str) else None


def _wikidata_population_values(claims: object) -> list[dict[str, object]]:
    if isinstance(claims, dict):
        statements = claims.get("P1082", claims.get("claims", []))
    else:
        statements = claims
    if not isinstance(statements, list):
        return []

    values: list[dict[str, object]] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        main_snak = statement.get("mainsnak")
        if not isinstance(main_snak, dict) or main_snak.get("snaktype") != "value":
            continue
        datavalue = main_snak.get("datavalue")
        quantity = datavalue.get("value") if isinstance(datavalue, dict) else None
        amount = quantity.get("amount") if isinstance(quantity, dict) else None
        if not isinstance(amount, str) or len(amount) > 128:
            continue
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", amount):
            continue
        point_in_time = None
        qualifiers = statement.get("qualifiers")
        time_snaks = qualifiers.get("P585") if isinstance(qualifiers, dict) else None
        if isinstance(time_snaks, list):
            for time_snak in time_snaks:
                if not isinstance(time_snak, dict):
                    continue
                time_data = time_snak.get("datavalue")
                time_value = time_data.get("value") if isinstance(time_data, dict) else None
                time_text = time_value.get("time") if isinstance(time_value, dict) else None
                if isinstance(time_text, str):
                    point_in_time = time_text
                    break
        observation: dict[str, object] = {
            "amount": amount.removeprefix("+"),
            "rank": statement.get("rank", "normal"),
        }
        statement_id = statement.get("id")
        if isinstance(statement_id, str):
            observation["statement_id"] = statement_id
        references = statement.get("references")
        if isinstance(references, list):
            observation["reference_count"] = len(references)
        if point_in_time is not None:
            observation["point_in_time"] = point_in_time
        if isinstance(quantity, dict):
            for bound in ("lowerBound", "upperBound"):
                bound_value = quantity.get(bound)
                if isinstance(bound_value, str) and len(bound_value) <= 128:
                    observation[bound] = bound_value.removeprefix("+")
        values.append(observation)

    preferred = [item for item in values if item.get("rank") == "preferred"]
    eligible = preferred or values
    dated = [item for item in eligible if isinstance(item.get("point_in_time"), str)]
    if dated:
        latest = max(str(item["point_in_time"]) for item in dated)
        eligible = [item for item in dated if item["point_in_time"] == latest]
    return eligible[:5]


def _wikidata_quantity_values(
    claims: object, property_id: str
) -> list[dict[str, object]]:
    statements = claims
    if isinstance(statements, dict):
        statements = statements.get(property_id, statements.get("claims", []))
    if not isinstance(statements, list):
        return []

    values: list[dict[str, object]] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        main_snak = statement.get("mainsnak")
        if not isinstance(main_snak, dict) or main_snak.get("snaktype") != "value":
            continue
        datavalue = main_snak.get("datavalue")
        quantity = datavalue.get("value") if isinstance(datavalue, dict) else None
        if not isinstance(quantity, dict):
            continue
        amount = quantity.get("amount")
        if (
            not isinstance(amount, str)
            or len(amount) > 128
            or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", amount)
        ):
            continue
        observation: dict[str, object] = {
            "amount": amount.removeprefix("+"),
            "rank": statement.get("rank", "normal"),
        }
        raw_unit = quantity.get("unit")
        if isinstance(raw_unit, str) and raw_unit != "1":
            unit_match = re.search(r"/entity/(Q[1-9][0-9]*)$", raw_unit)
            if unit_match:
                observation["unit_id"] = unit_match.group(1)
            else:
                observation["unit"] = raw_unit
        statement_id = statement.get("id")
        if isinstance(statement_id, str):
            observation["statement_id"] = statement_id
        references = statement.get("references")
        if isinstance(references, list):
            observation["reference_count"] = len(references)
        for bound in ("lowerBound", "upperBound"):
            bound_value = quantity.get(bound)
            if (
                isinstance(bound_value, str)
                and len(bound_value) <= 128
                and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", bound_value)
            ):
                observation[bound] = bound_value.removeprefix("+")
        qualifiers = statement.get("qualifiers")
        point_in_time = None
        time_snaks = qualifiers.get("P585") if isinstance(qualifiers, dict) else None
        if isinstance(time_snaks, list):
            for time_snak in time_snaks:
                if not isinstance(time_snak, dict):
                    continue
                time_data = time_snak.get("datavalue")
                time_value = time_data.get("value") if isinstance(time_data, dict) else None
                time_text = time_value.get("time") if isinstance(time_value, dict) else None
                if isinstance(time_text, str):
                    point_in_time = time_text
                    break
        if point_in_time is not None:
            observation["point_in_time"] = point_in_time
        values.append(observation)

    preferred = [item for item in values if item.get("rank") == "preferred"]
    eligible = preferred or values
    dated = [item for item in eligible if isinstance(item.get("point_in_time"), str)]
    if dated:
        latest = max(str(item["point_in_time"]) for item in dated)
        eligible = [item for item in dated if item["point_in_time"] == latest]
    return eligible[:5]


def _wikidata_entity_values(
    claims: object, property_id: str
) -> list[dict[str, object]]:
    statements = claims
    if isinstance(statements, dict):
        statements = statements.get(property_id, statements.get("claims", []))
    if not isinstance(statements, list):
        return []
    values: list[dict[str, object]] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        main_snak = statement.get("mainsnak")
        if not isinstance(main_snak, dict) or main_snak.get("snaktype") != "value":
            continue
        datavalue = main_snak.get("datavalue")
        entity_value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if not isinstance(entity_value, dict) or entity_value.get("entity-type") != "item":
            continue
        entity_id = entity_value.get("id")
        numeric_id = entity_value.get("numeric-id")
        if not isinstance(entity_id, str) and isinstance(numeric_id, int):
            entity_id = f"Q{numeric_id}"
        if not isinstance(entity_id, str) or not re.fullmatch(r"Q[1-9][0-9]*", entity_id):
            continue
        value: dict[str, object] = {"id": entity_id, "rank": statement.get("rank", "normal")}
        statement_id = statement.get("id")
        if isinstance(statement_id, str):
            value["statement_id"] = statement_id
        references = statement.get("references")
        if isinstance(references, list):
            value["reference_count"] = len(references)
        qualifiers = statement.get("qualifiers")
        if isinstance(qualifiers, dict):
            for property_name, output_name in (
                ("P580", "start_time"), ("P582", "end_time"), ("P585", "point_in_time")
            ):
                snaks = qualifiers.get(property_name)
                if not isinstance(snaks, list):
                    continue
                for snak in snaks:
                    snak_data = snak.get("datavalue") if isinstance(snak, dict) else None
                    time_value = snak_data.get("value") if isinstance(snak_data, dict) else None
                    time_text = time_value.get("time") if isinstance(time_value, dict) else None
                    if isinstance(time_text, str):
                        value[output_name] = time_text
                        break
        values.append(value)
    preferred = [value for value in values if value.get("rank") == "preferred"]
    return (preferred or values)[:5]


def _dbnomics(
    request: KnowledgeRequest, deadline: float | None
) -> tuple[object, dict[str, str]]:
    if request.operation == "search":
        url = "https://api.db.nomics.world/v22/search?" + urlencode(
            {"q": request.query or "", "limit": request.max_results}
        )
        response = _request_json(url, request.timeout_seconds, deadline)
        results = response.get("results")
        docs = results.get("docs") if isinstance(results, dict) else None
        if not isinstance(docs, list):
            raise KnowledgeProviderError("DBnomics returned an invalid dataset search response")
        candidates: list[dict[str, object]] = []
        for rank, item in enumerate(docs[: request.max_results], start=1):
            if not isinstance(item, dict):
                continue
            provider_code = item.get("provider_code")
            dataset_code = item.get("code")
            if not isinstance(provider_code, str) or not isinstance(dataset_code, str):
                continue
            candidate: dict[str, object] = {
                "id": f"dbnomics:dataset:{provider_code}/{dataset_code}",
                "rank": rank,
                "provider_code": provider_code,
                "provider_name": item.get("provider_name"),
                "dataset_code": dataset_code,
                "name": item.get("name") or dataset_code,
                "url": (
                    "https://db.nomics.world/"
                    f"{quote(provider_code, safe='')}/{quote(dataset_code, safe='')}"
                ),
            }
            description = item.get("description")
            if isinstance(description, str) and description.strip():
                candidate["description"] = description[:1000]
            for source_key, target_key in (
                ("nb_matching_series", "matching_series_count"),
                ("nb_series", "series_count"),
                ("updated_at", "updated_at"),
            ):
                value = item.get(source_key)
                if isinstance(value, (int, str)) and not isinstance(value, bool):
                    candidate[target_key] = value
            candidates.append(candidate)
        meta = response.get("_meta")
        args = meta.get("args") if isinstance(meta, dict) else None
        return {
            "query": request.query,
            "num_found": results.get("num_found"),
            "limit": args.get("limit") if isinstance(args, dict) else request.max_results,
            "offset": args.get("offset") if isinstance(args, dict) else 0,
            "candidates": candidates,
            "selection_required": bool(candidates),
        }, {
            "name": "DBnomics Web API dataset search",
            "url": url,
            "license": "Dataset-specific; consult the original series provider",
        }
    provider = quote(request.provider_code or "", safe="")
    dataset = quote(request.dataset_code or "", safe="")
    series = quote(request.series_code or "", safe="")
    url = f"https://api.db.nomics.world/v22/series/{provider}/{dataset}/{series}?observations=1"
    data = _request_json(url, request.timeout_seconds, deadline)
    return data.get("series", data), {
        "name": "DBnomics Web API",
        "url": f"https://db.nomics.world/{provider}/{dataset}/{series}",
        "license": "Provider-specific; consult the original series provider",
    }


_NIST_CONSTANTS: dict[str, dict[str, str]] = {
    "speed_of_light": {"name": "speed of light in vacuum", "symbol": "c", "value": "299792458", "unit": "m s^-1", "standard_uncertainty": "exact"},
    "planck_constant": {"name": "Planck constant", "symbol": "h", "value": "6.62607015e-34", "unit": "J Hz^-1", "standard_uncertainty": "exact"},
    "elementary_charge": {"name": "elementary charge", "symbol": "e", "value": "1.602176634e-19", "unit": "C", "standard_uncertainty": "exact"},
    "boltzmann_constant": {"name": "Boltzmann constant", "symbol": "k", "value": "1.380649e-23", "unit": "J K^-1", "standard_uncertainty": "exact"},
    "avogadro_constant": {"name": "Avogadro constant", "symbol": "N_A", "value": "6.02214076e23", "unit": "mol^-1", "standard_uncertainty": "exact"},
    "newtonian_constant_of_gravitation": {"name": "Newtonian constant of gravitation", "symbol": "G", "value": "6.67430e-11", "unit": "m^3 kg^-1 s^-2", "standard_uncertainty": "1.5e-15"},
    "fine_structure_constant": {"name": "fine-structure constant", "symbol": "alpha", "value": "7.2973525643e-3", "unit": "1", "standard_uncertainty": "1.1e-12"},
    "electron_mass": {"name": "electron mass", "symbol": "m_e", "value": "9.1093837139e-31", "unit": "kg", "standard_uncertainty": "2.8e-40"},
    "proton_mass": {"name": "proton mass", "symbol": "m_p", "value": "1.67262192595e-27", "unit": "kg", "standard_uncertainty": "5.2e-37"},
}


def supported_nist_constants() -> set[str]:
    """Return the stable keys in the bundled CODATA subset."""
    return set(_NIST_CONSTANTS)


def _nist(request: KnowledgeRequest) -> tuple[object, dict[str, str]]:
    key = re.sub(r"[^a-z0-9]+", "_", (request.constant or "").lower()).strip("_")
    value = _NIST_CONSTANTS.get(key)
    if value is None:
        names = ", ".join(sorted(_NIST_CONSTANTS))
        raise KnowledgeProviderError(f"Constant is not in the bundled CODATA 2022 subset. Available keys: {names}", 404)
    return {**value, "release": "CODATA 2022"}, {
        "name": "NIST CODATA recommended values (2022)",
        "url": "https://physics.nist.gov/constants",
    }


def _geonames(
    request: KnowledgeRequest, deadline: float | None
) -> tuple[object, dict[str, str]]:
    username = os.environ.get("GEONAMES_USERNAME")
    if not username:
        raise KnowledgeProviderError("GEONAMES_USERNAME is not configured", 503)
    params = {
        "q": request.query or "", "maxRows": str(request.max_results), "lang": request.language,
        "username": username, "type": "json",
    }
    data = _request_json(
        f"https://api.geonames.org/searchJSON?{urlencode(params)}", request.timeout_seconds, deadline
    )
    if data.get("status"):
        raise KnowledgeProviderError("GeoNames rejected the request or username", 502)
    return data.get("geonames", []), {
        "name": "GeoNames search API",
        "url": "https://www.geonames.org/",
        "license": "CC BY 4.0",
        "attribution": "GeoNames (https://www.geonames.org/)",
    }


def _open_meteo(
    request: KnowledgeRequest, deadline: float | None
) -> tuple[object, dict[str, str]]:
    latitude, longitude = request.latitude, request.longitude
    location_source = None
    if request.location is not None and latitude is None:
        params = {"name": request.location, "count": "1", "language": request.language, "format": "json"}
        geocoded = _request_json(
            f"https://geocoding-api.open-meteo.com/v1/search?{urlencode(params)}",
            request.timeout_seconds,
            deadline,
        )
        results = geocoded.get("results") or []
        if not results:
            raise KnowledgeProviderError("Open-Meteo could not find that location", 404)
        first = results[0]
        latitude, longitude = float(first["latitude"]), float(first["longitude"])
        location_source = {"name": "Open-Meteo geocoding API", "url": "https://open-meteo.com/"}
    params = {"latitude": str(latitude), "longitude": str(longitude), "timezone": "auto"}
    if request.operation == "weather_forecast":
        params.update({
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
            ),
            "forecast_days": "7",
        })
    else:
        params["current"] = (
            "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
            "precipitation,rain,showers,snowfall,weather_code,wind_speed_10m"
        )
    if request.units == "nonmetric":
        params.update({
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        })
    elif request.units == "metric":
        params.update({
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        })
    data = _request_json(
        f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}",
        request.timeout_seconds,
        deadline,
    )
    source = {
        "name": "Open-Meteo Forecast API",
        "url": "https://open-meteo.com/en/docs",
        "license": "CC BY 4.0",
        "attribution": "Data by Open-Meteo.com (https://open-meteo.com/)",
    }
    if location_source:
        data["resolved_location"] = first
        return data, source | {"geocoding_source": location_source["url"]}
    return data, source


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and perf_counter() >= deadline:
        raise KnowledgeDeadlineExceeded
