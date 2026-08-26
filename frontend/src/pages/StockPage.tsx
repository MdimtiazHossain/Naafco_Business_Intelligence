/**
 * Material stock.
 *
 * A current position per plant, storage location, material and material group,
 * split by what the stock is available for. Every figure comes from the Phase 3
 * stock tools — this page computes nothing.
 *
 * **The four categories stay four numbers.** Unrestricted stock is what can
 * actually be sold; quality inspection, blocked and in-transit are each held
 * back for a different reason. A single headline figure would hide that, so the
 * total is shown beside them rather than instead of them.
 *
 * The global date filter does not apply here and the page says so: the source
 * carries no posting date, so a stock figure is the same for any window.
 *
 * **The unit is part of every name here, and of no number.** A card reads
 * `Unrestricted Stock (KG/LTR)` over `125,500`, a column heading carries the
 * unit and its cells are plain figures, and the chart tooltip is given the same
 * labelled name. The Material Transaction Data states four quantities and no
 * unit of measure, and a plant holds stock in kilograms or in litres depending
 * on the material — so the unit is *named*, never converted: there is no factor
 * in the source to convert with, and one invented here would be
 * indistinguishable from a real measurement. That is also why there is no UOM
 * column anywhere on this page: a per-row unit would have to be guessed, and
 * naming both in the heading is the honest answer instead.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { CategoryBarChart } from '../charts/Charts';
import { ExportButtons } from '../components/ExportButtons';
import { StatCard } from '../components/KpiCard';
import { PageHeader, ResultNotes, Section } from '../components/PageHeader';
import { KpiSkeleton, QueryState } from '../components/States';
import { STOCK_FILTERS, useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { stockService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatStock, stockLabel } from '../utils/format';

/** Horizons offered for "expiring soon". The server's setting is the default. */
const HORIZONS = [30, 60, 90, 180] as const;

/**
 * How urgent each expiry bucket looks. Anything unbucketed reads as neutral.
 *
 * `EXPIRED` and `EXPIRING_SOON` are absent: both are stock statuses under the
 * application-wide standard, so their colour comes from `stockStatusClass` and
 * covers the label as well as the figure. Leaving a tone here too would give
 * the same card two answers.
 */
const BUCKET_TONE: Record<string, 'default' | 'warning' | 'danger' | 'success'> = {
  VALID: 'success',
  NO_EXPIRY: 'default',
};

/**
 * What a shelf-life bucket is called on a card.
 *
 * A card names a measure and a dropdown names a choice, and the two want
 * different words: the Shelf Life filter offers "Expired", while the card over
 * a figure has to say "Expired Stock (KG/LTR)" — the same name the executive
 * dashboard and the agent use for that measure. `stock.card.*` supplies the
 * card wording where it differs; anything without one falls back to the bucket
 * name the filter uses, which is right for "Valid" and "No Expiry Date".
 */
function bucketCardName(t: (key: string) => string, code: string): string {
  const card = t(`stock.card.${code}`);
  return card === `stock.card.${code}` ? t(`stock.bucket.${code}`) : card;
}

export default function StockPage() {
  const t = useT();
  const { queryFor } = useFilters();
  const [horizon, setHorizon] = useState<number | undefined>(undefined);

  // Only the filters this page can honour, and no period. Filter state is
  // global and lives in the URL, so arriving from Sales carries a region and a
  // customer the stock view has no column for; sending them made the page look
  // narrowed while every figure stayed the total.
  const query = queryFor(STOCK_FILTERS, false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['stock-page', query, horizon],
    queryFn: () => stockService.page({ ...query, expiring_within_days: horizon }),
  });

  const totals = data?.summary.values ?? {};
  // The order runs from where stock is held down to what it is: Plant and
  // Storage Location say where, then the material itself, then the two ways the
  // Material Master classifies it — its group and its brand.
  const breakdowns = [
    { key: 'plant', title: t('stock.byPlant'), result: data?.by_plant },
    { key: 'storage', title: t('stock.byStorageLocation'), result: data?.by_storage_location },
    { key: 'material', title: t('stock.byMaterial'), result: data?.by_material },
    { key: 'group', title: t('stock.byMaterialGroup'), result: data?.by_material_group },
    { key: 'brand', title: t('stock.byMaterialBrand'), result: data?.by_material_brand },
  ] as const;

  /** The four categories plus the total, for any grouped stock table. */
  const stockColumns = (labelHeader: string) => [
    { key: 'label', header: labelHeader },
    { key: 'unrestricted_stock', header: t('stock.unrestricted') },
    { key: 'quality_inspection_stock', header: t('stock.qualityInspection') },
    { key: 'blocked_stock', header: t('stock.blocked') },
    { key: 'stock_in_transit', header: t('stock.inTransit') },
    { key: 'total_stock', header: t('stock.total') },
  ];

  // Material code is placed with the other identifying codes rather than at the
  // end: the question this table answers is "what is expiring", and the answer
  // is a material, not a location. The description sits beside the code because
  // a code alone does not tell an operator what is about to expire.
  const expiringColumns = [
    { key: 'plant_name', header: t('stock.plant') },
    { key: 'storage_location_name', header: t('stock.storageLocation') },
    { key: 'material_code', header: t('stock.materialCode') },
    { key: 'material_description', header: t('stock.materialDescription') },
    { key: 'material_group_name', header: t('stock.materialGroup') },
    { key: 'material_brand', header: t('stock.materialBrand') },
    { key: 'shelf_life_expiration_date', header: t('stock.expires') },
    { key: 'unrestricted_stock', header: t('stock.unrestricted') },
    { key: 'total_stock', header: t('stock.total') },
  ];

  return (
    <>
      <PageHeader
        title={t('stock.title')}
        // The convention is stated once at the top — that the figures are the
        // uploaded values themselves and the unit is in each label — so a
        // reader knows nothing was converted to produce them.
        description={`${t('stock.snapshotNote')} ${t('stock.unitNote')}`}
        actions={
          <ExportButtons
            reportName={t('stock.title')}
            rows={data?.by_plant.rows ?? []}
          />
        }
      />

      {/*
        The stock filter set, and nothing else. These are exactly the columns
        `vw_material_stock_detail` carries: Company -> Plant -> Storage
        Location for where a position is held, Material Group -> Material Brand
        -> Material for what it is, and the derived shelf-life status. The sales
        hierarchy below company, the customer, the sales force and the batch are
        all absent from that view, so the backend skips them — offering them
        here would advertise a filter that silently does nothing.

        The material levels are shared with every other page since revision
        0022; the plant chain and the shelf-life bucket are what remain
        stock-only.

        Same component, same URL state, same chips, same sticky bar as every
        other page; only the set differs. The period selector is off because
        stock carries no posting date.
      */}
      <GlobalFilterBar
        levels={[]}
        showIndependent={false}
        pageFilters={STOCK_FILTERS}
        showDate={false}
      />

      <QueryState
        isLoading={isLoading}
        error={error}
        onRetry={() => void refetch()}
        skeleton={<KpiSkeleton count={5} />}
      >
        <div className="space-y-4">
          {/*
            Five cards: the four categories as independent measures, then the
            total. Unrestricted leads because it is the only one that can be
            sold — the ordering is the point, not decoration.
          */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            {/* Green on both the name and the figure — the status standard, not
                a tone on the number alone. */}
            <StatCard
              label={t('stock.unrestricted')}
              value={formatStock(totals.unrestricted_stock)}
              statusKey="unrestricted_stock"
            />
            <StatCard
              label={t('stock.qualityInspection')}
              value={formatStock(totals.quality_inspection_stock)}
            />
            <StatCard
              label={t('stock.blocked')}
              value={formatStock(totals.blocked_stock)}
              tone={totals.blocked_stock ? 'danger' : 'default'}
            />
            <StatCard
              label={t('stock.inTransit')}
              value={formatStock(totals.stock_in_transit)}
            />
            <StatCard
              label={t('stock.total')}
              value={formatStock(totals.total_stock)}
            />
          </div>

          <ResultNotes notes={data?.summary.notes} />

          {/* --- Expiry ---------------------------------------------------- */}
          <Section
            title={t('stock.expiryAnalysis')}
            actions={
              <label className="flex items-center gap-2 text-xs text-slate-500">
                {t('stock.expiringWithin')}
                <select
                  className="input py-1 text-xs"
                  value={horizon ?? ''}
                  onChange={(event) =>
                    setHorizon(event.target.value ? Number(event.target.value) : undefined)
                  }
                >
                  <option value="">{t('stock.defaultHorizon')}</option>
                  {HORIZONS.map((days) => (
                    <option key={days} value={days}>
                      {t('stock.days', { count: days })}
                    </option>
                  ))}
                </select>
              </label>
            }
          >
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {(data?.expiry.rows ?? []).map((row) => (
                <StatCard
                  key={String(row.code)}
                  // The bucket names come from the translations without a unit,
                  // so it is attached here — these cards carry a stock figure
                  // like every other card on the page.
                  label={stockLabel(bucketCardName(t, String(row.code)))}
                  value={formatStock(row.total_stock)}
                  hint={t('stock.positions', { count: Number(row.row_count ?? 0) })}
                  // Expired and expiring-soon are coloured by the status
                  // standard, which takes the label with it; the other two
                  // buckets fall through to a plain tone on the figure.
                  statusKey={String(row.code)}
                  tone={BUCKET_TONE[String(row.code)] ?? 'default'}
                />
              ))}
            </div>
            <ResultNotes notes={data?.expiry.notes} />
          </Section>

          {/* --- The three breakdowns -------------------------------------- */}
          {breakdowns.map(({ key, title, result }) => (
            <Section key={key} title={title}>
              <CategoryBarChart
                data={result?.rows ?? []}
                xKey="label"
                yKey="total_stock"
                valueKind="stock"
                // The tooltip is a label and a number like everything else here,
                // so it takes the unit on the name rather than on the figure.
                valueLabel={t('stock.total')}
              />
              <DataTable
                // One arrangement per breakdown: the sections list the same measures
                // about different things.
                tableId={`stock.${key}`}
                rows={result?.rows ?? []}
                columns={stockColumns(title)}
                // Says stock, not "records", and names the filters rather than
                // a period the page no longer offers.
                emptyMessage={t('stock.noData')}
                // Searchable only for materials. Plants, storage locations and
                // material groups fit on a screen; materials do not, and
                // finding one in the list is the whole point of the section.
                searchable={key === 'material'}
                pageSize={15}
                rowKey={(row, index) => `${key}-${row.code ?? index}`}
              />
              <ResultNotes notes={result?.notes} />
            </Section>
          ))}

          {/* --- Positions at or past shelf life --------------------------- */}
          <Section title={t('stock.expiringPositions')}>
            <DataTable
              tableId="stock.expiring"
              rows={data?.expiring.rows ?? []}
              columns={expiringColumns}
              pageSize={25}
              emptyMessage={t('stock.nothingExpiring')}
              rowKey={(_row, index) => `exp-${index}`}
            />
            <ResultNotes notes={data?.expiring.notes} />
          </Section>
        </div>
      </QueryState>
    </>
  );
}
