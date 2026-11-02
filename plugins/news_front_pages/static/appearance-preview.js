import { blacknessTable, whiteTable, chainTables, sharpness, processCanvas } from "../client.js";

const form = document.getElementById("nx-papers");
const sample = new Image();
sample.src = new URL("./text-sample.png", import.meta.url).href;

function redraw() {
  for (const figure of form.querySelectorAll("[data-nx-preview]")) {
    const name = figure.dataset.nxPreview;
    const value = form.elements.namedItem(name).value;
    const small = document.createElement("canvas");
    small.width = sample.naturalWidth;
    small.height = sample.naturalHeight;
    const context = small.getContext("2d", { willReadFrequently: true });
    const canvases = figure.querySelectorAll("canvas");
    for (let i = 0; i < canvases.length; i++) {
      context.drawImage(sample, 0, 0);
      if (i === 1) {
        processCanvas(
          small,
          chainTables(
            blacknessTable(name === "text_blackness" ? value : "off"),
            whiteTable(name === "clean_white" ? value : "off")
          ),
          sharpness(name === "text_sharpness" ? value : "off"),
          name === "dithering" ? value : "display"
        );
      }
      const output = canvases[i].getContext("2d");
      output.imageSmoothingEnabled = false;
      output.drawImage(small, 0, 0, canvases[i].width, canvases[i].height);
    }
    figure.hidden = false;
    figure.dataset.ready = "true";
  }
}

if (form) {
  sample
    .decode()
    .then(() => {
      redraw();
      form.addEventListener("change", redraw);
      form.addEventListener("reset", () => setTimeout(redraw, 0));
    })
    .catch(() => {
      for (const figure of form.querySelectorAll("[data-nx-preview]")) figure.hidden = true;
    });
}
