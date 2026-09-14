---
paths:
  - "**/*.canvas"
  - "Designs/**"
---

# Obsidian Canvas — Auto-loaded Rule

When creating or editing `.canvas` files, follow `~/Operations/Prompts/commands/canvas-builder.md`.

Key rules (read the full routine for details):

## Connectors Don't Route

Obsidian draws STRAIGHT LINES between connection points. No smart routing. Lines go through anything in their path. You must design layout so lines have clear paths.

## Side Selection

The `fromSide`/`toSide` of each edge is the most important decision. The side must face the direction the line needs to travel. Calculate both connection point coordinates and trace an imaginary straight line — if it crosses any node, pick different sides or add a waypoint.

## Backward Edges

Backward edges (fail loops, retries, rejects) that span multiple groups WILL cross content if connected directly. Use a **waypoint node** in open space to split the edge into two clean segments.

## Validate Every Edge

Before writing the canvas file, trace every edge as a straight line between its two connection points. Confirm nothing sits in that path. If it crosses content, fix the layout or routing.
