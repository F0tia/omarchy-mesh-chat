import QtQuick
import Quickshell
import Quickshell.Io

// Runs `meshbridge.py attach`, which connects this bar to the one mesh daemon
// that owns the radio, and keeps the chat state the panel renders. Every
// monitor's bar has its own Service; the daemon keeps them in step and sends
// the desktop notifications. The protocol is described at the top of the script.
Item {
  id: root

  property var settings: ({})

  readonly property string protocol: settings.protocol === "meshcore" ? "meshcore" : "meshtastic"
  readonly property string transport: ["serial", "tcp", "ble"].indexOf(settings.transport) >= 0 ? settings.transport : "serial"
  readonly property string address: String(settings.address || "").trim()
  readonly property bool notify: settings.notify !== false
  readonly property string protocolLabel: protocol === "meshcore" ? "MeshCore" : "Meshtastic"
  // Meshtastic payloads top out near 200 bytes; MeshCore near 150, and
  // channel posts also carry "Name: ".
  readonly property int maxBytes: protocol === "meshcore" ? 140 : 200

  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, ""))
  readonly property string python: (Quickshell.env("XDG_DATA_HOME") || (Quickshell.env("HOME") + "/.local/share")) + "/omarchy-mesh/venv/bin/python"

  // starting | connecting | connected | disconnected | missing-deps | stopped
  property string linkState: "starting"
  property string detail: ""
  property string selfId: ""
  property string selfName: ""
  property var channels: []
  property var nodes: []
  property var messages: []
  property var unread: ({})
  property string activeConv: "ch:0"
  property bool viewing: false
  property string notice: ""
  property var setupLog: []
  readonly property bool settingUp: setupProc.running
  readonly property bool connected: linkState === "connected"
  readonly property int unreadTotal: {
    var total = 0
    for (var k in unread) total += unread[k]
    return total
  }
  readonly property var conversations: buildConversations()
  readonly property var activeMessages: messages.filter(function(m) { return m.conv === root.activeConv })

  readonly property string commandKey: [protocol, transport, address, notify].join("|")
  onCommandKeyChanged: restart()

  onViewingChanged: reportViewing()
  onActiveConvChanged: reportViewing()

  function command(obj) {
    if (bridge.running) bridge.write(JSON.stringify(obj) + "\n")
  }

  function reportViewing() {
    command({ cmd: "viewing", conv: viewing ? activeConv : "" })
  }

  function statusText() {
    if (linkState === "connected") return protocolLabel + " · " + (transport === "serial" ? "USB" : transport.toUpperCase())
    if (linkState === "connecting") return "Connecting to radio…"
    if (linkState === "missing-deps") return "Python libraries not installed"
    if (linkState === "disconnected") return detail || "Radio disconnected"
    if (linkState === "stopped") return "Bridge stopped, restarting…"
    return "Starting…"
  }

  function nodeName(id) {
    for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return nodes[i].name
    return id
  }

  function convTitle(conv) {
    if (conv.indexOf("ch:") === 0) {
      var idx = parseInt(conv.slice(3))
      for (var i = 0; i < channels.length; i++) if (channels[i].index === idx) return "# " + channels[i].name
      return "# Channel " + idx
    }
    return "@ " + nodeName(conv.slice(3))
  }

  function buildConversations() {
    var list = []
    var seen = {}
    function add(conv) {
      if (seen[conv]) return
      seen[conv] = true
      list.push({ value: conv, label: convTitle(conv) + (unread[conv] ? "  (" + unread[conv] + ")" : "") })
    }
    for (var i = 0; i < channels.length; i++) add("ch:" + channels[i].index)
    if (channels.length === 0) add("ch:0")
    for (var j = messages.length - 1; j >= 0; j--) if (messages[j].conv.indexOf("dm:") === 0) add(messages[j].conv)
    add(activeConv)
    return list
  }

  function utf8Length(text) {
    var bytes = 0
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i)
      if (c < 0x80) bytes += 1
      else if (c < 0x800) bytes += 2
      else if (c >= 0xD800 && c <= 0xDBFF) { bytes += 4; i++ }
      else bytes += 3
    }
    return bytes
  }

  function send(text) {
    text = String(text || "").trim()
    if (text === "" || !bridge.running) return false
    if (utf8Length(text) > maxBytes) {
      showNotice("Message is longer than " + maxBytes + " bytes")
      return false
    }
    command({ cmd: "send", conv: activeConv, text: text })
    return true
  }

  function refresh() { command({ cmd: "refresh" }) }
  function advert() { command({ cmd: "advert" }) }

  function runSetup() {
    if (setupProc.running) return
    setupLog = []
    setupProc.running = true
  }

  function restart() {
    restartTimer.stop()
    messages = []
    unread = {}
    channels = []
    nodes = []
    linkState = "starting"
    if (bridge.running) {
      bridge.restartPending = true
      bridge.running = false
    } else {
      bridge.running = true
    }
  }

  function showNotice(text) {
    notice = text
    noticeTimer.restart()
  }

  function handleLine(line) {
    var ev
    try { ev = JSON.parse(line) } catch (e) { return }
    switch (ev.type) {
    case "history": messages = ev.items || []; reportViewing(); break
    case "unread": unread = ev.counts || {}; break
    case "status": linkState = ev.state; detail = ev.detail || ""; break
    case "self": selfId = ev.id || ""; selfName = ev.name || ""; break
    case "channels": channels = ev.items || []; break
    case "nodes": nodes = ev.items || []; break
    case "message": addMessage(ev.message); break
    case "ack": updateStatus(ev.id, ev.status, ev.detail); break
    case "error": showNotice(ev.detail || "Error"); break
    }
  }

  function addMessage(m) {
    var next = messages.slice(-499)
    next.push(m)
    messages = next
  }

  function updateStatus(id, status, why) {
    var next = messages.slice()
    for (var i = next.length - 1; i >= 0; i--) {
      if (next[i].id !== id) continue
      next[i] = Object.assign({}, next[i], { status: status })
      messages = next
      break
    }
    if (status === "failed" && why) showNotice("Not delivered: " + why)
  }

  Process {
    id: bridge
    property bool restartPending: false
    // A missing venv exits 3, the same code the bridge uses for missing libraries.
    command: ["sh", "-c", "[ -x \"$1\" ] || exit 3; exec \"$@\"", "sh",
              root.python, "-I", root.pluginDir + "bin/meshbridge.py", "attach",
              "--protocol", root.protocol, "--transport", root.transport, "--address", root.address]
             .concat(root.notify ? ["--notify"] : [])
    running: true
    stdinEnabled: true
    stdout: SplitParser { onRead: function(data) { root.handleLine(data) } }
    stderr: SplitParser { onRead: function(data) { console.log("mesh-chat bridge:", data) } }
    onExited: function(exitCode) {
      if (restartPending) {
        restartPending = false
        running = true
        return
      }
      if (exitCode === 3) {
        root.linkState = "missing-deps"
        return
      }
      root.linkState = "stopped"
      restartTimer.start()
    }
  }

  Process {
    id: setupProc
    command: [root.pluginDir + "bin/mesh-setup"]
    stdout: SplitParser { onRead: function(data) { root.setupLog = root.setupLog.concat([data]) } }
    stderr: SplitParser { onRead: function(data) { root.setupLog = root.setupLog.concat([data]) } }
    onExited: function(exitCode) {
      root.setupLog = root.setupLog.concat([exitCode === 0 ? "Done." : "Setup failed (exit " + exitCode + ")"])
      if (exitCode === 0) root.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 5000
    onTriggered: bridge.running = true
  }

  Timer {
    id: noticeTimer
    interval: 6000
    onTriggered: root.notice = ""
  }
}
