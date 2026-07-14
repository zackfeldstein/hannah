"""Hannah Lab - the public, read-only layer around the local Hannah runtime.

Everything in this package turns Hannah's private run bundles
(research/runs/<label>/) into public-safe artifacts and a static site
(public_lab/site/). Nothing here talks to the model, controls the daemon,
or accepts input from the public: the flow is strictly

    local run bundle -> sanitizer -> public artifacts -> static site -> (push)

Modules:
    sanitizer  - redaction + fail-closed secret detection
    rundata    - parse a run folder into structured, sanitized data
    state      - fold runs into derived memories / beliefs / questions / timeline
    artifacts  - write per-run public artifact files
    site       - render the static public site
"""
