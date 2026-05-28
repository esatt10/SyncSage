import { useState } from "react";
import type { ReactNode } from "react";
import { useExplain } from "./ExplainContext";
import { explainRegistry } from "./explainRegistry";

interface ExplainableProps {
  id: keyof typeof explainRegistry | string;
  children: ReactNode;
  as?: "div" | "span" | "section";
  className?: string;
}

// Wraps any component. When Explain mode is on, it shows a discovery outline and
// reveals a tooltip on hover describing what the component does.
export function Explainable({ id, children, as = "div", className }: ExplainableProps) {
  const { enabled } = useExplain();
  const [hovered, setHovered] = useState(false);
  const text = explainRegistry[id];
  const Tag = as;

  if (!enabled || !text) {
    return <Tag className={className}>{children}</Tag>;
  }

  return (
    <Tag
      className={`explainable${hovered ? " explainable--active" : ""}${
        className ? ` ${className}` : ""
      }`}
      data-explain={id}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {children}
      {hovered && (
        <span className="explain-tooltip" role="tooltip">
          {text}
        </span>
      )}
    </Tag>
  );
}
