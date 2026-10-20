import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from leanproof_service.api import app
from leanproof_service.natural_language_query import NaturalLanguageClarification
from leanproof_service.query_language_model import (
    QueryInterpretationResponse,
    generate_query_candidate,
    query_interpreter_configuration,
)


def _chat_response(content: str) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps({
        "choices": [{"message": {"content": content}}]
    }).encode("utf-8")
    manager = MagicMock()
    manager.__enter__.return_value = response
    return manager


class QueryLanguageModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "query-model-contract-token"
        cls.client = TestClient(app)
        cls.headers = {"Authorization": "Bearer query-model-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_disabled_model_does_not_intercept_non_math_requests(self) -> None:
        with patch.dict(os.environ, {
            "QUERY_INTERPRETER_BASE_URL": "",
            "QUERY_INTERPRETER_MODEL": "",
        }, clear=False):
            self.assertFalse(query_interpreter_configuration()["configured"])
            self.assertIsNone(generate_query_candidate("population of France", 1.0))

    def test_model_candidate_is_typed_and_cannot_set_assumptions(self) -> None:
        model_content = json.dumps({
            "status": "candidate",
            "operation": "differentiate",
            "expression": "x^3",
            "variable": "x",
        })
        with (
            patch.dict(os.environ, {
                "QUERY_INTERPRETER_BASE_URL": "http://127.0.0.1:8082/v1",
                "QUERY_INTERPRETER_MODEL": "mock-math-model",
            }, clear=False),
            patch(
                "leanproof_service.query_language_model.urlopen",
                return_value=_chat_response(model_content),
            ) as model_call,
        ):
            candidate = generate_query_candidate(
                "Could you calculate the derivative of x cubed?", 2.0
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.operation, "differentiate")
        self.assertEqual(candidate.expression, "x^3")
        self.assertEqual(candidate.variable, "x")
        self.assertEqual(candidate.candidate_generator, "language_model")
        self.assertEqual(candidate.model, "mock-math-model")
        request = model_call.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8082/v1/chat/completions")

        with self.assertRaises(ValidationError):
            QueryInterpretationResponse.model_validate({
                "status": "candidate",
                "operation": "differentiate",
                "expression": "x^3",
                "variable": "x",
                "assumption": "coordinate:spherical-3d",
            })

    def test_model_must_ask_clarification_when_variable_is_underspecified(self) -> None:
        model_content = json.dumps({
            "status": "clarification",
            "question": "Which variable should I differentiate with respect to?",
        })
        with (
            patch.dict(os.environ, {
                "QUERY_INTERPRETER_BASE_URL": "http://127.0.0.1:8082/v1",
                "QUERY_INTERPRETER_MODEL": "mock-math-model",
            }, clear=False),
            patch(
                "leanproof_service.query_language_model.urlopen",
                return_value=_chat_response(model_content),
            ),
        ):
            with self.assertRaisesRegex(NaturalLanguageClarification, "Which variable"):
                generate_query_candidate("Find the derivative of x*y", 2.0)

    def test_invalid_model_fields_are_rejected_as_clarification(self) -> None:
        model_content = json.dumps({
            "status": "candidate",
            "operation": "differentiate",
            "expression": "x^3",
            "variable": "x",
            "assumptions": ["x > 0"],
        })
        with (
            patch.dict(os.environ, {
                "QUERY_INTERPRETER_BASE_URL": "http://127.0.0.1:8082/v1",
                "QUERY_INTERPRETER_MODEL": "mock-math-model",
            }, clear=False),
            patch(
                "leanproof_service.query_language_model.urlopen",
                return_value=_chat_response(model_content),
            ),
        ):
            with self.assertRaisesRegex(
                NaturalLanguageClarification, "valid typed interpretation"
            ):
                generate_query_candidate("Find the derivative of x cubed", 2.0)

    def test_model_candidate_flows_through_lean_verification(self) -> None:
        model_content = json.dumps({
            "status": "candidate",
            "operation": "differentiate",
            "expression": "x^3",
            "variable": "x",
        })
        with (
            patch.dict(os.environ, {
                "QUERY_INTERPRETER_BASE_URL": "http://127.0.0.1:8082/v1",
                "QUERY_INTERPRETER_MODEL": "mock-math-model",
            }, clear=False),
            patch(
                "leanproof_service.query_language_model.urlopen",
                return_value=_chat_response(model_content),
            ),
        ):
            response = self.client.get(
                "/v2/query",
                params={
                    "input": "Could you calculate the derivative of x cubed?",
                    "verify": "true",
                    "output": "json",
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["queryresult"]
        plan = result["interpretation"]["natural_language_plan"]
        self.assertEqual(plan["candidate_generator"], "language_model")
        self.assertEqual(plan["selection_source"], "model_candidate")
        self.assertEqual(
            plan["semantic_fidelity"], "english_to_formula_correspondence_unverified"
        )
        verification = result["evidence"]["formal_verification"]
        self.assertEqual(verification["status"], "checked", verification)
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["axiom_audit"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
