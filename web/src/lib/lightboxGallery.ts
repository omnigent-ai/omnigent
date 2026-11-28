// The images a lightbox steps through: every zoomable image in the same gallery
// (a PR description, a comment), in reading order. An image in no gallery, or
// one that's a link, opens alone or not at all.

/** Marks an element whose zoomable images are one gallery. */
export const LIGHTBOX_GALLERY_ATTRIBUTE = "data-lightbox-gallery";
/** Spread on an element to make its images one gallery. */
export const LIGHTBOX_GALLERY_PROPS = { [LIGHTBOX_GALLERY_ATTRIBUTE]: "" } as const;
/** Carries a zoomable image's source on its trigger, so a gallery can list it. */
export const LIGHTBOX_SOURCE_ATTRIBUTE = "data-lightbox-src";
/** Carries a zoomable image's alt text on its trigger. */
export const LIGHTBOX_ALT_ATTRIBUTE = "data-lightbox-alt";

export interface LightboxImage {
  src: string;
  alt: string;
}

export interface LightboxGallery {
  images: LightboxImage[];
  /** Which of `images` to show first. */
  index: number;
}

/** Whether a click on `trigger` belongs to a link around it, which should win. */
export function insideLink(trigger: Element): boolean {
  return trigger.closest("a[href]") !== null;
}

/** The gallery a click on `trigger`, showing `image`, opens. */
export function lightboxGallery(trigger: Element, image: LightboxImage): LightboxGallery {
  const root = trigger.closest(`[${LIGHTBOX_GALLERY_ATTRIBUTE}]`);
  if (!root) return { images: [image], index: 0 };
  const triggers = Array.from(root.querySelectorAll(`[${LIGHTBOX_SOURCE_ATTRIBUTE}]`)).filter(
    (candidate) =>
      // A nested gallery keeps its own images, and a linked image is the link's.
      candidate.closest(`[${LIGHTBOX_GALLERY_ATTRIBUTE}]`) === root && !insideLink(candidate),
  );
  const index = triggers.indexOf(trigger);
  if (index < 0) return { images: [image], index: 0 };
  const images = triggers.map((candidate) => ({
    src: candidate.getAttribute(LIGHTBOX_SOURCE_ATTRIBUTE) ?? "",
    alt: candidate.getAttribute(LIGHTBOX_ALT_ATTRIBUTE) ?? "",
  }));
  images[index] = image;
  return { images, index };
}

/** The gallery after `src` stops being viewable, or null when nothing is left to show. */
export function withoutImage(gallery: LightboxGallery, src: string): LightboxGallery | null {
  if (gallery.images[gallery.index]?.src === src) return null;
  const images = gallery.images.filter((image) => image.src !== src);
  const shown = gallery.images[gallery.index];
  return { images, index: Math.max(0, images.indexOf(shown)) };
}

/** The gallery moved by `step` images, stopping at either end like Quick Look. */
export function steppedGallery(gallery: LightboxGallery, step: number): LightboxGallery {
  const index = Math.min(gallery.images.length - 1, Math.max(0, gallery.index + step));
  return index === gallery.index ? gallery : { ...gallery, index };
}
