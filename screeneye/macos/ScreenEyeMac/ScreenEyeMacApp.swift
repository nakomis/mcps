import SwiftUI

@main
struct ScreenEyeMacApp: App {
    @State private var viewer = Viewer()
    @State private var finder = PhoneFinder()

    var body: some Scene {
        Window("ScreenEye", id: "main") {
            ContentView(viewer: viewer, finder: finder)
                .frame(minWidth: 640, minHeight: 480)
                .onAppear { finder.start() }
        }
        .defaultSize(width: 1280, height: 860)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .saveItem) {
                Button("Take Picture") { viewer.takePicture(note: viewer.note) }
                    .keyboardShortcut("s")
                Button("Take Picture of the Whole Frame") { viewer.takePicture(note: viewer.note, fullFrame: true) }
                    .keyboardShortcut("s", modifiers: [.command, .option])
            }
            CommandGroup(after: .pasteboard) {
                Button("Copy Live Frame") { viewer.copyFrame() }
                    .keyboardShortcut("c", modifiers: [.command, .shift])
            }
            CommandMenu("Camera") {
                Button("Zoom In") { viewer.zoom(by: 1.25) }.keyboardShortcut("=")
                Button("Zoom Out") { viewer.zoom(by: 0.8) }.keyboardShortcut("-")
                Button("Zoom to 1×") { viewer.setZoom(1) }.keyboardShortcut("1")
                Button("Recentre") { viewer.recentre() }.keyboardShortcut("0")
                Divider()
                Button(viewer.status?.focusLocked == true ? "Unlock Focus" : "Lock Focus") {
                    viewer.setFocusLock(!(viewer.status?.focusLocked ?? false))
                }
                .keyboardShortcut("l")
                Button(viewer.paused ? "Resume Live View" : "Pause Live View") { viewer.paused.toggle() }
                    .keyboardShortcut("p", modifiers: [.command, .option])
            }
        }

        Settings {
            SettingsView(finder: finder)
        }
    }
}
