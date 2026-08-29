/*
 * PhotoBooth Pro — ESP32-S3 USB button trigger
 * --------------------------------------------
 * Same wire protocol as the Arduino Nano sketch (../booth_trigger), ported to the
 * ESP32-S3. The Jetson backend (backend/triggers.py :: ArduinoTrigger) listens for:
 *
 *   TRIG   -> start a capture    (CAPTURE button)
 *   PRINT  -> print last session (PRINT button)
 *
 * The host may send back (newline-terminated):
 *   LED:1 / LED:0   -> "ready" LED on/off (only if PIN_LED is set below)
 *
 * WIRING: each button between its pin and GND. Internal pull-ups are used, so a
 * pressed button reads LOW.
 *
 * WHICH USB SOCKET?  An ESP32-S3 dev board usually has two: one marked "UART"
 * (a CH343/CP210x bridge, shows up as VID 1a86/10c4) and one marked "USB" (the
 * chip's own USB, VID 303a). Which one carries `Serial` depends on the
 * "USB CDC On Boot" build option, which is a very easy way to end up with a board
 * that looks alive but never reaches the booth. This sketch sidesteps it by
 * printing every event to BOTH the native USB CDC and UART0, so it works in
 * either socket, whatever that option is set to.
 *
 * BUILD (Arduino IDE): Board = "ESP32S3 Dev Module", 115200 baud. No other option
 * matters — see above. Or with arduino-cli:
 *   arduino-cli compile -b esp32:esp32:esp32s3 booth_trigger_esp32s3
 *   arduino-cli upload  -b esp32:esp32:esp32s3 -p /dev/ttyACM0 booth_trigger_esp32s3
 */

// GPIOs chosen to avoid the ESP32-S3's reserved pins: 0/3/45/46 are strapping pins,
// 19/20 are USB D-/D+, 26-37 are flash/PSRAM, 43/44 are UART0 TX/RX.
const int PIN_CAPTURE = 4;
const int PIN_PRINT   = 5;
// Set to a plain GPIO to drive a "ready" LED. Left off by default: the onboard LED
// on most S3 dev boards is an addressable WS2812 on GPIO48, which digitalWrite()
// cannot drive.
const int PIN_LED     = -1;

const unsigned long DEBOUNCE_MS = 40;   // local edge filter; the host debounces too

// When the core is built with USB CDC On Boot, `Serial` is the native-USB CDC and
// UART0 is exposed separately as `Serial0`. Without it, `Serial` IS UART0.
#if ARDUINO_USB_CDC_ON_BOOT
  #define HAVE_SEPARATE_UART 1
#endif

// Both the resting level AND which edge means "pressed" are learned at boot, not
// assumed. A button wired to GND rests HIGH and pulses LOW; the RF remote fitted to
// this booth does the opposite — GPIO4 rests LOW and pulses HIGH — so a hardcoded
// falling edge fired on RELEASE instead of press. Whatever the pin reads at rest is
// "not pressed"; the other level is.
//
// Seeding from the real pin also matters because opening the serial port asserts
// DTR/RTS, which is wired to EN and reboots the chip: assuming HIGH invented a
// falling edge and the booth took a phantom photo every time the backend
// reconnected. Observed doing exactly that.
int lastCap, lastPrn;          // level at the previous poll
int activeCap, activePrn;      // the level that means "pressed"
unsigned long tCap = 0, tPrn = 0;

static void emit(const char *msg) {
  Serial.println(msg);
#ifdef HAVE_SEPARATE_UART
  Serial0.println(msg);
#endif
}

void setup() {
  Serial.begin(115200);
#ifdef HAVE_SEPARATE_UART
  Serial0.begin(115200);
#endif
  pinMode(PIN_CAPTURE, INPUT_PULLUP);
  pinMode(PIN_PRINT,   INPUT_PULLUP);
  if (PIN_LED >= 0) {
    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, HIGH);        // ready
  }
  delay(200);                           // let the USB CDC host side attach first
  lastCap = digitalRead(PIN_CAPTURE);   // adopt the resting state; see above
  lastPrn = digitalRead(PIN_PRINT);
  activeCap = !lastCap;                 // ...and infer which edge is a press
  activePrn = !lastPrn;
  emit("READY");
}

static void handleButton(int pin, int &last, int active, unsigned long &t,
                         const char *msg) {
  int v = digitalRead(pin);
  if (v != last && (millis() - t) > DEBOUNCE_MS) {
    t = millis();
    if (v == active) emit(msg);         // fire on press, whichever level that is
    last = v;
  }
}

static void handleHostCommand(Stream &in) {
  if (!in.available()) return;
  String cmd = in.readStringUntil('\n');
  cmd.trim();
  if (PIN_LED < 0) return;
  if (cmd == "LED:1") digitalWrite(PIN_LED, HIGH);
  else if (cmd == "LED:0") digitalWrite(PIN_LED, LOW);
}

void loop() {
  handleButton(PIN_CAPTURE, lastCap, activeCap, tCap, "TRIG");
  handleButton(PIN_PRINT,   lastPrn, activePrn, tPrn, "PRINT");
  handleHostCommand(Serial);
#ifdef HAVE_SEPARATE_UART
  handleHostCommand(Serial0);
#endif
  delay(5);
}
