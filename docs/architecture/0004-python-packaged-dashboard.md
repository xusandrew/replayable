# ADR 0004: Python-packaged dashboard

Status: accepted

The dashboard is a Vite-built React single-page application served by the
loopback Python HTTP process. Production does not use a Node server, SSR,
React Server Components, or framework routing. The wheel contains one HTML
entry point and hashed CSS/JavaScript assets.

API requests are same-origin in production. Vite supplies a loopback proxy for
development. Hashed assets are immutable-cached, HTML is not cached, and the
content-security policy permits only self-hosted scripts. Tailwind utility
scanning is disabled because the UI uses semantic component classes.
