"""tests/test_merge_duplicate_skus.py — proof test.

Linnworks' Orders/CreateOrders rejects orders where the same SKU appears
on more than one OrderItem (observed live: order dCxw8888clDsEA5t5SbkvNcOaFEZY
failed with "LineId: SPR22 is duplicated" — the Square POS order rang the
same Sprague capacitor up on two separate lines). The order-pull tool merges
same-SKU lines before sending. This in-process test proves that.

Run directly: `python3 tests/test_merge_duplicate_skus.py`
(exit 0 = pass, exit 1 = fail). No network / credentials needed — the
`lib.*` modules are stubbed in sys.modules before the tool is imported, the
same proof-test pattern used elsewhere in this repo.
"""

import sys
import types
from pathlib import Path

# Make the repo root importable so `import tools.pull_...` resolves when this
# file is run directly from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- lib stubs -------------------------------------------------------------
# The tool does `from lib import db, linnworks, square` at import time, and
# lib.db pulls in the supabase client (not installed in the test env and not
# needed here). Stub the package + submodules so the import resolves to inert
# modules. _build_linnworks_payload / _merge_duplicate_skus touch none of
# them, so empty stubs are sufficient.
_lib = types.ModuleType("lib")
_lib.__path__ = []  # mark as a package
sys.modules["lib"] = _lib
for _sub in ("db", "linnworks", "square"):
    _m = types.ModuleType(f"lib.{_sub}")
    sys.modules[f"lib.{_sub}"] = _m
    setattr(_lib, _sub, _m)

from tools import pull_square_orders_to_linnworks as pull  # noqa: E402


def _make_square_order() -> dict:
    """A Square order that rings up SPR22 (Sprague capacitor) on TWO
    separate line items, qty 1 each at £3.49 (349 pence) — the exact shape
    that tripped Linnworks' duplicate-SKU validation in production.
    """
    line = {
        "catalog_object_id": "VAR_SPR22",
        "name": "Sprague Capacitor",
        "quantity": "1",
        "total_money": {"amount": 349, "currency": "GBP"},
    }
    return {
        "id": "dCxw8888clDsEA5t5SbkvNcOaFEZY",
        "created_at": "2026-05-28T11:13:00Z",
        "updated_at": "2026-05-28T11:13:00Z",
        "line_items": [dict(line), dict(line)],
    }


def test_merge_collapses_duplicate_spr22() -> None:
    sku_map = {"VAR_SPR22": {"sku": "SPR22", "linnworks_item_id": "LW-UUID-SPR22"}}

    payload = pull._build_linnworks_payload(_make_square_order(), sku_map)
    items = payload["OrderItems"]

    assert len(items) == 1, f"expected 1 merged line, got {len(items)}: {items!r}"
    merged = items[0]
    assert merged["SKU"] == "SPR22", f"unexpected SKU: {merged['SKU']!r}"
    assert merged["Qty"] == 2, f"expected merged Qty 2, got {merged['Qty']!r}"
    assert merged["PricePerUnit"] == 3.49, (
        f"expected PricePerUnit 3.49, got {merged['PricePerUnit']!r}"
    )
    # The strong-link reference must survive the merge.
    assert merged.get("StockItemId") == "LW-UUID-SPR22", (
        f"StockItemId lost in merge: {merged.get('StockItemId')!r}"
    )


def test_merge_helper_in_isolation() -> None:
    """Directly exercise _merge_duplicate_skus on a hand-built OrderItems
    list of two SPR22 lines (qty 1 each @ £3.49) — the minimal assertion
    requested: one entry, qty 2, PricePerUnit £3.49.
    """
    order_items = [
        {"SKU": "SPR22", "Qty": 1, "PricePerUnit": 3.49, "ItemTitle": "Sprague"},
        {"SKU": "SPR22", "Qty": 1, "PricePerUnit": 3.49, "ItemTitle": "Sprague"},
    ]
    merged = pull._merge_duplicate_skus(order_items)
    assert len(merged) == 1
    assert merged[0]["Qty"] == 2
    assert merged[0]["PricePerUnit"] == 3.49


def test_merge_weights_price_by_line_total() -> None:
    """When the same SKU appears with differing per-unit prices (e.g. a
    partial discount on one line), the merged PricePerUnit must be the
    value-weighted average, not a naive per-unit mean.

      2 @ £10.00  +  1 @ £4.00  →  total £24.00 over qty 3  →  £8.00/unit
    (a naive (10+4)/2 = £7.00 mean would be wrong).
    """
    order_items = [
        {"SKU": "SPR22", "Qty": 2, "PricePerUnit": 10.00},
        {"SKU": "SPR22", "Qty": 1, "PricePerUnit": 4.00},
    ]
    merged = pull._merge_duplicate_skus(order_items)
    assert len(merged) == 1
    assert merged[0]["Qty"] == 3
    assert merged[0]["PricePerUnit"] == 8.00, merged[0]["PricePerUnit"]


def main() -> int:
    tests = [
        test_merge_collapses_duplicate_spr22,
        test_merge_helper_in_isolation,
        test_merge_weights_price_by_line_total,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'}: "
          f"{len(tests) - failures}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
