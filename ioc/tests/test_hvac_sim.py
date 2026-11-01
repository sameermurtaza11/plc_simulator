"""Tests of the HVAC simulator. They need no EPICS: values go to a dry-run writer.

Run from the ioc/ folder:   python -m unittest discover -s tests -v
"""
import csv
import json
import os
import tempfile
import unittest
from collections import Counter

from hvac_sim import epics_alarm
from hvac_sim.catalog import Catalog
from hvac_sim.model import MODULE_TYPES
from hvac_sim.scenarios import SCENARIOS
from hvac_sim.simulator import Simulator
from hvac_sim.writers import DryRunWriter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HVAC_DB = os.path.join(HERE, "db", "hvac_test.db")
SUPPORT_DB = os.path.join(HERE, "db", "hvac_sim_support.db")


def sim(scenario="normal", dbs=(HVAC_DB, SUPPORT_DB), **kw):
    w = DryRunWriter(echo=False)
    kw.setdefault("log", lambda *_: None)
    return Simulator(list(dbs), w, scenario=scenario, **kw), w


def run_for(s, seconds, dt=1.0, on_step=None):
    for _ in range(int(seconds / dt)):
        s.step(dt)
        if on_step:
            on_step(s)


class CatalogTests(unittest.TestCase):
    def test_hvac_database_is_read_as_the_source_of_truth(self):
        c = Catalog.load(HVAC_DB)
        self.assertEqual(len(c), 25)
        self.assertEqual(Counter(r.rtype for r in c), {"ai": 12, "bi": 11, "mbbi": 2})
        r = c["HVAC:AHU1:SupplyAirTemp"]
        self.assertEqual((r.egu, r.prec, r.initial, r.num("HIHI"), r.fields["HHSV"]), ("degC", 1, 14.5, 35.0, "MAJOR"))
        self.assertEqual(c["HVAC:AHU1:SmokeDetected"].states, ["NORMAL", "SMOKE"])
        self.assertEqual(c["HVAC:AHU1:OperatingMode"].states, ["OFF", "OCCUPIED", "UNOCCUPIED", "WARMUP"])
        self.assertEqual(set(k for _, k in c.by_equipment()), {"AHU1", "AHU2"})

    def test_a_record_defined_twice_is_an_error(self):
        with self.assertRaises(ValueError):
            Catalog.load(HVAC_DB, HVAC_DB)


class EpicsAlarmModelTests(unittest.TestCase):
    def setUp(self):
        self.rec = Catalog.load(HVAC_DB)["HVAC:AHU1:SupplyAirTemp"]  # LOLO 5, LOW 10, HIGH 30, HIHI 35, HYST 1

    def test_limits_and_severities(self):
        ev = lambda v: epics_alarm.evaluate(self.rec, v)  # noqa: E731
        self.assertEqual(ev(15), ("NO_ALARM", 0))
        self.assertEqual(ev(31), ("HIGH", 1))
        self.assertEqual(ev(36), ("HIHI", 2))
        self.assertEqual(ev(9), ("LOW", 1))
        self.assertEqual(ev(4), ("LOLO", 2))

    def test_hysteresis_keeps_an_alarm_until_the_value_clears_the_limit_by_hyst(self):
        st = epics_alarm.AlarmState()
        self.assertEqual(epics_alarm.evaluate(self.rec, 30.5, st), ("HIGH", 1))
        self.assertEqual(epics_alarm.evaluate(self.rec, 29.5, st), ("HIGH", 1))   # within HYST=1 of the limit
        self.assertEqual(epics_alarm.evaluate(self.rec, 28.9, st), ("NO_ALARM", 0))

    def test_a_limit_without_a_severity_is_not_an_alarm(self):
        r = Catalog.load(HVAC_DB)["HVAC:AHU1:FilterDifferentialPressure"]  # LOW 0 with LSV NO_ALARM
        self.assertEqual(epics_alarm.evaluate(r, -5), ("NO_ALARM", 0))

    def test_binary_states_use_zsv_osv(self):
        c = Catalog.load(HVAC_DB)
        self.assertEqual(epics_alarm.evaluate(c["HVAC:AHU1:SupplyFanRunning"], 0)[1], 2)
        self.assertEqual(epics_alarm.evaluate(c["HVAC:AHU1:SupplyFanRunning"], 1)[1], 0)
        self.assertEqual(epics_alarm.evaluate(c["HVAC:AHU1:SmokeDetected"], 1)[1], 2)
        self.assertEqual(epics_alarm.evaluate(c["HVAC:AHU1:FilterDirty"], 1)[1], 1)


class NormalOperationTests(unittest.TestCase):
    def test_a_full_day_in_normal_operation_raises_no_alarm_and_stays_in_range(self):
        s, w = sim("normal")
        c = s.catalog
        problems, seen = [], set()

        def check(s):
            for pv, v in s.values.items():
                seen.add(pv)
                r = c[pv]
                if r.rtype in ("ai",) and r.writable_range and not (r.writable_range[0] <= v <= r.writable_range[1]):
                    problems.append("%s=%s outside display range" % (pv, v))
            for pv, (st, sv) in s.expected_alarms().items():
                if not pv.startswith("HVAC:SIM:"):
                    problems.append("normal operation raised %s %s" % (pv, st))

        run_for(s, 1500, on_step=check)  # > one 1200 s day
        self.assertEqual(problems[:5], [])
        simulated = {r.name for r in c} - set(s.unsimulated)
        self.assertTrue(simulated <= seen | {r.name for r in c if r.rtype == "calc"}, "every simulated PV must get values")

    def test_values_move_and_are_not_random_noise(self):
        s, w = sim("normal", day_seconds=86400.0)  # stay in the daytime: the setpoint changes at night
        run_for(s, 60)
        a = s.values["HVAC:AHU1:SupplyAirTemp"]
        run_for(s, 600)
        series = []
        run_for(s, 120, on_step=lambda s: series.append(s.values["HVAC:AHU1:SupplyAirTemp"]))
        self.assertAlmostEqual(sum(series) / len(series), 14.5, delta=1.0, msg="the supply air controller holds its setpoint")
        self.assertLess(max(series) - min(series), 1.0, "supply air temperature is steady, not random")
        self.assertNotEqual(a, series[-1], "values change over time")

    def test_physics_are_consistent(self):
        s, _ = sim("normal")
        run_for(s, 300)
        v = s.values
        # cold supply air needs the cooling valve open and the heating valve closed
        self.assertGreater(v["HVAC:AHU1:CoolingValveFeedback"], 10)
        self.assertEqual(v["HVAC:AHU1:CoolingValveOpenProof"], 1)
        self.assertEqual(v["HVAC:AHU1:HeatingValveOpenProof"], 0)
        # return air is warmer than supply air; mixed air lies between outdoor-ish and return
        self.assertGreater(v["HVAC:AHU1:ReturnAirTemp"], v["HVAC:AHU1:SupplyAirTemp"] + 5)
        # fan running at a plausible speed with a static pressure that follows it
        self.assertEqual(v["HVAC:AHU1:SupplyFanRunning"], 1)
        self.assertTrue(50 < v["HVAC:AHU1:FanSpeedFeedback"] < 80)
        self.assertTrue(250 < v["HVAC:AHU1:StaticPressure"] < 500)

    def test_day_night_cycle_changes_the_operating_mode_without_stopping_fans(self):
        s, _ = sim("normal", start_hour=21.0)
        modes, fans = set(), set()
        run_for(s, 1200, on_step=lambda s: (modes.add(s.values["HVAC:AHU1:OperatingMode"]), fans.add(s.values["HVAC:AHU1:SupplyFanRunning"])))
        self.assertTrue({1, 2, 3} <= modes, "occupied, unoccupied and warm-up are all visited: %s" % modes)
        self.assertEqual(fans, {1}, "unoccupied means setback, not a stopped fan (a stopped fan is a MAJOR alarm in this database)")

    def test_only_changed_values_are_resent_until_the_refresh_interval(self):
        s, w = sim("normal", refresh_s=30.0)
        s.step(1.0)
        first = w.writes
        self.assertGreaterEqual(first, 25)  # everything is sent the first time
        s.step(1.0)
        second = w.writes - first
        self.assertLess(second, first, "digital values that did not change are not re-sent every second")
        run_for(s, 28)  # t = 30 s
        before = w.writes
        s.step(1.0)  # t = 31 s: a refresh falls due (an IOC restart must be refilled)
        self.assertGreater(w.writes - before, 15)


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_raises_the_native_alarms_it_promises(self):
        for name, sc in SCENARIOS.items():
            if not sc.expected:
                continue
            with self.subTest(scenario=name):
                s, _ = sim(name)
                reached = {}

                def watch(s):
                    for pv, (st, sv) in s.expected_alarms().items():
                        reached[pv] = max(reached.get(pv, 0), sv)

                run_for(s, 900, on_step=watch)
                equip = "AHU2" if name.startswith("ahu2") else "AHU1"
                for point, min_sev in sc.expected:
                    pv = "HVAC:%s:%s" % (equip, point)
                    self.assertGreaterEqual(reached.get(pv, 0), min_sev, "%s should reach severity %d in %s (reached: %s)" % (pv, min_sev, name, reached))

    def test_smoke_trips_the_fans_and_closes_the_damper_but_only_on_that_unit(self):
        s, _ = sim("smoke_ahu1")
        run_for(s, 120)
        v = s.values
        self.assertEqual((v["HVAC:AHU1:SmokeDetected"], v["HVAC:AHU1:SupplyFanRunning"], v["HVAC:AHU1:ReturnFanRunning"]), (1, 0, 0))
        self.assertEqual((v["HVAC:AHU1:OperatingMode"], v["HVAC:AHU1:DamperPositionStatus"]), (0, 0))
        self.assertEqual((v["HVAC:AHU2:SmokeDetected"], v["HVAC:AHU2:SupplyFanRunning"]), (0, 1), "AHU2 is unaffected")

    def test_freezestat_opens_the_heating_valve(self):
        s, _ = sim("freezestat_ahu1")
        run_for(s, 400)
        v = s.values
        self.assertEqual(v["HVAC:AHU1:FreezeStatTrip"], 1)
        self.assertGreater(v["HVAC:AHU1:HeatingValveFeedback"], 80)
        self.assertEqual(v["HVAC:AHU1:DamperPositionStatus"], 3)

    def test_fault_and_recover_raises_and_clears(self):
        s, _ = sim("fault_and_recover")
        states = []
        run_for(s, 300, on_step=lambda s: states.append(s.values["HVAC:AHU1:FilterDirty"]))
        self.assertIn(1, states, "the filter gets dirty")
        self.assertEqual(states[-1], 0, "and is clean again after the replacement")

    def test_a_dead_sensor_freezes_its_value_but_the_others_move(self):
        s, _ = sim("sensor_dead_ahu1")
        run_for(s, 20)
        a = s.values["HVAC:AHU1:SupplyAirTemp"]
        b = s.values["HVAC:AHU1:ReturnAirTemp"]
        run_for(s, 60)
        self.assertEqual(s.values["HVAC:AHU1:SupplyAirTemp"], a)
        self.assertNotEqual(s.values["HVAC:AHU1:ReturnAirTemp"], b)

    def test_scenarios_are_switchable_while_running_through_the_control_file(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = os.path.join(d, "control.json")
            s, _ = sim("normal", control_file=ctl)
            run_for(s, 30)
            self.assertEqual(s.values["HVAC:AHU1:SmokeDetected"], 0)
            with open(ctl, "w") as fh:
                json.dump({"scenario": "smoke_ahu1"}, fh)
            os.utime(ctl, (s._control_mtime + 5, s._control_mtime + 5))
            run_for(s, 60)
            self.assertEqual(s.runner.name, "smoke_ahu1")
            self.assertEqual(s.values["HVAC:AHU1:SmokeDetected"], 1)
            self.assertEqual(s.values["HVAC:SIM:Scenario"], "smoke_ahu1", "the active scenario is published for the operator")

    def test_an_unknown_scenario_in_the_control_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = os.path.join(d, "control.json")
            logs = []
            s, _ = sim("normal", control_file=ctl, log=logs.append)
            with open(ctl, "w") as fh:
                json.dump({"scenario": "no-such"}, fh)
            run_for(s, 3)
            self.assertEqual(s.runner.name, "normal")
            self.assertTrue(any("unknown scenario" in m for m in logs))


class ExtensibilityTests(unittest.TestCase):
    def test_equipment_without_a_model_is_reported_and_keeps_its_initial_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "extra.db")
            with open(p, "w") as fh:
                fh.write('record(ai, "HVAC:CH1:ChilledWaterTemp") {\n field(EGU, "degC")\n field(VAL, "7")\n field(LOPR, "0")\n field(HOPR, "30")\n}\n')
            logs = []
            s, w = sim("normal", dbs=[HVAC_DB, p], log=logs.append)
            run_for(s, 5)
            self.assertIn("HVAC:CH1:ChilledWaterTemp", s.unsimulated)
            self.assertNotIn("HVAC:CH1:ChilledWaterTemp", w.values)
            self.assertTrue(any("CH1" in m for m in logs))

    def test_a_new_pv_of_a_supported_module_is_simulated_without_code_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ahu3.db")
            with open(p, "w") as fh:
                fh.write('record(ai, "HVAC:AHU3:SupplyAirTemp") {\n field(EGU, "degC")\n field(PREC, "1")\n field(VAL, "15")\n field(LOPR, "0")\n field(HOPR, "50")\n}\n')
            s, w = sim("normal", dbs=[p])
            run_for(s, 30)
            self.assertIn("HVAC:AHU3:SupplyAirTemp", w.values)
        self.assertIn("AHU", MODULE_TYPES)


class InventoryExportTests(unittest.TestCase):
    def test_export_matches_the_database(self):
        from hvac_sim.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "inv.csv")
            main(["export-inventory", "--db", HVAC_DB, "--out", out, "--stale-after", "45"])
            with open(out, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 25)
        by = {r["epics_pv_name"]: r for r in rows}
        t = by["HVAC:AHU1:SupplyAirTemp"]
        self.assertEqual((t["unit"], t["equipment"], t["system"], t["record_type"], t["display_low"], t["display_high"], t["stale_after_s"]),
                         ("degC", "AHU1", "HVAC", "ai", "0.0", "50.0", "45"))
        self.assertEqual(by["HVAC:AHU1:SmokeDetected"]["state1"], "SMOKE")
        self.assertEqual(by["HVAC:AHU1:SmokeDetected"]["stale_after_s"], "", "silence is normal for a binary status")


if __name__ == "__main__":
    unittest.main()
