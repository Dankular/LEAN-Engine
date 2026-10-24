"""Bounded exact matrix queries for Full Results.

Matrices are literal ``{{...},{...}}`` or ``[[...],[...]]`` grids of rational entries
(integers, decimals, or ``a/b`` fractions). Computation uses SymPy's exact matrix
routines; results are not Lean proofs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from sympy import Matrix, N, Rational, Symbol, simplify

from .number_theory import NumberTheoryPod


_MAX_DIMENSION = 6
_MAX_SPECTRAL_DIMENSION = 4
_MAX_POWER = 32
_MAX_ENTRY_CHARS = 80
_ENTRY = re.compile(r"[+-]?\d+(?:\.\d+)?(?:\s*/\s*\d+)?")
_BRACE_MATRIX = re.compile(r"\{\s*\{[^{}]*\}(?:\s*,\s*\{[^{}]*\})*\s*\}")
_BRACKET_MATRIX = re.compile(r"\[\s*\[[^\[\]]*\](?:\s*,\s*\[[^\[\]]*\])*\s*\]")
_PREFIX = re.compile(
    r"^(?:please\s+)?(?:(?:what\s+is|what's|calculate|compute|find|determine|evaluate)\s+)?"
    r"(?:the\s+)?",
    re.IGNORECASE,
)
_UNARY = {
    "determinant": "det", "det": "det",
    "inverse": "inverse", "inv": "inverse", "matrix inverse": "inverse",
    "transpose": "transpose",
    "rank": "rank",
    "trace": "trace", "tr": "trace",
    "eigenvalues": "eigenvalues", "eigenvalue": "eigenvalues",
    "eigenvectors": "eigenvectors", "eigenvector": "eigenvectors",
    "rref": "rref", "row reduce": "rref", "row reduction": "rref",
    "nullspace": "nullspace", "null space": "nullspace",
    "characteristic polynomial": "charpoly", "charpoly": "charpoly",
}


class MatrixAlgebraError(ValueError):
    """A recognized matrix request is undefined or outside the service bounds."""


@dataclass(frozen=True)
class MatrixAlgebraResult:
    operation: str
    arguments: tuple[str, ...]
    input_text: str
    result_text: str
    extra_pods: tuple[NumberTheoryPod, ...] = field(default_factory=tuple)
    kind: str = "matrix_algebra_result"
    scanner: str = "Matrix"


def evaluate_matrix_algebra(source: str) -> MatrixAlgebraResult | None:
    query = source.strip().rstrip("?.! ").strip()
    if not query or len(query) > 2000 or not ("{{" in query.replace(" ", "") or "[[" in query.replace(" ", "")):
        return None
    query = re.sub(r"\s+", " ", query)
    matrices: list[Matrix] = []
    parse_errors: list[str] = []

    def stash(match: re.Match[str]) -> str:
        try:
            matrices.append(_parse_matrix(match.group(0)))
        except MatrixAlgebraError as exc:
            parse_errors.append(str(exc))
            matrices.append(Matrix([[0]]))
        return f"@{len(matrices) - 1}@"

    template = _BRACE_MATRIX.sub(stash, query)
    template = _BRACKET_MATRIX.sub(stash, template)
    if not matrices:
        return None
    body = _PREFIX.sub("", template, count=1)
    plan = _plan(body)
    if plan is None:
        return None
    if parse_errors:
        raise MatrixAlgebraError(parse_errors[0])
    operation, indexes, scalar = plan
    operands = [matrices[index] for index in indexes]
    return _execute(operation, operands, scalar)


def _parse_matrix(text: str) -> Matrix:
    inner = text.strip()[1:-1]
    rows_text = re.findall(r"[{\[]([^{}\[\]]*)[}\]]", inner)
    rows: list[list[Rational]] = []
    for row_text in rows_text:
        entries = [part.strip() for part in row_text.split(",")]
        row = []
        for entry in entries:
            if not _ENTRY.fullmatch(entry) or len(entry) > _MAX_ENTRY_CHARS:
                raise MatrixAlgebraError(
                    "matrix entries must be integers, decimals, or fractions such as 1/2"
                )
            row.append(Rational(entry.replace(" ", "")))
        rows.append(row)
    if not rows or len({len(row) for row in rows}) != 1:
        raise MatrixAlgebraError("matrix rows must all have the same length")
    if len(rows) > _MAX_DIMENSION or len(rows[0]) > _MAX_DIMENSION:
        raise MatrixAlgebraError(f"matrices are limited to {_MAX_DIMENSION} rows and columns")
    return Matrix(rows)


def _plan(body: str) -> tuple[str, list[int], Rational | None] | None:
    unary = re.fullmatch(
        r"(?P<name>[A-Za-z ]+?)\s*(?:\bof\b)?\s*(?:[(\[]\s*)?@(?P<a>\d+)@(?:\s*[)\]])?",
        body,
        re.IGNORECASE,
    )
    if unary:
        name = re.sub(r"\s+", " ", unary.group("name").strip().lower())
        name = re.sub(r"^(?:the|of)\s+", "", name)
        if name in _UNARY:
            return _UNARY[name], [int(unary.group("a"))], None
    power = re.fullmatch(
        r"@(\d+)@\s*(?:\^|\*\*|to the power(?: of)?)\s*\(?(-?\d{1,3})\)?", body, re.IGNORECASE
    )
    if power:
        return f"power:{int(power.group(2))}", [int(power.group(1))], None
    binary = re.fullmatch(
        r"@(\d+)@\s*(\+|-|\*|\.|times|plus|minus)\s*@(\d+)@", body, re.IGNORECASE
    )
    if binary:
        symbol = binary.group(2).lower()
        operation = {"+": "add", "plus": "add", "-": "sub", "minus": "sub"}.get(symbol, "mul")
        return operation, [int(binary.group(1)), int(binary.group(3))], None
    scaled = re.fullmatch(
        rf"({_ENTRY.pattern})\s*\*?\s*@(\d+)@|@(\d+)@\s*\*\s*({_ENTRY.pattern})", body
    )
    if scaled:
        if scaled.group(1) is not None:
            return "scale", [int(scaled.group(2))], Rational(scaled.group(1).replace(" ", ""))
        return "scale", [int(scaled.group(3))], Rational(scaled.group(4).replace(" ", ""))
    return None


def _show(value: object) -> str:
    return re.sub(r"\bI\b", "i", str(value).replace("**", "^"))


def format_matrix(matrix: Matrix) -> str:
    return "{" + ", ".join(
        "{" + ", ".join(_show(matrix[i, j]) for j in range(matrix.cols)) + "}"
        for i in range(matrix.rows)
    ) + "}"


def _grid(matrix: Matrix) -> str:
    cells = [[_show(matrix[i, j]) for j in range(matrix.cols)] for i in range(matrix.rows)]
    widths = [max(len(row[j]) for row in cells) for j in range(matrix.cols)]
    return "\n".join(
        "| " + "  ".join(cell.rjust(widths[j]) for j, cell in enumerate(row)) + " |"
        for row in cells
    )


def _square(matrix: Matrix, name: str) -> None:
    if matrix.rows != matrix.cols:
        raise MatrixAlgebraError(f"{name} requires a square matrix")


def _matrix_result(
    operation: str, arguments: list[Matrix], title: str, result: Matrix
) -> MatrixAlgebraResult:
    return MatrixAlgebraResult(
        operation,
        tuple(format_matrix(item) for item in arguments),
        f"{title} {' and '.join(format_matrix(item) for item in arguments)}",
        format_matrix(result),
        (NumberTheoryPod("MatrixForm", "Matrix form", _grid(result)),),
    )


def _execute(operation: str, operands: list[Matrix], scalar: Rational | None) -> MatrixAlgebraResult:
    first = operands[0]
    if operation.startswith("power:"):
        exponent = int(operation.split(":")[1])
        _square(first, "matrix power")
        if abs(exponent) > _MAX_POWER:
            raise MatrixAlgebraError(f"matrix powers are limited to |n| of at most {_MAX_POWER}")
        if exponent < 0 and first.det() == 0:
            raise MatrixAlgebraError("matrix is singular, so it has no inverse")
        return _matrix_result("power", operands, f"matrix power {exponent} of", first**exponent)
    if operation == "add" or operation == "sub":
        second = operands[1]
        if first.shape != second.shape:
            raise MatrixAlgebraError("matrix sum and difference require equal dimensions")
        label = "sum of" if operation == "add" else "difference of"
        return _matrix_result(operation, operands, label, first + second if operation == "add" else first - second)
    if operation == "mul":
        second = operands[1]
        if first.cols != second.rows:
            raise MatrixAlgebraError(
                "matrix product requires columns of the first to equal rows of the second"
            )
        return _matrix_result("mul", operands, "product of", first * second)
    if operation == "scale":
        assert scalar is not None
        return _matrix_result("scale", operands, f"{scalar} times", first * scalar)
    text = format_matrix(first)
    if operation == "det":
        _square(first, "determinant")
        return MatrixAlgebraResult("det", (text,), f"determinant of {text}", str(first.det()))
    if operation == "inverse":
        _square(first, "inverse")
        if first.det() == 0:
            raise MatrixAlgebraError("matrix is singular, so it has no inverse")
        return _matrix_result("inverse", operands, "inverse of", first.inv())
    if operation == "transpose":
        return _matrix_result("transpose", operands, "transpose of", first.T)
    if operation == "rank":
        return MatrixAlgebraResult("rank", (text,), f"rank of {text}", str(first.rank()))
    if operation == "trace":
        _square(first, "trace")
        return MatrixAlgebraResult("trace", (text,), f"trace of {text}", str(first.trace()))
    if operation == "rref":
        return _matrix_result("rref", operands, "row reduction of", first.rref()[0])
    if operation == "nullspace":
        basis = first.nullspace()
        shown = "{" + ", ".join(format_matrix(vector.T)[1:-1] for vector in basis) + "}" if basis else "{}"
        return MatrixAlgebraResult(
            "nullspace", (text,), f"null space of {text}", shown
        )
    if operation == "charpoly":
        _square(first, "characteristic polynomial")
        x = Symbol("x")
        return MatrixAlgebraResult(
            "charpoly", (text,), f"characteristic polynomial of {text}",
            _show(first.charpoly(x).as_expr()),
        )
    if operation in {"eigenvalues", "eigenvectors"}:
        _square(first, operation)
        if first.rows > _MAX_SPECTRAL_DIMENSION:
            raise MatrixAlgebraError(
                f"eigen computations are limited to {_MAX_SPECTRAL_DIMENSION}x"
                f"{_MAX_SPECTRAL_DIMENSION} matrices"
            )
        if operation == "eigenvalues":
            values = [value for value, multiplicity in first.eigenvals().items() for _ in range(multiplicity)]
            return MatrixAlgebraResult(
                "eigenvalues", (text,), f"eigenvalues of {text}",
                "{" + ", ".join(_show(simplify(v)) for v in _by_magnitude(values)) + "}",
            )
        lines = []
        for value, multiplicity, vectors in first.eigenvects():
            shown = ", ".join(format_matrix(vector.T)[1:-1] for vector in vectors)
            lines.append(f"λ = {_show(simplify(value))} (multiplicity {multiplicity}): {shown}")
        return MatrixAlgebraResult(
            "eigenvectors", (text,), f"eigenvectors of {text}", "\n".join(lines)
        )
    raise MatrixAlgebraError("unsupported matrix operation")


def _by_magnitude(values: list) -> list:
    try:
        return sorted(values, key=lambda v: (-float(abs(N(v))), str(v)))
    except (TypeError, ValueError):
        return values
