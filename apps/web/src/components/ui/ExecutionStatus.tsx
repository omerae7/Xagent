import { Check, Circle, CircleHelp, Clock3, Loader2, Pause, ShieldAlert, X } from "lucide-react";
import { cn } from ".";

export type ExecutionState = "NOT_STARTED" | "ATTEMPTED" | "PLANNED" | "RUNNING" | "WAITING_APPROVAL" | "EXECUTED" | "OBSERVED" | "VERIFIED" | "PARTIAL" | "UNKNOWN_EFFECT" | "FAILED" | "BLOCKED" | "CANCELLED";
const STATES = {
  NOT_STARTED: ["اجرا آغاز نشد", Circle, "planned"],
  ATTEMPTED: ["تلاش ثبت شد", Clock3, "running"],
  PLANNED: ["برنامه‌ریزی‌شده", Circle, "planned"],
  RUNNING: ["در حال اجرا", Loader2, "running"],
  WAITING_APPROVAL: ["منتظر اجازه", ShieldAlert, "approval"],
  EXECUTED: ["اجراشده؛ تأیید نهایی نشده", Check, "executed"],
  OBSERVED: ["مشاهده ثبت شد", Check, "executed"],
  VERIFIED: ["نتیجه تأیید شد", Check, "verified"],
  PARTIAL: ["بخشی انجام شد", Pause, "partial"],
  UNKNOWN_EFFECT: ["پیامد نامعلوم", CircleHelp, "unknown"],
  FAILED: ["ناموفق", X, "failed"],
  BLOCKED: ["مسدود", ShieldAlert, "blocked"],
  CANCELLED: ["لغوشده", Clock3, "cancelled"],
} as const;

/** A handler finishing is not proof that the user's objective was completed. */
export function stepState(step: any): ExecutionState {
  const detail = step.detail || {};
  if (detail.outcome === "UNKNOWN_EFFECT" || detail.outcome_uncertain) return "UNKNOWN_EFFECT";
  if (detail.outcome === "NOT_STARTED") return "NOT_STARTED";
  if (detail.outcome === "OBSERVED") return "OBSERVED";
  if (detail.outcome === "FAILED" || detail.verified === false) return "FAILED";
  if (detail.outcome === "PARTIAL") return "PARTIAL";
  if (step.status === "running") return "RUNNING";
  if (step.status === "blocked") return step.kind === "approval" ? "WAITING_APPROVAL" : "BLOCKED";
  if (step.status === "error") return "FAILED";
  if (step.status === "cancelled") return "CANCELLED";
  if (step.status === "skipped") return "PLANNED";
  if (step.kind === "plan" || step.kind === "goal") return "PLANNED";
  if (step.kind === "tool") return detail.verified === true ? "VERIFIED" : "EXECUTED";
  if (step.kind === "validate") return "VERIFIED";
  return "OBSERVED";
}

export function ExecutionStatus({ state, className }: { state: ExecutionState; className?: string }) {
  const [label, Icon, tone] = STATES[state];
  return <span className={cn("execution-status", className)} data-state={state} data-tone={tone}>
    <Icon aria-hidden className={cn("h-3 w-3 shrink-0", state === "RUNNING" && "animate-spin")} />{label}
  </span>;
}
