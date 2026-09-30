"""
Offline stand-in for Dhan's instrument master in tests (1 Oct 2026).

Several code paths now ask the instrument master a question the older tests
never mocked - e.g. breakout_signal.record_alert filters index/MCX symbols
with dhan_wrapper.is_mcx_commodity (01d4500, 26 Sep), and Swing/Bollinger
signals check is_mcx_commodity before every fetch. In a test that means
dhan_wrapper.instruments() -> the real client -> a Dhan login, which the
pin_totp session-collision guard (rightly) refuses from a local process - so
those tests died before reaching what they test.

install() replaces dhan_wrapper.instruments with a tiny DataFrame that has the
real master's columns: the given NSE equities and MCX commodity futures, and
nothing else (so "is this MCX?" answers False unless listed). Returns a
restore function. Never touches the network.
"""
import pandas as pd

COLUMNS = ["SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
           "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_SERIES",
           "SEM_TICK_SIZE"]


def frame(equities=(), mcx=(), options=()) -> pd.DataFrame:
    """options: (custom symbol e.g. "NATURALGAS 23 SEP 280 CALL", "NSE" | "MCX", lot size) - option contracts a
    test's code resolves by symbol (their expiry is left empty)."""
    rows = []
    for i, (custom, exch, lot) in enumerate(options):
        rows.append([exch, "D" if exch == "NSE" else "M", 30000 + i, "OPTIDX" if exch == "NSE" else "OPTFUT",
                     custom.replace(" ", "-"), custom, None, None, "CE" if custom.endswith("CALL") else "PE",
                     float(lot), None, 5.0])
    for i, sym in enumerate(equities):
        rows.append(["NSE", "E", 10000 + i, "EQUITY", sym, sym, None, None, None, 1.0, "EQ", 5.0])
    for i, sym in enumerate(mcx):
        rows.append(["MCX", "M", 20000 + i, "FUTCOM", f"{sym}-31Oct2026-FUT", f"{sym} OCT FUT", "2026-10-31 23:30:00",
                     None, None, 1.0, None, 5.0])
    df = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL",
                "SEM_SERIES", "SEM_OPTION_TYPE", "SEM_EXPIRY_DATE"):
        df[col] = df[col].astype(object)
    return df


def install(wrapper, equities=(), mcx=(), options=()):
    """wrapper.instruments() -> the offline frame; also clears the per-symbol MCX answer cache so a
    previous test's answer cannot leak in. Returns restore()."""
    df = frame(equities, mcx, options)
    had_own = "instruments" in vars(wrapper)
    previous = vars(wrapper).get("instruments")
    cache = getattr(wrapper, "_mcx_underlying_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    wrapper.instruments = lambda: df

    def restore():
        if had_own:
            wrapper.instruments = previous
        else:
            try:
                del wrapper.instruments
            except AttributeError:
                pass
        if isinstance(cache, dict):
            cache.clear()
    return restore
