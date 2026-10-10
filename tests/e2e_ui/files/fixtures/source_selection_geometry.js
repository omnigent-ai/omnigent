([headingLine, paragraphLine]) => {
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right };
  };
  const heading = document.querySelector(`[data-line="${headingLine}"]`);
  const paragraph = document.querySelector(`[data-line="${paragraphLine}"]`);
  const walker = document.createTreeWalker(heading, NodeFilter.SHOW_TEXT);
  let headingText = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.textContent.trim()) {
      headingText = node;
      break;
    }
  }
  const widthOf = (start, end) => {
    const r = document.createRange();
    r.setStart(headingText, start);
    r.setEnd(headingText, end);
    return r.getBoundingClientRect().width;
  };
  return {
    selectedText: window.getSelection().toString(),
    headingRect: rectOf(heading),
    paragraphRect: rectOf(paragraph),
    headingSpans: [...heading.querySelectorAll("span")].map((s) => ({
      text: s.textContent,
      rect: rectOf(s),
      fontFamily: getComputedStyle(s).fontFamily,
      fontVariantLigatures: getComputedStyle(s).fontVariantLigatures,
      fontFeatureSettings: getComputedStyle(s).fontFeatureSettings,
    })),
    // Laid-out width of the three leading markers vs. the space after them.
    markerWidth: headingText ? widthOf(0, 3) : null,
    cellWidth: headingText ? widthOf(3, 4) : null,
    devicePixelRatio: window.devicePixelRatio,
  };
}
