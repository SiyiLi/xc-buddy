import Foundation

enum RelayMode: String, CaseIterable {
    case disabled
    case sender
    case receiver

    var displayName: String {
        switch self {
        case .disabled:
            return "Disabled"
        case .sender:
            return "Sender"
        case .receiver:
            return "Receiver"
        }
    }
}
