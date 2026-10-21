import type { ResearchTask } from "../data/types";
import { formatAppRoute } from "../data/route";

export function taskHref(taskId: string): string {
  return formatAppRoute({ page: "research", task: taskId, conversation: "", tab: "research" });
}

export function safeReaderHref(value: string | undefined, origin = location.origin): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, origin);
    return url.origin === origin && url.pathname === "/report-reader.html"
      ? url.pathname + url.search + url.hash : null;
  } catch { return null; }
}

export function destination(task: ResearchTask): string {
  const pending = task.latest_run && (["queued", "running"].includes(task.latest_run.status)
    || task.latest_run.decision?.status === "pending");
  return (!pending && safeReaderHref(task.latest_report?.reader_href || task.reader_href)) || taskHref(task.task_id);
}
