"""
Paper01 safety-invariant tests - user request 15 Sep 2026 ("never uses
real money"). Mirrors the same invariant IndexScalping/K01/Options'
paper_webhook.py each document and enforce: PAPER_TRADING_ONLY stays
True, and no file in the package ever references a real order-placement
function.

HOW TO RUN:
    uv run python tests/test_paper01_safety_invariant.py
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FORBIDDEN_PATTERNS = [
    r"\bplace_market_order\b",
    r"\bplace_stop_loss_limit_order\b",
    r"\bplace_stop_loss_market_order\b",
    r"\bplace_equity_market_order\b",
    r"\bplace_equity_stop_loss_limit_order\b",
    r"\bplace_mcx_market_order\b",
    r"\bplace_mcx_stop_loss_limit_order\b",
    r"\bcancel_order\b",
    r"\border_placement\b",
]


def _strip_comments_and_strings(source: str) -> str:
    """Best-effort strip of docstrings/comments/string literals via the
    tokenizer, so a module docstring that merely MENTIONS a forbidden
    function name (e.g. explaining the safety invariant, as this
    package's own files do) doesn't false-positive this scan - only
    actual code (an attribute access / call) should ever match."""
    import io
    import tokenize
    out = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, tok_string, *_ in tokens:
            if tok_type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok_string)
    except tokenize.TokenizeError:
        return source
    return " ".join(out)


def test_1_no_real_order_placement_function_is_referenced_anywhere():
    paper01_dir = REPO_ROOT / "Paper01"
    py_files = sorted(paper01_dir.glob("*.py"))
    assert py_files, "Paper01 package must exist with at least one .py file"

    violations = []
    for path in py_files:
        code_only = _strip_comments_and_strings(path.read_text())
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, code_only):
                violations.append(f"{path.name}: matched forbidden pattern {pattern!r}")

    assert not violations, (
        "Paper01 must NEVER reference a real order-placement function - found:\n"
        + "\n".join(violations)
    )
    print(f"1. Scanned {len(py_files)} file(s) in Paper01/ - no real order-placement "
          f"function referenced anywhere: PASSED")


def test_2_paper_trading_only_flag_is_true():
    import Paper01.config as p1config
    assert p1config.PAPER_TRADING_ONLY is True, \
        "PAPER_TRADING_ONLY must be True - this is a hard safety invariant, not just a label"
    print("2. Paper01.config.PAPER_TRADING_ONLY is True: PASSED")


def test_3_lifespan_asserts_the_invariant():
    import inspect
    import Paper01.paper01_main as p1main
    source = inspect.getsource(p1main.lifespan)
    assert "PAPER_TRADING_ONLY" in source, \
        "Paper01.paper01_main.lifespan must assert config.PAPER_TRADING_ONLY at startup, " \
        "matching IndexScalping/K01's own runtime-assertion pattern"
    print("3. Paper01.paper01_main.lifespan asserts PAPER_TRADING_ONLY at startup: PASSED")


def main():
    print("=== Paper01 safety-invariant test suite ===\n")
    test_1_no_real_order_placement_function_is_referenced_anywhere()
    test_2_paper_trading_only_flag_is_true()
    test_3_lifespan_asserts_the_invariant()
    print("\nALL PAPER01 SAFETY-INVARIANT CHECKS PASSED")


if __name__ == "__main__":
    main()
