import AppKit
import SwiftUI

/// Which phone to show: one found over Bonjour (by name), or the address typed in Settings.
private let manualChoice = "\u{0}manual"

struct ContentView: View {
    @Bindable var viewer: Viewer
    let finder: PhoneFinder
    @AppStorage(Keys.manualURL) private var manualURL = ""
    /// The phone picked in the toolbar; nil picks the first found (or the typed address).
    @State private var chosen: String?
    @GestureState private var drag = CGSize.zero
    @GestureState private var pinchStart: Double?
    /// Where focus was last asked for, as fractions of the picture, shown briefly.
    @State private var focusMark: CGPoint?

    var body: some View {
        VStack(spacing: 0) {
            picture
            Divider()
            controls
        }
        .toolbar { toolbar }
        .navigationTitle(viewer.phoneName.isEmpty ? "ScreenEye" : "ScreenEye — \(viewer.phoneName)")
        .task(id: target) { await connect() }
    }

    // MARK: Choosing the phone

    /// What to connect to, as an identity for `.task(id:)`.
    private var target: String {
        if chosen == manualChoice || (chosen == nil && finder.phones.isEmpty) { return "url:" + manualURL }
        let name = chosen.flatMap { c in finder.phones.first { $0.name == c }?.name } ?? finder.phones.first?.name
        return name.map { "bonjour:" + $0 } ?? "url:" + manualURL
    }

    private func connect() async {
        let t = target
        if t.hasPrefix("bonjour:") {
            let name = String(t.dropFirst("bonjour:".count))
            guard let phone = finder.phones.first(where: { $0.name == name }) else { return }
            // Keep trying: the phone may be asleep, or the Local Network prompt not yet answered.
            while !Task.isCancelled {
                do {
                    let url = try await PhoneFinder.resolve(phone.endpoint)
                    guard !Task.isCancelled else { return }
                    viewer.connect(to: PhoneAPI(base: url), name: phone.name)
                    return
                } catch {
                    guard !Task.isCancelled else { return }
                    viewer.connect(to: nil, name: phone.name, problem: "Found \(phone.name), but couldn't connect to it. "
                                   + "Is Local Network access allowed for ScreenEye (System Settings → Privacy & Security)? Retrying…")
                    try? await Task.sleep(for: .seconds(3))
                }
            }
        } else {
            let api = PhoneAPI.base(from: manualURL).map(PhoneAPI.init)
            viewer.connect(to: api, name: api?.base.host() ?? "")
        }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            Menu {
                Picker("Phone", selection: $chosen) {
                    Text("First found").tag(String?.none)
                    ForEach(finder.phones) { Text($0.name).tag(String?.some($0.name)) }
                    if !manualURL.isEmpty { Text("Typed address (\(manualURL))").tag(String?.some(manualChoice)) }
                }
                .pickerStyle(.inline)
                Divider()
                SettingsLink { Text("Settings…") }
            } label: {
                Label(viewer.phoneName.isEmpty ? "No phone" : viewer.phoneName, systemImage: "iphone.rear.camera")
                    .labelStyle(.titleAndIcon)
            }
            .help("Which phone to show")
        }
        ToolbarItem(placement: .status) {
            Text(viewer.framesPerSecond > 0 ? String(format: "%.1f fps", viewer.framesPerSecond) : "")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(minWidth: 60)
        }
        ToolbarItem {
            Toggle(isOn: $viewer.paused) { Label("Pause", systemImage: viewer.paused ? "play.fill" : "pause.fill") }
                .help(viewer.paused ? "Resume the live view" : "Pause the live view")
        }
        ToolbarItem {
            Button { viewer.copyFrame() } label: { Label("Copy", systemImage: "doc.on.doc") }
                .help("Copy the live frame (⇧⌘C)")
                .disabled(viewer.frame == nil)
        }
    }

    // MARK: The picture

    private var picture: some View {
        GeometryReader { g in
            let rect = PictureMath.fitted(viewer.frame?.size ?? .zero, in: g.size)
            ZStack(alignment: .topLeading) {
                Color.black
                if let frame = viewer.frame {
                    Image(nsImage: frame)
                        .resizable()
                        .interpolation(.high)
                        .frame(width: rect.width, height: rect.height)
                        .offset(x: rect.minX + viewer.dragOffset.width + drag.width,
                                y: rect.minY + viewer.dragOffset.height + drag.height)
                }
                if let m = focusMark {
                    RoundedRectangle(cornerRadius: 3)
                        .stroke(.yellow, lineWidth: 1.5)
                        .frame(width: 64, height: 64)
                        .position(x: rect.minX + m.x * rect.width, y: rect.minY + m.y * rect.height)
                        .allowsHitTesting(false)
                }
                if viewer.status?.canPan == true {
                    OverviewInset(image: viewer.overview, viewport: viewer.status?.viewportRect ?? .zero)
                        .padding(10)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                }
                if let problem = viewer.problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .padding(10)
                        .background(.red.opacity(0.85), in: RoundedRectangle(cornerRadius: 8))
                        .foregroundStyle(.white)
                        .padding(12)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                } else if viewer.frame == nil {
                    ProgressView("Waiting for the phone…")
                        .controlSize(.large)
                        .tint(.white)
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .clipped()
            .contentShape(Rectangle())
            .onTapGesture(count: 2) { viewer.recentre() }
            .onTapGesture { location in
                guard let p = PictureMath.fraction(of: location, in: rect) else { return }
                viewer.focus(at: p)
                focusMark = p
                Task {
                    try? await Task.sleep(for: .seconds(1.5))
                    if focusMark == p { focusMark = nil }
                }
            }
            .gesture(panGesture(drawn: rect.size).simultaneously(with: pinch))
        }
    }

    /// While zoomed in past the lens, dragging moves the visible region: the picture follows
    /// the pointer. Double-click recentres.
    private func panGesture(drawn: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 4)
            .updating($drag) { value, state, _ in
                if viewer.status?.canPan == true { state = value.translation }
            }
            .onEnded { value in viewer.pan(by: value.translation, drawn: drawn) }
    }

    /// Trackpad pinch zooms.
    private var pinch: some Gesture {
        MagnifyGesture()
            .updating($pinchStart) { value, start, _ in
                guard let z = viewer.status?.zoom else { return }
                if start == nil { start = z }
                viewer.setZoom((start ?? z) * value.magnification)
            }
    }

    // MARK: Controls

    private var controls: some View {
        HStack(alignment: .center, spacing: 18) {
            zoomControls
            Divider().frame(height: 44)
            exposureControls
            Spacer(minLength: 8)
            pictureControls
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .disabled(viewer.api == nil)
    }

    private var zoomControls: some View {
        let s = viewer.status
        let lower = s?.lower ?? 1, upper = max(s?.maxZoom ?? 1, lower)
        let zoom = s?.zoom ?? 1
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(ZoomMath.label(zoom))
                    .font(.system(.headline, design: .monospaced))
                    .frame(minWidth: 52, alignment: .leading)
                ForEach(ZoomMath.steps(lenses: s?.lensSteps ?? [1], min: lower, max: upper), id: \.self) { step in
                    Button(ZoomMath.label(step)) { viewer.setZoom(step) }
                        .buttonStyle(.bordered)
                        .tint(abs(zoom - step) < 0.05 ? .yellow : nil)
                        .font(.system(.callout, design: .monospaced))
                }
                Button { viewer.recentre() } label: { Image(systemName: "scope") }
                    .buttonStyle(.borderless)
                    .help("Recentre (⌘0, or double-click the picture)")
                    .disabled(s?.canPan != true)
            }
            Slider(value: Binding(
                get: { ZoomMath.sliderPosition(zoom: zoom, min: lower, max: upper) },
                set: { viewer.setZoom(ZoomMath.zoom(sliderPosition: $0, min: lower, max: upper)) }
            ), in: 0...1)
            .frame(width: 280)
            .disabled(upper <= lower)
            .help("Zoom (or pinch on the trackpad)")
        }
    }

    private var exposureControls: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle("Lock focus", isOn: Binding(
                get: { viewer.status?.focusLocked ?? false },
                set: { viewer.setFocusLock($0) }
            ))
            .help("Click the picture to focus there")
            HStack(spacing: 6) {
                Text("EV").font(.caption).foregroundStyle(.secondary)
                Slider(value: Binding(
                    get: { viewer.status?.ev ?? 0 },
                    set: { viewer.setEV(($0 * 3).rounded() / 3) }
                ), in: -3...3)
                .frame(width: 140)
                Text(String(format: "%+.1f", viewer.status?.ev ?? 0))
                    .font(.system(.caption, design: .monospaced))
                    .frame(width: 34, alignment: .trailing)
                Button("0") { viewer.setEV(0) }
                    .buttonStyle(.borderless)
                    .help("Reset the exposure bias")
            }
        }
    }

    private var pictureControls: some View {
        VStack(alignment: .trailing, spacing: 6) {
            HStack(spacing: 8) {
                TextField("Note (optional)", text: $viewer.note)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 200)
                    .onSubmit { viewer.takePicture(note: viewer.note) }
                Button { viewer.takePicture(note: viewer.note) } label: {
                    Label("Take Picture", systemImage: "camera.shutter.button")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(viewer.taken == .taking)
                .help("Full resolution, saved on this Mac (⌘S; ⌥⌘S for the whole frame)")
            }
            takenStatus
        }
    }

    @ViewBuilder private var takenStatus: some View {
        switch viewer.taken {
        case .idle:
            Text(" ").font(.caption)
        case .taking:
            Text("Taking…").font(.caption).foregroundStyle(.secondary)
        case .saved(let url):
            HStack(spacing: 6) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                Button(url.lastPathComponent) { NSWorkspace.shared.open(url) }
                    .buttonStyle(.link)
                    .help(url.path)
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                    .buttonStyle(.link)
                Button("Copy") { if let d = try? Data(contentsOf: url) { Viewer.copy(jpeg: d) } }
                    .buttonStyle(.link)
            }
            .font(.caption)
        case .failed(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(.red)
                .lineLimit(2)
                .frame(maxWidth: 420, alignment: .trailing)
        }
    }
}

/// The lens's whole frame, small, with the visible region outlined.
struct OverviewInset: View {
    let image: NSImage?
    let viewport: CGRect

    var body: some View {
        let aspect = image.map { $0.size.width / max($0.size.height, 1) } ?? 16.0 / 9.0
        ZStack {
            if let image { Image(nsImage: image).resizable() } else { Color.black.opacity(0.5) }
        }
        .aspectRatio(aspect, contentMode: .fit)
        .frame(width: aspect >= 1 ? 180 : 100)
        .overlay {
            GeometryReader { g in
                Rectangle()
                    .stroke(.yellow, lineWidth: 2)
                    .frame(width: viewport.width * g.size.width, height: viewport.height * g.size.height)
                    .offset(x: viewport.minX * g.size.width, y: viewport.minY * g.size.height)
            }
        }
        .border(.white.opacity(0.6))
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

struct SettingsView: View {
    let finder: PhoneFinder
    @AppStorage(Keys.manualURL) private var manualURL = ""
    @AppStorage(Keys.folder) private var folder = PictureStore.defaultFolder.path
    @AppStorage(Keys.copyAfterTaking) private var copyAfterTaking = false
    @AppStorage(Keys.previewSize) private var previewSize = Keys.defaultPreviewSize

    var body: some View {
        Form {
            Section("Phone") {
                if finder.phones.isEmpty {
                    Text("None found over Bonjour yet").foregroundStyle(.secondary)
                } else {
                    ForEach(finder.phones) { Text($0.name) }
                }
                if let problem = finder.problem {
                    Text(problem).foregroundStyle(.red).font(.caption)
                }
                TextField("Address", text: $manualURL, prompt: Text("e.g. 172.29.0.40:8080"))
                if !manualURL.isEmpty, PhoneAPI.base(from: manualURL) == nil {
                    Text("That isn't a valid http address.").foregroundStyle(.red).font(.caption)
                }
                Text("Used when no phone is found over Bonjour, or when picked in the toolbar.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Pictures") {
                HStack {
                    Text(folder).lineLimit(1).truncationMode(.middle).help(folder)
                    Spacer()
                    Button("Choose…") { chooseFolder() }
                    Button("Show") { NSWorkspace.shared.open(URL(filePath: folder, directoryHint: .isDirectory)) }
                }
                Text("Saved as YYYY-MM-DD/HHMMSS[-note].jpg, with a .json sidecar of the camera's state.")
                    .font(.caption).foregroundStyle(.secondary)
                Toggle("Copy each picture to the clipboard too", isOn: $copyAfterTaking)
            }
            Section("Live view") {
                Picker("Frame size", selection: $previewSize) {
                    Text("1280 px").tag(1280)
                    Text("1600 px").tag(1600)
                    Text("1920 px").tag(1920)
                    Text("Full resolution").tag(0)
                }
                Text("Smaller is smoother. Pictures you take are always full resolution.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func chooseFolder() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.canChooseFiles = false
        p.canCreateDirectories = true
        p.directoryURL = URL(filePath: folder, directoryHint: .isDirectory)
        if p.runModal() == .OK, let url = p.url { folder = url.path }
    }
}
