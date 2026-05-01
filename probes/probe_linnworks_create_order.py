"""DEPRECATED — use probes/probe_linnworks_create_orders.py instead.

This file probed `Orders/CreateNewOrder` (singular). The first run
returned HTTP 400 "The request is invalid." on every body shape.
Research after that run revealed two issues:

1. Wrong endpoint. Linnworks' own docs describe `Orders/CreateNewOrder`
   as creating "an empty draft order" and direct readers to
   `Orders/CreateOrders` (plural) for fully-formed orders with line
   items inline — which is what we need for converting one Square
   sale into one Linnworks order in a single call.

2. Missing mandatory fields. Per
   https://help.linnworks.com/support/solutions/articles/7000013635 ,
   CreateOrders requires Source, SubSource, ReferenceNumber,
   ReceivedDate, DispatchBy, OrderItems, DeliveryAddress (named
   exactly that, not ShippingAddress). v1 sent none of these.

The replacement script is `probes/probe_linnworks_create_orders.py`
(plural) and its workflow is `probe-linnworks-create-orders.yml`.
The old workflow `probe-linnworks-create-order.yml` was deleted in
the same commit that introduced this stub.

This stub is kept in the repo so anyone reading commit history can
see what was tried and why it was abandoned.
"""

import sys


def main() -> int:
    print(
        "probe_linnworks_create_order.py is DEPRECATED.\n"
        "Use probes/probe_linnworks_create_orders.py (plural) instead — "
        "Orders/CreateNewOrder creates empty draft orders, not the "
        "fully-formed orders we need for the Square→Linnworks pipeline."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
