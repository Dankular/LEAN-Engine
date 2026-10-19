import unittest

import sympy
from pydantic import ValidationError

from leanproof_service.symbolic import SymbolicRequest, run_symbolic


class CoordinateLaplacianSymbolicTests(unittest.TestCase):
    def _compute(self, expression: str, coordinate_system: str) -> dict[str, object]:
        response = run_symbolic(
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression=expression,
                coordinate_system=coordinate_system,
            )
        )
        self.assertNotIn("error", response, response)
        return response

    def test_cartesian_laplacian_returns_derivatives_and_mathml(self) -> None:
        response = self._compute("x**2 + y**2 + z**2", "Cartesian3D")

        self.assertEqual(response["coordinate_variables"], ["x", "y", "z"])
        self.assertEqual(response["first_derivative_terms"], ["2*x", "2*y", "2*z"])
        self.assertEqual(response["second_derivative_terms"], ["2", "2", "2"])
        self.assertEqual(response["result"], "6")
        self.assertIsInstance(response["result_mathml"], str)
        self.assertEqual(response["domain_conditions"], [])
        self.assertEqual(response["evidence"]["kind"], "coordinate_system_symbolic_result")

    def test_cylindrical_laplacian_uses_radial_and_angular_terms(self) -> None:
        response = self._compute("rho**2 + theta**2 + z**2", "Cylindrical3D")
        rho, theta = sympy.symbols("rho theta", real=True)

        self.assertEqual(response["coordinate_variables"], ["rho", "theta", "z"])
        self.assertEqual(response["first_derivative_terms"], ["2*rho", "2*theta", "2*z"])
        self.assertEqual(response["second_derivative_terms"], ["2", "2", "2"])
        parsed_result = sympy.sympify(
            response["result"], locals={"rho": rho, "theta": theta}
        )
        self.assertEqual(sympy.simplify(parsed_result - (6 + 2 / rho**2)), 0)
        self.assertEqual(response["domain_conditions"], ["rho > 0"])

    def test_spherical_laplacian_uses_radial_correction(self) -> None:
        response = self._compute("r**2", "Spherical3D")

        self.assertEqual(response["coordinate_variables"], ["r", "theta", "phi"])
        self.assertEqual(response["first_derivative_terms"], ["2*r", "0", "0"])
        self.assertEqual(response["second_derivative_terms"], ["2", "0", "0"])
        self.assertEqual(response["result"], "6")
        self.assertEqual(response["domain_conditions"], ["r > 0", "sin(theta) != 0"])

    def test_trigonometric_normalization_does_not_apply_identity_rewrites(self) -> None:
        response = self._compute("sin(x)**2", "Cartesian3D")
        x = sympy.Symbol("x", real=True)
        result = sympy.sympify(response["result"], locals={"x": x})

        self.assertNotEqual(result, -2)
        self.assertTrue(result.has(sympy.sin(x) ** 2))
        self.assertTrue(result.has(sympy.cos(x) ** 2))

    def test_polynomial_degree_and_trigonometric_grammar_are_bounded(self) -> None:
        over_degree = run_symbolic(
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression="x**17",
                coordinate_system="Cartesian3D",
            )
        )
        unsupported_function = run_symbolic(
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression="tan(x)",
                coordinate_system="Cartesian3D",
            )
        )
        nested_calls = "cos(" * 34 + "x" + ")" * 34
        excessive_depth = run_symbolic(
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression=nested_calls,
                coordinate_system="Cartesian3D",
            )
        )

        self.assertIn("degree to 16", over_degree["error"])
        self.assertIn("only sin and cos", unsupported_function["error"])
        self.assertIn("only sin and cos", excessive_depth["error"])

    def test_request_requires_only_a_scalar_and_coordinate_system(self) -> None:
        with self.assertRaises(ValidationError):
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression="x**2",
                coordinate_system="Cartesian3D",
                components=["x", "y", "z"],
            )

        with self.assertRaises(ValidationError):
            SymbolicRequest(
                operation="coordinate_laplacian",
                expression="x**2",
                coordinate_system="Cartesian3D",
                assumptions={"x": ["real"]},
            )


if __name__ == "__main__":
    unittest.main()
