import Foundation

enum RelayState: String, Codable {
    case idle
    case working
    case approvalNeeded = "approval_needed"
}

enum RelayNotice: String, Codable {
    case done
    case error
}

enum RelayInboundMessage: Equatable {
    case state(RelayState, snapshot: Bool)
    case notice(RelayNotice)

    var lifecycleEvent: CodexLifecycleEvent {
        switch self {
        case .state(.idle, _):
            return .idle
        case .state(.working, _):
            return .working
        case .state(.approvalNeeded, _):
            return .approvalNeeded
        case .notice(.done):
            return .done
        case .notice(.error):
            return .error("Codex error")
        }
    }

    private enum CodingKeys: String, CodingKey {
        case kind
        case state
        case snapshot
        case notice
    }

    private enum Kind: String, Decodable {
        case state
        case notice
    }
}

private struct RelayCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

extension RelayInboundMessage: Decodable {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawContainer = try decoder.container(keyedBy: RelayCodingKey.self)
        let receivedKeys = Set(rawContainer.allKeys.map(\.stringValue))
        let kind = try container.decode(Kind.self, forKey: .kind)
        switch kind {
        case .state:
            guard receivedKeys == ["kind", "state", "snapshot"] else {
                throw DecodingError.dataCorruptedError(
                    forKey: .kind,
                    in: container,
                    debugDescription: "Invalid relay state message."
                )
            }
            self = .state(
                try container.decode(RelayState.self, forKey: .state),
                snapshot: try container.decode(Bool.self, forKey: .snapshot)
            )
        case .notice:
            guard receivedKeys == ["kind", "notice"] else {
                throw DecodingError.dataCorruptedError(
                    forKey: .kind,
                    in: container,
                    debugDescription: "Invalid relay notice message."
                )
            }
            self = .notice(try container.decode(RelayNotice.self, forKey: .notice))
        }
    }
}

private struct RelayStateMessage: Encodable {
    let kind = "state"
    let state: RelayState
}

private struct RelayNoticeMessage: Encodable {
    let kind = "notice"
    let notice: RelayNotice
}

final class RelayClient: NSObject {
    var onConnectionStatus: ((String) -> Void)?
    var onConnectionLost: ((RelayMode) -> Void)?
    var onMessage: ((RelayInboundMessage) -> Void)?

    private lazy var session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    private var mode: RelayMode = .disabled
    private var endpoint = ""
    private var token = ""
    private var task: URLSessionWebSocketTask?
    private var taskIdentity: UUID?
    private var hasOpened = false
    private var reconnectAttempt = 0
    private var reconnectWorkItem: DispatchWorkItem?
    private var pingWorkItem: DispatchWorkItem?
    private var pongTimeoutWorkItem: DispatchWorkItem?
    private var pingInFlight = false
    private var senderState: RelayState = .idle
    private let pingInterval: TimeInterval = 30
    private let pongTimeout: TimeInterval = 10

    func start(mode: RelayMode, endpoint: String, token: String) {
        precondition(Thread.isMainThread)
        guard mode != .disabled else {
            stop()
            onConnectionStatus?("Disabled")
            return
        }
        if self.mode == mode && self.endpoint == endpoint && self.token == token {
            guard task == nil, reconnectWorkItem == nil else { return }
            connect()
            return
        }
        stop()
        self.mode = mode
        self.endpoint = endpoint
        self.token = token
        connect()
    }

    func stop() {
        precondition(Thread.isMainThread)
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        cancelPing()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        taskIdentity = nil
        hasOpened = false
        reconnectAttempt = 0
        mode = .disabled
        token = ""
    }

    func retainSenderState(_ state: RelayState) {
        precondition(Thread.isMainThread)
        senderState = state
    }

    func publishSenderLifecycleEvent(_ event: CodexLifecycleEvent) {
        precondition(Thread.isMainThread)
        switch event {
        case .idle:
            publishSenderState(.idle)
        case .working, .toolCallStarted:
            publishSenderState(.working)
        case .approvalNeeded:
            publishSenderState(.approvalNeeded)
        case .done:
            senderState = .idle
            sendSenderNotice(.done)
        case .error:
            senderState = .idle
            sendSenderNotice(.error)
        }
    }

    func publishSenderState(_ state: RelayState) {
        precondition(Thread.isMainThread)
        senderState = state
        guard mode == .sender else { return }
        send(RelayStateMessage(state: state))
    }

    func sendSenderNotice(_ notice: RelayNotice) {
        precondition(Thread.isMainThread)
        guard mode == .sender else { return }
        send(RelayNoticeMessage(notice: notice))
    }

    private func connect() {
        precondition(Thread.isMainThread)
        guard mode != .disabled, task == nil else { return }
        guard let url = URL(string: endpoint.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme?.lowercased() == "wss" else {
            onConnectionStatus?("Relay URL must use wss://")
            return
        }

        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else {
            onConnectionStatus?("Relay \(mode.displayName.lowercased()) token is not set")
            return
        }

        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let task = session.webSocketTask(with: request)
        let identity = UUID()
        self.task = task
        taskIdentity = identity
        onConnectionStatus?("Connecting")
        task.resume()
    }

    private func receiveNext(on task: URLSessionWebSocketTask, identity: UUID) {
        task.receive { [weak self] result in
            DispatchQueue.main.async {
                guard let self, self.taskIdentity == identity else { return }
                switch result {
                case .success(let message):
                    let data: Data?
                    switch message {
                    case .data(let value):
                        data = value
                    case .string(let value):
                        data = Data(value.utf8)
                    @unknown default:
                        data = nil
                    }
                    if let data, let inbound = try? JSONDecoder().decode(RelayInboundMessage.self, from: data) {
                        self.onMessage?(inbound)
                    }
                    self.receiveNext(on: task, identity: identity)
                case .failure:
                    self.connectionClosed(identity: identity)
                }
            }
        }
    }

    private func send<Message: Encodable>(_ message: Message) {
        guard let task, let identity = taskIdentity,
              let data = try? JSONEncoder().encode(message) else { return }
        guard let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { [weak self] error in
            guard error != nil else { return }
            DispatchQueue.main.async {
                self?.connectionClosed(identity: identity)
            }
        }
    }

    private func connectionOpened(task: URLSessionWebSocketTask) {
        guard task === self.task, let identity = taskIdentity else { return }
        hasOpened = true
        reconnectAttempt = 0
        onConnectionStatus?("Connected")
        if mode == .sender {
            send(RelayStateMessage(state: senderState))
        }
        receiveNext(on: task, identity: identity)
        schedulePing(on: task, identity: identity)
    }

    private func connectionClosed(identity: UUID) {
        guard taskIdentity == identity else { return }
        let closedMode = mode
        let wasConnected = hasOpened
        cancelPing()
        task = nil
        taskIdentity = nil
        hasOpened = false
        if wasConnected {
            onConnectionLost?(closedMode)
        }
        guard closedMode != .disabled else { return }
        onConnectionStatus?("Reconnecting")
        scheduleReconnect()
    }

    private func schedulePing(on task: URLSessionWebSocketTask, identity: UUID) {
        pingWorkItem?.cancel()
        let workItem = DispatchWorkItem { [weak self, weak task] in
            guard let self, let task else { return }
            self.pingWorkItem = nil
            self.sendPing(on: task, identity: identity)
        }
        pingWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + pingInterval, execute: workItem)
    }

    private func sendPing(on task: URLSessionWebSocketTask, identity: UUID) {
        guard self.task === task, taskIdentity == identity, !pingInFlight else { return }
        pingInFlight = true

        let timeoutWorkItem = DispatchWorkItem { [weak self, weak task] in
            guard let self, let task,
                  self.task === task, self.taskIdentity == identity, self.pingInFlight else { return }
            task.cancel()
            self.connectionClosed(identity: identity)
        }
        pongTimeoutWorkItem = timeoutWorkItem
        DispatchQueue.main.asyncAfter(deadline: .now() + pongTimeout, execute: timeoutWorkItem)

        task.sendPing { [weak self, weak task] error in
            DispatchQueue.main.async {
                guard let self, let task,
                      self.task === task, self.taskIdentity == identity, self.pingInFlight else { return }
                self.pongTimeoutWorkItem?.cancel()
                self.pongTimeoutWorkItem = nil
                self.pingInFlight = false
                if error != nil {
                    self.connectionClosed(identity: identity)
                } else {
                    self.schedulePing(on: task, identity: identity)
                }
            }
        }
    }

    private func cancelPing() {
        pingWorkItem?.cancel()
        pingWorkItem = nil
        pongTimeoutWorkItem?.cancel()
        pongTimeoutWorkItem = nil
        pingInFlight = false
    }

    private func scheduleReconnect() {
        reconnectWorkItem?.cancel()
        let delay = min(pow(2.0, Double(reconnectAttempt)), 30.0)
        reconnectAttempt += 1
        let workItem = DispatchWorkItem { [weak self] in
            self?.reconnectWorkItem = nil
            self?.connect()
        }
        reconnectWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }
}

extension RelayClient: URLSessionWebSocketDelegate, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        DispatchQueue.main.async { [weak self] in
            self?.connectionOpened(task: webSocketTask)
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        DispatchQueue.main.async { [weak self] in
            guard let self, let identity = self.taskIdentity, self.task === webSocketTask else { return }
            self.connectionClosed(identity: identity)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let webSocketTask = task as? URLSessionWebSocketTask else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, let identity = self.taskIdentity, self.task === webSocketTask else { return }
            self.connectionClosed(identity: identity)
        }
    }
}
