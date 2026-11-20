# ShushDRAC

*Not affiliated with or endorsed by Dell Technologies. "iDRAC" and "PowerEdge" are trademarks of Dell Inc.*

Lightweight web UI for quieting Dell PowerEdge servers in a home lab. Set fixed fan speeds or draw a custom
temperature curve, with automatic failsafe back to iDRAC control. Uses `ipmitool` raw commands over IPMI LAN.

## Compatibility
| iDRAC | Typical servers | Fan control |
|---|---|---|
| iDRAC7 | 12th gen: R620, R720, R820, T620 | Works |
| iDRAC8 | 13th gen: R630, R730, R730xd, T630 | Works |
| iDRAC9, firmware ≤ 3.30.30.30 | 14th gen: R640, R740, R840, R940 | Should work |
| iDRAC9, firmware 3.34+ | 14th gen and newer | Sensors only. Dell blocks manual fan control; use iDRAC's Thermal settings instead |

Check your firmware version: `ipmitool -I lanplus -H <ip> -U <user> -P <password> mc info` ("Firmware Revision").

## Setup
1. In iDRAC: **iDRAC Settings → Network → IPMI Settings → Enable IPMI over LAN**.
2. Install `ipmitool`:
   - **Windows:** Dell's *OpenManage BMC Utility* includes `ipmitool.exe`.
     [Download Dell OpenManage BMC Utility](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=m63f3).
     It installs to `C:\Program Files (x86)\Dell\SysMgt\bmc\`.
   - **Linux:** `sudo apt install ipmitool` (Debian/Ubuntu) or `sudo dnf install ipmitool` (Fedora/RHEL).
   - **macOS:** `brew install ipmitool`.

   If it isn't on PATH, set `IPMITOOL` to the full path of the executable.
3. `pip install flask`
4. `python app.py` → open http://127.0.0.1:5000 (set `BIND=0.0.0.0` to expose on LAN — there is no auth, so only on a trusted network).

## Notes
- Password is held in memory only; profiles are saved to `profiles.json`.
- Curve profiles run a background loop on this machine: it polls temps, uses the hottest CPU sensor, and sets the fan %.
  If a read fails, the app stops, or temp hits the failsafe, it **returns control to iDRAC auto**.
  If the app is killed hard, fans stay at the last manual speed — click "Return to iDRAC Auto" or reboot the iDRAC.
- "Disable 3rd-party PCIe cooling boost" stops the server from ramping fans for non-Dell PCIe cards.
- Raw commands used: manual `0x30 0x30 0x01 0x00`, auto `0x30 0x30 0x01 0x01`, speed `0x30 0x30 0x02 0xff <pct>`.

## License
MIT, see [LICENSE](LICENSE). Use at your own risk: running fans too slow can overheat hardware.
