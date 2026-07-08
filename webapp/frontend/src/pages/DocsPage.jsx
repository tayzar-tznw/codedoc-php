import { useEffect, useState } from "react";
import MarkdownView from "../components/MarkdownView";

export default function DocsPage() {
  const [doc, setDoc] = useState(null);

  useEffect(() => {
    fetch("/api/docs/index")
      .then((r) => r.json())
      .then(setDoc);
  }, []);

  if (!doc) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <p>Loading...</p>
        </div>
      </main>
    );
  }

  return (
    <main className="main-content">
      <MarkdownView content={doc.markdown} />
    </main>
  );
}
