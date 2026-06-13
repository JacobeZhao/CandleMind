/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg:      "#0b0e17",
        card:    "#141823",
        border:  "#1e2436",
        accent:  "#f0b90b",
        green:   "#0ecb81",
        red:     "#f6465d",
        muted:   "#8b94b2",
        surface: "#1a1f2e",
      },
    },
  },
  plugins: [],
};
