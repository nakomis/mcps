import SwiftUI
import UIKit

/// The UserDefaults key for the host address typed in Settings.
let hostURLKey = "hostURL"

struct ContentView: View {
    let camera: Camera
    @State private var finder = HostFinder()
    @State private var saver = Saver()
    @State private var parcelHost = HostFinder(type: "_parcel-finder._tcp")
    @State private var runner = ParcelRunner()
    /// Looking for the pairing QR code in the camera's view (started from Settings).
    @State private var scanningForPairing = false
    @State private var pairingMessage: String?
    @State private var note = ""
    @State private var showSettings = false
    @GestureState private var pinchStart: Double?
    /// The viewport centre when a drag began.
    @GestureState private var dragStart: CGPoint?
    /// The preview's clockwise turn of the raw frame, and its size on screen.
    @State private var previewAngle = 90
    @State private var previewSize = CGSize.zero
    @AppStorage(hostURLKey) private var hostURL = ""
    @FocusState private var noteFocused: Bool

    var body: some View {
        ZStack {
            PreviewView(session: camera.session, viewport: camera.viewport, aspect: camera.frameAspect) { previewAngle = $0 }
                .ignoresSafeArea()
                .onGeometryChange(for: CGSize.self) { $0.size } action: { previewSize = $0 }
                .contentShape(Rectangle())
                .gesture(pinch.simultaneously(with: drag))
                .onTapGesture(count: 2) { camera.recentre() }
                .onTapGesture { noteFocused = false }

            VStack(spacing: 8) {
                HStack(alignment: .top) {
                    Text(camera.statusLine)
                        .font(.system(.caption, design: .monospaced))
                        .padding(6)
                        .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))
                    Spacer()
                    VStack(alignment: .trailing, spacing: 8) {
                        Button { showSettings = true } label: {
                            Image(systemName: "gearshape.fill")
                                .padding(8)
                                .background(.black.opacity(0.6), in: Circle())
                        }
                        .accessibilityLabel("Settings")
                        if camera.viewport.digital > 1.001 {
                            OverviewInset(image: camera.overview, viewport: camera.viewport,
                                          angle: previewAngle, aspect: camera.frameAspect)
                        }
                    }
                }
                Spacer()
                zoomControls
                saveControls
            }
            .padding()
            .foregroundStyle(.white)
        }
        .background(.black)
        .onAppear { finder.start(); parcelHost.start() }
        .sheet(isPresented: $showSettings) {
            SettingsView(finder: finder, hostURL: $hostURL, parcelHost: parcelHost) {
                showSettings = false
                scanForPairing()
            }
        }
        .sheet(item: Binding(get: { runner.waiting }, set: { _ in })) { run in
            ParcelChoiceView(run: run) { runner.choose($0) }
                .presentationDetents([.medium, .large])
                .interactiveDismissDisabled()
        }
    }

    // MARK: Zoom

    private var pinch: some Gesture {
        // The zoom when the pinch began; @GestureState resets it even if the pinch is cancelled.
        MagnifyGesture()
            .updating($pinchStart) { value, start, _ in
                if start == nil { start = camera.zoom }
                camera.setZoom((start ?? camera.zoom) * value.magnification)
            }
    }

    /// While zoomed in past the lens, dragging moves the visible region: the picture follows the
    /// finger. Double tap recentres.
    private var drag: some Gesture {
        DragGesture(minimumDistance: 8)
            .updating($dragStart) { value, start, _ in
                let v = camera.viewport
                guard v.digital > 1.001 else { return }
                if start == nil { start = v.center }
                let shown = ViewportMath.fitted(
                    aspect: ViewportMath.shownAspect(raw: camera.frameAspect, angle: previewAngle), in: previewSize)
                camera.setView(zoom: nil, center: ViewportMath.dragged(
                    from: start ?? v.center, translation: value.translation,
                    angle: previewAngle, shown: shown, digital: v.digital))
            }
    }

    private var sliderPosition: Binding<Double> {
        Binding(
            get: { ZoomMath.sliderPosition(zoom: camera.zoom, min: camera.minZoom, max: camera.maxZoom) },
            set: { camera.setZoom(ZoomMath.zoom(sliderPosition: $0, min: camera.minZoom, max: camera.maxZoom)) }
        )
    }

    private var zoomControls: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                Text(ZoomMath.label(camera.zoom))
                    .font(.system(.headline, design: .monospaced))
                    .frame(minWidth: 56)
                    .accessibilityLabel("Zoom \(ZoomMath.label(camera.zoom))")
                ForEach(ZoomMath.steps(lenses: camera.lensSteps, min: camera.minZoom, max: camera.maxZoom), id: \.self) { step in
                    let current = abs(camera.zoom - step) < 0.05
                    Button(ZoomMath.label(step)) { camera.setZoom(step) }
                        .font(.system(.subheadline, design: .monospaced).bold())
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(current ? Color.yellow : Color.black.opacity(0.6), in: Capsule())
                        .foregroundStyle(current ? .black : .white)
                }
            }
            if camera.maxZoom > camera.minZoom {
                Slider(value: sliderPosition, in: 0...1)
                    .tint(.yellow)
                    .frame(maxWidth: 420)
                    .accessibilityLabel("Zoom")
                    .accessibilityValue(ZoomMath.label(camera.zoom))
            }
        }
        .padding(8)
        .background(.black.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
    }

    // MARK: Save

    private var saveControls: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                TextField("Note (optional)", text: $note)
                    .textFieldStyle(.roundedBorder)
                    .foregroundStyle(.primary)
                    .frame(maxWidth: 260)
                    .focused($noteFocused)
                    .submitLabel(.done)
                    .onChange(of: note) { _, new in
                        if new.count > 60 { note = String(new.prefix(60)) }
                    }
                Button {
                    noteFocused = false
                    saver.save(camera: camera, finder: finder, note: note,
                               manualURL: hostURL, device: UIDevice.current.name)
                } label: {
                    Label("Save to Mac", systemImage: "square.and.arrow.down")
                        .font(.headline)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(saver.isSaving ? Color.gray : Color.blue, in: Capsule())
                }
                .disabled(saver.isSaving)
                Button {
                    noteFocused = false
                    runner.open(camera: camera, finder: parcelHost)
                } label: {
                    Label("Open in Claude", systemImage: "sparkle.magnifyingglass")
                        .font(.headline)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(runner.isBusy ? Color.gray : Color.orange, in: Capsule())
                }
                .disabled(runner.isBusy)
            }
            saveStatus
            parcelStatus
        }
    }

    // MARK: Open in Claude

    @ViewBuilder private var parcelStatus: some View {
        if let pairingMessage {
            Label(pairingMessage, systemImage: scanningForPairing ? "qrcode.viewfinder" : "checkmark.circle.fill")
                .font(.caption.bold())
                .padding(6)
                .background((scanningForPairing ? Color.black : Color.green).opacity(0.8), in: RoundedRectangle(cornerRadius: 6))
        }
        switch runner.state {
        case .idle:
            EmptyView()
        case .preparing(let step):
            ProgressView { Text(step).font(.caption) }.tint(.white)
        case .running(let run):
            VStack(spacing: 2) {
                ProgressView { Text(run.step).font(.caption) }.tint(.white)
                if let item = run.item { Text(item).font(.caption2).foregroundStyle(.white.opacity(0.8)) }
            }
            .padding(6)
            .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))
        case .finished(let run):
            Label(run.summary, systemImage: run.succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.caption.bold())
                .padding(6)
                .background((run.succeeded ? Color.green : Color.red).opacity(0.85), in: RoundedRectangle(cornerRadius: 6))
                .onTapGesture { runner.dismiss() }
        case .failed(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.caption.bold())
                .multilineTextAlignment(.leading)
                .padding(6)
                .background(.red.opacity(0.85), in: RoundedRectangle(cornerRadius: 6))
                .onTapGesture { runner.dismiss() }
        }
    }

    /// Looks for the QR code from `parcel-finder pair` in the camera's view for up to a minute.
    private func scanForPairing() {
        guard !scanningForPairing else { return }
        scanningForPairing = true
        pairingMessage = "Point the camera at the code from `parcel-finder pair`…"
        Task {
            let deadline = Date().addingTimeInterval(60)
            while Date() < deadline {
                let found = await Task.detached { () -> String? in
                    camera.uprightFrame(maxDim: 1600).flatMap(ParcelQR.payload).flatMap(ParcelPairing.token(from:))
                }.value
                if let found {
                    ParcelPairing.save(found)
                    scanningForPairing = false
                    pairingMessage = "Paired with parcel-finder"
                    try? await Task.sleep(for: .seconds(3))
                    pairingMessage = nil
                    return
                }
                try? await Task.sleep(for: .milliseconds(500))
            }
            scanningForPairing = false
            pairingMessage = nil
        }
    }

    @ViewBuilder private var saveStatus: some View {
        switch saver.state {
        case .idle:
            EmptyView()
        case .saving(let fraction):
            ProgressView(value: fraction) { Text("Saving…").font(.caption) }
                .tint(.white)
                .frame(maxWidth: 260)
        case .saved(let name):
            Label("Saved \(name)", systemImage: "checkmark.circle.fill")
                .font(.caption.bold())
                .padding(6)
                .background(.green.opacity(0.8), in: RoundedRectangle(cornerRadius: 6))
        case .failed(let message):
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.caption.bold())
                .multilineTextAlignment(.leading)
                .padding(6)
                .background(.red.opacity(0.85), in: RoundedRectangle(cornerRadius: 6))
        }
    }
}

struct SettingsView: View {
    let finder: HostFinder
    @Binding var hostURL: String
    let parcelHost: HostFinder
    /// Close Settings and look for the pairing QR code in the camera's view.
    var scanPairingCode: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var pasted = ""
    @State private var paired = ParcelPairing.token != nil

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Status", value: paired ? "Paired" : "Not paired")
                    LabeledContent("On this network", value: parcelHost.hosts.isEmpty ? "Not found" : "Found")
                    Button("Scan pairing code") { scanPairingCode() }
                    TextField("…or paste the token", text: $pasted)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit {
                            if let t = ParcelPairing.token(from: pasted) { ParcelPairing.save(t); paired = true; pasted = "" }
                        }
                    if !pasted.isEmpty, ParcelPairing.token(from: pasted) == nil {
                        Text("That isn't a parcel-finder token.").foregroundStyle(.red)
                    }
                    if paired { Button("Forget pairing", role: .destructive) { ParcelPairing.forget(); paired = false } }
                } header: {
                    Text("parcel-finder (Open in Claude)")
                } footer: {
                    Text("Run `parcel-finder pair` on the Mac, then scan the code it shows. Open in Claude sends the picture and the text read from it to the Mac, which finds the Claude conversation the part was bought in.")
                }
                Section {
                    if finder.hosts.isEmpty {
                        Text("None found yet").foregroundStyle(.secondary)
                    } else {
                        ForEach(finder.hosts, id: \.name) { Text($0.name) }
                    }
                } header: {
                    Text("Found on this network")
                } footer: {
                    Text("Save uses the first host found over Bonjour (_screeneye-host._tcp).")
                }
                Section {
                    TextField("e.g. mace.local:8765", text: $hostURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    if !hostURL.isEmpty, HostURL.frames(base: hostURL) == nil {
                        Text("That isn't a valid http address.").foregroundStyle(.red)
                    }
                } header: {
                    Text("Host address")
                } footer: {
                    Text("Used when no host is found over Bonjour. Frames are posted to /api/frames on it. Plain http works only for .local names and IP addresses.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}

/// parcel-finder's pick-list: which conversation was this bought in?
struct ParcelChoiceView: View {
    let run: ParcelRun
    let choose: (String?) -> Void

    var body: some View {
        NavigationStack {
            List(run.choices) { c in
                Button { choose(c.sessionID) } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.title).font(.headline)
                        Text("\(c.when) · \(c.hits) matches").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle(run.item ?? "Which conversation?")
            .navigationBarTitleDisplayMode(.inline)
            .safeAreaInset(edge: .top) {
                Text(run.step).font(.subheadline).padding(.horizontal).frame(maxWidth: .infinity, alignment: .leading)
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel", role: .cancel) { choose(nil) } }
            }
        }
    }
}

/// The lens's whole frame, small, with the visible region outlined.
struct OverviewInset: View {
    let image: CGImage?
    let viewport: Viewport
    /// The preview's clockwise turn of the raw frame, so the inset matches the preview.
    let angle: Int
    let aspect: Double

    var body: some View {
        let shownAspect = ViewportMath.shownAspect(raw: aspect, angle: angle)
        let r = FrameMath.turned(viewport.rect, rotation: angle)
        ZStack {
            if let image {
                Image(decorative: image, scale: 1, orientation: Self.orientation(angle)).resizable()
            } else {
                Color.black.opacity(0.5)
            }
        }
        .aspectRatio(shownAspect, contentMode: .fit)
        .frame(width: shownAspect >= 1 ? 140 : 80)
        .overlay {
            GeometryReader { g in
                Rectangle()
                    .stroke(.yellow, lineWidth: 2)
                    .frame(width: r.width * g.size.width, height: r.height * g.size.height)
                    .offset(x: r.minX * g.size.width, y: r.minY * g.size.height)
            }
        }
        .border(.white.opacity(0.6))
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    static func orientation(_ angle: Int) -> Image.Orientation {
        switch angle {
        case 90: .right
        case 180: .down
        case 270: .left
        default: .up
        }
    }
}
