import type { ButtonHTMLAttributes, ComponentChildren, JSX } from "preact";
import styles from "./controls.module.css";
import { StateGlyph } from "./StatusMark.tsx";

type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "icon" | "type" | "disabled"> & {
  variant?: "primary" | "secondary" | "quiet";
  busy?: boolean;
  icon?: JSX.Element;
  type?: "button" | "submit";
  disabled?: boolean;
};

export function Button({
  variant = "secondary",
  busy = false,
  icon,
  children,
  disabled,
  type = "button",
  class: className,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      class={`${styles.button} ${styles[variant]} ${className ?? ""}`}
      disabled={disabled || busy}
      aria-busy={busy ? "true" : undefined}
    >
      {busy ? <StateGlyph state="running" /> : icon}
      <span>{children}</span>
    </button>
  );
}

function describedBy(id: string, hint?: ComponentChildren, error?: string | null): string | undefined {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length ? ids.join(" ") : undefined;
}

type FieldFrame = {
  id: string;
  label: string;
  hint?: ComponentChildren;
  error?: string | null;
  children: ComponentChildren;
  aside?: ComponentChildren;
};

function Frame({ id, label, hint, error, children, aside }: FieldFrame) {
  return (
    <div class={styles.field}>
      <div class={styles.labelRow}>
        <label class={styles.label} for={id}>
          {label}
        </label>
        {aside}
      </div>
      {children}
      {hint && (
        <p id={`${id}-hint`} class={styles.hint}>
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} class={styles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

type TextProps = Omit<FieldFrame, "children"> & {
  value: string;
  onValue: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
  autoComplete?: string;
  disabled?: boolean;
};

export function TextField({ id, label, hint, error, value, onValue, placeholder, mono, autoComplete = "off", disabled, aside }: TextProps) {
  return (
    <Frame id={id} label={label} hint={hint} error={error} aside={aside}>
      <input
        id={id}
        class={`${styles.input} ${mono ? styles.mono : ""}`}
        value={value}
        placeholder={placeholder}
        autocomplete={autoComplete}
        spellcheck={false}
        disabled={disabled}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={describedBy(id, hint, error)}
        onInput={(e) => onValue((e.currentTarget as HTMLInputElement).value)}
      />
    </Frame>
  );
}

export function TextArea({
  id,
  label,
  hint,
  error,
  value,
  onValue,
  placeholder,
  mono,
  disabled,
  rows = 4,
  aside,
}: TextProps & { rows?: number }) {
  return (
    <Frame id={id} label={label} hint={hint} error={error} aside={aside}>
      <textarea
        id={id}
        class={`${styles.input} ${styles.area} ${mono ? styles.mono : ""}`}
        value={value}
        rows={rows}
        placeholder={placeholder}
        spellcheck={false}
        disabled={disabled}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={describedBy(id, hint, error)}
        onInput={(e) => onValue((e.currentTarget as HTMLTextAreaElement).value)}
      />
    </Frame>
  );
}

type SelectProps = Omit<FieldFrame, "children"> & {
  value: string;
  onValue: (value: string) => void;
  options: { value: string; label: string }[];
  disabled?: boolean;
};

export function SelectField({ id, label, hint, error, value, onValue, options, disabled, aside }: SelectProps) {
  return (
    <Frame id={id} label={label} hint={hint} error={error} aside={aside}>
      <div class={styles.selectWrap}>
        <select
          id={id}
          class={`${styles.input} ${styles.select}`}
          value={value}
          disabled={disabled}
          aria-describedby={describedBy(id, hint, error)}
          onChange={(e) => onValue((e.currentTarget as HTMLSelectElement).value)}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
    </Frame>
  );
}

export function CheckField({
  id,
  checked,
  onChecked,
  children,
  hint,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChecked: (checked: boolean) => void;
  children: ComponentChildren;
  hint?: ComponentChildren;
  disabled?: boolean;
}) {
  return (
    <div class={styles.check}>
      <input
        id={id}
        type="checkbox"
        class={styles.box}
        checked={checked}
        disabled={disabled}
        aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(e) => onChecked((e.currentTarget as HTMLInputElement).checked)}
      />
      <div>
        <label for={id} class={styles.checkLabel}>
          {children}
        </label>
        {hint && (
          <p id={`${id}-hint`} class={styles.hint}>
            {hint}
          </p>
        )}
      </div>
    </div>
  );
}
