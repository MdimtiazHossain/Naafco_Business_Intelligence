/**
 * Render a server-produced marker SVG.
 *
 * The markup comes from the backend renderer, which is the single source of
 * truth for what a marker looks like — the designer canvas, this thumbnail, the
 * map legend and the marker the map draws are all the same bytes.
 *
 * `dangerouslySetInnerHTML` is used deliberately and is safe here for reasons
 * that hold at the boundary, not by convention: the SVG is composed on the
 * server from a validated `MarkerDefinition` (enums, hex colours, bounded
 * numbers), any uploaded artwork inside it has passed the whitelist sanitiser,
 * and every text value is XML-escaped on the way out. Nothing user-supplied
 * reaches this component un-vetted.
 */

interface MarkerPreviewProps {
  svg: string | null | undefined;
  /** Box to fit the marker into, in pixels. */
  size?: number;
  className?: string;
  title?: string;
}

export function MarkerPreview({ svg, size = 44, className = '', title }: MarkerPreviewProps) {
  if (!svg) {
    return (
      <div
        className={`flex items-center justify-center rounded border border-dashed border-slate-300 text-[10px] text-slate-400 dark:border-slate-700 ${className}`}
        style={{ width: size, height: size }}
        aria-label="No preview"
      >
        —
      </div>
    );
  }

  return (
    <div
      className={`flex items-center justify-center ${className}`}
      style={{ width: size, height: size }}
      title={title}
      role="img"
      aria-label={title ?? 'Marker preview'}
      // The wrapper constrains the SVG, which carries its own intrinsic size.
      dangerouslySetInnerHTML={{
        __html: svg.replace(
          '<svg',
          `<svg style="max-width:${size}px;max-height:${size}px;width:100%;height:100%"`,
        ),
      }}
    />
  );
}
