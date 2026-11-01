"""Simulation scenarios: timelines of faults applied to the model.

A scenario is a list of steps (seconds after the scenario started, scope, action). The scope is the
equipment name ("AHU1") or "*" for all equipment; the action sets or clears fault flags that the
model modules read (see model.AhuModel). Scenarios can be switched while the simulator runs.

Add a scenario by appending to SCENARIOS; nothing else needs to change.
"""
from dataclasses import dataclass, field


@dataclass
class Step:
    at: float                # seconds after the scenario started
    scope: str               # equipment name or "*"
    set: dict = field(default_factory=dict)
    clear: tuple = ()        # fault keys to remove; ("*",) removes all faults of the scope


@dataclass
class Scenario:
    name: str
    description: str
    steps: list
    expected: tuple = ()     # (PV point name, minimum native severity) the scenario should raise on its equipment


SCENARIOS = {}


def _add(name, description, steps, expected=()):
    SCENARIOS[name] = Scenario(name, description, steps, expected)


_add("normal", "Normal operation: day/night cycle, no faults, no alarms.", [])

_add("filter_clogged", "AHU1 filter loads up quickly: differential pressure climbs past 250 Pa (minor) and 400 Pa (major); FilterDirty sets.",
     [Step(5, "AHU1", {"filter_clog_rate": 4.0})],
     (("FilterDifferentialPressure", 2), ("FilterDirty", 1)))

_add("smoke_ahu1", "Smoke detected at AHU1: the safety trip stops both fans and closes the damper.",
     [Step(10, "AHU1", {"smoke": True})],
     (("SmokeDetected", 2), ("SupplyFanRunning", 2)))

_add("freezestat_ahu1", "Cold outside air floods AHU1: mixed air falls, the freeze-stat trips, fans stop, the heating valve opens.",
     [Step(10, "AHU1", {"oa_temp": -8.0}), Step(20, "AHU1", {"freeze": True})],
     (("FreezeStatTrip", 2), ("MixedAirTemp", 2)))

_add("supply_fan_fail_ahu1", "AHU1 supply fan fails: it stops, the static pressure collapses.",
     [Step(10, "AHU1", {"supply_fan_fail": True})],
     (("SupplyFanRunning", 2), ("StaticPressure", 2)))

_add("cooling_failure_ahu1", "AHU1 chilled water is lost on a hot, loaded day: the cooling valve saturates and the supply air warms.",
     [Step(5, "AHU1", {"cooling_fail": True, "oa_temp": 45.0, "heat_load": 3.0})],
     (("CoolingValveFeedback", 2), ("SupplyAirTemp", 1)))

_add("high_humidity_ahu1", "AHU1 supply air becomes very humid.",
     [Step(5, "AHU1", {"humidity_add": 45.0})],
     (("SupplyAirHumidity", 2),))

_add("condensate_ahu1", "AHU1 condensate pan overflows (level switch).",
     [Step(5, "AHU1", {"condensate": True})],
     (("CondensateHighLevel", 1),))

_add("high_load_ahu1", "Heat load in the served zone rises: return air temperature climbs past 28 degC and then 32 degC.",
     [Step(5, "AHU1", {"heat_load": 9.0})],
     (("ReturnAirTemp", 1),))

_add("ahu2_smoke", "Smoke at AHU2 only.",
     [Step(5, "AHU2", {"smoke": True})],
     (("SmokeDetected", 2),))

_add("sensor_dead_ahu1", "The AHU1 supply air temperature sensor stops updating (value freezes): tests the application's stale detection.",
     [Step(5, "AHU1", {"dead_sensors": ("SupplyAirTemp",)})])

_add("fault_and_recover", "AHU1 filter clogs, then is replaced: shows an alarm raising and clearing.",
     [Step(5, "AHU1", {"filter_clog_rate": 6.0}), Step(90, "AHU1", {"filter_clog_rate": 0.0}, ()),
      Step(95, "AHU1", {"filter_reset": True})])


class Runner:
    """Applies the steps of the current scenario to a World as simulated time passes."""

    def __init__(self, world, modules):
        self.world, self.modules = world, modules
        self.name = "normal"
        self.started = 0.0
        self.done = 0

    def start(self, name):
        if name not in SCENARIOS:
            raise KeyError("unknown scenario %r; known: %s" % (name, ", ".join(sorted(SCENARIOS))))
        self.world.faults.clear()
        for m in self.modules:  # a fresh start: clean model state that faults changed
            if hasattr(m, "scenario_name"):
                m.scenario_name = name
        self.name, self.started, self.done = name, self.world.t, 0

    def tick(self):
        sc = SCENARIOS[self.name]
        while self.done < len(sc.steps) and self.world.t - self.started >= sc.steps[self.done].at:
            st = sc.steps[self.done]
            self.done += 1
            if st.set.get("filter_reset"):
                for m in self.modules:
                    if m.name == st.scope and hasattr(m, "filter_dp"):
                        m.filter_dp = 80.0
                continue
            if st.clear:
                if st.clear == ("*",):
                    self.world.clear_fault(st.scope)
                else:
                    self.world.clear_fault(st.scope, *st.clear)
            if st.set:
                self.world.set_fault(st.scope, **st.set)
