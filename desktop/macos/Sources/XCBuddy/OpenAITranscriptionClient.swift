import Foundation

enum ASRResultType: String {
    case full
    case single
}

struct ASRSessionOptions {
    var hotwords: [String] = []
    var resultType: ASRResultType = .full
    var showUtterances: Bool = false
}

struct ASRSegment {
    let text: String
    let definite: Bool
    let startTime: Int?
    let endTime: Int?
}

protocol ASRClient: AnyObject {
    var onPartial: ((String) -> Void)? { get set }
    var onSegment: ((ASRSegment) -> Void)? { get set }
    var onFinal: ((String) -> Void)? { get set }
    var onError: ((String) -> Void)? { get set }

    func start(options: ASRSessionOptions) -> Bool
    func sendOggOpusChunk(_ data: Data, isLast: Bool)
    func finish()
    func cancel()
}

final class OpenAITranscriptionClient: ASRClient {
    private struct ChatResponse: Decodable {
        struct Choice: Decodable {
            struct Message: Decodable { let content: String? }
            let message: Message
        }

        let choices: [Choice]
    }

    private let config: AppConfig
    private let queue = DispatchQueue(label: "XCBuddy.NVIDIAInferenceTranscriptionClient")
    private var audio = Data()
    private var task: URLSessionDataTask?
    private var active = false
    private var submitted = false
    private var options = ASRSessionOptions()

    var onPartial: ((String) -> Void)?
    var onSegment: ((ASRSegment) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?

    init(config: AppConfig) { self.config = config }

    func start(options: ASRSessionOptions) -> Bool {
        guard chatCompletionsURL != nil else {
            onError?("Invalid NVIDIA inference base URL")
            return false
        }
        guard !config.openAIModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            onError?("Missing audio transcription model")
            return false
        }
        guard !config.openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            onError?("Missing NVIDIA inference API key")
            return false
        }

        queue.sync {
            task?.cancel()
            task = nil
            audio.removeAll(keepingCapacity: true)
            self.options = options
            active = true
            submitted = false
        }
        return true
    }

    func sendOggOpusChunk(_ data: Data, isLast: Bool) {
        queue.async { [weak self] in
            guard let self, self.active, !self.submitted else { return }
            self.audio.append(data)
            if isLast { self.submit() }
        }
    }

    func finish() {
        queue.async { [weak self] in self?.submit() }
    }

    func cancel() {
        queue.async { [weak self] in
            self?.task?.cancel()
            self?.task = nil
            self?.audio.removeAll(keepingCapacity: false)
            self?.active = false
            self?.submitted = false
        }
    }

    private var chatCompletionsURL: URL? {
        let raw = config.openAIBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: raw), components.scheme != nil else { return nil }
        var path = components.path
        while path.hasSuffix("/") { path.removeLast() }
        if path.hasSuffix("/chat/completions") {
            components.path = path
        } else if path.hasSuffix("/v1") {
            components.path = path + "/chat/completions"
        } else {
            components.path = path + "/v1/chat/completions"
        }
        return components.url
    }

    private func submit() {
        guard active, !submitted, let url = chatCompletionsURL else { return }
        submitted = true

        let configuredPrompt = config.openAIPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        var prompt = configuredPrompt.isEmpty
            ? "Transcribe this audio accurately and verbatim. The speaker may mix Mandarin Chinese and English technical terms in the same sentence. Preserve each language, technical names, punctuation, and capitalization. Output transcript only."
            : configuredPrompt
        let hotwords = options.hotwords.filter { !$0.isEmpty }
        if !hotwords.isEmpty {
            prompt += "\nLikely technical terms and names: " + hotwords.joined(separator: ", ")
        }

        let payload: [String: Any] = [
            "model": config.openAIModel,
            "messages": [[
                "role": "user",
                "content": [
                    [
                        "type": "input_audio",
                        "input_audio": [
                            "data": audio.base64EncodedString(),
                            "format": "ogg"
                        ]
                    ],
                    ["type": "text", "text": prompt]
                ]
            ]],
            "temperature": 0,
            "max_tokens": 2048
        ]

        let body: Data
        do {
            body = try JSONSerialization.data(withJSONObject: payload)
        } catch {
            active = false
            notifyError("Could not encode transcription request: \(error.localizedDescription)")
            return
        }

        var request = URLRequest(url: url, timeoutInterval: 90)
        request.httpMethod = "POST"
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(config.openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines))", forHTTPHeaderField: "Authorization")

        task = URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }
            self.queue.async {
                self.active = false
                self.audio.removeAll(keepingCapacity: false)
                if let error = error as NSError?, error.code != NSURLErrorCancelled {
                    self.notifyError("Transcription request failed: \(error.localizedDescription)")
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    self.notifyError("Transcription server returned no HTTP response")
                    return
                }
                guard (200..<300).contains(http.statusCode), let data else {
                    let detail = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                    self.notifyError("Transcription HTTP \(http.statusCode)\(detail.isEmpty ? "" : ": \(detail.prefix(240))")")
                    return
                }
                do {
                    let decoded = try JSONDecoder().decode(ChatResponse.self, from: data)
                    let text = decoded.choices.first?.message.content?
                        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    guard !text.isEmpty else {
                        self.notifyError("Transcription response contained no text")
                        return
                    }
                    DispatchQueue.main.async { self.onFinal?(text) }
                } catch {
                    self.notifyError("Invalid transcription response: \(error.localizedDescription)")
                }
            }
        }
        task?.resume()
    }

    private func notifyError(_ message: String) {
        DispatchQueue.main.async { [weak self] in self?.onError?(message) }
    }
}

func makeASRClient(config: AppConfig) -> any ASRClient {
    OpenAITranscriptionClient(config: config)
}
