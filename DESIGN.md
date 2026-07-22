# Corvus Visual System

## Theme

Corvus is used on a developer's desktop during focused work, often in a dim environment. Dark mode is the primary demo surface, with an equally functional light mode.

## Color

Use a restrained palette of slightly blue-tinted neutrals with cyan reserved for primary actions, current selection, and verified information. The central work surface is the darkest layer; the navigation sidebar is one step lighter. Error, warning, and success states use distinct semantic colors plus text or icons.

## Typography

Use Inter Tight for interface text and IBM Plex Mono for identifiers and machine state. Fraunces is limited to rare editorial moments outside dense product controls; product-page headings use the interface family with compact fixed sizing.

## Layout

Use a fixed desktop app shell that fits the viewport. The shell itself never scrolls; only the active content region or deliberately scrollable transcript/list may scroll. Settings replaces the application navigation with its own category sidebar and a clear Back to app action.

## Components

Controls use a consistent 7px radius, visible focus rings, restrained borders, and explicit disabled/loading/error states. Lists and tables are preferred for repositories, runs, schedules, and skills. Empty states include one clear next action when the action is available.

## Motion

Keep state transitions between 150 and 220ms with ease-out timing. Respect reduced motion. Do not animate layout or add decorative page-load sequences.
