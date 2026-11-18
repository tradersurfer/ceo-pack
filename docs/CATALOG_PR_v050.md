# Catalog PR prep — ceo-pack v0.5.0 (do NOT file yet)

Adrian files this PR himself (or says go) after the v0.5.0-dev branch is pushed
and reviewed. One file changes in NousResearch/hermes-agent:
`plugin-catalog/ceo-pack.yaml`. Same route as PR #155549 (v0.4.0) and #157203
(v0.4.1): pencil-edit the file in the GitHub web UI (auto-forks), branch
`ceo-pack-0.5.0`, validate first with `scripts/validate_plugin_catalog.py`.

## The image line (the install-card thumbnail)

Layout copied from the advanced-delegation catalog entry
(`plugin-catalog/advanced-delegation.yaml` in the hermes-agent checkout):
the catalog `image:` is a `raw.githubusercontent.com` URL pointing at an
`assets/` file in the plugin repo, pinned to the entry's SHA — so the
thumbnail is as immutable as the sha pin. The validator
(`scripts/validate_plugin_catalog.py`, `_is_allowed_image_url`) only accepts
https URLs on `raw.githubusercontent.com` / `github.com` /
`*.githubusercontent.com`.

```
image: https://raw.githubusercontent.com/tradersurfer/ceo-pack/<V050_SHA>/assets/ceo-pack-cover-v040.webp
```

`<V050_SHA>` is the full 40-char HEAD of the `v0.5.0-dev` branch at push time.
Do NOT substitute the v0.4.1 sha (6331b08a) — the image must resolve at the
pinned v0.5.0 commit, and `assets/ceo-pack-cover-v040.webp` is in the
v0.5.0 tree (447,488 bytes, 1920x1280, verified present in this repo copy).

URL format verified against the validator's own rule (2026-11-18, Phase 4):
`_is_allowed_image_url` returns True for this shape with a 40-hex SHA.

## Entry changes (vs the v0.4.1 entry, PR #157203)

- `sha`: 6331b08a03f18c202a292eb8c4741b69aad69994 → `<V050_SHA>`
- `version`: `"0.4.1"` → `"0.5.0"`
- `description`: keep the v0.4.1 wording ("The agent runs on your Hermes
  model; the plugin recommends a pick per job" + hardening sentence), plus
  one v0.5.0 sentence: dashboard tab, persistent task registry, unified
  delegation.
- `image`: add the line above (new — v0.4.0/v0.4.1 entries had no image,
  which is why the install card has no thumbnail).
- `requires_hermes`: `">=0.22.5"` (already the plugin.yaml value for v0.5.0;
  confirm against the entry — v0.4.1 said `>=0.21.6`).

## Known quirk — flag for Adrian, do not silently rename

`assets/ceo-pack-cover-v040.webp` contains **JPEG bytes** (JFIF,
1920x1280), not WebP, despite the `.webp` extension. It has lived in the
repo under this name since v0.4.0 without being referenced by any catalog
entry, so nothing live breaks today. Before the catalog PR, consider
renaming it to `assets/ceo-pack-cover-v040.jpg` and using the `.jpg` URL
instead — a `.webp` extension served with JPEG bytes may fail to decode in
the Desktop catalog browser. If renamed, the `image:` line above changes
extension to match; nothing else references the file.

## Pre-flight (before filing)

1. Push `v0.5.0-dev` to GitHub; record the HEAD SHA.
2. Confirm the image URL loads in a browser at that SHA.
3. Run `scripts/validate_plugin_catalog.py` on the edited entry (rc=0).
4. File the PR; note the fork-PR checks wait for a Nous maintainer to
   approve the run (normal hold, not a rejection).
