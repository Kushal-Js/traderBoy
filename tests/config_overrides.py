"""
Small shared helpers for the standalone-style test files (1 Oct 2026).

apply_config_overrides([("Options.config", "BREAKOUT_SIGNAL_ENABLED", False), ...])
sets module attributes and returns restore(). A test module that defines
TEST_CONFIG_OVERRIDES gets them from tests/conftest.py for the duration of the
module under pytest; its own main() calls this when run standalone. Used by
the files written for the DIRECT webhook entry path: since 21 Sep 2026 the
breakout scanner is the default entry path for Options/Luxury (the webhook
queues the alert, "queued_for_breakout_signal"); the direct path still exists
behind BREAKOUT_SIGNAL_ENABLED=false, and that is what those files test.
"""
import importlib


def apply_config_overrides(overrides):
    saved = []
    for module_name, attr, value in overrides:
        module = importlib.import_module(module_name)
        saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, value)

    def restore():
        for module, attr, value in reversed(saved):
            setattr(module, attr, value)
    return restore
