import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NewResearch } from "../../src/features/research/NewResearch";
import { ResearchWorkspace } from "../../src/features/research/ResearchWorkspace";
import "../../src/features/research/Research.module.css";

function App() {
  const params = new URLSearchParams(location.search);
  const initial = params.get("view") === "workspace" ? "workspace" : "new";
  const [view, setView] = useState(initial);
  const [taskId, setTaskId] = useState(params.get("task") || "task-workspace");
  if (view === "left") return <main><h1 data-testid="left">Left the research page</h1></main>;
  return <main>
    <nav>
      <button type="button" data-testid="leave" onClick={() => setView("left")}>Leave page</button>
    </nav>
    {view === "new"
      ? <NewResearch readOnly={false} onTask={(id) => { setTaskId(id); setView("workspace"); }} />
      : <ResearchWorkspace taskId={taskId} readOnly={false} onReport={() => undefined} onChanged={() => undefined} />}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
