import { useState, useRef, useEffect } from "react";
import MarkdownView from "../components/MarkdownView";

export default function ChatPage() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(scrollToBottom, [messages]);
  useEffect(() => inputRef.current?.focus(), []);

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setLoading(true);

    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let botText = "";
      let buffer = "";

      // Add empty bot message
      setMessages((prev) => [...prev, { role: "bot", text: "" }]);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (!raw) continue;

          try {
            const data = JSON.parse(raw);
            if (data.type === "session") {
              setSessionId(data.session_id);
            } else if (data.type === "text" || data.type === "delta") {
              botText += data.text;
              setMessages((prev) => {
                const updated = [...prev];
                updated[updated.length - 1] = { role: "bot", text: botText };
                return updated;
              });
            }
          } catch {
            // skip invalid JSON
          }
        }
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "bot", text: `Error: ${err.message}` },
      ]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <main className="main-content full-width">
      <div className="chat-layout">
        <div className="chat-header">
          <h2>Codebase Q&A</h2>
          <p>Ask questions about the codebase. Powered by Vertex AI Search + Gemini.</p>
        </div>

        <div className="chat-messages">
          {messages.length === 0 && (
            <div className="empty-state">
              <div className="icon">&#128269;</div>
              <h3>Ask anything about the code</h3>
              <p>
                This agent searches the indexed documentation to answer questions
                about architecture, components, data flows, and more.
              </p>
            </div>
          )}

          {messages.map((msg, i) => (
            <div key={i} className={`chat-message ${msg.role}`}>
              <div className="chat-avatar">
                {msg.role === "user" ? "U" : "AI"}
              </div>
              <div className="chat-bubble">
                {msg.role === "bot" ? (
                  <MarkdownView content={msg.text || "..."} />
                ) : (
                  msg.text
                )}
              </div>
            </div>
          ))}

          {loading && messages[messages.length - 1]?.role !== "bot" && (
            <div className="chat-message bot">
              <div className="chat-avatar">AI</div>
              <div className="chat-bubble">
                <div className="loading-dots">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        <div className="chat-input-area">
          <div className="chat-input-row">
            <textarea
              ref={inputRef}
              className="chat-input"
              placeholder="Ask about the codebase..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={1}
            />
            <button
              className="chat-send-btn"
              onClick={sendMessage}
              disabled={loading || !input.trim()}
            >
              Send
            </button>
          </div>
        </div>
      </div>
    </main>
  );
}
