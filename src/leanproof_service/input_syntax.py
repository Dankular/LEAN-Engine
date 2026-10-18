from __future__ import annotations

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET


_MAX_SOURCE_CHARS = 4000
_MAX_TOKENS = 256
_MAX_DEPTH = 32
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER = re.compile(r"[A-Za-z](?:[A-Za-z0-9]|_[A-Za-z0-9]+)*")
_IMPLICIT_COEFFICIENT = re.compile(r"(?<![A-Za-z0-9_])(?:\d+(?:\.\d*)?|\.\d+)(?=[^\W\d_])")
_MATHML_ROOT = re.compile(r"^\s*(?:<\?xml\b[^?]*\?>\s*)?<(?:[A-Za-z_][\w.-]*:)?math(?:\s|>)", re.I)
_MATH_COMMAND = re.compile(r"\\([A-Za-z]+|.)")
_GREEK_COMMANDS = {
    "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta",
    "epsilon": "epsilon", "varepsilon": "epsilon", "zeta": "zeta", "eta": "eta",
    "theta": "theta", "vartheta": "theta", "iota": "iota", "kappa": "kappa",
    "lambda": "lambda", "mu": "mu", "nu": "nu", "xi": "xi", "omicron": "omicron",
    "rho": "rho", "sigma": "sigma", "tau": "tau", "upsilon": "upsilon",
    "phi": "phi", "varphi": "phi", "chi": "chi", "psi": "psi", "omega": "omega",
    "Gamma": "Gamma", "Delta": "Delta", "Theta": "Theta", "Lambda": "Lambda",
    "Xi": "Xi", "Sigma": "Sigma", "Upsilon": "Upsilon", "Phi": "Phi",
    "Psi": "Psi", "Omega": "Omega",
}
_GREEK_UNICODE = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "ρ": "rho", "σ": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega", "Γ": "Gamma", "Δ": "Delta",
    "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi", "Σ": "Sigma", "Υ": "Upsilon",
    "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
_FUNCTIONS = {
    "sin", "cos", "tan", "asin", "acos", "atan", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "exp", "log", "ln", "sqrt", "root", "abs", "floor",
    "ceiling",
}
_FUNCTION_ALIASES = {"arcsin": "asin", "arccos": "acos", "arctan": "atan"}


class MathInputError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedMathInput:
    expression: str
    input_format: str


def parse_math_input(source: str) -> ParsedMathInput:
    """Convert a bounded presentation-math subset into the service's safe expression syntax."""
    if len(source) > _MAX_SOURCE_CHARS:
        raise MathInputError(f"mathematical input exceeds {_MAX_SOURCE_CHARS} characters")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", source, re.I):
        raise MathInputError("MathML document types and entity declarations are not supported")
    if _MATHML_ROOT.match(source):
        expression = _parse_mathml(source)
        return ParsedMathInput(expression, "mathml")
    if re.match(r"^\s*(?:<\?|<!|</?[A-Za-z])", source):
        raise MathInputError("XML mathematical input must have a MathML math root element")
    if _looks_like_latex(source):
        expression = _LatexParser(_lex_latex(_strip_math_delimiters(source))).parse()
        if len(expression) > _MAX_SOURCE_CHARS:
            raise MathInputError("normalized mathematical input is too long")
        return ParsedMathInput(expression, "latex")
    return ParsedMathInput(source, "native")


def _looks_like_latex(source: str) -> bool:
    value = source.strip()
    delimited_dollars = value.startswith("$") and value.endswith("$")
    return delimited_dollars or any(
        marker in source for marker in ("\\", "^", "{", "}", "π", "×", "·", "÷", "−", "∞")
    ) or any(character in _GREEK_UNICODE for character in source) or (
        _IMPLICIT_COEFFICIENT.search(value) is not None
    )


def _strip_math_delimiters(source: str) -> str:
    value = source.strip()
    delimiters = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))
    for opening, closing in delimiters:
        if value.startswith(opening):
            if not value.endswith(closing) or len(value) < len(opening) + len(closing):
                raise MathInputError("mathematical input has an unmatched display delimiter")
            return value[len(opening):-len(closing)].strip()
    if value.endswith(("\\]", "\\)", "$$")):
        raise MathInputError("mathematical input has an unmatched display delimiter")
    return value


def _lex_latex(source: str) -> list[tuple[str, str]]:
    degree_patterns = (
        (r"\^\s*\{\s*\\circ\s*\}", r"* pi / 180"),
        (r"\^\s*\\circ\b", r"* pi / 180"),
    )
    for pattern, replacement in degree_patterns:
        source = re.sub(pattern, replacement, source)

    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if character == "\\":
            match = _MATH_COMMAND.match(source, index)
            if match is None:
                raise MathInputError("invalid LaTeX command")
            command = match.group(1)
            index = match.end()
            token = _latex_command(command, source, index)
            if token is None:
                if command in {"mathrm", "operatorname"}:
                    text_match = re.match(r"\s*\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}", source[index:])
                    if text_match is None:
                        raise MathInputError(f"\\{command} requires a simple identifier")
                    name = text_match.group(1)
                    index += text_match.end()
                    if command == "operatorname" and name in _FUNCTIONS:
                        tokens.append(("function", _FUNCTION_ALIASES.get(name, name)))
                    else:
                        tokens.append(("identifier", "E" if name == "e" else name))
                    continue
                raise MathInputError(f"unsupported LaTeX command: \\{command}")
            kind, value = token
            if kind != "ignore":
                tokens.append((kind, value))
            continue
        number = _NUMBER.match(source, index)
        if number:
            tokens.append(("number", number.group(0)))
            index = number.end()
            continue
        identifier = _IDENTIFIER.match(source, index)
        if identifier:
            value = identifier.group(0)
            if len(value) > 32:
                raise MathInputError("symbol names are limited to 32 characters")
            tokens.append(("identifier", value))
            index = identifier.end()
            continue
        if character in _GREEK_UNICODE:
            tokens.append(("identifier", _GREEK_UNICODE[character]))
            index += 1
            continue
        unicode_operators = {"π": ("identifier", "pi"), "∞": ("identifier", "oo"),
                             "×": ("operator", "*"), "·": ("operator", "*"),
                             "⋅": ("operator", "*"), "÷": ("operator", "/"),
                             "−": ("operator", "-")}
        if character in unicode_operators:
            tokens.append(unicode_operators[character])
            index += 1
            continue
        if character in "+-*/^(){}[]_,":
            tokens.append(("operator", character))
            index += 1
            continue
        raise MathInputError(f"unsupported character in mathematical input: {character!r}")

    if len(tokens) > _MAX_TOKENS:
        raise MathInputError(f"mathematical input exceeds the {_MAX_TOKENS}-token limit")
    return tokens


def _latex_command(command: str, source: str, after_command: int) -> tuple[str, str] | None:
    if command in {"frac", "dfrac", "tfrac"}:
        return "fraction", ""
    if command == "sqrt":
        return "square_root", ""
    if command in {"left", "right", "displaystyle", "textstyle", "scriptstyle",
                   "quad", "qquad", "enspace", ";", ",", ":", "!"}:
        return "ignore", ""
    operators = {"cdot": "*", "times": "*", "ast": "*", "div": "/"}
    if command in operators:
        return "operator", operators[command]
    function_names = {
        "sin": "sin", "cos": "cos", "tan": "tan", "arcsin": "asin", "arccos": "acos",
        "arctan": "atan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "exp": "exp",
        "log": "log", "ln": "ln", "abs": "abs", "floor": "floor", "ceiling": "ceiling",
    }
    if command in function_names:
        return "function", function_names[command]
    if command == "pi":
        return "identifier", "pi"
    if command in {"infty", "infinity"}:
        return "identifier", "oo"
    if command in {"e", "imaginaryi"}:
        return "identifier", "E" if command == "e" else "I"
    if command in _GREEK_COMMANDS:
        return "identifier", _GREEK_COMMANDS[command]
    if command == "{" or command == "}":
        return "operator", command
    if command in {"(", ")", "[", "]", "+", "-", "*", "/", "^", ","}:
        return "operator", command
    return None


class _LatexParser:
    _PRECEDENCE = {"+": 10, "-": 10, "*": 20, "/": 20, "^": 30}
    _OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]"}
    _PRIMARY_KINDS = {"number", "identifier", "function", "fraction", "square_root"}

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> str:
        if not self.tokens:
            raise MathInputError("mathematical input is empty")
        expression = self._expression(0, 0)
        if self.index != len(self.tokens):
            raise MathInputError("mathematical input contains an unexpected token")
        return expression

    def _expression(self, minimum_precedence: int, depth: int) -> str:
        if depth > _MAX_DEPTH:
            raise MathInputError(f"mathematical input exceeds the {_MAX_DEPTH}-level limit")
        left = self._prefix(depth + 1)
        while self.index < len(self.tokens):
            kind, value = self.tokens[self.index]
            implicit_product = kind in self._PRIMARY_KINDS or (kind == "operator" and value in self._OPEN_TO_CLOSE)
            if kind == "operator" and value in self._PRECEDENCE:
                operator = value
                precedence = self._PRECEDENCE[operator]
                if precedence < minimum_precedence:
                    break
                self.index += 1
                right_minimum = precedence if operator == "^" else precedence + 1
                right = self._expression(right_minimum, depth + 1)
            elif implicit_product:
                operator = "*"
                precedence = self._PRECEDENCE[operator]
                if precedence < minimum_precedence:
                    break
                right = self._expression(precedence + 1, depth + 1)
            else:
                break
            emitted_operator = "**" if operator == "^" else operator
            left = f"({left}{emitted_operator}{right})"
        return left

    def _prefix(self, depth: int) -> str:
        if self.index >= len(self.tokens):
            raise MathInputError("mathematical input ends unexpectedly")
        kind, value = self.tokens[self.index]
        self.index += 1
        if kind == "operator" and value in {"+", "-"}:
            operand = self._expression(25, depth + 1)
            return f"({value}{operand})"
        if kind == "identifier" and self._current() == ("operator", "_"):
            self.index += 1
            current = self._current()
            if current and current[0] == "operator" and current[1] == "{":
                subscript = self._required_group(depth + 1)
            elif current and current[0] in {"number", "identifier"}:
                self.index += 1
                subscript = current[1]
            else:
                raise MathInputError("symbol subscript must be a simple identifier or integer")
            if not re.fullmatch(r"[A-Za-z0-9]+", subscript):
                raise MathInputError("symbol subscript must be a simple identifier or integer")
            symbol = f"{value}_{subscript}"
            if len(symbol) > 32:
                raise MathInputError("symbol names are limited to 32 characters")
            return symbol
        if kind == "number" or kind == "identifier":
            return value
        if kind == "operator" and value in self._OPEN_TO_CLOSE:
            return self._group(value, depth + 1)
        if kind == "fraction":
            numerator = self._required_group(depth + 1)
            denominator = self._required_group(depth + 1)
            return f"(({numerator})/({denominator}))"
        if kind == "square_root":
            root_index = "2"
            if self._current() == ("operator", "["):
                root_index = self._required_group(depth + 1, opening="[")
            radicand = self._required_group(depth + 1)
            if root_index == "2":
                return f"sqrt({radicand})"
            return f"root({radicand},{root_index})"
        if kind == "function":
            power = None
            if self._current() == ("operator", "^"):
                self.index += 1
                power = self._expression(31, depth + 1)
            if self._current() == ("operator", "_") and value == "log":
                self.index += 1
                current = self._current()
                if current and current[0] == "operator" and current[1] == "{":
                    base = self._required_group(depth + 1)
                elif current and current[0] in {"number", "identifier"}:
                    self.index += 1
                    base = current[1]
                else:
                    raise MathInputError("logarithm base must be a number or simple symbol")
                argument = self._function_argument(depth + 1)
                call = f"log({argument},{base})"
            else:
                argument = self._function_argument(depth + 1)
                call = f"log({argument},E)" if value == "ln" else f"{value}({argument})"
            return f"({call})**({power})" if power is not None else call
        raise MathInputError("mathematical input contains an unsupported construct")

    def _function_argument(self, depth: int) -> str:
        current = self._current()
        if current and current[0] == "operator" and current[1] in self._OPEN_TO_CLOSE:
            opening = current[1]
            self.index += 1
            return self._group(opening, depth + 1)
        # An unbracketed function argument consumes a multiplicative expression,
        # while stopping before additive terms: `\\sin 2x + y` means `sin(2x) + y`.
        return self._expression(20, depth + 1)

    def _required_group(self, depth: int, opening: str | None = None) -> str:
        current = self._current()
        if current is None or current[0] != "operator" or current[1] not in self._OPEN_TO_CLOSE:
            raise MathInputError("fraction, root, and subscript arguments must be grouped")
        if opening is not None and current[1] != opening:
            raise MathInputError("mathematical input has an unexpected grouping delimiter")
        self.index += 1
        return self._group(current[1], depth + 1)

    def _group(self, opening: str, depth: int) -> str:
        closing = self._OPEN_TO_CLOSE[opening]
        expression = self._expression(0, depth + 1)
        if self._current() != ("operator", closing):
            raise MathInputError("mathematical input has an unmatched grouping delimiter")
        self.index += 1
        return expression

    def _current(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None


def _parse_mathml(source: str) -> str:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", source, re.I):
        raise MathInputError("MathML document types and entity declarations are not supported")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise MathInputError("MathML input is not well-formed XML") from exc
    if _local_name(root.tag) != "math":
        raise MathInputError("MathML input must have a math root element")
    count = [0]
    latex = _mathml_node(root, 0, count)
    if not latex.strip():
        raise MathInputError("MathML input contains no mathematical expression")
    tokens = _lex_latex(latex)
    return _LatexParser(tokens).parse()


def _mathml_node(element: ET.Element, depth: int, count: list[int]) -> str:
    count[0] += 1
    if count[0] > _MAX_TOKENS or depth > _MAX_DEPTH:
        raise MathInputError("MathML input exceeds its node or nesting limit")
    tag = _local_name(element.tag)
    if tag in {"math", "mrow", "mstyle", "mphantom"}:
        return " ".join(
            value for child in element
            if (value := _mathml_node(child, depth + 1, count)).strip()
        )
    if tag == "semantics":
        for child in element:
            if _local_name(child.tag) != "annotation":
                return _mathml_node(child, depth + 1, count)
        raise MathInputError("MathML semantics element has no presentation expression")
    if tag in {"annotation", "annotation-xml", "mspace"}:
        return ""
    text = (element.text or "").strip()
    if tag == "mn":
        if not _NUMBER.fullmatch(text):
            raise MathInputError("MathML numeric tokens must be numeric literals")
        return text
    if tag == "mi":
        if text == "π":
            return "pi"
        if text == "∞":
            return "oo"
        if text == "e" and element.attrib.get("mathvariant") == "normal":
            return "E"
        if text in _GREEK_UNICODE:
            return _GREEK_UNICODE[text]
        if text in _FUNCTIONS:
            return "\\" + text
        if len(text) > 32 or not _IDENTIFIER.fullmatch(text):
            raise MathInputError("MathML identifiers must be simple symbols or supported functions")
        return text
    if tag == "mo":
        operators = {
            "+": "+", "-": "-", "−": "-", "*": "*", "×": r"\cdot",
            "·": r"\cdot", "⋅": r"\cdot", "/": "/", "÷": "/", "^": "^",
            "(": "(", ")": ")", "[": "[", "]": "]", "{": "{", "}": "}",
            "⁢": r"\cdot", "⁡": "", ",": ",",
        }
        if text not in operators:
            raise MathInputError(f"MathML operator {text!r} is not supported")
        return operators[text]
    if tag in {"mfrac", "msup", "msub", "msqrt", "mroot"}:
        children = list(element)
        expected = {"mfrac": 2, "msup": 2, "msub": 2, "msqrt": 1, "mroot": 2}[tag]
        if len(children) != expected:
            raise MathInputError(f"MathML {tag} requires {expected} child elements")
        converted = [_mathml_node(child, depth + 1, count) for child in children]
        if tag == "mfrac":
            return rf"\frac{{{converted[0]}}}{{{converted[1]}}}"
        if tag == "msup":
            return f"{{{converted[0]}}}^{{{converted[1]}}}"
        if tag == "msub":
            if converted[0] == r"\log":
                return rf"\log_{{{converted[1]}}}"
            if (
                len(converted[0]) > 32
                or not _IDENTIFIER.fullmatch(converted[0])
                or not re.fullmatch(r"[A-Za-z0-9]+", converted[1])
                or len(converted[0]) + len(converted[1]) + 1 > 32
            ):
                raise MathInputError("MathML subscripts are supported only on simple symbols and log bases")
            return f"{converted[0]}_{converted[1]}"
        if tag == "msqrt":
            return rf"\sqrt{{{converted[0]}}}"
        return rf"\sqrt[{converted[1]}]{{{converted[0]}}}"
    if tag == "mfenced":
        opening = element.attrib.get("open", "(")
        closing = element.attrib.get("close", ")")
        separator = element.attrib.get("separators", ",")[:1]
        matching = {"(": ")", "[": "]", "{": "}"}
        if opening not in matching or matching[opening] != closing or separator not in {",", ";"}:
            raise MathInputError("MathML mfenced delimiters are unsupported")
        values = [_mathml_node(child, depth + 1, count) for child in element]
        if len(values) != 1:
            raise MathInputError("MathML mfenced must contain exactly one scalar expression")
        return opening + values[0] + closing
    raise MathInputError(f"MathML element {tag!r} is not supported")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
