import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "banan.banansms-monitor"
  ipcTarget: moduleName
  manageIpc: false
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal
  readonly property bool demo: setting("demo", false) === true
  readonly property string scriptPath: Qt.resolvedUrl("client.py").toString().replace(/^file:\/\//, "")
  property bool ready: false
  property bool exists: false
  property bool locked: true
  property bool busy: false
  property bool settingsPage: false
  property string message: ""
  property var accounts: []
  property string selectedAccount: ""
  property var prefs: ({timeout: 900, interval: 300, low_limit: 100, failure_percent: 10, pending_minutes: 15, notify: true, show_limits: true, show_delivery: true, show_cost: true})
  property string editAccount: ""
  property string removeCandidate: ""
  property bool accountForm: false
  property double lastActivity: Date.now()
  property double lastTouch: 0
  property int serial: 0
  property string requestId: ""
  property string job: ""
  property string queued: ""
  readonly property var account: accounts.find(function(a) { return a.id === root.selectedAccount }) || ({})
  readonly property var limits: account.limits || ({})
  readonly property var reports: account.reports || ({})
  readonly property var summary: reports.data || ({})
  readonly property var alerts: account.alerts || []
  implicitWidth: barSize
  implicitHeight: barSize

  IpcHandler {
    target: root.moduleName
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function overview(): void { root.settingsPage = false; root.open() }
    function demoGeometry(): string {
      if (!root.demo || !root.opened || !root.accounts.length) return "{}"
      return JSON.stringify({x: popup.cardOrigin.x, y: popup.cardOrigin.y, width: popup.contentWidth, height: popup.contentHeight})
    }
    function settings(): void { root.settingsPage = true; root.open() }
    function lock(): void { root.lockNow() }
    function status(): string { return JSON.stringify({ready: root.ready, locked: root.locked, demo: root.demo, busy: root.busy, accounts: root.accounts.length}) }
  }
  function clearSecrets() {
    password.text = ""; confirmation.text = ""; apiToken.text = ""; accountName.text = ""
    currentPassword.text = ""; newPassword.text = ""; newConfirmation.text = ""
  }
  function clearView() {
    accounts = []; selectedAccount = ""; accountForm = false; editAccount = ""; removeCandidate = ""
    settingsPage = false; clearSecrets(); message = ""
    prefs = ({timeout: 900, interval: 300, low_limit: 100, failure_percent: 10, pending_minutes: 15, notify: true, show_limits: true, show_delivery: true, show_cost: true})
    fillSettings()
  }
  function stop() {
    requestId = ""; queued = ""; busy = false; backend.available = false
    if (backend.running) backend.signal(9)
  }
  function lockNow() {
    if (demo) return
    locked = true; stop(); clearView()
  }
  function activity() {
    if (!ready || locked || demo || !opened) return
    if (Date.now() - lastActivity >= prefs.timeout * 1000) { lockNow(); return }
    lastActivity = Date.now()
    if (!busy && Date.now() - lastTouch >= 1000) { lastTouch = Date.now(); if (backend.available) backend.write(JSON.stringify({action: "touch", id: "activity"}) + "\n") }
  }
  function startBackend() {
    backend.command = ["/usr/bin/python3", "-I", scriptPath, "--serve"].concat(demo ? ["--demo"] : [])
    backend.running = true
  }
  function request(data) {
    if (busy) return
    requestId = String(++serial); data.id = requestId; job = data.action; busy = true
    var line = JSON.stringify(data) + "\n"
    if (backend.running && backend.available) backend.write(line)
    else { queued = line; if (!backend.running) startBackend() }
  }
  function boot() { request({action: "status"}) }
  function refresh(force) {
    if (!ready || locked || busy) return
    request({action: "snapshot", refresh: !!force})
  }
  function unlock() {
    request({action: exists ? "unlock" : "create", password: password.text})
    password.text = ""; confirmation.text = ""
  }
  function setDemo(value) {
    Quickshell.execDetached(["/usr/share/omarchy/bin/omarchy", "bar", "set", moduleName, "demo", JSON.stringify(value), "--json"])
  }
  function fillSettings() {
    timeoutField.text = String(prefs.timeout); intervalField.text = String(prefs.interval)
    lowField.text = String(prefs.low_limit); failureField.text = String(prefs.failure_percent); pendingField.text = String(prefs.pending_minutes)
  }
  function saveSettings(extra) {
    request(Object.assign({action: "settings", timeout: Number(timeoutField.text), interval: Number(intervalField.text),
      low_limit: Number(lowField.text), failure_percent: Number(failureField.text), pending_minutes: Number(pendingField.text)}, extra || {}))
  }
  function clockText(value) { return value ? Qt.formatDateTime(new Date(value * 1000), "HH:mm:ss") : "Not checked yet" }
  function number(value, suffix) { return value === undefined || value === null ? "—" : String(value) + (suffix || "") }
  function alertText(value) {
    if (value.indexOf("low_") === 0) return "Low " + value.slice(4).toUpperCase() + " limit"
    return ({limits_api: "Limits API check failed", reports_api: "Reports API check failed", delivery_failures: "Delivery failure threshold reached", pending_too_long: "Some messages have been pending too long"})[value] || "Monitoring needs attention"
  }
  onDemoChanged: {
    stop(); locked = true; ready = false; clearView()
    if (!backend.running) Qt.callLater(boot); else restartTimer.restart()
  }
  onOpenedChanged: {
    if (opened) { if (!demo && !locked && Date.now() - lastActivity >= prefs.timeout * 1000) lockNow(); refresh(false) }
    else clearSecrets()
  }
  onSettingsPageChanged: { clearSecrets(); fillSettings() }
  Component.onCompleted: boot()
  Timer { id: restartTimer; interval: 100; repeat: true; onTriggered: { if (!backend.running) { restartTimer.stop(); root.boot() } } }
  Timer { interval: 250; repeat: true; running: !root.demo && !root.locked; onTriggered: if (Date.now() - root.lastActivity >= root.prefs.timeout * 1000) root.lockNow() }
  Timer { interval: 60000; repeat: true; running: root.ready && !root.locked; onTriggered: root.refresh(false) }

  Process {
    id: backend
    property bool available: false
    stdinEnabled: true
    onStarted: { available = true; if (root.queued) { write(root.queued); root.queued = "" } }
    stdout: SplitParser {
      onRead: function(line) {
        var result
        try { result = JSON.parse(line) } catch(e) { root.message = "Invalid helper response."; return }
        if (result.event === "locked") { root.lockNow(); return }
        if (String(result.id) !== root.requestId) return
        var kind = root.job; root.requestId = ""; root.busy = false
        if (!result.ok) { if (result.locked) root.lockNow(); root.message = result.error; return }
        if (kind === "touch") return
        if (kind === "status") {
          root.ready = true; root.exists = !!result.exists; root.locked = !root.demo
          if (root.demo) Qt.callLater(function() { root.refresh(false) })
          return
        }
        if (result.settings) { root.prefs = result.settings; if (kind !== "snapshot") root.fillSettings() }
        if (kind === "unlock" || kind === "create") {
          root.locked = false; root.exists = true; root.lastActivity = Date.now(); root.lastTouch = Date.now(); root.message = ""
          Qt.callLater(function() { root.refresh(false) }); return
        }
        if (kind === "snapshot") {
          root.accounts = result.accounts || []
          if (!root.accounts.some(function(a) { return a.id === root.selectedAccount })) root.selectedAccount = root.accounts.length ? root.accounts[0].id : ""
          if (result.notify && !root.demo) Quickshell.execDetached(["notify-send", "BananSMS Monitor", "Monitoring needs attention. Open the panel for details."])
          return
        }
        root.clearSecrets(); root.accountForm = false; root.editAccount = ""; root.removeCandidate = ""
        root.message = kind === "remove_account" ? "Local account removed. Your SerwerSMS account is unchanged." : "Saved in your encrypted vault."
        if (kind === "settings" || kind === "password") { root.lastActivity = Date.now(); root.lastTouch = Date.now() }
        Qt.callLater(function() { root.refresh(false) })
      }
    }
    onExited: {
      available = false
      if (root.queued) { Qt.callLater(root.startBackend); return }
      if (root.busy || (!root.demo && !root.locked)) {
        root.lockNow(); root.busy = false; root.message = "The local helper closed. Unlock again to resume monitoring."
      }
    }
  }
  BarIconButton {
    id: barButton
    anchors.fill: parent; bar: root.bar; text: ""; foreground: root.foreground
    iconComponent: Component { MonitorIcon { color: root.foreground } }
    tooltipText: "BananSMS Monitor" + (root.demo ? " · DEMO" : root.locked ? " · Locked" : root.accounts.some(function(a) { return (a.alerts || []).length > 0 }) ? " · Attention" : " · SerwerSMS")
    onPressed: function(button) { if (button === Qt.LeftButton) root.toggle() }
    Text { anchors.right: parent.right; anchors.bottom: parent.bottom; text: "!"; color: root.foreground; font.bold: true; visible: !root.locked && root.accounts.some(function(a) { return (a.alerts || []).length > 0 }) }
  }
  component Label: Text { color: root.foreground; font.family: Style.font.family; font.pixelSize: Style.font.body; textFormat: Text.PlainText; wrapMode: Text.Wrap }
  component Field: TextField { onTextEdited: root.activity() }
  component Action: Button { foreground: root.foreground; focusable: true; onClicked: root.activity() }
  component Divider: Rectangle { Layout.fillWidth: true; height: 1; color: root.foreground; opacity: 0.18 }
  component Metric: ColumnLayout {
    property string title: ""
    property string value: ""
    Layout.fillWidth: true; spacing: 3
    Label { text: parent.title; opacity: 0.7 }
    Label { text: parent.value; font.bold: true; font.pixelSize: Style.font.body * 1.3 }
  }
  KeyboardPanel {
    id: popup
    anchorItem: barButton; owner: root; bar: root.bar; open: root.opened
    contentWidth: fittedContentWidth(Style.space(560))
    contentHeight: cappedContentHeight(Style.space(root.locked ? 510 : 850))
    focusTarget: content
    Item {
      id: content
      anchors.fill: parent
      Keys.onEscapePressed: root.close()
      TapHandler { onPressedChanged: if (pressed) root.activity() }
      WheelHandler { target: null; blocking: false; onWheel: function(event) { root.activity(); event.accepted = false } }
      Controls.ScrollView {
        anchors.fill: parent; clip: true; contentWidth: availableWidth
        ColumnLayout {
          width: parent.width; spacing: Style.space(12)
          PanelHero {
            Layout.fillWidth: true; title: "BananSMS Monitor"; meta: "FOR SERWERSMS"; detail: root.demo ? "DEMO" : root.locked ? "LOCKED" : "READ ONLY"
            iconComponent: Component { MonitorIcon { iconSize: Style.space(28); color: root.foreground } }
            trailingControl: Component { PanelActionButton { iconText: "⚙"; tooltipText: "Settings"; foreground: root.foreground; enabled: !root.locked; onClicked: root.settingsPage = !root.settingsPage } }
          }
          Label { Layout.fillWidth: true; visible: root.demo; text: "Demo · Fictional statistics. No account access or network requests." }
          Label { Layout.fillWidth: true; visible: root.message !== ""; text: root.message }
          ColumnLayout {
            Layout.fillWidth: true; visible: root.locked; spacing: 10
            Label { Layout.fillWidth: true; text: !root.ready ? "Checking encrypted storage…" : root.exists ? "Unlock your vault to resume monitoring." : "Create your encrypted monitor. One master password protects API tokens, settings and cached statistics." }
            Field { id: password; Layout.fillWidth: true; password: true; placeholderText: root.exists ? "Master password" : "Master password · at least 8 characters"; maximumLength: 128; enabled: root.ready && !root.busy; onAccepted: if (root.exists && text.length > 0 && !root.busy) root.unlock() }
            Field { id: confirmation; Layout.fillWidth: true; visible: !root.exists; password: true; placeholderText: "Confirm master password"; maximumLength: 128; enabled: !root.busy }
            Label { Layout.fillWidth: true; visible: !root.exists; text: "There is no password recovery. Store your password safely. Monitoring pauses whenever the vault is locked." }
            Action { text: root.busy ? "Opening…" : root.exists ? "Unlock" : "Create encrypted vault"; enabled: root.ready && !root.busy && (root.exists ? password.text.length > 0 : password.text.length >= 8 && password.text === confirmation.text); onClicked: root.unlock() }
            Action { text: "Explore demo"; enabled: !root.busy; onClicked: root.setDemo(true) }
            Action { text: "Retry"; visible: !root.ready; enabled: !root.busy; onClicked: root.boot() }
          }
          ColumnLayout {
            Layout.fillWidth: true; visible: !root.locked && !root.settingsPage; spacing: 12
            Flow {
              Layout.fillWidth: true; spacing: 6
              Repeater { model: root.accounts; Action { required property var modelData; text: modelData.name; selected: modelData.id === root.selectedAccount; onClicked: { root.activity(); root.selectedAccount = modelData.id } } }
            }
            Label { Layout.fillWidth: true; visible: !root.accounts.length; text: "Connect a SerwerSMS account to monitor limits and delivery reports. Enter the API token in Settings." }
            Action { visible: !root.accounts.length; text: "Connect account"; onClicked: { root.settingsPage = true; root.accountForm = true } }
            ColumnLayout {
              Layout.fillWidth: true; visible: root.accounts.length > 0; spacing: 10
              RowLayout {
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; font.bold: true; text: root.limits.error || root.reports.error ? "API · Check failed" : root.limits.at && root.reports.at ? "API · Responding" : "API · Not checked yet" }
                Action { text: root.busy ? "Checking…" : "Refresh"; enabled: !root.busy; onClicked: root.refresh(true) }
              }
              Label { Layout.fillWidth: true; text: "Limits: " + root.clockText(root.limits.at) + " · Reports: " + root.clockText(root.reports.at) }
              Label { Layout.fillWidth: true; visible: !!root.reports.latency_ms; text: "Last reports response: " + root.number(root.reports.latency_ms, " ms") + ". API availability does not guarantee SMS delivery." }
              Label { Layout.fillWidth: true; visible: !!root.limits.error || !!root.reports.error; text: [root.limits.error, root.reports.error].filter(Boolean).join("\n") + "\nPrevious successful values, if any, remain visible and may be stale." }
              Repeater { model: root.alerts; Label { required property string modelData; Layout.fillWidth: true; text: "! " + root.alertText(modelData); font.bold: true } }
              Divider {}
              ColumnLayout {
                Layout.fillWidth: true; visible: root.prefs.show_limits
                Label { text: "Service limits"; font.bold: true }
                Repeater {
                  model: root.limits.data || []
                  RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Label { Layout.fillWidth: true; text: modelData.type.toUpperCase() }
                    Label { text: modelData.value === "no_limit" ? "Unlimited" : modelData.value === "not_available" ? "Not available" : modelData.value + " remaining" }
                  }
                }
              }
              ColumnLayout {
                Layout.fillWidth: true; visible: root.prefs.show_delivery; spacing: 10
                Divider {}
                Label { text: "Returned delivery reports · last 24 hours"; font.bold: true }
                GridLayout {
                  Layout.fillWidth: true; columns: 3; rowSpacing: 12
                  Metric { title: "Total"; value: root.number(root.summary.total) }
                  Metric { title: "Delivered"; value: root.number((root.summary.counts || {}).delivered || 0) }
                  Metric { title: "Failed"; value: root.number(root.summary.failed) }
                  Metric { title: "Pending"; value: root.number(root.summary.pending) }
                  Metric { title: "Delivery rate"; value: root.number(root.summary.delivery_percent, "%") }
                  Metric { title: "Mean delivery"; value: root.number(root.summary.mean_delay, " s") }
                }
                Label { Layout.fillWidth: true; opacity: 0.7; text: "Rate includes completed outcomes only. Delivery time uses reports with valid sent and delivered timestamps. Dates without an offset are interpreted in Europe/Warsaw." }
                Repeater { model: root.summary.reasons || []; Label { required property var modelData; Layout.fillWidth: true; text: modelData.reason.replace(/_/g, " ") + " · " + modelData.count } }
              }
              ColumnLayout {
                Layout.fillWidth: true; visible: root.prefs.show_cost
                Divider {}
                Label { text: "Reported cost · " + root.number(root.summary.cost); font.bold: true }
                Label { Layout.fillWidth: true; text: root.number(root.summary.cost_count || 0) + " reports with cost · " + root.number(root.summary.parts || 0) + " reported segments. Values come from the API; this is not an invoice or account balance." }
              }
            }
          }
          ColumnLayout {
            Layout.fillWidth: true; visible: !root.locked && root.settingsPage; spacing: 10
            Label { Layout.fillWidth: true; text: "Encrypted storage · SQLCipher. Locking clears the panel and stops monitoring until you enter your master password." }
            ColumnLayout {
              Layout.fillWidth: true; visible: !root.demo
              Label { text: "Connected accounts"; font.bold: true }
              Repeater {
                model: root.accounts
                ColumnLayout {
                  required property var modelData
                  Layout.fillWidth: true
                  Label { text: modelData.name }
                  Flow {
                    Layout.fillWidth: true; spacing: 6
                    Action { text: "Update token"; enabled: !root.busy; onClicked: { root.editAccount = modelData.id; root.accountForm = true; accountName.text = modelData.name; apiToken.text = "" } }
                    Action { text: "Remove"; enabled: !root.busy; onClicked: root.removeCandidate = modelData.id }
                    Action { text: "Confirm removal"; visible: root.removeCandidate === modelData.id; enabled: !root.busy; onClicked: root.request({action: "remove_account", account: modelData.id}) }
                    Action { text: "Cancel"; visible: root.removeCandidate === modelData.id; onClicked: root.removeCandidate = "" }
                  }
                }
              }
              Action { text: root.accountForm ? "Cancel" : "Add account"; enabled: !root.busy; onClicked: { root.accountForm = !root.accountForm; root.editAccount = ""; accountName.text = ""; apiToken.text = "" } }
              ColumnLayout {
                Layout.fillWidth: true; visible: root.accountForm
                Label { Layout.fillWidth: true; text: "Create an API token in SerwerSMS: Interface settings → HTTPS API → API tokens. This plugin uses only limits and reports endpoints." }
                Field { id: accountName; Layout.fillWidth: true; placeholderText: "Account label"; maximumLength: 60 }
                Field { id: apiToken; Layout.fillWidth: true; placeholderText: "SerwerSMS API token"; password: true; maximumLength: 514 }
                Action { text: "Check and save account"; enabled: !root.busy && accountName.text.trim().length > 0 && apiToken.text.length >= 16; onClicked: { root.request({action: "save_account", account: root.editAccount, name: accountName.text, token: apiToken.text}); apiToken.text = "" } }
              }
              Divider {}
              Label { text: "Monitoring and alerts"; font.bold: true }
              Label { text: "Reports refresh interval · seconds (60–3600)" }
              Field { id: intervalField; Layout.fillWidth: true; text: "300"; validator: IntValidator { bottom: 60; top: 3600 } }
              Label { Layout.fillWidth: true; text: "Limits refresh hourly. Manual refresh has a 30-second minimum interval. Monitoring runs only while unlocked."; opacity: 0.7 }
              Label { text: "Low service limit · remaining units" }
              Field { id: lowField; Layout.fillWidth: true; text: "100"; validator: IntValidator { bottom: 0; top: 1000000 } }
              Label { text: "Failure alert · percent (at least 5 completed reports)" }
              Field { id: failureField; Layout.fillWidth: true; text: "10"; validator: IntValidator { bottom: 1; top: 100 } }
              Label { text: "Pending alert · minutes" }
              Field { id: pendingField; Layout.fillWidth: true; text: "15"; validator: IntValidator { bottom: 1; top: 1440 } }
              Label { text: "Lock after inactivity · seconds (30–86400)" }
              Field { id: timeoutField; Layout.fillWidth: true; text: "900"; validator: IntValidator { bottom: 30; top: 86400 } }
              Action { text: "Save settings"; enabled: !root.busy && intervalField.acceptableInput && lowField.acceptableInput && failureField.acceptableInput && pendingField.acceptableInput && timeoutField.acceptableInput; onClicked: root.saveSettings() }
              Repeater {
                model: [{key: "notify", title: "Generic desktop alerts"}, {key: "show_limits", title: "Show service limits"}, {key: "show_delivery", title: "Show delivery statistics"}, {key: "show_cost", title: "Show reported costs"}]
                RowLayout {
                  required property var modelData
                  Layout.fillWidth: true
                  Label { Layout.fillWidth: true; text: modelData.title }
                  ToggleSwitch { checked: !!root.prefs[modelData.key]; enabled: !root.busy; onToggled: { var value = {}; value[modelData.key] = !checked; root.saveSettings(value) } }
                }
              }
              Divider {}
              Label { text: "Master password"; font.bold: true }
              Field { id: currentPassword; Layout.fillWidth: true; password: true; placeholderText: "Current master password"; maximumLength: 128 }
              Field { id: newPassword; Layout.fillWidth: true; password: true; placeholderText: "New master password · at least 8 characters"; maximumLength: 128 }
              Field { id: newConfirmation; Layout.fillWidth: true; password: true; placeholderText: "Confirm new master password"; maximumLength: 128 }
              Action { text: "Change master password"; enabled: !root.busy && currentPassword.text.length > 0 && newPassword.text.length >= 8 && newPassword.text === newConfirmation.text; onClicked: { root.request({action: "password", current_password: currentPassword.text, password: newPassword.text}); root.clearSecrets() } }
              Action { text: "Lock now"; onClicked: root.lockNow() }
            }
            Label { Layout.fillWidth: true; visible: root.demo; text: "Switch off demo to create your encrypted vault and configure accounts, thresholds and visibility." }
          }
          Action { visible: !root.locked; text: root.demo ? "Leave demo · configure my monitor" : "Explore demo"; enabled: !root.busy; onClicked: root.setDemo(!root.demo) }
          Label { Layout.fillWidth: true; opacity: 0.5; text: "BananSMS Monitor 0.1.0 · Read-only SerwerSMS monitoring" }
        }
      }
    }
  }
}
