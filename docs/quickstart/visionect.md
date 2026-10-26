# Quickstart: Visionect signs

Visionect's e-paper signs (the 32" Place & Play, Joan, the 13" boards) are thin clients. The sign dials a server address stored in its flash on TCP port 11113 and draws whatever framebuffer the server pushes. Normally that server is the Visionect Software Suite (VSS), which needs a paid licence from version 7.7. Tesserae can be that server instead: it listens on 11113 and speaks the sign's protocol directly, through [pyvisionect](https://github.com/schlarpc/pyvisionect). No VSS, no Visionect account, no flashing.

!!! note "Tested on one sign so far"
    A Place & Play 32 (HardwareNameID 8, firmware 6.17.4168) has drawn frames pushed over this protocol by pyvisionect. Other models use the same protocol but haven't been tried yet.

!!! warning "No authentication"
    The protocol has none: anything that can reach port 11113 can claim to be any sign. Keep 11113 on your LAN and don't forward it from the internet.

## 01 — Point the sign at Tesserae

The sign has no discovery and no network setting you can change remotely. It dials the address in its flash, so pick one:

- **Use the old server's address.** If Tesserae runs on the same host as your old Visionect server, stop VSS so port 11113 is free. The sign reconnects to Tesserae within a minute or two, or at its next check-in. To go back, stop Tesserae's listener and start VSS again.
- **Set the address over USB.** Connect the board's micro-USB port (an FTDI serial console, 115200 8N1) and run `server_tcp_set <tesserae-ip> 11113`, then `flash_save` and `reboot`.

Tesserae must be reachable on 11113:

- **Docker:** the compose file uses host networking on Linux, so nothing extra is needed. With port mappings (macOS, Windows), add `"11113:11113"`.
- **Home Assistant App:** the App must publish 11113/tcp. Point the sign at the Home Assistant host.
- **Other port:** set `TESSERAE_VISIONECT_PORT` (or `visionect_port` in the app settings) and point the sign at that port instead.

## 02 — Add the sign in Tesserae

1. **Settings → Devices → Add device**, kind **Visionect sign**.
2. The panel defaults to the 32" sign, 1440×2560 portrait. Choose **portrait (flipped)** if the frame hangs the other way up. A landscape dashboard is turned onto the portrait panel for you.
3. Leave **Sign UUID** blank to bind the first unknown sign that connects, or paste the UUID from the sign's label or the old VSS device list.

Tesserae only opens port 11113 while at least one Visionect device exists. If the port is busy (VSS still running), the log says so.

## 03 — Bind a dashboard

Bind a page, schedule or lineup to the sign like any other device. Each frame is rendered at 1440×2560 in 16 greys. The device card's **Picture quality** (dither, contrast) applies, and the frame is sent to the sign as is. A connected sign draws the frame straight away. A sleeping sign gets it at its next check-in, and only the newest frame is sent.

When the sign reconnects already showing the current frame (after a Tesserae restart, say), Tesserae doesn't push it again, so there's no needless redraw.

## 04 — Sleep and battery

Battery, signal, temperature and firmware from each check-in show on the device card, in battery history and in Home Assistant.

To save battery overnight, set quiet hours (app-wide or on the device) and turn on **sleep through**. When a window starts, the sign gets the current frame and then a deep-sleep command lasting until the window ends. Nothing can wake a sleeping sign early over the network: a frame pushed during its sleep shows when it wakes.
