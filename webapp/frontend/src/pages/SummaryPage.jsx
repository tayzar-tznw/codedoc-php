import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import MarkdownView from "../components/MarkdownView";

export default function SummaryPage() {
  const { kind, name } = useParams();
  const [doc, setDoc] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setDoc(null);
    setError(null);
    fetch(`/api/docs/summaries/${kind}/${encodeURIComponent(name)}`)
      .then((r) => {
        if (!r.ok) throw new Error("Not found");
        return r.json();
      })
      .then(setDoc)
      .catch((e) => setError(e.message));
  }, [kind, name]);

  if (error) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <h3>Summary not found</h3>
          <p>{name}</p>
        </div>
      </main>
    );
  }

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
