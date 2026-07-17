import { useLayoutEffect, useRef } from 'react';

/**
 * Minimal FLIP animation. Runs after every commit: measures each tracked
 * element, and if it moved since the previous commit (e.g. cards re-sorted by
 * relevance, or shifted as new results streamed in) it plays a smooth transform
 * from the old position to the new one. Register elements via `register(id)`.
 */
export function useFlip() {
  const nodes = useRef<Map<string, HTMLElement>>(new Map());
  const prevRects = useRef<Map<string, DOMRect>>(new Map());

  useLayoutEffect(() => {
    const next = new Map<string, DOMRect>();
    nodes.current.forEach((el, id) => next.set(id, el.getBoundingClientRect()));

    next.forEach((rect, id) => {
      const prev = prevRects.current.get(id);
      const el = nodes.current.get(id);
      if (!prev || !el) return; // freshly mounted -> no move to animate
      const dx = prev.left - rect.left;
      const dy = prev.top - rect.top;
      if (dx === 0 && dy === 0) return;
      el.style.transition = 'transform 0s';
      el.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        el.style.transition = 'transform 0.45s cubic-bezier(0.2, 0.8, 0.2, 1)';
        el.style.transform = '';
      });
    });

    prevRects.current = next;
  });

  const register = (id: string) => (el: HTMLElement | null) => {
    if (el) nodes.current.set(id, el);
    else nodes.current.delete(id);
  };

  return register;
}
