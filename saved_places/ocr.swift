import Foundation
import Vision
import AppKit

// Apple Vision runs locally. Output is JSON so source text never becomes shell code.
var result: [[String: Any]] = []
for filename in CommandLine.arguments.dropFirst() {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.automaticallyDetectsLanguage = true
    let handler = VNImageRequestHandler(url: URL(fileURLWithPath: filename))
    do {
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        result.append(["frame": URL(fileURLWithPath: filename).lastPathComponent, "lines": lines])
    } catch {
        result.append(["frame": URL(fileURLWithPath: filename).lastPathComponent, "error": "OCR failed"])
    }
}
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
