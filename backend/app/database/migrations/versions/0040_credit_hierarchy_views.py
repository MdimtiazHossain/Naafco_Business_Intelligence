"""Credit Control's views carry the sales hierarchy, so a scope can be honoured.

Revision ID: 0040_credit_hierarchy_views
Revises: 0039_deduction_convention
Create Date: 2026-09-11

Three views rebuilt. ``vw_credit_invoice_detail`` gains the six organisational
levels above the customer, ``vw_customer_credit_exposure`` carries them through,
and ``vw_credit_aging`` gains the ones a regional aging report needs.

**This is the revision four refusals were waiting for.** ``queries.SCOPE_POLICY``
declares REFUSE for this view, and `/api/reports/credit-control` answered 403,
``ai.tools`` raised, ``get_business_alerts`` skipped its overdue check and the
executive dashboard carried no Overdue card — all of them because the view
reached the customer's sub-territory and no further, so a region-scoped caller's
scope could only be dropped in silence or refused outright. CLAUDE.md said of the
dashboard card: "goes back on the table once the credit view carries the sales
hierarchy." This is that.

**Two chains, each whole, and never mixed.** A row's levels come from the
resolved keys 0038 put on the fact, or — for a row loaded before that existed —
from its customer's sub-territory walked up through the masters. Both are
internally consistent by construction, which is what makes a per-level
``COALESCE`` safe here: ``resolve_org`` derives the *entire* chain upward from
the deepest level a file states, so a fact row holds either a complete chain or
none of one, and a legacy row holds none. There is no case where a region comes
from one source and the area beneath it from another.

The first draft of this view got that wrong in a way worth recording. It anchored
on the sub-territory alone and derived everything above it, reasoning that one
anchor could not disagree with itself — and so a row that stated its *territory*
and no sub-territory came out with no hierarchy at all, every level NULL, silently
invisible to any scoped report. The SPL extract states both levels so it looked
correct on the real file; it was a fixture stating only a territory that showed
the hole. Reading the chain the loader already resolved has neither problem.

The customer fallback is not a concession either; it is the only hierarchy the
older Credit Invoice extract ever had. That file carries no organisational column
at all, so its rows have always been placed by their customer's sub-territory,
and rows for companies no receivables extract covers keep exactly the placement
they had.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0040_credit_hierarchy_views"
down_revision: Union[str, None] = "0039_deduction_convention"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: In creation order — the two aggregates read the detail view. Dropped reversed.
_VIEWS: tuple[str, ...] = (
    "vw_credit_invoice_detail",
    "vw_customer_credit_exposure",
    "vw_credit_aging",
)


def _revision_0031():
    """Load 0031 by path, for its ``_days_between`` and its downgrade bodies.

    The device 0020 and 0038 both use. The lateness expression is the one thing
    in these views that cannot be written portably, and a second copy of it here
    would be a second thing to drift — with the failure mode that SQLite and
    PostgreSQL quietly bucket an invoice differently.
    """
    import importlib.util
    from pathlib import Path

    name = "0031_credit_control"
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Both chains. The fact's resolved keys first, then the customer-derived
#: fallback for rows that predate them.
#:
#: The fact side joins on surrogate keys, which is what 0038's nine indexes are
#: for. The customer side walks parent *codes*, because that is what a parent
#: link is in this schema — ``dim_territory.unit_code`` names a unit, not a
#: ``unit_id`` — and it is the same walk ``resolve_ancestors`` performs.
_CHAIN = """
LEFT JOIN dim_business_unit fbu ON fbu.business_unit_id = f.business_unit_id
LEFT JOIN dim_sales_line    fsl ON fsl.sales_line_id    = f.sales_line_id
LEFT JOIN dim_zone          fz  ON fz.zone_id           = f.zone_id
LEFT JOIN dim_region        fr  ON fr.region_id         = f.region_id
LEFT JOIN dim_area          fa  ON fa.area_id           = f.area_id
LEFT JOIN dim_unit          fu  ON fu.unit_id           = f.unit_id
LEFT JOIN dim_territory     ft  ON ft.territory_id      = f.territory_id
LEFT JOIN dim_sub_territory fst ON fst.sub_territory_id = f.sub_territory_id

LEFT JOIN dim_sub_territory cst ON cst.sub_territory_code = c.sub_territory_code
LEFT JOIN dim_territory     ct  ON ct.territory_code     = cst.territory_code
LEFT JOIN dim_unit          cu  ON cu.unit_code          = ct.unit_code
LEFT JOIN dim_area          ca  ON ca.area_code          = cu.area_code
LEFT JOIN dim_region        cr  ON cr.region_code        = ca.region_code
LEFT JOIN dim_zone          cz  ON cz.zone_code          = cr.zone_code
LEFT JOIN dim_sales_line    csl ON csl.sales_line_code   = cz.sales_line_code
LEFT JOIN dim_business_unit cbu ON cbu.bu_code           = csl.bu_code
"""


def _detail_view(dialect: str) -> str:
    """``vw_credit_invoice_detail``, now placed in the organisation.

    Column order puts the hierarchy where the sales and target views put it —
    between the company and the customer — so a reader moving between the three
    finds the same shape.

    ``company_code`` still comes off the fact rather than out of ``dim_company``.
    It is NOT NULL there, it is what the invoice was raised under, and it is what
    every existing filter and the exposure view's grain already use. Taking it
    from the derived chain instead would make a row's company depend on its
    customer's sub-territory resolving, which is a much weaker thing to hang a
    company-filtered report on.
    """
    lateness = _revision_0031()._days_between(dialect, "CURRENT_DATE", "dd.full_date")
    return f"""
SELECT
    f.credit_invoice_id,
    f.company_code,
    dc.company_name,
    COALESCE(fbu.bu_code, cbu.bu_code) AS bu_code,
    COALESCE(fbu.bu_name, cbu.bu_name) AS bu_name,
    COALESCE(fsl.sales_line_code, csl.sales_line_code) AS sales_line_code,
    COALESCE(fsl.sales_line_name, csl.sales_line_name) AS sales_line_name,
    COALESCE(fz.zone_code, cz.zone_code) AS zone_code,
    COALESCE(fz.zone_name, cz.zone_name) AS zone_name,
    COALESCE(fr.region_code, cr.region_code) AS region_code,
    COALESCE(fr.region_name, cr.region_name) AS region_name,
    COALESCE(fa.area_code, ca.area_code) AS area_code,
    COALESCE(fa.area_name, ca.area_name) AS area_name,
    COALESCE(fu.unit_code, cu.unit_code) AS unit_code,
    COALESCE(fu.unit_name, cu.unit_name) AS unit_name,
    COALESCE(ft.territory_code, ct.territory_code) AS territory_code,
    COALESCE(ft.territory_name, ct.territory_name) AS territory_name,
    COALESCE(fst.sub_territory_code, cst.sub_territory_code) AS sub_territory_code,
    COALESCE(fst.sub_territory_name, cst.sub_territory_name) AS sub_territory_name,
    f.invoice_no,
    f.plant_code,
    p.plant_name,
    f.customer_code,
    c.customer_name,
    f.invoice_date_id,
    idt.full_date AS invoice_date,
    f.credit_days,
    f.due_date_id,
    dd.full_date AS due_date,
    f.invoice_value,
    f.return_amount,
    f.net_invoice_amount,
    f.payment_amount,
    f.discount_amount,
    f.adjustment_amount,
    f.balance_amount,
    f.payment_mode,
    f.last_payment_date_id,
    lpd.full_date AS last_payment_date,
    f.clearing_date_id,
    cld.full_date AS clearing_date,
    f.clearing_document,
    f.data_quality_flag,
    CASE WHEN {lateness} > 0 THEN {lateness} ELSE 0 END AS days_overdue,
    CASE
        WHEN f.balance_amount <= 0 THEN 'CLEARED'
        WHEN {lateness} > 0 THEN 'OVER_DUE'
        ELSE 'NOT_YET_DUE'
    END AS credit_status,
    CASE
        WHEN f.balance_amount <= 0 THEN NULL
        WHEN {lateness} <= 0 THEN 'NOT_YET_DUE'
        WHEN {lateness} <= 30 THEN '1-30'
        WHEN {lateness} <= 60 THEN '31-60'
        WHEN {lateness} <= 90 THEN '61-90'
        WHEN {lateness} <= 120 THEN '91-120'
        WHEN {lateness} <= 180 THEN '121-180'
        WHEN {lateness} <= 365 THEN '181-365'
        ELSE '365+'
    END AS aging_bucket,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_credit_invoice f
JOIN dim_date dd ON dd.date_id = f.due_date_id
JOIN dim_date idt ON idt.date_id = f.invoice_date_id
LEFT JOIN dim_date lpd ON lpd.date_id = f.last_payment_date_id
LEFT JOIN dim_date cld ON cld.date_id = f.clearing_date_id
LEFT JOIN dim_customer c ON c.customer_code = f.customer_code
LEFT JOIN dim_plant p ON p.plant_id = f.plant_id
LEFT JOIN dim_company dc ON dc.company_code = f.company_code
{_CHAIN}
WHERE f.is_void = FALSE
"""


#: ``vw_customer_credit_exposure`` — one row per customer per company, now placed.
#:
#: The hierarchy joins the grain rather than being aggregated away, for the same
#: reason company is in it: a customer belongs to one sub-territory, so grouping
#: by it adds no rows, and without it a regionally scoped caller could not read
#: this view at all.
CUSTOMER_CREDIT_EXPOSURE = """
SELECT
    company_code,
    company_name,
    bu_code,
    bu_name,
    sales_line_code,
    sales_line_name,
    zone_code,
    zone_name,
    region_code,
    region_name,
    area_code,
    area_name,
    unit_code,
    unit_name,
    territory_code,
    territory_name,
    sub_territory_code,
    sub_territory_name,
    customer_code,
    customer_name,
    COUNT(*) AS invoice_count,
    SUM(invoice_value) AS total_invoice_amount,
    SUM(net_invoice_amount) AS net_invoice_amount,
    SUM(payment_amount) AS payment_amount,
    SUM(discount_amount) AS discount_amount,
    SUM(adjustment_amount) AS adjustment_amount,
    SUM(CASE WHEN balance_amount > 0 THEN balance_amount ELSE 0 END)
        AS outstanding_amount,
    SUM(CASE WHEN credit_status = 'OVER_DUE' THEN balance_amount ELSE 0 END)
        AS overdue_amount,
    SUM(CASE WHEN credit_status = 'OVER_DUE' THEN 1 ELSE 0 END)
        AS overdue_invoice_count,
    SUM(CASE WHEN credit_status = 'CLEARED' THEN 1 ELSE 0 END)
        AS cleared_invoice_count,
    MIN(CASE WHEN credit_status = 'OVER_DUE' THEN due_date END) AS oldest_due_date,
    MAX(last_payment_date) AS last_payment_date
FROM vw_credit_invoice_detail
GROUP BY
    company_code, company_name, bu_code, bu_name, sales_line_code,
    sales_line_name, zone_code, zone_name, region_code, region_name,
    area_code, area_name, unit_code, unit_name, territory_code, territory_name,
    sub_territory_code, sub_territory_name, customer_code, customer_name
"""


#: ``vw_credit_aging`` — outstanding per bucket, per company **and per region**.
#:
#: Region and territory join the grain because an aging report that cannot be
#: narrowed to a region is not one a regional manager can act on, and because the
#: Aging x Region matrix the page is about to draw reads exactly this. Adding the
#: whole chain instead would multiply the row count for no gain: every level
#: above region is derivable from it, and this view exists to be small.
CREDIT_AGING = """
SELECT
    company_code,
    zone_code,
    region_code,
    region_name,
    area_code,
    territory_code,
    aging_bucket,
    COUNT(*) AS invoice_count,
    SUM(balance_amount) AS outstanding_amount,
    MIN(days_overdue) AS min_days_overdue,
    MAX(days_overdue) AS max_days_overdue
FROM vw_credit_invoice_detail
WHERE aging_bucket IS NOT NULL
GROUP BY company_code, zone_code, region_code, region_name, area_code,
         territory_code, aging_bucket
"""


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    for view in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.execute(f"CREATE VIEW vw_credit_invoice_detail AS {_detail_view(dialect)}")
    op.execute(f"CREATE VIEW vw_customer_credit_exposure AS {CUSTOMER_CREDIT_EXPOSURE}")
    op.execute(f"CREATE VIEW vw_credit_aging AS {CREDIT_AGING}")


def downgrade() -> None:
    """Back to 0031's bodies, read from the revision that authored them.

    Rebuilt from 0031 rather than transcribed, which is the rule in this chain:
    a view is restored from whichever revision last authored it, so a downgrade
    cannot resurrect a body that was corrected somewhere in between.
    """
    revision_0031 = _revision_0031()
    dialect = op.get_bind().dialect.name
    for view in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.execute("CREATE VIEW vw_credit_invoice_detail AS "
               f"{revision_0031._detail_view(dialect)}")
    op.execute("CREATE VIEW vw_customer_credit_exposure AS "
               f"{revision_0031.CUSTOMER_CREDIT_EXPOSURE}")
    op.execute(f"CREATE VIEW vw_credit_aging AS {revision_0031.CREDIT_AGING}")
