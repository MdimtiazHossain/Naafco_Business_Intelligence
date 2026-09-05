/**
 * Turning a declared shape into something MapLibre can draw at a point.
 *
 * A circle layer takes its colour from a paint property, so the analysis map
 * needs no images at all. A shape does not: MapLibre draws one through a
 * `symbol` layer, and a symbol layer's icon has to be an image registered on
 * the map by name. This module is the bridge — it takes the `{key, path,
 * viewbox}` the server publishes and produces an `ImageData` the map can hold.
 *
 * **Pre-coloured images rather than SDF.** MapLibre can recolour a single
 * greyscale image per shape if it is registered with `{sdf: true}` and
 * `icon-color` is set, which would be one image per shape instead of one per
 * shape-and-colour. It is not used here: `sdf` treats the alpha channel as a
 * *signed distance field*, and a plain canvas rasterisation is not one — the
 * result has hard, badly-scaling edges, worst at exactly the small sizes a
 * demarcation map uses. Colouring at rasterisation time costs an image per
 * (shape, colour) pair, and that pair count is bounded by the number of
 * visible layers, which is at most eleven. A `Map` keyed on the pair means two
 * layers sharing a colour share an image.
 *
 * **Images live and die with the style.** `setStyle` — which is what a theme
 * switch does — discards registered images exactly as it discards layers, so
 * every image is re-added when the renderer re-adds its layers. `hasImage`
 * before `addImage` keeps that idempotent.
 *
 * A derived coordinate is drawn hollow: the same path, stroked rather than
 * filled, so a centroid is never mistaken for a position somebody surveyed.
 * Two images per pair, therefore, and the renderer picks between them with a
 * data-driven expression rather than a second layer.
 */

import type { Map as MapLibreInstance } from 'maplibre-gl';
import type { MapShape } from '../../types/api';

/**
 * How many device pixels a shape is rasterised into.
 *
 * Larger than any size the map draws it at, because scaling an icon down is
 * clean and scaling one up is not. 64 is comfortably above the largest
 * `icon-size` this map uses and small enough that eleven of them cost nothing.
 */
const RASTER_SIZE = 64;

/** Suffix on the image name of the hollow variant. */
const DERIVED_SUFFIX = 'outline';

export function shapeImageId(shape: string, color: string, derived: boolean): string {
  return `shape-${shape}-${color.replace('#', '')}${derived ? `-${DERIVED_SUFFIX}` : ''}`;
}

/**
 * Rasterise one shape at one colour.
 *
 * Returns `null` when the browser gives no 2D context — jsdom under test does
 * not, and a renderer that threw there would make every map test a canvas
 * test. The caller treats a missing image as "draw nothing extra", which is
 * what the fallback in `useShapeRenderer` handles.
 */
export function rasteriseShape(
  shape: MapShape,
  color: string,
  { derived = false, size = RASTER_SIZE }: { derived?: boolean; size?: number } = {},
): ImageData | null {
  if (typeof document === 'undefined') return null;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext('2d');
  if (!context) return null;

  // The path is drawn on a `viewbox`-unit square; scale it to the raster and
  // leave a margin so a stroked edge is not clipped by the image bounds.
  const margin = size * 0.06;
  const scale = (size - margin * 2) / shape.viewbox;
  context.setTransform(scale, 0, 0, scale, margin, margin);

  let path: Path2D;
  try {
    path = new Path2D(shape.path);
  } catch {
    // A path the browser cannot parse is a server-side mistake, and drawing a
    // wrong shape silently would be worse than drawing none.
    return null;
  }

  if (derived) {
    context.globalAlpha = 1;
    context.fillStyle = color;
    // A faint fill so the point is still clickable across its whole area, with
    // the outline carrying the colour. Hollow, not invisible.
    context.globalAlpha = 0.18;
    context.fill(path);
    context.globalAlpha = 1;
    context.strokeStyle = color;
    context.lineWidth = 2.2 / scale;
    context.stroke(path);
  } else {
    context.fillStyle = color;
    context.fill(path);
    context.strokeStyle = '#ffffff';
    context.lineWidth = 1.4 / scale;
    context.stroke(path);
  }

  try {
    return context.getImageData(0, 0, size, size);
  } catch {
    return null;
  }
}

/**
 * The two image ids for one (shape, colour) pair.
 *
 * Deterministic and computed without touching the map, so a layer can name its
 * icon before — or without — the image existing.
 */
export function shapeImageIds(shapeKey: string, color: string):
{ solid: string; outline: string } {
  return {
    solid: shapeImageId(shapeKey, color, false),
    outline: shapeImageId(shapeKey, color, true),
  };
}

/**
 * Register both variants of one (shape, colour) pair, if they can be made.
 *
 * **The ids are returned whether or not the images were registered**, and the
 * caller adds its layer either way. The first version of this returned `null`
 * when rasterisation failed and the renderer skipped the layer — which meant a
 * browser that could not give a 2D context drew no points, no source and no
 * clusters, silently. Naming an icon that does not exist costs a console
 * warning and an invisible symbol; skipping the layer costs the data.
 *
 * MapLibre also tolerates an image arriving after the layer that names it, so
 * this ordering is the ordinary one rather than a concession.
 */
export function ensureShapeImages(
  map: MapLibreInstance,
  shapes: MapShape[],
  shapeKey: string,
  color: string,
): { solid: string; outline: string } {
  const ids = shapeImageIds(shapeKey, color);
  const shape = shapes.find((candidate) => candidate.key === shapeKey);
  if (!shape) return ids;

  ([[ids.solid, false], [ids.outline, true]] as const).forEach(([id, derived]) => {
    if (map.hasImage(id)) return;
    const image = rasteriseShape(shape, color, { derived });
    if (image) map.addImage(id, image, { pixelRatio: 2 });
  });
  return ids;
}
