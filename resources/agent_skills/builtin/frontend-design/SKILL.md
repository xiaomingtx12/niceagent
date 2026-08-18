---
name: frontend-design
description: Design and implement distinctive, production-ready web interfaces with intentional visual direction, responsive layout, accessibility, and polished interaction states.
allowed-tools:
  - read_file
  - write_file
  - edit_file
  - glob
  - grep
metadata:
  my_dify:
    version: 1.0.0
    category: design
    execution_mode: inline
---

# Frontend Design

Use this skill when creating or substantially reshaping a web page, dashboard,
component, or frontend application.

## Workflow

1. Identify the user, task, content hierarchy, and technical constraints before
   writing markup.
2. Choose one clear visual direction. Decide the typography pairing, color
   system, spacing scale, surface treatment, and interaction character together.
3. Build the actual usable experience first. Keep important content visible in
   the first viewport and avoid placeholder sections.
4. Make responsive behavior explicit for narrow and wide viewports. Keep text
   inside its container and preserve stable dimensions for controls and grids.
5. Implement loading, empty, error, hover, focus, disabled, and success states.
6. Prefer the existing component and icon system. Do not introduce a second
   design language without a clear reason.
7. Review the result for accessibility, keyboard navigation, contrast, semantic
   structure, and visual consistency before finishing.

Avoid generic template composition, unexplained gradients, decorative elements
that compete with the task, and layouts that look like a marketing landing page
when the user needs an operational tool.
