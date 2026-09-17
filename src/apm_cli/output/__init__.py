"""Output formatting and presentation layer for APM CLI."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .formatters import CompilationFormatter
    from .models import CompilationResults, OptimizationDecision, OptimizationStats, ProjectAnalysis

__all__ = [
    "CompilationFormatter",
    "CompilationResults",
    "OptimizationDecision",
    "OptimizationStats",
    "ProjectAnalysis",
]


def __getattr__(name: str) -> Any:
    """Load public output types without eagerly importing the compilation graph."""
    if name == "CompilationFormatter":
        from .formatters import CompilationFormatter

        return CompilationFormatter
    if name in {
        "CompilationResults",
        "OptimizationDecision",
        "OptimizationStats",
        "ProjectAnalysis",
    }:
        from . import models

        return getattr(models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
