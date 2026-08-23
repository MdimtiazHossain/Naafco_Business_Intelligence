"""Reporting layer: read-only queries over the Phase 2 views.

Everything the future AI agent needs is expressed here as parameterised
functions, never as SQL strings assembled from user input.
"""

from .service import (
    sales_report,
    stock_report,
    target_report,
)

__all__ = [
    "sales_report",
    "stock_report",
    "target_report",
]
