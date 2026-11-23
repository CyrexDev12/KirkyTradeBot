from dataclasses import replace

from backend.updown.store import Store
from backend.updown.strategy import Book, Decision, Settings, Window, match_entry, match_exit


WINDOW = Window("btc", "btc-window", 100, 1000, 100.0, "MARKET_STATUS_OPEN", 0.095)
SETTINGS = Settings()


def test_same_depth_is_unavailable_after_restart_and_growth_replenishes_only_increment(tmp_path):
    db = tmp_path / "paper.sqlite3"
    store = Store(db)
    original = Decision("BUY", "legacy", side="UP", fair_up=0.8, quantity=6, price=0.5, cost=3.0)
    trade = store.open(WINDOW, original, 180)
    raw_book = Book(((0.3, 4),), ((0.5, 100),), 201, exchange_at=200, sequence=1)
    book = store.execution_book(WINDOW.slug, raw_book)
    fill = match_exit(trade, book, 6, 0, "stop_loss", SETTINGS)
    assert fill["quantity"] == 2
    trade = store.partial_exit(trade["id"], fill, 202, book=raw_book)
    assert trade["remaining"] == 4 and trade["exits"][0]["fills"] == ((0.3, 2),)

    store = Store(db)
    unchanged = replace(raw_book, at=211)
    book = store.execution_book(WINDOW.slug, unchanged)
    assert match_exit(trade, book, 4, 0, "stop_loss", SETTINGS) is None
    # Selling Up and buying Down touch the same canonical YES bids.
    assert book.side_asks("DOWN") == ((0.7, 0.0),)
    assert store.partial_exit(trade["id"], fill, 212, book=unchanged) is None

    increased = replace(unchanged, at=221, exchange_at=220, sequence=2, yes_bids=((0.3, 6),))
    book = store.execution_book(WINDOW.slug, increased)
    fill = match_exit(trade, book, 4, 0, "stop_loss", SETTINGS)
    assert fill["quantity"] == 1
    assert store.partial_exit(trade["id"], fill, 222, book=increased)["remaining"] == 3
    assert store.execution_book(WINDOW.slug, replace(raw_book, at=231)) is None
    independent = Store(tmp_path / "other-model.sqlite3")
    assert independent.execution_book(WINDOW.slug, increased).yes_bids == ((0.3, 6),)


def test_entry_matches_same_intent_at_new_price_and_checks_book_version_atomically(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    first = Book(((0.3, 5),), ((0.42, 5),), 202, exchange_at=201, sequence=1)
    later = replace(first, at=204, exchange_at=203, sequence=2, yes_offers=((0.38, 5), (0.45, 100)))
    buy = store.execution_book(WINDOW.slug, first)
    result = match_entry(WINDOW, buy, "UP", 4, .42, .8, 4, SETTINGS, balance=100, open_stake=0)
    assert result.fills == ((0.42, 2),)
    buy = store.execution_book(WINDOW.slug, later)
    assert store.open(WINDOW, result, 205, book=first) is None  # a newer observation intervened
    result = match_entry(WINDOW, buy, "UP", 4, .42, .8, 4, SETTINGS, balance=100, open_stake=0)
    assert result.fills == ((0.38, 2),)
    trade = store.open(WINDOW, result, 205, book=later)
    assert trade["quantity"] == 2 and trade["entry_fills"] == ((0.38, 2),)
    assert store.execution_book(WINDOW.slug, replace(later, at=206)).side_bids("DOWN")[0] == (0.62, 0.0)


def test_atomic_risk_and_close_rejection_leave_depth_available(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    other = replace(WINDOW, asset="eth", slug="eth-window")
    settings = replace(SETTINGS, max_stake=0.08, max_open_stake=0.09)
    raw = Book(((0.4, 100),), ((0.5, 100),), 200)
    entries = [match_entry(w, store.execution_book(w.slug, raw), "UP", 10, 0.5, 0.8, 8, settings,
                           balance=100, open_stake=0) for w in (WINDOW, other)]
    first = store.open(WINDOW, entries[0], 201, book=raw)
    assert first and entries[0].cost + entries[1].cost > 9
    assert store.open(other, entries[1], 202, book=raw) is None
    assert store.execution_book(other.slug, raw).yes_offers == raw.yes_offers

    last = replace(raw, at=WINDOW.end - 1)
    book = store.execution_book(WINDOW.slug, last)
    fill = match_exit(first, book, 6, 0, "stop_loss", settings)
    assert store.partial_exit(first["id"], fill, WINDOW.end, book=last) is None
    assert store.open(other, entries[1], other.end, book=raw) is None
    assert store.execution_book(WINDOW.slug, last).yes_bids == last.yes_bids
