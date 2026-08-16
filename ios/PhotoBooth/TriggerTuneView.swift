import SwiftUI

/// Live gesture tuning: change the trigger gesture and its thresholds from the
/// iPad while WATCHING the detection overlay react — present as a half-height
/// sheet so the live view (with the skeleton overlay) stays visible above it.
/// Changes auto-apply (debounced) via PUT /api/settings; the gesture worker
/// hot-reloads them within ~3s. Requires the admin PIN once per app session.
struct TriggerTuneView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss

    @State private var cfg = TriggerConfig()
    @State private var loaded = false
    @State private var pin = ""
    @State private var saveState: String?
    @State private var saveTask: Task<Void, Never>?

    private let gestures: [(String, String)] = [
        ("open_palm", "✋ Open palm"), ("wave", "👋 Wave"), ("fist", "✊ Fist"),
        ("peace", "✌️ Peace"), ("thumbs_up", "👍 Thumbs up"), ("three", "🤟 Three fingers"),
        ("rock", "🤘 Rock"), ("one", "☝️ Point up"), ("pinky", "🤙 Pinky"),
        ("call_me", "🤙 Call me"), ("love", "🤟 Love"), ("any_hand", "🖐 Any hand"),
    ]

    var body: some View {
        NavigationStack {
            Group { if booth.isAdmin { form } else { gate } }
                .navigationTitle("Gesture tuning")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
        .task { await load() }
    }

    private var gate: some View {
        Form {
            Section("Admin PIN required") {
                SecureField("PIN", text: $pin).keyboardType(.numberPad)
                Button("Unlock") { Task { _ = await booth.login(pin: pin); await load() } }
            }
        }
    }

    private var form: some View {
        Form {
            Section {
                Picker("Trigger gesture", selection: $cfg.gesture_type) {
                    ForEach(gestures, id: \.0) { g in Text(g.1).tag(g.0) }
                }
                Toggle("Show detection overlay on video", isOn: $cfg.show_gesture_overlay)
                Toggle("Show live detection numbers", isOn: $cfg.show_gesture_stats)
                Toggle("Tune mode (don't trigger the camera)", isOn: $cfg.tune_mode)
                if let s = saveState { Text(s).font(.footnote).foregroundStyle(.secondary) }
            } footer: {
                Text("Watch the overlay while tuning: green skeleton = pose matches, amber = seen but rejected (the label says why).")
            }
            Section("Thresholds") {
                slider(String(format: "Hold for %.2gs", cfg.gesture_hold_seconds),
                       $cfg.gesture_hold_seconds, 0...5, step: 0.25)
                slider("Min hand size \(Int(cfg.hand_min_size * 100))% of frame",
                       $cfg.hand_min_size, 0...0.4, step: 0.01)
                slider(String(format: "Cooldown %.2gs", cfg.cooldown_seconds),
                       $cfg.cooldown_seconds, 0...15, step: 0.5)
                slider(String(format: "Start delay %.2gs", cfg.gesture_start_delay),
                       $cfg.gesture_start_delay, 0...3, step: 0.25)
                Toggle("Require a face in the zone", isOn: $cfg.require_face)
            }
            Section("Distance preset") {
                HStack {
                    Button("📷 Near") { preset(min: 0.12, scale: 0.45, assoc: 3, confirm: 3, ratio: 0.7) }
                    Spacer()
                    Button("🧍 Mid") { preset(min: 0.08, scale: 0.45, assoc: 4, confirm: 3, ratio: 0.7) }
                    Spacer()
                    Button("🏃 Far") { preset(min: 0.04, scale: 0.40, assoc: 5, confirm: 2, ratio: 0.6) }
                }
                .buttonStyle(.bordered)
                Text("Fills the gates below for the subject's distance from the 24–70; applies immediately. Far range also engages the automatic subject zoom.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section("Advanced (subject-aware gates)") {
                Stepper("Max hands tracked: \(cfg.max_hands)", value: $cfg.max_hands, in: 1...4)
                Stepper("Confirm frames before hold: \(cfg.confirm_frames)",
                        value: $cfg.confirm_frames, in: 1...10)
                slider(String(format: "Match ratio during hold %.0f%%", cfg.match_ratio * 100),
                       $cfg.match_ratio, 0...1, step: 0.05)
                slider(String(format: "Hand size vs face height %.0f%% (0 = off)", cfg.hand_face_scale * 100),
                       $cfg.hand_face_scale, 0...1, step: 0.05)
                slider(String(format: "Max hand-face distance %.1f face-heights (0 = off)", cfg.assoc_face_dist),
                       $cfg.assoc_face_dist, 0...10, step: 0.5)
            }
        }
        .onChange(of: cfg.gesture_type) { _ in apply() }
        .onChange(of: cfg.gesture_hold_seconds) { _ in apply() }
        .onChange(of: cfg.hand_min_size) { _ in apply() }
        .onChange(of: cfg.cooldown_seconds) { _ in apply() }
        .onChange(of: cfg.gesture_start_delay) { _ in apply() }
        .onChange(of: cfg.require_face) { _ in apply() }
        .onChange(of: cfg.show_gesture_overlay) { _ in apply() }
        .onChange(of: cfg.show_gesture_stats) { _ in apply() }
        .onChange(of: cfg.tune_mode) { _ in apply() }
        .onChange(of: cfg.max_hands) { _ in apply() }
        .onChange(of: cfg.confirm_frames) { _ in apply() }
        .onChange(of: cfg.match_ratio) { _ in apply() }
        .onChange(of: cfg.hand_face_scale) { _ in apply() }
        .onChange(of: cfg.assoc_face_dist) { _ in apply() }
    }

    private func slider(_ label: String, _ v: Binding<Double>,
                        _ range: ClosedRange<Double>, step: Double) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.subheadline)
            Slider(value: v, in: range, step: step)
        }
    }

    private func preset(min: Double, scale: Double, assoc: Double,
                        confirm: Int, ratio: Double) {
        cfg.hand_min_size = min
        cfg.hand_face_scale = scale
        cfg.assoc_face_dist = assoc
        cfg.confirm_frames = confirm
        cfg.match_ratio = ratio          // onChange fires -> debounced apply
    }

    // MARK: - load / debounced apply
    private func load() async {
        guard booth.isAdmin, !loaded else { return }
        if let t = await booth.triggerConfig() { cfg = t; loaded = true }
    }

    /// Debounce slider drags: one PUT ~0.6s after the last change.
    private func apply() {
        guard loaded else { return }
        saveTask?.cancel()
        saveState = "applying…"
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(600))
            guard !Task.isCancelled else { return }
            let ok = await booth.saveTrigger(cfg)
            saveState = ok ? "applied — worker picks it up in ~3s" : "save failed (still admin?)"
        }
    }
}
