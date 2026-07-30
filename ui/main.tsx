import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/globals.css";
import { Dashboard } from "@/components/dashboard";

const root = document.getElementById("root");
if (!root) {
  throw new Error("dashboard root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <Dashboard />
  </StrictMode>,
);
