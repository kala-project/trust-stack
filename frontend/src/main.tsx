import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles/base.css";

// Fail closed: writes are enabled only by an explicit production build flag.
const readOnly = import.meta.env.VITE_READ_ONLY !== "false";
createRoot(document.getElementById("root")!).render(<App readOnly={readOnly} />);
