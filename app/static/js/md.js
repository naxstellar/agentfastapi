// Markdown -> sanitized HTML. marked and DOMPurify are vendored (static/vendor), loaded as classic scripts.
const { marked, DOMPurify } = window;

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

export function renderMarkdown(text) {
  const html = marked.parse(text, { gfm: true, breaks: true });
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
