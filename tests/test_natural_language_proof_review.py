import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from leanproof_service import api
from leanproof_service.natural_language import normalize_lean_source


PROBLEM = "For every real x, x + 0 = x."
MODEL_SOURCE = normalize_lean_source(
    """```lean
import Mathlib

theorem identity_candidate (x : ℝ) : x + 0 = x := by
  ring
```"""
)


class NaturalLanguageProofReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_token = os.environ.get("API_BEARER_TOKEN")
        os.environ["API_BEARER_TOKEN"] = "nl-review-contract-token"
        cls.client = TestClient(api.app)
        cls.headers = {"Authorization": "Bearer nl-review-contract-token"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        if cls.original_token is None:
            os.environ.pop("API_BEARER_TOKEN", None)
        else:
            os.environ["API_BEARER_TOKEN"] = cls.original_token

    def test_first_request_returns_reviewable_draft_without_running_lean(self) -> None:
        with (
            patch.object(api, "generate_lean", return_value=("mock-model", MODEL_SOURCE)),
            patch.object(api, "run_generated_source") as run_lean,
        ):
            response = self.client.post(
                "/v1/natural-language/prove",
                json={"problem": PROBLEM},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outcome"], "needs_review")
        self.assertEqual(payload["theorem_status"], "not_submitted_to_lean")
        self.assertEqual(payload["formalization_fidelity"]["status"], "awaiting_caller_review")
        self.assertIn("theorem llm_natural_language_proof_", payload["formalization"]["lean_source"])
        self.assertEqual(len(payload["formalization"]["sha256"]), 64)
        run_lean.assert_not_called()

    def test_approved_draft_is_checked_and_reports_review_boundary(self) -> None:
        with (
            patch.object(api, "generate_lean", return_value=("mock-model", MODEL_SOURCE)),
            patch.object(
                api,
                "run_generated_source",
                return_value=SimpleNamespace(
                    status="checked",
                    source=MODEL_SOURCE,
                    diagnostics=None,
                    axioms=("propext",),
                ),
            ) as run_lean,
        ):
            draft_response = self.client.post(
                "/v1/natural-language/prove",
                json={"problem": PROBLEM},
                headers=self.headers,
            )
            draft = draft_response.json()
            approved_response = self.client.post(
                "/v1/natural-language/prove",
                json={
                    "problem": PROBLEM,
                    "draft_id": draft["formalization"]["draft_id"],
                    "formalization_sha256": draft["formalization"]["sha256"],
                    "approve_formalization": True,
                },
                headers=self.headers,
            )
            approved = approved_response.json()

        self.assertEqual(draft_response.status_code, 200)
        self.assertEqual(approved_response.status_code, 200)
        self.assertEqual(approved["outcome"], "proof_checked")
        self.assertEqual(
            approved["formalization_fidelity"]["status"],
            "caller_approved_but_not_mechanically_verified",
        )
        self.assertEqual(approved["formalization"]["approval"], "caller_approved")
        run_lean.assert_called_once()

    def test_changed_digest_cannot_be_approved(self) -> None:
        with patch.object(api, "generate_lean", return_value=("mock-model", MODEL_SOURCE)):
            draft_response = self.client.post(
                "/v1/natural-language/prove",
                json={"problem": PROBLEM},
                headers=self.headers,
            )
        draft = draft_response.json()

        with patch.object(api, "run_generated_source") as run_lean:
            response = self.client.post(
                "/v1/natural-language/prove",
                json={
                    "problem": PROBLEM,
                    "draft_id": draft["formalization"]["draft_id"],
                    "formalization_sha256": "0" * 64,
                    "approve_formalization": True,
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 409)
        run_lean.assert_not_called()

    def test_approval_requires_explicit_opt_in(self) -> None:
        with patch.object(api, "generate_lean", return_value=("mock-model", MODEL_SOURCE)):
            draft_response = self.client.post(
                "/v1/natural-language/prove",
                json={"problem": PROBLEM},
                headers=self.headers,
            )
        draft = draft_response.json()

        with patch.object(api, "run_generated_source") as run_lean:
            response = self.client.post(
                "/v1/natural-language/prove",
                json={
                    "problem": PROBLEM,
                    "draft_id": draft["formalization"]["draft_id"],
                    "formalization_sha256": draft["formalization"]["sha256"],
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 422)
        run_lean.assert_not_called()


if __name__ == "__main__":
    unittest.main()
