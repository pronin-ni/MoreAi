"""
MoreAI E2E Regression — pytest configuration.

Markers:
- smoke:     fast critical-path checks
- regression: full regression suite
- live:      optional real-provider tests (skipped by default)
"""



def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: fast critical-path checks")
    config.addinivalue_line("markers", "regression: full regression suite")
    config.addinivalue_line("markers", "live: optional real-provider tests")
