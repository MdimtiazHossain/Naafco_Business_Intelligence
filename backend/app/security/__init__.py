"""Application security metadata shared by the API, the admin panel and the UI.

The section catalogue lives here rather than in the API layer so that exactly one
list of application sections exists: the backend enforces it, the admin panel
renders it, and the frontend navigation reads it from the same endpoint.
"""

from .sections import (
    SECTION_BY_KEY,
    SECTIONS,
    Section,
    SectionKey,
    section_keys,
)

__all__ = ["SECTIONS", "SECTION_BY_KEY", "Section", "SectionKey", "section_keys"]
