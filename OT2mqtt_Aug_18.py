import json
import os
from queue import Queue, Empty
from time import sleep, time
import paho.mqtt.client as mqtt
import opentrons.execute
import logging
import time as pytime
from datetime import datetime, timezone
from opentrons.types import Point
from threading import Timer

# =========================
# Config
# =========================
DEBUG = True

# --- Sensor state (prevents out-of-sequence sensor_status from doing moves) ---
ACTIVE_SENSOR_EXPERIMENT_ID = None   # str | None
SENSOR_DEPLOYED = False              # True only after sensor has been picked up and moved to well
SENSOR_DEPLOYED_UNIX_S = None        # debug timestamp

count = 0

rindex = 0

cindex = 1

TIP_RACK_EMPTY = False
WELL_PLATE_FULL = False

# Fixed tip positions per color (one reusable tip each)
COLOR_TIP_WELLS = {"R": "B1", "Y": "B2", "B": "B3"}

ROWS = ["A", "B", "C", "D", "E", "F", "G", "H"]

PLATE_Z_OFFSET = 5  # mm; physical riser under wellplate in slot 1

SENSOR_PICKUP_Z = -1.6
SENSOR_DROPOFF_Z = -80

AUTO_HOME_AFTER_EXPERIMENT = True  # home after sensor return ("experiment complete")
HOME_EVERY_N_EXPERIMENTS = 10  # tune later (e.g., 5–25). 1 means "every experiment".

completed_experiments_since_home = 0

TIP_RETURN_METHOD = "seat_then_drop"  # "return_tip", "drop_deeper", "seat_then_drop"

# Tune this slowly: start -12, then -16, -20… until it reseats reliably
TIPRACK_RETURN_Z = -52          # release height (below well top)
TIPRACK_SEAT_Z = -56            # seat height (a bit deeper than release)
TIPRACK_SEAT_PAUSE_S = 0.10     # tiny dwell to let friction settle
TIPRACK_SEAT_SPEED = 40         # slower vertical motion can reduce tip “hangups”

# Waste clearing tuning
WASTE_WELL_NAME = "A1"
WASTE_BLOWOUT_Z = -35            # deeper into the tube
WASTE_BLOWOUT_PASSES = 3         # try 2–5
WASTE_HOME_PLUNGER_BETWEEN = True
WASTE_PLUNGER_RESET_DELAY_S = 0.05

# Gentler wipe (less “smash”), with lifting between wipes
WASTE_TOUCH_V_OFFSET = -4        # near the mouth, not deep
WASTE_TOUCH_SPEED = 30
WASTE_TOUCH_RADIUS = 0.9
WASTE_TOUCH_CYCLES = 2
WASTE_LIFT_Z = 10                # lift above the tube between wipes

#Sensor timing

MIN_SENSOR_MEASURE_TIME = 3.5

#Debug
SIMULATE_PICO_SENSOR = False #turn to True if you have issues
SIMULATED_SENSOR_DELAY_S = 3
DUMMY_3_CYCLE_MODE = False
dummy_idx = 0

DUMMY_COMMANDS = [
    {"session_id" : "dummy", "experiment_id" : "dummy_001", "command" : {"R" : 0, "Y" : 0, "B" : 0, "well" : "A1"}},
    {"session_id" : "dummy", "experiment_id" : "dummy_002", "command" : {"R" : 0, "Y" : 0, "B" : 0, "well" : "A3"}},
    {"session_id" : "dummy", "experiment_id" : "dummy_003", "command" : {"R" : 0, "Y" : 0, "B" : 0, "well" : "A2"}}
]

dummy_idx = 0
dummy_cycle = 0

def enqueue_next_dummy():
    global dummy_idx, dummy_cycle

    if not DUMMY_3_CYCLE_MODE:
        return

    template = DUMMY_COMMANDS[dummy_idx]

    payload = {
        "session_id": "dummy",
        "experiment_id": f"dummy_{dummy_cycle:06d}",
        "command": template["command"].copy()
    }

    log(
        f"[dummy] queueing idx={dummy_idx} "
        f"experiment_id={payload['experiment_id']} "
        f"well={payload['command']['well']}"
    )

    command_queue.put(payload)

    dummy_idx = (dummy_idx + 1) % len(DUMMY_COMMANDS)
    dummy_cycle += 1
    
    
    
    dummy_idx = (dummy_idx + 1) % len(DUMMY_COMMANDS)
def fake_sensor_complete(session_id, experiment_id):
    command_queue.put({
        "session_id": session_id,
        "experiment_id" : experiment_id, 
        "command": {"sensor_status": "sensor_complete"}
    })

# =========================
# Per-run logging
# =========================
LOG_DIR = "/var/lib/jupyter/notebooks/ot2mqtt_run_logs"
os.makedirs(LOG_DIR, exist_ok=True)

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
MAIN_LOG_PATH = os.path.join(LOG_DIR, f"ot2mqtt_{RUN_ID}.log")
MQTT_LOG_PATH = os.path.join(LOG_DIR, f"ot2mqtt_mqtt_{RUN_ID}.log")

def _make_logger(name: str, path: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = pytime.gmtime  # UTC timestamps

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Optional: also emit to stdout during interactive debugging
    if DEBUG:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger

LOG = _make_logger("ot2mqtt", MAIN_LOG_PATH)
MQTTLOG = _make_logger("ot2mqtt.mqtt", MQTT_LOG_PATH)

def log(msg: str) -> None:
    LOG.info(msg)

def log_err(msg: str) -> None:
    LOG.error(msg)

def mqtt_log(msg: str) -> None:
    MQTTLOG.info(msg)

log(f"=== OT2mqtt starting RUN_ID={RUN_ID} pid={os.getpid()} ===")
log(f"MAIN_LOG_PATH={MAIN_LOG_PATH}")
log(f"MQTT_LOG_PATH={MQTT_LOG_PATH}")

# =========================
# MQTT Setup
# =========================

protocol = opentrons.execute.get_protocol_api("2.12")

OT2_SERIAL = "OT2CEP20240218R04"
PICO_ID = "e66130100f895134"

# MQTT Broker Configuration
host = "248cc294c37642359297f75b7b023374.s2.eu.hivemq.cloud"
username = "sgbaird"
password = "D.Pq5gYtejYbU#L"
port = 8883

OT2_COMMAND_TOPIC = f"command/ot2/{OT2_SERIAL}/pipette"
OT2_STATUS_TOPIC = f"status/ot2/{OT2_SERIAL}/complete"

client = mqtt.Client()
client.tls_set(tls_version=mqtt.ssl.PROTOCOL_TLS_CLIENT)  # type: ignore
client.username_pw_set(username, password)

command_queue = Queue()


def on_connect(client, userdata, flags, rc):
    mqtt_log(f"CONNECT rc={rc} subscribe={OT2_COMMAND_TOPIC}")
    client.subscribe(OT2_COMMAND_TOPIC, qos=2)

def on_message(client, userdata, msg):
    payload_str = msg.payload.decode("utf-8", errors="replace")
    mqtt_log(
        f"RX topic={msg.topic} qos={getattr(msg,'qos',None)} "
        f"retain={getattr(msg,'retain',None)} dup={getattr(msg,'dup',None)} "
        f"payload={payload_str}"
    )
    try:
        payload = json.loads(payload_str)
        if msg.topic == OT2_COMMAND_TOPIC:
            command_queue.put(payload)
            log(f"[queue] enqueued experiment_id={payload.get('experiment_id')} keys={list(payload.get('command',{}).keys())}")
    except json.JSONDecodeError as e:
        mqtt_log(f"RX JSON_DECODE_ERROR {e} payload={payload_str}")
        log_err(f"[mqtt] JSON decode error: {e}")

client.on_connect = on_connect
client.on_message = on_message
client.connect(host, port)
client.loop_start()

log("MQTT client connected")

# Home once at startup as your "I'm alive" check
protocol.home()

# =========================
# Labware / instruments
# =========================
with open("/var/lib/jupyter/notebooks/ac_color_sensor_charging_port.json") as labware_file1:
    labware_def1 = json.load(labware_file1)
    tiprack_2 = protocol.load_labware_from_definition(labware_def1, 10)

with open("/var/lib/jupyter/notebooks/ac_6_tuberack_15000ul.json") as labware_file2:
    labware_def2 = json.load(labware_file2)
    reservoir = protocol.load_labware_from_definition(labware_def2, 3)

plate = protocol.load_labware(
    load_name="greiner_96_wellplate_323ul",
    location=1,
)
#with open ("/var/lib/jupyter/notebooks/35_tube_rack_2ml_v2.json") as labware_file3:
    #labware_def3 = json.load(labware_file3)
    #plate = protocol.load_labware_from_definition(labware_def3, 1)
#
tiprack_1 = protocol.load_labware(
    load_name="opentrons_96_tiprack_300ul",
    location=9,
)

p300 = protocol.load_instrument(
    instrument_name="p300_single_gen2",
    mount="right",
    tip_racks=[tiprack_1],
)

#trash = protocol.fixed_trash

p300.well_bottom_clearance.dispense = 8 #19.25
#remember 19.25

log("Labwares loaded")

def pause_for_tip_reload():
    global client

    payload = {
        "status": {
            "robot_status": "out_of_tips"
        }
    }

    mqtt_log(
        f"TX topic={OT2_STATUS_TOPIC} payload={json.dumps(payload)}"
    )

    client.publish(
        OT2_STATUS_TOPIC,
        json.dumps(payload),
        qos=2
    )

    while True:
        response = input(
            "\nReload the P300 tip rack and type 'y' to continue: "
        ).strip().lower()

        if response == "y":
            break

    p300.reset_tipracks()

    payload = {
        "status": {
            "robot_status": "tips_reloaded"
        }
    }

    mqtt_log(
        f"TX topic={OT2_STATUS_TOPIC} payload={json.dumps(payload)}"
    )

    client.publish(
        OT2_STATUS_TOPIC,
        json.dumps(payload),
        qos=2
    )
    
def pause_for_plate_reload():

    global client

    payload = {
        "status": {
            "robot_status": "plate_full"
        }
    }

    mqtt_log(
        f"TX topic={OT2_STATUS_TOPIC} payload={json.dumps(payload)}"
    )

    client.publish(
        OT2_STATUS_TOPIC,
        json.dumps(payload),
        qos=2
    )

    while True:

        response = input(
            "\nReplace the 96-well plate and type 'x' to continue: "
        ).strip().lower()

        if response == "x":
            break

    payload = {
        "status": {
            "robot_status": "plate_reloaded"
        }
    }

    mqtt_log(
        f"TX topic={OT2_STATUS_TOPIC} payload={json.dumps(payload)}"
    )

    client.publish(
        OT2_STATUS_TOPIC,
        json.dumps(payload),
        qos=2
    )

def clear_tip_in_waste() -> None:
    """More aggressive purge: multiple blowouts with plunger reset + gentle wipe/lift cycles."""
    waste_well = reservoir[WASTE_WELL_NAME]
    blow_loc = waste_well.top(z=WASTE_BLOWOUT_Z)

    # Multiple blow-outs (air pulses) with plunger reset between them
    for i in range(WASTE_BLOWOUT_PASSES):
        p300.blow_out(blow_loc)

        if WASTE_HOME_PLUNGER_BETWEEN and i < (WASTE_BLOWOUT_PASSES - 1):
            p300.home_plunger()
            protocol.delay(seconds=WASTE_PLUNGER_RESET_DELAY_S)

    # Gentle wipe cycles with lift between (less wall-smashing)
    for _ in range(WASTE_TOUCH_CYCLES):
        try:
            p300.touch_tip(
                waste_well,
                radius=WASTE_TOUCH_RADIUS,
                v_offset=WASTE_TOUCH_V_OFFSET,
                speed=WASTE_TOUCH_SPEED,
            )
            p300.move_to(waste_well.top(z=WASTE_LIFT_Z))
        except Exception as e:
            log(f"[waste] touch_tip failed (non-fatal): {e}")
            break

def return_color_tip_to_rack(tip_well_name: str) -> None:
    """Return a reused tip to its rack location with better seating."""
    if TIP_RETURN_METHOD == "return_tip":
        p300.return_tip()
        return

    well = tiprack_1[tip_well_name]

    if TIP_RETURN_METHOD == "drop_deeper":
        p300.drop_tip(well.top(z=TIPRACK_RETURN_Z))
        return

    if TIP_RETURN_METHOD == "seat_then_drop":
        # Approach -> seat deeper -> tiny pause -> release
        old_speed = p300.default_speed
        p300.default_speed = TIPRACK_SEAT_SPEED

        p300.move_to(well.top(z=5))
        p300.move_to(well.top(z=TIPRACK_SEAT_Z))
        protocol.delay(seconds=TIPRACK_SEAT_PAUSE_S)

        p300.drop_tip(well.top(z=TIPRACK_RETURN_Z))

        p300.default_speed = old_speed
        return

    raise ValueError(f"Unknown TIP_RETURN_METHOD={TIP_RETURN_METHOD!r}")
# =========================
# Core actions
# =========================
def mix_color(payload):
    
    global ROWS, rindex, cindex, TIP_RACK_EMPTY, WELL_PLATE_FULL
    
    
    if p300.has_tip:
        log("[startup] Unexpected tip detected. Homing before experiment.")
        p300.drop_tip(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move(Point(x=1, y=0, z=0)))
        protocol.home()
        

    if p300.has_tip:
        raise RuntimeError("Pipette still has a tip before experiment.")
    
    R = payload["command"]["R"]
    Y = payload["command"]["Y"]
    B = payload["command"]["B"]

    mix_well = payload["command"]["well"]
    if mix_well == "":
        WELL_PLATE_FULL = True
    session_id = payload["session_id"]
    experiment_id = payload["experiment_id"]
    global ACTIVE_SENSOR_EXPERIMENT_ID, SENSOR_DEPLOYED, SENSOR_DEPLOYED_UNIX_S, SENSOR_PICKUP_Z
    ACTIVE_SENSOR_EXPERIMENT_ID = experiment_id #trial
    SENSOR_DEPLOYED = False #trial
    SENSOR_DEPLOYED_UNIX_S = None #trial

    total = R + Y + B
    if total > 300:
        log_err("The sum of the volumes must be <= 300 uL")
        raise ValueError("The sum of the volumes must be <= 300 uL")

    # Reservoir mapping (your existing convention)
    reservoir_positions = ["B1", "B2", "B3"]
    portion = {"B1": R, "B2": Y, "B3": B}
    res_to_color = {"B1": "R", "B2": "Y", "B3": "B"}

    count = 0
    last_round = 0

    for p in portion:
        if portion[p] >= 1.0:
            last_round += 1

    for res_well in reservoir_positions:

        vol = float(portion[res_well])

        if vol < 1.0:
            continue

        count += 1
        color_key = res_to_color[res_well]
        tip_well = COLOR_TIP_WELLS[color_key]

        p300.pick_up_tip(tiprack_1[tip_well])

        p300.default_speed = 250
        p300.aspirate(vol, reservoir[res_well].bottom(z = 2))
        p300.dispense(vol, plate[mix_well])

        p300.blow_out()
        #p300.move_to(plate[mix_well].top(z=2))

        p300.default_speed = 100
        p300.blow_out(reservoir["A1"].top(z=-5))
        p300.default_speed = 350

        return_color_tip_to_rack(tip_well)
        
        if count == last_round:
            p300.default_speed = 600
            
            if ROWS[rindex] == "B" and cindex == 1:
                cindex = 4
            
            mix_tip = ROWS[rindex] + str(cindex)
            
            p300.pick_up_tip(tiprack_1[mix_tip])
            
            p300.mix(4, 150, plate[mix_well].bottom(z=1.5)) #19.25
            p300.default_speed = 600
            p300.drop_tip()
                        
            if cindex % 12 == 0:
                rindex += 1
                cindex = 0
            
            if mix_tip == "H12":
                rindex = 0
                cindex = 0
                TIP_RACK_EMPTY = True
                
            cindex += 1

    while True:
        try:
            pending = command_queue.get_nowait()

            if ("command" in pending and pending["command"].get("sensor_status") is not None):
                log("[queue] Discarding stale sensor message")
                continue

            command_queue.put(pending)
            break

        except Empty:
            break
    
    # Pick up the sensor holder / "special tip"
    p300.pick_up_tip(tiprack_2["A2"].top().move(Point(x=1, y=0, z=0)))
    # p300.move_to(plate[mix_well].top(z=-1.3)) # old
    p300.default_speed = 1000
    p300.move_to(plate[mix_well].top(z=-27.5))
    
    SENSOR_DEPLOYED = True #trial
    SENSOR_DEPLOYED_UNIX_S = pytime.time() #trial
    log(f"[sensor] deployed=True experiment_id={experiment_id} well={mix_well} t={SENSOR_DEPLOYED_UNIX_S}")

    log("Sending status to HF...")
    payload_status = (
        f'{{"status": {{"sensor_status":"in_place"}}, '
        f'"experiment_id": "{experiment_id}", '
        f'"session_id": "{session_id}"}}'
    )

    if DUMMY_3_CYCLE_MODE:
        Timer(
            SIMULATED_SENSOR_DELAY_S,
            fake_sensor_complete,
            args=(session_id, experiment_id)
        ).start()

    mqtt_log(f"TX topic={OT2_STATUS_TOPIC} payload={payload_status}")
    client.publish(OT2_STATUS_TOPIC, payload_status, qos=2)
    if SIMULATE_PICO_SENSOR:
        log("[simulate] Pico bypass enabled; faking sensor complete")
        sleep(SIMULATED_SENSOR_DELAY_S)
        command_queue.put({
            "session_id" : session_id,
            "experiment_id" : experiment_id,
            "command": {
                "sensor_status" : "sensor_complete"
            }
        })
    

def move_sensor_back(payload):
    
    global count, ACTIVE_SENSOR_EXPERIMENT_ID, SENSOR_DEPLOYED, SENSOR_DEPLOYED_UNIX_S,SENSOR_DROPOFF_Z, TIP_RACK_EMPTY, WELL_PLATE_FULL

    # Wait until the sensor has actually had time to measure
    elapsed = pytime.time() - SENSOR_DEPLOYED_UNIX_S

    if elapsed < MIN_SENSOR_MEASURE_TIME:
        wait_time = MIN_SENSOR_MEASURE_TIME - elapsed
        log(f"[sensor] Waiting {wait_time:.2f}s before returning sensor")
        protocol.delay(seconds=wait_time)
    
    results_status = payload["command"]["sensor_status"]
    session_id = payload["session_id"]
    experiment_id = payload["experiment_id"]

    # 1) Ignore sensor_status for the wrong experiment
    if ACTIVE_SENSOR_EXPERIMENT_ID is None or experiment_id != ACTIVE_SENSOR_EXPERIMENT_ID:
        log(f"[sensor] IGNORE sensor_status={results_status} for experiment_id={experiment_id} (active={ACTIVE_SENSOR_EXPERIMENT_ID})")
        return

    # 2) Ignore sensor_timeout unless sensor is deployed (your request)
    if results_status == "sensor_timeout" and not SENSOR_DEPLOYED:
        log(f"[sensor] IGNORE sensor_timeout because SENSOR_DEPLOYED={SENSOR_DEPLOYED}")
        return

    # 3) Strong safety gate: never try to drop if we aren't holding *something*
    # (prevents 'detach tip called with no tip')
    if not getattr(p300, "has_tip", False):
        log(f"[sensor] IGNORE sensor_status={results_status}: p300.has_tip is False")
        return

    # Return sensor to dock
    
    dock = tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move(Point(x=1, y=0, z=0))

    p300.drop_tip(dock)

    protocol.delay(seconds=0.5)

    # Verify the sensor was actually released
    if p300.has_tip:

        log("[sensor] Sensor still attached. Trying again.")

        # Push slightly deeper
        p300.move_to(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z-4).move(Point(x=1, y=0, z=0)))

        protocol.delay(seconds=0.5)

        p300.drop_tip(dock)

        protocol.delay(seconds=0.5)
        
    if p300.has_tip:
        log_err("[sensor] Failed to release sensor.")
        protocol.home()
        raise RuntimeError("Sensor holder failed to detach.")
    
    SENSOR_DEPLOYED = False
    SENSOR_DEPLOYED_UNIX_S = None
    ACTIVE_SENSOR_EXPERIMENT_ID = None
    log("[sensor] deployed=False; cleared active experiment")

    payload_status = (
        f'{{"status": {{"sensor_status":"charging"}}, '
        f'"experiment_id": "{experiment_id}", '
        f'"session_id": "{session_id}"}}'
    )

    count += 1
    print(count)

    if DUMMY_3_CYCLE_MODE and results_status != "sensor_timeout":
        enqueue_next_dummy()

    mqtt_log(f"TX topic={OT2_STATUS_TOPIC} payload={payload_status}")
    client.publish(OT2_STATUS_TOPIC, payload_status, qos=2)
    
    if TIP_RACK_EMPTY:
            log("[tips] Last tip used. Waiting until experiment completes.")
            pause_for_tip_reload()
            TIP_RACK_EMPTY = False
            
    if WELL_PLATE_FULL:
            log("[tips] Last well used.")
            pause_for_plate_reload()
            WELL_PLATE_FULL = False

    global completed_experiments_since_home

    # Always home on timeout as a recovery behavior
    if results_status == "sensor_timeout":
        #p300.drop_tip(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move( Point (x=1, y=0,z=0)))
        protocol.home()
        
        p300.pick_up_tip(tiprack_2["A2"].top(z=SENSOR_PICKUP_Z).move(Point(x=1, y=0, z=0)))
        p300.move_to(tiprack_2["A2"].top(z = 10))
        p300.drop_tip(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move(Point(x=1, y=0, z=0))) #
        
        completed_experiments_since_home = 0
        return

    if not AUTO_HOME_AFTER_EXPERIMENT:
        return

    completed_experiments_since_home += 1
    log(f"[home] completed_experiments_since_home={completed_experiments_since_home} / {HOME_EVERY_N_EXPERIMENTS}")

    if completed_experiments_since_home % 5 == 0:
            p300.pick_up_tip(tiprack_2["A2"].top(z=SENSOR_PICKUP_Z).move(Point(x=1, y=0, z=0)))
            p300.move_to(tiprack_2["A2"].top(z = 10))
            p300.drop_tip(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move(Point(x=1, y=0, z=0))) #-4
    
    # Home every N completed experiments
    if HOME_EVERY_N_EXPERIMENTS <= 1 or completed_experiments_since_home >= HOME_EVERY_N_EXPERIMENTS:
        protocol.home()
        
        completed_experiments_since_home = 0


def handle_command(payload):
    if {"R", "Y", "B", "well"}.issubset(payload["command"].keys()):
        print(f"Handling mix command: {payload}")
        mix_color(payload)

    elif {"sensor_status"}.issubset(payload["command"].keys()):
        print("Sensor measure complete")
        move_sensor_back(payload)
        #hi
def startup_ready_dance(): # new, trial
    """
    Very obvious operator-visible 'READY' sequence.
    No tips, no liquids, no GPIO. Just motion.
    """
    log("[startup] READY dance begin")

    # Ensure we start from a known state
    protocol.home()
    
    p300.drop_tip(tiprack_2["A2"].top().move(Point(x=-0.6, y=0, z=0)))

    # Two corners of the plate as a clear visual cue
    try:
        #p300.move_to(plate["H12"].top(z=30 + PLATE_Z_OFFSET))
        #p300.move_to(plate["H12"].top(z=6 + PLATE_Z_OFFSET))
        #p300.move_to(plate["H12"].top(z=30 + PLATE_Z_OFFSET))

        #p300.move_to(plate["A1"].top(z=30 + PLATE_Z_OFFSET))
        #p300.move_to(plate["A1"].top(z=6 + PLATE_Z_OFFSET))
        #p300.move_to(plate["A1"].top(z=30 + PLATE_Z_OFFSET))
        print("ready")
    except Exception as e:
        log(f"[startup] READY dance error (non-fatal): {e}")

    protocol.home()
    log("[startup] READY dance done — robot ready for commands")


# print("OT-2 is waiting for command") # old
# protocol.home()  # show readiness # old
#startup_ready_dance()
log("OT-2 is waiting for command")

# =========================
# Main loop
# =========================
if DUMMY_3_CYCLE_MODE:
    enqueue_next_dummy()
    
p300.pick_up_tip(tiprack_2["A2"].top(z=SENSOR_PICKUP_Z).move(Point(x=1, y=0, z=0)))
p300.move_to(tiprack_2["A2"].top(z = 10))
p300.drop_tip(tiprack_2["A2"].top(z=SENSOR_DROPOFF_Z).move(Point(x=1, y=0, z=0))) #-4

while True:
    
    try:
        
        command = command_queue.get(timeout=210)
        log(f"Processing command from queue: {command}")

        if "command" in command and "experiment_id" in command:
            try:
                
                handle_command(command)
                
            except Exception as e:
                log_err(f"Error processing command: {e}")

    except Empty:
        pass
    except Exception as e:
        log_err(f"Unexpected error in main loop: {e}")

    sleep(1)
