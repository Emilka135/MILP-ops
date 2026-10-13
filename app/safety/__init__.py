"""Safety-слой: красные флаги, оператор, медицинские вопросы (ARCHITECTURE.md 3.3)."""

from app.safety.detector import (  # noqa: F401
    BOUNDARY_PHRASES,
    DEFAULT_GLOSSARY_PATH,
    ROUTES,
    SafetyLayer,
)

__all__ = ["SafetyLayer", "DEFAULT_GLOSSARY_PATH", "ROUTES", "BOUNDARY_PHRASES"]
