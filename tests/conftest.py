"""Load pure integration modules without requiring a full HA installation."""

from pathlib import Path
import importlib.util
import sys
import types

ROOT = Path(__file__).parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "home_energy_intelligence"

# The calculation/storage modules do not import Home Assistant. Registering the
# package path avoids executing the runtime integration __init__ during unit tests.
if importlib.util.find_spec("homeassistant") is None:
    package = types.ModuleType("custom_components.home_energy_intelligence")
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules.setdefault("custom_components.home_energy_intelligence", package)
