# Entity mapping

All mappings are selected in the Home Assistant UI and may be edited without reinstalling. No installation-specific entity IDs are shipped as defaults.

| Key | Expected source | Required |
|---|---|---:|
| `ecodan_energy` | Heat-pump cumulative electrical energy, kWh | Yes |
| `ecodan_power` | Heat-pump instantaneous electrical power, W | Yes |
| `ecodan_mode` | Exact operation state such as heating, DHW, cooling, idle | Yes |
| `ecodan_dhw` | Water heater with measured `current_temperature` | No |
| `ac_1..3_energy` | One cumulative electrical counter per AC | Yes |
| `ac_1..3_power` | One instantaneous power sensor per AC | Yes |
| `ac_1..3_climate` | Climate state and, where reliable, `hvac_action` | Yes |
| `ac_1..2_temperature` | Dedicated room measurement | No |
| `pv_energy` | Cumulative PV production | Yes |
| `grid_import` | Cumulative imported energy | Yes |
| `grid_export` | Cumulative exported energy, separate from import | Yes |
| `outdoor_temperature` | Actual outdoor temperature | Yes |
| `flow_temperature` | Primary heating supply temperature | Yes |
| `representative_room_temperature` | A clearly labelled indicative room sensor | No |
| `weather` | Existing Home Assistant weather entity | No; reserved for V0.3 |

Never map one physical counter under multiple aliases. A supply sensor is not a return sensor. V0.1 does not calculate thermal output from a temperature difference.
