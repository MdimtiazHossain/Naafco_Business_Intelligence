/**
 * A minimal stand-in for `maplibre-gl` for tests that are not about drawing.
 *
 * It accepts everything the renderer asks of a map and records nothing
 * beyond what a page needs to mount; the settings tests import it so the map
 * page can render while they exercise the drawer and its editors.
 */

export class FakeMap {
  /**
   * Every map built since the last reset, newest last.
   *
   * A test that asks what the page told the map to do needs a handle on the
   * map, and the page owns its own instance. Reset in `beforeEach` by the
   * tests that read it; the settings tests never look.
   */
  static instances: FakeMap[] = [];

  static get last(): FakeMap | undefined {
    return FakeMap.instances[FakeMap.instances.length - 1];
  }

  handlers = new Map<string, ((event: unknown) => void)[]>();
  sources = new Map<string, { setData: (data: unknown) => void }>();
  layers = new Map<string, Record<string, unknown>>();
  canvas = { style: { cursor: '' } };
  options: Record<string, unknown>;

  constructor(options: Record<string, unknown>) {
    this.options = options;
    FakeMap.instances.push(this);
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

  /**
   * Registered icon images, by name.
   *
   * The demarcation renderer draws shapes through `symbol` layers, whose icons
   * must exist on the map before the layer references them. jsdom has no 2D
   * canvas context, so the real rasterisation returns nothing here — what the
   * tests can still pin is *which* images the renderer asked for, which is the
   * same thing the rest of this stub records: what the page asks the map to do,
   * never what WebGL made of it.
   */
  images = new Map<string, unknown>();

  addImage(id: string, image: unknown) {
    this.images.set(id, image);
  }

  hasImage(id: string) {
    return this.images.has(id);
  }

  removeImage(id: string) {
    this.images.delete(id);
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

  /**
   * `beforeId` is recorded, not ignored.
   *
   * It is the whole mechanism by which the administrative backdrop stays
   * *underneath* the points, so a test that could not see it could not tell a
   * correct stacking order from a broken one.
   */
  addLayer(spec: Record<string, unknown>, beforeId?: string) {
    this.layers.set(spec.id as string, { ...spec, beforeId });
    return this;
  }

  /** MapLibre's own liveness check; the renderer uses it before adding layers. */
  getStyle() {
    return { layers: [...this.layers.values()] };
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
