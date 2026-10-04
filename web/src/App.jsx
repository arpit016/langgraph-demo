import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, runAgent } from "./agui.js";
import { artifactCard, buildMessages, newAssistant } from "./messages.js";

const STEP_LABELS = {
  // live graph nodes
  flight_agent: "Searching flights",
  hotel_agent: "Finding hotels",
  itinerary_agent: "Drafting the itinerary",
  final_agent: "Writing your travel plan",
  // stored tool calls
  search_flights: "Searched flights",
  search_tavily: "Searched hotels",
  rails_build_trip: "Booked the trip",
};

// Pure reducer: apply one AG-UI event to the assistant message
function applyEvent(msg, ev) {
  switch (ev.type) {
    case "STEP_STARTED":
      return { ...msg, steps: [...msg.steps, { name: ev.stepName, status: "running", detail: null }] };
    case "STEP_FINISHED":
      return {
        ...msg,
        steps: msg.steps.map((s) => (s.name === ev.stepName ? { ...s, status: "done" } : s)),
      };
    case "CUSTOM":
      if (ev.name === "step_detail") {
        return {
          ...msg,
          steps: msg.steps.map((s) => (s.name === ev.value.step ? { ...s, detail: ev.value.detail } : s)),
        };
      }
      if (ev.name === "persisted") {
        const a = ev.value.artifact;
        return {
          ...msg,
          eventId: ev.value.assistant_event_id,
          artifacts: [...msg.artifacts, { ...a, isCurrent: true }],
        };
      }
      return msg;
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
  const [open, setOpen] = useState(!msg.loaded);
  useEffect(() => {
    if (streamingAnswer && !msg.loaded) setOpen(false);
  }, [streamingAnswer, msg.loaded]);

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
    : "Tools used";

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
            {msg.steps.map((s, i) => (
              <li key={s.callId || s.name + i} className={s.status}>
                <span className="icon">{s.status === "done" ? "✓" : "○"}</span>
                <div>
                  <div>{STEP_LABELS[s.name] || s.name}</div>
                  {s.detail && <div className="detail">{s.detail}</div>}
                </div>
              </li>
            ))}
          </ul>
          {msg.reasoning && (
            <pre className="draft" ref={draftRef}>
              {msg.reasoning}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function ArtifactCard({ card, onBuild }) {
  const status = card.build_status;
  const ids = card.build_results?.booking_ids;
  return (
    <div className={`card ${card.isCurrent ? "" : "old"}`}>
      <div className="card-head">
        <strong>Trip plan · v{card.version_number}</strong>
        {!card.isCurrent && <span className="badge">older version</span>}
        {status && <span className={`badge ${status}`}>{status}</span>}
      </div>
      {card.query && <div className="card-query">{card.query}</div>}
      {ids && <div className="detail">Booking IDs: {ids.join(", ")}</div>}
      {status === "failed" && card.build_results?.error && (
        <div className="error">{card.build_results.error.message}</div>
      )}
      <button
        className="build"
        disabled={status === "built" || status === "building"}
        onClick={() => onBuild(card)}
      >
        {status === "built" ? "Booked" : status === "failed" ? "Retry build" : "Build"}
      </button>
    </div>
  );
}

function Message({ msg, onBuild, onRate }) {
  if (msg.role === "user") {
    return <div className={`msg user ${msg.isAction ? "action" : ""}`}>{msg.content}</div>;
  }
  return (
    <div className="msg assistant">
      <Thinking msg={msg} />
      {msg.content && (
        <div className="answer">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          {msg.stopped && <div className="detail">(stopped early)</div>}
        </div>
      )}
      {msg.artifacts.map((card) => (
        <ArtifactCard key={card.version_id} card={card} onBuild={onBuild} />
      ))}
      {msg.eventId && msg.status === "done" && (
        <div className="rate">
          <button className={msg.rating === "up" ? "on" : ""} onClick={() => onRate(msg, "up")}>
            👍
          </button>
          <button className={msg.rating === "down" ? "on" : ""} onClick={() => onRate(msg, "down")}>
            👎
          </button>
        </div>
      )}
      {msg.error && <div className="error">Something went wrong: {msg.error}</div>}
    </div>
  );
}

export default function App() {
  const [employees, setEmployees] = useState([]);
  const [employeeId, setEmployeeId] = useState(() => {
    try {
      return Number(localStorage.getItem("employeeId")) || null;
    } catch {
      return null;
    }
  });
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef(null);

  const refreshList = useCallback(
    (id = employeeId) => (id ? api("/conversations", id).then(setConversations) : null),
    [employeeId]
  );

  const openConversation = useCallback(
    async (id, emp = employeeId) => {
      const data = await api(`/conversations/${id}`, emp);
      setActiveId(id);
      setMessages(buildMessages(data.events, data.artifacts, data.feedback));
    },
    [employeeId]
  );

  useEffect(() => {
    fetch("/employees")
      .then((r) => r.json())
      .then((list) => {
        setEmployees(list);
        setEmployeeId((cur) => (list.some((e) => e.employee_id === cur) ? cur : list[0]?.employee_id));
      });
  }, []);

  useEffect(() => {
    if (!employeeId) return;
    try {
      localStorage.setItem("employeeId", String(employeeId));
    } catch {}
    setActiveId(null);
    setMessages([]);
    refreshList(employeeId);
  }, [employeeId]); // eslint-disable-line

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

    const update = (fn) => setMessages((m) => m.map((x) => (x.id === assistant.id ? fn(x) : x)));

    try {
      await runAgent({
        message: text,
        conversationId: activeId,
        employeeId,
        onEvent: (ev) => {
          if (ev.type === "RUN_STARTED") setActiveId(ev.threadId);
          update((msg) => applyEvent(msg, ev));
        },
      });
    } catch (err) {
      update((msg) => ({ ...msg, status: "error", error: err.message }));
    } finally {
      update((msg) => (msg.status === "running" ? { ...msg, status: "done" } : msg));
      setBusy(false);
      refreshList();
    }
  }

  async function onBuild(card) {
    if (busy) return;
    setBusy(true);
    try {
      await api(
        `/conversations/${activeId}/artifacts/${card.artifact_id}/versions/${card.version_id}/build`,
        employeeId,
        { method: "POST" }
      );
      await openConversation(activeId);
      refreshList();
    } finally {
      setBusy(false);
    }
  }

  async function onRate(msg, rating) {
    await api(`/conversations/${activeId}/events/${msg.eventId}/feedback`, employeeId, {
      method: "POST",
      body: JSON.stringify({ rating }),
    });
    setMessages((m) => m.map((x) => (x.id === msg.id ? { ...x, rating } : x)));
  }

  async function remove(id) {
    await api(`/conversations/${id}`, employeeId, { method: "DELETE" });
    if (id === activeId) {
      setActiveId(null);
      setMessages([]);
    }
    refreshList();
  }

  const current = employees.find((e) => e.employee_id === employeeId);

  return (
    <div className="layout">
      <aside>
        <div className="who">
          <label>Signed in as</label>
          <select
            value={employeeId || ""}
            onChange={(e) => setEmployeeId(Number(e.target.value))}
            disabled={busy}
          >
            {employees.map((e) => (
              <option key={e.employee_id} value={e.employee_id}>
                {e.name} · {e.account}
              </option>
            ))}
          </select>
          {current && (
            <div className="detail">
              account {current.account_id} · employee {current.employee_id}
            </div>
          )}
        </div>
        <button
          className="new"
          disabled={busy}
          onClick={() => {
            setActiveId(null);
            setMessages([]);
          }}
        >
          + New chat
        </button>
        <ul className="history">
          {conversations.map((c) => (
            <li key={c.id} className={c.id === activeId ? "active" : ""}>
              <button className="title" disabled={busy} onClick={() => openConversation(c.id)}>
                {c.title || "Untitled"}
              </button>
              <button className="del" title="Delete" disabled={busy} onClick={() => remove(c.id)}>
                ×
              </button>
            </li>
          ))}
          {conversations.length === 0 && <li className="detail">No conversations yet</li>}
        </ul>
      </aside>

      <div className="app">
        <header>
          <h1>✈️ TripMate</h1>
        </header>

        <main>
          {messages.length === 0 && (
            <div className="empty">
              <p>Where do you want to go?</p>
              <p className="hint">Try: “Plan a 3 day trip from Delhi to Goa”</p>
            </div>
          )}
          {messages.map((m) => (
            <Message key={m.id} msg={m} onBuild={onBuild} onRate={onRate} />
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
    </div>
  );
}
