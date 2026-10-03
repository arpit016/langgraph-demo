import React, { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { runAgent } from "./agui.js";

const STEP_LABELS = {
  flight_agent: "Searching flights",
  hotel_agent: "Finding hotels",
  itinerary_agent: "Drafting the itinerary",
  final_agent: "Writing your travel plan",
};

const newAssistant = () => ({
  id: crypto.randomUUID(),
  role: "assistant",
  steps: [], // { name, status: "running" | "done", detail }
  reasoning: "",
  content: "",
  status: "running", // running | done | error
  startedAt: Date.now(),
  thoughtFor: null,
  error: null,
});

// Pure reducer: apply one AG-UI event to the assistant message
function applyEvent(msg, ev) {
  switch (ev.type) {
    case "STEP_STARTED":
      return {
        ...msg,
        steps: [...msg.steps, { name: ev.stepName, status: "running", detail: null }],
      };
    case "STEP_FINISHED":
      return {
        ...msg,
        steps: msg.steps.map((s) => (s.name === ev.stepName ? { ...s, status: "done" } : s)),
      };
    case "CUSTOM":
      if (ev.name !== "step_detail") return msg;
      return {
        ...msg,
        steps: msg.steps.map((s) =>
          s.name === ev.value.step ? { ...s, detail: ev.value.detail } : s
        ),
      };
    case "REASONING_MESSAGE_CONTENT":
      return { ...msg, reasoning: msg.reasoning + ev.delta };
    case "TEXT_MESSAGE_CONTENT": {
      const thoughtFor = msg.thoughtFor ?? Math.round((Date.now() - msg.startedAt) / 1000);
      return { ...msg, content: msg.content + ev.delta, thoughtFor };
    }
    case "RUN_FINISHED":
      return { ...msg, status: "done" };
    case "RUN_ERROR":
      return { ...msg, status: "error", error: ev.message };
    default:
      return msg;
  }
}

function Thinking({ msg }) {
  const streamingAnswer = msg.content.length > 0;
  // Open while thinking, collapse by itself once the answer starts streaming
  const [open, setOpen] = useState(true);
  useEffect(() => {
    if (streamingAnswer) setOpen(false);
  }, [streamingAnswer]);

  // Keep the draft scrolled to the newest text while it streams
  const draftRef = useRef(null);
  useEffect(() => {
    if (draftRef.current) draftRef.current.scrollTop = draftRef.current.scrollHeight;
  }, [msg.reasoning, open]);

  if (msg.steps.length === 0) return null;
  const working = msg.status === "running" && !streamingAnswer;
  const title = working
    ? "Thinking…"
    : msg.thoughtFor != null
    ? `Thought for ${msg.thoughtFor}s`
    : "Thought process";

  return (
    <div className="thinking">
      <button className="thinking-toggle" onClick={() => setOpen(!open)}>
        <span className={working ? "dot pulse" : "dot"} />
        {title}
        <span className="chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="thinking-body">
          <ul className="steps">
            {msg.steps.map((s) => (
              <li key={s.name} className={s.status}>
                <span className="icon">{s.status === "done" ? "✓" : "○"}</span>
                <div>
                  <div>{STEP_LABELS[s.name] || s.name}</div>
                  {s.detail && <div className="detail">{s.detail}</div>}
                </div>
              </li>
            ))}
          </ul>
          {msg.reasoning && <pre className="draft" ref={draftRef}>{msg.reasoning}</pre>}
        </div>
      )}
    </div>
  );
}

function Message({ msg }) {
  if (msg.role === "user") {
    return <div className="msg user">{msg.content}</div>;
  }
  return (
    <div className="msg assistant">
      <Thinking msg={msg} />
      {msg.content && (
        <div className="answer">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
        </div>
      )}
      {msg.error && <div className="error">Something went wrong: {msg.error}</div>}
    </div>
  );
}

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const threadId = useRef(null);
  const bottom = useRef(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;

    const user = { id: crypto.randomUUID(), role: "user", content: text };
    const assistant = newAssistant();
    setMessages((m) => [...m, user, assistant]);
    setInput("");
    setBusy(true);

    const update = (fn) =>
      setMessages((m) => m.map((x) => (x.id === assistant.id ? fn(x) : x)));

    try {
      await runAgent({
        message: text,
        threadId: threadId.current,
        onEvent: (ev) => {
          if (ev.type === "RUN_STARTED") threadId.current = ev.threadId;
          update((msg) => applyEvent(msg, ev));
        },
      });
    } catch (err) {
      update((msg) => ({ ...msg, status: "error", error: err.message }));
    } finally {
      update((msg) => (msg.status === "running" ? { ...msg, status: "done" } : msg));
      setBusy(false);
    }
  }

  function newChat() {
    threadId.current = null;
    setMessages([]);
  }

  return (
    <div className="app">
      <header>
        <h1>✈️ TripMate</h1>
        <button onClick={newChat} disabled={busy}>
          New chat
        </button>
      </header>

      <main>
        {messages.length === 0 && (
          <div className="empty">
            <p>Where do you want to go?</p>
            <p className="hint">Try: “Plan a 3 day trip from Delhi to Goa”</p>
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} msg={m} />
        ))}
        <div ref={bottom} />
      </main>

      <form onSubmit={send}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask TripMate to plan a trip…"
          disabled={busy}
          autoFocus
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
