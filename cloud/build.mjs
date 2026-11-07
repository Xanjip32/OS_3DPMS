import { readFile, writeFile } from "node:fs/promises";

const files = {
  "/": ["index.html", "text/html"],
  "/app.js": ["app.js", "text/javascript"],
  "/app.css": ["app.css", "text/css"],
  "/sw.js": ["sw.js", "text/javascript"],
  "/manifest.webmanifest": ["manifest.webmanifest", "application/manifest+json"],
  "/icon-192.png": ["icon-192.png", "image/png"],
  "/icon-512.png": ["icon-512.png", "image/png"],
  "/apple-touch-icon.png": ["apple-touch-icon.png", "image/png"],
};
const assets = {};
for (const [path, [file, type]] of Object.entries(files)) {
  const content = await readFile(new URL("../web/" + file, import.meta.url));
  assets[path] = { type, base64: content.toString("base64") };
}
await writeFile(new URL("assets.mjs", import.meta.url), "export default " + JSON.stringify(assets) + ";\n");
