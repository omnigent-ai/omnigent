import { afterEach, describe, expect, it } from "vitest";

import { lightboxGallery, steppedGallery, withoutImage } from "./lightboxGallery";

afterEach(() => {
  document.body.innerHTML = "";
});

function trigger(src: string): Element {
  return document.querySelector(`[data-lightbox-src="${src}"]`)!;
}

describe("lightboxGallery", () => {
  it("lists a gallery's images in reading order, leaving out a nested gallery's and linked ones", () => {
    document.body.innerHTML = `
      <section data-lightbox-gallery>
        <button data-lightbox-src="/one.png" data-lightbox-alt="one"></button>
        <div data-lightbox-gallery><button data-lightbox-src="/nested.png"></button></div>
        <a href="https://example.com"><button data-lightbox-src="/badge.svg"></button></a>
        <button data-lightbox-src="/two.png" data-lightbox-alt="two"></button>
      </section>`;
    expect(lightboxGallery(trigger("/two.png"), { src: "/two.png", alt: "two" })).toEqual({
      images: [
        { src: "/one.png", alt: "one" },
        { src: "/two.png", alt: "two" },
      ],
      index: 1,
    });
    expect(lightboxGallery(trigger("/nested.png"), { src: "/nested.png", alt: "" })).toEqual({
      images: [{ src: "/nested.png", alt: "" }],
      index: 0,
    });
  });

  it("opens an image alone outside a gallery", () => {
    document.body.innerHTML = `<button data-lightbox-src="/alone.png"></button>`;
    expect(lightboxGallery(trigger("/alone.png"), { src: "/alone.png", alt: "" })).toEqual({
      images: [{ src: "/alone.png", alt: "" }],
      index: 0,
    });
  });
});

describe("moving through a gallery", () => {
  const gallery = {
    images: [
      { src: "/a.png", alt: "" },
      { src: "/b.png", alt: "" },
      { src: "/c.png", alt: "" },
    ],
    index: 1,
  };

  it("steps one image at a time and stops at either end", () => {
    expect(steppedGallery(gallery, 1).index).toBe(2);
    expect(steppedGallery(steppedGallery(gallery, 1), 1).index).toBe(2);
    expect(steppedGallery(steppedGallery(gallery, -1), -1).index).toBe(0);
  });

  it("drops an image that went away and keeps the shown one, or closes when it was shown", () => {
    expect(withoutImage(gallery, "/a.png")).toEqual({ images: gallery.images.slice(1), index: 0 });
    expect(withoutImage(gallery, "/b.png")).toBeNull();
  });
});
