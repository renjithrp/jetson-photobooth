"""Settings schema for the photo booth, validated with pydantic v2.

The whole settings tree is editable from the admin dashboard. Defaults here are
what a fresh install boots with; they are merged with the saved settings.json so
new fields added in later versions appear automatically.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class GeneralSettings(BaseModel):
    booth_name: str = "AI Photo Booth"
    # {gesture} = the selected gesture's emoji, {gesture_name} = its name (kiosk fills these in)
    welcome_message: str = "Show {gesture} to start!"
    thanks_message: str = "Thanks! Grab your photo 👇"
    language: str = "en"
    show_ip_overlay: bool = True          # show the admin URL in a screen corner
    admin_pin: str = "1234"               # gate for the admin dashboard


class TriggerSettings(BaseModel):
    mode: Literal["gesture", "gpio", "both"] = "gesture"
    # GPIO (Orange Pi 5B uses libgpiod character device)
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
    # Sony CrSDK helper binary on the Pi (captures + downloads to output_dir)
    capture_binary: str = "/root/CrSDK/external/crsdk/boothCapture"
    capture_output_dir: str = "/root/photos"
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
    """On-device AI effects via the RK3588 NPU (RKNN). Hook now, model later."""
    enabled: bool = False
    effect: Literal["none", "bg_remove", "bg_replace"] = "none"
    background_image: str = ""
    rknn_model: str = ""                  # path to a .rknn segmentation model on the Pi


class ShareSettings(BaseModel):
    qr_enabled: bool = True
    base_url: str = ""                    # override public URL; blank = auto-detect


class FacesSettings(BaseModel):
    """Group photos by the person's face. Detection = MediaPipe (CPU);
    embedding = ArcFace on the RK3588 NPU (RKNN)."""
    enabled: bool = False
    engine: Literal["rknn", "off"] = "rknn"
    rknn_model: str = "/opt/photobooth/models/arcface.rknn"
    match_threshold: float = 0.45         # cosine similarity to consider "same person"
    min_face_px: int = 60                 # ignore faces smaller than this
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
    share: ShareSettings = ShareSettings()
    faces: FacesSettings = FacesSettings()
