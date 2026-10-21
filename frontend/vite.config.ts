import { defineConfig } from "vite";

// Deliberately no API proxy: the reviewed Python server controls read/write
// policy. Both HTML entries use fixed entry names for its narrow allowlist;
// shared chunks remain content-addressed and are enumerated by the manifest.
export default defineConfig({
  server: { host: "127.0.0.1", strictPort: true },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    manifest: true,
    rollupOptions: {
      input: {
        app: new URL("./index.html", import.meta.url).pathname,
        reader: new URL("./report-reader.html", import.meta.url).pathname,
      },
      output: {
        entryFileNames: "workbench-[name].js",
        chunkFileNames: "workbench-shared.js",
        assetFileNames: asset => asset.name?.endsWith(".css") ? "workbench-[name][extname]" : "assets/[name]-[hash][extname]",
      },
    },
  },
});
