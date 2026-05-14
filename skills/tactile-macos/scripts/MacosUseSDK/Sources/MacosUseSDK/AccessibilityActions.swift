import Foundation
import CoreGraphics
import AppKit
import ApplicationServices

// AX-driven write/press paths. These bypass the input event tap entirely and
// talk to the target app via the Accessibility API instead. Two cases where
// this is the only thing that works:
//   1) Catalyst right-pane controls that swallow synthetic mouse events.
//   2) Sandboxed/secure-input contexts where the HID tap is filtered.
// Both functions hit-test by point against the application's AXUIElement tree
// and operate on the deepest element under the coordinate.

fileprivate func axElement(at point: CGPoint, pid: Int32) throws -> AXUIElement {
    let appElement = AXUIElementCreateApplication(pid)
    var hit: AXUIElement?
    let err = AXUIElementCopyElementAtPosition(appElement, Float(point.x), Float(point.y), &hit)
    guard err == .success, let element = hit else {
        throw MacosUseSDKError.inputSimulationFailed(
            "no AX element at (\(point.x), \(point.y)) for pid \(pid) — AXError \(err.rawValue)"
        )
    }
    return element
}

fileprivate enum AXPathSegment {
    case windows(Int)
    case mainWindow
    case children(Int)
}

fileprivate func indexedSegment(_ value: String, prefix: String) -> Int? {
    let marker = "\(prefix)["
    guard value.hasPrefix(marker), value.hasSuffix("]") else { return nil }
    let start = value.index(value.startIndex, offsetBy: marker.count)
    let end = value.index(before: value.endIndex)
    return Int(value[start..<end])
}

fileprivate func parseAXPath(_ path: String) throws -> [AXPathSegment] {
    let parts = path.split(separator: ".").map(String.init)
    guard parts.first == "app" else {
        throw MacosUseSDKError.inputInvalidArgument("AX path must start with 'app': \(path)")
    }

    return try parts.dropFirst().map { part in
        if part == "mainWindow" {
            return .mainWindow
        }
        if let index = indexedSegment(part, prefix: "windows") {
            return .windows(index)
        }
        if let index = indexedSegment(part, prefix: "children") {
            return .children(index)
        }
        throw MacosUseSDKError.inputInvalidArgument("unsupported AX path segment '\(part)' in \(path)")
    }
}

fileprivate func childElement(of element: AXUIElement, at index: Int, path: String) throws -> AXUIElement {
    var childCount: CFIndex = 0
    let countErr = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &childCount)
    guard countErr == .success, index >= 0, index < childCount else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AX path \(path) child index \(index) is unavailable — AXError \(countErr.rawValue), childCount \(childCount)"
        )
    }

    var childrenRef: CFArray?
    let fetchErr = AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, CFIndex(index), 1, &childrenRef)
    guard fetchErr == .success,
          let children = childrenRef as? [AXUIElement],
          let child = children.first else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AX path \(path) failed to fetch child index \(index) — AXError \(fetchErr.rawValue)"
        )
    }
    return child
}

fileprivate func windowElement(of element: AXUIElement, at index: Int, path: String) throws -> AXUIElement {
    var windowsValue: AnyObject?
    let err = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &windowsValue)
    guard err == .success,
          let windows = windowsValue as? [AXUIElement],
          index >= 0,
          index < windows.count else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AX path \(path) window index \(index) is unavailable — AXError \(err.rawValue)"
        )
    }
    return windows[index]
}

fileprivate func mainWindowElement(of element: AXUIElement, path: String) throws -> AXUIElement {
    var mainWindowValue: AnyObject?
    let err = AXUIElementCopyAttributeValue(element, kAXMainWindowAttribute as CFString, &mainWindowValue)
    guard err == .success,
          let mainWindowRef = mainWindowValue,
          CFGetTypeID(mainWindowRef) == AXUIElementGetTypeID() else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AX path \(path) mainWindow is unavailable — AXError \(err.rawValue)"
        )
    }
    return mainWindowRef as! AXUIElement
}

fileprivate func axElement(pid: Int32, atPath path: String) throws -> AXUIElement {
    var current = AXUIElementCreateApplication(pid)
    for segment in try parseAXPath(path) {
        switch segment {
        case .windows(let index):
            current = try windowElement(of: current, at: index, path: path)
        case .mainWindow:
            current = try mainWindowElement(of: current, path: path)
        case .children(let index):
            current = try childElement(of: current, at: index, path: path)
        }
    }
    return current
}

// Reads the screen-space frame (origin top-left) of an AX element.
fileprivate func axFrame(of element: AXUIElement) -> CGRect? {
    var posVal: AnyObject?
    var sizeVal: AnyObject?
    let pErr = AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posVal)
    let sErr = AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeVal)
    guard pErr == .success, sErr == .success,
          let p = posVal, let s = sizeVal,
          CFGetTypeID(p) == AXValueGetTypeID(),
          CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var origin = CGPoint.zero
    var size = CGSize.zero
    AXValueGetValue(p as! AXValue, .cgPoint, &origin)
    AXValueGetValue(s as! AXValue, .cgSize, &size)
    return CGRect(origin: origin, size: size)
}

fileprivate func axCenter(of element: AXUIElement) -> CGPoint? {
    guard let frame = axFrame(of: element), frame.width > 0, frame.height > 0 else {
        return nil
    }
    return CGPoint(x: frame.midX, y: frame.midY)
}

fileprivate func prepareVirtualCursorForAXPress(_ element: AXUIElement, fallback point: CGPoint? = nil) {
    if let center = axCenter(of: element) {
        prepareVirtualCursorAXPress(at: center)
    } else if let point {
        prepareVirtualCursorAXPress(at: point)
    }
}

// Walks the application's AX tree breadth-first looking for the smallest
// element whose frame contains `point` and whose role is in `preferredRoles`
// (when provided). Falls back to the smallest containing element of any role.
//
// This exists because `AXUIElementCopyElementAtPosition` does not reliably
// penetrate into table rows in Catalyst apps; the rows are reachable by
// walking the tree but not by hit-test.
fileprivate func findAXElement(in app: AXUIElement, at point: CGPoint, preferredRoles: Set<String>, maxNodes: Int = 4000) -> AXUIElement? {
    var bestPreferred: (element: AXUIElement, area: CGFloat)? = nil
    var bestAny: (element: AXUIElement, area: CGFloat)? = nil
    var queue: [AXUIElement] = [app]
    var visited = 0
    while let current = queue.first, visited < maxNodes {
        queue.removeFirst()
        visited += 1
        if let frame = axFrame(of: current), frame.contains(point) {
            let area = frame.width * frame.height
            if let role = axRole(of: current), preferredRoles.contains(role) {
                if bestPreferred == nil || area < bestPreferred!.area {
                    bestPreferred = (current, area)
                }
            }
            if bestAny == nil || area < bestAny!.area {
                bestAny = (current, area)
            }
        }
        // Enqueue children
        var children: AnyObject?
        let cErr = AXUIElementCopyAttributeValue(current, kAXChildrenAttribute as CFString, &children)
        if cErr == .success, let arr = children as? [AXUIElement] {
            queue.append(contentsOf: arr)
        }
    }
    return bestPreferred?.element ?? bestAny?.element
}

// Returns the role of an AX element, or nil if unavailable.
fileprivate func axRole(of element: AXUIElement) -> String? {
    var role: AnyObject?
    let err = AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
    guard err == .success else { return nil }
    return role as? String
}

fileprivate func axSupportedActions(of element: AXUIElement) -> [String] {
    var actionsRef: CFArray?
    let err = AXUIElementCopyActionNames(element, &actionsRef)
    guard err == .success, let actions = actionsRef as? [String] else {
        return []
    }
    return actions
}

fileprivate func axSetFocused(_ element: AXUIElement) -> AXError {
    AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
}

fileprivate func axSetSelected(_ element: AXUIElement, selected: Bool) -> AXError {
    let value: CFBoolean = selected ? kCFBooleanTrue : kCFBooleanFalse
    return AXUIElementSetAttributeValue(element, kAXSelectedAttribute as CFString, value)
}

fileprivate func axPerformPress(_ element: AXUIElement) -> AXError {
    AXUIElementPerformAction(element, kAXPressAction as CFString)
}

fileprivate func isTextInputRole(_ role: String) -> Bool {
    ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"].contains(role)
}

fileprivate func isSelectionRole(_ role: String) -> Bool {
    ["AXRow", "AXOutlineRow", "AXListItem", "AXCell"].contains(role)
}

// Walks up the AX parent chain (depth-capped) and returns the first ancestor
// whose role matches one of `targetRoles`. If none match, returns the original
// element so callers can fall back to the deepest hit.
//
// Catalyst hit-tests typically return an AXCell or AXStaticText inside a row,
// but the selectable element is the parent AXRow. This walks up to find it.
fileprivate func axAncestor(of element: AXUIElement, matching targetRoles: Set<String>, maxDepth: Int = 12) -> AXUIElement {
    if let r = axRole(of: element), targetRoles.contains(r) { return element }
    var current = element
    for _ in 0..<maxDepth {
        var parent: AnyObject?
        let err = AXUIElementCopyAttributeValue(current, kAXParentAttribute as CFString, &parent)
        guard err == .success, let parentRef = parent, CFGetTypeID(parentRef) == AXUIElementGetTypeID() else {
            break
        }
        let parentEl = parentRef as! AXUIElement
        if let r = axRole(of: parentEl), targetRoles.contains(r) { return parentEl }
        current = parentEl
    }
    return element
}

/// Sets `kAXValueAttribute` on the AX element under `point` for the given pid.
/// Useful for filling text fields without simulating key events — works in
/// Catalyst/secure-input contexts where typing is filtered.
/// - Parameters:
///   - pid: Target application's process id.
///   - point: Top-left CGPoint of the element to target. Use coordinates from
///     a recent traversal.
///   - value: New string value to write.
/// - Throws: `MacosUseSDKError` if hit-test fails or the AX set call rejects.
public func setAccessibilityValue(pid: Int32, at point: CGPoint, value: String) throws {
    fputs("log: AX set value at (\(point.x), \(point.y)) for pid \(pid): \"\(value)\"\n", stderr)
    // Tree-walk finder: hit-test does not penetrate into Catalyst app controls.
    let app = AXUIElementCreateApplication(pid)
    let preferredRoles: Set<String> = ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"]
    guard let element = findAXElement(in: app, at: point, preferredRoles: preferredRoles) else {
        throw MacosUseSDKError.inputSimulationFailed(
            "no value-bearing AX element found at (\(point.x), \(point.y)) for pid \(pid)"
        )
    }
    let targetRole = axRole(of: element) ?? "<unknown>"
    fputs("log: AX set value target role=\(targetRole)\n", stderr)
    let err = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementSetAttributeValue(kAXValueAttribute) on \(targetRole) failed at (\(point.x), \(point.y)) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX set value complete.\n", stderr)
}

/// Sets `kAXValueAttribute` on the AX element identified by a traversal path.
/// This avoids coordinate hit-testing entirely; use a path returned as
/// `ElementData.axPath` from a recent traversal of the same app state.
public func setAccessibilityValue(pid: Int32, atPath path: String, value: String) throws {
    fputs("log: AX set value at path \(path) for pid \(pid): \"\(value)\"\n", stderr)
    let element = try axElement(pid: pid, atPath: path)
    let targetRole = axRole(of: element) ?? "<unknown>"
    let err = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementSetAttributeValue(kAXValueAttribute) on \(targetRole) failed for path \(path) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX set value by path complete.\n", stderr)
}

/// Performs `kAXPressAction` on the AX element under `point` for the given
/// pid. Replaces a synthetic click for buttons, menu items, and other
/// pressable controls. Often the only thing that works for Catalyst
/// right-pane controls.
/// - Parameters:
///   - pid: Target application's process id.
///   - point: Top-left CGPoint of the element to press.
/// - Throws: `MacosUseSDKError` if hit-test fails or the action is unsupported.
public func pressAccessibilityElement(pid: Int32, at point: CGPoint) throws {
    fputs("log: AX press at (\(point.x), \(point.y)) for pid \(pid)\n", stderr)
    // Tree-walk finder: hit-test does not penetrate into Catalyst app controls.
    let app = AXUIElementCreateApplication(pid)
    let preferredRoles: Set<String> = [
        "AXButton", "AXMenuItem", "AXRadioButton", "AXCheckBox",
        "AXMenuButton", "AXPopUpButton"
    ]
    guard let element = findAXElement(in: app, at: point, preferredRoles: preferredRoles) else {
        throw MacosUseSDKError.inputSimulationFailed(
            "no pressable AX element found at (\(point.x), \(point.y)) for pid \(pid)"
        )
    }
    let targetRole = axRole(of: element) ?? "<unknown>"
    fputs("log: AX press target role=\(targetRole)\n", stderr)
    prepareVirtualCursorForAXPress(element, fallback: point)
    let err = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementPerformAction(kAXPressAction) on \(targetRole) failed at (\(point.x), \(point.y)) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX press complete.\n", stderr)
}

/// Performs `kAXPressAction` on the AX element identified by a traversal path.
/// This is the direct element-action equivalent of clicking a button/menu item.
public func pressAccessibilityElement(pid: Int32, atPath path: String) throws {
    fputs("log: AX press at path \(path) for pid \(pid)\n", stderr)
    let element = try axElement(pid: pid, atPath: path)
    let targetRole = axRole(of: element) ?? "<unknown>"
    prepareVirtualCursorForAXPress(element)
    let err = axPerformPress(element)
    guard err == .success else {
        let actions = axSupportedActions(of: element).joined(separator: ",")
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementPerformAction(kAXPressAction) on \(targetRole) failed for path \(path) — AXError \(err.rawValue), actions=[\(actions)]"
        )
    }
    fputs("log: AX press by path complete.\n", stderr)
}

/// Focuses the AX element identified by a traversal path.
public func focusAccessibilityElement(pid: Int32, atPath path: String) throws {
    fputs("log: AX focus at path \(path) for pid \(pid)\n", stderr)
    let element = try axElement(pid: pid, atPath: path)
    let targetRole = axRole(of: element) ?? "<unknown>"
    let err = axSetFocused(element)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementSetAttributeValue(kAXFocusedAttribute) on \(targetRole) failed for path \(path) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX focus by path complete.\n", stderr)
}

/// Semantically activates an AX element identified by a traversal path.
/// Text inputs are focused, selection-bearing rows/items are selected, and
/// pressable controls receive `AXPress`. It never posts a mouse event.
public func activateAccessibilityElement(pid: Int32, atPath path: String) throws {
    fputs("log: AX activate at path \(path) for pid \(pid)\n", stderr)
    let element = try axElement(pid: pid, atPath: path)
    let targetRole = axRole(of: element) ?? "<unknown>"
    prepareVirtualCursorForAXPress(element)

    if isTextInputRole(targetRole) {
        let focusErr = axSetFocused(element)
        if focusErr == .success {
            fputs("log: AX activate focused text input role=\(targetRole).\n", stderr)
            return
        }
        let pressErr = axPerformPress(element)
        if pressErr == .success {
            fputs("log: AX activate pressed text input fallback role=\(targetRole).\n", stderr)
            return
        }
        throw MacosUseSDKError.inputSimulationFailed(
            "AX activate failed to focus or press \(targetRole) for path \(path) — focus AXError \(focusErr.rawValue), press AXError \(pressErr.rawValue)"
        )
    }

    if isSelectionRole(targetRole) {
        let selectedErr = axSetSelected(element, selected: true)
        if selectedErr == .success {
            fputs("log: AX activate selected role=\(targetRole).\n", stderr)
            return
        }
        let pressErr = axPerformPress(element)
        if pressErr == .success {
            fputs("log: AX activate pressed selection fallback role=\(targetRole).\n", stderr)
            return
        }
        throw MacosUseSDKError.inputSimulationFailed(
            "AX activate failed to select or press \(targetRole) for path \(path) — selected AXError \(selectedErr.rawValue), press AXError \(pressErr.rawValue)"
        )
    }

    let pressErr = axPerformPress(element)
    if pressErr == .success {
        fputs("log: AX activate pressed role=\(targetRole).\n", stderr)
        return
    }

    let focusErr = axSetFocused(element)
    if focusErr == .success {
        fputs("log: AX activate focused fallback role=\(targetRole).\n", stderr)
        return
    }

    let actions = axSupportedActions(of: element).joined(separator: ",")
    throw MacosUseSDKError.inputSimulationFailed(
        "AX activate failed for \(targetRole) at path \(path) — press AXError \(pressErr.rawValue), focus AXError \(focusErr.rawValue), actions=[\(actions)]"
    )
}

/// Sets `kAXSelectedAttribute` on the AX element under `point`. The right
/// primitive for selecting table rows, list items, sidebar entries, and
/// other selection-bearing controls in Catalyst apps where rows expose the
/// `AXSelected` attribute but no `AXPress` action.
///
/// In single-selection tables, setting this attribute typically deselects
/// any prior selection automatically; the host app reconciles the parent
/// table's `kAXSelectedRowsAttribute` in response.
/// - Parameters:
///   - pid: Target application's process id.
///   - point: Top-left CGPoint of the element to (de)select.
///   - selected: True to select, false to deselect.
/// - Throws: `MacosUseSDKError` if hit-test fails or the AX set call rejects.
public func setAccessibilitySelected(pid: Int32, at point: CGPoint, selected: Bool) throws {
    fputs("log: AX set selected=\(selected) at (\(point.x), \(point.y)) for pid \(pid)\n", stderr)
    // Catalyst hit-test is unreliable for table rows (returns the window-level
    // AXGroup, not the row). Walk the tree from the app root to find an
    // AXRow/AXOutlineRow/AXListItem whose frame contains the point.
    let app = AXUIElementCreateApplication(pid)
    let preferredRoles: Set<String> = ["AXRow", "AXOutlineRow", "AXListItem"]
    guard let target = findAXElement(in: app, at: point, preferredRoles: preferredRoles) else {
        throw MacosUseSDKError.inputSimulationFailed(
            "no selectable AX element found at (\(point.x), \(point.y)) for pid \(pid)"
        )
    }
    let targetRole = axRole(of: target) ?? "<unknown>"
    fputs("log: AX set selected target role=\(targetRole)\n", stderr)
    let value: CFBoolean = selected ? kCFBooleanTrue : kCFBooleanFalse
    let err = AXUIElementSetAttributeValue(target, kAXSelectedAttribute as CFString, value)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementSetAttributeValue(kAXSelectedAttribute=\(selected)) on \(targetRole) failed at (\(point.x), \(point.y)) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX set selected complete.\n", stderr)
}

/// Sets `kAXSelectedAttribute` on the AX element identified by a traversal path.
public func setAccessibilitySelected(pid: Int32, atPath path: String, selected: Bool) throws {
    fputs("log: AX set selected=\(selected) at path \(path) for pid \(pid)\n", stderr)
    let element = try axElement(pid: pid, atPath: path)
    let targetRole = axRole(of: element) ?? "<unknown>"
    let err = axSetSelected(element, selected: selected)
    guard err == .success else {
        throw MacosUseSDKError.inputSimulationFailed(
            "AXUIElementSetAttributeValue(kAXSelectedAttribute=\(selected)) on \(targetRole) failed for path \(path) — AXError \(err.rawValue)"
        )
    }
    fputs("log: AX set selected by path complete.\n", stderr)
}
