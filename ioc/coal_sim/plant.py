"""The point list of a 300 MW pulverised-coal power plant (one unit): what exists, its range, its alarm limits.

Everything the .db file, the CSV inventory and the simulator need comes from this one list, so the three can
never disagree. A point is either an analog input (ai) or a binary input (bi). The names follow the HVAC
convention COAL:<Equipment>:<Point>; the plant section ("system") is kept separately.

The values the simulator makes for a point come from a few numbers:
  nom   value at 100 % unit load while the equipment runs        idle  value at 0 % load while it runs
  off   value while the equipment is stopped (None: independent of it)
  shape value = idle + (nom - idle) * load**shape
"""
from dataclasses import dataclass, field

PREFIX = "COAL"
AMBIENT = 35.0  # degC of a stopped, hot-climate machine


@dataclass
class Equip:
    key: str
    system: str
    label: str
    always: bool = True      # always running (boiler, turbine ...) or switched by the unit load
    threshold: float = 0.0   # unit load (0..1) from which a switched machine runs
    trip_h: float = 0.0      # trips per hour (a switched machine only)


@dataclass
class Point:
    name: str
    system: str
    equip: str
    point: str
    desc: str
    rtype: str                       # ai | bi
    egu: str = ""
    prec: int = 1
    nom: float = 0.0
    idle: float = 0.0
    off: object = None
    lopr: float = 0.0
    hopr: float = 100.0
    sigma: float = 0.1
    tau: float = 15.0
    shape: float = 1.0
    lim: dict = field(default_factory=dict)   # HIHI HIGH LOW LOLO
    hyst: float = 0.0
    # binary
    kind: str = ""                   # run | trip | follow | sw | const
    znam: str = ""
    onam: str = ""
    zsv: str = "NO_ALARM"
    osv: str = "NO_ALARM"
    value0: int = 0                  # const value / initial value
    rate_h: float = 0.0              # sw: occurrences per hour
    dur: float = 60.0                # sw: seconds it stays on

    @property
    def initial(self):
        if self.rtype == "bi":
            return self.value0
        return round(self.idle + (self.nom - self.idle) * 0.8 ** self.shape, self.prec)


class Plant:
    def __init__(self):
        self.equips = {}
        self.points = []
        self._names = set()

    # -- building blocks -----------------------------------------------------------------------
    def equip(self, system, key, label, always=True, threshold=0.0, trip_h=0.0):
        e = self.equips[key] = Equip(key, system, label, always, threshold, trip_h)
        return e

    def _add(self, p):
        if p.name in self._names:
            raise ValueError("duplicate point " + p.name)
        self._names.add(p.name)
        self.points.append(p)
        return p

    def ai(self, e, point, desc, egu, nom, idle=None, lo=0.0, hi=None, sigma=None, tau=15.0, shape=1.0,
           off="auto", low=False, prec=1):
        idle = nom * 0.55 if idle is None else idle
        hi = (nom * 1.5 if nom > 0 else nom + 1.0) if hi is None else hi
        span = hi - lo
        sigma = 0.004 * span if sigma is None else sigma
        if off == "auto":
            off = None if e.always else (AMBIENT if egu == "degC" else 0.0)
        top, bottom = max(nom, idle), min(nom, idle)       # the normal operating band
        lim = {"HIGH": top + 0.3 * (hi - top), "HIHI": top + 0.6 * (hi - top)}
        if low and e.always:
            lim["LOW"] = bottom - 0.3 * (bottom - lo)
            lim["LOLO"] = bottom - 0.6 * (bottom - lo)
        lim = {k: round(v, prec + 1) for k, v in lim.items()}
        e_ = self.equips[e.key]
        return self._add(Point("%s:%s:%s" % (PREFIX, e.key, point), e_.system, e.key, point, desc[:40], "ai", egu, prec,
                               nom, idle, off, lo, hi, sigma, tau, shape, lim, round(0.01 * span, prec + 1)))

    def temp(self, e, point, desc, nom, idle=None, hi=None, **kw):
        return self.ai(e, point, desc, "degC", nom, idle, hi=hi, **kw)

    def cur(self, e, point, desc, nom, **kw):
        kw.setdefault("tau", 4.0)
        return self.ai(e, point, desc, "A", nom, **kw)

    def vib(self, e, point, desc, nom=40.0, **kw):
        kw.setdefault("off", 2.0)
        kw.setdefault("idle", nom * 0.8)
        kw.setdefault("hi", nom * 3.0)
        kw.setdefault("sigma", nom * 0.03)
        kw.setdefault("tau", 8.0)
        kw.setdefault("prec", 0)
        return self.ai(e, point, desc, "um", nom, **kw)

    def _bi(self, e, point, desc, kind, znam, onam, zsv="NO_ALARM", osv="NO_ALARM", value0=0, rate_h=0.0, dur=60.0):
        return self._add(Point("%s:%s:%s" % (PREFIX, e.key, point), self.equips[e.key].system, e.key, point, desc[:40], "bi",
                               kind=kind, znam=znam, onam=onam, zsv=zsv, osv=osv, value0=value0, rate_h=rate_h, dur=dur))

    def run(self, e, point="Running", desc=None):
        return self._bi(e, point, desc or e.label + " run status", "run", "STOPPED", "RUNNING", value0=1)

    def trip(self, e, point="Tripped", desc=None):
        return self._bi(e, point, desc or e.label + " trip", "trip", "NORMAL", "TRIPPED", osv="MAJOR")

    def follow(self, e, point, desc, znam, onam):
        return self._bi(e, point, desc, "follow", znam, onam, value0=1)

    def sw(self, e, point, desc, znam="NORMAL", onam="ALARM", osv="MINOR", rate_h=0.5, dur=60.0):
        return self._bi(e, point, desc, "sw", znam, onam, osv=osv, rate_h=rate_h, dur=dur)

    def const(self, e, point, desc, znam, onam, value, zsv="NO_ALARM", osv="NO_ALARM"):
        return self._bi(e, point, desc, "const", znam, onam, zsv, osv, value0=value)


# -- the plant -----------------------------------------------------------------------------------------
def _motor(P, e, mA, bearing=75.0, vibn=40.0, windings=True, extra_vib=True):
    """Run, trip, current, two bearing temperatures, vibration and (optionally) three winding temperatures."""
    P.run(e)
    P.trip(e)
    P.cur(e, "MotorCurrent", e.label + " motor current", mA)
    P.temp(e, "BrgTempDE", e.label + " bearing temp DE", bearing, idle=bearing * 0.8, hi=bearing * 1.6)
    P.temp(e, "BrgTempNDE", e.label + " bearing temp NDE", bearing - 4, idle=(bearing - 4) * 0.8, hi=bearing * 1.6)
    P.vib(e, "VibDE", e.label + " vibration DE", vibn)
    if extra_vib:
        P.vib(e, "VibNDE", e.label + " vibration NDE", vibn * 0.85)
    if windings:
        for ph in "UVW":
            P.temp(e, "WindTemp" + ph, "%s winding temp %s" % (e.label, ph), 92.0, idle=62.0, hi=150.0)


def build(count=1000):
    P = Plant()

    # Coal handling ------------------------------------------------------------------------------------
    S = "Coal Handling"
    for i in range(1, 5):
        e = P.equip(S, "Conveyor%d" % i, "Conveyor C%d" % i, always=False, threshold=0.0, trip_h=0.4)
        P.run(e)
        P.trip(e)
        P.sw(e, "PullCord", e.label + " pull cord", "NORMAL", "PULLED", "MAJOR", rate_h=0.1, dur=45)
        P.ai(e, "BeltSpeed", e.label + " belt speed", "%", 98.0, idle=97.0, hi=120.0, sigma=0.4, tau=3.0)
        P.cur(e, "MotorCurrent", e.label + " motor current", 118.0 - 6 * i)
        P.temp(e, "GearboxTemp", e.label + " gearbox oil temp", 68.0, idle=55.0, hi=105.0)
        P.ai(e, "BeltTension", e.label + " belt tension", "kN", 42.0, idle=36.0, hi=70.0, sigma=0.5)
    for k in "AB":
        e = P.equip(S, "Crusher" + k, "Coal crusher " + k, always=False, threshold=0.0 if k == "A" else 0.6, trip_h=0.3)
        P.run(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 160.0)
        P.temp(e, "BrgTempDE", e.label + " bearing temp DE", 70.0, idle=58.0, hi=110.0)
        P.temp(e, "BrgTempNDE", e.label + " bearing temp NDE", 66.0, idle=55.0, hi=110.0)
        P.vib(e, "Vib", e.label + " vibration", 55.0)
    for k in "ABCDEF":
        e = P.equip(S, "Bunker" + k, "Coal bunker " + k)
        P.ai(e, "Level", e.label + " level", "%", 72.0, idle=88.0, hi=100.0, lo=0.0, sigma=0.4, tau=40.0, shape=1.0, low=True)
        P.temp(e, "CoalTempTop", e.label + " coal temp (top)", 38.0, idle=34.0, hi=80.0, sigma=0.3, tau=60.0)
        P.sw(e, "LevelLow", e.label + " low level switch", "NORMAL", "LOW", "MINOR", rate_h=0.15, dur=90)

    # Pulverisers (mills) ------------------------------------------------------------------------------
    S = "Pulverisers"
    for k, thr in zip("ABCDEF", (0.0, 0.0, 0.0, 0.0, 0.55, 0.8)):
        e = P.equip(S, "Mill" + k, "Mill " + k, always=False, threshold=thr, trip_h=0.25)
        _motor(P, e, 78.0, bearing=72.0, vibn=45.0)
        P.ai(e, "CoalFeed", e.label + " coal feed rate", "kg/hr", 48000.0, idle=26000.0, hi=75000.0, tau=12.0, prec=0)
        P.ai(e, "FeederSpeed", e.label + " feeder speed", "%", 78.0, idle=42.0, hi=100.0, tau=10.0)
        P.temp(e, "OutletTemp", e.label + " outlet temp", 82.0, idle=77.0, hi=115.0, sigma=0.5)
        P.temp(e, "InletAirTemp", e.label + " primary air inlet", 285.0, idle=250.0, hi=380.0, sigma=1.2)
        P.ai(e, "DiffPress", e.label + " differential pressure", "kPa", 4.6, idle=3.1, hi=8.0, sigma=0.05, prec=2)
        P.ai(e, "PAFlow", e.label + " primary air flow", "m3/hr", 76000.0, idle=60000.0, hi=115000.0, prec=0)
        P.ai(e, "GearboxOilTemp", e.label + " gearbox oil temp", "degC", 58.0, idle=48.0, hi=95.0)
        P.ai(e, "GearboxOilPress", e.label + " gearbox oil pressure", "bar", 2.4, idle=2.4, hi=4.5, sigma=0.03, prec=2)
        P.ai(e, "SeparatorSpeed", e.label + " separator speed", "%", 72.0, idle=60.0, hi=110.0, sigma=0.4)
        for n in range(1, 5):
            P.follow(e, "Flame%d" % n, "%s burner %d flame" % (e.label, n), "NO FLAME", "FLAME")

    # Boiler -------------------------------------------------------------------------------------------
    S = "Boiler"
    e = P.equip(S, "Boiler", "Boiler")
    P.ai(e, "DrumPressure", "Drum pressure", "bar", 172.0, idle=165.0, hi=210.0, sigma=0.4, tau=25.0, low=True)
    for n in (1, 2, 3):
        P.ai(e, "DrumLevel%d" % n, "Drum level transmitter %d" % n, "mm", 0.0, idle=0.0, lo=-300.0, hi=300.0, sigma=3.0, tau=8.0, low=True)
    P.ai(e, "MainSteamPress", "Main steam pressure", "bar", 168.0, idle=162.0, hi=205.0, sigma=0.4, tau=20.0, low=True)
    P.temp(e, "MainSteamTemp", "Main steam temperature", 540.0, idle=525.0, hi=600.0, sigma=1.0, tau=40.0, low=True)
    P.ai(e, "MainSteamFlow", "Main steam flow", "kg/hr", 950000.0, idle=420000.0, hi=1200000.0, tau=15.0, prec=0, low=True)
    P.ai(e, "ReheatPress", "Hot reheat pressure", "bar", 38.0, idle=21.0, hi=55.0, sigma=0.2, tau=25.0, prec=2)
    P.temp(e, "ReheatTemp", "Hot reheat temperature", 540.0, idle=510.0, hi=600.0, sigma=1.0, tau=40.0)
    P.ai(e, "FeedwaterFlow", "Feedwater flow", "kg/hr", 960000.0, idle=430000.0, hi=1200000.0, tau=15.0, prec=0)
    P.temp(e, "FeedwaterTemp", "Feedwater temp at economiser", 252.0, idle=215.0, hi=290.0, sigma=0.6)
    P.temp(e, "SHSprayTemp", "Superheater spray water temp", 185.0, idle=170.0, hi=240.0)
    for n in (1, 2, 3):
        P.ai(e, "FurnacePress%d" % n, "Furnace pressure %d" % n, "kPa", -0.05, idle=-0.05, lo=-0.6, hi=0.6, sigma=0.01, tau=3.0, prec=3)
    for n in (1, 2, 3, 4):
        P.ai(e, "O2_%d" % n, "Economiser outlet O2 probe %d" % n, "%", 3.4, idle=5.2, lo=0.0, hi=10.0, sigma=0.05, tau=20.0, prec=2)
    P.ai(e, "CO", "Economiser outlet CO", "ppm", 45.0, idle=30.0, hi=400.0, sigma=3.0, tau=20.0, prec=0)
    P.temp(e, "FurnaceExitGasTemp", "Furnace exit gas temperature", 1020.0, idle=880.0, hi=1250.0, sigma=3.0, tau=45.0)
    P.temp(e, "EconoGasOutTemp", "Economiser gas outlet temp", 375.0, idle=320.0, hi=480.0, sigma=1.0, tau=60.0)
    P.ai(e, "SootBlowerSteam", "Soot blower steam pressure", "bar", 14.0, idle=14.0, hi=25.0, sigma=0.1, prec=2)
    P.sw(e, "SootBlowing", "Soot blowing in progress", "IDLE", "BLOWING", "NO_ALARM", rate_h=2.0, dur=180)
    P.sw(e, "MFTTrip", "Master fuel trip", "NORMAL", "TRIPPED", "MAJOR", rate_h=0.0, dur=30)
    for k in "AB":
        e = P.equip(S, "Econ" + k, "Economiser " + k)
        P.temp(e, "GasInTemp", e.label + " gas inlet temp", 560.0, idle=470.0, hi=700.0, sigma=1.0, tau=60.0)
        P.temp(e, "GasOutTemp", e.label + " gas outlet temp", 375.0, idle=320.0, hi=480.0, sigma=1.0, tau=60.0)
        P.ai(e, "WaterDP", e.label + " water differential press", "bar", 3.6, idle=1.2, hi=8.0, sigma=0.03, prec=2, shape=1.6)

    # Air and flue gas ---------------------------------------------------------------------------------
    S = "Air and Flue Gas"
    for tag, label, amps, flow, dp in (("FDFan", "FD fan", 210.0, 380000.0, 4.2), ("PAFan", "PA fan", 260.0, 260000.0, 11.0),
                                       ("IDFan", "ID fan", 340.0, 620000.0, 3.1)):
        for k in "AB":
            e = P.equip(S, tag + k, "%s %s" % (label, k), always=False, threshold=0.0, trip_h=0.15)
            _motor(P, e, amps, bearing=70.0, vibn=38.0)
            P.ai(e, "Flow", e.label + " flow", "m3/hr", flow, idle=flow * 0.55, hi=flow * 1.4, prec=0, tau=10.0)
            P.ai(e, "DiffPress", e.label + " differential pressure", "kPa", dp, idle=dp * 0.7, hi=dp * 1.6, sigma=0.03, prec=2)
            P.ai(e, "BladePitch", e.label + " blade pitch", "%", 68.0, idle=40.0, hi=100.0, sigma=0.3, tau=6.0)
            P.ai(e, "OilPress", e.label + " lube oil pressure", "bar", 3.2, idle=3.2, hi=6.0, sigma=0.03, prec=2)
            P.ai(e, "OilTemp", e.label + " lube oil temperature", "degC", 46.0, idle=42.0, hi=80.0)
    for k in "AB":
        e = P.equip(S, "APH" + k, "Air preheater " + k)
        P.run(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 24.0, idle=22.0)
        P.temp(e, "GasInTemp", e.label + " gas inlet temp", 375.0, idle=320.0, hi=480.0, sigma=1.0, tau=60.0)
        P.temp(e, "GasOutTemp", e.label + " gas outlet temp", 138.0, idle=118.0, hi=200.0, sigma=0.6, tau=60.0)
        P.temp(e, "AirInTemp", e.label + " air inlet temp", 32.0, idle=30.0, hi=70.0, sigma=0.3, tau=90.0)
        P.temp(e, "AirOutTemp", e.label + " air outlet temp", 305.0, idle=255.0, hi=400.0, sigma=0.8, tau=60.0)
        P.ai(e, "GasDP", e.label + " gas differential press", "kPa", 1.1, idle=0.5, hi=2.6, sigma=0.02, prec=2, shape=1.8)
        P.ai(e, "AirDP", e.label + " air differential press", "kPa", 0.9, idle=0.4, hi=2.2, sigma=0.02, prec=2, shape=1.8)
        P.temp(e, "BrgTemp", e.label + " guide bearing temp", 62.0, idle=55.0, hi=100.0)
    for k in "AB":
        for f in range(1, 5):
            e = P.equip(S, "ESP%s%d" % (k, f), "ESP %s field %d" % (k, f))
            P.ai(e, "Voltage", e.label + " voltage", "V", 62000.0 - 3000.0 * f, idle=58000.0 - 3000.0 * f, hi=85000.0, prec=0, sigma=250.0, tau=6.0)
            P.ai(e, "Current", e.label + " current", "mA", 780.0 - 90.0 * f, idle=620.0 - 90.0 * f, hi=1200.0, prec=0, sigma=8.0, tau=6.0)
            P.ai(e, "SparkRate", e.label + " spark rate", "1/min", 4.0 + f, idle=2.0 + f, hi=60.0, sigma=0.8, prec=0, tau=10.0)
            P.sw(e, "RapperFault", e.label + " rapper fault", "NORMAL", "FAULT", "MINOR", rate_h=0.15, dur=120)
    for k in "ABC":
        e = P.equip(S, "FGDPump" + k, "FGD recycle pump " + k, always=False, threshold=0.0 if k != "C" else 0.75, trip_h=0.2)
        P.run(e)
        P.trip(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 98.0)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 2.6, idle=2.6, hi=4.5, sigma=0.03, prec=2)
    e = P.equip(S, "FGD", "FGD absorber")
    P.ai(e, "pH", "Absorber slurry pH", "", 5.5, idle=5.5, lo=3.0, hi=8.0, sigma=0.03, prec=2, tau=40.0, low=True)
    P.ai(e, "Level", "Absorber level", "m", 9.2, idle=9.2, hi=13.0, sigma=0.03, prec=2, tau=30.0, low=True)
    P.ai(e, "SlurryDensity", "Slurry density", "kg/m3", 1120.0, idle=1100.0, lo=1000.0, hi=1300.0, sigma=2.0, prec=0, tau=60.0)
    P.ai(e, "LimestoneFlow", "Limestone slurry flow", "m3/hr", 18.0, idle=7.0, hi=35.0, sigma=0.2)
    P.ai(e, "SO2In", "SO2 absorber inlet", "ppm", 1650.0, idle=900.0, hi=2600.0, prec=0, sigma=12.0, tau=25.0)
    P.ai(e, "SO2Out", "SO2 stack", "ppm", 120.0, idle=70.0, hi=400.0, prec=0, sigma=4.0, tau=25.0)
    P.ai(e, "NOxStack", "NOx stack", "ppm", 330.0, idle=250.0, hi=600.0, prec=0, sigma=4.0, tau=25.0)
    P.ai(e, "DustStack", "Dust at stack", "mg/m3", 24.0, idle=15.0, hi=100.0, prec=1, sigma=0.8, tau=25.0)

    # Feedwater and condensate -------------------------------------------------------------------------
    S = "Feedwater"
    for k, thr in zip("ABC", (0.0, 0.0, 0.85)):
        e = P.equip(S, "BFP" + k, "Boiler feed pump " + k, always=False, threshold=thr, trip_h=0.1)
        _motor(P, e, 420.0, bearing=74.0, vibn=35.0, windings=True)
        P.ai(e, "SuctionPress", e.label + " suction pressure", "bar", 11.5, idle=11.5, hi=18.0, sigma=0.05, prec=2)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 205.0, idle=185.0, hi=260.0, sigma=0.5)
        P.ai(e, "Flow", e.label + " flow", "m3/hr", 1050.0, idle=520.0, hi=1500.0, prec=0, tau=10.0)
        P.ai(e, "LubeOilPress", e.label + " lube oil pressure", "bar", 2.2, idle=2.2, hi=4.0, sigma=0.03, prec=2)
        P.temp(e, "LubeOilTemp", e.label + " lube oil temp", 48.0, idle=45.0, hi=85.0)
        P.ai(e, "SealWaterPress", e.label + " seal water pressure", "bar", 13.0, idle=13.0, hi=20.0, sigma=0.05, prec=2)
        P.ai(e, "ScoopPos", e.label + " scoop tube position", "%", 74.0, idle=48.0, hi=100.0, sigma=0.3, tau=8.0)
    for k, thr in zip("ABC", (0.0, 0.0, 0.8)):
        e = P.equip(S, "CEP" + k, "Condensate pump " + k, always=False, threshold=thr, trip_h=0.1)
        _motor(P, e, 62.0, bearing=58.0, vibn=30.0, windings=False, extra_vib=False)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 31.0, idle=29.0, hi=45.0, sigma=0.1, prec=2)
    for n in (1, 2, 3):
        e = P.equip(S, "HPH%d" % n, "HP heater %d" % n)
        P.ai(e, "ShellPress", e.label + " shell pressure", "bar", [44.0, 26.0, 13.0][n - 1], idle=[20.0, 11.0, 5.5][n - 1], hi=70.0, sigma=0.1, prec=2, tau=25.0)
        P.temp(e, "FWInTemp", e.label + " feedwater inlet temp", [188.0, 160.0, 135.0][n - 1], idle=[160.0, 132.0, 108.0][n - 1], hi=260.0, tau=40.0)
        P.temp(e, "FWOutTemp", e.label + " feedwater outlet temp", [252.0, 188.0, 160.0][n - 1], idle=[215.0, 160.0, 132.0][n - 1], hi=290.0, tau=40.0)
        P.ai(e, "Level", e.label + " shell level", "mm", 0.0, idle=0.0, lo=-150.0, hi=250.0, sigma=2.0, tau=15.0)
        P.ai(e, "DrainValve", e.label + " drain valve position", "%", 38.0, idle=22.0, hi=100.0, sigma=0.5, tau=10.0)
    for n in (1, 2, 3, 4):
        e = P.equip(S, "LPH%d" % n, "LP heater %d" % n)
        P.ai(e, "ShellPress", e.label + " shell pressure", "bar", [4.2, 2.1, 0.9, 0.35][n - 1], idle=[2.2, 1.1, 0.5, 0.2][n - 1], hi=8.0, sigma=0.02, prec=3, tau=25.0)
        P.temp(e, "FWInTemp", e.label + " condensate inlet temp", [90.0, 72.0, 52.0, 40.0][n - 1], idle=[75.0, 58.0, 44.0, 38.0][n - 1], hi=150.0, tau=40.0)
        P.temp(e, "FWOutTemp", e.label + " condensate outlet temp", [118.0, 90.0, 72.0, 52.0][n - 1], idle=[95.0, 75.0, 58.0, 44.0][n - 1], hi=170.0, tau=40.0)
        P.ai(e, "Level", e.label + " shell level", "mm", 0.0, idle=0.0, lo=-150.0, hi=250.0, sigma=2.0, tau=15.0)
    e = P.equip(S, "Deaerator", "Deaerator")
    P.ai(e, "Pressure", "Deaerator pressure", "bar", 8.5, idle=4.8, hi=14.0, sigma=0.03, prec=2, tau=25.0, low=True)
    P.ai(e, "Level", "Deaerator level", "%", 62.0, idle=62.0, lo=0.0, hi=100.0, sigma=0.4, tau=20.0, low=True)
    P.temp(e, "Temp", "Deaerator temperature", 172.0, idle=150.0, hi=220.0, tau=40.0)
    P.ai(e, "OutletO2", "Deaerator outlet dissolved O2", "ppb", 5.0, idle=4.0, hi=40.0, sigma=0.3, prec=1)

    # Turbine ------------------------------------------------------------------------------------------
    S = "Turbine"
    e = P.equip(S, "Turbine", "Turbine")
    for n in (1, 2, 3):
        P.ai(e, "Speed%d" % n, "Shaft speed channel %d" % n, "Hz", 50.0, idle=50.0, lo=0.0, hi=60.0, sigma=0.01, prec=2, tau=2.0)
    P.ai(e, "SteamChestPress", "HP steam chest pressure", "bar", 160.0, idle=150.0, hi=200.0, sigma=0.4, tau=20.0)
    P.temp(e, "SteamChestTemp", "HP steam chest temperature", 536.0, idle=520.0, hi=600.0, sigma=0.8, tau=40.0)
    P.ai(e, "HPExhaustPress", "HP exhaust pressure", "bar", 42.0, idle=22.0, hi=60.0, sigma=0.15, prec=2, tau=20.0)
    P.temp(e, "HPExhaustTemp", "HP exhaust temperature", 335.0, idle=300.0, hi=430.0, tau=40.0)
    P.ai(e, "IPInletPress", "IP inlet pressure", "bar", 38.0, idle=21.0, hi=55.0, sigma=0.15, prec=2, tau=20.0)
    P.ai(e, "LPExhaustPress", "LP exhaust pressure", "kPa", 8.5, idle=5.5, hi=22.0, sigma=0.05, prec=2, tau=25.0)
    P.temp(e, "LPExhaustTemp", "LP exhaust temperature", 43.0, idle=35.0, hi=90.0, tau=40.0)
    P.ai(e, "ExtractionPress1", "Extraction 1 pressure (HPH-1)", "bar", 46.0, idle=21.0, hi=70.0, sigma=0.15, prec=2)
    P.ai(e, "ExtractionPress4", "Extraction 4 pressure (deaerator)", "bar", 9.0, idle=5.0, hi=14.0, sigma=0.05, prec=2)
    for n in range(1, 9):
        P.temp(e, "BrgMetalTemp%d" % n, "Bearing %d metal temperature" % n, 84.0 - n, idle=74.0 - n, hi=130.0, sigma=0.3, tau=60.0)
        P.vib(e, "BrgVibX%d" % n, "Bearing %d vibration X" % n, 42.0 + 2 * n, off=None)
        P.vib(e, "BrgVibY%d" % n, "Bearing %d vibration Y" % n, 38.0 + 2 * n, off=None)
    for n in (1, 2, 3, 4):
        P.temp(e, "ThrustPadTemp%d" % n, "Thrust bearing pad %d temp" % n, 78.0, idle=68.0, hi=115.0, sigma=0.3, tau=60.0)
    for n in (1, 2):
        P.ai(e, "AxialShift%d" % n, "Axial shift channel %d" % n, "mm", 0.18, idle=0.1, lo=-1.5, hi=1.5, sigma=0.01, prec=2, tau=20.0)
    for tag, d in (("HP", 3.2), ("IP", 2.4), ("LP", 5.8)):
        P.ai(e, tag + "DiffExpansion", tag + " differential expansion", "mm", d, idle=d * 0.4, lo=-3.0, hi=d * 2.2, sigma=0.03, prec=2, tau=90.0)
    P.ai(e, "CasingExpansion", "HP casing expansion", "mm", 24.0, idle=14.0, hi=45.0, sigma=0.08, prec=2, tau=120.0)
    P.ai(e, "RotorEccentricity", "Rotor eccentricity", "um", 22.0, idle=20.0, hi=75.0, sigma=0.5, prec=1, tau=10.0)
    for tag in ("HPUpper", "HPLower", "IPUpper", "IPLower"):
        P.temp(e, tag + "MetalTemp", "%s casing metal temp" % tag, 505.0 if tag[0] == "H" else 470.0, idle=440.0, hi=580.0, sigma=0.6, tau=120.0)
    P.ai(e, "EHCOilPress", "EHC oil pressure", "bar", 140.0, idle=140.0, hi=180.0, sigma=0.3, prec=1, low=True)
    P.temp(e, "EHCOilTemp", "EHC oil temperature", 46.0, idle=44.0, hi=75.0)
    P.ai(e, "LubeOilPress", "Lube oil header pressure", "bar", 1.9, idle=1.9, hi=3.2, sigma=0.02, prec=2, low=True)
    P.temp(e, "LubeOilTemp", "Lube oil header temperature", 47.0, idle=43.0, hi=75.0)
    P.ai(e, "LubeOilTankLevel", "Lube oil tank level", "%", 74.0, idle=74.0, hi=100.0, sigma=0.2, tau=60.0, low=True)
    for n in (1, 2):
        P.ai(e, "HPStopValve%d" % n, "HP stop valve %d position" % n, "%", 100.0, idle=100.0, lo=0.0, hi=110.0, sigma=0.05, tau=4.0)
    P.ai(e, "IPStopValve", "IP stop valve position", "%", 100.0, idle=100.0, lo=0.0, hi=110.0, sigma=0.05, tau=4.0)
    for n in (1, 2, 3, 4):
        P.ai(e, "ControlValve%d" % n, "HP control valve %d position" % n, "%", 62.0 + 3 * n, idle=36.0 + 3 * n, hi=100.0, sigma=0.2, tau=6.0)
    P.sw(e, "TurbineTrip", "Turbine trip", "NORMAL", "TRIPPED", "MAJOR", rate_h=0.0, dur=30)
    P.const(e, "TurningGear", "Turning gear engaged", "DISENGAGED", "ENGAGED", 0)
    for k in ("MOP", "AOP", "EOP"):
        ee = P.equip(S, "Oil" + k, "Oil pump " + k, always=(k == "MOP"), threshold=0.0, trip_h=0.0)
        P.run(ee, desc="Oil pump %s run status" % k)
        P.cur(ee, "MotorCurrent", "Oil pump %s motor current" % k, 38.0, idle=36.0)

    # Generator and electrical ---------------------------------------------------------------------------
    S = "Generator"
    e = P.equip(S, "Generator", "Generator")
    P.ai(e, "ActivePower", "Generator active power", "MW", 300.0, idle=160.0, hi=360.0, sigma=0.5, prec=1, tau=6.0)
    P.ai(e, "Frequency", "Grid frequency", "Hz", 50.0, idle=50.0, lo=48.0, hi=52.0, sigma=0.012, prec=3, tau=2.0, low=True)
    P.ai(e, "TerminalVoltage", "Terminal voltage", "V", 15750.0, idle=15750.0, lo=0.0, hi=18000.0, sigma=18.0, prec=0, tau=4.0, low=True)
    for ph in "RYB":
        P.cur(e, "StatorCurrent" + ph, "Stator current phase " + ph, 11500.0, idle=6200.0, hi=14500.0, prec=0, sigma=20.0, tau=5.0)
    P.ai(e, "PowerFactor", "Power factor", "", 0.88, idle=0.9, lo=0.5, hi=1.0, sigma=0.002, prec=3, tau=5.0)
    for n in range(1, 7):
        P.temp(e, "StatorWindTemp%d" % n, "Stator winding temp %d" % n, 78.0 + n % 3, idle=52.0, hi=130.0, sigma=0.3, tau=90.0)
    for n in (1, 2, 3):
        P.temp(e, "StatorCoreTemp%d" % n, "Stator core temp %d" % n, 71.0, idle=48.0, hi=120.0, sigma=0.3, tau=90.0)
    for n in (1, 2, 3, 4):
        P.temp(e, "ColdGasTemp%d" % n, "Cold hydrogen temp %d" % n, 42.0, idle=40.0, hi=70.0, sigma=0.15, tau=90.0)
        P.temp(e, "HotGasTemp%d" % n, "Hot hydrogen temp %d" % n, 66.0, idle=48.0, hi=100.0, sigma=0.2, tau=90.0)
    P.ai(e, "H2Pressure", "Hydrogen pressure", "bar", 3.1, idle=3.1, hi=5.0, sigma=0.01, prec=3, tau=60.0, low=True)
    P.ai(e, "H2Purity", "Hydrogen purity", "%", 98.6, idle=98.6, lo=90.0, hi=100.0, sigma=0.03, prec=2, tau=120.0, low=True)
    P.temp(e, "H2DewPoint", "Hydrogen dew point", -8.0, idle=-9.0, hi=10.0, lo=-30.0, sigma=0.1, tau=120.0)
    P.ai(e, "SealOilPress", "Seal oil pressure", "bar", 5.6, idle=5.6, hi=8.5, sigma=0.02, prec=2, low=True)
    P.temp(e, "SealOilTemp", "Seal oil temperature", 44.0, idle=42.0, hi=70.0)
    P.ai(e, "StatorCoolFlow", "Stator cooling water flow", "m3/hr", 42.0, idle=42.0, hi=60.0, sigma=0.15, low=True)
    P.temp(e, "StatorCoolIn", "Stator cooling water inlet", 40.0, idle=39.0, hi=65.0, sigma=0.1)
    P.temp(e, "StatorCoolOut", "Stator cooling water outlet", 52.0, idle=43.0, hi=80.0, sigma=0.15, tau=60.0)
    P.ai(e, "StatorCoolCond", "Stator cooling conductivity (uS/cm)", "", 0.6, idle=0.5, hi=5.0, sigma=0.01, prec=2)
    P.ai(e, "ExcitationVoltage", "Excitation voltage", "V", 310.0, idle=190.0, hi=520.0, sigma=0.8, tau=6.0)
    P.cur(e, "ExcitationCurrent", "Excitation current", 2900.0, idle=1700.0, hi=4200.0, prec=0, sigma=6.0)
    P.temp(e, "FieldWindTemp", "Field winding temperature", 92.0, idle=62.0, hi=150.0, sigma=0.3, tau=120.0)
    P.const(e, "Breaker", "Generator breaker", "OPEN", "CLOSED", 1, zsv="MAJOR")
    P.const(e, "AVRAuto", "AVR in auto mode", "MANUAL", "AUTO", 1, zsv="MINOR")
    for k, kv, cap in (("A", 11.0, 25.0), ("B", 11.0, 25.0), ("GT", 400.0, 360.0)):
        e = P.equip("Electrical", "Transformer" + k, "Transformer " + k)
        P.temp(e, "OilTemp", e.label + " top oil temp", 62.0, idle=44.0, hi=105.0, sigma=0.3, tau=120.0)
        P.temp(e, "WindingTemp", e.label + " winding temp", 74.0, idle=48.0, hi=125.0, sigma=0.3, tau=120.0)
        P.ai(e, "OilLevel", e.label + " oil level", "%", 70.0, idle=70.0, hi=100.0, sigma=0.1, tau=300.0, low=True)
        P.sw(e, "BuchholzAlarm", e.label + " Buchholz relay", "NORMAL", "ALARM", "MAJOR", rate_h=0.02, dur=60)
    for name, volts in (("Bus11kVA", 11000.0), ("Bus11kVB", 11000.0), ("Bus6k6VA", 6600.0), ("Bus6k6VB", 6600.0),
                        ("Bus415VA", 415.0), ("Bus415VB", 415.0)):
        e = P.equip("Electrical", name, "Bus " + name[3:])
        P.ai(e, "Voltage", e.label + " voltage", "V", volts, idle=volts * 1.004, lo=0.0, hi=volts * 1.2, sigma=volts * 0.0015,
             prec=0 if volts > 1000 else 1, tau=5.0, low=True)
        P.ai(e, "Frequency", e.label + " frequency", "Hz", 50.0, idle=50.0, lo=48.0, hi=52.0, sigma=0.012, prec=3, tau=2.0, low=True)
        P.const(e, "IncomerBreaker", e.label + " incomer breaker", "OPEN", "CLOSED", 1, zsv="MAJOR")
        P.const(e, "TieBreaker", e.label + " bus tie breaker", "OPEN", "CLOSED", 0)
    e = P.equip("Electrical", "DCSystem", "DC system")
    P.ai(e, "Battery220V", "220 V battery voltage", "V", 231.0, idle=231.0, lo=190.0, hi=260.0, sigma=0.15, prec=1, tau=60.0, low=True)
    P.ai(e, "Battery24V", "24 V battery voltage", "V", 26.4, idle=26.4, lo=20.0, hi=30.0, sigma=0.03, prec=2, tau=60.0, low=True)
    P.cur(e, "ChargerCurrent", "Battery charger current", 18.0, idle=16.0)
    P.sw(e, "EarthFault", "DC earth fault", "NORMAL", "EARTH FAULT", "MAJOR", rate_h=0.03, dur=120)
    e = P.equip("Electrical", "Diesel", "Emergency diesel generator", always=False, threshold=9.0)
    P.run(e)
    P.const(e, "Ready", "Diesel generator ready", "NOT READY", "READY", 1, zsv="MINOR")
    P.ai(e, "FuelLevel", "Diesel fuel tank level", "%", 83.0, idle=83.0, hi=100.0, sigma=0.05, tau=600.0, low=True, off=None)

    # Condenser and cooling water -----------------------------------------------------------------------
    S = "Condenser and Cooling"
    for k in "AB":
        e = P.equip(S, "Condenser" + k, "Condenser shell " + k)
        P.ai(e, "Vacuum", e.label + " absolute pressure", "kPa", 8.5, idle=5.5, hi=22.0, sigma=0.05, prec=2, tau=25.0)
        P.temp(e, "CWInTemp", e.label + " CW inlet temp", 31.0, idle=30.0, hi=50.0, sigma=0.1, tau=120.0)
        P.temp(e, "CWOutTemp", e.label + " CW outlet temp", 40.0, idle=33.0, hi=60.0, sigma=0.12, tau=90.0)
        P.ai(e, "HotwellLevel", e.label + " hotwell level", "mm", 0.0, idle=0.0, lo=-400.0, hi=400.0, sigma=3.0, tau=20.0, low=True)
        P.temp(e, "HotwellTemp", e.label + " hotwell temp", 43.0, idle=36.0, hi=75.0, sigma=0.15, tau=60.0)
        P.ai(e, "WaterboxDP", e.label + " waterbox differential press", "kPa", 24.0, idle=18.0, hi=60.0, sigma=0.2, prec=1, tau=60.0)
    for k, thr in zip("ABC", (0.0, 0.0, 0.7)):
        e = P.equip(S, "CWP" + k, "Cooling water pump " + k, always=False, threshold=thr, trip_h=0.1)
        _motor(P, e, 190.0, bearing=64.0, vibn=34.0)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 2.6, idle=2.4, hi=4.5, sigma=0.03, prec=2)
        P.ai(e, "Flow", e.label + " flow", "m3/hr", 15500.0, idle=15000.0, hi=21000.0, prec=0, tau=10.0)
    for n in range(1, 7):
        e = P.equip(S, "CTFan%d" % n, "Cooling tower fan %d" % n, always=False, threshold=[0.0, 0.0, 0.3, 0.5, 0.7, 0.9][n - 1], trip_h=0.1)
        P.run(e)
        P.trip(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 94.0)
        P.vib(e, "Vib", e.label + " vibration", 30.0)
    e = P.equip(S, "CoolingTower", "Cooling tower")
    P.ai(e, "BasinLevel", "Basin level", "m", 2.6, idle=2.6, hi=4.0, sigma=0.01, prec=2, tau=60.0, low=True)
    P.temp(e, "BasinTemp", "Basin cold water temp", 30.5, idle=29.5, hi=45.0, sigma=0.1, tau=180.0)
    P.temp(e, "HotWaterTemp", "Tower hot water inlet temp", 40.5, idle=33.0, hi=55.0, sigma=0.1, tau=120.0)
    P.ai(e, "MakeupFlow", "Makeup water flow", "m3/hr", 620.0, idle=300.0, hi=900.0, prec=0, sigma=4.0)
    P.ai(e, "Blowdown", "Blowdown flow", "m3/hr", 160.0, idle=80.0, hi=300.0, prec=0, sigma=2.0)
    for k, thr in zip("AB", (0.0, 0.5)):
        e = P.equip(S, "VacPump" + k, "Vacuum pump " + k, always=False, threshold=thr, trip_h=0.1)
        P.run(e)
        P.trip(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 72.0)
        P.temp(e, "SealWaterTemp", e.label + " seal water temp", 34.0, idle=31.0, hi=65.0)

    # Ash handling -------------------------------------------------------------------------------------
    S = "Ash Handling"
    e = P.equip(S, "BottomAsh", "Bottom ash")
    P.ai(e, "HopperLevel", "Bottom ash hopper level", "%", 54.0, idle=40.0, hi=100.0, sigma=0.5, tau=90.0)
    P.temp(e, "QuenchWaterTemp", "Quench water temperature", 58.0, idle=48.0, hi=95.0, tau=90.0)
    P.cur(e, "GrinderCurrent", "Clinker grinder current", 52.0, idle=34.0)
    P.ai(e, "JetPumpPress", "Jet pump pressure", "bar", 6.8, idle=6.8, hi=10.0, sigma=0.05, prec=2)
    for n in range(1, 13):
        e = P.equip(S, "FlyAshHopper%d" % n, "Fly ash hopper %d" % n)
        P.temp(e, "Temp", e.label + " temperature", 142.0, idle=124.0, hi=220.0, sigma=0.6, tau=90.0)
        P.sw(e, "LevelHigh", e.label + " high level", "NORMAL", "HIGH", "MINOR", rate_h=0.4, dur=140)
        P.ai(e, "Level", e.label + " level", "%", 48.0, idle=32.0, hi=100.0, sigma=0.5, tau=120.0)
    for k in "AB":
        e = P.equip(S, "AshSilo" + k, "Ash silo " + k)
        P.ai(e, "Level", e.label + " level", "%", 58.0, idle=46.0, hi=100.0, sigma=0.2, tau=240.0)
    for k, thr in zip("ABC", (0.0, 0.0, 0.8)):
        e = P.equip(S, "AshComp" + k, "Ash conveying compressor " + k, always=False, threshold=thr, trip_h=0.2)
        P.run(e)
        P.trip(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 168.0)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 7.5, idle=7.2, hi=10.0, sigma=0.04, prec=2)
        P.temp(e, "DischargeTemp", e.label + " discharge temp", 92.0, idle=80.0, hi=135.0)

    # Water treatment, chemistry, compressed air -----------------------------------------------------------
    S = "Water and Utilities"
    e = P.equip(S, "BoilerChem", "Boiler water chemistry")
    for point, desc, egu, nom, hi_, prec in (
            ("DrumPH", "Drum water pH", "", 9.3, 10.5, 2), ("DrumCond", "Drum water conductivity (uS/cm)", "", 18.0, 40.0, 1),
            ("DrumSilica", "Drum water silica (ppb)", "", 120.0, 400.0, 0), ("FeedwaterPH", "Feedwater pH", "", 9.1, 10.5, 2),
            ("FeedwaterDO", "Feedwater dissolved O2 (ppb)", "", 4.0, 20.0, 1), ("FeedwaterCond", "Feedwater cation cond (uS/cm)", "", 0.12, 0.6, 3),
            ("SteamSodium", "Main steam sodium (ppb)", "", 1.5, 8.0, 2), ("SteamCond", "Main steam cation cond (uS/cm)", "", 0.1, 0.5, 3),
            ("CondensateCond", "Condensate cation cond (uS/cm)", "", 0.15, 0.8, 3), ("HotwellSodium", "Hotwell sodium (ppb)", "", 2.0, 10.0, 2)):
        P.ai(e, point, desc, egu, nom, idle=nom * 0.9, lo=0.0 if nom < 8 or hi_ > 11 else 6.0, hi=hi_, sigma=hi_ * 0.003, prec=prec, tau=120.0)
    e = P.equip(S, "DMPlant", "Demineralised water plant")
    P.ai(e, "ProductCond", "DM product conductivity (uS/cm)", "", 0.08, idle=0.07, hi=0.4, sigma=0.002, prec=3, tau=60.0)
    P.ai(e, "Silica", "DM product silica (ppb)", "", 3.0, idle=2.5, hi=20.0, sigma=0.1, prec=1, tau=60.0)
    P.ai(e, "TankLevel", "DM water tank level", "%", 74.0, idle=82.0, hi=100.0, sigma=0.2, tau=120.0, low=True)
    P.ai(e, "ProductFlow", "DM product flow", "m3/hr", 38.0, idle=14.0, hi=70.0, sigma=0.4)
    e = P.equip(S, "CondensateTank", "Condensate storage tank")
    P.ai(e, "Level", "Condensate tank level", "%", 76.0, idle=84.0, hi=100.0, sigma=0.15, tau=120.0, low=True)
    for k, thr in zip("ABC", (0.0, 0.0, 0.9)):
        e = P.equip(S, "InstAir" + k, "Instrument air compressor " + k, always=False, threshold=thr, trip_h=0.15)
        P.run(e)
        P.trip(e)
        P.cur(e, "MotorCurrent", e.label + " motor current", 112.0, idle=96.0)
        P.ai(e, "DischargePress", e.label + " discharge pressure", "bar", 8.2, idle=8.0, hi=11.0, sigma=0.04, prec=2)
        P.temp(e, "DischargeTemp", e.label + " discharge temp", 96.0, idle=88.0, hi=140.0)
    e = P.equip(S, "AirReceivers", "Compressed air receivers")
    P.ai(e, "InstrAirHeader", "Instrument air header pressure", "bar", 7.4, idle=7.4, hi=10.0, sigma=0.03, prec=2, low=True)
    P.ai(e, "ServiceAirHeader", "Service air header pressure", "bar", 6.9, idle=6.9, hi=9.0, sigma=0.03, prec=2, low=True)
    P.ai(e, "DewPoint", "Instrument air dew point", "degC", -42.0, idle=-42.0, lo=-70.0, hi=0.0, sigma=0.2, prec=1, tau=120.0)
    e = P.equip(S, "FireWater", "Fire water system")
    P.ai(e, "HeaderPress", "Fire water header pressure", "bar", 9.2, idle=9.2, hi=12.0, sigma=0.02, prec=2, low=True)
    P.sw(e, "JockeyPumpRun", "Jockey pump running", "STOPPED", "RUNNING", "NO_ALARM", rate_h=6.0, dur=40)

    # Bulk metal-temperature arrays (thermocouples), added until the requested count is reached ------------
    bulk = [("Waterwall", "Boiler", "WaterwallTC", "Waterwall tube metal TC", 428.0, 360.0, 560.0),
            ("Superheater", "Boiler", "SHTubeTC", "Superheater tube metal TC", 565.0, 505.0, 660.0),
            ("Reheater", "Boiler", "RHTubeTC", "Reheater tube metal TC", 575.0, 510.0, 665.0),
            ("Roof", "Boiler", "RoofTC", "Furnace roof metal TC", 410.0, 350.0, 520.0)]
    for key, system, _, _, _, _, _ in bulk:
        P.equip(system, key, key)
    base_count = len(P.points)
    if count < base_count:
        P.points = P.points[:count]
        return P
    n = 0
    while len(P.points) < count:
        key, system, point, desc, nom, idle, hi = bulk[n % len(bulk)]
        idx = n // len(bulk) + 1
        e = P.equips[key]
        P.temp(e, "%s%03d" % (point, idx), "%s %03d" % (desc, idx), nom + (n * 7 % 23) - 11, idle=idle + (n * 5 % 17) - 8, hi=hi, sigma=0.8, tau=70.0 + (n % 5) * 10)
        n += 1
    return P
