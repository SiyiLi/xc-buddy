import CryptoKit
import Foundation

struct FirmwareRelease {
    static let supportedHardware = "stick_s3"
    static let otaAssetName = "xc-buddy-sticks3-ota.bin"

    let version: String
    let otaURL: URL
    let otaSHA256: String
    let otaSize: Int
}

struct DeviceFirmwareInfo {
    var hardware: String?
    var currentVersion: String?
    var latestVersion: String?
    var updateAvailable = false
    var isChecking = false
}

enum FirmwareVersion {
    static func isVersion(_ current: String, olderThan latest: String) -> Bool {
        guard let current = ParsedVersion(current), let latest = ParsedVersion(latest) else {
            return false
        }
        return current < latest
    }

    private struct ParsedVersion: Comparable {
        let numbers: [Int]
        let suffix: String?

        init?(_ text: String) {
            var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.lowercased().hasPrefix("v") {
                trimmed.removeFirst()
            }
            guard !trimmed.isEmpty else { return nil }
            let parts = trimmed.split(separator: "-", maxSplits: 1).map(String.init)
            let numberParts = parts[0].split(separator: ".").map(String.init)
            guard !numberParts.isEmpty else { return nil }
            var parsedNumbers: [Int] = []
            for part in numberParts {
                guard let value = Int(part) else { return nil }
                parsedNumbers.append(value)
            }
            numbers = parsedNumbers
            suffix = parts.count > 1 ? parts[1] : nil
        }

        static func < (lhs: ParsedVersion, rhs: ParsedVersion) -> Bool {
            let count = max(lhs.numbers.count, rhs.numbers.count)
            for index in 0..<count {
                let left = index < lhs.numbers.count ? lhs.numbers[index] : 0
                let right = index < rhs.numbers.count ? rhs.numbers[index] : 0
                if left != right {
                    return left < right
                }
            }

            switch (lhs.suffix, rhs.suffix) {
            case (.some, nil):
                return true
            case (nil, .some):
                return false
            case let (.some(left), .some(right)):
                return left.localizedStandardCompare(right) == .orderedAscending
            case (nil, nil):
                return false
            }
        }
    }
}

final class FirmwareReleaseClient {
    enum FirmwareReleaseError: LocalizedError {
        case invalidResponse
        case noPublishedRelease
        case missingOTAAsset
        case missingDigest
        case checksumMismatch
        case sizeMismatch

        var errorDescription: String? {
            switch self {
            case .invalidResponse:
                return "GitHub returned an invalid firmware release response."
            case .noPublishedRelease:
                return "No published firmware release is available yet."
            case .missingOTAAsset:
                return "The latest release does not contain the StickS3 OTA image."
            case .missingDigest:
                return "The OTA release asset does not include a SHA-256 digest."
            case .checksumMismatch:
                return "The downloaded firmware checksum does not match GitHub."
            case .sizeMismatch:
                return "The downloaded firmware size does not match GitHub."
            }
        }
    }

    private struct GitHubRelease: Decodable {
        struct Asset: Decodable {
            let name: String
            let size: Int
            let digest: String?
            let browserDownloadURL: URL

            enum CodingKeys: String, CodingKey {
                case name
                case size
                case digest
                case browserDownloadURL = "browser_download_url"
            }
        }

        let tagName: String
        let assets: [Asset]

        enum CodingKeys: String, CodingKey {
            case tagName = "tag_name"
            case assets
        }
    }

    private let latestReleaseURL = URL(
        string: "https://api.github.com/repos/SiyiLi/xc-buddy/releases/latest"
    )!

    func fetchLatest(completion: @escaping (Result<FirmwareRelease, Error>) -> Void) {
        var request = URLRequest(
            url: latestReleaseURL,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData
        )
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.setValue("XC-Buddy", forHTTPHeaderField: "User-Agent")

        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let data, let response = response as? HTTPURLResponse else {
                completion(.failure(FirmwareReleaseError.invalidResponse))
                return
            }
            if response.statusCode == 404 {
                completion(.failure(FirmwareReleaseError.noPublishedRelease))
                return
            }
            guard (200..<300).contains(response.statusCode) else {
                completion(.failure(FirmwareReleaseError.invalidResponse))
                return
            }

            do {
                let release = try JSONDecoder().decode(GitHubRelease.self, from: data)
                guard let asset = release.assets.first(where: { $0.name == FirmwareRelease.otaAssetName }) else {
                    completion(.failure(FirmwareReleaseError.missingOTAAsset))
                    return
                }
                guard let digest = asset.digest?.lowercased(), digest.hasPrefix("sha256:") else {
                    completion(.failure(FirmwareReleaseError.missingDigest))
                    return
                }
                let version = release.tagName.lowercased().hasPrefix("v")
                    ? String(release.tagName.dropFirst())
                    : release.tagName
                completion(.success(FirmwareRelease(
                    version: version,
                    otaURL: asset.browserDownloadURL,
                    otaSHA256: String(digest.dropFirst("sha256:".count)),
                    otaSize: asset.size
                )))
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }

    func downloadOTA(from release: FirmwareRelease,
                     completion: @escaping (Result<Data, Error>) -> Void) {
        var request = URLRequest(
            url: release.otaURL,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData
        )
        request.setValue("XC-Buddy", forHTTPHeaderField: "User-Agent")

        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let data,
                  let response = response as? HTTPURLResponse,
                  (200..<300).contains(response.statusCode) else {
                completion(.failure(FirmwareReleaseError.invalidResponse))
                return
            }
            guard data.count == release.otaSize else {
                completion(.failure(FirmwareReleaseError.sizeMismatch))
                return
            }
            let digest = SHA256.hash(data: data)
                .map { String(format: "%02x", $0) }
                .joined()
            guard digest == release.otaSHA256 else {
                completion(.failure(FirmwareReleaseError.checksumMismatch))
                return
            }
            completion(.success(data))
        }.resume()
    }
}
