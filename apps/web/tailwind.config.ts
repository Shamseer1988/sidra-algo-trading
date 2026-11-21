import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./features/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  // `dark:` follows the app's own theme switch (html[data-theme]), not the
  // operating system. Left as the default ("media"), a screen written as
  // `text-slate-900 dark:text-white` drew dark text on the dark theme whenever
  // Windows itself was set to light, and the reverse.
  darkMode: ["selector", '[data-theme="dark"]'],
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
