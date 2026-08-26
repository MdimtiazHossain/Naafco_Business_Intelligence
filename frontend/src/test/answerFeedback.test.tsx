/** The thumbs-up/down control on an AI answer. */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AnswerFeedback } from '../components/AnswerFeedback';
import { I18nProvider } from '../contexts/I18nContext';
import { ThemeProvider } from '../contexts/ThemeContext';

const feedback = vi.fn();

vi.mock('../services', () => ({
  chatService: {
    feedback: (...args: unknown[]) => feedback(...args),
  },
}));

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ThemeProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </ThemeProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  feedback.mockReset();
  feedback.mockResolvedValue({
    message_id: 7,
    rating: 'UP',
    updated: false,
    sanitized: false,
  });
});

describe('AnswerFeedback', () => {
  it('sends the verdict with the message it belongs to', async () => {
    wrap(<AnswerFeedback messageId={7} />);
    fireEvent.click(screen.getByLabelText('Helpful'));
    await waitFor(() => expect(feedback).toHaveBeenCalledWith(7, 'UP', undefined));
  });

  it('asks what was expected only on a thumbs-down', async () => {
    wrap(<AnswerFeedback messageId={7} />);

    fireEvent.click(screen.getByLabelText('Helpful'));
    await waitFor(() => expect(feedback).toHaveBeenCalled());
    // A reader who liked the answer has nothing to correct, so the box that
    // would ask them to is never shown.
    expect(
      screen.queryByPlaceholderText(/What did you expect instead/),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Not helpful'));
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText(/What did you expect instead/),
      ).toBeInTheDocument(),
    );
  });

  it('sends the note with the thumbs-down it belongs to', async () => {
    wrap(<AnswerFeedback messageId={7} />);
    fireEvent.click(screen.getByLabelText('Not helpful'));

    const box = await screen.findByPlaceholderText(/What did you expect instead/);
    fireEvent.change(box, { target: { value: 'I wanted it by region' } });
    fireEvent.click(screen.getByText('Send'));

    await waitFor(() =>
      expect(feedback).toHaveBeenLastCalledWith(7, 'DOWN', 'I wanted it by region'),
    );
  });

  it('shows the verdict this reader already recorded', () => {
    wrap(<AnswerFeedback messageId={7} initial="DOWN" />);
    // Replayed from history: the thumb they actually pressed, not a blank
    // control that would invite a second vote.
    expect(screen.getByLabelText('Not helpful')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByLabelText('Helpful')).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('says so when the note was edited on the way in', async () => {
    feedback.mockResolvedValue({
      message_id: 7,
      rating: 'DOWN',
      updated: false,
      sanitized: true,
    });
    wrap(<AnswerFeedback messageId={7} />);
    fireEvent.click(screen.getByLabelText('Not helpful'));

    // Said plainly rather than storing something other than what was typed.
    await waitFor(() =>
      expect(
        screen.getByText(/instruction-like text removed/),
      ).toBeInTheDocument(),
    );
  });

  it('puts the thumb back when the request fails', async () => {
    feedback.mockRejectedValue(new Error('offline'));
    wrap(<AnswerFeedback messageId={7} />);

    fireEvent.click(screen.getByLabelText('Helpful'));
    await waitFor(() =>
      expect(screen.getByText(/Couldn't save that/)).toBeInTheDocument(),
    );
    expect(screen.getByLabelText('Helpful')).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });
});
