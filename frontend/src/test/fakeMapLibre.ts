/**
 * A minimal stand-in for `maplibre-gl` for tests that are not about drawing.
 *
 * It accepts everything the renderer asks of a map and records nothing
 * beyond what a page needs to mount; the settings tests import it so the map
 * page can render while they exercise the drawer and its editors.
 */

export class FakeMap {
  handlers = new Map<string, ((event: unknown) => void)[]>();
  sources = new Map<string, { setData: (data: unknown) => void }>();
  layers = new Map<string, Record<string, unknown>>();
  canvas = { style: { cursor: '' } };
  options: Record<string, unknown>;

  constructor(options: Record<string, unknown>) {
    this.options = options;
    queueMicrotask(() => {
      this.fire('style.load');
      this.fire('load');
    });
  }

  on(event: string, handler: (event: unknown) => void) {
    this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
    return this;
  }

  off(event: string, handler: (event: unknown) => void) {
    this.handlers.set(event, (this.handlers.get(event) ?? []).filter((h) => h !== handler));
    return this;
  }

  fire(event: string, payload: unknown = {}) {
    (this.handlers.get(event) ?? []).forEach((handler) => handler(payload));
  }

  addControl() {
    return this;
  }

  remove() {}

  setStyle() {}

  jumpTo() {}

  easeTo() {}

  fitBounds() {}

  isStyleLoaded() {
    return true;
  }

  addSource(id: string) {
    this.sources.set(id, { setData: () => undefined });
  }

  getSource(id: string) {
    return this.sources.get(id);
  }

  removeSource(id: string) {
    this.sources.delete(id);
  }

  addLayer(spec: Record<string, unknown>) {
    this.layers.set(spec.id as string, spec);
  }

  getLayer(id: string) {
    return this.layers.get(id);
  }

  removeLayer(id: string) {
    this.layers.delete(id);
  }

  moveLayer() {}

  setPaintProperty() {}

  setLayoutProperty() {}

  setFilter() {}

  setLayerZoomRange() {}

  queryRenderedFeatures() {
    return [];
  }

  getCanvas() {
    return this.canvas;
  }
}

/** What `vi.mock('maplibre-gl', …)` should return. */
export function mapLibreModule() {
  return {
    Map: FakeMap,
    NavigationControl: class {},
    FullscreenControl: class {},
    AttributionControl: class {},
    setWorkerUrl: () => undefined,
  };
}
