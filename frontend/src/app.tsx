import { useEffect } from "preact/hooks";
import { AccessKeyGate } from "./components/AccessKeyGate.tsx";
import { Notice } from "./components/common.tsx";
import { InkDefs } from "./ink/Drawings.tsx";
import { Header } from "./layout/Header.tsx";
import layout from "./layout/layout.module.css";
import { Nav } from "./layout/Nav.tsx";
import { Activity } from "./pages/Activity.tsx";
import { Overview } from "./pages/overview/Overview.tsx";
import { Security } from "./pages/Security.tsx";
import { Settings } from "./pages/Settings.tsx";
import { Workspaces } from "./pages/Workspaces.tsx";
import { Link, usePath } from "./router.tsx";
import { useAppState } from "./state/store.ts";

const PAGES: Record<string, { title: string; render: () => preact.JSX.Element }> = {
  "/": { title: "Overview", render: () => <Overview /> },
  "/workspaces": { title: "Workspaces", render: () => <Workspaces /> },
  "/security": { title: "Security", render: () => <Security /> },
  "/activity": { title: "Activity", render: () => <Activity /> },
  "/settings": { title: "Settings", render: () => <Settings /> },
};

function NotFound() {
  return (
    <div class={layout.page}>
      <h1>No page here</h1>
      <p class="lede">
        PatchPilot has no page at this address. <Link href="/">Go to the overview</Link>.
      </p>
    </div>
  );
}

function ServerUnreachable({ message }: { message: string }) {
  return (
    <div class={layout.page}>
      <h1>PatchPilot's server is not answering</h1>
      <Notice tone="warning" title="Nothing can be shown until it answers">
        <p>{message}</p>
        <p>
          Start it again with <code>patchpilot-ui</code>, then reload this page.
        </p>
      </Notice>
    </div>
  );
}

export function App() {
  const { path, hash } = usePath();
  const sessionError = useAppState((s) => s.sessionError);
  const access = useAppState((s) => s.access);
  const announcement = useAppState((s) => s.announcement);
  const page = PAGES[path];

  useEffect(() => {
    document.title = `${page?.title ?? "Not found"} – PatchPilot`;
    const target = hash ? document.getElementById(hash) : null;
    if (target) {
      target.scrollIntoView({ block: "start" });
      return;
    }
    window.scrollTo(0, 0);
    document.querySelector<HTMLElement>("main h1")?.focus({ preventScroll: true });
  }, [path, hash, page]);

  return (
    <>
      <InkDefs />
      <a class="skip-link" href="#main">
        Skip to main content
      </a>
      <Header />
      <div class={layout.shell}>
        <Nav path={path} />
        <main id="main" class={layout.main} tabIndex={-1}>
          {access !== "ok" ? (
            <AccessKeyGate rejected={access === "rejected"} />
          ) : sessionError ? (
            <ServerUnreachable message={sessionError.message} />
          ) : page ? (
            page.render()
          ) : (
            <NotFound />
          )}
        </main>
      </div>
      <div class="visually-hidden" aria-live="polite">
        {announcement}
      </div>
    </>
  );
}
