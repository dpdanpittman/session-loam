/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: '#0f1115',
        panel: '#15181f',
        'panel-soft': '#1a1e26',
        text: '#d8dde7',
        muted: '#8a93a3',
        accent: '#6fd3ff',
        warm: '#f0b46a',
        'accent-green': '#95d36b',
        border: '#232833',
        'code-bg': '#11141a',
        'code-border': '#2a2f3a',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
};
