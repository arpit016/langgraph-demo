// Minimal AG-UI client: POST a message, read the Server-Sent Events stream,
// and call onEvent(event) for every event the backend emits.
export async function runAgent({ message, conversationId, employeeId, onEvent, signal }) {
  const res = await fetch("/agent", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Employee-Id": String(employeeId) },
    body: JSON.stringify({ message, conversation_id: conversationId }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE messages are separated by a blank line
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      const data = part
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (data) onEvent(JSON.parse(data));
    }
  }
}

// Plain JSON calls (history, build, feedback, ...) always carry the employee header
export async function api(path, employeeId, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Employee-Id": String(employeeId),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`${options.method || "GET"} ${path} failed (${res.status})`);
  return res.json();
}
