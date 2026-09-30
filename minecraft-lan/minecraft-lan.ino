/*
 * minecraft-lan.ino — ESP32 as a stand-alone Wi-Fi access point for LAN gaming
 *
 * Power the board and it creates the Wi-Fi network below. Everyone joins it,
 * one player opens their Minecraft world to LAN, the others join. No router,
 * no internet, nothing else needed.
 *
 * A tiny status page at http://192.168.4.1 shows who is connected (with IPs),
 * which is handy for "Direct Connect" if the LAN world does not show up.
 *
 * Board: any ESP32 dev board. Arduino-ESP32 core 3.x. No extra libraries.
 */

#include <WiFi.h>
#include <WebServer.h>
#include <esp_wifi.h>
#include <esp_wifi_ap_get_sta_list.h>

// ------------------------------------------------------------------
//  Network settings
// ------------------------------------------------------------------
#define AP_SSID        "Minecraft-LAN"
#define AP_PASSWORD    "minecraft"   // 8+ characters, or "" for an open network
#define AP_CHANNEL     6             // 1, 6 or 11; change if it is slow/laggy
#define AP_MAX_CLIENTS 10            // ESP32 hardware limit is 10
#define LED_PIN        2             // on-board LED on most dev boards (-1 = none)

// ------------------------------------------------------------------
WebServer server(80);

String macToString(const uint8_t *m) {
  char buf[18];
  snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(buf);
}

// Fills a JSON array with the connected devices (MAC, IP, RSSI).
String clientsJson() {
  wifi_sta_list_t stas;
  wifi_sta_mac_ip_list_t ips;
  String out = "[";
  if (esp_wifi_ap_get_sta_list(&stas) == ESP_OK && esp_wifi_ap_get_sta_list_with_ip(&stas, &ips) == ESP_OK) {
    for (int i = 0; i < stas.num; i++) {
      if (i) out += ",";
      out += "{\"mac\":\"" + macToString(stas.sta[i].mac) + "\",\"ip\":\"" +
             IPAddress(ips.sta[i].ip.addr).toString() + "\",\"rssi\":" + String(stas.sta[i].rssi) + "}";
    }
  }
  return out + "]";
}

static const char PAGE[] PROGMEM = R"html(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Minecraft LAN</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:20px;max-width:520px}
h1{font-size:22px}table{width:100%;border-collapse:collapse}td,th{padding:8px 4px;border-bottom:1px solid #333;text-align:left}
th{color:#999;font-weight:normal}code{background:#222;padding:2px 6px;border-radius:4px}.muted{color:#999;font-size:14px}</style></head>
<body><h1>&#9635; Minecraft LAN</h1>
<p>Network <b>SSID_PLACEHOLDER</b> &middot; <span id="n">0</span> connected</p>
<table><tr><th>IP</th><th>Signal</th><th>Device</th></tr><tbody id="t"></tbody></table>
<p class="muted">Host: open your world to LAN. Friends: Multiplayer &rarr; the world appears under LAN.
If not, use <b>Direct Connect</b> with the host's IP from this list and the port shown in the host's chat.</p>
<script>
const load=async()=>{try{const r=await fetch('/api/clients');const j=await r.json();document.getElementById('n').textContent=j.length;
document.getElementById('t').innerHTML=j.map(c=>'<tr><td><code>'+c.ip+'</code></td><td>'+c.rssi+' dBm</td><td class="muted">'+c.mac+'</td></tr>').join('')||'<tr><td colspan=3 class="muted">nobody yet</td></tr>'}catch(e){}};
load();setInterval(load,4000);
</script></body></html>)html";

void setup() {
  Serial.begin(115200);
  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);

  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);                      // lowest latency
  bool ok = WiFi.softAP(AP_SSID, strlen(AP_PASSWORD) ? AP_PASSWORD : NULL, AP_CHANNEL, 0, AP_MAX_CLIENTS);
  esp_wifi_set_ps(WIFI_PS_NONE);             // no power saving on the radio

  Serial.printf("\nAP '%s' %s, IP %s, channel %d, max %d clients\n", AP_SSID, ok ? "started" : "FAILED",
                WiFi.softAPIP().toString().c_str(), AP_CHANNEL, AP_MAX_CLIENTS);

  server.on("/", []() {
    String p = FPSTR(PAGE);
    p.replace("SSID_PLACEHOLDER", AP_SSID);
    server.send(200, "text/html", p);
  });
  server.on("/api/clients", []() { server.send(200, "application/json", clientsJson()); });
  server.onNotFound([]() { server.sendHeader("Location", "http://192.168.4.1/"); server.send(302, "text/plain", ""); });
  server.begin();
}

void loop() {
  server.handleClient();

  // LED: solid = network up, short blink every second per connected client count
  if (LED_PIN >= 0) {
    static uint32_t last = 0;
    static int phase = 0;
    int n = WiFi.softAPgetStationNum();
    uint32_t now = millis();
    if (n == 0) {
      digitalWrite(LED_PIN, HIGH);
    } else if (now - last >= 150) {
      last = now;
      phase = (phase + 1) % (2 * n + 6);          // n quick blinks, then a pause
      digitalWrite(LED_PIN, (phase < 2 * n && (phase & 1)) ? LOW : HIGH);
    }
  }
  delay(2);
}
