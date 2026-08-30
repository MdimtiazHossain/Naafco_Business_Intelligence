/**
 * The country-target upload: the header buttons and the preview panel.
 *
 * **They are two components because they belong in two places.** The buttons go
 * in the Country Target section's own header, beside the figures they load; the
 * preview goes above the grid and only when there is something to say. Both
 * sat below a 245-row table once, where nobody found them — a control nobody
 * can find is the same as a control that is not there.
 *
 * **Nothing is applied by choosing a file.** Preview, then apply.
 *
 * **The Apply button is absent while anything is rejected**, not present and
 * refusing: the upload is all-or-nothing, because a country target loaded in
 * part is short by whatever the rest was worth.
 *
 * **A blank cell and a refused cell look different.** A blank is "nothing
 * stated here" and reads as a dash with a tooltip; a refused one shows the text
 * the file actually contained.
 */

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  CountryTargetUpload,
  CountryTargetUploadActions,
} from '../components/targetmgmt/CountryTargetUpload';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';
import type { TargetUploadPreview, TargetUploadRow } from '../types/api';

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

// ---------------------------------------------------------------------------
// The header buttons
// ---------------------------------------------------------------------------

function showActions(
  props: Partial<Parameters<typeof CountryTargetUploadActions>[0]> = {},
) {
  const handlers = { onTemplate: vi.fn(), onFile: vi.fn() };
  wrap(
    <CountryTargetUploadActions
      editable
      canUpload
      busy={false}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe('CountryTargetUploadActions', () => {
  it('offers the template and the file chooser', () => {
    showActions();
    expect(screen.getByText('Download template')).toBeTruthy();
    expect(screen.getByText('Choose a file')).toBeTruthy();
  });

  it('draws nothing at all for a reader who cannot upload', () => {
    showActions({ canUpload: false });
    expect(screen.queryByText('Download template')).toBeNull();
    expect(screen.queryByText('Choose a file')).toBeNull();
  });

  it('keeps the template but drops the chooser on a frozen version', () => {
    // Reading what the target *is* is not editing it.
    showActions({ editable: false });
    expect(screen.getByText('Download template')).toBeTruthy();
    expect(screen.queryByText('Choose a file')).toBeNull();
  });

  it('reads the file the moment one is chosen', () => {
    const { onFile } = showActions();
    const file = new File(['Material Code,Target Volume\nSKU001,1\n'],
                          'country.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByLabelText('Choose a file'), {
      target: { files: [file] },
    });
    expect(onFile).toHaveBeenCalledWith(file);
  });

  it('downloads the template without touching the file input', () => {
    const { onTemplate, onFile } = showActions();
    fireEvent.click(screen.getByText('Download template'));
    expect(onTemplate).toHaveBeenCalled();
    expect(onFile).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The preview panel
// ---------------------------------------------------------------------------

function showPanel(
  props: Partial<Parameters<typeof CountryTargetUpload>[0]> = {},
) {
  const handlers = { onApply: vi.fn(), onDismiss: vi.fn() };
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
  it('draws nothing when there is nothing to report', () => {
    // No empty panel waiting on the ordinary Country Target screen.
    const { container } = render(
      <I18nProvider>
        <ThemeProvider>
          <CountryTargetUpload
            preview={null} result={null} editable canUpload busy={false}
            error={null} onApply={vi.fn()} onDismiss={vi.fn()}
          />
        </ThemeProvider>
      </I18nProvider>,
    );
    expect(container.textContent).toBe('');
  });

  it('shows what a clean file would change, and offers to apply it', () => {
    const { onApply } = showPanel({ preview: preview() });
    expect(screen.getByText(/2 rows read/)).toBeTruthy();
    fireEvent.click(screen.getByText('Apply 2 rows'));
    expect(onApply).toHaveBeenCalled();
  });

  it('offers no apply while anything is rejected', () => {
    showPanel({
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
    showPanel({
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
    showPanel({
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

  it('reports what was applied, including what it did not touch', () => {
    showPanel({
      result: {
        created: 1, updated: 1, rows_read: 2, skipped: 0, untouched: 4,
        file_name: 'country.csv',
      },
    });
    expect(screen.getByText(/4 material\(s\) already on this version/)).toBeTruthy();
  });

  it('shows an error on its own, with no preview', () => {
    showPanel({ error: 'That file could not be read.' });
    expect(screen.getByText('That file could not be read.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('lets a preview be dismissed without applying it', () => {
    const { onDismiss, onApply } = showPanel({ preview: preview() });
    fireEvent.click(screen.getByText('Cancel'));
    expect(onDismiss).toHaveBeenCalled();
    expect(onApply).not.toHaveBeenCalled();
  });
});
