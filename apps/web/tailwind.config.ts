import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./features/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        terminal: { 950: "#090c10", 900: "#10151c", 800: "#171e27", 700: "#202936" },
        bullish: "#34d399",
        bearish: "#fb7185",
        watch: "#fbbf24",
      },
    },
  },
  plugins: [],
};

export default config;
