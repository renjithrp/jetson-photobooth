/*
 * PhotoBooth Pro — Arduino Nano USB button trigger
 * -------------------------------------------------
 * Prints a line over USB serial when a button is pressed. The Jetson booth backend
 * (backend/triggers.py :: ArduinoTrigger) listens for these lines:
 *
 *   TRIG   -> start a capture   (CAPTURE button, pin D2 -> GND)
 *   PRINT  -> print last session (PRINT button,   pin D3 -> GND)
 *
 * The host may also send us commands (newline-terminated):
 *   LED:1 / LED:0   -> turn the "ready" LED on D13 on/off
 *
 * Wiring: each button between the pin and GND (internal pull-ups used, so pressed = LOW).
 * Baud must match settings.trigger.arduino_baud (default 115200).
 */

const int PIN_CAPTURE = 2;
const int PIN_PRINT   = 3;
const int PIN_LED     = 13;
const unsigned long DEBOUNCE_MS = 40;   // host also debounces; this is the local edge filter

int lastCap = HIGH, lastPrn = HIGH;
unsigned long tCap = 0, tPrn = 0;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_CAPTURE, INPUT_PULLUP);
  pinMode(PIN_PRINT,   INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);          // ready
  Serial.println("READY");
}

void handleButton(int pin, int &last, unsigned long &t, const char *msg) {
  int v = digitalRead(pin);
  if (v != last && (millis() - t) > DEBOUNCE_MS) {
    t = millis();
    if (v == LOW) Serial.println(msg);  // fire on press (LOW = pressed with pull-up)
    last = v;
  }
}

void loop() {
  handleButton(PIN_CAPTURE, lastCap, tCap, "TRIG");
  handleButton(PIN_PRINT,   lastPrn, tPrn, "PRINT");

  // optional host -> device commands (LED control)
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "LED:1") digitalWrite(PIN_LED, HIGH);
    else if (cmd == "LED:0") digitalWrite(PIN_LED, LOW);
  }
  delay(5);
}
