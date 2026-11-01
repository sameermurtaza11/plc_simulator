"""Command line: python -m hvac_sim <command>   (run from the ioc/ folder)

  run               start the simulator (writes values to the IOC over Channel Access)
  scenarios         list the scenarios
  set <scenario>    switch the scenario of a running simulator (through the control file)
  list-pvs          show the PV inventory read from the .db files and what is simulated
  export-inventory  write the PV inventory as CSV (imported by the monitoring application)
"""
import argparse
import csv
import json
import os
import sys

from .catalog import Catalog
from .scenarios import SCENARIOS
from .simulator import Simulator
from .writers import make_writer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = [os.path.join(HERE, "db", "hvac_test.db"), os.path.join(HERE, "db", "hvac_sim_support.db")]
DEFAULT_CONTROL = os.path.join(HERE, "hvac_control.json")


def _dbs(a):
    return a.db or [p for p in DEFAULT_DB if os.path.exists(p)]


def cmd_run(a):
    writer = make_writer(a.writer, a.caput)
    sim = Simulator(_dbs(a), writer, scenario=a.scenario, seed=a.seed, day_seconds=a.day_seconds, start_hour=a.start_hour,
                    refresh_s=a.refresh, control_file=a.control_file)
    sim.run(interval=a.interval, speed=a.speed, duration=a.duration)


def cmd_scenarios(a):
    for s in SCENARIOS.values():
        print("%-24s %s" % (s.name, s.description))


def cmd_set(a):
    if a.scenario not in SCENARIOS:
        sys.exit("unknown scenario %r; run: python -m hvac_sim scenarios" % a.scenario)
    with open(a.control_file, "w", encoding="utf-8") as fh:
        json.dump({"scenario": a.scenario}, fh)
    print("scenario %s requested (control file %s); a running simulator switches within one interval" % (a.scenario, a.control_file))


def cmd_list(a):
    sim = Simulator(_dbs(a), make_writer("dry-run"), log=lambda *_: None)
    simulated = {r.name for r in sim.catalog} - set(sim.unsimulated)
    for r in sim.catalog:
        print("%-5s %-45s %-8s %s%s" % (r.rtype, r.name, r.egu, "computed in the IOC" if r.rtype in ("calc", "calcout", "sub", "aSub") else ("simulated" if r.name in simulated else "initial value only"),
                                         "  states=%s" % r.states if r.states else ""))
    print("\n%d records" % len(sim.catalog))


def cmd_export(a):
    cat = Catalog.load(*_dbs(a))
    fh = open(a.out, "w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["epics_pv_name", "pv_name", "system", "equipment", "description", "unit", "record_type",
                "display_low", "display_high", "state0", "state1", "history_deadband", "stale_after_s"])
    for r in cat:
        rng = r.writable_range or ("", "")
        analog = r.is_analog
        w.writerow([r.name, r.name, r.prefix or "HVAC", r.equipment, r.fields.get("DESC", ""), r.egu, r.rtype,
                    rng[0], rng[1],
                    r.states[0] if r.is_binary else "", r.states[1] if r.is_binary else "",
                    "", a.stale_after if analog else ""])
    fh.close()
    print("wrote %d PVs to %s" % (len(cat), a.out))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hvac_sim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", action="append", help="EPICS .db file (repeatable). Default: db/hvac_test.db and db/hvac_sim_support.db")

    r = sub.add_parser("run", help="start the simulator")
    common(r)
    r.add_argument("--writer", default="pyepics", choices=["pyepics", "caput", "dry-run"], help="how values reach the IOC (default pyepics)")
    r.add_argument("--caput", default="caput", help="path of EPICS Base's caput (writer caput)")
    r.add_argument("--scenario", default="normal", choices=sorted(SCENARIOS))
    r.add_argument("--interval", type=float, default=1.0, help="seconds between updates (default 1)")
    r.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second (default 1)")
    r.add_argument("--day-seconds", type=float, default=1200.0, help="simulated seconds per 24 h day (default 1200 = a 20 minute day)")
    r.add_argument("--start-hour", type=float, default=10.0)
    r.add_argument("--refresh", type=float, default=10.0, help="re-send every value at least this often, so a restarted IOC is refilled (default 10)")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--duration", type=float, help="stop after this many seconds")
    r.add_argument("--control-file", default=DEFAULT_CONTROL)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("scenarios", help="list scenarios")
    s.set_defaults(fn=cmd_scenarios)

    t = sub.add_parser("set", help="switch the scenario of a running simulator")
    t.add_argument("scenario")
    t.add_argument("--control-file", default=DEFAULT_CONTROL)
    t.set_defaults(fn=cmd_set)

    l = sub.add_parser("list-pvs", help="show the PV inventory")
    common(l)
    l.set_defaults(fn=cmd_list)

    e = sub.add_parser("export-inventory", help="write the PV inventory as CSV")
    common(e)
    e.add_argument("--out", default=os.path.join(HERE, "generated", "hvac_inventory.csv"))
    e.add_argument("--stale-after", type=int, default=30, help="stale_after_s for analog PVs (a live sensor changes every update; default 30)")
    e.set_defaults(fn=cmd_export)

    a = ap.parse_args(argv)
    if getattr(a, "out", None):
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()
