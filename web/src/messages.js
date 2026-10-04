// Turns stored conversation events (agents.conversation_events) into chat messages.
//
// Events are grouped by their root: a user `text` or `action` event starts an exchange, and every
// event generated for it carries reply_to_id = that root.

export const newAssistant = () => ({
  id: crypto.randomUUID(),
  role: "assistant",
  steps: [], // { name, status: "running" | "done", detail }
  reasoning: "",
  content: "",
  artifacts: [], // trip plan cards
  eventId: null, // the stored assistant text event (feedback target)
  rating: null,
  status: "running", // running | done | error
  startedAt: Date.now(),
  thoughtFor: null,
  error: null,
  stopped: false,
  loaded: false,
});

const firstLine = (s) => String(s || "").trim().split("\n")[0];

function resultDetail(data) {
  if (data?.error) return data.error.message;
  const r = data?.result;
  if (typeof r === "string") return firstLine(r);
  if (r?.booking_ids) return r.booking_ids.join(", ");
  return null;
}

export function artifactCard(artifact, version, eventId) {
  return {
    artifact_id: artifact.id,
    version_id: version.id,
    version_number: version.version_number,
    event_id: eventId,
    build_status: version.build_status,
    build_results: version.build_results,
    query: version.data?.query,
    isCurrent: artifact.current_version_id === version.id,
  };
}

export function buildMessages(events, artifacts, feedback) {
  const versions = {};
  artifacts.forEach((a) => a.versions.forEach((v) => (versions[v.id] = { a, v })));

  const out = [];
  const turns = new Map();

  for (const e of events) {
    if (e.role === "user" && !e.reply_to_id) {
      if (e.type === "text") {
        out.push({ id: e.id, role: "user", content: e.content });
      } else if (e.type === "action") {
        const hit = versions[e.data?.artifact_version_id];
        out.push({
          id: e.id,
          role: "user",
          isAction: true,
          content: `Build trip plan${hit ? ` v${hit.v.version_number}` : ""}`,
        });
      }
      const am = { ...newAssistant(), id: `a-${e.id}`, status: "done", loaded: true };
      turns.set(e.id, am);
      out.push(am);
      continue;
    }

    const am = turns.get(e.reply_to_id);
    if (!am) continue;
    switch (e.type) {
      case "tool_call":
        am.steps.push({ name: e.data.name, status: "done", detail: null, callId: e.id });
        break;
      case "tool_result": {
        const step = am.steps.find((s) => s.callId === e.parent_id);
        if (step) step.detail = resultDetail(e.data);
        break;
      }
      case "text":
        am.content += e.content || "";
        am.eventId = e.id;
        am.rating = feedback[e.id] || null;
        if (e.status === "cancelled") am.stopped = true;
        break;
      case "artifact": {
        const hit = versions[e.data?.artifact_version_id];
        if (hit) am.artifacts.push(artifactCard(hit.a, hit.v, e.id));
        break;
      }
      case "error":
        am.error = e.data?.message || "Error";
        am.status = "error";
        break;
      default:
    }
  }

  return out.filter(
    (m) => m.role === "user" || m.steps.length || m.content || m.artifacts.length || m.error
  );
}
