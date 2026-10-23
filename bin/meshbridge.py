#!/usr/bin/env python3
"""Bridge between the Mesh Chat panel and a Meshtastic or MeshCore radio.

One radio, many bars: every monitor runs its own copy of the widget, but a
serial port can only be opened once. So the radio belongs to a single daemon
(`serve`), and each widget runs `attach`, a proxy that starts the daemon if
needed and relays JSON lines between its stdin/stdout and the daemon's socket.
The daemon exits once no widget has been attached for IDLE_SECONDS.

Events:   status, self, channels, nodes, unread, history, message, ack, error
Commands: {"cmd": "send", "conv": "ch:0" | "dm:<node>", "text": "..."}
          {"cmd": "refresh"}
          {"cmd": "advert"}                (MeshCore only)
          {"cmd": "viewing", "conv": "..."} ("" when the panel closes)
          {"cmd": "read", "conv": "..."}
"""

import argparse
import asyncio
import glob
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

RETRY_SECONDS = 10
HISTORY_LIMIT = 500
IDLE_SECONDS = 15
# Events a newly attached widget needs to catch up on.
SNAPSHOT_TYPES = ("status", "self", "channels", "nodes", "unread")

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omarchy-mesh-{os.getuid()}"
SOCKET_PATH = os.path.join(RUNTIME_DIR, "omarchy-mesh.sock")
LOCK_PATH = os.path.join(RUNTIME_DIR, "omarchy-mesh.lock")


def state_dir():
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(state, "omarchy-mesh")
    os.makedirs(path, exist_ok=True)
    return path


class Hub:
    """Fans events out to every attached widget and remembers the latest
    snapshot events for widgets that attach later."""

    def __init__(self):
        self.clients = {}  # socket -> conv the panel is showing ("" if closed)
        self.snapshot = {}
        self.lock = threading.Lock()
        self.last_client = time.time()

    def emit(self, obj):
        line = (json.dumps(obj, default=str) + "\n").encode()
        with self.lock:
            if obj["type"] in SNAPSHOT_TYPES:
                self.snapshot[obj["type"]] = line
            for conn in list(self.clients):
                try:
                    conn.sendall(line)
                except OSError:
                    self.clients.pop(conn, None)

    def add(self, conn, history):
        with self.lock:
            conn.sendall((json.dumps({"type": "history", "items": history}) + "\n").encode())
            for line in self.snapshot.values():
                conn.sendall(line)
            self.clients[conn] = ""

    def remove(self, conn):
        with self.lock:
            self.clients.pop(conn, None)
            self.last_client = time.time()

    def set_viewing(self, conn, conv):
        with self.lock:
            if conn in self.clients:
                self.clients[conn] = conv

    def is_viewed(self, conv):
        with self.lock:
            return conv in self.clients.values()

    def idle(self):
        with self.lock:
            return not self.clients and time.time() - self.last_client > IDLE_SECONDS


HUB = Hub()


def emit(kind, **fields):
    fields["type"] = kind
    HUB.emit(fields)


def status(state, detail="", **extra):
    emit("status", state=state, detail=detail, **extra)


def now():
    return int(time.time())


def new_id():
    return uuid.uuid4().hex[:12]


def serial_ports():
    ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if ports:
        return [os.path.realpath(p) for p in ports]
    return sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))


class History:
    """Append-only JSONL log of messages, plus ack records applied on load."""

    def __init__(self, protocol):
        self.path = os.path.join(state_dir(), f"{protocol}.jsonl")
        self.lock = threading.Lock()

    def load(self):
        messages, order = {}, []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if "ack" in rec:
                        if rec["ack"] in messages:
                            messages[rec["ack"]]["status"] = rec.get("status", "")
                    elif "id" in rec:
                        if rec["id"] not in messages:
                            order.append(rec["id"])
                        messages[rec["id"]] = rec
        except FileNotFoundError:
            return []
        items = [messages[i] for i in order][-HISTORY_LIMIT:]
        if len(order) > HISTORY_LIMIT * 2:
            self._rewrite(items)
        return items

    def _rewrite(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in items:
                f.write(json.dumps(rec) + "\n")
        os.replace(tmp, self.path)

    def append(self, rec):
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


class Bridge:
    """Shared plumbing: history, message records and ack bookkeeping."""

    protocol = ""

    def __init__(self, args):
        self.args = args
        self.history = History(self.protocol)
        self.commands = queue.Queue()
        self.connected = False
        self.unread = {}
        self.submit = self.commands.put

    def message(self, conv, sender, sender_name, text, direction, **extra):
        rec = {
            "id": extra.pop("id", None) or new_id(),
            "ts": extra.pop("ts", None) or now(),
            "conv": conv,
            "from": sender,
            "fromName": sender_name,
            "text": text,
            "dir": direction,
            "status": extra.pop("status", ""),
        }
        rec.update({k: v for k, v in extra.items() if v is not None})
        self.history.append(rec)
        emit("message", message=rec)
        if direction == "in" and not HUB.is_viewed(conv):
            self.unread[conv] = self.unread.get(conv, 0) + 1
            emit("unread", counts=self.unread)
            if self.args.notify:
                subprocess.Popen(["notify-send", "-a", "Mesh Chat", "-i", "network-wireless",
                                  sender_name or sender or "Mesh", text],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        return rec

    def mark_read(self, conv):
        if self.unread.pop(conv, None):
            emit("unread", counts=self.unread)

    def ack(self, message_id, state, detail=""):
        self.history.append({"ack": message_id, "status": state})
        emit("ack", id=message_id, status=state, detail=detail)


class MeshtasticBridge(Bridge):
    protocol = "meshtastic"

    def __init__(self, args):
        super().__init__(args)
        self.iface = None
        self.my_id = ""
        self.lost = threading.Event()

    def connect(self):
        from meshtastic import ble_interface, serial_interface, tcp_interface

        transport, address = self.args.transport, self.args.address
        if transport == "tcp":
            if not address:
                raise RuntimeError("Set a host name or IP for TCP")
            return tcp_interface.TCPInterface(hostname=address)
        if transport == "ble":
            return ble_interface.BLEInterface(address or None)
        if not address:
            from meshtastic.util import findPorts
            ports = findPorts(True)
            if not ports:
                raise RuntimeError("No radio found on USB")
            address = ports[0]
        return serial_interface.SerialInterface(devPath=address)

    def node_name(self, node_id):
        node = (self.iface.nodes or {}).get(node_id) if self.iface else None
        user = (node or {}).get("user", {})
        return user.get("longName") or user.get("shortName") or node_id

    def publish_state(self):
        info = self.iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        self.my_id = user.get("id", "")
        emit("self", id=self.my_id, name=user.get("longName", ""), short=user.get("shortName", ""))

        channels = []
        for ch in self.iface.localNode.channels or []:
            if ch.role == 0:  # DISABLED
                continue
            name = ch.settings.name or ("Primary" if ch.role == 1 else f"Channel {ch.index}")
            channels.append({"index": ch.index, "name": name})
        emit("channels", items=channels or [{"index": 0, "name": "Primary"}])
        self.publish_nodes()

    def publish_nodes(self):
        nodes = []
        for node in (self.iface.nodes or {}).values():
            user = node.get("user", {})
            if not user.get("id") or user.get("id") == self.my_id:
                continue
            nodes.append({
                "id": user["id"],
                "name": user.get("longName") or user["id"],
                "short": user.get("shortName", ""),
                "lastHeard": node.get("lastHeard", 0) or 0,
                "snr": node.get("snr"),
                "hops": node.get("hopsAway"),
            })
        nodes.sort(key=lambda n: n["lastHeard"], reverse=True)
        emit("nodes", items=nodes)

    def on_text(self, packet, interface):
        if interface is not self.iface:
            return
        decoded = packet.get("decoded", {})
        text = decoded.get("text")
        if text is None:
            return
        sender = packet.get("fromId") or "!%08x" % packet.get("from", 0)
        if sender == self.my_id:
            return
        broadcast = packet.get("to") == 0xFFFFFFFF
        conv = f"ch:{packet.get('channel', 0)}" if broadcast else f"dm:{sender}"
        hops = None
        if "hopStart" in packet and "hopLimit" in packet:
            hops = packet["hopStart"] - packet["hopLimit"]
        self.message(conv, sender, self.node_name(sender), text, "in",
                     ts=packet.get("rxTime"), snr=packet.get("rxSnr"),
                     rssi=packet.get("rxRssi"), hops=hops)

    def on_lost(self, interface):
        if interface is self.iface:
            self.lost.set()

    def on_node(self, node, interface):
        if interface is self.iface and self.connected:
            self.publish_nodes()

    def send(self, conv, text):
        from meshtastic import portnums_pb2

        if conv.startswith("ch:"):
            dest, channel = "^all", int(conv[3:])
        else:
            dest, channel = conv[3:], 0
        rec = self.message(conv, self.my_id, "You", text, "out", status="sending")

        def on_response(packet):
            reason = packet.get("decoded", {}).get("routing", {}).get("errorReason", "NONE")
            if reason == "NONE":
                self.ack(rec["id"], "delivered")
            else:
                self.ack(rec["id"], "failed", reason)

        self.iface.sendData(text.encode("utf-8"), destinationId=dest,
                            portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                            wantAck=True, onResponse=on_response,
                            onResponseAckPermitted=True, channelIndex=channel)
        self.ack(rec["id"], "sent")

    def close(self):
        if self.iface:
            try:
                self.iface.close()
            except Exception:
                pass
        self.iface = None
        self.connected = False

    def run(self):
        from pubsub import pub

        pub.subscribe(self.on_text, "meshtastic.receive.text")
        pub.subscribe(self.on_lost, "meshtastic.connection.lost")
        pub.subscribe(self.on_node, "meshtastic.node.updated")

        next_attempt = 0
        while True:
            if not self.connected and time.time() >= next_attempt:
                status("connecting", self.args.address or self.args.transport)
                try:
                    self.lost.clear()
                    self.iface = self.connect()
                    self.connected = True
                    self.publish_state()
                    status("connected", self.args.address or self.args.transport)
                except (Exception, SystemExit) as exc:
                    self.close()
                    status("disconnected", str(exc) or exc.__class__.__name__)
                    next_attempt = time.time() + RETRY_SECONDS
            if self.connected and self.lost.is_set():
                self.close()
                status("disconnected", "Connection lost")
                next_attempt = time.time() + RETRY_SECONDS

            try:
                cmd = self.commands.get(timeout=1)
            except queue.Empty:
                continue
            if cmd.get("cmd") == "quit":
                self.close()
                return
            self.handle(cmd)

    def handle(self, cmd):
        name = cmd.get("cmd")
        if name == "refresh":
            if self.connected:
                self.publish_state()
            return
        if name == "send":
            if not self.connected:
                emit("error", detail="Not connected to a radio")
                return
            try:
                self.send(cmd["conv"], cmd["text"])
            except Exception as exc:
                emit("error", detail=f"Send failed: {exc}")
            return
        emit("error", detail=f"Unsupported command: {name}")


class MeshCoreBridge(Bridge):
    protocol = "meshcore"

    def __init__(self, args):
        super().__init__(args)
        self.mc = None
        self.my_key = ""
        self.pending_acks = {}  # expected_ack hex -> message id
        self.channel_names = {}

    async def connect(self):
        from meshcore import MeshCore

        transport, address = self.args.transport, self.args.address
        if transport == "tcp":
            if not address:
                raise RuntimeError("Set a host name or IP for TCP")
            host, _, port = address.partition(":")
            return await MeshCore.create_tcp(host, int(port or 5000))
        if transport == "ble":
            return await MeshCore.create_ble(address or None)
        if not address:
            ports = serial_ports()
            if not ports:
                raise RuntimeError("No radio found on USB")
            address = ports[0]
        return await MeshCore.create_serial(address, 115200)

    def contact_name(self, prefix):
        contact = self.mc.get_contact_by_key_prefix(prefix) if self.mc else None
        return (contact or {}).get("adv_name") or prefix

    async def publish_state(self):
        from meshcore import EventType

        info = self.mc.self_info or {}
        self.my_key = info.get("public_key", "")
        emit("self", id=self.my_key[:12], name=info.get("name", ""), short="")

        channels = []
        for idx in range(8):
            try:
                event = await self.mc.commands.get_channel(idx)
            except Exception:
                break
            if event.type == EventType.ERROR:
                break
            name = event.payload.get("channel_name", "")
            if name or idx == 0:
                name = name or "Public"
                self.channel_names[idx] = name
                channels.append({"index": idx, "name": name})
        emit("channels", items=channels or [{"index": 0, "name": "Public"}])
        await self.publish_nodes()

    async def publish_nodes(self):
        await self.mc.commands.get_contacts()
        kinds = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
        nodes = []
        for key, contact in (self.mc.contacts or {}).items():
            nodes.append({
                "id": key[:12],
                "name": contact.get("adv_name") or key[:12],
                "short": kinds.get(contact.get("type"), ""),
                "lastHeard": contact.get("last_advert", 0) or 0,
                "snr": None,
                "hops": contact.get("out_path_len") if contact.get("out_path_len", -1) >= 0 else None,
            })
        nodes.sort(key=lambda n: n["lastHeard"], reverse=True)
        emit("nodes", items=nodes)

    async def on_direct(self, event):
        p = event.payload
        prefix = p.get("pubkey_prefix", "")
        self.message(f"dm:{prefix}", prefix, self.contact_name(prefix), p.get("text", ""), "in",
                     ts=p.get("sender_timestamp"), snr=p.get("SNR"),
                     hops=p.get("path_len") if p.get("path_len", 255) != 255 else None)

    async def on_channel(self, event):
        p = event.payload
        # Channel text arrives as "Sender: message".
        sender, sep, body = p.get("text", "").partition(": ")
        if not sep:
            sender, body = "", p.get("text", "")
        self.message(f"ch:{p.get('channel_idx', 0)}", sender, sender or "Unknown", body, "in",
                     ts=p.get("sender_timestamp"), snr=p.get("SNR"), rssi=p.get("RSSI"),
                     hops=p.get("path_len") if p.get("path_len", 255) != 255 else None)

    async def on_ack(self, event):
        message_id = self.pending_acks.pop(event.payload.get("code", ""), None)
        if message_id:
            self.ack(message_id, "delivered")

    async def on_new_contact(self, event):
        await self.publish_nodes()

    async def on_disconnected(self, event):
        self.connected = False

    async def send(self, conv, text):
        from meshcore import EventType

        rec = self.message(conv, self.my_key[:12], "You", text, "out", status="sending")
        if conv.startswith("ch:"):
            result = await self.mc.commands.send_chan_msg(int(conv[3:]), text)
            ok = result.type != EventType.ERROR
            self.ack(rec["id"], "sent" if ok else "failed", "" if ok else str(result.payload))
            return
        contact = self.mc.get_contact_by_key_prefix(conv[3:])
        if not contact:
            self.ack(rec["id"], "failed", "Unknown contact")
            return
        result = await self.mc.commands.send_msg(contact, text)
        if result.type == EventType.ERROR:
            self.ack(rec["id"], "failed", str(result.payload))
            return
        expected = result.payload.get("expected_ack")
        if isinstance(expected, bytes):
            expected = expected.hex()
        if expected:
            self.pending_acks[expected] = rec["id"]
        self.ack(rec["id"], "sent")

    async def handle(self, cmd):
        name = cmd.get("cmd")
        if name == "refresh":
            if self.connected:
                await self.publish_state()
            return
        if not self.connected:
            emit("error", detail="Not connected to a radio")
            return
        try:
            if name == "send":
                await self.send(cmd["conv"], cmd["text"])
            elif name == "advert":
                await self.mc.commands.send_advert(flood=True)
                emit("error", detail="Advert sent")
            else:
                emit("error", detail=f"Unsupported command: {name}")
        except Exception as exc:
            emit("error", detail=f"{name} failed: {exc}")

    async def run_async(self):
        from meshcore import EventType

        loop = asyncio.get_running_loop()
        commands = asyncio.Queue()
        self.submit = lambda c: loop.call_soon_threadsafe(commands.put_nowait, c)

        while True:
            if not self.connected:
                status("connecting", self.args.address or self.args.transport)
                try:
                    self.mc = await self.connect()
                    if self.mc is None:
                        raise RuntimeError("Radio did not answer")
                    self.mc.subscribe(EventType.CONTACT_MSG_RECV, self.on_direct)
                    self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel)
                    self.mc.subscribe(EventType.ACK, self.on_ack)
                    self.mc.subscribe(EventType.DISCONNECTED, self.on_disconnected)
                    self.mc.subscribe(EventType.NEW_CONTACT, self.on_new_contact)
                    self.connected = True
                    await self.publish_state()
                    await self.mc.start_auto_message_fetching()
                    status("connected", self.args.address or self.args.transport)
                except Exception as exc:
                    self.connected = False
                    status("disconnected", str(exc) or exc.__class__.__name__)
                    deadline = loop.time() + RETRY_SECONDS
                    while loop.time() < deadline:
                        try:
                            cmd = await asyncio.wait_for(commands.get(), deadline - loop.time())
                        except asyncio.TimeoutError:
                            break
                        if cmd.get("cmd") == "quit":
                            return
                        emit("error", detail="Not connected to a radio")
                    continue

            try:
                cmd = await asyncio.wait_for(commands.get(), 2)
            except asyncio.TimeoutError:
                if not self.connected:
                    status("disconnected", "Connection lost")
                continue
            if cmd.get("cmd") == "quit":
                await self.mc.disconnect()
                return
            await self.handle(cmd)

    def run(self):
        asyncio.run(self.run_async())


def config_of(args):
    return {"protocol": args.protocol, "transport": args.transport,
            "address": args.address, "notify": args.notify}


def client_loop(bridge, conn):
    """Read one widget's commands until it detaches."""
    try:
        for raw in conn.makefile("r", encoding="utf-8"):
            try:
                cmd = json.loads(raw)
            except ValueError:
                continue
            name = cmd.get("cmd")
            if name == "hello":
                if cmd.get("config") != config_of(bridge.args):
                    # Settings changed: let attach start a daemon with the new ones.
                    conn.sendall(b'{"type": "restart"}\n')
                    bridge.submit({"cmd": "quit"})
                    return
                HUB.add(conn, bridge.history.load())
            elif name == "viewing":
                conv = cmd.get("conv") or ""
                HUB.set_viewing(conn, conv)
                if conv:
                    bridge.mark_read(conv)
            elif name == "read":
                bridge.mark_read(cmd.get("conv", ""))
            elif name != "quit":
                bridge.submit(cmd)
    except OSError:
        pass
    finally:
        HUB.remove(conn)
        conn.close()


def serve(args):
    import fcntl

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 4  # another daemon owns the radio

    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    server.listen()

    bridge = (MeshtasticBridge if args.protocol == "meshtastic" else MeshCoreBridge)(args)

    def accept():
        while True:
            conn, _ = server.accept()
            threading.Thread(target=client_loop, args=(bridge, conn), daemon=True).start()

    def watchdog():
        while not HUB.idle():
            time.sleep(2)
        bridge.submit({"cmd": "quit"})

    threading.Thread(target=accept, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        bridge.run()
    finally:
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
    os._exit(0)  # don't wait on library threads still blocked on the radio


def attach(args):
    import importlib.util

    # Keep the real stdout for events, so a stray print() can't corrupt them.
    events = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    sys.stdout = sys.stderr

    if importlib.util.find_spec(args.protocol) is None:
        events.write(json.dumps({"type": "status", "state": "missing-deps",
                                 "detail": f"The {args.protocol} Python package is not installed"}) + "\n")
        return 3

    hello = (json.dumps({"cmd": "hello", "config": config_of(args)}) + "\n").encode()
    for _ in range(3):
        conn = connect_or_spawn(args)
        if conn is None:
            events.write('{"type": "status", "state": "disconnected", "detail": "Mesh daemon did not start"}\n')
            return 1
        conn.sendall(hello)
        reader = conn.makefile("r", encoding="utf-8")
        first = reader.readline()
        if '"restart"' in first:
            conn.close()
            time.sleep(1)  # let the old daemon release the radio
            continue
        break
    else:
        return 1

    def pump_stdin():
        try:
            for line in sys.stdin:
                conn.sendall(line.encode())
        except OSError:
            pass
        os._exit(0)

    threading.Thread(target=pump_stdin, daemon=True).start()
    events.write(first)
    for line in reader:
        events.write(line)
    return 0  # daemon went away; the widget restarts us


def connect_or_spawn(args):
    def connect():
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(SOCKET_PATH)
            return conn
        except OSError:
            conn.close()
            return None

    conn = connect()
    if conn:
        return conn
    log = open(os.path.join(state_dir(), "daemon.log"), "a")
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "serve",
                      "--protocol", args.protocol, "--transport", args.transport,
                      "--address", args.address] + (["--notify"] if args.notify else []),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        conn = connect()
        if conn:
            return conn
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["attach", "serve"], nargs="?", default="attach")
    parser.add_argument("--protocol", choices=["meshtastic", "meshcore"], default="meshtastic")
    parser.add_argument("--transport", choices=["serial", "tcp", "ble"], default="serial")
    parser.add_argument("--address", default="", help="Serial device, host[:port] or BLE address")
    parser.add_argument("--notify", action="store_true", help="Desktop notification per incoming message")
    args = parser.parse_args()
    try:
        return serve(args) if args.mode == "serve" else attach(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
