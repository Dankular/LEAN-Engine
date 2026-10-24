"""Bounded natural-language templates for symbolic Full Results queries."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re

from .input_syntax import MathInputError, parse_math_input


_IDENTIFIER = r"[A-Za-z][A-Za-z0-9_]{0,31}"
_FUNCTIONS = {
    "sin", "cos", "tan", "asin", "acos", "atan", "arcsin", "arccos", "arctan",
    "atanh", "arctanh", "artanh",
    "asinh", "arcsinh", "acosh", "arccosh",
    "sinh", "cosh", "tanh", "coth", "sech", "csch", "sec", "csc", "cot", "exp", "log", "ln", "sqrt", "root",
    "abs",
    "floor", "ceiling",
}
_DEFAULT_SERIES_ORDER = 6
_CONSTANTS = {"pi", "E", "I", "oo", "e"}
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}


@dataclass(frozen=True)
class NaturalLanguageOperation:
    operation: str
    expression: str
    rule_id: str
    components: tuple[str, ...] | None = None
    coordinate_system: str | None = None
    rhs: str | None = None
    variable: str | None = None
    variable_source: str | None = None
    lower: str | None = None
    upper: str | None = None
    point: str | None = None
    order: int | None = None
    variable_candidates: tuple[str, ...] = ()
    equations: tuple[str, ...] | None = None
    variables: tuple[str, ...] = ()
    candidate_generator: str = "deterministic_template"
    model: str | None = None


class NaturalLanguageClarification(ValueError):
    """A recognized operation needs an explicit interpretation from the caller."""

    def __init__(
        self,
        message: str,
        *,
        candidate_variables: tuple[str, ...] = (),
        operation: str | None = None,
        expression: str | None = None,
    ) -> None:
        super().__init__(message)
        self.candidate_variables = candidate_variables
        self.operation = operation
        self.expression = expression


def plan_natural_language_operation(
    source: str,
    *,
    variable_assumption: str | None = None,
) -> NaturalLanguageOperation | None:
    """Parse a small, explicit set of English math instructions.

    This is a deterministic candidate generator, not a general language model. Every
    formula produced here is passed through the service's bounded math parser before
    execution. No assumptions are inferred or selected by this layer.
    """
    query = source.strip().rstrip("?.! ").strip()
    if not query or len(query) > 4000:
        return None

    coordinate = re.fullmatch(
        r"(?:(?:please\s+)?(?:(?:find|compute|calculate|determine)\s+)?|what\s+is\s+)"
        r"(?:the\s+)?(?P<operator>gradient|grad|divergence|div|curl|laplacian|lap)\s+"
        r"(?:(?:of|for)\s+)?(?P<body>.+)",
        query,
        re.IGNORECASE,
    )
    if coordinate:
        operator = coordinate.group("operator").lower()
        body, coordinate_system = _split_coordinate_chart(coordinate.group("body"))
        if operator in {"laplacian", "lap"}:
            expression = _math_phrase(_strip_coordinate_field_prefix(body))
            if not expression or expression.casefold() in {"of", "for"}:
                raise NaturalLanguageClarification(
                    "Give the scalar field for the Laplacian, for example "
                    "'find the Laplacian of x^2 + y^2 + z^2'."
                )
            return NaturalLanguageOperation(
                operation="coordinate_laplacian",
                expression=expression,
                coordinate_system=coordinate_system,
                rule_id="coordinate_laplacian_scalar",
            )
        if operator in {"gradient", "grad"}:
            expression = _math_phrase(_strip_coordinate_field_prefix(body))
            if not expression or expression.casefold() in {"of", "for"}:
                raise NaturalLanguageClarification(
                    "Give the scalar field for the gradient, for example "
                    "'find the gradient of x^2 + y^2'."
                )
            return NaturalLanguageOperation(
                operation="coordinate_gradient",
                expression=expression,
                coordinate_system=coordinate_system,
                rule_id="coordinate_gradient_field",
            )

        components = _split_coordinate_components(body)
        normalized_components = tuple(_math_phrase(item) for item in components)
        if operator in {"divergence", "div"}:
            return NaturalLanguageOperation(
                operation="coordinate_divergence",
                expression=", ".join(normalized_components),
                components=normalized_components,
                coordinate_system=coordinate_system,
                rule_id="coordinate_divergence_components",
            )
        return NaturalLanguageOperation(
            operation="coordinate_curl",
            expression=", ".join(normalized_components),
            components=normalized_components,
            coordinate_system=coordinate_system,
            rule_id="coordinate_curl_components",
        )

    incomplete_laplacian = re.fullmatch(
        r"(?:(?:please\s+)?(?:(?:find|compute|calculate|determine)\s+)?|what\s+is\s+)"
        r"(?:the\s+)?(?:laplacian|lap)(?:\s+(?:of|for))?\s*",
        query,
        re.IGNORECASE,
    )
    if incomplete_laplacian:
        raise NaturalLanguageClarification(
            "Give the scalar field for the Laplacian, for example "
            "'find the Laplacian of x^2 + y^2 + z^2'."
        )

    match = re.fullmatch(
        r"(?:please\s+)?(simplify|factor|expand)\s+"
        r"(?:(?:the\s+)?(?:expression|formula)\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if match:
        operation = match.group(1).lower()
        return NaturalLanguageOperation(
            operation=operation,
            expression=_math_phrase(match.group(2)),
            rule_id=f"{operation}_expression",
        )

    identity = re.fullmatch(
        r"(?:please\s+)?(?:prove|show|verify|check)\s+(?:that\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if identity:
        equation = _math_phrase(identity.group(1))
        lhs, rhs = _split_equation(equation)
        if rhs is not None:
            return NaturalLanguageOperation(
                operation="identity",
                expression=lhs,
                rhs=rhs,
                rule_id="prove_identity",
            )

    derivative = re.fullmatch(
        r"(?:(?:what\s+is|find|calculate|compute)\s+)?(?:the\s+)?"
        r"(?:derivative\s+of|differentiate)\s+"
        r"(?:(?:the\s+)?(?:expression|formula)\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if derivative:
        expression, variable = _split_variable_clause(derivative.group(1))
        expression = _math_phrase(expression)
        variable, variable_source, variable_candidates = _choose_variable(
            expression, variable, "differentiate", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="differentiate",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            rule_id="derivative_expression",
        )

    integration = re.fullmatch(
        r"(?:(?:what\s+is|find|calculate|compute)\s+)?(?:the\s+)?"
        r"(?:integral\s+of|integrate)\s+(.+)",
        query,
        re.IGNORECASE,
    )
    if integration:
        expression, variable, lower, upper = _split_integral(integration.group(1))
        expression = _math_phrase(expression)
        variable, variable_source, variable_candidates = _choose_variable(
            expression, variable, "integrate", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="integrate",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            lower=_math_phrase(lower) if lower is not None else None,
            upper=_math_phrase(upper) if upper is not None else None,
            rule_id="integral_expression",
        )

    apart = re.fullmatch(
        r"(?:(?:please\s+)?(?:find|compute|calculate|give|show)\s+)?(?:the\s+)?"
        r"(?:partial\s+fractions?(?:\s+(?:decomposition|expansion))?|apart)\s+(?:of\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if apart:
        body, variable = _split_variable_clause(apart.group(1))
        expression = _math_phrase(body)
        variable, variable_source, variable_candidates = _choose_variable(
            expression, variable, "apart", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="apart",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            rule_id="partial_fractions",
        )

    system = re.fullmatch(
        r"(?:(?:please\s+)?solve|find\s+the\s+solutions\s+of)\s+(?:the\s+system\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if system:
        planned = _plan_equation_system(system.group(1))
        if planned is not None:
            return planned

    solve = re.fullmatch(
        r"(?:(?:please\s+)?solve|find\s+the\s+(?:roots|solutions)\s+of|"
        r"what\s+are\s+the\s+(?:roots|solutions)\s+of)\s+(.+)",
        query,
        re.IGNORECASE,
    )
    if solve:
        equation, variable = _split_variable_clause(solve.group(1), prepositions=("for", "with respect to", "wrt", "w.r.t."))
        equation = re.sub(r"\bequals\b", "=", equation, flags=re.IGNORECASE)
        left, right = _split_equation(equation)
        normalized_left = _math_phrase(left)
        normalized_right = _math_phrase(right) if right is not None else "0"
        expression = f"({normalized_left})-({normalized_right})"
        variable, variable_source, variable_candidates = _choose_variable(
            expression, variable, "solve", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="solve",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            rule_id="solve_equation",
        )

    limit = re.fullmatch(
        rf"(?:(?:what\s+is|find)\s+)?(?:the\s+)?limit\s+(?:of\s+)?(.+?)\s+"
        rf"as\s+({_IDENTIFIER})\s+(?:approaches|approach|tends\s+to|goes\s+to|->)\s+(.+)",
        query,
        re.IGNORECASE,
    )
    if limit:
        expression = _math_phrase(limit.group(1))
        variable, variable_source, variable_candidates = _choose_variable(
            expression, limit.group(2), "limit", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="limit",
            expression=expression,
            variable=variable,
            point=_math_phrase(limit.group(3)),
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            rule_id="limit_approaches_point",
        )

    series = re.fullmatch(
        rf"(?:(?:find|compute|calculate|give)\s+)?(?:the\s+)?(?:Taylor\s+|Maclaurin\s+)?"
        rf"series(?:\s+(?:expansion|representation))?\s+(?:of\s+)?(.+?)"
        rf"(?:\s+(?:around|about|near|at)\s+(?:({_IDENTIFIER})\s*=\s*)?(.+?))?"
        rf"(?:\s+(?:to\s+)?order\s+(\d+))?",
        query,
        re.IGNORECASE,
    )
    if series:
        expression = _math_phrase(series.group(1))
        variable, variable_source, variable_candidates = _choose_variable(
            expression, series.group(2), "series", variable_assumption
        )
        order = int(series.group(4)) if series.group(4) else _DEFAULT_SERIES_ORDER
        return NaturalLanguageOperation(
            operation="series",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            point=_math_phrase(series.group(3)) if series.group(3) else "0",
            order=order,
            rule_id="series_at_point" if series.group(3) and series.group(4) else "series_defaults",
        )

    aggregation = re.fullmatch(
        r"(?:(?:calculate|compute|find)\s+)?(?:the\s+)?(sum|summation|product)\s+"
        r"(?:of\s+)?(.+?)\s+(?:for\s+([A-Za-z][A-Za-z0-9_]{0,31})\s+)?"
        r"from\s+(.+?)\s+to\s+(.+)",
        query,
        re.IGNORECASE,
    )
    if aggregation:
        operation = "sum" if aggregation.group(1).lower() in {"sum", "summation"} else "product"
        expression = _math_phrase(aggregation.group(2))
        variable, variable_source, variable_candidates = _choose_variable(
            expression, aggregation.group(3), operation, variable_assumption
        )
        return NaturalLanguageOperation(
            operation=operation,
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            lower=_math_phrase(aggregation.group(4)),
            upper=_math_phrase(aggregation.group(5)),
            rule_id=f"{operation}_finite_range",
        )

    plot = re.fullmatch(
        r"(?:plot|graph)\s+(?:of\s+)?(.+?)\s+"
        r"(?:for\s+([A-Za-z][A-Za-z0-9_]{0,31})\s+)?"
        r"from\s+(.+?)\s+to\s+(.+)",
        query,
        re.IGNORECASE,
    )
    if plot:
        expression = _math_phrase(plot.group(1))
        variable, variable_source, variable_candidates = _choose_variable(
            expression, plot.group(2), "plot", variable_assumption
        )
        return NaturalLanguageOperation(
            operation="plot",
            expression=expression,
            variable=variable,
            variable_source=variable_source,
            variable_candidates=variable_candidates,
            lower=_math_phrase(plot.group(3)),
            upper=_math_phrase(plot.group(4)),
            rule_id="plot_finite_range",
        )

    evaluation = re.fullmatch(
        r"(?:what\s+is|what's|evaluate|calculate|compute|find\s+the\s+value\s+of)\s+"
        r"(?:(?:the\s+)?(?:value\s+of)\s+)?(.+)",
        query,
        re.IGNORECASE,
    )
    if evaluation:
        expression = _math_phrase(evaluation.group(1))
        if not _is_math_expression_candidate(expression):
            return None
        return NaturalLanguageOperation(
            operation="evaluate",
            expression=expression,
            rule_id="evaluate_expression",
        )

    return None


def _split_coordinate_chart(source: str) -> tuple[str, str | None]:
    match = re.search(
        r"\s+(?:in|using)\s+(?P<chart>[A-Za-z][A-Za-z-]*)(?:\s+coordinates?)?$",
        source,
        re.IGNORECASE,
    )
    if match is None:
        return source.strip(), None
    charts = {
        "cartesian": "Cartesian3D",
        "rectangular": "Cartesian3D",
        "cylindrical": "Cylindrical3D",
        "spherical": "Spherical3D",
    }
    chart_name = match.group("chart").lower()
    if chart_name not in charts:
        raise NaturalLanguageClarification(
            f"The {chart_name} chart is unsupported here; choose Cartesian, cylindrical, "
            "or spherical coordinates."
        )
    return source[: match.start()].strip(), charts[chart_name]


def _strip_coordinate_field_prefix(source: str) -> str:
    value = source.strip()
    value = re.sub(
        r"^(?:(?:the\s+)?(?:scalar\s+)?field\s+|with\s+respect\s+to\s+the\s+field\s+)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip().strip("`").strip()


def _split_coordinate_components(source: str) -> tuple[str, str, str]:
    value = source.strip()
    value = re.sub(
        r"^(?:(?:the\s+)?(?:vector\s+)?field\s+(?:with\s+components\s+)?|"
        r"with\s+components\s+)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^[A-Za-z][A-Za-z0-9_]{0,31}\s*=\s*", "", value)
    if value.startswith("(") and value.endswith(")"):
        depth = 0
        wraps_entire_value = True
        for index, character in enumerate(value):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    wraps_entire_value = False
                    break
                if depth < 0:
                    wraps_entire_value = False
                    break
        if wraps_entire_value and depth == 0:
            value = value[1:-1].strip()

    parts: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(value):
        if character in depths:
            depths[character] += 1
        elif character in closing:
            opening = closing[character]
            depths[opening] -= 1
            if depths[opening] < 0:
                break
        elif character == "," and not any(depths.values()):
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    if any(depths.values()) or len(parts) != 3 or any(not item for item in parts):
        raise NaturalLanguageClarification(
            "Give exactly three vector components, for example "
            "'find the divergence of (x, y, z)'."
        )
    return parts[0], parts[1], parts[2]


def _split_top_level(source: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and (
            char in ",;"
            or re.match(r"\s+and\s+", source[index:], re.IGNORECASE)
        ):
            parts.append(source[start:index].strip())
            if char in ",;":
                index += 1
            else:
                index += re.match(r"\s+and\s+", source[index:], re.IGNORECASE).end()
            start = index
            continue
        index += 1
    parts.append(source[start:].strip())
    return parts


def _plan_equation_system(body: str) -> NaturalLanguageOperation | None:
    variable_clause = re.fullmatch(
        r"(.+?)\s+for\s+((?:" + _IDENTIFIER + r")(?:\s*(?:,|and)\s*(?:" + _IDENTIFIER + r"))+)",
        body.strip(),
        re.IGNORECASE,
    )
    named: tuple[str, ...] = ()
    if variable_clause:
        body = variable_clause.group(1)
        named = tuple(re.split(r"\s*(?:,|\band\b)\s*", variable_clause.group(2).strip()))
    parts = [part for part in _split_top_level(body) if part]
    if len(parts) < 2 or not all(
        len(re.findall(r"(?<![<>=])=(?!=)", part)) == 1 for part in parts
    ):
        return None
    if len(parts) > 4:
        raise NaturalLanguageClarification("Systems are limited to four equations.")
    equations = []
    for part in parts:
        left, right = _split_equation(part)
        equations.append(f"({_math_phrase(left)})-({_math_phrase(right or '0')})")
    inferred = tuple(sorted({
        symbol for equation in equations for symbol in free_symbols(equation)
    }))
    variables = named or inferred
    if not 2 <= len(variables) <= 4 or len(set(variables)) != len(variables):
        raise NaturalLanguageClarification(
            "Name two to four distinct variables, for example "
            "'solve x^2+y^2=1, x+y=1 for x, y'."
        )
    return NaturalLanguageOperation(
        operation="solve_system",
        expression="; ".join(equations),
        equations=tuple(equations),
        variables=variables,
        rule_id="solve_polynomial_system",
    )


def _split_variable_clause(
    source: str,
    *,
    prepositions: tuple[str, ...] = ("with respect to", "wrt", "w.r.t."),
) -> tuple[str, str | None]:
    alternatives = "|".join(re.escape(value) for value in sorted(prepositions, key=len, reverse=True))
    match = re.fullmatch(
        rf"(.+?)\s+(?:{alternatives})\s+(?:the\s+)?(?:variable\s+)?({_IDENTIFIER})",
        source.strip(),
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2)
    return source.strip(), None


def _split_integral(source: str) -> tuple[str, str | None, str | None, str | None]:
    patterns = (
        re.fullmatch(
            rf"(.+?)\s+from\s+(.+?)\s+to\s+(.+?)\s+with\s+respect\s+to\s+({_IDENTIFIER})",
            source,
            re.IGNORECASE,
        ),
        re.fullmatch(
            rf"(.+?)\s+with\s+respect\s+to\s+({_IDENTIFIER})\s+from\s+(.+?)\s+to\s+(.+)",
            source,
            re.IGNORECASE,
        ),
        re.fullmatch(r"(.+?)\s+from\s+(.+?)\s+to\s+(.+)", source, re.IGNORECASE),
    )
    first, second, third = patterns
    if first:
        return first.group(1), first.group(4), first.group(2), first.group(3)
    if second:
        return second.group(1), second.group(2), second.group(3), second.group(4)
    if third:
        return third.group(1), None, third.group(2), third.group(3)
    expression, variable = _split_variable_clause(source)
    return expression, variable, None, None


def _split_equation(source: str) -> tuple[str, str | None]:
    parts = re.split(r"(?<![<>=])=(?!=)", source, maxsplit=1)
    if len(parts) == 1:
        return source.strip(), None
    if not parts[0].strip() or not parts[1].strip() or re.search(r"(?<![<>=])=(?!=)", parts[1]):
        raise NaturalLanguageClarification("Provide one equation, such as 'solve x^2 = 4 for x'.")
    return parts[0].strip(), parts[1].strip()


def _choose_variable(
    expression: str,
    variable: str | None,
    operation: str,
    variable_assumption: str | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    variables = tuple(free_symbols(expression))
    if variable:
        if variable_assumption is not None and variable_assumption != variable:
            raise NaturalLanguageClarification(
                f"The request names variable {variable}, which conflicts with the selected "
                f"formula variable {variable_assumption}."
            )
        if variable_assumption is not None and variable_assumption not in variables:
            raise NaturalLanguageClarification(
                f"operation:variable:{variable_assumption} is not a variable candidate "
                f"for this {operation} request."
            )
        return (
            variable,
            "caller_assumption" if variable_assumption is not None else "caller_phrase",
            (variable,),
        )
    if variable_assumption is not None:
        if variable_assumption not in variables:
            raise NaturalLanguageClarification(
                f"operation:variable:{variable_assumption} is not a variable candidate "
                f"for this {operation} request."
            )
        if len(variables) > 32:
            return variable_assumption, "caller_assumption", (variable_assumption,)
        return variable_assumption, "caller_assumption", variables
    if len(variables) > 32:
        raise NaturalLanguageClarification(
            "This natural-language operation has more than 32 possible variables; "
            "name the variable explicitly."
        )
    if len(variables) == 1:
        return variables[0], "unique_symbol_in_expression", variables
    if not variables:
        raise NaturalLanguageClarification(
            f"Specify the variable for {operation}, for example 'with respect to x'."
        )
    choices = ", ".join(variables[:8])
    raise NaturalLanguageClarification(
        f"Specify the variable for {operation} (possible variables: {choices}); "
        "for example, add 'with respect to x'.",
        candidate_variables=variables,
        operation=operation,
        expression=expression,
    )


def free_symbols(expression: str) -> list[str]:
    try:
        normalized = parse_math_input(expression).expression
        tree = ast.parse(normalized, mode="eval")
    except (MathInputError, SyntaxError):
        return []
    names = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _FUNCTIONS | _CONSTANTS
    }
    return sorted(names)


def _is_math_expression_candidate(expression: str) -> bool:
    value = expression.strip()
    if re.fullmatch(_IDENTIFIER, value):
        return value in _CONSTANTS or len(value) == 1
    return bool(re.search(r"\d|[+*/^(){}\[\],\\-]", value))


def _math_phrase(source: str) -> str:
    expression = source.strip().strip("`").strip()
    function_phrase = (
        r"(?:square\s+root\s+(?:of\s+)?|"
        r"(?:natural\s+logarithm|sine|cosine|tangent|logarithm|sin|cos|tan|log|ln)\s+of\s+)"
    )
    atom = r"(?:\([^()]*\)|[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)"
    trailing_operator = (
        r"(?:\b(?:plus|minus|times|multiplied\s+by|divided\s+by|squared|cubed|"
        r"to\s+the\s+power\s+of|raised\s+to)\b|[+*/^-])"
    )
    if re.search(
        rf"\b{function_phrase}{atom}\s+{trailing_operator}",
        expression,
        flags=re.IGNORECASE,
    ):
        raise NaturalLanguageClarification(
            "Parenthesize ambiguous function arguments, for example 'sin(x + 1)' or 'sin(x) + 1'."
        )
    expression = re.sub(r"\bnegative\s+infinity\b", "-oo", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\b(?:positive\s+)?infinity\b", "oo", expression, flags=re.IGNORECASE)
    expression = re.sub(
        rf"\bsquare\s+root\s+(?:of\s+)?({atom})",
        r"sqrt(\1)", expression, flags=re.IGNORECASE,
    )
    function_names = {
        "sine": "sin", "cosine": "cos", "tangent": "tan",
        "sin": "sin", "cos": "cos", "tan": "tan", "log": "log", "ln": "ln",
        "logarithm": "log", "natural logarithm": "ln",
    }
    for phrase, function in sorted(function_names.items(), key=lambda item: len(item[0]), reverse=True):
        expression = re.sub(
            rf"\b{re.escape(phrase)}\s+of\s+(\([^()]*\)|[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)",
            rf"{function}(\1)", expression, flags=re.IGNORECASE,
        )
    expression = re.sub(
        r"\b(\([^()]*\)|[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)\s+squared\b",
        r"(\1)**2", expression, flags=re.IGNORECASE,
    )
    expression = re.sub(
        r"\b(\([^()]*\)|[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)\s+cubed\b",
        r"(\1)**3", expression, flags=re.IGNORECASE,
    )
    expression = re.sub(
        r"\b([A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)\s+(?:to the power of|raised to)\s+"
        r"([A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?)\b",
        r"(\1)**(\2)", expression, flags=re.IGNORECASE,
    )
    for word, number in _NUMBER_WORDS.items():
        expression = re.sub(rf"\b{word}\b", number, expression, flags=re.IGNORECASE)
    replacements = (
        (r"\bis\s+equal\s+to\b", "="),
        (r"\bequal\s+to\b", "="),
        (r"\bdivided\s+by\b", "/"),
        (r"\bmultiplied\s+by\b", "*"),
        (r"\btimes\b", "*"),
        (r"\bplus\b", "+"),
        (r"\bminus\b", "-"),
        (r"\bequals\b", "="),
    )
    for pattern, replacement in replacements:
        expression = re.sub(pattern, replacement, expression, flags=re.IGNORECASE)
    return expression.strip()
