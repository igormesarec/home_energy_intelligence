# Entity mapping

All mappings are selected in the Home Assistant UI and may be edited without reinstalling. No installation-specific entity IDs are shipped as defaults or used by calculations.

| Equipment field | Expected source | Required |
|---|---|---:|
| `energy_entity` | Device cumulative electrical energy with energy device class and total/total-increasing state class | Yes per enabled device |
| `power_entity` | Device instantaneous electrical power | Recommended; missing coverage remains residual |
| `state_entity` / `state_attribute` | Operating status or climate action | Yes per enabled device |
| `state_map` | Installation-specific raw strings mapped to heating, DHW, cooling, idle/standby or unknown categories | Yes |
| room/supply/return mappings | Real, independent temperature measurements only | No |

Site PV, grid import and grid export are independently optional. Temperature sources are a variable-length list with roles (`outdoor`, `indoor`, `supply`, `return`, `dhw`, `equipment_room`) and per-source filtering. Weather is optional and reserved for later forecasting.

Equipment IDs remain stable when entity IDs change. Mapping revisions store effective dates and non-secret provenance, so replacing a sensor does not delete or reinterpret historical daily rows. Options Flow rebuilds the configured equipment list and therefore supports add, edit, disable and removal.

Never map one physical counter under multiple aliases. Circuit 2K cannot be selected as supply or return. A supply sensor is not a return sensor, and no unrelated value is substituted. V0.1 does not calculate thermal output from a temperature difference.
