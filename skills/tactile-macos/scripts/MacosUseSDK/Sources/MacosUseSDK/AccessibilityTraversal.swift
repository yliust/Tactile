// The Swift Programming Language
// https://docs.swift.org/swift-book

import AppKit // For NSWorkspace, NSRunningApplication, NSApplication
import Foundation // For basic types, JSONEncoder, Date
import ApplicationServices // For Accessibility API (AXUIElement, etc.)
import Darwin

// --- Error Enum ---
public enum MacosUseSDKError: Error, LocalizedError {
    case accessibilityDenied
    case appNotFound(pid: Int32)
    case jsonEncodingFailed(Error)
    case internalError(String) // For unexpected issues

    public var errorDescription: String? {
        switch self {
        case .accessibilityDenied:
            return "Accessibility access is denied. Please grant permissions in System Settings > Privacy & Security > Accessibility."
        case .appNotFound(let pid):
            return "No running application found with PID \(pid)."
        case .jsonEncodingFailed(let underlyingError):
            return "Failed to encode response to JSON: \(underlyingError.localizedDescription)"
        case .internalError(let message):
            return "Internal SDK error: \(message)"
        }
    }
}


// --- Public Data Structures for API Response ---

public struct ElementData: Codable, Hashable, Sendable {
    public var role: String
    public var text: String?
    public var x: Double?
    public var y: Double?
    public var width: Double?
    public var height: Double?
    public var axPath: String? = nil
    public var axActions: [String]? = nil
    public var isSettable: Bool? = nil
    public var isFocused: Bool? = nil
    public var isSelected: Bool? = nil

    // Implement Hashable for use in Set
    public func hash(into hasher: inout Hasher) {
        hasher.combine(role)
        hasher.combine(text)
        hasher.combine(x)
        hasher.combine(y)
        hasher.combine(width)
        hasher.combine(height)
    }
    public static func == (lhs: ElementData, rhs: ElementData) -> Bool {
        lhs.role == rhs.role &&
            lhs.text == rhs.text &&
            lhs.x == rhs.x &&
            lhs.y == rhs.y &&
            lhs.width == rhs.width &&
            lhs.height == rhs.height
    }
}

public struct Statistics: Codable, Sendable {
    public var count: Int = 0
    public var excluded_count: Int = 0
    public var excluded_non_interactable: Int = 0
    public var excluded_no_text: Int = 0
    public var with_text_count: Int = 0
    public var without_text_count: Int = 0
    public var visible_elements_count: Int = 0
    public var truncated: Bool = false
    public var role_counts: [String: Int] = [:]
}

public struct ResponseData: Codable, Sendable {
    public let app_name: String
    public var elements: [ElementData]
    public var stats: Statistics
    public let processing_time_seconds: String
}


// --- Main Public Function ---

/// Traverses the accessibility tree of an application specified by its PID.
///
/// - Parameter pid: The Process ID (PID) of the target application.
/// - Parameter onlyVisibleElements: If true, only collects elements with valid position and size. Defaults to false.
/// - Parameter activateApp: If true, activates the target app before traversal. Defaults to true.
/// - Returns: A `ResponseData` struct containing the collected elements, statistics, and timing information.
/// - Throws: `MacosUseSDKError` if accessibility is denied, the app is not found, or an internal error occurs.
public func traverseAccessibilityTree(pid: Int32, onlyVisibleElements: Bool = false, activateApp: Bool = true) throws -> ResponseData {
    let operation = AccessibilityTraversalOperation(pid: pid, onlyVisibleElements: onlyVisibleElements, activateApp: activateApp)
    return try operation.executeTraversal()
}


// --- Internal Implementation Detail ---

// Class to encapsulate the state and logic of a single traversal operation
fileprivate class AccessibilityTraversalOperation {
    let pid: Int32
    let onlyVisibleElements: Bool
    let activateApp: Bool
    var visitedElements: Set<AXUIElement> = []
    var collectedElements: Set<ElementData> = []
    var statistics: Statistics = Statistics()
    var stepStartTime: Date = Date()
    let maxDepth = 100
    let maxElements = 2000
    let maxTraversalSeconds: Double = 5.0
    var traversalStartTime: Date = Date()

    // Define roles considered non-interactable by default
    let nonInteractableRoles: Set<String> = [
        "AXGroup", "AXStaticText", "AXUnknown", "AXSeparator",
        "AXHeading", "AXLayoutArea", "AXHelpTag", "AXGrowArea",
        "AXOutline", "AXScrollArea", "AXSplitGroup", "AXSplitter",
        "AXToolbar", "AXDisclosureTriangle",
    ]

    init(pid: Int32, onlyVisibleElements: Bool, activateApp: Bool) {
        self.pid = pid
        self.onlyVisibleElements = onlyVisibleElements
        self.activateApp = activateApp
    }

    // --- Main Execution Method ---
    func executeTraversal() throws -> ResponseData {
        let overallStartTime = Date()
        fputs("info: starting traversal for pid: \(pid) (Visible Only: \(onlyVisibleElements), Activate App: \(activateApp))\n", stderr)
        stepStartTime = Date() // Initialize step timer

        // 1. Validate PID exists (fast fail before potentially blocking AX check)
        guard let runningApp = NSRunningApplication(processIdentifier: pid) else {
            fputs("error: no running application found with pid \(pid).\n", stderr)
            throw MacosUseSDKError.appNotFound(pid: pid)
        }

        // 2. Accessibility Check
        fputs("info: checking accessibility permissions...\n", stderr)
        let shouldPromptForAccessibility = ProcessInfo.processInfo.environment["MACOS_USE_SDK_PROMPT_FOR_ACCESSIBILITY"] == "1"
        let checkOptions = ["AXTrustedCheckOptionPrompt": shouldPromptForAccessibility ? kCFBooleanTrue : kCFBooleanFalse] as CFDictionary
        let isTrusted = AXIsProcessTrustedWithOptions(checkOptions)

        if !isTrusted {
            fputs("❌ error: accessibility access is denied.\n", stderr)
            fputs("       please grant permissions in system settings > privacy & security > accessibility.\n", stderr)
            fputs("       executable requesting access: \(Bundle.main.executableURL?.path ?? CommandLine.arguments.first ?? "unknown")\n", stderr)
            fputs("       parent pid: \(getppid())\n", stderr)
            throw MacosUseSDKError.accessibilityDenied
        }
        logStepCompletion("checking accessibility permissions (granted)")
        let targetAppName = runningApp.localizedName ?? "App (PID: \(pid))"
        let appElement = AXUIElementCreateApplication(pid)
        // logStepCompletion("finding application '\(targetAppName)'") // Logging step completion implicitly here

        // 3. Activate App if needed
        var didActivate = false
        if activateApp && runningApp.activationPolicy == NSApplication.ActivationPolicy.regular {
            if !runningApp.isActive {
                // fputs("info: activating application '\(targetAppName)'...\n", stderr) // Optional start log
                runningApp.activate()
                // Consider adding a small delay or a check loop if activation timing is critical
                // Thread.sleep(forTimeInterval: 0.2)
                didActivate = true
            }
        }
        if didActivate {
            logStepCompletion("activating application '\(targetAppName)'")
        }

        // 4. Start Traversal
        traversalStartTime = Date()
        walkElementTreeBFS(rootElement: appElement)
        if statistics.truncated {
            fputs("warning: traversal truncated at \(maxElements) elements cap\n", stderr)
        }
        logStepCompletion("traversing accessibility tree (\(collectedElements.count) elements collected\(statistics.truncated ? ", TRUNCATED" : ""))")

        // 5. Process Results
        // fputs("info: sorting elements...\n", stderr) // Optional start log
        let sortedElements = collectedElements.sorted {
            let y0 = $0.y ?? Double.greatestFiniteMagnitude
            let y1 = $1.y ?? Double.greatestFiniteMagnitude
            if y0 != y1 { return y0 < y1 }
            let x0 = $0.x ?? Double.greatestFiniteMagnitude
            let x1 = $1.x ?? Double.greatestFiniteMagnitude
            return x0 < x1
        }
        // logStepCompletion("sorting \(sortedElements.count) elements") // Log implicitly

        // Set the final count statistic
        statistics.count = sortedElements.count

        // --- Calculate Total Time ---
        let overallEndTime = Date()
        let totalProcessingTime = overallEndTime.timeIntervalSince(overallStartTime)
        let formattedTime = String(format: "%.2f", totalProcessingTime)
        fputs("info: total execution time: \(formattedTime) seconds\n", stderr)

        // 6. Prepare Response
        let response = ResponseData(
            app_name: targetAppName,
            elements: sortedElements,
            stats: statistics,
            processing_time_seconds: formattedTime
        )

        return response
        // JSON encoding will be handled by the caller of the library function if needed
    }


    // --- Helper Functions (now methods of the class) ---

    // Safely copy an attribute value
    func copyAttributeValue(element: AXUIElement, attribute: String) -> CFTypeRef? {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
        if result == .success {
            return value
        } else if result != .attributeUnsupported && result != .noValue {
            // fputs("warning: could not get attribute '\(attribute)' for element: error \(result.rawValue)\n", stderr)
        }
        return nil
    }

    // Extract string value
    func getStringValue(_ value: CFTypeRef?) -> String? {
        guard let value = value else { return nil }
        let typeID = CFGetTypeID(value)
        if typeID == CFStringGetTypeID() {
            let cfString = value as! CFString
            return cfString as String
        } else if typeID == AXValueGetTypeID() {
            // AXValue conversion is complex, return nil for generic string conversion
            return nil
        }
        return nil
    }

    // Extract CGPoint
    func getCGPointValue(_ value: CFTypeRef?) -> CGPoint? {
        guard let value = value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        let axValue = value as! AXValue
        var pointValue = CGPoint.zero
        if AXValueGetValue(axValue, .cgPoint, &pointValue) {
            return pointValue
        }
        // fputs("warning: failed to extract cgpoint from axvalue.\n", stderr)
        return nil
    }

    // Extract CGSize
    func getCGSizeValue(_ value: CFTypeRef?) -> CGSize? {
        guard let value = value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        let axValue = value as! AXValue
        var sizeValue = CGSize.zero
        if AXValueGetValue(axValue, .cgSize, &sizeValue) {
            return sizeValue
        }
        // fputs("warning: failed to extract cgsize from axvalue.\n", stderr)
        return nil
    }

    func getBoolValue(_ value: CFTypeRef?) -> Bool? {
        guard let value = value, CFGetTypeID(value) == CFBooleanGetTypeID() else { return nil }
        return CFBooleanGetValue((value as! CFBoolean))
    }

    func copyActionNames(element: AXUIElement) -> [String] {
        var actions: CFArray?
        guard AXUIElementCopyActionNames(element, &actions) == .success,
              let values = actions as? [String] else {
            return []
        }
        return values
    }

    func isValueSettable(element: AXUIElement) -> Bool {
        var settable = DarwinBoolean(false)
        let result = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
        return result == .success && settable.boolValue
    }

    // Extract attributes, text, and geometry
    func extractElementAttributes(element: AXUIElement) -> (role: String, roleDesc: String?, text: String?, allTextParts: [String], position: CGPoint?, size: CGSize?) {
        var role = "AXUnknown"
        var roleDesc: String? = nil
        var textParts: [String] = []
        var position: CGPoint? = nil
        var size: CGSize? = nil

        if let roleValue = copyAttributeValue(element: element, attribute: kAXRoleAttribute as String) {
            role = getStringValue(roleValue) ?? "AXUnknown"
        }
        if let roleDescValue = copyAttributeValue(element: element, attribute: kAXRoleDescriptionAttribute as String) {
            roleDesc = getStringValue(roleDescValue)
        }

        let textAttributes = [
            kAXValueAttribute as String, kAXTitleAttribute as String, kAXDescriptionAttribute as String,
            "AXLabel", "AXHelp",
        ]
        for attr in textAttributes {
            if let attrValue = copyAttributeValue(element: element, attribute: attr),
               let text = getStringValue(attrValue),
               !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                textParts.append(text)
            }
        }
        let combinedText = textParts.isEmpty ? nil : textParts.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)

        if let posValue = copyAttributeValue(element: element, attribute: kAXPositionAttribute as String) {
            position = getCGPointValue(posValue)
            // if position == nil { fputs("debug: failed to get position for element (role: \(role))\n", stderr) }
        } else {
            // fputs("debug: position attribute ('\(kAXPositionAttribute)') not found or unsupported for element (role: \(role))\n", stderr)
        }

        if let sizeValue = copyAttributeValue(element: element, attribute: kAXSizeAttribute as String) {
            size = getCGSizeValue(sizeValue)
             // if size == nil { fputs("debug: failed to get size for element (role: \(role))\n", stderr) }
        } else {
             // fputs("debug: size attribute ('\(kAXSizeAttribute)') not found or unsupported for element (role: \(role))\n", stderr)
        }

        return (role, roleDesc, combinedText, textParts, position, size)
    }

    // BFS traversal function — processes all siblings at each depth before going deeper.
    // This ensures dialog buttons (siblings of file lists) are discovered before
    // deep-diving into individual file list rows.
    func walkElementTreeBFS(rootElement: AXUIElement) {
        // Queue: array of (element, depth) with advancing read index for O(1) dequeue
        var queue: [(element: AXUIElement, depth: Int, path: String)] = [(rootElement, 0, "app")]
        var readIndex = 0
        let maxChildrenPerElement = 200

        while readIndex < queue.count {
            // Check caps
            if collectedElements.count >= maxElements || visitedElements.count >= maxElements
                || Date().timeIntervalSince(traversalStartTime) >= maxTraversalSeconds {
                statistics.truncated = true
                return
            }

            let (element, depth, path) = queue[readIndex]
            readIndex += 1

            // Skip visited or too deep
            if visitedElements.contains(element) || depth > maxDepth { continue }
            visitedElements.insert(element)

            // --- Process current element ---
            let (role, roleDesc, combinedText, _, position, size) = extractElementAttributes(element: element)
            let hasText = combinedText != nil && !combinedText!.isEmpty
            let isNonInteractable = nonInteractableRoles.contains(role)
            let roleWithoutAX = role.starts(with: "AX") ? String(role.dropFirst(2)) : role

            statistics.role_counts[role, default: 0] += 1

            // Geometry and visibility
            var finalX: Double? = nil
            var finalY: Double? = nil
            var finalWidth: Double? = nil
            var finalHeight: Double? = nil
            if let p = position, let s = size, s.width > 0 || s.height > 0 {
                finalX = Double(p.x)
                finalY = Double(p.y)
                finalWidth = s.width > 0 ? Double(s.width) : nil
                finalHeight = s.height > 0 ? Double(s.height) : nil
            }
            let isGeometricallyVisible = finalX != nil && finalY != nil && finalWidth != nil && finalHeight != nil

            if isGeometricallyVisible {
                statistics.visible_elements_count += 1
            }

            // Filtering
            var displayRole = role
            if let desc = roleDesc, !desc.isEmpty, !desc.elementsEqual(roleWithoutAX) {
                displayRole = "\(role) (\(desc))"
            }

            let passesOriginalFilter = !isNonInteractable || hasText
            let shouldCollectElement = onlyVisibleElements ? isGeometricallyVisible : passesOriginalFilter

            if shouldCollectElement {
                let actions = copyActionNames(element: element)
                let elementData = ElementData(
                    role: displayRole, text: combinedText,
                    x: finalX, y: finalY, width: finalWidth, height: finalHeight,
                    axPath: path,
                    axActions: actions.isEmpty ? nil : actions,
                    isSettable: isValueSettable(element: element),
                    isFocused: getBoolValue(copyAttributeValue(element: element, attribute: kAXFocusedAttribute as String)),
                    isSelected: getBoolValue(copyAttributeValue(element: element, attribute: kAXSelectedAttribute as String))
                )
                if collectedElements.insert(elementData).inserted {
                    if hasText { statistics.with_text_count += 1 }
                    else { statistics.without_text_count += 1 }
                }
            } else {
                statistics.excluded_count += 1
                if isNonInteractable { statistics.excluded_non_interactable += 1 }
                if !hasText { statistics.excluded_no_text += 1 }
            }

            // --- Enqueue children (BFS: windows first, then main window, then regular children) ---
            // a) Windows
            if let windowsValue = copyAttributeValue(element: element, attribute: kAXWindowsAttribute as String) {
                if let windowsArray = windowsValue as? [AXUIElement] {
                    for (index, windowElement) in windowsArray.enumerated() where !visitedElements.contains(windowElement) {
                        queue.append((windowElement, depth + 1, "\(path).windows[\(index)]"))
                    }
                }
            }

            // b) Main Window
            if let mainWindowValue = copyAttributeValue(element: element, attribute: kAXMainWindowAttribute as String) {
                if CFGetTypeID(mainWindowValue) == AXUIElementGetTypeID() {
                    let mainWindowElement = mainWindowValue as! AXUIElement
                    if !visitedElements.contains(mainWindowElement) {
                        queue.append((mainWindowElement, depth + 1, "\(path).mainWindow"))
                    }
                }
            }

            // c) Regular Children — ranged retrieval to avoid blocking on huge containers
            var childCount: CFIndex = 0
            let countResult = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &childCount)
            if countResult == .success && childCount > 0 {
                let fetchCount = min(CFIndex(maxChildrenPerElement), childCount)
                var childrenRef: CFArray?
                let fetchResult = AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, fetchCount, &childrenRef)
                if fetchResult == .success, let cfArray = childrenRef {
                    let childrenArray = cfArray as [AnyObject]
                    for (index, child) in childrenArray.enumerated() {
                        let childElement = child as! AXUIElement
                        if !visitedElements.contains(childElement) {
                            queue.append((childElement, depth + 1, "\(path).children[\(index)]"))
                        }
                    }
                }
            }
        }
    }


    // Helper function logs duration of the step just completed
    func logStepCompletion(_ stepDescription: String) {
        let endTime = Date()
        let duration = endTime.timeIntervalSince(stepStartTime)
        let durationStr = String(format: "%.3f", duration)
        fputs("info: [\(durationStr)s] finished '\(stepDescription)'\n", stderr)
        stepStartTime = endTime // Reset start time for the next step
    }
} // End of AccessibilityTraversalOperation class
