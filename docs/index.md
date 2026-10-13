---
title: Documentation
---

<section class="docs-hero">
  <p class="docs-eyebrow">PHP call edges for graphify</p>
  <h1>A wrong edge is worse than a missing one.</h1>
  <p class="docs-lede">
    Bind <code>$this-&gt;emails-&gt;send()</code> to the method it calls, and
    count every call it cannot prove instead of guessing.
  </p>
  <div class="docs-actions">
    <a class="docs-action docs-action-primary" href="install.html">Install</a>
    <a class="docs-action" href="architecture.html">Explore the architecture</a>
  </div>
</section>

## Documentation

<nav class="docs-grid" aria-label="Documentation sections">
  <a class="docs-card" href="install.html">
    <span class="docs-card-label">Start here</span>
    <strong>Install</strong>
    <span>Put graphify-php into graphify's environment and check that builds load it.</span>
  </a>
  <a class="docs-card" href="setup.html">
    <span class="docs-card-label">Guide</span>
    <strong>Setup</strong>
    <span>Install, register the resolvers, and run a gated build.</span>
  </a>
  <a class="docs-card" href="configuration.html">
    <span class="docs-card-label">Reference</span>
    <strong>Configuration</strong>
    <span>Look up environment variables, wrapper flags, exit codes, and the status record.</span>
  </a>
  <a class="docs-card" href="architecture.html">
    <span class="docs-card-label">Concepts</span>
    <strong>Architecture</strong>
    <span>Understand the ports, source precedence, edge keys, and unresolved reasons.</span>
  </a>
  <a class="docs-card" href="phpstan.html">
    <span class="docs-card-label">Sources</span>
    <strong>PHPStan source</strong>
    <span>See how the collector is wired into a project's own PHPStan run.</span>
  </a>
  <a class="docs-card" href="evaluation.html">
    <span class="docs-card-label">Measurement</span>
    <strong>Evaluation</strong>
    <span>Score a resolver against hand-labelled call sites, not against its edge count.</span>
  </a>
</nav>

## What graphify-php guarantees

- An edge only where a source proves the target.
- An unresolved call counted under a fixed reason string, never guessed.
- A weaker source adds reach but never overrides a stronger one.
- No published graph when an expected resolver failed or never reported.

[View the source on GitHub](https://github.com/OxCom/graphify-php)
