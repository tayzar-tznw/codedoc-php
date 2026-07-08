import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

let initialized = false;

if (!initialized) {
  mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    themeVariables: {
      primaryColor: "#24253a",
      primaryTextColor: "#c0caf5",
      primaryBorderColor: "#7aa2f7",
      lineColor: "#565f89",
      secondaryColor: "#1a1b26",
      tertiaryColor: "#292e42",
    },
  });
  initialized = true;
}

let counter = 0;

export default function Mermaid({ chart }) {
  const ref = useRef(null);
  const [svg, setSvg] = useState("");

  useEffect(() => {
    const id = `mermaid-${++counter}`;
    let cancelled = false;

    mermaid.render(id, chart).then(({ svg }) => {
      if (!cancelled) setSvg(svg);
    }).catch(() => {
      if (!cancelled) setSvg(`<pre style="color:#f7768e">${chart}</pre>`);
    });

    return () => { cancelled = true; };
  }, [chart]);

  return (
    <div
      className="mermaid"
      ref={ref}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
