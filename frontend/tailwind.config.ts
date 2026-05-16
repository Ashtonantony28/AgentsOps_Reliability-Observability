import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#f7f8fa",
          100: "#eef0f4",
          200: "#d9dde5",
          300: "#b6bdc9",
          400: "#8c95a5",
          500: "#646f81",
          600: "#4a5466",
          700: "#363f50",
          800: "#222a3b",
          900: "#141a28",
          950: "#0c1120",
        },
        brand: {
          500: "#7c5cff",
          600: "#6841ff",
          700: "#5732e6",
        },
        ok: "#22c55e",
        warn: "#f59e0b",
        bad: "#ef4444",
      },
    },
  },
  plugins: [],
} satisfies Config;
