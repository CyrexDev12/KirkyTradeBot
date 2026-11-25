"""Level accounting shared by live paper execution and its replay."""

from dataclasses import replace


def available_depth(book, spent):
    adjusted = {}
    for side, levels in (("bids", book.yes_bids), ("offers", book.yes_offers)):
        # Only observed increases in depth are available after a level was spent.
        spent[side] = {str(price): min(qty, spent[side][str(price)]) for price, qty in levels
                       if str(price) in spent[side]}
        adjusted[side] = tuple((price, qty - spent[side].get(str(price), 0.0)) for price, qty in levels)
    return replace(book, yes_bids=adjusted["bids"], yes_offers=adjusted["offers"])


def spend_depth(book, spent, side, buying, fills, participation):
    canonical = "offers" if buying == (side == "UP") else "bids"
    raw = book.yes_offers if canonical == "offers" else book.yes_bids
    quoted = book.side_asks(side) if buying else book.side_bids(side)
    physical = {price: (str(yes), depth) for (price, _), (yes, depth) in zip(quoted, raw)}
    used = spent[canonical].copy()
    for price, quantity in fills:
        if price not in physical:
            return False
        key, displayed = physical[price]
        if quantity <= 0 or quantity > int(max(0.0, displayed - used.get(key, 0.0)) * participation):
            return False
        used[key] = displayed
    spent[canonical] = used
    return True
