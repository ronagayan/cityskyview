# ar.shimushili.com — image-tracking AR with an Instagram pop-up

A static web page. When the phone camera sees your **target image** (a sticker, poster, business card,
packaging, a picture on a screen…), a **preset image pops up** on top of it. Tapping the pop-up, or the
button at the bottom of the screen, opens your **Instagram** profile (the Instagram app when installed).

No app install: it runs in the phone's browser (Safari on iPhone, Chrome on Android) over HTTPS.
Nothing is loaded from third-party CDNs; the libraries ship in `vendor/`.

Tested headlessly with a fake camera (see `tools/`): target detected → pop-up shown → tap opens the Instagram URL.

## Files

| Path | What it is |
| --- | --- |
| `index.html` | The AR page. You should not need to edit it. |
| `config.js` | **Edit this**: Instagram URL, button label, pop-up image, pop-up size, auto-open. |
| `assets/popup.png` | The image that pops up: the logo, the Instagram name and "click and follow" (built by `tools/make_placeholders.py` from `assets/logo.png`). |
| `targets/target.png` | The sample target image the camera looks for (placeholder, replace with yours). |
| `targets/targets.mind` | The compiled form of the target image(s), built with `compile.html`. |
| `compile.html` | Browser tool that turns your target image(s) into `targets.mind`. |
| `vendor/` | [A-Frame](https://aframe.io) 1.5.0 and [MindAR](https://github.com/hiukim/mind-ar-js) 1.2.5 (both MIT). |
| `tools/` | Optional: placeholder generator and a headless end-to-end test. |

## Deploy

Upload the contents of this folder to the web root of `ar.shimushili.com` (GitHub Pages, Cloudflare Pages,
Netlify, nginx, anything that serves static files). The only hard requirement is **HTTPS**: browsers refuse
camera access on plain `http://`. Serve `.mind` as `application/octet-stream` (any default config does).

## Customise

1. **Instagram link** — `instagramUrl` in `config.js` (currently `https://www.instagram.com/shimushi.li/`).
2. **Pop-up image** — either replace `assets/popup.png` with any PNG/JPG (transparent PNG works), or change
   the name / call-to-action text at the top of the pop-up section in `tools/make_placeholders.py` and run
   `python3 tools/make_placeholders.py .` to rebuild it from `assets/logo.png`.
   `popupScale` in `config.js` sets its width relative to the printed target (1 = same width).
3. **Target image** — this is what people point the camera at.
   - Use something with lots of detail and contrast, no big plain areas, nothing repetitive (a logo on a
     plain background tracks poorly; a photo, an illustration or a busy card tracks well). Roughly 1000 px
     on the long edge is plenty.
   - Open `compile.html` on the deployed site (or locally, see below), choose the image, press **Compile**,
     and save the downloaded `targets.mind` into `targets/`. Also copy the image to `targets/target.png`
     so the on-screen hint shows the right picture.
   - Several images can be compiled at once; the pop-up is attached to index 0 (the first one).
4. **Auto-open** — set `autoOpenDelayMs` in `config.js` (for example `1500`) to jump to Instagram
   automatically shortly after the target is recognised, instead of waiting for a tap.

### Try it without printing anything

Open `targets/target.png` on a laptop screen and point the phone at it. The scanning screen also has a
link to the target image for exactly this purpose.

### Run locally

Any static server works, e.g. `npx http-server ar -p 8080`, then open `http://localhost:8080/` on the
same computer (localhost is allowed to use the camera without HTTPS). To test on a phone you need HTTPS,
for example `npx http-server ar -S` with a self-signed cert, or a tunnel such as ngrok / Cloudflare Tunnel.

### Headless test (optional)

`tools/headless_check.cjs` starts a local server, opens the page in headless Chromium with a fake camera
that shows `targets/target.png`, waits for detection, taps the pop-up and checks the Instagram URL opens.
It needs Node, Playwright with its Chromium, and Python 3 with Pillow:

```sh
node tools/headless_check.cjs            # test the current targets.mind
node tools/headless_check.cjs --compile  # rebuild targets/targets.mind from targets/target.png, then test
```

## Can the AR run inside the phone's normal camera app?

Not directly. The built-in camera apps (iPhone Camera, Google/Samsung Camera…) do not let third parties
add AR content; they only run the vendor's own effects. What they **do** do on every modern phone is
recognise **QR codes** without any extra app. So the practical flow is:

1. Print a QR code pointing at `https://ar.shimushili.com/` next to (or inside) the target image.
2. The person opens their camera app, it shows a "open link" banner, one tap opens the page in the browser.
3. The page asks for camera permission once, then recognises the target and shows the pop-up.

Any QR generator works; make the code at least 2 cm wide for print. An NFC sticker with the same URL
does the same job on phones that read NFC tags.

Alternatives, if you want the effect *inside* a social app rather than the browser:

- **Snapchat**: build a Lens in Lens Studio (free). People scan a Snapcode in Snapchat's camera.
- **Instagram/Facebook**: not possible any more. Meta shut down its Spark AR platform for third-party
  filters in January 2025, so custom Instagram camera effects cannot be published.
- **iPhone "AR Quick Look"**: Safari can place a 3D object (USDZ) in the room with one tap, but it is not
  image tracking and its banner link options are limited. Not needed for this use case.

## Troubleshooting

- **Black screen / "Camera not available"** — the page is not on HTTPS, or camera permission was denied
  (check the site permissions in the browser), or another app is using the camera.
- **Target not recognised** — poor lighting, glare, or a weak target image (see tips above). Hold the phone
  still 20–50 cm from the target so it fills a good part of the view.
- **Pop-up jitters** — expected on low-end phones; a bigger, more detailed target helps.
- **Tap does nothing on the 3D pop-up** — use the button at the bottom; both go to the same URL.
