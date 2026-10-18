from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .models import BinaryTerm, ClaimRequest, Comparison, NumberTerm, Sort, Term, VariableTerm

DEFAULT_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
AXIOM_LINE = re.compile(r"depends on axioms:\s*\[([^\]]*)\]")


@dataclass(frozen=True)
class LeanResult:
    status: str
    source: str
    diagnostics: str | None = None
    axioms: tuple[str, ...] = ()


def lake_executable() -> str | None:
    configured = os.environ.get("LAKE_BIN")
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    elan_lake = Path.home() / ".elan" / "bin" / ("lake.exe" if os.name == "nt" else "lake")
    if elan_lake.is_file():
        return str(elan_lake)
    return shutil.which("lake")


def render_term(term: Term, sort: Sort) -> str:
    if isinstance(term, VariableTerm):
        return term.name
    if isinstance(term, NumberTerm):
        return _lean_number(term, sort)
    if isinstance(term, BinaryTerm):
        op = {"add": "+", "sub": "-", "mul": "*"}[term.kind]
        return f"({render_term(term.left, sort)} {op} {render_term(term.right, sort)})"
    raise TypeError(f"unsupported term type: {type(term)!r}")


def render_comparison(comparison: Comparison, sort: Sort) -> str:
    op = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[comparison.op]
    return f"{render_term(comparison.left, sort)} {op} {render_term(comparison.right, sort)}"


def render_statement(request: ClaimRequest) -> str:
    sort = request.variables[0].sort if request.variables else Sort.INT
    binders = " ".join(f"({var.name} : {var.sort.value})" for var in request.variables)
    assumptions = " ".join(
        f"(h{index} : {render_comparison(item, sort)})"
        for index, item in enumerate(request.assumptions)
    )
    arguments = " ".join(part for part in (binders, assumptions) if part)
    claim = render_comparison(request.claim, sort)
    return f"theorem llm_fact_check {arguments} : {claim}".replace("  :", " :")


def make_source(request: ClaimRequest) -> str:
    sort = request.variables[0].sort if request.variables else Sort.INT
    tactic = "omega" if sort is Sort.INT else "linarith"
    imports = "import Lean\n"
    if sort is Sort.REAL:
        imports += "import Mathlib.Tactic.Linarith\n"
    return (
        imports
        + "\n"
        f"{render_statement(request)} := by\n"
        f"  {tactic}\n\n"
        "#print llm_fact_check\n"
        "#print axioms llm_fact_check\n"
    )


def check_with_lean(request: ClaimRequest, timeout_seconds: float) -> LeanResult:
    return run_generated_source(make_source(request), timeout_seconds)


def run_generated_source(source: str, timeout_seconds: float) -> LeanResult:
    lake = lake_executable()
    project_dir = Path(
        os.environ.get("LEAN_PROJECT_DIR", str(Path(__file__).resolve().parents[2] / "lean"))
    )
    if lake is None or not project_dir.is_dir():
        return LeanResult("unavailable", source, "Lean/Lake project is not configured")
    try:
        with tempfile.TemporaryDirectory(prefix="leanproof-") as temp_dir:
            source_file = Path(temp_dir) / "FactCheck.lean"
            source_file.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [lake, "env", "lean", str(source_file)],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return LeanResult("timeout", source, f"Lean exceeded {timeout_seconds:.2f} seconds")
    except OSError as exc:
        return LeanResult("unavailable", source, f"Could not start Lean: {exc}")

    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        return LeanResult("rejected", source, output[-4000:] or "Lean rejected the generated proof")
    match = AXIOM_LINE.search(output)
    if match:
        axioms = tuple(item.strip() for item in match.group(1).split(",") if item.strip())
    elif "does not depend on any axioms" in output:
        axioms = ()
    else:
        return LeanResult("rejected", source, "Lean output did not include an axiom audit")
    unexpected = set(axioms) - DEFAULT_AXIOMS
    if unexpected:
        return LeanResult(
            "rejected", source,
            f"Axiom audit found unapproved dependencies: {', '.join(sorted(unexpected))}",
            axioms,
        )
    return LeanResult("checked", source, None, axioms)


def _lean_number(term: NumberTerm, sort: Sort) -> str:
    value = Fraction(term.value)
    if sort is Sort.INT:
        return f"({value.numerator} : Int)"
    if value.denominator == 1:
        return f"({value.numerator} : Real)"
    return f"(({value.numerator} : Real) / {value.denominator})"
