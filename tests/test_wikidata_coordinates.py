import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import app
from leanproof_service.full_results import _provider_request
from leanproof_service.knowledge import KnowledgeRequest, ProviderResponse, query_knowledge


class WikidataCoordinateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "wikidata-coordinate-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer wikidata-coordinate-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_coordinate_phrases_route_to_allowlisted_property(self) -> None:
        for phrase in (
            "coordinates of Paris",
            "GPS coordinates for Paris",
            "geographic coordinates of Paris",
            "coordinate location in Paris",
        ):
            with self.subTest(phrase=phrase):
                request = _provider_request(phrase, None, None, None)
                self.assertIsNotNone(request)
                self.assertEqual(request.property_id, "P625")
                self.assertEqual(request.query, "Paris")

        self.assertEqual(
            KnowledgeRequest(
                provider="wikidata",
                operation="property",
                property_id="P625",
                query="Paris",
            ).property_id,
            "P625",
        )

    def test_adapter_preserves_coordinate_metadata_and_rejects_invalid_values(self) -> None:
        cache_metadata = {
            "status": "miss",
            "source_retrieved_at": "2026-10-19T12:00:00+00:00",
        }
        responses = [
            ProviderResponse({"search": [{"id": "Q90", "label": "Paris"}]}, cache_metadata),
            ProviderResponse({
                "entities": {
                    "Q90": {
                        "labels": {"en": {"value": "Paris"}},
                        "claims": {
                            "P625": [
                                {
                                    "id": "Q90$coordinate",
                                    "rank": "preferred",
                                    "mainsnak": {
                                        "snaktype": "value",
                                        "datavalue": {"value": {
                                            "latitude": 48.8566,
                                            "longitude": 2.3522,
                                            "precision": 0.0001,
                                            "globe": "http://www.wikidata.org/entity/Q2",
                                        }},
                                    },
                                    "references": [{}, {}],
                                    "qualifiers": {
                                        "P1480": [{
                                            "snaktype": "value",
                                            "datavalue": {"value": "circa"},
                                        }],
                                    },
                                },
                                {
                                    "rank": "normal",
                                    "mainsnak": {
                                        "snaktype": "value",
                                        "datavalue": {"value": {
                                            "latitude": 91,
                                            "longitude": 2,
                                            "precision": 0.1,
                                            "globe": "http://www.wikidata.org/entity/Q2",
                                        }},
                                    },
                                },
                            ],
                        },
                    },
                },
            }, cache_metadata),
        ]
        request = KnowledgeRequest(
            provider="wikidata",
            operation="property",
            property_id="P625",
            query="Paris",
        )

        with patch("leanproof_service.knowledge._request_json", side_effect=responses) as fetch:
            result = query_knowledge(request)

        self.assertEqual(fetch.call_count, 2)
        value = result["data"]["matches"][0]["coordinates"][0]
        self.assertEqual(value["latitude"], 48.8566)
        self.assertEqual(value["longitude"], 2.3522)
        self.assertEqual(value["precision"], 0.0001)
        self.assertEqual(value["globe_id"], "Q2")
        self.assertEqual(value["rank"], "preferred")
        self.assertEqual(value["reference_count"], 2)
        self.assertEqual(value["statement_id"], "Q90$coordinate")
        self.assertIn({"property_id": "P1480", "value": "circa"}, value["qualifiers"])
        self.assertIn("latitude, longitude, precision, globe", result["data"]["selection_rule"])

    def test_full_results_renders_coordinates_with_globe_and_precision(self) -> None:
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
                "property_id": "P625",
                "property": "coordinate location",
                "entity_query": "Paris",
                "matches": [{
                    "id": "Q90",
                    "search_rank": 1,
                    "label": "Paris",
                    "coordinates": [{
                        "latitude": 48.8566,
                        "longitude": 2.3522,
                        "precision": 0.0001,
                        "globe": "http://www.wikidata.org/entity/Q2",
                        "globe_id": "Q2",
                    }],
                }],
                "selection_rule": "Wikidata candidates retained",
            },
            "evidence": {"kind": "retrieved_source_data", "verified": False},
            "qualification": "Retrieved Wikidata data; not a formal proof.",
        }
        with patch("leanproof_service.full_results.query_knowledge", return_value=knowledge):
            response = self.client.get(
                "/v2/query",
                params={"input": "coordinates of Paris", "output": "json"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        queryresult = response.json()["queryresult"]
        result_pod = next(pod for pod in queryresult["pods"] if pod["id"] == "Coordinates")
        result_text = result_pod["subpods"][0]["plaintext"]
        self.assertIn("Paris [Q90]", result_text)
        self.assertIn("48.8566, 2.3522", result_text)
        self.assertIn("precision 0.0001", result_text)
        self.assertIn("globe Q2", result_text)
        self.assertIs(queryresult["evidence"]["verified"], False)


if __name__ == "__main__":
    unittest.main()
