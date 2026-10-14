// ar.shimushili.com — edit this file to customise the experience.
// Everything else (index.html, vendor/) can stay as it is.
window.AR_CONFIG = {
  // Where a tap on the pop-up sends people. Use your real Instagram profile URL.
  // This URL opens the Instagram app when it is installed, otherwise the website.
  instagramUrl: "https://www.instagram.com/shimushi.li/",

  // Label on the on-screen button that appears once the target is found.
  buttonLabel: "Open Instagram",

  // The image that pops up on top of the target (PNG/JPG, transparent PNG works).
  popupImage: "assets/popup.png",

  // Width of the pop-up relative to the printed target (1 = same width as the target).
  popupScale: 0.9,

  // Compiled image targets (made with compile.html). Index 0 is the first image you compiled.
  targetsFile: "targets/targets.mind",

  // Keep the "Open Instagram" button on screen this long after the target leaves the view.
  buttonLingerMs: 5000,

  // Optional: automatically go to Instagram N ms after the target is first found.
  // 0 = off (people tap the pop-up or the button instead). Note this navigates away from the AR page.
  autoOpenDelayMs: 0
};
