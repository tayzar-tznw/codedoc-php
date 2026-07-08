import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Mermaid from "./Mermaid";

export default function MarkdownView({ content }) {
  return (
    <div className="doc-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || "");
            if (match && match[1] === "mermaid") {
              return <Mermaid chart={String(children).trim()} />;
            }
            if (match) {
              return (
                <pre>
                  <code className={className} {...props}>
                    {children}
                  </code>
                </pre>
              );
            }
            return (
              <code className={className} {...props}>
                {children}
              </code>
            );
          },
          pre({ children }) {
            // If children is a Mermaid component or already a <pre>, unwrap
            if (children?.type === Mermaid || children?.type === "pre") {
              return children;
            }
            return <pre>{children}</pre>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
