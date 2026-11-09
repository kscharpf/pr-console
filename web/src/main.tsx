import "@mantine/core/styles.css";
import "./app.css";
import { createTheme, localStorageColorSchemeManager, MantineProvider } from "@mantine/core";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const theme = createTheme({ primaryColor: "blue", defaultRadius: "sm" });
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      {/* CSP has no 'unsafe-inline': no <style> tags from Mantine; the default variables come from styles.css. */}
      <MantineProvider
        theme={theme}
        defaultColorScheme="auto"
        colorSchemeManager={localStorageColorSchemeManager({ key: "pr-console-color-scheme" })}
        withCssVariables={false}
        withGlobalClasses={false}
      >
        <App />
      </MantineProvider>
    </StrictMode>,
  );
}
