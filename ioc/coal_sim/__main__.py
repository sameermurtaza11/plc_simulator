"""Command line: python -m coal_sim <command>   (run from the ioc/ folder)

  generate-db       write db/coal_plant.db (the IOC database, ~1000 records)
  export-inventory  write generated/coal_inventory.csv (the PV list for the monitoring application's CSV import)
  run               feed the IOC: every PV at its own random delay of 0.25 to 5 s
  set-load <0-100|auto>   pin the unit load of a running simulator, or give it back to the programme
  list              the point count per plant section
"""
import argparse
import collections
import json
import os
import sys

from hvac_sim.writers import DryRunWriter, make_writer

from .dbgen import write_db, write_inventory
from .engine import Simulator
from .plant import build

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "db", "coal_plant.db")
DEFAULT_CSV = os.path.join(HERE, "generated", "coal_inventory.csv")
DEFAULT_CONTROL = os.path.join(HERE, "coal_control.json")


def cmd_generate(a):
    plant = build(a.count)
    write_db(plant, a.out)
    print("wrote %d records to %s" % (len(plant.points), a.out))


def cmd_export(a):
    plant = build(a.count)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    write_inventory(plant, a.out, a.stale_after)
    print("wrote %d PVs to %s" % (len(plant.points), a.out))


def cmd_list(a):
    plant = build(a.count)
    c = collections.Counter(p.system for p in plant.points)
    for k, v in c.most_common():
        print("%-26s %4d" % (k, v))
    print("%-26s %4d  (%d analog, %d binary)" % ("total", len(plant.points), sum(p.rtype == "ai" for p in plant.points), sum(p.rtype == "bi" for p in plant.points)))


def cmd_run(a):
    plant = build(a.count)
    writer = DryRunWriter(echo=False) if a.writer == "dry-run" else make_writer(a.writer, a.caput)
    if a.writer == "pyepics":
        _connect(writer, plant)
    sim = Simulator(plant, writer, seed=a.seed, fault_rate_h=a.fault_rate, control_file=a.control_file, status_every=a.status_every)
    sim.run(duration=a.duration)


def _connect(writer, plant, wait=15.0):
    """Create all channels at once so they connect in parallel instead of one by one at the first write."""
    import time
    for p in plant.points:
        writer.pvs[p.name] = writer.epics.PV(p.name, auto_monitor=False, connection_timeout=1.0)
    t0 = time.time()
    while time.time() - t0 < wait:
        n = sum(1 for pv in writer.pvs.values() if pv.connected)
        if n == len(writer.pvs):
            break
        time.sleep(0.25)
    print("%d of %d channels connected after %.1f s%s" % (n, len(writer.pvs), time.time() - t0,
                                                         "" if n == len(writer.pvs) else " (the rest keep trying; is the IOC running with db/coal_plant.db?)"))


def cmd_set_load(a):
    load = None if a.load.lower() == "auto" else float(a.load) / 100.0
    with open(a.control_file, "w", encoding="utf-8") as fh:
        json.dump({"load": load}, fh)
    print("unit load %s requested; a running simulator follows within a second" % ("released" if load is None else "%.0f %%" % (load * 100)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coal_sim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def count(p):
        p.add_argument("--count", type=int, default=1000, help="number of PVs (default 1000)")

    g = sub.add_parser("generate-db")
    count(g)
    g.add_argument("--out", default=DEFAULT_DB)
    g.set_defaults(fn=cmd_generate)

    e = sub.add_parser("export-inventory")
    count(e)
    e.add_argument("--out", default=DEFAULT_CSV)
    e.add_argument("--stale-after", type=int, default=30, help="stale_after_s for analog PVs (a point is fed at least every 5 s; default 30)")
    e.set_defaults(fn=cmd_export)

    l = sub.add_parser("list")
    count(l)
    l.set_defaults(fn=cmd_list)

    r = sub.add_parser("run")
    count(r)
    r.add_argument("--writer", default="pyepics", choices=["pyepics", "caput", "dry-run"],
                   help="how values reach the IOC (default pyepics; caput starts one process per value and cannot keep up with 1000 PVs)")
    r.add_argument("--caput", default="caput")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--fault-rate", type=float, default=30.0, help="random limit excursions per hour (default 30; 0 = none)")
    r.add_argument("--duration", type=float, help="stop after this many seconds")
    r.add_argument("--status-every", type=float, default=10.0)
    r.add_argument("--control-file", default=DEFAULT_CONTROL)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("set-load")
    s.add_argument("load", help="percent (for example 95) or auto")
    s.add_argument("--control-file", default=DEFAULT_CONTROL)
    s.set_defaults(fn=cmd_set_load)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
