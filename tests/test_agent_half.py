import importlib.util
from pathlib import Path

INIT_FILE = Path(__file__).resolve().parent.parent / "__init__.py"


class _UntouchableContext:
    """A plugin context that fails the test if register() uses it."""

    def __getattr__(self, name):
        raise AssertionError(f"register() touched ctx.{name}")


def _load_init():
    spec = importlib.util.spec_from_file_location("hermes_plugins_pairdown_test", INIT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_half_defines_register_and_registers_nothing():
    module = _load_init()
    assert callable(module.register)
    assert module.register(_UntouchableContext()) is None
