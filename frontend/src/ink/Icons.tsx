// Crisp geometric interface icons on a 20px grid. Deliberately not
// hand-drawn: controls stay precise; the ink layer is for evidence.

import type { JSX } from "preact";

type IconProps = { size?: number; class?: string };

function Icon({ size = 20, class: className, children }: IconProps & { children: JSX.Element | JSX.Element[] }) {
  return (
    <svg
      class={className}
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const IconOverview = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 4.5h7v11h-7zM12.5 4.5h4v5h-4zM12.5 12h4v3.5h-4z" />
  </Icon>
);

export const IconWorkspaces = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="6" cy="5" r="1.8" />
    <circle cx="6" cy="15" r="1.8" />
    <circle cx="14" cy="7" r="1.8" />
    <path d="M6 6.8v6.4M14 8.8c0 3.6-3.4 4.4-6.6 5" />
  </Icon>
);

export const IconSecurity = (p: IconProps) => (
  <Icon {...p}>
    <path d="M10 2.8 4.5 4.8v4.6c0 3.8 2.4 6.4 5.5 7.8 3.1-1.4 5.5-4 5.5-7.8V4.8z" />
  </Icon>
);

export const IconActivity = (p: IconProps) => (
  <Icon {...p}>
    <path d="M8 5.5h8.5M8 10h8.5M8 14.5h8.5" />
    <circle cx="4.5" cy="5.5" r="0.9" />
    <circle cx="4.5" cy="10" r="0.9" />
    <circle cx="4.5" cy="14.5" r="0.9" />
  </Icon>
);

export const IconSettings = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 6.5h13M3.5 13.5h13" />
    <circle cx="7.5" cy="6.5" r="2" fill="var(--stock)" />
    <circle cx="12.5" cy="13.5" r="2" fill="var(--stock)" />
  </Icon>
);

export const IconRefresh = (p: IconProps) => (
  <Icon {...p}>
    <path d="M15.6 8.4A6 6 0 1 0 16 11.6M16.2 4.2v4.4h-4.4" />
  </Icon>
);

export const IconChevron = (p: IconProps) => (
  <Icon {...p}>
    <path d="m7.5 5 5 5-5 5" />
  </Icon>
);

export const IconUpload = (p: IconProps) => (
  <Icon {...p}>
    <path d="M10 13V4M6.5 7.5 10 4l3.5 3.5M4.5 13.5V16h11v-2.5" />
  </Icon>
);

export const IconPen = (p: IconProps) => (
  <Icon {...p}>
    <path d="m12.8 4.2 3 3L8 15l-3.6.6L5 12z" />
  </Icon>
);
