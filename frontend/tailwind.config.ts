import type { Config } from "tailwindcss";
import defaultColors from "tailwindcss/colors";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surface scale — zinc backbone for a quiet, professional surface.
        // Aliased to our own naming so component classes read better.
        ink: {
          900: defaultColors.zinc[950],
          800: defaultColors.zinc[900],
          700: "#1a1a1f",
          600: defaultColors.zinc[800],
          500: defaultColors.zinc[700],
          400: defaultColors.zinc[600],
          300: defaultColors.zinc[500],
          200: defaultColors.zinc[400],
          100: defaultColors.zinc[300],
          50: defaultColors.zinc[200],
        },
        // Accent — restrained emerald (one accent only, used for the brand
        // mark, the recommendation pick, and the "high confidence" badge).
        accent: {
          DEFAULT: defaultColors.emerald[400],
          dim: defaultColors.emerald[600],
          glow: defaultColors.emerald[300],
        },
        // Semantic confidence palette (the traffic-light idea).
        good: defaultColors.emerald[400],
        warn: defaultColors.amber[400],
        bad: defaultColors.rose[400],
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SF Mono",
          "Menlo",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      fontSize: {
        // Tight scale — no `lg`, only the sizes we actually want to use.
        xs: ["0.7rem", "1rem"],
        sm: ["0.825rem", "1.25rem"],
        base: ["0.95rem", "1.5rem"],
        h2: ["1.05rem", "1.4rem"],
        h1: ["1.6rem", "2rem"],
        display: ["2.25rem", "2.5rem"],
      },
      borderRadius: {
        DEFAULT: "0.375rem",
      },
    },
  },
  plugins: [],
};

export default config;
