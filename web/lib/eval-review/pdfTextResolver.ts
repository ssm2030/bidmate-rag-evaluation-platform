export type NormalizedBox = {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
};

export type ViewportBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export function viewportBox(
  box: NormalizedBox,
  pageWidth: number,
  pageHeight: number,
  zoom: number,
): ViewportBox {
  if (
    box.x0 < 0 || box.y0 < 0 || box.x1 > 1 || box.y1 > 1 ||
    box.x0 >= box.x1 || box.y0 >= box.y1 ||
    pageWidth <= 0 || pageHeight <= 0 || zoom <= 0
  ) {
    throw new Error("invalid normalized highlight geometry");
  }
  return {
    left: box.x0 * pageWidth * zoom,
    top: box.y0 * pageHeight * zoom,
    width: (box.x1 - box.x0) * pageWidth * zoom,
    height: (box.y1 - box.y0) * pageHeight * zoom,
  };
}
