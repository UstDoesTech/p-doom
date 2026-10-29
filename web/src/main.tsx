import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import HumanApp from "./HumanApp";
import "./styles.css";

// The public build (VITE_HUMAN_ONLY=true) is people only; the other build is the full app.
const Root = import.meta.env.VITE_HUMAN_ONLY === "true" ? HumanApp : App;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
