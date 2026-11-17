export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#1E1630",
        plum: {
          50: "#F7F4FB",
          100: "#EFE7F7",
          700: "#5B2A86",
          800: "#452068",
          900: "#301448",
        },
        gold: "#A6793D",
      },
      fontFamily: {
        sans: ["Plus Jakarta Sans", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(49, 20, 72, 0.06), 0 12px 32px rgba(49, 20, 72, 0.04)",
      },
    },
  },
  plugins: [],
};
