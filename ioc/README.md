# Test IOC databases (v2)

EPICS databases for testing the PLC_Monitoring v2 Channel Access adapter **before** it meets the real
IOC. They run on a soft IOC (`softIoc` from EPICS Base) on the test laptop; the monitoring machine
connects to it over Ethernet.

Nothing here is a statement about the production IOC. Record types, PV names, fields and scan rates
of the real IOC are **unverified**; these files only make the adapter's behaviour testable.

| File | Purpose |
|---|---|
| `db/test_small.db` | About 20 hand-written records, each chosen to exercise one case: all four limits, HIGH only, no limits, fast scan with `MDEL`, slow scan (silence is normal), a constant, a manually driven record, `UDF` and broken-link (INVALID) records, `bi` with each severity setting, change-of-state alarm, `mbbi`. |
| `generate_db.py` | Generates `generated/load_<N>.db` (default 3000 records), the matching `load_<N>_pvs.csv` (inventory to import into the monitoring app) and `load_<N>_expected.json` (units, ranges, limits and severities the IOC was configured with, for comparison against what the adapter reads). Deterministic per `--seed`. |
| `generated/` | Output of the generator (git-ignored). |
| `db/hvac_test.db` | The HVAC database supplied for the end-to-end test (25 records, AHU1/AHU2). Source of truth for the HVAC PV inventory. |
| `db/hvac_sim_support.db` | Simulator heartbeat, scenario echo and a "simulator alive" alarm (optional, test only). |
| `hvac_sim/`, `tests/` | The HVAC process simulator that writes values into the IOC over Channel Access, and its tests. |
| `db/coal_plant.db`, `coal_sim/`, `COAL_PLANT.md` | A 1000-PV coal power plant (one 300 MW unit) and its simulator: every PV fed at a random 0.25 to 5 s that changes over time. How to run it: `COAL_PLANT.md`. |
| `HVAC_IOC_LAPTOP.md` | What to run on the IOC laptop for the HVAC end-to-end test. |

## Start the IOC (on the test laptop)

Small functional database:

```bash
softIoc -m "P=SIM:" -d db/test_small.db
```

Load test (generate first; do not load together with `test_small.db`, both define `$(P)tick`):

```bash
python generate_db.py --count 3000 --prefix LOAD:
softIoc -m "P=LOAD:" -d generated/load_3000.db
```

Useful generator options: `--count`, `--scan-mix fast:normal:slow` (default `0.05:0.75:0.20`),
`--digital` (fraction of bi/mbbi), `--nolimits`, `--mdel`, `--seed`. It prints an upper bound of monitor
events per second, assuming every scan posts an update (deadbands reduce this).

## Network (IOC laptop and monitoring machine)

- Allow UDP 5064 (name search) and TCP 5064 plus the dynamic TCP port(s) the IOC reports, inbound on the IOC laptop. Allow UDP 5065 if EPICS beacons are used.
- On the **monitoring machine** use explicit addressing, not broadcast:

  ```powershell
  $env:EPICS_CA_AUTO_ADDR_LIST = "NO"
  $env:EPICS_CA_ADDR_LIST      = "<IOC laptop IP>"
  ```

- Check clocks on both machines (timestamps are compared in the tests).

## Manual scenarios (done by the tester **on the IOC laptop**; the monitoring app never writes)

| Scenario | How |
|---|---|
| Walk an alarm through LOW → LOLO → back (checks `HYST` and transitions) | `caput SIM:sim:level 18`, `8`, `12`, `25`, `50` (`ai:manual`: LOW=20, LOLO=10, HYST=3) |
| Disconnect | Stop the IOC; or unplug the cable; or block TCP 5064 with the firewall. Then restore. |
| IOC restart | Stop and start `softIoc`; the adapter must resubscribe and not duplicate alarm events. |
| INVALID data | `SIM:ai:undefined` (UDF) and `SIM:ai:badlink` (LINK) are INVALID from the start; `caput SIM:ai:undefined 1` clears the UDF. |
| Simulated values | `caput SIM:ai:full.SIMM YES` / `.SIML`, or set `.PROC` manually, as needed. |

`caget`/`camonitor`/`caput` come with EPICS Base on the IOC laptop. Record the observed STAT, SEVR,
TIME and field values in `docs/v2/01-v2-audit-and-design.md` (section 4.3) in the PLC_Monitoring repo.

## Not validated yet

These files have not been loaded into an IOC: no EPICS Base is available on the development machine.
The generator output was checked for structure only (unique record names, balanced braces, CSV and
JSON agree with the `.db`). The first load on the test laptop may need small corrections (for example a
record field name accepted differently by the installed EPICS Base version); record any such fix here.
