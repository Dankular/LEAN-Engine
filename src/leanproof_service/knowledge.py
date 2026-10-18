from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import Field, model_validator

from .models import StrictModel


class KnowledgeRequest(StrictModel):
    provider: Literal["wikidata", "dbnomics", "nist_codata", "geonames", "open_meteo"]
    operation: Literal["search", "entity", "series", "constant", "weather"]
    query: str | None = Field(default=None, min_length=1, max_length=400)
    entity_id: str | None = Field(default=None, pattern=r"^[QP][1-9][0-9]*$")
    provider_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    dataset_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    series_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    constant: str | None = Field(default=None, min_length=1, max_length=80)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")
    max_results: int = Field(default=5, ge=1, le=10)
    timeout_seconds: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_provider_operation(self) -> KnowledgeRequest:
        expected = {
            "wikidata": {"search", "entity"},
            "dbnomics": {"series"},
            "nist_codata": {"constant"},
            "geonames": {"search"},
            "open_meteo": {"weather"},
        }
        if self.operation not in expected[self.provider]:
            raise ValueError(f"{self.operation} is not supported by {self.provider}")
        if self.provider == "wikidata":
            if self.operation == "search" and self.query is None:
                raise ValueError("Wikidata search requires query")
            if self.operation == "entity" and self.entity_id is None:
                raise ValueError("Wikidata entity lookup requires entity_id")
        elif self.provider == "dbnomics":
            if not all((self.provider_code, self.dataset_code, self.series_code)):
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
        return self


class KnowledgeProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def query_knowledge(request: KnowledgeRequest) -> dict[str, object]:
    try:
        if request.provider == "wikidata":
            data, source = _wikidata(request)
        elif request.provider == "dbnomics":
            data, source = _dbnomics(request)
        elif request.provider == "nist_codata":
            data, source = _nist(request)
        elif request.provider == "geonames":
            data, source = _geonames(request)
        else:
            data, source = _open_meteo(request)
    except KnowledgeProviderError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, HTTPError):
            code = 502 if exc.code < 500 else 503
            raise KnowledgeProviderError(f"{request.provider} returned HTTP {exc.code}", code) from exc
        raise KnowledgeProviderError(f"Could not retrieve data from {request.provider}") from exc

    return {
        "provider": request.provider,
        "operation": request.operation,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sources": [source],
        "data": data,
        "evidence": {"kind": "retrieved_source_data", "verified": False},
        "qualification": "Source data is returned with provenance; retrieval does not establish a formal proof.",
    }


def _request_json(url: str, timeout: int) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": os.environ.get(
                "DATA_PROVIDER_USER_AGENT", "LeanProofService/0.1 (source-aware computation API)"
            ),
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise KnowledgeProviderError("Provider response exceeded the 2 MB limit", 502)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise KnowledgeProviderError("Provider returned an unexpected JSON shape", 502)
    return value


def _wikidata(request: KnowledgeRequest) -> tuple[object, dict[str, str]]:
    base = "https://www.wikidata.org/w/api.php"
    if request.operation == "search":
        params = {
            "action": "wbsearchentities", "search": request.query or "", "language": request.language,
            "format": "json", "type": "item", "limit": str(request.max_results),
        }
        data = _request_json(f"{base}?{urlencode(params)}", request.timeout_seconds)
        results = data.get("search", [])
        return results, {
            "name": "Wikidata entity search", "url": "https://www.wikidata.org/", "license": "CC0"
        }
    params = {
        "action": "wbgetentities", "ids": request.entity_id or "", "props": "labels|descriptions|claims",
        "languages": request.language, "format": "json",
    }
    data = _request_json(f"{base}?{urlencode(params)}", request.timeout_seconds)
    entities = data.get("entities", {})
    entity = entities.get(request.entity_id or "") if isinstance(entities, dict) else None
    return entity, {
        "name": "Wikidata entity API", "url": "https://www.wikidata.org/", "license": "CC0"
    }


def _dbnomics(request: KnowledgeRequest) -> tuple[object, dict[str, str]]:
    provider = quote(request.provider_code or "", safe="")
    dataset = quote(request.dataset_code or "", safe="")
    series = quote(request.series_code or "", safe="")
    url = f"https://api.db.nomics.world/v22/series/{provider}/{dataset}/{series}?observations=1"
    data = _request_json(url, request.timeout_seconds)
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


def _geonames(request: KnowledgeRequest) -> tuple[object, dict[str, str]]:
    username = os.environ.get("GEONAMES_USERNAME")
    if not username:
        raise KnowledgeProviderError("GEONAMES_USERNAME is not configured", 503)
    params = {
        "q": request.query or "", "maxRows": str(request.max_results), "lang": request.language,
        "username": username, "type": "json",
    }
    data = _request_json(f"https://api.geonames.org/searchJSON?{urlencode(params)}", request.timeout_seconds)
    if data.get("status"):
        raise KnowledgeProviderError("GeoNames rejected the request or username", 502)
    return data.get("geonames", []), {
        "name": "GeoNames search API",
        "url": "https://www.geonames.org/",
        "license": "CC BY 4.0",
        "attribution": "GeoNames (https://www.geonames.org/)",
    }


def _open_meteo(request: KnowledgeRequest) -> tuple[object, dict[str, str]]:
    latitude, longitude = request.latitude, request.longitude
    location_source = None
    if request.location is not None and latitude is None:
        params = {"name": request.location, "count": "1", "language": request.language, "format": "json"}
        geocoded = _request_json(
            f"https://geocoding-api.open-meteo.com/v1/search?{urlencode(params)}", request.timeout_seconds
        )
        results = geocoded.get("results") or []
        if not results:
            raise KnowledgeProviderError("Open-Meteo could not find that location", 404)
        first = results[0]
        latitude, longitude = float(first["latitude"]), float(first["longitude"])
        location_source = {"name": "Open-Meteo geocoding API", "url": "https://open-meteo.com/"}
    params = {
        "latitude": str(latitude), "longitude": str(longitude),
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,rain,showers,snowfall,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    data = _request_json(f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}", request.timeout_seconds)
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
