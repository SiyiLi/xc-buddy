import AppKit

final class SettingsWindowController: NSWindowController {
    private let apiKeyField = NSTextField()
    private let openAIBaseURLField = NSTextField()
    private let openAIModelField = NSTextField()
    private let openAIPromptField = NSTextField()
    private let hotwordsTextView = NSTextView()
    private let hotwordsScrollView = NSScrollView()
    private let llmBaseURLField = NSTextField()
    private let llmAPIKeyField = NSTextField()
    private let llmModelField = NSTextField()
    private let debugAudioButton = NSButton(checkboxWithTitle: "Save debug audio files", target: nil, action: nil)
    private let debugAudioDirectoryField = NSTextField()
    private let statusLabel = NSTextField(labelWithString: "")
    private let versionLabel = NSTextField(labelWithString: "")
    private let codexBridgePortField = NSTextField()
    private let codexSuccessChimeButton = NSButton(
        checkboxWithTitle: "Play on Stick when Codex finishes",
        target: nil,
        action: nil
    )
    private let displayDimSecondsField = NSTextField()
    private let displayOffMinutesField = NSTextField()
    private let idleSleepMinutesField = NSTextField()
    private let codexSleepMinutesField = NSTextField()
    private let relayURLField = NSTextField()
    private let relaySenderTokenField = NSSecureTextField()
    private let relayReceiverTokenField = NSSecureTextField()
    private let onCheckForUpdates: (() -> Void)?
    var onConfigChanged: ((AppConfig) -> Void)?

    private var config: AppConfig

    init(config: AppConfig = AppConfig.load(), onCheckForUpdates: (() -> Void)? = nil) {
        self.config = config
        self.onCheckForUpdates = onCheckForUpdates
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 640),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "XC Buddy Settings"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildContent()
        loadConfigIntoFields()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func show() {
        config = AppConfig.load()
        loadConfigIntoFields()
        showWindow(nil)
        window?.makeFirstResponder(apiKeyField)
        window?.center()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildContent() {
        guard let contentView = window?.contentView else { return }

        let root = settingsStack()
        root.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(root)

        let tabView = NSTabView()
        tabView.translatesAutoresizingMaskIntoConstraints = false
        tabView.addTabViewItem(makeTab(title: "AI Services") { stack in
            stack.addArrangedSubview(self.sectionTitle("Transcription"))
            stack.addArrangedSubview(self.row(label: "API Key", control: self.apiKeyField))
            stack.addArrangedSubview(self.row(label: "Base URL", control: self.openAIBaseURLField))
            stack.addArrangedSubview(self.row(label: "Model", control: self.openAIModelField))
            stack.addArrangedSubview(self.row(label: "Prompt", control: self.openAIPromptField))
            self.configureHotwordsTextView()
            stack.addArrangedSubview(self.row(label: "Hotwords", control: self.hotwordsScrollView))
            stack.addArrangedSubview(self.hintRow("Separate hotwords with commas or new lines."))

            stack.addArrangedSubview(self.sectionTitle("LLM"))
            stack.addArrangedSubview(self.row(label: "Base URL", control: self.llmBaseURLField))
            stack.addArrangedSubview(self.row(label: "API Key", control: self.llmAPIKeyField))
            stack.addArrangedSubview(self.row(label: "Model", control: self.llmModelField))
        })
        tabView.addTabViewItem(makeTab(title: "Codex & Relay") { stack in
            stack.addArrangedSubview(self.sectionTitle("Codex Bridge (Loopback Only)"))
            stack.addArrangedSubview(self.row(label: "Port", control: self.codexBridgePortField))
            stack.addArrangedSubview(self.row(label: "Codex Chimes", control: self.codexSuccessChimeButton))

            stack.addArrangedSubview(self.sectionTitle("XC Body Relay"))
            stack.addArrangedSubview(self.row(label: "Relay URL", control: self.relayURLField))
            stack.addArrangedSubview(self.row(label: "Sender Token", control: self.relaySenderTokenField))
            stack.addArrangedSubview(self.row(label: "Receiver Token", control: self.relayReceiverTokenField))
            stack.addArrangedSubview(self.hintRow("Tokens are stored with the local XC Buddy configuration."))
        })
        tabView.addTabViewItem(makeTab(title: "Device & Advanced") { stack in
            stack.addArrangedSubview(self.sectionTitle("Stick Power"))
            stack.addArrangedSubview(self.timerRow(label: "Display", timers: [
                ("Dim", self.displayDimSecondsField, "sec"),
                ("Off", self.displayOffMinutesField, "min")
            ]))
            stack.addArrangedSubview(self.timerRow(label: "Deep Sleep", timers: [
                ("Idle", self.idleSleepMinutesField, "min"),
                ("Codex", self.codexSleepMinutesField, "min")
            ]))

            stack.addArrangedSubview(self.sectionTitle("Debug"))
            stack.addArrangedSubview(self.row(label: "Audio Cache", control: self.debugAudioButton))
            stack.addArrangedSubview(self.row(label: "Audio Folder", control: self.debugAudioDirectoryControl()))
        })
        tabView.addTabViewItem(makeTab(title: "About") { stack in
            let identity = NSStackView()
            identity.orientation = .horizontal
            identity.alignment = .centerY
            identity.spacing = 16

            let iconView = NSImageView()
            iconView.image = NSApp.applicationIconImage
            iconView.imageScaling = .scaleProportionallyUpOrDown
            iconView.widthAnchor.constraint(equalToConstant: 72).isActive = true
            iconView.heightAnchor.constraint(equalToConstant: 72).isActive = true

            let identityText = NSStackView()
            identityText.orientation = .vertical
            identityText.alignment = .leading
            identityText.spacing = 6
            let appName = NSTextField(labelWithString: "XC Buddy")
            appName.font = .systemFont(ofSize: 18, weight: .semibold)
            self.versionLabel.stringValue = "Version: \(Self.applicationVersion)"
            self.versionLabel.textColor = .secondaryLabelColor
            identityText.addArrangedSubview(appName)
            identityText.addArrangedSubview(self.versionLabel)

            identity.addArrangedSubview(iconView)
            identity.addArrangedSubview(identityText)
            stack.addArrangedSubview(identity)

            let actions = NSStackView()
            actions.orientation = .horizontal
            actions.alignment = .centerY
            actions.spacing = 10
            actions.addArrangedSubview(NSButton(
                title: "Website",
                target: self,
                action: #selector(openWebsite)
            ))
            if self.onCheckForUpdates != nil {
                actions.addArrangedSubview(NSButton(
                    title: "Check for App Updates...",
                    target: self,
                    action: #selector(checkForUpdates)
                ))
            }
            stack.addArrangedSubview(actions)
        })
        root.addArrangedSubview(tabView)
        tabView.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        tabView.heightAnchor.constraint(equalToConstant: 520).isActive = true

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.spacing = 10
        let openFolderButton = NSButton(title: "Open Config Folder", target: self, action: #selector(openConfigFolder))
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let saveButton = NSButton(title: "Save", target: self, action: #selector(saveSettings))
        saveButton.keyEquivalent = "\r"
        buttonRow.addArrangedSubview(openFolderButton)
        buttonRow.addArrangedSubview(statusLabel)
        buttonRow.addArrangedSubview(spacer)
        buttonRow.addArrangedSubview(saveButton)
        root.addArrangedSubview(buttonRow)
        buttonRow.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        statusLabel.textColor = .secondaryLabelColor

        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 24),
            root.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -24)
        ])
    }

    private func makeTab(title: String, content: (NSStackView) -> Void) -> NSTabViewItem {
        let view = NSView()
        let stack = settingsStack()
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        content(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 16),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.bottomAnchor, constant: -12)
        ])

        let item = NSTabViewItem(identifier: title)
        item.label = title
        item.view = view
        return item
    }

    private func settingsStack() -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        return stack
    }

    private func debugAudioDirectoryControl() -> NSStackView {
        let control = NSStackView()
        control.orientation = .horizontal
        control.alignment = .centerY
        control.spacing = 8
        debugAudioDirectoryField.isEditable = false
        debugAudioDirectoryField.lineBreakMode = .byTruncatingMiddle
        let chooseButton = NSButton(title: "Choose...", target: self, action: #selector(chooseDebugDirectory))
        control.addArrangedSubview(debugAudioDirectoryField)
        control.addArrangedSubview(chooseButton)
        debugAudioDirectoryField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        return control
    }

    private func configureHotwordsTextView() {
        hotwordsScrollView.hasVerticalScroller = true
        hotwordsScrollView.borderType = .bezelBorder
        hotwordsScrollView.heightAnchor.constraint(equalToConstant: 78).isActive = true
        hotwordsScrollView.translatesAutoresizingMaskIntoConstraints = false

        hotwordsTextView.isRichText = false
        hotwordsTextView.isEditable = true
        hotwordsTextView.isSelectable = true
        hotwordsTextView.font = .systemFont(ofSize: 13)
        hotwordsTextView.textColor = .textColor
        hotwordsTextView.backgroundColor = .textBackgroundColor
        hotwordsTextView.drawsBackground = true
        hotwordsTextView.textContainerInset = NSSize(width: 4, height: 4)
        hotwordsTextView.minSize = NSSize(width: 0, height: hotwordsScrollView.contentSize.height)
        hotwordsTextView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        hotwordsTextView.isVerticallyResizable = true
        hotwordsTextView.isHorizontallyResizable = false
        hotwordsTextView.autoresizingMask = [.width]
        hotwordsTextView.frame = NSRect(origin: .zero, size: NSSize(width: 300, height: 78))
        hotwordsTextView.textContainer?.containerSize = NSSize(
            width: hotwordsTextView.frame.width,
            height: CGFloat.greatestFiniteMagnitude
        )
        hotwordsTextView.textContainer?.widthTracksTextView = true
        hotwordsScrollView.documentView = hotwordsTextView
    }

    private func loadConfigIntoFields() {
        apiKeyField.stringValue = config.openAIAPIKey
        openAIBaseURLField.stringValue = config.openAIBaseURL
        openAIModelField.stringValue = config.openAIModel
        openAIPromptField.stringValue = config.openAIPrompt
        hotwordsTextView.string = config.asrHotwords.joined(separator: ",")
        llmBaseURLField.stringValue = config.llmBaseURL
        llmAPIKeyField.stringValue = config.llmAPIKey
        llmModelField.stringValue = config.llmModel
        debugAudioButton.state = config.debugAudioCache ? .on : .off
        debugAudioDirectoryField.stringValue = config.debugAudioDirectory.path
        codexBridgePortField.stringValue = String(config.codexBridgePort)
        codexSuccessChimeButton.state = config.codexSuccessChime ? .on : .off
        relayURLField.stringValue = config.relayURL
        relaySenderTokenField.stringValue = config.relaySenderToken
        relayReceiverTokenField.stringValue = config.relayReceiverToken
        displayDimSecondsField.integerValue = config.devicePowerTimers.displayDimSeconds
        displayOffMinutesField.stringValue = minuteText(config.devicePowerTimers.displayOffSeconds)
        idleSleepMinutesField.stringValue = minuteText(config.devicePowerTimers.idleDeepSleepSeconds)
        codexSleepMinutesField.stringValue = minuteText(config.devicePowerTimers.codexDeepSleepSeconds)

        statusLabel.stringValue = ""
    }

    @objc private func chooseDebugDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: debugAudioDirectoryField.stringValue)
        if panel.runModal() == .OK, let url = panel.url {
            debugAudioDirectoryField.stringValue = url.path
        }
    }

    @objc private func saveSettings() {
        config = AppConfig(
            openAIBaseURL: openAIBaseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            openAIAPIKey: apiKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            openAIModel: openAIModelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            openAILanguage: config.openAILanguage,
            openAIPrompt: openAIPromptField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            llmBaseURL: llmBaseURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            llmAPIKey: llmAPIKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            llmModel: llmModelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            interactionMode: config.interactionMode,
            asrHotwords: AppConfig.hotwordList(hotwordsTextView.string),
            pairedDeviceIDs: config.pairedDeviceIDs,
            deviceThemeColors: config.deviceThemeColors,
            deviceOverlayPositions: config.deviceOverlayPositions,
            defaultOutputProfile: config.defaultOutputProfile,
            deviceOutputProfiles: config.deviceOutputProfiles,
            autoEnter: config.autoEnter,
            debugAudioCache: debugAudioButton.state == .on,
            debugAudioDirectory: URL(fileURLWithPath: debugAudioDirectoryField.stringValue, isDirectory: true),
            codexBridgePort: max(1, min(65535, codexBridgePortField.integerValue)),
            codexBridgeToken: config.codexBridgeToken,
            codexSuccessChime: codexSuccessChimeButton.state == .on,
            relayMode: config.relayMode,
            relayURL: relayURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            relaySenderToken: relaySenderTokenField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            relayReceiverToken: relayReceiverTokenField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            devicePowerTimers: DevicePowerTimers(
                displayDimSeconds: max(5, min(3600, displayDimSecondsField.integerValue)),
                displayOffSeconds: seconds(fromMinutesField: displayOffMinutesField,
                                           range: 30...86400),
                idleDeepSleepSeconds: seconds(fromMinutesField: idleSleepMinutesField,
                                              range: 60...86400),
                codexDeepSleepSeconds: seconds(fromMinutesField: codexSleepMinutesField,
                                               range: 60...86400)
            )
        )

        do {
            try config.save()
            onConfigChanged?(config)
            statusLabel.stringValue = "Saved."
            window?.close()
        } catch {
            statusLabel.stringValue = ""
            showErrorAlert(title: "Could Not Save Settings", message: error.localizedDescription)
        }
    }

    @objc private func openConfigFolder() {
        AppConfig.openConfigDirectory()
    }

    @objc private func openWebsite() {
        NSWorkspace.shared.open(AppConfig.websiteURL)
    }

    @objc private func checkForUpdates() {
        onCheckForUpdates?()
    }

    private func showErrorAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        if let window {
            alert.beginSheetModal(for: window)
        } else {
            alert.runModal()
        }
    }

    private func sectionTitle(_ title: String) -> NSTextField {
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 13, weight: .semibold)
        label.textColor = .secondaryLabelColor
        return label
    }

    private func row(label: String, control: NSView) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .right
        labelView.textColor = .secondaryLabelColor
        labelView.widthAnchor.constraint(equalToConstant: 120).isActive = true
        if control is NSTextField || control is NSPopUpButton || control is NSStackView || control is NSScrollView {
            control.widthAnchor.constraint(greaterThanOrEqualToConstant: 300).isActive = true
        }
        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        return row
    }

    private func timerRow(label: String, timers: [(String, NSTextField, String)]) -> NSStackView {
        let controls = NSStackView()
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.spacing = 16

        for (name, field, unit) in timers {
            let timer = NSStackView()
            timer.orientation = .horizontal
            timer.alignment = .centerY
            timer.spacing = 6

            let nameLabel = NSTextField(labelWithString: name)
            nameLabel.alignment = .right
            nameLabel.textColor = .secondaryLabelColor
            nameLabel.widthAnchor.constraint(equalToConstant: 44).isActive = true
            field.alignment = .right
            field.widthAnchor.constraint(equalToConstant: 52).isActive = true
            let unitLabel = NSTextField(labelWithString: unit)
            unitLabel.textColor = .secondaryLabelColor
            unitLabel.widthAnchor.constraint(equalToConstant: 24).isActive = true

            timer.addArrangedSubview(nameLabel)
            timer.addArrangedSubview(field)
            timer.addArrangedSubview(unitLabel)
            controls.addArrangedSubview(timer)
        }

        return row(label: label, control: controls)
    }

    private func minuteText(_ seconds: Int) -> String {
        if seconds.isMultiple(of: 60) {
            return String(seconds / 60)
        }
        var text = String(format: "%.2f", Double(seconds) / 60.0)
        while text.last == "0" { text.removeLast() }
        if text.last == "." { text.removeLast() }
        return text
    }

    private func seconds(fromMinutesField field: NSTextField,
                         range: ClosedRange<Int>) -> Int {
        let seconds = (field.doubleValue * 60.0).rounded()
        guard seconds.isFinite else { return range.lowerBound }
        let bounded = min(max(seconds, Double(range.lowerBound)), Double(range.upperBound))
        return Int(bounded)
    }

    private func hintRow(_ text: String) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12

        let spacer = NSView()
        spacer.widthAnchor.constraint(equalToConstant: 120).isActive = true

        let label = NSTextField(labelWithString: text)
        label.textColor = .secondaryLabelColor
        label.font = .systemFont(ofSize: 11)
        label.widthAnchor.constraint(greaterThanOrEqualToConstant: 300).isActive = true

        row.addArrangedSubview(spacer)
        row.addArrangedSubview(label)
        return row
    }

    private static var applicationVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "Unknown"
    }
}
