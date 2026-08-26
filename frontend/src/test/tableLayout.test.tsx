/**
 * Column arrangement: the order and the widths a reader sets on a report table.
 *
 * Two things here are worth more than the rest. A remembered order must survive
 * the report changing shape — a column that disappears must not take a live one
 * with it, and a column the report gains must not be swallowed, because there is
 * no control anywhere that would bring back a column the order forgot. And a
 * table nobody has arranged must render exactly as it always did: the fixed
 * layout that makes widths exact is only switched on by the first resize.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../contexts/I18nContext';
import { reconcileOrder } from '../hooks/useTableLayout';
import { DataTable } from '../tables/DataTable';

const ROWS = [
  { label: 'Dhaka', net_sales: 15_000_000, achievement_percent: 31.4 },
  { label: 'Chattogram', net_sales: 7_200_000, achievement_percent: 28.1 },
];

const COLUMNS = [
  { key: 'label', header: 'Name' },
  { key: 'net_sales', header: 'Net Sales' },
  { key: 'achievement_percent', header: 'Achievement' },
];

function show(tableId?: string, columns = COLUMNS) {
  return render(
    <I18nProvider>
      <DataTable rows={ROWS} columns={columns} tableId={tableId} />
    </I18nProvider>,
  );
}

function headers() {
  return screen.getAllByRole('columnheader').map((cell) => cell.textContent?.trim());
}

function colWidths() {
  return Array.from(document.querySelectorAll('colgroup col')).map(
    (col) => (col as HTMLElement).style.width,
  );
}

function stored(tableId: string) {
  return JSON.parse(localStorage.getItem(`bi.table-layout.${tableId}`) ?? 'null');
}

/** Drag one header onto another, carrying the payload a browser would. */
function dragOnto(source: HTMLElement, target: HTMLElement) {
  const payload = new Map<string, string>();
  const dataTransfer = {
    effectAllowed: '',
    dropEffect: '',
    setData: (type: string, value: string) => payload.set(type, value),
    getData: (type: string) => payload.get(type) ?? '',
  };
  fireEvent.dragStart(source, { dataTransfer });
  fireEvent.dragOver(target, { dataTransfer });
  fireEvent.drop(target, { dataTransfer });
}

/** Pull a column's grab strip from one x position to another. */
function resizeBy(handle: HTMLElement, from: number, to: number) {
  fireEvent.pointerDown(handle, { clientX: from, pointerId: 1 });
  fireEvent.pointerMove(handle, { clientX: to, pointerId: 1 });
  fireEvent.pointerUp(handle, { clientX: to, pointerId: 1 });
}

describe('reconcileOrder', () => {
  it('drops a name the table no longer has', () => {
    expect(reconcileOrder(['b', 'gone', 'a'], ['a', 'b'])).toEqual(['b', 'a']);
  });

  // Appending would be safe but wrong: a column added in the middle of a report
  // would surface at the far right of every reader who had ever arranged it.
  it('puts a column the report has gained beside the one it follows', () => {
    expect(reconcileOrder(['c', 'a'], ['a', 'b', 'c'])).toEqual(['c', 'a', 'b']);
  });

  it('places a new first column first', () => {
    expect(reconcileOrder(['b'], ['a', 'b'])).toEqual(['a', 'b']);
  });

  it('keeps one entry per column when the stored order repeats one', () => {
    expect(reconcileOrder(['a', 'b', 'a'], ['a', 'b'])).toEqual(['a', 'b']);
  });
});

describe('column order', () => {
  it('moves a column and remembers where it was put', () => {
    const view = show('regions');
    expect(headers()).toEqual(['Name', 'Net Sales', 'Achievement']);

    fireEvent.click(screen.getByRole('button', { name: 'Columns' }));
    fireEvent.click(screen.getByRole('button', { name: 'Name: Move right' }));
    expect(headers()).toEqual(['Net Sales', 'Name', 'Achievement']);
    expect(stored('regions').order).toEqual([
      'net_sales',
      'label',
      'achievement_percent',
    ]);

    view.unmount();
    show('regions');
    expect(headers()).toEqual(['Net Sales', 'Name', 'Achievement']);
  });

  it('drops a dragged header onto the column it was dropped on', () => {
    show();
    const [first, , third] = screen.getAllByRole('columnheader');
    dragOnto(first.querySelector('span[draggable]') as HTMLElement, third);
    expect(headers()).toEqual(['Net Sales', 'Achievement', 'Name']);
  });

  it('reads a stale stored order without losing or repeating a column', () => {
    localStorage.setItem(
      'bi.table-layout.moved',
      JSON.stringify({ order: ['achievement_percent', 'retired_column', 'label'], widths: {} }),
    );
    show('moved', [...COLUMNS, { key: 'quantity', header: 'Quantity' }]);
    // The retired name is gone and the arranged columns keep their places. The
    // gained column follows the column it was declared after — Achievement —
    // and so travels with it rather than landing at whichever end the reader
    // happened to leave empty.
    expect(headers()).toEqual(['Achievement', 'Quantity', 'Name', 'Net Sales']);
  });

  it('falls back to the declared order when storage holds nonsense', () => {
    localStorage.setItem('bi.table-layout.broken', '{ not json');
    show('broken');
    expect(headers()).toEqual(['Name', 'Net Sales', 'Achievement']);
  });

  it('restores the declared order and forgets the entry on reset', () => {
    show('resettable');
    fireEvent.click(screen.getByRole('button', { name: 'Columns' }));
    fireEvent.click(screen.getByRole('button', { name: 'Achievement: Move left' }));
    expect(headers()).toEqual(['Name', 'Achievement', 'Net Sales']);

    fireEvent.click(screen.getByRole('button', { name: /Reset layout/ }));
    expect(headers()).toEqual(['Name', 'Net Sales', 'Achievement']);
    expect(localStorage.getItem('bi.table-layout.resettable')).toBeNull();
  });

  // An ad-hoc table — a chat answer, an upload's rejected rows — has no identity
  // to remember an arrangement under, and must not borrow one.
  it('arranges without a table id but stores nothing', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: 'Columns' }));
    fireEvent.click(screen.getByRole('button', { name: 'Name: Move right' }));
    expect(headers()).toEqual(['Net Sales', 'Name', 'Achievement']);
    expect(
      Object.keys(localStorage).filter((key) => key.startsWith('bi.table-layout')),
    ).toEqual([]);
  });
});

describe('column width', () => {
  it('leaves an unarranged table sizing itself to its content', () => {
    show();
    const table = document.querySelector('table') as HTMLElement;
    expect(table.style.tableLayout).toBe('');
    expect(table.className).toContain('min-w-max');
    expect(colWidths()).toEqual(['', '', '']);
  });

  it('pins the dragged column, freezes the rest and switches layout', () => {
    show('sized');
    resizeBy(screen.getByRole('separator', { name: /^Name:/ }), 0, 180);

    const table = document.querySelector('table') as HTMLElement;
    expect(table.style.tableLayout).toBe('fixed');
    expect(colWidths()[0]).toBe('180px');
    expect(stored('sized').widths.label).toBe(180);
    // The columns nobody touched were pinned at the width they already had, so
    // the switch to a fixed layout moved none of them.
    expect(stored('sized').widths.net_sales).toBeDefined();
  });

  it('stops at the minimum readable width', () => {
    show('narrow');
    resizeBy(screen.getAllByRole('separator')[0], 500, 0);
    expect(colWidths()[0]).toBe('60px');
  });

  it('gives a column back to the browser on a double click', () => {
    show('reverted');
    resizeBy(screen.getAllByRole('separator')[0], 0, 200);
    expect(colWidths()[0]).toBe('200px');

    fireEvent.doubleClick(screen.getAllByRole('separator')[0]);
    expect(colWidths()[0]).toBe('');
  });

  it('ignores a stored width too small to read', () => {
    localStorage.setItem(
      'bi.table-layout.tiny',
      JSON.stringify({ order: [], widths: { label: 4 } }),
    );
    show('tiny');
    expect(colWidths()[0]).toBe('');
  });
});
