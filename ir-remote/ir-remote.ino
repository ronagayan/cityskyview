/*
 * ir-remote.ino — Wi-Fi IR remote for a small ESP32-WROOM-32E IR board
 *
 *  - Wi-Fi setup through a WiFiManager captive portal (AP "IR-Remote-Setup")
 *  - mDNS: http://irremote.local
 *  - Learns IR codes (raw timings + decoded protocol) and replays them
 *  - Direct air-conditioner control (temperature, mode, fan, swing) through
 *    IRremoteESP8266's IRac once an AC protocol has been learned
 *  - Mobile-first web UI served from flash, no internet needed
 *  - Built-in pin finder to discover which GPIOs the IR LED / receiver use
 *
 * Target: Arduino-ESP32 core 3.x (uses ledcAttach / ledcWrite / ledcDetach).
 * A fallback for core 2.x (ledcSetup / ledcAttachPin) is kept for convenience.
 *
 * Libraries: IRremoteESP8266, WiFiManager (tzapu), ArduinoJson 7.
 * See README.md for board settings, first-boot steps and the HTTP API.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

#include <IRremoteESP8266.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>
#include <IRac.h>

// =====================================================================
//  PIN CONFIGURATION — run the pin finder (Settings page or
//  /api/pinfind/rx and /api/pinfind/tx) once, then set these and reflash.
//  -1 = unknown (learning / sending is disabled until set).
// =====================================================================
#define IR_TX_PIN        -1     // GPIO driving the "IR TX" LED transistor
#define IR_RX_PIN        -1     // GPIO connected to the "IR RX" receiver output
#define IR_TX_INVERTED   false  // set true if the LED is ON while the pin idles LOW
                                // (phone camera shows the LED lit all the time)

// TODO(mic): a MAX4466 analog microphone will be connected to GPIO36
// (ADC1_CH0, input-only, powered from the module's 3.3 V pad).
// Keep GPIO36 free: nothing in this sketch drives or claims it.
#define MIC_PIN          36

// ---------------------------------------------------------------------
//  General settings
// ---------------------------------------------------------------------
#define FW_VERSION       "1.0.0"
#define HOSTNAME         "irremote"          // -> irremote.local
#define AP_NAME          "IR-Remote-Setup"   // captive-portal SSID
#define BUTTONS_FILE     "/buttons.json"
#define BUTTONS_TMP      "/buttons.tmp"
#define AC_FILE          "/ac.json"

static const uint16_t kCaptureBufferSize = 1024;  // AC frames can be long
static const uint8_t  kCaptureTimeoutMs  = 50;    // gap that ends a frame
static const uint32_t kLearnTimeoutMs    = 15000; // wait this long for a press
static const uint16_t kMinRawLen         = 12;    // shorter captures are noise
static const uint16_t kSonyRepeats       = 2;     // Sony expects 3 sends total
static const uint16_t kRawRepeatGapMs    = 40;    // gap between raw repeats
static const uint32_t kRxFindNoiseMs     = 500;   // baseline before RX scan
static const uint32_t kRxFindScanMs      = 4000;  // press the remote this long
static const uint16_t kTxFindBursts      = 25;    // 1 ms on / 1 ms off bursts

// Candidate GPIOs for the pin finder. Skipped: 0 (boot), 1/3 (UART to the
// CH340), 6..11 (SPI flash). 34/35/36/39 are input-only (RX candidates only).
static const uint8_t kRxCandidates[] = {2, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19,
                                        21, 22, 23, 25, 26, 27, 32, 33,
                                        34, 35, 36, 39};
static const uint8_t kTxCandidates[] = {2, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19,
                                        21, 22, 23, 25, 26, 27, 32, 33};

// ---------------------------------------------------------------------
//  Globals
// ---------------------------------------------------------------------
// Pins are cast to uint16_t; with -1 the objects are constructed but never
// used (every send/receive path checks txReady()/rxReady() first).
IRrecv irrecv((uint16_t)IR_RX_PIN, kCaptureBufferSize, kCaptureTimeoutMs, true);
IRsend irsend((uint16_t)IR_TX_PIN, IR_TX_INVERTED);
IRac   irac((uint16_t)IR_TX_PIN, IR_TX_INVERTED);
decode_results results;

WebServer server(80);
JsonDocument buttonsDoc;          // {"buttons":[{dev,name,protocol,bits,value,repeat,ac,raw[]}]}

stdAc::state_t acState;           // last AC state we sent / decoded
bool acKnown = false;             // true once an IRac-supported protocol is learned
bool rxEnabled = false;           // receiver is only on while learning

enum LearnStatus : uint8_t { LEARN_IDLE, LEARN_WAITING, LEARN_DONE, LEARN_TIMEOUT, LEARN_ERROR };
static const char *const kLearnStatusNames[] = {"idle", "waiting", "done", "timeout", "error"};

struct LearnState {
  LearnStatus status = LEARN_IDLE;
  String   dev;
  String   name;
  String   protocol;
  String   message;
  uint16_t bits = 0;
  uint16_t rawLen = 0;
  bool     isAc = false;
  uint32_t startedAt = 0;
} learn;

volatile uint32_t rxEdges = 0;    // edge counter used by the TX pin finder

// ---------------------------------------------------------------------
//  Small helpers
// ---------------------------------------------------------------------
static inline bool txReady() { return IR_TX_PIN >= 0; }
static inline bool rxReady() { return IR_RX_PIN >= 0; }

static bool inList(const uint8_t *list, size_t n, int pin) {
  for (size_t i = 0; i < n; i++) if (list[i] == pin) return true;
  return false;
}
static bool isInputOnly(int pin) { return pin >= 34; }

void rxOn() {
  if (!rxReady() || rxEnabled) return;
  irrecv.enableIRIn(true);        // pull-up is harmless for open-collector receivers
  rxEnabled = true;
}
void rxOff() {
  if (!rxEnabled) return;         // disableIRIn() must not run before enableIRIn()
  irrecv.disableIRIn();
  rxEnabled = false;
}

void sendJson(int code, JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  server.send(code, "application/json", out);
}
void sendError(int code, const String &msg) {
  JsonDocument d;
  d["ok"] = false;
  d["error"] = msg;
  sendJson(code, d);
}
void sendOk(const String &msg) {
  JsonDocument d;
  d["ok"] = true;
  d["message"] = msg;
  sendJson(200, d);
}

// ---------------------------------------------------------------------
//  Button storage (LittleFS + ArduinoJson 7)
// ---------------------------------------------------------------------
void loadButtons() {
  buttonsDoc.clear();
  File f = LittleFS.open(BUTTONS_FILE, "r");
  if (f) {
    DeserializationError err = deserializeJson(buttonsDoc, f);
    f.close();
    if (err) {
      Serial.printf("[fs] %s is corrupt (%s), starting empty\n", BUTTONS_FILE, err.c_str());
      buttonsDoc.clear();
    }
  }
  if (!buttonsDoc["buttons"].is<JsonArray>()) buttonsDoc["buttons"].to<JsonArray>();
  Serial.printf("[fs] %u buttons loaded\n", (unsigned)buttonsDoc["buttons"].size());
}

bool saveButtons() {
  File f = LittleFS.open(BUTTONS_TMP, "w");
  if (!f) { Serial.println("[fs] cannot open temp file"); return false; }
  serializeJson(buttonsDoc, f);
  f.close();
  LittleFS.remove(BUTTONS_FILE);              // write-then-rename keeps the file whole
  if (!LittleFS.rename(BUTTONS_TMP, BUTTONS_FILE)) { Serial.println("[fs] rename failed"); return false; }
  return true;
}

JsonObject findButton(const String &dev, const String &name) {
  for (JsonObject b : buttonsDoc["buttons"].as<JsonArray>()) {
    if (dev.equalsIgnoreCase(b["dev"] | "") && name.equalsIgnoreCase(b["name"] | "")) return b;
  }
  return JsonObject();
}

bool removeButton(const String &dev, const String &name) {
  JsonArray arr = buttonsDoc["buttons"].as<JsonArray>();
  for (size_t i = 0; i < arr.size(); i++) {
    JsonObject b = arr[i];
    if (dev.equalsIgnoreCase(b["dev"] | "") && name.equalsIgnoreCase(b["name"] | "")) {
      arr.remove(i);
      saveButtons();
      loadButtons();                          // reload to reclaim pool memory
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------
//  AC state storage
// ---------------------------------------------------------------------
void acToJson(JsonObject o) {
  o["protocol"] = typeToString(acState.protocol);
  o["model"]    = acState.model;
  o["power"]    = acState.power;
  o["mode"]     = IRac::opmodeToString(acState.mode);
  o["temp"]     = acState.degrees;
  o["celsius"]  = acState.celsius;
  o["fan"]      = IRac::fanspeedToString(acState.fanspeed);
  o["swingv"]   = IRac::swingvToString(acState.swingv);
  o["swingh"]   = IRac::swinghToString(acState.swingh);
  o["quiet"]    = acState.quiet;
  o["turbo"]    = acState.turbo;
  o["econo"]    = acState.econo;
  o["light"]    = acState.light;
  o["sleep"]    = acState.sleep;
}

void saveAcState() {
  JsonDocument d;
  acToJson(d.to<JsonObject>());
  File f = LittleFS.open(AC_FILE, "w");
  if (!f) { Serial.println("[fs] cannot write ac.json"); return; }
  serializeJson(d, f);
  f.close();
}

void loadAcState() {
  IRac::initState(&acState);
  acState.degrees = 24;
  acState.mode = stdAc::opmode_t::kCool;
  File f = LittleFS.open(AC_FILE, "r");
  if (!f) return;
  JsonDocument d;
  DeserializationError err = deserializeJson(d, f);
  f.close();
  if (err) { Serial.printf("[fs] ac.json corrupt (%s)\n", err.c_str()); return; }
  JsonObject o = d.as<JsonObject>();
  acState.protocol = strToDecodeType(o["protocol"] | "UNKNOWN");
  acState.model    = o["model"] | -1;
  acState.power    = o["power"] | false;
  acState.mode     = IRac::strToOpmode(o["mode"] | "cool", stdAc::opmode_t::kCool);
  acState.degrees  = o["temp"] | 24.0f;
  acState.celsius  = o["celsius"] | true;
  acState.fanspeed = IRac::strToFanspeed(o["fan"] | "auto", stdAc::fanspeed_t::kAuto);
  acState.swingv   = IRac::strToSwingV(o["swingv"] | "off", stdAc::swingv_t::kOff);
  acState.swingh   = IRac::strToSwingH(o["swingh"] | "off", stdAc::swingh_t::kOff);
  acState.quiet    = o["quiet"] | false;
  acState.turbo    = o["turbo"] | false;
  acState.econo    = o["econo"] | false;
  acState.light    = o["light"] | false;
  acState.sleep    = o["sleep"] | -1;
  acKnown = (acState.protocol != decode_type_t::UNKNOWN) && IRac::isProtocolSupported(acState.protocol);
  Serial.printf("[ac] protocol %s (%s)\n", typeToString(acState.protocol).c_str(),
                acKnown ? "IRac supported" : "not supported by IRac");
}

// ---------------------------------------------------------------------
//  Sending
// ---------------------------------------------------------------------
// Replays a stored raw array 1 + repeat times.
bool sendRawFromJson(JsonArray raw, uint16_t khz, uint16_t repeat) {
  uint16_t n = raw.size();
  if (n == 0) return false;
  uint16_t *buf = new (std::nothrow) uint16_t[n];
  if (!buf) return false;
  uint16_t i = 0;
  for (JsonVariant v : raw) buf[i++] = v.as<uint16_t>();
  for (uint16_t r = 0; r <= repeat; r++) {
    irsend.sendRaw(buf, n, khz);
    if (r < repeat) delay(kRawRepeatGapMs);
  }
  delete[] buf;
  return true;
}

// Sends a learned button. Known non-AC protocols are re-encoded from the
// decoded value (so protocol-specific repeats work, e.g. Sony x3); AC frames
// and unknown protocols are replayed from the raw timings. forceRaw=true
// always replays raw (handy when a re-encoded code does not work).
bool sendButton(JsonObject btn, bool forceRaw) {
  if (!txReady()) return false;
  decode_type_t proto = strToDecodeType(btn["protocol"] | "UNKNOWN");
  uint16_t repeat = btn["repeat"] | 0;
  bool isAc = btn["ac"] | false;
  uint16_t khz = (proto == decode_type_t::SONY) ? 40 : 38;

  if (!forceRaw && proto != decode_type_t::UNKNOWN && !isAc) {
    uint64_t value = strtoull(btn["value"] | "0", nullptr, 16);
    uint16_t bits = btn["bits"] | 0;
    if (bits > 0 && irsend.send(proto, value, bits, repeat)) return true;
    // protocol not sendable by the library -> fall through to raw
  }
  return sendRawFromJson(btn["raw"].as<JsonArray>(), khz, repeat);
}

// ---------------------------------------------------------------------
//  Learning
// ---------------------------------------------------------------------
void learnToJson(JsonObject o) {
  o["status"]   = kLearnStatusNames[learn.status];
  o["dev"]      = learn.dev;
  o["name"]     = learn.name;
  o["protocol"] = learn.protocol;
  o["bits"]     = learn.bits;
  o["rawLen"]   = learn.rawLen;
  o["ac"]       = learn.isAc;
  o["message"]  = learn.message;
  uint32_t remaining = 0;
  if (learn.status == LEARN_WAITING) {
    uint32_t elapsed = millis() - learn.startedAt;
    remaining = elapsed < kLearnTimeoutMs ? kLearnTimeoutMs - elapsed : 0;
  }
  o["remainingMs"] = remaining;
}

// Stores the capture in buttonsDoc and, for AC protocols, seeds the AC state.
void storeLearned(const decode_results &r) {
  uint16_t len = getCorrectedRawLength(&r);
  uint16_t *raw = resultToRawArray(&r);      // caller owns the array
  bool isAc = hasACState(r.decode_type);

  JsonObject btn = findButton(learn.dev, learn.name);
  if (btn.isNull()) {
    btn = buttonsDoc["buttons"].as<JsonArray>().add<JsonObject>();
  } else {
    btn.clear();                             // re-learning overwrites
  }
  btn["dev"]      = learn.dev;
  btn["name"]     = learn.name;
  btn["protocol"] = typeToString(r.decode_type);
  btn["bits"]     = r.bits;
  btn["value"]    = uint64ToString(r.value, 16);
  btn["repeat"]   = (r.decode_type == decode_type_t::SONY ||
                     r.decode_type == decode_type_t::SONY_38K) ? kSonyRepeats : 0;
  btn["ac"]       = isAc;
  JsonArray arr = btn["raw"].to<JsonArray>();
  for (uint16_t i = 0; i < len; i++) arr.add(raw[i]);
  delete[] raw;

  learn.protocol = typeToString(r.decode_type);
  learn.bits     = r.bits;
  learn.rawLen   = len;
  learn.isAc     = isAc;
  learn.message  = r.overflow ? "Capture buffer overflowed; the code may be truncated" : "";

  if (!saveButtons()) learn.message = "Learned but could not save to flash";

  if (isAc) {
    // Seed a stdAc state from the remote's frame so /api/ac starts from the
    // real settings, and remember the protocol for IRac.
    stdAc::state_t s;
    IRac::initState(&s);
    if (IRAcUtils::decodeToState(&r, &s, acKnown ? &acState : nullptr)) {
      acState = s;
      acKnown = IRac::isProtocolSupported(acState.protocol);
    } else if (IRac::isProtocolSupported(r.decode_type)) {
      acState.protocol = r.decode_type;
      acKnown = true;
    }
    if (acKnown) {
      saveAcState();
      learn.message += String(learn.message.length() ? " · " : "") +
                       "AC protocol set to " + typeToString(acState.protocol);
    }
  }
  Serial.printf("[learn] %s/%s -> %s, %u bits, %u raw\n", learn.dev.c_str(), learn.name.c_str(),
                learn.protocol.c_str(), r.bits, len);
}

void startLearn(const String &dev, const String &name) {
  learn = LearnState();
  learn.dev = dev;
  learn.name = name;
  learn.status = LEARN_WAITING;
  learn.startedAt = millis();
  rxOn();
  irrecv.resume();                           // drop anything captured before
}

void cancelLearn(const char *why) {
  rxOff();
  if (learn.status == LEARN_WAITING) {
    learn.status = LEARN_ERROR;
    learn.message = why;
  }
}

// Called from loop(): non-blocking, keeps the web server responsive.
void pollLearn() {
  if (learn.status != LEARN_WAITING) return;

  if (irrecv.decode(&results)) {
    // Ignore repeat frames (NEC "held button") and tiny noise blips.
    bool usable = results.rawlen >= kMinRawLen && !results.repeat;
    if (usable) {
      storeLearned(results);
      learn.status = LEARN_DONE;
      rxOff();
      return;
    }
    irrecv.resume();
  }
  if (millis() - learn.startedAt > kLearnTimeoutMs) {
    learn.status = LEARN_TIMEOUT;
    learn.message = "No IR signal received";
    rxOff();
  }
}

// ---------------------------------------------------------------------
//  Pin finder
// ---------------------------------------------------------------------
// Fast-ish poll of all candidate inputs, counting level changes.
static void countToggles(uint32_t durationMs, uint32_t *counts, uint8_t *last, size_t n) {
  uint32_t t0 = millis();
  uint32_t iter = 0;
  while (millis() - t0 < durationMs) {
    for (size_t i = 0; i < n; i++) {
      uint8_t v = digitalRead(kRxCandidates[i]);
      if (v != last[i]) { counts[i]++; last[i] = v; }
    }
    if ((++iter & 0x3FF) == 0) yield();
  }
}

// GET /api/pinfind/rx — samples every candidate for 4 s while the user
// presses a remote; the receiver output pin toggles hundreds of times.
void handlePinFindRx() {
  if (learn.status == LEARN_WAITING) return sendError(409, "Learning in progress");
  rxOff();
  const size_t n = sizeof(kRxCandidates);
  uint32_t noise[sizeof(kRxCandidates)] = {0};
  uint32_t hits[sizeof(kRxCandidates)]  = {0};
  uint8_t  last[sizeof(kRxCandidates)];
  uint8_t  idle[sizeof(kRxCandidates)];

  for (size_t i = 0; i < n; i++) {
    if (txReady() && kRxCandidates[i] == IR_TX_PIN) continue;   // leave the LED driver alone
    pinMode(kRxCandidates[i], isInputOnly(kRxCandidates[i]) ? INPUT : INPUT_PULLUP);
  }
  delay(10);
  for (size_t i = 0; i < n; i++) idle[i] = last[i] = digitalRead(kRxCandidates[i]);

  countToggles(kRxFindNoiseMs, noise, last, n);   // baseline: nothing pressed yet
  countToggles(kRxFindScanMs, hits, last, n);     // user is pressing the remote

  for (size_t i = 0; i < n; i++) {
    if (txReady() && kRxCandidates[i] == IR_TX_PIN) continue;
    pinMode(kRxCandidates[i], INPUT);
  }
  if (txReady()) irsend.begin();                  // make sure the TX pin is an output again

  JsonDocument d;
  d["ok"] = true;
  d["noiseMs"] = kRxFindNoiseMs;
  d["scanMs"] = kRxFindScanMs;
  int best = -1;
  uint32_t bestScore = 0;
  JsonArray arr = d["candidates"].to<JsonArray>();
  for (size_t i = 0; i < n; i++) {
    uint8_t pin = kRxCandidates[i];
    // Scale the noise baseline to the scan window and subtract it.
    uint32_t expectedNoise = noise[i] * (kRxFindScanMs / kRxFindNoiseMs);
    uint32_t score = hits[i] > expectedNoise ? hits[i] - expectedNoise : 0;
    JsonObject o = arr.add<JsonObject>();
    o["pin"] = pin;
    o["idle"] = idle[i];
    o["noise"] = noise[i];
    o["toggles"] = hits[i];
    String note;
    if (txReady() && pin == IR_TX_PIN) { note = "configured IR_TX_PIN (skipped)"; score = 0; }
    else if (pin == MIC_PIN) note = "reserved for the MAX4466 mic (TODO)";
    else if (isInputOnly(pin)) note = "input-only, no pull-up";
    else if (pin == 12) note = "strapping pin";
    o["score"] = score;
    o["note"] = note;
    if (score >= 20 && score > bestScore) { bestScore = score; best = pin; }
  }
  d["best"] = best;
  d["hint"] = best >= 0
      ? String("IR RX looks like GPIO") + best + ". Set #define IR_RX_PIN " + best + " and reflash."
      : String("No pin toggled clearly. Press the remote repeatedly during the 4 s window and try again.");
  sendJson(200, d);
}

static void IRAM_ATTR onRxEdge() { rxEdges++; }

// 38 kHz carrier on/off helpers, core 3.x API with a core 2.x fallback.
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
static bool carrierAttach(uint8_t pin) { return ledcAttach(pin, 38000, 8); }
static void carrierWrite(uint8_t pin, uint32_t duty) { ledcWrite(pin, duty); }
static void carrierDetach(uint8_t pin) { ledcDetach(pin); }
#else
static const uint8_t kFinderLedcChannel = 7;
static bool carrierAttach(uint8_t pin) { ledcSetup(kFinderLedcChannel, 38000, 8); ledcAttachPin(pin, kFinderLedcChannel); return true; }
static void carrierWrite(uint8_t pin, uint32_t duty) { (void)pin; ledcWrite(kFinderLedcChannel, duty); }
static void carrierDetach(uint8_t pin) { ledcDetachPin(pin); }
#endif

// GET /api/pinfind/tx?rx=N — pulses a 38 kHz burst train on each candidate
// output while counting edges on the receiver pin. The receiver sees the
// board's own LED, so the right TX pin produces a burst of edges.
void handlePinFindTx() {
  if (learn.status == LEARN_WAITING) return sendError(409, "Learning in progress");
  int rx = server.hasArg("rx") ? server.arg("rx").toInt() : IR_RX_PIN;
  if (rx < 0 || !inList(kRxCandidates, sizeof(kRxCandidates), rx))
    return sendError(400, "Pass a valid receiver pin, e.g. /api/pinfind/tx?rx=19 (run the RX finder first)");

  rxOff();
  pinMode(rx, isInputOnly(rx) ? INPUT : INPUT_PULLUP);
  rxEdges = 0;
  attachInterrupt(digitalPinToInterrupt(rx), onRxEdge, CHANGE);

  delay(60);
  uint32_t baseline = rxEdges;               // edges with no transmitter active

  JsonDocument d;
  d["ok"] = true;
  d["rx"] = rx;
  d["baseline"] = baseline;
  JsonArray arr = d["candidates"].to<JsonArray>();
  int best = -1;
  uint32_t bestEdges = 0;

  const size_t n = sizeof(kTxCandidates);
  for (size_t i = 0; i < n; i++) {
    uint8_t pin = kTxCandidates[i];
    if (pin == rx) continue;
    JsonObject o = arr.add<JsonObject>();
    o["pin"] = pin;
    if (!carrierAttach(pin)) { o["edges"] = 0; o["note"] = "PWM attach failed"; continue; }
    rxEdges = 0;
    for (uint16_t b = 0; b < kTxFindBursts; b++) {
      carrierWrite(pin, 128);                // 50 % duty = 38 kHz carrier
      delayMicroseconds(1000);
      carrierWrite(pin, 0);
      delayMicroseconds(1000);
    }
    delay(5);
    uint32_t edges = rxEdges;
    carrierWrite(pin, 0);
    carrierDetach(pin);
    pinMode(pin, INPUT);
    o["edges"] = edges;
    if (pin == 12) o["note"] = "strapping pin";
    if (edges >= 10 && edges > baseline * 3 && edges > bestEdges) { bestEdges = edges; best = pin; }
    yield();
  }
  detachInterrupt(digitalPinToInterrupt(rx));
  pinMode(rx, INPUT);
  if (txReady()) irsend.begin();              // restore normal TX output

  d["best"] = best;
  d["hint"] = best >= 0
      ? String("IR TX looks like GPIO") + best + ". Set #define IR_TX_PIN " + best + " and reflash."
      : String("No candidate produced edges on the receiver. Check the RX pin, or point a phone camera at the LED while the finder runs.");
  sendJson(200, d);
}

// ---------------------------------------------------------------------
//  HTTP API handlers
// ---------------------------------------------------------------------
void handleButtons() {
  if (server.hasArg("full")) {
    // Full dump including raw timings (backup). Streamed to avoid a big String.
    server.setContentLength(measureJson(buttonsDoc));
    server.send(200, "application/json", "");
    WiFiClient client = server.client();
    serializeJson(buttonsDoc, client);
    return;
  }
  JsonDocument d;
  JsonArray out = d["buttons"].to<JsonArray>();
  for (JsonObject b : buttonsDoc["buttons"].as<JsonArray>()) {
    JsonObject o = out.add<JsonObject>();
    o["dev"]      = b["dev"];
    o["name"]     = b["name"];
    o["protocol"] = b["protocol"];
    o["bits"]     = b["bits"];
    o["value"]    = b["value"];
    o["repeat"]   = b["repeat"];
    o["ac"]       = b["ac"];
    o["rawLen"]   = b["raw"].size();
  }
  sendJson(200, d);
}

void handleLearnStart() {
  if (!rxReady()) return sendError(400, "IR_RX_PIN is not set. Run the pin finder, set the #define and reflash.");
  if (learn.status == LEARN_WAITING) return sendError(409, "Already learning; wait or cancel first");
  String dev = server.arg("dev");  dev.trim();
  String name = server.arg("name"); name.trim();
  if (dev.isEmpty() || name.isEmpty()) return sendError(400, "dev and name are required");
  if (dev.length() > 32 || name.length() > 32) return sendError(400, "dev/name too long (max 32)");
  startLearn(dev, name);
  JsonDocument d;
  learnToJson(d.to<JsonObject>());
  d["ok"] = true;
  sendJson(200, d);
}

void handleLearnStatus() {
  JsonDocument d;
  learnToJson(d.to<JsonObject>());
  sendJson(200, d);
}

void handleLearnCancel() {
  cancelLearn("Cancelled");
  sendOk("Learning cancelled");
}

void handleSend() {
  if (!txReady()) return sendError(400, "IR_TX_PIN is not set. Run the pin finder, set the #define and reflash.");
  if (learn.status == LEARN_WAITING) return sendError(409, "Learning in progress");
  String dev = server.arg("dev");  dev.trim();
  String name = server.arg("name"); name.trim();
  JsonObject btn = findButton(dev, name);
  if (btn.isNull()) return sendError(404, "Unknown button " + dev + "/" + name);
  bool ok = sendButton(btn, server.hasArg("raw"));
  if (!ok) return sendError(500, "Send failed (no raw data?)");
  sendOk("Sent " + dev + "/" + name);
}

void handleDeleteButton() {
  String dev = server.arg("dev");  dev.trim();
  String name = server.arg("name"); name.trim();
  if (!removeButton(dev, name)) return sendError(404, "Unknown button " + dev + "/" + name);
  sendOk("Deleted " + dev + "/" + name);
}

// GET /api/ac?power=on|off&mode=cool|heat|dry|fan|auto&temp=24&fan=auto|low|medium|high&swing=off|auto
// Parameters are optional; missing ones keep the last value.
void handleAc() {
  if (!txReady()) return sendError(400, "IR_TX_PIN is not set");
  if (learn.status == LEARN_WAITING) return sendError(409, "Learning in progress");
  if (!acKnown) return sendError(409, "No AC protocol learned yet. Learn any button from the AC remote first.");

  stdAc::state_t prev = acState;
  stdAc::state_t next = acState;

  if (server.hasArg("power")) next.power = IRac::strToBool(server.arg("power").c_str(), next.power);
  if (server.hasArg("mode")) {
    String m = server.arg("mode");
    if (m.equalsIgnoreCase("off")) next.power = false;
    else next.mode = IRac::strToOpmode(m.c_str(), next.mode);
  }
  if (server.hasArg("temp")) {
    float t = server.arg("temp").toFloat();
    if (t < 16) t = 16;
    if (t > 30) t = 30;
    next.degrees = t;
  }
  if (server.hasArg("fan"))   next.fanspeed = IRac::strToFanspeed(server.arg("fan").c_str(), next.fanspeed);
  if (server.hasArg("swing")) {
    String s = server.arg("swing");
    if (s.equalsIgnoreCase("on")) next.swingv = stdAc::swingv_t::kAuto;
    else next.swingv = IRac::strToSwingV(s.c_str(), next.swingv);
  }
  if (server.hasArg("turbo")) next.turbo = IRac::strToBool(server.arg("turbo").c_str(), next.turbo);
  if (server.hasArg("quiet")) next.quiet = IRac::strToBool(server.arg("quiet").c_str(), next.quiet);
  if (server.hasArg("light")) next.light = IRac::strToBool(server.arg("light").c_str(), next.light);
  if (server.hasArg("sleep")) next.sleep = server.arg("sleep").toInt();

  if (!irac.sendAc(next, &prev)) return sendError(500, "IRac could not send " + typeToString(next.protocol));
  acState = next;
  saveAcState();

  JsonDocument d;
  d["ok"] = true;
  JsonObject o = d["ac"].to<JsonObject>();
  o["known"] = acKnown;
  acToJson(o);
  sendJson(200, d);
}

void handleState() {
  JsonDocument d;
  d["version"]     = FW_VERSION;
  d["hostname"]    = HOSTNAME;
  d["mdns"]        = HOSTNAME ".local";
  d["ip"]          = WiFi.localIP().toString();
  d["ssid"]        = WiFi.SSID();
  d["rssi"]        = WiFi.RSSI();
  d["connected"]   = WiFi.status() == WL_CONNECTED;
  d["uptimeS"]     = millis() / 1000;
  d["heap"]        = ESP.getFreeHeap();
  d["txPin"]       = IR_TX_PIN;
  d["rxPin"]       = IR_RX_PIN;
  d["txInverted"]  = IR_TX_INVERTED;
  d["micPin"]      = MIC_PIN;
  d["pinsOk"]      = txReady() && rxReady();
  d["buttonCount"] = buttonsDoc["buttons"].size();
  d["fsUsed"]      = LittleFS.usedBytes();
  d["fsTotal"]     = LittleFS.totalBytes();
  JsonObject ac = d["ac"].to<JsonObject>();
  ac["known"] = acKnown;
  acToJson(ac);
  learnToJson(d["learn"].to<JsonObject>());
  sendJson(200, d);
}

void handleRestart() {
  sendOk("Restarting");
  delay(250);
  ESP.restart();
}

void handleNotFound() {
  sendError(404, "Not found: " + server.uri());
}

// ---------------------------------------------------------------------
//  Web UI (single page, served from flash, no external resources)
// ---------------------------------------------------------------------
static const char INDEX_HTML[] PROGMEM = R"rawliteral(<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>IR Remote</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--key:#242934;--key2:#2d3340;--accent:#4f8cff;--ok:#3ecf8e;--warn:#f5b342;--danger:#ff5c5c;--text:#e8eaf0;--muted:#8b92a5;--radius:14px}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
body{padding-bottom:env(safe-area-inset-bottom)}
header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.95);backdrop-filter:blur(8px);padding:calc(10px + env(safe-area-inset-top)) 16px 8px;border-bottom:1px solid #22262f}
header h1{margin:0;font-size:20px;display:flex;align-items:center;gap:8px}
header .sub{font-size:12px;color:var(--muted);margin-top:2px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:var(--danger);display:inline-block}
.dot.ok{background:var(--ok)}
nav{display:flex;gap:8px;overflow-x:auto;padding:10px 16px;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{flex:0 0 auto;background:var(--card);color:var(--text);border:1px solid #2a2f3a;border-radius:999px;padding:8px 14px;font-size:14px}
nav button.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{padding:0 16px 24px;max-width:560px;margin:0 auto}
.card{background:var(--card);border-radius:var(--radius);padding:14px;margin:12px 0}
.head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.head h2{margin:0;font-size:18px}
.acts{display:flex;gap:6px}
button{font:inherit;cursor:pointer}
.sm{background:var(--key);color:var(--text);border:0;border-radius:10px;padding:8px 12px;font-size:13px}
.sm.on{background:var(--accent);color:#fff}
.sm.danger{background:var(--danger);color:#fff}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.key{position:relative;height:60px;border:0;border-radius:var(--radius);background:var(--key);color:var(--text);font-size:15px;font-weight:600;box-shadow:0 2px 0 #12151b;transition:transform .05s,background .1s;overflow:hidden;padding:4px 6px;word-break:break-word}
.key:active,.key.sent{transform:translateY(2px);box-shadow:none;background:var(--accent);color:#fff}
.key .del{position:absolute;top:4px;right:6px;font-size:12px;color:var(--danger);display:none}
.edit .key .del{display:block}
.edit .key{border:1px dashed var(--danger)}
.hint{color:var(--muted);font-size:14px;margin:8px 0}
.warn{background:#3a2e12;color:var(--warn);padding:10px 12px;border-radius:10px;font-size:14px;margin:12px 0}
.ac-temp{display:flex;align-items:center;justify-content:center;gap:18px;margin:8px 0 14px}
.ac-temp .t{font-size:56px;font-weight:700;min-width:120px;text-align:center}
.ac-temp .t small{font-size:20px;color:var(--muted)}
.round{width:64px;height:64px;border-radius:50%;border:0;background:var(--key2);color:var(--text);font-size:30px;font-weight:700}
.round:active{background:var(--accent);color:#fff}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.chip{background:var(--key);color:var(--text);border:0;border-radius:999px;padding:9px 14px;font-size:14px}
.chip.on{background:var(--accent);color:#fff}
.chip.power{background:var(--danger);color:#fff}
.chip.power.on{background:var(--ok);color:#0b2a1c}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:10px 0 4px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:6px 4px;border-bottom:1px solid #262b35;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
tr.best td{color:var(--ok);font-weight:600}
code{background:#0b0d11;padding:2px 6px;border-radius:6px;font-size:13px}
input[type=text],input[type=number]{width:100%;background:#0b0d11;color:var(--text);border:1px solid #2a2f3a;border-radius:10px;padding:12px;font-size:16px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:flex-end;justify-content:center;z-index:20}
.modal.show{display:flex}
.sheet{background:var(--card);width:100%;max-width:560px;border-radius:18px 18px 0 0;padding:18px 16px calc(18px + env(safe-area-inset-bottom))}
.sheet h3{margin:0 0 10px;font-size:18px}
.row{display:flex;gap:8px;margin-top:12px}
.row button{flex:1;padding:14px;border-radius:12px;border:0;background:var(--key);color:var(--text);font-size:15px}
.row button.primary{background:var(--accent);color:#fff}
.spin{width:36px;height:36px;border:4px solid #2a2f3a;border-top-color:var(--accent);border-radius:50%;animation:s 1s linear infinite;margin:14px auto}
@keyframes s{to{transform:rotate(360deg)}}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translateX(-50%);background:#2a2f3a;color:#fff;padding:10px 16px;border-radius:999px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:30;max-width:90%;text-align:center}
.toast.show{opacity:1}
.toast.err{background:var(--danger)}
.ok{color:var(--ok)}.no{color:var(--warn)}
pre{white-space:pre-wrap;font-size:13px;background:#0b0d11;padding:10px;border-radius:10px}
</style></head>
<body>
<header><h1>📡 IR Remote</h1><div class="sub" id="sub">connecting…</div></header>
<nav id="tabs"></nav>
<main id="main"></main>
<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>
<div class="toast" id="toast"></div>
<script>
(()=>{
const $=s=>document.querySelector(s);
const DEF=['TV','AC','Fan','Light'];
const KEYS={
 TV:['Power','Vol +','Vol -','Ch +','Ch -','Mute','Input','Menu','OK','Up','Down','Left','Right','Back','Home','Netflix','1','2','3','4','5','6','7','8','9','0'],
 AC:['Power','Temp +','Temp -','Mode','Fan','Swing','Timer','Turbo','Sleep'],
 Fan:['Power','Speed +','Speed -','Light','Timer','Reverse','Natural'],
 Light:['On','Off','Bright +','Bright -','Warm','Cool','Color'],
 '*':['Power','On','Off','Up','Down','Left','Right','OK','Menu','Back','+','-']};
const BRANDS={
 'TV':[
  ['Sony','Yes','SONY protocol (12/15/20-bit). Sony wants each code sent 3 times; the firmware does this automatically.'],
  ['Samsung','Yes','SAMSUNG / SAMSUNG36 protocols.'],
  ['LG','Yes','LG / LG2 protocols.'],
  ['Hisense','Via NEC','Most Hisense TVs use plain NEC codes, which decode and replay fine.'],
  ['TCL','Via NEC','TCL TVs generally use NEC codes (TCL112AC is only for their ACs).'],
  ['Xiaomi (Mi TV)','Via NEC','Mi TV remotes use NEC; works. Bluetooth remotes cannot be learned.'],
  ['Philips','Yes','RC5 / RC6 protocols.'],
  ['Toshiba','Via NEC','Toshiba TVs use NEC-style codes.']],
 'Air conditioners':[
  ['Tadiran','Yes','Tadiran units almost always speak the ELECTRA_AC protocol (Electra is the OEM). Full IRac control: temp, mode, fan, swing.'],
  ['Electra','Yes','ELECTRA_AC.'],
  ['Tornado','Yes','Tornado is an Electra brand; usually ELECTRA_AC.'],
  ['Fujitsu','Yes','FUJITSU_AC (several models).'],
  ['LG','Yes','LG / LG2 AC protocols.'],
  ['Samsung','Yes','SAMSUNG_AC.'],
  ['Mitsubishi Electric','Yes','MITSUBISHI_AC, MITSUBISHI112, MITSUBISHI136.'],
  ['Gree','Yes','GREE (also used by many rebrands).'],
  ['Midea','Yes','MIDEA (also used by Carrier/Comfee rebrands).'],
  ['Haier','Yes','HAIER_AC, HAIER_AC_YRW02, HAIER_AC176.'],
  ['Daikin','Yes','DAIKIN and many variants (DAIKIN2/64/128/152/160/176/200/216).'],
  ['Hitachi','Yes','HITACHI_AC and variants (1/264/296/344/424).'],
  ['Carrier','Partial','CARRIER_AC64 / AC84 / AC128 have IRac support; older 32-bit Carrier remotes can only be replayed raw.']]};
const st={buttons:[],custom:JSON.parse(localStorage.getItem('irDevices')||'[]'),sel:localStorage.getItem('irSel')||'TV',edit:false,sys:null,
 ac:{power:false,mode:'cool',temp:24,fan:'auto',swingv:'off'},pin:{rx:null,tx:null}};
let toastT=null,pollT=null;

async function api(p,o){const r=await fetch(p,o);let j=null;try{j=await r.json()}catch(e){}if(!r.ok)throw new Error((j&&j.error)||('HTTP '+r.status));return j}
function toast(m,err){const t=$('#toast');t.textContent=m;t.className='toast show'+(err?' err':'');clearTimeout(toastT);toastT=setTimeout(()=>t.className='toast',2200)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function norm(s){return String(s).toLowerCase().replace(/[^a-z0-9+-]/g,'')}
function devices(){const d=[...DEF];st.custom.forEach(x=>{if(!d.some(y=>y.toLowerCase()===x.toLowerCase()))d.push(x)});st.buttons.forEach(b=>{if(!d.some(y=>y.toLowerCase()===b.dev.toLowerCase()))d.push(b.dev)});return d}
function btnsOf(dev){return st.buttons.filter(b=>b.dev.toLowerCase()===dev.toLowerCase())}
function acKnown(){return !!(st.sys&&st.sys.ac&&st.sys.ac.known)}
function fmtUp(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+h+'h '+m+'m'}

async function refresh(){
 try{
  const b=await api('/api/buttons');st.buttons=b.buttons||[];
  const s=await api('/api/state');st.sys=s;
  if(s.ac&&s.ac.known){st.ac={power:!!s.ac.power,mode:String(s.ac.mode||'cool').toLowerCase(),temp:Math.round(s.ac.temp||24),fan:String(s.ac.fan||'auto').toLowerCase(),swingv:String(s.ac.swingv||'off').toLowerCase()};if(st.ac.mode==='off')st.ac.mode='cool'}
  $('#sub').innerHTML='<span class="dot '+(s.connected?'ok':'')+'"></span> '+esc(s.mdns)+' · '+esc(s.ip)+(s.pinsOk?'':' · <span class="no">pins not set</span>');
 }catch(e){$('#sub').innerHTML='<span class="dot"></span> '+esc(e.message)}
 render();
}

function render(){
 const devs=devices();
 if(st.sel!=='settings'&&!devs.some(d=>d.toLowerCase()===st.sel.toLowerCase()))st.sel='TV';
 localStorage.setItem('irSel',st.sel);
 $('#tabs').innerHTML=devs.map(d=>'<button data-d="'+esc(d)+'" class="'+(d.toLowerCase()===st.sel.toLowerCase()?'on':'')+'">'+esc(d)+'</button>').join('')
  +'<button data-d="+add">＋</button><button data-d="settings" class="'+(st.sel==='settings'?'on':'')+'">⚙︎ Settings</button>';
 $('#tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{const d=b.dataset.d;if(d==='+add')return addDevice();st.sel=d;st.edit=false;render()});
 const m=$('#main');
 if(st.sel==='settings')return renderSettings(m);
 let html='';
 if(st.sys&&!st.sys.pinsOk)html+='<div class="warn">IR pins are not configured yet. Open <b>Settings → Pin finder</b>, then set IR_TX_PIN / IR_RX_PIN in the sketch and reflash.</div>';
 if(st.sel.toLowerCase()==='ac')html+=renderAC();
 html+=renderDevice(st.sel);
 m.innerHTML=html;
 bindDevice(st.sel);
}

function renderDevice(dev){
 const list=btnsOf(dev),custom=!DEF.some(d=>d.toLowerCase()===dev.toLowerCase());
 let h='<section class="card'+(st.edit?' edit':'')+'"><div class="head"><h2>'+(dev.toLowerCase()==='ac'?'Learned AC buttons':esc(dev))+'</h2><div class="acts">'
  +'<button class="sm" id="learnBtn">＋ Learn</button><button class="sm'+(st.edit?' on':'')+'" id="editBtn">'+(st.edit?'Done':'Edit')+'</button></div></div>';
 if(list.length){h+='<div class="grid">'+list.map(b=>'<button class="key" data-n="'+esc(b.name)+'" title="'+esc(b.protocol)+'">'+esc(b.name)+'<span class="del">✕</span></button>').join('')+'</div>'}
 else h+='<p class="hint">No buttons yet. Tap <b>Learn</b>, name the button, then press it on the original remote while pointing at the board.</p>';
 if(st.edit)h+='<p class="hint">Tap a key to delete it or copy its /send URL.'+(custom&&!list.length?' <button class="sm danger" id="delDev">Remove device</button>':'')+'</p>';
 return h+'</section>';
}

function bindDevice(dev){
 const lb=$('#learnBtn'),eb=$('#editBtn'),dd=$('#delDev');
 if(lb)lb.onclick=()=>learnFlow(dev);
 if(eb)eb.onclick=()=>{st.edit=!st.edit;render()};
 if(dd)dd.onclick=()=>{st.custom=st.custom.filter(d=>d.toLowerCase()!==dev.toLowerCase());localStorage.setItem('irDevices',JSON.stringify(st.custom));st.sel='TV';st.edit=false;render()};
 document.querySelectorAll('.key').forEach(k=>k.onclick=()=>st.edit?keyMenu(dev,k.dataset.n):sendBtn(dev,k.dataset.n,k));
 if(dev.toLowerCase()==='ac')bindAC();
}

async function sendBtn(dev,name,el,raw){
 if(el){el.classList.add('sent');setTimeout(()=>el.classList.remove('sent'),250)}
 try{await api('/send?dev='+encodeURIComponent(dev)+'&name='+encodeURIComponent(name)+(raw?'&raw=1':''))}catch(e){toast(e.message,true)}
}

function keyMenu(dev,name){
 const url=location.origin+'/send?dev='+encodeURIComponent(dev)+'&name='+encodeURIComponent(name);
 sheet('<h3>'+esc(dev)+' / '+esc(name)+'</h3><p class="hint">Shortcut URL:</p><pre>'+esc(url)+'</pre>'
  +'<div class="row"><button id="mCopy">Copy URL</button><button id="mRaw">Send raw</button></div><div class="row"><button id="mDel" style="background:var(--danger);color:#fff">Delete</button><button id="mClose">Close</button></div>');
 $('#mCopy').onclick=async()=>{try{await navigator.clipboard.writeText(url);toast('Copied')}catch(e){prompt('Copy this URL',url)}};
 $('#mRaw').onclick=()=>{sendBtn(dev,name,null,true);toast('Sent raw timings')};
 $('#mClose').onclick=closeSheet;
 $('#mDel').onclick=async()=>{if(!confirm('Delete "'+name+'" from '+dev+'?'))return;try{await api('/api/button?dev='+encodeURIComponent(dev)+'&name='+encodeURIComponent(name),{method:'DELETE'});toast('Deleted');closeSheet();refresh()}catch(e){toast(e.message,true)}};
}

function addDevice(){
 const n=(prompt('New device name (e.g. Soundbar):')||'').trim();
 if(!n)return;if(n.length>32)return toast('Name too long',true);
 if(!devices().some(d=>d.toLowerCase()===n.toLowerCase())){st.custom.push(n);localStorage.setItem('irDevices',JSON.stringify(st.custom))}
 st.sel=n;st.edit=false;render();
}

/* ---------- Learn flow ---------- */
function sheet(html){$('#sheet').innerHTML=html;$('#modal').classList.add('show')}
function closeSheet(){$('#modal').classList.remove('show');clearInterval(pollT);pollT=null}
$('#modal').onclick=e=>{if(e.target.id==='modal'){if(pollT)fetch('/api/learn/cancel',{method:'POST'});closeSheet()}};

function learnFlow(dev,preset){
 const sug=(KEYS[dev]||KEYS['*']).filter(k=>!btnsOf(dev).some(b=>b.name.toLowerCase()===k.toLowerCase()));
 sheet('<h3>Learn a button for '+esc(dev)+'</h3><input type="text" id="lname" placeholder="Button name (e.g. Power)" maxlength="32" value="'+esc(preset||'')+'">'
  +(sug.length?'<div class="chips">'+sug.map(k=>'<button class="chip" data-k="'+esc(k)+'">'+esc(k)+'</button>').join('')+'</div>':'')
  +'<div class="row"><button id="lCancel">Cancel</button><button class="primary" id="lNext">Next</button></div>');
 document.querySelectorAll('#sheet .chip').forEach(c=>c.onclick=()=>{$('#lname').value=c.dataset.k;startLearn(dev,c.dataset.k)});
 $('#lCancel').onclick=closeSheet;
 $('#lNext').onclick=()=>{const n=$('#lname').value.trim();if(!n)return toast('Enter a name',true);startLearn(dev,n)};
 setTimeout(()=>{const i=$('#lname');if(i&&!preset)i.focus()},50);
}

async function startLearn(dev,name){
 const exists=btnsOf(dev).some(b=>b.name.toLowerCase()===name.toLowerCase());
 if(exists&&!confirm('"'+name+'" already exists. Re-learn it?'))return;
 try{await api('/api/learn?dev='+encodeURIComponent(dev)+'&name='+encodeURIComponent(name),{method:'POST'})}catch(e){return toast(e.message,true)}
 sheet('<h3>Point the remote at the board</h3><p>Press <b>'+esc(name)+'</b> on the '+esc(dev)+' remote once.</p><div class="spin"></div><p class="hint" id="lCnt" style="text-align:center">15 s</p>'
  +'<div class="row"><button id="lCancel">Cancel</button></div>');
 $('#lCancel').onclick=async()=>{try{await fetch('/api/learn/cancel',{method:'POST'})}catch(e){}closeSheet()};
 clearInterval(pollT);
 pollT=setInterval(async()=>{
  let s;try{s=await api('/api/learn/status')}catch(e){return}
  if(s.status==='waiting'){const c=$('#lCnt');if(c)c.textContent=Math.ceil((s.remainingMs||0)/1000)+' s';return}
  clearInterval(pollT);pollT=null;
  if(s.status==='done'){
   sheet('<h3>✅ Learned '+esc(name)+'</h3><p>Protocol: <b>'+esc(s.protocol)+'</b>'+(s.bits?' · '+s.bits+' bits':'')+' · '+s.rawLen+' raw marks'+(s.ac?' · <span class="ok">AC state decoded</span>':'')+'</p>'
    +(s.message?'<p class="hint">'+esc(s.message)+'</p>':'')+'<div class="row"><button id="lAgain">Learn another</button><button class="primary" id="lDone">Done</button></div>');
   $('#lAgain').onclick=()=>{refresh();learnFlow(dev)};$('#lDone').onclick=()=>{closeSheet();refresh()};
  }else{
   sheet('<h3>⏱ Nothing received</h3><p class="hint">'+esc(s.message||'No IR signal was seen in 15 seconds.')+' Hold the remote 10–30 cm from the IR RX window and press the key once.</p>'
    +'<div class="row"><button id="lClose">Close</button><button class="primary" id="lRetry">Try again</button></div>');
   $('#lClose').onclick=closeSheet;$('#lRetry').onclick=()=>startLearn(dev,name);
  }
 },500);
}

/* ---------- AC ---------- */
function renderAC(){
 const a=st.ac,k=acKnown();
 const modes=['cool','heat','dry','fan','auto'],fans=['auto','low','medium','high'];
 let h='<section class="card"><div class="head"><h2>Air conditioner</h2><span class="hint" style="margin:0">'+(k?'<span class="ok">'+esc(st.sys.ac.protocol)+'</span>':'<span class="no">no protocol yet</span>')+'</span></div>';
 if(!k)h+='<p class="hint">Learn any button from the AC remote (e.g. Power). If the protocol is recognised, these controls will set temperature and mode directly. Until then they fall back to learned buttons named <code>Power</code>, <code>Temp +</code>, <code>Temp -</code>, <code>Mode</code>, <code>Fan</code>, <code>Swing</code>.</p>';
 h+='<div class="ac-temp"><button class="round" id="acDn">−</button><div class="t">'+a.temp+'<small>°C</small></div><button class="round" id="acUp">+</button></div>';
 h+='<div class="chips"><button class="chip power'+(a.power?' on':'')+'" id="acPow">'+(a.power?'ON':'OFF')+'</button><button class="chip'+(a.swingv!=='off'?' on':'')+'" id="acSw">Swing</button></div>';
 h+='<div class="label">Mode</div><div class="chips">'+modes.map(m=>'<button class="chip'+(a.mode===m?' on':'')+'" data-m="'+m+'">'+m+'</button>').join('')+'</div>';
 h+='<div class="label">Fan</div><div class="chips">'+fans.map(f=>'<button class="chip'+(a.fan===f?' on':'')+'" data-f="'+f+'">'+f+'</button>').join('')+'</div>';
 return h+'</section>';
}
function bindAC(){
 $('#acUp').onclick=()=>acAct('tempUp',{temp:Math.min(30,st.ac.temp+1)});
 $('#acDn').onclick=()=>acAct('tempDown',{temp:Math.max(16,st.ac.temp-1)});
 $('#acPow').onclick=()=>acAct('power',{power:!st.ac.power});
 $('#acSw').onclick=()=>acAct('swing',{swingv:st.ac.swingv==='off'?'auto':'off'});
 document.querySelectorAll('[data-m]').forEach(b=>b.onclick=()=>acAct('mode',{mode:b.dataset.m,power:true}));
 document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>acAct('fan',{fan:b.dataset.f}));
}
async function acAct(action,patch){
 if(acKnown()){
  Object.assign(st.ac,patch);
  const q=new URLSearchParams({power:st.ac.power?'on':'off',mode:st.ac.mode,temp:st.ac.temp,fan:st.ac.fan,swing:st.ac.swingv});
  render();
  try{await api('/api/ac?'+q.toString());toast('AC '+(st.ac.power?'on · '+st.ac.mode+' · '+st.ac.temp+'°C':'off'))}catch(e){toast(e.message,true)}
  return;
 }
 // Fallback: drive the AC with learned buttons.
 const names={power:['Power','On/Off',patch.power?'On':'Off'],tempUp:['Temp +','Temp+','Up','Temp Up'],tempDown:['Temp -','Temp-','Down','Temp Down'],
  mode:[patch.mode,'Mode'],fan:[patch.fan+' fan','Fan','Fan Speed'],swing:['Swing']}[action];
 const list=btnsOf('AC'),hit=names.map(n=>list.find(b=>norm(b.name)===norm(n))).find(Boolean);
 if(!hit)return toast('No AC protocol yet — learn a button named "'+names[0]+'"',true);
 Object.assign(st.ac,patch);render();
 sendBtn('AC',hit.name,null);toast('Sent '+hit.name);
}

/* ---------- Settings ---------- */
function renderSettings(m){
 const s=st.sys||{};
 let h='<section class="card"><div class="head"><h2>Device</h2><button class="sm" id="reload">Refresh</button></div><table>'
  +'<tr><th>Address</th><td>http://'+esc(s.mdns||'irremote.local')+' · '+esc(s.ip||'?')+'</td></tr>'
  +'<tr><th>Wi-Fi</th><td>'+esc(s.ssid||'?')+' ('+esc(s.rssi||'?')+' dBm)</td></tr>'
  +'<tr><th>Pins</th><td>TX = '+(s.txPin>=0?'GPIO'+s.txPin:'<span class="no">not set</span>')+' · RX = '+(s.rxPin>=0?'GPIO'+s.rxPin:'<span class="no">not set</span>')+' · mic (TODO) = GPIO'+esc(s.micPin||36)+'</td></tr>'
  +'<tr><th>AC protocol</th><td>'+(s.ac&&s.ac.known?'<span class="ok">'+esc(s.ac.protocol)+'</span>':'<span class="no">none learned</span>')+'</td></tr>'
  +'<tr><th>Buttons</th><td>'+esc(s.buttonCount||0)+' · flash '+Math.round((s.fsUsed||0)/1024)+' / '+Math.round((s.fsTotal||0)/1024)+' KB</td></tr>'
  +'<tr><th>Uptime</th><td>'+fmtUp(s.uptimeS||0)+' · heap '+Math.round((s.heap||0)/1024)+' KB · v'+esc(s.version||'?')+'</td></tr></table>'
  +'<div class="row"><button id="backup">Backup buttons (JSON)</button><button id="restart" style="background:var(--danger);color:#fff">Restart</button></div></section>';
 h+='<section class="card"><div class="head"><h2>Pin finder</h2></div>'
  +'<p class="hint">1. Tap <b>Find RX</b>, then press keys on any remote pointed at the board for 4 seconds.<br>2. Tap <b>Find TX</b>: the board pulses 38 kHz on each candidate pin and listens on the RX pin for its own LED.<br>3. Put the pins into <code>IR_TX_PIN</code> / <code>IR_RX_PIN</code> at the top of the sketch and reflash. A phone camera pointed at the IR LED shows it flashing purple-white.</p>'
  +'<div class="row"><button class="primary" id="findRx">Find RX</button></div>'
  +'<div class="row" style="align-items:center"><input type="number" id="rxIn" placeholder="RX pin" value="'+(st.pin.rx??(s.rxPin>=0?s.rxPin:''))+'" style="flex:1"><button class="primary" id="findTx" style="flex:2">Find TX (uses RX pin)</button></div>'
  +'<div id="pinOut"></div></section>';
 h+='<section class="card"><div class="head"><h2>Brands (Israel)</h2></div><p class="hint">Native support means IRremoteESP8266 decodes the protocol; for ACs it also means the temperature / mode controls work directly. Anything else still works by raw replay of learned buttons.</p>';
 for(const g in BRANDS){h+='<div class="label">'+esc(g)+'</div><table>'+BRANDS[g].map(r=>'<tr><td><b>'+esc(r[0])+'</b></td><td class="'+(r[1]==='Yes'?'ok':'no')+'">'+esc(r[1])+'</td><td class="hint" style="margin:0">'+esc(r[2])+'</td></tr>').join('')+'</table>'}
 h+='</section>';
 m.innerHTML=h;
 $('#reload').onclick=refresh;
 $('#backup').onclick=()=>{location.href='/api/buttons?full=1'};
 $('#restart').onclick=async()=>{if(!confirm('Restart the board?'))return;try{await api('/api/restart',{method:'POST'});toast('Restarting…')}catch(e){toast(e.message,true)}setTimeout(refresh,6000)};
 $('#findRx').onclick=async()=>{
  if(!confirm('After OK, keep pressing buttons on a remote pointed at the board for 4 seconds.'))return;
  $('#pinOut').innerHTML='<div class="spin"></div><p class="hint" style="text-align:center">Press the remote now…</p>';
  try{const r=await api('/api/pinfind/rx');st.pin.rx=r.best>=0?r.best:null;
   $('#pinOut').innerHTML='<p><b>'+esc(r.hint)+'</b></p><table><tr><th>GPIO</th><th>idle</th><th>noise</th><th>toggles</th><th>note</th></tr>'+r.candidates.sort((a,b)=>b.score-a.score).map(c=>'<tr class="'+(c.pin===r.best?'best':'')+'"><td>'+c.pin+'</td><td>'+c.idle+'</td><td>'+c.noise+'</td><td>'+c.toggles+'</td><td class="hint" style="margin:0">'+esc(c.note||'')+'</td></tr>').join('')+'</table>';
   if(r.best>=0)$('#rxIn').value=r.best;
  }catch(e){$('#pinOut').innerHTML='<p class="no">'+esc(e.message)+'</p>'}
 };
 $('#findTx').onclick=async()=>{
  const rx=parseInt($('#rxIn').value);if(isNaN(rx))return toast('Enter the RX pin first',true);
  $('#pinOut').innerHTML='<div class="spin"></div><p class="hint" style="text-align:center">Pulsing each candidate…</p>';
  try{const r=await api('/api/pinfind/tx?rx='+rx);st.pin.tx=r.best>=0?r.best:null;
   $('#pinOut').innerHTML='<p><b>'+esc(r.hint)+'</b></p><p class="hint">Baseline edges without TX: '+r.baseline+'</p><table><tr><th>GPIO</th><th>edges seen on RX</th><th>note</th></tr>'+r.candidates.sort((a,b)=>b.edges-a.edges).map(c=>'<tr class="'+(c.pin===r.best?'best':'')+'"><td>'+c.pin+'</td><td>'+c.edges+'</td><td class="hint" style="margin:0">'+esc(c.note||'')+'</td></tr>').join('')+'</table>'
    +(r.best>=0?'<pre>#define IR_TX_PIN '+r.best+'\n#define IR_RX_PIN '+rx+'</pre>':'');
  }catch(e){$('#pinOut').innerHTML='<p class="no">'+esc(e.message)+'</p>'}
 };
}

refresh();
setInterval(()=>{if(st.sel==='settings'||!st.sys)return;api('/api/state').then(s=>{st.sys=s}).catch(()=>{})},30000);
})();
</script>
</body></html>)rawliteral";

// ---------------------------------------------------------------------
//  Wi-Fi
// ---------------------------------------------------------------------
void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setSleep(false);                       // snappier HTTP responses

  WiFiManager wm;
  wm.setHostname(HOSTNAME);
  wm.setConnectTimeout(30);                   // seconds to try saved credentials
  wm.setConfigPortalTimeout(300);             // portal closes after 5 min, then we reboot & retry
  wm.setWiFiAutoReconnect(true);
  wm.setTitle("IR Remote");

  Serial.printf("[wifi] connecting (portal SSID '%s' if no credentials)\n", AP_NAME);
  if (!wm.autoConnect(AP_NAME)) {
    Serial.println("[wifi] portal timed out, restarting");
    delay(1000);
    ESP.restart();
  }
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  Serial.printf("[wifi] connected to %s, IP %s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());

  if (MDNS.begin(HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("[mdns] http://%s.local\n", HOSTNAME);
  } else {
    Serial.println("[mdns] failed to start");
  }
}

// Reconnect if the link drops; reboot if it stays down for 5 minutes.
void wifiWatchdog() {
  static uint32_t lastOk = millis();
  static uint32_t lastTry = 0;
  if (WiFi.status() == WL_CONNECTED) { lastOk = millis(); return; }
  if (millis() - lastTry > 15000) {
    lastTry = millis();
    Serial.println("[wifi] link down, reconnecting");
    WiFi.reconnect();
  }
  if (millis() - lastOk > 5UL * 60UL * 1000UL) {
    Serial.println("[wifi] down for 5 min, restarting");
    ESP.restart();
  }
}

// ---------------------------------------------------------------------
//  setup / loop
// ---------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n[boot] IR Remote v%s, TX=%d RX=%d\n", FW_VERSION, IR_TX_PIN, IR_RX_PIN);

  if (!LittleFS.begin(true)) Serial.println("[fs] LittleFS mount failed");
  loadButtons();
  loadAcState();

  if (txReady()) irsend.begin();              // sets the LED pin as output, idle level off
  // The receiver stays off until a learn request arrives so it never
  // captures our own transmissions.

  setupWifi();

  server.on("/",                 HTTP_GET,    []() { server.send_P(200, "text/html", INDEX_HTML); });
  server.on("/api/buttons",      HTTP_GET,    handleButtons);
  server.on("/api/learn",        HTTP_POST,   handleLearnStart);
  server.on("/api/learn/status", HTTP_GET,    handleLearnStatus);
  server.on("/api/learn/cancel", HTTP_POST,   handleLearnCancel);
  server.on("/send",             HTTP_GET,    handleSend);
  server.on("/api/button",       HTTP_DELETE, handleDeleteButton);
  server.on("/api/ac",           HTTP_GET,    handleAc);
  server.on("/api/state",        HTTP_GET,    handleState);
  server.on("/api/pinfind/rx",   HTTP_GET,    handlePinFindRx);
  server.on("/api/pinfind/tx",   HTTP_GET,    handlePinFindTx);
  server.on("/api/restart",      HTTP_POST,   handleRestart);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.println("[http] server started on port 80");
}

void loop() {
  server.handleClient();
  pollLearn();
  wifiWatchdog();
  delay(1);
}
