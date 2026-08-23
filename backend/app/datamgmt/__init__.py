"""Data Management: browsing and maintaining the records already in the warehouse.

The rest of the platform *reports on* data. This package is the one place that
changes it by hand, so it is built around three refusals:

* a business code is never edited — it is the identity every fact references;
* master data is never destroyed — it is retired, and can be restored;
* a transaction is never deleted — it is voided, and only when nothing depends
  on it.

Everything it exposes is filtered by the caller's data scope before the query
runs, gated by a per-action permission, and recorded field by field in
``data_change_log``.
"""

#: Deliberately re-exports types only. ``catalogue`` is the name of a submodule
#: *and* of the function inside it, so binding the function here would shadow
#: the module and break ``from app.datamgmt import catalogue``.
from .catalogue import (
    MASTER,
    TRANSACTION,
    ManagedEntity,
    ManagedField,
    get_master,
    get_transaction,
)

__all__ = [
    "MASTER",
    "TRANSACTION",
    "ManagedEntity",
    "ManagedField",
    "get_master",
    "get_transaction",
]
