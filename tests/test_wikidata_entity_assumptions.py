import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app


class WikidataEntityAssumptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "wikidata-entity-assumption-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer wikidata-entity-assumption-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    @staticmethod
    def _knowledge(matches: list[dict[str, object]]) -> dict[str, object]:
        return {
            "provider": "wikidata",
            "operation": "property",
            "retrieved_at": "2026-10-20T12:00:00+00:00",
            "sources": [{
                "name": "Wikidata API",
                "url": "https://www.wikidata.org/w/api.php",
                "license": "CC0",
                "attribution": "Wikidata contributors",
            }],
            "data": {
                "property_id": "P1082",
                "property": "population",
                "entity_query": "Springfield",
                "matches": matches,
                "selection_rule": "Wikidata candidates retained",
            },
            "evidence": {"kind": "retrieved_source_data", "verified": False},
            "qualification": "Retrieved Wikidata data; not a formal proof.",
        }

    @staticmethod
    def _matches() -> list[dict[str, object]]:
        return [
            {"id": "Q90", "search_rank": 1, "label": "Springfield", "description": "city in the United States", "population": []},
            {"id": "Q99", "search_rank": 2, "label": "Springfield", "description": "fictional city", "population": []},
        ]

    def test_dynamic_selector_scheme_is_published_in_the_assumption_registry(self) -> None:
        response = self.client.get("/v2/assumptions", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        registry = response.json()
        self.assertEqual(registry["registry_version"], "2.8.0")
        entity_scheme = next(
            item for item in registry["dynamic_assumptions"]
            if item["type"] == "WikidataEntity"
        )
        self.assertEqual(entity_scheme["id_pattern"], "entity:wikidata:Q{numeric_id}")
        self.assertEqual(entity_scheme["entry_version"], 1)

    def test_unselected_query_returns_ranked_candidates_and_dynamic_assumption(self) -> None:
        with patch(
            "leanproof_service.full_results.query_knowledge",
            return_value=self._knowledge(self._matches()),
        ):
            response = self.client.get(
                "/v2/query",
                params={"input": "population of Springfield", "output": "json"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        assumption = next(
            item for item in queryresult["assumptions"]
            if item["type"] == "WikidataEntity"
        )
        self.assertIsNone(assumption["selected"])
        self.assertEqual(assumption["selection_source"], "needs_clarification")
        self.assertEqual(
            [item["input"] for item in assumption["values"]],
            ["entity:wikidata:Q90", "entity:wikidata:Q99"],
        )
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Population")
        self.assertEqual(len(result_pod["subpods"][0]["data"]["matches"]), 2)

    def test_caller_selection_filters_to_a_current_candidate(self) -> None:
        with patch(
            "leanproof_service.full_results.query_knowledge",
            return_value=self._knowledge(self._matches()),
        ):
            response = self.client.get(
                "/v2/query",
                params={
                    "input": "population of Springfield",
                    "assumption": "entity:wikidata:Q99",
                    "output": "json",
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        assumption = next(
            item for item in queryresult["assumptions"]
            if item["type"] == "WikidataEntity"
        )
        self.assertEqual(assumption["selected"], "entity:wikidata:Q99")
        self.assertEqual(assumption["selection_source"], "caller_override")
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Population")
        self.assertEqual(
            [item["id"] for item in result_pod["subpods"][0]["data"]["matches"]],
            ["Q99"],
        )

    def test_selection_rejects_an_id_outside_the_current_candidate_set(self) -> None:
        with patch(
            "leanproof_service.full_results.query_knowledge",
            return_value=self._knowledge(self._matches()),
        ):
            response = self.client.get(
                "/v2/query",
                params={
                    "input": "population of Springfield",
                    "assumption": "entity:wikidata:Q404",
                    "output": "json",
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 400)
        message = response.json()["queryresult"]["error_detail"]["msg"]
        self.assertIn("entity:wikidata:Q90", message)
        self.assertIn("entity:wikidata:Q99", message)

    def test_validatequery_recognizes_the_stable_entity_selector(self) -> None:
        response = self.client.get(
            "/v2/validatequery",
            params={
                "input": "population of Springfield",
                "assumption": "entity:wikidata:Q90",
                "output": "json",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["validatequeryresult"]
        assumption = next(
            item for item in result["assumptions"]
            if item["type"] == "WikidataEntity"
        )
        self.assertEqual(assumption["selected"], "entity:wikidata:Q90")
        self.assertEqual(
            assumption["candidate_validation"], "deferred_to_query_execution"
        )

    def test_entity_selector_composes_with_gdp_measure_assumption(self) -> None:
        gdp_knowledge = self._knowledge([{
            "id": "Q142", "search_rank": 1, "label": "France", "description": "country",
            "nominal_gdp": [],
        }])
        gdp_knowledge["data"]["property_id"] = "P2131"  # type: ignore[index]
        with patch(
            "leanproof_service.full_results.query_knowledge",
            return_value=gdp_knowledge,
        ):
            response = self.client.get(
                "/v2/query",
                params=[
                    ("input", "GDP of France"),
                    ("assumption", "gdp:type:nominal"),
                    ("assumption", "entity:wikidata:Q142"),
                    ("output", "json"),
                ],
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        assumptions = response.json()["queryresult"]["assumptions"]
        self.assertEqual(
            {item["type"] for item in assumptions}, {"GDPType", "WikidataEntity"}
        )


if __name__ == "__main__":
    unittest.main()
