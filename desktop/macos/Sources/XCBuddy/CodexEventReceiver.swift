import Foundation
import Network

enum CodexLifecycleEvent {
    case working
    case approvalNeeded
    case done
    case error(String)
}

final class CodexEventReceiver {
    private let port: UInt16
    private let token: String
    private let queue = DispatchQueue(label: "XCBuddy.CodexEventReceiver")
    private var listener: NWListener?

    var onEvent: ((CodexLifecycleEvent) -> Void)?
    var onStatusChange: ((String) -> Void)?

    init(port: Int, token: String) {
        self.port = UInt16(port)
        self.token = token
    }

    func start() {
        do {
            let parameters = NWParameters.tcp
            parameters.requiredLocalEndpoint = .hostPort(
                host: NWEndpoint.Host("127.0.0.1"),
                port: NWEndpoint.Port(rawValue: port)!
            )
            let listener = try NWListener(using: parameters)
            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    self?.notifyStatus("Listening on 127.0.0.1:\(self?.port ?? 0)")
                case .failed(let error):
                    self?.notifyStatus("Unavailable: \(error.localizedDescription)")
                    self?.listener?.cancel()
                    self?.listener = nil
                case .cancelled:
                    self?.notifyStatus("Stopped")
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in
                self?.receiveRequest(on: connection)
            }
            self.listener = listener
            listener.start(queue: queue)
        } catch {
            notifyStatus("Unavailable: \(error.localizedDescription)")
        }
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    private func receiveRequest(on connection: NWConnection) {
        connection.start(queue: queue)
        receiveMore(on: connection, buffer: Data())
    }

    private func receiveMore(on connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, complete, error in
            guard let self else { return }
            var request = buffer
            if let data { request.append(data) }
            if request.count > 1_048_576 {
                self.respond(413, body: "request too large", on: connection)
                return
            }
            if let parsed = self.parseIfComplete(request) {
                self.handle(parsed, on: connection)
            } else if error != nil || complete {
                self.respond(400, body: "invalid request", on: connection)
            } else {
                self.receiveMore(on: connection, buffer: request)
            }
        }
    }

    private func parseIfComplete(_ data: Data) -> (headers: String, body: Data)? {
        let marker = Data("\r\n\r\n".utf8)
        guard let range = data.range(of: marker),
              let headers = String(data: data[..<range.lowerBound], encoding: .utf8) else { return nil }
        let contentLength = headers.split(separator: "\r\n")
            .first { $0.lowercased().hasPrefix("content-length:") }
            .flatMap { Int($0.split(separator: ":", maxSplits: 1)[1].trimmingCharacters(in: .whitespaces)) } ?? 0
        let bodyStart = range.upperBound
        guard data.count >= bodyStart + contentLength else { return nil }
        return (headers, data.subdata(in: bodyStart..<(bodyStart + contentLength)))
    }

    private func handle(_ request: (headers: String, body: Data), on connection: NWConnection) {
        guard request.headers.hasPrefix("POST ") else {
            respond(405, body: "POST required", on: connection)
            return
        }
        if !token.isEmpty {
            let authorized = request.headers.split(separator: "\r\n").contains {
                $0.lowercased() == "authorization: bearer \(token)".lowercased()
            }
            guard authorized else {
                respond(401, body: "unauthorized", on: connection)
                return
            }
        }
        guard let object = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any] else {
            respond(400, body: "invalid json", on: connection)
            return
        }
        let rawType = (object["type"] as? String) ?? (object["event"] as? String) ?? ""
        let type = rawType.lowercased()
        let event: CodexLifecycleEvent
        if type.contains("approval") {
            event = .approvalNeeded
        } else if type.contains("error") || type.contains("fail") {
            let message = (object["message"] as? String) ?? rawType
            event = .error(message.isEmpty ? "Codex error" : message)
        } else if type.contains("complete") || type.contains("done") {
            event = .done
        } else {
            event = .working
        }
        DispatchQueue.main.async { [weak self] in self?.onEvent?(event) }
        respond(204, body: "", on: connection)
    }

    private func respond(_ status: Int, body: String, on connection: NWConnection) {
        let reason = status == 204 ? "No Content" : status == 401 ? "Unauthorized" : status == 405 ? "Method Not Allowed" : status == 413 ? "Payload Too Large" : "Bad Request"
        let bodyData = Data(body.utf8)
        let response = "HTTP/1.1 \(status) \(reason)\r\nContent-Length: \(bodyData.count)\r\nConnection: close\r\n\r\n"
        var data = Data(response.utf8)
        data.append(bodyData)
        connection.send(content: data, completion: .contentProcessed { _ in connection.cancel() })
    }

    private func notifyStatus(_ text: String) {
        DispatchQueue.main.async { [weak self] in self?.onStatusChange?(text) }
    }
}
