# ESP32 Wi-Fi IR Remote

Firmware for a small Chinese **ESP32-WROOM-32E** IR board (USB-C / CH340, one IR LED
marked `IR TX`, one IR receiver marked `IR RX`). It learns codes from your existing
remotes and replays them over HTTP, so any phone, shortcut or home-automation tool on
your LAN can control the TV, the air conditioner, and anything else that uses IR.

* Wi-Fi setup through a captive portal (`IR-Remote-Setup`), reachable as **http://irremote.local**
* Learn buttons from any remote (raw timings + decoded protocol), including long AC frames
* Direct AC control (`/api/ac?temp=23&mode=cool…`) once the AC protocol has been learned
* Mobile-first dark web UI served from the chip itself, works with no internet
* Built-in **pin finder** so you don't have to trace the PCB to find the IR GPIOs
* Everything is one sketch: `ir-remote.ino`

---

## 1. Hardware

| Item | Notes |
|---|---|
| Module | ESP32-WROOM-32E, 4 MB flash (N4) |
| USB | USB-C, CH340 serial chip. Auto-reset works on most of these boards. |
| Header | `GND · 5V · RX · TX · IO0 · GND` — no reset / boot buttons |
| IR TX | IR LED driven through a transistor (GPIO unknown → pin finder) |
| IR RX | 38 kHz demodulating receiver (GPIO unknown → pin finder) |
| Mic (later) | MAX4466 analog mic on **GPIO36**, powered from the module's 3.3 V pad — **not implemented yet**, see the `TODO(mic)` at the top of the sketch. GPIO36 is kept free. |

The GPIOs used for the LED and the receiver differ between board batches, so they are
`#define`s at the top of the sketch and default to `-1` (unknown):

```cpp
#define IR_TX_PIN        -1     // GPIO driving the "IR TX" LED transistor
#define IR_RX_PIN        -1     // GPIO connected to the "IR RX" receiver output
#define IR_TX_INVERTED   false  // true if the LED is ON while the pin idles LOW
```

Until both are set, the web UI shows a "pins not set" warning and learning/sending
is disabled, but Wi-Fi, the UI and the pin finder all work.

### No BOOT button?

CH340 boards normally reset into the bootloader automatically. If the upload fails
with *"Failed to connect to ESP32: Wrong boot mode"*, bridge **IO0 → GND** on the
header with a jumper wire while plugging the USB cable in, start the upload, and
remove the jumper after it finishes.

---

## 2. Libraries to install (Arduino IDE → Library Manager)

| Library | Author | Version |
|---|---|---|
| **IRremoteESP8266** | crankyoldgit / David Conran | 2.9.0 or newer (has Arduino-ESP32 core 3.x support built in) |
| **WiFiManager** | tzapu | 2.0.17 or newer |
| **ArduinoJson** | Benoit Blanchon | 7.x |

`WebServer`, `ESPmDNS`, `LittleFS` and `WiFi` ship with the ESP32 core.

## 3. Board settings

Boards Manager: **esp32 by Espressif Systems**, version **3.x**
(the sketch uses the core 3 LEDC API `ledcAttach()`; a core 2.x fallback is included
but 3.x is the target).

| Setting | Value |
|---|---|
| Board | **ESP32 Dev Module** |
| Flash Size | 4MB (32Mb) |
| Partition Scheme | **No OTA (2MB APP / 2MB SPIFFS)** — the full IRremoteESP8266 + IRac build is 1.26 MB, which only just squeezes into the default 1.2 MB app partition; No OTA gives headroom. LittleFS uses the partition labelled `spiffs`. |
| Upload Speed | 921600 (drop to 460800 / 115200 if the CH340 misbehaves) |
| Core Debug Level | None |
| Erase All Flash Before Sketch Upload | Enabled for the very first flash, then Disabled (otherwise every upload wipes your learned buttons and Wi-Fi credentials) |

Open `ir-remote/ir-remote.ino`, select the port, upload.

Verified build (arduino-cli, ESP32 core 3.3.12, IRremoteESP8266 2.9.0, WiFiManager 2.0.17,
ArduinoJson 7.4.2): compiles with no warnings, 1,259,075 bytes of flash, 51 KB static RAM.

### No IDE yet? Flash the prebuilt first-boot image

`firmware/ir-remote-v1.0.0-pins-unset.merged.bin` is the exact build above with the pins
still at `-1`. It is enough for the Wi-Fi portal, the web UI and the **pin finder**; you
still need the IDE for the final flash with the pins filled in. Flash it at offset 0:

```sh
pip install esptool
esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 460800 write_flash 0x0 firmware/ir-remote-v1.0.0-pins-unset.merged.bin
```

Or drag the file into a browser flasher such as https://esp.huhn.me (Chrome/Edge, offset `0x0`).

---

## 4. First boot

1. **Wi-Fi portal.** On first boot the board creates an open access point called
   **`IR-Remote-Setup`**. Join it from your phone; the captive portal opens (or browse
   to `192.168.4.1`). Pick your network, enter the password, save. The board reboots
   and joins your Wi-Fi. Credentials are stored in flash; the portal only reappears if
   the network cannot be reached for 30 s. Watch the serial monitor at 115200 baud to
   see the IP address, or use **http://irremote.local**.

2. **Reserve a fixed IP** for the board in your router (DHCP reservation by MAC).
   mDNS (`irremote.local`) works from iPhones, Macs and most Android phones, but
   shortcuts are more reliable against a fixed IP.

3. **Find the pins.** Open the web UI → **Settings → Pin finder**:
   * Tap **Find RX**, then press keys on *any* remote pointed at the board for
     4 seconds. The firmware samples every safe GPIO and reports which one toggled
     (that's the receiver output). Typical result: hundreds of toggles on one pin,
     near zero on the rest.
   * Tap **Find TX**. The board pulses a 38 kHz burst train on each candidate output
     pin while counting edges on the RX pin; the IR receiver sees the board's own LED,
     so the right pin produces a burst of edges. A pin that shows edges on the receiver
     is your TX pin.
   * The same thing is available as plain HTTP: `GET /api/pinfind/rx` and
     `GET /api/pinfind/tx?rx=<pin>`.
   * Candidate pins are 2, 4, 5, 12–19, 21–23, 25–27, 32–35, 36, 39. Skipped: 0 (boot),
     1/3 (USB serial), 6–11 (flash). 34/35/36/39 are input-only so they are only RX
     candidates. GPIO36 is flagged as reserved for the microphone.

4. **Phone camera check.** Phone cameras see IR: point the camera at the `IR TX` LED
   while the TX finder runs (or while you send a learned button) and you will see it
   flash purple-white. (Front cameras usually show it best; some rear cameras have IR
   filters.) If the LED is lit *all the time* at idle, set `IR_TX_INVERTED true`.

5. **Set the defines and reflash.** Put the two pin numbers into `IR_TX_PIN` and
   `IR_RX_PIN`, upload again (with *Erase All Flash* **disabled** so Wi-Fi and buttons
   survive). The warning banner in the UI disappears.

---

## 5. Learning buttons

Open **http://irremote.local**. There is one tab per device: **TV, AC, Fan, Light** by
default; the **＋** tab adds more (e.g. "Soundbar"). Buttons are laid out in a 3-column
grid like a physical remote.

1. Tap **＋ Learn** on the device tab.
2. Pick a suggested name (Power, Vol +, …) or type your own, tap **Next**.
3. *"Point the remote at the board"* — press the key **once** on the original remote,
   10–30 cm from the `IR RX` window. The receiver is only enabled during this 15 s
   window, so the board never captures its own transmissions.
4. Done: the UI shows the decoded protocol (e.g. `SONY`, 12 bits). Tap **Learn another**
   or **Done**.

The firmware stores the raw timings *and* the decoded protocol/value in
`/buttons.json` (LittleFS). When you send a button:

* known simple protocols are re-encoded from the decoded value, so protocol-specific
  repeats work — **Sony** codes are automatically sent 3× (repeat = 2) as Sony TVs require;
* AC frames and anything the library couldn't decode are replayed from the raw timings;
* add `&raw=1` to `/send` to force a raw replay if a re-encoded code doesn't work.

**Edit mode** (tap *Edit* on a device): tap a key to delete it, copy its ready-made
`/send` URL for a phone shortcut, or send it raw.

### Air conditioner

Learn **any** button from the AC remote (e.g. Power). If IRremoteESP8266 recognises the
protocol as an AC protocol (has AC state), the firmware:

* saves the protocol in `/ac.json` and decodes the frame into a standard AC state
  (power / mode / temperature / fan / swing) — this seeds the AC panel;
* switches the AC tab controls (temp − / +, mode chips, fan speed, swing, on/off) to
  `/api/ac`, which builds a fresh frame with **IRac** — so you can set 23 °C directly
  rather than sending "Temp +" five times.

Until an AC protocol is known, the same controls fall back to learned buttons named
`Power`, `Temp +`, `Temp -`, `Mode`, `Fan`, `Swing` (case/space insensitive).

**Tadiran** remotes almost always speak the **ELECTRA_AC** protocol (Electra is the
OEM behind Tadiran and Tornado), which IRac fully supports. If you see `UNKNOWN` when
learning an AC key, the raw replay still works for that key; you just won't get the
direct temperature control.

---

## 6. HTTP API

All responses are JSON. `dev` / `name` are matched case-insensitively.
Remember to URL-encode: a space is `%20` and **`+` must be written `%2B`**
(`name=Vol%20%2B`), otherwise the server decodes `+` as a space.

| Method & path | What it does |
|---|---|
| `GET /` | Web UI |
| `GET /api/buttons` | List learned buttons (no raw data). `?full=1` returns the whole `/buttons.json` (backup). |
| `POST /api/learn?dev=TV&name=Power` | Start learning (15 s window). Re-learning an existing name overwrites it. |
| `GET /api/learn/status` | `{"status":"idle\|waiting\|done\|timeout\|error", "protocol":…, "remainingMs":…}` |
| `POST /api/learn/cancel` | Abort learning and switch the receiver off |
| `GET /send?dev=TV&name=Power` | Send a learned button (add `&raw=1` to force raw replay) |
| `DELETE /api/button?dev=TV&name=Power` | Delete a button |
| `GET /api/ac?power=on&mode=cool&temp=23&fan=auto&swing=off` | Build and send an AC frame with IRac (needs a learned AC protocol). Every parameter is optional; omitted ones keep their last value. `mode`: cool/heat/dry/fan/auto/off · `fan`: auto/min/low/medium/high/max · `swing`: off/auto/on/highest/high/middle/low/lowest · also `turbo`, `quiet`, `light`, `sleep`. |
| `GET /api/state` | IP, mDNS, RSSI, uptime, heap, configured pins, AC protocol + last AC state, learn status |
| `GET /api/pinfind/rx` | RX pin finder (blocks ~4.5 s, press a remote meanwhile) |
| `GET /api/pinfind/tx?rx=19` | TX pin finder using the given receiver pin (~2 s) |
| `POST /api/restart` | Reboot |

Examples with `curl`:

```sh
curl "http://irremote.local/send?dev=TV&name=Power"
curl "http://irremote.local/send?dev=TV&name=Vol%20%2B"
curl "http://irremote.local/api/ac?power=on&mode=cool&temp=23&fan=auto"
curl "http://irremote.local/api/ac?power=off"
curl -X POST "http://irremote.local/api/learn?dev=AC&name=Power"
curl "http://irremote.local/api/learn/status"
```

Storage on the chip:

* `/buttons.json` — `{"buttons":[{"dev","name","protocol","bits","value","repeat","ac","raw":[…]}]}`
* `/ac.json` — AC protocol and the last state sent (`power, mode, temp, fan, swingv, …`)

`GET /api/buttons?full=1` downloads the full buttons file as a backup
(Settings → *Backup buttons*).

---

## 7. Phone shortcuts

Use the **fixed IP** you reserved in the router (e.g. `192.168.1.50`) rather than
`irremote.local` for shortcuts; it's faster and never fails to resolve.
In edit mode each key has a **Copy URL** button that gives you the correctly encoded
`/send` URL.

### iPhone — Shortcuts app

1. Shortcuts → **＋** → *Add Action* → search **"Get Contents of URL"**.
2. URL: `http://192.168.1.50/send?dev=TV&name=Power` — Method: **GET**.
3. Name it "TV Power", add it to the Home Screen or say "Hey Siri, TV Power".

AC at 23 °C: `http://192.168.1.50/api/ac?power=on&mode=cool&temp=23&fan=auto`
AC off: `http://192.168.1.50/api/ac?power=off`

Tip: an Automation (Shortcuts → Automation) can run these on a schedule or when you
arrive home. Shortcuts with a `Get Contents of URL` action run without opening the app.

### Android — "HTTP Shortcuts" app (free, Play Store / F-Droid)

1. **＋** → *Regular shortcut* → name "TV Power".
2. Method **GET**, URL `http://192.168.1.50/send?dev=TV&name=Power`.
3. Set *Response handling* to "Toast" or "None", then long-press → *Place on home screen*.
   HTTP Shortcuts also exposes them to Tasker / Google Assistant routines.

Home Assistant `rest_command`, Node-RED, Tasker, or any device on the LAN can call the
same URLs.

---

## 8. Ceiling fan with light — IR or RF?

Many ceiling-fan remotes (and most "fan + light" ones) are **433 MHz RF**, not IR,
because they need to work through walls and without line of sight.

**Check with a phone camera:** open the camera, point it at the front of the fan
remote, and press a button. An **IR** remote shows a visible purple-white flashing LED
in the camera image (invisible to the eye). No flash at all → it is almost certainly
**RF**. Other hints: RF remotes have no clear plastic window at the front, and they
work when you point them away from the fan.

If it's IR, just learn its buttons on the **Fan** tab. If it's RF, the board can't
learn it yet: a **433 MHz transmitter module** (e.g. FS1000A, or a CC1101 for
learning) can be added later on a free GPIO and driven with the `rc-switch` library —
the header's `5V`/`GND` and a spare GPIO are all it needs. Keep GPIO36 for the mic.

---

## 9. Brands (Israel) and native support

"Native" = IRremoteESP8266 decodes the protocol. For TVs any brand works by raw replay
regardless; for ACs "native" is what enables the direct temperature/mode control.
The same table is shown in the UI under **Settings → Brands**.

| TV brand | Native | Notes |
|---|---|---|
| Sony | ✅ | `SONY`; codes sent 3× automatically |
| Samsung | ✅ | `SAMSUNG`, `SAMSUNG36` |
| LG | ✅ | `LG`, `LG2` |
| Hisense | ✅ via NEC | uses plain NEC codes |
| TCL | ✅ via NEC | TCL TVs use NEC (`TCL112AC` is their AC protocol) |
| Xiaomi (Mi TV) | ✅ via NEC | IR remotes only; Bluetooth remotes can't be learned |
| Philips | ✅ | `RC5` / `RC6` |
| Toshiba | ✅ via NEC | |

| AC brand | Native (IRac) | Notes |
|---|---|---|
| Tadiran | ✅ | speaks **`ELECTRA_AC`** (Electra OEM) |
| Electra | ✅ | `ELECTRA_AC` |
| Tornado | ✅ | Electra brand → `ELECTRA_AC` |
| Fujitsu | ✅ | `FUJITSU_AC` (several models) |
| LG | ✅ | `LG` / `LG2` AC variants |
| Samsung | ✅ | `SAMSUNG_AC` |
| Mitsubishi Electric | ✅ | `MITSUBISHI_AC`, `MITSUBISHI112`, `MITSUBISHI136` |
| Gree | ✅ | `GREE` (many rebrands) |
| Midea | ✅ | `MIDEA` (Carrier/Comfee rebrands too) |
| Haier | ✅ | `HAIER_AC`, `HAIER_AC_YRW02`, `HAIER_AC176` |
| Daikin | ✅ | `DAIKIN` + many variants |
| Hitachi | ✅ | `HITACHI_AC` + variants |
| Carrier | ◐ partial | `CARRIER_AC64/84/128` via IRac; older 32-bit Carrier remotes raw-only |

---

## 10. Troubleshooting

* **Learn times out** — hold the remote closer (10–30 cm) and aim at the receiver
  window; make sure the pins are set; check the serial monitor for `[learn]` lines.
* **Learned button does nothing** — try *Send raw* from edit mode (`&raw=1`). If raw
  works and the normal send doesn't, the decoded value was lossy; raw is what the
  remote actually sent. Point a phone camera at the LED to confirm it flashes.
* **LED flashes but the device ignores it** — you may be too far (this LED is small);
  some ACs need the full frame which needs the 1024-slot buffer already configured.
* **AC controls change nothing** — verify `/api/state` shows `"ac":{"known":true,…}`.
  If the protocol decoded as `UNKNOWN`, learn each AC key individually instead.
* **`irremote.local` doesn't resolve** — use the IP; on Android without mDNS use the
  fixed IP in shortcuts.
* **Wi-Fi changed** — the board falls back to the `IR-Remote-Setup` portal
  automatically when it cannot connect for 30 s.
* **Buttons vanished after flashing** — *Erase All Flash Before Sketch Upload* was
  enabled; disable it and restore from your `?full=1` backup by re-learning.

## 11. TODO

* **MAX4466 microphone on GPIO36** (`TODO(mic)` in the sketch): clap / sound-level
  triggers. GPIO36 is ADC1_CH0, input-only, and is deliberately left untouched.
* 433 MHz transmitter for RF fan remotes (see section 8).
