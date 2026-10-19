import unittest

from leanproof_service.api import _format_wolfram_output


class WolframComputeOutputContractTests(unittest.TestCase):
    def test_integer_output_omits_value_separator_space(self) -> None:
        self.assertEqual(_format_wolfram_output(1, "0"), "Out[1]=0")
        self.assertEqual(_format_wolfram_output(50, "-12"), "Out[50]=-12")

    def test_non_integer_output_uses_value_separator_space(self) -> None:
        self.assertEqual(_format_wolfram_output(50, "1/2"), "Out[50]= 1/2")
        self.assertEqual(_format_wolfram_output(1, "x + 1"), "Out[1]= x + 1")


if __name__ == "__main__":
    unittest.main()
