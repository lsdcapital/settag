import { defineConfig } from "vite";

import { tanstackStart } from "@tanstack/react-start/plugin/vite";

import viteReact from "@vitejs/plugin-react";

// Prerendered to static HTML so the site can be served from Cloudflare Pages
// with no Worker behind it. If a page ever needs a server function, drop the
// prerender block and add the Cloudflare Workers adapter instead.
const config = defineConfig({
  resolve: { tsconfigPaths: true },
  plugins: [
    tanstackStart({
      prerender: {
        enabled: true,
        crawlLinks: true,
        failOnError: true,
      },
    }),
    viteReact(),
  ],
});

export default config;
