"""The built-in icon library.

Every icon is a hand-authored SVG path drawn on a 24×24 grid, in the categories
the specification asks for. They are deliberately *not* lifted from a third-party
icon pack: the server renders the marker SVG that the legend and the map both
use, so the geometry has to live on the server, and copying a licensed pack's
path data into the database layer would attach that pack's licence to the
warehouse. Simple geometric glyphs are all a 24px marker can show anyway.

`lucide-react` is already a project dependency and stays in use for ordinary
interface chrome. It is not used for marker geometry, so a designer preview and
the rendered marker can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Every path is authored in this box and scaled by the renderer.
ICON_VIEWBOX = 24.0

CATEGORY_BUSINESS = "Business"
CATEGORY_WAREHOUSE = "Warehouse"
CATEGORY_CUSTOMER = "Customer"
CATEGORY_PERSON = "Person"
CATEGORY_TRANSPORT = "Transport"
CATEGORY_AGRICULTURE = "Agriculture"
CATEGORY_OFFICE = "Office"
CATEGORY_LOCATION = "Location"
CATEGORY_SALES = "Sales"
CATEGORY_WARNING = "Warning"
CATEGORY_ANALYTICS = "Analytics"

CATEGORIES: tuple[str, ...] = (
    CATEGORY_BUSINESS, CATEGORY_WAREHOUSE, CATEGORY_CUSTOMER, CATEGORY_PERSON,
    CATEGORY_TRANSPORT, CATEGORY_AGRICULTURE, CATEGORY_OFFICE, CATEGORY_LOCATION,
    CATEGORY_SALES, CATEGORY_WARNING, CATEGORY_ANALYTICS,
)


@dataclass(frozen=True)
class Icon:
    """One glyph, as a filled SVG path on a 24×24 grid."""

    key: str
    label: str
    category: str
    path: str
    #: True when the glyph reads better as a stroke than a fill.
    stroked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "path": self.path,
            "stroked": self.stroked,
            "viewbox": ICON_VIEWBOX,
        }


def _i(key: str, label: str, category: str, path: str, stroked: bool = False) -> Icon:
    return Icon(key, label, category, " ".join(path.split()), stroked)


ICONS: tuple[Icon, ...] = (
    # --- Business ---------------------------------------------------------
    _i("building", "Building", CATEGORY_BUSINESS,
       "M4 21V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v16H4Zm3-13h3v3H7V8Zm0 5h3v3H7v-3Z"
       "M16 21V10h4v11h-4Zm1-8h2v2h-2v-2Z"),
    _i("factory", "Factory", CATEGORY_BUSINESS,
       "M3 21V11l5 3V11l5 3V11l5 3V6h3v15H3Zm3-3h3v-2H6v2Zm5 0h3v-2h-3v2Z"),
    _i("briefcase", "Briefcase", CATEGORY_BUSINESS,
       "M9 4h6a2 2 0 0 1 2 2v1h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8"
       "a1 1 0 0 1 1-1h3V6a2 2 0 0 1 2-2Zm0 3h6V6H9v1Z"),
    # --- Warehouse --------------------------------------------------------
    _i("warehouse", "Warehouse", CATEGORY_WAREHOUSE,
       "M12 3 2 8v13h4v-8h12v8h4V8L12 3Zm-4 12h8v6H8v-6Z"),
    _i("box", "Box", CATEGORY_WAREHOUSE,
       "M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 2.3L18.5 8 12 11.6 5.5 8 12 4.3Z"
       "M5 9.6l6 3.4v6.6l-6-3.3V9.6Zm14 0v6.7l-6 3.3V13l6-3.4Z"),
    _i("pallet", "Pallet", CATEGORY_WAREHOUSE,
       "M3 15h18v2H3v-2Zm0 3h18v2H3v-2ZM6 5h5v8H6V5Zm7 0h5v8h-5V5Z"),
    # --- Customer ---------------------------------------------------------
    _i("store", "Store", CATEGORY_CUSTOMER,
       "M4 4h16l1.5 5A2.5 2.5 0 0 1 19 12v8H5v-8a2.5 2.5 0 0 1-2.5-3L4 4Z"
       "M9 14h6v6H9v-6Z"),
    _i("shop", "Shop Front", CATEGORY_CUSTOMER,
       "M3 9 5 4h14l2 5v2H3V9Zm1 4h16v8h-6v-5H10v5H4v-8Z"),
    _i("cart", "Cart", CATEGORY_CUSTOMER,
       "M3 4h2.2l2.6 10.5A2 2 0 0 0 9.7 16h8.1a2 2 0 0 0 1.9-1.4L21.5 8H7"
       "M9.5 20a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Zm8 0a1.5 1.5 0 1 0 0-3 1.5 "
       "1.5 0 0 0 0 3Z"),
    # --- Person -----------------------------------------------------------
    _i("person", "Person", CATEGORY_PERSON,
       "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 2c-4.4 0-8 2.4-8 5.3V21h16v-1.7"
       "c0-2.9-3.6-5.3-8-5.3Z"),
    _i("people", "Team", CATEGORY_PERSON,
       "M8 12a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm8 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
       "M2 20v-1.4C2 16 5 14 8 14s6 2 6 4.6V20H2Zm14 0v-1.6c0-1.4-.6-2.6-1.5-3.5"
       "1 -.2 1-.4 1.5-.4 2.8 0 6 1.8 6 4.1V20h-6Z"),
    _i("badge-person", "Field Officer", CATEGORY_PERSON,
       "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Zm0 6a2.5 2.5 0 1 1 0 5"
       " 2.5 2.5 0 0 1 0-5Zm0 6.5c2 0 3.8 1 3.8 2.3V18H8.2v-1.2c0-1.3 1.8-2.3 3.8-2.3Z"),
    # --- Transport --------------------------------------------------------
    _i("truck", "Truck", CATEGORY_TRANSPORT,
       "M2 6h11v9H2V6Zm12 3h4l3 3.5V15h-7V9ZM6.5 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"
       "m11 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"),
    _i("van", "Delivery Van", CATEGORY_TRANSPORT,
       "M3 7h9v8H3V7Zm10 2h3.5L20 13v2h-7V9ZM7 19a1.8 1.8 0 1 0 0-3.6A1.8 1.8 0 0 0 7 19Z"
       "m10 0a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6Z"),
    _i("motorbike", "Motorbike", CATEGORY_TRANSPORT,
       "M5 19a3.2 3.2 0 1 0 0-6.4A3.2 3.2 0 0 0 5 19Zm14 0a3.2 3.2 0 1 0 0-6.4"
       "A3.2 3.2 0 0 0 19 19ZM8 14l3-5h4l-2-3h4"),
    # --- Agriculture ------------------------------------------------------
    _i("plant", "Plant", CATEGORY_AGRICULTURE,
       "M12 21v-7m0 0c0-3.3-2.7-6-6-6 0 3.3 2.7 6 6 6Zm0 0c0-3.9 3.1-7 7-7 0 3.9-3.1 7-7 7Z"),
    _i("wheat", "Crop", CATEGORY_AGRICULTURE,
       "M12 21V9m0 0 3-3-3-3-3 3 3 3Zm0 4 3.5-3-3.5-2.5L8.5 10 12 13Zm0 4.5 3.5-3"
       "-3.5-2.5-3.5 2.5 3.5 3Z"),
    _i("tractor", "Tractor", CATEGORY_AGRICULTURE,
       "M4 6h6l1 5h6v4H6l-2-9Zm3 14a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm11 0a2.5 "
       "2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z"),
    # --- Office -----------------------------------------------------------
    _i("office", "Office", CATEGORY_OFFICE,
       "M5 21V3h9v18H5Zm2-15h2v2H7V6Zm3 0h2v2h-2V6ZM7 10h2v2H7v-2Zm3 0h2v2h-2v-2Z"
       "M16 21V9h4v12h-4Z"),
    _i("desk", "Desk", CATEGORY_OFFICE,
       "M3 6h18v2H3V6Zm2 3h4v10H7v-8H5V9Zm10 0h4v2h-2v8h-2V9Z"),
    _i("document", "Document", CATEGORY_OFFICE,
       "M6 2h8l4 4v16H6V2Zm8 1.5V7h3.5L14 3.5ZM8 11h8v1.6H8V11Zm0 3.4h8V16H8v-1.6Z"),
    # --- Location ---------------------------------------------------------
    _i("pin", "Map Pin", CATEGORY_LOCATION,
       "M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7Zm0 9.5A2.5 2.5 0 1 1 "
       "12 6.5a2.5 2.5 0 0 1 0 5Z"),
    _i("globe", "Region", CATEGORY_LOCATION,
       "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 2c1.6 0 3.2 3.3 3.2 8S13.6 20 12 20"
       "s-3.2-3.3-3.2-8S10.4 4 12 4ZM2.6 10h18.8v4H2.6v-4Z"),
    _i("flag", "Territory", CATEGORY_LOCATION,
       "M6 2v20h2v-8h9l-2-4 2-4H8V2H6Z"),
    _i("compass", "Zone", CATEGORY_LOCATION,
       "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm4 6-2.5 6L7 16.5 9.5 10.5 16 8Z"),
    # --- Sales ------------------------------------------------------------
    _i("tag", "Sales Tag", CATEGORY_SALES,
       "M11 2h9v9l-9.5 9.5a2 2 0 0 1-2.8 0l-6.2-6.2a2 2 0 0 1 0-2.8L11 2Zm5.5 3.5"
       "a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Z"),
    _i("coins", "Coins", CATEGORY_SALES,
       "M9 4c3.9 0 7 1.3 7 3s-3.1 3-7 3-7-1.3-7-3 3.1-3 7-3Zm7 6.5c0 1.7-3.1 3-7 3"
       "s-7-1.3-7-3V14c0 1.7 3.1 3 7 3s7-1.3 7-3v-3.5Z M22 12c0 1.7-2.4 3-5.5 3v-2"
       "c1.9 0 3.5-.6 3.5-1h2Z"),
    _i("receipt", "Invoice", CATEGORY_SALES,
       "M5 2h14v20l-2.3-1.6L14.4 22l-2.4-1.6L9.6 22l-2.3-1.6L5 22V2Zm3 5h8v2H8V7Zm0 4h8"
       "v2H8v-2Z"),
    # --- Warning ----------------------------------------------------------
    _i("warning", "Warning", CATEGORY_WARNING,
       "M12 2 1 21h22L12 2Zm-1 7h2v6h-2V9Zm0 8h2v2h-2v-2Z"),
    _i("alert-circle", "Alert", CATEGORY_WARNING,
       "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-1 5h2v7h-2V7Zm0 9h2v2h-2v-2Z"),
    _i("low-stock", "Low Stock", CATEGORY_WARNING,
       "M3 4h18v4H3V4Zm2 6h14v10H5V10Zm3 3v4h8v-4H8Z"),
    # --- Analytics --------------------------------------------------------
    _i("chart-bar", "Performance", CATEGORY_ANALYTICS,
       "M4 20V10h4v10H4Zm6 0V4h4v16h-4Zm6 0v-7h4v7h-4Z"),
    _i("trend-up", "Growth", CATEGORY_ANALYTICS,
       "M3 17l6-6 4 4 8-8v5h2V3h-9v2h5l-6 6-4-4-7 7 1.4 1.4Z"),
    _i("target", "Target", CATEGORY_ANALYTICS,
       "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 4a6 6 0 1 1 0 12 6 6 0 0 1 0-12Z"
       "m0 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5Z"),
)

ICON_BY_KEY: dict[str, Icon] = {icon.key: icon for icon in ICONS}
ICON_KEYS: tuple[str, ...] = tuple(ICON_BY_KEY)


def get_icon(key: str) -> Icon:
    icon = ICON_BY_KEY.get(key)
    if icon is None:
        raise ValueError(
            f"Unknown icon {key!r}. Supported: {', '.join(sorted(ICON_KEYS))}."
        )
    return icon


def catalogue() -> dict[str, Any]:
    """The library, grouped by category, as the icon picker consumes it."""
    return {
        "viewbox": ICON_VIEWBOX,
        "categories": [
            {
                "key": category,
                "icons": [i.to_dict() for i in ICONS if i.category == category],
            }
            for category in CATEGORIES
        ],
    }


__all__ = ["Icon", "ICONS", "ICON_BY_KEY", "ICON_KEYS", "ICON_VIEWBOX",
           "CATEGORIES", "get_icon", "catalogue"]
