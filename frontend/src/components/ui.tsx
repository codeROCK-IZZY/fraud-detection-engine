import clsx from "clsx";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { ApiError } from "../api/client";

export function Card({ title, actions, children, className }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={clsx("rounded-xl border border-slate-200 bg-white shadow-sm", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-100 px-5 py-3">
          <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger"; busy?: boolean };
export function Button({ variant = "primary", busy, className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" && "bg-sky-700 text-white hover:bg-sky-800",
        variant === "secondary" && "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
        variant === "danger" && "bg-red-600 text-white hover:bg-red-700",
        className,
      )}
      disabled={disabled || busy}
      {...rest}
    >
      {busy && <Spinner small />}
      {children}
    </button>
  );
}

export function Spinner({ small }: { small?: boolean }) {
  return (
    <span role="status" aria-label="Loading"
      className={clsx("inline-block animate-spin rounded-full border-2 border-current border-r-transparent",
        small ? "h-3.5 w-3.5" : "h-6 w-6 text-sky-700")} />
  );
}

export function Loading() {
  return <div className="flex justify-center p-10"><Spinner /></div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  const details = error instanceof ApiError ? error.errors : [];
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
      <p className="font-medium">{msg}</p>
      {details.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs">
          {details.slice(0, 8).map((d, i) => (
            <li key={i}>{d.msg ?? d.error}{d.loc ? ` (${d.loc.slice(1).join(".")})` : ""}{d.row ? ` (row ${d.row})` : ""}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

const inputCls = "w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm shadow-sm focus:border-sky-600 focus:outline-none";
export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={clsx(inputCls, props.className)} />;
}
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={clsx(inputCls, props.className)} />;
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-slate-200 text-sm [&_td]:px-3 [&_td]:py-2 [&_th]:px-3 [&_th]:py-2 [&_th]:text-left [&_th]:text-xs [&_th]:font-semibold [&_th]:uppercase [&_th]:tracking-wide [&_th]:text-slate-500 [&_tbody_tr]:border-t [&_tbody_tr]:border-slate-100">
        {children}
      </table>
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}
      </div>
      <div className="flex gap-2">{actions}</div>
    </div>
  );
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "green" | "amber" | "red" | "sky" | "violet" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-700 ring-slate-300",
    green: "bg-emerald-50 text-emerald-800 ring-emerald-600/20",
    amber: "bg-amber-50 text-amber-800 ring-amber-600/20",
    red: "bg-red-50 text-red-800 ring-red-600/20",
    sky: "bg-sky-50 text-sky-800 ring-sky-600/20",
    violet: "bg-violet-50 text-violet-800 ring-violet-600/20",
  };
  return <span className={clsx("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset", tones[tone])}>{children}</span>;
}

/** Labels where a value came from, so AI output is never mistaken for deterministic extraction. */
export function SourceTag({ source }: { source: string | null | undefined }) {
  if (!source) return null;
  const ai = source.startsWith("claude");
  return (
    <span title={ai ? "Generated by an AI model" : "Produced by deterministic rules"}
      className={clsx("rounded px-1.5 py-0.5 text-[11px] font-medium", ai ? "bg-violet-50 text-violet-700" : "bg-slate-100 text-slate-600")}>
      {ai ? `AI · ${source.replace("claude:", "")}` : "Rule-based"}
    </span>
  );
}
