# Minecraft LAN Wi-Fi box (ESP32)

Power the ESP32 and it broadcasts its own Wi-Fi network. Everyone joins it, one
player opens their world to LAN, the others join. No router or internet needed.

| Setting | Value |
|---|---|
| Wi-Fi name | `Minecraft-LAN` |
| Password | `minecraft` |
| Status page | http://192.168.4.1 (shows who is connected, with IPs) |
| Max players | 10 devices (ESP32 hardware limit) |

Change the name/password/channel at the top of `minecraft-lan.ino` if you want.

## Flash it (2 minutes, no software to install)

1. Plug the ESP32 into a computer with a **data** USB cable (USB-A port, try both plug orientations on USB-C boards).
2. Open **https://esp.huhn.me** in Chrome or Edge, click **Connect**, pick the board's serial port.
3. Click **Erase** to wipe whatever is on the board.
4. Click **Add**, choose `firmware/minecraft-lan.merged.bin`, set the offset to `0x0`, click **Program**.
5. Unplug, plug into any USB charger or power bank. The board's blue LED turns on
   solid when the network is up and blinks once per connected player.

Command-line alternative:

```sh
pip install esptool
esptool.py --chip esp32 --port COM5 erase_flash
esptool.py --chip esp32 --port COM5 --baud 460800 write_flash 0x0 firmware/minecraft-lan.merged.bin
```

Building from source instead: Arduino IDE, board **ESP32 Dev Module**, no libraries
needed (verified with ESP32 core 3.3.12, 928 KB, no warnings).

## Playing

**Java Edition**: host presses Esc → *Open to LAN* → *Start LAN World*. Friends: *Multiplayer*,
the world appears under *LAN* after a few seconds. If it doesn't, *Direct Connect* to the
host's IP (see http://192.168.4.1) with the port shown in the host's chat, e.g. `192.168.4.2:52346`.
Java needs each player to have logged into their account once before (the network has no internet).

**Bedrock** (phones, Windows): host opens the world with *Visible to LAN players* on. Friends:
*Play* → *Friends* tab → *LAN Games*. Consoles (Xbox/PlayStation/Switch) need Xbox Live
online for multiplayer and will not work on this offline network.

## Notes

- The network has no internet, only the players talk to each other.
- ESP32 Wi-Fi tops out around 15–20 Mbit/s total, plenty for Minecraft with 10 players.
- If it lags, set `AP_CHANNEL` to 1 or 11 and reflash, or keep the box central to everyone.
