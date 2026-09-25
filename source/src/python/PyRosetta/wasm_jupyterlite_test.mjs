// Node end of the `build-wasm.py --jupyterlite-test` phase: start the JupyterLite
// Pyodide kernel the way its Web Worker does, against a served site, and run a
// notebook's code cells through it.
//
// The kernel is two halves. The JavaScript half, a Web Worker, needs a browser.
// The Python half, `pyodide_kernel`, is what actually executes a cell, and it
// needs only Pyodide. The worker's start-up is plain Pyodide calls, so this
// script makes the same calls in the same order under Node, then drives
// `pyodide_kernel.kernel_instance` the way the worker's `execute` does. What it
// cannot reach is anything the worker does with browser APIs: the `/drive`
// mount of the site's file browser, stdin, and comms. So a code cell tagged
// <browser-only tag> is skipped and reported as skipped, rather than run where
// it cannot pass; `--jupyterlite-browser-test` runs its code instead.
//
// The order and the URLs below follow `@jupyterlite/pyodide-kernel` 0.7.2 —
// `initRemoteOptions` and `PyodideRemoteKernel.initialize` in its bundle. Move
// them together with the kernel pin in wasm_jupyterlite_requirements.txt.
//
//   node wasm_jupyterlite_test.mjs <pyodide dist> <site URL> <notebook>
//       <package cache dir> <report file> <browser-only tag>
//
// Everything a cell prints goes to this script's stdout as it arrives. What
// each cell did goes to the report file as JSON, one entry per cell run, and
// the harness judges it. Running stops at the first cell that fails, as a
// notebook's "Run All" does.

import fs from "node:fs";
import path from "node:path";

const [distDir, siteUrl, notebookPath, packageCacheDir, reportFile, browserOnlyTag] =
  process.argv.slice(2);
if (!browserOnlyTag) {
  console.error("usage: node wasm_jupyterlite_test.mjs <pyodide dist> " +
    "<site URL> <notebook> <package cache dir> <report file> <browser-only tag>");
  process.exit(2);
}

const KERNEL_PLUGIN = "@jupyterlite/pyodide-kernel-extension:kernel";
const started = Date.now();

function mark(message) {
  console.log(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${message}`);
}

async function fetchOk(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`fetching ${url} failed: ${response.status} ${response.statusText}`);
  }
  return response;
}

// What the page hands the worker. The kernel's settings come from the site's
// jupyter-lite.json. JupyterLite's config loader (`fixRelativeUrls` in
// config-utils.js) rebases each "./" setting whose key ends in `Url` or `Urls`
// onto the directory of that file, the site root. The extension then resolves
// `loadPyodideOptions`' `...URL` keys against the site's base URL. So each
// relative URL here is relative to the site root.
const siteConfig = (await (await fetchOk(new URL("jupyter-lite.json", siteUrl))).json())[
  "jupyter-config-data"];
const settings = (siteConfig.litePluginSettings || {})[KERNEL_PLUGIN] || {};
const extensionStatic = new URL(
  `${siteConfig.fullLabextensionsUrl}/@jupyterlite/pyodide-kernel-extension/static/`,
  siteUrl);

// The extension appends its own wheel index to whatever the settings list, and
// falls back to the piplite wheel it ships when the settings name none. Its
// bundle hard-codes that wheel's file name; reading it from the index it sits
// in finds the same file without a second copy of the kernel's version here.
const kernelIndexUrl = new URL("pypi/all.json", extensionStatic);
const kernelIndex = await (await fetchOk(kernelIndexUrl)).json();
const pipliteRelease = Object.values(kernelIndex.piplite.releases)[0][0];
const pipliteWheelUrl = settings.pipliteWheelUrl
  ? new URL(settings.pipliteWheelUrl, siteUrl).href
  : new URL(pipliteRelease.url, kernelIndexUrl).href;
const pipliteUrls = [
  ...(settings.pipliteUrls || []).map((url) => new URL(url, siteUrl).href),
  kernelIndexUrl.href,
];
const loadPyodideOptions = {...(settings.loadPyodideOptions || {})};
for (const [key, value] of Object.entries(loadPyodideOptions)) {
  if (key.endsWith("URL") && typeof value === "string") {
    loadPyodideOptions[key] = new URL(value, siteUrl).href;
  }
}

// initRuntime. The worker loads Pyodide from the site's `pyodideUrl`, the
// release on the CDN; Node cannot import a module over HTTPS, so this loads the
// same release from the cross-build environment, whose runtime files are
// byte-identical to the CDN's (ADR 0006). `packageCacheDir` is Node-only: under
// Node, Pyodide saves every package it downloads, and without a directory of
// its own it saves them beside `pyodide.mjs`, inside the toolchain.
const {loadPyodide} = await import(path.join(distDir, "pyodide.mjs"));
const pyodide = await loadPyodide({
  indexURL: distDir + path.sep,
  // The worker sends these to the browser console and JupyterLab's log
  // console, not to a cell.
  stdout: (line) => console.log(`[pyodide] ${line}`),
  stderr: (line) => console.log(`[pyodide] ${line}`),
  ...loadPyodideOptions,
  // Last, so that nothing in the site's settings can move it.
  packageCacheDir: packageCacheDir + path.sep,
});
mark(`Pyodide ${pyodide.version} booted`);

// initFilesystem is skipped. Where the page can sync with the file browser,
// through its service worker or cross-origin isolation, the worker mounts it
// at `/drive` and changes into it before installing anything. A tab that can do
// neither, such as a private window, skips the mount too, and so the replay
// runs the path that tab takes.

// initPackageManager.
const preloaded = loadPyodideOptions.packages || [];
if (!preloaded.includes("micropip")) {
  await pyodide.loadPackage(["micropip"]);
}
if (!preloaded.includes("piplite")) {
  await pyodide.runPythonAsync(
    // A JSON string is a valid Python string literal, where a URL spliced
    // between quotes could close them.
    `import micropip\nawait micropip.install(${JSON.stringify(pipliteWheelUrl)}, keep_going=True)`);
}
// The worker takes `disablePyPIFallback` from the site's settings, and the site
// leaves the fallback on for visitors' own installs. The replay turns it off
// whatever the site says. Everything the kernel and PyRosetta install is
// shipped in the site, so this changes nothing today; if a later kernel starts
// installing something the site does not bundle, the gate fails, where it would
// otherwise run whatever PyPI served.
await pyodide.runPythonAsync([
  "import piplite.piplite",
  "piplite.piplite._PIPLITE_DISABLE_PYPI = True",
  `piplite.piplite._PIPLITE_URLS = ${JSON.stringify(pipliteUrls)}`,
].join("\n"));
mark("piplite ready");

// initKernel. With `/drive` mounted, the worker also changes into the
// notebook's own directory under it.
const kernelPackages = ["sqlite3", "ipykernel", "comm", "pyodide_kernel", "jedi", "ipython"];
await pyodide.runPythonAsync([
  ...kernelPackages
    .filter((name) => !preloaded.includes(name))
    .map((name) => `await piplite.install('${name}', keep_going=True)`),
  "import pyodide_kernel",
].join("\n"));

// initGlobals.
const pyodideKernel = pyodide.globals.get("pyodide_kernel");
const kernel = pyodideKernel.kernel_instance;
const interpreter = kernel.interpreter;
mark("kernel ready");

// The worker's `execute` rebinds these callbacks for every request, to tag each
// message with its request. One notebook needs them bound once.
let outputs = [];
const toJs = (value) =>
  value instanceof pyodide.ffi.PyProxy ? value.toJs({dict_converter: Object.fromEntries}) : value;
const onStream = (name, text) => {
  process.stdout.write(String(text));
  outputs.push({output_type: "stream", name: String(name), text: String(text)});
};
pyodideKernel.stdout_stream.publish_stream_callback = onStream;
pyodideKernel.stderr_stream.publish_stream_callback = onStream;
interpreter.display_pub.display_data_callback = (data, metadata) =>
  outputs.push({output_type: "display_data", data: toJs(data), metadata: toJs(metadata)});
interpreter.displayhook.publish_execution_result = (count, data, metadata) => {
  const bundle = toJs(data);
  console.log(`Out[${count}]: ${bundle["text/plain"]}`);
  outputs.push({output_type: "execute_result", data: bundle, metadata: toJs(metadata)});
};

// The notebook is found the way JupyterLite's file browser finds it: listed in
// the contents index the build wrote, and fetched from `files/`.
const listing = await (await fetchOk(new URL("api/contents/all.json", siteUrl))).json();
if (!listing.content.some((entry) => entry.path === notebookPath)) {
  throw new Error(`${notebookPath} is not in the site's contents index`);
}
const notebook = await (await fetchOk(new URL(`files/${notebookPath}`, siteUrl))).json();

const cells = [];
for (const cell of notebook.cells) {
  if (cell.cell_type !== "code") continue;
  const source = Array.isArray(cell.source) ? cell.source.join("") : cell.source;
  console.log(`\nIn: ${source.split("\n").join("\n    ")}`);
  if ((cell.metadata?.tags || []).includes(browserOnlyTag)) {
    cells.push({id: cell.id, source, status: "skipped", outputs: []});
    mark(`cell ${cell.id}: skipped, tagged ${browserOnlyTag}`);
    continue;
  }
  outputs = [];
  const result = toJs(await kernel.run(source));
  cells.push({
    id: cell.id,
    source,
    status: result.status,
    ename: result.ename,
    evalue: result.evalue,
    traceback: result.traceback,
    outputs,
  });
  mark(`cell ${cell.id}: ${result.status}`);
  if (result.status !== "ok") break;
}

fs.writeFileSync(reportFile, JSON.stringify({pyodide: pyodide.version, cells}, null, 1));
mark("done");
