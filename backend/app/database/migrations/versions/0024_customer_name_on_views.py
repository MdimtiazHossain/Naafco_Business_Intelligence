"""Customer name on the sales and target detail views.

Revision ID: 0024_customer_name_on_views
Revises: 0023_material_company
Create Date: 2026-08-23

A customer-wise report showed ``2000060120`` where every other dimension showed
a name. Region, territory, material, plant and storage location all reach their
label through the master the view already joins; customer was the one exception,
and it was an exception for a reason that has expired.

``dim_customer`` was ``PENDING_SOURCE_DATA``: no customer master had arrived, the
table was empty, and a join would have turned every customer on every report
into a NULL. So the fact's raw ``customer_code`` was both the key and the label.
The master is here now — 2,091 customers, every one named, and all 909 codes the
sales data uses resolve — so the exception costs a name for nothing.

**This changes what is displayed, not what anything is keyed on.**
``customer_code`` stays on both facts, stays in both views, stays the business
key, stays what filters and scope are applied to, and stays what a report groups
by. The name is added beside it as a label, exactly as ``region_name`` sits
beside ``region_code``. Nothing joins on the name and nothing may.

**A LEFT JOIN, and on the code.** Left, because a sale whose customer the master
has not received must keep its figures and show its code rather than vanish from
a total — the same rule every other join in these views follows. On the code
rather than on ``customer_id``, because the surrogate key is NULL on any fact
loaded before its customer master arrived while the code is always present; the
code is unique in ``dim_customer``, so the join stays one-to-one either way.

Retired customers are **not** excluded. ``is_deleted`` retires a master record
from selection, not from history: last July's invoices were real and still name
who bought.

Views only — no table is created, altered or rewritten, and no row is read or
written. The SQLite view-capture dance revisions 0020 and 0022 needed does not
apply: there is no table rewrite for a dependent view to fail against.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0024_customer_name_on_views"
down_revision: Union[str, None] = "0023_material_company"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _load(module_name: str):
    """Load another revision file by path — see 0020 for why this is necessary."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name(f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {module_name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The one column added, and the one join that supplies it.
_SELECT_ANCHOR = "    f.customer_code,\n"
_SELECT_WITH_NAME = "    f.customer_code,\n    cu.customer_name,\n"

_JOIN_ANCHOR = "JOIN dim_date d"
_CUSTOMER_JOIN = "LEFT JOIN dim_customer cu ON cu.customer_code = f.customer_code\n"


def _with_customer_name(body: str) -> str:
    """0022's body plus the customer name and the join behind it.

    Derived from the revision that last authored these views rather than copied,
    so the two do not drift: this revision's whole content is one column and one
    join, and stating it as an edit says exactly that. A missing anchor raises
    rather than silently producing a view without the name.
    """
    if _SELECT_ANCHOR not in body or _JOIN_ANCHOR not in body:  # pragma: no cover
        raise RuntimeError(
            "0022's view body no longer has the anchors this revision edits; "
            "rebuild these views from whichever revision authored them last."
        )
    body = body.replace(_SELECT_ANCHOR, _SELECT_WITH_NAME, 1)
    # Before the date join, so the customer master is joined at the same level
    # as every other dimension rather than inside the organisational block.
    return body.replace(_JOIN_ANCHOR, _CUSTOMER_JOIN + _JOIN_ANCHOR, 1)


def upgrade() -> None:
    previous = _load("0022_remove_product_architecture")
    op.execute("DROP VIEW IF EXISTS vw_sales_detail")
    op.execute("DROP VIEW IF EXISTS vw_target_detail")
    op.execute("CREATE VIEW vw_sales_detail AS "
               f"{_with_customer_name(previous.SALES_DETAIL)}")
    op.execute("CREATE VIEW vw_target_detail AS "
               f"{_with_customer_name(previous.TARGET_DETAIL)}")


def downgrade() -> None:
    """Restore 0022's bodies verbatim — the name goes, the code was never touched."""
    previous = _load("0022_remove_product_architecture")
    op.execute("DROP VIEW IF EXISTS vw_sales_detail")
    op.execute("DROP VIEW IF EXISTS vw_target_detail")
    op.execute(f"CREATE VIEW vw_sales_detail AS {previous.SALES_DETAIL}")
    op.execute(f"CREATE VIEW vw_target_detail AS {previous.TARGET_DETAIL}")
