import type { JSX } from "preact";
import { IconActivity, IconOverview, IconSecurity, IconSettings, IconWorkspaces } from "../ink/Icons.tsx";
import { MarkerUnderline } from "../ink/Ink.tsx";
import { clock } from "../lib/format.ts";
import { Link } from "../router.tsx";
import { useAppState } from "../state/store.ts";
import styles from "./layout.module.css";

const ITEMS: { href: string; label: string; icon: (p: { size?: number }) => JSX.Element }[] = [
  { href: "/", label: "Overview", icon: IconOverview },
  { href: "/workspaces", label: "Workspaces", icon: IconWorkspaces },
  { href: "/security", label: "Security", icon: IconSecurity },
  { href: "/activity", label: "Activity", icon: IconActivity },
  { href: "/settings", label: "Settings", icon: IconSettings },
];

export function Nav({ path }: { path: string }) {
  const session = useAppState((s) => s.session);
  const config = useAppState((s) => s.config);
  return (
    <nav class={styles.nav} aria-label="Primary">
      <ul class={styles.navList}>
        {ITEMS.map(({ href, label, icon: Icon }) => {
          const current = path === href;
          return (
            <li key={href}>
              <Link href={href} class={`${styles.navLink} ${current ? styles.current : ""}`} aria-current={current ? "page" : undefined}>
                <Icon size={18} />
                <span class={styles.navText}>
                  {label}
                  {current && <MarkerUnderline key={href} seed={href} />}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {session && (
        <p class={styles.navFoot}>
          PatchPilot {session.version}
          {config && (
            <>
              <br />
              AgntID SDK {config.sdkVersion}
            </>
          )}
          <br />
          Server started {clock(session.startedAt)}
        </p>
      )}
    </nav>
  );
}
