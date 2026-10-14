// ar.shimushili.com — edit this file to customise the experience.
// Everything else (index.html, vendor/) can stay as it is.
//
// Caching: phones keep old copies of images. When you replace an image or targets file,
// bump the ?v= number on it below (and the ?v= on config.js in index.html when you edit this file).
window.AR_CONFIG = {
  // Where a tap on the pop-up sends people. Opens the Instagram app when installed, otherwise the website.
  instagramUrl: "https://www.instagram.com/shimushi.li/",

  // The image that pops up on top of the target (PNG/JPG, transparent PNG works).
  popupImage: "assets/popup.png?v=3",

  // Width of the pop-up relative to the printed target (1 = same width as the target).
  popupScale: 0.9,

  // Compiled image targets (made with compile.html). Index 0 is the first image you compiled.
  targetsFile: "targets/targets.mind?v=3",

  // Optional: automatically go to Instagram N ms after the target is first found.
  // 0 = off (people tap the pop-up instead). Note this navigates away from the AR page.
  autoOpenDelayMs: 0
};
