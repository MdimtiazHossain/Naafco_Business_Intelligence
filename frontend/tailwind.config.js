/** @type {import('tailwindcss').Config} */
export default {
  // Dark mode is driven by a class on <html>, so the user's choice (light /
  // dark / system) is applied by ThemeContext rather than by the OS alone.
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
        severity: {
          critical: '#dc2626',
          high: '#ea580c',
          medium: '#ca8a04',
          low: '#0891b2',
        },
      },
      fontFamily: {
        // Bangla needs a font with Bengali coverage; the stack falls back to
        // the system UI font for Latin text.
        sans: ['Inter', 'Noto Sans Bengali', 'system-ui', 'sans-serif'],
      },
      keyframes: {
        'fade-in': { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
      },
      animation: { 'fade-in': 'fade-in 150ms ease-out' },
    },
  },
  plugins: [],
};
