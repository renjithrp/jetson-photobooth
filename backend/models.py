"""Settings schema for the photo booth, validated with pydantic v2.

The whole settings tree is editable from the admin dashboard. Defaults here are
what a fresh install boots with; they are merged with the saved settings.json so
new fields added in later versions appear automatically.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class GeneralSettings(BaseModel):
    booth_name: str = "PhotoBooth Pro"
    # {gesture} = the selected gesture's emoji, {gesture_name} = its name (kiosk fills these in)
    welcome_message: str = "Show {gesture} to start!"
    thanks_message: str = "Thanks! Grab your photo 👇"
    language: str = "en"
    show_ip_overlay: bool = True          # show the admin URL in a screen corner
    admin_pin: str = "1234"               # gate for the admin dashboard


class TriggerSettings(BaseModel):
    # gesture = AI hand gesture; arduino = USB Arduino Nano button; both = either fires
    mode: Literal["gesture", "arduino", "both", "gpio"] = "gesture"
    # USB Arduino Nano trigger (serial over USB). The sketch prints a line per button
    # press; the host fires a capture on it. Hot-pluggable with auto-reconnect.
    arduino_port: str = "auto"            # "auto" = detect by USB VID/PID, or e.g. /dev/ttyACM0
    arduino_baud: int = 115200
    arduino_trigger_token: str = "TRIG"   # line that fires a capture ("" = any non-empty line)
    arduino_print_token: str = "PRINT"    # line that requests a print of the last session
    arduino_debounce_ms: int = 400        # ignore repeated presses within this window
    # GPIO (legacy — Orange Pi 5B libgpiod; unused on Jetson, kept for compatibility)
    gpio_chip: str = "gpiochip0"
    gpio_line: int = 17
    gpio_active_low: bool = True
    gpio_debounce_ms: int = 200
    # Gesture (MediaPipe hands on the preview camera)
    gesture_type: Literal["open_palm", "fist", "peace", "thumbs_up", "three",
                          "rock", "one", "pinky", "call_me", "love",
                          "any_hand"] = "open_palm"
    gesture_hold_seconds: float = 1.5     # how long the gesture must be held
    gesture_start_delay: float = 0.0      # extra delay after detect before countdown
    cooldown_seconds: float = 5.0         # ignore triggers right after a session
    # Face gating — require a face inside a target zone before a gesture counts.
    # Stops the booth firing when no one is actually standing in front of it.
    require_face: bool = False
    face_region: Literal["full", "center_square", "center_wide", "custom"] = "center_square"
    # Custom zone as a normalised box (0..1) of the preview frame; used when
    # face_region == "custom". Defaults match the "center_square" preset.
    face_region_x: float = 0.30
    face_region_y: float = 0.12
    face_region_w: float = 0.40
    face_region_h: float = 0.76
    face_min_size: float = 0.0            # min face width as a fraction of frame (0 = off)


class TimerSettings(BaseModel):
    countdown_seconds: int = 3
    num_shots: int = 1                    # >1 enables multi-shot
    interval_seconds: float = 2.0         # gap between multi-shots
    review_seconds: int = 10              # how long the result/QR stays up
    attract_after_seconds: int = 60       # idle time before the attract screen


class CameraSettings(BaseModel):
    backend: Literal["mock", "sony", "webcam"] = "mock"
    transfer_size: Literal["small", "original"] = "small"   # Sony PC-save size
    save_subdir_by_date: bool = True
    # Sony CrSDK helper binary on the Jetson (captures + downloads to output_dir).
    # capture_output_dir MUST match the camera daemon's BOOTH_CAPTURE_DIR (where the
    # SDK downloads stills); the booth then moves new files into the session folder.
    capture_binary: str = "/opt/CrSDK/external/crsdk/boothCapture"
    capture_output_dir: str = "/opt/photobooth/data/incoming"
    capture_timeout_seconds: int = 40


class PreviewSettings(BaseModel):
    enabled: bool = True
    source: Literal["mock", "webcam", "sony_http"] = "mock"
    webcam_index: int = 0
    sony_http_url: str = "http://127.0.0.1:8080/"
    mirror: bool = True
    fps: int = 15


class GDriveDestination(BaseModel):
    enabled: bool = False
    rclone_remote: str = "gdrive"         # name of a configured rclone remote
    folder: str = "PhotoBooth"
    make_share_link: bool = True


class FTPDestination(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 21
    username: str = ""
    password: str = ""
    remote_dir: str = "/photobooth"
    use_tls: bool = True
    passive: bool = True


class StorageSettings(BaseModel):
    local_dir: str = "captures"           # relative to data dir, or absolute
    keep_local: bool = True
    max_local_sessions: int = 0           # 0 = unlimited, else prune oldest
    # Background sync: queue uploads to a durable worker (retry/backoff, survives reboot &
    # offline) instead of blocking the capture. Turn off for legacy inline uploads.
    background_sync: bool = True
    gdrive: GDriveDestination = GDriveDestination()
    ftp: FTPDestination = FTPDestination()


class OverlaySettings(BaseModel):
    enabled: bool = False
    frame_png: str = ""                   # full-frame PNG with alpha, overlaid on top
    logo_png: str = ""
    logo_position: Literal["tl", "tr", "bl", "br"] = "br"
    apply_to: Literal["each", "collage", "both"] = "both"


class CollageSettings(BaseModel):
    enabled: bool = False
    layout: Literal["strip_vertical", "strip_horizontal", "grid_2x2"] = "grid_2x2"
    background: str = "#ffffff"
    gap_px: int = 16


class AISettings(BaseModel):
    """AI background effects via portrait segmentation (rembg + onnxruntime, GPU/CPU)."""
    model_config = {"protected_namespaces": ()}
    enabled: bool = False
    effect: Literal["none", "bg_remove", "bg_replace"] = "none"
    model: str = "u2net_human_seg"        # rembg model: u2net_human_seg / isnet-general-use / birefnet-portrait
    use_gpu: bool = True
    background_image: str = ""            # backdrop for bg_replace (falls back to colour)
    background_color: str = "#ffffff"    # solid fill for bg_remove / when no backdrop image


class GazeSettings(BaseModel):
    """Gaze correction (eye redirection) — SCAFFOLD, measurement only for now.

    On each shot this detects faces, head pose and eye-openness and LOGS how often a
    correction WOULD fire given the gate below — so you can see how relevant the feature
    is on real guests before investing in a redirection model. It does NOT alter photos
    yet; the actual eye-redirection step (a flow-warp ONNX run on each eye crop) is the
    next phase and will honour `strength`. Reuses the InsightFace pack from `faces` for
    detection + 106-pt landmarks + head pose (no extra model download)."""
    model_config = {"protected_namespaces": ()}
    enabled: bool = False
    strength: int = 40                 # 0-100: how far to pull the iris toward centre (used by the model step)
    max_head_angle: int = 20           # safety gate: skip when |yaw| or |pitch| exceeds this (degrees)
    min_eye_openness: float = 0.15     # skip when an eye is more closed than this (EAR-like proxy)
    use_gpu: bool = True               # CUDA/TensorRT EP when available, else CPU


class PrintSettings(BaseModel):
    """Photo printing via CUPS. Works with dye-sub photo printers (DNP/Selphy/Mitsubishi)
    or any USB/network printer. The Arduino PRINT button prints the last session."""
    enabled: bool = False
    printer: str = ""                     # "" = system default (CUPS)
    copies: int = 1
    media: str = ""                       # e.g. "4x6", "A6", "" = printer default
    fit_to_page: bool = True
    auto_print: bool = False              # print automatically after every session
    which: Literal["final", "each", "collage"] = "final"   # what to print


class NetworkSettings(BaseModel):
    """Guest hotspot (AP) config. Runs on a SEPARATE radio (the USB dongle) so the booth
    stays online on the M.2 while serving guests offline. The management radio is never
    used for the AP."""
    hotspot_enabled: bool = False
    hotspot_ssid: str = "PhotoBooth"
    hotspot_password: str = "booth1234"   # WPA2, >=8 chars — secret, masked by the API
    hotspot_band: Literal["bg", "a"] = "bg"   # bg = 2.4GHz (compatible), a = 5GHz
    hotspot_hidden: bool = False


class ShareSettings(BaseModel):
    qr_enabled: bool = True
    base_url: str = ""                    # override public URL; blank = auto-detect


class FacesSettings(BaseModel):
    """Group photos by the person's face. On the Jetson: InsightFace (SCRFD detector +
    ArcFace r50 embedding) on the GPU via onnxruntime (CUDA/TensorRT EP), CPU fallback."""
    model_config = {"protected_namespaces": ()}   # allow the model_pack field name
    enabled: bool = False
    engine: Literal["insightface", "off"] = "insightface"
    model_pack: str = "buffalo_l"         # InsightFace pack: SCRFD-10G det + ArcFace r50
    det_size: int = 640                   # detector input (square); larger = smaller faces
    use_gpu: bool = True                  # CUDA EP when available, else CPU
    match_threshold: float = 0.35         # cosine similarity to consider "same person"
    min_face_px: int = 50                 # ignore faces smaller than this (bbox px)
    allow_guest_find: bool = True         # let guests "find my photos" via a selfie


class Settings(BaseModel):
    general: GeneralSettings = GeneralSettings()
    trigger: TriggerSettings = TriggerSettings()
    timer: TimerSettings = TimerSettings()
    camera: CameraSettings = CameraSettings()
    preview: PreviewSettings = PreviewSettings()
    storage: StorageSettings = StorageSettings()
    overlay: OverlaySettings = OverlaySettings()
    collage: CollageSettings = CollageSettings()
    ai: AISettings = AISettings()
    gaze: GazeSettings = GazeSettings()
    share: ShareSettings = ShareSettings()
    faces: FacesSettings = FacesSettings()
    network: NetworkSettings = NetworkSettings()
    printing: PrintSettings = PrintSettings()
