"""Load pure integration modules without requiring a full HA installation."""

from pathlib import Path
import sys
import types

ROOT = Path(__file__).parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "home_energy_intelligence"

# The calculation/storage modules do not import Home Assistant. Registering the
# package path avoids executing the runtime integration __init__ during unit tests.
package = types.ModuleType("custom_components.home_energy_intelligence")
package.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.home_energy_intelligence", package)
