import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        panel: {
          bg: '#0b0f14',
          surface: '#111823',
          border: '#1e2a3a',
          text: '#e6edf3',
          muted: '#8b98a9',
          accent: '#d97706',
          accentHover: '#b45309',
          ok: '#16a34a',
          warn: '#d97706',
          bad: '#dc2626',
        },
      },
    },
  },
  plugins: [],
};

export default config;
