import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

// Deployment target: memory.mabus.ai via Caddy → k8s nginx pod (hostPort 3403)
// See site/k8s/memory-website.yaml for the deployment manifest.
export default defineConfig({
  integrations: [tailwind()],
  site: 'https://memory.mabus.ai',
});
