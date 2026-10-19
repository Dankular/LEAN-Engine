import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.full_results import _provider_request
from leanproof_service.knowledge import KnowledgeRequest, ProviderResponse, query_knowledge


class WikidataAreaElevationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "wikidata-measure-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer wikidata-measure-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_area_and_elevation_phrases_resolve_to_allowlisted_properties(self) -> None:
        area = _provider_request("land area of France", None, None, None)
        elevation = _provider_request("elevation above sea level of Mount Everest", None, None, None)

        self.assertIsNotNone(area)
        self.assertEqual(area.property_id, "P2046")
        self.assertEqual(area.query, "France")
        self.assertIsNotNone(elevation)
        self.assertEqual(elevation.property_id, "P2044")
        self.assertEqual(elevation.query, "Mount Everest")
        altitude = _provider_request("altitude of Mount Everest", None, None, None)
        self.assertIsNotNone(altitude)
        self.assertEqual(altitude.property_id, "P2044")
        self.assertEqual(altitude.query, "Mount Everest")
        self.assertEqual(
            KnowledgeRequest(
                provider="wikidata",
                operation="property",
                property_id="P2046",
                query="France",
            ).property_id,
            "P2046",
        )
        self.assertEqual(
            KnowledgeRequest(
                provider="wikidata",
                operation="property",
                property_id="P2044",
                query="Mount Everest",
            ).property_id,
            "P2044",
        )

    def test_full_results_renders_ranked_area_statement_with_unit(self) -> None:
        knowledge = {
            "provider": "wikidata",
            "operation": "property",
            "retrieved_at": "2026-10-19T12:00:00+00:00",
            "sources": [{
                "name": "Wikidata API",
                "url": "https://www.wikidata.org/w/api.php",
                "license": "CC0",
                "attribution": "Wikidata contributors",
            }],
            "data": {
                "property_id": "P2046",
                "property": "area",
                "entity_query": "France",
                "matches": [{
                    "id": "Q142",
                    "search_rank": 1,
                    "label": "France",
                    "area": [{
                        "amount": "551695",
                        "unit_id": "Q712226",
                        "unit_label": "square kilometre",
                        "rank": "preferred",
                        "reference_count": 1,
                    }],
                }],
                "selection_rule": "preferred statement, preserving unit and provenance",
            },
            "evidence": {"kind": "retrieved_source_data", "verified": False},
            "qualification": "Retrieved Wikidata data; not a formal proof.",
        }
        with patch("leanproof_service.full_results.query_knowledge", return_value=knowledge):
            response = self.client.get(
                "/v2/query",
                params={"input": "land area of France", "output": "json"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Area")
        result_text = result_pod["subpods"][0]["plaintext"]
        self.assertIn("France [Q142]", result_text)
        self.assertIn("551,695 square kilometre", result_text)
        self.assertEqual(queryresult["sources"][0]["attribution"], "Wikidata contributors")
        self.assertIs(queryresult["evidence"]["verified"], False)
        self.assertEqual(queryresult["qualification"], "Wikidata's ranked entity matches are returned as candidates. Area statements are retrieved data, not a formal proof.")

    def test_wikidata_quantity_adapter_preserves_unit_bounds_and_qualifiers(self) -> None:
        cache_metadata = {
            "status": "miss",
            "source_retrieved_at": "2026-10-19T12:00:00+00:00",
        }
        responses = [
            ProviderResponse({"search": [{"id": "Q142", "label": "France"}]}, cache_metadata),
            ProviderResponse({
                "entities": {
                    "Q142": {
                        "labels": {"en": {"value": "France"}},
                        "claims": {
                            "P2046": [{
                                "id": "Q142$area",
                                "rank": "preferred",
                                "mainsnak": {
                                    "snaktype": "value",
                                    "datavalue": {"value": {
                                        "amount": "+551695",
                                        "unit": "http://www.wikidata.org/entity/Q712226",
                                        "lowerBound": "551694",
                                        "upperBound": "551696",
                                    }},
                                },
                                "references": [{}],
                                "qualifiers": {
                                    "P585": [{
                                        "datavalue": {"value": {
                                            "time": "+2020-01-01T00:00:00Z",
                                        }},
                                    }],
                                    "P518": [{
                                        "snaktype": "value",
                                        "datavalue": {
                                            "type": "wikibase-entityid",
                                            "value": {
                                                "entity-type": "item",
                                                "numeric-id": 2468,
                                                "id": "Q2468",
                                            },
                                        },
                                    }],
                                },
                            }],
                        },
                    },
                },
            }, cache_metadata),
            ProviderResponse({
                "entities": {
                    "Q712226": {"labels": {"en": {"value": "square kilometre"}}},
                },
            }, cache_metadata),
        ]
        request = KnowledgeRequest(
            provider="wikidata",
            operation="property",
            property_id="P2046",
            query="France",
        )

        with patch("leanproof_service.knowledge._request_json", side_effect=responses):
            result = query_knowledge(request)

        value = result["data"]["matches"][0]["area"][0]
        self.assertEqual(value["amount"], "551695")
        self.assertEqual(value["lowerBound"], "551694")
        self.assertEqual(value["upperBound"], "551696")
        self.assertEqual(value["unit_label"], "square kilometre")
        self.assertEqual(value["rank"], "preferred")
        self.assertEqual(value["reference_count"], 1)
        self.assertEqual(value["point_in_time"], "+2020-01-01T00:00:00Z")
        qualifiers = value["qualifiers"]
        self.assertIn(
            {
                "property_id": "P518",
                "type": "wikibase-entityid",
                "value": {"entity-type": "item", "numeric-id": 2468, "id": "Q2468"},
            },
            qualifiers,
        )
        self.assertIn("preferred quantity statements", result["data"]["selection_rule"])


if __name__ == "__main__":
    unittest.main()
