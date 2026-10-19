import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service.api import _freeform_prompt_text, app
from leanproof_service.full_results import _provider_request


class WolframFreeformPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "freeform-prompt-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer freeform-prompt-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_freeform_prompt_spellings_extract_text(self) -> None:
        self.assertEqual(
            _freeform_prompt_text(r'\[FreeformPrompt]["France population"]'),
            "France population",
        )
        self.assertEqual(
            _freeform_prompt_text('FreeformPrompt["France population"]'),
            "France population",
        )
        self.assertEqual(_freeform_prompt_text('= France population'), "France population")
        self.assertIsNone(_freeform_prompt_text("Sin[Pi]"))

    def test_freeform_prompt_rejects_non_string_arguments(self) -> None:
        with self.assertRaisesRegex(ValueError, "one quoted text prompt"):
            _freeform_prompt_text(r"\[FreeformPrompt][France population]")

    def test_documented_population_prompt_returns_source_backed_result(self) -> None:
        query_response = {
            "queryresult": {
                "success": True,
                "error": False,
                "queryid": "query-123",
                "pods": [
                    {
                        "id": "Wikidata",
                        "primary": True,
                        "subpods": [{"plaintext": "67,000,000 people"}],
                    }
                ],
                "evidence": {"kind": "retrieved_source_data", "verified": False},
                "sources": [{"name": "Wikidata", "url": "https://www.wikidata.org/"}],
                "qualification": "Retrieved data is not a formal proof.",
            }
        }
        with patch("leanproof_service.api.execute_query", return_value=query_response) as query:
            response = self.client.post(
                "/api/cag/v1/WolframLanguageCompute",
                json={
                    "code": r'\[FreeformPrompt]["France population"]',
                    "line": 50,
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["result"], "Out[50]= 67,000,000 people")
        self.assertFalse(body["evidence"]["verified"])
        self.assertEqual(body["evidence"]["sources"], query_response["queryresult"]["sources"])
        query.assert_called_once()
        self.assertEqual(query.call_args.args[:3], ("France population", None, ["plaintext"]))
        self.assertIs(query.call_args.kwargs["verify"], True)

    def test_freeform_population_runs_the_local_provider_router(self) -> None:
        knowledge_result = {
            "provider": "wikidata",
            "operation": "property",
            "retrieved_at": "2026-10-19T00:00:00+00:00",
            "sources": [{"name": "Wikidata", "url": "https://www.wikidata.org/"}],
            "data": {
                "property_id": "P1082",
                "matches": [
                    {
                        "id": "Q142",
                        "label": "France",
                        "population": [{"amount": "+68000000"}],
                    }
                ],
            },
            "evidence": {"kind": "retrieved_source_data", "verified": False},
            "qualification": "Retrieved data is not a formal proof.",
        }
        with patch("leanproof_service.full_results.query_knowledge", return_value=knowledge_result):
            response = self.client.post(
                "/api/cag/v1/WolframLanguageCompute",
                json={"code": r'\[FreeformPrompt]["France population"]'},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["result"], "Out[1]= France [Q142]: 68,000,000 people")
        self.assertFalse(body["evidence"]["verified"])
        self.assertEqual(body["evidence"]["formal_verification"]["status"], "unsupported")
        self.assertEqual(body["evidence"]["sources"], knowledge_result["sources"])

    def test_unresolved_freeform_prompt_uses_501_tips(self) -> None:
        query_response = {
            "queryresult": {
                "success": False,
                "error": False,
                "tips": [{"text": "Try 'population of <entity>'."}],
                "assumptions": [],
            }
        }
        with patch("leanproof_service.api.execute_query", return_value=query_response):
            response = self.client.post(
                "/api/cag/v1/WolframLanguageCompute",
                json={"code": r'\[FreeformPrompt]["unknown request"]'},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["result"], "Try 'population of <entity>'.")

    def test_reversed_population_intent_matches_documented_word_order(self) -> None:
        request = _provider_request("France population", None, None, None)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.provider, "wikidata")
        self.assertEqual(request.operation, "property")
        self.assertEqual(request.property_id, "P1082")
        self.assertEqual(request.query, "France")


if __name__ == "__main__":
    unittest.main()
