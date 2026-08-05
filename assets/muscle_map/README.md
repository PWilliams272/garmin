# Muscle map reference

`body_reference.png` is the source image the front/back muscle-map SVGs in
`garmin/prototypes/activity_explorer.py` were traced from.

**Keep it here.** An earlier version of this file lived only in a chat and was
lost, and the forearm regions on the back view had to be hand-drawn from
silhouette coordinates as a result. They looked wrong — crude polygons
spanning the arm's whole diagonal sweep instead of a muscle belly — and stayed
wrong until the image turned up again.

## Re-tracing a region

1. Isolate the region by colour in HSV (the reference highlights one muscle
   group at a time).
2. Upscale 4x with `INTER_CUBIC`, Gaussian blur, re-threshold at 127. Tracing
   at native resolution turns JPEG artefacts into polygon vertices.
3. `findContours` + `approxPolyDP` (epsilon ≈ 0.006 × arc length).
4. Map image → viewBox coordinates using the two silhouette bounding boxes,
   which is what aligns the trace with the existing paths.

Front and back figures sit side by side; split them on the image x midpoint.
