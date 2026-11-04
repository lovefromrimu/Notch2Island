import AppKit
import CoreGraphics

for (index, screen) in NSScreen.screens.enumerated() {
    let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
    let builtin = CGDisplayIsBuiltin(id) != 0
    let vendor = CGDisplayVendorNumber(id)
    let sidecar = !builtin && vendor == 0x6161706c &&
        screen.localizedName.localizedCaseInsensitiveContains("Sidecar")
    print("screen=\(index) id=\(id) name=\(screen.localizedName) builtIn=\(builtin) sidecarCandidate=\(sidecar) vendor=0x\(String(vendor,radix:16)) size=\(Int(screen.frame.width))x\(Int(screen.frame.height))")
}
