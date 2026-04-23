from flask import Flask, render_template, jsonify, request
import socket
import json
import threading
import time
import re
import serial

app = Flask(__name__)

# ── WTBU daemon socket ──────────────────────────────────────────────────────
SOCKET_PATH = '/tmp/wtbu_socket'
RELAYS = ['main', 'fireflies', 'well-pump', 'rain-pump', 'pc', 'free1', 'free2', 'free3']

import queue as _queue_mod
_daemon_queue  = _queue_mod.Queue(maxsize=4)
_last_response = {'monitor': '', 'relays': '', 'valves': ''}


def _raw_connect(command):
    last_err = None
    for attempt in range(3):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(command).encode())
            data = sock.recv(500).decode('utf-8')
            lines = data.splitlines()
            return {
                'monitor': lines[0] if len(lines) > 0 else '',
                'relays':  lines[1] if len(lines) > 1 else '',
                'valves':  lines[2] if len(lines) > 2 else ''
            }
        except socket.timeout:
            # Commando is verzonden, daemon verwerkt het nog — niet opnieuw proberen
            return {'monitor': '', 'relays': '', 'valves': ''}
        except OSError as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.05)
        finally:
            sock.close()
    raise last_err or OSError('Connection failed')


def _daemon_worker():
    """Enige thread die ooit verbinding maakt met de daemon."""
    status_cmd = {
        'on': [], 'off': [], 'reset_relays': False, 'status': True,
        'wait': False, 'valves': [], 'close_all': False, 'shutdown': False,
        'user': 'web'
    }
    while True:
        try:
            cmd, event, holder = _daemon_queue.get(timeout=0.35)
        except _queue_mod.Empty:
            # Geen request → stuur keepalive
            try:
                r = _raw_connect(status_cmd)
                _last_response.update(r)
            except OSError:
                pass
            continue
        try:
            r = _raw_connect(cmd)
            _last_response.update(r)
            holder[0] = r
        except OSError as e:
            holder[0] = {'error': str(e)}
        finally:
            event.set()


threading.Thread(target=_daemon_worker, daemon=True).start()


def send_command(command, timeout=8):
    holder = [None]
    event  = threading.Event()
    try:
        _daemon_queue.put((command, event, holder), block=True, timeout=3)
    except _queue_mod.Full:
        return {'error': 'Daemon bezig, probeer opnieuw'}
    event.wait(timeout=timeout)
    return holder[0] or {'error': 'Timeout'}


def base_command():
    return {
        'on': [], 'off': [], 'reset_relays': False, 'status': False,
        'wait': False, 'valves': [], 'close_all': False, 'shutdown': False,
        'user': 'web'
    }


# ── Firefly serial ──────────────────────────────────────────────────────────
FIREFLY_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Zolertia_Firefly_platform_ZOL-B001-A200002574-if00-port0"
FIREFLY_BAUDRATE = 460800

CALIBRATION = {
    1: (0.0188, -188.0213),
    2: (0.0054, -65.1942),
    3: (0.0181, -181.4417),
    4: (0.0184, -182.8977),
    5: (0.0187, -184.0834),
    6: (0.0054, -59.3130),
    7: (0.0054, -59.6097),
}

NODE_SENSOR_MAP = {
    201: (1, 2),
    202: (3, 4),
    203: (5, 6),
    204: (7, None),
}

_sensor_re   = re.compile(r"Node:\s*(20[1-4])\sSensor1:\s(\d+)\sSensor2:\s(\d+)")
_actuator_re = re.compile(r"Node:\s*(20[1-4])\sActuator:\s(\d+)")

_firefly_data_lock  = threading.Lock()
_firefly_write_lock = threading.Lock()
_firefly_serial = None
_sensor_raw   = {}
_sensor_cm    = {}
_actuator_state = {}


def _raw_to_cm(sensor_id, raw):
    a, b = CALIBRATION[sensor_id]
    return round(a * raw + b, 2)


def _firefly_reader():
    global _firefly_serial
    while True:
        try:
            if _firefly_serial is None or not _firefly_serial.is_open:
                _firefly_serial = serial.Serial(
                    FIREFLY_PORT, FIREFLY_BAUDRATE, timeout=0.2, write_timeout=0.5
                )
                time.sleep(0.2)

            raw = _firefly_serial.readline()
            if not raw:
                continue
            line = raw.decode('utf-8', errors='replace').strip()
            if not line:
                continue

            m = _sensor_re.search(line)
            if m:
                node = int(m.group(1))
                s1, s2 = int(m.group(2)), int(m.group(3))
                sid1, sid2 = NODE_SENSOR_MAP[node]
                with _firefly_data_lock:
                    _sensor_raw[sid1] = s1
                    _sensor_cm[sid1]  = _raw_to_cm(sid1, s1)
                    if sid2 is not None:
                        _sensor_raw[sid2] = s2
                        _sensor_cm[sid2]  = _raw_to_cm(sid2, s2)
                continue

            m = _actuator_re.search(line)
            if m:
                node = int(m.group(1))
                with _firefly_data_lock:
                    _actuator_state[node] = int(m.group(2))

        except Exception:
            _firefly_serial = None
            time.sleep(1)


threading.Thread(target=_firefly_reader, daemon=True).start()


def _send_firefly(cmd: str):
    global _firefly_serial
    with _firefly_write_lock:
        try:
            if _firefly_serial is None or not _firefly_serial.is_open:
                return {'error': 'Firefly niet verbonden'}
            _firefly_serial.write((cmd + '\n').encode('utf-8'))
            _firefly_serial.flush()
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}


# ── Routes: WTBU ────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def status():
    cmd = base_command()
    cmd['status'] = True
    return jsonify(send_command(cmd))


@app.route('/api/relay/<relay>/<action>', methods=['POST'])
def relay(relay, action):
    if relay not in RELAYS or action not in ['on', 'off']:
        return jsonify({'error': 'Ongeldige relay of actie'}), 400
    cmd = base_command()
    cmd['on']  = [relay] if action == 'on'  else []
    cmd['off'] = [relay] if action == 'off' else []
    return jsonify(send_command(cmd))


@app.route('/api/valve', methods=['POST'])
def valve():
    data = request.get_json()
    try:
        valve_num = int(data['valve'])
        position  = float(data['position'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'Ongeldige invoer'}), 400
    if not (1 <= valve_num <= 9):
        return jsonify({'error': 'Valve moet tussen 1 en 9 zijn'}), 400
    if not (0 <= position <= 90):
        return jsonify({'error': 'Positie moet tussen 0 en 90 graden zijn'}), 400
    cmd = base_command()
    cmd['valves'] = [[valve_num, position]]
    return jsonify(send_command(cmd))


@app.route('/api/close-all', methods=['POST'])
def close_all():
    cmd = base_command()
    cmd['close_all'] = True
    return jsonify(send_command(cmd))


@app.route('/api/reset-relays', methods=['POST'])
def reset_relays():
    cmd = base_command()
    cmd['reset_relays'] = True
    return jsonify(send_command(cmd))


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    cmd = base_command()
    cmd['shutdown'] = True
    return jsonify(send_command(cmd))


# ── Routes: Firefly ─────────────────────────────────────────────────────────
@app.route('/api/firefly/status')
def firefly_status():
    with _firefly_data_lock:
        sensors = {
            str(sid): {'raw': _sensor_raw.get(sid), 'cm': _sensor_cm.get(sid)}
            for sid in range(1, 8)
        }
        actuators = {str(node): _actuator_state.get(node) for node in NODE_SENSOR_MAP}
    connected = _firefly_serial is not None and _firefly_serial.is_open
    return jsonify({'sensors': sensors, 'actuators': actuators, 'connected': connected})


@app.route('/api/firefly/gate/<int:node>/<int:value>', methods=['POST'])
def firefly_gate(node, value):
    if node not in NODE_SENSOR_MAP:
        return jsonify({'error': f'Ongeldig node {node}'}), 400
    if not (0 <= value <= 255):
        return jsonify({'error': 'Waarde moet tussen 0 en 255 zijn'}), 400
    return jsonify(_send_firefly(f"{node} {value}"))


@app.route('/api/firefly/mode/<mode>', methods=['POST'])
def firefly_mode(mode):
    if mode == 'manual':
        return jsonify(_send_firefly('205 0'))
    if mode == 'auto':
        return jsonify(_send_firefly('205 1'))
    return jsonify({'error': 'Ongeldig mode'}), 400


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
