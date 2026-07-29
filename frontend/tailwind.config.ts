import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#111318",
          900: "#16181D",
          800: "#1E2128",
          700: "#2A2E37",
          500: "#8B8F98",
          200: "#E8E6E1",
        },
        reel: {
          amber: "#E8A33D",
          amberDim: "#B9822F",
        },
        state: {
          success: "#5FB88E",
          error: "#E0665A",
          pending: "#8B8F98",
        },
      },
      fontFamily: {
        display: ["'Space Grotesk'", "sans-serif"],
        body: ["'Inter'", "sans-serif"],
        mono: ["'JetBrains Mono'", "monospace"],
      },
      borderRadius: {
        DEFAULT: "6px",
      },
    },
  },
  plugins: [],
} satisfies Config;
