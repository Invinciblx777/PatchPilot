import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/shantell-sans/infm.css";
import "@fontsource/commit-mono/400.css";
import "@fontsource/commit-mono/600.css";
import "./styles/tokens.css";
import "./styles/base.css";
import { render } from "preact";
import { App } from "./app.tsx";
import { boot } from "./state/store.ts";

const root = document.getElementById("app");
if (root) {
  render(<App />, root);
  void boot();
}
