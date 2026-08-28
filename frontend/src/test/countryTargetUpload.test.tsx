/**
 * The country-target upload panel.
 *
 * **Nothing is applied by choosing a file.** The preview names every row it
 * would change first — this is the figure a whole sales force is measured on.
 *
 * **The Apply button is absent while anything is rejected, not present and
 * refusing.** A control whose only outcome is a refusal teaches people to
 * ignore controls, and the upload is all-or-nothing: a country target loaded in
 * part is short by whatever the rest was worth.
 *
 * **A blank cell and a refused cell look different.** A blank is "nothing
 * stated here" and reads as a dash with a tooltip; a refused one shows the text
 * the file actually contained, because telling somebody `12,5OO` was rejected
 * is useful where "row 4 was rejected" is not.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CountryTargetUpload } from '../components/targetmgmt/CountryTargetUpload';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type {
  TargetUploadPreview,
  TargetUploadRow,
} from '../types/api';

function wrap(node: React.ReactNode) {
  return render(
    <I18nProvider>
      <ThemeProvider>{node}</ThemeProvider>
    </I18nProvider>,
  );
}

function row(overrides: Partial<TargetUploadRow> = {}): TargetUploadRow {
  return {
    row_number: 2,
    material_code: 'SKU001',
    raw_volume: '150000',
    volume: 150000,
    current_volume: 120000,
    status: 'CHANGED',
    error: null,
    ...overrides,
  };
}

function preview(
  overrides: Partial<TargetUploadPreview> = {},
): TargetUploadPreview {
  return {
    plan: {} as never,
    version: {} as never,
    upload_token: 'abc-123.csv',
    file_name: 'country.csv',
    editable: true,
    rows: [row(), row({ row_number: 3, material_code: 'SKU002',
                        volume: 45000, current_volume: null, status: 'NEW' })],
    counts: {
      read: 2, rejected: 0, new: 1, changed: 1, unchanged: 0, skipped: 0,
      untouched: 0,
    },
    applicable: true,
    headers: { material: 'Material Code', volume: 'Target Volume' },
    notes: [],
    ...overrides,
  };
}

function show(props: Partial<Parameters<typeof CountryTargetUpload>[0]> = {}) {
  const handlers = {
    onTemplate: vi.fn(),
    onFile: vi.fn(),
    onApply: vi.fn(),
    onDismiss: vi.fn(),
  };
  wrap(
    <CountryTargetUpload
      preview={null}
      result={null}
      editable
      canUpload
      busy={false}
      error={null}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe('CountryTargetUpload', () => {
  it('offers the template and the file chooser', () => {
    show();
    expect(screen.getByText('Download template')).toBeTruthy();
    expect(screen.getByText('Choose a file')).toBeTruthy();
  });

  it('draws nothing at all for a reader who cannot upload', () => {
    show({ canUpload: false });
    expect(screen.queryByText('Download template')).toBeNull();
  });

  it('offers the template but no chooser on a frozen version', () => {
    show({ editable: false });
    expect(screen.getByText('Download template')).toBeTruthy();
    expect(screen.queryByText('Choose a file')).toBeNull();
  });

  it('shows what a clean file would change, and offers to apply it', () => {
    const { onApply } = show({ preview: preview() });
    expect(screen.getByText(/2 rows read/)).toBeTruthy();
    fireEvent.click(screen.getByText('Apply 2 rows'));
    expect(onApply).toHaveBeenCalled();
  });

  it('offers no apply while anything is rejected', () => {
    show({
      preview: preview({
        rows: [row({ volume: null, raw_volume: '12,5OO', status: 'REJECTED',
                     error: 'Target Volume for SKU001 is not a number.' })],
        counts: { read: 1, rejected: 1, new: 0, changed: 0, unchanged: 0,
                  skipped: 0, untouched: 0 },
        applicable: false,
        notes: ['1 row cannot be used, so nothing will be applied.'],
      }),
    });
    expect(screen.queryByText(/^Apply /)).toBeNull();
    expect(screen.getByText(/nothing will be applied/)).toBeTruthy();
  });

  it('shows a refused cell back verbatim', () => {
    show({
      preview: preview({
        rows: [row({ volume: null, raw_volume: '12,5OO', status: 'REJECTED',
                     error: 'Target Volume for SKU001 is not a number.' })],
        counts: { read: 1, rejected: 1, new: 0, changed: 0, unchanged: 0,
                  skipped: 0, untouched: 0 },
        applicable: false,
      }),
    });
    expect(screen.getByText('12,5OO')).toBeTruthy();
    expect(screen.getByText(/is not a number/)).toBeTruthy();
  });

  it('distinguishes a blank cell from a refused one', () => {
    show({
      preview: preview({
        rows: [row({ volume: null, raw_volume: null, current_volume: 5000,
                     status: 'SKIPPED' })],
        counts: { read: 1, rejected: 0, new: 0, changed: 0, unchanged: 0,
                  skipped: 1, untouched: 0 },
        applicable: false,
      }),
    });
    const table = screen.getByRole('table');
    const titles = within(table)
      .getAllByText('—')
      .map((node) => node.getAttribute('title'))
      .join(' ');
    expect(titles).toContain('never written as zero');
    expect(within(table).getByText('skipped')).toBeTruthy();
  });

  it('reads the file the moment one is chosen, without applying it', () => {
    const { onFile, onApply } = show();
    const file = new File(['Material Code,Target Volume\nSKU001,1\n'],
                          'country.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('Choose a file'), {
      target: { files: [file] },
    });
    expect(onFile).toHaveBeenCalledWith(file);
    expect(onApply).not.toHaveBeenCalled();
  });

  it('reports what was applied, including what it did not touch', () => {
    show({
      result: {
        created: 1, updated: 1, rows_read: 2, skipped: 0, untouched: 4,
        file_name: 'country.csv',
      },
    });
    expect(screen.getByText(/4 material\(s\) already on this version/)).toBeTruthy();
  });

  it('lets a preview be dismissed without applying it', () => {
    const { onDismiss, onApply } = show({ preview: preview() });
    fireEvent.click(screen.getByText('Cancel'));
    expect(onDismiss).toHaveBeenCalled();
    expect(onApply).not.toHaveBeenCalled();
  });
});
