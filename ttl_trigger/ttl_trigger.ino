// TTL -> USB serial trigger for Camera Viewer.
//
// Wire the TTL signal to TTL_PIN and the signal's ground to the Arduino GND.
// Each time the line changes, the board prints "H" (went high) or "L" (went low)
// at 115200 baud. In Camera Viewer pick "Arduino (serial)" and this board's COM port.
//
// 5 V boards (Uno, Nano, Mega) accept 3.3 V and 5 V TTL. 3.3 V boards
// (Due, Zero, Teensy, ESP32) must NOT get 5 V on the pin - use a divider or level shifter.

const int TTL_PIN = 2;
const unsigned long DEBOUNCE_US = 100;   // ignore glitches shorter than this

int lastLevel = -1;
unsigned long lastChange = 0;

void setup() {
  pinMode(TTL_PIN, INPUT);   // driven by the TTL source; use INPUT_PULLUP for a switch to GND
  Serial.begin(115200);
}

void loop() {
  int level = digitalRead(TTL_PIN);
  unsigned long now = micros();
  if (level != lastLevel && now - lastChange >= DEBOUNCE_US) {
    lastLevel = level;
    lastChange = now;
    Serial.println(level == HIGH ? "H" : "L");
  }
}
